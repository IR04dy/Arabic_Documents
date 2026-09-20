#!/usr/bin/env python3
"""Extract Arabic name/meaning entries from the two scanned dictionary PDFs.

The pipeline is deliberately local and deterministic:
PDF -> grayscale page images -> Tesseract TSV -> layout-aware entries -> JSON.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCUMENTS = (
    {
        "id": "arabic_names_comprehensive",
        "filename": "arabic12805.pdf",
        "title": "قاموس الأسماء العربية: دراسة شاملة للأسماء العربية ومعانيها",
        "layout": "single_column",
        "psm": 3,
        "gender_sections": (
            (29, 98, "male", "أسماء الذكور"),
            (99, 150, "female", "أسماء الإناث"),
            (151, 180, "unisex", "أسماء مشتركة بين الذكور والإناث"),
            (181, 182, "male", "أسماء مذكرة مضافة إلى ياء النسبة"),
            (183, 186, "unisex", "أسماء أعجمية للأبناء والبنات"),
        ),
        # These two section headings occur in the middle of a page.
        "gender_page_splits": {
            181: (500, "unisex", "أسماء ذكور وإناث في العهد التركي",
                  "male", "أسماء مذكرة مضافة إلى ياء النسبة"),
            183: (905, "male", "أسماء مذكرة مضافة إلى ياء النسبة",
                  "unisex", "أسماء أعجمية للأبناء والبنات"),
        },
    },
    {
        "id": "arabic_and_arabized_names",
        "filename": "قاموس_الأسماء_العربية_والمعربة_وتفسير_معانيها.pdf",
        "title": "قاموس الأسماء العربية والمعربة وتفسير معانيها",
        "layout": "two_column",
        "psm": 4,
        "gender_sections": (
            (26, 70, "male", "القسم الأول: أسماء الذكور"),
            (71, 106, "female", "القسم الثاني: أسماء الإناث"),
            (107, 120, "unisex", "القسم الثالث: الأسماء المشتركة بين الذكور والإناث"),
        ),
    },
)

ARABIC_LETTER = r"\u0621-\u064A\u066E-\u06D3\u06FA-\u06FF"
ARABIC_MARK = r"\u064B-\u065F\u0670\u06D6-\u06ED"
ARABIC_TOKEN = rf"[{ARABIC_LETTER}{ARABIC_MARK}]+"
ENTRY_START_RE = re.compile(
    rf"(?:^|(?<=[.۔؟!|]))[\s\d٠-٩۰-۹]{{0,6}}"
    rf"(?P<name>{ARABIC_TOKEN}(?:\s+{ARABIC_TOKEN}){{0,3}})\s*[:؛]"
)
ARABIC_LETTER_RE = re.compile(rf"[{ARABIC_LETTER}]")
DIACRITICS_RE = re.compile(rf"[{ARABIC_MARK}]")
BIDI_RE = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
SPACE_RE = re.compile(r"\s+")
EDGE_NOISE_RE = re.compile(r"^[\s\d٠-٩۰-۹|/\\\-_=+*•·]+|[\s|/\\\-_=+*•·]+$")
REJECTED_NAMES = {
    "المعنى", "معناه", "ملاحظة", "مثال", "مثلا", "قال", "وقال", "أي", "هو",
    "هي", "اسم", "الأسماء", "الفصل", "الباب", "المصدر", "المرجع",
}
REJECTED_NAME_TOKENS = {
    "في", "من", "إلى", "على", "عن", "مع", "عند", "بين", "ثم", "الذي", "التي",
    "هو", "هي", "هذا", "هذه", "كل", "غير", "بعد", "قبل", "أخوته", "اخوته",
    "اشتهرت", "منها", "أطرافها", "تابعي", "الرماح", "فنسبت", "وقيل", "المختلفة",
    "الروائح", "العطرة", "ذكر", "الحبارى", "وشعراء", "وصحابيين", "وأمراء",
    "وعليه", "عليه", "قول", "الشاعر", "وكاتب", "مؤرخ", "وفقيه", "ولقب",
    "ومحكم", "الأمر", "الشيء", "ومنه", "الأعشى", "ونبات", "الرائحة",
}


def run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_commands() -> None:
    missing = [name for name in ("pdfinfo", "pdftoppm", "tesseract", "sips") if not shutil.which(name)]
    if missing:
        raise SystemExit(f"Missing required command(s): {', '.join(missing)}")
    languages = run(["tesseract", "--list-langs"]).stdout.splitlines()
    if "ara" not in languages:
        raise SystemExit("Tesseract Arabic data is missing (language code: ara).")


def pdf_page_count(pdf: Path) -> int:
    output = run(["pdfinfo", str(pdf)]).stdout
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Unable to read page count from {pdf}")
    return int(match.group(1))


def render_document(pdf: Path, output_dir: Path, dpi: int, expected_pages: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(output_dir.glob("page-*.png"))
    if len(existing) != expected_pages:
        for stale in existing:
            stale.unlink()
        run(
            ["pdftoppm", "-r", str(dpi), "-gray", "-png", str(pdf), str(output_dir / "page")],
            capture=False,
        )
    pages = sorted(output_dir.glob("page-*.png"))
    if len(pages) != expected_pages:
        raise RuntimeError(f"Rendered {len(pages)} pages for {pdf}; expected {expected_pages}")
    return pages


def parse_tsv(tsv: str) -> list[dict]:
    groups: dict[tuple[int, int, int], dict] = {}
    reader = csv.DictReader(tsv.splitlines(), delimiter="\t")
    for row in reader:
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        group = groups.setdefault(key, {"words": [], "confidence": [], "boxes": []})
        group["words"].append((int(row["word_num"]), row["text"].strip()))
        confidence = float(row["conf"])
        if confidence >= 0:
            group["confidence"].append(confidence)
        group["boxes"].append(
            (int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]))
        )

    lines = []
    for order, group in groups.items():
        words = " ".join(text for _, text in sorted(group["words"]))
        left = min(box[0] for box in group["boxes"])
        top = min(box[1] for box in group["boxes"])
        right = max(box[0] + box[2] for box in group["boxes"])
        bottom = max(box[1] + box[3] for box in group["boxes"])
        lines.append(
            {
                "text": clean_text(words),
                "confidence": mean(group["confidence"]) / 100 if group["confidence"] else 0.0,
                "bbox": [left, top, right - left, bottom - top],
                "order": order,
            }
        )
    return sorted(lines, key=lambda item: item["order"])


def ocr_image(image: Path, psm: int) -> list[dict]:
    return ocr_image_with_model(image, psm, None)


def ocr_image_with_model(image: Path, psm: int, tessdata_dir: Path | None) -> list[dict]:
    command = ["tesseract", str(image), "stdout"]
    if tessdata_dir is not None:
        command.extend(["--tessdata-dir", str(tessdata_dir)])
    command.extend(["-l", "ara", "--psm", str(psm), "tsv"])
    completed = run(command)
    return parse_tsv(completed.stdout)


def image_dimensions(image: Path) -> tuple[int, int]:
    output = run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(image)]).stdout
    width_match = re.search(r"pixelWidth:\s*(\d+)", output)
    height_match = re.search(r"pixelHeight:\s*(\d+)", output)
    if not width_match or not height_match:
        raise RuntimeError(f"Unable to determine image dimensions for {image}")
    return int(width_match.group(1)), int(height_match.group(1))


def crop_column(image: Path, destination: Path, side: str, dimensions: tuple[int, int]) -> None:
    width, height = dimensions
    crop_width = int(width * 0.48)
    horizontal_offset = 1 if side == "left" else width - crop_width
    shutil.copy2(image, destination)
    run(
        [
            "sips", "-c", str(height), str(crop_width),
            "--cropOffset", "1", str(horizontal_offset), str(destination),
        ]
    )


def clean_text(value: str) -> str:
    value = BIDI_RE.sub("", value)
    value = value.replace("ـ", "")
    value = SPACE_RE.sub(" ", value)
    return value.strip()


def normalize_name(value: str, *, search: bool = False) -> str:
    value = clean_text(unicodedata.normalize("NFC", value))
    value = DIACRITICS_RE.sub("", value)
    value = re.sub(r"[^\u0621-\u064A\u066E-\u06D3\u06FA-\u06FF ]", "", value)
    value = SPACE_RE.sub(" ", value).strip()
    if search:
        value = value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"}))
    return value


def canonical_dedup_key(value: str) -> str:
    """Return a conservative key for cross-source spelling deduplication.

    Arabic dictionaries and OCR differ frequently on an initial alef/hamza and
    on whether a compound name contains a space. Other letters are deliberately
    left untouched because folds such as taa marbuta->haa or yaa->alif maqsura
    can collapse genuinely different names.
    """
    value = normalize_name(value).replace(" ", "")
    if value.startswith(("أ", "إ", "آ", "ٱ")):
        value = "ا" + value[1:]
    return value


def clean_name_display(value: str) -> str:
    """Remove isolated OCR/border glyphs without rewriting Arabic spelling."""
    value = clean_text(value)
    tokens = value.split()
    while len(tokens) > 1 and len(normalize_name(tokens[0])) <= 1:
        tokens.pop(0)
    return " ".join(tokens)


def plausible_name(value: str) -> bool:
    normalized = normalize_name(value)
    letters = ARABIC_LETTER_RE.findall(normalized)
    tokens = normalized.split()
    if not 2 <= len(letters) <= 28 or not 1 <= len(tokens) <= 4:
        return False
    if normalized in REJECTED_NAMES or any(token in REJECTED_NAMES for token in tokens):
        return False
    if any(token in REJECTED_NAME_TOKENS for token in tokens):
        return False
    if len(tokens) >= 3 and not any(
        token == "أو" or (index > 0 and token.startswith("و"))
        for index, token in enumerate(tokens)
    ):
        return False
    return True


def meaningful_text(value: str) -> bool:
    return len(ARABIC_LETTER_RE.findall(value)) >= 2


def split_entry_starts(text: str) -> list[tuple[str, str]]:
    text = clean_text(text)
    text = EDGE_NOISE_RE.sub("", text)
    matches = [match for match in ENTRY_START_RE.finditer(text) if plausible_name(match.group("name"))]
    results = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start("name") if index + 1 < len(matches) else len(text)
        name = clean_name_display(match.group("name"))
        meaning = clean_text(text[match.end():end]).strip(" .،,؛|-/")
        results.append((name, meaning))
    return results


def extract_entries(lines: list[dict], *, allow_continuations: bool = True) -> list[dict]:
    entries: list[dict] = []
    current: dict | None = None

    def finish() -> None:
        nonlocal current
        if current and meaningful_text(current["meaning_raw"]):
            current["meaning_raw"] = clean_text(current["meaning_raw"]).strip(" .،,؛|-/")
            entries.append(current)
        current = None

    for line in lines:
        text = line["text"]
        # Guard against rare malformed TSV rows being folded into one OCR line.
        if len(text) > 300 or len(re.findall(r"\b5\s+1\s+\d", text)) >= 2:
            continue
        starts = split_entry_starts(text)
        if starts:
            finish()
            for index, (name, meaning) in enumerate(starts):
                candidate = {
                    "name_raw": name,
                    "meaning_raw": meaning,
                    "ocr_confidence": line["confidence"],
                    "bbox": line["bbox"],
                }
                if index + 1 < len(starts):
                    if meaningful_text(meaning):
                        entries.append(candidate)
                else:
                    current = candidate
        elif allow_continuations and current and meaningful_text(text):
            stripped = EDGE_NOISE_RE.sub("", text)
            if meaningful_text(stripped) and len(stripped) < 240:
                current["meaning_raw"] += " " + stripped
                current["ocr_confidence"] = min(current["ocr_confidence"], line["confidence"])
    finish()
    return entries


def infer_dictionary_range(entries_by_page: dict[int, list[dict]], page_count: int) -> tuple[int, int]:
    # OCR workers finish out of order, so page numbers must be sorted before
    # contiguous dictionary sections are inferred.
    dense = sorted(page for page, entries in entries_by_page.items() if len(entries) >= 4)
    if not dense:
        raise RuntimeError("No dictionary-like pages were detected")

    groups: list[list[int]] = [[dense[0]]]
    for page in dense[1:]:
        # Permit title/section-divider pages between male and female names.
        if page - groups[-1][-1] <= 4:
            groups[-1].append(page)
        else:
            groups.append([page])
    best = max(groups, key=lambda pages: sum(len(entries_by_page[p]) for p in pages))
    return max(1, best[0] - 1), min(page_count, best[-1] + 1)


def ocr_pages(
    pages: list[Path], psm: int, workers: int, cache_file: Path, tessdata_dir: Path | None
) -> dict[int, list[dict]]:
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if len(data) == len(pages):
            return {int(page): lines for page, lines in data.items()}

    results: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(ocr_image_with_model, image, psm, tessdata_dir): index
            for index, image in enumerate(pages, 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            results[page] = future.result()
            if page % 25 == 0:
                print(f"OCR page {page}/{len(pages)}", flush=True)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    return results


def ocr_two_columns(
    pages: list[Path], start: int, end: int, workers: int, cache_file: Path,
    tessdata_dir: Path | None,
) -> dict[int, dict[str, list[dict]]]:
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        expected = end - start + 1
        if len(data) == expected:
            return {int(page): columns for page, columns in data.items()}

    dimensions = image_dimensions(pages[start - 1])

    def process(page_number: int) -> tuple[int, dict[str, list[dict]]]:
        with tempfile.TemporaryDirectory(prefix="arabic_names_") as temporary:
            temp = Path(temporary)
            columns = {}
            for side in ("right", "left"):
                cropped = temp / f"{side}.png"
                crop_column(pages[page_number - 1], cropped, side, dimensions)
                columns[side] = ocr_image_with_model(cropped, 6, tessdata_dir)
            return page_number, columns

    results: dict[int, dict[str, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process, page): page for page in range(start, end + 1)}
        for future in as_completed(futures):
            page, columns = future.result()
            results[page] = columns
            if (page - start + 1) % 20 == 0:
                print(f"Column OCR page {page}/{end}", flush=True)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    return results


def add_provenance(
    entries: list[dict], document: dict, page: int, column: str
) -> list[dict]:
    enriched = []
    for entry in entries:
        name_raw = clean_name_display(entry["name_raw"])
        meaning_raw = clean_text(entry["meaning_raw"])
        dedup_key = normalize_name(name_raw)
        if not dedup_key or not meaningful_text(meaning_raw):
            continue
        confidence = round(float(entry["ocr_confidence"]), 4)
        gender, gender_source, gender_section = classify_gender(
            meaning_raw, document, page, entry.get("bbox")
        )
        enriched.append(
            {
                "name_raw": name_raw,
                "name_normalized": normalize_name(name_raw, search=True),
                "dedup_key": dedup_key,
                "meaning": meaning_raw,
                "source_document": document["id"],
                "source_pdf": document["filename"],
                "pdf_page": page,
                "column": column,
                "ocr_confidence": confidence,
                "gender": gender,
                "gender_source": gender_source,
                "gender_section": gender_section,
                "bbox": entry.get("bbox"),
                "contextual_initial_repair": False,
                "needs_review": (
                    confidence < 0.62
                    or dedup_key.startswith(("ؤ", "ئ"))
                    or len(dedup_key.replace(" ", "")) <= 2
                ),
            }
        )
    return enriched


def classify_gender(
    meaning: str, document: dict, page: int, bbox: list[int] | None
) -> tuple[str, str, str | None]:
    """Classify an entry using explicit wording, then the book's section."""
    text = DIACRITICS_RE.sub("", meaning).replace("ـ", "")
    explicit_unisex = (
        r"اسم(?:\s+علم)?\s+مشترك",
        r"للذكور\s+وال[إا]ناث",
        r"للمذكر\s+والمؤنث",
        r"للذكر\s+وال[أا]نثى",
    )
    explicit_female = (
        r"اسم(?:\s+علم)?\s+مؤنث",
        r"اسم(?:\s+علم)?\s+لل[إا]ناث",
    )
    explicit_male = (
        r"اسم(?:\s+علم)?\s+مذكر",
        r"اسم(?:\s+علم)?\s+للذكور",
    )
    if any(re.search(pattern, text) for pattern in explicit_unisex):
        return "unisex", "explicit_description", None
    if any(re.search(pattern, text) for pattern in explicit_female):
        return "female", "explicit_description", None
    if any(re.search(pattern, text) for pattern in explicit_male):
        return "male", "explicit_description", None

    split = document.get("gender_page_splits", {}).get(page)
    if split and bbox:
        split_y, before_gender, before_label, after_gender, after_label = split
        if bbox[1] < split_y:
            return before_gender, "document_section", before_label
        return after_gender, "document_section", after_label

    for start, end, gender, label in document.get("gender_sections", ()):
        if start <= page <= end:
            return gender, "document_section", label
    return "unknown", "unclassified", None


