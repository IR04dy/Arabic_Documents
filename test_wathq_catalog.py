"""The Wathq catalog and the generic view, for every product and endpoint.

No network: answers are Wathq's own examples from templates/wathq/*.yaml and
synthetic answers generated from each endpoint's response schema, with a
recognisable fake ID planted in every personal field, so the test can prove
the view never shows one unmasked."""
import itertools
import json
import re
import unittest

from wathq_catalog import (CatalogError, Input, KINDS, get_catalog, load_catalog)
from wathq_view import (_resolve, build_view, load_spec, mask, redact_digits, response_schema,
                        spec_labels, split_description)

CATALOG = get_catalog()
ENDPOINTS = list(CATALOG.endpoints.values())


def walk_view(nodes):
    """Every string the browser would receive."""
    for n in nodes:
        for key in ("label", "value"):
            if isinstance(n.get(key), str):
                yield n[key]
        for item in n.get("items", []) or []:
            if isinstance(item, str):
                yield item
            else:
                yield from walk_view([item])
        for col in n.get("columns", []) or []:
            yield col
        for row in n.get("rows", []) or []:
            yield from (c for c in row if isinstance(c, str))
        yield from walk_view(n.get("children", []) or [])


_counter = itertools.count(1)


def synth(spec, schema, path, personal, planted, depth=0):
    """An answer shaped like `schema`. Personal leaves get a unique fake ID
    (recorded in `planted`); other strings get a short marker."""
    schema = _resolve(spec, schema)
    if depth > 8 or not schema:
        return None
    if schema.get("type") == "array" or "items" in schema:
        return [synth(spec, schema.get("items") or {}, path + "[]", personal, planted, depth + 1)
                for _ in range(2)]
    props = schema.get("properties")
    if props is not None or schema.get("type") == "object":
        out = {}
        for key, child in (props or {}).items():
            full = f"{path}.{key}" if path else key
            out[key] = synth(spec, child, full, personal, planted, depth + 1)
        return out
    kind = schema.get("type")
    if path in personal:
        fake = f"9{next(_counter):09d}"
        planted.add(fake)
        return fake
    if kind == "integer":
        return 7
    if kind == "number":
        return 7.5
    if kind == "boolean":
        return True
    return f"v{next(_counter)}"


def example(spec, path):
    op = spec["paths"][path]["get"]
    ok = op["responses"].get("200") or op["responses"].get(200) or {}
    ok = _resolve(spec, ok)
    return (ok.get("examples") or {}).get("application/json")


def values_at(data, path):
    """Every scalar at a dotted path ([] for list items) in an answer."""
    parts = re.findall(r"\[\]|[^.\[\]]+", path)
    nodes = [data]
    for part in parts:
        nxt = []
        for n in nodes:
            if part == "[]":
                if isinstance(n, list):
                    nxt.extend(n)
            elif isinstance(n, dict) and part in n:
                nxt.append(n[part])
        nodes = nxt
    return [n for n in nodes if isinstance(n, (str, int)) and not isinstance(n, bool)]


class CatalogShapeTests(unittest.TestCase):
    def test_all_products_and_their_endpoints(self):
        self.assertEqual(list(CATALOG.products),
                         ["cr", "contracts", "national_address", "attorney", "real_estate",
                          "employee", "investor", "drug"])
        self.assertGreaterEqual(len(ENDPOINTS), 40)
        for e in ENDPOINTS:
            self.assertTrue(e.label and re.search(r"[؀-ۿ]", e.label), e.id)
            self.assertTrue(e.price >= 0 or e.price == -1, e.id)
            for inp in e.inputs:
                self.assertIn(inp.kind, KINDS, e.id)
                self.assertTrue(re.search(r"[؀-ۿ]", inp.label), (e.id, inp.name))

    def test_every_path_and_parameter_is_in_the_spec(self):
        load_catalog()          # raises CatalogError on any mismatch

    def test_spec_mismatch_is_caught(self):
        import tempfile, pathlib, yaml
        data = yaml.safe_load(open("templates/wathq/catalog.yaml", encoding="utf-8"))
        data["products"][0]["endpoints"][0]["path"] = "/fullinfo/{id}/x"
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "c.yaml"
            bad.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(CatalogError):
                load_catalog(bad)
            data["products"][0]["endpoints"][0]["path"] = "/fullinfo/{id}"
            data["products"][0]["endpoints"][0]["inputs"][0]["name"] = "crId"
            bad.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(CatalogError):
                load_catalog(bad)

    def test_known_prices(self):
        p = {e.id: e.price for e in ENDPOINTS}
        self.assertEqual(p["cr.fullinfo"], 12)
        self.assertEqual(p["contracts.info"], 12)
        self.assertEqual(p["national_address.info"], 2)
        self.assertEqual(p["employee.info"], 7)
        self.assertEqual(p["investor.fullinfo"], 50)
        self.assertEqual(p["drug.price"], 0)

    def test_sandbox_availability(self):
        self.assertTrue(CATALOG.available(CATALOG.endpoints["cr.fullinfo"], "sandbox"))
        self.assertFalse(CATALOG.available(CATALOG.endpoints["employee.info"], "sandbox"))
        self.assertTrue(CATALOG.available(CATALOG.endpoints["employee.info"], "production"))

    def test_public_catalog_has_no_privacy_lists(self):
        text = json.dumps(CATALOG.public("production"), ensure_ascii=False)
        self.assertNotIn("identity.id", text)
        self.assertNotIn("personal_fields", text)
        self.assertIn('"one_of": ["principalId", "agentId"]', text)


