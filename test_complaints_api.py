import contextlib
import csv
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import complaints
import complaints_api as api
from complaints_api import ComplaintService, clean_filename, create_router, detect_upload
from complaints_llm import Provider, ProviderBusy, ProviderRegistry, ProviderUnavailable
from complaints_store import SCHEMA_VERSION, Store
from complaints_taxonomy import get_taxonomy

TAX = get_taxonomy()
NOW = datetime(2026, 9, 24, 9, 0, 0, tzinfo=timezone.utc)
SECRET = "نص سري من داخل المستند"          # must never reach an API response


def doc(n: int = 1, city: str = "الرياض", district: str = "حي النسيم") -> str:
    """A complaint as OCR or a paste delivers it; `n` keeps the bytes distinct."""
    return ("صاحب السمو الملكي أمير منطقة الرياض حفظه الله\n"
            "الموضوع: طفح المجاري أمام مدرسة ابتدائية\n"
            f"نحن سكان {district} نعاني من طفح المجاري أمام مدرسة النسيم الابتدائية منذ أسبوعين.\n"
            f"سبق أن تقدمت ببلاغ رقم {47810 + n} ولم يعالج.\n"
            "مقدم الشكوى: سالم عبدالله الحربي\n"
            "رقم الهوية: 1098765432\n"
            f"المدينة: {city}\n"
            f"الحي: {district}")


def pdf(n: int = 1) -> bytes:
    return b"%PDF-1.4\n%" + str(n).encode() + b"\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

STRUCT = {"addressed_to": "صاحب السمو الملكي أمير منطقة الرياض حفظه الله",
          "complainant_name": "سالم عبدالله الحربي", "national_id": "1098765432", "phone": "",
          "email": "", "city": "الرياض", "district_or_address": "حي النسيم", "region": "riyadh",
          "governorate": "riyadh_city",
          "against_entity": "", "incident_date": "منذ أسبوعين", "submission_date": "",
          "reference_numbers": [], "requested_action": "",
          "key_facts": ["طفح أمام مدرسة ابتدائية"],
          "subject": "طفح المجاري أمام مدرسة ابتدائية",
          "summary": "يشكو السكان من طفح المجاري أمام مدرسة.", "is_complaint": True}
CLS = {"evidence": ["نعاني من طفح المجاري أمام مدرسة النسيم الابتدائية"],
       "rationale": "طفح صرف صحي أمام مدرسة. الأولوية: عالية",
       "category": "water_sewage", "subcategory": "sewage_overflow", "ministry": "mewa",
       "priority": "high", "priority_factors": ["health_risk"], "affected_scope": "community",
       "tone": "neutral", "confidence": "high"}


class FakeProvider(Provider):
    """Answers by schema (structuring, classification, insights), so any number
    of complaints can be processed. `script` holds exceptions (or JSON strings)
    served before the normal answers."""

    def __init__(self, id="qwen", *, release_after_batch=False):
        self.id, self.label, self.model, self.n_ctx = id, id.upper(), f"{id}-model.gguf", 8192
        self.local, self.release_after_batch = True, release_after_batch
        self.script, self.calls, self.ready, self.released = [], 0, 0, 0
        self.unavailable, self.struct, self.cls = False, dict(STRUCT), dict(CLS)
        self.seen_refs = []

    def ensure_ready(self):
        self.ready += 1
        if self.unavailable:
            raise ProviderUnavailable("تعذر تشغيل النموذج المحلي")

    def release(self):
        self.released += 1

    def status(self):
        return self._describe("ready")

    def chat_json(self, messages, schema, max_tokens, temperature=0.0):
        self.calls += 1
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step, "stop"
        props = schema["properties"]
        if "is_complaint" in props:
            return json.dumps(self.struct, ensure_ascii=False), "stop"
        if "headline" in props:
            refs = props["insights"]["items"]["properties"]["refs"]["items"].get("enum", [])
            self.seen_refs = list(refs)
            return json.dumps({
                "insights": [{"title": "تركز الشكاوى", "detail": "شكوى واحدة عن الصرف.",
                              "refs": refs[:1] + ["CMP-1999-000001"]}],
                "recommendations": [{"ministry": "mewa", "action": "معالجة الطفح", "priority": "high"}],
                "watch": ["شكاوى الصرف"], "headline": "الصرف الصحي في المقدمة"},
                ensure_ascii=False), "stop"
        return json.dumps(self.cls, ensure_ascii=False), "stop"


class Harness:
    def __init__(self, test, *, autostart=False, ocr_text=None):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        test.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "c.db"), os.path.join(self.tmp.name, "files"),
                           clock=lambda: NOW)
        test.addCleanup(self.store.close)
        self.qwen, self.allam = FakeProvider("qwen"), FakeProvider("allam", release_after_batch=True)
        self.registry = ProviderRegistry([self.qwen, self.allam], "qwen")
        self.ocr_calls, self.ocr_text, self.ocr_error = [], ocr_text if ocr_text is not None else doc(1), None
        self.pages = 2                          # what the page counter reports for a PDF
        self.service = ComplaintService(self.store, TAX, self.registry, ocr=self.ocr,
                                        page_count=lambda data: self.pages,
                                        autostart=autostart, clock=lambda: NOW)
        self.service.busy_wait = 0
        test.addCleanup(self.service.stop, 2)
        app = FastAPI()
        app.include_router(create_router(self.service))
        self.client = TestClient(app)

    def ocr(self, data, filename):
        self.ocr_calls.append((data, filename))
        if self.ocr_error:
            raise self.ocr_error
        return SimpleNamespace(full_text=self.ocr_text, page_count=2)

    def paste(self, text=None, **extra):
        r = self.client.post("/complaints/text", json={"text": text or doc(1), **extra})
        assert r.status_code == 200, r.text
        return r.json()["item"]

    def process(self):
        with quiet():
            return self.service.process_next()

    def drain(self):
        n = 0
        while self.process():
            n += 1
        return n

    def detail(self, id):
        return self.client.get(f"/complaints/items/{id}").json()


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def upload(client, *files):
    return client.post("/complaints/upload", files=[("files", f) for f in files])


class HelperTests(unittest.TestCase):
    def test_clean_filename(self):
        self.assertEqual(clean_filename("C:\\fakepath\\شكوى المياه.pdf"), "شكوى المياه.pdf")
        self.assertEqual(clean_filename("../../etc/passwd"), "passwd")
        self.assertEqual(clean_filename("a\x00b\r\n\tc.pdf"), "abc.pdf")
        # A right-to-left override would make «evil\u202efdp.exe» display as «evilexe.pdf».
        self.assertEqual(clean_filename("evil\u202efdp.exe"), "evilfdp.exe")
        self.assertEqual(clean_filename("  شكوى   \u2066رقم\u2069  ٣.txt "), "شكوى رقم ٣.txt")
        long = clean_filename("ش" * 300 + ".pdf")
        self.assertEqual(len(long), api.MAX_FILENAME)
        self.assertTrue(long.endswith(".pdf"))
        self.assertEqual(len(clean_filename("x" * 300)), api.MAX_FILENAME)
        self.assertEqual(clean_filename(None), "")
        self.assertEqual(clean_filename("شكوى/مياه", path=False), "شكوى/مياه")

    def test_detect_upload(self):
        self.assertEqual(detect_upload(pdf(), "x.bin"), ("pdf", "pdf"))
        self.assertEqual(detect_upload(PNG, "scan"), ("image", "png"))
        self.assertEqual(detect_upload(b"\xff\xd8\xff\xe0" + b"0" * 20), ("image", "jpg"))
        self.assertEqual(detect_upload(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ("image", "webp"))
        self.assertEqual(detect_upload(b"II*\x00" + b"0" * 12), ("image", "tif"))
        self.assertEqual(detect_upload(b"MM\x00*" + b"0" * 12), ("image", "tif"))
        self.assertEqual(detect_upload(b"BM" + b"0" * 30), ("image", "bmp"))
        self.assertEqual(detect_upload("نص".encode(), "a.TXT"), ("txt", "txt"))
        self.assertEqual(detect_upload(b"hello", "note", "text/plain; charset=utf-8"), ("txt", "txt"))
        # A PDF header after a little junk counts only for a .pdf name.
        self.assertEqual(detect_upload(b"\r\n junk %PDF-1.7", "a.pdf"), ("pdf", "pdf"))
        self.assertIsNone(detect_upload(b"\r\n junk %PDF-1.7", "a.bin"))
        # Never by name alone: a renamed executable, a GIF.
        self.assertIsNone(detect_upload(b"MZ\x90\x00", "complaint.pdf", "application/pdf"))
        self.assertIsNone(detect_upload(b"GIF89a....", "a.gif", "image/gif"))
        self.assertIsNone(detect_upload(b"", "a.png"))

    def test_log_escapes_arabic_in_the_message_too(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(console):
            api._log("complaints: ملف", RuntimeError(SECRET))
            api._log("complaints: " + SECRET)
            console.flush()
        out = raw.getvalue()
        self.assertIn(b"complaints: \\u0645\\u0644\\u0641 RuntimeError(", out)
        self.assertIn(("complaints: " + ascii(SECRET)[1:-1]).encode(), out)

    def test_decode_text(self):
        self.assertEqual(api.decode_text("\ufeffسطر\r\nثان\rثالث".encode()), "سطر\nثان\nثالث")
        self.assertIsNone(api.decode_text(b"\xff\xfe\x00a"))
        self.assertIsNone(api.decode_text(b"a\x00b"))

    def test_default_page_count_reads_real_pdfs_under_the_pdfium_lock(self):
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument.new()
        for _ in range(3):
            doc.new_page(200, 300)
        buf = io.BytesIO()
        doc.save(buf)
        doc.close()
        self.assertEqual(api._default_page_count(buf.getvalue()), 3)
        self.assertEqual(api._default_page_count(b"%PDF-1.4 not really"), 0)     # OCR will say why
        # The lock the app's other PDFium users hold, without importing extract.
        own, surya = threading.Lock(), threading.Lock()
        with mock.patch.dict(sys.modules, {"extract": SimpleNamespace(PDFIUM_LOCK=own, _infer_lock=surya)}):
            self.assertIs(api._pdfium_lock(), own)
        with mock.patch.dict(sys.modules, {"extract": SimpleNamespace(_infer_lock=surya)}):
            self.assertIs(api._pdfium_lock(), surya)
            with surya:                           # taken: the count waits for it
                counter = threading.Thread(target=api._default_page_count, args=(buf.getvalue(),))
                counter.start()
                counter.join(0.2)
                self.assertTrue(counter.is_alive())
            counter.join(5)
            self.assertFalse(counter.is_alive())
        with mock.patch.dict(sys.modules, {"extract": None}):
            self.assertIs(api._pdfium_lock(), api._PDFIUM_FALLBACK)

    def test_default_ocr_keeps_complaints_out_of_the_analysis_tab_progress(self):
        calls = []

        def quiet_capable(data, filename="document", progress=True):
            calls.append((filename, progress))

        def plain(data, filename="document"):
            calls.append((filename, "no option"))
        for fn in (quiet_capable, plain):
            with mock.patch.dict(sys.modules, {"extract": SimpleNamespace(extract_document=fn)}):
                api._default_ocr(b"%PDF", "7.pdf")
        self.assertEqual(calls, [("7.pdf", False), ("7.pdf", "no option")])

    def test_register_searches_are_cut_from_the_access_log(self):
        access = logging.getLogger("uvicorn.access")
        self.addCleanup(lambda: [access.removeFilter(f) for f in list(access.filters)
                                 if isinstance(f, api._RedactQuery)])
        Harness(self)
        Harness(self)                              # once, however many routers
        self.assertEqual(sum(isinstance(f, api._RedactQuery) for f in access.filters), 1)
        fmt = '%s - "%s %s HTTP/%s" %d'

        def line(path):
            record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, fmt,
                                       ("127.0.0.1:50000", "GET", path, "1.1", 200), None)
            self.assertTrue(access.filter(record))
            return record.getMessage()
        searched = line("/complaints/items?q=1098765432&limit=100")
        self.assertNotIn("1098765432", searched)
        self.assertIn('"GET /complaints/items?[redacted] HTTP/1.1" 200', searched)
        self.assertNotIn("%D8%B3", line("/complaints/export.csv?q=%D8%B3%D8%A7%D9%84%D9%85"))
        self.assertIn("/complaints/items/3 HTTP", line("/complaints/items/3"))
        self.assertIn("/progress?x=1", line("/progress?x=1"))                  # other tabs: as they were

    def test_import_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "cms")
            code = ("import sys, threading, complaints_api\n"
                    "assert threading.active_count() == 1, threading.enumerate()\n"
                    "bad = [m for m in ('extract', 'torch', 'surya', 'llm') if m in sys.modules]\n"
                    "assert not bad, bad\n")
            env = {**os.environ, "CMS_DATA_DIR": data_dir}
            proc = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(os.path.abspath(__file__)),
                                  env=env, capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertFalse(os.path.exists(data_dir))


