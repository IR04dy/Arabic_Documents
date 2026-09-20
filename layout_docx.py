# -*- coding: utf-8 -*-
"""Layout-aware Word export (lightweight, image-only).

Design (why it looks the way it does):

The OCR text handed to this exporter is plain text with NO per-line geometry, so
we cannot pin each OCR line to a pixel box. What we CAN do reliably:

  * Classify each OCR line by its own SHAPE -- heading / field / body. Headings
    in these documents are short, punctuation-free lines; fields are "label:
    value"; everything else is body. This is robust and needs no geometry.
  * Ask Surya `fast_layout` (reads the IMAGE) for the authentic COLOUR PALETTE --
    the navy title colour and the blue section-heading colour, sampled from the
    real pixels. That is exactly the "colours from the image" the user asked for,
    and Surya never has to align to individual OCR lines.
  * Turn runs of "label: value" lines into real 2-column tables (best-effort;
    genuinely 2-D tables can't be rebuilt from linearised text without the heavy
    model, which we deliberately don't load).

No AGPL deps. Surya runs on CPU so it never contends with the app's GPU models.
"""
from __future__ import annotations

import io
import os
import re
import statistics
import threading

import numpy as np
import pypdfium2 as pdfium
from PIL import Image
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

ARABIC_FONT = "Arial"
LAYOUT_DPI = int(os.environ.get("LAYOUT_DPI", "200"))

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_ARABIC_LETTER = _ARABIC
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]")
# "label : value" -- a short-ish label, a colon, then a value. Arabic & ASCII colons.
_FIELD = re.compile(r"^\s*(?P<k>[^:：]{1,32}?)\s*[:：]\s*(?P<v>.+\S)\s*$")

HEADING_MAX_WORDS = 6
HEADING_MAX_CHARS = 48

# size (pt), bold  -- headings are only slightly larger in the source; colour +
# weight carry the hierarchy, so we keep sizes conservative and honest.
TITLE_PT = 18
HEADING_PT = 13
BODY_PT = 11
LABEL_PT = 11

_LP = None
_LOCK = threading.Lock()      # serialize Surya init + inference (one CPU model)


def _predictor():
    global _LP
    if _LP is None:
        # Pin Surya's layout model to the CPU. FastLayoutPredictor spawns a
        # server subprocess whose rf-detr detector (and reading-order model)
        # pick their device from FAST_DETECTOR_DEVICE, NOT TORCH_DEVICE; left
        # unset it defaults to cuda when a GPU is present, which would load the
        # model onto the 16 GB card already holding the resident OCR/structurer.
        # These env vars are inherited by the spawned server, so they must be set
        # before the first inference. (TORCH_DEVICE is set too for completeness.)
        os.environ["FAST_DETECTOR_DEVICE"] = "cpu"
        os.environ.setdefault("TORCH_DEVICE", "cpu")
        from surya.fast_layout import FastLayoutPredictor
        _LP = FastLayoutPredictor()
    return _LP


def shutdown_layout_server() -> None:
    """Best-effort: terminate the persistent Surya fast_layout server subprocess.

    FastLayoutPredictor spawns it detached with keep_alive (so it deliberately
    registers no atexit kill — many clients can share one server). This app is
    the only fast_layout user on the box, so if we ever started it, reap it on
    shutdown by matching its command line; otherwise it lingers past exit."""
    if _LP is None:
        return
    try:
        import subprocess
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.CommandLine -like '*surya.fast_layout.server*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
             "-ErrorAction SilentlyContinue }"],
            timeout=15, capture_output=True,
        )
    except Exception:
        pass


def _has_arabic(s: str) -> bool:
    return bool(_ARABIC.search(s or ""))


def _mostly_digits(s: str) -> bool:
    letters = _ARABIC_LETTER.findall(s)
    digits = _DIGITS.findall(s)
    return len(digits) >= len(letters) and len(digits) > 0


def _is_field(s: str):
    m = _FIELD.match(s)
    if not m:
        return None
    k = m.group("k").strip()
    # a real field label has letters and is short; avoid catching sentences that
    # merely contain a colon.
    if not _has_arabic(k) and not re.search(r"[A-Za-z]", k):
        return None
    # times / ratios / references ("الساعة 10:30", "البقرة 2:255") split at an
    # internal colon and masquerade as fields — a real label doesn't end in a
    # digit, so reject when the char before the colon is a digit.
    if k and _DIGITS.match(k[-1]):
        return None
    return (k, m.group("v").strip())