class InputTests(unittest.TestCase):
    def req(self, endpoint, **inputs):
        return CATALOG.request(endpoint, inputs)

    def test_company_numbers(self):
        r = self.req("cr.fullinfo", id="٧٠٠١٢٧٢١٢٤")
        self.assertEqual(r.values["id"], "7001272124")
        self.assertIsNone(r.conversion_value())
        r = self.req("cr.fullinfo", id="1010 711 252")
        self.assertEqual(r.conversion_value(), "1010711252")
        self.assertEqual(r.path("7001272475"), "/fullinfo/7001272475")
        for bad in ("123", "7101234567", "abc", "70012721245"):
            with self.assertRaises(ValueError):
                self.req("cr.fullinfo", id=bad)

    def test_language_and_query(self):
        r = CATALOG.request("cr.status_dates", {"id": "7001272124"}, "en")
        self.assertEqual(r.query(), {"includeDates": "true", "language": "en"})
        r = CATALOG.request("cr.status", {"id": "7001272124"}, "en")
        self.assertEqual(r.query(), {"language": "en"})
        r = CATALOG.request("national_address.info", {"crNumber": "1010711252"})
        self.assertEqual(r.query(), {})                       # no language parameter there

    def test_header_inputs_and_personal_privacy(self):
        r = self.req("employee.info", id="1012 345-678")
        self.assertEqual(r.headers(), {"id": "1012345678"})
        self.assertEqual(r.path(), "/v2/info")
        self.assertEqual(r.public_inputs(), {})               # a person's ID is never echoed

    def test_choices(self):
        ok = self.req("real_estate.deed", deedNumber="310105045", idNumber="1012345678", idType="National_ID")
        self.assertEqual(ok.path(), "/deed/310105045/1012345678/National_ID")
        with self.assertRaises(ValueError):
            self.req("real_estate.deed", deedNumber="310105045", idNumber="1012345678", idType="national_id")

    def test_one_of(self):
        with self.assertRaises(ValueError) as ctx:
            self.req("attorney.info", code="4317608")
        self.assertIn("واحدًا على الأقل", str(ctx.exception))
        self.assertEqual(self.req("attorney.info", code="4317608", agentId="1012345678").query(),
                         {"agentId": "1012345678"})

    def test_unknown_inputs_and_endpoints(self):
        with self.assertRaises(ValueError):
            self.req("cr.fullinfo", id="7001272124", evil="x")
        with self.assertRaises(ValueError):
            CATALOG.request("cr.nope", {})
        with self.assertRaises(ValueError):
            CATALOG.request("cr.fullinfo", {"id": "7001272124"}, "fr")

    def test_path_values_are_quoted(self):
        inp = Input(name="id", location="path", kind="other", label="x")
        with self.assertRaises(ValueError):
            inp.clean("../../x")
        with self.assertRaises(ValueError):
            inp.clean("a/b")

    def test_cache_keys_differ_by_input_and_language(self):
        a = CATALOG.request("cr.info", {"id": "7001272124"}, "ar").cache_key("production")
        b = CATALOG.request("cr.info", {"id": "7001272124"}, "en").cache_key("production")
        c = CATALOG.request("cr.info", {"id": "7001272125"}, "ar").cache_key("production")
        self.assertEqual(len({a, b, c}), 3)