class ConfigAndProviderTests(unittest.TestCase):
    def test_assets(self):
        h = Harness(self)
        js, css = h.client.get("/complaints/ui.js"), h.client.get("/complaints/ui.css")
        self.assertEqual(js.status_code, 200)
        self.assertTrue(js.headers["content-type"].startswith("text/javascript"))
        self.assertEqual(css.status_code, 200)
        self.assertTrue(css.headers["content-type"].startswith("text/css"))

    def test_config_shape(self):
        h = Harness(self)
        data = h.client.get("/complaints/config").json()
        self.assertEqual(data["taxonomy"], TAX.to_public())
        # The receiving entity and its governorates come from the taxonomy, not the code.
        entity = data["taxonomy"]["receiving_entity"]
        self.assertEqual(set(entity), {"id", "label_ar", "label_en", "region", "desk_ar"})
        self.assertEqual((entity["id"], entity["region"]), ("riyadh_emirate", "riyadh"))
        governorates = data["taxonomy"]["governorates"]
        self.assertEqual(set(governorates[0]), {"id", "label_ar", "label_en"})
        self.assertTrue({"riyadh_city", "kharj", "unknown"} <= {g["id"] for g in governorates})
        self.assertEqual([p["id"] for p in data["providers"]], ["qwen", "allam"])
        self.assertEqual(set(data["providers"][0]), {"id", "label", "model", "n_ctx", "local", "status"})
        self.assertEqual(data["active_provider"], "qwen")
        self.assertEqual(data["limits"], {"max_files": 20, "max_bytes": 104857600, "max_text_chars": 40000})

    def test_provider_switch_and_errors(self):
        h = Harness(self)
        r = h.client.put("/complaints/provider", json={"provider": "allam"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active_provider"], "allam")
        self.assertEqual([p["id"] for p in r.json()["providers"]], ["qwen", "allam"])
        for body in ({"provider": "gpt"}, {"provider": ["allam"]}, {}, {"provider": None}):
            r = h.client.put("/complaints/provider", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn("error", r.json())
        self.assertEqual(h.registry.active().id, "allam")          # unchanged by the failures
        self.assertEqual(h.client.put("/complaints/provider", content="allam").status_code, 415)
        self.assertEqual(h.client.put("/complaints/provider", content="{",
                                      headers={"Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(h.client.put("/complaints/provider", content="[" * 50000,
                                      headers={"Content-Type": "application/json"}).status_code, 400)
        big = json.dumps({"provider": "qwen", "pad": "x" * api.MAX_JSON_BYTES})
        self.assertEqual(h.client.put("/complaints/provider", content=big,
                                      headers={"Content-Type": "application/json"}).status_code, 413)

    def test_switch_applies_to_next_item_and_allam_is_released_after_batch(self):
        h = Harness(self)
        first, second = h.paste(doc(1)), h.paste(doc(2))
        self.assertTrue(h.process())
        self.assertEqual(h.detail(first["id"])["provider"], "qwen")
        h.client.put("/complaints/provider", json={"provider": "allam"})
        self.assertTrue(h.process())
        self.assertEqual(h.detail(second["id"])["provider"], "allam")
        self.assertEqual(h.detail(second["id"])["analysis"]["model"], "allam-model.gguf")
        self.assertEqual(h.allam.released, 0)             # not while the batch runs
        self.assertFalse(h.process())                      # queue drained
        self.assertEqual(h.allam.released, 1)
        self.assertEqual(h.qwen.released, 0)               # qwen is shared and resident
        self.assertFalse(h.process())
        self.assertEqual(h.allam.released, 1)              # nothing used since: nothing to free


class UploadTests(unittest.TestCase):
    def test_several_files_duplicate_txt_and_rejections(self):
        h = Harness(self)
        txt = "\ufeff".encode() + doc(3).replace("\n", "\r\n").encode()
        r = upload(h.client,
                   ("C:\\fakepath\\شكوى.pdf", pdf(1), "application/pdf"),
                   ("scan.png", PNG, "image/png"),
                   ("again.pdf", pdf(1), "application/pdf"),
                   ("note.txt", txt, "text/plain"),
                   ("bad.txt", b"\xff\xfe\xfa", "text/plain"),
                   ("blank.txt", "  \n ".encode(), "text/plain"),
                   ("tool.exe", b"MZ\x90\x00\x03", "application/octet-stream"),
                   ("fake.pdf", b"MZ\x90\x00\x03", "application/pdf"),
                   ("empty.pdf", b"", "application/pdf"))
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        self.assertEqual([it["filename"] for it in items],
                         ["شكوى.pdf", "scan.png", "again.pdf", "note.txt", "bad.txt", "blank.txt",
                          "tool.exe", "fake.pdf", "empty.pdf"])
        a, png, again, note = items[:4]
        self.assertEqual((a["stage"], a["duplicate"]), ("queued", False))
        self.assertTrue(a["ref"].startswith("CMP-2026-"))
        self.assertEqual((again["id"], again["duplicate"]), (a["id"], True))
        self.assertFalse(png["duplicate"])
        self.assertIn("UTF-8", items[4]["error"])
        self.assertIn("error", items[5])
        self.assertEqual(items[6]["error"], api.MSG_UNSUPPORTED)
        self.assertEqual(items[7]["error"], api.MSG_UNSUPPORTED)
        self.assertEqual(items[8]["error"], "الملف فارغ")
        # Stored under <id>.<ext>, the extension from the content, not the name.
        files = sorted(os.listdir(h.store.files_dir))
        self.assertEqual(files, sorted([f"{a['id']}.pdf", f"{png['id']}.png", f"{note['id']}.txt"]))
        # A .txt carries its text: BOM gone, CRLF folded, no OCR needed.
        detail = h.detail(note["id"])
        self.assertEqual(detail["text"], doc(3))
        self.assertEqual(detail["file_kind"], "txt")
        self.assertEqual(h.client.get("/complaints/items?limit=50").json()["total"], 3)

    def test_txt_upload_skips_ocr_and_pdf_goes_through_it(self):
        h = Harness(self)
        items = upload(h.client, ("a.txt", doc(5).encode(), "text/plain"),
                       ("b.pdf", pdf(9), "application/pdf")).json()["items"]
        h.drain()
        self.assertEqual(len(h.ocr_calls), 1)
        # OCR gets the stored name (<id>.<ext> by content), not the display name.
        self.assertEqual(h.ocr_calls[0], (pdf(9), f"{items[1]['id']}.pdf"))
        pdf_detail = h.detail(items[1]["id"])
        self.assertEqual(pdf_detail["stage"], "done")
        self.assertEqual(pdf_detail["page_count"], 2)
        self.assertEqual(pdf_detail["text"], doc(1))
        self.assertEqual([e["detail"].get("stage") for e in reversed(pdf_detail["events"])
                          if e["kind"] == "stage"], ["ocr", "structuring", "classifying"])
        self.assertEqual(h.detail(items[0]["id"])["stage"], "done")

    def test_an_image_named_pdf_is_read_by_its_stored_name(self):
        # Scanner and phone apps save JPEGs as «scan.pdf». Intake types the
        # file by its bytes; OCR, which types by name first, must agree.
        h = Harness(self)
        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 40
        item = upload(h.client, ("scan.pdf", jpeg, "application/pdf")).json()["items"][0]
        self.assertEqual(h.detail(item["id"])["file_kind"], "image")
        h.process()
        self.assertEqual(h.ocr_calls, [(jpeg, f"{item['id']}.jpg")])
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["filename"]), ("done", "scan.pdf"))   # the register keeps the user's name

    def test_too_many_files_and_no_files(self):
        h = Harness(self)
        r = upload(h.client, *[(f"{i}.pdf", pdf(i), "application/pdf") for i in range(api.MAX_FILES + 1)])
        self.assertEqual(r.status_code, 413)
        self.assertIn("error", r.json())
        self.assertEqual(h.client.get("/complaints/items").json()["total"], 0)
        ok = upload(h.client, *[(f"{i}.pdf", pdf(i), "application/pdf") for i in range(api.MAX_FILES)])
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.json()["items"]), api.MAX_FILES)
        self.assertEqual(h.client.post("/complaints/upload", data={"note": "x"}).status_code, 400)
        self.assertEqual(h.client.post("/complaints/upload", files=[("other", ("a.pdf", pdf(), "application/pdf"))]).status_code, 400)
        self.assertEqual(h.client.post("/complaints/upload", json={"files": []}).status_code, 400)
        broken = h.client.post("/complaints/upload", content=b"--x\r\nnonsense",
                               headers={"Content-Type": "multipart/form-data; boundary=x"})
        self.assertEqual(broken.status_code, 400)

    def test_per_file_cap_is_an_item_error(self):
        h = Harness(self)
        with mock.patch.object(api, "MAX_BYTES", 100):
            r = upload(h.client, ("big.pdf", pdf(1) + b"0" * 200, "application/pdf"),
                       ("small.pdf", pdf(2), "application/pdf"))
            self.assertEqual(r.status_code, 200)
            big, small = r.json()["items"]
            self.assertIn("حجم الملف", big["error"])
            self.assertNotIn("id", big)
            self.assertIn("id", small)
            self.assertEqual(h.client.get("/complaints/config").json()["limits"]["max_bytes"], 100)

    def test_content_length_over_the_request_cap_is_413(self):
        h = Harness(self)
        with mock.patch.multiple(api, MAX_BYTES=100, MAX_FILES=2, FORM_OVERHEAD=50):
            r = upload(h.client, ("a.pdf", pdf(1) + b"0" * 300, "application/pdf"))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(os.listdir(h.store.files_dir), [])

    def test_declared_length_is_refused_before_reading(self):
        h = Harness(self)
        form = (b"--b\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.pdf\"\r\n"
                b"Content-Type: application/pdf\r\n\r\n" + pdf(1) + b"\r\n--b--\r\n")
        limit = api.MAX_FILES * api.MAX_BYTES + api.FORM_OVERHEAD
        headers = {"Content-Type": "multipart/form-data; boundary=b"}
        r = h.client.post("/complaints/upload", content=form,
                          headers={**headers, "Content-Length": str(limit + 1)})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(h.client.get("/complaints/items").json()["total"], 0)
        r = h.client.post("/complaints/upload", content=form, headers=headers)
        self.assertEqual((r.status_code, r.json()["items"][0]["filename"]), (200, "a.pdf"))

    def test_body_without_length_is_still_capped(self):
        h = Harness(self)
        form = (b"--b\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.pdf\"\r\n"
                b"Content-Type: application/pdf\r\n\r\n" + pdf(1) + b"0" * 500 + b"\r\n--b--\r\n")

        def chunks():
            for i in range(0, len(form), 64):
                yield form[i:i + 64]
        with mock.patch.multiple(api, MAX_BYTES=100, MAX_FILES=2, FORM_OVERHEAD=50):
            r = h.client.post("/complaints/upload", content=chunks(),
                              headers={"Content-Type": "multipart/form-data; boundary=b"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(h.client.get("/complaints/items").json()["total"], 0)


class TextTests(unittest.TestCase):
    def test_paste_duplicate_and_validation(self):
        h = Harness(self)
        first = h.paste(doc(1), title="  بلاغ\u202e المياه ")
        self.assertEqual((first["stage"], first["duplicate"]), ("queued", False))
        self.assertEqual(first["filename"], "بلاغ المياه")
        again = h.paste(doc(1))
        self.assertEqual((again["id"], again["duplicate"]), (first["id"], True))
        post = lambda body: h.client.post("/complaints/text", json=body)
        self.assertEqual(post({"text": "   \n "}).status_code, 400)
        self.assertEqual(post({"text": 5}).status_code, 400)
        self.assertEqual(post({}).status_code, 400)
        self.assertEqual(post({"text": doc(2), "title": ["x"]}).status_code, 400)
        self.assertEqual(post({"text": "ش" * (api.MAX_TEXT_CHARS + 1)}).status_code, 413)
        self.assertEqual(h.client.post("/complaints/text", content=doc(2).encode()).status_code, 415)
        self.assertEqual(h.client.post("/complaints/text", data={"text": doc(2)}).status_code, 415)

    def test_longest_arabic_text_fits_the_body_cap(self):
        # 40 000 Arabic characters are ~80 KB of UTF-8: beyond the 64 KB cap
        # of the other JSON bodies, so /text has its own.
        h = Harness(self)
        text = ("شكوى " * 8000)[:api.MAX_TEXT_CHARS]
        r = h.client.post("/complaints/text", json={"text": text})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(h.detail(r.json()["item"]["id"])["text"]), api.MAX_TEXT_CHARS)
        body = json.dumps({"text": text}, ensure_ascii=True)            # escaped: 6 bytes each
        self.assertEqual(h.client.post("/complaints/text", content=body,
                                       headers={"Content-Type": "application/json"}).status_code, 200)

    def test_crlf_is_folded(self):
        h = Harness(self)
        item = h.paste(doc(4).replace("\n", "\r\n"))
        self.assertEqual(h.detail(item["id"])["text"], doc(4))


class RegisterTests(unittest.TestCase):
    def test_items_filters_and_invalid_values(self):
        h = Harness(self)
        a, b = h.paste(doc(1)), h.paste(doc(2))
        h.process()                       # a is done, b still queued
        get = lambda qs: h.client.get("/complaints/items?" + qs)
        self.assertEqual(get("").json()["total"], 2)
        self.assertEqual([r["id"] for r in get("stage=queued").json()["items"]], [b["id"]])
        done = get("category=water_sewage&ministry=mewa&priority=high&region=riyadh"
                   "&governorate=riyadh_city&status=new").json()
        self.assertEqual([r["id"] for r in done["items"]], [a["id"]])
        row = done["items"][0]
        self.assertEqual((row["governorate"], row["model_governorate"]), ("riyadh_city", "riyadh_city"))
        self.assertIsInstance(row["review_reasons"], list)
        self.assertNotIn("outside_jurisdiction", row["review_reasons"])
        self.assertEqual(get("governorate=kharj").json()["total"], 0)
        self.assertEqual(get("category=&stage=&governorate=").json()["total"], 2)   # empty = no filter
        self.assertEqual(get("needs_review=0").json()["total"],
                         2 - get("needs_review=1").json()["total"])
        self.assertEqual(get("q=" + a["ref"]).json()["total"], 1)
        self.assertEqual(get("q=%D8%B3%D8%A7%D9%84%D9%85").json()["total"], 1)   # «سالم»: only the done one has a name
        self.assertEqual(len(get("limit=1").json()["items"]), 1)
        self.assertEqual(get("limit=1&offset=1&sort=priority&order=asc").json()["total"], 2)
        for bad in ("category=plumbing", "ministry=x", "priority=urgent", "region=paris",
                    "governorate=paris", "governorate=riyadh",           # a region id, not a governorate
                    "status=open", "stage=running", "needs_review=2", "sort=name", "order=up",
                    "limit=abc", "limit=-1", "offset=1.5", "q=" + "x" * 201):
            r = get(bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("error", r.json())
        self.assertEqual(len(get("limit=100000").json()["items"]), 2)          # clamped, not refused

    def test_detail_acknowledgment_only_when_done(self):
        h = Harness(self)
        item = h.paste(doc(1))
        before = h.detail(item["id"])
        self.assertEqual(before["stage"], "queued")
        self.assertNotIn("acknowledgment", before)
        h.process()
        after = h.detail(item["id"])
        self.assertEqual(after["stage"], "done")
        self.assertIn(item["ref"], after["acknowledgment"])
        self.assertIn("وزارة البيئة والمياه والزراعة", after["acknowledgment"])
        self.assertEqual(after["analysis"]["classification"]["category"], "water_sewage")
        for bad in ("999", "abc", "1.0", "-1", "9" * 30):
            r = h.client.get(f"/complaints/items/{bad}")
            self.assertEqual(r.status_code, 404, bad)
            self.assertEqual(r.json(), {"error": api.MSG_NOT_FOUND})

    def test_file_endpoint(self):
        h = Harness(self)
        items = upload(h.client, ("شكوى.pdf", pdf(1), "application/pdf"), ("s.png", PNG, "image/png"),
                       ("n.txt", "\ufeffسطر".encode(), "text/plain")).json()["items"]
        pasted = h.paste(doc(2))
        cases = [(items[0], "application/pdf", "pdf", pdf(1)), (items[1], "image/png", "png", PNG),
                 (items[2], "text/plain; charset=utf-8", "txt", "\ufeffسطر".encode())]
        for item, media, ext, content in cases:
            r = h.client.get(f"/complaints/items/{item['id']}/file")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], media)
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertEqual(r.headers["content-disposition"], f'inline; filename="complaint-{item["id"]}.{ext}"')
            self.assertEqual(r.content, content)
        # A pasted complaint has no original file: its text is in the detail.
        r = h.client.get(f"/complaints/items/{pasted['id']}/file")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"error": api.MSG_NO_ORIGINAL})
        self.assertEqual(h.client.get("/complaints/items/999/file").status_code, 404)
        self.assertEqual(h.client.get("/complaints/items/..%2F..%2Fc.db/file").status_code, 404)

    def test_status_endpoint(self):
        h = Harness(self)
        item = h.paste(doc(1))
        r = h.client.post(f"/complaints/items/{item['id']}/status", json={"status": "in_review", "note": " بدأنا "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "in_review")
        self.assertEqual(r.json()["events"][0]["detail"], {"from": "new", "to": "in_review", "note": "بدأنا"})
        for body in ({"status": "closed"}, {"status": 1}, {}, {"status": "resolved", "note": 5},
                     {"status": "resolved", "note": "x" * 1001}):
            self.assertEqual(h.client.post(f"/complaints/items/{item['id']}/status", json=body).status_code, 400, body)
        self.assertEqual(h.client.post("/complaints/items/999/status", json={"status": "resolved"}).status_code, 404)
        self.assertEqual(h.client.post(f"/complaints/items/{item['id']}/status", content="x").status_code, 415)

    def test_delete(self):
        h = Harness(self)
        item = upload(h.client, ("a.pdf", pdf(1), "application/pdf")).json()["items"][0]
        path = os.path.join(h.store.files_dir, f"{item['id']}.pdf")
        self.assertTrue(os.path.exists(path))
        h.store.claim_next()                                   # now being processed
        self.assertEqual(h.client.delete(f"/complaints/items/{item['id']}").status_code, 409)
        h.store.fail(item["id"], "x")
        r = h.client.delete(f"/complaints/items/{item['id']}")
        self.assertEqual((r.status_code, r.json()), (200, {"deleted": True}))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(h.client.delete(f"/complaints/items/{item['id']}").status_code, 404)
        self.assertEqual(h.client.get(f"/complaints/items/{item['id']}").status_code, 404)

    def test_due_sort_puts_closed_complaints_last(self):
        h = Harness(self)
        a, b = h.paste(doc(1)), h.paste(doc(2))
        h.drain()                                                   # the same deadline
        h.client.post(f"/complaints/items/{a['id']}/status", json={"status": "resolved"})
        rows = h.client.get("/complaints/items?sort=due&order=asc").json()["items"]
        self.assertEqual([r["id"] for r in rows], [b["id"], a["id"]])

    def test_queue_endpoint(self):
        h = Harness(self)
        h.paste(doc(1))
        h.paste(doc(2))
        q = h.client.get("/complaints/queue").json()
        self.assertEqual(q, {"queued": 2, "processing": None, "errors": 0,
                             "worker": "stopped", "active_provider": "qwen"})
        claimed = h.store.claim_next()
        q = h.client.get("/complaints/queue").json()
        self.assertEqual(q["processing"]["id"], claimed["id"])
        self.assertEqual(q["queued"], 1)


class ReprocessAndFeedbackTests(unittest.TestCase):
    def test_reprocess(self):
        h = Harness(self)
        item = upload(h.client, ("a.pdf", pdf(1), "application/pdf")).json()["items"][0]
        h.store.claim_next()
        url = f"/complaints/items/{item['id']}/reprocess"
        r = h.client.post(url, json={"ocr": False})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"error": api.MSG_PROCESSING})
        h.store.fail(item["id"], "x")
        r = h.client.post(url, json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["item"]["stage"], "queued")
        h.drain()
        self.assertEqual(len(h.ocr_calls), 1)
        h.ocr_text = doc(7)
        self.assertEqual(h.client.post(url, json={"ocr": True}).status_code, 200)
        h.drain()
        self.assertEqual(len(h.ocr_calls), 2)                       # text was cleared and re-read
        self.assertEqual(h.detail(item["id"])["text"], doc(7))
        self.assertEqual(h.client.post(url, json={"ocr": "yes"}).status_code, 400)
        self.assertEqual(h.client.post("/complaints/items/999/reprocess", json={}).status_code, 404)
        self.assertEqual(h.client.post(url, content="{}").status_code, 415)

    def test_feedback_validation(self):
        h = Harness(self)
        item = h.paste(doc(1))
        url = f"/complaints/items/{item['id']}/feedback"
        r = h.client.post(url, json={"verdict": "confirm"})
        self.assertEqual(r.status_code, 409)                        # not processed yet
        h.process()
        bad = [{"verdict": "maybe"}, {},
               {"verdict": "correct", "changes": []},
               {"verdict": "correct", "changes": {"status": "resolved"}},
               {"verdict": "correct", "changes": {"category": "plumbing"}},
               {"verdict": "correct", "changes": {"ministry": ""}},
               {"verdict": "correct", "changes": {"priority": None}},
               {"verdict": "correct", "changes": {"region": 3}},
               {"verdict": "correct", "changes": {"governorate": "riyadh"}},       # a region id
               {"verdict": "correct", "changes": {"governorate": ""}},
               {"verdict": "correct", "changes": {"governorate": ["kharj"]}},
               {"verdict": "correct", "changes": {"subcategory": "no_such_sub"}},
               {"verdict": "correct", "changes": {"subcategory": "medical_error"}},     # health, not water
               {"verdict": "correct", "changes": {"category": "health_services", "subcategory": "water_outage"}},
               {"verdict": "confirm", "note": "x" * 1001},
               {"verdict": "confirm", "reviewer": "x" * 101},
               {"verdict": "confirm", "note": {"a": 1}}]
        for body in bad:
            r = h.client.post(url, json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn("error", r.json())
        self.assertEqual(h.detail(item["id"])["reviewed"], False)             # nothing applied
        self.assertEqual(h.client.post("/complaints/items/999/feedback", json={"verdict": "confirm"}).status_code, 404)
        big = json.dumps({"verdict": "confirm", "note": "x" * api.MAX_JSON_BYTES})
        self.assertEqual(h.client.post(url, content=big, headers={"Content-Type": "application/json"}).status_code, 413)

    def test_confirm_and_correct(self):
        h = Harness(self)
        item = h.paste(doc(1))
        h.process()
        url = f"/complaints/items/{item['id']}/feedback"
        start = h.detail(item["id"])
        self.assertEqual(start["due_at"], "2026-09-27T09:00:00Z")            # high: 72 h
        r = h.client.post(url, json={"verdict": "confirm", "reviewer": "مراجع"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["reviewed"])
        self.assertEqual(r.json()["feedback"][0]["changes"], {})
        self.assertIn("acknowledgment", r.json())
        # Priority change: the deadline follows it (critical: 24 h from receipt).
        r = h.client.post(url, json={"verdict": "correct", "changes": {"priority": "critical"},
                                     "note": "خطر على الأطفال"})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((body["priority"], body["model_priority"]), ("critical", "high"))
        self.assertEqual(body["due_at"], "2026-09-25T09:00:00Z")
        self.assertEqual(body["feedback"][0]["changes"], {"priority": {"from": "high", "to": "critical"}})
        self.assertIn("حرجة", body["acknowledgment"])
        # A category change alone drops the subcategory of the old category.
        r = h.client.post(url, json={"verdict": "correct", "changes": {"category": "municipal_services",
                                                                        "ministry": "municipal"}})
        body = r.json()
        self.assertEqual((body["category"], body["subcategory"], body["ministry"]),
                         ("municipal_services", None, "municipal"))
        self.assertEqual(body["due_at"], "2026-09-25T09:00:00Z")             # priority untouched
        r = h.client.post(url, json={"verdict": "correct", "changes": {"subcategory": "roads_potholes"}})
        self.assertEqual(r.json()["subcategory"], "roads_potholes")
        r = h.client.post(url, json={"verdict": "correct", "changes": {"subcategory": ""}})
        self.assertIsNone(r.json()["subcategory"])
        # The governorate is corrected like the region; the model's answer stays for analytics.
        r = h.client.post(url, json={"verdict": "correct", "changes": {"governorate": "kharj"}})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((body["governorate"], body["model_governorate"]),
                         ("kharj", start["model_governorate"]))
        self.assertEqual(body["feedback"][0]["changes"],
                         {"governorate": {"from": start["governorate"], "to": "kharj"}})
        self.assertEqual(body["due_at"], "2026-09-25T09:00:00Z")             # priority untouched
        # Precedents reach the next classification.
        self.assertEqual(h.store.recent_corrections(3)[0]["category"], "municipal_services")


    def test_region_and_governorate_stay_consistent(self):
        h = Harness(self)
        item = h.paste(doc(1))
        h.process()
        url = f"/complaints/items/{item['id']}/feedback"
        post = lambda changes: h.client.post(url, json={"verdict": "correct", "changes": changes})
        # Another region has none of the entity's governorates.
        body = post({"region": "makkah"}).json()
        self.assertEqual((body["region"], body["governorate"], body["outside_jurisdiction"]),
                         ("makkah", "unknown", True))
        self.assertEqual(body["feedback"][0]["changes"],
                         {"region": {"from": "riyadh", "to": "makkah"},
                          "governorate": {"from": "riyadh_city", "to": "unknown"}})
        # Naming one there contradicts it.
        r = post({"region": "eastern", "governorate": "kharj"})
        self.assertEqual((r.status_code, r.json()), (400, {"error": api.MSG_GOVERNORATE_REGION}))
        self.assertEqual(h.detail(item["id"])["region"], "makkah")            # nothing applied
        # Naming a governorate alone places the complaint in the entity's region.
        body = post({"governorate": "kharj"}).json()
        self.assertEqual((body["region"], body["governorate"], body["outside_jurisdiction"]),
                         ("riyadh", "kharj", False))
        self.assertEqual(body["feedback"][0]["changes"],
                         {"region": {"from": "makkah", "to": "riyadh"},
                          "governorate": {"from": "unknown", "to": "kharj"}})
        self.assertEqual(post({"region": "riyadh", "governorate": "diriyah"}).status_code, 200)
        self.assertEqual(h.detail(item["id"])["governorate"], "diriyah")
        # An unknown region cannot keep a governorate: one would place it in Riyadh.
        body = post({"region": "unknown"}).json()
        self.assertEqual((body["region"], body["governorate"]), ("unknown", "unknown"))
        self.assertEqual(body["feedback"][0]["changes"],
                         {"region": {"from": "riyadh", "to": "unknown"},
                          "governorate": {"from": "diriyah", "to": "unknown"}})
        r = post({"region": "unknown", "governorate": "kharj"})
        self.assertEqual((r.status_code, r.json()), (400, {"error": api.MSG_GOVERNORATE_REGION}))
        # Also when the region already was unknown (a pair stored before this rule).
        h.store.add_feedback(item["id"], verdict="correct", changes={"governorate": "kharj"})
        body = post({"region": "unknown"}).json()
        self.assertEqual((body["region"], body["governorate"]), ("unknown", "unknown"))
        self.assertEqual(body["feedback"][0]["changes"], {"governorate": {"from": "kharj", "to": "unknown"}})

    def test_a_reextraction_that_reads_nothing_keeps_the_text(self):
        h = Harness(self)
        item = upload(h.client, ("a.pdf", pdf(1), "application/pdf")).json()["items"][0]
        h.process()
        url = f"/complaints/items/{item['id']}"
        h.client.post(url + "/feedback", json={"verdict": "confirm"})
        before = h.detail(item["id"])
        h.ocr_text = ""                                   # a bad pass reads nothing
        self.assertEqual(h.client.post(url + "/reprocess", json={"ocr": True}).status_code, 200)
        self.assertEqual(h.detail(item["id"])["text"], doc(1))                # kept while queued
        h.process()
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["error"]), ("error", api.MSG_REOCR_EMPTY))
        self.assertEqual((d["text"], d["subject"], d["complainant_name"], d["national_id"]),
                         (before["text"], before["subject"], before["complainant_name"],
                          before["national_id"]))
        self.assertEqual(len(h.ocr_calls), 2)
        # Analysing the text it has brings it back, the reviewer's verdict intact.
        h.client.post(url + "/reprocess", json={})
        h.process()
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["text"], d["reviewed"], d["needs_review"]), ("done", doc(1), True, False))
        self.assertEqual(len(h.ocr_calls), 2)


