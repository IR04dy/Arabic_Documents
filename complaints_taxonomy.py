"""Complaint taxonomy: the closed vocabularies the CMS routes complaints into.

`templates/complaints_taxonomy.yaml` holds the receiving entity (the desk
every complaint is addressed to, e.g. إمارة منطقة الرياض, and the region it has
jurisdiction over), priorities, ministries, categories (with subcategories and
a default ministry), regions (with their cities), the governorates of the
entity's region (with their places; both also with Latin-script names),
statuses, priority factors, scopes, tones, rule signals and review reasons.
Every id there becomes an enum in the
LLM's JSON schema, a filter in the API and a stored column value, so the
loader is strict: a broken file raises TaxonomyError at startup naming the
offending entry, instead of silently producing a schema the model cannot
satisfy or a label the UI cannot show.

Usage:

    from complaints_taxonomy import get_taxonomy
    tax = get_taxonomy()
    tax.default_ministry("water_sewage")          -> "mewa"
    tax.region_for_city("حي النسيم بالرياض")       -> "riyadh"
    tax.governorate_for_place("حي العودة بالدرعية") -> "diriyah"
    tax.governorates_in("من الخرج إلى الدرعية")     -> {"kharj", "diriyah"}
    tax.governorate_for_place("Shaqra, Al Wurud district") -> "shaqra"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from registry import Normalizer

DEFAULT_PATH = Path(__file__).resolve().parent / "templates" / "complaints_taxonomy.yaml"

KINDS = ("priorities", "ministries", "categories", "regions", "governorates", "statuses",
         "factors", "scopes", "tones", "signals")
TOP_LEVEL = ("version", "receiving_entity", *KINDS, "review_reasons")
ENTITY_KEYS = ("id", "label_ar", "label_en", "region", "desk_ar")

# Keys each entry kind must carry. `label_en` is optional where the public JSON
# shape does not need it; `description_ar` is prose, allowed anywhere.
_REQUIRED = {
    "priorities": ("id", "label_ar", "label_en", "rank", "sla_hours", "description_ar"),
    "ministries": ("id", "label_ar", "label_en"),
    "categories": ("id", "label_ar", "label_en", "ministry", "description_ar", "subcategories"),
    "regions": ("id", "label_ar", "label_en", "cities"),
    "governorates": ("id", "label_ar", "label_en", "places"),
    "statuses": ("id", "label_ar", "label_en", "open"),
    "factors": ("id", "label_ar"),
    "scopes": ("id", "label_ar"),
    "tones": ("id", "label_ar"),
    "signals": ("id", "label_ar", "floor", "patterns"),
}
_OPTIONAL = ("label_en", "description_ar")
# Optional keys of one kind only: the Latin-script names of a place.
_OPTIONAL_BY_KIND = {"regions": ("places_en",), "governorates": ("places_en",)}

# The pipeline, store and UI emit or rely on these ids by name; a taxonomy
# without them would fail later and far less clearly.
REQUIRED_IDS = {
    "categories": ("other",),
    "ministries": ("other",),
    "regions": ("unknown",),
    "governorates": ("unknown",),
    "statuses": ("new",),
}
REQUIRED_REVIEW_REASONS = (
    "not_a_complaint", "low_confidence", "priority_floor_applied", "category_other",
    "ministry_other", "empty_text", "input_truncated", "evidence_unverified", "fields_unverified",
    "repeat_complainant", "ministry_mismatch", "outside_jurisdiction", "addressed_elsewhere",
    "suspected_instructions", "critical_unsupported",
)

_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")   # URL/enum/SQL-safe, stable

_NORMALIZE = Normalizer()

# --- city matching ------------------------------------------------------------
# Arabic attaches prepositions and conjunctions to the next word: «بالرياض»,
# «وجدة», «لمكة». A city may carry one of و/ف then one of ب/ل/ك; «لـ + الرياض»
# is written «للرياض» (the article's alef drops), handled separately.
_PROCLITIC = "[وف]?[بلك]?"

# Cities that are also everyday words. «المدينة» means "the city" (المدينة
# الصناعية، المدينة الجامعية), so inside running text only «المدينة المنورة» —
# a separate, longer city entry — counts. Bare «المدينة» is trusted only where
# it can only be a city field: the whole text, a whole comma/dash-separated
# segment («المدينة، حي قباء»), or the start of the text right before «حي» or
# «شارع» (the pipeline passes `city + " " + district_or_address`). Even then it
# is the weakest rule: any other city or region name in the text wins, so the
# label in «المدينة: الرياض» never beats الرياض.
AMBIGUOUS_CITIES = frozenset({_NORMALIZE("المدينة")})
_SEGMENTS = re.compile(r"[\n\r,،;؛/|()\[\]\-–—]+")
_LEAD_FOLLOWER = re.compile(r" (?:حي|شارع)(?!\w)")

# A city name right after one of these words is a street/road or a person's
# name, not where the complainant is: «طريق مكة» is a road in Riyadh, «شارع
# الأمير بدر» a street, «خالد بن بدر» a person. The check is on the word
# immediately before the match (plain whitespace between, no punctuation).
_NAME_PREFIXES = frozenset(_NORMALIZE(w) for w in (
    "طريق", "شارع", "الأمير", "الملك", "الشيخ", "بن", "ابن", "بنت", "أبو"))
_PREV_WORD = re.compile(r"(\w+)\s+$")
_EDGE_PUNCT = re.compile(r"^\W+|\W+$")

# A governorate place right after «منطقة» / «إمارة» / «أمير» (with an attached
# preposition: «بمنطقة») names the region or its emirate, not the city:
# «منطقة الرياض» is the whole region, only «الرياض» or «مدينة الرياض» is
# riyadh_city.
_REGION_WORD = re.compile("[وف]?[بلك]?(?:" + "|".join(
    re.escape(_NORMALIZE(w)) for w in ("منطقة", "إمارة", "أمير")) + ")")

# --- Latin-script names -------------------------------------------------------
# English e-mails and letters write places in Latin script: «Shaqra, Al Wurud
# district», «Al-Kharj», «Ar-Riyadh». Each region's and governorate's label_en
# and its `places_en` match case-insensitively as whole words. The article is
# optional and loose wherever it stands («Kharj», «Al Kharj», «Al-Kharj»,
# «Alkharj»; «Wadi Dawasir» for «Wadi Al-Dawasir»), so names are compared
# without it (_latin_tokens). A name inside an e-mail address or a domain
# («riyadh.fan@example.com», «kharj.gov.sa») is not a place.
_ARTICLES = frozenset({"al", "el", "ar", "as", "az", "ad", "adh", "ash", "at", "ath", "an"})
_LATIN_LETTERS = "A-Za-z\u00C0-\u024F"        # ASCII and the accented Latin letters
_LATIN_NAME = re.compile(rf"[{_LATIN_LETTERS}]+(?:[ '’‘`\-]+[{_LATIN_LETTERS}]+)*")
_LATIN_TOKEN = re.compile(rf"[{_LATIN_LETTERS}]+")
_LATIN_SEP = r"[\s'’‘`\-]+"
# «al»/«el» may be written joined to the name («Alkharj»); the sun-letter forms
# only apart from it («Ar-Riyadh», «Az Zulfi»): «as», «at», «an» are English words.
_LATIN_ARTICLE = rf"(?:(?:al|el)[\s'’‘`\-]*|(?:ar|as|az|ad|adh|ash|at|ath|an){_LATIN_SEP})"
# The English counterparts of the Arabic rules above. A road is named AFTER
# its place («Makkah Road», «Kharj Road» are roads in Riyadh); a person after
# a title or «bin» («Prince Badr»). «Riyadh Region», «Riyadh Province»,
# «Emirate of Riyadh» and «Prince of Riyadh» name the region or its emirate,
# not the city (checked for governorates only, like «منطقة الرياض»).
_LATIN_PERSON_BEFORE = re.compile(
    r"(?:^|\W)(?:king|prince|princess|sheikh|shaikh|bin|ibn|bint|abu)\s+$", re.IGNORECASE)
_LATIN_ROAD_AFTER = re.compile(
    r"\s+(?:road|rd|street|st|highway|hwy|expressway|avenue|ave)(?!\w)", re.IGNORECASE)
_LATIN_REGION_BEFORE = re.compile(
    r"(?:^|\W)(?:region|province|emirate|principality|prince|emir|amir)\s+of\s+(?:the\s+)?$",
    re.IGNORECASE)
_LATIN_REGION_AFTER = re.compile(r"\s+(?:region|province|emirate|principality)(?!\w)", re.IGNORECASE)
_CONTEXT = 40                      # characters looked at around a Latin match


class TaxonomyError(Exception):
    """Raised when the taxonomy file is structurally invalid. Never recoverable."""


@dataclass(frozen=True)
class Item:
    """One taxonomy entry. `extra` holds the kind-specific keys (rank,
    sla_hours, ministry, cities, places, places_en, open, floor, patterns,
    description_ar, subcategories; region and desk_ar of the receiving entity
    …); lists there are tuples so the shared cached taxonomy cannot be mutated
    by accident. A governorate's `places_en` includes its label_en, as its
    `places` include its label_ar; a region's holds only what the file lists."""

    id: str
    label_ar: str
    label_en: str = ""
    extra: dict = field(default_factory=dict, compare=False, hash=False)


