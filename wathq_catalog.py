"""The Wathq catalog: every product and endpoint the verify-data tab offers.

The data lives in templates/wathq/catalog.yaml — one entry per endpoint with
its Arabic label, price, inputs and the response paths that hold personal
data. This module loads it strictly (anything malformed is a CatalogError),
cross-checks every endpoint against Wathq's own spec in
templates/wathq/<product>.yaml (the path exists; every path parameter is an
input; every input and fixed parameter is one the spec declares), and turns a
user's form into a validated request. wathq_api turns a CatalogError into a
message in the tab; the rest of the app is unaffected.

Nothing here touches the network; wathq_client.py does that.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import yaml

from wathq_view import load_spec, spec_labels

CATALOG_FILE = Path(__file__).with_name("templates") / "wathq" / "catalog.yaml"

_FOLD = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SEPARATORS = re.compile(r"[\s\-_.‎‏‪-‮⁦-⁩؜]+")
_BIDI = re.compile(r"[‎‏‪-‮⁦-⁩؜]")

UNIFIED = re.compile(r"70[0-9]{8}\Z")
LEGACY_CR = re.compile(r"[1-6][0-9]{9}\Z")

# What each kind of input accepts once Arabic digits are folded and
# separators dropped. A catalog `pattern` narrows these further.
KINDS = {
    "unified_number": r"70[0-9]{8}",
    "company_number": r"70[0-9]{8}|[1-6][0-9]{9}",       # an old CR is converted first
    "cr_number_any": r"[1-7][0-9]{9}",                   # Wathq takes either as-is
    "person_or_entity_id": r"[A-Za-z0-9]{3,20}",
    "id_type": r"[A-Za-z_]{2,40}",
    "deed_number": r"[0-9]{1,20}",
    "attorney_code": r"[0-9A-Za-z]{1,30}",
    "drug_id": r"[0-9A-Za-z]{1,40}",
    "investor_id": r"[0-9A-Za-z]{1,40}",
    "permission_id": r"[0-9]{1,6}",
    "copy_number": r"[0-9]{1,5}",
    "nationality": r"[0-9A-Za-z]{1,10}",
    "boolean": r"true|false",
    "other": r"[0-9A-Za-z_\-]{1,60}",
}
# Typing artefacts (spaces, dashes, dots, bidi marks) are dropped from these;
# free-form kinds keep their spaces.
_STRIP_SEPARATORS = {"unified_number", "company_number", "cr_number_any", "deed_number",
                     "permission_id", "copy_number", "person_or_entity_id", "attorney_code",
                     "drug_id", "investor_id", "nationality"}

MSG_REQUIRED = "الحقل «{}» مطلوب."
MSG_REQUIRED_FOR = "الحقل «{}» مطلوب عند اختيار «{}»."
MSG_INVALID = "قيمة «{}» غير صالحة."
MSG_INVALID_FOR = "قيمة «{}» غير صالحة لنوع «{}»."
MSG_CHOICE = "اختر قيمة صحيحة لـ «{}»."
MSG_DIGITS = "«{}» يتكوّن من ١٠ أرقام."
MSG_UNIFIED = "«{}» هو الرقم الوطني الموحد: ١٠ أرقام تبدأ بـ ٧٠."
MSG_COMPANY = "«{}»: أدخل الرقم الوطني الموحد (يبدأ بـ ٧٠) أو رقم السجل التجاري القديم."
MSG_BAD_REQUEST = "بيانات الطلب غير صالحة."

# Cache keys hold a keyed hash of a person's ID, never the ID itself; the key
# lives only as long as the process.
_CACHE_SECRET = secrets.token_bytes(32)


class CatalogError(ValueError):
    """The catalog file is malformed or disagrees with Wathq's spec."""


