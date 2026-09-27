import copy
import json
import os
import tempfile
import unittest

import yaml

from complaints_taxonomy import (DEFAULT_PATH, Item, Taxonomy, TaxonomyError,
                                 get_taxonomy, load_taxonomy)

RAW = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))


def mutated(change):
    raw = copy.deepcopy(RAW)
    change(raw)
    return raw


def find(raw, kind, id):
    return next(e for e in raw[kind] if e["id"] == id)


class DefaultTaxonomyTests(unittest.TestCase):
    def setUp(self):
        self.tax = get_taxonomy()

    def test_default_file_loads_cached_with_expected_sizes(self):
        self.assertIs(get_taxonomy(), self.tax)
        self.assertEqual(self.tax.version, 1)
        sizes = {k: len(self.tax.ids(k)) for k in (
            "priorities", "ministries", "categories", "regions", "governorates", "statuses",
            "factors", "scopes", "tones", "signals", "review_reasons")}
        self.assertEqual(sizes, {"priorities": 4, "ministries": 23, "categories": 20,
                                 "regions": 14, "governorates": 22, "statuses": 5, "factors": 10,
                                 "scopes": 4, "tones": 3, "signals": 5, "review_reasons": 15})

    def test_receiving_entity_is_the_riyadh_emirate(self):
        ent = self.tax.receiving_entity
        self.assertIsInstance(ent, Item)
        self.assertEqual((ent.id, ent.label_ar, ent.label_en),
                         ("riyadh_emirate", "إمارة منطقة الرياض", "Riyadh Region Principality"))
        self.assertEqual(ent.extra, {"region": "riyadh", "desk_ar": "إدارة الشكاوى"})
        self.assertIn("خارج نطاق منطقة الرياض", self.tax.review_reasons["outside_jurisdiction"])
        # a letter to one of its governorates counts as addressed to it (policy, 2026-09-25)
        self.assertEqual(self.tax.review_reasons["addressed_elsewhere"],
                         "الخطاب موجّه إلى جهة غير إمارة منطقة الرياض ومحافظاتها")

    def test_priorities_are_highest_first_with_spec_slas(self):
        self.assertEqual(self.tax.ids("priorities"), ["critical", "high", "medium", "low"])
        self.assertEqual([self.tax.priority_rank(p) for p in self.tax.ids("priorities")], [4, 3, 2, 1])
        self.assertEqual([self.tax.sla_hours(p) for p in self.tax.ids("priorities")], [24, 72, 168, 336])
        self.assertEqual(self.tax.label("priorities", "critical"), "حرجة")

    def test_labels_are_verbatim_and_english_names_official(self):
        self.assertEqual(self.tax.label("ministries", "mewa"), "وزارة البيئة والمياه والزراعة")
        self.assertEqual(self.tax.get("ministries", "municipal").label_en,
                         "Ministry of Municipalities and Housing")
        self.assertEqual(self.tax.get("ministries", "hrsd").label_en,
                         "Ministry of Human Resources and Social Development")
        self.assertEqual(self.tax.label("categories", "water_sewage"), "المياه والصرف الصحي")
        self.assertEqual(self.tax.label("statuses", "referred"), "محالة للجهة المختصة")
        self.assertEqual(self.tax.review_reasons["category_other"], "لم يُحدَّد تصنيف واضح")
        for m in self.tax.ministries:
            self.assertTrue(m.label_en, m.id)

    def test_every_category_routes_somewhere_and_is_described(self):
        for cat in self.tax.categories:
            self.assertIn(cat.extra["ministry"], self.tax.ids("ministries"))
            self.assertGreater(len(cat.extra["description_ar"]), 30, cat.id)
            self.assertTrue(self.tax.subcategory_ids(cat.id), cat.id)
        subs = self.tax.subcategory_ids()
        self.assertEqual(len(subs), len(set(subs)))
        self.assertEqual(len(subs), sum(len(c.extra["subcategories"]) for c in self.tax.categories))

    def test_injection_and_unsupported_critical_review_reasons(self):
        self.assertIn("تخاطب النظام", self.tax.review_reasons["suspected_instructions"])
        self.assertIn("حرجة", self.tax.review_reasons["critical_unsupported"])
        for reason in ("suspected_instructions", "critical_unsupported"):
            with self.assertRaises(TaxonomyError) as cm:
                Taxonomy.from_dict(mutated(lambda r: r["review_reasons"].pop(reason)))
            self.assertIn(reason, str(cm.exception))

    def test_unmatched_fields_review_reason(self):
        # A field the text does not carry verbatim waits for a reviewer to
        # accept or change it (change request §1); listed after evidence_unverified.
        self.assertEqual(self.tax.review_reasons["fields_unverified"],
                         "حقول لم تُطابق نص المستند حرفياً وتنتظر قبول المراجع أو تعديله")
        ids = self.tax.ids("review_reasons")
        self.assertEqual(ids[ids.index("evidence_unverified") + 1], "fields_unverified")
        with self.assertRaises(TaxonomyError) as cm:
            Taxonomy.from_dict(mutated(lambda r: r["review_reasons"].pop("fields_unverified")))
        self.assertIn("fields_unverified", str(cm.exception))

    def test_descriptions_draw_the_disputed_lines(self):
        # Orchestrator decisions d and e: the category descriptions are what
        # the model routes by, so each disputed line is written into both sides.
        desc = {c.id: c.extra["description_ar"] for c in self.tax.categories}
        for platform, owner in (("أبشر", "الداخلية"), ("ناجز", "العدل"), ("صحتي وموعد", "الصحة"),
                                ("مدرستي ونور", "التعليم"),
                                ("قوى ومساند", "الموارد البشرية والتنمية الاجتماعية"),
                                ("بلدي وسكني", "البلديات والإسكان"), ("اعتماد", "المالية")):
            self.assertIn(f"{platform} ({owner})", desc["digital_services"])
        self.assertEqual(self.tax.default_ministry("digital_services"), "other")
        for word in ("الفنادق", "الشقق المفروشة", "الشاليهات", "المنتجعات", "حجوزات الإيواء"):
            self.assertIn(word, desc["tourism"])
        self.assertIn("حماية المستهلك", desc["tourism"])
        self.assertIn("الشاليهات", desc["consumer_protection"])
        self.assertIn("تتبع السياحة", desc["consumer_protection"])
        for word in ("التفحيط", "القيادة المتهورة", "الداخلية لا البلديات"):
            self.assertIn(word, desc["security_safety"])
        self.assertIn("التفحيط", desc["municipal_services"])
        self.assertIn("(الأمن والسلامة)", desc["municipal_services"])
        self.assertIn("والطرق بين المدن (النقل والطرق)", desc["municipal_services"])
        self.assertIn("التفحيط تتبع الأمن", desc["transport"])

    def test_signals_only_raise_and_never_to_the_top(self):
        floors = {s.id: s.extra["floor"] for s in self.tax.signals}
        self.assertEqual(floors, {"life_safety": "high", "health_risk": "medium",
                                  "vulnerable_person": "medium", "service_outage": "medium",
                                  "repeated_unresolved": "medium"})
        self.assertIn("تسرب غاز", self.tax.get("signals", "life_safety").extra["patterns"])
        self.assertIn("تقدمت سابقا", self.tax.get("signals", "repeated_unresolved").extra["patterns"])

    def test_statuses_open_flags(self):
        self.assertEqual({s.id: s.extra["open"] for s in self.tax.statuses},
                         {"new": True, "in_review": True, "referred": True,
                          "resolved": False, "rejected": False})


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tax = get_taxonomy()

    def test_get_and_label_for_known_unknown_and_non_string_ids(self):
        self.assertIsInstance(self.tax.get("tones", "angry"), Item)
        self.assertIsNone(self.tax.get("tones", "furious"))
        self.assertIsNone(self.tax.get("tones", None))
        self.assertEqual(self.tax.label("tones", "furious"), "")
        self.assertEqual(self.tax.label("scopes", None), "")
        self.assertEqual(self.tax.label("review_reasons", "empty_text"), "لا يوجد نص مقروء كافٍ")
        self.assertIn("ministry_mismatch", self.tax.ids("review_reasons"))

    def test_unknown_kind_is_a_programming_error(self):
        for call in (lambda: self.tax.ids("colours"), lambda: self.tax.get("colours", "x"),
                     lambda: self.tax.get("colours", None)):
            with self.assertRaises(KeyError):
                call()

    def test_subcategory_lookups(self):
        self.assertEqual(self.tax.subcategory_ids("housing"),
                         ["housing_support", "rental_disputes", "developer_delays"])
        self.assertEqual(self.tax.subcategory_ids("no_such_category"), [])
        self.assertEqual(self.tax.subcategory_label("sewage_overflow"), "طفح الصرف الصحي")
        self.assertEqual(self.tax.subcategory_label("nope"), "")
        self.assertEqual(self.tax.subcategory_label(None), "")
        self.assertEqual(self.tax.category_of_subcategory("sewage_overflow"), "water_sewage")
        self.assertEqual(self.tax.category_of_subcategory("other_general"), "other")
        self.assertIsNone(self.tax.category_of_subcategory("nope"))
        self.assertIsNone(self.tax.category_of_subcategory(None))

    def test_default_ministry_rank_and_sla(self):
        self.assertEqual(self.tax.default_ministry("water_sewage"), "mewa")
        self.assertEqual(self.tax.default_ministry("housing"), "municipal")
        self.assertEqual(self.tax.default_ministry("digital_services"), "other")
        self.assertEqual(self.tax.default_ministry("no_such_category"), "other")
        self.assertEqual(self.tax.default_ministry(None), "other")
        self.assertEqual(self.tax.priority_rank("urgent"), 0)
        self.assertEqual(self.tax.priority_rank(None), 0)
        with self.assertRaises(KeyError):
            self.tax.sla_hours("urgent")

    def test_items_are_hashable_and_shared_lists_immutable(self):
        self.assertEqual(len({self.tax.get("regions", "riyadh"), self.tax.get("regions", "riyadh")}), 1)
        self.assertIsInstance(self.tax.get("regions", "riyadh").extra["cities"], tuple)
        self.assertIsInstance(self.tax.get("regions", "riyadh").extra["places_en"], tuple)
        self.assertIsInstance(self.tax.get("signals", "health_risk").extra["patterns"], tuple)
        self.assertIsInstance(self.tax.get("categories", "labor").extra["subcategories"], tuple)


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.tax = get_taxonomy()
        self.r = self.tax.region_for_city

    def test_every_listed_city_maps_to_its_region(self):
        for region in self.tax.regions:
            for city in region.extra["cities"]:
                self.assertEqual(self.r(city), region.id, city)
                self.assertEqual(self.r(f"مقدم الشكوى من {city}"), region.id if city != "المدينة" else None, city)

    def test_normalised_spellings_match(self):
        cases = {"جده": "makkah", "جدة": "makkah", "جِدَّة": "makkah", "ابها": "asir",
                 "الاحساء": "eastern", "راس تنوره": "eastern", "الريـــاض": "riyadh",
                 "مكه المكرمه": "makkah", "المدينه المنوره": "madinah", "ضبا": None,
                 "أبها": "asir",          # alef + combining hamza
                 "حفر   الباطن": "eastern", "‏الدمام‏": "eastern"}
        for text, region in cases.items():
            self.assertEqual(self.r(text), region, text)

    def test_attached_prepositions_and_conjunctions(self):
        for text, region in {"حي النسيم بالرياض": "riyadh", "للرياض": "riyadh", "وجدة": "makkah",
                             "لمكة": "makkah", "بحي الريان ببريدة": "qassim",
                             "فالخبر": "eastern", "بالمدينة المنورة": "madinah",
                             "وبالطائف": "makkah"}.items():
            self.assertEqual(self.r(text), region, text)

    def test_whole_words_only(self):
        for text in ("وصلتني الرسالة أمس", "الرسوم الدراسية", "الخبراء", "البريد السعودي",
                     "جدتي مريضة", "التبوكي", "القصيمي", "الجوفية", "واحد",
                     "رائحة سمكة فاسدة", "محقل الشارع"):    # سمكة contains مكة, محقل contains حقل
            self.assertIsNone(self.r(text), text)

    def test_longest_city_wins_then_earliest(self):
        self.assertEqual(self.r("مكة المكرمة"), "makkah")
        self.assertEqual(self.r("انتقلت من الرس إلى المدينة المنورة"), "madinah")
        self.assertEqual(self.r("أعمل في الخبر وأسكن الرياض"), "riyadh")      # 6 letters beat 5
        self.assertEqual(self.r("تبوك ثم حائل"), "tabuk")                       # equal length: first
        self.assertEqual(self.r("حائل ثم تبوك"), "hail")

    def test_bare_madinah_is_ambiguous_in_running_text(self):
        for text, region in {
            "المدينة": "madinah", "  المدينة. ": "madinah", "بالمدينة": "madinah",
            "المدينة، حي قباء": "madinah", "المدينة\nحي قباء": "madinah",
            "المدينة حي العزيزية": "madinah",          # city + " " + district, as the pipeline joins them
            "المدينة الصناعية الثانية": None, "المدينة الجامعية": None, "في المدينة": None,
            "سكان المدينة يعانون": None, "المدينة الجامعية بالرياض": "riyadh",
            "المدينة: الرياض": "riyadh",               # a field label never beats a real city
            "المدينة - الخبر": "eastern",
            "المدينة المنورة، حي قباء": "madinah",
        }.items():
            self.assertEqual(self.r(text), region, text)

    def test_streets_and_person_names_are_not_locations(self):
        self.assertIsNone(self.r("طريق مكة المكرمة"))
        self.assertEqual(self.r("طريق مكة المكرمة، الرياض"), "riyadh")
        self.assertIsNone(self.r("شارع الأمير بدر"))
        self.assertIsNone(self.r("خالد بن بدر"))
        self.assertEqual(self.r("بدر"), "madinah")
        self.assertEqual(self.r("شارع الملك فهد، جدة"), "makkah")
        self.assertEqual(self.r("طريق الملك فهد بالدمام"), "eastern")

    def test_region_names_are_a_fallback(self):
        self.assertEqual(self.r("منطقة القصيم"), "qassim")
        self.assertEqual(self.r("بالقصيم"), "qassim")
        self.assertEqual(self.r("المنطقة الشرقية"), "eastern")
        self.assertEqual(self.r("منطقة الحدود الشمالية"), "northern_borders")
        self.assertIsNone(self.r("الجهة الشرقية من المبنى"))
        self.assertIsNone(self.r("غير محدد"))                  # the unknown region is never matched
        self.assertEqual(self.r("منطقة القصيم - محافظة الخبر"), "eastern")  # a city beats a region name

    def test_multi_line_address_block(self):
        block = "مقدم الشكوى: سالم\nالمدينة: جدة\nالحي: الصفا"
        self.assertEqual(self.r(block), "makkah")

    def test_nothing_found(self):
        for text in ("", "   ", None, "Al Wurud district", "١٢٣٤", 5, ["الرياض"]):
            self.assertIsNone(self.r(text), repr(text))

    def test_latin_names_match_as_whole_words_with_or_without_the_article(self):
        # English e-mails name places in Latin script only (heldout v1 h09,
        # heldout_v2 v09): the lookups must know those names too.
        for text, region in {
            "Riyadh": "riyadh", "RIYADH": "riyadh", "I live in Riyadh.": "riyadh",
            "Jeddah": "makkah", "jeddah, Al Safa district": "makkah", "Mecca": "makkah",
            "Khobar": "eastern", "Al Khobar": "eastern", "Al-Khobar": "eastern", "Alkhobar": "eastern",
            "Hafar Al-Batin": "eastern", "Hafar Albatin": "eastern", "Hafar Batin": "eastern",
            "Buraidah": "qassim", "Ar Rass": "qassim", "Ar-Rass": "qassim",
            "AlUla": "madinah", "Al Ula": "madinah", "Al-Ula": "madinah", "Medina": "madinah",
            "Khamis Mushait": "asir", "Ha'il": "hail",
            "Makkah Region": "makkah", "Al-Qassim Region": "qassim", "Qassim Region": "qassim",
            "Eastern Province": "eastern", "Riyadh Province": "riyadh",
            "Shaqra": "riyadh", "Al Kharj": "riyadh",            # the entity's governorates
            "Taif, then Riyadh": "riyadh",                        # longest name, as in Arabic
        }.items():
            self.assertEqual(self.r(text), region, text)
        for text in ("Riyadhi food", "Jeddahs", "a hail storm", "Hail", "the eastern side",
                     "Makkah Road", "Dammam Highway", "Mohammed bin Najran", "Prince Tabuk",
                     "riyadh.fan@example.com", "fan@jeddah.example", "www.jeddah.example",
                     "abha2020"):
            self.assertIsNone(self.r(text), text)
        self.assertEqual(self.r("Makkah Road, Riyadh"), "riyadh")           # a road in Riyadh
        self.assertEqual(self.r("مقدم الشكوى من جدة، Riyadh"), "riyadh")    # both scripts, longest

    def test_every_listed_latin_name_maps_to_its_region(self):
        home = self.tax.receiving_entity.extra["region"]
        for region in self.tax.regions:
            for name in (*region.extra["places_en"], region.label_en):
                if region.id != "unknown":
                    self.assertEqual(self.r(name), region.id, name)
                    self.assertEqual(self.r(f"Sent from {name}, thank you"), region.id, name)
        for gov in self.tax.governorates:
            for name in gov.extra["places_en"]:
                self.assertEqual(self.r(name), home, name)
        self.assertIsNone(self.r("Unknown"))


