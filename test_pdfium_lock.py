# -*- coding: utf-8 -*-
"""PDFium is not thread-safe: every PDFium call in extract.py and layout_docx.py
holds extract.PDFIUM_LOCK, for that call only, and a background OCR
(extract_document(..., progress=False), the complaints worker) leaves the
analysis tab's progress record alone.

No Surya/torch model loads here: the recognizer and _page_text are stand-ins,
and so is pdfium, whose every method records whether the calling thread held
the lock (RLock._is_owned) when it was called. The last class runs the same
paths on a real (blank) PDF, so explicit page closing is checked against the
real library too.
"""
import io
import logging
import os
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import extract
import layout_docx as L

PDF = b"%PDF-1.4 stand-in"


def _held() -> bool:
    return extract.PDFIUM_LOCK._is_owned()


class FakePdfium:
    """Stands in for the pypdfium2 module. `calls` lists (call, lock held)."""

    def __init__(self, pages=2, size=(600.0, 800.0), fail_render=False):
        self.calls = []
        fake = self

        class Page:
            def get_size(self):
                fake.calls.append(("get_size", _held()))
                return size

            def render(self, scale):
                fake.calls.append(("render", _held()))
                if fail_render:
                    raise RuntimeError("damaged page")
                return SimpleNamespace(to_pil=lambda: Image.new("RGB", (20, 30), "white"))

            def close(self):
                fake.calls.append(("page.close", _held()))

        class PdfDocument:
            def __init__(self, data):
                fake.calls.append(("open", _held()))

            def __len__(self):
                fake.calls.append(("len", _held()))
                return pages

            def __getitem__(self, i):
                fake.calls.append(("page", _held()))
                return Page()

            def close(self):
                fake.calls.append(("close", _held()))

        self.PdfDocument = PdfDocument

    def names(self):
        return [name for name, _ in self.calls]


class ExtractHoldsTheLock(unittest.TestCase):

    def run_ocr(self, fake, **kw):
        seen = []

        def page_text(rec, img):
            # The OCR itself runs under Surya's lock and NOT under PDFium's:
            # PDFIUM_LOCK is held for each PDFium call alone.
            seen.append((_held(), extract._infer_lock.locked(), dict(extract._progress)))
            return "نص", None

        with mock.patch.object(extract, "pdfium", fake), \
                mock.patch.object(extract, "ensure_loaded", lambda: object()), \
                mock.patch.object(extract, "_page_text", page_text):
            result = extract.extract_document(PDF, "doc.pdf", **kw)
        return result, seen

    def test_every_pdfium_call_holds_the_lock(self):
        fake = FakePdfium(pages=2)
        result, seen = self.run_ocr(fake)
        self.assertEqual(result.page_count, 2)
        self.assertEqual(fake.names(), ["open", "len", "page", "render", "page.close",
                                        "page", "render", "page.close", "close"])
        self.assertTrue(all(held for _, held in fake.calls), fake.calls)
        self.assertEqual([(held, surya) for held, surya, _ in seen], [(False, True)] * 2)
        self.assertFalse(_held())

    def test_a_failed_render_still_closes_the_page_and_the_document_under_the_lock(self):
        fake = FakePdfium(pages=2, fail_render=True)
        with mock.patch.dict(extract._progress):
            with self.assertRaisesRegex(RuntimeError, "damaged page"):
                self.run_ocr(fake)
        self.assertEqual(fake.names(), ["open", "len", "page", "render", "page.close", "close"])
        self.assertTrue(all(held for _, held in fake.calls), fake.calls)

    def test_the_lock_is_one_reentrant_lock_other_threads_wait_for(self):
        self.assertIsInstance(extract.PDFIUM_LOCK, type(threading.RLock()))
        fake, done = FakePdfium(pages=1), threading.Event()

        def ocr():
            self.run_ocr(fake)
            done.set()

        with mock.patch.dict(extract._progress):
            with extract.PDFIUM_LOCK:         # e.g. the complaints page count
                worker = threading.Thread(target=ocr)
                worker.start()
                self.assertFalse(done.wait(0.2))
                self.assertEqual(fake.calls, [])   # not even the open got through
            worker.join(5)
        self.assertTrue(done.is_set())

    def test_background_ocr_leaves_the_progress_record_alone(self):
        users = {"active": True, "page": 3, "total": 9, "filename": "user.pdf"}
        with mock.patch.dict(extract._progress, users):
            result, seen = self.run_ocr(FakePdfium(pages=2), progress=False)
            self.assertEqual(extract._progress, users)          # still the user's own run
        self.assertEqual(result.page_count, 2)
        self.assertEqual([p for _, _, p in seen], [users, users])

    def test_background_ocr_that_fails_leaves_the_progress_record_alone(self):
        users = {"active": True, "page": 3, "total": 9, "filename": "user.pdf"}
        with mock.patch.dict(extract._progress, users):
            with self.assertRaises(RuntimeError):
                self.run_ocr(FakePdfium(fail_render=True), progress=False)
            self.assertEqual(extract._progress, users)

    def test_interactive_ocr_still_reports_its_progress(self):
        with mock.patch.dict(extract._progress, {"active": False, "page": 0, "total": 0, "filename": ""}):
            _, seen = self.run_ocr(FakePdfium(pages=2))
            self.assertEqual([p for _, _, p in seen], [
                {"active": True, "page": 1, "total": 2, "filename": "doc.pdf"},
                {"active": True, "page": 2, "total": 2, "filename": "doc.pdf"}])
            self.assertEqual(extract.progress(),
                             {"active": False, "page": 2, "total": 2, "filename": "doc.pdf"})


