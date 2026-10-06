"""Compare what the document says with what the register answered.

Input: the structured result of the document and one ``/wathq/query`` answer
as the page received it (personal values already masked). Both are flattened
to ``label: value`` lines. Numbers and dates are compared exactly by code; the
rest (names, statuses, cities, free text) is judged by the local model, which
answers only with item ids and a verdict from a fixed set:

    matches · partly_matches · differs · not_in_document

A register line the model does not rule on is ``not_compared``. A difference
is always worded as a difference from the register, never as a judgement on
the document.
"""
from __future__ import annotations

import re

from wathq_suggest import ascii_digits, flatten, fold

VERDICTS = {
    "matches": "يطابق",
    "partly_matches": "يطابق جزئيًا",
    "differs": "يختلف عن سجل وثق",
    "not_in_document": "غير وارد في المستند",
    "not_compared": "لم تتم مقارنته",
}
JUDGED = ("matches", "partly_matches", "differs", "not_in_document")
MAX_REGISTER = 60          # register lines shown to the model
MAX_DOCUMENT = 60          # document lines shown to the model
MAX_VALUE = 160
SKIP_VALUES = {"بيانات أعمق لم تُعرض"}

# Arabic labels for the bespoke contract view (shape_contract in wathq_verify).
_CONTRACT = [
    (("contract", "copy_number"), "رقم نسخة العقد"), (("contract", "date"), "تاريخ العقد"),
    (("entity", "national_number"), "الرقم الوطني الموحد"), (("entity", "cr_number"), "رقم السجل التجاري"),
    (("entity", "name"), "اسم الشركة"), (("entity", "entity_type"), "نوع الكيان"),
    (("entity", "legal_form"), "الشكل القانوني"), (("entity", "duration"), "مدة الشركة"),
    (("entity", "headquarters"), "مدينة المقر الرئيسي"), (("management", "structure"), "الهيكل الإداري"),
]
_CAPITAL = {"total": "رأس المال", "cash": "رأس المال النقدي", "in_kind": "رأس المال العيني",
            "share_value": "قيمة الحصة", "shares": "عدد الحصص", "currency": "عملة رأس المال"}


def _clip(value) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:MAX_VALUE]


def _add(out, label, value):
    text = _clip(value)
    if text and text not in SKIP_VALUES and len(out) < MAX_REGISTER:
        out.append((_clip(label), text))


def _walk(nodes, prefix, out):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        label = _clip(n.get("label"))
        name = f"{prefix} - {label}" if prefix else label
        kind = n.get("type")
        if kind == "field":
            _add(out, name, n.get("value"))
        elif kind == "group":
            _walk(n.get("children"), name, out)
        elif kind == "list":
            _add(out, name, "، ".join(_clip(x) for x in (n.get("items") or [])[:8]))
        elif kind == "table":
            cols = [_clip(c) for c in n.get("columns") or []]
            for i, row in enumerate((n.get("rows") or [])[:10], 1):
                for col, cell in zip(cols, row if isinstance(row, list) else []):
                    _add(out, f"{name} {i} - {col}", cell)
        elif kind == "cards":                   # each card is a group already named "X (i)"
            for item in (n.get("items") or [])[:10]:
                _walk([item] if isinstance(item, dict) and item.get("type") else item, prefix, out)
        if len(out) >= MAX_REGISTER:
            return


def _contract_pairs(result) -> list:
    out = []
    for path, label in _CONTRACT:
        node = result
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        _add(out, label, node)
    capital = result.get("capital") if isinstance(result.get("capital"), dict) else {}
    for key, label in _CAPITAL.items():
        _add(out, label, capital.get(key))
    for i, p in enumerate((result.get("parties") or [])[:10], 1):
        if isinstance(p, dict):
            _add(out, f"الشريك {i} - الاسم", p.get("name"))
            _add(out, f"الشريك {i} - رقم الهوية", p.get("id") or p.get("id_masked"))
            _add(out, f"الشريك {i} - الجنسية", p.get("nationality"))
            _add(out, f"الشريك {i} - الحصص", p.get("shares") or p.get("share"))
    management = result.get("management") if isinstance(result.get("management"), dict) else {}
    for i, m in enumerate((management.get("managers") or [])[:10], 1):
        if isinstance(m, dict):
            _add(out, f"المدير {i} - الاسم", m.get("name"))
            _add(out, f"المدير {i} - رقم الهوية", m.get("id") or m.get("id_masked"))
    acts = [a.get("name") for a in result.get("activities") or [] if isinstance(a, dict) and a.get("name")]
    if acts:
        _add(out, "الأنشطة", "، ".join(acts[:8]))
    return out