@dataclass(frozen=True)
class Input:
    name: str                       # the parameter name exactly as Wathq spells it
    location: str                   # path | query | header
    kind: str
    label: str
    required: bool = True
    personal: bool = False          # identifies a natural person: never echoed or logged
    pattern: str = ""
    choices: tuple = ()             # ((value, label), …)
    sandbox_choices: tuple = ()     # the subset of choice values the sandbox accepts
    hint: str = ""
    legacy_ok: bool = False         # company_number: Wathq also finds struck-off records by the old CR
    required_if: tuple = ()         # ((other input, (values…)), …): required when the other has one of them
    pattern_by: tuple = ()          # ((other input, ((value, regex), …)), …): stricter pattern per value

    def clean(self, raw, env: str = "production") -> str:
        """The value to send, or ValueError with an Arabic message.
        `required` is checked by the caller (it may depend on other inputs)."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return ""
        if isinstance(raw, bool):
            raw = "true" if raw else "false"
        if not isinstance(raw, (str, int)):
            raise ValueError(MSG_INVALID.format(self.label))
        text = unicodedata.normalize("NFKC", str(raw)).translate(_FOLD)
        text = _SEPARATORS.sub("", text) if self.kind in _STRIP_SEPARATORS else _BIDI.sub("", text).strip()
        if self.choices:
            allowed = {v for v, _ in self.choices_for(env)}
            if text not in allowed:
                raise ValueError(MSG_CHOICE.format(self.label))
            return text
        if self.kind == "boolean":
            text = text.lower()
        if not re.fullmatch(KINDS.get(self.kind, KINDS["other"]), text):
            if self.kind == "unified_number":
                raise ValueError(MSG_UNIFIED.format(self.label))
            if self.kind == "company_number":
                raise ValueError(MSG_COMPANY.format(self.label))
            if self.kind == "cr_number_any":
                raise ValueError(MSG_DIGITS.format(self.label))
            raise ValueError(MSG_INVALID.format(self.label))
        if self.pattern and not re.fullmatch(self.pattern, text):
            raise ValueError(MSG_INVALID.format(self.label))
        return text

    def choices_for(self, env: str) -> tuple:
        if env == "sandbox" and self.sandbox_choices:
            return tuple(c for c in self.choices if c[0] in self.sandbox_choices)
        return self.choices


@dataclass(frozen=True)
class Endpoint:
    id: str                         # "<product>.<endpoint>", e.g. "cr.fullinfo"
    product: str
    path: str                       # exactly as in the spec, e.g. /fullinfo/{id}
    label: str
    description: str
    price: float                    # SAR, prepaid; -1 when Wathq doesn't list it
    lookup: bool
    in_sandbox: bool
    language: bool                  # sends ?language=ar|en
    inputs: tuple
    personal: frozenset             # response paths to mask
    overrides: dict = field(default_factory=dict, hash=False, compare=False)
    view: str = "generic"           # "generic" | "contract" (the bespoke contract card)
    one_of: tuple = ()              # input names of which at least one is required
    redact: frozenset = frozenset() # free-text response paths: digit runs are masked
    fixed: tuple = ()               # ((query name, value), …) always sent; never user input

    @property
    def needs_conversion_input(self) -> Input | None:
        return next((i for i in self.inputs if i.kind == "company_number"), None)

    def labels(self) -> dict:
        return spec_labels(self.product, self.path)


@dataclass(frozen=True)
class Product:
    id: str
    label: str
    label_en: str
    base: str                       # e.g. /commercial-registration
    sandbox_base: str               # "" when Wathq has no sandbox for it
    endpoints: tuple


@dataclass(frozen=True)
class Request:
    """A validated call: what to send, and what may be shown or cached."""
    endpoint: Endpoint
    values: dict                    # input name -> cleaned value
    language: str                   # "" when the endpoint has no language parameter

    def path(self, unified: str | None = None) -> str:
        values = dict(self.values)
        conv = self.endpoint.needs_conversion_input
        if conv is not None and unified:
            values[conv.name] = unified
        path = self.endpoint.path
        for inp in self.endpoint.inputs:
            if inp.location == "path":
                path = path.replace("{" + inp.name + "}", quote(values.get(inp.name, ""), safe=""))
        return path

    def query(self) -> dict:
        q = {i.name: self.values[i.name] for i in self.endpoint.inputs
             if i.location == "query" and self.values.get(i.name)}
        q.update(dict(self.endpoint.fixed))
        if self.endpoint.language and self.language:
            q["language"] = self.language
        return q

    def headers(self) -> dict:
        return {i.name: self.values[i.name] for i in self.endpoint.inputs
                if i.location == "header" and self.values.get(i.name)}

    def cache_key(self, env: str) -> tuple:
        """What identifies this answer in the in-memory cache. A person's ID
        appears only as a keyed hash (the key dies with the process)."""
        personal = {i.name for i in self.endpoint.inputs if i.personal}
        items = []
        for name, value in sorted(self.values.items()):
            if name in personal:
                value = hmac.new(_CACHE_SECRET, value.encode("utf-8"), hashlib.sha256).hexdigest()
            items.append((name, value))
        return (env, self.endpoint.id, tuple(items), self.language)

    def public_inputs(self) -> dict:
        """The inputs that may be echoed back to the page: never a person's ID."""
        return {i.name: self.values[i.name] for i in self.endpoint.inputs
                if not i.personal and self.values.get(i.name)}

    def conversion_value(self) -> str | None:
        """An old CR number typed where the unified number is needed, else None."""
        conv = self.endpoint.needs_conversion_input
        if conv is None:
            return None
        value = self.values.get(conv.name, "")
        return value if LEGACY_CR.fullmatch(value) else None

    @property
    def legacy_ok(self) -> bool:
        conv = self.endpoint.needs_conversion_input
        return bool(conv and conv.legacy_ok)


