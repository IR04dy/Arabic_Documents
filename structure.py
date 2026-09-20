"""Structuring stage: OCR text -> fields, guided by the classified template.

Two modes.

GENERIC (`parse_structure`) — the behaviour for any document the input layer
could not classify: Qwen3 returns the document's own labelled fields grouped
under its own section headings, schema-constrained. The pass is given every
output token the input leaves free (a tabular deed's JSON is several times
LONGER than its text), and when the model still runs out, the text is split at
a line boundary and each half is structured on its own.

TEMPLATE-DRIVEN (`parse_structure_for_template`) — once classify.py has named a
template, the registry says what MUST be in the document and what MAY be. The
document is then read in three steps:

  1. the generic pass above, over the whole text — it already harvests every
     printed label/value pair the document carries;
  2. alignment, in pure Python, mapping those pairs onto the template's declared
     field keys through the registry's normalised labels and aliases. Generic
     aliases (الاسم, رقم الهوية) are only accepted when the section they were
     found under disambiguates which role they belong to — from the heading, or
     failing that from the nearest role-specific label beside them — because a
     template that instantiates person_identity for three roles prints that same
     label three times. Fields the registry marks `repeatable` are collected as
     RECORDS, one row per person/side/clause: a label coming round again inside
     the same record group starts a new row, so a ten-heir table yields ten
     heirs, not one heir with ten names;
  3. targeted, schema-constrained passes asking Qwen3 for what is STILL empty:
     an array-of-rows pass for a required record group alignment found no rows
     for, then the declared scalar fields — required first. Their catalogues are
     small precisely because alignment already did the bulk, so the document
     text keeps almost the whole context window.

Every value then goes through a fidelity check against the OCR text: a number,
date or ID the model rewrote into the other digit system is put back in the
document's own form, and a value the document does not carry verbatim is marked
`unverified` rather than shown as fact. The same match yields the value's
PROVENANCE — page, line, character span and the source line as a quote
(provenance.py) — so the UI can jump to it and chat can cite it. A record row's
cells are cited on the line of the row's most distinctive value (a name or an
ID, never a relation that repeats down the table).

Whatever alignment could not place stays in the result as `additional` — "the
other information found in the document". Nothing the OCR read is discarded.

A required field that comes back empty is reported, not hidden: that is a
completeness finding about the document, and chat.py tells the model not to
invent a value for it.

The GPU server lifecycle lives in llm.py; this module re-exports ensure_loaded /
stop_server / status so existing callers keep working.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field as dc_field

from guard import map_tokens, protected_tokens
from llm import N_CTX, chat_json
from provenance import Locator
from registry import Normalizer
from llm import ensure_loaded as ensure_loaded   # re-export for app.py
from llm import status as status                  # re-export (server status)
from llm import stop_server as stop_server        # re-export
from expiry import detect as detect_expiry

MAX_INPUT_CHARS = int(os.environ.get("STRUCTURE_MAX_INPUT_CHARS", "12000"))
MAX_NEW_TOKENS = int(os.environ.get("STRUCTURE_MAX_NEW_TOKENS", "4096"))

# Targeted registry passes for SCALAR fields. 1 = required fields only; the
# default 2 adds one optional-field sweep. Everything past that is left to
# alignment, which is free — raising this buys diminishing recall for a whole
# extra model call.
REGISTRY_MAX_PASSES = int(os.environ.get("STRUCTURE_REGISTRY_PASSES", "2"))
# Array-of-rows passes: one per required record group that alignment left
# empty. A deed rarely carries more than one such table.
RECORD_MAX_PASSES = int(os.environ.get("STRUCTURE_RECORD_PASSES", "2"))

# Context budgeting. Matches chat.py's conservative estimator: Arabic tokenizes
# denser than the usual ~2 chars/token, so over-estimate the cost of text.
_CHARS_PER_TOKEN = 1.5
_MARGIN_TOKENS = 512
_DOC_FLOOR_CHARS = 3000        # never squeeze the document below this
_SECTION_MATCH_MIN = 6         # floor for containment-based heading match

# The generic pass REWRITES the document as label/value JSON. For a tabular
# deed that JSON is several times longer than the text it came from (measured
# on a ten-heir حصر ورثة: 1.3k chars in, 6.3k chars out), so the output must
# never be budgeted as a fraction of the input. It gets every token the input
# leaves free, never fewer than this floor — and the INPUT is what gets capped
# so the floor always fits.
_GENERIC_OUT_FLOOR = 1536
# If the model still stops at the cap, split the text at a line boundary and
# structure each half with the whole output budget to itself. Depth 2 bounds
# the retry at four chunks (seven calls) for a pathological document.
_SPLIT_MAX_DEPTH = 2
_SPLIT_MIN_CHARS = 600

_EXTRAS_SECTION_AR = "معلومات أخرى في المستند"

SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_type": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["label", "value"],
                        },
                    },
                },
                "required": ["title", "fields"],
            },
        },
    },
    "required": ["document_type", "sections"],
}

SYSTEM_PROMPT = (
    "You are a precise document-structuring assistant for Arabic and English "
    "forms and contracts. You are given the raw text of ONE document that was "
    "extracted by OCR. Reorganise it into structured data. Rules:\n"
    "- Group fields under the document's OWN section headings (e.g. "
    "\"بيانات صاحب العمل\", \"بيانات العامل\"). If the document has no explicit "
    "headings, use one section with an empty title.\n"
    "- The text may contain TABLES or header rows — cells laid out in columns or "
    "separated by \"|\" pipes. Extract EVERY cell as a field. Use the table's "
    "column header as the label when there is one; if a value has no explicit "
    "label (e.g. a reference/contract number or code such as \"CON-...\" at the "
    "top of the document, a job title, or an ID), give it a short descriptive "
    "label in the document's language. NEVER drop reference numbers, contract/"
    "document numbers, IDs, or codes — always capture them.\n"
    "- Each field is a label and its value, the VALUE copied VERBATIM from the "
    "text. Keep the original language and the exact digits/letters.\n"
    "- Do NOT translate, do NOT summarise, do NOT invent labels or values, do "
    "NOT reformat numbers. If a labelled field has no value in the text, use an "
    "empty string.\n"
    "- Values must be SHORT field values only: names, numbers, dates, amounts, "
    "codes, or short phrases. NEVER put a full sentence, a paragraph, a contract "
    "clause/article body, or a preamble into a value. Skip the document's "
    "clauses/articles and any long narrative text entirely.\n"
    "- Only include information that actually appears in the text.\n"
    "- Set document_type to a short label in the document's language "
    "(e.g. \"عقد عمل\").\n"
    "Return ONLY the JSON object."
)

TARGETED_PROMPT = (
    "You extract named fields from ONE Arabic legal instrument (صك). The "
    "document has already been identified, and you are given the exact list of "
    "fields it is expected to carry: each has a machine key, its Arabic label "
    "as the document prints it, and other wordings the same field may appear "
    "under. Rules:\n"
    "- For each key, return the value EXACTLY as it appears in the text — same "
    "digits, same spelling, same letters. Do not translate, normalise, reformat "
    "or convert digit systems.\n"
    "- If the document does not carry a field, return an empty string for it. "
    "An empty string is a CORRECT answer. Never guess, never infer a value from "
    "another field, never copy a value from a different field.\n"
    "- Beware of repeated labels: a deed may print \"الاسم\" or \"رقم الهوية\" "
    "once per person. Use the section heading and the surrounding wording to "
    "take the one belonging to the role named in the field's label.\n"
    "- Take values the document ITSELF states. If the document quotes another "
    "instrument (بموجب الصك رقم …), do not take that instrument's numbers as "
    "this document's own.\n"
    "- Values are short: names, numbers, dates, amounts, codes, short phrases. "
    "Never return a clause or a paragraph.\n"
    "Return ONLY the JSON object."
)

RECORD_PROMPT = (
    "You extract a TABLE from ONE Arabic legal instrument (صك). The document "
    "has already been identified, and it carries a repeated block — one row per "
    "person, side or clause. You are told what one row is (e.g. الوارث) and the "
    "exact columns to return: each has a machine key, its Arabic label as the "
    "document prints it, and other wordings it may appear under. Rules:\n"
    "- Return ONE object per row, in document order. Never merge two rows into "
    "one object, and never join several rows' values with commas.\n"
    "- Copy each value EXACTLY as it appears in the text — same digits, same "
    "spelling. Do not translate, normalise, reformat or convert digit systems.\n"
    "- A column the row does not carry is an empty string. An empty string is a "
    "CORRECT answer. Never guess, and never copy a value from another row or "
    "another column.\n"
    "- Rows are the repeated block only. Do not add the document's other "
    "parties (e.g. المتوفى on a حصر ورثة) as rows.\n"
    "Return ONLY the JSON object."
)

_PAGE_MARKER = re.compile(r"^--- Page \d+ ---\s*$", re.MULTILINE)
_TO_ASCII = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)


@dataclass
class Field:
    label: str
    value: str
    key: str = ""              # registry field key, "" for undeclared extras
    required: bool = False
    status: str = ""           # ok | empty | mismatch | unverified  (registry validation)
    origin: str = ""           # aligned | model | document
    source: dict | None = None # {page, line, start, end, quote[, approx]} in the OCR text

    def to_dict(self) -> dict:
        d = {"label": self.label, "value": self.value}
        if self.key:
            d.update(key=self.key, required=self.required,
                     status=self.status, origin=self.origin)
        elif self.origin:
            d["origin"] = self.origin
        if self.source:
            d["source"] = self.source
        return d


@dataclass
class Section:
    title: str
    fields: list                                     # scalar fields
    records: list = dc_field(default_factory=list)   # rows of Field, one per person/side
    record_label: str = ""                           # what one row is: الوارث, الشاهد

    def to_dict(self) -> dict:
        d = {"title": self.title, "fields": [f.to_dict() for f in self.fields]}
        if self.records:
            d["record_label"] = self.record_label
            d["records"] = [[f.to_dict() for f in row] for row in self.records]
        return d


@dataclass
class StructureResult:
    document_type: str
    sections: list
    truncated: bool                  # the INPUT was cut to fit the window
    output_capped: bool = False      # the model ran out of OUTPUT tokens
    template_id: str = ""
    template_name_ar: str = ""
    missing_required: list = dc_field(default_factory=list)
    extras: int = 0
    passes: int = 0
    expiry: dict | None = None  # expiry.py's verdict on the document's own expiry

    def to_dict(self) -> dict:
        d = {
            "document_type": self.document_type,
            "truncated": self.truncated,
            "output_capped": self.output_capped,
            "sections": [s.to_dict() for s in self.sections],
        }
        if self.template_id:
            d.update(template_id=self.template_id,
                     template_name_ar=self.template_name_ar,
                     missing_required=self.missing_required,
                     extras=self.extras,
                     passes=self.passes)
        if self.expiry is not None:
            d["expiry"] = self.expiry
        return d


# =============================================================================
# Budgets
# =============================================================================


def _est_tokens(chars: int) -> int:
    return int(chars / _CHARS_PER_TOKEN) + 1


def _input_chars(out_tokens: int) -> int:
    """Document chars that fit alongside an `out_tokens` output reservation."""
    avail = max(1024, N_CTX - out_tokens - _MARGIN_TOKENS)
    return int(avail * _CHARS_PER_TOKEN)


def _generic_fixed_tokens() -> int:
    return _est_tokens(len(SYSTEM_PROMPT)) + 32       # prompt + chat template


def _generic_out_tokens(doc_chars: int) -> int:
    """Everything the input leaves free, within [floor, MAX_NEW_TOKENS]."""
    free = N_CTX - _MARGIN_TOKENS - _generic_fixed_tokens() - _est_tokens(doc_chars)
    return max(_GENERIC_OUT_FLOOR, min(MAX_NEW_TOKENS, free))


def _generic_input_cap() -> int:
    """How much text the generic pass may actually send.

    MAX_INPUT_CHARS is a ceiling, not a promise. The window has to hold the
    prompt, the document AND at least _GENERIC_OUT_FLOOR tokens of answer; what
    does not fit is cut from the tail of the document, never from its head
    (where the deed number and the title are). Raise STRUCTURE_N_CTX to send
    more (Qwen3 is GQA, so its KV cache grows slowly).
    """
    room = N_CTX - _MARGIN_TOKENS - _GENERIC_OUT_FLOOR - _generic_fixed_tokens()
    return max(_DOC_FLOOR_CHARS, min(MAX_INPUT_CHARS, int(room * _CHARS_PER_TOKEN)))


def _clean(text: str) -> tuple:
    """Drop page markers; cap length so the prompt fits the context window."""
    text = _PAGE_MARKER.sub("", text).strip()
    cap = _generic_input_cap()
    if len(text) > cap:
        return text[:cap], True
    return text, False


# =============================================================================
# Fidelity: values against the text they were extracted from
# =============================================================================


class _Fidelity:
    """Check extracted values against the source text.

    The models are told to copy verbatim and still turn ١٠٠١١٦٧٦٤٠ into
    1001167640 or السعودية into سعودي. guard.py's protected-token patterns
    (IDs, dates, digit runs, IBANs) are reused: every such token in a value
    must exist in the document, and one that exists only in the other digit
    system is put back in the document's own form. A value the document does
    not carry verbatim is not discarded — the user still sees it — but it is
    reported as `unverified` instead of as fact.

    The check is a search of the text, so it also says WHERE the value is:
    `locate` returns the page, line and span of the match (provenance.py). A
    value not carried verbatim is cited, approximately, on the most specific
    number or date it does carry, if any.
    """

    def __init__(self, source: str, normalize=None):
        self._by_ascii: dict = {}
        for tok in protected_tokens(source):
            self._by_ascii.setdefault(tok.translate(_TO_ASCII), tok)
        # Page markers stay in: a citation has to know its page. Matches that
        # touch a marker line are ignored by the locator. `normalize` is the
        # registry's Normalizer OBJECT (reg.normalizer, which carries the
        # offset-preserving with_index); a bare callable gets the defaults.
        if not hasattr(normalize, "with_index"):
            normalize = Normalizer()
        self.locator = Locator(source or "", normalize)

    def restore(self, value: str) -> str:
        """Digits back to the document's own form; tokens it lacks are left alone."""
        return map_tokens(value, lambda t: self._by_ascii.get(t.translate(_TO_ASCII), t))

    def check(self, value: str) -> tuple:
        """(value with digits restored, verified)."""
        if not value or not value.strip():
            return value, True
        missing = False

        def swap(tok: str) -> str:
            nonlocal missing
            src = self._by_ascii.get(tok.translate(_TO_ASCII))
            if src is None:
                missing = True                # a number the document never prints
                return tok
            return src

        out = map_tokens(value, swap)
        if missing:
            return out, False
        return out, self.locator.has(out)

    def locate(self, value: str, **where) -> dict | None:
        """Citation for `value` (see Locator.find for the hints), or None.

        A value the text does not carry verbatim is cited on its longest
        number/date token instead and flagged `approx`, so a rewritten
        1443/04/09 هـ still points at the line that prints ١٤٤٣/٠٤/٠٩."""
        if not value or not value.strip():
            return None
        span = self.locator.find(value, **where)
        if span is not None:
            return span
        for tok in sorted(set(protected_tokens(value)), key=len, reverse=True):
            span = self.locator.find(tok, **where)
            if span is not None:
                span["approx"] = True
                return span
        return None


