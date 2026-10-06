# Architecture

Internal design of the Arabic PDF Pipeline. For the HTTP contract see
[`API.md`](API.md); for setup/run see [`README.md`](README.md).

## 1. Overview

A single FastAPI process (`app.py`, port **8100**) serves a four-panel RTL web UI
(`ui.html`: تحويل مستند → التدقيق اللغوي → تحليل المستند → شات و Q&A, after
`deeds_ui_wireframe.drawio`) and a JSON/stream API. It orchestrates three GPU models through a four-stage pipeline.
All processing is local; documents never leave the host. Two more workspace
tabs share the page and the models: document comparison (`comparison*.py`) and
complaint management (§5; [`COMPLAINTS.md`](COMPLAINTS.md)).

```
                         FastAPI app  (app.py, :8100)
                         ├─ UI (/) + JSON API + NDJSON stream
                         │
  PDF ──/extract──► Surya 2 OCR ──► full_text, pages[] (text + layout)
                         │                    │
                         │        ┌───────────┼───────────────┐
                         │        ▼           ▼               ▼
                         │   /structure   /chat          /export/{docx,layout-docx}
                         │   Qwen3        Qwen3           python-docx; the page image
                         │                                measured against the layout
                         │        ▲
                         └─/proofread─► ALLaM (reading aid; not fed downstream)
```

## 2. Runtime topology

The vendored llama.cpp CUDA `llama-server.exe` (in `vendor/llama-cuda/`) is the
inference runtime for **all three** models — spawned as separate processes:

| Process | Port | Model | Lifetime | Purpose |
|---|---|---|---|---|
| FastAPI (uvicorn) | 8100 | — | foreground | API + UI + orchestration |
| **STRUCT** llama-server | 8123 | Qwen3-4B-Instruct-2507 (Q8) | resident | `/structure` + `/chat` (+ `/classify`, comparison, the CMS `qwen` provider) |
| **PROOF** llama-server | 8124 | ALLaM-7B-Instruct (Q4_K_M) | **lazy** (per `/proofread` or CMS batch, freed after) | `/proofread` + the CMS `allam` provider |
| **Surya OCR** llama-server | ephemeral | `surya-2.gguf` + mmproj | resident after first `/extract` | OCR foundation VLM |

