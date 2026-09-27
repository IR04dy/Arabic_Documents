import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import complaints_store
from complaints_store import (LIST_MAX, SCHEMA_VERSION, Store, default_paths,
                              normalize_national_id)

NOW = "2026-09-24T12:00:00Z"      # 15:00 in Riyadh

# The schema-1 database as the first release created it (no governorate,
# model_governorate or review_reasons): the migration test starts from this.
V1_SCHEMA = (
    """CREATE TABLE complaints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source TEXT NOT NULL,
        filename TEXT NOT NULL DEFAULT '',
        file_kind TEXT,
        file_sha256 TEXT NOT NULL,
        file_size INTEGER,
        file_ext TEXT NOT NULL DEFAULT '',
        page_count INTEGER NOT NULL DEFAULT 0,
        stage TEXT NOT NULL DEFAULT 'queued',
        error TEXT,
        text TEXT,
        analysis TEXT,
        provider TEXT,
        model TEXT,
        subject TEXT,
        summary TEXT,
        complainant_name TEXT,
        national_id TEXT,
        category TEXT,
        subcategory TEXT,
        ministry TEXT,
        priority TEXT,
        region TEXT,
        model_category TEXT,
        model_ministry TEXT,
        model_priority TEXT,
        model_region TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        needs_review INTEGER NOT NULL DEFAULT 0,
        reviewed INTEGER NOT NULL DEFAULT 0,
        due_at TEXT,
        processed_at TEXT,
        timings TEXT,
        attempts INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        verdict TEXT NOT NULL,
        changes TEXT NOT NULL DEFAULT '{}',
        note TEXT NOT NULL DEFAULT '',
        reviewer TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    )""",
    *(f"CREATE INDEX complaints_{c} ON complaints({c})"
      for c in ("stage", "status", "category", "ministry", "priority", "created_at",
                "file_sha256", "national_id")),
    "CREATE INDEX feedback_complaint ON feedback(complaint_id)",
    "CREATE INDEX events_complaint ON events(complaint_id)",
    "PRAGMA user_version = 1",
)


