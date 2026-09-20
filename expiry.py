"""Does this document expire, and has it?

The registry already declares ONE expiry field (wakalah_shariyyah's
تاريخ انتهاء الوكالة). That is not enough to answer "is this instrument still
valid": an expiry date can sit in a header the template does not declare, in a
clause the structurer's prompt is told to skip ("Skip the document's
clauses/articles and any long narrative text entirely"), or past the character
cap the structurer applies to fit the context window. So expiry is detected on
its own, over the WHOLE text, in three tiers — cheapest and most certain first:

  1. the structured fields the caller already extracted, when one of them IS an
     expiry field (`wakala_expiry_date_hijri`). Already cited, already checked
     against the text: nothing to redo;
  2. a deterministic scan of the full OCR text for an expiry LABEL followed by a
     date. Regex over the whole document, so neither the prompt's narrative
     exclusion nor the input cap can hide a date from it;
  3. only if both come back empty, a focused model pass over the full document
     in overlapping chunks, asking for one thing. Its answer is then required to
     appear VERBATIM in the text (the caller's `verify`); one that does not is
     reported as unverified, never as fact.

The date is then normalised to a Gregorian calendar date and compared with
today. Hijri → Gregorian uses `hijridate` (or the older `hijri_converter`) when
the server has one installed: that is the real Umm al-Qura calendar, which is
what a Saudi instrument's dates mean. INSTALL IT — `pip install hijridate`. The
built-in tabular fallback exists so a machine without it degrades instead of
failing, but it is an arithmetic approximation: measured against Umm al-Qura
over 1400-1490 AH it runs 0 to +2 days late (93.7% within a day). That is
harmless for a 90-day bucket and wrong for "which day exactly", so `converter`
in the result names whichever one ran.

WHAT THIS MODULE DOES NOT DECIDE. A power of attorney that is ملغاة (revoked)
or موقوفة (suspended) is not valid whatever its expiry date says, and an expiry
date is silent about it. Those states are reported SEPARATELY as
`state_override`, and `status` never merges them: a revoked wakalah with a 1450
expiry date comes back status="valid", state_override="cancelled", and it is the
caller (UI, chat) that must show the override first.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, timedelta

# --- policy ------------------------------------------------------------------

# "Expiring soon" window, in days, inclusive. Today counts as expiring, not
# expired: an instrument is valid through the whole of its last day.
SOON_DAYS = int(os.environ.get("EXPIRY_SOON_DAYS", "90"))

# Tier 3. Off => tiers 1-2 only, and a document whose date is buried in a clause
# comes back "unknown" instead of costing a model call per chunk.
USE_MODEL = os.environ.get("EXPIRY_MODEL_PASS", "1").lower() not in ("0", "false", "no")
MAX_CHUNKS = int(os.environ.get("EXPIRY_MAX_CHUNKS", "4"))
_CHUNK_OVERLAP = 400

# Derive an expiry from "مدة الوكالة سنة واحدة" + the issue date. OFF by
# default: the result is a computed date the document never prints, so it can
# neither be cited nor verified, and a wrong "valid" is the one answer that
# costs something. Turn on only where the corpus is known to need it.
FROM_DURATION = os.environ.get("EXPIRY_FROM_DURATION", "0").lower() in ("1", "true", "yes")

STATUS_AR = {
    "valid": "سارية بحسب تاريخ الانتهاء",
    "expiring_soon": "تقارب الانتهاء",
    "expired": "منتهية",
    "unknown": "غير محدّد — تحتاج مراجعة",
}
OVERRIDE_AR = {
    "cancelled": "ملغاة",
    "suspended": "موقوفة",
    "declared_expired": "منتهية بحسب حالة الوثيقة",
}


# =============================================================================
# Normalisation — the registry's folds, reimplemented locally
# =============================================================================
# expiry.py must not import registry.py: it runs before/beside a template and on
# documents that have none. These are the same folds ksa_deeds.yaml declares,
# EXCEPT that newlines are kept (line proximity is evidence) and ى is NOT folded
# to ي — for the same reason the registry refuses to (الموصى vs الموصي).

_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_BIDI = re.compile("[​-‏؜⁦-⁩]")
_SPACES = re.compile("[^\\S\n]+")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)
_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                          "ة": "ه", "ؤ": "و", "ئ": "ي"})


def norm(text: str) -> str:
    """Fold text the way the registry folds it, keeping line structure."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _BIDI.sub("", text)
    text = _TASHKEEL.sub("", text)
    text = text.translate(_DIGITS).translate(_LETTERS)
    return _SPACES.sub(" ", text)