def _expiry(text: str, result: StructureResult, fid: "_Fidelity") -> dict | None:
    """The document's own expiry date, and what it means today (expiry.py).

    Runs on the RAW text, never on `cleaned`: a date past MAX_INPUT_CHARS is
    exactly the case the prompt cannot reach. The fidelity helpers are handed
    over so the date is cited and checked like every other value. Never raises —
    an expiry verdict is an addition to the result, not a precondition for it.
    """
    try:
        return detect_expiry(
            text,
            sections=[s.to_dict() for s in result.sections],
            locate=lambda v, labels=(): fid.locate(v, labels=list(labels)),
            verify=lambda v: fid.check(v)[1],
            restore=fid.restore,
        )
    except Exception as exc:
        print("expiry detection failed:", repr(exc))
        return None

# =============================================================================
# Generic pass
# =============================================================================


def _loads_lenient(raw: str) -> dict:
    """Parse the model's JSON, salvaging a run cut off at the token cap."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cut = raw.rfind("}")
    while cut != -1:
        candidate = raw[: cut + 1]
        for suffix in ("", "]}", "}]}", "]}]}"):
            try:
                return json.loads(candidate + suffix)
            except json.JSONDecodeError:
                continue
        cut = raw.rfind("}", 0, cut)
    return {"document_type": "", "sections": []}


def _generic_call(text: str) -> tuple:
    """One schema-constrained call. Returns (data, hit_output_cap)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Document text:\n\n" + text},
    ]
    raw, finish = chat_json(messages, SCHEMA, _generic_out_tokens(len(text)))
    return _loads_lenient(raw), finish == "length"


