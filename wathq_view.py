"""Any Wathq answer → a safe, labelled view the verify-data tab can render.

Wathq has eight products and ~40 endpoints, each answering with its own
JSON. Instead of a hand-written screen per endpoint, this module turns any
answer into a small tree of display nodes:

    {"type": "field", "label": "…", "value": "…"}
    {"type": "group", "label": "…", "children": [node, …]}
    {"type": "list",  "label": "…", "items": ["…", …]}
    {"type": "table", "label": "…", "columns": ["…", …], "rows": [["…", …], …]}
    {"type": "cards", "label": "…", "items": [group, …]}

and does three jobs on the way:

1. LABELS. Every field is labelled from Wathq's own spec (templates/wathq/*.yaml):
   the Arabic half of its bilingual description ("Commercial Registry Number -
   رقم السجل التجاري"), else a curated Arabic override, else the English half,
   else the key itself made readable.
2. PRIVACY. Values that identify a natural person — identity, iqama,
   passport and border numbers, phone numbers, e-mail addresses, dates of
   birth — are masked HERE, on the server, before the browser ever sees them.
   Two layers: each endpoint's explicit list of personal paths (from the
   catalog), and a name-based backstop that catches the same kinds of field
   anywhere. Company identifiers (CR and unified numbers) are not personal.
3. TIDYING. Code/name pairs collapse to the name ({"id": 3, "name": "الرياض"}
   becomes "الرياض"), an `xxxId` next to its `xxxName` is dropped, and nulls,
   empty values and Swagger placeholders ("string") disappear. Every value
   leaves as a string; booleans read نعم / لا.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

SPEC_DIR = Path(__file__).with_name("templates") / "wathq"

MAX_DEPTH = 10
MAX_ITEMS = 300
MAX_TEXT = 6000
MAX_COLUMNS = 12

_ARABIC = re.compile(r"[؀-ۿ]")


# ---------------------------------------------------------------- the specs

@lru_cache(maxsize=None)
def load_spec(name: str) -> dict:
    """A bundled Wathq spec, parsed once."""
    path = SPEC_DIR / f"{name}.yaml"
    with open(path, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    if not isinstance(spec, dict) or "paths" not in spec:
        raise ValueError(f"{path.name} is not a Swagger file")
    return spec


def _resolve(spec: dict, node, seen=()):
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen or not isinstance(ref, str) or not ref.startswith("#/"):
            return {}
        seen = seen + (ref,)
        target = spec
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node = target
    if isinstance(node, dict) and "allOf" in node:
        merged: dict = {"type": "object", "properties": {}}
        for part in node.get("allOf") or []:
            part = _resolve(spec, part, seen)
            merged["properties"].update(part.get("properties") or {})
            if part.get("description") and not merged.get("description"):
                merged["description"] = part["description"]
        return merged
    return node if isinstance(node, dict) else {}


def split_description(text) -> tuple:
    """(arabic, english) halves of a Wathq description such as
    'Commercial Registry Number - رقم السجل التجاري'."""
    if not isinstance(text, str):
        return "", ""
    text = " ".join(text.split())
    junk = " -–—:،.'\""
    parts = [p.strip(junk) for p in re.split(r"\s+[-–—]\s+|\n", text) if p.strip(junk)]
    arabic = " - ".join(p for p in parts if _ARABIC.search(p))
    english = " - ".join(p for p in parts if not _ARABIC.search(p))
    return arabic[:120], english[:120]


def response_schema(spec: dict, path: str, method: str = "get") -> dict:
    op = ((spec.get("paths") or {}).get(path) or {}).get(method) or {}
    responses = op.get("responses") or {}
    ok = responses.get("200") or responses.get(200) or {}
    ok = _resolve(spec, ok)
    return _resolve(spec, ok.get("schema") or {})


@lru_cache(maxsize=None)
def spec_labels(name: str, path: str) -> dict:
    """{dotted path: (arabic, english)} for every field of an endpoint's 200
    answer, walked through $ref/allOf, arrays written as `[]`."""
    spec = load_spec(name)
    labels: dict = {}

    def walk(node, prefix, depth, seen):
        node = _resolve(spec, node)
        if depth > MAX_DEPTH or not node:
            return
        if node.get("type") == "array" or "items" in node:
            walk(node.get("items") or {}, prefix + "[]", depth + 1, seen)
            return
        for key, child in (node.get("properties") or {}).items():
            ref = child.get("$ref") if isinstance(child, dict) else None
            full = f"{prefix}.{key}" if prefix else key
            resolved = _resolve(spec, child)
            ar, en = split_description(resolved.get("description") or
                                       (child.get("description") if isinstance(child, dict) else ""))
            labels[full] = (ar, en)
            if ref and ref in seen:
                continue
            walk(child, full, depth + 1, seen + ((ref,) if ref else ()))

    walk(response_schema(spec, path), "", 0, ())
    return labels


# ---------------------------------------------------------------- privacy

# Backstop for personal fields the catalog didn't list: keys that hold a
# natural person's identifier or contact detail by their very name.
_PERSONAL_KEY = re.compile(
    r"(?i)^(?:"
    r"id(?:entity)?(?:number|no|num)|national_?id(?:number)?|nin|"
    r"iqama(?:number|no)?|residen(?:t|cy)_?(?:id|number)|"
    r"passport(?:number|no)?|border(?:number|no)?|gcc_?id|"
    r"(?:mobile|phone|telephone|tel|fax)(?:number|no)?|"
    r"e?mail(?:address)?|"
    r"(?:birth_?date|birth_?day|date_?of_?birth|dob|birthdate(?:gregorian|hijri|greg)?|"
    r"(?:gregorian|hijri)?birth_?date)"
    r")$")
_IDENTITY_PARENT = re.compile(r"(?i)^(?:identity|identifier|ident|person|personal_?id)$")
_BIRTH_KEY = re.compile(r"(?i)birth|dob")
_CONTACT_KEY = re.compile(r"(?i)mail")


def _is_personal(path: str, key: str, parent: str, personal: frozenset) -> bool:
    if path in personal:
        return True
    if _PERSONAL_KEY.match(key):
        return True
    return key.lower() == "id" and bool(_IDENTITY_PARENT.match(parent or ""))


def mask(value: str, key: str = "") -> str:
    """Hide a personal value but keep enough to recognise it: the last four
    characters of an ID or phone, the year of a birth date, the first
    letter and the domain of an e-mail."""
    if not value:
        return value
    if _CONTACT_KEY.search(key) and "@" in value:
        local, _, domain = value.partition("@")
        return (local[:1] + "•••@" + domain)[:80]
    if _BIRTH_KEY.search(key):
        year = re.search(r"\d{4}", value)
        masked = re.sub(r"\d", "•", value)
        if year:
            masked = masked[:year.start()] + year.group(0) + masked[year.end():]
        return masked
    keep = 4 if len(value) > 6 else max(0, len(value) - 3)
    return "•" * (len(value) - keep) + value[len(value) - keep:]


# ---------------------------------------------------------------- tidying

_NAME_KEYS = ("name", "nameAr", "nameAR", "arabicName", "nameEn", "nameEN", "englishName",
              "description", "desc", "descAr", "descriptionAr")
_ID_EXACT = re.compile(r"(?i)^(?:id|code|key|typeid)$")
_ID_SUFFIX = re.compile(r"[a-z](?:Id|ID|Code)$")          # camelCase only: cityId, not "paid"


def _text(value) -> str:
    """A scalar as a display string: '' for null / placeholders / containers.
    Long text is cut at MAX_TEXT (redaction, where due, happens first)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "نعم" if value else "لا"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # Pair what pairs, replace what doesn't: a lone surrogate can't be
        # encoded as UTF-8 and would turn the whole response into a 500.
        text = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace").strip()
        if text.lower() == "string":
            return ""
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"
    return ""


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {} or (
        isinstance(value, str) and value.strip().lower() in ("", "string"))


