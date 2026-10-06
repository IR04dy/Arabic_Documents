"""The suggest step: structured data in, the model's ranked services out.

No model and no Wathq: ``ask`` is a fake that answers from a script."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_wathq_api import FakeClient, Clock, HEADERS, LOCAL_PEER
from wathq_api import MSG_MODEL, create_router
from wathq_suggest import NONE, SERVICES, flatten, prefill, prompt, rank, suggest

WAKALAH = {
    "document_type": "وكالة شرعية",
    "sections": [
        {"title": "بيانات الوكالة", "fields": [
            {"label": "رقم الوكالة", "value": "٤٣٨٢١٩٠٠٥١١٢"},
            {"label": "تاريخ الوكالة", "value": "١٤٤٥/٠١/١٥هـ"},
            {"label": "حالة الوكالة", "value": "سارية"},
            {"label": "ختم كاتب العدل", "value": ""},
        ]},
        {"title": "الأطراف", "fields": [], "record_label": "الموكل", "records": [[
            {"label": "اسم الموكل", "value": "محمد بن عبدالله"},
            {"label": "رقم هوية الموكل", "value": "١٠٢٣٤٥٦٧٨٩"},
        ]]},
        {"title": "الوكلاء", "fields": [], "record_label": "الوكيل", "records": [[
            {"label": "اسم الوكيل", "value": "خالد بن سعد"},
            {"label": "رقم هوية الوكيل", "value": "1044 556 677"},
        ]]},
    ],
}
LICENCE = {"sections": [{"title": "المنشأة", "fields": [
    {"label": "الاسم التجاري", "value": "مقهى سحابة النخيل"},
    {"label": "السجل التجاري", "value": "١٠١٠٨٤٢٧٦٣"},
    {"label": "العنوان", "value": "مدينة النور، حي الندى"},
]}]}


def scripted(*answers):
    """An ``ask`` that answers from a list and records every prompt."""
    log = []

    def ask(text, options):
        log.append((text, list(options)))
        answer = answers[len(log) - 1] if len(log) <= len(answers) else NONE
        return answer
    ask.log = log
    return ask


class FlattenTests(unittest.TestCase):
    def test_fields_and_records_become_label_value_lines(self):
        pairs = flatten(WAKALAH)
        self.assertIn(("رقم الوكالة", "٤٣٨٢١٩٠٠٥١١٢"), pairs)
        self.assertIn(("الموكل 1 - رقم هوية الموكل", "١٠٢٣٤٥٦٧٨٩"), pairs)
        self.assertNotIn("ختم كاتب العدل", [k for k, _ in pairs])      # empty values are dropped

    def test_garbage_is_harmless(self):
        self.assertEqual(flatten(None), [])
        self.assertEqual(flatten({"sections": [None, {"fields": [None, {"label": 1, "value": 2}]}]}),
                         [("1", "2")])


class RankTests(unittest.TestCase):
    def test_the_order_of_answers_is_the_ranking_and_none_stops(self):
        ask = scripted("power_of_attorney", "real_estate_deed", NONE)
        self.assertEqual(rank(flatten(WAKALAH), ask), ["power_of_attorney", "real_estate_deed"])
        self.assertEqual(len(ask.log), 3)
        first, second = ask.log[0][1], ask.log[1][1]
        self.assertIn("power_of_attorney", first)
        self.assertNotIn("power_of_attorney", second)         # a chosen service is not offered again
        self.assertEqual(second[-1], NONE)

    def test_the_prompt_is_the_pairs_plus_the_menu(self):
        text = prompt(flatten(WAKALAH), SERVICES)
        self.assertIn("رقم الوكالة: ٤٣٨٢١٩٠٠٥١١٢", text)
        for s in SERVICES:
            self.assertIn(f"- {s['id']}: {s['label']} — يعيد: ", text)
        self.assertTrue(text.rstrip().endswith("أو None إن لم تنطبق أي خدمة."))

    def test_an_answer_outside_the_options_ends_the_ranking(self):
        ask = scripted("commercial_registration", "nonsense", "drug")
        self.assertEqual(rank(flatten(LICENCE), ask), ["commercial_registration"])

    def test_every_service_can_be_chosen_once_at_most(self):
        ask = scripted(*[s["id"] for s in SERVICES], NONE)
        self.assertEqual(rank(flatten(LICENCE), ask), [s["id"] for s in SERVICES])
        self.assertEqual(len(ask.log), len(SERVICES))          # nothing left to ask about


class PrefillTests(unittest.TestCase):
    def by_id(self, sid):
        return next(s for s in SERVICES if s["id"] == sid)

    def test_wakalah_keys_are_read_from_the_lines(self):
        values = prefill(flatten(WAKALAH), self.by_id("power_of_attorney"))
        self.assertEqual(values, {"code": "438219005112", "principalId": "1023456789", "agentId": "1044556677"})

    def test_company_number_from_a_licence(self):
        pairs = flatten(LICENCE)
        self.assertEqual(prefill(pairs, self.by_id("commercial_registration")), {"id": "1010842763"})
        self.assertEqual(prefill(pairs, self.by_id("national_address")), {"crNumber": "1010842763"})
        self.assertEqual(prefill(pairs, self.by_id("foreign_investor")), {})     # needs a 70… number

    def test_deed_owner_id_type_follows_the_leading_digit(self):
        pairs = [("رقم الصك", "٩١٠٢٠٣٠٤٠٥"), ("رقم هوية المالك", "2345678901")]
        self.assertEqual(prefill(pairs, self.by_id("real_estate_deed")),
                         {"deedNumber": "9102030405", "idNumber": "2345678901", "idType": "Resident_ID"})

    def test_nothing_is_invented(self):
        self.assertEqual(prefill([("ملاحظة", "وثيقة إلكترونية")], self.by_id("employee")), {})


class SuggestFunctionTests(unittest.TestCase):
    def test_result_lists_ranked_services_with_their_inputs(self):
        out = suggest(WAKALAH, scripted("power_of_attorney", NONE))
        self.assertEqual(out["pairs"], 7)
        self.assertEqual([s["id"] for s in out["services"]], ["power_of_attorney"])
        self.assertEqual(out["services"][0]["endpoint"], "attorney.info")
        self.assertEqual(out["services"][0]["inputs"]["code"], "438219005112")

    def test_an_empty_structure_asks_nothing(self):
        ask = scripted("drug")
        self.assertEqual(suggest({"sections": []}, ask)["services"], [])
        self.assertEqual(ask.log, [])


def make(ask, client=None):
    app = FastAPI()
    app.include_router(create_router(client or FakeClient(), environ={}, clock=Clock(), ask=ask))
    return TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)


class RouteTests(unittest.TestCase):
    def test_suggest_returns_the_services_with_prices_and_inputs(self):
        http = make(scripted("power_of_attorney", "commercial_registration", NONE))
        r = http.post("/wathq/suggest", json={"struct": WAKALAH}, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        ids = [s["id"] for s in body["services"]]
        self.assertEqual(ids, ["power_of_attorney", "commercial_registration"])
        poa = body["services"][0]
        self.assertEqual(poa["endpoint"], "attorney.info")
        self.assertEqual(poa["price"], 5)
        self.assertTrue(poa["available"])
        self.assertEqual([i["name"] for i in poa["inputs_spec"]], ["code", "principalId", "agentId"])
        self.assertEqual(poa["one_of"], ["principalId", "agentId"])
        self.assertEqual(poa["inputs"]["principalId"], "1023456789")
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_suggest_never_calls_wathq(self):
        client = FakeClient()
        http = make(scripted("commercial_registration", NONE), client)
        http.post("/wathq/suggest", json={"struct": LICENCE}, headers=HEADERS)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.sent, 0)

    def test_sandbox_marks_unavailable_services(self):
        http = make(scripted("power_of_attorney", NONE), FakeClient(env="sandbox"))
        body = http.post("/wathq/suggest", json={"struct": WAKALAH}, headers=HEADERS).json()
        self.assertFalse(body["services"][0]["available"])
        self.assertEqual(body["env"], "sandbox")

    def test_guards_apply(self):
        http = make(scripted("power_of_attorney", NONE))
        self.assertEqual(http.post("/wathq/suggest", json={"struct": WAKALAH}).status_code, 403)
        self.assertEqual(http.post("/wathq/suggest", json={"struct": WAKALAH},
                                   headers={**HEADERS, "Origin": "http://evil.test"}).status_code, 403)
        self.assertEqual(http.post("/wathq/suggest", content="x", headers=HEADERS).status_code, 415)
        self.assertEqual(http.post("/wathq/suggest", json={"struct": "no"}, headers=HEADERS).status_code, 400)
        self.assertEqual(http.post("/wathq/suggest", json=[1], headers=HEADERS).status_code, 400)

    def test_model_trouble_is_a_503_with_a_plain_message(self):
        def broken(text, options):
            raise RuntimeError("model is busy; please retry")
        http = make(broken)
        r = http.post("/wathq/suggest", json={"struct": WAKALAH}, headers=HEADERS)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], MSG_MODEL)

    def test_a_remote_peer_is_refused(self):
        app = FastAPI()
        app.include_router(create_router(FakeClient(), environ={}, clock=Clock(),
                                         ask=scripted("drug", NONE)))
        http = TestClient(app, base_url="http://127.0.0.1", client=("192.168.1.9", 50000))
        self.assertEqual(http.post("/wathq/suggest", json={"struct": WAKALAH}, headers=HEADERS).status_code, 403)


if __name__ == "__main__":
    unittest.main()