def _split_lines(text: str) -> tuple:
    """Halve the text at the line break nearest its middle."""
    mid = len(text) // 2
    cut = text.rfind("\n", 0, mid)
    if cut < len(text) // 4:
        nxt = text.find("\n", mid)
        if nxt != -1 and nxt < len(text) * 3 // 4:
            cut = nxt
    if cut <= 0:
        cut = mid
    return text[:cut].strip(), text[cut:].strip()


def _merge_generic(a: dict, b: dict) -> dict:
    """Concatenate two chunks' output. A section that continues across the cut
    (same title, or both untitled) is joined back into one, so a table split
    between the halves comes out as one run of rows."""
    merged: list = []
    for s in list(a.get("sections") or []) + list(b.get("sections") or []):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title", ""))
        fields = [f for f in (s.get("fields") or []) if isinstance(f, dict)]
        if merged and merged[-1]["title"] == title:
            merged[-1]["fields"].extend(fields)
        else:
            merged.append({"title": title, "fields": list(fields)})
    return {"document_type": str(a.get("document_type") or b.get("document_type") or ""),
            "sections": merged}

def _generic_pass(text: str, depth: int = 0) -> tuple:
    """(data, output_capped). When the model runs out of output tokens the
    text is split and each half structured with the whole budget to itself."""
    data, capped = _generic_call(text)
    if not capped or depth >= _SPLIT_MAX_DEPTH or len(text) < 2 * _SPLIT_MIN_CHARS:
        return data, capped
    head, tail = _split_lines(text)
    if not head or not tail:
        return data, capped
    print(f"structure: generic pass hit the output cap on {len(text)} chars; splitting")
    d1, c1 = _generic_pass(head, depth + 1)
    d2, c2 = _generic_pass(tail, depth + 1)
    return _merge_generic(d1, d2), c1 or c2