def _collapse(value):
    """{"id": 3, "name": "الرياض"} → "الرياض": a code/name pair is its name."""
    if not isinstance(value, dict):
        return value
    live = {k: v for k, v in value.items() if not _empty(v)}
    names = [k for k in _NAME_KEYS if isinstance(live.get(k), str)]
    if not names:
        return value
    others = [k for k in live if k not in names]
    if all((_ID_EXACT.match(k) or _ID_SUFFIX.search(k)) and not isinstance(live[k], (dict, list))
           for k in others):
        if len(names) == 1:
            return live[names[0]]
        return " / ".join(dict.fromkeys(live[k] for k in names))
    return value


_NAMEISH = re.compile(r"^(.*?)(?:Name|Desc|Description|NameAr|NameEn)$", re.I)


def _drop_paired_ids(d: dict) -> dict:
    """A code next to the name it codes is noise: cityId beside cityName,
    partnersNationalityId beside PartnersNationalityName, classReferenceID
    beside className, a bare id beside a bare name. Keep the name."""
    name_bases = set()
    for key, value in d.items():
        m = _NAMEISH.match(key)
        if m and isinstance(value, str):
            name_bases.add(m.group(1).lower())
    has_plain_name = any(k in d and isinstance(d[k], str) for k in _NAME_KEYS)
    out = {}
    for key, value in d.items():
        if not isinstance(value, (dict, list)):
            # A short id beside a name is a lookup code; a long one (a
            # person's ID number next to their name) is data and stays.
            if key.lower() in ("id", "code") and has_plain_name and len(_text(value)) <= 5:
                continue
            m = re.match(r"^(.*?[a-z])(?:Id|ID|Code)$", key)
            if m:
                base = m.group(1).lower()
                if any(b and (base == b or base.startswith(b)) for b in name_bases):
                    continue
        out[key] = value
    return out