class GovernorateTests(unittest.TestCase):
    def setUp(self):
        self.tax = get_taxonomy()
        self.g = self.tax.governorate_for_place

    def test_governorates_of_the_entity_region(self):
        ids = self.tax.ids("governorates")
        self.assertEqual(ids[:3], ["riyadh_city", "diriyah", "kharj"])
        self.assertEqual(ids[-1], "unknown")
        self.assertEqual(len(ids), 22)                   # 20 governorates + the city + unknown
        self.assertEqual(self.tax.label("governorates", "wadi_dawasir"), "وادي الدواسر")
        self.assertEqual(self.tax.get("governorates", "kharj").label_en, "Al-Kharj")
        self.assertEqual(self.tax.get("governorates", "unknown").extra["places"], ())
        self.assertIsInstance(self.tax.get("governorates", "kharj").extra["places"], tuple)

    def test_every_place_maps_to_its_governorate_and_the_entity_region(self):
        home = self.tax.receiving_entity.extra["region"]
        for gov in self.tax.governorates:
            if gov.id == "unknown":
                continue
            self.assertIn(gov.label_ar, gov.extra["places"])        # its own name is a place
            for place in gov.extra["places"]:
                self.assertEqual(self.g(place), gov.id, place)
                self.assertEqual(self.g(f"مقدم الشكوى من {place}"), gov.id, place)
                self.assertEqual(self.tax.region_for_city(place), home, place)

    def test_riyadh_city_versus_the_region(self):
        for text, gov in {"الرياض": "riyadh_city", "مدينة الرياض": "riyadh_city",
                          "بمدينة الرياض": "riyadh_city", "حي النسيم بالرياض": "riyadh_city",
                          "الرياض، منطقة الرياض": "riyadh_city",
                          "منطقة الرياض": None, "بمنطقة الرياض": None, "ومنطقة الرياض": None,
                          "إمارة منطقة الرياض": None, "أمير الرياض": None,
                          "حي النسيم، منطقة الرياض": None}.items():
            self.assertEqual(self.g(text), gov, text)
        # the region lookup still sees the region in «منطقة الرياض»
        self.assertEqual(self.tax.region_for_city("منطقة الرياض"), "riyadh")

    def test_whole_words_prefixes_and_longest_match(self):
        for text, gov in {"الخارج": None, "من الخارج": None, "الخرج": "kharj", "بالخرج": "kharj",
                          "للخرج": "kharj", "والدلم": "kharj", "فالسيح": "kharj",
                          "محافظة الخرج، حي الخزامى": "kharj", "حي الفيصلية بالمجمعة": "majmaah",
                          "حوطة سدير": "majmaah", "حوطة بني تميم": "hotat_bani_tamim",
                          "الجبيلة": "diriyah", "الجبيل": None,             # an eastern city, not الجبيلة
                          "الدرعيه": "diriyah", "الأفلاج": "aflaj", "الافلاج": "aflaj",
                          "حي العودة، الدرعية": "diriyah", "جدة": None,
                          "المجمعة ثم الزلفي": "majmaah", "الزلفي ثم المجمعة": "majmaah",  # longer wins
                          "عفيف ثم ضرما": "afif", "ضرما ثم عفيف": "dhurma"}.items():  # equal: first
            self.assertEqual(self.g(text), gov, text)

    def test_roads_and_names_are_not_places(self):
        self.assertIsNone(self.g("طريق الخرج"))
        self.assertEqual(self.g("طريق الخرج، الرياض"), "riyadh_city")
        self.assertIsNone(self.g("شارع الأمير ثادق"))

    def test_nothing_found(self):
        for text in ("", "   ", None, "Al Wurud district", "غير محدد", "Unknown", 5, ["الرياض"]):
            self.assertIsNone(self.g(text), repr(text))

    def test_latin_place_names(self):
        # heldout_v2 v09 writes its place only as «Shaqra, Al Wurud district».
        for text, gov in {
            "Shaqra, Al Wurud district": "shaqra", "SHAQRA": "shaqra", "Shaqraa": "shaqra",
            "Riyadh": "riyadh_city", "Riyadh City": "riyadh_city", "Ar-Riyadh": "riyadh_city",
            "Al Nakheel district, Riyadh.": "riyadh_city",
            "Al-Kharj": "kharj", "Al Kharj": "kharj", "al kharj": "kharj", "Alkharj": "kharj",
            "Kharj": "kharj", "Dilam": "kharj",
            "Wadi Al-Dawasir": "wadi_dawasir", "Wadi Dawasir": "wadi_dawasir",
            "Wadi Ad-Dawasir": "wadi_dawasir", "Khamasin": "wadi_dawasir",
            "Diriyah": "diriyah", "Dir'iyah": "diriyah", "Dir’iyah": "diriyah", "Uyaynah": "diriyah",
            "Az-Zulfi": "zulfi", "Az Zulfi": "zulfi", "Zulfi": "zulfi", "As-Sulayyil": "sulayyil",
            "Hotat Bani Tamim": "hotat_bani_tamim", "Hawtat Bani Tamim": "hotat_bani_tamim",
            "Tumair, Al Majmaah": "majmaah",
            "Kharj Road, Shaqra": "shaqra",                       # the road is in Riyadh; Shaqra is the place
        }.items():
            self.assertEqual(self.g(text), gov, text)
        for text in ("Riyadh Region", "Riyadh Province", "Emirate of Riyadh", "Riyadh Emirate",
                     "Prince of the Riyadh Region", "Principality of Riyadh",    # the region, not the city
                     "Kharj Road", "Kharj Rd.", "Shaqra Street",                  # roads
                     "Prince Shaqra", "Faisal bin Zulfi",                         # people
                     "kharj.gov.sa", "shaqra@example.com", "Shaqrawi",
                     "Jeddah", "Jubail"):                                         # Jubail is not Jubailah
            self.assertIsNone(self.g(text), text)

    def test_every_latin_name_maps_to_its_governorate(self):
        for gov in self.tax.governorates:
            if gov.id == "unknown":
                self.assertEqual(gov.extra["places_en"], ())
                continue
            self.assertEqual(self.g(gov.label_en), gov.id)                # its own name is a place
            for name in gov.extra["places_en"]:
                self.assertEqual(self.g(name), gov.id, name)
                self.assertEqual(self.g(f"Complainant from {name}"), gov.id, name)
        self.assertIsInstance(self.tax.get("governorates", "shaqra").extra["places_en"], tuple)
        # spellings that differ only by the article or a hyphen are one name, kept once
        self.assertEqual(self.tax.get("governorates", "kharj").extra["places_en"], ("Al Kharj", "Dilam"))

    def test_every_governorate_and_region_in_a_text(self):
        # The pipeline accepts a governorate or region the model inferred only
        # when the text names it (review F2), so it needs all of them, not one.
        gi, ri = self.tax.governorates_in, self.tax.regions_in
        self.assertEqual(gi("من الخرج إلى الدرعية مروراً بالرياض"), {"kharj", "diriyah", "riyadh_city"})
        for text in ("منطقة الرياض", "إمارة منطقة الرياض", "أمير الرياض", "طريق الخرج", "من الخارج",
                     "", None, 5):
            self.assertEqual(gi(text), set(), repr(text))
        self.assertEqual(ri("أسكن الرياض وأعمل في جدة"), {"riyadh", "makkah"})
        self.assertEqual(ri("طريق مكة"), set())
        self.assertEqual(ri("سكان المدينة يعانون"), set())            # bare «المدينة» in running text
        self.assertEqual(ri("المدينة"), {"madinah"})
        self.assertEqual(ri("منطقة القصيم والمنطقة الشرقية"), {"qassim", "eastern"})
        self.assertEqual(ri(None), set())
        # Latin-script names, under the same rules
        self.assertEqual(gi("From Al Kharj to Diriyah via Riyadh"), {"kharj", "diriyah", "riyadh_city"})
        for text in ("Riyadh Region", "Emirate of Riyadh", "Kharj Road", "info@shaqra.example"):
            self.assertEqual(gi(text), set(), text)
        self.assertEqual(ri("I live in Riyadh and work in Jeddah"), {"riyadh", "makkah"})
        self.assertEqual(ri("Shaqra, Al Wurud district"), {"riyadh"})
        self.assertEqual(ri("Makkah Road"), set())
        self.assertEqual(ri("Qassim Region and Eastern Province"), {"qassim", "eastern"})
        # agree with the single lookups
        for text in ("حي النسيم بالرياض", "الخرج – حي الخالدية", "بريدة", "المدينة المنورة",
                     "Shaqra, Al Wurud district", "Al-Kharj", "Buraidah", "Riyadh Region"):
            if self.g(text):
                self.assertIn(self.g(text), gi(text))
            self.assertIn(self.tax.region_for_city(text), ri(text))

    def test_place_names_inside_a_value(self):
        # detect_signals exempts the place NAMES of a complaint's own place
        # fields, not the whole value (review F6).
        self.assertEqual(self.tax.places_in("محافظة الحريق"), ["الحريق"])
        self.assertEqual(self.tax.places_in("حي الخالدية، الخرج"), ["الخرج"])
        self.assertEqual(self.tax.places_in("بالدلم ثم جدة"), ["الدلم", "جدة"])
        self.assertEqual(self.tax.places_in("المدينة"), [])                # «the city», never listed
        self.assertEqual(self.tax.places_in("المدينة المنورة"), ["المدينة المنورة"])
        for text in ("", None, "الخارج"):
            self.assertEqual(self.tax.places_in(text), [], repr(text))


