"""Complaint-management service and HTTP API (the «إدارة الشكاوى» tab).

ComplaintService owns ONE daemon worker thread that drains the store's queue a
complaint at a time: OCR (Surya, only for files that have no text yet) -> the
two-step LLM pipeline (complaints.analyze) -> store.save_analysis. One at a
time because every model here has a single inference slot on a shared 16 GB
GPU: a second worker would only wait behind the first and crowd /structure and
/chat. Uploads therefore return at once with queued items, and the UI polls
/complaints/queue.

The router trusts nothing a browser sends: body sizes are checked before
parsing, uploads are typed by magic bytes, ids are checked against the
taxonomy before the store sees them, and a state change sent from another
site (Origin / Sec-Fetch-Site) is refused. Errors are {"error": "<Arabic>"};
document text, model output and exception details never appear in one (the
log gets an escaped repr at most), and the query strings of /complaints paths
(register searches: names, national ids) are cut from uvicorn's access log.

Importing this module touches no file and starts no thread. create_router()
builds the default service (the store under CMS_DATA_DIR), but the worker
starts with the app (a router startup handler), so `import app` never begins
processing a queue. Even then only the process that holds the store's owner
lock works the queue: a second app started on the same data directory leaves
the running one's queue alone. If the store cannot be opened at all (locked,
damaged, newer schema), the routes answer 503 and the rest of the app runs.
"""
from __future__ import annotations

import csv
import inspect
import io
import json
import logging
import os
import re
import sys
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

import complaints
import complaints_llm
from complaints import AnalysisError
from complaints_llm import ProviderBusy, ProviderError, ProviderUnavailable
from complaints_store import (FEEDBACK_FIELDS, FIELD_ACTIONS, LIST_MAX, MAX_FIELD_VALUE, PROCESSING,
                              STAGES, Store, default_paths)
from complaints_taxonomy import get_taxonomy

MAX_FILES = 20                          # per upload request
MAX_BYTES = 100 * 1024 * 1024           # per file, as app.py's /extract
MAX_TEXT_CHARS = complaints.MAX_TEXT_CHARS
MAX_JSON_BYTES = 64 * 1024
# A pasted complaint is JSON as well, but 40 000 Arabic characters are ~80 KB of
# UTF-8 (six bytes each if the client escapes non-ASCII), past every other cap.
MAX_TEXT_BODY = MAX_TEXT_CHARS * 6 + 4096
FORM_OVERHEAD = 1024 * 1024             # boundaries and part headers of a full upload
MAX_FORM_FIELDS = 16
MAX_NOTE = 1000
MAX_REVIEWER = 100
MAX_QUERY = 200
MAX_FILENAME = 200
EXPORT_PAGE = LIST_MAX                  # the export reads every matching row, a page at a time
BUSY_RETRIES = 3
BUSY_WAIT_S = 5.0
IDLE_POLL_S = 30.0                      # safety net; notify() is what wakes the worker
STOP_JOIN_S = 5.0
# OCR holds the one Surya slot for a whole document (the analysis tab's
# /extract waits behind it) and reads ~3 s a page; the model sees ~12 000
# characters anyway. A longer PDF is refused before OCR starts.
MAX_PAGES_DEFAULT = 30
# A complaint interrupted this many times in a row (the app stopped or died
# while processing it) is failed at startup instead of being requeued forever.
MAX_INTERRUPTIONS = 3
SORTS = ("created", "priority", "due", "updated")

MSG_BUSY = "النموذج مشغول حالياً؛ أعد المعالجة لاحقاً"
MSG_UNAVAILABLE = "النموذج غير متاح حالياً؛ تحقق من تشغيله ثم أعد المعالجة"
MSG_FAILED = "تعذرت معالجة الشكوى"
MSG_OCR = "تعذر استخراج النص من الملف"
MSG_NO_FILE = "الملف الأصلي غير متاح لاستخراج النص"
MSG_NOT_FOUND = "الشكوى غير موجودة"
MSG_NO_ORIGINAL = "لا يوجد ملف أصلي لهذه الشكوى"
MSG_PROCESSING = "الشكوى قيد المعالجة حالياً؛ انتظر اكتمالها ثم أعد المحاولة"
MSG_UNSUPPORTED = "نوع الملف غير مدعوم (PDF أو صورة أو نص)"
MSG_TOO_LARGE = "حجم الطلب أكبر من الحد المسموح"
MSG_REOCR_EMPTY = "لم تستخرج إعادة القراءة أي نص؛ بقي النص السابق كما هو"
MSG_GAVE_UP = "توقفت معالجة الشكوى مراراً قبل اكتمالها؛ راجع الملف ثم أعد المعالجة"
MSG_CROSS_SITE = "رُفض الطلب لأنه صادر من موقع آخر"
MSG_SERVICE_DOWN = "خدمة إدارة الشكاوى غير متاحة حالياً (تعذر فتح قاعدة بياناتها)؛ راجع سجل التطبيق ثم أعد تشغيله"
MSG_GOVERNORATE_REGION = "المحافظة لا تتبع المنطقة المختارة"
MSG_NOT_DONE = "لا يمكن مراجعة الشكوى قبل اكتمال معالجتها"
MSG_NO_FIELD = "الحقل غير موجود في بيانات الشكوى"

# Saudi Arabia keeps UTC+3 without DST: the store and the UI count days the same way.
RIYADH = timezone(timedelta(hours=3), "Asia/Riyadh")
_AR_DIGITS = str.maketrans("0123456789,", "٠١٢٣٤٥٦٧٨٩٬")
_ID = re.compile(r"[0-9]{1,18}")        # fits SQLite's 64-bit integer
_INT = re.compile(r"[0-9]{1,9}")
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"),
                (b"II*\x00", "tif"), (b"MM\x00*", "tif"), (b"BM", "bmp"))
