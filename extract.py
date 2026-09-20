# -*- coding: utf-8 -*-
"""OCR with Surya 2 — full-page OCR + layout (Arabic + English).

Docker-free GPU path: Surya 2's foundation VLM runs via the **llama.cpp backend**
(`SURYA_INFERENCE_BACKEND=llamacpp`) using the app's bundled CUDA `llama-server`,
so no vLLM/Docker is needed. `full_page=True` returns OCR text AND layout (each
block carries a layout label + reading order) in one VLM call per page.

Produces an `ExtractResult` (per-page `text` + a joined `full_text` with
`--- Page N ---` markers) that the proofread / structure / chat stages consume.
The VLM weights are the `surya-ocr-2` GGUF, loaded inside the spawned llama-server
(not into this process), and the server is reaped on shutdown via
`shutdown_server()` because Surya's own atexit cleanup fails on Windows.
"""
from __future__ import annotations

import html as html_mod
import io
import os
import re
import threading
from dataclasses import asdict, dataclass

# Configure Surya's llama.cpp backend BEFORE surya is imported (surya reads these
# from the environment). Point it at the app's CUDA llama-server, run all layers
# on the GPU, and keep the KV cache small (single slot) so Qwen/ALLaM still fit
# alongside it on the 16 GB card.
_VENDOR_LLAMA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "vendor", "llama-cuda", "llama-server.exe")
os.environ.setdefault("SURYA_INFERENCE_BACKEND", "llamacpp")
if os.path.isfile(_VENDOR_LLAMA):
    os.environ.setdefault("LLAMA_CPP_BINARY", _VENDOR_LLAMA)
os.environ.setdefault("LLAMA_CPP_NGL", "99")            # all layers on GPU
os.environ.setdefault("SURYA_INFERENCE_PARALLEL", "1")  # serialize; small KV cache
os.environ.setdefault("SURYA_INFERENCE_CTX_SIZE", "16384")

import pypdfium2 as pdfium
from PIL import Image

RENDER_DPI = int(os.environ.get("SURYA_OCR_DPI", "300"))
MODEL = "datalab-to/surya-ocr-2 (GGUF · llama.cpp)"

# Layout labels whose content we drop from the plain-text transcription.
_SKIP_LABELS = {"Picture", "Figure"}

# Accepted inputs: a PDF (rendered page-by-page) or a single raster image
# (OCR'd directly as one page). Pillow decodes all of these.
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp")