class PublicShapeTests(unittest.TestCase):
    def test_to_public_shape(self):
        tax = get_taxonomy()
        pub = tax.to_public()
        self.assertEqual(list(pub), ["version", "receiving_entity", "priorities", "ministries",
                                     "categories", "regions", "governorates", "statuses", "factors",
                                     "scopes", "tones", "signals", "review_reasons"])
        self.assertEqual(pub["receiving_entity"], {"id": "riyadh_emirate", "label_ar": "إمارة منطقة الرياض",
                                                   "label_en": "Riyadh Region Principality",
                                                   "region": "riyadh", "desk_ar": "إدارة الشكاوى"})
        self.assertEqual(pub["governorates"][0], {"id": "riyadh_city", "label_ar": "مدينة الرياض",
                                                  "label_en": "Riyadh City"})    # no place lists
        self.assertEqual(pub["governorates"][-1]["id"], "unknown")
        self.assertEqual(set(pub["priorities"][0]),
                         {"id", "label_ar", "label_en", "rank", "sla_hours", "description_ar"})
        self.assertEqual(set(pub["ministries"][0]), {"id", "label_ar", "label_en"})
        cat = pub["categories"][0]
        self.assertEqual(set(cat), {"id", "label_ar", "label_en", "ministry", "description_ar",
                                    "subcategories"})
        self.assertEqual(cat["subcategories"][0], {"id": "medical_error", "label_ar": "خطأ طبي"})
        self.assertEqual(set(pub["regions"][0]), {"id", "label_ar", "label_en"})   # no city lists
        self.assertEqual(pub["statuses"][3], {"id": "resolved", "label_ar": "مغلقة",
                                              "label_en": "Closed", "open": False})
        self.assertEqual(set(pub["factors"][0]), {"id", "label_ar", "label_en"})
        self.assertEqual(pub["signals"][0], {"id": "life_safety", "label_ar": "خطر على الحياة أو السلامة",
                                             "floor": "high"})       # no patterns
        self.assertEqual(pub["review_reasons"], tax.review_reasons)
        self.assertEqual(json.loads(json.dumps(pub, ensure_ascii=False)), pub)

    def test_to_public_returns_fresh_copies(self):
        tax = get_taxonomy()
        pub = tax.to_public()
        pub["categories"][0]["subcategories"].clear()
        pub["review_reasons"].clear()
        self.assertTrue(tax.subcategory_ids("health_services"))
        self.assertTrue(tax.to_public()["review_reasons"])