def register_pairs(result) -> list:
    """The register answer as (label, value) lines, as the page shows it."""
    if not isinstance(result, dict):
        return []
    if result.get("view_type") == "contract":
        return _contract_pairs(result)
    out = []
    _walk(result.get("view"), "", out)
    return out


_NUMERIC = re.compile(r"^[0-9\s\-/.:,،٠-٩۰-۹هـمH%٪]+$")


def _digits(value: str) -> str:
    d = re.sub(r"\D", "", fold(value))
    return d


def _date_forms(value: str) -> set:
    """A date's digits in the two usual orders (y-m-d and d-m-y)."""
    parts = re.findall(r"\d+", ascii_digits(value))
    if len(parts) != 3:
        return set()
    return {"".join(parts), "".join(reversed(parts))}


def _plain(value: str) -> str:
    """Digits only; a decimal fraction's trailing zeros dropped (240.0 → 240)."""
    text = ascii_digits(value)
    if re.search(r"\d\.\d", text):
        text = re.sub(r"(\d)\.(\d*?)0+(?!\d)", lambda m: m.group(1) + ("." + m.group(2) if m.group(2) else ""), text)
    return re.sub(r"\D", "", text)


def exact(doc_pairs, reg_pairs) -> dict:
    """Code verdicts for numbers, dates and masked IDs: index -> verdict."""
    verdicts = {}
    doc_digits = []
    for _, v in doc_pairs:
        # all the digits, and the first number on its own ("٢٤٠ م٢" is 240, not 2402)
        first = re.search(r"\d[\d.,]*", ascii_digits(v))
        doc_digits.append(({_digits(v), _plain(first.group(0)) if first else ""} - {""}, _date_forms(v)))
    for i, (_, value) in enumerate(reg_pairs):
        if "•" in value:                                   # a masked ID: compare the visible tail
            tail = _digits(value)
            if len(tail) >= 4 and any(d.endswith(tail) for ds, _ in doc_digits for d in ds):
                verdicts[i] = "matches"
            else:
                verdicts[i] = "not_compared"
            continue
        if not _NUMERIC.match(value):
            continue
        digits = _plain(value)
        if len(digits) < 3:
            continue
        forms = _date_forms(value)
        for ds, df in doc_digits:
            if digits in ds or (forms and forms & df):
                verdicts[i] = "matches"
                break
    return verdicts


def prompt(doc_pairs, reg_pairs, pending) -> str:
    doc = "\n".join(f"d{i+1}: {k}: {v}" for i, (k, v) in enumerate(doc_pairs))
    reg = "\n".join(f"r{i+1}: {reg_pairs[i][0]}: {reg_pairs[i][1]}" for i in pending)
    return ("قارن ما ورد في المستند بما أعاده سجل وثق.\n\n"
            f"بنود المستند:\n{doc}\n\n"
            f"بنود السجل المطلوب مقارنتها:\n{reg}\n\n"
            "لكل بند من بنود السجل اختر بند المستند المقابل له والحكم: "
            "matches إذا كانت القيمتان الشيء نفسه ولو اختلفت الصياغة (اسم بلا لقب، مدينة بصيغة أخرى، "
            "حالة بكلمة مرادفة)؛ partly_matches إذا تطابق جزء منهما؛ differs إذا اختلفتا؛ "
            "not_in_document إذا لم يذكر المستند هذا البند أصلًا (واترك بند المستند فارغًا). "
            "احكم بالقيم نفسها لا بتشابه أسماء البنود: بندان بالاسم نفسه وقيمتين مختلفتين حكمهما differs. "
            "لا تخمّن ولا تحكم على صحة المستند.")