_MEDIA = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
          "jpeg": "image/jpeg", "webp": "image/webp", "tif": "image/tiff",
          "tiff": "image/tiff", "bmp": "image/bmp", "txt": "text/plain; charset=utf-8"}
_FILTER_KINDS = (("status", "statuses"), ("category", "categories"), ("ministry", "ministries"),
                 ("priority", "priorities"), ("region", "regions"), ("governorate", "governorates"))
_FIELD_KINDS = {"category": "categories", "ministry": "ministries",
                "priority": "priorities", "region": "regions", "governorate": "governorates"}
_FIELD_LABELS = {"category": "التصنيف", "subcategory": "التصنيف الفرعي",
                 "ministry": "الجهة المختصة", "priority": "الأولوية", "region": "المنطقة",
                 "governorate": "المحافظة"}
CSV_HEADERS = ("المرجع", "تاريخ الاستلام", "الموضوع", "مقدم الشكوى", "التصنيف",
               "التصنيف الفرعي", "الجهة المختصة", "الأولوية", "المنطقة", "المحافظة",
               "الحالة", "المهلة", "تحتاج مراجعة", "مصدر الأولوية", "الملف")
_PRIORITY_SOURCES = {"llm": "تقدير النموذج", "rule_floor": "رفعتها القواعد",
                     "rule_cap": "أبقتها القواعد دون الأعلى لحين المراجعة"}
_CSV_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _ar(n: int) -> str:
    return f"{n:,}".translate(_AR_DIGITS)


def _size(n: int) -> str:
    mb = 1024 * 1024
    return f"{_ar(n // mb)} ميجابايت" if n >= mb else f"{_ar(n)} بايت"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: str, exc: BaseException | None = None) -> None:
    """A log line that cannot fail. ascii() escapes Arabic: the app's stdout is
    often cp1252 (run.ps1 redirects it to a log), where printing a repr that
    holds Arabic raises inside an except block and would strand the item.
    The message is escaped too: an f-string may carry a name or a path."""
    message = message.encode("ascii", "backslashreplace").decode("ascii")
    try:
        print(message, ascii(exc)) if exc is not None else print(message)
    except Exception:
        pass


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name) or default)))
    except ValueError:
        return default


# =============================================================================
# Upload helpers
# =============================================================================


def clean_filename(name, *, path: bool = True) -> str:
    """Display name of an upload (or a pasted complaint's title): the last path
    component, without control or bidi-override characters (they can disguise
    a name in the register), Arabic kept, at most MAX_FILENAME characters. It
    is never used as a path: files are stored as <id>.<ext>."""
    if not isinstance(name, str):
        return ""
    if path:
        name = re.split(r"[\\/]", name)[-1]
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > MAX_FILENAME:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and len(ext) <= 10:
            name = stem[:MAX_FILENAME - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:MAX_FILENAME].rstrip()
    return name


def detect_upload(data: bytes, filename: str = "", content_type: str = "") -> tuple[str, str] | None:
    """(file_kind, stored extension), or None when unsupported.

    The magic bytes of extract.detect_kind, which cannot be imported here (it
    loads torch). Unlike it, a PDF or image is never accepted by its name
    alone: a renamed file would be stored and only fail later, in OCR. The one
    exception is a PDF whose header sits after a little junk, which PDF
    readers accept. A .txt (by name, or sent as text/plain) must then decode
    as UTF-8."""
    name = (filename or "").lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    if name.endswith(".txt"):
        return "txt", "txt"
    if data[:5] == b"%PDF-":
        return "pdf", "pdf"
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return "image", ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "webp"
    if name.endswith(".pdf") and b"%PDF-" in data[:1024]:
        return "pdf", "pdf"
    if ctype == "text/plain":
        return "txt", "txt"
    return None


def _normalize_text(text: str) -> str:
    # One newline convention: provenance numbers lines on "\n", and so does the UI.
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def decode_text(data: bytes) -> str | None:
    """A .txt upload's text (strict UTF-8, BOM dropped), or None if it is not text."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else _normalize_text(text)


def _default_ocr(data: bytes, filename: str):
    from extract import extract_document      # lazily: importing extract loads torch/Surya
    # The analysis tab polls extract's one progress record: a complaint's
    # pages must not show up there as the user's own document, where
    # extract_document can leave them out.
    try:
        quiet = "progress" in inspect.signature(extract_document).parameters
    except (TypeError, ValueError):
        quiet = False
    return extract_document(data, filename, progress=False) if quiet else extract_document(data, filename)


_PDFIUM_FALLBACK = threading.Lock()


def _pdfium_lock():
    """The lock the app's other PDFium users hold (PDFium is not thread-safe):
    extract.PDFIUM_LOCK when extract provides one, else the Surya lock its
    page renders run under. Never imports extract (that loads torch): when
    nothing has imported it, nothing else in this process renders PDFs."""
    extract = sys.modules.get("extract")
    return (getattr(extract, "PDFIUM_LOCK", None) or getattr(extract, "_infer_lock", None)
            or _PDFIUM_FALLBACK)


def _default_page_count(data: bytes) -> int:
    """Pages of a PDF; 0 when PDFium cannot read it (OCR then reports that)."""
    import pypdfium2 as pdfium                # lazily: only the worker counts pages
    with _pdfium_lock():
        try:
            doc = pdfium.PdfDocument(data)
        except Exception:
            return 0
        try:
            return len(doc)
        finally:
            doc.close()


class _RedactQuery(logging.Filter):
    """uvicorn's access log prints the query string, and a register search
    (/complaints/items?q=…, export.csv?q=…) is a name or a national id: the
    query of a /complaints path is cut from the line."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn: '%s - "%s %s HTTP/%s" %d' % (client, method, path?query, version, status)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path.startswith("/complaints") and "?" in path:
                record.args = (*args[:2], path.split("?", 1)[0] + "?[redacted]", *args[3:])
        return True


def _redact_access_log() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactQuery) for f in access.filters):
        access.addFilter(_RedactQuery())


