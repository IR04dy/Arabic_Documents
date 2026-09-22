# -*- coding: utf-8 -*-
"""Formatted Word export: rebuild the original page as an editable .docx.

WHERE THE FORMAT COMES FROM

The OCR pass (extract.py) already knows what every block on the page is and
where it sits: Surya labels each block (SectionHeader, PageHeader, Table,
Picture, ...) and boxes it, and the page's `layout` carries those blocks with
the text lines each one contributed. With that and the page image, the format
is measured, not guessed:

  * kind        -- the block's label. A SectionHeader is a heading, a Picture
                   is an image, a Table is a table.
  * alignment   -- where a line's ink sits between the margins.
  * font size   -- the line's ascent: from its baseline (the row Arabic joins
                   its letters on) up to the top of its tall letters (alef,
                   lam, kaf). Unlike the ink box it ignores dots, diacritics
                   and descenders, which is what makes it steady (ASCENT_RATIO).
  * weight      -- stroke thickness relative to that size (BOLD_STROKE).
  * colour      -- the darkest ink of the line, so anti-aliasing does not wash
                   it out.
  * shading     -- the paper behind a block, how far the band reaches above
                   and below the text, and an accent bar beside a heading.
  * rules       -- horizontal lines between blocks.
  * columns     -- a row is split where the page shows a column gap, and the
                   table's column proportions come from where the text sat.
                   Blocks that sit side by side become one table row.
  * pictures    -- logos, stamps, signatures and QR codes are cropped from the
                   page and placed where they were.
  * spacing     -- the gap above each block, in points.

The TEXT never comes from the image: it is the (proofread) text the caller
sends, matched back to the OCR lines, so an accepted correction is kept and a
rejected one is not.

A page with no layout (an API caller that does not send one, or a page whose
layout was incomplete) takes the text-only path: each line is classified by its
shape, forms are rebuilt from the registry's label vocabulary, and the heading
colours are sampled once with Surya's layout model on the CPU.

No AGPL deps.
"""
from __future__ import annotations

import difflib
import io
import math
import os
import re
import statistics
import threading
from dataclasses import dataclass, field

import numpy as np
import pypdfium2 as pdfium
from PIL import Image
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor

ARABIC_FONT = "Arial"
# Every measurement reads one render of the page at this resolution. Stroke
# thickness is the finest of them: at 200 DPI an 11pt regular stroke is ~3 px
# and a bold one ~4 px, which is enough to tell apart; at 120 DPI it is not.
LAYOUT_DPI = int(os.environ.get("LAYOUT_DPI", "200"))
# Text-only path: sample the heading colours with Surya's CPU layout model.
# Off -> those headings are simply uncoloured. Pages with a layout never need it.
USE_PALETTE = os.environ.get("LAYOUT_PALETTE", "1").lower() not in ("0", "false", "no")
# Rebuild pages from their layout. Off -> every page takes the text-only path,
# as before layouts existed: a switch to fall back without redeploying.
USE_GEOMETRY = os.environ.get("LAYOUT_GEOMETRY", "1").lower() not in ("0", "false", "no")

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]")
# "label : value" -- a short-ish label, a colon, then a value. Arabic & ASCII
# colons. The colon is captured: it is the document's own punctuation, and a
# row keeps it on the label rather than silently dropping it.
_FIELD = re.compile(r"^\s*(?P<k>[^:：]{1,32}?)\s*(?P<c>[:：])\s*(?P<v>.+\S)\s*$")
# A label followed by a date or a number and nothing else:
#     تاريخ انتهاء الهوية ١٤٥٢/٠٨/١١هـ
_VALUE_TAIL = re.compile(
    r"^(?P<k>[^0-9٠-٩۰-۹|:：]+?)\s+"
    r"(?P<v>[0-9٠-٩۰-۹](?:[0-9٠-٩۰-۹/\-.\s]*[0-9٠-٩۰-۹])?\s*(?:هـ|ه|م)?)\s*$")

# extract.py flattens Surya's <td> cells to "a | b | c" (see `_block_text`
# there). That pipe is the ONLY surviving trace in the text that the line was a
# table row; it is an internal separator and never reaches the output.
_PIPE = re.compile(r"\s*\|\s*")

# Surya linearises a two-column form in two other shapes, neither of which
# carries a separator to split on:
#
#     رقم الوكالة ٤٣٨٢١٩٠٠٥١١٢        label and value space-joined, one line
#     الاسم                            label and value on consecutive lines
#     محمد بن عبدالله بن سالم السالم
#
# Splitting those on shape alone (first N words, short line, …) would cut real
# sentences in half. The registry already declares, per template, the exact
# Arabic label every field is printed under plus its aliases — a curated
# vocabulary the classifier is built on. Matching against THAT is exact, not a
# guess: a line is a label because a template says those words are one. (With a
# layout, the page's own column gap decides instead; see _split_by_width.)
_LABEL_CACHE = None
LABEL_LINE_MAX = 60           # a form label+value line is short; prose is not
LABEL_MAX_WORDS = 5


def _labels() -> tuple:
    """Every Arabic field label and alias the registry declares, normalised."""
    global _LABEL_CACHE
    if _LABEL_CACHE is None:
        out, secs, titles = set(), set(), set()
        try:
            from registry import get_registry
            reg = get_registry()
            for tpl in reg.templates.values():
                for f in tpl.fields:
                    for name in (f.label_ar,) + tuple(f.all_aliases_ar):
                        n = reg.normalize(name or "").strip()
                        if n:
                            out.add(n)
                # Section headings are declared too, and several OPEN WITH a
                # field label: "موضوع الوكالة ونوعها" begins with the field
                # موضوع الوكالة, "نص الوكالة والصلاحيات الممنوحة" with نص
                # الوكالة. Splitting those would turn the page's own headings
                # into nonsense rows, so they are held out by name.
                for name in tpl.sections:
                    n = reg.normalize(name or "").strip()
                    if n:
                        secs.add(n)
                # What this KIND of instrument is called: the template's own
                # name, plus the phrases its title-group anchors match on. The
                # document's title line is one of these, and nothing else on the
                # page is — which is how the letterhead stops stealing the
                # title style from "وكالة شرعية".
                titles.add(reg.normalize(tpl.name_ar or "").strip())
                for a in tpl.anchors:
                    if getattr(a, "group", "") == "title":
                        n = reg.normalize(a.text or "").strip()
                        if n:
                            titles.add(n)
            titles.discard("")
            _LABEL_CACHE = (out, secs, titles, reg.normalize)
        except Exception as exc:              # registry unavailable -> no folding
            print("layout export: registry labels unavailable:", ascii(exc))
            _LABEL_CACHE = (set(), set(), set(), lambda t: (t or "").strip())
    return _LABEL_CACHE


def _norm(text: str) -> str:
    return _labels()[3](text or "").strip()


def _is_label(text: str) -> bool:
    labels, secs, _titles_, norm = _labels()
    n = norm(text or "").strip()
    return bool(labels) and n in labels and n not in secs


def _is_section(text: str) -> bool:
    _l, secs, _t, norm = _labels()
    return norm(text or "").strip() in secs


def _is_doc_title(text: str) -> bool:
    """Is this line the document's own title (وكالة شرعية), as opposed to the
    letterhead above it (المملكة العربية السعودية)?"""
    _l, _s, titles, norm = _labels()
    return norm(text or "").strip() in titles


def _label_prefix(s: str):
    """The registry label this line STARTS with, longest first, or None.

    Bounded to a short line and a few words, and it must leave a value behind:
    a clause that happens to open with a field's wording is prose, not a row."""
    if len(s) > LABEL_LINE_MAX or _is_section(s):
        return None
    words = s.split()
    for n in range(min(LABEL_MAX_WORDS, len(words) - 1), 0, -1):
        head = " ".join(words[:n])
        if _is_label(head):
            return head
    return None