def repair_contextual_initials(entries: list[dict]) -> None:
    """Repair only impossible initial hamzas supported by alphabetic neighbors."""
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for entry in entries:
        groups[(entry["pdf_page"], entry["column"])].append(entry)

    for group in groups.values():
        group.sort(key=lambda item: (item.get("bbox") or [0, 0])[1])
        for index, entry in enumerate(group):
            key = entry["dedup_key"]
            if not key.startswith(("ؤ", "ئ")):
                continue

            before = [
                candidate["dedup_key"][0]
                for candidate in group[max(0, index - 2):index]
                if candidate["dedup_key"] and not candidate["dedup_key"].startswith(("ؤ", "ئ"))
            ]
            after = [
                candidate["dedup_key"][0]
                for candidate in group[index + 1:index + 3]
                if candidate["dedup_key"] and not candidate["dedup_key"].startswith(("ؤ", "ئ"))
            ]
            before_initial = before[0] if len(before) == 2 and before[0] == before[1] else None
            after_initial = after[0] if len(after) == 2 and after[0] == after[1] else None
            inferred = after_initial or before_initial
            if before_initial and after_initial and before_initial != after_initial:
                inferred = None
            if not inferred:
                continue

            entry["name_raw"] = inferred + entry["name_raw"][1:]
            entry["dedup_key"] = normalize_name(entry["name_raw"])
            entry["name_normalized"] = normalize_name(entry["name_raw"], search=True)
            entry["contextual_initial_repair"] = True
            entry["needs_review"] = True


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def arabic_signature(value: str) -> str:
    return normalize_name(value).replace(" ", "")


