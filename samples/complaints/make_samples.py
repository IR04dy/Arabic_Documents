"""Regenerate the synthetic complaint PDFs used to demo and test the CMS tab.

The CMS is the complaints desk of إمارة منطقة الرياض, so every letter is
addressed to the Emirate and, except #13 and #14 (deliberately outside its
jurisdiction), concerns a place in the Riyadh region. #13's sender lives in
Jeddah and writes about Jeddah. #14's family lives in Riyadh but writes about a
park in Taif, so the sender's address must not decide the place. Each sample is
authored as a standalone RTL HTML page in `src/` and printed by
Edge headless, so the PDFs carry a real Arabic text layer shaped by a browser —
the same kind of born-digital PDF the OCR stage meets in practice. The scanned
sample (#11) is then rasterised and degraded into an image-only PDF: the case
where nothing but OCR can recover the text, which is exactly what it tests.

Run with any Python that has pypdfium2 and Pillow (system or project venv):

    python samples/complaints/make_samples.py              # rebuild all + verify
    python samples/complaints/make_samples.py 03 11        # rebuild some + verify all
    python samples/complaints/make_samples.py --verify     # verify only

Edge location can be overridden with the EDGE_PATH environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
EXPECTED = HERE / "expected.json"

# Samples that must reach the pipeline as pictures of text, not text.
SCANNED = {"11_scanned_housing_support_shaqra"}
SCAN_DPI = 150              # a typical office-scanner setting; OCR renders at 300 anyway
SCAN_ANGLE = 0.8            # degrees — a sheet fed slightly askew
SCAN_NOISE = 0.10           # uniform noise amplitude: 0.10 of the byte range, about ±13 grey levels
SCAN_QUALITY = 55           # JPEG quality of the page image inside the PDF
MAX_PDF_BYTES = 400 * 1024  # keep the repo light; a page of text is ~100 KB
# Words every addressee variant carries («أمير/إمارة منطقة الرياض»): proof in
# the text layer that a letter marked addressed_to_entity really names the Emirate.
ENTITY_WORDS = "منطقة الرياض"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# Arabic letters in any encoding a PDF text layer may use: the base block and
# the presentation forms (some producers map glyphs back to those, not to the
# logical letters).
_ARABIC = re.compile("[\u0621-\u064A\uFB50-\uFDFF\uFE70-\uFEFC]")


def find_edge() -> str:
    for path in (os.environ.get("EDGE_PATH"), *EDGE_CANDIDATES, shutil.which("msedge")):
        if path and Path(path).is_file():
            return path
    sys.exit("Microsoft Edge was not found; set EDGE_PATH to msedge.exe")


def print_pdf(edge: str, html: Path, out: Path, profile: Path, timeout: float = 90.0) -> None:
    """Print one HTML page to PDF with Edge headless.

    A throwaway --user-data-dir keeps the run isolated from the user's own
    browser: without it Edge may hand the job to an already-running instance.
    On Windows the msedge.exe launcher also returns (exit 0) before the browser
    it spawned has written anything, so success is judged by waiting for the
    PDF to appear and for the profile's `lockfile` to go away — the browser
    holds that file until it exits, and a PDF read before then may be partial.
    """
    out.unlink(missing_ok=True)
    cmd = [
        edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        f"--user-data-dir={profile}", f"--print-to-pdf={out}", html.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    lock, last = profile / "lockfile", -1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        size = out.stat().st_size if out.is_file() else 0
        if size and size == last and not lock.exists():
            return
        last = size
        time.sleep(0.3)
    detail = proc.stderr.decode("utf-8", "replace").strip()[-400:]
    raise RuntimeError(f"Edge produced no PDF for {html.name} (exit {proc.returncode}): {detail}")


def make_scan(pdf_in: Path, out: Path, seed: int) -> None:
    """Turn a born-digital page into what a flatbed scanner would return.

    Grey paper, a slight skew, a soft blur, seeded noise and JPEG compression:
    enough to make OCR earn its keep while staying readable. Noise and specks
    come from a seeded RNG, so rebuilding does not reshuffle them.
    """
    import pypdfium2 as pdfium
    from PIL import Image, ImageChops, ImageFilter

    pdf = pdfium.PdfDocument(str(pdf_in))
    try:
        pages = [pdf[i].render(scale=SCAN_DPI / 72, grayscale=True).to_pil().convert("L")
                 for i in range(len(pdf))]
    finally:
        pdf.close()

    rng = random.Random(seed)
    scans = []
    for img in pages:
        img = img.point(lambda v: 18 + v * 224 // 255)                # ink never pure black, paper ~#F2
        img = img.rotate(SCAN_ANGLE, resample=Image.BICUBIC, fillcolor=236)
        img = img.filter(ImageFilter.GaussianBlur(0.45))
        noise = Image.frombytes("L", img.size, rng.randbytes(img.width * img.height))
        noise = noise.point(lambda v: 128 + round((v - 128) * SCAN_NOISE))
        img = ImageChops.add(img, noise, scale=1.0, offset=-128)        # img + (noise - 128)
        for _ in range(140):                                           # dust specks
            x, y = rng.randrange(img.width - 3), rng.randrange(img.height - 3)
            img.paste(rng.randrange(60, 150), (x, y, x + rng.randint(1, 3), y + rng.randint(1, 3)))
        scans.append(img)
    scans[0].save(out, "PDF", save_all=True, append_images=scans[1:], resolution=SCAN_DPI,
                  quality=SCAN_QUALITY, title="Scanned complaint (synthetic sample)")


def build(stems: list[str]) -> None:
    edge = find_edge()
    with tempfile.TemporaryDirectory(prefix="cms-samples-", ignore_cleanup_errors=True) as tmp:
        for stem in stems:
            # a fresh profile per page: a job handed to a still-exiting
            # instance of the previous profile could be silently dropped
            profile = Path(tmp) / f"edge-profile-{stem[:2]}"
            html, out = SRC / f"{stem}.html", HERE / f"{stem}.pdf"
            if stem in SCANNED:
                digital = Path(tmp) / f"{stem}.digital.pdf"
                print_pdf(edge, html, digital, profile)
                make_scan(digital, out, seed=int(stem[:2]))
            else:
                print_pdf(edge, html, out, profile)
            print(f"built  {out.name}  ({out.stat().st_size // 1024} KB)")


def _has_name(text: str, name: str) -> bool:
    """True when every word of `name` appears as a word of `text`, in any letter order.

    pdfium hands RTL lines back in visual order and reverses the letters of
    some words but not others \u2014 even part of a word: \u00ab\u0639\u0628\u062f\u0627\u0644\u0644\u0647\u00bb comes back as
    \u00ab\u0627\u0644\u0644\u0647\u062f\u0628\u0639\u00bb, the ligated \u00ab\u0627\u0644\u0644\u0647\u00bb kept and \u00ab\u0639\u0628\u062f\u00bb mirrored. Neither the phrase
    nor its mirror image is reliably present, so each word is compared as a
    bag of letters (after folding presentation forms), which is still a
    specific enough test for a full name.
    """
    def key(word: str) -> str:
        return "".join(sorted(word))

    folded = unicodedata.normalize("NFKC", text).replace("\u0640", "")
    words = {key(w) for w in re.findall(r"\w+", folded)}
    return all(key(w) in words for w in name.split())


def verify() -> bool:
    """Check every expected PDF exists, opens, is small, and has the right text layer."""
    import pypdfium2 as pdfium

    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    ok = True
    for row in expected:
        path, stem = HERE / row["file"], Path(row["file"]).stem
        problems = []
        if not path.is_file():
            print(f"FAIL   {row['file']}: missing")
            ok = False
            continue
        size = path.stat().st_size
        if size > MAX_PDF_BYTES:
            problems.append(f"{size // 1024} KB exceeds {MAX_PDF_BYTES // 1024} KB")
        try:
            pdf = pdfium.PdfDocument(str(path))
        except Exception as exc:                      # a broken file is a failure, not a crash
            print(f"FAIL   {row['file']}: does not open ({exc!r})")
            ok = False
            continue
        try:
            pages = len(pdf)
            text = ""
            for i in range(pages):
                page = pdf[i]
                tp = page.get_textpage()
                text += tp.get_text_range()
                tp.close()
                page.close()
        finally:
            pdf.close()
        letters = len(_ARABIC.findall(text))
        if stem in SCANNED:
            if text.strip():
                problems.append(f"expected no text layer, found {len(text.strip())} chars")
        else:
            if letters < 100:
                problems.append(f"text layer has only {letters} Arabic letters")
            name = row.get("complainant_name") or ""
            if name and not _has_name(text, name):
                problems.append("complainant name not found in the text layer")
            if row.get("addressed_to_entity") and not _has_name(text, ENTITY_WORDS):
                problems.append("receiving entity not found in the text layer")
        status = "FAIL " if problems else "ok   "
        detail = "; ".join(problems) or (
            "image only, no text layer" if stem in SCANNED else f"{letters} Arabic letters")
        print(f"{status}  {row['file']}: {pages} page(s), {size // 1024} KB, {detail}")
        ok = ok and not problems
    # a PDF left behind by a renamed sample would still be picked up by demos
    listed = {row["file"] for row in expected}
    for path in sorted(HERE.glob("*.pdf")):
        if path.name not in listed:
            print(f"FAIL   {path.name}: not listed in expected.json (stale?)")
            ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("only", nargs="*", help="rebuild only samples whose name starts with these prefixes")
    parser.add_argument("--verify", action="store_true", help="verify the existing PDFs without rebuilding")
    args = parser.parse_args(argv)

    if not args.verify:
        stems = sorted(p.stem for p in SRC.glob("*.html"))
        if args.only:
            stems = [s for s in stems if s.startswith(tuple(args.only))]
            if not stems:
                parser.error("no sample matches " + " ".join(args.only))
        build(stems)
    return 0 if verify() else 1


if __name__ == "__main__":
    sys.exit(main())