# Arabic for container keys Wathq's specs leave undescribed (used only when
# neither the spec nor the catalog gives an Arabic label).
COMMON_AR = {
    "identity": "الهوية", "identifier": "المعرّف", "entityType": "نوع الكيان",
    "characters": "صفة الشركة", "confirmationDate": "تاريخ التأكيد السنوي",
    "reactivationDate": "تاريخ إعادة التفعيل", "suspensionDate": "تاريخ الإيقاف",
    "deletionDate": "تاريخ الشطب", "issueDate": "تاريخ الإصدار", "expiryDate": "تاريخ الانتهاء",
    "gregorian": "ميلادي", "hijri": "هجري", "eStore": "المتجر الإلكتروني",
    "storeActivities": "أنشطة المتجر", "nationality": "الجنسية", "address": "العنوان",
    "status": "الحالة", "type": "النوع", "name": "الاسم", "city": "المدينة",
    "district": "الحي", "region": "المنطقة", "street": "الشارع", "zipCode": "الرمز البريدي",
    "postCode": "الرمز البريدي", "buildingNumber": "رقم المبنى", "additionalNumber": "الرقم الإضافي",
    "latitude": "خط العرض", "longitude": "خط الطول", "partners": "الشركاء", "owners": "الملاك",
    "managers": "المديرون", "branches": "الفروع", "activities": "الأنشطة", "capital": "رأس المال",
    "delegate": "المفوَّض", "principals": "الموكلون", "agents": "الوكلاء", "title": "العنوان",
    # keys Wathq's examples use but its schemas don't describe
    "id": "الرقم", "typeName": "النوع", "partnerShare": "حصة الشريك",
    "partnership": "طبيعة الشراكة", "positions": "المناصب", "permissions": "الصلاحيات",
    "isLicensed": "مدير مرخّص", "canDelegate": "جواز الإنابة", "canIssuePOA": "جواز التوكيل",
    "cashContributionCount": "عدد الحصص النقدية", "inKindContributionCount": "عدد الحصص العينية",
    "totalContributionCount": "إجمالي عدد الحصص", "exerciseMethodDescription": "طريقة ممارسة الصلاحية",
    "specialConditionText": "شرط خاص بالصلاحية",
    # reference lists, shown with their codes
    "code": "الرمز", "nameAr": "الاسم بالعربية", "nameEn": "الاسم بالإنجليزية",
    "nameEr": "الاسم بالإنجليزية", "description": "الوصف", "descAr": "الوصف بالعربية",
    "descEn": "الوصف بالإنجليزية", "nicCode": "رمز الجنسية (NIC)", "birthday": "تاريخ الميلاد",
    "birthDate": "تاريخ الميلاد",
}
COMMON_AR_CI = {k.lower(): v for k, v in COMMON_AR.items()}