def _classify(s: str) -> str:
    s = s.strip()
    if not s:
        return "body"
    if _is_field(s):
        return "field"
    words = s.split()
    if (len(words) <= HEADING_MAX_WORDS and len(s) <= HEADING_MAX_CHARS
            and _has_arabic(s) and not _mostly_digits(s)
            and not s.endswith((".", "،", ":", "؛"))):
        return "heading"
    return "body"


# ---------------- OOXML helpers ----------------

def _para(host, rtl: bool, center: bool = False):
    """A paragraph aligned for Arabic. In a w:bidi paragraph Word resolves
    w:jc="right" as the *trailing* edge (the LEFT side in RTL), so for RTL we
    OMIT w:jc and take the default leading edge (the right side). center=True
    emits w:jc="center" (direction-independent) for titles; LTR paragraphs get a
    physical right edge."""
    p = host.add_paragraph()
    if rtl:
        p._p.get_or_add_pPr().append(OxmlElement("w:bidi"))
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif not rtl:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    return p


def _run(p, text, *, rtl: bool, bold=False, size_pt=BODY_PT, color=None):
    r = p.add_run(text)
    r.bold = bold
    rPr = r._r.get_or_add_rPr()
    if bold:
        rPr.get_or_add_bCs()               # complex-script bold (Arabic)
    r.font.size = Pt(size_pt)
    if color:
        r.font.color.rgb = RGBColor(*color)
    rf = rPr.get_or_add_rFonts()
    for a in ("w:ascii", "w:hAnsi", "w:cs"):
        rf.set(qn(a), ARABIC_FONT)
    szCs = OxmlElement("w:szCs"); szCs.set(qn("w:val"), str(size_pt * 2)); rPr.append(szCs)
    if rtl:
        rPr.append(OxmlElement("w:rtl"))
    return r


def _table_rtl(table):
    # w:bidiVisual gives the table right-to-left column order. It must be placed
    # at its CT_TblPr schema position (before tblW / tblLook, which add_table and
    # the 'Table Grid' style already inserted) — a raw append() puts it last,
    # which is schema-invalid and makes Word repair-drop it (table renders LTR).
    tblPr = table._tbl.tblPr
    bidi = OxmlElement("w:bidiVisual")
    tblPr.insert_element_before(
        bidi,
        "w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc",
        "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout",
        "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription",
    )


# ---------------- colour sampling ----------------

def _sample_color(arr, bbox):
    """Ink colour of a block: mean RGB of pixels clearly darker than the paper.

    A fixed dark-percentile fails on large sparse text (a short title in a wide
    box), where the strokes are a tiny fraction of the pixels. Instead we take
    the paper luminance (a high percentile) and keep pixels a margin below it,
    relaxing the margin until enough ink pixels are found."""
    x0, y0, x1, y1 = [int(v) for v in bbox]
    h, w = arr.shape[:2]
    x0 = max(0, x0); y0 = max(0, y0); x1 = min(w, x1); y1 = min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    flat = arr[y0:y1, x0:x1].reshape(-1, 3).astype(float)
    lum = flat.mean(1)
    bg = np.percentile(lum, 85)            # paper (background) luminance
    for margin in (55, 40, 25):
        ink = flat[lum < bg - margin]
        if len(ink) >= 20:
            r, g, b = ink.mean(0)
            if not (r > 210 and g > 210 and b > 210):
                return (int(r), int(g), int(b))
    return None


def _median_color(colors):
    colors = [c for c in colors if c]
    if not colors:
        return None
    return tuple(int(statistics.median(c[i] for c in colors)) for i in range(3))


def _palette(img):
    """Run Surya on the page image and return (title_color, heading_color).

    title_color is the tallest heading block's colour when it is distinctly
    larger than the rest (a real page title); otherwise None. heading_color is
    the median colour of the remaining section-heading blocks."""
    try:
        with _LOCK:
            res = _predictor()([img], use_order=True)[0]
    except Exception:
        return None, None
    arr = np.asarray(img.convert("RGB"))
    heads = []
    for b in res.bboxes:
        if b.label in ("SectionHeader", "Title"):
            heads.append((int(b.bbox[3] - b.bbox[1]), _sample_color(arr, b.bbox)))
    if not heads:
        return None, None
    heads.sort(key=lambda t: t[0], reverse=True)
    title_color = None
    body_heads = heads
    if len(heads) >= 2:
        med_h = statistics.median(h for h, _ in heads[1:])
        if heads[0][0] >= 1.2 * med_h:      # tallest is a distinct title
            title_color = heads[0][1]
            body_heads = heads[1:]
    heading_color = _median_color([c for _, c in body_heads])
    return title_color, heading_color


