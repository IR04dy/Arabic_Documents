"""Input-layer registry: parse templates/*.yaml and resolve field lists.

This is the module the classifier and the structurer both sit on. It does four
things, in this order:

  1. Parse the registry YAML.
  2. Build a normaliser from the registry's own `normalization:` block and give
     every Arabic string that is MATCHED against page text a normalised twin:
     anchors, negatives, aliases, enum values, field labels, section names, the
     `in_section` / `not_preceded_by` / `co_occurs_with` gating lists, family
     evidence phrases and the shared citation markers. The registry declares
     `apply_to_registry_at_load: true` precisely so a maintainer can write
     readable Arabic; an un-normalised string can never match normalised OCR
     text, so it would be a silently dead string.

     Prose — descriptions, notes, issuer_notes, name_ar — is deliberately NOT
     normalised. It is for display, and normalising it is exactly what the
     "readable Arabic" commitment exists to prevent.

     Regex PATTERNS are the one exception: they are compiled as written, so the
     loader rejects any pattern not already in normalised form rather than
     rewriting it. Normalising a pattern's source corrupts it — stripping the
     tanween from `ملكاً?` rebinds the `?` to the alef.
  3. Validate the whole registry structurally and raise on any error. A broken
     registry must fail at startup, not silently mis-route a document.
  4. Expand group references into the resolved `required_fields` /
     `optional_fields` lists for each template.

Usage:

    from registry import load_registry

    reg = load_registry()                     # templates/ksa_deeds.yaml
    tpl = reg.get("sakk_milkiyyah_aqar")

    tpl.required_fields   -> [Field, ...]     # must be present
    tpl.optional_fields   -> [Field, ...]     # may be present

    reg.normalize("صك مِلْكِيَّة")            -> "صك ملكيه"

Run it directly to inspect a template:

    python registry.py                        # list every template
    python registry.py sakk_milkiyyah_aqar    # dump its resolved fields
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field as dc_field
from functools import lru_cache
from typing import Iterator

import yaml

DEFAULT_REGISTRY = os.environ.get(
    "DEED_REGISTRY_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "ksa_deeds.yaml"),
)


class RegistryError(Exception):
    """Raised when the registry is structurally invalid. Never recoverable."""


# =============================================================================
# Normalisation
# =============================================================================

# Combining marks: tashkeel U+064B-U+065F (which includes the maddah and the
# detached hamza-above/below at U+0653-U+0655), superscript alef U+0670, and the
# Quranic annotation range U+06D6-U+06ED. Matches the class the project's own
# reference pipeline already uses.
_TASHKEEL = re.compile("[ً-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"

# Bidi controls plus the zero-width joiners and spaces. Surya emits bidi marks
# around mixed Arabic/Latin runs; a stray ZWJ inside a word is invisible on
# screen and silently defeats an exact anchor match.
_BIDI = re.compile("[​-‏؜‪-‮⁦-⁩﻿]")
_WS = re.compile(r"\s+")

_ALEF_FORMS = "أإآٱ"           # أ إ آ ٱ
_ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
_EXT_ARABIC_INDIC = "۰۱۲۳۴۵۶۷۸۹"

# Arabic decimal separator ٫ and thousands separator ٬. Folding the digits but
# leaving these produces hybrids like "123٬456٫78", which every numeric value
# type then rejects.
_ARABIC_SEPARATORS = {0x066B: ".", 0x066C: ","}


@dataclass(frozen=True)
class Normalizer:
    """Text normaliser built from the registry's `normalization:` block.

    The same instance is applied to OCR text and to the registry's own strings,
    which is the only way anchor matching can work at all.
    """

    strip_tashkeel: bool = True
    strip_tatweel: bool = True
    collapse_whitespace: bool = True
    strip_bidi_marks: bool = True
    unify_alef: bool = True
    unify_taa_marbuta: bool = True
    unify_hamza_forms: bool = True
    arabic_indic_digits_to_ascii: bool = True
    unify_alef_maqsura: bool = False

    _table: dict = dc_field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        table: dict[int, str] = {}
        if self.unify_alef:
            table.update({ord(c): "ا" for c in _ALEF_FORMS})
        if self.unify_taa_marbuta:
            table[ord("ة")] = "ه"                 # ة -> ه
        if self.unify_hamza_forms:
            table[ord("ؤ")] = "و"                 # ؤ -> و
            table[ord("ئ")] = "ي"                 # ئ -> ي
        if self.unify_alef_maqsura:
            table[ord("ى")] = "ي"                 # ى -> ي
        if self.arabic_indic_digits_to_ascii:
            for digits in (_ARABIC_INDIC, _EXT_ARABIC_INDIC):
                table.update({ord(c): str(i) for i, c in enumerate(digits)})
            table.update(_ARABIC_SEPARATORS)
        if self.strip_tatweel:
            table[ord(_TATWEEL)] = ""
        object.__setattr__(self, "_table", table)

    @classmethod
    def from_config(cls, cfg: dict) -> "Normalizer":
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        return cls(**{k: bool(v) for k, v in (cfg or {}).items() if k in known})

    def __call__(self, text: str) -> str:
        if not text:
            return ""
        # NFC first. The alef and hamza folds below key on the PRECOMPOSED
        # codepoints (U+0622/0623/0625/0624/0626), so decomposed input — a bare
        # alef followed by a combining hamza — slips past them untouched while
        # rendering identically on screen. Composing first makes the folds
        # reachable. NFC leaves ى (U+0649) alone, which is what keeps الموصى
        # and الموصي distinct.
        text = unicodedata.normalize("NFC", text)
        if self.strip_bidi_marks:
            text = _BIDI.sub("", text)
        if self.strip_tashkeel:
            text = _TASHKEEL.sub("", text)
        text = text.translate(self._table)
        if self.collapse_whitespace:
            text = _WS.sub(" ", text).strip()
        return text

    def with_index(self, text: str) -> tuple:
        """(normalised text, raw offset of every normalised character).

        The same folds as __call__, applied one character at a time so each
        output character remembers where it came from. That is what lets a
        value matched in normalised space be cited back to the page, line and
        span of the OCR text it was read from (provenance.py). Whole-string NFC
        can compose a bare letter with a following combining hamza; here the
        letter stays and the hamza goes with the rest of the tashkeel, which is
        the same folded letter either way.
        """
        if not text:
            return "", []
        out: list = []
        idx: list = []
        table = self._table
        for i, ch in enumerate(text):
            for c in unicodedata.normalize("NFC", ch):
                if self.strip_bidi_marks and _BIDI.match(c):
                    continue
                if self.strip_tashkeel and _TASHKEEL.match(c):
                    continue
                c = c.translate(table)
                if not c:
                    continue
                if self.collapse_whitespace and _WS.match(c):
                    if not out or out[-1] == " ":
                        continue
                    c = " "
                out.append(c)
                idx.append(i)
        if self.collapse_whitespace and out and out[-1] == " ":
            out.pop()
            idx.pop()
        return "".join(out), idx


# =============================================================================
# Resolved objects
# =============================================================================


@dataclass(frozen=True)
class ValueType:
    name: str
    pattern: str | None
    compiled: re.Pattern | None
    example: str = ""
    note: str = ""
    status: str = "active"
    fallback: tuple[str, ...] = ()


def _bare(text: str) -> str:
    """Each word without a leading definite article: الابن -> ابن, الأخ الشقيق
    -> اخ شقيق (input is already normalised). Words of three letters or fewer
    are left alone — there ال is the word, not an article."""
    return " ".join(w[2:] if w.startswith("ال") and len(w) > 3 else w
                    for w in text.split())


@dataclass(frozen=True)
class Field:
    """One resolved field on one template.

    `aliases_ar` are role-specific and safe to match directly.
    `shared_aliases_ar` are the underlying group's generic labels (الاسم,
    رقم الهوية). They are correct but AMBIGUOUS whenever a template instantiates
    the same group for more than one role, so a matcher should scope them by
    `section` or down-rank them rather than taking the first hit.
    """

    key: str
    label_ar: str
    label_en: str
    type: str
    section: str
    required: bool
    label_ar_norm: str = ""
    section_norm: str = ""
    repeatable: bool = False
    aliases_ar: tuple[str, ...] = ()
    aliases_ar_norm: tuple[str, ...] = ()
    shared_aliases_ar: tuple[str, ...] = ()
    shared_aliases_ar_norm: tuple[str, ...] = ()
    enum_values_ar: tuple[str, ...] = ()
    enum_values_ar_norm: tuple[str, ...] = ()
    role: str | None = None
    role_label_ar: str = ""          # the instance's Arabic role name (الوارث), "" for local fields
    source: str = "local"
    note: str = ""
    classification_weight: int | None = None
    fallback_types: tuple[str, ...] = ()

    @property
    def all_aliases_ar(self) -> tuple[str, ...]:
        return self.aliases_ar + self.shared_aliases_ar

    def validate(self, value: str, registry: "Registry") -> str:
        """Return "ok", "empty", or "mismatch" for an extracted value.

        A required field is allowed to come back empty: the structurer's
        contract is that it must emit the key, using "" when the document
        genuinely does not carry the value. "empty" on a required field is a
        completeness finding; "mismatch" is a correctness finding.
        """
        if value is None or not str(value).strip():
            return "empty"
        raw = str(value).strip()
        candidate = registry.normalize(raw)

        # Checked BEFORE the pattern loop: the `enum` value type carries no
        # pattern, so falling through to the loop would return "ok" for every
        # value and the enum constraint would never be enforced.
        if self.type == "enum":
            if not self.enum_values_ar_norm:
                return "ok"
            if candidate in self.enum_values_ar_norm:
                return "ok"
            # The registry lists bare forms (ابن, زوجة); the printed table says
            # الابن, الزوجة. Compare with the definite article stripped from
            # each word ON BOTH SIDES, here only. This must never move into the
            # Normalizer: folding ال globally would make الموصى له / موصى له
            # and similar anchor pairs indistinguishable across the registry.
            allowed = {_bare(v) for v in self.enum_values_ar_norm}
            return "ok" if _bare(candidate) in allowed else "mismatch"

        declared = registry.value_types.get(self.type)
        chain = (self.type,) + (declared.fallback if declared else ()) + self.fallback_types
        for type_name in chain:
            vt = registry.value_types.get(type_name)
            if vt is None or vt.compiled is None:
                return "ok"          # unconstrained type (text/long_text/name)
            probe = candidate.replace(" ", "") if type_name == "iban" else candidate
            if vt.compiled.search(probe) or vt.compiled.search(raw):
                return "ok"
        return "mismatch"


@dataclass(frozen=True)
class Anchor:
    text: str
    text_norm: str
    weight: int
    group: str | None = None
    voice: str = "any"
    not_preceded_by: tuple[str, ...] = ()
    not_preceded_by_norm: tuple[str, ...] = ()
    window: int = 40
    co_occurs_with: tuple[str, ...] = ()
    co_occurs_with_norm: tuple[str, ...] = ()
    within: int | None = None
    in_section: tuple[str, ...] = ()
    in_section_norm: tuple[str, ...] = ()
    note: str = ""
    neutralized_by: str | None = None


@dataclass(frozen=True)
class AnchorRegex:
    name: str
    pattern: str
    compiled: re.Pattern
    weight: int
    group: str | None = None
    voice: str = "any"
    note: str = ""


@dataclass(frozen=True)
class NegativeAnchor:
    text: str
    text_norm: str
    reason: str = ""


@dataclass(frozen=True)
class Family:
    """A stage-1 routing group. `evidence_phrases_norm` is what a classifier
    matches against normalised OCR text; `evidence_phrases` keeps the readable
    original for display and for error messages."""

    id: str
    name_ar: str
    members: tuple[str, ...]
    evidence: tuple[str, ...] = ()
    evidence_phrases: tuple[str, ...] = ()
    evidence_phrases_norm: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Template:
    id: str
    version: int
    name_ar: str
    name_en: str
    families: tuple[str, ...]
    description: str
    issuer_notes: str
    sections: tuple[str, ...]
    sections_norm: tuple[str, ...]
    anchors: tuple[Anchor, ...]
    anchor_regexes: tuple[AnchorRegex, ...]
    negative_anchors: tuple[NegativeAnchor, ...]
    required_fields: tuple[Field, ...]
    optional_fields: tuple[Field, ...]
    uncertainties: tuple[str, ...] = ()

    @property
    def family(self) -> str:
        return self.families[0]

    @property
    def fields(self) -> tuple[Field, ...]:
        return self.required_fields + self.optional_fields

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)

    @property
    def scoring_anchors(self) -> tuple[Anchor, ...]:
        """Anchors that may contribute to a template score (weight > 0)."""
        return tuple(a for a in self.anchors if a.weight > 0)

    @property
    def max_possible_score(self) -> int:
        """Sum of the best anchor per scoring group, for score normalisation."""
        best: dict[str, int] = {}
        for a in self.scoring_anchors:
            g = a.group or f"__{a.text_norm}"
            best[g] = max(best.get(g, 0), a.weight)
        for r in self.anchor_regexes:
            g = r.group or f"__re_{r.name}"
            best[g] = max(best.get(g, 0), r.weight)
        return sum(best.values())


@dataclass(frozen=True)
class Registry:
    path: str
    registry_id: str
    registry_version: int
    normalizer: Normalizer
    value_types: dict[str, ValueType]
    field_groups: dict
    families: dict[str, Family]
    templates: dict[str, Template]
    classification: dict
    disambiguation: tuple
    open_items: tuple
    citation_markers: tuple[str, ...] = ()
    citation_markers_norm: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def normalize(self, text: str) -> str:
        return self.normalizer(text)

    def get(self, template_id: str) -> Template:
        try:
            return self.templates[template_id]
        except KeyError:
            raise RegistryError(
                f"unknown template {template_id!r}; known: {sorted(self.templates)}"
            ) from None

    def resolve(self, template_id: str) -> tuple[tuple[Field, ...], tuple[Field, ...]]:
        """The input layer's payload: (required_fields, optional_fields)."""
        t = self.get(template_id)
        return t.required_fields, t.optional_fields

    @property
    def fallback_template(self) -> str:
        return self.classification.get("fallback_template", "generic_document")

    @property
    def corroboration_floor(self) -> float:
        """Minimum rules-tier score before agreeing with the model counts as
        corroboration. Below it the rules tier found next to nothing, and
        merely ranking first among ten near-zero scores is not evidence."""
        return float(self.classification.get("corroboration_min_score", 0.30))

    @property
    def review_threshold(self) -> float:
        return float(self.classification.get("review_threshold", 0.7))

    def __iter__(self) -> Iterator[Template]:
        return iter(self.templates.values())

    def __len__(self) -> int:
        return len(self.templates)


