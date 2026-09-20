"""Build an editable, RTL Arabic .docx from extracted OCR text (python-docx, MIT).

The extracted text uses '--- Page N ---' markers (inserted by extract.py). Each
page becomes a section: a bold Arabic page heading, then one paragraph per line
so the file stays faithful to the OCR line order and is fully editable in Word.
Every paragraph is set right-to-left (w:bidi + right alignment) and every run
carries the complex-script (Arabic) font + w:rtl so Arabic renders — and edits —
correctly in Word / LibreOffice.

No AGPL deps (python-docx is MIT); nothing leaves the machine.
"""

from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

ARABIC_FONT = "Arial"          # ships with Word on Windows/Mac and renders Arabic
BODY_PT = 12
HEADING_PT = 13

_PAGE_HEADER = re.compile(r"^--- Page (\d+) ---$")
# Arabic + Arabic-Supplement/Presentation-forms ranges.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def _has_arabic(text: str) -> bool:
    return bool(_ARABIC.search(text or ""))


def _paragraph(doc, *, rtl: bool):
    """A right-hugging paragraph. RTL lines get w:bidi (RTL base direction);
    pure Latin/numeric lines stay LTR-base so space-separated numbers, phone
    numbers and dates are NOT visually reversed by Word's bidi algorithm.

    Alignment gotcha: in a w:bidi paragraph, Word resolves w:jc="right" as the
    *trailing* edge, which for RTL is the LEFT side -- so setting RIGHT here
    pushes Arabic to the left. We therefore OMIT w:jc on RTL paragraphs and let
    them fall to the default leading edge (the right side, natural for Arabic).
    LTR lines have no bidi, so w:jc="right" is the physical right edge as
    intended."""
    p = doc.add_paragraph()
    if rtl:
        p._p.get_or_add_pPr().append(OxmlElement("w:bidi"))
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    return p


def _run(paragraph, text, *, rtl: bool, bold=False, size_pt=BODY_PT):
    """Add a run. Arabic runs are flagged complex-script + w:rtl; Latin/numeric
    runs are left LTR. Complex-script size (w:szCs) and, when bold, the
    complex-script bold toggle (w:bCs) are emitted so Arabic renders correctly
    (w:b / w:sz alone do not apply to Arabic glyphs)."""
    run = paragraph.add_run(text)
    run.bold = bold
    rPr = run._r.get_or_add_rPr()
    if bold:
        rPr.get_or_add_bCs()                   # complex-script bold (for Arabic)
    run.font.size = Pt(size_pt)                # w:sz
    rFonts = rPr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rFonts.set(qn(attr), ARABIC_FONT)
    szCs = OxmlElement("w:szCs")               # complex-script size (half-points)
    szCs.set(qn("w:val"), str(size_pt * 2))
    rPr.append(szCs)
    if rtl:
        rPr.append(OxmlElement("w:rtl"))       # mark the run right-to-left
    return run


def _split_pages(full_text: str):
    """[(page_label|None, [lines])] split on the '--- Page N ---' markers."""
    lines = (full_text or "").split("\n")
    pages: list[tuple] = []
    label = None
    buf: list[str] = []

    def flush():
        if buf or label is not None:
            pages.append((label, buf[:]))

    for ln in lines:
        m = _PAGE_HEADER.match(ln.strip())
        if m:
            flush()
            label, buf = m.group(1), []
        else:
            buf.append(ln)
    flush()
    if not pages:
        pages = [(None, lines)]
    return pages


def build_docx(full_text: str, filename: str = "document") -> bytes:
    """Return .docx bytes for the extracted text, editable and RTL."""
    doc = Document()
    normal = doc.styles["Normal"].font          # default for blank paras / new typing
    normal.name = ARABIC_FONT
    normal.size = Pt(BODY_PT)

    pages = _split_pages(full_text)
    for idx, (label, plines) in enumerate(pages):
        if idx > 0:
            doc.add_page_break()
        if label is not None:
            h = _paragraph(doc, rtl=True)       # "صفحة N" is Arabic
            _run(h, f"صفحة {label}", rtl=True, bold=True, size_pt=HEADING_PT)
        for line in plines:
            rtl = _has_arabic(line)             # direction follows the line's content
            p = _paragraph(doc, rtl=rtl)
            if line.strip():                    # blank lines -> empty paragraph
                _run(p, line, rtl=rtl)

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()