# The model's wording for two values the letter writes otherwise: neither is in doc().
PARAPHRASED = {**STRUCT, "complainant_name": "سالم الحربي", "against_entity": "شركة المياه الوطنية"}


def fields_of(detail):
    return {f["key"]: f for f in detail["analysis"]["structured"]["fields"]}


class FieldReviewTests(unittest.TestCase):
    def processed(self, h):
        h.qwen.struct = dict(PARAPHRASED)
        item = h.paste(doc(1))
        h.process()
        return item

    def test_accept_and_change_answer_the_pending_fields(self):
        h = Harness(self)
        item = self.processed(h)
        url = f"/complaints/items/{item['id']}/fields"
        d = h.detail(item["id"])
        fields = fields_of(d)
        self.assertEqual(d["pending_fields"], 2)
        self.assertIs(fields["against_entity"]["pending"], True)
        self.assertIs(fields["complainant_name"]["pending"], True)
        self.assertNotIn("pending", fields["national_id"])            # found in the text
        self.assertEqual(h.client.get("/complaints/items").json()["items"][0]["pending_fields"], 2)
        self.assertEqual(h.client.get("/complaints/analytics").json()["totals"]["fields_pending"], 1)
        # A verdict on the classification leaves the fields to answer.
        r = h.client.post(f"/complaints/items/{item['id']}/feedback", json={"verdict": "confirm"})
        self.assertEqual((r.json()["reviewed"], r.json()["needs_review"]), (True, True))

        r = h.client.post(url, json={"key": "against_entity", "action": "accept", "reviewer": " مراجع ",
                                     "note": " كما ورد "})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        review = fields_of(body)["against_entity"]["review"]
        self.assertEqual((review["action"], review["reviewer"], review["from"], review["note"], review["at"]),
                         ("accept", "مراجع", "شركة المياه الوطنية", "كما ورد", "2026-09-24T09:00:00Z"))
        self.assertEqual((body["pending_fields"], body["needs_review"]), (1, True))
        self.assertIn("acknowledgment", body)

        r = h.client.post(url, json={"key": "complainant_name", "action": "change",
                                     "value": " سالم عبدالله الحربي "})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        field = fields_of(body)["complainant_name"]
        self.assertEqual((field["value"], field["review"]["action"], field["review"]["from"]),
                         ("سالم عبدالله الحربي", "change", "سالم الحربي"))
        self.assertEqual((body["complainant_name"], body["pending_fields"], body["needs_review"]),
                         ("سالم عبدالله الحربي", 0, False))
        self.assertNotIn("fields_unverified", body["review_reasons"])
        self.assertIn("سالم عبدالله الحربي", body["acknowledgment"])     # the letter follows the name
        self.assertEqual(body["events"][0], {"at": "2026-09-24T09:00:00Z", "kind": "field_review", "detail": {
            "key": "complainant_name", "action": "change", "from": "سالم الحربي", "to": "سالم عبدالله الحربي"}})
        rows = list(csv.reader(io.StringIO(h.client.get("/complaints/export.csv").content.decode("utf-8-sig"),
                                           newline="")))
        self.assertEqual(dict(zip(rows[0], rows[1]))["مقدم الشكوى"], "سالم عبدالله الحربي")
        self.assertEqual(h.client.get("/complaints/analytics").json()["totals"]["fields_pending"], 0)
        # "" clears a value; any field of the analysis may be corrected.
        r = h.client.post(url, json={"key": "national_id", "action": "change", "value": ""})
        self.assertEqual((r.status_code, r.json()["national_id"]), (200, None))

    def test_validation(self):
        h = Harness(self)
        h.qwen.struct = dict(PARAPHRASED)
        item = h.paste(doc(1))
        url = f"/complaints/items/{item['id']}/fields"
        accept = {"key": "against_entity", "action": "accept"}
        r = h.client.post(url, json=accept)
        self.assertEqual((r.status_code, r.json()), (409, {"error": api.MSG_NOT_DONE}))   # not processed yet
        h.process()
        bad = [{}, {"action": "accept"}, {"key": 5, "action": "accept"}, {"key": "", "action": "accept"},
               {"key": ["against_entity"], "action": "accept"},
               {"key": "against_entity"}, {"key": "against_entity", "action": "approve"},
               {"key": "against_entity", "action": "change"},                   # a change needs a value
               {"key": "against_entity", "action": "change", "value": None},
               {"key": "against_entity", "action": "change", "value": 7},
               {"key": "against_entity", "action": "change", "value": "x" * 501},
               {**accept, "reviewer": "x" * 101}, {**accept, "reviewer": 3},
               {**accept, "note": "x" * 1001}, {**accept, "note": ["x"]},
               {"key": "no_such_field", "action": "accept"},
               {"key": "subject", "action": "change", "value": "x"}]            # not a structured field
        for body in bad:
            r = h.client.post(url, json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn("error", r.json())
        d = h.detail(item["id"])
        self.assertEqual(d["pending_fields"], 2)                               # nothing applied
        self.assertNotIn("field_review", [e["kind"] for e in d["events"]])
        self.assertEqual(h.client.post("/complaints/items/999/fields", json=accept).status_code, 404)
        self.assertEqual(h.client.post("/complaints/items/abc/fields", json=accept).status_code, 404)
        self.assertEqual(h.client.post(url, content="x").status_code, 415)
        big = json.dumps({**accept, "note": "x" * api.MAX_JSON_BYTES})
        self.assertEqual(h.client.post(url, content=big, headers={"Content-Type": "application/json"}).status_code, 413)
        # The longest value fits; a value sent with an accept is ignored.
        r = h.client.post(url, json={"key": "against_entity", "action": "change", "value": "ش" * 500})
        self.assertEqual((r.status_code, fields_of(r.json())["against_entity"]["value"]), (200, "ش" * 500))
        r = h.client.post(url, json={"key": "complainant_name", "action": "accept", "value": 7})
        self.assertEqual((r.status_code, fields_of(r.json())["complainant_name"]["value"]), (200, "سالم الحربي"))
        # Being reprocessed: nothing to review until the new analysis is in.
        h.client.post(f"/complaints/items/{item['id']}/reprocess", json={})
        r = h.client.post(url, json=accept)
        self.assertEqual((r.status_code, r.json()), (409, {"error": api.MSG_NOT_DONE}))
        # The new analysis keeps the reviewer's change.
        h.process()
        self.assertEqual(fields_of(h.detail(item["id"]))["against_entity"]["value"], "ش" * 500)


class CrossSiteTests(unittest.TestCase):
    def test_state_changes_from_another_site_are_refused(self):
        h = Harness(self)
        item = h.paste(doc(1))
        h.process()
        base = f"/complaints/items/{item['id']}"
        calls = (lambda hd: h.client.post("/complaints/upload", headers=hd,
                                          files=[("files", ("a.pdf", pdf(5), "application/pdf"))]),
                 lambda hd: h.client.post("/complaints/text", headers=hd, json={"text": doc(6)}),
                 lambda hd: h.client.post(base + "/reprocess", headers=hd, json={}),
                 lambda hd: h.client.post(base + "/feedback", headers=hd, json={"verdict": "confirm"}),
                 lambda hd: h.client.post(base + "/status", headers=hd, json={"status": "resolved"}),
                 lambda hd: h.client.put("/complaints/provider", headers=hd, json={"provider": "allam"}),
                 lambda hd: h.client.post("/complaints/insights", headers=hd, json={}),
                 lambda hd: h.client.delete(base, headers=hd),
                 lambda hd: h.client.post(base + "/fields", headers=hd,
                                          json={"key": "complainant_name", "action": "change", "value": "x"}))
        for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site"},              # another port of this host
                        {"Origin": "null"}, {"Origin": "http://testserver:9999"}):
            for n, call in enumerate(calls):
                r = call(headers)
                self.assertEqual((r.status_code, r.json()), (403, {"error": api.MSG_CROSS_SITE}), (headers, n))
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["status"], d["reviewed"]), ("done", "new", False))
        self.assertEqual(d["complainant_name"], STRUCT["complainant_name"])
        self.assertNotIn("field_review", [e["kind"] for e in d["events"]])
        self.assertEqual(h.client.get("/complaints/items").json()["total"], 1)   # nothing planted
        self.assertEqual(h.registry.active().id, "qwen")
        # The app's own page is same-origin; reads need no check.
        own = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(calls[0](own).status_code, 200)
        self.assertEqual(calls[4](own).status_code, 200)
        self.assertEqual(h.client.get("/complaints/items", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 200)


class WorkerTests(unittest.TestCase):
    def test_a_pdf_over_the_page_cap_fails_before_ocr(self):
        h = Harness(self)
        self.assertEqual(h.service.max_pages, api.MAX_PAGES_DEFAULT)
        h.pages = h.service.max_pages + 1
        item = upload(h.client, ("big.pdf", pdf(1), "application/pdf")).json()["items"][0]
        h.process()
        d = h.detail(item["id"])
        self.assertEqual(d["stage"], "error")
        self.assertIn("عدد صفحات الملف (٣١)", d["error"])
        self.assertIn("(٣٠ صفحة)", d["error"])
        self.assertEqual(h.ocr_calls, [])                 # the Surya slot was never taken
        h.pages = h.service.max_pages
        h.client.post(f"/complaints/items/{item['id']}/reprocess", json={})
        h.process()
        self.assertEqual(h.detail(item["id"])["stage"], "done")
        with mock.patch.dict(os.environ, {"CMS_MAX_PAGES": "5"}):
            self.assertEqual(ComplaintService(h.store, TAX, h.registry, autostart=False).max_pages, 5)

    def test_a_failure_the_database_refuses_is_retried_until_it_lands(self):
        h = Harness(self)
        item = h.paste(doc(1))
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(h.store, "save_analysis", side_effect=locked), \
                mock.patch.object(h.store, "fail", side_effect=locked):
            self.assertTrue(h.process())                  # the worker survives both
        self.assertEqual(h.detail(item["id"])["stage"], "classifying")
        self.assertEqual(h.client.get("/complaints/queue").json()["processing"]["id"], item["id"])
        with mock.patch.object(h.store, "fail", side_effect=locked), self.assertRaises(sqlite3.OperationalError):
            h.process()                                   # still refused: the loop backs off
        self.assertFalse(h.process())                     # it lands; nothing else is queued
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["error"]), ("error", api.MSG_FAILED))
        self.assertIsNone(h.client.get("/complaints/queue").json()["processing"])

    def test_a_row_no_worker_holds_can_be_reprocessed_or_deleted(self):
        h = Harness(self)
        locked = sqlite3.OperationalError("database is locked")

        def strand(text):
            item = h.paste(text)
            with mock.patch.object(h.store, "save_analysis", side_effect=locked), \
                    mock.patch.object(h.store, "fail", side_effect=locked):
                h.process()
            self.assertEqual(h.detail(item["id"])["stage"], "classifying")
            return item
        first = strand(doc(1))
        self.assertEqual(h.client.post(f"/complaints/items/{first['id']}/reprocess", json={}).status_code, 200)
        h.process()                                       # the deferred failure leaves it queued
        self.assertEqual(h.detail(first["id"])["stage"], "done")
        second = strand(doc(2))
        self.assertEqual(h.client.delete(f"/complaints/items/{second['id']}").status_code, 200)
        self.assertFalse(h.process())

    def test_a_lazy_model_stopped_under_a_request_gets_one_reload(self):
        h = Harness(self)
        h.client.put("/complaints/provider", json={"provider": "allam"})
        h.allam.script = [ProviderUnavailable("تعذر الاتصال بخدمة النموذج")]
        item = h.paste(doc(1))
        with quiet():
            h.process()
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["provider"]), ("done", "allam"))
        h.allam.script = [ProviderUnavailable("تعذر الاتصال بخدمة النموذج")] * 2   # a real outage
        other = h.paste(doc(2))
        with quiet():
            h.process()
        self.assertEqual(h.detail(other["id"])["error"], api.MSG_UNAVAILABLE)

    def test_a_second_app_on_the_same_data_leaves_the_running_queue_alone(self):
        h = Harness(self)                                 # the app already running…
        self.assertTrue(h.store.acquire_owner())
        item = h.paste(doc(1))
        h.store.claim_next()                              # …is processing this complaint
        second = Store(h.store.db_path, h.store.files_dir, clock=lambda: NOW)
        self.addCleanup(second.close)
        service = ComplaintService(second, TAX, h.registry, ocr=h.ocr, autostart=False)
        self.addCleanup(service.stop, 2)
        with quiet():
            service.boot()                                # uvicorn runs startup before the bind fails
        self.assertFalse(service.running)
        self.assertFalse(service.owns_queue)
        self.assertEqual(h.detail(item["id"])["stage"], "structuring")   # not requeued under the worker
        app = FastAPI()
        app.include_router(create_router(service))
        client = TestClient(app)
        self.assertEqual(client.delete(f"/complaints/items/{item['id']}").status_code, 409)
        self.assertEqual(client.get("/complaints/queue").json()["worker"], "stopped")
        # It still takes complaints in; the running app's worker processes them.
        new = client.post("/complaints/text", json={"text": doc(2)}).json()["item"]
        self.assertTrue(h.process())
        self.assertEqual(h.detail(new["id"])["stage"], "done")

    def test_boot_fails_a_complaint_that_keeps_getting_interrupted(self):
        h = Harness(self)
        item = h.paste(doc(1))
        for restart in range(1, api.MAX_INTERRUPTIONS + 1):
            h.store.claim_next()                          # the app dies while processing it
            service = ComplaintService(h.store, TAX, h.registry, ocr=h.ocr, autostart=False)
            with mock.patch.object(ComplaintService, "start"), quiet():
                service.boot()
            stage = h.detail(item["id"])["stage"]
            self.assertEqual(stage, "queued" if restart < api.MAX_INTERRUPTIONS else "error", restart)
        self.assertEqual(h.detail(item["id"])["error"], api.MSG_GAVE_UP)

    def test_empty_ocr_text_is_done_with_empty_text_reason(self):
        for n, text in enumerate(("", "--- Page 1 ---\n\n--- Page 2 ---\n")):
            with self.subTest(text=text):
                h = Harness(self, ocr_text=text)
                item = upload(h.client, ("scan.pdf", pdf(n), "application/pdf")).json()["items"][0]
                self.assertTrue(h.process())
                d = h.detail(item["id"])
                self.assertEqual((d["stage"], d["text"], d["page_count"]), ("done", text, 2))
                self.assertEqual(d["analysis"]["review_reasons"], ["empty_text"])
                self.assertTrue(d["needs_review"])
                self.assertEqual((d["category"], d["ministry"], d["priority"]), ("other", "other", "low"))
                self.assertEqual(d["due_at"], "2026-10-08T09:00:00Z")         # low: 336 h
                self.assertEqual(h.qwen.calls, 0)

    def test_ocr_failure_is_a_safe_error(self):
        h = Harness(self)
        h.ocr_error = RuntimeError(SECRET)
        item = upload(h.client, ("a.pdf", pdf(1), "application/pdf")).json()["items"][0]
        h.process()
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["error"]), ("error", api.MSG_OCR))
        self.assertNotIn(SECRET, json.dumps(d, ensure_ascii=False))

    def test_missing_file_is_an_error(self):
        h = Harness(self)
        item = upload(h.client, ("a.pdf", pdf(1), "application/pdf")).json()["items"][0]
        os.remove(os.path.join(h.store.files_dir, f"{item['id']}.pdf"))
        h.process()
        self.assertEqual(h.detail(item["id"])["error"], api.MSG_NO_FILE)
        self.assertEqual(h.ocr_calls, [])

    def test_busy_is_retried_then_fails(self):
        h = Harness(self)
        h.qwen.script = [ProviderBusy("مشغول")] * (api.BUSY_RETRIES + 1)
        item = h.paste(doc(1))
        h.process()
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["error"]), ("error", api.MSG_BUSY))
        self.assertEqual(h.qwen.calls, api.BUSY_RETRIES + 1)
        # Busy once, then free: the retry succeeds.
        h.qwen.script = [ProviderBusy("مشغول")]
        other = h.paste(doc(2))
        h.process()
        self.assertEqual(h.detail(other["id"])["stage"], "done")

    def test_busy_wait_uses_the_configured_delay(self):
        h = Harness(self)
        h.service.busy_wait = 0.05
        h.qwen.script = [ProviderBusy("مشغول")] * 2
        h.paste(doc(1))
        start = time.monotonic()
        h.process()
        self.assertGreaterEqual(time.monotonic() - start, 0.09)

    def test_unavailable_provider(self):
        h = Harness(self)
        h.qwen.unavailable = True
        item = h.paste(doc(1))
        h.process()
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["error"]), ("error", api.MSG_UNAVAILABLE))
        self.assertEqual(h.qwen.calls, 0)
        h.qwen.unavailable = False                               # reprocess once it is back
        h.client.post(f"/complaints/items/{item['id']}/reprocess", json={})
        h.process()
        self.assertEqual(h.detail(item["id"])["stage"], "done")

    def test_invalid_model_output_is_an_analysis_error(self):
        h = Harness(self)
        h.qwen.script = ["not json", "still not json"]
        item = h.paste(doc(1))
        h.process()
        self.assertEqual(h.detail(item["id"])["error"], complaints.MSG_INVALID)

    def test_unexpected_exception_is_generic_and_the_next_item_proceeds(self):
        h = Harness(self)
        h.qwen.script = [RuntimeError(SECRET)]
        first, second = h.paste(doc(1)), h.paste(doc(2))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(h.service.process_next())
            self.assertTrue(h.service.process_next())
        d1, d2 = h.detail(first["id"]), h.detail(second["id"])
        self.assertEqual((d1["stage"], d1["error"]), ("error", api.MSG_FAILED))
        self.assertNotIn(SECRET, json.dumps(d1, ensure_ascii=False))
        self.assertEqual(d2["stage"], "done")
        self.assertIn("complaints worker error", out.getvalue())

    def test_logging_survives_a_cp1252_console(self):
        # The app's stdout is often cp1252: an Arabic repr must not raise
        # inside the error handler and strand the complaint mid-stage.
        h = Harness(self)
        h.qwen.script = [RuntimeError(SECRET)]
        item = h.paste(doc(1))
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(console):
            self.assertTrue(h.service.process_next())
            console.flush()
        self.assertEqual(h.detail(item["id"])["error"], api.MSG_FAILED)
        self.assertIn(b"complaints worker error", raw.getvalue())
        self.assertIn(ascii(SECRET)[1:-1].encode(), raw.getvalue())      # escaped, not lost

    def test_repeat_complainant_and_stages(self):
        h = Harness(self)
        first, second = h.paste(doc(1)), h.paste(doc(2))
        h.drain()
        self.assertEqual(h.detail(first["id"])["analysis"]["classification"]["repeat_count"], 0)
        d = h.detail(second["id"])
        self.assertEqual(d["analysis"]["classification"]["repeat_count"], 1)
        self.assertIn("repeat_complainant", d["analysis"]["review_reasons"])
        self.assertEqual([e["detail"].get("stage") for e in reversed(d["events"]) if e["kind"] == "stage"],
                         ["structuring", "classifying"])

    def test_worker_thread_processes_notified_items_and_stops(self):
        h = Harness(self, autostart=True)
        self.assertTrue(h.service.running)
        h.qwen.script = [RuntimeError(SECRET)]                  # the worker must survive this
        with quiet():
            a, b = h.paste(doc(1)), h.paste(doc(2))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                stages = {h.detail(i["id"])["stage"] for i in (a, b)}
                if stages <= {"done", "error"}:
                    break
                time.sleep(0.02)
        self.assertEqual(h.detail(a["id"])["stage"], "error")
        self.assertEqual(h.detail(b["id"])["stage"], "done")
        self.assertEqual(h.client.get("/complaints/queue").json()["worker"], "running")
        c = h.paste(doc(3))                                        # woken by notify, not the idle poll
        deadline = time.monotonic() + 10
        while h.detail(c["id"])["stage"] != "done" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(h.detail(c["id"])["stage"], "done")
        thread = h.service._thread
        start = time.monotonic()
        h.service.stop()
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(h.service.running)
        self.assertEqual(h.client.get("/complaints/queue").json()["worker"], "stopped")
        self.assertFalse(h.service.process_next())                  # stopped: nothing is claimed

    def test_stop_interrupts_a_busy_wait_and_requeues(self):
        h = Harness(self, autostart=True)
        h.service.busy_wait = 30
        h.qwen.script = [ProviderBusy("مشغول")] * 10
        item = h.paste(doc(1))
        deadline = time.monotonic() + 10
        while h.qwen.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(h.qwen.calls, 1)
        start = time.monotonic()
        h.service.stop()
        self.assertLess(time.monotonic() - start, 3)
        d = h.detail(item["id"])
        self.assertEqual(d["stage"], "queued")                      # not blamed on the complaint
        self.assertIsNone(d["error"])

    def test_failure_while_stopping_requeues_instead_of_failing(self):
        # At shutdown app.py stops the worker, then the model servers; a call
        # still in flight then fails, and that is not the complaint's fault.
        h = Harness(self, autostart=True)
        entered, release = threading.Event(), threading.Event()

        def dying(*args, **kwargs):
            entered.set()
            release.wait(5)
            raise ProviderUnavailable("تعذر الاتصال بخدمة النموذج")
        h.qwen.chat_json = dying
        item = h.paste(doc(1))
        self.assertTrue(entered.wait(5))
        thread = h.service._thread
        h.service.stop(timeout=0.05)                                 # the call is still running
        self.assertTrue(thread.is_alive())
        release.set()
        with quiet():
            thread.join(5)
        self.assertFalse(thread.is_alive())
        d = h.detail(item["id"])
        self.assertEqual((d["stage"], d["error"]), ("queued", None))

    def test_idle_worker_sleeps_until_notified(self):
        h = Harness(self, autostart=True)
        calls = []
        original = h.service.process_next

        def counting():
            calls.append(1)
            return original()
        h.service.process_next = counting
        h.service.notify()
        time.sleep(0.3)
        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 3)                          # no busy loop on an empty queue

    def test_default_router_boots_with_the_app(self):
        # The directory's cleanup is registered first, so it runs last: after
        # the store has closed complaints.db (Windows cannot delete an open file).
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        fake = FakeProvider("qwen")
        registry = ProviderRegistry([fake], "qwen")
        with mock.patch.dict(os.environ, {"CMS_DATA_DIR": tmp.name}), \
                mock.patch("complaints_llm.get_registry", return_value=registry):
            router = create_router()
        service = router.service
        self.addCleanup(service.store.close)
        self.addCleanup(service.stop, 2)
        self.assertEqual(os.path.dirname(str(service.store.db_path)), tmp.name)
        self.assertEqual(service.store.home_region, TAX.receiving_entity.extra["region"])
        self.assertFalse(service.running)                       # nothing runs before startup
        # A row a previous process left half-done:
        summary, _ = service.store.create(source="text", filename="", file_kind=None, text=doc(1))
        service.store.claim_next()
        app = FastAPI()
        app.include_router(router)
        with quiet(), TestClient(app) as client:
            self.assertTrue(service.running)
            self.assertTrue(service.store.is_owner)
            deadline = time.monotonic() + 10
            while client.get(f"/complaints/items/{summary['id']}").json()["stage"] != "done":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            detail = client.get(f"/complaints/items/{summary['id']}").json()
        events = list(reversed(detail["events"]))
        self.assertEqual([e["kind"] for e in events],
                         ["created", "stage", "reprocess", "stage", "stage", "processed"])
        self.assertEqual(events[2]["detail"], {"ocr": False, "interrupted": True})
        service.stop()
        service.store.close()
        tmp.cleanup()
        self.assertFalse(os.path.exists(tmp.name))              # nothing left behind in %TEMP%

    def test_an_unopenable_database_answers_503_and_the_app_still_starts(self):
        registry = ProviderRegistry([FakeProvider("qwen")], "qwen")
        for case in ("damaged", "newer"):
            with self.subTest(case=case):
                tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                self.addCleanup(tmp.cleanup)
                path = os.path.join(tmp.name, "complaints.db")
                if case == "damaged":
                    with open(path, "wb") as fh:
                        fh.write(b"this is not a database " * 200)
                else:
                    db = sqlite3.connect(path)
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
                    db.close()
                out = io.StringIO()
                with mock.patch.dict(os.environ, {"CMS_DATA_DIR": tmp.name}), \
                        mock.patch("complaints_llm.get_registry", return_value=registry), \
                        contextlib.redirect_stdout(out):
                    router = create_router()                    # must not raise out of `import app`
                self.assertIsNone(router.service)
                self.assertIn("complaints service unavailable", out.getvalue())
                app = FastAPI()
                app.include_router(router)

                @app.get("/other-tab")
                def other():
                    return {"ok": True}
                with TestClient(app) as client:
                    for method, url in (("GET", "/complaints/queue"), ("GET", "/complaints/config"),
                                        ("GET", "/complaints/items/1"), ("POST", "/complaints/text"),
                                        ("DELETE", "/complaints/items/1")):
                        r = client.request(method, url)
                        self.assertEqual((r.status_code, r.json()), (503, {"error": api.MSG_SERVICE_DOWN}), url)
                    self.assertEqual(client.get("/complaints/ui.js").status_code, 200)   # the tab can say why
                    self.assertEqual(client.get("/other-tab").json(), {"ok": True})


