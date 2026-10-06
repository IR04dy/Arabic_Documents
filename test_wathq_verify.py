"""parse_number and shape_contract: pure functions, no network."""
import copy
import json
import unittest

from wathq_verify import KIND_CR, KIND_UNIFIED, mask_id, parse_number, shape_contract

# Shaped after the /info 200 example in Wathq's Company Contracts v2.8.0 spec,
# with synthetic names and numbers. Note the spec's own inconsistencies kept on
# purpose: guardian.identity.id is an int, crNumber a string, "string"
# placeholders in the boards, and a dict where a list was promised.
CONTRACT = {
    "contractCopyNumber": 1,
    "contractDate": "2023-01-24",
    "entity": {
        "crNationalNumber": "7001272124",
        "crNumber": "1023236575",
        "name": "شركة الاختبار للتجارة",
        "nameLangDesc": "اللغة العربية",
        "companyDuration": 25,
        "headquarterCityName": "الرياض",
        "isLicenseBased": False,
        "licenseIssuerName": None,
        "entityType": {"id": 1, "name": "شركة", "formId": 1, "formName": "ذات مسؤولية محدودة",
                       "characters": [{"id": 1, "name": "شخص واحد"}]},
        "capital": {
            "currencyName": "ريال سعودى",
            "contributionCapital": {"typeName": "نقدي", "cashCapital": 100000, "inKindCapital": 0,
                                    "contributionValue": 1000, "totalCashContribution": 100,
                                    "totalInKindContribution": 0},
        },
        "fiscalYear": {"isFirst": False, "calendarTypeName": "ميلادي", "endMonth": 12, "endDay": 31,
                       "endYear": 2024},
        "parties": [{
            "name": "شريك تجريبي أول",
            "typeName": "فرد سعودي",
            "identity": {"id": "1000000001", "typeId": 1, "typeName": "هوية وطنية"},
            "partnership": [{"id": 2, "name": "شريك"}],
            "partnerShare": {"cashContributionCount": 100, "inKindContributionCount": 0,
                             "totalContributionCount": 100},
            "partnerProfitLossDistribution": {"profitDistribution": 100, "lossDistribution": 100},
            "nationality": {"id": 113, "name": "السعودية"},
            "crNumber": None,
            "guardian": {"name": "ولي تجريبي", "identity": {"id": 1000000002, "typeName": "هوية وطنية"},
                         "nationality": {"name": "السعودية"}, "isFatherGuardian": True},
        }],
        "management": {
            "structureName": "مدير",
            "dismissalMethod": None,
            "managers": {  # one object where the schema promises a list
                "name": "مدير تجريبي", "typeName": "سعودى", "isLicensed": False,
                "identity": {"id": "1000000003", "typeName": "هوية وطنية"},
                "nationality": {"name": "السعودية"},
                "positions": [{"id": 1, "name": "مدير"}],
            },
            "directorsBoard": {"wayOfWork": "string"},
        },
        "activities": [{"id": "4711", "name": "البيع بالتجزئة في المتاجر غير المتخصصة"}],
    },
    "notificationChannel": [{"id": 1, "name": "رسائل نصية"}],
    "partnerDecision": [{"id": 1, "name": "زيادة رأس مال الشركة", "approvePercentage": "75",
                         "approveAdditionalText": None}],
    "additionalDecisionText": None,
    "setAsideDetails": {"isSetAsideEnabled": True, "profitAllocation": {"percentage": 10, "purpose": "احتياطي"}},
    "articles": [{"id": 23, "text": "يكون لمالك رأس المال الصلاحيات المنصوص عليها في نظام الشركات.",
                  "partId": None, "partName": None}],
    "additionalArticles": [{"title": "string", "text": "string", "partId": 0, "partName": "string"}],
}


class ParseNumberTests(unittest.TestCase):
    def test_unified_and_legacy(self):
        self.assertEqual(parse_number("7001272124"), ("7001272124", KIND_UNIFIED))
        self.assertEqual(parse_number("1023236575"), ("1023236575", KIND_CR))
        self.assertEqual(parse_number("4030123456"), ("4030123456", KIND_CR))

    def test_arabic_indic_digits_spaces_and_bidi_marks(self):
        self.assertEqual(parse_number("٧٠٠١٢٧٢١٢٤")[0], "7001272124")
        self.assertEqual(parse_number(" ۷۰۰ ۱۲۷ ۲۱۲۴ ")[0], "7001272124")
        self.assertEqual(parse_number("‏700-127-2124‎")[0], "7001272124")
        self.assertEqual(parse_number("⁧٧٠٠١٢٧٢١٢٤⁩")[0], "7001272124")

    def test_rejections_carry_arabic_messages(self):
        for bad in ("", "   ", "70012721", "70012721245", "8001272124", "0123456789",
                    "7101234567", "7999999999",           # 7 series but not 70: neither kind
                    "7001272124x", "abc", None, 7001272124):
            with self.assertRaises(ValueError, msg=repr(bad)) as ctx:
                parse_number(bad)
            self.assertRegex(str(ctx.exception), "[؀-ۿ]")