def at(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class Clock:
    """A frozen UTC clock the tests move by hand."""

    def __init__(self, iso=NOW):
        self.now = at(iso)

    def __call__(self):
        return self.now

    def set(self, iso):
        self.now = at(iso)

    def advance(self, **delta):
        self.now += timedelta(**delta)


def analysis(category="health_services", ministry="health", priority="high", region="riyadh", *,
             governorate="riyadh_city", subcategory="medical_error", subject="تأخر موعد العملية",
             summary="ملخص الشكوى.", name="سارة الحربي", national_id="", tone="upset",
             scope="individual", signals=(), needs_review=False, review_reasons=(),
             provider="qwen", model="Qwen3-4B.gguf", timings=None):
    """The analyze() shape from the spec, with only what the store reads filled in.
    governorate=None leaves the key out, as in an analysis made before governorates."""
    fields = [{"key": "addressed_to", "label_ar": "الجهة الموجّه إليها الخطاب",
               "value": "صاحب السمو الملكي أمير منطقة الرياض", "verified": True, "source": None},
              {"key": "complainant_name", "label_ar": "اسم مقدم الشكوى", "value": name,
               "verified": bool(name), "source": None},
              {"key": "national_id", "label_ar": "رقم الهوية", "value": national_id,
               "verified": bool(national_id), "source": None}]
    structured = {"is_complaint": True, "subject": subject, "summary": summary,
                  "key_facts": [], "fields": fields, "reference_numbers": [],
                  "region": {"id": region, "source": "city_map"},
                  "addressed_to_entity": True, "input_chars": 500, "truncated": False}
    if governorate is not None:
        structured["governorate"] = {"id": governorate, "source": "place_map"}
    return {
        "structured": structured,
        "classification": {"category": category, "subcategory": subcategory,
                           "ministry": ministry, "model_priority": priority,
                           "priority": priority, "priority_source": "llm",
                           "floors_applied": [], "priority_factors": [],
                           "affected_scope": scope, "tone": tone, "confidence": "high",
                           "rationale": "", "evidence": [], "evidence_dropped": 0,
                           "signals": [{"id": s, "label_ar": s, "floor": "medium", "quotes": []}
                                       for s in signals],
                           "repeat_count": 0},
        "needs_review": needs_review, "review_reasons": list(review_reasons), "warnings": [],
        "provider": provider, "model": model,
        "timings": {"structure_s": 1.5, "classify_s": 0.5} if timings is None else timings,
    }


def paraphrased(result, **values):
    """`result` with these fields given by the model but not found verbatim in
    the text (verified: false), as analyses before `near_source` recorded them."""
    fields = result["structured"]["fields"]
    for key, value in values.items():
        field = next((f for f in fields if f["key"] == key), None)
        if field is None:
            field = {"key": key, "label_ar": key}
            fields.append(field)
        field.update(value=value, verified=False, source=None)
    return result


def by_key(detail):
    return {f["key"]: f for f in detail["analysis"]["structured"]["fields"]
            if isinstance(f, dict) and isinstance(f.get("key"), str)}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = Clock()
        self.store = self.open()

    def open(self):
        store = Store(self.root / "db" / "complaints.db", self.root / "files", clock=self.clock)
        self.addCleanup(store.close)   # runs before tmp.cleanup: Windows needs handles closed
        return store

    def raw(self, sql, params=(), *, path=None):
        """Look at the database through a second connection, as an outsider would."""
        db = sqlite3.connect(path or self.root / "db" / "complaints.db")
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def text(self, body, **kw):
        summary, duplicate = self.store.create(source="text", filename=kw.pop("filename", "نص"),
                                               file_kind=None, text=body, **kw)
        self.assertFalse(duplicate)
        return summary["id"]

    def processed(self, body, result=None, *, due_at=None, filename="نص"):
        """create → claim → save_analysis, asserting the claim took this row."""
        id = self.text(body, filename=filename)
        claimed = self.store.claim_next()
        self.assertEqual(claimed["id"], id)
        self.store.save_analysis(id, result or analysis(), due_at=due_at)
        return id


class IntakeTests(StoreTestCase):
    def test_bytes_are_written_atomically_and_deduplicated_by_sha256(self):
        data = b"%PDF-1.7 complaint bytes"
        summary, duplicate = self.store.create(source="upload", filename="شكوى أولى.pdf",
                                               file_kind="pdf", data=data, file_ext=".PDF")
        self.assertFalse(duplicate)
        id = summary["id"]
        self.assertRegex(summary["ref"], r"^CMP-2026-\d{6}$")
        self.assertEqual(summary["ref"], f"CMP-2026-{id:06d}")
        self.assertEqual((summary["stage"], summary["status"], summary["source"]),
                         ("queued", "new", "upload"))
        self.assertIs(summary["needs_review"], False)
        self.assertEqual(summary["created_at"], NOW)
        path = self.store.file_path(id)
        self.assertEqual(path, self.root / "files" / f"{id}.pdf")
        self.assertEqual(path.read_bytes(), data)
        self.assertTrue(self.store.get(id)["file_available"])

        again, duplicate = self.store.create(source="upload", filename="اسم آخر.pdf",
                                             file_kind="pdf", data=data, file_ext="pdf")
        self.assertTrue(duplicate)
        self.assertEqual(again["id"], id)
        self.assertEqual(again["filename"], "شكوى أولى.pdf")     # the original row, unchanged
        self.assertEqual(self.store.list()[1], 1)
        self.assertEqual([e["kind"] for e in self.store.get(id)["events"]], ["created"])
        # Neither the kept upload nor the discarded duplicate leaves a temp file.
        self.assertEqual(sorted(p.name for p in (self.root / "files").iterdir()), [f"{id}.pdf"])

        other, duplicate = self.store.create(source="upload", filename="b.png", file_kind="image",
                                             data=data + b"!", file_ext="png")
        self.assertFalse(duplicate)
        self.assertNotEqual(other["id"], id)
        self.assertEqual(self.store.file_path(other["id"]).suffix, ".png")

    def test_text_is_deduplicated_by_the_sha256_of_its_utf8(self):
        body = "أتقدم بشكوى بخصوص انقطاع المياه عن حي النسيم."
        id = self.text(body)
        detail = self.store.get(id)
        self.assertEqual(detail["text"], body)
        self.assertIsNone(self.store.file_path(id))
        self.assertFalse(detail["file_available"])
        summary, duplicate = self.store.create(source="text", filename="x", file_kind=None, text=body)
        self.assertTrue(duplicate)
        self.assertEqual(summary["id"], id)
        # A .txt upload with exactly those bytes is the same complaint.
        summary, duplicate = self.store.create(source="upload", filename="a.txt", file_kind="txt",
                                               data=body.encode("utf-8"), text=body, file_ext="txt")
        self.assertTrue(duplicate)
        self.assertEqual(summary["id"], id)
        _, duplicate = self.store.create(source="text", filename="x", file_kind=None, text=body + " ")
        self.assertFalse(duplicate)

    def test_create_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            self.store.create(source="upload", filename="a", file_kind="pdf")
        with self.assertRaises(ValueError):
            self.store.create(source="email", filename="a", file_kind=None, text="x")
        with self.assertRaises(ValueError):
            self.store.create(source="upload", filename="a", file_kind="docx", data=b"x")
        self.assertEqual(self.store.list()[1], 0)

    def test_unusable_extension_falls_back_to_the_kind(self):
        summary, _ = self.store.create(source="upload", filename="x", file_kind="pdf",
                                       data=b"%PDF", file_ext="../../evil")
        self.assertEqual(self.store.file_path(summary["id"]).name, f"{summary['id']}.pdf")

    def test_ref_year_follows_riyadh_time(self):
        self.clock.set("2026-12-31T21:30:00Z")            # 00:30 on 1 Jan in Riyadh
        id = self.text("شكوى في أول السنة")
        self.assertEqual(self.store.get(id)["ref"], f"CMP-2027-{id:06d}")
        self.assertEqual(self.store.get(id)["created_at"], "2026-12-31T21:30:00Z")


class WorkerTests(StoreTestCase):
    def test_claim_order_stage_by_text_presence_and_attempts(self):
        pdf, _ = self.store.create(source="upload", filename="a.pdf", file_kind="pdf", data=b"%PDF a")
        pasted = self.text("نص ملصق")
        txt, _ = self.store.create(source="upload", filename="c.txt", file_kind="txt",
                                   data="ملف نصي".encode(), text="ملف نصي", file_ext="txt")

        first = self.store.claim_next()
        self.assertEqual((first["id"], first["stage"], first["attempts"]), (pdf["id"], "ocr", 1))
        self.assertIsNone(first["text"])
        self.assertEqual(Path(first["file_path"]).read_bytes(), b"%PDF a")
        second = self.store.claim_next()
        self.assertEqual((second["id"], second["stage"]), (pasted, "structuring"))
        self.assertEqual(second["text"], "نص ملصق")
        self.assertIsNone(second["file_path"])
        third = self.store.claim_next()
        self.assertEqual((third["id"], third["stage"]), (txt["id"], "structuring"))
        self.assertIsNone(self.store.claim_next())

        self.store.fail(pdf["id"], "تعذرت معالجة الشكوى")
        self.store.requeue(pdf["id"])
        again = self.store.claim_next()
        self.assertEqual((again["id"], again["attempts"], again["error"]), (pdf["id"], 2, None))

    def test_set_stage_logs_only_real_changes(self):
        id = self.text("نص")
        self.store.claim_next()                           # -> structuring
        self.store.set_stage(id, "structuring")           # the pipeline's own on_stage call
        self.store.set_stage(id, "classifying")
        stages = [e["detail"]["stage"] for e in self.store.get(id)["events"] if e["kind"] == "stage"]
        self.assertEqual(stages, ["classifying", "structuring"])     # newest first
        with self.assertRaises(ValueError):
            self.store.set_stage(id, "sleeping")
        self.store.set_stage(999, "done")                 # unknown id: silently nothing

    def test_ocr_time_is_measured_and_merged_with_pipeline_timings(self):
        summary, _ = self.store.create(source="upload", filename="a.pdf", file_kind="pdf", data=b"%PDF")
        id = summary["id"]
        self.store.claim_next()
        self.clock.advance(seconds=12.5)
        self.store.save_text(id, "--- Page 1 ---\nنص مستخرج", 3)
        detail = self.store.get(id)
        self.assertEqual((detail["text"], detail["page_count"]), ("--- Page 1 ---\nنص مستخرج", 3))
        self.assertEqual(detail["timings"], {"ocr_s": 12.5})
        self.store.save_analysis(id, analysis(), due_at="2026-09-27T12:00:00Z")
        self.assertEqual(self.store.get(id)["timings"],
                         {"ocr_s": 12.5, "structure_s": 1.5, "classify_s": 0.5})
        # A reprocess without OCR starts clean: the old OCR time is not this run's.
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis(), due_at="2026-09-27T12:00:00Z")
        self.assertEqual(self.store.get(id)["timings"], {"structure_s": 1.5, "classify_s": 0.5})

    def test_save_analysis_fills_effective_and_model_fields(self):
        result = analysis("water_sewage", "mewa", "critical", "eastern", governorate="unknown",
                          subcategory="sewage_overflow", subject="طفح الصرف", summary="ملخص.",
                          name="خالد العتيبي", national_id=" ١٠٩٨ ٧٦٥ ٤٣٢‏", needs_review=True,
                          review_reasons=["outside_jurisdiction", "repeat_complainant"])
        id = self.processed("نص الشكوى", result, due_at="2026-09-25T12:00:00Z")
        d = self.store.get(id)
        self.assertEqual((d["stage"], d["error"], d["processed_at"]), ("done", None, NOW))
        self.assertEqual((d["category"], d["subcategory"], d["ministry"], d["priority"], d["region"],
                          d["governorate"]),
                         ("water_sewage", "sewage_overflow", "mewa", "critical", "eastern", "unknown"))
        self.assertEqual((d["model_category"], d["model_ministry"], d["model_priority"], d["model_region"],
                          d["model_governorate"]),
                         ("water_sewage", "mewa", "critical", "eastern", "unknown"))
        self.assertEqual(d["review_reasons"], ["outside_jurisdiction", "repeat_complainant"])
        self.assertEqual(self.store.list()[0][0]["review_reasons"],       # the register's badge
                         ["outside_jurisdiction", "repeat_complainant"])
        self.assertEqual((d["subject"], d["summary"], d["complainant_name"]),
                         ("طفح الصرف", "ملخص.", "خالد العتيبي"))
        self.assertEqual(d["national_id"], "1098765432")
        self.assertEqual((d["provider"], d["model"]), ("qwen", "Qwen3-4B.gguf"))
        self.assertEqual(d["due_at"], "2026-09-25T12:00:00Z")
        self.assertIs(d["needs_review"], True)
        self.assertIs(d["reviewed"], False)
        self.assertEqual(d["analysis"], result)             # persisted verbatim
        self.assertEqual(d["events"][0]["kind"], "processed")
        self.assertEqual(d["events"][0]["detail"]["priority"], "critical")

    def test_reanalysis_after_review_keeps_the_reviewer_values(self):
        id = self.processed("نص", analysis("municipal_services", "municipal", "medium", "riyadh",
                                           governorate="riyadh_city", subcategory="roads_potholes"),
                            due_at="2026-10-01T12:00:00Z")
        self.store.add_feedback(id, verdict="correct", changes={"category": "housing",
                                "subcategory": "", "priority": "high", "governorate": "kharj"},
                                due_at="2026-09-27T12:00:00Z")
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis("electricity", "energy", "low", "qassim",
                                              governorate="unknown", subcategory="power_outage",
                                              needs_review=True, review_reasons=["outside_jurisdiction"]),
                                 due_at="2026-10-08T12:00:00Z")
        d = self.store.get(id)
        self.assertEqual((d["category"], d["subcategory"], d["ministry"], d["priority"], d["region"],
                          d["governorate"]),
                         ("housing", None, "municipal", "high", "riyadh", "kharj"))
        self.assertEqual((d["model_category"], d["model_ministry"], d["model_priority"], d["model_region"],
                          d["model_governorate"]),
                         ("electricity", "energy", "low", "qassim", "unknown"))
        self.assertIs(d["needs_review"], False)
        self.assertIs(d["reviewed"], True)
        self.assertEqual(d["due_at"], "2026-09-27T12:00:00Z")   # follows the kept priority
        # The reasons are the new analysis's own, even though no review is needed.
        self.assertEqual(d["review_reasons"], ["outside_jurisdiction"])

    def test_reviewed_complaint_is_reviewed_again_for_a_text_reason_it_has_not_seen(self):
        id = self.processed("نص", analysis(review_reasons=["input_truncated"]), due_at="2026-10-01T12:00:00Z")
        self.store.add_feedback(id, verdict="confirm", changes={})
        # The same reason the reviewer already saw: no new review.
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis(review_reasons=["input_truncated"]), due_at=None)
        self.assertIs(self.store.get(id)["needs_review"], False)
        # A re-read that now looks like no complaint at all: back to a reviewer,
        # with the reviewer's classification kept.
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis("other", "other", "low", review_reasons=["not_a_complaint"]),
                                 due_at=None)
        d = self.store.get(id)
        self.assertEqual((d["needs_review"], d["reviewed"], d["category"], d["priority"]),
                         (True, True, "health_services", "high"))
        # Any other new reason on a reviewed complaint does not reopen it.
        self.store.add_feedback(id, verdict="confirm", changes={})
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis(review_reasons=["not_a_complaint", "low_confidence"]),
                                 due_at=None)
        self.assertIs(self.store.get(id)["needs_review"], False)

    def test_an_empty_text_analysis_never_blanks_what_the_complaint_has(self):
        id = self.processed("نص", analysis(subject="طفح الصرف", summary="ملخص.", name="خالد",
                                           national_id="1098765432"))
        self.store.add_feedback(id, verdict="confirm", changes={})
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis("other", "other", "low", subject="", summary="", name="",
                                              national_id="", review_reasons=["empty_text"]), due_at=None)
        d = self.store.get(id)
        self.assertEqual((d["subject"], d["summary"], d["complainant_name"], d["national_id"]),
                         ("طفح الصرف", "ملخص.", "خالد", "1098765432"))
        self.assertEqual((d["needs_review"], d["review_reasons"]), (True, ["empty_text"]))
        # Without empty_text an analysis's blank values are its answer.
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis(subject="", name=""), due_at=None)
        d = self.store.get(id)
        self.assertEqual((d["subject"], d["complainant_name"], d["national_id"]), ("", "", None))

    def test_an_interrupted_reextraction_stays_pending_and_a_plain_reprocess_drops_it(self):
        pdf, _ = self.store.create(source="upload", filename="a.pdf", file_kind="pdf", data=b"%PDF")
        id = pdf["id"]
        self.store.claim_next()
        self.store.save_text(id, "نص قديم", 1)
        self.store.save_analysis(id, analysis(), due_at=None)
        self.store.requeue(id, ocr=True)
        self.store.claim_next()                             # the worker starts the re-read…
        self.store.requeue(id, interrupted=True)            # …and the app stops under it
        self.assertEqual(self.store.get(id)["events"][0]["detail"], {"ocr": True, "interrupted": True})
        self.assertEqual(self.store.claim_next()["stage"], "ocr")
        self.store.fail(id, "تعذر استخراج النص من الملف")
        self.store.requeue(id)                              # the user: analyse the text it has
        claimed = self.store.claim_next()
        self.assertEqual((claimed["stage"], claimed["text"]), ("structuring", "نص قديم"))
        # A restart also keeps a pending re-extraction pending.
        self.store.requeue(id, ocr=True)
        self.store.claim_next()
        self.assertEqual(self.store.reset_interrupted(), 1)
        self.assertEqual(self.store.get(id)["events"][0]["detail"], {"ocr": True, "interrupted": True})
        self.assertEqual(self.store.claim_next()["stage"], "ocr")

    def test_fail_if_stuck_touches_only_an_unheld_processing_row(self):
        id = self.text("نص")
        self.store.claim_next()
        self.assertFalse(self.store.fail(id, "x", if_stuck=True))      # our worker holds it
        self.assertTrue(self.store.is_claimed(id))
        self.store.release_claim(id)
        self.assertFalse(self.store.is_claimed(id))
        self.assertTrue(self.store.fail(id, "تعذرت معالجة الشكوى", if_stuck=True))
        self.assertEqual((self.store.get(id)["stage"], self.store.get(id)["error"]),
                         ("error", "تعذرت معالجة الشكوى"))
        self.store.requeue(id)                                          # the user requeued it
        self.assertFalse(self.store.fail(id, "x", if_stuck=True))
        self.assertEqual(self.store.get(id)["stage"], "queued")
        self.assertFalse(self.store.fail(999, "x"))

    def test_reset_interrupted_gives_up_on_a_complaint_that_never_finishes(self):
        id, other = self.text("ملف لا ينتهي"), self.text("شكوى أخرى")
        for _ in range(2):
            self.assertEqual(self.store.claim_next()["id"], id)
            self.assertEqual(self.store.reset_interrupted(max_interruptions=3, message="توقفت"), 1)
        self.assertEqual(self.store.claim_next()["id"], id)
        self.assertEqual(self.store.reset_interrupted(max_interruptions=3, message="توقفت"), 0)
        d = self.store.get(id)
        self.assertEqual((d["stage"], d["error"]), ("error", "توقفت"))
        self.assertEqual(d["events"][0], {"at": NOW, "kind": "error",
                                          "detail": {"message": "توقفت", "interrupted": True}})
        # A person's reprocess starts the count again.
        self.store.requeue(id)
        self.assertEqual(self.store.claim_next()["id"], id)
        self.assertEqual(self.store.reset_interrupted(max_interruptions=3, message="توقفت"), 1)
        self.assertEqual(self.store.claim_next()["id"], id)
        self.store.save_analysis(id, analysis(), due_at=None)
        self.assertEqual(self.store.claim_next()["id"], other)          # untouched by all of it

    def test_analysis_without_governorate_or_reasons_is_tolerated(self):
        result = analysis(governorate=None)
        del result["review_reasons"]
        id = self.processed("نص قديم", result)
        d = self.store.get(id)
        self.assertEqual((d["region"], d["governorate"], d["model_governorate"], d["review_reasons"]),
                         ("riyadh", None, None, []))
        result = analysis(governorate=None, review_reasons=["low_confidence", 7, None])
        result["structured"]["governorate"] = "diriyah"               # a bare id works as for region
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, result, due_at=None)
        d = self.store.get(id)
        self.assertEqual((d["governorate"], d["model_governorate"], d["review_reasons"]),
                         ("diriyah", "diriyah", ["low_confidence"]))
        # A damaged column reads as no reasons instead of breaking the register.
        self.store._db.execute("UPDATE complaints SET review_reasons = '{oops' WHERE id = ?", (id,))
        self.assertEqual(self.store.list()[0][0]["review_reasons"], [])

    def test_fail_and_requeue(self):
        pdf, _ = self.store.create(source="upload", filename="a.pdf", file_kind="pdf", data=b"%PDF")
        id = pdf["id"]
        self.store.claim_next()
        self.store.save_text(id, "نص مستخرج", 2)
        self.store.fail(id, "النموذج غير متاح")
        d = self.store.get(id)
        self.assertEqual((d["stage"], d["error"]), ("error", "النموذج غير متاح"))
        self.assertEqual(d["events"][0], {"at": NOW, "kind": "error",
                                          "detail": {"message": "النموذج غير متاح"}})

        summary = self.store.requeue(id)                  # keep the text: straight to the LLM
        self.assertEqual((summary["stage"], summary["error"]), ("queued", None))
        self.assertEqual(self.store.get(id)["text"], "نص مستخرج")
        self.assertEqual(self.store.claim_next()["stage"], "structuring")

        self.store.requeue(id, ocr=True)                  # the file exists: extract again
        d = self.store.get(id)
        # The text stays until the new OCR result replaces it.
        self.assertEqual((d["text"], d["page_count"]), ("نص مستخرج", 2))
        self.assertEqual(d["events"][0]["detail"], {"ocr": True})
        claimed = self.store.claim_next()
        self.assertEqual((claimed["stage"], claimed["needs_ocr"], claimed["text"]), ("ocr", True, "نص مستخرج"))
        self.store.save_text(id, "نص جديد", 1)
        self.store.requeue(id)
        claimed = self.store.claim_next()
        self.assertEqual((claimed["stage"], claimed["needs_ocr"], claimed["text"]),
                         ("structuring", False, "نص جديد"))

        pasted = self.text("نص ملصق بلا ملف")
        self.store.requeue(pasted, ocr=True)              # no file: the text is all there is
        self.assertEqual(self.store.get(pasted)["text"], "نص ملصق بلا ملف")
        self.assertEqual(self.store.get(pasted)["events"][0]["detail"], {"ocr": False})
        txt, _ = self.store.create(source="upload", filename="c.txt", file_kind="txt",
                                   data=b"plain", text="plain", file_ext="txt")
        self.store.requeue(txt["id"], ocr=True)           # a .txt's text IS its file
        self.assertEqual(self.store.get(txt["id"])["text"], "plain")
        self.assertIsNone(self.store.requeue(999))

    def test_reset_interrupted_requeues_only_in_flight_rows(self):
        ids = [self.text(f"شكوى {i}") for i in range(4)]
        for _ in ids:
            self.store.claim_next()
        self.store.set_stage(ids[1], "classifying")
        self.store.save_analysis(ids[2], analysis(), due_at=None)
        self.store.fail(ids[3], "خطأ")
        self.assertEqual(self.store.reset_interrupted(), 2)
        stages = {id: self.store.get(id)["stage"] for id in ids}
        self.assertEqual(stages, {ids[0]: "queued", ids[1]: "queued", ids[2]: "done", ids[3]: "error"})
        self.assertEqual(self.store.reset_interrupted(), 0)
        self.assertEqual(self.store.claim_next()["id"], ids[0])

    def test_queue_state(self):
        self.assertEqual(self.store.queue_state(), {"queued": 0, "processing": None, "errors": 0})
        a = self.text("أ")
        self.text("ب")
        self.text("ج")
        self.store.claim_next()
        self.store.fail(a, "خطأ")
        b = self.store.claim_next()
        state = self.store.queue_state()
        self.assertEqual(state["queued"], 1)
        self.assertEqual(state["errors"], 1)
        self.assertEqual(state["processing"], {"id": b["id"], "ref": b["ref"],
                                               "stage": "structuring", "filename": "نص"})