# app.py itself, with the GPU-bound modules replaced by stubs: importing the real
# extract loads torch and Surya. Runs in a child process so the stubs never
# reach this one's sys.modules.
APP_PROBE = r'''
import json, sys, types

def module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m

class Server:
    def __init__(self, name):
        self.model_path, self.n_ctx, self.stops, self.state = name + ".gguf", 4096, 0, "not_loaded"
    def ensure_loaded(self):
        self.state = "ready"
    def status(self):
        return {"status": self.state, "error": None, "model": self.model_path, "device": "gpu"}
    def chat_json(self, *args, **kwargs):
        raise RuntimeError("not in this probe")
    def stop(self):
        self.stops += 1
        self.state = "not_loaded"

STRUCT, PROOF = Server("qwen"), Server("allam")
noop = lambda *args, **kwargs: None
seen = {}

def proofread_run(text):
    import complaints_llm
    seen["holders"] = complaints_llm.server_lease(PROOF).holders
    return {"allam": {"corrected": text}}

module("extract", progress=lambda: {}, ensure_loaded=noop, extract_document=noop,
       detect_kind=lambda *a: None, status=lambda: {"status": "ready"}, shutdown_server=noop)
module("llm", STRUCT=STRUCT, PROOF=PROOF, ensure_loaded=noop, status=lambda: {"status": "ready"},
       stop_server=noop, ensure_proof=PROOF.ensure_loaded, stop_proof=PROOF.stop,
       proof_status=PROOF.status)
module("proofread", run=proofread_run)
module("structure", parse_structure=noop, parse_structure_for_template=noop)
module("classify", classify=noop)
module("chat", stream_events=noop)
module("docx_export", build_docx=noop)
module("layout_docx", build_layout_docx=noop, clean_layouts=noop, shutdown_layout_server=noop)

import complaints_llm
import app
from fastapi.testclient import TestClient

out = {}
with TestClient(app.app, base_url="http://127.0.0.1:8100") as client:
    r = client.get("/health")
    out["health"] = r.status_code
    out["csp"] = r.headers.get("content-security-policy")
    out["xfo"] = r.headers.get("x-frame-options")
    out["rebound"] = client.get("/complaints/queue", headers={"Host": "rebind.attacker.example:8100"}).status_code
    out["localhost"] = client.get("/health", headers={"Host": "localhost:8100"}).status_code
    out["lan"] = client.get("/health", headers={"Host": "lan-box:8100"}).status_code
    r = client.get("/complaints/queue")
    out["queue"] = [r.status_code, r.json()]
    lease = complaints_llm.server_lease(PROOF)
    lease.acquire()                      # a complaint request is using ALLaM
    out["proofread"] = client.post("/proofread", json={"text": "نص للتدقيق"}).status_code
    out["holders_during_run"] = seen.get("holders")
    out["stops_while_held"] = PROOF.stops
    lease.release()
    out["stops_after"] = PROOF.stops
    out["service"] = app.complaints_router.service is not None

# A /proofread cancelled mid-run (client gone, server stopping) still lets go
# of ALLaM: anyio re-delivers a cancellation at every await, so the release
# in its finally must be shielded.
import anyio, threading
entered, gate = threading.Event(), threading.Event()

def blocking_run(text):
    entered.set()
    gate.wait(5)
    return {"allam": {}}
app.proofread_run = blocking_run

async def cancelled_proofread():
    with anyio.CancelScope() as scope:
        async def canceller():
            await anyio.to_thread.run_sync(entered.wait, 5)
            scope.cancel()
            gate.set()
        async with anyio.create_task_group() as tg:
            tg.start_soon(canceller)
            await app.proofread_endpoint(app.TextIn(text="نص"))
before = PROOF.stops
anyio.run(cancelled_proofread)
out["cancelled_holders"] = complaints_llm.server_lease(PROOF).holders
out["cancelled_stops"] = PROOF.stops - before
print("PROBE" + json.dumps(out, ensure_ascii=True))
'''