def _humanize(key: str) -> str:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_", " ", key).strip()
    return words[:1].upper() + words[1:] if words else key


class _Labeler:
    def __init__(self, labels: dict, overrides: dict):
        self.labels, self.overrides = labels, overrides

    def __call__(self, path: str, key: str) -> str:
        if path in self.overrides:
            return self.overrides[path]
        ar, en = self.labels.get(path, ("", ""))
        return (ar or self.overrides.get(key, "") or COMMON_AR.get(key, "")
                or COMMON_AR_CI.get(key.lower(), "") or en or _humanize(key))


# ---------------------------------------------------------------- the view

_DIGIT = "0-9٠-٩۰-۹"
_DIGIT_RUN = re.compile(f"[{_DIGIT}](?: ?[{_DIGIT}]){{8,}}")
_PROSE = re.compile(r"\s\S+\s")          # at least two spaces: words, not a code


def redact_digits(text: str) -> str:
    """Free text (an agency's wording, a deed's text, a clause) can quote ID
    and phone numbers: every run of nine or more digits — Western, Arabic-Indic
    or Persian, optionally grouped by single spaces ('055 123 4567') — keeps
    only its last four digits. IDs are ten digits and phones nine or more;
    dates (1445/03/12), amounts (500000, 1250.75) and short numbers stay."""
    def hide(m):
        run = m.group(0)
        digits = sum(1 for ch in run if ch != " ")
        keep_from = digits - 4
        out, seen = [], 0
        for ch in run:
            if ch == " ":
                out.append(ch)
                continue
            out.append(ch if seen >= keep_from else "•")
            seen += 1
        return "".join(out)
    return _DIGIT_RUN.sub(hide, text)


def _norm(path: str) -> str:
    """A path as matched for privacy: no list markers, no case. Wathq sends a
    list where its schema says object (and the reverse) and re-cases keys
    between schema and example (SefaId / SefaID), so `principals[].id` must
    also cover `principals.id` and `Principals[].Id`."""
    return path.replace("[]", "").lower().strip(".")