# =============================================================================
# Hijri <-> Gregorian
# =============================================================================

_HIJRI_EPOCH_JDN = 1948440          # 1 Muharram 1 AH = JDN 1948440
_JDN_TO_ORDINAL = 1721425           # proleptic Gregorian: JDN 1721426 = 0001-01-01


def _hijri_to_jdn(y: int, m: int, d: int) -> int:
    """Tabular ('Kuwaiti algorithm') Hijri day number."""
    return ((11 * y + 3) // 30) + 354 * y + 30 * m - ((m - 1) // 2) + d + _HIJRI_EPOCH_JDN - 385


def _jdn_to_hijri(jdn: int) -> tuple:
    l = jdn - _HIJRI_EPOCH_JDN + 10632
    n = (l - 1) // 10631
    l = l - 10631 * n + 354
    j = (((10985 - l) // 5316) * ((50 * l) // 17719)
         + (l // 5670) * ((43 * l) // 15238))
    l = (l - ((30 - j) // 15) * ((17719 * j) // 50)
         - (j // 16) * ((15238 * j) // 43) + 29)
    m = (24 * l) // 709
    d = l - (709 * m) // 24
    return 30 * n + j - 30, m, d


def _lib():
    """`hijridate` / `hijri_converter` if the server has one, else None."""
    for mod in ("hijridate", "hijri_converter"):
        try:
            return __import__(mod, fromlist=["Hijri", "Gregorian"])
        except Exception:
            continue
    return None


def hijri_to_gregorian(y: int, m: int, d: int) -> tuple:
    """(date, converter name). Falls back to the tabular conversion."""
    lib = _lib()
    if lib is not None:
        try:
            g = lib.Hijri(y, m, d).to_gregorian()
            return date(g.year, g.month, g.day), lib.__name__
        except Exception:
            pass          # out of the library's supported range (1343-1500 AH)
    return date.fromordinal(_hijri_to_jdn(y, m, d) - _JDN_TO_ORDINAL), "tabular"


def gregorian_to_hijri(g: date) -> tuple:
    lib = _lib()
    if lib is not None:
        try:
            h = lib.Gregorian(g.year, g.month, g.day).to_hijri()
            return (h.year, h.month, h.day), lib.__name__
        except Exception:
            pass
    return _jdn_to_hijri(g.toordinal() + _JDN_TO_ORDINAL), "tabular"


# =============================================================================
# Dates in text
# =============================================================================

_HIJRI_MONTHS = {
    1: ["محرم"], 2: ["صفر"],
    3: ["ربيع الاول", "ربيع 1", "ربيع الأول"],
    4: ["ربيع الاخر", "ربيع الثاني", "ربيع 2"],
    5: ["جمادى الاولى", "جمادي الاولى", "جمادى الأولى", "جمادى 1", "جمادي 1"],
    6: ["جمادى الاخرة", "جمادي الاخرة", "جمادى الثانية", "جمادي الثانية",
        "جمادى 2", "جمادي 2"],
    7: ["رجب"], 8: ["شعبان"], 9: ["رمضان"], 10: ["شوال"],
    11: ["ذو القعدة", "ذي القعدة", "ذو القعده"],
    12: ["ذو الحجة", "ذي الحجة", "ذو الحجه"],
}
_GREG_MONTHS = {
    1: ["يناير", "كانون الثاني", "january", "jan"],
    2: ["فبراير", "شباط", "february", "feb"],
    3: ["مارس", "اذار", "آذار", "march", "mar"],
    4: ["ابريل", "أبريل", "نيسان", "april", "apr"],
    5: ["مايو", "ايار", "أيار", "may"],
    6: ["يونيو", "حزيران", "june", "jun"],
    7: ["يوليو", "تموز", "july", "jul"],
    8: ["اغسطس", "أغسطس", "اب", "august", "aug"],
    9: ["سبتمبر", "ايلول", "أيلول", "september", "sep"],
    10: ["اكتوبر", "أكتوبر", "تشرين الاول", "october", "oct"],
    11: ["نوفمبر", "تشرين الثاني", "november", "nov"],
    12: ["ديسمبر", "كانون الاول", "december", "dec"],
}


def _month_index(table: dict) -> dict:
    out = {}
    for num, names in table.items():
        for name in names:
            out[norm(name).strip().lower()] = num
    return out


_HIJRI_BY_NAME = _month_index(_HIJRI_MONTHS)
_GREG_BY_NAME = _month_index(_GREG_MONTHS)
_ALL_MONTH_NAMES = sorted(set(_HIJRI_BY_NAME) | set(_GREG_BY_NAME),
                          key=len, reverse=True)

# 1447/03/29 هـ  |  29/03/1447هـ  |  2026-09-20 م
_NUM_DATE = re.compile(
    r"(?<![0-9])(\d{1,4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,4})(?![0-9])"
    r"\s*(هجري|ميلادي|ه|م)?")
# 29 رجب 1447 هـ  |  في شهر رجب لعام 1447
_WORD_DATE = re.compile(
    r"(?:(\d{1,2})\s*(?:من\s*)?)?(?:شهر\s*)?"
    r"(" + "|".join(re.escape(n) for n in _ALL_MONTH_NAMES) + r")"
    r"\s*(?:من\s*)?(?:لعام|عام|لسنه|سنه)?\s*(\d{3,4})",
    re.IGNORECASE)


class Parsed:
    """One date found in the text: what it says, and what it means."""

    __slots__ = ("text", "start", "end", "calendar", "year", "month", "day",
                 "gregorian", "converter", "ambiguous", "era")

    def __init__(self, text, start, end, calendar, y, m, d, ambiguous=False,
                 era=""):
        self.text, self.start, self.end, self.era = text, start, end, era
        self.calendar, self.year, self.month, self.day = calendar, y, m, d
        self.ambiguous = ambiguous
        self.gregorian, self.converter = None, ""
        if y and m and d and not ambiguous:
            try:
                if calendar == "hijri":
                    self.gregorian, self.converter = hijri_to_gregorian(y, m, d)
                else:
                    self.gregorian, self.converter = date(y, m, d), "none"
            except Exception:
                self.gregorian = None

    @property
    def iso(self) -> str:
        if not (self.year and self.month and self.day):
            return ""
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


def _calendar_of(year: int, era: str) -> str:
    if era in ("ه", "هجري"):
        return "hijri"
    if era in ("م", "ميلادي"):
        return "gregorian"
    if 1200 <= year <= 1499:
        return "hijri"
    if 1900 <= year <= 2199:
        return "gregorian"
    return ""


def _valid(cal: str, y: int, m: int, d: int) -> bool:
    if not (1 <= m <= 12):
        return False
    if cal == "hijri":
        return 1200 <= y <= 1499 and 1 <= d <= 30
    try:
        date(y, m, d)
    except ValueError:
        return False
    return 1900 <= y <= 2199


def parse_dates(text: str) -> list:
    """Every date in `text` (already normalised), in text order."""
    found: list = []
    for m in _NUM_DATE.finditer(text):
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        era = (m.group(4) or "").strip()
        if len(m.group(1)) >= 3:                 # year-first: 1447/03/29
            y, mo, d = a, b, c
        elif len(m.group(3)) >= 3:               # day-first:  29/03/1447
            y, mo, d = c, b, a
        else:                                    # two-digit year: not decidable
            continue
        cal = _calendar_of(y, era)
        if not cal or not _valid(cal, y, mo, d):
            continue
        # The span is the NUMERIC date only. The era marker (هـ / م) is kept
        # aside: it is what `locate` and `restore` would trip over — the
        # document may print ١٤٤٧/٠٣/٢٩هـ, ١٤٤٧/٠٣/٢٩ هـ or no marker at all,
        # while the digits themselves are one protected token either way.
        span = m.span(3)
        found.append(Parsed(text[m.start(1):span[1]], m.start(1), span[1],
                            cal, y, mo, d, era=era))
    for m in _WORD_DATE.finditer(text):
        name = m.group(2).strip().lower()
        y = int(m.group(3))
        day = int(m.group(1)) if m.group(1) else 0
        if name in _HIJRI_BY_NAME and 1200 <= y <= 1499:
            cal, mo = "hijri", _HIJRI_BY_NAME[name]
        elif name in _GREG_BY_NAME and 1900 <= y <= 2199:
            cal, mo = "gregorian", _GREG_BY_NAME[name]
        else:
            continue
        # A month and a year but no day pins the date only to a month: report
        # it, and let `ambiguous` keep it out of a Valid/Expired verdict.
        found.append(Parsed(m.group(0).strip(), m.start(), m.end(), cal, y, mo,
                            day or 1, ambiguous=not day))
    found.sort(key=lambda p: p.start)
    return found


# =============================================================================
# Expiry labels
# =============================================================================

# (phrase, rank). Rank 3 names the instrument itself and is trusted outright;
# rank 1 is a bare "until", correct on a validity line and wrong next to a
# passport. Longest match wins within a rank, highest rank wins overall.
# Written as REGEX, not literals: the instrument names itself in the middle of
# the phrase (تنتهي هذه الوكالة في / ينتهي هذا العقد بتاريخ) and a literal list
# would need a row per wording. Written here in natural Arabic and normalised at
# import, exactly as ksa_deeds.yaml requires of its own strings.
_SELF = r"(?:هذه\s+|هذا\s+|هٰذه\s+)?(?:الوكالة|الوثيقة|الصك|العقد|التفويض|الرخصة|الشهادة|الحجة)"
_LABELS = [
    (r"تاريخ\s+(?:انتهاء|نهاية|إنهاء)\s+" + _SELF, 3),
    (r"مدة\s+" + _SELF + r"\s+وتاريخ\s+انتهائها", 3),
    (r"(?:تنتهي|ينتهي|تسري|يسري)\s+" + _SELF + r"\s*(?:في|بتاريخ|حتى|إلى)", 3),
    (r"تاريخ\s+انتهاء\s+الصلاحية", 3),
    (r"(?:expiry|expiration)\s+date", 3),
    (r"date\s+of\s+expiry", 3),
    (r"تاريخ\s+(?:الانتهاء|النهاية)", 2),
    (r"انتهاء\s+الصلاحية", 2),
    (r"نهاية\s+(?:المدة|مدة\s+" + _SELF + r")", 2),
    (r"(?:صالحة|صالح|سارية|ساري|نافذة)\s+(?:حتى|لغاية|إلى)", 2),
    (r"(?:تنتهي|ينتهي)\s+(?:بتاريخ|في\s+تاريخ)", 2),
    (r"valid\s+(?:until|through|to)", 2),
    (r"expires?\s+on", 2),
    (r"تاريخ\s+انتهاء", 1),
    (r"(?:تنتهي|ينتهي)\s+في", 1),
    (r"(?:حتى|لغاية)\s+تاريخ", 1),
    (r"expiry", 1),
]
# Ranked groups, highest first, so the most specific wording is what matches.
_LABEL_RES = [(re.compile(norm(p), re.IGNORECASE), r) for p, r in _LABELS]
_LABEL_RE = re.compile("|".join("(?:%s)" % norm(p) for p, _ in _LABELS),
                       re.IGNORECASE)


def _label_rank(phrase: str) -> int:
    """The highest rank whose pattern covers this whole matched phrase."""
    best = 1
    for rx, rank in _LABEL_RES:
        if rank > best and rx.fullmatch(phrase):
            best = rank
    return best

# A deed prints its parties' ID expiry dates too. "تاريخ انتهاء الهوية" is not
# the instrument's expiry, and reading it as one is the single worst thing this
# module could do — it would mark a perpetual title deed expired.
# Bare stems, not the definite forms: the document writes للجواز، بالهوية،
# وهويته, and "الجواز" would miss every one of them. رخصة / شهادة are NOT here —
# a licence or a certificate can be the instrument being read (they are in
# _SELF), so excluding them would blind the scanner to its own subject.
_NOT_OURS = [norm(w) for w in (
    "هوية", "إقامة", "جواز", "بطاقة", "رخصة القيادة", "السجل التجاري",
    "العضوية", "التأمين", "الاشتراك", "الضمان",
    "passport", "identity card", "id card", "iqama", "driving licence",
    "driving license", "membership", "insurance",
)]
# How far past the label the subject words still describe THAT label.
_SUBJECT_WINDOW = 28
# How far past the label a date still belongs to it.
_DATE_WINDOW = 90


def _subject_is_elsewhere(text: str, label_end: int) -> bool:
    tail = text[label_end:label_end + _SUBJECT_WINDOW].lower()
    return any(w in tail for w in _NOT_OURS)


def scan(text_norm: str, dates: list | None = None) -> dict | None:
    """Best (label, date) pair in the normalised text, or None."""
    dates = parse_dates(text_norm) if dates is None else dates
    if not dates:
        return None
    best = None
    for m in _LABEL_RE.finditer(text_norm):
        if _subject_is_elsewhere(text_norm, m.end()):
            continue
        rank = _label_rank(m.group(0))
        for p in dates:
            if not (m.end() <= p.start <= m.end() + _DATE_WINDOW):
                continue
            if "\n" in text_norm[m.end():p.start]:      # a date on a later line
                rank -= 1                               # is weaker evidence
            cand = (rank, -p.start, m.group(0), p)
            if best is None or cand[:2] > best[:2]:
                best = cand
            break
    if best is None:
        return None
    rank, _, label, p = best
    return {"label": label, "date": p, "rank": rank}


# =============================================================================
# Duration ("مدة الوكالة سنة واحدة") — optional, off by default
# =============================================================================

# Days per unit. The DUAL forms (سنتين، شهرين، يومين) mean "two of them" with
# no separate count word, so they carry the doubling themselves.
_UNITS = {"يوم": 1, "ايام": 1, "يوما": 1, "day": 1, "days": 1, "يومين": 2,
          "شهر": 30, "اشهر": 30, "شهور": 30, "شهرا": 30, "month": 30,
          "months": 30, "شهرين": 60,
          "سنه": 365, "سنوات": 365, "عام": 365, "اعوام": 365, "year": 365,
          "years": 365, "سنتين": 730, "سنتان": 730, "عامين": 730}
_WORD_COUNTS = {"واحد": 1, "واحده": 1, "سنه واحده": 1, "شهرين": 2, "سنتين": 2,
                "سنتان": 2, "ثلاث": 3, "ثلاثه": 3, "اربع": 4, "اربعه": 4,
                "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6, "سبع": 7, "سبعه": 7,
                "ثمان": 8, "ثمانيه": 8, "تسع": 9, "تسعه": 9, "عشر": 10, "عشره": 10}
_DURATION = re.compile(
    r"(?:مدتها|مدته|لمدة|مده|مدة)\s*(?:ال\w+\s*)?"
    r"(\d{1,3}|" + "|".join(re.escape(w) for w in sorted(_WORD_COUNTS, key=len, reverse=True)) + r")?"
    r"\s*(" + "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True)) + r")")


def find_duration(text_norm: str) -> tuple:
    """(days, matched text) for a stated term, or (0, "")."""
    m = _DURATION.search(norm(text_norm))
    if not m:
        return 0, ""
    unit = _UNITS.get(m.group(2), 0)
    if not unit:
        return 0, ""
    raw = (m.group(1) or "").strip()
    count = int(raw) if raw.isdigit() else _WORD_COUNTS.get(raw, 1)
    return count * unit, m.group(0).strip()


# =============================================================================
# Declared lifecycle state (حالة الوكالة)
# =============================================================================

_STATE_FIELD_HINTS = [norm(s) for s in ("حالة الوكالة", "حالة الصك",
                                        "حالة الوثيقة", "الحالة", "status")]
_STATE_VALUES = {"ملغاه": "cancelled", "ملغيه": "cancelled", "ملغى": "cancelled",
                 "منسوخه": "cancelled", "معزوله": "cancelled",
                 "موقوفه": "suspended", "موقوف": "suspended", "معلقه": "suspended",
                 "منتهيه": "declared_expired", "منتهي": "declared_expired",
                 "cancelled": "cancelled", "revoked": "cancelled",
                 "suspended": "suspended", "expired": "declared_expired"}


def declared_state(sections: list) -> tuple:
    """(state, field) read from the structured fields, else ("", None)."""
    for s in sections or []:
        for f in (s.get("fields") or []):
            key = str(f.get("key", ""))
            label = norm(str(f.get("label", ""))).strip().lower()
            value = norm(str(f.get("value", ""))).strip().lower()
            if not value:
                continue
            if not (key.endswith("_status") or key == "status"
                    or any(h == label or label.startswith(h) for h in _STATE_FIELD_HINTS)):
                continue
            for token, state in _STATE_VALUES.items():
                if token in value:
                    return state, f
    return "", None


# =============================================================================
# Tier 1: an expiry field the structurer already produced
# =============================================================================

_EXPIRY_KEYS = ("wakala_expiry_date_hijri", "expiry_date", "expiry_date_hijri",
                "expiry_date_gregorian", "contract_expiry_date")


def _from_fields(sections: list) -> dict | None:
    """An expiry the structurer already extracted, with its citation intact.

    Matched by registry KEY first. Generic documents have no keys — there the
    field's own printed label is all there is, so it is matched against the same
    ranked label patterns as the text scan, at rank 2 or better, and through the
    same party-document guard: a free-form pass emits the deed's
    "تاريخ انتهاء الهوية" as a field like any other, and that is the وكيل's ID,
    not the instrument.
    """
    for s in sections or []:
        for f in (s.get("fields") or []):
            if not isinstance(f, dict):
                continue
            value = str(f.get("value", "") or "").strip()
            if not value:
                continue
            label = norm(str(f.get("label", ""))).strip().lower()
            m = _LABEL_RE.search(label)
            if not (str(f.get("key", "")) in _EXPIRY_KEYS
                    or (m is not None and _label_rank(m.group(0)) >= 2
                        and not _subject_is_elsewhere(label, m.end()))):
                continue
            dates = parse_dates(norm(value))
            if dates:
                return {"field": f, "date": dates[0],
                        "label": str(f.get("label", ""))}
    return None


# =============================================================================
# Tier 3: a focused model pass over the whole document, in chunks
# =============================================================================

_MODEL_PROMPT = (
    "You are reading ONE Arabic legal instrument (صك، وكالة، عقد). Find the "
    "date on which THE INSTRUMENT ITSELF stops being valid — its expiry date, "
    "تاريخ انتهاء الوكالة / تاريخ انتهاء الصلاحية / تنتهي بتاريخ. Rules:\n"
    "- Copy the date EXACTLY as the text prints it: same digits, same "
    "separators, same era marker (هـ / م). Never convert or reformat it.\n"
    "- It may be inside a clause or a sentence, not only in a header.\n"
    "- Do NOT return the ISSUE date (تاريخ الإصدار، تاريخ التحرير، تاريخ "
    "الوكالة) and do NOT return a PARTY's document expiry (تاريخ انتهاء "
    "الهوية / الإقامة / الجواز). Those are not the instrument's expiry.\n"
    "- If this text does not state one, return an empty string for every "
    "field. An empty answer is a CORRECT answer; never guess.\n"
    "- quote: the phrase around the date, copied verbatim from the text.\n"
    "Return ONLY the JSON object."
)
_MODEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"expiry_date": {"type": "string"},
                   "calendar": {"type": "string"},
                   "label": {"type": "string"},
                   "quote": {"type": "string"}},
    "required": ["expiry_date", "calendar", "label", "quote"],
}
_MODEL_OUT_TOKENS = 192


def _chunks(text: str, size: int, limit: int) -> list:
    """Overlapping windows cut at line boundaries."""
    if len(text) <= size:
        return [text]
    out, pos = [], 0
    while pos < len(text) and len(out) < limit:
        end = min(len(text), pos + size)
        if end < len(text):
            cut = text.rfind("\n", pos + size // 2, end)
            if cut > pos:
                end = cut
        out.append(text[pos:end])
        if end >= len(text):
            break
        pos = max(end - _CHUNK_OVERLAP, pos + 1)
    return out


def _model_scan(text: str) -> dict | None:
    """Ask the structurer model, chunk by chunk, until one answers."""
    import json
    from llm import N_CTX, chat_json

    room = int(max(2000, (N_CTX - _MODEL_OUT_TOKENS - 512) * 1.5)
               - len(_MODEL_PROMPT) - 200)
    for chunk in _chunks(text, room, MAX_CHUNKS):
        messages = [{"role": "system", "content": _MODEL_PROMPT},
                    {"role": "user", "content": "Document text:\n\n" + chunk}]
        try:
            raw, _ = chat_json(messages, _MODEL_SCHEMA, _MODEL_OUT_TOKENS)
            data = json.loads(raw)
        except Exception as exc:
            print("expiry: model pass failed:", repr(exc))
            return None
        value = str(data.get("expiry_date", "") or "").strip()
        if not value:
            continue
        dates = parse_dates(norm(value))
        if dates:
            return {"date": dates[0], "raw": value,
                    "label": str(data.get("label", "") or "").strip(),
                    "quote": str(data.get("quote", "") or "").strip()}
    return None


# =============================================================================
# Verdict
# =============================================================================


def classify(expires_on: date, today: date) -> tuple:
    """(status, days remaining). Today counts as the last valid day."""
    days = (expires_on - today).days
    if days < 0:
        return "expired", days
    if days <= SOON_DAYS:
        return "expiring_soon", days
    return "valid", days


def _result(**kw) -> dict:
    out = {
        "status": "unknown", "status_ar": STATUS_AR["unknown"],
        "date_text": "", "date_normalized": "", "calendar": "",
        "date_gregorian": "", "days_remaining": None,
        "basis": "", "confidence": "low", "verified": False, "derived": False,
        "label": "", "source": None, "converter": "",
        "state_override": "", "state_override_ar": "", "note": "",
        "assessed_on": "", "assessed_on_hijri": "",
    }
    out.update(kw)
    out["status_ar"] = STATUS_AR.get(out["status"], STATUS_AR["unknown"])
    out["state_override_ar"] = OVERRIDE_AR.get(out["state_override"], "")
    return out


def detect(text: str, *, sections: list | None = None, locate=None,
           verify=None, restore=None, use_model: bool | None = None,
           today: date | None = None) -> dict:
    """The document's own expiry, as a JSON-ready dict. Never raises.

    `locate(value, labels=[...]) -> citation | None`, `verify(value) -> bool`
    and `restore(value) -> value` are the structurer's fidelity helpers, passed
    in so this module needs nothing from structure.py. All three are optional:
    without them the result simply carries no citation and `verified` is False.
    """
    today = today or date.today()
    hijri_today, _ = gregorian_to_hijri(today)
    stamp = {"assessed_on": today.isoformat(),
             "assessed_on_hijri": "%04d-%02d-%02d" % hijri_today}

    state, state_field = declared_state(sections or [])
    text_norm = norm(text or "")

    parsed, source, basis, label = None, None, "", ""
    hit = _from_fields(sections or [])
    if hit:
        parsed, label, basis = hit["date"], hit["label"], "template_field"
        source = hit["field"].get("source")
    else:
        found = scan(text_norm)
        if found:
            parsed, label, basis = found["date"], found["label"], "label"

    if parsed is None and (USE_MODEL if use_model is None else use_model):
        got = _model_scan(text or "")
        if got:
            parsed, basis = got["date"], "model"
            label = got["label"] or got["quote"]

    if parsed is None and FROM_DURATION:
        days, term = find_duration(text_norm)
        issue = _issue_date(text_norm)
        if days and issue and issue.gregorian:
            expires = issue.gregorian + timedelta(days=days)
            status, remaining = classify(expires, today)
            return _result(
                status=status, date_text=term, calendar="derived",
                date_gregorian=expires.isoformat(), days_remaining=remaining,
                basis="duration", confidence="low", derived=True, label=term,
                source=locate(term) if locate else None,
                state_override=state, note="محتسب من مدة الوكالة وتاريخ إصدارها",
                **stamp)

    if parsed is None:
        note = "لم يُعثر على تاريخ انتهاء في المستند"
        if state:
            note = f"لا يوجد تاريخ انتهاء؛ حالة الوثيقة: {OVERRIDE_AR[state]}"
        return _result(basis="", state_override=state, note=note, **stamp)

    # The value as the DOCUMENT prints it (Arabic-Indic digits and all).
    shown = restore(parsed.text) if restore else parsed.text
    verified = bool(verify(parsed.text)) if verify else False
    if source is None and locate is not None:
        source = locate(parsed.text, labels=[label] if label else [])
    if source is not None:
        verified = verified or not source.get("approx")

    common = dict(date_text=shown, date_normalized=parsed.iso,
                  calendar=parsed.calendar, basis=basis, label=label,
                  source=source, verified=verified,
                  converter=parsed.converter, state_override=state, **stamp)

    if parsed.ambiguous or parsed.gregorian is None:
        return _result(note="التاريخ غير مكتمل (شهر وسنة دون يوم) — يحتاج مراجعة",
                       **common)
    if basis == "model" and not verified:
        return _result(date_gregorian=parsed.gregorian.isoformat(),
                       note="التاريخ غير مقتبس حرفيًا من نص المستند — يحتاج مراجعة",
                       **common)

    status, days = classify(parsed.gregorian, today)
    confidence = "high" if (verified and basis in ("template_field", "label")) else "medium"
    return _result(status=status, days_remaining=days,
                   date_gregorian=parsed.gregorian.isoformat(),
                   confidence=confidence, **common)


_ISSUE_LABELS = [norm(s) for s in ("تاريخ الوكالة", "تاريخ الإصدار",
                                   "تاريخ التحرير", "تاريخ الصك", "حررت في",
                                   "تاريخ العقد", "بتاريخ")]


def _issue_date(text_norm: str):
    dates = parse_dates(text_norm)
    for lab in _ISSUE_LABELS:
        i = text_norm.find(lab)
        while i != -1:
            for p in dates:
                if i + len(lab) <= p.start <= i + len(lab) + _DATE_WINDOW:
                    return p
            i = text_norm.find(lab, i + 1)
    return dates[0] if dates else None