def parse_structure(text: str, *, fidelity: "_Fidelity | None" = None,
                    with_expiry: bool = True) -> StructureResult:
    """Structure OCR/proofread text into section-grouped label/value fields.

    `with_expiry` is False for the generic pass run INSIDE the template path:
    that result is scaffolding for alignment, and detecting expiry on it would
    run the whole detection twice per request."""
    cleaned, truncated = _clean(text or "")
    if not cleaned:
        return StructureResult("", [], truncated)

    data, capped = _generic_pass(cleaned)
    # Digits back to the document's own form, and each value cited to its
    # line. Built on the RAW text (markers included) so citations know pages.
    fid = fidelity or _Fidelity(text or "")

    def field(f: dict) -> Field:
        label = str(f.get("label", ""))
        value = fid.restore(str(f.get("value", "")))
        return Field(label, value, origin="document",
                     source=fid.locate(value, labels=[label]))

    sections = [
        Section(
            title=str(s.get("title", "")),
            fields=[field(f) for f in s.get("fields", []) if isinstance(f, dict)],
        )
        for s in data.get("sections", [])
        if isinstance(s, dict)
    ]
    result = StructureResult(str(data.get("document_type", "")), sections, truncated,
                             output_capped=capped)
    if with_expiry:
        result.expiry = _expiry(text or "", result, fid)
    return result


