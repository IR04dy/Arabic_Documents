"""/wathq/catalog and /wathq/query with a fake client: guards, conversion,
caching, privacy of personal inputs, sandbox availability, error bodies."""
import copy
import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_wathq_catalog import example
from test_wathq_verify import CONTRACT
from wathq_api import create_router
from wathq_client import MSG_NOT_FOUND, WathqError
from wathq_view import load_spec

HEADERS = {"X-Wathq-Request": "1"}
LOCAL = ("127.0.0.1", 50000)


class FakeClient:
    def __init__(self, env="production", answers=None, error=None):
        self.env, self.error = env, error
        self.answers = answers or {}
        self.calls, self.sent = [], 0

    def key_problem(self):
        return ""

    def call(self, base, path, query=None, headers=None, what="request"):
        self.calls.append((base, path, dict(query or {}), dict(headers or {}), what))
        self.sent += 1
        if self.error:
            raise self.error
        for prefix, answer in self.answers.items():
            if (base + path).startswith(prefix):
                return copy.deepcopy(answer)
        return {}

    def national_number(self, cr):
        self.calls.append(("convert", cr))
        self.sent += 1
        return "7001272475"


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make(client=None, environ=None, peer=LOCAL):
    client = client or FakeClient()
    clock = Clock()
    app = FastAPI()
    app.include_router(create_router(client, environ=environ or {}, clock=clock))
    return TestClient(app, base_url="http://127.0.0.1", client=peer), client, clock


def post(http, endpoint, language="ar", headers=HEADERS, **inputs):
    return http.post("/wathq/query", json={"endpoint": endpoint, "inputs": inputs, "language": language},
                     headers=headers)


CR_FULL = example(load_spec("cr"), "/fullinfo/{id}")


class CatalogRouteTests(unittest.TestCase):
    def test_catalog_lists_every_product(self):
        http, _, _ = make()
        body = http.get("/wathq/catalog").json()
        self.assertEqual(len(body["products"]), 8)
        self.assertEqual(body["env"], "production")
        ids = [e["id"] for p in body["products"] for e in p["endpoints"]]
        self.assertIn("employee.info", ids)
        self.assertNotIn("identity.id", json.dumps(body))

    def test_sandbox_marks_unavailable_endpoints(self):
        http, _, _ = make(FakeClient(env="sandbox"))
        eps = {e["id"]: e for p in http.get("/wathq/catalog").json()["products"] for e in p["endpoints"]}
        self.assertTrue(eps["cr.fullinfo"]["available"])
        self.assertFalse(eps["drug.price"]["available"])

    def test_catalog_is_local_only(self):
        http, _, _ = make(peer=("192.168.1.50", 1))
        self.assertEqual(http.get("/wathq/catalog").status_code, 403)