def likely_same_ocr_entry(left: dict, right: dict) -> bool:
    left_name = arabic_signature(left["name_raw"])
    right_name = arabic_signature(right["name_raw"])
    if not left_name or not right_name:
        return False
    name_similarity = difflib.SequenceMatcher(None, left_name, right_name).ratio()
    meaning_similarity = difflib.SequenceMatcher(
        None, arabic_signature(left["meaning"]), arabic_signature(right["meaning"])
    ).ratio()
    distance = edit_distance(left_name, right_name)
    left_box = left.get("bbox") or [0, 0, 0, 0]
    right_box = right.get("bbox") or [0, 0, 0, 0]
    vertical_distance = abs((left_box[1] + left_box[3] / 2) - (right_box[1] + right_box[3] / 2))
    return vertical_distance <= 55 and (
        (distance <= 2 and name_similarity >= 0.68 and meaning_similarity >= 0.65)
        or (name_similarity >= 0.5 and meaning_similarity >= 0.84)
    )


def text_quality(value: str) -> float:
    letters = len(ARABIC_LETTER_RE.findall(value))
    digits = len(re.findall(r"[\d٠-٩۰-۹]", value))
    return letters - digits * 3 - abs(len(value) - letters) * 0.05


def process_document(document: dict, args: argparse.Namespace) -> tuple[list[dict], dict]:
    pdf = ROOT / document["filename"]
    if not pdf.exists():
        raise FileNotFoundError(pdf)
    page_count = pdf_page_count(pdf)
    pages = render_document(pdf, args.work_dir / "pages" / document["id"], args.dpi, page_count)
    model_suffix = "_alternate" if args.tessdata_dir is not None else ""
    full_lines = ocr_pages(
        pages,
        document["psm"],
        args.workers,
        args.work_dir / "ocr" / f"{document['id']}{model_suffix}_full.json",
        args.tessdata_dir,
    )
    provisional = {page: extract_entries(lines) for page, lines in full_lines.items()}
    start, end = infer_dictionary_range(provisional, page_count)
    final_entries: list[dict] = []

    if document["layout"] == "single_column":
        for page in range(start, end + 1):
            final_entries.extend(add_provenance(provisional.get(page, []), document, page, "full"))
    else:
        columns = ocr_two_columns(
            pages,
            start,
            end,
            args.workers,
            args.work_dir / "ocr" / f"{document['id']}{model_suffix}_columns_{start}_{end}.json",
            args.tessdata_dir,
        )
        for page in range(start, end + 1):
            page_entries: list[dict] = []
            # Arabic books read the right column first.
            for side in ("right", "left"):
                extracted = extract_entries(columns.get(page, {}).get(side, []))
                page_entries.extend(add_provenance(extracted, document, page, side))

            # A full-page pass often recovers a first line hidden by a decorative border.
            known = {entry["dedup_key"] for entry in page_entries}
            recovered = extract_entries(full_lines.get(page, []), allow_continuations=False)
            for entry in add_provenance(recovered, document, page, "full_recovery"):
                if entry["dedup_key"] not in known:
                    match_index = next(
                        (
                            index for index, existing in enumerate(page_entries)
                            if likely_same_ocr_entry(existing, entry)
                        ),
                        None,
                    )
                    if match_index is None:
                        page_entries.append(entry)
                        known.add(entry["dedup_key"])
                    else:
                        # Full-page OCR sees the beginning of the line and usually
                        # restores a first letter clipped by a column crop.
                        existing = page_entries[match_index]
                        known.discard(existing["dedup_key"])
                        existing["name_raw"] = entry["name_raw"]
                        existing["name_normalized"] = entry["name_normalized"]
                        existing["dedup_key"] = entry["dedup_key"]
                        existing["ocr_confidence"] = entry["ocr_confidence"]
                        existing["needs_review"] = entry["needs_review"]
                        if text_quality(entry["meaning"]) > text_quality(existing["meaning"]):
                            existing["meaning"] = entry["meaning"]
                        known.add(existing["dedup_key"])
            final_entries.extend(page_entries)

    repair_contextual_initials(final_entries)

    # Remove exact duplicates caused by overlapping OCR passes.
    unique = {}
    for entry in final_entries:
        key = (entry["dedup_key"], entry["meaning"], entry["source_document"], entry["pdf_page"])
        previous = unique.get(key)
        if previous is None or entry["ocr_confidence"] > previous["ocr_confidence"]:
            unique[key] = entry
    final_entries = list(unique.values())
    final_entries.sort(key=lambda item: (item["pdf_page"], {"right": 0, "left": 1}.get(item["column"], 2)))
    summary = {
        "id": document["id"],
        "title": document["title"],
        "source_pdf": document["filename"],
        "page_count": page_count,
        "dictionary_page_range": [start, end],
        "entries_extracted": len(final_entries),
    }
    return final_entries, summary


