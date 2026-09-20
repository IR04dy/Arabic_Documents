"""Standalone Arabic PDF pipeline: OCR -> structured fields -> chat.

Run:
    python -m uvicorn app:app --host 127.0.0.1 --port 8100

Then open http://127.0.0.1:8100, drop in a PDF. Every page is OCR'd with Surya 2
(full-page OCR + layout, GPU via llama.cpp). The OCR text is proofread by ALLaM-7B
(GPU), structured into fields by Qwen3-4B, and a document-grounded chat answers
questions about it. Models load once at startup and stay resident (the Qwen3
llama.cpp server + the Surya OCR llama.cpp server; ALLaM loads on demand).
"""

from __future__ import annotations

import io
import json
import re
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# OCR is Surya 2 (full-page OCR + layout, run via the bundled llama.cpp server).
OCR_ENGINE = "surya"
from extract import progress
from extract import ensure_loaded as ensure_ocr
from extract import extract_document
from extract import detect_kind
from extract import status as ocr_status
from extract import shutdown_server as _shutdown_ocr
from llm import ensure_loaded as ensure_llm
from llm import status as llm_status
from llm import stop_server as stop_llm
from llm import ensure_proof
from llm import stop_proof
from llm import proof_status
from proofread import run as proofread_run
from structure import parse_structure, parse_structure_for_template
from classify import classify as classify_document
from registry import RegistryError, get_registry
from chat import stream_events as chat_stream_events
from docx_export import build_docx
from layout_docx import build_layout_docx, shutdown_layout_server

app = FastAPI(title="Arabic PDF Pipeline", version="3.4.0")

from qr_client import create_router as create_qr_router
app.include_router(create_qr_router(extract_document))

MAX_BYTES = 100 * 1024 * 1024        # 100 MB upload ceiling
PROOFREAD_MAX_CHARS = 40000          # cap on /proofread input
CHAT_FULLTEXT_MAX = 24000            # cap on chat grounding text
CHAT_MSG_MAX = 4000                  # cap per chat message
CHAT_HISTORY_MAX = 40                # cap on turns sent
CLASSIFY_MAX_CHARS = 40000           # cap on /classify input