def build_view(data, *, labels: dict | None = None, personal=(), overrides: dict | None = None,
               title: str = "", redact=(), tidy: bool = True) -> list:
    """The display nodes for one Wathq answer (see the module docstring).

    labels    {path: (arabic, english)} from spec_labels()
    personal  dotted paths ([] for list items) whose values must be masked
    overrides {path or bare key: arabic label} for fields the spec leaves in English
    title     the label for a top-level list or a bare value
    redact    free-text paths whose long digit runs are masked
    tidy      collapse code/name pairs and drop codes beside names; off for
              reference lists, whose codes are what was paid for
    """
    label = _Labeler(labels or {}, overrides or {})
    personal = frozenset(_norm(p) for p in personal or ())
    redact = frozenset(_norm(p) for p in redact or ())

    def scalar(value, path, key, parent):
        norm = _norm(path)
        if norm in redact and isinstance(value, str):
            # Redact the whole text before it is shortened for display.
            return _text(redact_digits(value))
        text = _text(value)
        if text and not isinstance(value, bool):
            if norm in redact:
                text = redact_digits(text)
            elif norm in personal or _is_personal(path, key, parent, frozenset()):
                text = mask(text, key)
            elif isinstance(value, str) and _PROSE.search(text):
                # Backstop for prose no catalog entry names: an ID or phone
                # number quoted in a sentence is masked like listed free text.
                text = redact_digits(text)
        return text

    def collapse(value):
        return _collapse(value) if tidy else value

    def node_for(value, path, key, parent, depth):
        """One child node, or None when there is nothing to show."""
        value = collapse(value)
        name = label(path, key) if key else (title or "النتيجة")
        if isinstance(value, dict):
            children = members(value, path, depth + 1)
            return {"type": "group", "label": name, "children": children} if children else None
        if isinstance(value, list):
            return list_node(value, path + "[]", key, name, depth + 1)
        text = scalar(value, path, key, parent)
        return {"type": "field", "label": name, "value": text, "_k": key} if text else None

    def members(d, prefix, depth):
        if depth > MAX_DEPTH:
            return [{"type": "field", "label": "…", "value": "بيانات أعمق لم تُعرض"}]
        out = []
        parent = prefix.rsplit(".", 1)[-1].rstrip("[]") if prefix else ""
        for key, value in (_drop_paired_ids(d) if tidy else d).items():
            if _empty(value):
                continue
            path = f"{prefix}.{key}" if prefix else key
            node = node_for(value, path, str(key), parent, depth)
            if node:
                out.append(node)
        return out

    def list_node(items, path, key, name, depth):
        if depth > MAX_DEPTH:
            return {"type": "field", "label": name, "value": "بيانات أعمق لم تُعرض"}
        live = [v for v in items if not _empty(v)]
        total = len(live)
        items = [collapse(v) for v in live[:MAX_ITEMS]]
        if not items:
            return None
        cut = {"truncated": True, "total": total} if total > MAX_ITEMS else {}
        if all(not isinstance(v, (dict, list)) for v in items):
            values = [scalar(v, path, key, key) for v in items]
            values = [v for v in values if v]
            return {"type": "list", "label": name, "items": values, **cut} if values else None
        groups = []
        for i, item in enumerate(items, 1):
            if isinstance(item, dict):
                children = members(item, path, depth)
            elif isinstance(item, list):
                inner = list_node(item, path + "[]", key, name, depth + 1)
                children = [inner] if inner else []
            else:
                text = scalar(item, path, key, key)
                children = [{"type": "field", "label": name, "value": text}] if text else []
            if children:
                groups.append({"type": "group", "label": f"{name} ({i})", "children": children})
        if not groups:
            return None
        flat = all(c["type"] == "field" for g in groups for c in g["children"])
        # Columns by key, labelled by label: two keys that share a label (nameEn
        # and nameEr both "الاسم بالإنجليزية") get the key appended, not merged.
        keys = list(dict.fromkeys((c.get("_k") or c["label"]) for g in groups for c in g["children"]))
        by_key = {}
        for g in groups:
            for c in g["children"]:
                by_key.setdefault(c.get("_k") or c["label"], c["label"])
        label_count: dict = {}
        for k in keys:
            label_count[by_key[k]] = label_count.get(by_key[k], 0) + 1
        columns = [by_key[k] if label_count[by_key[k]] == 1 else f"{by_key[k]} ({safe_key(k)})"
                   for k in keys]
        if flat and len(keys) <= MAX_COLUMNS and len(groups) > 1:
            rows = []
            for g in groups:
                cells = {(c.get("_k") or c["label"]): c["value"] for c in g["children"]}
                rows.append([cells.get(k, "") for k in keys])
            return {"type": "table", "label": name, "columns": columns, "rows": rows, **cut}
        return {"type": "cards", "label": name, "items": groups, **cut}

    def strip(nodes):
        for n in nodes:
            n.pop("_k", None)
            strip(n.get("children") or [])
            strip([i for i in n.get("items") or [] if isinstance(i, dict)])
        return nodes

    if isinstance(data, dict):
        return strip(members(data, "", 0))
    if isinstance(data, list):
        node = list_node(data, "[]", "", title or "النتائج", 0)
        return strip([node]) if node else []
    node = node_for(data, "", "", "", 0)
    return strip([node]) if node else []


def safe_key(key) -> str:
    """A field name as-is when it looks like one; otherwise its shape only."""
    text = str(key)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", text) and sum(c.isdigit() for c in text) < 6:
        return text
    return f"{len(text)}-char key"