def _fold_pairs(lines: list) -> list:
    """Fold a label line and the value line under it into one row.

    Only when the line is EXACTLY a declared label and the next line is not —
    so a section caption, a heading or a body line is never swallowed. The two
    are joined with the pipe the rest of this module already understands; it
    never reaches the output."""
    out: list = []
    i = 0
    while i < len(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if (nxt and "|" not in lines[i] and "|" not in nxt
                and _is_label(lines[i]) and not _is_label(nxt)
                and not _is_field(lines[i]) and not _is_field(nxt)):
            out.append("%s | %s" % (lines[i], nxt))
            i += 2
        else:
            out.append(lines[i])
            i += 1
    return out


HEADING_MAX_WORDS = 6
HEADING_MAX_CHARS = 48

# Text-only path sizes (pt). Headings are only slightly larger in the source;
# colour + weight carry the hierarchy, so the sizes stay conservative.
TITLE_PT = 18
HEADING_PT = 13
BODY_PT = 11
LABEL_PT = 11
# A form's caption is not the content. These instruments print the label quiet
# and the value strong; bolding the label instead inverts the page's emphasis
# and makes every row shout.
LABEL_COLOR = (0x59, 0x59, 0x59)
# The text-only path keeps Word's own rhythm: 10pt after a paragraph, 1.15 lines.
TEXT_AFTER_PT = 10.0
TEXT_LINE = 1.15

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


# ---------------- line shape ----------------

def _has_arabic(s: str) -> bool:
    return bool(_ARABIC.search(s or ""))


def _mostly_digits(s: str) -> bool:
    # isalpha(), not the Arabic block: Arabic-Indic digits (٠-٩) sit inside
    # that block, which made every Arabic date count as mostly letters.
    letters = sum(ch.isalpha() for ch in s)
    digits = len(_DIGITS.findall(s))
    return digits >= letters and digits > 0


def _is_field(s: str):
    """(label, colon, value) for a "label: value" line, or None."""
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
    v = m.group("v").strip()
    # "توقيع الموكل: …… ختم كاتب العدل: ……" is two fields on one line, and
    # cutting it at the first colon puts half of the second into the first.
    if _FIELD.match(v):
        return None
    return (k, m.group("c"), v)


def _trailing_value(s: str):
    """[label, value] for a short label followed by a date or number, or None."""
    m = _VALUE_TAIL.match((s or "").strip())
    if not m:
        return None
    k = m.group("k").strip()
    if not _has_arabic(k) or len(k.split()) > LABEL_MAX_WORDS or _is_section(k):
        return None
    return [k, m.group("v").strip()]


def _cells(s: str):
    """The line's table cells, or None when it is not a row by itself.

    A pipe-delimited line (a real table Surya read off the page), a
    "label: value" line whose label the registry declares, or a line opening
    with a declared label. The colon, when there is one, stays on the label."""
    s = (s or "").strip()
    if not s:
        return None
    if "|" in s:
        parts = [c.strip() for c in _PIPE.split(s) if c.strip()]
        return parts if len(parts) >= 2 else None
    f = _is_field(s)
    if f and _is_label(f[0]):
        return [f[0] + f[1], f[2]]
    lab = _label_prefix(s)
    if lab:
        return [lab, s[len(lab):].strip()]
    return None


def _rows(lines: list) -> list:
    """Every line's cells (or None), decided with its neighbours in view.

    Two shapes only count as rows in company:
      * a "label: value" line with a label the registry does not know — alone
        it is far likelier a clause heading ("المادة الأولى: نص الوكالة");
        next to another such line it is a form;
      * a label followed by a date or number (تاريخ انتهاء الهوية ١٤٥٢/٠٨/١١هـ)
        — when a row sits beside it, it is the next row of the same form, not a
        heading that breaks the table in two."""
    cells = [_cells(ln) for ln in lines]
    colon = [_is_field(ln) for ln in lines]
    for i, f in enumerate(colon):
        if cells[i] is None and f and (
                (i > 0 and colon[i - 1]) or (i + 1 < len(lines) and colon[i + 1])):
            cells[i] = [f[0] + f[1], f[2]]
    changed = True
    while changed:
        changed = False
        for i, ln in enumerate(lines):
            if cells[i] is None and (
                    (i > 0 and cells[i - 1]) or (i + 1 < len(lines) and cells[i + 1])):
                tail = _trailing_value(ln)
                if tail:
                    cells[i] = tail
                    changed = True
    return cells


_VALUE_ONLY = re.compile(r"^[0-9٠-٩۰-۹][0-9٠-٩۰-۹/\-.\s]*(?:هـ|ه|م)?$")


def _fold_values(lines: list, cells: list) -> tuple:
    """Fold a label line and the date or number on the line under it into one
    row, when a row sits right before or after the pair:

        تاريخ انتهاء الهوية        <- a label the registry does not declare
        ١٤٥٢/٠٨/١١هـ

    That is the consecutive-lines shape of the same form (_fold_pairs catches
    it only for the labels it knows); alone, the pair stays two paragraphs."""
    out_l, out_c = [], []
    i = 0
    while i < len(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        near = (i > 0 and cells[i - 1]) or (i + 2 < len(lines) and cells[i + 2])
        if (nxt and near and cells[i] is None and cells[i + 1] is None
                and _VALUE_ONLY.match(nxt.strip()) and _has_arabic(lines[i])
                and not _DIGITS.search(lines[i]) and len(lines[i].split()) <= LABEL_MAX_WORDS
                and not _is_section(lines[i])):
            out_l.append("%s | %s" % (lines[i], nxt))
            out_c.append([lines[i].strip(), nxt.strip()])
            i += 2
        else:
            out_l.append(lines[i])
            out_c.append(cells[i])
            i += 1
    return out_l, out_c


def _classify(s: str) -> str:
    """heading or body, for a line that is not a row."""
    s = s.strip()
    words = s.split()
    if (s and len(words) <= HEADING_MAX_WORDS and len(s) <= HEADING_MAX_CHARS
            and _has_arabic(s) and not _mostly_digits(s)
            and not _trailing_value(s)
            and not s.endswith((".", "،", ":", "؛"))):
        return "heading"
    return "body"


# ---------------- the page, as Word will see it ----------------

@dataclass
class Style:
    size: float = BODY_PT
    bold: bool = False
    color: tuple | None = None
    align: str = "right"            # physical: right | left | center | both
    shade: tuple | None = None      # paragraph background
    pad: float = 0.0                # how far the shading reaches above/below (pt)
    bar: tuple | None = None        # (colour, width pt, side) accent rule
    outline: int | None = None      # 0 title, 1 section heading (navigation pane)
    line: float = 1.0               # line spacing, as a multiple
    exact: float | None = None      # ...or exactly this many points, baseline to baseline


@dataclass
class Para:
    text: str
    style: Style
    before: float = 0.0             # space above (pt)
    after: float = 0.0              # space below (pt)


@dataclass
class Picture:
    png: bytes
    width: float                    # pt
    align: str = "center"
    before: float = 0.0


@dataclass
class Rule:
    color: tuple
    weight: float                   # pt
    before: float = 0.0


@dataclass
class Table:
    rows: list                      # rows of cells; a cell is a list of Para/Picture
    widths: list | None = None      # column fractions of the text width, rightmost first
    grid: tuple | None = None       # border colour; None = no borders
    before: float = 0.0
    pad: float = 0.0                # space above and below each cell's text (pt)
    shades: list | None = None      # per row, per cell: background colour or None
    width: float | None = None      # pt; None = the full text width


# ---------------- OOXML helpers ----------------

# CT_PPr child order. Word "repairs" a paragraph whose properties are out of
# order by dropping the offender, so everything added here goes to its place.
_PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
    "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
    "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
    "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
    "suppressOverlap", "jc", "textDirection", "textAlignment",
    "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
    "pPrChange")


def _ppr_add(pPr, el):
    """Insert `el` into pPr at its schema position."""
    later = set(_PPR_ORDER[_PPR_ORDER.index(el.tag.split("}")[1]) + 1:])
    for child in pPr:
        if isinstance(child.tag, str) and child.tag.split("}")[1] in later:
            child.addprevious(el)
            return el
    pPr.append(el)
    return el


def _hex(rgb) -> str:
    return "%02X%02X%02X" % tuple(int(v) for v in rgb)


def _align(p, rtl: bool, where: str) -> None:
    """Physical alignment. In a w:bidi paragraph Word resolves jc="left"/"right"
    against the reading direction: "right" is the TRAILING edge, i.e. the left
    side of the page. So a right-hugging Arabic line OMITS jc (the default
    leading edge is the right side) and a left-hugging one writes "right"."""
    if where == "center":
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif where == "both":
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    elif rtl:
        if where == "left":
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT if where == "left" else WD_ALIGN_PARAGRAPH.RIGHT


def _run(p, text, *, rtl: bool, bold=False, size_pt=BODY_PT, color=None):
    r = p.add_run(text)
    r.bold = bold
    rPr = r._r.get_or_add_rPr()
    if bold:
        rPr.get_or_add_bCs()               # complex-script bold (Arabic)
    r.font.size = Pt(size_pt)
    if color:
        r.font.color.rgb = RGBColor(*(int(c) for c in color))
    rf = rPr.get_or_add_rFonts()
    for a in ("w:ascii", "w:hAnsi", "w:cs"):
        rf.set(qn(a), ARABIC_FONT)
    szCs = OxmlElement("w:szCs"); szCs.set(qn("w:val"), str(int(round(size_pt * 2)))); rPr.append(szCs)
    if rtl:
        rPr.append(OxmlElement("w:rtl"))
    return r


def _borders(pPr, **edges) -> None:
    """Paragraph borders: edge=(colour, width_pt, space_pt), in schema order.
    Sides are physical, as Word and LibreOffice draw them on an RTL paragraph."""
    b = OxmlElement("w:pBdr")
    for side in ("top", "left", "bottom", "right"):
        if side not in edges:
            continue
        colour, width, space = edges[side]
        e = OxmlElement("w:" + side)
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), str(max(2, min(96, int(round(width * 8))))))   # eighths of a point
        e.set(qn("w:space"), str(max(0, min(31, int(round(space))))))    # points, 0-31
        e.set(qn("w:color"), _hex(colour))
        b.append(e)
    _ppr_add(pPr, b)


def _shade(pPr, rgb) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), _hex(rgb))
    _ppr_add(pPr, shd)


def _outline(pPr, level: int) -> None:
    o = OxmlElement("w:outlineLvl"); o.set(qn("w:val"), str(level))
    _ppr_add(pPr, o)


def _spacing(p, before: float, after: float, line: float | None = None, exact: float | None = None):
    pf = p.paragraph_format
    pf.space_before = Pt(max(0.0, before))
    pf.space_after = Pt(max(0.0, after))
    if exact is not None:
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.line_spacing = Pt(exact)
    elif line is not None:
        pf.line_spacing = line


def _write_para(host, it: Para, p=None, before=None, after=None):
    """One Para into `host` (the document or a table cell)."""
    st = it.style
    rtl = _has_arabic(it.text)
    p = p if p is not None else host.add_paragraph()
    pPr = p._p.get_or_add_pPr()
    if rtl:
        _ppr_add(pPr, OxmlElement("w:bidi"))
    _align(p, rtl, st.align)
    _spacing(p, it.before if before is None else before,
             it.after if after is None else after, line=st.line, exact=st.exact)
    edges = {}
    if st.shade is not None:
        _shade(pPr, st.shade)
        # Paragraph shading covers the text lines only. Borders drawn in the
        # shading colour, set `pad` points off the text, widen the band to the
        # height it has on the page — the shading fills up to the borders.
        if st.pad >= 1:
            edges["top"] = edges["bottom"] = (st.shade, 0.25, st.pad)
    if st.bar is not None:
        colour, width, side = st.bar
        edges[side] = (colour, width, 6)
    if edges:
        _borders(pPr, **edges)
    if st.outline is not None:
        _outline(pPr, st.outline)
    if it.text:
        _run(p, it.text, rtl=rtl, bold=st.bold, size_pt=st.size, color=st.color)
    return p


def _write_picture(host, it: Picture, p=None, before=None, after=0.0, avail_pt=None):
    p = p if p is not None else host.add_paragraph()
    _align(p, False, it.align)
    _spacing(p, it.before if before is None else before, after, line=1.0)
    width = it.width if not avail_pt else min(it.width, avail_pt)
    p.add_run().add_picture(io.BytesIO(it.png), width=Pt(max(4.0, width)))
    return p


def _write_rule(doc, it: Rule):
    """A horizontal rule: an empty, one-point-high paragraph with a bottom border.
    Left-to-right so its sides mean what they say."""
    p = doc.add_paragraph()
    _spacing(p, it.before, 0.0, exact=1.0)
    _borders(p._p.get_or_add_pPr(), bottom=(it.color, max(0.25, it.weight), 0))
    return p


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


def _table_borders(table, colour) -> None:
    """Explicit table borders: none, or a thin grid in `colour`.

    "Table Grid" draws solid black on every edge, which is far heavier than
    these documents print: a label/value form has no rules at all."""
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement("w:" + edge)
        if colour is None:
            e.set(qn("w:val"), "none")
            e.set(qn("w:sz"), "0")
        else:
            e.set(qn("w:val"), "single")
            e.set(qn("w:sz"), "4")            # eighths of a point
            e.set(qn("w:color"), _hex(colour))
        e.set(qn("w:space"), "0")
        borders.append(e)
    # Schema position: tblBorders precedes shd/tblLayout/tblCellMar/tblLook.
    table._tbl.tblPr.insert_element_before(
        borders, "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
        "w:tblCaption", "w:tblDescription")


def _table_margins(table) -> None:
    """Cell margins written out — none above and below, CELL_MARGIN_PT at the
    sides — so row heights and where a column's text sits do not depend on
    each renderer's own defaults."""
    mar = OxmlElement("w:tblCellMar")
    for side, pts in (("top", 0), ("left", CELL_MARGIN_PT), ("bottom", 0), ("right", CELL_MARGIN_PT)):
        e = OxmlElement("w:" + side)
        e.set(qn("w:w"), str(int(round(pts * 20))))
        e.set(qn("w:type"), "dxa")
        mar.append(e)
    table._tbl.tblPr.insert_element_before(mar, "w:tblLook", "w:tblCaption", "w:tblDescription")


def _table_widths(table, fractions, avail) -> None:
    """Pin the column proportions. Word's autofit would size columns from the
    text they happen to hold, and the page's proportions would be lost."""
    if avail <= 0:
        return
    table.autofit = False                       # -> w:tblLayout type="fixed"
    # Per-cell widths are only honoured when the TABLE itself has a width;
    # python-docx leaves w:tblW as type="auto" w="0", and a renderer then
    # recomputes the columns from their content. Twips = EMU / 635.
    twips = int(avail / 635)
    tblW = table._tbl.tblPr.find(qn("w:tblW"))
    if tblW is not None:
        tblW.set(qn("w:type"), "dxa")
        tblW.set(qn("w:w"), str(twips))
    # w:tblGrid is what a renderer actually lays the columns out from, and
    # python-docx writes it with equal columns: setting only the per-cell w:tcW
    # leaves the grid saying 50/50 and nothing moves.
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        for col, frac in zip(grid.findall(qn("w:gridCol")), fractions):
            col.set(qn("w:w"), str(int(twips * frac)))
    for row in table.rows:
        for cell, frac in zip(row.cells, fractions):
            cell.width = Emu(int(avail * frac))


