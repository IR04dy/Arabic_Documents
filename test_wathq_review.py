"""Regression tests for the multi-service review: privacy of free text and of
drifting answer shapes, honest prices, struck-off CR numbers, input rules
that depend on other inputs, caching, truncation and catalog robustness."""
import copy
import json
import pathlib
import tempfile
import unittest

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_wathq_catalog import walk_view
from test_wathq_verify import CONTRACT
from wathq_api import MSG_VIEW, create_router
from wathq_catalog import CATALOG_FILE, CatalogError, get_catalog, load_catalog
from wathq_client import MSG_CONVERT_NOT_FOUND, MSG_NOT_FOUND, WathqError
from wathq_verify import shape_contract
from wathq_view import MAX_ITEMS, build_view, redact_digits, split_description

CAT = get_catalog()
HEADERS = {"X-Wathq-Request": "1"}


def text_of(view):
    return "\n".join(walk_view(view))


def view_for(endpoint_id, data):
    e = CAT.endpoints[endpoint_id]
    return build_view(data, labels=e.labels(), personal=e.personal, overrides=e.overrides,
                      title=e.label, redact=e.redact, tidy=not e.lookup)


class FreeTextPrivacyTests(unittest.TestCase):
    def test_deed_text_masks_owner_id_and_phone(self):
        view = view_for("real_estate.deed", {"deedDetails": {
            "deedText": "بموجب السجل المدني رقم 1094791234 وجواله 0551234567 بمساحة 500000 م2"}})
        text = text_of(view)
        for raw in ("1094791234", "0551234567"):
            self.assertNotIn(raw, text)
        self.assertIn("1234", text)
        self.assertIn("500000", text)          # an area is not an identifier

    def test_beneficiary_and_manager_notes(self):
        text = text_of(view_for("cr.beneficiary", {"beneficiaries": [{"addingReasons": [
            {"additionalText": "ابنة المالك صاحب السجل المدني 1101552388"}]}]}))
        self.assertNotIn("1101552388", text)
        text = text_of(view_for("contracts.manager", [{"permissions": [
            {"specialConditionText": "بشرط موافقة 1132381599"}]}]))
        self.assertNotIn("1132381599", text)

    def test_prose_backstop_for_unlisted_fields(self):
        text = text_of(build_view({"someNote": "تواصل مع 0551234567 بخصوص الهوية 2012345678"}))
        self.assertNotIn("0551234567", text)
        self.assertNotIn("2012345678", text)
        self.assertEqual(text_of(build_view({"crNumber": "1010711252"})).count("1010711252"), 1)

    def test_contract_free_text(self):
        raw = copy.deepcopy(CONTRACT)
        raw["additionalArticles"] = [{"title": "تعيين", "text": "عُيّن فهد بموجب السجل المدني رقم 1132381599 وجواله 0551234567"}]
        raw["additionalDecisionText"] = "بحضور 1012345678"
        raw["entity"]["management"]["dismissalMethod"] = "بقرار من 2012345678"
        blob = json.dumps(shape_contract(raw), ensure_ascii=False)
        for value in ("1132381599", "0551234567", "1012345678", "2012345678"):
            self.assertNotIn(value, blob)

    def test_redact_digits(self):
        self.assertEqual(redact_digits("055 123 4567"), "••• ••• 4567")
        self.assertEqual(redact_digits("۲۴۲۴۴۰۴۱۳۱"), "••••••۴۱۳۱")
        self.assertEqual(redact_digits("١٠١٢٣٤٥٦٧٨"), "••••••٥٦٧٨")
        for keep in ("١٤٤٣/٠٢/٢١", "1250.75", "500000", "12345678"):
            self.assertEqual(redact_digits(keep), keep)

    def test_long_text_is_redacted_before_it_is_shortened(self):
        long = "أ " * 3500 + " 1012345678"
        text = text_of(view_for("real_estate.deed", {"deedDetails": {"deedText": long}}))
        self.assertNotIn("1012345678", text)


class ShapeDriftPrivacyTests(unittest.TestCase):
    def test_object_where_a_list_was_listed_and_recased_keys(self):
        text = text_of(view_for("attorney.info", {
            "principals": {"Id": "2424404131", "Birthday": "1987-08-27T00:00:00", "name": "x"},
            "agents": [{"id": "1059608891", "birthday": "1995-01-20"}]}))
        for raw in ("2424404131", "1059608891", "08-27", "01-20"):
            self.assertNotIn(raw, text)
        self.assertIn("1987", text)

    def test_investor_parties_as_object(self):
        text = text_of(view_for("investor.fullinfo", {"parties": {"id": "1180317999", "name": "x"}}))
        self.assertNotIn("1180317999", text)

    def test_top_level_list_paths_match_object_answers(self):
        text = text_of(view_for("cr.managers", {"identity": {"id": "1017162388"}, "name": "x"}))
        self.assertNotIn("1017162388", text)

    def test_birthday_backstop(self):
        text = text_of(build_view({"person": {"birthDay": "1990-05-01"}}))
        self.assertNotIn("05-01", text)