- STRUCT & PROOF are owned by `llm.py` (a `Server` class; shared `--api-key`,
  `--parallel 1`, model-name check so a stale server isn't reused).
- The Surya OCR server is spawned by the `surya` library's **llama.cpp backend**;
  `extract.py` reaps it on shutdown (its own atexit cleanup fails on Windows).
- Startup (`app.py` `_warmup`) loads the OCR engine + Qwen3 in background threads
  so the port is reachable immediately; `GET /health` reports readiness.
- **One outbound dependency.** The verify-data tab (`wathq_api.py`) calls
  `https://api.wathq.sa` when, and only when, the user presses its lookup button.
  It is the app's only internet traffic. Everything above stays on this machine.
  See `WATHQ_INTEGRATION.md`.

## 3. VRAM budget & lifecycle (16 GB target)

The card can't hold all three models plus inference activations at once, so ALLaM
is lazy:

| State | Resident | ~VRAM |
|---|---|---|
| Idle / OCR / structure / chat | Surya OCR (~3.4 GB) + Qwen3 (~4.8 GB) | **~9 GB** (≈7 GB free) |
| During `/proofread` | + ALLaM (~5 GB) | ~14 GB (fits the headroom) |
| During a CMS batch on the `allam` provider | + ALLaM (~5 GB), the same instance | ~14 GB |

`/proofread` holds ALLaM's lease (`complaints_llm.server_lease(llm.PROOF)`),
calls `ensure_proof()` (ALLaM loads in ~3 s, GGUF stays in OS cache) and, in a
shielded `finally`, releases the lease asking for a stop. The server stops only
once no holder is left, so a proofread and a complaints batch never stop ALLaM
under each other (§5). See [[blackwell-llamacpp-gpu]] for the GPU/driver
specifics.

## 4. Stage internals

### OCR — `extract.py` (Surya 2)
- Input is a **PDF** (each page rendered with **pypdfium2** at `SURYA_OCR_DPI`,
  300) or a **single raster image** (PNG/JPEG/WEBP/TIFF/BMP, loaded with Pillow as
  one page). `detect_kind()` classifies the upload by magic bytes (extension
  fallback) and is shared by the API validation and both the OCR and layout paths.
- `RecognitionPredictor(SuryaInferenceManager())([img], full_page=True)` →
  per-page `blocks[]`, each with a layout `label`, `reading_order`, `bbox`
  (input-image pixels) and `html`. Blocks sorted by reading order; errored
  ones dropped; `Picture`/`Figure` and skipped blocks give no text;
  HTML→text via `_block_text` (tables become ` | `-separated rows).
- Each page also keeps its **layout**: `{"blocks": [{label, bbox, lines}]}` —
  the box as fractions of the page and the lines that block contributed to the
  page text (joining them gives `text` back exactly). `Picture`/`Figure`/
  `Diagram` blocks are kept with no lines, so the Word export can copy them. A
  text block without a usable box voids the page's layout (`null`): a layout
  that does not account for every line is not trusted.
- **Docker-free GPU path:** Surya's foundation VLM runs on the bundled llama.cpp
  server (`SURYA_INFERENCE_BACKEND=llamacpp`, `LLAMA_CPP_BINARY`, `-ngl 99`), with
  KV pinned small (`SURYA_INFERENCE_PARALLEL=1`, `SURYA_INFERENCE_CTX_SIZE=16384`)
  so it co-exists with Qwen/ALLaM. Surya's default vLLM/Docker backend is not used.
- `shutdown_server()` matches the spawned process by `*surya-2.gguf*` and kills it
  precisely (never the Qwen/ALLaM servers).
- **PDFium is not thread-safe.** `PDFIUM_LOCK` (an `RLock`) is held around every
  PDFium call, and only for that call: opening a PDF with its page count, each
  page's access, render and close, and the document's close. `layout_docx._Source`
  (importing it lazily) and the complaints worker's page count take the same lock.
  It may be taken while holding `_infer_lock`, never the other way round.
- `extract_document(data, filename, progress=True)`: `progress=False` leaves the
  live `/progress` record alone. The complaints worker passes it, so its pages
  never show as the analysis tab's document.

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
- Both build `.docx` with **python-docx** (MIT). The plain export
  (`docx_export.py`) is one paragraph per line.
- The **formatted export** (`layout_docx.py`) rebuilds the page. The UI sends
  the original file, each page's text *after* the reader's proofreading
  decisions, and each page's `layout` from `/extract`. The page is rendered
  once at `LAYOUT_DPI` (200) with pypdfium2 and everything is measured from it
  with numpy — no model runs:

  | What | How |
  |---|---|
  | Text ↔ blocks | `difflib` alignment of the export lines to the OCR lines, so proofread lines keep their place |
  | Visual lines | row-ink runs inside each block's box; dots and diacritics folded into their line |
  | Kind | the block's Surya label: `SectionHeader` → heading (outline level 1), `Table` → table, `Picture`/`Figure`/`Diagram` → cropped image; a line naming the template's title → outline level 0 |
  | Font size | the ascent — baseline (where the long joining strokes are) to the top of the tall letters — over `ASCENT_RATIO` (Arial-calibrated); and the line's printed width against the same text set in Arial by Pillow (needs libraqm). The smaller wins, then sizes are snapped document-wide |
  | Bold | stroke thickness (2 × ink area / outline) over the size, against `BOLD_STROKE` |
  | Colour | the darkest 30% of the text's pixels (lightest, for light text on a fill) |
  | Fills | solid ink over a 10 px square (an opening); inside one, text is what differs from the fill — white on green, black on orange — and the fill becomes paragraph or cell shading |
  | Shading, bars | the paper behind a line (`w:shd`, widened with same-colour borders), a thin vertical rule at a heading's edge (`w:pBdr`) |
  | Rules | thin long runs between blocks → an empty paragraph with a bottom border |
  | Columns | a column gap inside a line splits it into cells (registry labels first, else by letters-per-width); side-by-side blocks become one borderless row; a ruled table takes its columns from its own vertical rules and its rows from its horizontal ones |
  | Alignment | where the ink sits between the margins: right, left, centre or justified |
  | Spacing | every item's baseline placed where the page had it, using Word's line box (baseline 0.93 em below its top, 0.22 em above its bottom); wrapped paragraphs and cells keep the page's line pitch |

  A page with no layout (an API caller that does not send one) takes the
  **text-only path**: lines classified by shape, forms rebuilt from the
  registry's labels, heading colours sampled once by Surya `FastLayoutPredictor`
  on the **CPU** (`FAST_DETECTOR_DEVICE=cpu`). It cannot align, size or place
  pictures.
- **Size sanity:** a line that did not wrap on the page is never set larger than
  fits its Word column; cells in one table column share a size; headings of one
  level share a size.
- **RTL alignment:** Arabic paragraphs use `w:bidi` and **omit `w:jc`** (default
  leading edge = right); a left-hugging Arabic line writes `w:jc="right"` (in a
  bidi paragraph that is the *trailing*, left, edge — the bug that once made all
  Arabic render left-aligned). Paragraph borders are physical sides.
- **Tests:** `test_layout_docx.py` runs both paths on the three sample documents
  (a digital Najiz deed, a ruled vector form, a scanned deed with filled bands).

## 5. Complaint management (CMS)

The **إدارة الشكاوى** tab is a complaints desk for إمارة منطقة الرياض. It
reads incoming complaints, cites their facts, classifies them, picks the
ministry to refer each to and a priority, and keeps a register. What it does
for its users (the pipeline, the priority model, review, evaluation) is in
[`COMPLAINTS.md`](COMPLAINTS.md); the HTTP contract is in `API.md`. This
section is how it is built.

### Module map

| Module | Role |
|---|---|
| `complaints_taxonomy.py` + `templates/complaints_taxonomy.yaml` | The closed vocabularies (entity, priorities, ministries, categories, regions, governorates, statuses, signals, review reasons). A strict loader, and the place and city matching (Arabic and Latin-script names). Every id becomes a schema enum, a filter value and a column value. |
| `complaints_llm.py` | `Provider` (`qwen`, `allam`, `openai`), `ProviderRegistry` (the active provider) and `ServerLease` (shared use of a lazily loaded llama-server). |
| `complaints.py` | The pure pipeline over `(text, provider, taxonomy)`: `structure_complaint` and `classify_complaint` (one constrained LLM call each), the rules tier, `analyze`, `acknowledgment` and `insights`. No database, no HTTP. |
| `complaints_store.py` | The SQLite register: rows, files, queue claims, feedback, events, analytics. |
| `complaints_api.py` | `ComplaintService` (the worker) and the `/complaints` router. |
| `complaints_ui.js`, `complaints.css` | The tab. It builds the DOM with `createElement`/`textContent` only. |

`app.py` only includes the router, stops the service first on shutdown, and
shares ALLaM through the lease in `/proofread`.

```
 browser ─/complaints/upload|text─► router ─► Store.create (row queued, file saved) ─► notify()
                                                                                         │
            complaints-worker thread (one complaint at a time) ◄────────────────────────┘
              Store.claim_next
              ├─ OCR: extract.extract_document (Surya), only when there is no text yet
              ├─ complaints.analyze(text, registry.active(), taxonomy, precedents, repeat lookup)
              │     step 1 structure (LLM) ─► step 2 classify (LLM) ─► rules tier
              └─ Store.save_analysis (stage done, due_at) or Store.fail (stage error)
 browser ◄─/complaints/queue (polled every 2 s)─ Store.queue_state
```

### The worker thread

- **One daemon thread, `complaints-worker`.** A second worker would only wait
  behind the first on the single model slots and crowd `/structure` and
  `/chat`.
- **It starts with the app, not at import.** The router's startup handler
  boots it, so `import app` never begins processing. It boots only if this
  process takes the store's owner lock (`complaints.db.lock` beside the
  database). A second app started on the same data directory serves the
  register but leaves the queue alone.