# =============================================================================
# Template-driven pass
# =============================================================================


def _catalogue_line(f, *, with_aliases: int) -> str:
    """One field as the model sees it: key, printed label, alternate wordings."""
    line = f"- {f.key} | {f.label_ar}"
    if with_aliases:
        alts = [a for a in f.aliases_ar[:with_aliases] if a and a != f.label_ar]
        if alts:
            line += " | also: " + "، ".join(alts)
    if f.section:
        line += f" | section: {f.section}"
    return line + "\n"


def _budget(n_keys: int) -> tuple:
    """(max_new_tokens, total input chars) for a targeted pass of n_keys."""
    out = min(MAX_NEW_TOKENS, 96 + 48 * max(1, n_keys))
    return out, _input_chars(out)


def _pack(fields: list, overhead: int, with_aliases: int) -> list:
    """Split fields into passes whose catalogue still leaves room for the text."""
    passes: list = []
    current: list = []
    used = 0
    for f in fields:
        line = len(_catalogue_line(f, with_aliases=with_aliases))
        _, total = _budget(len(current) + 1)
        room = total - overhead - _DOC_FLOOR_CHARS
        if current and used + line > max(400, room):
            passes.append(current)
            current, used = [], 0
        current.append(f)
        used += line
    if current:
        passes.append(current)
    return passes


def _targeted_pass(text: str, fields: list, template, *, with_aliases: int) -> dict:
    """Ask the model for these field keys. Returns {key: value}."""
    if not fields:
        return {}
    catalogue = "".join(_catalogue_line(f, with_aliases=with_aliases) for f in fields)
    out_tokens, total = _budget(len(fields))
    header = (f"Document: {template.name_ar} ({template.name_en}).\n"
              f"Fields to extract:\n{catalogue}\nDocument text:\n\n")
    room = max(_DOC_FLOOR_CHARS, total - len(header) - len(TARGETED_PROMPT))

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fields": {
                "type": "object",
                "additionalProperties": False,
                "properties": {f.key: {"type": "string"} for f in fields},
                "required": [f.key for f in fields],
            }
        },
        "required": ["fields"],
    }
    messages = [
        {"role": "system", "content": TARGETED_PROMPT},
        {"role": "user", "content": header + text[:room]},
    ]
    raw, _ = chat_json(messages, schema, out_tokens)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = _loads_lenient(raw)
    got = data.get("fields")
    if not isinstance(got, dict):
        return {}
    return {k: str(v) for k, v in got.items() if isinstance(v, (str, int, float))}


# ---- record groups ----------------------------------------------------------


def _record_groups(template) -> dict:
    """Repeatable fields grouped by the section they repeat in.

    One group is one record shape — the heir row, the boundary side, the
    witness. Keyed by section because a row spans group-instance fields
    (heir_name, from person_identity) AND the template's own local fields
    (heir_relation, heir_share), and the section is the one thing they share.
    """
    groups: dict = {}
    for f in template.fields:
        if f.repeatable:
            groups.setdefault(f.section_norm, []).append(f)
    return {k: tuple(v) for k, v in groups.items()}


def _group_of(f, groups: dict):
    return f.section_norm if f.repeatable and f.section_norm in groups else None


def _record_label(shape) -> str:
    """What one row is, for the UI and the chat prompt: the instance's role
    name (الوارث) when the group came from an instantiated field group, else
    the section it lives in."""
    for f in shape:
        if getattr(f, "role_label_ar", ""):
            return f.role_label_ar
    return shape[0].section if shape else ""


def _record_budget(n_keys: int) -> tuple:
    """(max_new_tokens, total input chars) for an array-of-rows pass. The row
    count is unknown up front; reserve for a dozen rows of n_keys cells."""
    out = min(MAX_NEW_TOKENS, max(1024, 128 + 40 * n_keys * 12))
    return out, _input_chars(out)