class Catalog:
    def __init__(self, products: dict):
        self.products = products
        self.endpoints = {e.id: e for p in products.values() for e in p.endpoints}

    def product(self, endpoint: Endpoint) -> Product:
        return self.products[endpoint.product]

    def base(self, endpoint: Endpoint, env: str) -> str:
        product = self.products[endpoint.product]
        return product.base if env == "production" else product.sandbox_base

    def available(self, endpoint: Endpoint, env: str) -> bool:
        if env == "production":
            return True
        return bool(self.products[endpoint.product].sandbox_base) and endpoint.in_sandbox

    def request(self, endpoint_id, inputs, language="ar", env: str = "production") -> Request:
        """A validated Request from the page's form, or ValueError (Arabic)."""
        endpoint = self.endpoints.get(endpoint_id) if isinstance(endpoint_id, str) else None
        if endpoint is None:
            raise ValueError("خدمة غير معروفة.")
        if not isinstance(inputs, dict):
            raise ValueError(MSG_BAD_REQUEST)
        if set(inputs) - {i.name for i in endpoint.inputs}:
            raise ValueError(MSG_BAD_REQUEST)
        if language not in ("ar", "en"):
            raise ValueError(MSG_BAD_REQUEST)
        values = {}
        for inp in endpoint.inputs:
            value = inp.clean(inputs.get(inp.name), env)
            if value:
                values[inp.name] = value
        labels = {i.name: i.label for i in endpoint.inputs}
        for inp in endpoint.inputs:
            if values.get(inp.name):
                for other, by_value in inp.pattern_by:
                    regex = dict(by_value).get(values.get(other, ""))
                    if regex and not re.fullmatch(regex, values[inp.name]):
                        choice = dict(next(i for i in endpoint.inputs if i.name == other).choices)
                        raise ValueError(MSG_INVALID_FOR.format(inp.label, choice.get(values[other], values[other])))
                continue
            if inp.required:
                raise ValueError(MSG_REQUIRED.format(inp.label))
            for other, triggers in inp.required_if:
                if values.get(other) in triggers:
                    choice = dict(next(i for i in endpoint.inputs if i.name == other).choices)
                    raise ValueError(MSG_REQUIRED_FOR.format(inp.label, choice.get(values[other], values[other])))
        if endpoint.one_of and not any(values.get(n) for n in endpoint.one_of):
            names = "، ".join(f"«{labels[n]}»" for n in endpoint.one_of)
            raise ValueError("أدخل واحدًا على الأقل من: " + names + ".")
        # The language only matters where Wathq takes it: elsewhere it must not
        # split the cache (a hidden select would otherwise re-bill a repeat).
        return Request(endpoint, values, language if endpoint.language else "")

    def public(self, env: str) -> dict:
        """The catalog as the page needs it: no spec internals, no PII lists."""
        return {"env": env, "products": [{
            "id": p.id, "label": p.label, "label_en": p.label_en,
            "sandbox": bool(p.sandbox_base),
            "endpoints": [{
                "id": e.id, "label": e.label, "description": e.description,
                "price": e.price, "lookup": e.lookup, "view": e.view,
                "available": self.available(e, env), "language": e.language,
                "converts": e.needs_conversion_input is not None,
                "legacy_ok": bool(e.needs_conversion_input and e.needs_conversion_input.legacy_ok),
                "one_of": list(e.one_of),
                "inputs": [{"name": i.name, "kind": i.kind, "label": i.label,
                            "required": i.required, "personal": i.personal,
                            "choices": [{"value": v, "label": l} for v, l in i.choices_for(env)],
                            "required_if": {o: list(t) for o, t in i.required_if},
                            "pattern_by": {o: dict(m) for o, m in i.pattern_by},
                            "hint": i.hint} for i in e.inputs],
            } for e in p.endpoints],
        } for p in self.products.values()]}


