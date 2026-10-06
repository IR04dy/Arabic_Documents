"""Document lines against a register answer: exact passes, model verdicts, the route."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_wathq_api import FakeClient, Clock, HEADERS, LOCAL_PEER
from test_wathq_suggest import WAKALAH, scripted
from wathq_api import MSG_COMPARE_MODEL, create_router
from wathq_compare import VERDICTS, compare, exact, register_pairs

ANSWER = {
    "view_type": "generic", "endpoint": "attorney.info",
    "view": [
        {"type": "field", "label": "رقم الوكالة", "value": "438219005112"},
        {"type": "field", "label": "حالة الوكالة", "value": "سارية"},
        {"type": "field", "label": "تاريخ الإصدار (هجري)", "value": "1445-01-15"},
        {"type": "field", "label": "جهة الإصدار", "value": "كتابة العدل الأولى"},
        {"type": "cards", "label": "الموكلون", "items": [
            {"type": "group", "label": "الموكلون (1)", "children": [
                {"type": "field", "label": "الاسم", "value": "محمد عبدالله السالم"},
                {"type": "field", "label": "رقم الهوية", "value": "••••••6789"},
            ]}]},
        {"type": "table", "label": "الوكلاء", "columns": ["الاسم", "رقم الهوية"],
         "rows": [["خالد سعد القحطاني", "••••••6677"]]},
        {"type": "list", "label": "بنود الوكالة", "items": ["بيع", "إفراغ"]},
        {"type": "field", "label": "مساحة", "value": "بيانات أعمق لم تُعرض"},
    ],
}


def judging(rows):
    """A ``judge`` that answers with the given rows and records the prompt."""
    log = []

    def judge(text, schema, max_tokens):
        log.append((text, schema, max_tokens))
        return {"rows": rows}
    judge.log = log
    return judge


class RegisterPairsTests(unittest.TestCase):
    def test_view_nodes_become_labelled_lines(self):
        pairs = register_pairs(ANSWER)
        labels = [k for k, _ in pairs]
        self.assertIn("رقم الوكالة", labels)
        self.assertIn("الموكلون (1) - الاسم", labels)
        self.assertIn("الوكلاء 1 - رقم الهوية", labels)
        self.assertIn(("بنود الوكالة", "بيع، إفراغ"), pairs)
        self.assertNotIn("مساحة", labels)                    # the "deeper data" placeholder is skipped

    def test_contract_view_uses_its_own_labels(self):
        pairs = register_pairs({"view_type": "contract", "entity": {"name": "شركة النخيل", "cr_number": "1010842763"},
                                "capital": {"total": "500000"}, "parties": [{"name": "سعد", "id": "••••1234"}]})
        self.assertIn(("اسم الشركة", "شركة النخيل"), pairs)
        self.assertIn(("رقم السجل التجاري", "1010842763"), pairs)
        self.assertIn(("رأس المال", "500000"), pairs)
        self.assertIn(("الشريك 1 - رقم الهوية", "••••1234"), pairs)


class ExactTests(unittest.TestCase):
    def test_numbers_dates_and_masked_ids_are_matched_by_code(self):
        doc = [("رقم الوكالة", "٤٣٨٢١٩٠٠٥١١٢"), ("تاريخ الوكالة", "١٤٤٥/٠١/١٥هـ"),
               ("رقم هوية الموكل", "١٠٢٣٤٥٦٧٨٩"), ("المساحة", "٢٤٠ م٢")]
        reg = [("رقم الوكالة", "438219005112"), ("تاريخ الإصدار", "15-01-1445"), ("رقم الهوية", "••••••6789"),
               ("رقم الهوية", "••••••0000"), ("المساحة", "240.0"), ("حالة الوكالة", "سارية"), ("رقم آخر", "999")]
        v = exact(doc, reg)
        self.assertEqual(v[0], "matches")                    # digits folded
        self.assertEqual(v[1], "matches")                    # d-m-y against y/m/d
        self.assertEqual(v[2], "matches")                    # masked tail
        self.assertEqual(v[3], "not_compared")               # masked tail nowhere in the document
        self.assertEqual(v[4], "matches")                    # 240.0 vs ٢٤٠
        self.assertNotIn(5, v)                               # words are left to the model
        self.assertNotIn(6, v)                               # an unmatched number is left to the model


class CompareTests(unittest.TestCase):
    def test_code_and_model_verdicts_combine(self):
        # the model rules on the pending (non-numeric) register lines
        # document lines: d1 رقم الوكالة, d2 تاريخ, d3 حالة, d4 اسم الموكل, d5 هوية الموكل, d6 اسم الوكيل, d7 هوية الوكيل
        judge = judging([{"register": "r2", "document": "d3", "verdict": "matches"},
                         {"register": "r4", "document": "", "verdict": "not_in_document"},
                         {"register": "r5", "document": "d4", "verdict": "partly_matches"}])
        out = compare(WAKALAH, ANSWER, judge)
        by_label = {r["label"]: r for r in out["rows"]}
        self.assertEqual(by_label["رقم الوكالة"]["verdict"], "matches")
        self.assertEqual(by_label["رقم الوكالة"]["by"], "code")
        self.assertEqual(by_label["رقم الوكالة"]["document"], "٤٣٨٢١٩٠٠٥١١٢")
        self.assertEqual(by_label["الموكلون (1) - رقم الهوية"]["verdict"], "matches")   # masked tail
        self.assertEqual(by_label["حالة الوكالة"]["verdict"], "matches")
        self.assertEqual(by_label["حالة الوكالة"]["by"], "model")
        self.assertEqual(by_label["حالة الوكالة"]["document"], "سارية")
        self.assertEqual(by_label["جهة الإصدار"]["verdict"], "not_in_document")
        self.assertEqual(by_label["بنود الوكالة"]["verdict"], "not_compared")      # the model said nothing
        self.assertEqual(by_label["الموكلون (1) - الاسم"]["verdict"], "partly_matches")
        self.assertEqual(by_label["الموكلون (1) - الاسم"]["document"], "محمد بن عبدالله")
        self.assertEqual(out["counts"]["matches"], 5)                              # number, date, 2 masked IDs, status
        self.assertTrue(all(r["verdict_ar"] == VERDICTS[r["verdict"]] for r in out["rows"]))
        # the model saw only the pending lines, numbered as the schema allows
        text, schema, _ = judge.log[0]
        self.assertIn("r2: حالة الوكالة: سارية", text)
        self.assertNotIn("r1:", text)
        self.assertEqual(schema["properties"]["rows"]["items"]["properties"]["register"]["enum"][0], "r2")

    def test_bad_model_rows_are_ignored(self):
        judge = judging([{"register": "r1", "document": "d1", "verdict": "differs"},      # r1 was decided by code
                         {"register": "r2", "document": "d99", "verdict": "matches"},     # no such document line
                         {"register": "r5", "document": "", "verdict": "differs"},        # a verdict needs a counterpart
                         "junk"])
        out = compare(WAKALAH, ANSWER, judge)
        by_label = {r["label"]: r for r in out["rows"]}
        self.assertEqual(by_label["رقم الوكالة"]["verdict"], "matches")
        self.assertEqual(by_label["حالة الوكالة"]["verdict"], "not_compared")
        self.assertEqual(by_label["بنود الوكالة"]["verdict"], "not_compared")

    def test_numbers_linked_by_the_model_are_still_compared_by_code(self):
        answer = {"view_type": "generic", "view": [
            {"type": "field", "label": "رقم الوكالة", "value": "7"},            # too short for the exact pass
            {"type": "field", "label": "تاريخ الإصدار", "value": "1445-01-15"},
            {"type": "field", "label": "حالة الوكالة", "value": "سارية"}]}
        judge = judging([{"register": "r1", "document": "d1", "verdict": "matches"},   # the model is wrong here
                         {"register": "r3", "document": "d3", "verdict": "matches"}])
        out = compare(WAKALAH, answer, judge)
        by_label = {r["label"]: r for r in out["rows"]}
        self.assertEqual((by_label["رقم الوكالة"]["verdict"], by_label["رقم الوكالة"]["by"]), ("differs", "code"))
        self.assertEqual((by_label["تاريخ الإصدار"]["verdict"], by_label["تاريخ الإصدار"]["by"]), ("matches", "code"))
        self.assertEqual((by_label["حالة الوكالة"]["verdict"], by_label["حالة الوكالة"]["by"]), ("matches", "model"))

    def test_an_empty_document_asks_nothing(self):
        judge = judging([])
        out = compare({"sections": []}, ANSWER, judge)
        self.assertEqual(judge.log, [])
        self.assertTrue(all(r["verdict"] in ("not_in_document", "not_compared") for r in out["rows"]))


def make(judge, client=None):
    app = FastAPI()
    app.include_router(create_router(client or FakeClient(), environ={}, clock=Clock(),
                                     ask=scripted("None"), judge=judge))
    return TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)


class RouteTests(unittest.TestCase):
    def test_compare_returns_rows_and_never_calls_wathq(self):
        client = FakeClient()
        http = make(judging([{"register": "r2", "document": "d3", "verdict": "matches"}]), client)
        r = http.post("/wathq/compare", json={"struct": WAKALAH, "result": ANSWER}, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["rows"][1]["verdict_ar"], "يطابق")
        self.assertEqual(client.calls, [])
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_guards_and_bad_bodies(self):
        http = make(judging([]))
        self.assertEqual(http.post("/wathq/compare", json={"struct": WAKALAH, "result": ANSWER}).status_code, 403)
        self.assertEqual(http.post("/wathq/compare", json={"struct": WAKALAH}, headers=HEADERS).status_code, 400)
        self.assertEqual(http.post("/wathq/compare", json={"struct": 1, "result": ANSWER}, headers=HEADERS).status_code, 400)
        self.assertEqual(http.post("/wathq/compare", content="x", headers=HEADERS).status_code, 415)

    def test_model_trouble_is_a_503(self):
        def broken(text, schema, max_tokens):
            raise RuntimeError("model is busy; please retry")
        r = make(broken).post("/wathq/compare", json={"struct": WAKALAH, "result": ANSWER}, headers=HEADERS)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], MSG_COMPARE_MODEL)


if __name__ == "__main__":
    unittest.main()
