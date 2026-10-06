"""The /wathq routes with a fake client: guards, caching, conversion, errors."""
import copy
import json
import threading
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_wathq_verify import CONTRACT
from wathq_api import create_router
from wathq_client import MSG_NOT_CONFIGURED, MSG_NOT_FOUND, WathqError

HEADERS = {"X-Wathq-Request": "1"}
SECRET_ID = "1000000001"


class FakeClient:
    def __init__(self, env="production", problem="", error=None, answer=None):
        self.env, self.problem, self.error = env, problem, error
        self.answer = copy.deepcopy(CONTRACT) if answer is None else answer
        self.calls = []
        self.sent = 0
        self.gate = None

    def key_problem(self):
        return self.problem

    def company_contract(self, national, language="ar", copy_number=None):
        self.calls.append(("contract", national, language))
        self.sent += 1
        if self.gate:
            self.gate.wait(5)
        if self.error:
            raise self.error
        return copy.deepcopy(self.answer)

    def national_number(self, cr):
        self.calls.append(("convert", cr))
        self.sent += 1
        return "7001272124"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


LOCAL_PEER = ("127.0.0.1", 50000)


def make(client=None, environ=None, base_url="http://127.0.0.1", peer=LOCAL_PEER):
    client = client or FakeClient()
    clock = Clock()
    app = FastAPI()
    app.include_router(create_router(client, environ=environ or {}, clock=clock))
    return TestClient(app, base_url=base_url, client=peer), client, clock


def lookup(http, number="7001272124", headers=HEADERS, **extra):
    return http.post("/wathq/company-contract", json={"number": number, **extra}, headers=headers)


class LookupTests(unittest.TestCase):
    def test_unified_number_returns_the_shaped_contract(self):
        http, client, _ = make()
        r = lookup(http, "٧٠٠١٢٧٢١٢٤")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["entity"]["name"], "شركة الاختبار للتجارة")
        self.assertEqual(body["query"], {"input": "7001272124", "kind": "unified",
                                         "national_number": "7001272124", "converted": False})
        self.assertEqual((body["cached"], body["calls_used"]), (False, 1))
        self.assertEqual(client.calls, [("contract", "7001272124", "ar")])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn(SECRET_ID, r.text)
        self.assertIn("fetched_at", body)

    def test_old_cr_number_is_converted_first(self):
        http, client, _ = make()
        body = lookup(http, "1023236575", language="en").json()
        self.assertEqual(client.calls, [("convert", "1023236575"), ("contract", "7001272124", "en")])
        self.assertTrue(body["query"]["converted"])
        self.assertEqual(body["calls_used"], 2)

    def test_repeat_lookups_are_cached_and_cost_nothing(self):
        http, client, clock = make()
        lookup(http, "1023236575")
        body = lookup(http, "1023236575").json()
        self.assertTrue(body["cached"])
        self.assertEqual(body["calls_used"], 0)
        self.assertEqual(len(client.calls), 2)
        lookup(http, "1023236575", language="en")          # another language is another answer
        self.assertEqual(client.calls[-1], ("contract", "7001272124", "en"))
        clock.now += 901                                    # past WATHQ_CACHE_SECONDS
        lookup(http, "7001272124")
        self.assertEqual(client.calls[-1], ("contract", "7001272124", "ar"))

    def test_cache_can_be_disabled(self):
        http, client, _ = make(environ={"WATHQ_CACHE_SECONDS": "0"})
        lookup(http)
        lookup(http)
        self.assertEqual(len(client.calls), 2)

    def test_invalid_input_never_calls_wathq(self):
        http, client, _ = make()
        for number in ("", "123", "8001272124", "70012721245", None, 7001272124):
            r = lookup(http, number)
            self.assertEqual(r.status_code, 400, number)
            self.assertRegex(r.json()["error"], "[؀-ۿ]")
        self.assertEqual(lookup(http, language="fr").status_code, 400)
        self.assertEqual(http.post("/wathq/company-contract", content=b"[1]", headers={
            **HEADERS, "content-type": "application/json"}).status_code, 400)
        self.assertEqual(http.post("/wathq/company-contract", content=b"x" * 5000, headers={
            **HEADERS, "content-type": "application/json"}).status_code, 413)
        nested = b"[" * 2000 + b"]" * 2000                      # under 4 KB, too deep for json
        self.assertEqual(http.post("/wathq/company-contract", content=nested, headers={
            **HEADERS, "content-type": "application/json"}).status_code, 400)
        self.assertEqual(lookup(http, "7101234567").status_code, 400)   # neither 70… nor an old CR
        self.assertEqual(http.post("/wathq/company-contract", data={"number": "7001272124"},
                                   headers=HEADERS).status_code, 415)
        self.assertEqual(client.calls, [])

    def test_wathq_errors_pass_through_with_their_code(self):
        http, _, _ = make(FakeClient(error=WathqError(MSG_NOT_FOUND, 404, "404.2.1")))
        r = lookup(http)
        self.assertEqual(r.status_code, 404)
        # The failed call may still have been billed: the count comes back too.
        self.assertEqual(r.json(), {"error": MSG_NOT_FOUND, "code": "404.2.1", "sent": 1})

    def test_wathq_wording_reaches_the_page(self):
        err = WathqError(MSG_NOT_FOUND, 404, "404.2.1", detail="No Results Found")
        http, _, _ = make(FakeClient(error=err))
        self.assertEqual(lookup(http).json()["detail"], "No Results Found")

    def test_unusable_answer_is_a_502_not_a_crash(self):
        http, _, _ = make(FakeClient(answer={"unexpected": True}))
        self.assertEqual(lookup(http).status_code, 502)

    def test_one_lookup_at_a_time(self):
        client = FakeClient()
        client.gate = threading.Event()
        http, _, _ = make(client)
        results = {}
        worker = threading.Thread(target=lambda: results.update(first=lookup(http)))
        worker.start()
        for _ in range(200):
            if client.calls:
                break
            threading.Event().wait(0.01)
        second = lookup(http, "7001272125")
        client.gate.set()
        worker.join(5)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(results["first"].status_code, 200)


