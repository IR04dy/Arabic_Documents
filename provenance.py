"""Where each extracted value came from.

The structurer already checks every value against the OCR text in normalised
space (digits folded, tashkeel dropped, alef forms unified). This module keeps
the offsets that check throws away: the normaliser's `with_index` hands back
the raw position of every normalised character, so a match in normalised space
is a span of the original text — and a span has a page, a line and a quote.

A `--- Page N ---` marker line belongs to no page: it starts page N, and the
lines after it are numbered from 1 (blank lines count, so the client can
reproduce the numbering from the same text). Text before the first marker, or
text with no markers at all, is page 1. Matches that touch a marker line are
ignored: a value of "1" is not evidenced by "--- Page 1 ---".
"""

from __future__ import annotations

import bisect
import re

_PAGE_MARKER = re.compile(r"^--- Page (\d+) ---[ \t]*$", re.MULTILINE)
_QUOTE_MAX = 200          # a cited line longer than this is windowed around the hit
_FAR = 10 ** 6            # a candidate BEFORE its label costs this much extra


class Locator:
    """Find values in a text and report where they sit."""

    def __init__(self, text: str, normalize):
        self.text = text or ""
        self._normalize = normalize
        self.norm, self._index = normalize.with_index(self.text)

        # Offsets are reported the way the BROWSER indexes the same string: in
        # UTF-16 code units, not Python code points. They differ only past an
        # astral character (an emoji, a U+1EE00 Arabic mathematical letter, a
        # Rumi digit), which OCR can emit — and one such character anywhere in
        # the page would shift every later citation by one, silently marking the
        # wrong characters. `_astral` holds their positions so the conversion is
        # a bisect, not a rescan per citation.
        self._astral = [i for i, ch in enumerate(self.text) if ord(ch) > 0xFFFF]
        self._astral16 = [a + i for i, a in enumerate(self._astral)]

        # line table: start offset, page, line-in-page, is-a-marker
        self._starts = [0] + [m.end() for m in re.finditer("\n", self.text)]
        n = len(self._starts)
        self._page = [1] * n
        self._line = [0] * n
        self._marker = [False] * n
        page, k = 1, 0
        for i, start in enumerate(self._starts):
            end = self._starts[i + 1] - 1 if i + 1 < n else len(self.text)
            m = _PAGE_MARKER.match(self.text, start, end)
            if m and m.end() == end:
                page, k = int(m.group(1)), 0
                self._marker[i] = True
            else:
                k += 1
            self._page[i] = page
            self._line[i] = k

    # ------------------------------------------------------------------ text

    def norm_value(self, value: str) -> str:
        return self._normalize.with_index(value or "")[0]

    def _line_at(self, offset: int) -> int:
        return bisect.bisect_right(self._starts, offset) - 1

    def _touches_marker(self, start: int, end: int) -> bool:
        first, last = self._line_at(start), self._line_at(max(start, end - 1))
        return any(self._marker[first:last + 1])

    def find_all(self, value: str) -> list:
        """Every raw (start, end) span where `value` occurs, in text order."""
        nv = self.norm_value(value)
        if not nv:
            return []
        spans: list = []
        pos = 0
        while True:
            j = self.norm.find(nv, pos)
            if j < 0:
                break
            s = self._index[j]
            e = self._index[j + len(nv) - 1] + 1
            # The normaliser drops tashkeel, tatweel and bidi marks, and a
            # dropped character has no index — so a value ending in one is cited
            # one character short: the highlight over ١٤٤٣/٠٤/١٩ هـ would stop
            # before the ـ. Extend over the dropped characters that follow, up
            # to the next character that survived normalisation (never across
            # whitespace, which would swallow the following word's separator).
            nxt = (self._index[j + len(nv)] if j + len(nv) < len(self._index)
                   else len(self.text))
            while (e < nxt and not self.text[e].isspace()
                   and not self._normalize(self.text[e])):
                e += 1
            if not self._touches_marker(s, e):
                spans.append((s, e))
            pos = j + 1
        return spans

    def has(self, value: str) -> bool:
        return bool(self.find_all(value))

    # -------------------------------------------------------------- choosing

    def find(self, value: str, *, labels=(), near: int | None = None,
             same_line_as: int | None = None, after: int | None = None) -> dict | None:
        """The span of `value` that best fits the hints, as a citation dict.

        Precedence: a hit on the same line as `same_line_as` (a row's other
        cells sit on its line), else the hit nearest `near` (or `same_line_as`
        as a fallback), else the hit just after the nearest printed label in
        `labels`, else the first hit after `after`, else the first hit.
        """
        cands = self.find_all(value)
        if not cands:
            return None
        if same_line_as is not None:
            line = self._line_at(same_line_as)
            on_line = [c for c in cands if self._line_at(c[0]) == line]
            if on_line:
                cands = on_line
            elif near is None:
                near = same_line_as
        if near is not None:
            pick = min(cands, key=lambda c: (abs(c[0] - near), c[0]))
            return self.span(*pick)
        anchors = [s for lab in labels if lab for s, _ in self.find_all(lab)]
        if anchors:
            def cost(c):
                return min((c[0] - a) if c[0] >= a else (a - c[0]) + _FAR
                           for a in anchors)
            return self.span(*min(cands, key=lambda c: (cost(c), c[0])))
        if after is not None:
            later = [c for c in cands if c[0] > after]
            if later:
                return self.span(*later[0])
        return self.span(*cands[0])

    # --------------------------------------------------------------- output

    def _utf16(self, offset: int) -> int:
        """A code-point offset as the same position in a JavaScript string."""
        if not self._astral:
            return offset
        return offset + bisect.bisect_left(self._astral, offset)

    def code_point(self, offset: int) -> int:
        """The inverse of `_utf16`: a citation's `start` back in Python space.

        Callers that feed one citation's offset back as a hint (`near`,
        `after`, `same_line_as`) must convert first — the hints index `text`,
        the published offsets index the browser's copy of it."""
        if not self._astral:
            return offset
        return offset - bisect.bisect_left(self._astral16, offset)

    def span(self, start: int, end: int) -> dict:
        li = self._line_at(start)
        ls = self._starts[li]
        le = self._starts[li + 1] - 1 if li + 1 < len(self._starts) else len(self.text)
        if le - ls <= _QUOTE_MAX:
            quote = self.text[ls:le].strip()
        else:                                  # window the long line around the hit
            a = max(ls, start - _QUOTE_MAX // 3)
            b = min(le, end + _QUOTE_MAX // 3)
            quote = ("…" if a > ls else "") + self.text[a:b].strip() + ("…" if b < le else "")
        return {"page": self._page[li], "line": self._line[li],
                "start": self._utf16(start), "end": self._utf16(end),
                "quote": quote}