def build_database(entries: list[dict]) -> list[dict]:
    exact_groups: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        exact_groups[entry["dedup_key"]].append(entry)

    canonical_groups: dict[str, list[str]] = defaultdict(list)
    for exact_key in exact_groups:
        canonical_groups[canonical_dedup_key(exact_key)].append(exact_key)

    grouped: list[list[dict]] = []
    for exact_keys in canonical_groups.values():
        page_sets = {
            exact_key: {
                (source["source_document"], source["pdf_page"])
                for source in exact_groups[exact_key]
            }
            for exact_key in exact_keys
        }
        same_page_collision = any(
            page_sets[left] & page_sets[right]
            for index, left in enumerate(exact_keys)
            for right in exact_keys[index + 1:]
        )
        if same_page_collision:
            # Two separately printed entries on one source page may differ only
            # by hamza or vocalization while having different meanings.
            grouped.extend(exact_groups[key] for key in exact_keys)
        else:
            grouped.append(
                [source for key in exact_keys for source in exact_groups[key]]
            )

    records = []
    for sources in grouped:
        best = max(sources, key=lambda item: item["ocr_confidence"])
        variants = sorted({source["name_raw"] for source in sources})
        source_genders = {source["gender"] for source in sources if source["gender"] != "unknown"}
        if not source_genders:
            gender = "unknown"
        elif source_genders == {"male"}:
            gender = "male"
        elif source_genders == {"female"}:
            gender = "female"
        else:
            # Evidence from both gender sections, or a shared-name section,
            # intentionally resolves to unisex.
            gender = "unisex"
        records.append(
            {
                "name": best["name_raw"],
                "name_normalized": best["name_normalized"],
                "gender": gender,
                "variants": variants,
                "meanings": [
                    {
                        "text": source["meaning"],
                        "source_document": source["source_document"],
                        "source_pdf": source["source_pdf"],
                        "pdf_page": source["pdf_page"],
                        "column": source["column"],
                        "ocr_confidence": source["ocr_confidence"],
                        "gender": source["gender"],
                        "gender_source": source["gender_source"],
                        "gender_section": source["gender_section"],
                        "needs_review": source["needs_review"],
                        "bbox": source.get("bbox"),
                        "contextual_initial_repair": source.get("contextual_initial_repair", False),
                    }
                    for source in sources
                ],
            }
        )
    records.sort(key=lambda item: item["name_normalized"])
    return records