# =============================================================================
# Arabic label composition
# =============================================================================

_AL = "ال"   # ال


def _lam_form(possessor: str) -> str:
    """Prefix the possessive lam: الوارث -> للوارث, ولي الزوجة -> لولي الزوجة."""
    if possessor.startswith(_AL):
        return "ل" + possessor[1:]      # ل + (ال -> ل): alef elides
    return "ل" + possessor


def compose_idafa(base_label: str, possessor: str) -> str:
    """Attach a role possessor to a field label, grammatically.

    Two constructions, chosen by inspecting the base label:

      IDAFA EXTENSION, when the base is a genuine construct chain — a first noun
      with no definite article, at most two words. The second noun sheds its
      article and the possessor is appended:
          الاسم        + الوارث -> اسم الوارث
          رقم الهوية    + الوارث -> رقم هوية الوارث

      LAM PREFIX, for everything else. A base whose FIRST word carries ال is
      noun-plus-adjective, not a construct chain, and stripping the article off
      its last word would turn the adjective into a bare noun. Three-word bases
      end in an adjective for the same reason. Both take the preposition:
          الرقم الموحد       + الوارث -> الرقم الموحد للوارث
          رقم السجل التجاري  + الوارث -> رقم السجل التجاري للوارث

    Getting this wrong produces labels like "رقم السجل تجاري الوارث", which a
    label matcher will never find on a real document.
    """
    base, possessor = (base_label or "").strip(), (possessor or "").strip()
    if not base:
        return possessor
    if not possessor:
        return base

    words = base.split()
    if len(words) == 1:
        head = words[0][2:] if words[0].startswith(_AL) else words[0]
        return f"{head} {possessor}"
    if len(words) == 2 and not words[0].startswith(_AL) and words[1].startswith(_AL):
        return f"{words[0]} {words[1][2:]} {possessor}"
    return f"{base} {_lam_form(possessor)}"