def _write_table(doc, it: Table):
    ncols = max(len(r) for r in it.rows)
    t = doc.add_table(rows=len(it.rows), cols=ncols)
    _table_borders(t, it.grid)
    _table_rtl(t)
    _table_margins(t)
    sec = doc.sections[-1]
    avail = sec.page_width - sec.left_margin - sec.right_margin
    if it.width:
        avail = min(avail, Pt(it.width))
    widths = it.widths if it.widths and len(it.widths) == ncols else [1.0 / ncols] * ncols
    _table_widths(t, widths, avail)
    for i, cells in enumerate(it.rows):
        row_shades = it.shades[i] if it.shades and i < len(it.shades) else None
        for c in range(ncols):
            cell = t.cell(i, c)
            shade = row_shades[c] if row_shades and c < len(row_shades) else None
            if shade is not None:
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), _hex(shade))
                cell._tc.get_or_add_tcPr().append(shd)       # after tcW: its schema place
            content = cells[c] if c < len(cells) else []
            first = cell.paragraphs[0]
            if not content:
                _spacing(first, it.pad, it.pad, line=1.0)
                continue
            col_pt = Emu(int(avail * widths[c])).pt - 11     # minus the cell margins
            for k, piece in enumerate(content):
                p = first if k == 0 else None
                before = it.pad if k == 0 else piece.before
                after = it.pad if k == len(content) - 1 else 0.0
                if isinstance(piece, Picture):
                    _write_picture(cell, piece, p=p, before=before, after=after, avail_pt=col_pt)
                else:
                    _write_para(cell, piece, p=p, before=before, after=after)
    return t


def _spacer(doc, height: float, page_break: bool = False):
    """An empty paragraph exactly `height` points tall. It carries a gap before
    a table, keeps two tables apart (Word merges adjacent ones), and holds the
    page break when a page opens on a table."""
    p = doc.add_paragraph()
    _spacing(p, 0.0, 0.0, exact=max(1.0, height))
    if page_break:
        p.paragraph_format.page_break_before = True
    return p


def _emit(doc, items: list, new_page: bool) -> str | None:
    """Append one page's items. With `new_page`, the page starts on a new sheet
    — a page-break-before on its first paragraph, not a break paragraph of its
    own, which would push an empty line onto the top of the page.

    Returns what was written last: "p" (a paragraph), "t" (a table) or None."""
    last = last_p = None
    if not items:
        if new_page:
            _spacer(doc, 1.0, page_break=True)
            return "p"
        return None
    for k, it in enumerate(items):
        brk = new_page and k == 0
        if isinstance(it, Table):
            if brk or last != "p":
                _spacer(doc, it.before, page_break=brk)
            elif it.before > 0:
                pf = last_p.paragraph_format
                pf.space_after = Pt((pf.space_after.pt if pf.space_after else 0.0) + it.before)
            _write_table(doc, it)
            last = "t"
            continue
        if isinstance(it, Picture):
            p = _write_picture(doc, it)
        elif isinstance(it, Rule):
            p = _write_rule(doc, it)
        else:
            p = _write_para(doc, it)
        if brk:
            p.paragraph_format.page_break_before = True
        last, last_p = "p", p
    return last


# ---------------- page geometry ----------------

# Label/value column proportions for the text-only path. The label is a short
# fixed caption and the value carries the content, so an even split wastes the
# value column and wraps it while the label side sits empty.
FORM_COLS = (0.35, 0.65)

MIN_MARGIN_PT = 28.0          # ~1 cm: a full-bleed scan must not give 0 margins
MAX_MARGIN_PT = 144.0         # 2 in: a mostly-empty page must not give huge ones
PAPER_SIZES = ((595.28, 841.89), (612.0, 792.0))    # A4, US Letter (portrait)


def _ink_box(img):
    """Margins as fractions of the page, from the bounding box of its ink.

    (left, top, right, bottom). Everything clearly darker than paper counts, so
    this needs no layout model — and it measures what a reader actually
    perceives as the margin. None for a blank page."""
    a = np.asarray(img.convert("L"))
    dark = a < 240
    rows = np.flatnonzero(dark.any(axis=1))
    cols = np.flatnonzero(dark.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return None
    h, w = a.shape
    return (cols[0] / w, rows[0] / h,
            1 - (cols[-1] + 1) / w, 1 - (rows[-1] + 1) / h)


def _image_page_size(img) -> tuple:
    """A raster upload has no paper size of its own. A4 or Letter when its
    proportions say so (either orientation), else A4's width at its aspect."""
    w, h = img.size
    for pw, ph in PAPER_SIZES:
        for a, b in ((pw, ph), (ph, pw)):
            if abs(h / w - b / a) <= 0.03 * (b / a):
                return (a, b)
    base = PAPER_SIZES[0][0] if h >= w else PAPER_SIZES[0][1]
    return (base, base * h / w)


def _page_setup(sec, size_pt, img):
    """Give the section the ORIGINAL's paper size and margins.

    python-docx starts from a US Letter template, so an A4 instrument came back
    Letter and every line reflowed. Page 1 sets the margins for the document —
    right for the single-template instruments seen here; a later page of a
    different paper size gets a section of its own (build_layout_docx)."""
    if size_pt:
        w, h = size_pt
        if w > 0 and h > 0:
            sec.page_width, sec.page_height = Pt(w), Pt(h)
    else:
        w, h = sec.page_width.pt, sec.page_height.pt
    box = _ink_box(img) if img is not None else None
    if not box:
        return
    clamp = lambda v: max(MIN_MARGIN_PT, min(MAX_MARGIN_PT, v))
    left, top, right, bottom = box
    top_pt = clamp(top * h)
    # The ink box measures where the text BLOCK sits, which is the margin on
    # three sides — but not at the foot: a page whose content stops halfway
    # reports half the sheet as "bottom margin". Trailing space is content, not
    # layout, so the foot may be tighter than the head and never looser.
    bottom_pt = min(clamp(bottom * h), top_pt)
    sec.left_margin, sec.right_margin = Pt(clamp(left * w)), Pt(clamp(right * w))
    sec.top_margin, sec.bottom_margin = Pt(top_pt), Pt(bottom_pt)


# ---------------- measuring the page image ----------------

INK_DELTA = 60        # this much darker than the paper is ink
FILL_PX = 10          # ink solid across this many px both ways is a fill, not a stroke
FAINT_DELTA = 25      # this much darker, in a long straight run, is a rule
SHADE_DELTA = 8       # a background this far from the paper (any channel) is shading

# An Arabic line's ascent — baseline to the top of its tall letters — as a
# fraction of the font size, as Arial (the export font) prints it. Measured by
# rendering Arial at 9-20pt: within ±3% across ordinary lines, where the full
# ink height swings ±20% with dots, diacritics and descenders, and the single
# longest stroke is thrown by a final lam dropping below the baseline. Matching
# the ascent makes the export's letters as tall as the original's, whatever
# font the original used; on real pages it lands within ~5-10% of the size.
#
# Only Arabic lines of at least ASCENT_MIN_LETTERS letters with a tall letter
# are measured. A short word, a number, a Latin line (whose densest rows are
# its lowercase body, not a baseline) takes the size of its row or block.
ASCENT_RATIO = 0.52
ASCENT_MIN_LETTERS = 8
_TALL_AR = set("اأإآلكطظﻻﻷﻹﻵ")
# Stroke thickness over font size above which text is bold. Arial prints Arabic
# ~0.08-0.095 regular and ~0.11-0.13 bold (Geeza Pro, the Najiz instruments, is
# in the same bands); Latin ~0.115 regular and ~0.155 bold.
BOLD_STROKE = {"arabic": 0.108, "latin": 0.135}
MIN_PT, MAX_PT = 7.0, 30.0
CELL_MARGIN_PT = 5.4          # left/right cell margin, as written by _table_margins
COLUMNS_MIN_SPAN = 0.3        # a free-standing row spans this share of the page width
GRID_PT = 0.5                 # a table rule, as written by _table_borders


def _measurable(text: str) -> bool:
    """Is this text's size readable from its ascent? (See ASCENT_RATIO.)
    Not with brackets: they are the tallest strokes on the line."""
    return (sum(ch.isalpha() for ch in text) >= ASCENT_MIN_LETTERS
            and _has_arabic(text) and any(ch in _TALL_AR for ch in text)
            and not any(ch in "()[]{}«»﴾﴿" for ch in text))


# A second reading of the size: the line's printed WIDTH against the same text
# set in Arial by Pillow (which needs libraqm to shape Arabic). On an Arial
# original it is within ~3%. On any other font it is the Arial size that fills
# the same width — so the smaller of the two readings is used: the export's
# letters are never taller than the original's, and its lines never wider, so
# nothing wraps that did not. Without libraqm or an Arial file the ascent
# stands alone. LAYOUT_ARIAL / LAYOUT_ARIAL_BOLD name the font files.
WIDTH_MIN_CHARS = 6
_ARIAL_FILES = (
    (os.environ.get("LAYOUT_ARIAL", ""), os.environ.get("LAYOUT_ARIAL_BOLD", "")),
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
     "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf"),
)
_FONTS = None


def _arial():
    """(regular, bold) Pillow fonts at 100pt, or None when width can't be read."""
    global _FONTS
    if _FONTS is None:
        _FONTS = False
        try:
            from PIL import ImageFont, features
            if features.check("raqm"):
                for reg, bold in _ARIAL_FILES:
                    if reg and bold and os.path.isfile(reg) and os.path.isfile(bold):
                        _FONTS = (ImageFont.truetype(reg, 100), ImageFont.truetype(bold, 100))
                        break
            if not _FONTS:
                print("layout export: sizes read from letter height only "
                      "(needs Pillow with libraqm and an Arial font file)")
        except Exception as exc:
            print("layout export: width sizing unavailable:", ascii(exc))
    return _FONTS or None


def _em_width(text: str, bold: bool):
    """How wide `text` prints in Arial, in points per point of size, or None."""
    fonts = _arial()
    if not fonts or len(text.replace(" ", "")) < WIDTH_MIN_CHARS:
        return None
    try:
        x0, _, x1, _ = fonts[1 if bold else 0].getbbox(
            text, direction="rtl" if _has_arabic(text) else "ltr")
    except Exception:
        return None
    return (x1 - x0) / 100.0 if x1 > x0 else None


def _width_size(text: str, width_px: float, pt: float, bold: bool):
    """The Arial size (pt) at which `text` prints `width_px` wide, or None."""
    em = _em_width(text, bold)
    return width_px * pt / em if em and width_px > 0 else None


def _is_bold(text: str, stroke_px: float, size_pt: float, pt: float) -> bool:
    """Bold, from stroke thickness against the font size (BOLD_STROKE)."""
    latin = bool(re.search(r"[A-Za-z]", text or "")) and not _has_arabic(text)
    return stroke_px * pt / max(1.0, size_pt) > BOLD_STROKE["latin" if latin else "arabic"]