def build_simple_database(records: list[dict]) -> list[dict]:
    simplified = []
    for record in records:
        meanings = []
        seen = set()
        for meaning in record["meanings"]:
            text = meaning["text"]
            if text not in seen:
                meanings.append(text)
                seen.add(text)
        simplified.append(
            {"name": record["name"], "gender": record["gender"], "meanings": meanings}
        )
    return simplified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dpi", type=int, default=240)
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "arabic_names.json")
    parser.add_argument(
        "--tessdata-dir", type=Path, default=None,
        help="Optional directory containing an alternate ara.traineddata model",
    )
    args = parser.parse_args()
    args.work_dir = args.work_dir.resolve()
    args.output = args.output.resolve()
    if args.tessdata_dir is not None:
        args.tessdata_dir = args.tessdata_dir.resolve()

    require_commands()
    all_entries = []
    documents = []
    for document in DEFAULT_DOCUMENTS:
        print(f"Processing {document['filename']}", flush=True)
        entries, summary = process_document(document, args)
        all_entries.extend(entries)
        documents.append(summary)

    exact_normalized_names = len({entry["dedup_key"] for entry in all_entries})
    names = build_database(all_entries)
    orthographic_duplicates_merged = exact_normalized_names - len(names)
    counted_genders = Counter(name["gender"] for name in names)
    gender_counts = {
        gender: counted_genders.get(gender, 0)
        for gender in ("male", "female", "unisex", "unknown")
    }
    result = {
        "metadata": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "language": "ar",
            "ocr_engine": (
                f"Tesseract 5 / ara / {args.tessdata_dir}"
                if args.tessdata_dir is not None else "Tesseract 5 / ara (standard model)"
            ),
            "render_dpi": args.dpi,
            "documents": documents,
            "source_entries": len(all_entries),
            "exact_normalized_names_before_cross_source_deduplication": exact_normalized_names,
            "orthographic_duplicates_merged": orthographic_duplicates_merged,
            "unique_names": len(names),
            "gender_counts": gender_counts,
            "review_required": sum(
                1 for name in names for meaning in name["meanings"] if meaning["needs_review"]
            ),
            "notes": [
                "The PDFs are scanned images; text was produced with OCR.",
                "Original OCR spelling and source page are retained for audit.",
                "Low-confidence entries are marked needs_review=true.",
                "Context-inferred initial-letter repairs are marked and still require review.",
                "Gender is inferred from the books' explicit male, female, and shared-name sections.",
                "Names found in both male and female evidence are labeled unisex.",
                "Deduplication ignores diacritics, initial alef/hamza spelling, and name spacing.",
                "Same-page spelling collisions are kept separate to protect genuinely different names.",
            ],
        },
        "names": names,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    simple_output = args.output.with_name("arabic_names_with_meanings.json")
    simple_result = {
        "metadata": {
            "language": "ar",
            "unique_names": len(names),
            "orthographic_duplicates_merged": orthographic_duplicates_merged,
            "gender_counts": gender_counts,
            "format": "Each name is paired with its gender label and unique extracted meanings.",
        },
        "names": build_simple_database(names),
    }
    simple_output.write_text(
        json.dumps(simple_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    simple_names = simple_result["names"]
    for gender in ("male", "female", "unisex", "unknown"):
        gender_names = [name for name in simple_names if name["gender"] == gender]
        gender_output = args.output.with_name(f"{gender}_names.json")
        gender_output.write_text(
            json.dumps(
                {
                    "metadata": {
                        "language": "ar",
                        "gender": gender,
                        "unique_names": len(gender_names),
                    },
                    "names": gender_names,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(f"Wrote {len(names)} unique names to {args.output}", flush=True)
    print(f"Wrote simplified name/meaning data to {simple_output}", flush=True)
    print(f"Gender counts: {gender_counts}", flush=True)


if __name__ == "__main__":
    main()