class Taxonomy:
    """Validated taxonomy with the lookups the pipeline, store, API and UI need."""

    def __init__(self, *, version: int, receiving_entity: Item, review_reasons: dict[str, str],
                 **kinds):
        self.version = version
        # extra: region (the region id it has jurisdiction over), desk_ar
        self.receiving_entity: Item = receiving_entity
        self.priorities: tuple[Item, ...] = kinds["priorities"]
        self.ministries: tuple[Item, ...] = kinds["ministries"]
        self.categories: tuple[Item, ...] = kinds["categories"]
        self.regions: tuple[Item, ...] = kinds["regions"]
        self.governorates: tuple[Item, ...] = kinds["governorates"]
        self.statuses: tuple[Item, ...] = kinds["statuses"]
        self.factors: tuple[Item, ...] = kinds["factors"]
        self.scopes: tuple[Item, ...] = kinds["scopes"]
        self.tones: tuple[Item, ...] = kinds["tones"]
        self.signals: tuple[Item, ...] = kinds["signals"]
        self.review_reasons: dict[str, str] = dict(review_reasons)

        self._index = {k: {it.id: it for it in getattr(self, k)} for k in KINDS}
        self._subs: dict[str, tuple[str, str]] = {}          # sub id -> (category, label)
        for cat in self.categories:
            for sub in cat.extra["subcategories"]:
                self._subs[sub["id"]] = (cat.id, sub["label_ar"])
        self._city_rules = self._build_city_rules()
        # (pattern, governorate id, length, latin) per place name, either script
        self._place_rules = [(_word_pattern(norm), gov.id, len(norm), False)
                             for gov in self.governorates
                             for norm in (_NORMALIZE(p) for p in gov.extra["places"])]
        for gov in self.governorates:
            for name in gov.extra["places_en"]:
                pattern, length = _latin_rule(name)
                self._place_rules.append((pattern, gov.id, length, True))
        # Every Arabic place name as written, for places_in (governorate places
        # first). Latin names are left out: they never overlap a signal phrase.
        names = [p for gov in self.governorates for p in gov.extra["places"]] + [
            c for region in self.regions for c in region.extra["cities"]]
        self._names = [(_word_pattern(_NORMALIZE(n)), n)
                       for n in dict.fromkeys(names) if _NORMALIZE(n) not in AMBIGUOUS_CITIES]

    @classmethod
    def from_dict(cls, raw) -> "Taxonomy":
        """Validate a parsed YAML document and build the taxonomy."""
        return cls(**_validate(raw))

    # ------------------------------------------------------------ lookups

    def _kind(self, kind: str) -> dict:
        if kind == "review_reasons":
            return {k: Item(k, v) for k, v in self.review_reasons.items()}
        try:
            return self._index[kind]
        except KeyError:
            raise KeyError(f"unknown taxonomy kind {kind!r}") from None

    def ids(self, kind: str) -> list[str]:
        return list(self._kind(kind))

    def get(self, kind: str, id: str | None) -> Item | None:
        items = self._kind(kind)
        return items.get(id) if isinstance(id, str) else None

    def label(self, kind: str, id: str | None) -> str:
        item = self.get(kind, id)
        return item.label_ar if item else ""

    def subcategory_ids(self, category_id: str | None = None) -> list[str]:
        if category_id is None:
            return list(self._subs)
        cat = self.get("categories", category_id)
        return [s["id"] for s in cat.extra["subcategories"]] if cat else []

    def subcategory_label(self, sub_id: str | None) -> str:
        return self._subs.get(sub_id, ("", ""))[1] if isinstance(sub_id, str) else ""

    def category_of_subcategory(self, sub_id: str | None) -> str | None:
        return self._subs.get(sub_id, (None, ""))[0] if isinstance(sub_id, str) else None

    def default_ministry(self, category_id: str | None) -> str:
        cat = self.get("categories", category_id)
        return cat.extra["ministry"] if cat else "other"

    def priority_rank(self, priority_id: str | None) -> int:
        """Rank for ordering; 0 for an unknown id so it sorts below everything."""
        item = self.get("priorities", priority_id)
        return item.extra["rank"] if item else 0

    def sla_hours(self, priority_id: str) -> int:
        """SLA of a priority. Unknown ids raise: a silent default would stamp a
        wrong due date on a real complaint."""
        item = self.get("priorities", priority_id)
        if item is None:
            raise KeyError(f"unknown priority {priority_id!r}")
        return item.extra["sla_hours"]

    # ------------------------------------------------------------ regions

    def _build_city_rules(self) -> list:
        """(pattern, region id, normalised length, ambiguous, is_city, latin)
        per name. Region names (the label, and the label without «منطقة»; the
        English label) rank below cities, so «منطقة القصيم» or «بالقصيم»
        resolve when no city is named. The Latin names of the entity's
        governorates are Latin cities of its region, so both lookups agree
        without listing them twice."""
        rules = []
        home = self.receiving_entity.extra["region"]
        for region in self.regions:
            if region.id == "unknown":
                continue
            for city in region.extra["cities"]:
                norm = _NORMALIZE(city)
                rules.append((_word_pattern(norm), region.id, len(norm),
                              norm in AMBIGUOUS_CITIES, True, False))
            label = _NORMALIZE(region.label_ar)
            # «منطقة القصيم» -> «القصيم»; «المنطقة الشرقية» keeps only its full
            # label, since «الشرقية» alone is just "eastern".
            for name in {label, label.removeprefix(_NORMALIZE("منطقة") + " ")}:
                rules.append((_word_pattern(name), region.id, len(name),
                              name in AMBIGUOUS_CITIES, False, False))
            latin = [(name, True) for name in region.extra["places_en"]]
            if region.id == home:
                latin += [(name, True) for gov in self.governorates for name in gov.extra["places_en"]]
            if _latin_tokens(region.label_en):
                latin.append((region.label_en, False))
            for name, is_city in dict.fromkeys(latin):
                pattern, length = _latin_rule(name)
                rules.append((pattern, region.id, length, False, is_city, True))
        return rules

    def region_for_city(self, text: str | None) -> str | None:
        """Region id of the city named in `text`, or None.

        Matching is on normalised text (registry.Normalizer: alef/hamza/taa
        marbuta folded, tashkeel/tatweel dropped, Arabic-Indic digits folded),
        so «جده» finds جدة and «أبها» finds «ابها». A city must be a whole word,
        optionally with an attached proclitic («بالرياض», «وجدة», «للرياض») —
        «الرس» never matches inside «الرسالة». When several cities match, the
        longest wins («مكة المكرمة» over «مكة»), then the earliest.

        Two ambiguity rules keep everyday words from routing a complaint:
          * bare «المدينة» ("the city") is trusted only as a whole field (see
            AMBIGUOUS_CITIES) and loses to any other match; in running text
            only «المدينة المنورة» counts;
          * a city right after طريق/شارع/الأمير/الملك/الشيخ/بن/ابن/بنت/أبو is a
            street or a person's name and is skipped («طريق مكة» in Riyadh).
        A region's own name («منطقة القصيم», «القصيم») is a fallback when no
        city matches. Latin-script names match too, under their own rules
        (see «Latin-script names» above): «Buraidah», «Al Khobar», «Makkah
        Region»; «Makkah Road» is a road.
        """
        if not isinstance(text, str):
            return None
        norm = _NORMALIZE(text)
        if not norm:
            return None
        best = None                        # ((unambiguous, is_city, length, -start), region)
        for pattern, region, length, ambiguous, is_city, latin in self._city_rules:
            if ambiguous:
                start = 0 if _whole_field(pattern, text, norm) else None
            else:
                start = next((m.start() for m in pattern.finditer(norm)
                              if not _not_a_place(norm, m, latin)), None)
            if start is None:
                continue
            key = (not ambiguous, is_city, length, -start)
            if best is None or key > best[0]:
                best = (key, region)
        return best[1] if best else None

    # -------------------------------------------------------- governorates

    def governorate_for_place(self, text: str | None) -> str | None:
        """Governorate id of the place named in `text`, or None.

        The same matching as region_for_city — normalised, whole words with
        an optional attached proclitic («بالخرج», «للخرج»), longest place
        first, then the earliest — so «الخرج» never matches inside «الخارج».
        A place right after طريق/شارع/الأمير/… is a road or a person
        («طريق الخرج» is a road in Riyadh), and one right after
        منطقة/إمارة/أمير names the region: «منطقة الرياض» alone is NOT
        riyadh_city, while «الرياض» and «مدينة الرياض» are. Latin-script
        names match the same way: «Shaqra», «Al-Kharj», «Riyadh» (but not
        «Riyadh Region», «Emirate of Riyadh» or «Kharj Road»). The `unknown`
        governorate has no places and is never returned.
        """
        if not isinstance(text, str):
            return None
        norm = _NORMALIZE(text)
        if not norm:
            return None
        best = None                        # ((length, -start), governorate)
        for pattern, gov, length, latin in self._place_rules:
            start = next((m.start() for m in pattern.finditer(norm)
                          if not _not_a_place(norm, m, latin, region_word=True)), None)
            if start is None:
                continue
            key = (length, -start)
            if best is None or key > best[0]:
                best = (key, gov)
        return best[1] if best else None

    # ------------------------------------------------- every place in a text

    def governorates_in(self, text: str | None) -> set[str]:
        """Every governorate with a place named anywhere in `text`, under the
        rules of governorate_for_place (so «منطقة الرياض» and «طريق الخرج»
        name none). The pipeline uses it to check that a governorate the
        model inferred is backed by the text itself."""
        norm = _NORMALIZE(text) if isinstance(text, str) else ""
        return {gov for pattern, gov, _, latin in self._place_rules
                if any(not _not_a_place(norm, m, latin, region_word=True)
                       for m in pattern.finditer(norm))} if norm else set()

    def regions_in(self, text: str | None) -> set[str]:
        """Every region with a city or its own name in `text`, under the rules
        of region_for_city (bare «المدينة» only as a whole field; «طريق مكة»
        and «Makkah Road» are roads). Where region_for_city picks ONE region,
        this lists all."""
        if not isinstance(text, str):
            return set()
        norm = _NORMALIZE(text)
        found = set()
        for pattern, region, _, ambiguous, _, latin in self._city_rules:
            if region in found or not norm:
                continue
            if (_whole_field(pattern, text, norm) if ambiguous else
                    any(not _not_a_place(norm, m, latin) for m in pattern.finditer(norm))):
                found.add(region)
        return found

    def places_in(self, text: str | None) -> list[str]:
        """The taxonomy's Arabic place names (governorate places and region
        cities, as listed) that occur in `text` as whole words, e.g. «الحريق» in
        «محافظة الحريق». No street/region-word rules: this lists names, it
        does not locate the complaint. Bare «المدينة» is never listed."""
        norm = _NORMALIZE(text) if isinstance(text, str) else ""
        return [name for pattern, name in self._names if norm and pattern.search(norm)]

    # ------------------------------------------------------------- public

    def to_public(self) -> dict:
        """JSON for GET /complaints/config (no city or place lists and no
        signal patterns: those are matching internals, not UI vocabulary)."""
        def basic(it: Item) -> dict:
            return {"id": it.id, "label_ar": it.label_ar, "label_en": it.label_en}

        entity = self.receiving_entity
        return {
            "version": self.version,
            "receiving_entity": {**basic(entity), "region": entity.extra["region"],
                                 "desk_ar": entity.extra["desk_ar"]},
            "priorities": [{**basic(p), "rank": p.extra["rank"], "sla_hours": p.extra["sla_hours"],
                            "description_ar": p.extra["description_ar"]} for p in self.priorities],
            "ministries": [basic(m) for m in self.ministries],
            "categories": [{**basic(c), "ministry": c.extra["ministry"],
                            "description_ar": c.extra["description_ar"],
                            "subcategories": [dict(s) for s in c.extra["subcategories"]]}
                           for c in self.categories],
            "regions": [basic(r) for r in self.regions],
            "governorates": [basic(g) for g in self.governorates],
            "statuses": [{**basic(s), "open": s.extra["open"]} for s in self.statuses],
            "factors": [basic(f) for f in self.factors],
            "scopes": [basic(s) for s in self.scopes],
            "tones": [basic(t) for t in self.tones],
            "signals": [{"id": s.id, "label_ar": s.label_ar, "floor": s.extra["floor"]}
                        for s in self.signals],
            "review_reasons": dict(self.review_reasons),
        }