- **Boot requeues what a previous process left half-done.** A complaint
  interrupted three times in a row is failed instead, so one that keeps
  bringing the app down is not retried forever.
- **Waking.** An intake calls `notify()`; otherwise the worker polls every
  30 s. Each pass claims the oldest queued complaint and records every stage
  change as an event: `queued → ocr → structuring → classifying → done |
  error`.
- **OCR runs only when there is no text yet** (a PDF or image never
  extracted) or a re-extraction was asked for. The PDF page count is checked
  against `CMS_MAX_PAGES` first.
- **A busy model** raises `ProviderBusy` after llm's own 150 s wait for the
  slot. The worker retries 3 times, 5 s apart, then fails the complaint. An
  unavailable ALLaM (stopped under the request) gets one reload.
- **A failure the database refuses** (locked, full): the worker lets go of the
  claim, so delete and reprocess still work, and retries the failure on every
  pass.
- **When the queue drains**, the worker releases providers marked
  `release_after_batch` (ALLaM) and sweeps orphaned files.
- **Delete and reprocess answer `409`** while this process's worker holds the
  complaint, or while another process owns the queue and the complaint is in
  a processing stage.
- **Shutdown.** `app.py` stops the service before the model servers, joining
  for 5 s. An item in flight is requeued as interrupted rather than failed. A
  model call in flight cannot be interrupted, but the thread is a daemon.