class ReviewTests(StoreTestCase):
    def test_feedback_records_only_real_changes(self):
        id = self.processed("نص", analysis(), due_at="2026-09-27T12:00:00Z")
        self.clock.advance(hours=1)
        d = self.store.add_feedback(id, verdict="correct",
                                    changes={"category": "health_services", "ministry": "interior",
                                             "subcategory": "", "priority": "high",
                                             "governorate": "dawadmi", "region": "riyadh"},
                                    note="الجهة خاطئة", reviewer="مراجع ١",
                                    due_at="2026-09-26T13:00:00Z")
        self.assertEqual((d["ministry"], d["governorate"]), ("interior", "dawadmi"))
        self.assertIsNone(d["subcategory"])
        self.assertEqual(d["model_ministry"], "health")    # the model's answer is kept for analytics
        self.assertEqual(d["model_governorate"], "riyadh_city")
        self.assertIs(d["reviewed"], True)
        self.assertIs(d["needs_review"], False)
        self.assertEqual(d["due_at"], "2026-09-26T13:00:00Z")
        self.assertEqual(d["updated_at"], "2026-09-24T13:00:00Z")
        fb = d["feedback"][0]
        self.assertEqual(fb["changes"], {"subcategory": {"from": "medical_error", "to": None},
                                         "ministry": {"from": "health", "to": "interior"},
                                         "governorate": {"from": "riyadh_city", "to": "dawadmi"}})
        self.assertEqual((fb["verdict"], fb["note"], fb["reviewer"], fb["created_at"]),
                         ("correct", "الجهة خاطئة", "مراجع ١", "2026-09-24T13:00:00Z"))
        self.assertEqual(d["events"][0]["kind"], "feedback")
        self.assertEqual(d["events"][0]["detail"], {"verdict": "correct",
                                                    "fields": ["subcategory", "ministry", "governorate"]})
        self.assertIsNone(self.store.add_feedback(999, verdict="confirm", changes={}))

    def test_confirm_without_changes(self):
        id = self.processed("نص", analysis(needs_review=True), due_at="2026-09-27T12:00:00Z")
        d = self.store.add_feedback(id, verdict="confirm", changes={})
        self.assertEqual(d["feedback"][0]["changes"], {})
        self.assertIs(d["reviewed"], True)
        self.assertIs(d["needs_review"], False)
        self.assertEqual(d["due_at"], "2026-09-27T12:00:00Z")          # untouched without due_at
        self.assertEqual((d["category"], d["priority"]), ("health_services", "high"))

    def test_feedback_rejects_unknown_verdict_or_field(self):
        id = self.processed("نص")
        with self.assertRaises(ValueError):
            self.store.add_feedback(id, verdict="maybe", changes={})
        with self.assertRaises(ValueError):
            self.store.add_feedback(id, verdict="correct", changes={"stage": "done"})
        self.assertEqual(self.store.get(id)["feedback"], [])

    def test_set_status(self):
        id = self.processed("نص")
        d = self.store.set_status(id, "referred", "أحيلت لأمانة الرياض")
        self.assertEqual(d["status"], "referred")
        self.assertEqual(d["events"][0], {"at": NOW, "kind": "status", "detail": {
            "from": "new", "to": "referred", "note": "أحيلت لأمانة الرياض"}})
        self.assertIsNone(self.store.set_status(999, "resolved"))

    def test_delete_cascades_and_removes_the_file(self):
        summary, _ = self.store.create(source="upload", filename="a.pdf", file_kind="pdf", data=b"%PDF")
        id = summary["id"]
        keep = self.text("شكوى أخرى")
        path = self.store.file_path(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis(), due_at=None)
        self.store.add_feedback(id, verdict="confirm", changes={})
        self.assertTrue(self.store.delete(id))
        self.assertIsNone(self.store.get(id))
        self.assertFalse(path.exists())
        self.assertEqual(self.raw("SELECT COUNT(*) FROM feedback WHERE complaint_id = ?", (id,)), [(0,)])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM events WHERE complaint_id = ?", (id,)), [(0,)])
        self.assertIsNotNone(self.store.get(keep))
        self.assertFalse(self.store.delete(id))
        # AUTOINCREMENT: even the highest deleted id is never handed out again.
        self.assertTrue(self.store.delete(keep))
        self.assertGreater(self.text("شكوى جديدة"), keep)

    def test_a_file_that_cannot_be_deleted_yet_is_swept_later(self):
        summary, _ = self.store.create(source="upload", filename="a.pdf", file_kind="pdf", data=b"%PDF a")
        keep, _ = self.store.create(source="upload", filename="b.pdf", file_kind="pdf", data=b"%PDF b")
        path = self.store.file_path(summary["id"])
        # On Windows a download still streaming the file makes unlink fail.
        with mock.patch.object(Path, "unlink", side_effect=PermissionError(13, "in use")), quiet():
            self.assertTrue(self.store.delete(summary["id"]))
        self.assertIsNone(self.store.get(summary["id"]))
        self.assertTrue(path.exists())                     # the personal data is still there…
        self.assertEqual(self.store.sweep_orphans(), 1)    # …until the next sweep
        self.assertFalse(path.exists())
        self.assertTrue(self.store.file_path(keep["id"]).exists())
        self.assertEqual(self.store.sweep_orphans(), 0)    # nothing pending: no directory scan
        # One left behind by an earlier run goes when the queue is taken over;
        # a file above the highest id ever committed may be another app's new upload.
        stray, newer = self.root / "files" / f"{summary['id']}.png", self.root / "files" / "99.pdf"
        stray.write_bytes(b"x")
        newer.write_bytes(b"y")
        self.assertTrue(self.store.acquire_owner())
        self.assertFalse(stray.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(self.store.file_path(keep["id"]).exists())

    def test_deleted_text_does_not_linger_in_the_database_files(self):
        markers = [f"MARK{i}-{'x' * 40}-1098765432" for i in range(3)]
        ids = [self.processed(f"نص الشكوى {m} " * 20, analysis(subject=m)) for m in markers]
        self.store.add_feedback(ids[1], verdict="correct", changes={"priority": "low"}, note=markers[1])
        self.assertTrue(self.store.delete(ids[1]))
        self.store.close()
        blob = b"".join(p.read_bytes() for p in (self.root / "db").iterdir() if p.is_file())
        # assertTrue/False: a failure must not print the whole database.
        self.assertFalse(markers[1].encode() in blob, "the deleted complaint's text is still on disk")
        self.assertTrue(markers[0].encode() in blob, "the other complaints are gone too")

    def test_detail_decodes_json_and_lists_history_newest_first(self):
        id = self.processed("نص", due_at="2026-09-27T12:00:00Z")
        self.clock.advance(minutes=5)
        self.store.add_feedback(id, verdict="confirm", changes={})
        self.clock.advance(minutes=5)
        self.store.add_feedback(id, verdict="correct", changes={"region": "makkah"})
        d = self.store.get(id)
        self.assertIsInstance(d["analysis"], dict)
        self.assertIsInstance(d["timings"], dict)
        self.assertEqual([f["verdict"] for f in d["feedback"]], ["correct", "confirm"])
        self.assertEqual([e["kind"] for e in d["events"]],
                         ["feedback", "feedback", "processed", "stage", "created"])
        self.assertEqual(d["model_region"], "riyadh")
        self.assertEqual(d["attempts"], 1)
        # A damaged JSON column reads as empty instead of breaking the page.
        self.store._db.execute("UPDATE complaints SET analysis = '{broken', timings = '[1]' WHERE id = ?", (id,))
        d = self.store.get(id)
        self.assertIsNone(d["analysis"])
        self.assertEqual(d["timings"], {})
        self.assertEqual(self.store.analytics()["totals"]["done"], 1)


class FieldReviewTests(StoreTestCase):
    def row(self, id):
        return {r["id"]: r for r in self.store.list()[0]}[id]

    def test_unverified_values_are_pending_until_accepted_or_changed(self):
        result = paraphrased(analysis(review_reasons=["fields_unverified"]),
                             against_entity="شركة المياه الوطنية", complainant_name="سارة الحربى",
                             incident_date="")                       # nothing given: nothing to review
        result["structured"]["fields"] += [{"key": "phone", "value": "0500000000"},   # no verified flag
                                           "damaged entry", {"key": ["x"], "value": "y", "verified": False}]
        id = self.processed("نص", result)
        other = self.processed("نص آخر")
        d = self.store.get(id)
        fields = by_key(d)
        self.assertEqual((d["pending_fields"], d["needs_review"], d["review_reasons"]),
                         (2, True, ["fields_unverified"]))
        self.assertEqual(self.row(id)["pending_fields"], 2)
        self.assertEqual(self.row(other)["pending_fields"], 0)
        self.assertIs(fields["against_entity"]["pending"], True)
        self.assertIs(fields["complainant_name"]["pending"], True)
        for key in ("addressed_to", "national_id", "incident_date", "phone"):
            self.assertNotIn("pending", fields[key], key)
        self.assertIn("damaged entry", d["analysis"]["structured"]["fields"])
        self.assertEqual(d["complainant_name"], "سارة الحربى")          # the model's until reviewed
        self.assertEqual(self.store.analytics()["totals"]["fields_pending"], 1)

        # Accept: the current value is recorded, whatever `value` says.
        self.clock.advance(minutes=5)
        d = self.store.review_field(id, "against_entity", action="accept", value="غيرها",
                                    reviewer="مراجع ١", note="كما في الخطاب")
        field = by_key(d)["against_entity"]
        self.assertEqual(field["value"], "شركة المياه الوطنية")
        self.assertEqual(field["review"], {"action": "accept", "reviewer": "مراجع ١",
                                           "at": "2026-09-24T12:05:00Z", "from": "شركة المياه الوطنية",
                                           "note": "كما في الخطاب"})
        self.assertNotIn("pending", field)
        self.assertEqual((d["pending_fields"], d["needs_review"], d["review_reasons"]),
                         (1, True, ["fields_unverified"]))
        self.assertEqual(d["updated_at"], "2026-09-24T12:05:00Z")
        self.assertEqual(d["events"][0], {"at": "2026-09-24T12:05:00Z", "kind": "field_review", "detail": {
            "key": "against_entity", "action": "accept",
            "from": "شركة المياه الوطنية", "to": "شركة المياه الوطنية"}})

        # Change: the reviewer's value replaces the model's, here and in the register.
        d = self.store.review_field(id, "complainant_name", action="change", value="سارة الحربي")
        field = by_key(d)["complainant_name"]
        self.assertEqual((field["value"], field["review"]["action"], field["review"]["from"],
                          field["review"]["reviewer"]), ("سارة الحربي", "change", "سارة الحربى", ""))
        self.assertEqual(d["complainant_name"], "سارة الحربي")
        self.assertEqual(self.row(id)["complainant_name"], "سارة الحربي")
        # Nothing left: no review needed, and the answered reason is no longer shown.
        self.assertEqual((d["pending_fields"], d["needs_review"], d["review_reasons"]), (0, False, []))
        self.assertEqual(d["analysis"]["review_reasons"], [])
        self.assertEqual((self.row(id)["review_reasons"], self.row(id)["needs_review"]), ([], False))
        self.assertEqual(self.store.analytics()["totals"]["fields_pending"], 0)
        self.assertEqual(d["events"][0]["detail"], {"key": "complainant_name", "action": "change",
                                                    "from": "سارة الحربى", "to": "سارة الحربي"})
        # The analysis itself is kept as the model gave it; the reviews sit beside it.
        self.assertEqual(self.raw("SELECT json_extract(analysis, '$.review_reasons'), "
                                  "json_extract(analysis, '$.structured.fields[1].value') "
                                  "FROM complaints WHERE id = ?", (id,)),
                         [('["fields_unverified"]', "سارة الحربى")])
        stored = json.loads(self.raw("SELECT field_reviews FROM complaints WHERE id = ?", (id,))[0][0])
        self.assertEqual(set(stored), {"against_entity", "complainant_name"})
        self.assertEqual(stored["complainant_name"], {
            "action": "change", "value": "سارة الحربي", "from": "سارة الحربى", "reviewer": "",
            "note": "", "at": "2026-09-24T12:05:00Z"})

    def test_a_changed_name_or_id_moves_the_register_search_and_repeat_detection(self):
        first = self.processed("أ", analysis(national_id="1098765432", name="خالد العتيبي"))
        second = self.processed("ب", paraphrased(analysis(name="خالد"), national_id="١٠٩٨ ٧٦٥ ٤٣٣"))
        self.assertEqual(self.store.get(second)["national_id"], "1098765433")
        self.assertEqual(self.store.repeat_count("1098765432"), 1)
        d = self.store.review_field(second, "national_id", action="change", value=" ١٠٩٨ ٧٦٥ ٤٣٢")
        self.assertEqual(d["national_id"], "1098765432")              # normalised as the pipeline's
        self.assertEqual(by_key(d)["national_id"]["value"], " ١٠٩٨ ٧٦٥ ٤٣٢")   # as the reviewer typed it
        self.assertEqual(self.store.repeat_count("1098765432"), 2)
        self.assertEqual(self.store.repeat_count("1098765432", exclude_id=first), 1)
        self.assertEqual(sorted(r["id"] for r in self.store.list(q="1098 765 432")[0]), [first, second])
        # A verified field may be corrected too.
        self.store.review_field(second, "complainant_name", action="change", value="خالد العتيبي")
        self.assertEqual(sorted(r["id"] for r in self.store.list(q="العتيبي")[0]), [first, second])
        # "" clears it.
        d = self.store.review_field(second, "national_id", action="change", value="")
        self.assertEqual((d["national_id"], by_key(d)["national_id"]["value"]), (None, ""))
        self.assertEqual(self.store.repeat_count("1098765432"), 1)

    def test_review_field_rejects_what_it_cannot_apply(self):
        id = self.processed("نص", paraphrased(analysis(), against_entity="شركة المياه"))
        queued = self.text("لم تعالج بعد")
        for key, kw in (("no_such_field", {"action": "accept"}),
                        ("subject", {"action": "change", "value": "x"}),      # not a structured field
                        ("against_entity", {"action": "approve"}),
                        ("against_entity", {"action": "change"}),               # a change needs a value
                        ("against_entity", {"action": "change", "value": 7}),
                        ("against_entity", {"action": "change", "value": "x" * 501})):
            with self.subTest(key=key, kw=kw), self.assertRaises(ValueError):
                self.store.review_field(id, key, **kw)
        with self.assertRaises(ValueError):                     # no analysis yet: no fields
            self.store.review_field(queued, "complainant_name", action="accept")
        self.assertIsNone(self.store.review_field(999, "against_entity", action="accept"))
        d = self.store.get(id)
        self.assertEqual((d["pending_fields"], d["needs_review"]), (1, True))
        self.assertNotIn("field_review", [e["kind"] for e in d["events"]])
        d = self.store.review_field(id, "against_entity", action="change", value="x" * 500)
        self.assertEqual(by_key(d)["against_entity"]["value"], "x" * 500)

    def test_needs_review_waits_for_both_the_fields_and_the_verdict(self):
        # Another reason: accepting the fields leaves the verdict to give.
        a = self.processed("أ", paraphrased(analysis(review_reasons=["low_confidence", "fields_unverified"]),
                                           against_entity="شركة المياه"))
        self.assertIs(self.store.review_field(a, "against_entity", action="accept")["needs_review"], True)
        self.assertIs(self.store.add_feedback(a, verdict="confirm", changes={})["needs_review"], False)
        # The verdict first: the pending field still keeps it in review.
        b = self.processed("ب", paraphrased(analysis(review_reasons=["fields_unverified"]),
                                           against_entity="شركة المياه"))
        d = self.store.add_feedback(b, verdict="confirm", changes={})
        self.assertEqual((d["reviewed"], d["needs_review"], d["pending_fields"]), (True, True, 1))
        self.assertEqual(self.store.list(needs_review=True)[0][0]["id"], b)
        self.assertIs(self.store.review_field(b, "against_entity", action="accept")["needs_review"], False)
        # A reviewed complaint re-analysed with new pending fields: back in review
        # until they are answered, not until another verdict.
        self.store.requeue(b)
        self.store.claim_next()
        self.store.save_analysis(b, paraphrased(analysis(review_reasons=["fields_unverified"]),
                                                against_entity="شركة المياه", requested_action="الإصلاح"),
                                 due_at=None)
        self.assertEqual((self.store.get(b)["needs_review"], self.store.get(b)["pending_fields"]), (True, 1))
        self.assertIs(self.store.review_field(b, "requested_action", action="accept")["needs_review"], False)

    def test_a_field_review_does_not_answer_a_text_reason_the_reviewer_has_not_seen(self):
        id = self.processed("نص")
        self.store.add_feedback(id, verdict="confirm", changes={})
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, analysis(review_reasons=["not_a_complaint"]), due_at=None)
        self.assertIs(self.store.get(id)["needs_review"], True)
        d = self.store.review_field(id, "addressed_to", action="change", value="أمير منطقة الرياض")
        self.assertIs(d["needs_review"], True)                  # still waiting for the verdict
        self.assertIs(self.store.add_feedback(id, verdict="confirm", changes={})["needs_review"], False)
        # With pending fields too, the text reason keeps it after they are answered.
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, paraphrased(analysis(review_reasons=["input_truncated"]),
                                                 against_entity="شركة المياه"), due_at=None)
        self.assertIs(self.store.review_field(id, "against_entity", action="accept")["needs_review"], True)
        self.assertIs(self.store.add_feedback(id, verdict="confirm", changes={})["needs_review"], False)

    def test_reprocess_keeps_changes_and_drops_stale_accepts(self):
        def result(action="إصلاح الشبكة"):
            return paraphrased(analysis(review_reasons=["fields_unverified"]), against_entity="شركة المياه",
                               requested_action=action, complainant_name="سارة")
        id = self.processed("نص", result())
        self.store.review_field(id, "against_entity", action="accept")
        self.store.review_field(id, "requested_action", action="accept")
        self.store.review_field(id, "complainant_name", action="change", value="سارة الحربي")
        self.assertEqual(self.store.get(id)["needs_review"], False)

        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, result("إصلاح الشبكة فوراً"), due_at=None)
        d = self.store.get(id)
        fields = by_key(d)
        self.assertEqual(fields["against_entity"]["review"]["action"], "accept")     # the same value
        self.assertNotIn("review", fields["requested_action"])                         # another one: stale
        self.assertEqual((fields["requested_action"]["value"], fields["requested_action"]["pending"]),
                         ("إصلاح الشبكة فوراً", True))
        self.assertEqual((fields["complainant_name"]["value"], fields["complainant_name"]["review"]["action"]),
                         ("سارة الحربي", "change"))
        self.assertEqual(d["complainant_name"], "سارة الحربي")              # a change always applies
        self.assertEqual((d["pending_fields"], d["needs_review"], d["review_reasons"]),
                         (1, True, ["fields_unverified"]))
        stored = json.loads(self.raw("SELECT field_reviews FROM complaints WHERE id = ?", (id,))[0][0])
        self.assertEqual(set(stored), {"against_entity", "complainant_name"})

        self.store.review_field(id, "requested_action", action="accept")
        # Every unverified field answered: fields_unverified alone asks for no review.
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, result("إصلاح الشبكة فوراً"), due_at=None)
        d = self.store.get(id)
        self.assertEqual((d["pending_fields"], d["needs_review"], d["review_reasons"], d["reviewed"]),
                         (0, False, [], False))
        # A national id changed by the reviewer stays through a re-analysis.
        self.store.review_field(id, "national_id", action="change", value="1098765432")
        self.store.requeue(id)
        self.store.claim_next()
        self.store.save_analysis(id, result("إصلاح الشبكة فوراً"), due_at=None)
        self.assertEqual(self.store.get(id)["national_id"], "1098765432")


class ListTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        s = self.store
        self.clock.set("2026-09-20T08:00:00Z")
        self.low = self.processed("١", analysis("municipal_services", "municipal", "low", "asir",
                                                governorate="unknown", subject="إزعاج مقهى",
                                                name="أحمد علي"),
                                  due_at="2026-10-04T08:00:00Z", filename="REPORT-noise.pdf")
        self.clock.advance(hours=1)
        self.crit_old = self.processed("٢", analysis("health_services", "health", "critical", "eastern",
                                                     governorate="unknown", subject="خطأ طبي لطفل",
                                                     national_id="١٠٩٨٧٦٥٤٣٢", needs_review=True,
                                                     review_reasons=["low_confidence"]),
                                       due_at="2026-09-21T09:00:00Z")
        self.clock.advance(hours=1)
        self.medium = self.processed("٣", analysis("water_sewage", "mewa", "medium", "riyadh",
                                                   governorate="kharj",
                                                   subject="تسرب مياه 100% في الشارع_الرئيسي"),
                                     due_at=None)
        self.clock.advance(hours=1)
        self.crit_new = self.processed("٤", analysis("health_services", "health", "critical", "riyadh",
                                                     subject="مدرسة بلا إسعاف"),
                                       due_at="2026-09-21T11:00:00Z")
        self.clock.advance(hours=1)
        self.queued = self.text("٥ لم تعالج بعد")
        self.clock.advance(minutes=5)
        s.set_status(self.low, "resolved")

    def ids(self, **kw):
        return [r["id"] for r in self.store.list(**kw)[0]]

    def test_filters(self):
        self.assertEqual(self.ids(category="health_services"), [self.crit_new, self.crit_old])
        self.assertEqual(self.ids(ministry="mewa"), [self.medium])
        self.assertEqual(self.ids(priority="critical", region="riyadh"), [self.crit_new])
        self.assertEqual(self.ids(governorate="kharj"), [self.medium])
        self.assertEqual(self.ids(governorate="riyadh_city", priority="critical"), [self.crit_new])
        self.assertEqual(self.ids(governorate="unknown"), [self.crit_old, self.low])
        self.assertEqual(self.ids(governorate="riyadh_city", region="eastern"), [])
        self.assertEqual(len(self.ids(governorate="")), 5)
        self.assertEqual(self.ids(stage="queued"), [self.queued])
        self.assertEqual(self.ids(status="resolved"), [self.low])
        self.assertEqual(self.ids(needs_review=True), [self.crit_old])
        self.assertEqual(self.ids(needs_review="1"), [self.crit_old])
        self.assertNotIn(self.crit_old, self.ids(needs_review=False))
        self.assertNotIn(self.crit_old, self.ids(needs_review="0"))
        self.assertEqual(len(self.ids(category="")), 5)       # empty means "no filter"

    def test_search_folds_arabic_digits_and_case_and_escapes_wildcards(self):
        ref = self.store.get(self.medium)["ref"]
        self.assertEqual(self.ids(q=ref.lower()), [self.medium])
        self.assertEqual(self.ids(q="احمد"), [self.low])          # أحمد without the hamza
        self.assertEqual(self.ids(q="مدرسه"), [self.crit_new])    # ة / ه
        self.assertEqual(self.ids(q="1098765432"), [self.crit_old])  # stored national id
        self.assertEqual(self.ids(q="١٠٩٨٧"), [self.crit_old])    # Arabic-Indic digits in the query
        self.assertEqual(self.ids(q="report-NOISE"), [self.low])  # filename, any case
        self.assertEqual(self.ids(q="%"), [self.medium])          # literal, not "everything"
        self.assertEqual(self.ids(q="100%"), [self.medium])
        self.assertEqual(self.ids(q="_"), [self.medium])
        self.assertEqual(self.ids(q="ع_الرئيسي"), [self.medium])
        self.assertEqual(self.ids(q="طبي_لطفل"), [])            # "_" is not "any character"
        self.assertEqual(self.ids(q="لا يوجد"), [])
        self.assertEqual(len(self.ids(q="   ")), 5)

    def test_search_finds_a_national_id_typed_as_printed(self):
        # Stored as 1098765432; a letter prints it spaced, in either digit set.
        for q in ("1098 765", "١٠٩٨ ٧٦٥", "1098 765 432", "‏١٠٩٨ ٧٦٥ ٤٣٢‏", "1098-765-432", "10 98"):
            with self.subTest(q=q):
                self.assertEqual(self.ids(q=q), [self.crit_old])
        self.assertEqual(self.ids(q="1098 766"), [])

    def test_sorts(self):
        self.assertEqual(self.ids(), [self.queued, self.crit_new, self.medium, self.crit_old, self.low])
        self.assertEqual(self.ids(order="asc"), [self.low, self.crit_old, self.medium, self.crit_new, self.queued])
        # critical > high > medium > low; ties newest first; no priority last.
        self.assertEqual(self.ids(sort="priority"),
                         [self.crit_new, self.crit_old, self.medium, self.low, self.queued])
        self.assertEqual(self.ids(sort="priority", order="asc"),
                         [self.queued, self.low, self.medium, self.crit_new, self.crit_old])
        # Open processed complaints first, closed (low: resolved) and queued
        # ones after them; undated rows last within each, in both directions.
        self.assertEqual(self.ids(sort="due"), [self.crit_new, self.crit_old, self.medium, self.low, self.queued])
        self.assertEqual(self.ids(sort="due", order="asc"),
                         [self.crit_old, self.crit_new, self.medium, self.low, self.queued])
        # What counts as open is the caller's (the taxonomy's) to say.
        self.assertEqual(self.ids(sort="due", order="asc", open_statuses=("new", "resolved")),
                         [self.crit_old, self.crit_new, self.low, self.medium, self.queued])
        self.assertEqual(self.ids(sort="updated")[0], self.low)   # the status change touched it last
        with self.assertRaises(ValueError):
            self.store.list(sort="subject; DROP TABLE complaints")
        with self.assertRaises(ValueError):
            self.store.list(order="sideways")

    def test_paging_and_totals(self):
        rows, total = self.store.list(limit=2)
        self.assertEqual(([r["id"] for r in rows], total), ([self.queued, self.crit_new], 5))
        rows, total = self.store.list(limit=2, offset=4)
        self.assertEqual(([r["id"] for r in rows], total), ([self.low], 5))
        rows, total = self.store.list(category="health_services", limit=1, offset=1)
        self.assertEqual(([r["id"] for r in rows], total), ([self.crit_old], 2))
        self.assertEqual(self.store.list(limit=-3, offset=-1), ([], 5))
        row = self.store.list(limit=1)[0][0]
        self.assertEqual(set(row), {
            "id", "ref", "created_at", "updated_at", "source", "filename", "file_kind",
            "page_count", "stage", "error", "status", "subject", "summary", "complainant_name",
            "category", "subcategory", "ministry", "priority", "region", "governorate",
            "model_category", "model_ministry", "model_priority", "model_governorate",
            "needs_review", "review_reasons", "reviewed", "due_at", "provider", "model",
            "pending_fields", "outside_jurisdiction"})
        # A queued row has no governorate yet and no reasons (a list, never NULL).
        self.assertEqual((row["governorate"], row["model_governorate"], row["review_reasons"],
                          row["outside_jurisdiction"], row["pending_fields"]), (None, None, [], False, 0))

    def test_limit_is_clamped(self):
        for i in range(LIST_MAX):
            self.text(f"شكوى رقم {i}")
        rows, total = self.store.list(limit=10_000)
        self.assertEqual((len(rows), total), (LIST_MAX, LIST_MAX + 5))


