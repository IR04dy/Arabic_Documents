"""Pure, network-free logic for the "Verify data" (التحقق من البيانات) service.

Two jobs, both unit-testable without Wathq:

1. `parse_number` turns what a person typed (or what OCR read) into a
   10-digit number and says which kind it is: the unified national number
   (الرقم الوطني الموحد, 70xxxxxxxx) that the Company Contracts API takes, or
   an old commercial-registration number (رقم السجل التجاري) that has to be
   converted first.

2. `shape_contract` turns Wathq's /company-contract/info answer into the
   small, stable shape the UI renders. Wathq's own spec contradicts itself
   (an ID is an integer in one place and a string in the next; a list is
   sometimes a single object), so every field is read defensively and
   every value leaves here as a string. It is also a whitelist: anything
   not named here never reaches the browser, and identity numbers of people
   (partners, managers, guardians) are masked to their last four digits.
"""

from __future__ import annotations

import re
import unicodedata

from wathq_client import LEGACY_CR, UNIFIED
from wathq_view import redact_digits

_FOLD = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SEPARATORS = re.compile(r"[\s\-_.‎‏‪-‮⁦-⁩؜]+")

MAX_ARTICLES = 300
MAX_TEXT = 6000            # one clause; real ones are a few hundred characters
MAX_LIST = 200             # partners, managers, activities…

KIND_UNIFIED = "unified"
KIND_CR = "cr"


def parse_number(raw) -> tuple[str, str]:
    """(digits, kind) for a typed or extracted number, or ValueError with an
    Arabic message. Arabic-Indic and Persian digits are folded; spaces,
    dashes, dots and bidi marks between the digits are dropped."""
    if not isinstance(raw, str):
        raise ValueError("أدخل رقمًا.")
    text = unicodedata.normalize("NFKC", raw).translate(_FOLD)
    digits = _SEPARATORS.sub("", text)
    if not digits:
        raise ValueError("أدخل الرقم الوطني الموحد أو رقم السجل التجاري.")
    if not digits.isascii() or not digits.isdigit():
        raise ValueError("يقبل الحقل الأرقام فقط.")
    if len(digits) != 10:
        raise ValueError("الرقم يتكوّن من ١٠ أرقام.")
    if UNIFIED.fullmatch(digits):
        return digits, KIND_UNIFIED
    if LEGACY_CR.fullmatch(digits):
        return digits, KIND_CR
    raise ValueError("هذا ليس رقمًا وطنيًا موحدًا (يبدأ بـ ٧٠) ولا رقم سجل تجاري.")


# ---------------------------------------------------------------- readers

def _s(value) -> str:
    """Any scalar as a trimmed string; containers, booleans and null as ''."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        # A lone surrogate (from a \ud83d escape, or CESU-8 upstream) can't be
        # encoded as UTF-8 and would turn the response into a 500. Pair what
        # pairs, replace what doesn't.
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace").strip()
    if isinstance(value, int):
        return str(value).strip()
    return ""


def _free(value, limit: int = MAX_TEXT) -> str:
    """Free text (a clause, a note) with any quoted ID or phone number masked
    to its last four digits — before it is shortened."""
    raw = value if isinstance(value, str) else _s(value)
    return _text(redact_digits(raw), limit) if raw else ""


def _text(value, limit: int = MAX_TEXT) -> str:
    text = _s(value)
    if text.lower() == "string":          # Swagger placeholder seen in examples
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value, limit: int = MAX_LIST) -> list:
    if isinstance(value, dict):           # one object where a list was promised
        value = [value]
    if not isinstance(value, list):
        return []
    return [v for v in value[:limit] if isinstance(v, dict)]


def _flag(value):
    return value if isinstance(value, bool) else None


def _names(value) -> list:
    return [n for n in (_text(_obj(v).get("name"), 300) for v in _list(value)) if n]


def mask_id(value) -> str:
    """A person's identity number with all but the last four digits hidden."""
    digits = _s(value)
    if not digits:
        return ""
    if len(digits) <= 4:
        return "•" * len(digits)
    return "•" * (len(digits) - 4) + digits[-4:]


def _identity(value) -> dict:
    ident = _obj(value)
    return {"id_masked": mask_id(ident.get("id")), "id_type": _text(ident.get("typeName"), 100)}


def _number(value) -> str:
    """A numeric amount as digits; 0 is kept (a zero cash share is information)."""
    text = _s(value)
    return text if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", text) else ""


# ---------------------------------------------------------------- shaping

def _capital(raw: dict) -> dict:
    contrib = _obj(raw.get("contributionCapital"))
    stock = _obj(raw.get("stockCapital"))
    out = {"currency": _text(raw.get("currencyName"), 60)}
    if contrib:
        out["contribution"] = {
            "type": _text(contrib.get("typeName"), 60),
            "cash": _number(contrib.get("cashCapital")),
            "in_kind": _number(contrib.get("inKindCapital")),
            "share_value": _number(contrib.get("contributionValue")),
            "cash_shares": _number(contrib.get("totalCashContribution")),
            "in_kind_shares": _number(contrib.get("totalInKindContribution")),
        }
    if stock:
        out["stock"] = {
            "type": _text(stock.get("typeName"), 60),
            "capital": _number(stock.get("capital")),
            "announced": _number(stock.get("announcedCapital")),
            "paid": _number(stock.get("paidCapital")),
            "cash": _number(stock.get("cashCapital")),
            "in_kind": _number(stock.get("inKindCapital")),
            "stocks": [{
                "type": _text(s.get("typeName"), 60),
                "count": _number(s.get("count")),
                "value": _number(s.get("value")),
                "class": _text(s.get("className"), 100),
            } for s in _list(stock.get("stocks"))],
        }
    return out