class ValidationTests(unittest.TestCase):
    def assertInvalid(self, raw, *fragments):
        with self.assertRaises(TaxonomyError) as cm:
            Taxonomy.from_dict(raw)
        for fragment in fragments:
            self.assertIn(fragment, str(cm.exception))

    def test_bundled_document_is_valid_as_a_dict(self):
        self.assertEqual(Taxonomy.from_dict(copy.deepcopy(RAW)).ids("tones"), ["angry", "upset", "neutral"])

    def test_duplicate_ids(self):
        self.assertInvalid(mutated(lambda r: r["categories"][1].update(id="health_services")),
                           "categories[1]", "duplicate id")
        self.assertInvalid(mutated(lambda r: r["ministries"][2].update(id="health")),
                           "ministries[2]", "duplicate id")
        self.assertInvalid(mutated(lambda r: r["tones"].append({"id": "angry", "label_ar": "x"})),
                           "tones[3]", "duplicate id")

    def test_duplicate_subcategory_across_categories(self):
        raw = mutated(lambda r: r["categories"][1]["subcategories"][0].update(id="medical_error"))
        self.assertInvalid(raw, "duplicate subcategory id 'medical_error'", "health_services",
                           "categories[1] (id 'education').subcategories[0]")

    def test_unknown_default_ministry(self):
        self.assertInvalid(mutated(lambda r: find(r, "categories", "housing").update(ministry="housing_ministry")),
                           "'housing'", "ministry 'housing_ministry' is not a ministry id")

    def test_bad_signal_floor(self):
        self.assertInvalid(mutated(lambda r: r["signals"][0].update(floor="urgent")),
                           "signals[0] (id 'life_safety')", "not a priority id")
        self.assertInvalid(mutated(lambda r: r["signals"][1].update(floor="critical")),
                           "signals[1]", "top priority")
        self.assertInvalid(mutated(lambda r: r["signals"][2].update(patterns=[])), "patterns")
        self.assertInvalid(mutated(lambda r: r["signals"][2].update(patterns=["مسن", " "])), "patterns[1]")

    def test_required_other_and_unknown_entries(self):
        self.assertInvalid(mutated(lambda r: r["categories"].pop()), "categories: required id 'other'")
        self.assertInvalid(mutated(lambda r: r["ministries"].pop()), "ministries: required id 'other'")
        self.assertInvalid(mutated(lambda r: r["regions"].pop()), "regions: required id 'unknown'")
        self.assertInvalid(mutated(lambda r: r["governorates"].pop()), "governorates: required id 'unknown'")
        self.assertInvalid(mutated(lambda r: r["statuses"].pop(0)), "statuses: required id 'new'")
        self.assertInvalid(mutated(lambda r: r["review_reasons"].pop("empty_text")),
                           "review_reasons", "empty_text")
        self.assertInvalid(mutated(lambda r: r["review_reasons"].pop("outside_jurisdiction")),
                           "review_reasons", "outside_jurisdiction")

    def test_receiving_entity(self):
        self.assertInvalid(mutated(lambda r: r.pop("receiving_entity")),
                           "missing top-level key(s) ['receiving_entity']")
        self.assertInvalid(mutated(lambda r: r.update(receiving_entity="إمارة منطقة الرياض")),
                           "receiving_entity: must be a mapping")
        self.assertInvalid(mutated(lambda r: r["receiving_entity"].update(region="riyad")),
                           "receiving_entity", "region 'riyad' is not a region id")
        self.assertInvalid(mutated(lambda r: r["receiving_entity"].update(region="unknown")),
                           "region 'unknown' is not a region id")
        self.assertInvalid(mutated(lambda r: r["receiving_entity"].pop("desk_ar")),
                           "receiving_entity: missing required key(s) ['desk_ar']")
        self.assertInvalid(mutated(lambda r: r["receiving_entity"].update(desk_ar=" ")), "'desk_ar'")
        self.assertInvalid(mutated(lambda r: r["receiving_entity"].update(phone="920")),
                           "receiving_entity: unknown key(s) ['phone']")
        self.assertInvalid(mutated(lambda r: r["receiving_entity"].update(id="Riyadh Emirate")),
                           "receiving_entity: 'id' must match")

    def test_governorates(self):
        self.assertInvalid(mutated(lambda r: r["governorates"][1].update(id="riyadh_city")),
                           "governorates[1]", "duplicate id")
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "diriyah")["places"].append("الدلم")),
                           "governorates[2] (id 'kharj')", "place 'الدلم' is already listed under "
                           "governorate 'diriyah'")
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "kharj")["places"].append("جدة")),
                           "place 'جدة' is a city of region 'makkah'")
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "unknown")["places"].append("الرياض")),
                           "'unknown' governorate cannot list places")
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "kharj").pop("places")),
                           "missing required key(s) ['places']")
        # the governorate's own name is a place even when the list omits it
        tax = Taxonomy.from_dict(mutated(lambda r: find(r, "governorates", "kharj").update(places=["الدلم"])))
        self.assertEqual(tax.get("governorates", "kharj").extra["places"], ("الدلم", "الخرج"))
        self.assertEqual(tax.governorate_for_place("بالخرج"), "kharj")

    def test_latin_place_names_are_validated_like_places(self):
        # one governorate each, compared by their words (article and hyphens aside)
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "diriyah")["places_en"].append("Kharj")),
                           "governorates[2] (id 'kharj')", "Latin name 'Al Kharj' is already listed "
                           "under governorate 'diriyah'")
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "kharj")["places_en"].append("Ad-Diriyah")),
                           "Latin name 'Ad-Diriyah' is already listed under governorate 'diriyah'")
        # the label_en is a name too
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "kharj")["places_en"].append("Thadiq")),
                           "governorates[16] (id 'thadiq')", "Latin name 'Thadiq' is already listed "
                           "under governorate 'kharj'")
        # never a Latin name of another region
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "kharj")["places_en"].append("Jeddah")),
                           "Latin name 'Jeddah' is a name of region 'makkah'")
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "kharj")["places_en"].append("Makkah Region")),
                           "is a name of region 'makkah'")
        # one region each, the English label included
        self.assertInvalid(mutated(lambda r: find(r, "regions", "riyadh")["places_en"].append("Jedda")),
                           "regions[1] (id 'makkah')", "Latin name 'Jedda' is already listed under region 'riyadh'")
        self.assertInvalid(mutated(lambda r: find(r, "regions", "madinah")["places_en"].append("Al Qassim Region")),
                           "regions[3] (id 'qassim')", "Latin name 'Al-Qassim Region' is already listed "
                           "under region 'madinah'")
        # Latin letters only, a word besides the article, and never on `unknown`
        for bad in ("الخرج", "Kharj2", "Al", "Al-", "  ", "Kharj/Dilam"):
            self.assertInvalid(mutated(lambda r: find(r, "governorates", "kharj").update(places_en=[bad])),
                               "governorates[2] (id 'kharj')", "places_en[0]")
        self.assertInvalid(mutated(lambda r: find(r, "regions", "hail").update(places_en="Hail")),
                           "'places_en' must be a list")
        self.assertInvalid(mutated(lambda r: find(r, "governorates", "unknown").update(places_en=["Nowhere"])),
                           "'unknown' governorate cannot list places_en")
        self.assertInvalid(mutated(lambda r: find(r, "regions", "unknown").update(places_en=["Nowhere"])),
                           "'unknown' region cannot list places_en")
        self.assertInvalid(mutated(lambda r: find(r, "ministries", "health").update(places_en=["MOH"])),
                           "unknown key(s) ['places_en']")
        # optional: without it only the label_en is matched
        def drop(r):
            find(r, "governorates", "shaqra").pop("places_en")
            find(r, "regions", "makkah").pop("places_en")
        tax = Taxonomy.from_dict(mutated(drop))
        self.assertEqual(tax.get("governorates", "shaqra").extra["places_en"], ("Shaqra",))
        self.assertEqual(tax.get("regions", "makkah").extra["places_en"], ())
        self.assertEqual(tax.governorate_for_place("Shaqra"), "shaqra")
        self.assertIsNone(tax.governorate_for_place("Shaqraa"))
        self.assertIsNone(tax.region_for_city("Jeddah"))
        self.assertEqual(tax.region_for_city("Makkah Region"), "makkah")

    def test_another_entity_is_configuration(self):
        def qassim(r):
            r["receiving_entity"] = {"id": "qassim_emirate", "label_ar": "إمارة منطقة القصيم",
                                     "label_en": "Al-Qassim Region Principality", "region": "qassim",
                                     "desk_ar": "مكتب الشكاوى"}
            r["governorates"] = [{"id": "buraidah", "label_ar": "بريدة", "label_en": "Buraidah", "places": []},
                                 {"id": "unaizah", "label_ar": "عنيزة", "label_en": "Unaizah", "places": []},
                                 {"id": "unknown", "label_ar": "غير محدد", "label_en": "Unknown", "places": []}]
        tax = Taxonomy.from_dict(mutated(qassim))
        self.assertEqual(tax.receiving_entity.extra["region"], "qassim")
        self.assertEqual(tax.governorate_for_place("حي الريان ببريدة"), "buraidah")
        self.assertIsNone(tax.governorate_for_place("الخرج"))
        self.assertEqual(tax.governorate_for_place("Al Rayyan, Buraidah"), "buraidah")   # label_en
        self.assertIsNone(tax.governorate_for_place("Al Kharj"))
        self.assertEqual(tax.region_for_city("Riyadh"), "riyadh")        # now another region
        # the Riyadh governorates' places are cities of another region now
        self.assertInvalid(mutated(lambda r: r["receiving_entity"].update(region="qassim")),
                           "is a city of region 'riyadh'", "not of the receiving entity's region 'qassim'")

    def test_priority_numbers(self):
        self.assertInvalid(mutated(lambda r: r["priorities"][2].update(sla_hours=0)),
                           "priorities[2] (id 'medium')", "positive")
        self.assertInvalid(mutated(lambda r: r["priorities"][2].update(sla_hours="168")), "must be an integer")
        self.assertInvalid(mutated(lambda r: r["priorities"][0].update(rank=True)), "must be an integer")
        self.assertInvalid(mutated(lambda r: r["priorities"][1].update(rank=4)), "distinct")
        self.assertInvalid(mutated(lambda r: r["priorities"].reverse()), "highest first")
        self.assertInvalid(mutated(lambda r: r["priorities"][3].update(rank=0)), ">= 1")

    def test_keys_ids_and_labels(self):
        self.assertInvalid(mutated(lambda r: r["categories"][0].update(minstry="health")),
                           "categories[0] (id 'health_services')", "unknown key(s) ['minstry']")
        self.assertInvalid(mutated(lambda r: r["categories"][0].pop("description_ar")),
                           "missing required key(s) ['description_ar']")
        self.assertInvalid(mutated(lambda r: r["ministries"][0].update(id="Interior Ministry")), "must match")
        self.assertInvalid(mutated(lambda r: r["ministries"][0].update(label_ar="  ")), "'label_ar'")
        self.assertInvalid(mutated(lambda r: r["ministries"][0].pop("label_en")), "label_en")
        self.assertInvalid(mutated(lambda r: r["categories"][0]["subcategories"][0].update(note="x")),
                           "exactly 'id' and 'label_ar'")
        self.assertInvalid(mutated(lambda r: r["categories"][0].update(subcategories=[])), "subcategories")
        self.assertInvalid(mutated(lambda r: r["factors"].append("health_risk")), "must be a mapping")
        # label_en is optional for factors/scopes/tones
        Taxonomy.from_dict(mutated(lambda r: r["tones"][0].pop("label_en")))

    def test_regions_and_statuses(self):
        self.assertInvalid(mutated(lambda r: find(r, "regions", "riyadh")["cities"].append("جده")),
                           "regions[1] (id 'makkah')", "city 'جدة' is already listed under region 'riyadh'")
        self.assertInvalid(mutated(lambda r: find(r, "regions", "unknown")["cities"].append("الرياض")),
                           "'unknown' region cannot list cities")
        self.assertInvalid(mutated(lambda r: r["statuses"][0].update(open="yes")), "true or false")
        self.assertInvalid(mutated(lambda r: [s.update(open=True) for s in r["statuses"]]),
                           "one open and one closed")

    def test_document_level_errors(self):
        self.assertInvalid([], "root must be a mapping")
        self.assertInvalid(mutated(lambda r: r.update(colours=[])), "unknown top-level key(s) ['colours']")
        self.assertInvalid(mutated(lambda r: r.pop("tones")), "missing top-level key(s) ['tones']")
        self.assertInvalid(mutated(lambda r: r.update(version=2)), "version")
        self.assertInvalid(mutated(lambda r: r.update(version=True)), "version")
        self.assertInvalid(mutated(lambda r: r.update(scopes=[])), "scopes: must be a non-empty list")
        self.assertInvalid(mutated(lambda r: r.update(review_reasons=["x"])), "review_reasons")

    def test_load_taxonomy_from_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "t.yaml")
            with open(good, "w", encoding="utf-8") as fh:
                yaml.safe_dump(mutated(lambda r: find(r, "tones", "neutral").update(label_ar="هادئ")),
                               fh, allow_unicode=True)
            self.assertEqual(load_taxonomy(good).label("tones", "neutral"), "هادئ")
            broken = os.path.join(tmp, "broken.yaml")
            with open(broken, "w", encoding="utf-8") as fh:
                fh.write("version: 1\npriorities: [\n")
            with self.assertRaises(TaxonomyError) as cm:
                load_taxonomy(broken)
            self.assertIn("not valid YAML", str(cm.exception))
            with self.assertRaises(TaxonomyError) as cm:
                load_taxonomy(os.path.join(tmp, "missing.yaml"))
            self.assertIn("not found", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