class HelperTests(StoreTestCase):
    def test_normalize_national_id(self):
        self.assertEqual(normalize_national_id("١٠٩٨٧٦٥٤٣٢"), "1098765432")
        self.assertEqual(normalize_national_id("۱۰۹۸ ۷۶۵ ۴۳۲"), "1098765432")   # Extended (Persian)
        self.assertEqual(normalize_national_id("‎1098 765\t432\n"), "1098765432")
        self.assertEqual(normalize_national_id("   "), "")
        self.assertEqual(normalize_national_id(None), "")

    def test_repeat_count_normalises_national_ids(self):
        a = self.processed("أ", analysis(national_id="1098765432"))
        self.processed("ب", analysis(national_id="١٠٩٨٧٦٥٤٣٢"))
        self.processed("ج", analysis(national_id="۱۰۹۸ ۷۶۵ ۴۳۲"))
        self.processed("د", analysis(national_id="1111111111"))
        self.processed("هـ", analysis(national_id=""))
        self.assertEqual(self.store.repeat_count("1098765432"), 3)
        self.assertEqual(self.store.repeat_count(" ١٠٩٨٧٦٥٤٣٢ "), 3)
        self.assertEqual(self.store.repeat_count("1098765432", exclude_id=a), 2)
        self.assertEqual(self.store.repeat_count(""), 0)
        self.assertEqual(self.store.repeat_count("  "), 0)
        self.assertEqual(self.store.repeat_count("2000000000"), 0)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM complaints WHERE national_id IS NULL"), [(1,)])

    def test_recent_corrections(self):
        confirmed = self.processed("أ", analysis(subject="مؤكدة"))
        old = self.processed("ب", analysis(subject="قديمة"))
        new = self.processed("ج", analysis(subject="حديثة", summary="س" * 300))
        noop = self.processed("د", analysis(subject="تصحيح بلا تغيير"))
        self.store.add_feedback(confirmed, verdict="confirm", changes={})
        self.store.add_feedback(old, verdict="correct", changes={"priority": "low"})
        self.store.add_feedback(new, verdict="correct", changes={"category": "education",
                                                                 "ministry": "education"})
        self.store.add_feedback(noop, verdict="correct", changes={"priority": "high"})  # already high
        rows = self.store.recent_corrections()
        self.assertEqual([r["subject"] for r in rows], ["حديثة", "قديمة"])
        self.assertEqual(rows[0], {"subject": "حديثة", "summary": "س" * 200,
                                   "category": "education", "ministry": "education",
                                   "priority": "high"})
        self.assertEqual(rows[1]["priority"], "low")
        self.assertEqual(len(self.store.recent_corrections(limit=1)), 1)
        # Correcting the old one again makes it the most recent precedent.
        self.store.add_feedback(old, verdict="correct", changes={"region": "makkah"})
        self.assertEqual(self.store.recent_corrections()[0]["subject"], "قديمة")

    def test_priority_sources(self):
        floor = analysis()
        floor["classification"]["priority_source"] = "rule_floor"
        a, b = self.processed("أ", analysis()), self.processed("ب", floor)
        damaged = self.processed("د", analysis())
        c = self.text("لم تعالج")
        self.store._db.execute("UPDATE complaints SET analysis = '{broken' WHERE id = ?", (damaged,))
        self.assertEqual(self.store.priority_sources([a, b, c, damaged, 999]), {a: "llm", b: "rule_floor"})
        self.assertEqual(self.store.priority_sources([]), {})

    def test_samples(self):
        first = self.processed("أ", analysis(subject="الأولى"))
        self.clock.advance(minutes=1)
        second = self.processed("ب", analysis("education", "education", "low", "riyadh",
                                              governorate="dawadmi", subject="الثانية"))
        self.text("لم تعالج")
        rows = self.store.samples()
        self.assertEqual([r["subject"] for r in rows], ["الثانية", "الأولى"])
        self.assertEqual(rows[0], {"ref": self.store.get(second)["ref"], "subject": "الثانية",
                                   "category": "education", "ministry": "education",
                                   "priority": "low", "region": "riyadh", "governorate": "dawadmi",
                                   "status": "new"})
        self.assertEqual([r["ref"] for r in self.store.samples(limit=1)], [self.store.get(second)["ref"]])
        self.assertNotEqual(first, second)

    def test_file_error_log_survives_a_cp1252_console(self):
        # An Arabic Windows words its OSError messages in Arabic.
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        failure = PermissionError(13, "تم رفض الوصول", str(self.root / "files" / "1.pdf"))
        with mock.patch.object(Path, "unlink", side_effect=failure), \
                contextlib.redirect_stdout(console):
            complaints_store._remove(self.root / "files" / "1.pdf")      # must not raise
            console.flush()
        self.assertIn(b"complaints store file error: PermissionError(13, '\\u062a\\u0645 ", raw.getvalue())

    def test_default_paths(self):
        db, files = default_paths({"CMS_DATA_DIR": str(self.root / "cms")})
        self.assertEqual((db, files), (self.root / "cms" / "complaints.db", self.root / "cms" / "files"))
        db, files = default_paths({})
        self.assertEqual(db.parent.name, "complaints")
        self.assertEqual(db.parent.parent.name, "data")

    def test_reopen_is_idempotent_and_the_owner_cleans_stale_temp_files(self):
        id = self.text("باقية بعد إعادة الفتح")
        self.store.close()
        crashed, live = self.root / "files" / ".upload-crash.tmp", self.root / "files" / ".upload-live.tmp"
        crashed.write_bytes(b"half")
        live.write_bytes(b"being written by a running app")
        old = time.time() - complaints_store.TEMP_MAX_AGE_S - 60
        os.utime(crashed, (old, old))
        store = self.open()
        self.assertEqual(store.get(id)["text"], "باقية بعد إعادة الفتح")
        # Opening alone touches no file: the queue may belong to a running app.
        self.assertTrue(crashed.exists())
        self.assertTrue(store.acquire_owner())
        self.assertFalse(crashed.exists())
        self.assertTrue(live.exists())                   # recent: maybe another app's upload
        self.assertEqual(self.raw("PRAGMA user_version"), [(SCHEMA_VERSION,)])
        self.assertEqual(self.raw("PRAGMA journal_mode"), [("wal",)])
        self.assertEqual(store._db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(store._db.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_one_owner_per_store_until_it_closes(self):
        self.assertTrue(self.store.acquire_owner())
        self.assertTrue(self.store.acquire_owner())      # already ours
        self.assertTrue(self.store.is_owner)
        second = self.open()                             # another app on the same directory
        self.assertFalse(second.acquire_owner())
        self.assertFalse(second.is_owner)
        self.store.close()
        self.assertTrue(second.acquire_owner())
        second.close()
        third = self.open()
        self.assertTrue(third.acquire_owner())

    def test_schema_2_database_gains_the_reextraction_flag(self):
        id = self.processed("نص محفوظ")
        self.store.close()
        for column in ("pending_fields", "field_reviews", "reocr"):    # as schema 2 left it
            self.raw(f"ALTER TABLE complaints DROP COLUMN {column}")
        self.raw("PRAGMA user_version = 2")
        store = self.open()
        self.assertEqual(self.raw("PRAGMA user_version"), [(SCHEMA_VERSION,)])
        self.assertEqual(self.raw("SELECT reocr FROM complaints WHERE id = ?", (id,)), [(0,)])
        fresh = self.root / "fresh" / "complaints.db"
        Store(fresh, self.root / "fresh-files").close()
        self.assertEqual(self.raw("PRAGMA table_info(complaints)"),
                         self.raw("PRAGMA table_info(complaints)", path=fresh))
        self.assertEqual((store.get(id)["text"], store.get(id)["stage"]), ("نص محفوظ", "done"))

    def test_schema_3_database_gains_field_reviews_and_pending_counts(self):
        unreviewed = self.processed("أ", paraphrased(analysis(), against_entity="شركة المياه"))
        reviewed = self.processed("ب", paraphrased(analysis(), requested_action="الإصلاح",
                                                    incident_date="أمس"))
        self.store.add_feedback(reviewed, verdict="confirm", changes={})
        clean = self.processed("ج", analysis(review_reasons=["low_confidence"]))
        damaged = self.processed("د")
        # As schema 3 left them: no review ever needed for a paraphrased value.
        self.store._db.execute("UPDATE complaints SET needs_review = 0 WHERE id != ?", (clean,))
        self.store._db.execute("UPDATE complaints SET analysis = '{broken' WHERE id = ?", (damaged,))
        self.store.close()
        for column in ("pending_fields", "field_reviews"):
            self.raw(f"ALTER TABLE complaints DROP COLUMN {column}")
        self.raw("PRAGMA user_version = 3")

        store = self.open()
        self.assertEqual(self.raw("PRAGMA user_version"), [(SCHEMA_VERSION,)])
        fresh = self.root / "fresh" / "complaints.db"
        Store(fresh, self.root / "fresh-files").close()
        self.assertEqual(self.raw("PRAGMA table_info(complaints)"),
                         self.raw("PRAGMA table_info(complaints)", path=fresh))
        state = {id: (d["pending_fields"], d["needs_review"]) for id in (unreviewed, reviewed, clean, damaged)
                 for d in [store.get(id)]}
        self.assertEqual(state, {unreviewed: (1, True), reviewed: (2, True), clean: (0, True),
                                 damaged: (0, False)})
        self.assertIs(by_key(store.get(unreviewed))["against_entity"]["pending"], True)
        self.assertEqual(store.analytics()["totals"]["fields_pending"], 2)
        d = store.review_field(unreviewed, "against_entity", action="accept")
        self.assertEqual((d["pending_fields"], d["needs_review"]), (0, False))
        # A database already carrying the columns, labelled schema 3 again,
        # keeps what the reviewers answered.
        store.close()
        self.raw("PRAGMA user_version = 3")
        again = self.open()
        self.assertEqual((again.get(unreviewed)["pending_fields"], again.get(unreviewed)["needs_review"]),
                         (0, False))
        self.assertEqual(again.get(reviewed)["pending_fields"], 2)

    def test_newer_schema_is_refused(self):
        self.store.close()
        db = sqlite3.connect(self.root / "db" / "complaints.db")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        db.close()
        with self.assertRaises(RuntimeError):
            Store(self.root / "db" / "complaints.db", self.root / "files", clock=self.clock)

    def test_schema_1_database_is_migrated_in_place(self):
        path = self.root / "old" / "complaints.db"
        path.parent.mkdir()
        old = analysis("water_sewage", "mewa", "high", "riyadh", governorate=None,
                       review_reasons=["repeat_complainant"])
        db = sqlite3.connect(path)
        for statement in V1_SCHEMA:
            db.execute(statement)
        insert = ("INSERT INTO complaints (ref, created_at, updated_at, source, filename, file_sha256, "
                  "stage, text, analysis, subject, category, subcategory, ministry, priority, region, "
                  "model_category, model_ministry, model_priority, model_region, reviewed, due_at) "
                  "VALUES (?, ?, ?, 'text', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        t = "2026-09-01T08:00:00Z"
        db.execute(insert, ("CMP-2026-000001", t, t, "قديمة", "a" * 64, "done", "نص قديم",
                            json.dumps(old, ensure_ascii=False), "طفح الصرف", "housing", "municipal",
                            "high", "riyadh", "water_sewage", "mewa", "high", "riyadh", 1,
                            "2026-09-04T08:00:00Z"))
        db.execute(insert, ("CMP-2026-000002", t, t, "تالفة", "b" * 64, "done", "نص", "{broken",
                            "", "other", "other", "low", "unknown", "other", "other", "low", "unknown",
                            0, None))
        db.execute(insert, ("CMP-2026-000003", t, t, "في الانتظار", "c" * 64, "queued", "لم تعالج",
                            None, None, None, None, None, None, None, None, None, None, 0, None))
        db.execute("INSERT INTO feedback (complaint_id, created_at, verdict, changes) VALUES "
                   "(1, ?, 'correct', ?)", (t, '{"category":{"from":"water_sewage","to":"housing"}}'))
        db.execute("INSERT INTO events (complaint_id, at, kind) VALUES (1, ?, 'created')", (t,))
        db.commit()
        db.close()

        store = Store(path, self.root / "old-files", clock=self.clock)
        self.addCleanup(store.close)
        self.assertEqual(self.raw("PRAGMA user_version", path=path), [(SCHEMA_VERSION,)])
        # Same columns in the same order as a database the new code creates.
        self.assertEqual(self.raw("PRAGMA table_info(complaints)", path=path),
                         self.raw("PRAGMA table_info(complaints)"))
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE type = 'index' "
                                  "AND name = 'complaints_governorate'", path=path),
                         [("complaints_governorate",)])
        # Old rows, their feedback and history survive; reasons come from the old analysis.
        d = store.get(1)
        self.assertEqual((d["ref"], d["subject"], d["category"], d["model_category"], d["region"],
                          d["reviewed"], d["due_at"]),
                         ("CMP-2026-000001", "طفح الصرف", "housing", "water_sewage", "riyadh", True,
                          "2026-09-04T08:00:00Z"))
        self.assertEqual((d["governorate"], d["model_governorate"], d["review_reasons"]),
                         (None, None, ["repeat_complainant"]))
        self.assertEqual(d["analysis"], old)
        self.assertEqual(d["feedback"][0]["changes"], {"category": {"from": "water_sewage", "to": "housing"}})
        self.assertEqual([e["kind"] for e in d["events"]], ["created"])
        self.assertEqual(store.get(2)["review_reasons"], [])            # damaged analysis: none
        self.assertEqual(store.get(3)["review_reasons"], [])
        self.assertEqual(store.list()[1], 3)
        self.assertEqual(store.list(governorate="riyadh_city"), ([], 0))
        a = store.analytics()
        self.assertEqual((a["totals"]["done"], a["totals"]["outside_jurisdiction"], a["by_governorate"]),
                         (2, 0, []))
        self.assertEqual((a["model_quality"]["category_agreement"], a["model_quality"]["governorate_agreement"]),
                         (0.0, None))                  # no model governorate to agree with yet
        # Net: what the register shows against the model's answer, whatever
        # the one feedback row recorded (its ministry and subcategory differ too).
        self.assertEqual(a["model_quality"]["top_corrections"],
                         [{"field": "category", "from": "water_sewage", "to": "housing", "count": 1,
                           "refs": ["CMP-2026-000001"]},
                          {"field": "ministry", "from": "mewa", "to": "municipal", "count": 1,
                           "refs": ["CMP-2026-000001"]},
                          {"field": "subcategory", "from": "medical_error", "to": None, "count": 1,
                           "refs": ["CMP-2026-000001"]}])

        # Re-analysed: the reviewer's category stays, the governorate the
        # reviewer never saw is filled from the model.
        store.requeue(1)
        self.assertEqual(store.claim_next()["id"], 1)
        store.save_analysis(1, analysis("electricity", "energy", "low", "riyadh", governorate="kharj"),
                            due_at="2026-09-15T08:00:00Z")
        d = store.get(1)
        self.assertEqual((d["category"], d["governorate"], d["model_governorate"], d["review_reasons"]),
                         ("housing", "kharj", "kharj", []))
        self.assertEqual(store.analytics()["model_quality"]["governorate_agreement"], 1.0)

        # Idempotent: a database already carrying the columns, labelled
        # schema 1 again, opens without a "duplicate column" error.
        store.close()
        self.raw("PRAGMA user_version = 1", path=path)
        again = Store(path, self.root / "old-files", clock=self.clock)
        self.addCleanup(again.close)
        self.assertEqual(again.get(1)["governorate"], "kharj")
        self.assertEqual(self.raw("PRAGMA user_version", path=path), [(SCHEMA_VERSION,)])


class OutsideJurisdictionTests(StoreTestCase):
    def open(self):
        store = Store(self.root / "db" / "complaints.db", self.root / "files", clock=self.clock,
                      home_region="riyadh")
        self.addCleanup(store.close)
        return store

    def outside(self, id):
        return (self.store.get(id)["outside_jurisdiction"],
                {r["id"]: r for r in self.store.list()[0]}[id]["outside_jurisdiction"])

    def test_the_effective_region_decides_and_the_recorded_reason_is_the_fallback(self):
        far = self.processed("١", analysis(region="eastern", governorate="unknown",
                                           review_reasons=["outside_jurisdiction"]))
        near = self.processed("٢", analysis(region="riyadh"))
        placeless = self.processed("٣", analysis(region="unknown", governorate="unknown",
                                                 review_reasons=["outside_jurisdiction"]))
        unplaced = self.processed("٤", analysis(region="unknown", governorate="unknown"))
        self.assertEqual([self.outside(i) for i in (far, near, placeless, unplaced)],
                         [(True, True), (False, False), (True, True), (False, False)])
        self.assertEqual(self.store.analytics()["totals"]["outside_jurisdiction"], 2)
        # A reviewer places the far one in Riyadh after all, and the near one in Qassim.
        self.store.add_feedback(far, verdict="correct", changes={"region": "riyadh"})
        self.store.add_feedback(near, verdict="correct", changes={"region": "qassim"})
        self.assertEqual((self.outside(far), self.outside(near)), ((False, False), (True, True)))
        self.assertEqual(self.store.get(far)["review_reasons"], ["outside_jurisdiction"])   # kept as recorded
        self.assertEqual(self.store.analytics()["totals"]["outside_jurisdiction"], 2)
        self.store.add_feedback(near, verdict="correct", changes={"region": "riyadh"})
        self.assertEqual(self.store.analytics()["totals"]["outside_jurisdiction"], 1)   # placeless only

    def test_without_a_home_region_only_the_recorded_reason_counts(self):
        self.store.close()
        store = Store(self.root / "db" / "complaints.db", self.root / "files", clock=self.clock)
        self.addCleanup(store.close)
        self.store = store
        far = self.processed("١", analysis(region="eastern", review_reasons=["outside_jurisdiction"]))
        self.store.add_feedback(far, verdict="correct", changes={"region": "riyadh"})
        self.assertTrue(self.store.get(far)["outside_jurisdiction"])
        self.assertEqual(self.store.analytics()["totals"]["outside_jurisdiction"], 1)


class AnalyticsTests(StoreTestCase):
    def test_empty_database(self):
        a = self.store.analytics()
        self.assertEqual(a["generated_at"], NOW)
        self.assertEqual(a["totals"], {"all": 0, "done": 0, "queued": 0, "processing": 0, "error": 0,
                                       "open": 0, "closed": 0, "needs_review": 0, "fields_pending": 0,
                                       "reviewed": 0, "overdue": 0, "critical_open": 0,
                                       "outside_jurisdiction": 0})
        for key in ("by_category", "by_ministry", "by_region", "by_governorate", "by_status",
                    "by_tone", "by_scope", "category_priority", "providers", "signals"):
            self.assertEqual(a[key], [], key)
        self.assertEqual(a["by_priority"], [{"id": p, "count": 0} for p in ("critical", "high", "medium", "low")])
        self.assertEqual(len(a["trend"]), 30)
        self.assertEqual(sum(d["count"] for d in a["trend"]), 0)
        self.assertEqual(a["sla"], {"on_track": 0, "due_soon": 0, "overdue": 0})
        self.assertEqual(a["processing"], {"avg_total_s": 0.0, "avg_ocr_s": 0.0,
                                           "avg_structure_s": 0.0, "avg_classify_s": 0.0})
        self.assertEqual(a["model_quality"], {"reviewed": 0, "category_agreement": None,
                                              "ministry_agreement": None, "priority_agreement": None,
                                              "region_agreement": None, "governorate_agreement": None,
                                              "top_corrections": []})

    def test_hand_built_dataset(self):
        s = self.store
        self.clock.set("2026-09-22T09:00:00Z")
        r1 = self.processed("١", analysis("health_services", "health", "high", "eastern", tone="angry",
                                          governorate="unknown", signals=["vulnerable_person"],
                                          review_reasons=["outside_jurisdiction"],
                                          timings={"ocr_s": 10, "structure_s": 3, "classify_s": 2}),
                            due_at="2026-09-25T09:00:00Z")
        r2 = self.processed("٢", analysis("water_sewage", "mewa", "high", "riyadh", scope="community",
                                          governorate="riyadh_city",
                                          signals=["health_risk", "vulnerable_person"],
                                          timings={"structure_s": 5, "classify_s": 3}),
                            due_at="2026-09-24T22:00:00Z")
        r3 = self.processed("٣", analysis("municipal_services", "municipal", "medium", "riyadh",
                                          governorate="kharj", tone="neutral", provider="allam",
                                          timings={}),
                            due_at="2026-09-28T16:00:00Z")
        r4 = self.processed("٤", analysis("municipal_services", "municipal", "low", "asir",
                                          governorate="unknown", tone="neutral", provider="allam",
                                          needs_review=True,
                                          review_reasons=["outside_jurisdiction", "addressed_elsewhere"],
                                          timings={"ocr_s": 20, "structure_s": 4, "classify_s": 6}),
                            due_at="2026-09-22T10:00:00Z")
        r5 = self.processed("٥", analysis("municipal_services", "municipal", "medium", "riyadh",
                                          governorate="kharj", scope="household",
                                          review_reasons=["addressed_elsewhere"],
                                          timings={"structure_s": 6, "classify_s": 1, "junk": "x"}),
                            due_at="2026-09-30T09:00:00Z")
        self.clock.set(NOW)
        s.add_feedback(r1, verdict="correct", changes={"priority": "critical"},
                       due_at="2026-09-24T07:00:00Z")                      # 5 h overdue
        s.add_feedback(r2, verdict="confirm", changes={})                  # due in 10 h: due soon
        s.add_feedback(r3, verdict="correct", changes={"category": "housing", "region": "qassim"})
        s.add_feedback(r5, verdict="correct", changes={"category": "housing",
                                                       "governorate": "riyadh_city"})
        s.set_status(r2, "in_review")
        s.set_status(r3, "referred")
        s.set_status(r4, "resolved")                                       # overdue but closed
        s.set_status(r5, "rejected")
        err = self.text("٦")
        s.claim_next()
        s.fail(err, "خطأ")
        self.text("٧")
        busy = s.claim_next()
        self.assertEqual(busy["stage"], "structuring")
        self.text("٨")

        a = s.analytics()
        self.assertEqual(a["totals"], {"all": 8, "done": 5, "queued": 1, "processing": 1, "error": 1,
                                       "open": 3, "closed": 2, "needs_review": 1, "fields_pending": 0,
                                       "reviewed": 4, "overdue": 1, "critical_open": 1,
                                       "outside_jurisdiction": 2})
        self.assertEqual(a["by_category"], [{"id": "housing", "count": 2},
                                            {"id": "health_services", "count": 1},
                                            {"id": "municipal_services", "count": 1},
                                            {"id": "water_sewage", "count": 1}])
        self.assertEqual(a["by_ministry"], [{"id": "municipal", "count": 3}, {"id": "health", "count": 1},
                                            {"id": "mewa", "count": 1}])
        self.assertEqual(a["by_priority"], [{"id": "critical", "count": 1}, {"id": "high", "count": 1},
                                            {"id": "medium", "count": 2}, {"id": "low", "count": 1}])
        self.assertEqual(a["by_region"], [{"id": "riyadh", "count": 2}, {"id": "asir", "count": 1},
                                          {"id": "eastern", "count": 1}, {"id": "qassim", "count": 1}])
        # Effective values: r5's reviewer moved it from kharj to the city.
        self.assertEqual(a["by_governorate"], [{"id": "riyadh_city", "count": 2},
                                               {"id": "unknown", "count": 2},
                                               {"id": "kharj", "count": 1}])
        self.assertEqual([r["id"] for r in a["by_status"]],
                         ["in_review", "new", "referred", "rejected", "resolved"])
        self.assertEqual(a["by_tone"], [{"id": "neutral", "count": 2}, {"id": "upset", "count": 2},
                                        {"id": "angry", "count": 1}])
        self.assertEqual(a["by_scope"], [{"id": "individual", "count": 3}, {"id": "community", "count": 1},
                                         {"id": "household", "count": 1}])
        self.assertEqual(a["category_priority"], [
            {"category": "housing", "critical": 0, "high": 0, "medium": 2, "low": 0},
            {"category": "health_services", "critical": 1, "high": 0, "medium": 0, "low": 0},
            {"category": "municipal_services", "critical": 0, "high": 0, "medium": 0, "low": 1},
            {"category": "water_sewage", "critical": 0, "high": 1, "medium": 0, "low": 0}])
        self.assertEqual(a["sla"], {"on_track": 1, "due_soon": 1, "overdue": 1})
        self.assertEqual(a["processing"], {"avg_total_s": 15.0, "avg_ocr_s": 15.0,
                                           "avg_structure_s": 4.5, "avg_classify_s": 3.0})
        mq = a["model_quality"]
        self.assertEqual((mq["reviewed"], mq["category_agreement"], mq["ministry_agreement"],
                          mq["priority_agreement"], mq["region_agreement"], mq["governorate_agreement"]),
                         (4, 0.5, 1.0, 0.75, 0.75, 0.75))
        ref = {r: s.get(r)["ref"] for r in (r1, r3, r5)}
        self.assertEqual(mq["top_corrections"], [
            {"field": "category", "from": "municipal_services", "to": "housing", "count": 2,
             "refs": [ref[r5], ref[r3]]},                                  # newest feedback first
            {"field": "governorate", "from": "kharj", "to": "riyadh_city", "count": 1, "refs": [ref[r5]]},
            {"field": "priority", "from": "high", "to": "critical", "count": 1, "refs": [ref[r1]]},
            {"field": "region", "from": "riyadh", "to": "qassim", "count": 1, "refs": [ref[r3]]}])
        self.assertEqual(a["providers"], [{"id": "qwen", "count": 3}, {"id": "allam", "count": 2}])
        self.assertEqual(a["signals"], [{"id": "vulnerable_person", "count": 2},
                                        {"id": "health_risk", "count": 1}])
        self.assertEqual(sum(d["count"] for d in a["trend"]), 8)            # every row, any stage
        self.assertEqual(a["trend"][-3], {"date": "2026-09-22", "count": 5})
        self.assertEqual(a["trend"][-1], {"date": "2026-09-24", "count": 3})
        # open_statuses decides what "open" means.
        self.assertEqual(s.analytics(open_statuses=("new",))["totals"]["open"], 1)

    def test_top_corrections_cite_at_most_five_refs_newest_first(self):
        s = self.store
        ids = [self.processed(f"شكوى {i}", analysis(priority="high")) for i in range(7)]
        ref = {id: s.get(id)["ref"] for id in ids}
        for id in reversed(ids):                   # the first complaint is corrected last
            self.clock.advance(minutes=1)
            s.add_feedback(id, verdict="correct", changes={"priority": "low"})
        # Corrected back and forth: still one complaint with one net correction,
        # now the newest reviewed.
        s.add_feedback(ids[3], verdict="correct", changes={"priority": "high"})
        s.add_feedback(ids[3], verdict="correct", changes={"priority": "low"})
        top = s.analytics()["model_quality"]["top_corrections"]
        self.assertEqual(top, [{"field": "priority", "from": "high", "to": "low", "count": 7,
                                "refs": [ref[ids[3]], ref[ids[0]], ref[ids[1]], ref[ids[2]],
                                         ref[ids[4]]]}])
        # A deleted complaint goes, and so does its ref.
        s.delete(ids[3])
        top = s.analytics()["model_quality"]["top_corrections"]
        self.assertEqual(top, [{"field": "priority", "from": "high", "to": "low", "count": 6,
                                "refs": [ref[ids[0]], ref[ids[1]], ref[ids[2]], ref[ids[4]], ref[ids[5]]]}])

    def test_top_corrections_are_net_model_to_final_value(self):
        s = self.store
        rows = {k: self.processed(f"شكوى {k}", analysis(governorate="kharj"))
                for k in ("back", "chain", "early", "late", "moved")}
        ref = {k: s.get(id)["ref"] for k, id in rows.items()}
        fb = lambda k, changes, verdict="correct": s.add_feedback(rows[k], verdict=verdict, changes=changes)
        # Changed and changed back (v04: kharj → unknown → kharj): nothing net.
        fb("back", {"governorate": "unknown"})
        fb("back", {"governorate": "kharj"})
        # A chain of corrections counts once, model value → final value; the
        # subcategory cleared with the first category change counts too.
        fb("chain", {"category": "water_sewage", "subcategory": ""})
        fb("chain", {"category": "municipal_services"})
        # Cited newest REVIEWED first: "early" was corrected before "late" but
        # confirmed after it.
        fb("early", {"priority": "low"})
        fb("late", {"priority": "low"})
        fb("early", {}, verdict="confirm")
        # A re-analysis that now gives the reviewer's value leaves nothing to count.
        fb("moved", {"region": "qassim", "governorate": "unknown"})
        s.requeue(rows["moved"])
        self.assertEqual(s.claim_next()["id"], rows["moved"])
        s.save_analysis(rows["moved"], analysis(region="qassim", governorate="unknown"), due_at=None)

        top = s.analytics()["model_quality"]["top_corrections"]
        self.assertEqual(top, [
            {"field": "priority", "from": "high", "to": "low", "count": 2,
             "refs": [ref["early"], ref["late"]]},
            {"field": "category", "from": "health_services", "to": "municipal_services", "count": 1,
             "refs": [ref["chain"]]},
            {"field": "subcategory", "from": "medical_error", "to": None, "count": 1,
             "refs": [ref["chain"]]}])
        # A complaint being reprocessed is left out, as in the agreement figures.
        s.requeue(rows["chain"])
        self.assertEqual([c["field"] for c in s.analytics()["model_quality"]["top_corrections"]],
                         ["priority"])

    def test_trend_buckets_by_riyadh_day(self):
        for iso in ("2026-08-25T20:59:59Z",    # 23:59:59 on 25 Aug in Riyadh: outside 30 days
                    "2026-08-25T21:00:00Z",    # 00:00 on 26 Aug in Riyadh: the first bucket
                    "2026-09-23T20:59:59Z",    # still the 23rd in Riyadh
                    "2026-09-23T21:00:00Z",    # already the 24th in Riyadh
                    "2026-09-24T11:59:59Z"):
            self.clock.set(iso)
            self.text(f"شكوى {iso}")
        self.clock.set(NOW)
        trend = self.store.analytics()["trend"]
        self.assertEqual(len(trend), 30)
        self.assertEqual(trend[0], {"date": "2026-08-26", "count": 1})
        self.assertEqual(trend[-2], {"date": "2026-09-23", "count": 1})
        self.assertEqual(trend[-1], {"date": "2026-09-24", "count": 2})
        self.assertEqual(sum(d["count"] for d in trend), 4)
        dates = [d["date"] for d in trend]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(set(dates)), 30)
        week = self.store.analytics(days=7)["trend"]
        self.assertEqual([week[0]["date"], week[-1]["date"]], ["2026-09-18", "2026-09-24"])
        # 22:00 UTC is already tomorrow in Riyadh: the window moves with it.
        self.clock.set("2026-09-24T22:00:00Z")
        self.assertEqual(self.store.analytics(days=1)["trend"], [{"date": "2026-09-25", "count": 0}])

    def test_sla_boundaries_and_fallback_deadline(self):
        cases = {"2026-09-24T11:59:59Z": "overdue",       # a second late
                 "2026-09-24T12:00:00Z": "due_soon",      # due right now: not late yet
                 "2026-09-25T12:00:00Z": "due_soon",      # exactly 24 h left
                 "2026-09-25T12:00:01Z": "on_track"}
        for due in cases:
            self.processed(due, due_at=due)
        a = self.store.analytics()
        self.assertEqual(a["sla"], {"on_track": 1, "due_soon": 2, "overdue": 1})
        self.assertEqual(self.store.analytics(due_soon_hours=0)["sla"],
                         {"on_track": 2, "due_soon": 1, "overdue": 1})
        # No stored deadline: counted only when the SLA table can derive one.
        self.clock.set("2026-09-23T06:00:00Z")
        self.processed("بلا موعد", analysis(priority="critical"), due_at=None)
        self.clock.set(NOW)
        self.assertEqual(self.store.analytics()["sla"]["overdue"], 1)
        self.assertEqual(self.store.analytics(sla_hours={"critical": 24})["sla"]["overdue"], 2)
        self.assertEqual(self.store.analytics(sla_hours={"critical": 48})["sla"]["due_soon"], 3)