class LayoutExportHoldsTheLock(unittest.TestCase):

    def test_it_is_extracts_lock(self):
        self.assertIs(L._pdfium_lock(), extract.PDFIUM_LOCK)

    def test_every_pdfium_call_of_the_source_holds_the_lock(self):
        fake = FakePdfium(pages=3, size=(595.0, 842.0))
        with mock.patch.object(L, "pdfium", fake):
            src = L._Source(PDF, "doc.pdf")
            self.assertEqual(src.count, 3)
            self.assertEqual(src.size(0), (595.0, 842.0))
            self.assertEqual(src.render(2).mode, "RGB")
            src.close()
        self.assertEqual(fake.names(), ["open", "len", "page", "get_size", "page.close",
                                        "page", "render", "page.close", "close"])
        self.assertTrue(all(held for _, held in fake.calls), fake.calls)
        self.assertFalse(_held())

    def test_a_failed_render_still_closes_its_page_under_the_lock(self):
        fake = FakePdfium(fail_render=True)
        with mock.patch.object(L, "pdfium", fake):
            src = L._Source(PDF, "doc.pdf")
            with self.assertRaisesRegex(RuntimeError, "damaged page"):
                src.render(0)
        self.assertEqual(fake.names()[-2:], ["render", "page.close"])
        self.assertTrue(all(held for _, held in fake.calls), fake.calls)

    def test_an_export_does_not_wait_for_a_running_ocr(self):
        # Lock order: PDFIUM_LOCK may be taken under _infer_lock, never the
        # other way round -- the export needs PDFium only, not Surya.
        fake, done = FakePdfium(), threading.Event()

        def export():
            src = L._Source(PDF, "doc.pdf")
            src.size(0), src.render(0), src.close()
            done.set()

        with mock.patch.object(L, "pdfium", fake), extract._infer_lock:
            worker = threading.Thread(target=export)
            worker.start()
            self.assertTrue(done.wait(5))
        worker.join(5)

    def test_importing_the_module_still_does_not_import_extract(self):
        out = subprocess.run(
            [sys.executable, "-c", "import sys, layout_docx; print('extract' in sys.modules)"],
            capture_output=True, text=True, timeout=120,
            cwd=os.path.dirname(os.path.abspath(L.__file__)))
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


class RealPdfium(unittest.TestCase):
    """The same paths on a real blank PDF: pages closed by hand, then the
    document, with nothing left for pypdfium2 to complain about."""

    @classmethod
    def setUpClass(cls):
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument.new()
        for _ in range(2):
            doc.new_page(144, 216)            # 2 x 3 inches
        buf = io.BytesIO()
        doc.save(buf)
        doc.close()
        cls.data = buf.getvalue()

    def test_extract_renders_and_closes(self):
        sizes = []
        page_text = lambda rec, img: (sizes.append((img.mode, img.size)), ("", None))[1]
        with self.assertNoLogs("pypdfium2", level=logging.WARNING), \
                mock.patch.object(extract, "ensure_loaded", lambda: object()), \
                mock.patch.object(extract, "_page_text", page_text), \
                mock.patch.dict(extract._progress):
            result = extract.extract_document(self.data, "blank.pdf", progress=False)
        self.assertEqual(result.page_count, 2)
        self.assertEqual([mode for mode, _ in sizes], ["RGB", "RGB"])
        for _, size in sizes:
            self.assert_pixels(size, extract.RENDER_DPI)

    def assert_pixels(self, size, dpi):
        for got, inches in zip(size, (2, 3)):
            self.assertIn(got - inches * dpi, (0, 1), size)   # PDFium may round an edge up

    def test_layout_source_measures_renders_and_closes(self):
        with self.assertNoLogs("pypdfium2", level=logging.WARNING):
            src = L._Source(self.data, "blank.pdf")
            try:
                self.assertEqual(src.count, 2)
                self.assertEqual(src.size(1), (144.0, 216.0))
                self.assert_pixels(src.render(0).size, L.LAYOUT_DPI)
            finally:
                src.close()
            self.assertIsNone(src.pdf.raw)


if __name__ == "__main__":
    unittest.main()
