"""The all-services smoke test: planning from the asked numbers, and the run against a fake client."""
import io
import unittest

from test_wathq_query import FakeClient
from wathq_catalog import get_catalog
from wathq_client import MSG_FORBIDDEN, WathqError
from wathq_smoke import DRUG_EXAMPLE, plan, run

CAT = get_catalog()


class PlanTests(unittest.TestCase):
    def test_services_with_numbers_are_sent_and_the_rest_skipped(self):
        planned = plan({"company": "١٠١٠٨٤٢٧٦٣", "wakalah": "4317608", "principal": "1023456789"}, CAT, "production")
        by = {eid: (inputs, reason) for _, eid, inputs, reason in planned}
        self.assertEqual(by["cr.info"][0], {"id": "1010842763"})
        self.assertEqual(by["national_address.info"][0], {"crNumber": "1010842763"})
        self.assertEqual(by["attorney.info"][0], {"code": "4317608", "principalId": "1023456789"})
        self.assertEqual(by["drug.status"][0], {"id": DRUG_EXAMPLE})           # public example by default
        self.assertIsNone(by["real_estate.deed"][0])
        self.assertEqual(by["real_estate.deed"][1], "no number given")
        self.assertIsNone(by["employee.info"][0])

    def test_owner_id_type_follows_the_number(self):
        planned = plan({"deed": "910203040", "owner": "2345678901"}, CAT, "production", only={"deed"})
        self.assertEqual(planned[0][2], {"deedNumber": "910203040", "idNumber": "2345678901", "idType": "Resident_ID"})

    def test_sandbox_skips_what_it_lacks(self):
        planned = plan({"company": "7001272475", "employee": "1023456789"}, CAT, "sandbox")
        by = {eid: reason for _, eid, inputs, reason in planned if inputs is None}
        self.assertEqual(by["employee.info"], "not available in the sandbox")
        self.assertNotIn("cr.info", by)


class RunTests(unittest.TestCase):
    def test_ok_and_refused_services_are_reported(self):
        client = FakeClient(answers={"/commercial-registration/info": {"crName": "x", "status": {"name": "نشط"}},
                                     "/spl/national/address": [{"title": "y"}]})
        planned = plan({"company": "1010842763"}, CAT, "production", only={"company", "address"})
        out = io.StringIO()
        rows = run(planned, client, CAT, out)
        self.assertEqual([(eid, outcome) for _, eid, outcome, _ in rows],
                         [("cr.info", "ok"), ("cr.status", "ok"), ("national_address.info", "ok")])
        self.assertEqual(sum(1 for c in client.calls if c[0] == "convert"), 1)     # the old CR converted once
        text = out.getvalue()
        self.assertIn("skeleton", text)
        self.assertNotIn("1010842763", text)                                        # no number in the output
        self.assertNotIn("7001272475", text)

    def test_a_refusal_shows_wathq_wording(self):
        client = FakeClient(error=WathqError(MSG_FORBIDDEN, 502, "403.1.1",
                                             detail="you do not have permission for giving resource"))
        rows = run(plan({"drug": ""}, CAT, "production", only={"drug"}), client, CAT, io.StringIO())
        self.assertEqual(rows[0][2], "failed")
        self.assertIn("403.1.1", rows[0][3])
        self.assertIn("Wathq says: you do not have permission", rows[0][3])


if __name__ == "__main__":
    unittest.main()
