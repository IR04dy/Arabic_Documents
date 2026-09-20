# Architecture

Internal design of the Arabic PDF Pipeline. For the HTTP contract see
[`API.md`](API.md); for setup/run see [`README.md`](README.md).

## 1. Overview

A single FastAPI process (`app.py`, port **8100**) serves a four-panel RTL web UI
(`ui.html`: تحويل مستند → التدقيق اللغوي → تحليل المستند → شات و Q&A, after
`deeds_ui_wireframe.drawio`) and a JSON/stream API. It orchestrates three GPU models through a four-stage pipeline.
All processing is local; documents never leave the host.

```
                         FastAPI app  (app.py, :8100)
                         ├─ UI (/) + JSON API + NDJSON stream
                         │
  PDF ──/extract──► Surya 2 OCR ──► full_text, pages[]
                         │                    │
                         │        ┌───────────┼───────────────┐
                         │        ▼           ▼               ▼
                         │   /structure   /chat          /export/{docx,layout-docx}
                         │   Qwen3        Qwen3           python-docx (+ Surya layout)
                         │        ▲
                         └─/proofread─► ALLaM (reading aid; not fed downstream)
```

## 2. Runtime topology

The vendored llama.cpp CUDA `llama-server.exe` (in `vendor/llama-cuda/`) is the
inference runtime for **all three** models — spawned as separate processes:

| Process | Port | Model | Lifetime | Purpose |
|---|---|---|---|---|
| FastAPI (uvicorn) | 8100 | — | foreground | API + UI + orchestration |
| **STRUCT** llama-server | 8123 | Qwen3-4B-Instruct-2507 (Q8) | resident | `/structure` + `/chat` |
| **PROOF** llama-server | 8124 | ALLaM-7B-Instruct (Q4_K_M) | **lazy** (per `/proofread`, freed after) | `/proofread` |
| **Surya OCR** llama-server | ephemeral | `surya-2.gguf` + mmproj | resident after first `/extract` | OCR foundation VLM |

- STRUCT & PROOF are owned by `llm.py` (a `Server` class; shared `--api-key`,
  `--parallel 1`, model-name check so a stale server isn't reused).
- The Surya OCR server is spawned by the `surya` library's **llama.cpp backend**;
  `extract.py` reaps it on shutdown (its own atexit cleanup fails on Windows).
- Startup (`app.py` `_warmup`) loads the OCR engine + Qwen3 in background threads
  so the port is reachable immediately; `GET /health` reports readiness.

## 3. VRAM budget & lifecycle (16 GB target)

The card can't hold all three models plus inference activations at once, so ALLaM
is lazy:

| State | Resident | ~VRAM |
|---|---|---|
| Idle / OCR / structure / chat | Surya OCR (~3.4 GB) + Qwen3 (~4.8 GB) | **~9 GB** (≈7 GB free) |
| During `/proofread` | + ALLaM (~5 GB) | ~14 GB (fits the headroom) |

`/proofread` calls `ensure_proof()` (ALLaM loads in ~3 s, GGUF stays in OS cache)
and `stop_proof()` in a `finally` to free it again. See
[[blackwell-llamacpp-gpu]] for the GPU/driver specifics.

## 4. Stage internals

### OCR — `extract.py` (Surya 2)
- Input is a **PDF** (each page rendered with **pypdfium2** at `SURYA_OCR_DPI`,
  300) or a **single raster image** (PNG/JPEG/WEBP/TIFF/BMP, loaded with Pillow as
  one page). `detect_kind()` classifies the upload by magic bytes (extension
  fallback) and is shared by the API validation and both the OCR and layout paths.
- `RecognitionPredictor(SuryaInferenceManager())([img], full_page=True)` →
  per-page `blocks[]`, each with a layout `label`, `reading_order`, and `html`.
  Blocks sorted by reading order; `Picture`/`Figure` + skipped/errored dropped;
  HTML→text via `_block_text` (tables become pipe-`|` rows).
- **Docker-free GPU path:** Surya's foundation VLM runs on the bundled llama.cpp
  server (`SURYA_INFERENCE_BACKEND=llamacpp`, `LLAMA_CPP_BINARY`, `-ngl 99`), with
  KV pinned small (`SURYA_INFERENCE_PARALLEL=1`, `SURYA_INFERENCE_CTX_SIZE=16384`)
  so it co-exists with Qwen/ALLaM. Surya's default vLLM/Docker backend is not used.
- `shutdown_server()` matches the spawned process by `*surya-2.gguf*` and kills it
  precisely (never the Qwen/ALLaM servers).

### Proofread — `proofread.py` + `guard.py`
- ALLaM corrects Arabic spelling/grammar page-by-page. The **freeze-guard**
  (`guard.py`) accepts a page's correction only if every high-value token (digit
  runs incl. Arabic-Indic, dates, emails, IBANs) survives byte-for-byte; otherwise
  the raw OCR page is kept. **Output is a reading aid — not fed to stages 3/4.**