def _word_pattern(norm: str) -> re.Pattern:
    alts = [_PROCLITIC + re.escape(norm)]
    if norm.startswith("ال") and len(norm) > 2:
        alts.append("[وف]?لل" + re.escape(norm[2:]))
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)")


def _after_name_prefix(norm: str, start: int) -> bool:
    m = _PREV_WORD.search(norm, 0, start)
    return bool(m) and m.group(1) in _NAME_PREFIXES


def _after_region_word(norm: str, start: int) -> bool:
    m = _PREV_WORD.search(norm, 0, start)
    return bool(m) and _REGION_WORD.fullmatch(m.group(1)) is not None


def _latin_tokens(name: str) -> tuple[str, ...]:
    """A Latin name's words, lower-cased, without articles: «Wadi Al-Dawasir»
    -> ("wadi", "dawasir"), «Dir'iyah» -> ("dir", "iyah"). Two names with the
    same tokens are the same name."""
    return tuple(t for t in _LATIN_TOKEN.findall(name.lower()) if t not in _ARTICLES)


def _latin_rule(name: str) -> tuple[re.Pattern, int]:
    """(pattern, length) of a Latin name: its words in order, each with an
    optional article, as whole words outside e-mail addresses and domains."""
    tokens = _latin_tokens(name)
    body = _LATIN_SEP.join(f"{_LATIN_ARTICLE}?{re.escape(t)}" for t in tokens)
    return (re.compile(rf"(?<![\w@.])(?:{body})(?!\w|@|\.\w)", re.IGNORECASE),
            len(" ".join(tokens)))