def _runs(flags) -> list:
    """[(start, end)] of the True runs in a 1-D boolean array."""
    d = np.diff(np.concatenate(([0], np.asarray(flags, dtype=np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def _bands(mask) -> list:
    """The visual text lines of a region: [top, bottom) row ranges.

    Dots and diacritics separated from their letters by a sliver of paper come
    out as thin bands of their own; each is folded into its nearest neighbour.
    Specks too thin and too far to be anyone's dot are dropped."""
    b = [list(r) for r in _runs(mask.any(axis=1))]
    while len(b) > 1:
        ref = float(np.median([e - s for s, e in b]))
        for i, (s, e) in enumerate(b):
            if e - s >= 0.4 * ref:
                continue
            cand = []
            if i:
                cand.append((s - b[i - 1][1], i - 1))
            if i + 1 < len(b):
                cand.append((b[i + 1][0] - e, i + 1))
            gap, j = min(cand)
            if gap <= 0.5 * ref:
                lo, hi = min(i, j), max(i, j)
                b[lo] = [min(b[lo][0], b[hi][0]), max(b[lo][1], b[hi][1])]
                del b[hi]
                break
        else:
            break
    if b:
        ref = float(np.median([e - s for s, e in b]))
        b = [x for x in b if x[1] - x[0] >= max(2, 0.3 * ref)]
    return b


def _segments(mask, min_gap: int) -> list:
    """Runs of ink across a band separated by at least `min_gap` columns of
    paper — the columns of a row. Right-most first, the Arabic reading order."""
    runs = _runs(mask.any(axis=0))
    out = []
    for s, e in runs:
        if out and s - out[-1][1] < min_gap:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [tuple(x) for x in reversed(out)]


def _baseline(mask) -> float:
    """Row of the baseline: where Arabic letters join, i.e. where the long
    horizontal strokes are — the middle of the rows holding at least 80% of
    the most ink in runs of 30% of the line's height or more. Plain density
    is not enough: a number's digits pack their ink mid-height and pull a
    density baseline up into the middle of the line."""
    h, w = mask.shape
    run = max(3, int(round(0.3 * h)))
    if w > run:
        cs = np.pad(mask.astype(np.int32), ((0, 0), (1, 0))).cumsum(axis=1)
        long = ((cs[:, run:] - cs[:, :-run]) == run).sum(axis=1)
        if long.max() > 0:
            return float(np.mean(np.flatnonzero(long >= 0.8 * long.max())))
    density = mask.sum(axis=1)
    return float(np.mean(np.flatnonzero(density >= 0.8 * density.max())))


def _ascent(mask) -> float:
    """Pixels from the baseline up to the top of the line's tall letters.

    The baseline is where the ink is densest (Arabic letters join along it);
    the tall letters are the columns whose longest vertical stroke is at least
    60% of the longest, and their top is where those strokes start — the 35th
    percentile of those tops: lam reaches above alef, and a line rich in lams
    must not read as a larger font."""
    best = np.zeros(mask.shape[1], np.int32)
    cur = np.zeros_like(best)
    start = np.zeros_like(best)
    top = np.zeros_like(best)
    for r, row in enumerate(mask):
        start = np.where(row & (cur == 0), r, start)
        cur = (cur + 1) * row
        longer = cur > best
        top = np.where(longer, start, top)
        best = np.where(longer, cur, best)
    if not best.size or not best.any():
        return 0.0
    tops = top[best >= 0.6 * best.max()]
    return max(0.0, _baseline(mask) - float(np.percentile(tops, 35)) + 1)


def _stroke(mask) -> float:
    """Mean stroke thickness in pixels: twice the ink area over its outline."""
    area = int(mask.sum())
    if area == 0:
        return 0.0
    inner = (mask[1:-1, 1:-1] & mask[:-2, 1:-1] & mask[2:, 1:-1]
             & mask[1:-1, :-2] & mask[1:-1, 2:])
    return 2.0 * area / max(1, area - int(inner.sum()))


def _ink_color(rgb, mask, light: bool = False):
    """The printed colour of a region's text: the core of its strokes, i.e.
    the darkest 30% of them (the lightest, for light text on a fill), so the
    anti-aliased edges do not wash it out."""
    px = rgb[mask]
    if len(px) < 8:
        return None
    lum = px.mean(axis=1)
    core = lum >= np.percentile(lum, 70) if light else lum <= np.percentile(lum, 30)
    return tuple(int(v) for v in px[core].mean(axis=0))


def _box_sum(a, r: int):
    """Sum of every r×r window of `a` (valid positions only), by integral image."""
    ii = np.pad(a.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return ii[r:, r:] - ii[:-r, r:] - ii[r:, :-r] + ii[:-r, :-r]


def _fills(ink, r: int = FILL_PX):
    """Where the ink is a solid fill — a coloured band, a filled table cell —
    rather than letters: solid over an r×r square, which no stroke of text is.
    An opening (erode, then dilate) of the ink by that square."""
    h, w = ink.shape
    if h < r or w < r:
        return np.zeros_like(ink)
    core = _box_sum(ink, r) == r * r
    if not core.any():
        return np.zeros_like(ink)
    return _box_sum(np.pad(core, ((r - 1, r - 1), (r - 1, r - 1))), r) > 0


def _text_color(c):
    """Black ink (and a scan's dark grey) stays Word's automatic colour."""
    if c is None:
        return None
    if max(c) - min(c) < 24 and sum(c) / 3 < 70:
        return None
    return c


class _Page:
    """One rendered page and the masks every measurement reads."""

    def __init__(self, img, size_pt):
        self.img = img.convert("RGB")
        self.rgb = np.asarray(self.img)
        self.h, self.w = self.rgb.shape[:2]
        lum = self.rgb.mean(axis=2, dtype=np.float32)
        bright = lum >= np.percentile(lum, 60)
        paper = np.median(self.rgb[bright][::7], axis=0)
        self.paper = tuple(int(v) for v in paper)
        self.paper_lum = float(paper.mean())
        self.lum = lum
        self.ink = lum < self.paper_lum - INK_DELTA
        self.faint = lum < self.paper_lum - FAINT_DELTA
        self.pt = float(size_pt[0]) / self.w            # points per pixel

    def box(self, frac, pad: int = 0) -> tuple:
        x0 = max(0, int(frac[0] * self.w) - pad)
        y0 = max(0, int(frac[1] * self.h) - pad)
        x1 = min(self.w, int(math.ceil(frac[2] * self.w)) + pad)
        y1 = min(self.h, int(math.ceil(frac[3] * self.h)) + pad)
        return (x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))

    def png(self, box) -> bytes:
        bio = io.BytesIO()
        self.img.crop(box).save(bio, format="PNG")
        return bio.getvalue()

    def text_mask(self, box) -> tuple:
        """(mask, fill): the text of a region as a mask, and the colour of the
        solid fill behind each row (None on bare paper).

        On paper, text is what is darker than the paper. Inside a fill — white
        letters on a green header, black on an orange one, a filled label cell —
        the fill is the paper and the text is what differs strongly from it,
        lighter or darker."""
        x0, y0, x1, y1 = box
        ink = self.ink[y0:y1, x0:x1]
        mask = ink.copy()
        fill = _fills(ink)
        colours = [None] * (y1 - y0)
        if not fill.any():
            return mask, fill, colours
        lum, rgb = self.lum[y0:y1, x0:x1], self.rgb[y0:y1, x0:x1]
        spans = []
        for r in np.flatnonzero(fill.any(axis=1)):
            runs = []
            for a, b in _runs(fill[r]):             # letter-sized gaps are the letters
                if runs and a - runs[-1][1] <= 4 * FILL_PX:
                    runs[-1][1] = b
                else:
                    runs.append([a, b])
            for a, b in runs:
                solid = fill[r, a:b]
                f = float(np.median(lum[r, a:b][solid]))
                spans.append((r, a, b, np.abs(lum[r, a:b] - f)))
                colours[r] = tuple(int(v) for v in np.median(rgb[r, a:b][solid], axis=0))
        # Text is what stands at least half as far from its fill as the text
        # of the region does at its strongest: a scan's ringing and grain are
        # a little off the fill, the letters far off it. One threshold for the
        # region — a row with no letters in it must not lower it to its noise.
        strongest = float(np.percentile(np.concatenate([d for *_, d in spans]), 99.5))
        cut = max(0.75 * INK_DELTA, 0.5 * strongest)
        for r, a, b, diff in spans:
            mask[r, a:b] = diff > cut
            fill[r, a:b] = True
        # A fill's own edge — anti-aliased, or a scan's blur — is neither fill
        # nor text: two pixels either side of every fill boundary are cleared.
        near = _box_sum(np.pad(fill, 2), 5)
        mask[(near > 0) & (near < 25)] = False
        return mask, fill, colours

    def measure(self, x0, y0, mask, text) -> dict | None:
        """Extent, baseline (px), size (pt, or None when the text has no tall
        letter), stroke thickness (px) and colour of the ink in `mask`, whose
        top-left pixel is at (x0, y0). None when there is no ink."""
        rows = np.flatnonzero(mask.any(axis=1))
        cols = np.flatnonzero(mask.any(axis=0))
        if not rows.size:
            return None
        # A region can hold several visual lines (a cell whose text wraps):
        # the size and baseline are read from the first of them.
        lines = _bands(mask) or [[int(rows[0]), int(rows[-1]) + 1]]
        s0, e0 = lines[0]
        first = mask[s0:e0]
        size = (_ascent(first) * self.pt / ASCENT_RATIO) if _measurable(text) else None
        size = size or None
        sub = mask[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        region = self.rgb[y0 + rows[0]:y0 + rows[-1] + 1, x0 + cols[0]:x0 + cols[-1] + 1]
        # light text on a fill: its core is its lightest part, not its darkest
        rl = region.mean(axis=2)
        light = bool((~sub).any()) and float(rl[sub].mean()) > float(np.median(rl[~sub]))
        # Digits have no joining stroke — their densest rows are mid-glyph —
        # but they sit on the baseline, so there it is the bottom of the ink.
        letters = sum(ch.isalpha() for ch in text)
        wordy = letters >= 4 and letters >= len(_DIGITS.findall(text))
        fr = np.flatnonzero(first.any(axis=1))
        base = y0 + s0 + (_baseline(first) if wordy else float(fr[-1]))
        sl, el = lines[-1]
        last = y0 + sl + (_baseline(mask[sl:el]) if wordy else float(el - 1))
        return {
            "top": y0 + int(rows[0]), "bottom": y0 + int(rows[-1]) + 1,
            "lines": len(lines),
            "base": base,
            "base_last": last if len(lines) > 1 else base,
            "pitch": (last - base) / (len(lines) - 1) if len(lines) > 1 else None,
            "left": x0 + int(cols[0]), "right": x0 + int(cols[-1]) + 1,
            "size": size,
            "stroke": _stroke(sub),
            "color": _ink_color(region, sub, light),
        }

    def background(self, box, skip=None):
        """The colour behind a region when it is not the paper, else None."""
        x0, y0, x1, y1 = box
        keep = ~self.faint[y0:y1, x0:x1]
        if skip is not None:
            keep[:, skip[0] - x0:skip[1] - x0] = False
        px = self.rgb[y0:y1, x0:x1][keep]
        if len(px) < 20:
            return None
        c = tuple(int(v) for v in np.median(px, axis=0))
        if max(abs(a - b) for a, b in zip(c, self.paper)) < SHADE_DELTA:
            return None
        return c

    def reach(self, x0, x1, top, bottom, shade) -> float:
        """How far (pt) a shaded band extends above and below a line's ink."""
        xs = slice(x0 + (x1 - x0) // 4, max(x0 + (x1 - x0) // 4 + 1, x1 - (x1 - x0) // 4))
        limit = int(0.04 * self.h)
        near = lambda y: max(abs(int(a) - int(b)) for a, b in
                             zip(np.median(self.rgb[y, xs], axis=0), shade)) <= SHADE_DELTA
        up = 0
        while up < limit and top - up - 1 >= 0 and near(top - up - 1):
            up += 1
        down = 0
        while down < limit and bottom + down < self.h and near(bottom + down):
            down += 1
        return min(up, down) * self.pt

    def bar(self, box) -> dict | None:
        """A vertical accent rule at either end of a heading, clear of its text."""
        x0, y0, x1, y1 = box
        bh = y1 - y0
        if bh < 4:
            return None
        reach = int(0.03 * self.w)
        X0, X1 = max(0, x0 - reach), min(self.w, x1 + reach)
        reg = self.ink[y0:y1, X0:X1]
        maxw = max(3, int(0.012 * self.w))
        for s, e in _runs(reg.mean(axis=0) >= 0.85):
            if e - s > maxw:
                continue
            a, b = X0 + s, X0 + e
            right = (a + b) / 2 > (x0 + x1) / 2
            if (right and a < x1 - reach) or (not right and b > x0 + reach):
                continue                                  # in the middle of the text
            clear = max(2, bh // 6)
            inner = self.ink[y0:y1, max(0, a - clear):a] if right else self.ink[y0:y1, b:b + clear]
            if inner.any():
                continue                                  # touching text: a letter, not a bar
            return {"side": "right" if right else "left", "x0": a, "x1": b,
                    "color": _ink_color(self.rgb[y0:y1, a:b], reg[:, s:e]) or (0, 0, 0),
                    "width": (e - s) * self.pt}
        return None

    def grid(self, box) -> dict | None:
        """A table that prints its ruling lines: their colour, and the x of
        each vertical one (rightmost first) — the exact column boundaries.
        None for a table with no rules."""
        x0, y0, x1, y1 = box
        solid = _fills(self.ink[y0:y1, x0:x1])
        f = self.faint[y0:y1, x0:x1] & ~(_box_sum(np.pad(solid, 2), 5) > 0)
        thin = max(6, int(0.006 * self.w))
        hits = [r for a, b in _runs(f.mean(axis=1) >= 0.6) if b - a <= thin for r in range(a, b)]
        if not hits:
            return None
        colour = _ink_color(self.rgb[y0:y1, x0:x1][hits], f[hits]) or (0xBF, 0xBF, 0xBF)
        cols = [x0 + (a + b) / 2 for a, b in _runs(f.mean(axis=0) >= 0.6) if b - a <= thin]
        # The outermost rules are the table's frame; the ones between them
        # are its column boundaries.
        left = min(cols) if cols else x0
        right = max(cols) if cols else x1
        inner = [x for x in cols if left + thin < x < right - thin]
        return {"color": colour, "cols": sorted(inner, reverse=True),
                "left": left, "right": right}

    def rules(self, avoid) -> list:
        """Horizontal rules: thin, long runs of darker-than-paper pixels that
        are not part of any text or table block."""
        out = []
        thin = max(6, int(0.004 * self.h))
        for s, e in _runs(self.faint.mean(axis=1) >= 0.25):
            if e - s > thin:
                continue                                  # a filled box, not a rule
            runs = _runs(self.faint[s:e].any(axis=0))
            a, b = max(runs, key=lambda r: r[1] - r[0])
            if b - a < 0.3 * self.w:
                continue
            if any(y0 - 2 <= s < y1 + 2 and min(b, x1) - max(a, x0) > 0.5 * (b - a)
                   for x0, y0, x1, y1 in avoid):
                continue
            region = self.faint[s:e, a:b]
            out.append({"top": s, "bottom": e, "left": a, "right": b,
                        "color": _ink_color(self.rgb[s:e, a:b], region) or (0x80, 0x80, 0x80),
                        "weight": (e - s) * self.pt})
        return out


# ---------------- matching the text to the layout ----------------

def _assign(blocks, lines) -> list:
    """For every line of the text being exported, the (block, line-in-block) of
    the OCR it came from — or None when the layout has no text at all.

    The export text is the OCR after proofreading: most lines are identical,
    a corrected one differs by a few letters, and a rewrite may merge or split
    lines. A sequence alignment over normalised lines pairs the identical ones
    exactly, spreads a changed stretch evenly over the OCR stretch it replaced,
    and hangs a line the OCR never had on the line before it."""
    ocr = [(bi, li) for bi, b in enumerate(blocks) for li in range(len(b["lines"]))]
    if not ocr:
        return [None] * len(lines)
    a = [_norm(t) for b in blocks for t in b["lines"]]
    q = [_norm(t) for t in lines]
    where = [None] * len(lines)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, q, autojunk=False).get_opcodes():
        if tag in ("equal", "replace"):
            n_a, n_q = i2 - i1, j2 - j1
            for j in range(j1, j2):
                where[j] = ocr[i1 + min(n_a - 1, (j - j1) * n_a // n_q)]
    prev = None
    for j in range(len(lines)):
        where[j] = where[j] or prev
        prev = where[j]
    nxt = ocr[0]
    for j in reversed(range(len(lines))):
        where[j] = where[j] or nxt
        nxt = where[j]
    return where


# ---------------- the page, from its layout ----------------

PICTURE_LABELS = {"Picture", "Figure", "Diagram"}


@dataclass
class _Line:
    """One line of the page as measured: its ink extent (px) and, once the
    page's sizes are known, the Style it is written with."""
    text: str
    top: int
    bottom: int
    left: int
    right: int
    size: float | None = None
    stroke: float = 0.0             # px
    color: tuple | None = None
    base0: float | None = None      # baseline of its first visual line (px)
    base1: float | None = None      # ...and of its last
    bands: int = 1                  # visual lines it spans (a wrapped paragraph)
    pitch: float | None = None      # baseline to baseline between those lines (px)
    span: float = 0.0               # its ink width summed over those lines (px)
    heading: bool = False
    shade: tuple | None = None
    reach: float = 0.0              # pt the shading extends past the ink
    bar: dict | None = None
    cells: list | None = None       # a row: [(text, measure or None)], rightmost first
    key: tuple | None = None        # consecutive rows sharing a key make one table
    grid: tuple | None = None
    style: Style | None = None
    cell_styles: list | None = None


@dataclass
class _Unit:
    kind: str                       # text | picture | columns | rule
    box: tuple                      # px
    lines: list = field(default_factory=list)
    members: list = field(default_factory=list)     # columns: units, rightmost first
    bounds: list = field(default_factory=list)      # columns: x of each inner boundary
    col: tuple | None = None        # a member's column (x0, x1)
    rule: dict | None = None
    png: bytes = b""                # picture: its crop of the page

    @property
    def top(self):
        if self.kind == "text" and self.lines:
            return min(l.top for l in self.lines)
        if self.kind == "columns":
            return min(m.top for m in self.members)
        return self.box[1]

    @property
    def bottom(self):
        if self.kind == "text" and self.lines:
            return max(l.bottom for l in self.lines)
        if self.kind == "columns":
            return max(m.bottom for m in self.members)
        return self.box[3]


@dataclass
class _Measured:
    """A page measured from its layout. Its styles wait for every page to be
    measured, so a size means the same thing on page 1 and page 9."""
    units: list
    content: tuple                  # (left, top, right) of the text area, px
    pt: float                       # points per pixel


def _occupancy(n: int, seg_counts: list) -> list:
    """Which bands each of a block's n OCR lines sits on."""
    nb = len(seg_counts)
    if nb == n:
        return [(k,) for k in range(n)]
    if n == 1:
        return [tuple(range(nb))]
    if nb < n and sum(seg_counts) == n:
        # label and value read as consecutive lines of one visual row
        return [(k,) for k, c in enumerate(seg_counts) for _ in range(c)]
    if nb < n:
        return [(min(nb - 1, li * nb // n),) for li in range(n)]
    return [tuple(range(li * nb // n, max(li * nb // n + 1, (li + 1) * nb // n)))
            for li in range(n)]


def _group_segments(seg: list, k: int):
    """`seg` (rightmost first) merged into k columns at its k-1 widest gaps."""
    if k < 1 or len(seg) < k:
        return None
    if len(seg) == k:
        return list(seg)
    order = sorted(range(len(seg) - 1), key=lambda i: seg[i][0] - seg[i + 1][1], reverse=True)
    out, start = [], 0
    for cut in sorted(order[:k - 1]) + [len(seg) - 1]:
        grp = seg[start:cut + 1]
        out.append((min(a for a, _ in grp), max(b for _, b in grp)))
        start = cut + 1
    return out


def _split_by_width(text: str, seg: list):
    """Cut a line into the columns the page shows, at the word boundaries whose
    share of the letters best matches each column's share of the ink width.

    This is how a label the registry has never heard of still lands in its own
    cell: the page itself shows where the label ends. Refused (None) when no
    cut comes within 20% — better one honest paragraph than a wrong split."""
    words = text.split()
    k = min(len(seg), len(words), 4)
    groups = _group_segments(seg, k) if k >= 2 else None
    if not groups:
        return None
    widths = [b - a for a, b in groups]
    if not _has_arabic(text):
        widths = widths[::-1]                     # a Latin line reads left to right
    chars = np.cumsum([len(w) for w in words]) / sum(len(w) for w in words)
    target = np.cumsum(widths) / sum(widths)
    cuts, lo = [], 1
    for g in range(k - 1):
        hi = len(words) - (k - 1 - g)
        best = min(range(lo, hi + 1), key=lambda c: abs(chars[c - 1] - target[g]))
        if abs(chars[best - 1] - target[g]) > 0.2:
            return None
        cuts.append(best)
        lo = best + 1
    bounds = [0] + cuts + [len(words)]
    cells = [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:])]
    return cells if _has_arabic(text) else cells[::-1]


def _para_line(pg, mask, x0, y0, bands, text, heading) -> _Line:
    # a band much shorter than the line's tallest is a stray mark, not a line
    tallest = max(e - s for s, e in bands)
    bands = [(s, e) for s, e in bands if e - s >= 0.5 * tallest]
    ms = [m for m in (pg.measure(x0, y0 + s, mask[s:e], text) for s, e in bands) if m]
    if not ms:
        return _Line(text, y0 + bands[0][0], y0 + bands[-1][1], x0, x0 + mask.shape[1],
                     heading=heading)
    sizes = [m["size"] for m in ms if m["size"]]
    colors = [m["color"] for m in ms if m["color"]]
    return _Line(
        text, min(m["top"] for m in ms), max(m["bottom"] for m in ms),
        min(m["left"] for m in ms), max(m["right"] for m in ms),
        size=statistics.median(sizes) if sizes else None,
        stroke=statistics.median(m["stroke"] for m in ms),
        base0=ms[0]["base"], base1=ms[-1]["base"],
        pitch=(ms[-1]["base"] - ms[0]["base"]) / (len(ms) - 1) if len(ms) > 1 else None,
        span=float(sum(m["right"] - m["left"] for m in ms)),
        color=tuple(int(statistics.median(c[i] for c in colors)) for i in range(3)) if colors else None,
        bands=len(ms), heading=heading)


def _fill_colour(fill, colours, s, e, a=None, b=None):
    """The fill behind rows s:e (columns a:b), when it covers most of them."""
    region = fill[s:e, a:b]
    if not region.size or region.mean() < 0.5:
        return None
    rows = [c for c in colours[s:e] if c]
    return max(set(rows), key=rows.count) if rows else None


def _row_line(pg, mask, fill, colours, x0, y0, s, e, seg, cells, text, key, grid) -> _Line | None:
    band = mask[s:e]
    whole = pg.measure(x0, y0 + s, band, text)
    if whole is None:
        return None
    groups = _group_segments(seg, len(cells)) if seg else None
    measured = []
    for i, c in enumerate(cells):
        m = None
        if groups:
            a, b = groups[i]
            m = pg.measure(x0 + a, y0 + s, band[:, a:b], c)
            if m:
                m["fill"] = _fill_colour(fill, colours, s, e, a, b)   # a filled cell
        measured.append((c, m))
    # The row's baseline is read from its wordiest cell: a band that is half
    # digits has its densest rows in the middle of the numbers.
    wordiest = max((c for c in measured if c[1]), default=None,
                   key=lambda c: sum(ch.isalpha() for ch in c[0]))
    base = wordiest[1]["base"] if wordiest else whole["base"]
    # ...and its bottom from the cell that runs deepest: a name wrapped onto
    # a second line makes the whole row that much taller.
    last = max([c["base_last"] for _, c in measured if c] or [base])
    line = _Line(text, whole["top"], whole["bottom"], whole["left"], whole["right"],
                 size=None, stroke=whole["stroke"], color=whole["color"],
                 base0=base, base1=max(base, last), cells=measured, key=key, grid=grid)
    if key[0] == "table":                 # a shaded header row, a banded table
        line.shade = (_fill_colour(fill, colours, s, e)
                      or pg.background((x0 + 4, y0 + s, x0 + mask.shape[1] - 4, y0 + e)))
    return line


def _text_unit(pg, block, box, got, bi, pictures=()) -> _Unit:
    """A text block: its lines matched to the visual lines inside its box,
    each measured, and each split into cells when the page shows columns.
    `pictures`: the page's picture boxes, which a text box may overlap."""
    x0, y0, x1, y1 = box
    label = block["label"]
    heading = label == "SectionHeader"
    mask, fill, fillc = pg.text_mask(box)
    for px0, py0, px1, py1 in pictures:            # a logo's ink is not text
        a, b = max(px0, x0) - x0, min(px1, x1) - x0
        c, d = max(py0, y0) - y0, min(py1, y1) - y0
        if a < b and c < d:
            mask[c:d, a:b] = False
    ruled = None
    if label == "Table":
        # A table's own rules are not text: a vertical one would read as the
        # tallest letter of its row, a horizontal one would merge two rows.
        # A rule is thin and long; a filled header row is long but not thin.
        thin = max(6, int(0.006 * pg.w))
        lines = pg.faint[y0:y1, x0:x1] & ~fill
        hrules = [(a, b) for a, b in _runs((lines | fill).mean(axis=1) >= 0.6) if b - a <= thin]
        for a, b in hrules:
            mask[a:b] = False
        for a, b in _runs(lines.mean(axis=0) >= 0.6):
            if b - a <= thin:
                mask[:, a:b] = False
        # ...but the rules — and the edges of a filled row — are exactly where
        # its rows are, however many lines of text a row wraps to.
        cuts = sorted({a for a, _ in hrules}
                      | {v for a, b in _runs(fill.mean(axis=1) >= 0.6) if b - a > thin for v in (a, b)}
                      | {y1 - y0})
        ruled = []
        for a, b in zip([0] + cuts, cuts):
            # a light separator inside a filled header row spans the row's
            # full height, which no letter does
            for c0, c1 in _runs(mask[a:b].mean(axis=0) >= 0.95) if b - a > thin else []:
                if c1 - c0 <= thin:
                    mask[a:b, c0:c1] = False
            rows = np.flatnonzero(mask[a:b].any(axis=1))
            if rows.size:
                ruled.append([a + int(rows[0]), a + int(rows[-1]) + 1])
    bar = pg.bar(box) if heading else None
    if bar:
        lo, hi = max(0, bar["x0"] - x0), min(x1 - x0, bar["x1"] - x0)
        if hi > lo:
            mask[:, lo:hi] = False
    bands = _bands(mask)
    if ruled and len(ruled) == len(block["lines"]):
        bands = ruled                           # one band per ruled row
    if not bands:                               # nothing measurable (a faint scan)
        return _Unit("text", box, [_Line(t, y0, y1, x0, x1, heading=heading) for _, t in got])
    # Columns are told apart by the paper between them: on a free-standing form
    # a gap of a line and a half; inside a table, whose cells are padded
    # apart, a little over half a line — still wider than a space.
    gap = (lambda h: max(int(0.6 * h), 12)) if label == "Table" else (
        lambda h: max(int(1.5 * h), int(0.02 * pg.w)))
    segs = [_segments(mask[s:e], gap(e - s)) for s, e in bands]
    n = len(block["lines"])
    occ = _occupancy(n, [len(sg) for sg in segs])
    key = ("table", bi) if label == "Table" else None
    grid = pg.grid(box) if label == "Table" else None

    groups = []
    for li, text in got:
        where = occ[min(li, n - 1)]
        if groups and groups[-1][0] == where:
            groups[-1][1].append(text)
        else:
            groups.append((where, [text]))

    lines = []
    for where, texts in groups:
        s, e = bands[where[0]][0], bands[where[-1]][1]
        seg = segs[where[0]] if len(where) == 1 else []
        if len(texts) >= 2 and not heading and len(seg) == len(texts):
            row = _row_line(pg, mask, fill, fillc, x0, y0, s, e, seg, texts, "  ".join(texts),
                            key or ("form", len(texts)), grid)
            if row:
                lines.append(row)
                continue
        for text in texts:
            cells = None
            if not heading:
                if "|" in text:
                    cells = [c.strip() for c in _PIPE.split(text) if c.strip()]
                elif len(seg) >= 2 and (label == "Table"
                                        or seg[0][1] - seg[-1][0] >= COLUMNS_MIN_SPAN * pg.w):
                    # columns, not just a spaced-out line: a free-standing
                    # row must span a real share of the page ("1   من   2"
                    # in a corner is one line, not three cells)
                    cells = _cells(text) or _split_by_width(text, seg)
            row = None
            if cells and len(cells) >= 2:
                row = _row_line(pg, mask, fill, fillc, x0, y0, s, e, seg, cells, text,
                                key or ("form", len(cells)), grid)
            if row:
                lines.append(row)
                continue
            line = _para_line(pg, mask, x0, y0, [bands[k] for k in where], text, heading)
            skip = (bar["x0"], bar["x1"]) if bar else None
            line.shade = (_fill_colour(fill, fillc, line.top - y0, line.bottom - y0,
                                       line.left - x0, line.right - x0)
                          or pg.background(
                              (max(0, line.left - 4), max(0, line.top - 4),
                               min(pg.w, line.right + 4), min(pg.h, line.bottom + 4)), skip=skip))
            if line.shade:
                line.reach = pg.reach(line.left, line.right, line.top, line.bottom, line.shade)
            line.bar = bar
            lines.append(line)
    return _Unit("text", box, lines)


def _side_by_side(a: tuple, b: tuple) -> bool:
    ha, hb = a[3] - a[1], b[3] - b[1]
    wa, wb = a[2] - a[0], b[2] - b[0]
    over_y = min(a[3], b[3]) - max(a[1], b[1])
    over_x = min(a[2], b[2]) - max(a[0], b[0])
    return (over_y >= 0.5 * min(ha, hb) and max(ha, hb) <= 3 * min(ha, hb)
            and over_x <= 0.1 * min(wa, wb))


def _x_overlap(a: tuple, b: tuple) -> float:
    return min(a[2], b[2]) - max(a[0], b[0])


def _y_overlap(a: tuple, b: tuple) -> float:
    return min(a[3], b[3]) - max(a[1], b[1])


def _union(boxes) -> tuple:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _columns(units: list, cx0: float, cx1: float, pt: float) -> list:
    """Blocks that sit side by side on the page — a signature beside a stamp,
    a label block beside its value block, a title and subtitle beside a logo —
    become one unit, emitted as one table row with their columns. A block that
    sits under one column's block, still beside the other column, is stacked
    into that column (the subtitle under the title, next to the QR code)."""
    table = lambda u: any(l.key and l.key[0] == "table" for l in u.lines)
    ok = lambda u: u.kind in ("text", "picture") and not table(u)
    out, used = [], set()
    for i, u in enumerate(units):
        if i in used:
            continue
        if not ok(u):
            out.append(u)
            continue
        cols = [[u]]
        taken = {i}
        window = [j for j in range(i + 1, min(len(units), i + 13)) if j not in used and ok(units[j])]
        changed = True
        while changed:
            changed = False
            for j in window:
                if j in taken:
                    continue
                v = units[j]
                spans = [_union(g.box for g in c) for c in cols]
                w = v.box[2] - v.box[0]
                home = [k for k, sp in enumerate(spans) if _x_overlap(v.box, sp) >= 0.5 * w]
                if not home:
                    # a new column: clear of every column, beside at least one block
                    if (all(_x_overlap(v.box, sp) <= 0.1 * min(w, sp[2] - sp[0]) for sp in spans)
                            and any(_side_by_side(g.box, v.box) for c in cols for g in c)):
                        cols.append([v])
                        taken.add(j)
                        changed = True
                elif len(home) == 1 and len(cols) > 1:
                    k = home[0]
                    c = cols[k]
                    beside = any(_y_overlap(v.box, sp) >= 0.5 * (v.box[3] - v.box[1])
                                 for n, sp in enumerate(spans) if n != k)
                    stacked = all(_y_overlap(v.box, g.box) <= 0 for g in c)
                    if beside and stacked and all(g.kind == v.kind == "text" for g in c):
                        c.append(v)
                        taken.add(j)
                        changed = True
        if len(cols) == 1:
            out.append(u)
            continue
        used |= taken
        members = []
        for c in cols:
            c.sort(key=lambda g: g.box[1])
            if len(c) == 1:
                members.append(c[0])
            else:
                members.append(_Unit("text", _union(g.box for g in c),
                                     lines=[l for g in c for l in g.lines]))
        members.sort(key=lambda g: -g.box[2])        # rightmost first
        margin = CELL_MARGIN_PT / pt
        bounds = []
        for g, h in zip(members, members[1:]):
            if h.kind != "text":
                bounds.append((g.box[0] + h.box[2]) / 2)
                continue
            # One cell margin outside the left block's text, which Word will
            # right-align against it — so it ends where it ended on the page —
            # and never inside the right block's text.
            g_left = min((l.left for l in g.lines), default=g.box[0])
            h_right = max((l.right for l in h.lines), default=h.box[2])
            bounds.append(min(h_right + margin, g_left - 1))
        edges = [cx1] + bounds + [cx0]
        for g, (right, left) in zip(members, zip(edges, edges[1:])):
            g.col = (left, right)
        out.append(_Unit("columns", _union(g.box for g in members), members=members, bounds=bounds))
    return out


def _snapper(sizes: list, tol: float = 1.08):
    """Snap measured sizes to the few the document actually uses: sizes within
    `tol` of each other are one size, printed as their median to the half point."""
    groups = []
    for v in sorted(sizes):
        if groups and v <= groups[-1][0] * tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    table = [(g[0], g[-1], round(statistics.median(g) * 2) / 2) for g in groups]

    def snap(v):
        for lo, hi, rep in table:
            if lo <= v <= hi:
                return max(MIN_PT, min(MAX_PT, rep))
        return max(MIN_PT, min(MAX_PT, round(v * 2) / 2))
    return snap


def _where(left, right, cx0, cx1, multi=False) -> str:
    """right | left | center | both, from where the ink sits between cx0 and cx1."""
    cw = max(1.0, cx1 - cx0)
    gl, gr = left - cx0, cx1 - right
    if multi and gl < 0.04 * cw and gr < 0.04 * cw:
        return "both"
    if min(gl, gr) > 0.04 * cw and abs(gl - gr) < max(0.05 * cw, 0.3 * min(gl, gr)):
        return "center"
    return "right" if gr <= gl else "left"


def _finish_styles(pages: list) -> None:
    """Give every line and cell of every measured page its Style.

    Sizes are snapped across the whole document, so the letterhead that
    repeats on every page prints at one size, and headings of one level —
    measured one by one they scatter a few percent — share theirs."""
    texts = []
    for m in pages:
        for u in m.units:
            if u.kind == "text":
                texts.append((u, m))
            elif u.kind == "columns":
                texts += [(g, m) for g in u.members if g.kind == "text"]

    def settle(text, size, stroke, left, right, pt, fallback):
        guess = size or fallback
        wide = _width_size(text, right - left, pt, _is_bold(text, stroke, guess, pt))
        return min(size, wide) if size and wide else (size or wide)

    # Each line and cell: the smaller of its ascent and width readings (above).
    # A wrapped paragraph's text is as wide as its visual lines put end to end.
    for u, m in texts:
        for l in u.lines:
            if l.cells:
                for text, c in l.cells:
                    if c and c["lines"] == 1:
                        c["size"] = settle(text, c["size"], c["stroke"],
                                           c["left"], c["right"], m.pt, BODY_PT)
                        c["width_read"] = True
            elif l.bands == 1:
                l.size = settle(l.text, l.size, l.stroke, l.left, l.right, m.pt, BODY_PT)
            elif l.span:
                l.size = settle(l.text, l.size, l.stroke, 0, l.span, m.pt, BODY_PT)
    raw = []
    for u, _ in texts:
        for l in u.lines:
            if l.size:
                raw.append(l.size)
            for _, c in (l.cells or []):
                if c and c["size"]:
                    raw.append(c["size"])
    snap = _snapper(raw)
    counts = {}
    for v in raw:
        counts[snap(v)] = counts.get(snap(v), 0) + 1
    body = max(counts, key=counts.get) if counts else BODY_PT
    heads = [snap(l.size) for u, _ in texts for l in u.lines
             if l.heading and l.size and not _is_doc_title(l.text)]
    level = _snapper(heads, tol=1.12)
    # an unmeasurable heading (إقرار: too short) takes the headings' usual size
    levels = [level(h) for h in heads]
    heading = max(set(levels), key=levels.count) if levels else None

    for u, m in texts:
        known = [snap(l.size) for l in u.lines if l.size]
        default = statistics.median(known) if known else body
        cx0, cx1 = u.col if u.col else (m.content[0], m.content[2])
        for l in u.lines:
            size = snap(l.size) if l.size else default
            if l.heading and not l.size and heading and not _is_doc_title(l.text):
                size = heading
            if l.cells:
                # A cell with nothing tall to measure (a number, سعودي) takes
                # its row's measured size, not the whole band's.
                row = [snap(c["size"]) for _, c in l.cells if c and c["size"]]
                size = statistics.median(row) if row else default
                styles = []
                for text, c in l.cells:
                    cs = snap(c["size"]) if c and c["size"] else size
                    stroke = c["stroke"] if c else l.stroke
                    colour = c["color"] if c else l.color
                    pitch = c.get("pitch") if c else None
                    styles.append((text, Style(size=cs, bold=_is_bold(text, stroke, cs, m.pt),
                                               color=_text_color(colour),
                                               shade=c.get("fill") if c else None,
                                               line=(max(1.0, min(2.0, pitch * m.pt / (LINE_EM * cs)))
                                                     if pitch else 1.0))))
                l.cell_styles = styles
                l.style = Style(size=size)
                continue
            title = _is_doc_title(l.text)
            if l.heading and l.size and not title:
                size = level(size)
            if l.bands == 1:
                # a line that did not wrap on the page must not wrap in Word
                em = _em_width(l.text, _is_bold(l.text, l.stroke, size, m.pt))
                if em:
                    size = _fit(size, ((cx1 - cx0) * m.pt - 2) / em)
            bar = None
            if l.bar:
                bar = (l.bar["color"], max(0.75, min(6.0, l.bar["width"])), l.bar["side"])
            l.style = Style(
                size=size,
                bold=_is_bold(l.text, l.stroke, size, m.pt) or l.heading,
                color=_text_color(l.color),
                align=_where(l.left, l.right, cx0, cx1, multi=l.bands > 1),
                shade=l.shade, pad=min(20.0, l.reach) if l.shade else 0.0,
                bar=bar, outline=0 if title else (1 if l.heading else None),
                # a wrapped paragraph keeps the page's line pitch, exactly
                exact=(max(LINE_EM * size, min(3 * size, l.pitch * m.pt)) if l.pitch else None))


def _fit(size: float, limit) -> float:
    """`size`, but never above the half point at which the text still fits."""
    return min(size, max(MIN_PT, math.floor(limit * 2) / 2)) if limit else size


# Where Word puts a line's baseline in its box: 0.93 em below the top and
# 0.22 em above the bottom, 1.15 em in all (Arial, single spacing — measured).
ASCENT_EM, DESCENT_EM = 0.93, 0.22
LINE_EM = ASCENT_EM + DESCENT_EM


def _b0(l) -> float:
    return l.base0 if l.base0 is not None else l.bottom


def _b1(l) -> float:
    return l.base1 if l.base1 is not None else l.bottom


class _Spacing:
    """Spacing that puts each baseline where the page had it. It remembers
    the previous item's reference line (px) and how much of that item Word
    draws below it (pt); the gap above the next item is what is left over."""

    def __init__(self, pt: float, top: float):
        self.pt, self.y, self.below, self.pad = pt, top, 0.0, 0.0

    def before(self, y: float, above: float) -> float:
        return max(0.0, (y - self.y) * self.pt - self.below - above)

    def after(self, y: float, below: float, pad: float = 0.0) -> None:
        """`pad`: the part of `below` that is a shaded band's reach."""
        self.y, self.below, self.pad = y, below, pad


def _items(m: _Measured) -> list:
    """A measured page as Word items, each carrying the space above it."""
    items = []
    sp = _Spacing(m.pt, m.content[1])
    entries = []
    for u in m.units:
        if u.kind == "text":
            entries += [("line", l) for l in u.lines]
        else:
            entries.append((u.kind, u))

    k = 0
    while k < len(entries):
        kind, obj = entries[k]
        if kind == "line" and obj.cells:
            run = [obj]
            while (k + len(run) < len(entries) and entries[k + len(run)][0] == "line"
                   and entries[k + len(run)][1].cells and entries[k + len(run)][1].key == obj.key):
                run.append(entries[k + len(run)][1])
            t = _rows_table(m, run, 0.0)
            size = run[0].style.size
            t.before = sp.before(_b0(run[0]), ASCENT_EM * size + t.pad)
            sp.after(_b1(run[-1]), DESCENT_EM * size + t.pad)
            items.append(t)
            k += len(run)
            continue
        if kind == "columns":
            run = [obj]
            while (k + len(run) < len(entries) and entries[k + len(run)][0] == "columns"
                   and _same_columns(obj, entries[k + len(run)][1], m.content)):
                run.append(entries[k + len(run)][1])
            t = _columns_table(m, run, 0.0)
            # The row starts at whichever column reaches highest in Word and
            # ends at whichever reaches lowest: a text column by its first and
            # last baselines, a picture by its edges.
            tops, ends = [], []
            for g in obj.members:
                if g.kind == "picture":
                    tops.append((g.box[1], t.pad))
                elif g.lines:
                    tops.append((_b0(g.lines[0]), ASCENT_EM * g.lines[0].style.size + t.pad))
            for g in run[-1].members:
                if g.kind == "picture":
                    ends.append((g.box[3], t.pad))
                elif g.lines:
                    ends.append((_b1(g.lines[-1]), DESCENT_EM * g.lines[-1].style.size + t.pad))
            y, above = min(tops, key=lambda c: c[0] * m.pt - c[1]) if tops else (obj.top, t.pad)
            t.before = sp.before(y, above)
            y, below = max(ends, key=lambda c: c[0] * m.pt + c[1]) if ends else (run[-1].bottom, t.pad)
            sp.after(y, below)
            items.append(t)
            k += len(run)
            continue
        if kind == "line":
            st = obj.style
            prev = items[-1] if items else None
            pad = st.pad if st.shade else 0.0          # the band reaches past the text
            # With exact spacing Word sits the baseline a descent above the
            # bottom of the line box, whatever is left over going above it.
            ascent = (st.exact - DESCENT_EM * st.size) if st.exact else ASCENT_EM * st.size
            if (st.shade and isinstance(prev, Para) and prev.style.shade == st.shade
                    and prev.style.bar == st.bar):
                # Two shaded lines of one box: equal borders make Word draw them
                # as one band — no band edge between them, the gap shaded too.
                st.pad = prev.style.pad = pad = max(st.pad, prev.style.pad)
                before = sp.before(_b0(obj), ascent) + sp.pad
            else:
                before = sp.before(_b0(obj), ascent + pad)
            items.append(Para(obj.text, st, before=max(0.0, before)))
            sp.after(_b1(obj), DESCENT_EM * st.size + pad, pad)
        elif kind == "picture":
            x0, y0, x1, y1 = obj.box
            items.append(Picture(obj.png, (x1 - x0) * m.pt,
                                 align=_where(x0, x1, m.content[0], m.content[2]),
                                 before=sp.before(y0, 0.0)))
            sp.after(y1, 0.0)
        elif kind == "rule":
            r = obj.rule
            items.append(Rule(r["color"], r["weight"], before=sp.before(r["top"], 1.0)))
            sp.after(r["bottom"], 0.0)
        k += 1
    return items


def _row_pad(m: _Measured, rows, size) -> float:
    """Space above and below each row's text that reproduces the row pitch."""
    pitch = [(_b0(b) - _b1(a)) * m.pt for a, b in zip(rows, rows[1:])]
    if not pitch:
        return 1.0
    return max(0.0, min(18.0, (statistics.median(pitch) - LINE_EM * size) / 2))


def _rows_table(m: _Measured, run: list, before: float) -> Table:
    ncols = max(len(l.cells) for l in run)
    # One column prints at one size: a short label or a number measured on
    # its own is off by a few percent, the column's common size is not. And
    # columns within 12% of each other are one size too — a form's caption and
    # its value differ in weight and colour far more often than in size.
    common = []
    for c in range(ncols):
        # width readings, where the column has any, over height readings
        read = [(l.cell_styles[c][1].size, bool(l.cells[c][1] and l.cells[c][1].get("width_read")))
                for l in run if c < len(l.cell_styles)]
        sizes = [v for v, w in read if w] or [v for v, _ in read]
        common.append(max(set(sizes), key=lambda v: (sizes.count(v), v)))
    if max(common) <= min(common) * 1.12:
        every = [st.size for l in run for _, st in l.cell_styles]
        common = [round(statistics.median(every) * 2) / 2] * ncols
    # the table's wrapped cells share one line pitch, the most typical one
    pitches = [st.line for l in run for (_, c), (_, st) in zip(l.cells, l.cell_styles)
               if c and c.get("lines", 1) > 1]
    pitch = statistics.median(pitches) if pitches else 1.0
    for l in run:
        for c, (text, st) in enumerate(l.cell_styles):
            m_ = l.cells[c][1] if c < len(l.cells) else None
            st.size = common[c]
            st.line = pitch
            # weight is stroke against size: judged again at the settled size
            st.bold = _is_bold(text, m_["stroke"] if m_ else l.stroke, st.size, m.pt)
        l.style.size = max(st.size for _, st in l.cell_styles)
    rows = [[[Para(t, st)] if t else [] for t, st in l.cell_styles] for l in run]
    # a filled cell shades the whole cell, not just its paragraph
    cell_shades = [[st.shade for _, st in l.cell_styles] for l in run]
    for l in run:
        for _, st in l.cell_styles:
            st.shade = None
    cx0, cx1 = m.content[0], m.content[2]
    margin = CELL_MARGIN_PT / m.pt
    bounds = [[] for _ in range(ncols - 1)]
    for l in run:
        xs = [c for _, c in l.cells]
        if len(xs) != ncols or not all(xs):
            continue
        for i in range(ncols - 1):
            # the column boundary sits one cell margin outside the next cell's
            # text (which Word right-aligns against it), never inside this one's
            bounds[i].append(min(xs[i + 1]["right"] + margin, xs[i]["left"] - 1))
    grid = run[0].grid
    if grid and grid["right"] - grid["left"] > 0.2 * (cx1 - cx0):
        # A ruled table is as wide as its frame, not the text area.
        cx0, cx1 = grid["left"], grid["right"]
    inner = (grid or {}).get("cols", [])
    if grid and len(inner) == ncols - 1:
        bounds = [[x] for x in inner]             # the table's own rules: exact
    widths = None
    if ncols >= 2 and all(bounds):
        edges = [cx1] + [statistics.median(b) for b in bounds] + [cx0]
        fr = [max(0.05, (r - l) / max(1.0, cx1 - cx0)) for r, l in zip(edges, edges[1:])]
        widths = [f / sum(fr) for f in fr]
    elif ncols == 2:
        widths = list(FORM_COLS)
    if widths:
        # A cell that held one line on the page must hold one in Word: its
        # column less the cell margins, which can be wider than the
        # original's own padding.
        table_pt = (cx1 - cx0) * m.pt
        for l in run:
            for c, (text, st) in enumerate(l.cell_styles):
                meas = l.cells[c][1] if c < len(l.cells) else None
                if not meas or meas.get("lines", 1) != 1:
                    continue
                em = _em_width(text, st.bold)
                if em:
                    st.size = _fit(st.size, (widths[c] * table_pt - 2 * CELL_MARGIN_PT - 1) / em)
                    st.bold = _is_bold(text, meas["stroke"], st.size, m.pt)
        for l in run:
            l.style.size = max(st.size for _, st in l.cell_styles)
    pad = _row_pad(m, run, run[0].style.size)
    if grid:
        pad = max(0.0, pad - GRID_PT / 2)          # each row also carries one rule
    return Table(rows, widths=widths, grid=grid["color"] if grid else None, before=before,
                 pad=pad, width=(cx1 - cx0) * m.pt if grid else None,
                 shades=[[l.shade or c for c in cs] for l, cs in zip(run, cell_shades)]
                 if any(l.shade or any(cs) for l, cs in zip(run, cell_shades)) else None)


def _same_columns(a: _Unit, b: _Unit, content: tuple) -> bool:
    if len(a.members) != len(b.members):
        return False
    tol = 0.06 * (content[2] - content[0])
    return all(abs(x - y) <= tol for x, y in zip(a.bounds, b.bounds))


def _columns_table(m: _Measured, run: list, before: float) -> Table:
    cx0, cx1 = m.content[0], m.content[2]
    n = len(run[0].members)
    bounds = [statistics.median(u.bounds[i] for u in run) for i in range(n - 1)]
    edges = [cx1] + bounds + [cx0]
    widths = [max(0.05, (r - l) / max(1.0, cx1 - cx0)) for r, l in zip(edges, edges[1:])]
    widths = [w / sum(widths) for w in widths]
    rows = []
    for u in run:
        cells = []
        for g in u.members:
            if g.kind == "picture":
                x0, y0, x1, y1 = g.box
                cells.append([Picture(g.png, (x1 - x0) * m.pt, align=_where(x0, x1, *g.col))])
                continue
            paras, prev = [], None
            for l in g.lines:
                st = l.style or Style()
                if l.cells:                           # a row inside a column: its text, whole
                    text = "  ".join(t for t, _ in l.cells)
                    st = Style(size=st.size, align=_where(l.left, l.right, *g.col))
                else:
                    text = l.text
                gap = 0.0 if prev is None else max(
                    0.0, (_b0(l) - _b1(prev)) * m.pt - DESCENT_EM * (prev.style or st).size
                    - ASCENT_EM * st.size)
                paras.append(Para(text, st, before=gap))
                prev = l
            cells.append(paras)
        rows.append(cells)
    firsts = [g.lines[0] for u in run for g in u.members if g.lines]
    size = max((l.style.size for l in firsts if l.style), default=BODY_PT)
    lead = [min((l for g in u.members for l in g.lines[:1]), key=_b0, default=None) for u in run]
    lead = [l for l in lead if l is not None]
    return Table(rows, widths=widths, grid=None, before=before,
                 pad=_row_pad(m, lead, size) if len(lead) == len(run) else 1.0)


def _geometry_page(pg, text: str, blocks: list, margins: tuple) -> _Measured | None:
    """Measure a page from its layout. None when the layout cannot place the
    text (the caller falls back to the text-only path). `margins` are the
    document's (left, top, right) margins in points."""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    content = (margins[0] / pg.pt, margins[1] / pg.pt, pg.w - margins[2] / pg.pt)
    if not lines:
        return _Measured([], content, pg.pt)
    where = _assign(blocks, lines)
    if not any(where):
        return None
    got = [[] for _ in blocks]
    for j, w in enumerate(where):
        got[w[0]].append((w[1], lines[j]))
    units = []
    pictures = [pg.box(b["bbox"]) for b in blocks if b["label"] in PICTURE_LABELS]
    for bi, b in enumerate(blocks):
        box = pg.box(b["bbox"])
        if b["label"] in PICTURE_LABELS:
            if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
                units.append(_Unit("picture", box, png=pg.png(box)))
        elif got[bi]:
            units.append(_text_unit(pg, b, box, got[bi], bi, pictures))
    units = _columns(units, content[0], content[2], pg.pt)
    avoid = [u.box for u in units if u.kind in ("text", "columns")]
    for r in pg.rules(avoid):
        at = next((i for i, u in enumerate(units) if u.top >= r["top"]), len(units))
        units.insert(at, _Unit("rule", (r["left"], r["top"], r["right"], r["bottom"]), rule=r))
    return _Measured(units, content, pg.pt)


# ---------------- the page, from its text alone ----------------

def _form_table(rows: list) -> Table:
    """A run of rows -> one table. Two cells a row is a label/value form: the
    original prints it without rules, label quiet, value strong. Anything wider
    is a data table with a light grid — guessing a header row from linearised
    text is not reliable, so nothing in it is bolded."""
    ncols = max(len(r) for r in rows)
    form = ncols == 2 and all(len(r) == 2 for r in rows)
    out = []
    for r in rows:
        cells = []
        for c in range(ncols):
            txt = r[c] if c < len(r) else ""
            if form and c == 0:
                st = Style(LABEL_PT, color=LABEL_COLOR, line=TEXT_LINE)
            else:
                st = Style(LABEL_PT, bold=form, line=TEXT_LINE)
            cells.append([Para(txt, st)] if txt else [])
        out.append(cells)
    return Table(out, widths=list(FORM_COLS) if form else None,
                 grid=None if form else (0xBF, 0xBF, 0xBF), pad=2.0)


def _text_page(text: str, title_color, heading_color, first_page: bool = True) -> list:
    """A page with no layout: every line classified by its own shape."""
    lines = _fold_pairs([ln.strip() for ln in (text or "").split("\n") if ln.strip()])
    lines, cells = _fold_values(lines, _rows(lines))
    kinds = ["row" if c else _classify(ln) for ln, c in zip(lines, cells)]

    # Which line, if any, carries the document title. Without this the style
    # went to the FIRST heading on the page, which on every instrument in this
    # corpus is the ministry letterhead, not the title.
    title_at = next((k for k, ln in enumerate(lines)
                     if kinds[k] == "heading" and _is_doc_title(ln)), None)
    # Only page 1 may fall back to "first heading wins", and only when the page
    # never names itself. The letterhead repeats on every page of these
    # instruments, so without this a continuation page crowns its own header.
    if title_at is None and not first_page:
        title_at = -1
    title_used = False
    items, after_table = [], False
    i, n = 0, len(lines)
    while i < n:
        before = TEXT_AFTER_PT if after_table else 0.0
        after_table = False
        if kinds[i] == "row":
            j = i
            while j < n and kinds[j] == "row":
                j += 1
            if j - i >= 2:
                t = _form_table(cells[i:j])
                t.before = before
                items.append(t)
                after_table = True
            else:
                # A lone row is not a table. A pipe line still loses the pipe
                # (extract.py's internal cell separator); anything else is
                # written VERBATIM — its colon is the document's own.
                txt = "  ".join(cells[i]) if "|" in lines[i] else lines[i]
                items.append(Para(txt, Style(BODY_PT, line=TEXT_LINE),
                                  before=before, after=TEXT_AFTER_PT))
            i = j
            continue
        line = lines[i]
        if kinds[i] == "heading":
            # The title is styled as one whether or not a colour was sampled:
            # its size and centring come from being the title, not from Surya.
            is_title = (i == title_at) if title_at is not None else not title_used
            if is_title:
                st = Style(TITLE_PT, bold=True, color=title_color, align="center",
                           outline=0, line=TEXT_LINE)
                title_used = True
            else:
                st = Style(HEADING_PT, bold=True, color=heading_color, outline=1,
                           line=TEXT_LINE)
        else:
            st = Style(BODY_PT, line=TEXT_LINE)
        items.append(Para(line, st, before=before, after=TEXT_AFTER_PT))
        i += 1
    return items


# ---------------- colour sampling (text-only path) ----------------

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


# ---------------- the caller's layout ----------------

LAYOUT_MAX_BLOCKS = 800
LAYOUT_MAX_LINES = 4000
LAYOUT_MAX_LINE_CHARS = 20_000


def clean_layouts(raw, n_pages: int) -> list:
    """Validate the per-page `layout` a caller sends back (the one /extract
    returned). One entry per page: a cleaned {"blocks": [...]} or None.

    A malformed page is dropped whole rather than repaired: a layout missing a
    block no longer accounts for every line, and that page is better served by
    the text-only path. Raises ValueError when `raw` is not a list."""
    if not isinstance(raw, list):
        raise ValueError("layout must be a list")
    return [_clean_page(p) for p in raw[:n_pages]]


def _clean_page(p):
    if not isinstance(p, dict) or not isinstance(p.get("blocks"), list):
        return None
    blocks = p["blocks"]
    if len(blocks) > LAYOUT_MAX_BLOCKS:
        return None
    out, total = [], 0
    for b in blocks:
        if not isinstance(b, dict):
            return None
        label, box, lines = b.get("label"), b.get("bbox"), b.get("lines")
        if not isinstance(label, str) or len(label) > 40:
            return None
        if not (isinstance(box, list) and len(box) == 4 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in box)):
            return None
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in box)
        if x1 <= x0 or y1 <= y0:
            return None
        if not isinstance(lines, list) or not all(isinstance(t, str) for t in lines):
            return None
        total += len(lines)
        if total > LAYOUT_MAX_LINES:
            return None
        out.append({"label": label, "bbox": [x0, y0, x1, y1],
                    "lines": [t[:LAYOUT_MAX_LINE_CHARS] for t in lines]})
    return {"blocks": out}


# ---------------- assembly ----------------

class _Source:
    """The original document: a PDF rendered page by page, or one image."""

    def __init__(self, data: bytes, filename: str):
        self.pdf = self.image = None
        if data[:5] == b"%PDF-" or (filename or "").lower().endswith(".pdf"):
            self.pdf = pdfium.PdfDocument(data)
            self.count = len(self.pdf)
        else:
            self.image = Image.open(io.BytesIO(data)).convert("RGB")
            self.count = 1

    def size(self, i: int) -> tuple:
        return tuple(self.pdf[i].get_size()) if self.pdf is not None else _image_page_size(self.image)

    def render(self, i: int):
        if self.pdf is not None:
            return self.pdf[i].render(scale=LAYOUT_DPI / 72).to_pil().convert("RGB")
        return self.image

    def close(self) -> None:
        if self.pdf is not None:
            self.pdf.close()


def build_layout_docx(doc_bytes: bytes, pages_text, filename: str = "document",
                      layouts=None) -> bytes:
    """Build a formatted .docx from the original document and its page texts.

    `doc_bytes` is the original PDF (rendered page by page) or a single raster
    image (one page). `pages_text[i]` is page i's text as it should read — the
    OCR with the reader's proofreading decisions applied. `layouts[i]` is that
    page's layout from /extract (cleaned by `clean_layouts`), or None."""
    doc = Document()
    nf = doc.styles["Normal"].font; nf.name = ARABIC_FONT; nf.size = Pt(BODY_PT)
    src = _Source(doc_bytes, filename)
    try:
        # Only pages we have text for are rendered and analysed; the caller
        # caps pages_text, which bounds all the work below.
        n_pages = min(src.count, len(pages_text))
        layouts = list(layouts or []) if USE_GEOMETRY else []
        lay = [layouts[i] if i < len(layouts) else None for i in range(n_pages)]
        # Paper size and margins come from page 1 of the ORIGINAL, before any
        # content is laid out: a wrong page size reflows every line.
        first = None
        title_color = heading_color = None
        if n_pages:
            try:
                first = src.render(0)
                _page_setup(doc.sections[0], src.size(0), first)
            except Exception:
                first = None                  # keep the template's Letter default
            # The palette is only for pages without a layout, and it is colour
            # only: page 1's (the letterhead repeats) is the document's.
            if USE_PALETTE and first is not None and not all(lay):
                try:
                    title_color, heading_color = _palette(first)
                except Exception:
                    pass
        # Pass 1: measure every page that has a layout (each render is
        # dropped as soon as its page is measured). Pass 2: style them all
        # together, then write every page in order.
        sec0 = doc.sections[0]
        margins = (sec0.left_margin.pt, sec0.top_margin.pt, sec0.right_margin.pt)
        measured = []
        for pi in range(n_pages):
            m = None
            if lay[pi]:
                try:
                    img = first if pi == 0 and first is not None else src.render(pi)
                    m = _geometry_page(_Page(img, src.size(pi)), pages_text[pi] or "",
                                       lay[pi]["blocks"], margins)
                except Exception as exc:
                    print("layout export: page %d falls back to text-only:" % (pi + 1), ascii(exc))
            measured.append(m)
        _finish_styles([m for m in measured if m is not None])
        last = None
        for pi in range(n_pages):
            new_page = pi > 0
            size = src.size(pi)
            sec = doc.sections[-1]
            if new_page and (abs(sec.page_width.pt - size[0]) > 0.02 * size[0]
                             or abs(sec.page_height.pt - size[1]) > 0.02 * size[1]):
                sec = doc.add_section(WD_SECTION.NEW_PAGE)   # starts the new page itself
                sec.page_width, sec.page_height = Pt(size[0]), Pt(size[1])
                new_page = False
            if measured[pi] is not None:
                items = _items(measured[pi])
            else:
                items = _text_page(pages_text[pi] or "", title_color, heading_color,
                                   first_page=(pi == 0))
            last = _emit(doc, items, new_page) or last
        if last == "t":
            _spacer(doc, 1.0)                 # a document body must end in a paragraph
    finally:
        src.close()
    bio = io.BytesIO(); doc.save(bio); return bio.getvalue()