CSP = ("default-src 'self'; connect-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
       "frame-src blob:; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = CSP
    return resp


@app.on_event("startup")
def _warmup() -> None:
    # Load the models in the background so the server is reachable immediately.
    # Surya OCR + Qwen3 (structure/chat) stay resident. ALLaM (proofreader) is
    # loaded on demand per /proofread and freed after, to fit the 16 GB GPU.
    threading.Thread(target=_safe, args=(ensure_ocr,), daemon=True).start()
    threading.Thread(target=_safe, args=(ensure_llm,), daemon=True).start()
    get_registry()      # parse templates/*.yaml now: a bad registry must
                        # fail at boot, not on the first document.


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        pass   # status() already records the error


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_llm()
    shutdown_layout_server()      # reap the persistent Surya fast_layout server
    _shutdown_ocr()               # reap the Surya OCR llama.cpp server


@app.get("/health")
def health() -> dict:
    try:
        reg = get_registry()
        registry = {"status": "ok", "id": reg.registry_id,
                    "version": reg.registry_version, "templates": len(reg)}
    except Exception as exc:
        print("registry load error:", repr(exc))
        registry = {"status": "error"}
    return {"status": "ok", "ocr_engine": OCR_ENGINE, "engine": ocr_status(),
            "structurer": llm_status(), "proofreader": proof_status(),
            "registry": registry}


@app.get("/progress")
def progress_endpoint() -> dict:
    return progress()


@app.post("/extract")
async def extract(request: Request, file: UploadFile = File(...)):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BYTES + (1 << 20):
        return JSONResponse({"error": "file exceeds 100 MB"}, status_code=413)
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty file"}, status_code=400)
    if len(data) > MAX_BYTES:
        return JSONResponse({"error": "file exceeds 100 MB"}, status_code=413)
    if detect_kind(data, file.filename or "") is None:
        return JSONResponse({"error": "unsupported file type (PDF or image only)"},
                            status_code=415)
    try:
        result = await run_in_threadpool(
            extract_document, data, file.filename or "document")
    except Exception as exc:
        print("extract error:", repr(exc))
        return JSONResponse({"error": "extraction failed"}, status_code=500)
    return JSONResponse(result.to_dict())


EXPORT_MAX_BYTES = 16 * 1024 * 1024     # request-body ceiling for /export/docx


def _ascii_filename(name: str) -> str:
    """Header-safe ASCII fallback for Content-Disposition. The client sets the
    real (possibly Arabic) name via the download attribute, so this only needs
    to be a safe default: no path separators, no CR/LF header-injection, and no
    control bytes (a NUL would otherwise make h11 reject the response header)."""
    base = re.sub(r'[\x00-\x1f\x7f\\/:*?"<>|]+', "_", name or "").strip().strip(".")
    base = base.encode("ascii", "ignore").decode().strip() or "document"
    return base[:100]


@app.post("/export/docx")
async def export_docx(request: Request):
    """Return the extracted OCR text as an editable, RTL Arabic .docx.

    The whole OCR text is exported (no silent truncation — it must match the
    .txt export); the request body is bounded up front so a huge body can't
    exhaust memory."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > EXPORT_MAX_BYTES:
        return JSONResponse({"error": "text too large to export"}, status_code=413)
    raw = await request.body()
    if len(raw) > EXPORT_MAX_BYTES:
        return JSONResponse({"error": "text too large to export"}, status_code=413)
    try:
        payload = json.loads(raw or b"{}")
        text = payload.get("text") or ""
        filename = payload.get("filename") or "document"
    except Exception:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": "no text to export"}, status_code=400)
    name = _ascii_filename(filename if isinstance(filename, str) else "document")
    try:
        data = await run_in_threadpool(build_docx, text, name)
    except Exception as exc:
        print("docx export error:", repr(exc))
        return JSONResponse({"error": "export failed"}, status_code=500)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"),
        headers={"Content-Disposition": f'attachment; filename="{name}.docx"'},
    )


LAYOUT_PAGES_MAX = 500                   # sanity cap on page count
LAYOUT_PAGE_CHARS = 100_000             # per-page OCR text cap


@app.post("/export/layout-docx")
async def export_layout_docx(
    request: Request,
    file: UploadFile = File(...),
    pages: str = Form("[]"),
    filename: str = Form("document"),
):
    """Return a FORMATTED .docx: Surya reads the page image for the colour
    palette / heading structure, and the already-extracted per-page OCR text is
    poured in with heading, colour and table styling. Needs the original PDF or
    image (re-rendered locally) plus the per-page OCR text the client already has."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BYTES + (1 << 20):
        return JSONResponse({"error": "file exceeds 100 MB"}, status_code=413)
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty file"}, status_code=400)
    if len(data) > MAX_BYTES:
        return JSONResponse({"error": "file exceeds 100 MB"}, status_code=413)
    if detect_kind(data, file.filename or "") is None:
        return JSONResponse({"error": "unsupported file type (PDF or image only)"},
                            status_code=415)
    try:
        parsed = json.loads(pages or "[]")
        if not isinstance(parsed, list):
            raise ValueError
        pages_text = [(str(p) if p is not None else "")[:LAYOUT_PAGE_CHARS]
                      for p in parsed[:LAYOUT_PAGES_MAX]]
    except Exception:
        return JSONResponse({"error": "invalid pages"}, status_code=400)
    if not any(t.strip() for t in pages_text):
        return JSONResponse({"error": "no text to export"}, status_code=400)
    name = _ascii_filename(filename if isinstance(filename, str) else "document")
    try:
        docx = await run_in_threadpool(build_layout_docx, data, pages_text, name)
    except Exception as exc:
        print("layout docx export error:", repr(exc))
        return JSONResponse({"error": "export failed"}, status_code=500)
    return StreamingResponse(
        io.BytesIO(docx),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"),
        headers={"Content-Disposition": f'attachment; filename="{name}.docx"'},
    )


class TextIn(BaseModel):
    text: str


class StructureIn(BaseModel):
    text: str
    # Empty, "generic_document", or an unknown id all mean the free-form
    # structurer — an unclassified document must process exactly as before.
    template_id: str = ""


@app.post("/proofread")
async def proofread_endpoint(body: TextIn):
    """Proofread the OCR text with ALLaM, passed through the freeze-guard so
    high-value tokens can't be altered. Returns {"allam": {...}}."""
    text = (body.text or "")[:PROOFREAD_MAX_CHARS]
    if not text.strip():
        return JSONResponse({"error": "no text to proofread"}, status_code=400)
    try:
        await run_in_threadpool(ensure_proof)     # lazy-load ALLaM
    except Exception:
        return JSONResponse({"error": "proofreading model is not ready"},
                            status_code=503)
    try:
        result = await run_in_threadpool(proofread_run, text)
    except Exception as exc:
        print("proofread error:", repr(exc))
        return JSONResponse({"error": "proofreading failed"}, status_code=500)
    finally:
        await run_in_threadpool(stop_proof)       # free ALLaM's VRAM again
    return JSONResponse(result)


@app.post("/classify")
async def classify_endpoint(body: TextIn):
    """Route the document to one registry template before it is structured."""
    text = body.text or ""
    if not text.strip():
        return JSONResponse({"error": "no text to classify"}, status_code=400)
    try:
        reg = get_registry()
    except RegistryError as exc:
        print("registry error:", repr(exc))
        return JSONResponse({"error": "template registry is unavailable"},
                            status_code=503)
    try:
        await run_in_threadpool(ensure_llm)
    except Exception:
        pass          # classify() falls back to the rules tier on its own
    try:
        verdict = await run_in_threadpool(classify_document, text[:CLASSIFY_MAX_CHARS], reg)
    except Exception as exc:
        print("classify error:", repr(exc))
        return JSONResponse({"error": "classification failed"}, status_code=500)
    return JSONResponse(verdict.to_dict())


@app.post("/structure")
async def structure_endpoint(body: StructureIn):
    text = body.text or ""
    if not text.strip():
        return JSONResponse({"error": "no text to structure"}, status_code=400)

    reg = template = None
    template_id = (body.template_id or "").strip()
    if template_id:
        try:
            reg = get_registry()
            template = reg.get(template_id)
        except Exception as exc:
            # An unknown or unusable id is not an error the user should see:
            # fall through to the free-form structurer, which is what an
            # unclassified document gets anyway.
            print("structure: unusable template_id", repr(template_id), repr(exc))
            reg = template = None

    try:
        if template is not None:
            result = await run_in_threadpool(
                parse_structure_for_template, text, template, reg)
        else:
            result = await run_in_threadpool(parse_structure, text)
    except Exception as exc:
        print("structure error:", repr(exc))
        return JSONResponse({"error": "structuring failed"}, status_code=500)
    return JSONResponse(result.to_dict())


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatContext(BaseModel):
    document_type: str = ""
    sections: list = Field(default_factory=list)
    full_text: str = ""
    template_id: str = ""
    missing_required: list = Field(default_factory=list)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    context: ChatContext = Field(default_factory=ChatContext)


def _cap_field(f: dict) -> dict:
    d = {"label": str(f.get("label", ""))[:200], "value": str(f.get("value", ""))[:400]}
    # Provenance travels to chat as page/line only: enough for the model to
    # cite [صN سM], and nothing a client could inflate the prompt with.
    s = f.get("source")
    if isinstance(s, dict):
        try:
            page, line = int(s.get("page", 0)), int(s.get("line", 0))
        except (TypeError, ValueError):
            page = line = 0
        if 0 < page < 100000 and 0 < line < 1000000:
            d["source"] = {"page": page, "line": line}
    return d


def _cap_sections(sections: list) -> list:
    """Bound each field's size so the grounding block can't blow the context.
    Repeatable groups (heirs, witnesses) travel as `records`: rows of fields."""
    out: list = []
    for s in (sections or [])[:300]:
        if not isinstance(s, dict):
            continue
        fields = [_cap_field(f) for f in (s.get("fields") or [])[:100]
                  if isinstance(f, dict)]
        entry = {"title": str(s.get("title", ""))[:200], "fields": fields}
        rows = [[_cap_field(f) for f in row[:40] if isinstance(f, dict)]
                for row in (s.get("records") or [])[:200] if isinstance(row, list)]
        if rows:
            entry["records"] = rows
            entry["record_label"] = str(s.get("record_label", ""))[:100]
        out.append(entry)
    return out


def _cap_missing(missing: list) -> list:
    """The registry-declared fields the document did not carry. Chat is told
    about them so it reports the gap instead of inventing a value."""
    out: list = []
    for m in (missing or [])[:40]:
        if isinstance(m, dict):
            out.append({"key": str(m.get("key", ""))[:64],
                        "label_ar": str(m.get("label_ar", ""))[:120],
                        "label_en": str(m.get("label_en", ""))[:120]})
    return out


@app.post("/chat")
async def chat_endpoint(body: ChatIn):
    # Only user/assistant turns; the server builds the sole system message, so a
    # client-supplied system role can never shadow the DATA guard.
    history = [{"role": m.role, "content": (m.content or "")[:CHAT_MSG_MAX]}
               for m in body.messages
               if m.role in ("user", "assistant") and isinstance(m.content, str)]
    history = history[-CHAT_HISTORY_MAX:]
    if not history or history[-1]["role"] != "user":
        return JSONResponse({"error": "the last message must be from the user"},
                            status_code=400)
    sections = body.context.sections if isinstance(body.context.sections, list) else []
    context = {
        "document_type": (body.context.document_type or "")[:200],
        "sections": _cap_sections(sections),
        "full_text": (body.context.full_text or "")[:CHAT_FULLTEXT_MAX],
        "template_id": (body.context.template_id or "")[:64],
        "missing_required": _cap_missing(body.context.missing_required),
    }
    try:
        await run_in_threadpool(ensure_llm)
    except Exception:
        return JSONResponse({"error": "assistant model is not ready"}, status_code=503)
    if llm_status().get("status") != "ready":
        return JSONResponse({"error": "assistant model is not ready"}, status_code=503)

    def gen():
        try:
            for ev in chat_stream_events(context, history):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception:
            yield json.dumps({"error": "chat failed"}) + "\n"

    return StreamingResponse(
        gen(), media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


# The single-page UI lives in ui.html beside this file: four RTL panels laid out
# as in deeds_ui_wireframe.drawio — تحويل مستند → التدقيق اللغوي → تحليل المستند
# → شات و Q&A. Read once at import so a missing page fails at boot, not on the
# first request; restart the server after editing it (run.ps1 has no --reload).
PAGE = Path(__file__).with_name("ui.html").read_text(encoding="utf-8")