def _record_columns(shape, text_norm: str, required_keys: set) -> list:
    """Columns worth asking for: required ones, plus any whose label or alias
    is printed somewhere in the document. person_identity carries
    commercial-register and 700-number columns no heir table prints."""
    keep = []
    for f in shape:
        if f.key in required_keys:
            keep.append(f)
            continue
        names = (f.label_ar_norm,) + tuple(f.aliases_ar_norm) + tuple(f.shared_aliases_ar_norm)
        if any(n and n in text_norm for n in names):
            keep.append(f)
    return keep


def _record_pass(text: str, shape, template, reg, required_keys: set) -> list:
    """Ask the model for a record group as an array of rows. Returns [{key: value}]."""
    fields = _record_columns(shape, reg.normalize(text), required_keys)
    if not fields:
        return []
    label = _record_label(shape) or "سجل"
    catalogue = "".join(_catalogue_line(f, with_aliases=3) for f in fields)
    out_tokens, total = _record_budget(len(fields))
    header = (f"Document: {template.name_ar} ({template.name_en}).\n"
              f"One row = one {label}.\nColumns to extract per row:\n{catalogue}\n"
              f"Document text:\n\n")
    room = max(_DOC_FLOOR_CHARS, total - len(header) - len(RECORD_PROMPT))

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {f.key: {"type": "string"} for f in fields},
                    "required": [f.key for f in fields],
                },
            }
        },
        "required": ["records"],
    }
    messages = [
        {"role": "system", "content": RECORD_PROMPT},
        {"role": "user", "content": header + text[:room]},
    ]
    raw, _ = chat_json(messages, schema, out_tokens)
    data = _loads_lenient(raw)
    rows = data.get("records")
    if not isinstance(rows, list):
        return []
    out: list = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = {k: str(v).strip() for k, v in r.items()
               if isinstance(v, (str, int, float)) and str(v).strip()}
        if row:
            out.append(row)
    return out


# ---- alignment ---------------------------------------------------------------


def _alias_index(template, reg) -> tuple:
    """Two lookups from normalised label -> field.

    `exact` holds role-specific labels and aliases, which are safe to match
    outright. A label claimed by more than one field maps to None rather than
    guessing. `shared` holds the field groups' generic labels, which are correct
    but ambiguous whenever a template instantiates one group for several roles —
    those are only accepted when a heading or a neighbouring role-specific
    label resolves the role.
    """
    exact: dict = {}
    for f in template.fields:
        for norm in (f.label_ar_norm,) + f.aliases_ar_norm:
            if not norm:
                continue
            if norm in exact and exact[norm] is not None and exact[norm].key != f.key:
                exact[norm] = None
            else:
                exact.setdefault(norm, f)
    shared: dict = {}
    for f in template.fields:
        for norm in f.shared_aliases_ar_norm:
            if norm:
                shared.setdefault(norm, []).append(f)
    return exact, shared


def _section_match(doc: str, declared: str) -> bool:
    """Forgiving heading comparison, both already normalised.

    A document rarely prints the registry's exact string — it heads a block
    بيانات الصك where the template declares بيانات الصك والمحكمة. Containment
    either way covers that; the length floor stops a short fragment matching an
    unrelated heading. Normalisation still does NOT fold ى to ي, so
    الموصى لهم never collides with الموصي.
    """
    if not doc or not declared:
        return False
    if doc == declared:
        return True
    if min(len(doc), len(declared)) < _SECTION_MATCH_MIN:
        return False
    return doc in declared or declared in doc


def _nearest_role(pool: list, index: int, hits: list):
    """Which of `pool` does the nearest role-specific label vote for?

    `hits` are (position, field) for the labels in this generic section that
    matched a declared label outright — اسم المتوفى, صلة القرابة. A hit
    supports a candidate when they share a declared section and, when the hit
    names a role, that role too. A hit that supports more than one candidate
    is no evidence at all. Nearest wins; the preceding one on a tie, since a
    row's cells follow its first label.
    """
    best, best_d = None, None
    for j, hit in hits:
        supported = [c for c in pool
                     if c.section_norm == hit.section_norm
                     and (hit.role is None or c.role is None or hit.role == c.role)]
        if len(supported) != 1:
            continue
        d = abs(j - index)
        if best_d is None or d < best_d or (d == best_d and j < index):
            best, best_d = supported[0], d
    return best


def _shared_target(pool: list, section_norm: str, index: int = 0, hits: list = ()):
    """Resolve a generic label (الاسم, رقم الهوية) to one role, or refuse.

    Refusing is not a loss: the value goes to `additional`, where the user still
    sees it. Attaching a heir's name to the deceased would be a loss.
    """
    if not pool:
        return None
    if section_norm:
        scoped = [f for f in pool if _section_match(section_norm, f.section_norm)]
        if len(scoped) == 1:
            return scoped[0]
        if scoped:                   # the heading holds two roles: ask the neighbours
            return _nearest_role(scoped, index, hits)
        # The model's heading (ورثة المتوفى) matched nothing the registry
        # declares (بيانات الورثة). The labels beside it still know.
    near = _nearest_role(pool, index, hits)
    if near is not None:
        return near
    # No evidence at all. Accept only a label that exactly one field in the
    # whole template claims, and only when the document gave no heading to weigh.
    return pool[0] if len(pool) == 1 and not section_norm else None