# =============================================================================
# Loading
# =============================================================================


def _as_tuple(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _req(raw: dict, key: str, where: str) -> str:
    """Fetch a mandatory string key, naming the offending entry if it is absent.

    The registry is hand-edited by domain people, so a missing key must produce
    a message that says which template and which entry, never a bare KeyError
    from somewhere deep in the expansion.
    """
    value = (raw or {}).get(key)
    if value is None or not str(value).strip():
        raise RegistryError(f"{where}: entry is missing required key {key!r} ({raw!r})")
    return str(value)


def _int(raw: dict, key: str, default: int, where: str) -> int:
    """Fetch an integer key, naming the offending entry if it is not numeric."""
    value = (raw or {}).get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RegistryError(
            f"{where}: {key!r} must be an integer, got {value!r}"
        ) from None


def _build_value_types(raw: dict) -> dict[str, ValueType]:
    out: dict[str, ValueType] = {}
    for name, spec in (raw or {}).items():
        spec = spec or {}
        pattern = spec.get("pattern")
        compiled = None
        if pattern:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise RegistryError(f"value_types.{name}: invalid regex: {exc}") from None
        out[name] = ValueType(
            name=name,
            pattern=pattern,
            compiled=compiled,
            example=str(spec.get("example", "")),
            note=str(spec.get("note", "")),
            status=str(spec.get("status", "active")),
            fallback=tuple(str(f) for f in _as_tuple(spec.get("fallback"))),
        )
    if not out:
        raise RegistryError("registry declares no value_types")
    return out


def _build_anchors(
    raw_anchors, norm, neutral_phrases, template_id, warnings: list[str]
) -> tuple[Anchor, ...]:
    anchors = []
    for a in _as_tuple(raw_anchors):
        text = _req(a, "text", f"{template_id} anchor").strip()
        text_norm = norm(text)
        if "weight" not in (a or {}):
            # An explicit `weight: 0` is a documented pattern in the registry —
            # it pins a string that must never be scored. An OMITTED weight is
            # almost always an editing slip, and silently never scores.
            warnings.append(
                f"{template_id}: anchor {text!r} has no weight and will never score"
            )
        weight = _int(a, "weight", 0, f"{template_id} anchor {text!r}")
        neutralized = None
        if text_norm in neutral_phrases and weight > 0:
            neutralized = "classification.neutral_evidence.phrases"
            weight = 0
        anchors.append(
            Anchor(
                text=text,
                text_norm=text_norm,
                weight=weight,
                group=a.get("group"),
                voice=str(a.get("voice", "any")),
                not_preceded_by=_as_tuple(a.get("not_preceded_by")),
                not_preceded_by_norm=tuple(norm(x) for x in _as_tuple(a.get("not_preceded_by"))),
                window=_int(a, "window", 40, f"{template_id} anchor {text!r}"),
                co_occurs_with=_as_tuple(a.get("co_occurs_with")),
                co_occurs_with_norm=tuple(norm(x) for x in _as_tuple(a.get("co_occurs_with"))),
                within=(
                    None if a.get("within") is None
                    else _int(a, "within", 0, f"{template_id} anchor {text!r}")
                ),
                in_section=_as_tuple(a.get("in_section")),
                in_section_norm=tuple(
                    norm(x) for x in _as_tuple(a.get("in_section"))
                ),
                note=str(a.get("note", "")),
                neutralized_by=neutralized,
            )
        )
    return tuple(anchors)


def _resolve_group_field(
    raw: dict,
    *,
    norm,
    group_name: str,
    group_repeatable: bool,
    required: bool,
    section: str,
    role: str | None,
    prefix: str | None,
    role_label: str | None,
    fallback_types: tuple[str, ...],
) -> Field:
    where = f"field_groups.{group_name}"
    base_key = _req(raw, "key", where)
    key = base_key if prefix is None else f"{prefix}_{base_key}"
    base_label = str(raw.get("label_ar", ""))
    base_aliases = tuple(str(a) for a in _as_tuple(raw.get("aliases_ar")))

    if role_label:
        # Only the LABEL is composed with the role. Aliases are arbitrary
        # phrases — adjectival ("رقم الهوية الوطنية"), prepositional ("بموجب
        # الهوية رقم") — and composing them produces strings no document
        # prints. They stay generic and are matched scoped by section instead.
        label_ar = compose_idafa(base_label, role_label)
        specific = (label_ar,)
        shared = (base_label,) + base_aliases
    else:
        label_ar = base_label
        specific = base_aliases
        shared = ()

    enum_values = tuple(str(v) for v in _as_tuple(raw.get("enum_values_ar")))
    return Field(
        key=key,
        label_ar=label_ar,
        label_ar_norm=norm(label_ar),
        label_en=str(raw.get("label_en", "")),
        type=_req(raw, "type", f"{where} field {base_key!r}"),
        section=section,
        section_norm=norm(section),
        required=required,
        repeatable=bool(raw.get("repeatable", group_repeatable)),
        aliases_ar=specific,
        aliases_ar_norm=tuple(norm(a) for a in specific),
        shared_aliases_ar=shared,
        shared_aliases_ar_norm=tuple(norm(a) for a in shared),
        enum_values_ar=enum_values,
        enum_values_ar_norm=tuple(norm(v) for v in enum_values),
        role=role,
        role_label_ar=str(role_label or ""),
        source=group_name,
        note=str(raw.get("note", "")),
        classification_weight=raw.get("classification_weight"),
        fallback_types=(
            fallback_types if str(raw.get("type", "")) == "deed_number" else ()
        ),
    )


def _resolve_local_field(raw: dict, *, norm, template_id: str) -> Field:
    where = f"{template_id} fields"
    key = _req(raw, "key", where)
    aliases = tuple(str(a) for a in _as_tuple(raw.get("aliases_ar")))
    enum_values = tuple(str(v) for v in _as_tuple(raw.get("enum_values_ar")))
    return Field(
        key=key,
        label_ar=str(raw.get("label_ar", "")),
        label_ar_norm=norm(str(raw.get("label_ar", ""))),
        label_en=str(raw.get("label_en", "")),
        type=_req(raw, "type", f"{where} field {key!r}"),
        section=str(raw.get("section", "")),
        section_norm=norm(str(raw.get("section", ""))),
        required=bool(raw.get("required", False)),
        repeatable=bool(raw.get("repeatable", False)),
        aliases_ar=aliases,
        aliases_ar_norm=tuple(norm(a) for a in aliases),
        enum_values_ar=enum_values,
        enum_values_ar_norm=tuple(norm(v) for v in enum_values),
        source="local",
        note=str(raw.get("note", "")),
        classification_weight=raw.get("classification_weight"),
    )


def _expand_template_fields(
    tpl: dict, groups: dict, norm, warnings: list[str]
) -> tuple[list[Field], list[Field]]:
    tid = tpl["id"]
    resolved: list[Field] = []
    seen: dict[str, str] = {}

    # A template that carries the legacy paper-register block may also carry a
    # legacy 1-5 digit serial, which the electronic `deed_number` pattern
    # rejects. Closes open_items.legacy_deed_number_fallback.
    uses_legacy = any((g or {}).get("use") == "legacy_register" for g in _as_tuple(tpl.get("groups")))
    fallback = ("legacy_deed_number",) if uses_legacy else ()

    def claim(f: Field, origin: str) -> None:
        if f.key in seen:
            raise RegistryError(
                f"{tid}: duplicate field key {f.key!r} from {seen[f.key]} and {origin}"
            )
        seen[f.key] = origin
        resolved.append(f)

    for g in _as_tuple(tpl.get("groups")):
        g = g or {}
        if not isinstance(g, dict):
            raise RegistryError(
                f"{tid}: each entry under `groups:` must be a mapping, got {g!r}"
            )
        name = _req(g, "use", f"{tid} groups")
        if name not in groups:
            raise RegistryError(f"{tid}: references unknown field group {name!r}")
        spec = groups[name]
        if not isinstance(spec, dict):
            raise RegistryError(
                f"field_groups.{name}: group body must be a mapping, got {spec!r}"
            )
        group_fields = _as_tuple(spec.get("fields"))
        group_repeatable = bool(spec.get("repeatable", False))
        keys = {
            _req(f, "key", f"field_groups.{name}") for f in group_fields
        }
        instances = _as_tuple(g.get("instances"))

        # Only these keys are actually honoured. `require`/`section` are read
        # on the non-instantiated path and `instances` on the other; anything
        # else — a `require:` written alongside `instances:`, a stray
        # `repeatable:`, a typo — used to be silently dropped, quietly
        # downgrading required fields to optional with no error.
        allowed = {"use", "instances"} if instances else {"use", "require", "section"}
        extra = set(g) - allowed
        if extra:
            raise RegistryError(
                f"{tid}/{name}: unrecognised key(s) {sorted(extra)} on a group "
                f"reference; allowed here: {sorted(allowed)}"
            )

        if instances and not spec.get("instantiable"):
            raise RegistryError(
                f"{tid}/{name}: declares instances but the group is not instantiable"
            )

        if instances:
            for inst in instances:
                if not isinstance(inst, dict):
                    raise RegistryError(
                        f"{tid}/{name}: each instance must be a mapping, got {inst!r}"
                    )
                require = set(_as_tuple(inst.get("require")))
                unknown = require - keys
                if unknown:
                    raise RegistryError(
                        f"{tid}/{name}[{inst.get('role')}]: require names unknown "
                        f"field(s) {sorted(unknown)}"
                    )
                prefix = inst.get("prefix")
                if not prefix:
                    raise RegistryError(f"{tid}/{name}: instance without a prefix")
                for raw in group_fields:
                    claim(
                        _resolve_group_field(
                            raw,
                            norm=norm,
                            group_name=name,
                            group_repeatable=bool(inst.get("repeatable", group_repeatable)),
                            required=raw.get("key") in require,
                            section=str(inst.get("section", spec.get("section", ""))),
                            role=inst.get("role"),
                            prefix=prefix,
                            role_label=inst.get("label_ar"),
                            fallback_types=fallback,
                        ),
                        f"{name}[{inst.get('role')}]",
                    )
        else:
            require = set(_as_tuple(g.get("require")))
            unknown = require - keys
            if unknown:
                raise RegistryError(
                    f"{tid}/{name}: require names unknown field(s) {sorted(unknown)}"
                )
            for raw in group_fields:
                claim(
                    _resolve_group_field(
                        raw,
                        norm=norm,
                        group_name=name,
                        group_repeatable=group_repeatable,
                        required=raw.get("key") in require,
                        section=str(g.get("section", spec.get("section", ""))),
                        role=None,
                        prefix=None,
                        role_label=None,
                        fallback_types=fallback,
                    ),
                    name,
                )

    for raw in _as_tuple(tpl.get("fields")):
        claim(_resolve_local_field(raw, norm=norm, template_id=tid), "local")

    known_sections = {norm(s) for s in _as_tuple(tpl.get("sections"))}

    # An in_section gate naming a section the template does not declare can
    # never fire, silently narrowing or killing the anchor it was meant to
    # scope. Both of the registry's weight-3 section-gated anchors had one.
    for a in _as_tuple(tpl.get("anchors")):
        for sec in _as_tuple((a or {}).get("in_section")):
            if norm(str(sec)) not in known_sections:
                raise RegistryError(
                    f"{tid}: anchor {a.get('text')!r} is gated to section "
                    f"{sec!r}, which is not in the template's sections list"
                )

    for f in resolved:
        if f.section and norm(f.section) not in known_sections:
            warnings.append(
                f"{tid}: field {f.key!r} names section {f.section!r}, "
                "which is not in the template's sections list"
            )

    return [f for f in resolved if f.required], [f for f in resolved if not f.required]


def _is_gated(a: Anchor) -> bool:
    """True if an anchor carries a gating construct that scopes when it fires.

    A bare string that also names a shared field label is evidence of nothing.
    The same string restricted to the instrument's own header section, or to the
    operative voice, or required to co-occur with a companion phrase, is a
    legitimate discriminator. Only the ungated case is worth warning about.
    """
    return bool(
        a.in_section
        or a.not_preceded_by
        or a.co_occurs_with
        or a.voice == "operative"
    )


def _validate_types(templates: dict[str, Template], value_types: dict) -> None:
    for t in templates.values():
        for f in t.fields:
            if f.type not in value_types:
                raise RegistryError(
                    f"{t.id}: field {f.key!r} declares type {f.type!r}, which is not "
                    f"in the closed value_types vocabulary"
                )
            for fb in f.fallback_types + value_types[f.type].fallback:
                if fb not in value_types:
                    raise RegistryError(
                        f"{t.id}: field {f.key!r} declares fallback type {fb!r}, "
                        "which is not in value_types"
                    )
            if f.type == "enum" and not f.enum_values_ar:
                raise RegistryError(
                    f"{t.id}: field {f.key!r} is an enum with no enum_values_ar"
                )


def load_registry(path: str | None = None) -> Registry:
    """Parse, normalise, validate and resolve the registry at `path`."""
    path = path or DEFAULT_REGISTRY
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise RegistryError(f"registry not found: {path}") from None
    except yaml.YAMLError as exc:
        raise RegistryError(f"registry is not valid YAML: {exc}") from None

    if not isinstance(raw, dict):
        raise RegistryError(f"registry root must be a mapping, got {type(raw).__name__}")

    norm_cfg = raw.get("normalization") or {}
    normalizer = Normalizer.from_config(norm_cfg)
    if not norm_cfg.get("apply_to_registry_at_load", True):
        raise RegistryError(
            "normalization.apply_to_registry_at_load is false. The registry stores "
            "readable Arabic; without load-time normalisation every anchor is a "
            "dead string that can never match normalised OCR text."
        )

    warnings: list[str] = []
    value_types = _build_value_types(raw.get("value_types"))
    groups = raw.get("field_groups") or {}
    classification = raw.get("classification") or {}

    neutral = classification.get("neutral_evidence") or {}
    neutral_phrases = {
        normalizer(str(p.get("text", ""))) for p in _as_tuple(neutral.get("phrases"))
    }
    neutral_phrases.discard("")

    families_raw = _as_tuple(raw.get("families"))
    families: dict[str, Family] = {}
    for f in families_raw:
        if not isinstance(f, dict):
            raise RegistryError(f"families: entry must be a mapping, got {f!r}")
        fid = _req(f, "id", "families")
        if fid in families:
            raise RegistryError(f"duplicate family id {fid!r}")
        phrases = tuple(str(p) for p in _as_tuple(f.get("evidence_phrases")))
        families[fid] = Family(
            id=fid,
            name_ar=str(f.get("name_ar", "")),
            members=tuple(str(m) for m in _as_tuple(f.get("members"))),
            evidence=tuple(str(e) for e in _as_tuple(f.get("evidence"))),
            evidence_phrases=phrases,
            evidence_phrases_norm=tuple(normalizer(p) for p in phrases),
            note=str(f.get("note", "")),
        )

    # Stage-1 family routing matches these against normalised OCR text, so the
    # shared citation vocabulary needs the same normalised twin that each
    # anchor's own not_preceded_by already gets.
    gating = (classification.get("gating") or {}).get("not_preceded_by") or {}
    citation_markers = tuple(
        str(m) for m in _as_tuple(gating.get("common_citation_markers"))
    )

    templates: dict[str, Template] = {}
    for tpl in _as_tuple(raw.get("templates")):
        if not isinstance(tpl, dict):
            raise RegistryError(
                f"templates: each entry must be a mapping, got {tpl!r}"
            )
        tid = tpl.get("id")
        if not tid:
            raise RegistryError("a template has no id")
        if tid in templates:
            raise RegistryError(f"duplicate template id {tid!r}")

        fams = tuple(str(f) for f in _as_tuple(tpl.get("family")))
        if not fams:
            raise RegistryError(f"{tid}: declares no family")
        for fam in fams:
            if fam not in families:
                raise RegistryError(f"{tid}: unknown family {fam!r}")

        regexes = []
        for r in _as_tuple(tpl.get("anchor_regexes")):
            _req(r, "name", f"{tid} anchor_regex")
            _req(r, "pattern", f"{tid} anchor_regex {r.get('name')!r}")
            # The pattern is compiled as written and run against NORMALISED
            # page text, so its Arabic literals must already be in normalised
            # form. Normalising the pattern source instead would corrupt it:
            # stripping tashkeel from `ملكاً?` rebinds the `?` to the alef, and
            # whitespace collapsing rewrites literal spacing and character
            # classes. So validate, and make the author write it correctly.
            if normalizer(r["pattern"]) != r["pattern"]:
                raise RegistryError(
                    f"{tid}/{r['name']}: anchor regex is not written in "
                    f"normalised form and so can never match normalised page "
                    f"text. Expected: {normalizer(r['pattern'])!r}"
                )
            try:
                compiled = re.compile(r["pattern"])
            except re.error as exc:
                raise RegistryError(
                    f"{tid}/{r.get('name')}: invalid anchor regex: {exc}"
                ) from None
            regexes.append(
                AnchorRegex(
                    name=str(r.get("name", "")),
                    pattern=r["pattern"],
                    compiled=compiled,
                    weight=_int(r, "weight", 0, f"{tid} anchor_regex {r.get('name')!r}"),
                    group=r.get("group"),
                    voice=str(r.get("voice", "any")),
                    note=str(r.get("note", "")),
                )
            )

        required, optional = _expand_template_fields(tpl, groups, normalizer, warnings)

        templates[tid] = Template(
            id=tid,
            version=_int(tpl, "version", 1, f"{tid}"),
            name_ar=str(tpl.get("name_ar", "")),
            name_en=str(tpl.get("name_en", "")),
            families=fams,
            description=str(tpl.get("description", "")).strip(),
            issuer_notes=str(tpl.get("issuer_notes", "")).strip(),
            sections=tuple(str(s) for s in _as_tuple(tpl.get("sections"))),
            sections_norm=tuple(
                normalizer(str(s)) for s in _as_tuple(tpl.get("sections"))
            ),
            anchors=_build_anchors(
                tpl.get("anchors"), normalizer, neutral_phrases, tid, warnings
            ),
            anchor_regexes=tuple(regexes),
            negative_anchors=tuple(
                NegativeAnchor(
                    text=_req(n, "text", f"{tid} negative_anchor"),
                    text_norm=normalizer(_req(n, "text", f"{tid} negative_anchor")),
                    reason=str(n.get("reason", "")),
                )
                for n in _as_tuple(tpl.get("negative_anchors"))
            ),
            required_fields=tuple(required),
            optional_fields=tuple(optional),
            uncertainties=tuple(str(u) for u in _as_tuple(tpl.get("uncertainties"))),
        )

    if not templates:
        raise RegistryError("registry declares no templates")

    _validate_types(templates, value_types)

    for fam in families.values():
        for member in fam.members:
            if member not in templates:
                raise RegistryError(
                    f"family {fam.id}: lists unknown member template {member!r}"
                )

    # A weighted anchor shared by two templates is the failure mode the registry
    # audit called its largest: it hands one template a head start on the
    # other's documents. Surfaced, not fatal, because a few are intentional.
    weighted: dict[str, list[str]] = {}
    for t in templates.values():
        for a in t.scoring_anchors:
            weighted.setdefault(a.text_norm, []).append(t.id)
    for text_norm, ids in weighted.items():
        if len(ids) > 1:
            warnings.append(f"anchor {text_norm!r} carries weight in {ids}")

    # An anchor that merely restates a neutral block's label is evidence of
    # nothing, and scoring it re-creates the property-block collision.
    neutral_labels: set[str] = set()
    for block in _as_tuple(neutral.get("blocks")):
        spec = groups.get(block.get("group")) or {}
        for f in _as_tuple(spec.get("fields")):
            neutral_labels.add(normalizer(str(f.get("label_ar", ""))))
            neutral_labels.update(normalizer(str(a)) for a in _as_tuple(f.get("aliases_ar")))
    neutral_labels.discard("")
    for t in templates.values():
        for a in t.scoring_anchors:
            if a.text_norm in neutral_labels and not _is_gated(a):
                warnings.append(
                    f"{t.id}: UNGATED anchor {a.text!r} (weight {a.weight}) restates a "
                    "label from a neutral_evidence block"
                )

    return Registry(
        path=path,
        registry_id=str(raw.get("registry_id", "")),
        registry_version=_int(raw, "registry_version", 1, "registry"),
        normalizer=normalizer,
        value_types=value_types,
        field_groups=groups,
        families=families,
        templates=templates,
        classification=classification,
        citation_markers=citation_markers,
        citation_markers_norm=tuple(normalizer(m) for m in citation_markers),
        disambiguation=_as_tuple(raw.get("disambiguation")),
        open_items=_as_tuple(raw.get("open_items")),
        warnings=tuple(warnings),
    )


@lru_cache(maxsize=4)
def get_registry(path: str | None = None) -> Registry:
    """Process-wide cached registry. Use this from request handlers."""
    return load_registry(path)


# =============================================================================
# CLI
# =============================================================================


def _dump(template_id: str | None) -> int:
    try:
        reg = load_registry()
    except RegistryError as exc:
        print(f"registry error: {exc}", file=sys.stderr)
        return 1

    print(f"{reg.registry_id} v{reg.registry_version}  ({reg.path})")
    print(f"{len(reg)} templates, {len(reg.value_types)} value types, "
          f"{len(reg.field_groups)} field groups")

    if reg.warnings:
        print(f"\n{len(reg.warnings)} warning(s):")
        for w in reg.warnings:
            print(f"  ! {w}")

    if template_id is None:
        print(f"\n{'template':24} {'family':18} {'req':>4} {'opt':>4} {'anchors':>8} {'max':>5}")
        print("-" * 68)
        for t in reg:
            print(f"{t.id:24} {'+'.join(t.families):18} "
                  f"{len(t.required_fields):>4} {len(t.optional_fields):>4} "
                  f"{len(t.scoring_anchors):>8} {t.max_possible_score:>5}")
        return 0

    try:
        t = reg.get(template_id)
    except RegistryError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(f"\n=== {t.id}  —  {t.name_ar}  ({t.name_en})")
    print(f"family: {'+'.join(t.families)}   version: {t.version}")
    print(f"max possible anchor score: {t.max_possible_score}")

    for title, fields in (("REQUIRED", t.required_fields), ("OPTIONAL", t.optional_fields)):
        print(f"\n--- {title} ({len(fields)}) ---")
        for f in fields:
            rep = " [repeatable]" if f.repeatable else ""
            role = f" <{f.role}>" if f.role else ""
            print(f"  {f.key:34} {f.type:22} {f.label_ar}{role}{rep}")
            if f.aliases_ar:
                print(f"    {'aliases:':<32} {', '.join(f.aliases_ar[:4])}")
    return 0


if __name__ == "__main__":
    sys.exit(_dump(sys.argv[1] if len(sys.argv) > 1 else None))