def _party(raw: dict) -> dict:
    share = _obj(raw.get("partnerShare"))
    split = _obj(raw.get("partnerProfitLossDistribution"))
    party = {
        "name": _text(raw.get("name"), 300),
        "type": _text(raw.get("typeName"), 100),
        **_identity(raw.get("identity")),
        "nationality": _text(_obj(raw.get("nationality")).get("name"), 100),
        "roles": _names(raw.get("partnership")),
        "cash_shares": _number(share.get("cashContributionCount")),
        "in_kind_shares": _number(share.get("inKindContributionCount")),
        "total_shares": _number(share.get("totalContributionCount")),
        "profit_pct": _number(split.get("profitDistribution")),
        "loss_pct": _number(split.get("lossDistribution")),
        # A partner that is itself a company: its CR is public, not personal.
        "cr_number": _s(raw.get("crNumber")),
        "license_no": _s(raw.get("licenseNo")),
    }
    guardian = _obj(raw.get("guardian"))
    if _text(guardian.get("name")):
        party["guardian"] = {
            "name": _text(guardian.get("name"), 300),
            **_identity(guardian.get("identity")),
            "nationality": _text(_obj(guardian.get("nationality")).get("name"), 100),
            "is_father": _flag(guardian.get("isFatherGuardian")),
        }
    return party


def _manager(raw: dict) -> dict:
    return {
        "name": _text(raw.get("name"), 300),
        "type": _text(raw.get("typeName"), 100),
        **_identity(raw.get("identity")),
        "nationality": _text(_obj(raw.get("nationality")).get("name"), 100),
        "positions": _names(raw.get("positions")),
        "licensed": _flag(raw.get("isLicensed")),
    }


def _fiscal_year(raw: dict) -> dict:
    if not raw:
        return {}
    day, month, year = _number(raw.get("endDay")), _number(raw.get("endMonth")), _number(raw.get("endYear"))
    return {
        "first": _flag(raw.get("isFirst")),
        "calendar": _text(raw.get("calendarTypeName"), 60),
        "end": "/".join(p for p in (year, month.zfill(2) if month else "", day.zfill(2) if day else "") if p),
    }


def shape_contract(raw) -> dict:
    """Wathq's /info answer as the UI's contract card. Raises ValueError if
    the answer has no entity at all (nothing to show)."""
    raw = _obj(raw)
    entity = _obj(raw.get("entity"))
    if not entity:
        raise ValueError("no entity in the contract answer")
    etype = _obj(entity.get("entityType"))
    management = _obj(entity.get("management"))
    articles = []
    for item in _list(raw.get("articles"), MAX_ARTICLES):
        text = _free(item.get("text"))
        if text:
            articles.append({"part": _text(item.get("partName"), 200), "title": "", "text": text})
    for item in _list(raw.get("additionalArticles"), MAX_ARTICLES):
        text = _free(item.get("text"))
        if text:
            articles.append({"part": _text(item.get("partName"), 200),
                             "title": _free(item.get("title"), 300), "text": text})
    set_aside = _obj(raw.get("setAsideDetails"))
    allocation = _obj(set_aside.get("profitAllocation"))
    return {
        "contract": {
            "copy_number": _s(raw.get("contractCopyNumber")),
            "date": _text(raw.get("contractDate"), 40),
        },
        "entity": {
            "national_number": _s(entity.get("crNationalNumber")),
            "cr_number": _s(entity.get("crNumber")),
            "name": _text(entity.get("name"), 300),
            "name_language": _text(entity.get("nameLangDesc"), 60),
            "entity_type": _text(etype.get("name"), 100),
            "legal_form": _text(etype.get("formName"), 100),
            "characters": _names(etype.get("characters")),
            "duration": _number(entity.get("companyDuration")),
            "headquarters": _text(entity.get("headquarterCityName"), 100),
            "license_based": _flag(entity.get("isLicenseBased")),
            "license_issuer": _text(entity.get("licenseIssuerName"), 200),
        },
        "capital": _capital(_obj(entity.get("capital"))),
        "fiscal_year": _fiscal_year(_obj(entity.get("fiscalYear"))),
        "parties": [_party(p) for p in _list(entity.get("parties"))],
        "management": {
            "structure": _text(management.get("structureName"), 100),
            "dismissal": _free(management.get("dismissalMethod"), 1000),
            "managers": [_manager(m) for m in _list(management.get("managers"))],
        },
        "activities": [a for a in (
            {"code": _s(_obj(x).get("id")), "name": _text(_obj(x).get("name"), 400)}
            for x in _list(entity.get("activities"))) if a["name"]],
        "notification_channels": _names(raw.get("notificationChannel")),
        "decisions": [d for d in (
            {"name": _text(x.get("name"), 300),
             "approve_pct": _number(x.get("approvePercentage")),
             "note": _free(x.get("approveAdditionalText"), 1000)}
            for x in _list(raw.get("partnerDecision"))) if d["name"]],
        "decisions_note": _free(raw.get("additionalDecisionText"), 2000),
        "profit_set_aside": ({"pct": _number(allocation.get("percentage")),
                              "purpose": _free(allocation.get("purpose"), 500)}
                             if set_aside.get("isSetAsideEnabled") is True else {}),
        "articles": articles,
    }