class ConcurrencyTests(StoreTestCase):
    def test_parallel_creators_and_claimers_lose_and_duplicate_nothing(self):
        creators, per_creator = 4, 30
        created, claimed, errors = [], [], []
        done_creating = threading.Event()
        lock = threading.Lock()

        def create(n):
            try:
                for i in range(per_creator):
                    summary, duplicate = self.store.create(source="text", filename="t",
                                                           file_kind=None, text=f"شكوى {n}-{i}")
                    with lock:
                        created.append(summary["id"])
            except Exception as exc:          # surfaced by the assertion below
                errors.append(exc)

        def claim():
            try:
                while True:
                    item = self.store.claim_next()
                    if item is None:
                        if done_creating.is_set() and self.store.queue_state()["queued"] == 0:
                            return
                        time.sleep(0.001)
                        continue
                    with lock:
                        claimed.append(item["id"])
                    self.store.save_analysis(item["id"], analysis(), due_at=None)
            except Exception as exc:
                errors.append(exc)

        makers = [threading.Thread(target=create, args=(n,)) for n in range(creators)]
        takers = [threading.Thread(target=claim) for _ in range(3)]
        for t in makers + takers:
            t.start()
        for t in makers:
            t.join(30)
        done_creating.set()
        for t in takers:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(created), creators * per_creator)
        self.assertEqual(len(set(created)), len(created))
        self.assertEqual(sorted(claimed), sorted(created))          # each claimed exactly once
        self.assertEqual(self.raw("SELECT DISTINCT attempts, stage FROM complaints"), [(1, "done")])
        refs = self.raw("SELECT COUNT(DISTINCT ref) FROM complaints WHERE ref IS NOT NULL")
        self.assertEqual(refs, [(len(created),)])

    def test_parallel_duplicate_uploads_create_one_row(self):
        barrier = threading.Barrier(8)
        results, errors = [], []

        def upload():
            try:
                barrier.wait(10)
                results.append(self.store.create(source="upload", filename="same.pdf",
                                                 file_kind="pdf", data=b"%PDF same", file_ext="pdf"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=upload) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(dup for _, dup in results), [False] + [True] * 7)
        self.assertEqual(len({summary["id"] for summary, _ in results}), 1)
        self.assertEqual([p.name for p in (self.root / "files").iterdir()],
                         [f"{results[0][0]['id']}.pdf"])


if __name__ == "__main__":
    unittest.main()