# ---------------- document assembly ----------------

def _emit_field_table(doc, fields):
    """A run of 'label: value' lines -> a 2-column (label | value) RTL table."""
    t = doc.add_table(rows=len(fields), cols=2)
    try:
        t.style = "Table Grid"
    except Exception:
        pass
    _table_rtl(t)
    for i, (k, v) in enumerate(fields):
        for col, txt in ((0, k), (1, v)):
            cell = t.cell(i, col)
            p = cell.paragraphs[0]
            rtl = _has_arabic(txt)
            if rtl:
                p._p.get_or_add_pPr().append(OxmlElement("w:bidi"))
            else:                                # omit jc on RTL (leading = right)
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _run(p, txt, rtl=rtl, bold=(col == 0), size_pt=LABEL_PT)
    doc.add_paragraph()


def _emit_page(doc, text, title_color, heading_color):
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    kinds = [_classify(ln) for ln in lines]

    # a short heading line immediately followed by a field is a table label, not
    # a section heading -> keep it as bold body so it doesn't get the big colour.
    for i, k in enumerate(kinds):
        if k == "heading":
            nxt = kinds[i + 1] if i + 1 < len(kinds) else None
            if nxt == "field":
                kinds[i] = "label"

    title_used = False
    i = 0
    n = len(lines)
    while i < n:
        k = kinds[i]
        if k == "field":
            j = i
            fields = []
            while j < n and kinds[j] == "field":
                fields.append(_is_field(lines[j]))
                j += 1
            if len(fields) >= 2:
                _emit_field_table(doc, fields)
            else:                            # a lone field -> just a paragraph
                k0, v0 = fields[0]
                p = _para(doc, _has_arabic(lines[i]))
                _run(p, lines[i], rtl=_has_arabic(lines[i]), size_pt=BODY_PT)
            i = j
            continue

        line = lines[i]
        rtl = _has_arabic(line)
        if k == "heading":
            if not title_used and title_color is not None:
                _run(_para(doc, rtl, center=True), line, rtl=rtl, bold=True,
                     size_pt=TITLE_PT, color=title_color)
                title_used = True
            else:
                _run(_para(doc, rtl), line, rtl=rtl, bold=True,
                     size_pt=HEADING_PT, color=heading_color)
        elif k == "label":
            _run(_para(doc, rtl), line, rtl=rtl, bold=True, size_pt=LABEL_PT)
        else:
            _run(_para(doc, rtl), line, rtl=rtl, size_pt=BODY_PT)
        i += 1


def build_layout_docx(doc_bytes: bytes, pages_text, filename: str = "document") -> bytes:
    """Build a formatted .docx: Surya palette (from the page image) + the OCR text.

    `doc_bytes` is the original PDF (rendered page-by-page) or a single raster
    image (one page)."""
    doc = Document()
    nf = doc.styles["Normal"].font; nf.name = ARABIC_FONT; nf.size = Pt(BODY_PT)
    is_pdf = doc_bytes[:5] == b"%PDF-" or (filename or "").lower().endswith(".pdf")
    pdf = None
    try:
        if is_pdf:
            pdf = pdfium.PdfDocument(doc_bytes)
            page_count = len(pdf)
            get_image = lambda i: pdf[i].render(scale=LAYOUT_DPI / 72).to_pil().convert("RGB")
        else:
            page_img = Image.open(io.BytesIO(doc_bytes)).convert("RGB")  # single-image page
            page_count = 1
            get_image = lambda i: page_img
        # Only render/analyse pages we actually have text for. The caller caps
        # pages_text (LAYOUT_PAGES_MAX), so this also bounds the per-page render +
        # Surya inference — without it the loop would run over the whole document,
        # doing heavy work and emitting blank trailing pages past the text cap.
        n_pages = min(page_count, len(pages_text))
        for pi in range(n_pages):
            if pi > 0:
                doc.add_page_break()
            title_color = heading_color = None
            try:
                title_color, heading_color = _palette(get_image(pi))
            except Exception:
                pass                          # no palette -> headings stay un-coloured
            text = pages_text[pi] if pi < len(pages_text) else ""
            _emit_page(doc, text, title_color, heading_color)
    finally:
        if pdf is not None:
            pdf.close()
    bio = io.BytesIO(); doc.save(bio); return bio.getvalue()