### Provider abstraction

`complaints.py` only calls `provider.chat_json(messages, schema, max_tokens,
temperature)` and reads `provider.n_ctx`.

- **Prompt budgets come from `n_ctx`.** The document gets roughly
  `(n_ctx − answer − fixed prompt)` tokens at 1.5 characters each, capped at
  12 000 characters. When the full catalogue would leave step 2 too little room
  for the complaint (always so at 4096 tokens), it switches to a compact one.
- **Retries.** An answer cut off by the token cap, or breaking the schema, or
  a rejected request, is retried once with 60 % of the document.
- **Every decision is a taxonomy id**, because the JSON schema's enums are
  built from the taxonomy.
- **Switching.** `ProviderRegistry.active()` is read per complaint, so a
  switch (`PUT /complaints/provider`) applies from the next one.
  `set_active()` releases the old provider when it is `release_after_batch`.
- **Adapters.**
  - `LlamaServerProvider` wraps an `llm.Server` (`qwen` → `llm.STRUCT`,
    `allam` → `llm.PROOF`) and maps its errors to `ProviderBusy`,
    `ProviderUnavailable` and `ProviderError`.
  - `OpenAICompatibleProvider` posts to `/v1/chat/completions` with a strict
    `json_schema` response format. It refuses a non-loopback host unless
    `CMS_LLM_ALLOW_REMOTE=1`, bypasses environment proxies, does not follow
    redirects, and never logs the key or the body.
- **Error messages are safe Arabic.** They reach API responses and a
  complaint's stored error.

### Data store

- **SQLite in WAL mode**, one connection behind an `RLock`, shared by the
  worker and the API threadpool. Writes are `BEGIN IMMEDIATE` transactions, so
  read-then-write steps (dedup in `create`, `claim_next`) are atomic.
  `secure_delete` is on, and a delete is followed by a WAL checkpoint.
- **Schema version 3**, kept in `PRAGMA user_version`. Migrations are
  idempotent `ALTER TABLE … ADD COLUMN`s: 1→2 added `governorate`,
  `model_governorate` and `review_reasons`; 2→3 added `reocr`. A database from
  newer code is refused, and the routes answer `503`.
- **Tables:** `complaints` (effective and `model_*` values, the text, the
  analysis JSON, stage, status, due date, timings), `feedback` (verdict,
  `{field: {from, to}}` changes, note, reviewer) and `events` (ids, stages and
  short notes, never document text). The last two cascade on delete.
- **Files** live at `files/<id>.<ext>`. They are written to a temp file and
  moved into place with `os.replace` just before the row commits. The file
  sweep removes stale temps and files whose row is gone.
- **Location:** `CMS_DATA_DIR`, default `<repo>/data/complaints` (git-ignored).
  All timestamps are ISO UTC; day buckets use Riyadh time (UTC+3).

### Sharing the models with the analysis tab