# ---------------------------------------------------------------- loading

def _str(d: dict, key: str, where: str, required: bool = True) -> str:
    value = d.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise CatalogError(f"{where}: '{key}' must be a non-empty string")
    return value.strip()


def _list(d: dict, key: str, where: str) -> list:
    value = d.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise CatalogError(f"{where}: '{key}' must be a list")
    return value


def _map(d: dict, key: str, where: str) -> dict:
    value = d.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CatalogError(f"{where}: '{key}' must be a mapping")
    return value


def _spec_params(spec: dict, path: str) -> dict:
    """{name: location} of the parameters the spec declares for GET path."""
    item = (spec.get("paths") or {}).get(path) or {}
    params = list(item.get("parameters") or []) + list((item.get("get") or {}).get("parameters") or [])
    out = {}
    for p in params:
        if isinstance(p, dict) and "$ref" in p:
            node = spec
            for part in str(p["$ref"]).lstrip("#/").split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            p = node
        if isinstance(p, dict) and p.get("name"):
            out[p["name"]] = p.get("in", "")
    return out


def _regex(pattern: str, where: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise CatalogError(f"{where}: bad pattern ({exc})") from None
    return pattern


def _load_input(raw, where: str) -> Input:
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: an input must be a mapping")
    kind = _str(raw, "kind", where)
    if kind not in KINDS:
        raise CatalogError(f"{where}: unknown input kind {kind!r}")
    location = _str(raw, "location", where)
    if location not in ("path", "query", "header"):
        raise CatalogError(f"{where}: location must be path, query or header")
    pattern = _str(raw, "pattern", where, required=False)
    if pattern:
        _regex(pattern, where)
    choices = []
    for c in _list(raw, "choices", where):
        if not isinstance(c, dict) or "value" not in c:
            raise CatalogError(f"{where}: a choice needs a value")
        choices.append((str(c["value"]), str(c.get("label") or c["value"])))
    sandbox_choices = tuple(str(v) for v in _list(raw, "sandbox_choices", where))
    if not set(sandbox_choices) <= {v for v, _ in choices}:
        raise CatalogError(f"{where}: sandbox_choices must be among its choices")
    required_if = tuple((str(k), tuple(str(v) for v in (vals if isinstance(vals, list) else [vals])))
                        for k, vals in _map(raw, "required_if", where).items())
    pattern_by = []
    for other, by_value in _map(raw, "pattern_by", where).items():
        if not isinstance(by_value, dict):
            raise CatalogError(f"{where}: pattern_by.{other} must map values to patterns")
        pattern_by.append((str(other), tuple((str(v), _regex(str(rx), where)) for v, rx in by_value.items())))
    return Input(name=_str(raw, "name", where), location=location, kind=kind,
                 label=_str(raw, "label", where), required=bool(raw.get("required", True)),
                 personal=bool(raw.get("personal", False)), pattern=pattern,
                 choices=tuple(choices), sandbox_choices=sandbox_choices,
                 hint=_str(raw, "hint", where, required=False),
                 legacy_ok=bool(raw.get("legacy_ok", False)),
                 required_if=required_if, pattern_by=tuple(pattern_by))


def load_catalog(path: Path = CATALOG_FILE, check_specs: bool = True) -> Catalog:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path.name} is not valid YAML ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise CatalogError("catalog.yaml must have a 'products' list")
    products = {}
    for raw in data["products"]:
        if not isinstance(raw, dict):
            raise CatalogError("every product must be a mapping")
        pid = _str(raw, "id", "product")
        where = f"product {pid}"
        if pid in products:
            raise CatalogError(f"{where}: duplicate id")
        try:
            spec = load_spec(pid) if check_specs else {}
        except (yaml.YAMLError, OSError, ValueError) as exc:
            raise CatalogError(f"{where}: its spec templates/wathq/{pid}.yaml can't be read "
                               f"({type(exc).__name__})") from None
        endpoints = []
        seen = set()
        for e in _list(raw, "endpoints", where):
            if not isinstance(e, dict):
                raise CatalogError(f"{where}: every endpoint must be a mapping")
            eid = _str(e, "id", where)
            ew = f"{where}.{eid}"
            if eid in seen:
                raise CatalogError(f"{ew}: duplicate endpoint id")
            seen.add(eid)
            epath = _str(e, "path", ew)
            inputs = tuple(_load_input(i, f"{ew} input") for i in _list(e, "inputs", ew))
            names = [i.name for i in inputs]
            if len(set(names)) != len(names):
                raise CatalogError(f"{ew}: duplicate input names")
            by_name = {i.name: i for i in inputs}
            for placeholder in re.findall(r"{([^}]+)}", epath):
                if placeholder not in by_name or by_name[placeholder].location != "path":
                    raise CatalogError(f"{ew}: path parameter {{{placeholder}}} has no path input")
            if sum(1 for i in inputs if i.kind == "company_number") > 1:
                raise CatalogError(f"{ew}: at most one company_number input")
            for i in inputs:
                for other, _ in i.required_if + i.pattern_by:
                    if other not in by_name:
                        raise CatalogError(f"{ew}: {i.name} refers to unknown input {other!r}")
            fixed = tuple((str(k), str(v)) for k, v in _map(e, "fixed", ew).items())
            if set(dict(fixed)) & set(names) or "language" in dict(fixed):
                raise CatalogError(f"{ew}: a fixed parameter can't also be an input or the language")
            if check_specs:
                if epath not in (spec.get("paths") or {}):
                    raise CatalogError(f"{ew}: {epath} is not in templates/wathq/{pid}.yaml")
                declared = _spec_params(spec, epath)
                for inp in inputs:
                    if declared.get(inp.name) != inp.location:
                        raise CatalogError(f"{ew}: the spec has no {inp.location} parameter {inp.name!r}")
                for name, _ in fixed:
                    if declared.get(name) != "query":
                        raise CatalogError(f"{ew}: the spec has no query parameter {name!r}")
                if bool(e.get("language")) and declared.get("language") != "query":
                    raise CatalogError(f"{ew}: the spec has no language parameter")
            price = e.get("price", -1)
            if not isinstance(price, (int, float)) or isinstance(price, bool):
                raise CatalogError(f"{ew}: price must be a number")
            view = e.get("view", "generic")
            if view not in ("generic", "contract"):
                raise CatalogError(f"{ew}: unknown view {view!r}")
            overrides = _map(e, "labels", ew)
            one_of = tuple(str(n) for n in _list(e, "one_of", ew))
            if one_of and (len(one_of) < 2 or not set(one_of) <= set(names)
                           or any(by_name[n].required for n in one_of)):
                raise CatalogError(f"{ew}: one_of must name 2+ optional inputs of this endpoint")
            endpoints.append(Endpoint(
                id=f"{pid}.{eid}", product=pid, path=epath, label=_str(e, "label", ew),
                description=_str(e, "description", ew, required=False), price=float(price),
                lookup=bool(e.get("lookup", False)), in_sandbox=bool(e.get("in_sandbox", False)),
                language=bool(e.get("language", False)), inputs=inputs,
                personal=frozenset(str(p) for p in _list(e, "personal", ew)),
                overrides={str(k): str(v) for k, v in overrides.items()}, view=view,
                one_of=one_of, redact=frozenset(str(r) for r in _list(e, "redact", ew)),
                fixed=fixed))
        if not endpoints:
            raise CatalogError(f"{where}: no endpoints")
        products[pid] = Product(id=pid, label=_str(raw, "label", where),
                                label_en=_str(raw, "label_en", where),
                                base=_str(raw, "base", where),
                                sandbox_base=_str(raw, "sandbox_base", where, required=False),
                                endpoints=tuple(endpoints))
    if not products:
        raise CatalogError("catalog.yaml has no products")
    return Catalog(products)


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    return load_catalog()