def detect_kind(data: bytes, filename: str = "") -> str | None:
    """Classify an upload as 'pdf', 'image', or None (unsupported) by magic bytes
    (with a filename-extension fallback). Shared by the API validation and the
    OCR/layout paths so they agree on what's accepted."""
    name = (filename or "").lower()
    h = data[:16]
    if h[:5] == b"%PDF-" or name.endswith(".pdf"):
        return "pdf"
    if (h[:8] == b"\x89PNG\r\n\x1a\n"                 # PNG
            or h[:3] == b"\xff\xd8\xff"               # JPEG
            or h[:4] in (b"II*\x00", b"MM\x00*")      # TIFF (LE / BE)
            or h[:2] == b"BM"                         # BMP
            or (h[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WEBP
            or name.endswith(_IMAGE_EXT)):
        return "image"
    return None

_load_lock = threading.Lock()
_infer_lock = threading.Lock()
_state: dict = {"rec": None, "status": "not_loaded", "error": None, "device": None}
# Live progress, polled by the UI.
_progress: dict = {"active": False, "page": 0, "total": 0, "filename": ""}


def progress() -> dict:
    return dict(_progress)


@dataclass
class PageResult:
    page: int
    text: str          # OCR text
    chars: int


@dataclass
class ExtractResult:
    filename: str
    page_count: int
    pages: list
    full_text: str          # OCR (joined)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pages"] = [asdict(p) for p in self.pages]
        return d


def _render(page) -> "Image.Image":
    return page.render(scale=RENDER_DPI / 72).to_pil().convert("RGB")


def _block_text(html: str) -> str:
    """Block HTML -> plain text. Tables become pipe-delimited rows (matching the
    structure stage's TABLE convention); other blocks keep their line breaks."""
    h = html or ""
    if "<td" in h.lower() or "<table" in h.lower():
        h = re.sub(r"</t[dh]>\s*", " | ", h, flags=re.I)
        h = re.sub(r"</tr>\s*", "\n", h, flags=re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</(p|div|h[1-6]|li)>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    h = html_mod.unescape(h)
    out = []
    for ln in h.split("\n"):
        ln = re.sub(r"\s*\|\s*", " | ", ln.strip())   # normalise cell separators
        ln = re.sub(r"^\|\s*|\s*\|$", "", ln).strip()  # trim leading/trailing pipes
        if ln:
            out.append(ln)
    return "\n".join(out)


def ensure_loaded():
    """Build the Surya recognition predictor once (lazy). The heavy VLM itself
    loads in the spawned llama-server on first inference, not here."""
    if _state["rec"] is not None:
        return _state["rec"]
    with _load_lock:
        if _state["rec"] is None and _state["status"] != "error":
            try:
                _state["status"] = "loading"
                from surya.inference import SuryaInferenceManager
                from surya.recognition import RecognitionPredictor
                _state.update(rec=RecognitionPredictor(SuryaInferenceManager()),
                              status="ready", device="cuda (llama.cpp)")
            except Exception as exc:
                _state.update(status="error", error=f"{type(exc).__name__}: {exc}")
                raise
    return _state["rec"]


def status() -> dict:
    return {"status": _state["status"], "error": _state["error"],
            "device": _state["device"], "model": MODEL, "gpu": True,
            "engine": "surya"}


def _page_text(rec, img) -> str:
    """OCR one page image with Surya and flatten its blocks to plain text."""
    result = rec([img], full_page=True)[0]
    parts = []
    for b in sorted(result.blocks, key=lambda b: b.reading_order):
        if b.skipped or b.error or b.label in _SKIP_LABELS:
            continue
        t = _block_text(b.html)
        if t:
            parts.append(t)
    return "\n".join(parts)


def extract_document(data: bytes, filename: str = "document") -> ExtractResult:
    """Full-page OCR of a PDF (each page) or a single raster image (one page) with
    Surya; assemble the same shape the pipeline expects (per-page text + a joined
    full_text with page markers)."""
    rec = ensure_loaded()
    is_pdf = detect_kind(data, filename) == "pdf"
    pages: list[PageResult] = []
    doc = None
    try:
        if is_pdf:
            doc = pdfium.PdfDocument(data)
            total = len(doc)
            get_image = lambda i: _render(doc[i])
        else:
            img = Image.open(io.BytesIO(data)).convert("RGB")  # a single-image page
            total = 1
            get_image = lambda i: img
        with _infer_lock:                       # one server, serialize pages
            _progress.update(active=True, page=0, total=total, filename=filename)
            for index in range(total):
                _progress.update(page=index + 1)
                text = _page_text(rec, get_image(index))
                pages.append(PageResult(index + 1, text, len(text)))
    finally:
        _progress["active"] = False
        if doc is not None:
            doc.close()
    full_text = "\n\n".join(
        f"--- Page {p.page} ---\n{p.text}" for p in pages if p.text)
    return ExtractResult(filename, len(pages), pages, full_text)


# Backwards-compatible alias (the entry point used to be named extract_pdf).
extract_pdf = extract_document


def shutdown_server() -> None:
    """Reap the persistent Surya llama.cpp OCR server. Its own atexit cleanup
    fails on Windows (WinError 87), so match its command line by the model file
    (`surya-2.gguf`) — precise, so it never touches the app's Qwen/ALLaM servers."""
    if _state["rec"] is None:
        return
    try:
        import subprocess
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
             "'*surya-2.gguf*' } | ForEach-Object { Stop-Process -Id $_.ProcessId "
             "-Force -ErrorAction SilentlyContinue }"],
            timeout=15, capture_output=True)
    except Exception:
        pass