class MaskTests(unittest.TestCase):
    def test_mask_keeps_last_four(self):
        self.assertEqual(mask_id("1000000001"), "••••••0001")
        self.assertEqual(mask_id(1000000002), "••••••0002")
        self.assertEqual(mask_id("123"), "•••")
        self.assertEqual(mask_id(None), "")
        self.assertEqual(mask_id(True), "")


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.shaped = shape_contract(copy.deepcopy(CONTRACT))

    def test_entity(self):
        e = self.shaped["entity"]
        self.assertEqual(e["national_number"], "7001272124")
        self.assertEqual(e["cr_number"], "1023236575")
        self.assertEqual(e["name"], "شركة الاختبار للتجارة")
        self.assertEqual(e["legal_form"], "ذات مسؤولية محدودة")
        self.assertEqual(e["characters"], ["شخص واحد"])
        self.assertEqual(e["duration"], "25")
        self.assertIs(e["license_based"], False)
        self.assertEqual(self.shaped["contract"], {"copy_number": "1", "date": "2023-01-24"})

    def test_capital_and_fiscal_year(self):
        c = self.shaped["capital"]
        self.assertEqual(c["currency"], "ريال سعودى")
        self.assertEqual(c["contribution"]["cash"], "100000")
        self.assertEqual(c["contribution"]["in_kind"], "0")      # zero is information
        self.assertNotIn("stock", c)
        self.assertEqual(self.shaped["fiscal_year"]["end"], "2024/12/31")

    def test_people_ids_are_masked_everywhere(self):
        blob = json.dumps(self.shaped, ensure_ascii=False)
        for full in ("1000000001", "1000000002", "1000000003"):
            self.assertNotIn(full, blob)
        party = self.shaped["parties"][0]
        self.assertEqual(party["id_masked"], "••••••0001")
        self.assertEqual(party["guardian"]["id_masked"], "••••••0002")
        self.assertEqual(party["roles"], ["شريك"])
        self.assertEqual(party["total_shares"], "100")

    def test_single_object_where_a_list_was_promised(self):
        managers = self.shaped["management"]["managers"]
        self.assertEqual(len(managers), 1)
        self.assertEqual(managers[0]["name"], "مدير تجريبي")
        self.assertEqual(managers[0]["id_masked"], "••••••0003")
        self.assertEqual(managers[0]["positions"], ["مدير"])

    def test_placeholders_and_nulls_are_dropped(self):
        self.assertEqual(len(self.shaped["articles"]), 1)            # "string" article dropped
        self.assertEqual(self.shaped["management"]["dismissal"], "")
        self.assertEqual(self.shaped["decisions"][0]["approve_pct"], "75")
        self.assertEqual(self.shaped["profit_set_aside"], {"pct": "10", "purpose": "احتياطي"})

    def test_whitelist_only(self):
        blob = json.dumps(self.shaped, ensure_ascii=False)
        for raw_key in ("crNationalNumber", "identity", "typeId", "directorsBoard", "nameLangId"):
            self.assertNotIn(raw_key, blob)

    def test_every_value_is_a_string_bool_none_list_or_dict(self):
        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            else:
                self.assertTrue(v is None or isinstance(v, (str, bool)), repr(v))
        walk(self.shaped)

    def test_garbage_answers(self):
        for bad in (None, [], "x", {}, {"entity": None}, {"entity": []}):
            with self.assertRaises(ValueError):
                shape_contract(bad)
        minimal = shape_contract({"entity": {"name": "شركة"}})
        self.assertEqual(minimal["parties"], [])
        self.assertEqual(minimal["capital"], {"currency": ""})

    def test_long_text_is_capped(self):
        raw = copy.deepcopy(CONTRACT)
        raw["articles"] = [{"text": "ب" * 10000}] * 400
        shaped = shape_contract(raw)
        self.assertEqual(len(shaped["articles"]), 300)
        self.assertLessEqual(len(shaped["articles"][0]["text"]), 6001)


if __name__ == "__main__":
    unittest.main()