# =============================================================================
# Service
# =============================================================================


class ComplaintService:
    """The processing queue: one daemon worker, one complaint at a time."""

    def __init__(self, store, taxonomy, providers, *, ocr=None, page_count=None, autostart=True,
                 clock=None):
        self.store = store
        self.taxonomy = taxonomy
        self.providers = providers            # complaints_llm.ProviderRegistry
        self.home_region = taxonomy.receiving_entity.extra["region"]
        if getattr(store, "home_region", None) is None:
            store.home_region = self.home_region      # outside_jurisdiction follows it
        self._ocr = ocr or _default_ocr
        self._page_count = page_count or _default_page_count
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.busy_retries = BUSY_RETRIES
        self.busy_wait = BUSY_WAIT_S
        self.fewshot = _env_int("CMS_FEWSHOT", 3, 0, 10)
        self.max_pages = _env_int("CMS_MAX_PAGES", MAX_PAGES_DEFAULT, 1, 2000)
        self.sla_hours = {p.id: p.extra["sla_hours"] for p in taxonomy.priorities}
        self.open_statuses = tuple(s.id for s in taxonomy.statuses if s.extra["open"])
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._life = threading.Lock()         # start/stop
        self._busy = threading.Lock()         # one item at a time, worker or caller
        self._thread: threading.Thread | None = None
        self._booted = False
        # False once boot() finds another process working this store's queue.
        self.owns_queue = True
        self._used: set = set()               # providers used since the queue last drained
        self._used_lock = threading.Lock()
        self._unsettled: dict[int, str] = {}  # claims whose failure the database refused
        if autostart:
            self.start()

    # ---------------------------------------------------------- lifecycle

    def boot(self) -> None:
        """App startup: take the queue over, requeue what a previous process
        left half-done, then start.

        Once per service: FastAPI (0.141) runs an included router's startup
        handlers twice, and a second reset_interrupted() would requeue the item
        the worker has just claimed. Only as the queue's owner: uvicorn runs
        startup before it binds the port, so a second launch on the same data
        directory gets here too, and must not requeue — or process — the items
        of the app already running (its rows and uploads are still served)."""
        with self._life:
            if self._booted:
                return
            self._booted = True
        try:
            owner = self.store.acquire_owner()
        except Exception as exc:
            _log("complaints boot error:", exc)
            owner = False
        if not owner:
            self.owns_queue = False
            _log("complaints: another process is working this queue; the worker stays off")
            return
        try:
            n = self.store.reset_interrupted(max_interruptions=MAX_INTERRUPTIONS, message=MSG_GAVE_UP)
        except Exception as exc:
            _log("complaints boot error:", exc)
            n = 0
        if n:
            _log(f"complaints: requeued {n} interrupted complaint(s)")
        self.start()

    def start(self) -> None:
        with self._life:
            self._stop.clear()
            if self._thread is not None and self._thread.is_alive():
                return      # a stop() that timed out never ended it: it simply carries on
            self._thread = threading.Thread(target=self._run, name="complaints-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = STOP_JOIN_S) -> None:
        """Ask the worker to exit and wait briefly. A model call in flight
        cannot be interrupted; the thread is a daemon, and whatever it leaves
        half-done is requeued at the next start."""
        self._stop.set()
        self._wake.set()
        with self._life:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def notify(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    def now_iso(self) -> str:
        now = self._clock()
        return _iso(now if now.tzinfo else now.replace(tzinfo=timezone.utc))

    def _run(self) -> None:
        while not self._stop.is_set():
            # Cleared before looking at the queue, so a notify() that lands
            # after an empty claim is never lost.
            self._wake.clear()
            try:
                worked = self.process_next()
            except Exception as exc:          # e.g. the database is locked: never die
                _log("complaints worker error:", exc)
                self._stop.wait(1.0)
                continue
            if not worked:
                self._wake.wait(IDLE_POLL_S)

    # ---------------------------------------------------------- processing

    def process_next(self) -> bool:
        """Claim and process one complaint synchronously. False when the queue is
        empty (the worker then frees ALLaM-like providers) or the service stopped."""
        with self._busy:
            if self._stop.is_set():
                return False
            self._settle_deferred()
            item = self.store.claim_next()
            if item is None:
                self._release_idle()
                self.store.sweep_orphans()
                return False
            try:
                self._process(item)
            except Exception as exc:
                _log("complaints worker error:", exc)
                try:
                    self._settle(item["id"], MSG_FAILED)
                except Exception as again:
                    # The database refused the failure too (locked, full): the
                    # row would sit in its processing stage for good. Let go of
                    # the claim, so delete / reprocess work on it, and retry the
                    # failure on every pass until it lands.
                    _log("complaints worker error:", again)
                    self.store.release_claim(item["id"])
                    self._unsettled[item["id"]] = MSG_FAILED
            return True

    def _settle_deferred(self) -> None:
        """Retry the failures the database refused (see process_next). One that
        still fails raises, and the worker's loop backs off and comes back."""
        for id, message in list(self._unsettled.items()):
            if self._stop.is_set():
                return
            # Only while still stuck: a user may have requeued it meanwhile.
            self.store.fail(id, message, if_stuck=True)
            del self._unsettled[id]

    def is_busy(self, detail: dict) -> bool:
        """Whether a worker may be processing this complaint right now (the API
        then refuses delete / reprocess). In a processing stage, and held by
        our worker — or by another process's, when that process owns the queue."""
        if detail.get("stage") not in PROCESSING:
            return False
        return not self.owns_queue or self.store.is_claimed(detail["id"])

    def _process(self, item: dict) -> None:
        text = item.get("text")
        if item.get("needs_ocr", text is None):
            text = self._extract(item)
            if text is None:
                return
        if self._stop.is_set():
            self.store.requeue(item["id"], interrupted=True)   # shutting down: the next start continues
            return
        self._analyze(item, text)

    def _extract(self, item: dict) -> str | None:
        id, path = item["id"], item.get("file_path")
        if not path:
            self.store.fail(id, MSG_NO_FILE)
            return None
        try:
            data = Path(path).read_bytes()
            if item.get("file_kind") == "pdf":
                pages = self._page_count(data)
                if pages > self.max_pages:
                    self.store.fail(id, f"عدد صفحات الملف ({_ar(pages)}) يتجاوز الحد المسموح "
                                        f"({_ar(self.max_pages)} صفحة)")
                    return None
            # The stored name (<id>.<ext>, typed by magic bytes at intake), never
            # the display name: OCR types a file by its name first, and a scan
            # uploaded as «scan.pdf» that is really a JPEG would fail there.
            result = self._ocr(data, Path(path).name)
            text, pages = result.full_text, result.page_count
        except Exception as exc:
            _log("complaints ocr error:", exc)
            self._settle(id, MSG_OCR)
            return None
        text = text if isinstance(text, str) else ""
        try:
            pages = max(0, int(pages or 0))
        except (TypeError, ValueError):
            pages = 0
        if not text.strip() and (item.get("text") or "").strip():
            # A re-extraction that read nothing: the complaint keeps the text
            # (and analysis) it has instead of being blanked by a bad pass.
            self._settle(id, MSG_REOCR_EMPTY)
            return None
        # Empty text is still analysed: the pipeline flags it empty_text for a human.
        self.store.save_text(id, text, pages)
        return text

    def _analyze(self, item: dict, text: str) -> None:
        id = item["id"]
        # Read per item: switching the provider applies from the next complaint.
        provider = self.providers.active()
        with self._used_lock:
            self._used.add(provider)
        examples = self.store.recent_corrections(self.fewshot) if self.fewshot else []
        attempt = 0
        reloaded = False
        while True:
            try:
                provider.ensure_ready()
                analysis = complaints.analyze(
                    text, provider, self.taxonomy, examples=examples,
                    repeat_lookup=lambda nid: self.store.repeat_count(nid, exclude_id=id),
                    on_stage=lambda stage: self.store.set_stage(id, stage))
                break
            except ProviderBusy:
                # /structure and /chat share Qwen's single slot; llm.Server has
                # already waited for it, so these retries are for a long queue.
                attempt += 1
                if attempt > self.busy_retries:
                    self._settle(id, MSG_BUSY)
                    return
                if self._stop.wait(self.busy_wait):
                    self.store.requeue(id, interrupted=True)
                    return
            except ProviderUnavailable:
                # A lazily loaded model (ALLaM) may have been stopped under the
                # request by something outside this service; ensure_ready()
                # loads it again, so it gets one more try.
                if provider.release_after_batch and not reloaded and not self._stop.is_set():
                    reloaded = True
                    continue
                _log(f"complaints worker: provider {provider.id!r} unavailable")
                self._settle(id, MSG_UNAVAILABLE)
                return
            except (AnalysisError, ProviderError) as exc:
                _log("complaints analysis error: " + type(exc).__name__)   # str() is Arabic, no detail
                self._settle(id, str(exc) or MSG_FAILED)
                return
        due = complaints.due_at(item["created_at"], analysis["classification"]["priority"],
                                self.taxonomy)
        self.store.save_analysis(id, analysis, due_at=due)

    def _settle(self, id: int, message: str) -> None:
        """Fail the item, unless the app is shutting down: then the failure is
        most likely the model server being stopped under it, and the item is
        requeued rather than marked as the complaint's fault."""
        if self._stop.is_set():
            self.store.requeue(id, interrupted=True)
        else:
            self.store.fail(id, message)

    def _release_idle(self) -> None:
        with self._used_lock:
            used, self._used = self._used, set()
        for provider in used:
            if provider.release_after_batch:          # ALLaM: give its VRAM back
                try:
                    provider.release()
                except Exception as exc:
                    _log("complaints release error:", exc)

    def done_with(self, provider) -> None:
        """A caller outside the worker (insights) has used `provider`: an
        ALLaM-like one is released with the worker's next idle pass — after
        the batch, if one is running — or at once when no worker runs here."""
        if not provider.release_after_batch:
            return
        if self.running:
            with self._used_lock:
                self._used.add(provider)
            self.notify()
            return
        try:
            provider.release()
        except Exception as exc:
            _log("complaints release error:", exc)

    # ---------------------------------------------------------- queries

    def analytics(self) -> dict:
        return self.store.analytics(sla_hours=self.sla_hours, open_statuses=self.open_statuses)


def _default_service() -> ComplaintService:
    db_path, files_dir = default_paths()
    taxonomy = get_taxonomy()
    store = Store(db_path, files_dir, home_region=taxonomy.receiving_entity.extra["region"])
    try:
        return ComplaintService(store, taxonomy, complaints_llm.get_registry(), autostart=False)
    except BaseException:
        store.close()
        raise


# =============================================================================
# HTTP
# =============================================================================


class _Reject(Exception):
    """An error answer raised anywhere while handling a request."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message

    def response(self) -> JSONResponse:
        return JSONResponse({"error": self.message}, status_code=self.status)


class _BodyTooLarge(Exception):
    pass


async def _capped(stream, limit: int):
    """The request stream, cut off past `limit` bytes: a chunked body has no
    Content-Length to refuse up front."""
    size = 0
    async for chunk in stream:
        size += len(chunk)
        if size > limit:
            raise _BodyTooLarge
        yield chunk


async def _json_body(request: Request, limit: int = MAX_JSON_BYTES) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise _Reject(415, "يجب إرسال البيانات بصيغة JSON")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise _Reject(413, MSG_TOO_LARGE)
    body = bytearray()
    try:
        async for chunk in _capped(request.stream(), limit):
            body.extend(chunk)
    except _BodyTooLarge:
        raise _Reject(413, MSG_TOO_LARGE) from None
    if not body.strip():
        return {}                     # every body field here is optional or checked below
    try:
        payload = json.loads(bytes(body))
    except (ValueError, UnicodeError, RecursionError):
        raise _Reject(400, "JSON غير صالح") from None
    if not isinstance(payload, dict):
        raise _Reject(400, "بيانات الطلب غير صالحة")
    return payload


async def _read_form(request: Request):
    """The multipart form, bounded before a byte is spooled to disk."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "multipart/form-data":
        raise _Reject(400, "لم تُرفق أي ملفات")
    limit = MAX_FILES * MAX_BYTES + FORM_OVERHEAD
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise _Reject(413, MSG_TOO_LARGE)
    parser = MultiPartParser(request.headers, _capped(request.stream(), limit),
                             max_files=MAX_FILES, max_fields=MAX_FORM_FIELDS)
    try:
        return await parser.parse()
    except _BodyTooLarge:
        raise _Reject(413, MSG_TOO_LARGE) from None
    except MultiPartException as exc:
        if "Too many files" in str(getattr(exc, "message", "")):
            raise _Reject(413, f"عدد الملفات أكبر من الحد المسموح ({_ar(MAX_FILES)} ملفاً في الطلب الواحد)") from None
        raise _Reject(400, "تعذر قراءة الملفات المرفوعة") from None


def _same_origin(request: Request) -> None:
    """Refuse a state change sent from another site (CSRF). A multipart upload
    needs no CORS preflight, so any page could post one to this loopback app;
    browsers label such a request with Sec-Fetch-Site and Origin. A request
    without either (curl, the tests) is not a browser's cross-site one."""
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        raise _Reject(403, MSG_CROSS_SITE)
    origin = request.headers.get("origin")
    if origin is not None:
        own = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin.strip().lower() != own.lower():
            raise _Reject(403, MSG_CROSS_SITE)


def _item_id(raw: str) -> int:
    if not isinstance(raw, str) or not _ID.fullmatch(raw):
        raise _Reject(404, MSG_NOT_FOUND)
    return int(raw)


def _optional_str(body: dict, key: str, limit: int, label: str) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _Reject(400, f"{label} غير صالح")
    value = value.strip()
    if len(value) > limit:
        raise _Reject(400, f"{label} أطول من الحد المسموح ({_ar(limit)} حرف)")
    return value


def _csv_cell(value) -> str:
    """A cell Excel will not run: a leading = + - @ tab or CR makes a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_CSV_FORMULA) else text


def _local_time(iso) -> str:
    if not isinstance(iso, str) or not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(RIYADH).strftime("%Y-%m-%d %H:%M")


def _unavailable_router() -> APIRouter:
    """Stands in for the complaints routes when the default service cannot be
    built (the database is locked by another program, damaged, or newer than
    this code): every /complaints route answers 503 with the reason in Arabic,
    the tab's assets still load so it can show it, and the rest of the app
    starts as usual. `router.service` is None."""
    router = APIRouter(prefix="/complaints", tags=["complaints"])
    router.service = None
    here = Path(__file__).parent

    @router.get("/ui.js")
    def script():
        return FileResponse(here / "complaints_ui.js", media_type="text/javascript")

    @router.get("/ui.css")
    def stylesheet():
        return FileResponse(here / "complaints.css", media_type="text/css")

    @router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def unavailable(rest: str):
        return JSONResponse({"error": MSG_SERVICE_DOWN}, status_code=503)

    return router


def create_router(service: ComplaintService | None = None) -> APIRouter:
    """Routes under /complaints. With no service, the default one is built
    now (store at CMS_DATA_DIR or <repo>/data/complaints, the cached taxonomy
    and provider registry) and booted by the app's startup: interrupted rows
    requeued, worker started. If it cannot be built, the routes answer 503
    (_unavailable_router) instead of failing `import app`. A service passed in
    is the caller's to start and stop. Either way it is exposed as
    `router.service` (app.py stops it)."""
    _redact_access_log()
    startup = []
    if service is None:
        try:
            service = _default_service()
        except Exception as exc:
            _log("complaints service unavailable:", exc)
            return _unavailable_router()
        startup.append(service.boot)
    router = APIRouter(prefix="/complaints", tags=["complaints"], on_startup=startup)
    router.service = service
    store, taxonomy, registry = service.store, service.taxonomy, service.providers
    here = Path(__file__).parent
    insights_slot = threading.BoundedSemaphore(1)

    def limits() -> dict:
        return {"max_files": MAX_FILES, "max_bytes": MAX_BYTES, "max_text_chars": MAX_TEXT_CHARS}

    def label(kind: str, id) -> str:
        return taxonomy.label(kind, id) or (id or "")

    def detail_out(detail: dict) -> dict:
        if detail.get("stage") == "done":
            try:
                detail["acknowledgment"] = complaints.acknowledgment(detail, taxonomy)
            except Exception as exc:
                _log("complaints acknowledgment error:", exc)
        return detail

    def get_or_404(id: int) -> dict:
        detail = store.get(id)
        if detail is None:
            raise _Reject(404, MSG_NOT_FOUND)
        return detail

    def filters(params) -> dict:
        """Register filters from the query string; "" means no filter."""
        out: dict = {}
        for key, kind in _FILTER_KINDS:
            value = params.get(key)
            if value:
                if value not in taxonomy.ids(kind):
                    raise _Reject(400, f"قيمة غير صالحة لمعامل التصفية «{key}»")
                out[key] = value
        stage = params.get("stage")
        if stage:
            if stage not in STAGES:
                raise _Reject(400, "قيمة غير صالحة لمعامل التصفية «stage»")
            out["stage"] = stage
        review = (params.get("needs_review") or "").strip().lower()
        if review:
            if review not in ("0", "1", "true", "false"):
                raise _Reject(400, "قيمة غير صالحة لمعامل التصفية «needs_review»")
            out["needs_review"] = "1" if review in ("1", "true") else "0"
        q = (params.get("q") or "").strip()
        if len(q) > MAX_QUERY:
            raise _Reject(400, "نص البحث أطول من الحد المسموح")
        if q:
            out["q"] = q
        sort, order = params.get("sort") or "created", params.get("order") or "desc"
        if sort not in SORTS:
            raise _Reject(400, "قيمة غير صالحة لمعامل الترتيب «sort»")
        if order not in ("asc", "desc"):
            raise _Reject(400, "قيمة غير صالحة لمعامل الترتيب «order»")
        out.update(sort=sort, order=order)
        return out

    def int_param(params, key: str, default: int) -> int:
        raw = params.get(key)
        if raw in (None, ""):
            return default
        if not _INT.fullmatch(raw):
            raise _Reject(400, f"قيمة غير صالحة للمعامل «{key}»")
        return int(raw)

    # ------------------------------------------------------------ assets

    @router.get("/ui.js")
    def script():
        return FileResponse(here / "complaints_ui.js", media_type="text/javascript")

    @router.get("/ui.css")
    def stylesheet():
        return FileResponse(here / "complaints.css", media_type="text/css")

    # ------------------------------------------------------------ config

    @router.get("/config")
    def config():
        # Sync (threadpool): an OpenAI-compatible provider's status is a network probe.
        return {"taxonomy": taxonomy.to_public(), "providers": registry.list(),
                "active_provider": registry.active().id, "limits": limits()}

    @router.put("/provider")
    async def provider(request: Request):
        try:
            _same_origin(request)
            body = await _json_body(request)
        except _Reject as exc:
            return exc.response()
        wanted = body.get("provider")
        if not isinstance(wanted, str) or wanted not in registry.ids():
            return _Reject(400, "مزوّد النموذج غير معروف").response()
        # Leaving ALLaM stops its server, which can take seconds.
        await run_in_threadpool(registry.set_active, wanted)
        providers = await run_in_threadpool(registry.list)
        return {"active_provider": registry.active().id, "providers": providers}

    # ------------------------------------------------------------ intake

    async def intake(upload: UploadFile) -> dict:
        name = clean_filename(upload.filename)
        data = await upload.read(MAX_BYTES + 1)
        if not data:
            return {"filename": name, "error": "الملف فارغ"}
        if len(data) > MAX_BYTES:
            return {"filename": name, "error": f"حجم الملف يتجاوز الحد المسموح ({_size(MAX_BYTES)})"}
        kind = detect_upload(data, name, upload.content_type or "")
        if kind is None:
            return {"filename": name, "error": MSG_UNSUPPORTED}
        file_kind, ext = kind
        text = None
        if file_kind == "txt":
            text = decode_text(data)
            if text is None:
                return {"filename": name, "error": "الملف النصي غير صالح؛ يجب أن يكون بترميز UTF-8"}
            if not text.strip():
                return {"filename": name, "error": "الملف النصي لا يحتوي نصاً"}
            if len(text) > MAX_TEXT_CHARS:
                return {"filename": name, "error": f"النص أطول من الحد المسموح ({_ar(MAX_TEXT_CHARS)} حرف)"}
        try:
            summary, duplicate = await run_in_threadpool(
                store.create, source="upload", filename=name, file_kind=file_kind,
                data=data, text=text, file_ext=ext)
        except Exception as exc:
            _log("complaints upload error:", exc)
            return {"filename": name, "error": "تعذر حفظ الملف"}
        return {"filename": name, "id": summary["id"], "ref": summary["ref"],
                "stage": summary["stage"], "duplicate": duplicate}

    @router.post("/upload")
    async def upload(request: Request):
        try:
            _same_origin(request)                 # before a byte of the form is read
            form = await _read_form(request)
        except _Reject as exc:
            return exc.response()
        try:
            files = [v for k, v in form.multi_items() if k == "files" and isinstance(v, UploadFile)]
            if not files:
                return _Reject(400, "لم تُرفق أي ملفات").response()
            # One file at a time: at most one upload's bytes are in memory.
            items = [await intake(f) for f in files]
        finally:
            await form.close()
        if any("id" in it and not it["duplicate"] for it in items):
            service.notify()
        return {"items": items}

    @router.post("/text")
    async def paste(request: Request):
        try:
            _same_origin(request)
            body = await _json_body(request, MAX_TEXT_BODY)
            text = body.get("text")
            if not isinstance(text, str):
                raise _Reject(400, "نص الشكوى مطلوب")
            title = body.get("title")
            if title is not None and not isinstance(title, str):
                raise _Reject(400, "العنوان غير صالح")
            text = _normalize_text(text)
            if not text.strip():
                raise _Reject(400, "نص الشكوى فارغ")
            if len(text) > MAX_TEXT_CHARS:
                raise _Reject(413, f"النص أطول من الحد المسموح ({_ar(MAX_TEXT_CHARS)} حرف)")
            try:
                summary, duplicate = await run_in_threadpool(
                    store.create, source="text", filename=clean_filename(title, path=False),
                    file_kind=None, text=text)
            except Exception as exc:
                _log("complaints text error:", exc)
                raise _Reject(500, "تعذر حفظ الشكوى") from None
        except _Reject as exc:
            return exc.response()
        if not duplicate:
            service.notify()
        return {"item": {"id": summary["id"], "ref": summary["ref"], "stage": summary["stage"],
                         "duplicate": duplicate, "filename": summary["filename"]}}

    # ------------------------------------------------------------ register

    @router.get("/items")
    def items(request: Request):
        try:
            params = request.query_params
            chosen = filters(params)
            limit = min(int_param(params, "limit", 200), LIST_MAX)
            offset = int_param(params, "offset", 0)
        except _Reject as exc:
            return exc.response()
        rows, total = store.list(**chosen, limit=limit, offset=offset,
                                 open_statuses=service.open_statuses)
        return {"items": rows, "total": total}

    @router.get("/items/{item_id}")
    def item(item_id: str):
        try:
            return detail_out(get_or_404(_item_id(item_id)))
        except _Reject as exc:
            return exc.response()

    @router.get("/items/{item_id}/file")
    def original(item_id: str):
        """The uploaded bytes. The path comes from the database row only
        (files_dir/<id>.<ext>); a pasted complaint has no file, so 404 — its
        text is in the detail."""
        try:
            id = _item_id(item_id)
        except _Reject as exc:
            return exc.response()
        path = store.file_path(id)
        if path is None:
            return _Reject(404, MSG_NO_ORIGINAL).response()
        ext = path.suffix.lstrip(".").lower()
        return FileResponse(path, media_type=_MEDIA.get(ext, "application/octet-stream"), headers={
            "Content-Disposition": f'inline; filename="complaint-{id}.{ext}"',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    def do_reprocess(id: int, ocr: bool) -> dict:
        if service.is_busy(get_or_404(id)):
            raise _Reject(409, MSG_PROCESSING)
        summary = store.requeue(id, ocr=ocr)
        if summary is None:
            raise _Reject(404, MSG_NOT_FOUND)
        return summary

    @router.post("/items/{item_id}/reprocess")
    async def reprocess(item_id: str, request: Request):
        try:
            _same_origin(request)
            id = _item_id(item_id)
            body = await _json_body(request)
            ocr = body.get("ocr", False)
            if not isinstance(ocr, bool):
                raise _Reject(400, "قيمة «ocr» غير صالحة")
            summary = await run_in_threadpool(do_reprocess, id, ocr)
        except _Reject as exc:
            return exc.response()
        service.notify()
        return {"item": summary}

    def couple_place(clean: dict, detail: dict) -> None:
        """Keep a correction's region and governorate consistent, as the UI's
        selects do: every governorate lies in the receiving entity's region.
        Naming a governorate (and no region) sets the region to the entity's;
        a known region other than the entity's has none of its governorates, so
        its governorate becomes unknown — and naming one there is refused.
        Setting the region to unknown (explicitly, even if it already was)
        clears the governorate the same way, since a real one would place the
        complaint in the entity's region; naming one with it is refused too."""
        home = service.home_region
        named = clean.get("governorate") not in (None, "", "unknown")
        if named and "region" not in clean:
            clean["region"] = home
        region = clean.get("region", detail["region"])
        if region == home or (region in (None, "", "unknown") and "region" not in clean):
            return
        if named:
            raise _Reject(400, MSG_GOVERNORATE_REGION)
        if "governorate" in clean or detail.get("governorate") not in (None, "", "unknown"):
            clean["governorate"] = "unknown"

    def do_feedback(id: int, body: dict) -> dict:
        verdict = body.get("verdict")
        if verdict not in ("confirm", "correct"):
            raise _Reject(400, "نوع التقييم غير صالح")
        changes = body.get("changes")
        changes = {} if changes is None else changes
        if not isinstance(changes, dict) or set(changes) - set(FEEDBACK_FIELDS):
            raise _Reject(400, "التعديلات غير صالحة")
        note = _optional_str(body, "note", MAX_NOTE, "الملاحظة")
        reviewer = _optional_str(body, "reviewer", MAX_REVIEWER, "اسم المراجع")
        clean: dict = {}
        for field, kind in _FIELD_KINDS.items():
            if field in changes:
                value = changes[field]
                if not isinstance(value, str) or value not in taxonomy.ids(kind):
                    raise _Reject(400, f"قيمة غير صالحة للحقل «{_FIELD_LABELS[field]}»")
                clean[field] = value
        detail = get_or_404(id)
        if detail["stage"] != "done":
            raise _Reject(409, MSG_NOT_DONE)
        category = clean.get("category", detail["category"])
        if "subcategory" in changes:
            sub = changes["subcategory"]
            if sub in (None, ""):
                clean["subcategory"] = None
            elif not isinstance(sub, str) or taxonomy.category_of_subcategory(sub) is None:
                raise _Reject(400, f"قيمة غير صالحة للحقل «{_FIELD_LABELS['subcategory']}»")
            elif taxonomy.category_of_subcategory(sub) != category:
                raise _Reject(400, "التصنيف الفرعي لا يتبع التصنيف المختار")
            else:
                clean["subcategory"] = sub
        elif detail["subcategory"] and taxonomy.category_of_subcategory(detail["subcategory"]) != category:
            clean["subcategory"] = None       # it belonged to the category just replaced
        if "region" in clean or "governorate" in clean:
            couple_place(clean, detail)
        due = None
        if "priority" in clean and clean["priority"] != detail["priority"]:
            due = complaints.due_at(detail["created_at"], clean["priority"], taxonomy)
        updated = store.add_feedback(id, verdict=verdict, changes=clean, note=note,
                                     reviewer=reviewer, due_at=due)
        if updated is None:
            raise _Reject(404, MSG_NOT_FOUND)
        return detail_out(updated)

    @router.post("/items/{item_id}/feedback")
    async def feedback(item_id: str, request: Request):
        try:
            _same_origin(request)
            id = _item_id(item_id)
            body = await _json_body(request)
            return await run_in_threadpool(do_feedback, id, body)
        except _Reject as exc:
            return exc.response()

    def do_field_review(id: int, body: dict) -> dict:
        """A reviewer accepts a structured field's value or changes it (the
        store keeps the decision beside the analysis). Which keys exist is the
        analysis's to say, so an unknown key is found by the store."""
        key = body.get("key")
        if not isinstance(key, str) or not key:
            raise _Reject(400, "الحقل المطلوب مراجعته غير صالح")
        action = body.get("action")
        if action not in FIELD_ACTIONS:
            raise _Reject(400, "نوع المراجعة غير صالح")
        value = None
        if action == "change":                # an accept keeps the current value
            value = body.get("value")
            if not isinstance(value, str):
                raise _Reject(400, "القيمة الجديدة للحقل غير صالحة")
            value = value.strip()
            if len(value) > MAX_FIELD_VALUE:
                raise _Reject(400, f"القيمة أطول من الحد المسموح ({_ar(MAX_FIELD_VALUE)} حرف)")
        note = _optional_str(body, "note", MAX_NOTE, "الملاحظة")
        reviewer = _optional_str(body, "reviewer", MAX_REVIEWER, "اسم المراجع")
        if get_or_404(id)["stage"] != "done":
            raise _Reject(409, MSG_NOT_DONE)
        try:
            updated = store.review_field(id, key, action=action, value=value,
                                         reviewer=reviewer, note=note)
        except ValueError:
            raise _Reject(400, MSG_NO_FIELD) from None
        if updated is None:
            raise _Reject(404, MSG_NOT_FOUND)
        return detail_out(updated)

    @router.post("/items/{item_id}/fields")
    async def fields(item_id: str, request: Request):
        try:
            _same_origin(request)
            id = _item_id(item_id)
            body = await _json_body(request)
            return await run_in_threadpool(do_field_review, id, body)
        except _Reject as exc:
            return exc.response()

    def do_status(id: int, body: dict) -> dict:
        status = body.get("status")
        if not isinstance(status, str) or status not in taxonomy.ids("statuses"):
            raise _Reject(400, "حالة غير معروفة")
        note = _optional_str(body, "note", MAX_NOTE, "الملاحظة")
        detail = store.set_status(id, status, note)
        if detail is None:
            raise _Reject(404, MSG_NOT_FOUND)
        return detail_out(detail)

    @router.post("/items/{item_id}/status")
    async def status(item_id: str, request: Request):
        try:
            _same_origin(request)
            id = _item_id(item_id)
            body = await _json_body(request)
            return await run_in_threadpool(do_status, id, body)
        except _Reject as exc:
            return exc.response()

    @router.delete("/items/{item_id}")
    def delete(item_id: str, request: Request):
        try:
            _same_origin(request)
            id = _item_id(item_id)
            if service.is_busy(get_or_404(id)):
                raise _Reject(409, MSG_PROCESSING)
            if not store.delete(id):
                raise _Reject(404, MSG_NOT_FOUND)
        except _Reject as exc:
            return exc.response()
        return {"deleted": True}

    # ------------------------------------------------------------ dashboard

    @router.get("/queue")
    def queue():
        return {**store.queue_state(), "worker": "running" if service.running else "stopped",
                "active_provider": registry.active().id}

    @router.get("/analytics")
    def analytics():
        return service.analytics()

    def do_insights() -> dict:
        data = service.analytics()
        if not data["totals"]["done"]:
            raise _Reject(409, "لا توجد شكاوى مكتملة المعالجة لتوليد الرؤى منها")
        # One at a time: each run holds the model for many seconds, and a
        # second click would only queue behind it on the same slot.
        if not insights_slot.acquire(blocking=False):
            raise _Reject(409, "يجري توليد الرؤى حالياً؛ انتظر اكتماله")
        chosen = registry.active()
        try:
            chosen.ensure_ready()
            result = complaints.insights(data, store.samples(30), chosen, taxonomy)
        except (ProviderBusy, ProviderUnavailable) as exc:
            raise _Reject(503, str(exc) or MSG_UNAVAILABLE) from None
        except (AnalysisError, ProviderError) as exc:
            raise _Reject(502, str(exc) or "تعذر توليد الرؤى والتوصيات") from None
        except Exception as exc:
            _log("complaints insights error:", exc)
            raise _Reject(500, "تعذر توليد الرؤى والتوصيات") from None
        finally:
            insights_slot.release()
            service.done_with(chosen)         # ALLaM: not left holding ~5 GB of VRAM
        return {"insights": result, "provider": chosen.id, "generated_at": service.now_iso()}

    @router.post("/insights")
    async def insights(request: Request):
        try:
            _same_origin(request)
            await _json_body(request)
            return await run_in_threadpool(do_insights)
        except _Reject as exc:
            return exc.response()

    def priority_source(row: dict, sources: dict) -> str:
        if row["stage"] != "done":
            return ""
        if row["reviewed"] and row["priority"] != row["model_priority"]:
            return "تعديل المراجع"
        # Only the stored analysis knows whether a rule floor raised it.
        return _PRIORITY_SOURCES.get(sources.get(row["id"]), "")

    @router.get("/export.csv")
    def export(request: Request):
        """Every matching complaint: a report made from the CSV must not
        silently miss rows. X-Total-Count carries the number of rows."""
        try:
            chosen = filters(request.query_params)
        except _Reject as exc:
            return exc.response()
        rows: list = []
        while True:
            page, total = store.list(**chosen, limit=EXPORT_PAGE, offset=len(rows),
                                     open_statuses=service.open_statuses)
            rows.extend(page)
            if not page or len(rows) >= total:
                break
        sources = store.priority_sources(r["id"] for r in rows if r["stage"] == "done")
        buf = io.StringIO()
        writer = csv.writer(buf)              # excel dialect: CRLF rows, minimal quoting
        writer.writerow(CSV_HEADERS)
        for r in rows:
            writer.writerow([_csv_cell(v) for v in (
                r["ref"], _local_time(r["created_at"]), r["subject"], r["complainant_name"],
                label("categories", r["category"]),
                taxonomy.subcategory_label(r["subcategory"]) or (r["subcategory"] or ""),
                label("ministries", r["ministry"]), label("priorities", r["priority"]),
                label("regions", r["region"]), label("governorates", r["governorate"]),
                label("statuses", r["status"]),
                _local_time(r["due_at"]), "نعم" if r["needs_review"] else "لا",
                priority_source(r, sources), r["filename"])])
        # The BOM makes Excel read UTF-8; without it Arabic opens as mojibake.
        return Response(("﻿" + buf.getvalue()).encode("utf-8"),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="complaints.csv"',
                                 "Cache-Control": "no-store", "X-Total-Count": str(len(rows))})

    return router