class QueryTests(unittest.TestCase):
    def test_generic_view_with_masking(self):
        http, client, _ = make(FakeClient(answers={"/commercial-registration/fullinfo": CR_FULL}))
        r = post(http, "cr.fullinfo", id="7001272475")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["view_type"], "generic")
        self.assertEqual(body["label"], "البيانات الكاملة للسجل التجاري")
        self.assertEqual(client.calls[0][:3], ("/commercial-registration", "/fullinfo/7001272475",
                                               {"language": "ar"}))
        text = json.dumps(body["view"], ensure_ascii=False)
        self.assertIn("رقم السجل التجاري", text)
        for person in ("1017162388", "1017162234"):         # IDs in Wathq's own example
            self.assertNotIn(person, text)

    def test_old_cr_is_converted_then_cached(self):
        http, client, _ = make(FakeClient(answers={"/commercial-registration/info": {"name": "x"}}))
        body = post(http, "cr.info", id="1010711252").json()
        self.assertEqual(client.calls[0], ("convert", "1010711252"))
        self.assertEqual(client.calls[1][1], "/info/7001272475")
        self.assertEqual((body["calls_used"], body["query"]["converted"], body["query"]["national_number"]),
                         (2, True, "7001272475"))
        body = post(http, "cr.info", id="1010711252").json()
        self.assertEqual((body["calls_used"], body["cached"]), (0, True))

    def test_personal_inputs_go_in_headers_and_are_never_echoed(self):
        http, client, _ = make(FakeClient(answers={"/masdr/employee/v2/info": {"name": "موظف"}}))
        r = post(http, "employee.info", id="1012345678")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(client.calls[0][1:4], ("/v2/info", {}, {"id": "1012345678"}))
        self.assertNotIn("1012345678", r.text)
        self.assertEqual(r.json()["query"]["inputs"], {})

    def test_lookups_are_cached_for_a_day(self):
        http, client, clock = make(FakeClient(answers={"/commercial-registration/lookup": [{"id": 1, "name": "فعال"}]}))
        post(http, "cr.lookup_status")
        clock.now += 3600
        self.assertEqual(post(http, "cr.lookup_status").json()["calls_used"], 0)
        clock.now += 24 * 3600
        self.assertEqual(post(http, "cr.lookup_status").json()["calls_used"], 1)

    def test_contract_keeps_its_bespoke_view(self):
        http, _, _ = make(FakeClient(answers={"/company-contract/info": CONTRACT}))
        body = post(http, "contracts.info", crNationalNumber="7001272124").json()
        self.assertEqual(body["view_type"], "contract")
        self.assertEqual(body["entity"]["name"], CONTRACT["entity"]["name"])
        self.assertNotIn("1000000001", json.dumps(body))

    def test_sandbox_uses_the_sandbox_base_and_refuses_the_rest(self):
        http, client, _ = make(FakeClient(env="sandbox", answers={"/sandbox/commercial-registration": {"name": "x"}}))
        self.assertEqual(post(http, "cr.info", id="7001272124").status_code, 200)
        self.assertEqual(client.calls[0][0], "/sandbox/commercial-registration")
        r = post(http, "drug.price", id="2208240355")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(client.calls), 1)

    def test_validation_errors_never_call_wathq(self):
        http, client, _ = make()
        for endpoint, inputs in (("cr.fullinfo", {"id": "123"}), ("cr.nope", {}),
                                 ("attorney.info", {"code": "4317608"}),
                                 ("real_estate.deed", {"deedNumber": "1", "idNumber": "1012345678", "idType": "x"}),
                                 ("cr.fullinfo", {"id": "7001272124", "extra": "1"})):
            r = post(http, endpoint, **inputs)
            self.assertEqual(r.status_code, 400, (endpoint, r.text))
            self.assertRegex(r.json()["error"], "[؀-ۿ]")
        self.assertEqual(client.calls, [])

    def test_error_body_and_wathq_errors(self):
        http, _, _ = make(FakeClient(answers={"/commercial-registration/info": {"code": "404.2.1", "message": "x"}}))
        r = post(http, "cr.info", id="7001272124")
        self.assertEqual((r.status_code, r.json()["error"]), (404, MSG_NOT_FOUND))
        http, _, _ = make(FakeClient(error=WathqError(MSG_NOT_FOUND, 404, "404.2.1")))
        r = post(http, "cr.info", id="7001272124")
        self.assertEqual((r.status_code, r.json()["code"], r.json()["sent"]), (404, "404.2.1", 1))

    def test_guards(self):
        http, client, _ = make()
        self.assertEqual(post(http, "drug.price", headers={}, id="1").status_code, 403)
        self.assertEqual(post(http, "drug.price", headers={**HEADERS, "Origin": "https://evil.example"},
                              id="1").status_code, 403)
        http2, client2, _ = make(peer=("192.168.1.50", 1))
        self.assertEqual(post(http2, "drug.price", id="1").status_code, 403)
        self.assertEqual(client.calls + client2.calls, [])

    def test_bodies(self):
        http, _, _ = make()
        self.assertEqual(http.post("/wathq/query", content=b"x" * 9000, headers={
            **HEADERS, "content-type": "application/json"}).status_code, 413)
        self.assertEqual(http.post("/wathq/query", content=b"[" * 3000, headers={
            **HEADERS, "content-type": "application/json"}).status_code, 400)
        self.assertEqual(http.post("/wathq/query", data={"a": "b"}, headers=HEADERS).status_code, 415)

    def test_attorney_free_text_is_redacted(self):
        answer = {"code": 4317608, "text": "وكالة عن محمد هوية 1012345678 في البيع", "principals": [
            {"name": "محمد", "id": "1012345678", "birthday": "1400-01-01"}]}
        http, _, _ = make(FakeClient(answers={"/v1/attorney/info": answer}))
        r = post(http, "attorney.info", code="4317608", principalId="1012345678")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("1012345678", r.text)
        self.assertNotIn("1400-01-01", r.text)
        self.assertIn("5678", r.text)


if __name__ == "__main__":
    unittest.main()
