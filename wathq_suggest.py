"""Which Wathq services can verify this document? The local model decides.

After structuring, the page sends the structured result here. It is flattened
to ``label: value`` lines and the model is asked one question: given these
lines and the list of services (name + the data each returns), which service
can verify the document? One name, or None. The chosen service is removed and
the question repeats until None. The order of the answers is the ranking: the
first service is pre-ticked in the tab, the rest follow unticked, and the user
approves before anything is sent to Wathq.

The key fields of each service are pre-filled from the same lines by matching
the field's label, so the user corrects a misread value instead of typing it.
No Wathq call is made here.
"""
from __future__ import annotations

import re
import unicodedata

PROMPT_PAIRS = "البيانات المستخرجة من المستند:"
PROMPT_SERVICES = "خدمات التحقق المتاحة (اسم الخدمة — ما تعيده من بيانات):"
PROMPT_QUESTION = ("أي خدمة يمكن أن تتحقق من بيانات هذا المستند؟ "
                   "أجب باسم خدمة واحدة، أو None إن لم تنطبق أي خدمة.")
NONE = "None"
MAX_PAIRS = 120          # lines of the document shown to the model
MAX_VALUE = 300          # characters of one value

# The menu: service name, the data the service returns, and the catalog query
# that runs it. ``fill`` maps each query input to the labels a document uses
# for that value (regular expressions over the extracted label).
SERVICES = [
    {"id": "commercial_registration", "label": "السجل التجاري", "endpoint": "cr.info",
     "returns": "اسم المنشأة، رقم السجل، الرقم الوطني الموحد، حالة السجل، تاريخ الإصدار، تاريخ انتهاء السجل، "
                "نوع الكيان، رأس المال، المدينة، الأنشطة، الشركاء، المديرون، الفروع",
     "fill": {"id": "company"}},
    {"id": "company_contract", "label": "عقد تأسيس الشركة", "endpoint": "contracts.info",
     "returns": "اسم الشركة، تاريخ العقد، مدة الشركة، رأس المال، الشركاء وحصصهم، المديرون وصلاحياتهم، بنود العقد",
     "fill": {"crNationalNumber": "company", "copyNumber": "copy"}},
    {"id": "national_address", "label": "العنوان الوطني للمنشأة", "endpoint": "national_address.info",
     "returns": "اسم المنشأة، رقم المبنى، الشارع، الحي، المدينة، الرمز البريدي، الرقم الإضافي",
     "fill": {"crNumber": "company"}},
    {"id": "power_of_attorney", "label": "الوكالة", "endpoint": "attorney.info",
     "returns": "رقم الوكالة، حالة الوكالة، تاريخ الإصدار، تاريخ الانتهاء، اسم الموكل ورقم هويته، "
                "اسم الوكيل ورقم هويته، بنود الوكالة",
     "fill": {"code": "attorney", "principalId": "principal_id", "agentId": "agent_id"}},
    {"id": "real_estate_deed", "label": "الصك العقاري", "endpoint": "real_estate.deed",
     "returns": "رقم الصك، تاريخ الصك، حالة الصك، اسم المالك ورقم هويته، حصة الملكية، المساحة، المدينة، "
                "الحي، رقم القطعة، رقم المخطط",
     "fill": {"deedNumber": "deed", "idNumber": "owner_id", "idType": "owner_id_type"}},
    {"id": "employee", "label": "الموظف في التأمينات الاجتماعية", "endpoint": "employee.info",
     "returns": "اسم الموظف، رقم هويته، الجنسية، اسم المنشأة، حالة الاشتراك، الراتب الأساسي، البدلات",
     "fill": {"id": "employee_id"}},
    {"id": "foreign_investor", "label": "ترخيص المستثمر الأجنبي", "endpoint": "investor.fullinfo",
     "returns": "اسم المنشأة، الرقم الموحد، دولة المنشأ، حالة الترخيص، الشركاء ونسبهم، المفوض",
     "fill": {"id": "unified"}},
    {"id": "drug", "label": "الدواء المسجل في هيئة الغذاء والدواء", "endpoint": "drug.status",
     "returns": "رقم التسجيل، الاسم التجاري، حالة التسجيل، السعر، تصنيف الصرف",
     "fill": {"id": "drug"}},
]
_BY_ID = {s["id"]: s for s in SERVICES}