def _not_a_place(norm: str, m: re.Match, latin: bool, region_word: bool = False) -> bool:
    """True when the name matched at `m` is a road or a person's name, or
    (region_word) names the region or its emirate rather than the town."""
    if not latin:
        return (_after_name_prefix(norm, m.start())
                or (region_word and _after_region_word(norm, m.start())))
    before = norm[max(0, m.start() - _CONTEXT):m.start()]
    after = norm[m.end():m.end() + _CONTEXT]
    if _LATIN_PERSON_BEFORE.search(before) or _LATIN_ROAD_AFTER.match(after):
        return True
    return region_word and bool(_LATIN_REGION_BEFORE.search(before)
                                or _LATIN_REGION_AFTER.match(after))


def _whole_field(pattern: re.Pattern, raw: str, norm: str) -> bool:
    """An ambiguous city standing as a field of its own (see AMBIGUOUS_CITIES).
    Segments are split on the RAW text: normalising first would fold the
    newlines between fields into spaces."""
    if any(pattern.fullmatch(_EDGE_PUNCT.sub("", _NORMALIZE(seg)))
           for seg in _SEGMENTS.split(raw)):
        return True
    m = pattern.match(norm)
    return bool(m) and _LEAD_FOLLOWER.match(norm, m.end()) is not None