class ViewTests(unittest.TestCase):
    def test_every_endpoint_with_a_synthetic_answer(self):
        """Every endpoint: a schema-shaped answer with a fake ID in every
        personal field renders, and no fake ID reaches the browser."""
        for e in ENDPOINTS:
            with self.subTest(endpoint=e.id):
                spec = load_spec(e.product)
                planted: set = set()
                data = synth(spec, response_schema(spec, e.path), "", e.personal, planted)
                view = build_view(data, labels=e.labels(), personal=e.personal,
                                  overrides=e.overrides, title=e.label, redact=e.redact)
                text = "\n".join(walk_view(view))
                json.dumps(view, ensure_ascii=False).encode("utf-8")
                for fake in planted:
                    self.assertNotIn(fake, text)

    def test_every_endpoint_with_wathqs_own_example(self):
        seen = 0
        for e in ENDPOINTS:
            spec = load_spec(e.product)
            ex = example(spec, e.path)
            if ex is None:
                continue
            seen += 1
            with self.subTest(endpoint=e.id):
                view = build_view(ex, labels=e.labels(), personal=e.personal,
                                  overrides=e.overrides, title=e.label, redact=e.redact)
                text = "\n".join(walk_view(view))
                for path in e.personal:
                    for value in values_at(ex, path):
                        value = str(value)
                        if len(value) >= 6 and re.search(r"\d{4}", value):
                            self.assertNotIn(value, text, f"{e.id} {path}")
        self.assertGreater(seen, 20)

    def test_labels_come_from_the_spec_in_arabic(self):
        labels = spec_labels("cr", "/fullinfo/{id}")
        self.assertEqual(labels["crNumber"][0], "رقم السجل التجاري")
        e = CATALOG.endpoints["cr.fullinfo"]
        view = build_view(example(load_spec("cr"), "/fullinfo/{id}"), labels=e.labels(),
                          personal=e.personal, overrides=e.overrides)
        top = [n["label"] for n in view]
        self.assertIn("رقم السجل التجاري", top)
        english = [l for l in walk_view(view) if re.fullmatch(r"[A-Za-z ]{4,}", l)]
        self.assertEqual(english, [], english)

    def test_no_label_falls_back_to_english(self):
        """Every field of every endpoint — in Wathq's examples and in
        schema-shaped answers — gets an Arabic label."""
        import wathq_view
        english = set()
        original = wathq_view._Labeler.__call__

        def spy(self, path, key):
            out = original(self, path, key)
            if not re.search(r"[؀-ۿ]", out):
                english.add(key)
            return out

        wathq_view._Labeler.__call__ = spy
        try:
            for e in ENDPOINTS:
                spec = load_spec(e.product)
                for data in (example(spec, e.path),
                             synth(spec, response_schema(spec, e.path), "", e.personal, set())):
                    if data is not None:
                        build_view(data, labels=e.labels(), personal=e.personal,
                                   overrides=e.overrides, title=e.label, redact=e.redact)
        finally:
            wathq_view._Labeler.__call__ = original
        self.assertEqual(sorted(english), [])

    def test_split_description(self):
        self.assertEqual(split_description("Commercial Registry Number - رقم السجل التجاري"),
                         ("رقم السجل التجاري", "Commercial Registry Number"))
        self.assertEqual(split_description(None), ("", ""))

    def test_masking(self):
        self.assertEqual(mask("1012345678", "id"), "••••••5678")
        self.assertEqual(mask("1405-03-25", "birthDate"), "1405-••-••")
        self.assertEqual(mask("someone@mail.com", "email"), "s•••@mail.com")
        self.assertEqual(redact_digits("هوية 1012345678 بتاريخ 1445/03/12"), "هوية ••••••5678 بتاريخ 1445/03/12")

    def test_name_backstop_masks_unlisted_personal_fields(self):
        view = build_view({"person": {"mobileNumber": "0551234567", "nationalId": "1012345678",
                                      "identity": {"id": "2012345678"}, "birthDate": "1990-05-01"}})
        text = "\n".join(walk_view(view))
        for raw in ("0551234567", "1012345678", "2012345678", "05-01"):
            self.assertNotIn(raw, text)

    def test_tidying(self):
        view = build_view({"city": {"id": 3, "name": "الرياض"}, "cityId": 3, "cityName": "الرياض",
                           "x": None, "y": "string", "z": [], "ok": False})
        labels = [n["label"] for n in view]
        self.assertEqual(len(view), 3)                # city, cityName, ok
        self.assertNotIn("City Id", labels)
        self.assertEqual(view[0]["value"], "الرياض")
        self.assertEqual(view[-1]["value"], "لا")

    def test_shapes(self):
        flat = build_view([{"a": "1", "b": "2"}, {"a": "3", "b": "4"}], title="قائمة")
        self.assertEqual(flat[0]["type"], "table")
        nested = build_view([{"a": {"b": "1"}}, {"a": {"b": "2"}}], title="قائمة")
        self.assertEqual(nested[0]["type"], "cards")
        self.assertEqual(build_view(True, title="مالك؟")[0]["value"], "نعم")
        self.assertEqual(build_view([], title="x"), [])

    def test_lone_surrogates_survive(self):
        view = build_view({"name": "Test \ud83d Co"})
        json.dumps(view, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