class AppWiringTests(unittest.TestCase):
    def probe(self, data_dir, **env):
        proc = subprocess.run([sys.executable, "-c", APP_PROBE],
                              cwd=os.path.dirname(os.path.abspath(__file__)),
                              env={**os.environ, "CMS_DATA_DIR": data_dir, "PYTHONIOENCODING": "utf-8", **env},
                              capture_output=True, timeout=120)
        stdout = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-3000:])
        return json.loads(stdout.rsplit("PROBE", 1)[1])

    def test_app_refuses_foreign_hosts_forbids_framing_and_shares_allam(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        out = self.probe(os.path.join(tmp.name, "cms"))
        self.assertEqual((out["health"], out["localhost"]), (200, 200))
        self.assertEqual(out["rebound"], 400)                     # DNS rebinding gets nothing
        self.assertEqual(out["lan"], 400)
        self.assertIn("frame-ancestors 'self'", out["csp"])
        self.assertEqual(out["xfo"], "SAMEORIGIN")
        self.assertEqual(out["queue"][0], 200)
        self.assertTrue(out["service"])
        # /proofread holds ALLaM's lease beside the complaint request, and
        # leaves the stop to whoever lets go last.
        self.assertEqual((out["proofread"], out["holders_during_run"]), (200, 2))
        self.assertEqual((out["stops_while_held"], out["stops_after"]), (0, 1))
        self.assertEqual((out["cancelled_holders"], out["cancelled_stops"]), (0, 1))

    def test_app_starts_without_its_complaints_database(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "complaints.db"), "wb") as fh:
            fh.write(b"this is not a database " * 200)
        out = self.probe(tmp.name, APP_ALLOWED_HOSTS="lan-box, other")
        self.assertEqual(out["health"], 200)                      # the other tabs work
        self.assertEqual(out["queue"], [503, {"error": api.MSG_SERVICE_DOWN}])
        self.assertFalse(out["service"])                          # and shutdown did not trip on it
        self.assertEqual(out["lan"], 200)                         # an operator-added host name