def _align(free: StructureResult, template, reg) -> tuple:
    """Map the generic pass's pairs onto declared keys.

    Returns (values, records, extras): scalar values by key, rows by record
    group, and everything that could not be placed.
    """
    exact, shared = _alias_index(template, reg)
    groups = _record_groups(template)
    values: dict = {}
    records: dict = {g: [] for g in groups}
    open_rows: dict = {}
    extras: list = []

    def close(g: str) -> None:
        row = open_rows.pop(g, None)
        if row:
            records[g].append(row)

    for section in free.sections:
        section_norm = reg.normalize(section.title or "")
        items = [(reg.normalize(it.label or ""), it.label or "", it.value or "")
                 for it in section.fields if (it.value or "").strip()]

        # 1) labels the registry knows outright, and where they sit. A label
        #    claimed by two fields was stored as None: it stays unplaced rather
        #    than guessed — a wrong role is worse than an unplaced value.
        targets = [exact.get(norm) if norm in exact else None for norm, _, _ in items]
        hits = [(i, t) for i, t in enumerate(targets) if t is not None]

        # 2) generic labels, resolved by the heading or by their neighbours
        for i, (norm, _, _) in enumerate(items):
            if norm in exact:
                continue
            targets[i] = _shared_target(shared.get(norm, []), section_norm, i, hits)

        # 3) place, in document order. Inside a record group a key coming round
        #    again means the next row has begun — that is how a ten-heir table
        #    becomes ten rows and not one heir with ten names.
        for (norm, label, value), target in zip(items, targets):
            if target is None:
                extras.append((section.title or "", label, value))
                continue
            g = _group_of(target, groups)
            if g is not None:
                row = open_rows.setdefault(g, {})
                if target.key in row:
                    close(g)
                    row = open_rows.setdefault(g, {})
                row[target.key] = value
            elif target.key not in values:
                values[target.key] = value
            else:
                extras.append((section.title or "", label, value))

    for g in list(open_rows):
        close(g)
    return values, records, extras


def parse_structure_for_template(text: str, template, reg) -> StructureResult:
    """Structure against a classified template: declared fields + everything else."""
    cleaned, truncated = _clean(text or "")
    if not cleaned:
        return StructureResult("", [], truncated,
                               template_id=template.id,
                               template_name_ar=template.name_ar)
    # One fidelity/provenance index over the raw text serves every pass.
    fidelity = _Fidelity(text or "", reg.normalizer)

    # 1) the generic pass reads the whole document exactly as it always has
    free = parse_structure(text, fidelity=fidelity, with_expiry=False)
    truncated = truncated or free.truncated

    # 2) deterministic alignment onto declared keys and record rows
    values, records, extras = _align(free, template, reg)
    aligned_keys = set(values)      # snapshot: the targeted passes add to `values`
    record_origin = {g: "aligned" for g, rows in records.items() if rows}
    passes = 1

    required, optional = reg.resolve(template.id)
    required_keys = {f.key for f in required}
    groups = _record_groups(template)

    # 3a) a required table alignment found no rows for: ask for it AS ROWS.
    #     Asking for its keys as scalars is what produced one heir with ten
    #     comma-joined names.
    record_passes = 0
    for g, shape in groups.items():
        if records.get(g) or not any(f.key in required_keys for f in shape):
            continue
        if record_passes >= RECORD_MAX_PASSES:
            break
        try:
            rows = _record_pass(cleaned, shape, template, reg, required_keys)
        except Exception as exc:
            print("record structure pass failed:", repr(exc))
            break
        passes += 1
        record_passes += 1
        if rows:
            records[g] = rows
            record_origin[g] = "model"

    # 3b) targeted passes for the scalar fields still missing — required first
    def scalar(f) -> bool:
        return _group_of(f, groups) is None

    overhead = len(TARGETED_PROMPT) + 400
    missing_required = [f for f in required
                        if scalar(f) and not values.get(f.key, "").strip()]
    missing_optional = [f for f in optional
                        if scalar(f) and not values.get(f.key, "").strip()]

    queue = [(b, 3) for b in _pack(missing_required, overhead, with_aliases=3)]
    remaining = max(0, REGISTRY_MAX_PASSES - len(queue))
    if remaining and missing_optional:
        queue += [(b, 2) for b in
                  _pack(missing_optional, overhead, with_aliases=2)[:remaining]]

    for batch, aliases in queue:
        try:
            got = _targeted_pass(cleaned, batch, template, with_aliases=aliases)
        except Exception as exc:
            print("targeted structure pass failed:", repr(exc))
            break
        passes += 1
        for f in batch:
            value = (got.get(f.key) or "").strip()
            if value and not values.get(f.key, "").strip():
                values[f.key] = value

    result = _assemble(template, reg, values, extras, truncated, passes,
                       aligned_keys, records=records, record_origin=record_origin,
                       output_capped=free.output_capped, fidelity=fidelity)
    result.expiry = _expiry(text or "", result, fidelity)
    return result