def schema(doc_count: int, pending) -> dict:
    return {"type": "object", "required": ["rows"],
            "properties": {"rows": {"type": "array", "maxItems": len(pending), "items": {
                "type": "object", "required": ["register", "document", "verdict"],
                "properties": {"register": {"type": "string", "enum": [f"r{i+1}" for i in pending]},
                               "document": {"type": "string", "enum": [f"d{i+1}" for i in range(doc_count)] + [""]},
                               "verdict": {"type": "string", "enum": list(JUDGED)}}}}}}


def compare(struct, result, judge) -> dict:
    """Rows of (label, document value, register value, verdict).

    ``judge(prompt, schema, max_tokens)`` returns the model's parsed answer."""
    doc_pairs = flatten(struct)[:MAX_DOCUMENT]
    reg_pairs = register_pairs(result)
    verdicts = exact(doc_pairs, reg_pairs)
    by = {i: "code" for i in verdicts}
    links = {}
    pending = [i for i in range(len(reg_pairs)) if i not in verdicts]
    if pending and doc_pairs:
        # the model pretty-prints: about 35 tokens a row
        answer = judge(prompt(doc_pairs, reg_pairs, pending), schema(len(doc_pairs), pending),
                       min(2500, 80 + 40 * len(pending)))
        for row in (answer.get("rows") if isinstance(answer, dict) else None) or []:
            if not isinstance(row, dict):
                continue
            try:
                r = int(str(row.get("register"))[1:]) - 1
                d = int(str(row.get("document"))[1:]) - 1 if row.get("document") else None
            except ValueError:
                continue
            verdict = row.get("verdict")
            if r not in pending or verdict not in JUDGED or r in links:
                continue
            if d is not None and not 0 <= d < len(doc_pairs):
                d = None
            if verdict != "not_in_document" and d is None:
                continue                                    # a verdict needs a counterpart
            verdicts[r], by[r] = verdict, "model"
            links[r] = d
            # Numbers and dates are never the model's call: once it has named the
            # counterpart, the two values are compared exactly.
            if d is not None and _NUMERIC.match(reg_pairs[r][1]) and _NUMERIC.match(doc_pairs[d][1]):
                same = (_plain(reg_pairs[r][1]) == _plain(doc_pairs[d][1])
                        or bool(_date_forms(reg_pairs[r][1]) & _date_forms(doc_pairs[d][1])))
                verdicts[r], by[r] = ("matches" if same else "differs"), "code"
    elif pending and not doc_pairs:
        for i in pending:
            verdicts[i], by[i] = "not_in_document", "code"
    rows, counts = [], {k: 0 for k in VERDICTS}
    for i, (label, value) in enumerate(reg_pairs):
        verdict = verdicts.get(i, "not_compared")
        d = links.get(i)
        if d is None and verdict == "matches" and by.get(i) == "code":
            # the exact pass found the number: name the document line that carries it
            wanted = _plain(value) if "•" not in value else _digits(value)
            for j, (_, dv) in enumerate(doc_pairs):
                first = re.search(r"\d[\d.,]*", ascii_digits(dv))
                mine = {_digits(dv), _plain(first.group(0)) if first else ""}
                if wanted and (wanted in mine or ("•" in value and _digits(dv).endswith(wanted))
                               or _date_forms(dv) & _date_forms(value)):
                    d = j
                    break
        rows.append({"label": label, "register": value,
                     "document": doc_pairs[d][1] if d is not None else "",
                     "document_label": doc_pairs[d][0] if d is not None else "",
                     "verdict": verdict, "verdict_ar": VERDICTS[verdict], "by": by.get(i, "")})
        counts[verdict] += 1
    return {"rows": rows, "counts": counts, "document_lines": len(doc_pairs)}