class CatalogRuleTests(unittest.TestCase):
    def test_status_prices_are_honest(self):
        plain, dated = CAT.endpoints["cr.status"], CAT.endpoints["cr.status_dates"]
        self.assertEqual((plain.price, dated.price), (2, 5))
        self.assertNotIn("includeDates", CAT.request("cr.status", {"id": "7001272124"}).query())
        self.assertEqual(CAT.request("cr.status_dates", {"id": "7001272124"}).query()["includeDates"], "true")
        self.assertEqual([i.name for i in dated.inputs], ["id"])

    def test_nationality_required_for_passport(self):
        with self.assertRaises(ValueError) as ctx:
            CAT.request("cr.related", {"id": "12345678", "idType": "Passport"})
        self.assertIn("مطلوب", str(ctx.exception))
        CAT.request("cr.related", {"id": "12345678", "idType": "Passport", "nationality": "113"})
        CAT.request("cr.related", {"id": "1012345678", "idType": "National_ID"})

    def test_id_pattern_by_type(self):
        for bad in ("101234567", "A012345678", "2012345678"):
            with self.assertRaises(ValueError):
                CAT.request("real_estate.deed", {"deedNumber": "1", "idNumber": bad, "idType": "National_ID"})
        CAT.request("real_estate.deed", {"deedNumber": "1", "idNumber": "2012345678", "idType": "Resident_ID"})
        CAT.request("real_estate.deed", {"deedNumber": "1", "idNumber": "AB123456", "idType": "Passport"})

    def test_sandbox_choices(self):
        pub = {e["id"]: e for p in CAT.public("sandbox")["products"] for e in p["endpoints"]}
        values = [c["value"] for c in pub["contracts.manager"]["inputs"][2]["choices"]]
        self.assertNotIn("License_No", values)
        self.assertIn("National_ID", values)
        with self.assertRaises(ValueError):
            CAT.request("contracts.manager", {"crNationalNumber": "7001272124", "id": "1012345678",
                                              "idType": "License_No"}, env="sandbox")
        CAT.request("contracts.manager", {"crNationalNumber": "7001272124", "id": "1012345678",
                                          "idType": "License_No"}, env="production")

    def test_language_does_not_split_the_cache_where_unused(self):
        a = CAT.request("national_address.info", {"crNumber": "1010711252"}, "ar").cache_key("production")
        b = CAT.request("national_address.info", {"crNumber": "1010711252"}, "en").cache_key("production")
        self.assertEqual(a, b)

    def test_cache_key_never_holds_a_person_id(self):
        key = CAT.request("employee.info", {"id": "1012345678"}).cache_key("production")
        self.assertNotIn("1012345678", json.dumps(key))

    def test_malformed_catalogs_are_catalog_errors(self):
        data = yaml.safe_load(open(CATALOG_FILE, encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "c.yaml"
            bad.write_text("products: [ {id: cr, : }", encoding="utf-8")
            with self.assertRaises(CatalogError):
                load_catalog(bad)
            empty = copy.deepcopy(data)
            empty["products"][0]["endpoints"] = []
            bad.write_text(yaml.safe_dump(empty, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(CatalogError):
                load_catalog(bad)
            wrong = copy.deepcopy(data)
            wrong["products"][0]["endpoints"][0]["personal"] = "x"
            bad.write_text(yaml.safe_dump(wrong, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(CatalogError):
                load_catalog(bad)


class ViewShapeTests(unittest.TestCase):
    def test_lookups_keep_their_codes(self):
        view = view_for("cr.lookup_nationalities", [{"id": 113, "nameAr": "السعودية", "nicCode": "113"},
                                                    {"id": 22, "nameAr": "مصر", "nicCode": "22"}])
        self.assertEqual(view[0]["type"], "table")
        self.assertIn("113", view[0]["rows"][0])

    def test_truncation_is_reported(self):
        view = build_view([{"a": str(i), "b": "x"} for i in range(MAX_ITEMS + 50)], title="قائمة")
        self.assertEqual((view[0]["truncated"], view[0]["total"], len(view[0]["rows"])),
                         (True, MAX_ITEMS + 50, MAX_ITEMS))

    def test_nested_lists_are_bounded(self):
        deep = "x"
        for _ in range(40):
            deep = [deep]
        json.dumps(build_view({"a": deep}), ensure_ascii=False)

    def test_columns_with_the_same_label_stay_apart(self):
        view = build_view([{"nameEn": "A", "nameEr": "B"}, {"nameEn": "C", "nameEr": "D"}], title="ق")
        self.assertEqual(len(view[0]["columns"]), 2)
        self.assertEqual(view[0]["rows"][0], ["A", "B"])
        self.assertNotIn("_k", json.dumps(view))

    def test_em_dash_descriptions(self):
        self.assertEqual(split_description("Related CR — السجلات المرتبطة"), ("السجلات المرتبطة", "Related CR"))


class FakeClient:
    def __init__(self, env="production", convert=None, answers=None):
        self.env, self.convert = env, convert
        self.answers = answers or {}
        self.calls, self.sent = [], 0

    def key_problem(self):
        return ""

    def national_number(self, cr):
        self.calls.append(("convert", cr))
        self.sent += 1
        if isinstance(self.convert, Exception):
            raise self.convert
        return self.convert

    def call(self, base, path, query=None, headers=None, what=""):
        self.calls.append(("call", base + path))
        self.sent += 1
        answer = self.answers.get(base + path)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise WathqError(MSG_NOT_FOUND, 404, "404.2.1")
        return copy.deepcopy(answer)


def make(client):
    app = FastAPI()
    app.include_router(create_router(client, environ={}))
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1))


def post(http, endpoint, language="ar", **inputs):
    return http.post("/wathq/query", json={"endpoint": endpoint, "inputs": inputs, "language": language},
                     headers=HEADERS)


class StruckOffTests(unittest.TestCase):
    def test_conversion_not_found_falls_back_to_the_old_number(self):
        client = FakeClient(convert=WathqError(MSG_CONVERT_NOT_FOUND, 404, "404.2.1"),
                            answers={"/commercial-registration/info/1010711252": {"name": "مشطوب"}})
        body = post(make(client), "cr.info", id="1010711252").json()
        self.assertEqual(client.calls, [("convert", "1010711252"),
                                        ("call", "/commercial-registration/info/1010711252")])
        self.assertTrue(body["query"]["legacy_used"])
        self.assertEqual(body["calls_used"], 2)

    def test_unified_not_found_retries_with_the_old_number(self):
        client = FakeClient(convert="7001272475",
                            answers={"/commercial-registration/info/1010711252": {"name": "مشطوب"}})
        r = post(make(client), "cr.info", id="1010711252")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([c[1] for c in client.calls],
                         ["1010711252", "/commercial-registration/info/7001272475",
                          "/commercial-registration/info/1010711252"])

    def test_contracts_never_send_the_old_number(self):
        client = FakeClient(convert=WathqError(MSG_CONVERT_NOT_FOUND, 404, "404.2.1"))
        r = post(make(client), "contracts.management", crNationalNumber="1010711252")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(client.calls, [("convert", "1010711252")])


class RouteBehaviourTests(unittest.TestCase):
    def test_language_is_not_billed_twice_where_unused(self):
        client = FakeClient(answers={"/spl/national/address/info/1010711252": [{"city": "الرياض"}]})
        http = make(client)
        post(http, "national_address.info", "ar", crNumber="1010711252")
        self.assertEqual(post(http, "national_address.info", "en", crNumber="1010711252").json()["calls_used"], 0)

    def test_generic_failure_message(self):
        class Boom(dict):
            pass
        client = FakeClient(answers={"/drugs/price/1": {"x": "\ud83d"}})
        import wathq_api
        original = wathq_api.build_view
        wathq_api.build_view = lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
        try:
            r = post(make(client), "drug.price", id="1")
        finally:
            wathq_api.build_view = original
        self.assertEqual((r.status_code, r.json()["error"]), (502, MSG_VIEW))

    def test_sandbox_validation_uses_the_sandbox_choices(self):
        client = FakeClient(env="sandbox")
        r = post(make(client), "contracts.manager", crNationalNumber="7001272124", id="1012345678",
                 idType="License_No")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(client.calls, [])

    def test_lookup_codes_reach_the_page(self):
        client = FakeClient(answers={"/commercial-registration/lookup/nationalities":
                                     [{"id": 113, "nameAr": "السعودية", "nicCode": "113"},
                                      {"id": 22, "nameAr": "مصر", "nicCode": "22"}]})
        body = post(make(client), "cr.lookup_nationalities").json()
        self.assertIn("113", json.dumps(body["view"]))


if __name__ == "__main__":
    unittest.main()