def _assemble(template, reg, values: dict, extras: list,
              truncated: bool, passes: int,
              aligned_keys: set | None = None, *,
              records: dict | None = None, record_origin: dict | None = None,
              output_capped: bool = False,
              fidelity: "_Fidelity | None" = None) -> StructureResult:
    """Lay the resolved fields out in the registry's own section order."""
    required, optional = reg.resolve(template.id)
    required_keys = {f.key for f in required}
    aligned = aligned_keys if aligned_keys is not None else set(values)
    records = records or {}
    record_origin = record_origin or {}
    groups = _record_groups(template)

    def make(f, value: str, origin: str, **where) -> Field:
        verified, source = True, None
        if fidelity is not None and value:
            value, verified = fidelity.check(value)
            source = fidelity.locate(value, labels=[f.label_ar, *f.all_aliases_ar], **where)
        status = f.validate(value, reg)
        if status == "ok" and not verified:
            status = "unverified"
        return Field(label=f.label_ar, value=value, key=f.key,
                     required=f.key in required_keys, status=status, origin=origin,
                     source=source)

    def make_row(columns: list, row: dict, origin: str, after: int | None) -> tuple:
        """One record row, every cell cited on the row's own line.

        The anchor is the row's longest value — a name or an ID number is
        unique to its row, a relation or a nationality repeats down the table.
        It is searched AFTER the previous row's anchor so two rows that print
        the same value resolve in document order. Returns (fields, anchor)."""
        cells = {f.key: str(row.get(f.key, "") or "") for f in columns}
        anchor = None
        if fidelity is not None:
            for key in sorted((k for k, v in cells.items() if v.strip()),
                              key=lambda k: -len(cells[k])):
                restored, _ = fidelity.check(cells[key])
                anchor = fidelity.locator.find(restored, after=after)
                if anchor is not None:
                    break
        # `start` is published in the browser's UTF-16 units; the hints below
        # index the Python string, so convert before feeding it back.
        at = fidelity.locator.code_point(anchor["start"]) if anchor else None
        where = {"same_line_as": at} if at is not None else {}
        return ([make(f, cells[f.key], origin, **where) for f in columns],
                at if at is not None else after)

    def missing_entry(f) -> dict:
        return {"key": f.key, "label_ar": f.label_ar, "label_en": f.label_en}

    by_section: dict = {}
    rows_by_section: dict = {}
    missing: list = []

    # scalars
    for f in required + optional:
        if _group_of(f, groups) is not None:
            continue
        value = values.get(f.key, "")
        is_required = f.key in required_keys
        if not value and not is_required:
            continue                       # absent optional fields stay out of the UI
        fld = make(f, value, "aligned" if f.key in aligned else "model")
        if is_required and fld.status == "empty":
            missing.append(missing_entry(f))
        by_section.setdefault(f.section or "", []).append(fld)

    # records: every row shares one column set, in the registry's order
    for g, shape in groups.items():
        rows = records.get(g) or []
        present = {k for row in rows for k in row}
        section = shape[0].section or ""
        for f in shape:
            if f.key in required_keys and f.key not in present:
                missing.append(missing_entry(f))
                if not rows:               # keep the empty required field visible
                    by_section.setdefault(section, []).append(
                        Field(label=f.label_ar, value="", key=f.key, required=True,
                              status="empty", origin="model"))
        if not rows:
            continue
        columns = [f for f in shape if f.key in present or f.key in required_keys]
        origin = record_origin.get(g, "model")
        made: list = []
        after: int | None = None
        for row in rows:
            fields, after = make_row(columns, row, origin, after)
            made.append(fields)
        rows_by_section[section] = (_record_label(shape), made)

    names = list(template.sections)
    names += [n for n in list(by_section) + list(rows_by_section) if n not in names]
    sections: list = []
    seen: set = set()
    for name in names:
        if name in seen or (name not in by_section and name not in rows_by_section):
            continue
        seen.add(name)
        label, rows = rows_by_section.get(name, ("", []))
        sections.append(Section(title=name, fields=by_section.get(name, []),
                                records=rows, record_label=label))

    if extras:
        sections.append(Section(
            title=_EXTRAS_SECTION_AR,
            fields=[Field(label=label, value=value, origin="document",
                          source=fidelity.locate(value, labels=[label]) if fidelity else None)
                    for _, label, value in extras]))

    return StructureResult(
        document_type=template.name_ar,
        sections=sections,
        truncated=truncated,
        output_capped=output_capped,
        template_id=template.id,
        template_name_ar=template.name_ar,
        missing_required=missing,
        extras=len(extras),
        passes=passes,
    )