# =============================================================================
# Validation
# =============================================================================


def _text(value, where: str, key: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise TaxonomyError(f"{where}: {key!r} must be a non-empty string, got {value!r}")
    return value.strip()


def _int(value, where: str, key: str) -> int:
    if type(value) is not int:                      # bool is an int subclass: reject it
        raise TaxonomyError(f"{where}: {key!r} must be an integer, got {value!r}")
    return value


def _strings(value, where: str, key: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise TaxonomyError(f"{where}: {key!r} must be a {'' if allow_empty else 'non-empty '}list")
    return tuple(_text(v, where, f"{key}[{i}]") for i, v in enumerate(value))


def _entries(raw: dict, kind: str) -> list[tuple[str, dict]]:
    """(where, entry) for each entry of a kind, with ids and keys checked."""
    entries = raw.get(kind)
    if not isinstance(entries, list) or not entries:
        raise TaxonomyError(f"{kind}: must be a non-empty list")
    allowed = set(_REQUIRED[kind]) | set(_OPTIONAL) | set(_OPTIONAL_BY_KIND.get(kind, ()))
    seen: set[str] = set()
    out = []
    for i, entry in enumerate(entries):
        where = f"{kind}[{i}]"
        if not isinstance(entry, dict):
            raise TaxonomyError(f"{where}: entry must be a mapping, got {entry!r}")
        eid = entry.get("id")
        if not isinstance(eid, str) or not _ID.match(eid):
            raise TaxonomyError(f"{where}: 'id' must match {_ID.pattern}, got {eid!r}")
        where = f"{kind}[{i}] (id {eid!r})"
        if eid in seen:
            raise TaxonomyError(f"{where}: duplicate id")
        seen.add(eid)
        missing = [k for k in _REQUIRED[kind] if k not in entry]
        if missing:
            raise TaxonomyError(f"{where}: missing required key(s) {missing}")
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise TaxonomyError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
        out.append((where, entry))
    return out


def _item(kind: str, where: str, entry: dict, extra: dict) -> Item:
    if "description_ar" in entry:
        extra["description_ar"] = _text(entry["description_ar"], where, "description_ar")
    return Item(entry["id"], _text(entry["label_ar"], where, "label_ar"),
                _text(entry.get("label_en"), where, "label_en",
                      required="label_en" in _REQUIRED[kind]),
                extra)


def _latin_names(entry: dict, where: str) -> tuple[str, ...]:
    """The optional `places_en` list: Latin-script names, at least one word
    besides the article («Al» alone names nothing)."""
    if "places_en" not in entry:
        return ()
    names = _strings(entry["places_en"], where, "places_en", allow_empty=True)
    for i, name in enumerate(names):
        if not _LATIN_NAME.fullmatch(name) or not _latin_tokens(name):
            raise TaxonomyError(f"{where}: 'places_en[{i}]' must be a place name in Latin letters "
                                f"(words joined by spaces, hyphens or apostrophes), got {name!r}")
    return names


def _require_ids(kind: str, items) -> None:
    have = {it.id for it in items}
    for rid in REQUIRED_IDS.get(kind, ()):
        if rid not in have:
            raise TaxonomyError(f"{kind}: required id {rid!r} is missing")


def _validate(raw) -> dict:
    if not isinstance(raw, dict):
        raise TaxonomyError(f"taxonomy root must be a mapping, got {type(raw).__name__}")
    unknown = sorted(set(raw) - set(TOP_LEVEL))
    if unknown:
        raise TaxonomyError(f"unknown top-level key(s) {unknown}; expected {list(TOP_LEVEL)}")
    missing = [k for k in TOP_LEVEL if k not in raw]
    if missing:
        raise TaxonomyError(f"missing top-level key(s) {missing}")
    if raw["version"] != 1 or type(raw["version"]) is not int:
        raise TaxonomyError(f"version: unsupported taxonomy version {raw['version']!r} (expected 1)")

    out: dict = {"version": 1}

    # priorities: highest first, distinct descending ranks, positive SLAs
    items, last_rank = [], None
    for where, e in _entries(raw, "priorities"):
        rank = _int(e["rank"], where, "rank")
        sla = _int(e["sla_hours"], where, "sla_hours")
        if rank < 1:
            raise TaxonomyError(f"{where}: 'rank' must be >= 1, got {rank}")
        if sla <= 0:
            raise TaxonomyError(f"{where}: 'sla_hours' must be positive, got {sla}")
        if last_rank is not None and rank >= last_rank:
            raise TaxonomyError(f"{where}: ranks must be distinct and listed highest first "
                                f"(rank {rank} after {last_rank})")
        last_rank = rank
        items.append(_item("priorities", where, e, {"rank": rank, "sla_hours": sla}))
    out["priorities"] = tuple(items)
    priority_ids = [p.id for p in items]

    out["ministries"] = tuple(_item("ministries", w, e, {}) for w, e in _entries(raw, "ministries"))
    _require_ids("ministries", out["ministries"])
    ministry_ids = {m.id for m in out["ministries"]}

    # categories: default ministry must exist; subcategory ids globally unique
    items, sub_seen = [], {}
    for where, e in _entries(raw, "categories"):
        ministry = e["ministry"]
        if ministry not in ministry_ids:
            raise TaxonomyError(f"{where}: ministry {ministry!r} is not a ministry id")
        subs_raw = e["subcategories"]
        if not isinstance(subs_raw, list) or not subs_raw:
            raise TaxonomyError(f"{where}: 'subcategories' must be a non-empty list")
        subs = []
        for j, s in enumerate(subs_raw):
            sw = f"{where}.subcategories[{j}]"
            if not isinstance(s, dict) or set(s) != {"id", "label_ar"}:
                raise TaxonomyError(f"{sw}: must be a mapping with exactly 'id' and 'label_ar', got {s!r}")
            sid = s["id"]
            if not isinstance(sid, str) or not _ID.match(sid):
                raise TaxonomyError(f"{sw}: 'id' must match {_ID.pattern}, got {sid!r}")
            if sid in sub_seen:
                raise TaxonomyError(f"{sw}: duplicate subcategory id {sid!r} "
                                    f"(already used in category {sub_seen[sid]!r})")
            sub_seen[sid] = e["id"]
            subs.append({"id": sid, "label_ar": _text(s["label_ar"], sw, "label_ar")})
        items.append(_item("categories", where, e,
                           {"ministry": ministry, "subcategories": tuple(subs)}))
    out["categories"] = tuple(items)
    _require_ids("categories", items)

    # regions: a city may belong to one region only (after normalisation), and
    # so may a Latin name, the English label included (compared by its words)
    items, city_seen, latin_seen = [], {}, {}
    for where, e in _entries(raw, "regions"):
        cities = _strings(e["cities"], where, "cities", allow_empty=True)
        latin = _latin_names(e, where)
        if e["id"] == "unknown" and cities:
            raise TaxonomyError(f"{where}: the 'unknown' region cannot list cities")
        if e["id"] == "unknown" and latin:
            raise TaxonomyError(f"{where}: the 'unknown' region cannot list places_en")
        for c in cities:
            nc = _NORMALIZE(c)
            if nc in city_seen:
                raise TaxonomyError(f"{where}: city {c!r} is already listed under region "
                                    f"{city_seen[nc]!r}")
            city_seen[nc] = e["id"]
        names = [(n, True) for n in latin]
        if e["id"] != "unknown":           # the English label is a (region) name too
            names.append((_text(e["label_en"], where, "label_en"), False))
        kept = {}
        for n, listed in names:
            key = _latin_tokens(n)
            if not key:                    # a label with no Latin word names nothing to match
                continue
            if latin_seen.get(key, e["id"]) != e["id"]:
                raise TaxonomyError(f"{where}: Latin name {n!r} is already listed under region "
                                    f"{latin_seen[key]!r}")
            latin_seen[key] = e["id"]
            if listed:
                kept.setdefault(key, n)
        items.append(_item("regions", where, e, {"cities": cities, "places_en": tuple(kept.values())}))
    out["regions"] = tuple(items)
    _require_ids("regions", items)

    # receiving entity: one mapping; its jurisdiction must be a real region
    ent, where = raw["receiving_entity"], "receiving_entity"
    if not isinstance(ent, dict):
        raise TaxonomyError(f"{where}: must be a mapping, got {ent!r}")
    missing = [k for k in ENTITY_KEYS if k not in ent]
    if missing:
        raise TaxonomyError(f"{where}: missing required key(s) {missing}")
    unknown = sorted(set(ent) - set(ENTITY_KEYS))
    if unknown:
        raise TaxonomyError(f"{where}: unknown key(s) {unknown}; allowed: {list(ENTITY_KEYS)}")
    if not isinstance(ent["id"], str) or not _ID.match(ent["id"]):
        raise TaxonomyError(f"{where}: 'id' must match {_ID.pattern}, got {ent['id']!r}")
    home = ent["region"]
    if home == "unknown" or home not in {r.id for r in items}:
        raise TaxonomyError(f"{where}: region {home!r} is not a region id "
                            "(the region the entity has jurisdiction over)")
    out["receiving_entity"] = Item(ent["id"], _text(ent["label_ar"], where, "label_ar"),
                                   _text(ent["label_en"], where, "label_en"),
                                   {"region": home, "desk_ar": _text(ent["desk_ar"], where, "desk_ar")})

    # governorates of the entity's region: a place belongs to one governorate
    # and is never a city of ANOTHER region (that would route it both ways);
    # the same for Latin names, compared by their words
    items, place_seen, latin_place_seen = [], {}, {}
    for where, e in _entries(raw, "governorates"):
        places = _strings(e["places"], where, "places", allow_empty=True)
        latin = _latin_names(e, where)
        if e["id"] == "unknown":
            if places:
                raise TaxonomyError(f"{where}: the 'unknown' governorate cannot list places")
            if latin:
                raise TaxonomyError(f"{where}: the 'unknown' governorate cannot list places_en")
        else:                              # its own name is always a place, in either script
            places = (*places, _text(e["label_ar"], where, "label_ar"))
            latin = (*latin, _text(e["label_en"], where, "label_en"))
        kept = {}
        for p in places:
            np_ = _NORMALIZE(p)
            if place_seen.get(np_, e["id"]) != e["id"]:
                raise TaxonomyError(f"{where}: place {p!r} is already listed under governorate "
                                    f"{place_seen[np_]!r}")
            if city_seen.get(np_, home) != home:
                raise TaxonomyError(f"{where}: place {p!r} is a city of region {city_seen[np_]!r}, "
                                    f"not of the receiving entity's region {home!r}")
            place_seen[np_] = e["id"]
            kept.setdefault(np_, p)
        kept_latin = {}
        for n in latin:
            key = _latin_tokens(n)
            if not key:                    # a label with no Latin word names nothing to match
                continue
            if latin_place_seen.get(key, e["id"]) != e["id"]:
                raise TaxonomyError(f"{where}: Latin name {n!r} is already listed under governorate "
                                    f"{latin_place_seen[key]!r}")
            if latin_seen.get(key, home) != home:
                raise TaxonomyError(f"{where}: Latin name {n!r} is a name of region "
                                    f"{latin_seen[key]!r}, not of the receiving entity's region {home!r}")
            latin_place_seen[key] = e["id"]
            kept_latin.setdefault(key, n)
        items.append(_item("governorates", where, e, {"places": tuple(kept.values()),
                                                      "places_en": tuple(kept_latin.values())}))
    out["governorates"] = tuple(items)
    _require_ids("governorates", items)

    items = []
    for where, e in _entries(raw, "statuses"):
        if not isinstance(e["open"], bool):
            raise TaxonomyError(f"{where}: 'open' must be true or false, got {e['open']!r}")
        items.append(_item("statuses", where, e, {"open": e["open"]}))
    out["statuses"] = tuple(items)
    _require_ids("statuses", items)
    if not any(s.extra["open"] for s in items) or all(s.extra["open"] for s in items):
        raise TaxonomyError("statuses: need at least one open and one closed status")

    for kind in ("factors", "scopes", "tones"):
        out[kind] = tuple(_item(kind, w, e, {}) for w, e in _entries(raw, kind))

    # signals: a rule may only raise a priority, and never to the top one
    items = []
    for where, e in _entries(raw, "signals"):
        floor = e["floor"]
        if floor not in priority_ids:
            raise TaxonomyError(f"{where}: floor {floor!r} is not a priority id")
        if floor == priority_ids[0]:
            raise TaxonomyError(f"{where}: floor cannot be the top priority {floor!r}; "
                                "rules may raise a priority but never set it")
        patterns = _strings(e["patterns"], where, "patterns")
        items.append(_item("signals", where, e, {"floor": floor, "patterns": patterns}))
    out["signals"] = tuple(items)

    reasons = raw["review_reasons"]
    if not isinstance(reasons, dict) or not reasons:
        raise TaxonomyError("review_reasons: must be a non-empty mapping of id -> Arabic label")
    for rid, label in reasons.items():
        if not isinstance(rid, str) or not _ID.match(rid):
            raise TaxonomyError(f"review_reasons: id must match {_ID.pattern}, got {rid!r}")
        _text(label, f"review_reasons.{rid}", "label")
    missing = [r for r in REQUIRED_REVIEW_REASONS if r not in reasons]
    if missing:
        raise TaxonomyError(f"review_reasons: missing required id(s) {missing}")
    out["review_reasons"] = {k: v.strip() for k, v in reasons.items()}
    return out


# =============================================================================
# Loading
# =============================================================================


def load_taxonomy(path: str | Path | None = None) -> Taxonomy:
    """Parse and validate the taxonomy YAML at `path` (default: the bundled file)."""
    path = Path(path) if path else DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise TaxonomyError(f"taxonomy not found: {path}") from None
    except yaml.YAMLError as exc:
        raise TaxonomyError(f"taxonomy is not valid YAML: {exc}") from None
    return Taxonomy.from_dict(raw)


@lru_cache(maxsize=1)
def get_taxonomy() -> Taxonomy:
    """Process-wide cached default taxonomy. Use this from request handlers."""
    return load_taxonomy()