class GuardTests(unittest.TestCase):
    def test_cross_site_requests_are_refused_before_any_call(self):
        http, client, _ = make()
        for headers in ({}, {"X-Wathq-Request": "0"},
                        {**HEADERS, "Sec-Fetch-Site": "cross-site"},
                        {**HEADERS, "Sec-Fetch-Site": "same-site"},
                        {**HEADERS, "Origin": "https://evil.example"},
                        {**HEADERS, "Origin": "null"}):
            self.assertEqual(lookup(http, headers=headers).status_code, 403, headers)
        self.assertEqual(client.calls, [])
        ok = {**HEADERS, "Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1"}
        self.assertEqual(lookup(http, headers=ok).status_code, 200)

    def test_a_lan_peer_cannot_forge_a_local_host_header(self):
        # The app opened to a LAN (APP_ALLOWED_HOSTS): a peer at .50 sends
        # Host: 127.0.0.1. The Host header is the client's word; the peer isn't.
        http, client, _ = make(base_url="http://127.0.0.1:8100", peer=("192.168.1.50", 40000))
        self.assertEqual(lookup(http).status_code, 403)
        self.assertEqual(http.get("/wathq/status").status_code, 403)
        self.assertEqual(client.calls, [])
        http, client, _ = make(base_url="http://192.168.1.10:8100", peer=("127.0.0.1", 40000))
        self.assertEqual(lookup(http).status_code, 403)          # local peer, LAN name
        self.assertEqual(client.calls, [])
        http, client, _ = make(peer=("::1", 40000))
        self.assertEqual(lookup(http).status_code, 200)          # IPv6 loopback is local

    def test_lan_hosts_are_refused_unless_allowed(self):
        http, client, _ = make(base_url="http://192.168.1.20", peer=("192.168.1.50", 40000))
        self.assertEqual(lookup(http).status_code, 403)
        self.assertEqual(http.get("/wathq/status").status_code, 403)
        self.assertEqual(http.get("/wathq/ui.js").status_code, 200)     # the tab still loads
        self.assertEqual(client.calls, [])
        http, client, _ = make(environ={"WATHQ_ALLOW_REMOTE": "1"}, base_url="http://192.168.1.20",
                               peer=("192.168.1.50", 40000))
        self.assertEqual(lookup(http, headers={**HEADERS, "Origin": "http://192.168.1.20"}).status_code, 200)

    def test_get_is_not_a_lookup(self):
        http, client, _ = make()
        self.assertEqual(http.get("/wathq/company-contract").status_code, 405)
        self.assertEqual(client.calls, [])


class StatusTests(unittest.TestCase):
    def test_configured(self):
        http, client, _ = make()
        client.sent = 7
        body = http.get("/wathq/status").json()
        self.assertEqual(body, {"configured": True, "env": "production", "sent": 7,
                                "cache_seconds": 900, "reason": ""})

    def test_not_configured_says_what_to_do(self):
        http, _, _ = make(FakeClient(problem=MSG_NOT_CONFIGURED))
        body = http.get("/wathq/status").json()
        self.assertFalse(body["configured"])
        self.assertEqual(body["reason"], MSG_NOT_CONFIGURED)

    def test_a_bad_ca_bundle_does_not_break_the_app(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "not-a-cert.pem")
            with open(junk, "w") as fh:
                fh.write("hello")
            for path in (os.path.join(tmp, "missing.pem"), junk, tmp):
                app = FastAPI()
                app.include_router(create_router(environ={"WATHQ_CA_BUNDLE": path}))
                http = TestClient(app, base_url="http://127.0.0.1", client=('127.0.0.1', 50000))
                body = http.get("/wathq/status").json()
                self.assertFalse(body["configured"], path)
                self.assertIn("WATHQ_CA_BUNDLE", body["reason"])
                self.assertNotIn(tmp, body["reason"])            # no local paths in the UI
                self.assertEqual(lookup(http).status_code, 503)

    def test_bad_settings_do_not_break_the_app(self):
        app = FastAPI()
        app.include_router(create_router(environ={"WATHQ_ENV": "staging"}))
        http = TestClient(app, base_url="http://127.0.0.1", client=('127.0.0.1', 50000))
        body = http.get("/wathq/status").json()
        self.assertFalse(body["configured"])
        self.assertIn("WATHQ_ENV", body["reason"])
        self.assertEqual(lookup(http).status_code, 503)
        self.assertEqual(http.get("/wathq/ui.css").status_code, 200)

    def test_real_client_without_a_key(self):
        app = FastAPI()
        app.include_router(create_router(environ={}))
        http = TestClient(app, base_url="http://127.0.0.1", client=('127.0.0.1', 50000))
        self.assertEqual(http.get("/wathq/status").json()["reason"], MSG_NOT_CONFIGURED)
        r = lookup(http)
        self.assertEqual((r.status_code, r.json()["error"]), (503, MSG_NOT_CONFIGURED))


if __name__ == "__main__":
    unittest.main()