# How documents label each key, and what shape its value has once digits are
# folded. A label regex is tried in order; the first line whose label matches
# and whose value has the right shape wins.
_HOW = {
    "company": ([r"الرقم (الوطني )?الموحد", r"السجل التجاري|سجل تجاري|س\.?\s?ت\b|C\.?R\.?"], r"^[1-7][0-9]{9}$"),
    "unified": ([r"الرقم (الوطني )?الموحد", r"السجل التجاري"], r"^70[0-9]{8,10}$"),
    "copy": ([r"رقم (نسخة|النسخة)"], r"^[0-9]{1,5}$"),
    "attorney": ([r"رقم الوكالة|الوكالة رقم"], r"^[0-9]{1,20}$"),
    "principal_id": ([r"هوية.*الموكل|الموكل.*هوية"], r"^[12][0-9]{9}$"),
    "agent_id": ([r"هوية.*الوكيل|الوكيل.*هوية"], r"^[12][0-9]{9}$"),
    "deed": ([r"رقم الصك|الصك رقم|رقم الوثيقة"], r"^[0-9]{1,20}$"),
    "owner_id": ([r"هوية.*(المالك|البائع|الواهب|الراهن|الموصي)|(المالك|البائع|الواهب|الراهن|الموصي).*هوية",
                  r"رقم الهوية|الهوية الوطنية|رقم الإقامة"], r"^[12][0-9]{9}$"),
    "employee_id": ([r"هوية.*(الموظف|العامل|الطرف الثاني)|(الموظف|العامل|الطرف الثاني).*هوية",
                     r"رقم الهوية|الهوية الوطنية|رقم الإقامة"], r"^[12][0-9]{9}$"),
    "drug": ([r"رقم (التسجيل|تسجيل)"], r"^[0-9]{1,20}$"),
}
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def ascii_digits(value) -> str:
    """Arabic-Indic and Persian digits as ASCII; nothing else changed."""
    return unicodedata.normalize("NFKC", str(value or "")).translate(_DIGITS)


def fold(value: str) -> str:
    """Digits as ASCII, separators and bidi marks removed."""
    return re.sub(r"[\s\-_.,/​-‏‪-‮⁦-⁩؜﻿]+", "", ascii_digits(value))


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def flatten(struct) -> list[tuple[str, str]]:
    """The structured result as (label, value) lines, in document order."""
    pairs = []
    if not isinstance(struct, dict):
        return pairs
    for sec in struct.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        for f in sec.get("fields") or []:
            if isinstance(f, dict) and _clean(f.get("value")):
                pairs.append((_clean(f.get("label")), _clean(f.get("value"))[:MAX_VALUE]))
        role = _clean(sec.get("record_label") or sec.get("title"))
        for i, row in enumerate(sec.get("records") or [], 1):
            for f in row if isinstance(row, list) else []:
                if isinstance(f, dict) and _clean(f.get("value")):
                    pairs.append((f"{role} {i} - {_clean(f.get('label'))}", _clean(f.get("value"))[:MAX_VALUE]))
        if len(pairs) >= MAX_PAIRS:
            break
    return pairs[:MAX_PAIRS]


def prompt(pairs, remaining) -> str:
    lines = "\n".join(f"{k}: {v}" for k, v in pairs)
    services = "\n".join(f"- {s['id']}: {s['label']} — يعيد: {s['returns']}" for s in remaining)
    return f"{PROMPT_PAIRS}\n{lines}\n\n{PROMPT_SERVICES}\n{services}\n\n{PROMPT_QUESTION}"


def rank(pairs, ask, services=None) -> list[str]:
    """Ask until the model answers None; the order of the answers is the ranking.

    ``ask(prompt, options)`` returns one of ``options`` (the remaining service
    ids plus "None"). Anything else ends the ranking."""
    remaining = list(services if services is not None else SERVICES)
    chosen = []
    while remaining:
        options = [s["id"] for s in remaining] + [NONE]
        answer = ask(prompt(pairs, remaining), options)
        if answer not in options or answer == NONE:
            break
        chosen.append(answer)
        remaining = [s for s in remaining if s["id"] != answer]
    return chosen


def _find(pairs, how: str) -> str:
    patterns, shape = _HOW[how]
    for pattern in patterns:
        for label, value in pairs:
            if re.search(pattern, label, re.IGNORECASE) and re.match(shape, fold(value)):
                return fold(value)
    return ""


def prefill(pairs, service: dict) -> dict:
    """Values for the service's query inputs, read from the document's lines."""
    values = {}
    for name, how in service["fill"].items():
        if how == "owner_id_type":
            continue
        v = _find(pairs, how)
        if v:
            values[name] = v
    if "owner_id_type" in service["fill"].values():
        owner = values.get("idNumber", "")
        if owner.startswith("1"):
            values["idType"] = "National_ID"
        elif owner.startswith("2"):
            values["idType"] = "Resident_ID"
    return values


def suggest(struct, ask) -> dict:
    """The ranked services with their pre-filled inputs, for the tab to show."""
    pairs = flatten(struct)
    ranked = rank(pairs, ask) if pairs else []
    services = []
    for sid in ranked:
        s = _BY_ID[sid]
        services.append({"id": sid, "label": s["label"], "endpoint": s["endpoint"],
                         "inputs": prefill(pairs, s)})
    return {"pairs": len(pairs), "services": services}