- **Qwen.** The `qwen` provider is `llm.STRUCT`, whose single slot
  (`--parallel 1` and `llm`'s inference lock) also serves `/structure`,
  `/chat`, `/classify` and comparison. Each complaint makes two calls (about
  6–7 s in all) and takes the slot per call, so interactive requests
  interleave with the queue rather than waiting for it to drain.
- **Surya.** A complaint's OCR calls `extract.extract_document(...,
  progress=False)`, which holds extract's inference lock for the whole
  document. So the analysis tab's `/extract` waits behind a complaint's whole
  PDF (up to `CMS_MAX_PAGES` pages). `progress=False` keeps the complaint's
  pages out of the analysis tab's `/progress`.
  - The worker's PDF page count holds `extract.PDFIUM_LOCK`, as every PDFium
    call in the OCR pass and in `layout_docx.py` does (§4).
- **ALLaM.** `/proofread` and the `allam` provider share one llama-server
  through `ServerLease`. Every user holds the lease while using the server,
  and the provider holds it from its first call until the batch ends. A stop
  asked for by either side happens when the last holder lets go, and runs
  under the lease's lock, so a new caller waits and then loads a fresh server
  instead of posting to a dying one. `llm.stop_proof()` bypasses the lease;
  nothing calls it any more, and new code must not.

**VRAM.** Qwen (~4.8 GB) and Surya (~3.4 GB) stay resident whichever provider
is chosen. ALLaM adds ~5 GB while a batch runs on it, about 14 GB in all: the
same headroom `/proofread` uses, and still one instance when both overlap. It
is stopped when the queue drains, when another provider is chosen, and after
an insights run. An OpenAI-compatible server on the same GPU (a local vLLM,
say) needs its own VRAM on top of the resident models.

## 6. Security posture

- **The FastAPI app (8100) has no auth** and binds to `127.0.0.1`. Do not expose
  it beyond localhost without a reverse proxy + authentication.
- **Host allowlist** (`TrustedHostMiddleware`, outermost): a request whose
  `Host` is not `127.0.0.1`, `localhost` or a name in `APP_ALLOWED_HOSTS` gets
  `400`. Binding to loopback is the app's only protection, and DNS rebinding
  would get around it otherwise. `[::1]` is refused.
- Internal llama.cpp servers bind to localhost with a generated `--api-key`.
- **CSP** + `X-Content-Type-Options: nosniff` on every response (`app.py`
  middleware). The CSP's `frame-ancestors 'self'` (with `X-Frame-Options:
  SAMEORIGIN`) stops other sites framing the app to steer its one-click
  actions; `connect-src 'self' blob:` lets the PDF previews load their own
  `blob:` URLs. Chat is protected by a DATA fence + injection guard; the server
  builds the sole system message, so a client `system` role can't shadow it.
- Uploads capped (100 MB); export bodies capped (16 MB); text inputs capped per
  stage. Filenames sanitised to ASCII (strips NUL/CRLF/path).
- **The complaints routes** (§5):
  - A state change sent from another site is refused (`Sec-Fetch-Site` /
    `Origin`).
  - Bodies are capped before parsing, uploads are typed by magic bytes, and ids
    are checked against the taxonomy before the store sees them.
  - Errors are Arabic messages with no document text or exception detail.
  - `/complaints` query strings (names, national IDs) are cut from the access
    log, and CSV cells that would run as spreadsheet formulas are defused.
  - Complaint text reaches the model only inside a DATA fence, with fence-like
    markers stripped; wording aimed at the model is flagged and cannot win the
    top priority on the model's word alone.
  - The register is personal data at rest under `CMS_DATA_DIR`, and it is not
    encrypted.

- **The verify-data routes** (`/wathq`, see `WATHQ_INTEGRATION.md`). They
  cover all 8 Wathq products through one catalog (`templates/wathq/catalog.yaml`,
  checked at boot against Wathq's specs in the same folder) and one generic
  view builder (`wathq_view.py`):
  - They answer only to a loopback peer addressing a loopback host, unless
    `WATHQ_ALLOW_REMOTE=1`. The `Host` header alone isn't trusted, because a
    LAN client can forge it.
  - A lookup must be same-origin and carry `X-Wathq-Request: 1`, so another
    site cannot spend the paid quota.
  - The Wathq key is read per call, sent only as Wathq's `apiKey` header (never
    on a redirect), and never logged or returned.
  - The outbound host is fixed, TLS is always verified, and environment proxies
    are ignored.
  - Personal data in Wathq's answers (identity, phone, e-mail, birth date) is
    masked before it leaves the server, from each query's list of personal
    fields plus a name-based backstop. A person's ID typed as an input is never
    echoed back or logged. Answers are cached in memory only.

## 7. Configuration (environment variables)

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
| `LAYOUT_DPI` | `200` | Formatted-export render DPI (stroke widths need ≥200) |
| `LAYOUT_GEOMETRY` | `1` | Formatted export rebuilds pages from their layout; `0` = every page takes the text-only path |
| `LAYOUT_PALETTE` | `1` | Text-only export path: sample heading colours with Surya's CPU layout model (`0` = skip) |
| `LAYOUT_ARIAL` / `LAYOUT_ARIAL_BOLD` | Windows/macOS/Linux font paths | Arial files for width-based sizing (also needs Pillow with libraqm) |
| `SURYA_INFERENCE_BACKEND` | `llamacpp` | Surya inference backend (do not change) |
| `SURYA_INFERENCE_PARALLEL` | `1` | Surya server slots (keep 1 for VRAM) |
| `SURYA_INFERENCE_CTX_SIZE` | `16384` | Surya server context |
| `LLAMA_CPP_BINARY` | vendored `llama-server.exe` | Binary Surya spawns |
| `LLAMA_CPP_NGL` | `99` | Surya server GPU layers |
| `FAST_DETECTOR_DEVICE` | `cpu` (set by `layout_docx`) | Keeps the text-only path's layout model off the GPU |
| `HF_HOME` | `D:\Yousef\hf-cache` (host profile) | Hugging Face cache root |
| `HF_HUB_OFFLINE` | unset | Set `1` to skip HF network calls (cached models only) |
| `APP_ALLOWED_HOSTS` | unset | Extra `Host` names the app accepts, comma-separated (e.g. a LAN name). `127.0.0.1` and `localhost` are always allowed. |
| `CMS_DATA_DIR` | `<repo>/data/complaints` | Complaints register (`complaints.db`) and uploaded files |
| `CMS_LLM_PROVIDER` | `qwen` | Complaints provider at startup: `qwen`, `allam` or `openai` (unknown → `qwen`) |
| `CMS_FEWSHOT` | `3` | Reviewer precedents in each classification prompt (`0` = none; at most 3 are used) |
| `CMS_MAX_PAGES` | `30` | A complaint PDF with more pages is refused before OCR (1–2000) |
| `CMS_OPENAI_BASE_URL` | unset | OpenAI-compatible endpoint for the `openai` provider (with `CMS_OPENAI_MODEL`); loopback only unless `CMS_LLM_ALLOW_REMOTE=1` |
| `CMS_OPENAI_MODEL` | unset | Model name sent to that endpoint |
| `CMS_OPENAI_API_KEY` | unset | Bearer key for that endpoint (never logged) |
| `CMS_OPENAI_N_CTX` | `8192` | That endpoint's context window (≥ 4096); prompts are sized from it |
| `CMS_OPENAI_LABEL` | `<model> (OpenAI-compatible)` | Its name in the model selector |
| `CMS_LLM_ALLOW_REMOTE` | unset | `1` allows a non-loopback endpoint. Complaint text then leaves the host. |
| `WATHQ_API_KEY` | unset | Wathq key for the verify-data tab (never logged or returned) |
| `WATHQ_API_KEY_FILE` | unset | A file holding that key instead; re-read per lookup |
| `WATHQ_ENV` | `production` | `production` or `sandbox` |
| `WATHQ_TIMEOUT` | `20` | Seconds per network operation (3–120) |
| `WATHQ_PROXY_URL` | unset | Proxy for Wathq only; `HTTPS_PROXY` is ignored |
| `WATHQ_CA_BUNDLE` | unset | Extra root CA (PEM) for TLS-inspecting networks, added to the system store |
| `WATHQ_CACHE_SECONDS` | `900` | In-memory answer cache (`0` = off, max 86400) |
| `WATHQ_ALLOW_REMOTE` | unset | `1` lets non-local clients run paid lookups |

## 8. Licensing

Qwen3 = Apache 2.0 · pypdfium2 = Apache/BSD · llama.cpp = MIT · python-docx = MIT.
The `surya-ocr` **package** reports Apache-2.0, but Surya's **model weights** may
carry additional (revenue-gated) commercial terms — verify the
[surya-ocr-2 model card](https://huggingface.co/datalab-to/surya-ocr-2) before
commercial deployment. No Tesseract, Poppler, or PyMuPDF (AGPL).

## 9. History

The OCR engine was **Qari-OCR** (LoRA on Qwen2-VL-2B, via transformers/PEFT) until
2026-09-14, when it was replaced by Surya 2 and the Qari code path was removed
(the toggle and `extract_surya.py` are gone; `extract.py` is now the sole Surya
engine). Qari model files remain in the HF cache but are unused. This folder is
**not** under git — history lives in the maintainer's notes.