### Structuring — `structure.py`
- Qwen3 with a **JSON-schema-constrained** grammar → `{document_type, sections[]}`
  with `{label, value}` fields; the model can't emit malformed JSON or off-schema
  keys, and never sees the image. Fed the **raw OCR** (input capped at 12 000 chars).

### Chat — `chat.py`
- System prompt = structured fields + DATA-fenced document text + an injection
  guard. History windowed to fit context; the latest user question is never
  dropped. Streamed as NDJSON; model output is rendered via `textContent` only
  (XSS-safe) in the UI.

### Word export — `docx_export.py` / `layout_docx.py`
- Both build `.docx` with **python-docx** (MIT). Plain export = one paragraph per
  line; formatted export re-renders the page (pypdfium2, `LAYOUT_DPI`=200) and
  runs Surya `FastLayoutPredictor` (on **CPU** — `FAST_DETECTOR_DEVICE=cpu`) to
  sample the real title/heading colours, then pours text in by shape
  classification with 2-column RTL tables.
- **RTL alignment:** Arabic paragraphs use `w:bidi` and **omit `w:jc`** (default
  leading edge = right); titles use `w:jc="center"`. Setting `w:jc="right"` on a
  bidi paragraph resolves to the *trailing* (left) edge — the bug that made Arabic
  render left-aligned.

## 5. Security posture

- **The FastAPI app (8100) has no auth** and binds to `127.0.0.1`. Do not expose
  it beyond localhost without a reverse proxy + authentication.
- Internal llama.cpp servers bind to localhost with a generated `--api-key`.
- **CSP** + `X-Content-Type-Options: nosniff` on every response (`app.py`
  middleware). Chat is protected by a DATA fence + injection guard; the server
  builds the sole system message, so a client `system` role can't shadow it.
- Uploads capped (100 MB); export bodies capped (16 MB); text inputs capped per
  stage. Filenames sanitised to ASCII (strips NUL/CRLF/path).

## 6. Configuration (environment variables)

| Var | Default | Effect |
|---|---|---|
| `STRUCTURE_MODEL_PATH` | bundled Qwen3-4B GGUF | Structurer/chat model path |
| `STRUCTURE_PORT` | `8123` | STRUCT server port |
| `STRUCTURE_N_CTX` | `8192` | Structurer context window |
| `STRUCTURE_GPU_LAYERS` | `99` | GPU layers (`0` = CPU, ~30 s/page) |
| `STRUCTURE_MAX_INPUT_CHARS` | `12000` | `/structure` input cap |
| `STRUCTURE_MAX_NEW_TOKENS` | (llm.py) | Structurer output cap |
| `STRUCTURE_STARTUP_TIMEOUT` | `180` | Seconds to wait for a server to come up |
| `STRUCTURE_HOST` | `127.0.0.1` | llama-server bind host |
| `PROOF_MODEL_PATH` | bundled ALLaM GGUF | Proofreader model path |
| `PROOF_PORT` | `8124` | PROOF server port |
| `PROOF_N_CTX` | `4096` | Proofreader context window |
| `SURYA_OCR_DPI` | `300` | OCR render DPI |
| `LAYOUT_DPI` | `200` | Formatted-export render DPI |
| `SURYA_INFERENCE_BACKEND` | `llamacpp` | Surya inference backend (do not change) |
| `SURYA_INFERENCE_PARALLEL` | `1` | Surya server slots (keep 1 for VRAM) |
| `SURYA_INFERENCE_CTX_SIZE` | `16384` | Surya server context |
| `LLAMA_CPP_BINARY` | vendored `llama-server.exe` | Binary Surya spawns |
| `LLAMA_CPP_NGL` | `99` | Surya server GPU layers |
| `FAST_DETECTOR_DEVICE` | `cpu` (set by `layout_docx`) | Keeps the layout model off the GPU |
| `HF_HOME` | `D:\Yousef\hf-cache` (host profile) | Hugging Face cache root |
| `HF_HUB_OFFLINE` | unset | Set `1` to skip HF network calls (cached models only) |

## 7. Licensing

Qwen3 = Apache 2.0 · pypdfium2 = Apache/BSD · llama.cpp = MIT · python-docx = MIT.
The `surya-ocr` **package** reports Apache-2.0, but Surya's **model weights** may
carry additional (revenue-gated) commercial terms — verify the
[surya-ocr-2 model card](https://huggingface.co/datalab-to/surya-ocr-2) before
commercial deployment. No Tesseract, Poppler, or PyMuPDF (AGPL).

## 8. History

The OCR engine was **Qari-OCR** (LoRA on Qwen2-VL-2B, via transformers/PEFT) until
2026-09-14, when it was replaced by Surya 2 and the Qari code path was removed
(the toggle and `extract_surya.py` are gone; `extract.py` is now the sole Surya
engine). Qari model files remain in the HF cache but are unused. This folder is
**not** under git — history lives in the maintainer's notes.