class DashboardTests(unittest.TestCase):
    def test_analytics(self):
        h = Harness(self)
        h.paste(doc(1))
        h.paste(doc(2))
        h.process()
        data = h.client.get("/complaints/analytics").json()
        self.assertEqual((data["totals"]["all"], data["totals"]["done"], data["totals"]["queued"]), (2, 1, 1))
        self.assertEqual(data["by_priority"][1], {"id": "high", "count": 1})
        self.assertEqual(data["sla"], {"on_track": 1, "due_soon": 0, "overdue": 0})
        self.assertEqual(data["by_category"], [{"id": "water_sewage", "count": 1}])
        self.assertEqual(data["by_governorate"], [{"id": "riyadh_city", "count": 1}])
        self.assertEqual(data["totals"]["outside_jurisdiction"], 0)
        self.assertEqual(len(data["trend"]), 30)

    def test_outside_jurisdiction_governorates_and_cited_corrections(self):
        h = Harness(self)
        # Addressed to the Emirate, but about Jeddah (makkah region).
        h.qwen.struct = {**STRUCT, "city": "جدة", "district_or_address": "حي الصفا",
                         "region": "makkah", "governorate": "unknown"}
        far = h.paste(doc(1, city="جدة", district="حي الصفا"))
        h.process()
        h.qwen.struct = dict(STRUCT)
        near = h.paste(doc(2))
        h.process()
        rows = {r["id"]: r for r in h.client.get("/complaints/items").json()["items"]}
        self.assertEqual((rows[far["id"]]["region"], rows[far["id"]]["governorate"]), ("makkah", "unknown"))
        self.assertIn("outside_jurisdiction", rows[far["id"]]["review_reasons"])   # the register's badge
        self.assertEqual(rows[near["id"]]["governorate"], "riyadh_city")
        self.assertNotIn("outside_jurisdiction", rows[near["id"]]["review_reasons"])
        self.assertEqual(h.detail(far["id"])["review_reasons"],
                         h.detail(far["id"])["analysis"]["review_reasons"])
        self.assertEqual([r["id"] for r in h.client.get("/complaints/items?governorate=unknown")
                          .json()["items"]], [far["id"]])
        data = h.client.get("/complaints/analytics").json()
        self.assertEqual(data["totals"]["outside_jurisdiction"], 1)
        self.assertEqual(data["by_governorate"], [{"id": "riyadh_city", "count": 1},
                                                  {"id": "unknown", "count": 1}])
        # A confirmation clears needs_review but keeps the recorded reason.
        r = h.client.post(f"/complaints/items/{far['id']}/feedback", json={"verdict": "confirm"})
        self.assertEqual((r.json()["needs_review"], r.json()["review_reasons"]),
                         (False, rows[far["id"]]["review_reasons"]))
        # Each correction names the complaints that made it.
        h.client.post(f"/complaints/items/{near['id']}/feedback",
                      json={"verdict": "correct", "changes": {"governorate": "diriyah"}})
        mq = h.client.get("/complaints/analytics").json()["model_quality"]
        self.assertEqual(mq["top_corrections"], [{"field": "governorate", "from": "riyadh_city",
                                                  "to": "diriyah", "count": 1, "refs": [near["ref"]]}])
        self.assertEqual(mq["governorate_agreement"], 0.5)
        # Net: through an unknown place and back to the model's answer, nothing is left to count.
        for changes in ({"region": "unknown"}, {"governorate": "riyadh_city"}):
            h.client.post(f"/complaints/items/{near['id']}/feedback",
                          json={"verdict": "correct", "changes": changes})
        self.assertEqual((h.detail(near["id"])["region"], h.detail(near["id"])["governorate"]),
                         ("riyadh", "riyadh_city"))
        mq = h.client.get("/complaints/analytics").json()["model_quality"]
        self.assertEqual((mq["top_corrections"], mq["governorate_agreement"]), ([], 1.0))

    def test_outside_jurisdiction_follows_the_effective_region(self):
        h = Harness(self)
        h.qwen.struct = {**STRUCT, "city": "جدة", "district_or_address": "حي الصفا",
                         "region": "makkah", "governorate": "unknown"}
        far = h.paste(doc(1, city="جدة", district="حي الصفا"))
        h.process()
        h.qwen.struct = dict(STRUCT)
        near = h.paste(doc(2))
        h.process()

        def state():
            rows = {r["id"]: r for r in h.client.get("/complaints/items").json()["items"]}
            return ((rows[far["id"]]["outside_jurisdiction"], rows[near["id"]]["outside_jurisdiction"]),
                    h.client.get("/complaints/analytics").json()["totals"]["outside_jurisdiction"])
        self.assertEqual(state(), ((True, False), 1))
        self.assertTrue(h.detail(far["id"])["outside_jurisdiction"])
        # The reviewer finds the far one is in Riyadh after all, the near one in Qassim.
        fb = lambda item, changes: h.client.post(f"/complaints/items/{item['id']}/feedback",
                                                 json={"verdict": "correct", "changes": changes})
        self.assertFalse(fb(far, {"region": "riyadh", "governorate": "riyadh_city"}).json()["outside_jurisdiction"])
        self.assertTrue(fb(near, {"region": "qassim"}).json()["outside_jurisdiction"])
        self.assertEqual(state(), ((False, True), 1))
        self.assertIn("outside_jurisdiction", h.detail(far["id"])["review_reasons"])   # as the analysis recorded it

    def test_insights_on_allam_give_its_memory_back(self):
        h = Harness(self)
        h.paste(doc(1))
        h.process()
        h.client.put("/complaints/provider", json={"provider": "allam"})
        r = h.client.post("/complaints/insights", json={})
        self.assertEqual((r.status_code, r.json()["provider"]), (200, "allam"))
        self.assertEqual(h.allam.released, 1)              # no worker here: at once
        h.qwen.unavailable = h.allam.unavailable = True
        self.assertEqual(h.client.post("/complaints/insights", json={}).status_code, 503)
        self.assertEqual(h.allam.released, 2)              # a failed run too
        # With a worker running, its next idle pass releases it (after a batch).
        w = Harness(self, autostart=True)
        item = w.paste(doc(1))
        deadline = time.monotonic() + 10
        while w.detail(item["id"])["stage"] != "done":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        w.client.put("/complaints/provider", json={"provider": "allam"})
        self.assertEqual(w.client.post("/complaints/insights", json={}).status_code, 200)
        while w.allam.released < 1:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(w.allam.released, 1)

    def test_export_has_every_matching_row(self):
        h = Harness(self)
        items = [h.paste(doc(n)) for n in range(1, 6)]
        h.process()
        with mock.patch.object(api, "EXPORT_PAGE", 2, create=True), \
                mock.patch.object(api, "EXPORT_MAX_ROWS", 3, create=True):   # the old silent cap
            r = h.client.get("/complaints/export.csv")
        rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"), newline="")))
        self.assertEqual(len(rows), 6)
        self.assertEqual({row[0] for row in rows[1:]}, {it["ref"] for it in items})
        self.assertEqual(r.headers["x-total-count"], "5")

    def test_insights(self):
        h = Harness(self)
        post = lambda: h.client.post("/complaints/insights", json={})
        r = post()
        self.assertEqual(r.status_code, 409)
        self.assertIn("error", r.json())
        item = h.paste(doc(1))
        h.process()
        r = post()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["provider"], body["generated_at"]), ("qwen", "2026-09-24T09:00:00Z"))
        self.assertEqual(h.qwen.seen_refs, [item["ref"]])
        self.assertEqual(body["insights"]["insights"][0]["refs"], [item["ref"]])   # the made-up ref is gone
        self.assertEqual(body["insights"]["headline"], "الصرف الصحي في المقدمة")
        h.qwen.script = [ProviderBusy("النموذج مشغول حالياً؛ أعد المحاولة لاحقاً")]
        r = post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "النموذج مشغول حالياً؛ أعد المحاولة لاحقاً"})
        h.qwen.unavailable = True
        self.assertEqual(post().status_code, 503)
        h.qwen.unavailable = False
        h.qwen.script = ["{}", "[]"]
        with quiet():
            r = post()
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json(), {"error": complaints.MSG_INVALID})
        h.qwen.script = [RuntimeError(SECRET)]
        with quiet():
            r = post()
        self.assertEqual(r.status_code, 500)
        self.assertNotIn(SECRET, r.text)
        self.assertEqual(h.client.post("/complaints/insights", content="{}").status_code, 415)
        self.assertEqual(post().status_code, 200)                  # the slot was released every time

    def test_one_insights_run_at_a_time(self):
        h = Harness(self)
        h.paste(doc(1))
        h.process()
        entered, release = threading.Event(), threading.Event()
        original = h.qwen.chat_json

        def slow(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        h.qwen.chat_json = slow
        results = []
        thread = threading.Thread(target=lambda: results.append(h.client.post("/complaints/insights", json={})))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(h.client.post("/complaints/insights", json={}).status_code, 409)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(results[0].status_code, 200)

    def test_export_csv(self):
        h = Harness(self)
        h.qwen.struct = {**STRUCT, "subject": "=HYPERLINK(\"http://x\")"}
        a = h.paste(doc(1), title="@SUM(1+1)")
        b = h.paste(doc(2), title="-حالة")
        h.process()                                                   # a done, b queued
        r = h.client.get("/complaints/export.csv")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(r.headers["content-disposition"], 'attachment; filename="complaints.csv"')
        raw = r.content
        self.assertTrue(raw.startswith("\ufeff".encode("utf-8")))
        text = raw.decode("utf-8-sig")
        self.assertIn("\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(tuple(rows[0]), api.CSV_HEADERS)
        self.assertEqual(rows[0].index("المحافظة"), rows[0].index("المنطقة") + 1)
        self.assertEqual(len(rows), 3)
        by_ref = {row[0]: dict(zip(rows[0], row)) for row in rows[1:]}
        done, queued = by_ref[a["ref"]], by_ref[b["ref"]]
        self.assertEqual(done["التصنيف"], TAX.label("categories", "water_sewage"))
        self.assertEqual(done["التصنيف الفرعي"], TAX.subcategory_label("sewage_overflow"))
        self.assertEqual(done["الجهة المختصة"], "وزارة البيئة والمياه والزراعة")
        self.assertEqual(done["الأولوية"], "عالية")
        self.assertEqual(done["المنطقة"], TAX.label("regions", "riyadh"))
        self.assertEqual(done["المحافظة"], TAX.label("governorates", "riyadh_city"))
        self.assertEqual(queued["المحافظة"], "")
        self.assertEqual(done["الحالة"], "جديدة")
        self.assertEqual(done["تاريخ الاستلام"], "2026-09-24 12:00")         # Riyadh time
        self.assertEqual(done["المهلة"], "2026-09-27 12:00")
        self.assertEqual(done["مصدر الأولوية"], "تقدير النموذج")
        self.assertEqual(done["مقدم الشكوى"], "سالم عبدالله الحربي")
        # Formula injection neutralised in model output and in user-typed titles.
        self.assertEqual(done["الموضوع"], "'=HYPERLINK(\"http://x\")")
        self.assertEqual(done["الملف"], "'@SUM(1+1)")
        self.assertEqual(queued["الملف"], "'-حالة")
        self.assertEqual((queued["التصنيف"], queued["مصدر الأولوية"]), ("", ""))
        # Reviewer-changed priority, and the filters of /items.
        h.client.post(f"/complaints/items/{a['id']}/feedback",
                      json={"verdict": "correct", "changes": {"priority": "critical"}})
        rows = list(csv.reader(io.StringIO(h.client.get("/complaints/export.csv?stage=done")
                                           .content.decode("utf-8-sig"), newline="")))
        self.assertEqual(len(rows), 2)
        self.assertEqual(dict(zip(rows[0], rows[1]))["مصدر الأولوية"], "تعديل المراجع")
        # The governorate filter, and the reviewer's governorate as its Arabic label.
        h.client.post(f"/complaints/items/{a['id']}/feedback",
                      json={"verdict": "correct", "changes": {"governorate": "kharj"}})
        rows = list(csv.reader(io.StringIO(h.client.get("/complaints/export.csv?governorate=kharj")
                                           .content.decode("utf-8-sig"), newline="")))
        self.assertEqual(len(rows), 2)
        self.assertEqual(dict(zip(rows[0], rows[1]))["المحافظة"], TAX.label("governorates", "kharj"))
        for bad in ("priority=urgent", "governorate=nowhere"):
            r = h.client.get("/complaints/export.csv?" + bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("error", r.json())

    def test_rule_floor_source_in_export(self):
        h = Harness(self)
        h.qwen.cls = {**CLS, "priority": "low", "rationale": "إزعاج. الأولوية: منخفضة"}
        h.paste(doc(1))                        # «طفح المجاري» and «سبق أن تقدمت» raise it to medium
        h.process()
        rows = list(csv.reader(io.StringIO(h.client.get("/complaints/export.csv").content.decode("utf-8-sig"),
                                           newline="")))
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual((row["الأولوية"], row["مصدر الأولوية"]), ("متوسطة", "رفعتها القواعد"))


if __name__ == "__main__":
    unittest.main()
