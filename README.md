# Arabic PDF Pipeline (Surya OCR + Qwen3)

A standalone web app for Saudi deeds and rulings: drop in a **PDF or image** and
work through it in four right-to-left panels — upload and preview, proofreading
you accept or reject, the document's fields with **a citation back to the line
they came from**, and a chat that answers only from that document.

Everything runs **locally on the GPU** — documents never leave the machine.

> **Docs:** [`API.md`](API.md) — HTTP API guide for other teams · [`ARCHITECTURE.md`](ARCHITECTURE.md) — internal design, runtime topology, VRAM lifecycle.

## Three local models, five stages

1. **OCR** (`extract.py`) — reads a **PDF** (each page rendered to an image) or a
   **single raster image** (PNG/JPEG/WEBP/TIFF/BMP, OCR'd directly as one page)
   with **Surya 2** (full-page OCR **+ layout** in one pass) on the **GPU**. Its
   foundation VLM runs via the bundled llama.cpp `llama-server` (no vLLM/Docker
   needed). Handles Arabic **and** English/Latin text and numbers. Every page
   also keeps its **layout** — each block's label (heading, table, picture …),
   its position, and the lines it produced — which the formatted Word export
   rebuilds the page from.
2. **Proofread** (`proofread.py` + `guard.py`) — the OCR text is passed to
   **ALLaM-7B-Instruct** (GGUF, GPU) to fix Arabic spelling/grammar. It is
   deliberately narrow: a **deterministic freeze-guard** verifies that every
   high-value token (long digit runs incl. Arabic-Indic, dates, emails, IBANs)
   survives byte-for-byte; if a page's values changed, the raw OCR page is kept.
   **This stage is a reading aid only — it does NOT feed stages 4/5**, which use
   the raw OCR as the source of truth. Each correction is offered as a separate
   suggestion the reader accepts or rejects; the Word and text exports follow
   those decisions.
3. **Classify** (`classify.py` + `registry.py` + `templates/`) — decides which
   deed this is (حصر ورثة, ملكية عقار, وكالة, نكاح, …) from a YAML **template
   registry**: a deterministic rules tier scores Arabic anchor phrases, Qwen3
   votes, and the two must agree before the verdict counts as corroborated. The
   chosen template says which fields the document is *supposed* to carry, so a
   missing one is reported as a finding instead of being silently absent.
4. **Structuring** (`structure.py` + `provenance.py`) — Qwen3 reorganises the
   **raw OCR** into the template's fields, plus repeatable groups (one row per
   heir, witness or boundary). Output is JSON-schema-constrained, so it can't emit
   malformed JSON or off-schema keys, and it never sees the image. Every value is
   then checked back against the OCR text and **cited**: `{page, line, start, end,
   quote}`, the exact characters it was read from. A value that is not a verbatim
   span is flagged rather than trusted, and digits are restored to the form the
   deed actually prints.
5. **Chat** (`chat.py`) — ask questions about the document. Qwen3 answers,
   streamed live, grounded ONLY in the extracted fields + the document text
   (fenced as data with an injection guard). Each field reaches the model with its
   `[صN سM]` tag and the answer repeats those tags, so every claim in a reply is
   one click from the line that backs it. Model output is rendered as text,
   never HTML.

### Provenance

Nothing in the analysis panel is a bare assertion. The structurer matches each
value in *normalised* space (digits folded, tashkeel and tatweel dropped, alef
forms unified) but keeps the raw offsets, so a match maps back to real characters
in the very text the browser holds. Page and line come from the `--- Page N ---`
markers `extract.py` writes: a marker starts its page and the lines after it count
from 1, blank lines included — and a marker line is never itself usable as
evidence, so the value `1` can't be "proved" by `--- Page 1 ---`.

In the UI that appears as **مصدر: صفحة ١ · سطر ١٢ ⤴** under each value. Clicking
it switches to the extracted-text view, scrolls to that line and highlights the
exact characters. **مقارنة** shows the quoted source line under every value at
once. When a value isn't verbatim — a rewritten date, say — it is cited on its
strongest token (the number) and marked approximate with `≈`.

- **Local & private** — all models run on your GPU; documents never leave the
  machine. The internal llama.cpp servers bind to localhost with a generated
  API key. **The FastAPI app itself (port 8100) has no auth** — see the security
  note in [`ARCHITECTURE.md`](ARCHITECTURE.md) before exposing it beyond
  localhost.
- **Licensing** — Qwen3 is Apache 2.0; PDF rendering uses pypdfium2 (Apache/BSD);
  llama.cpp is MIT; python-docx (Word export) is MIT. The `surya-ocr` package
  reports Apache-2.0, but the Surya **model weights** may carry additional usage
  terms — verify the Surya model card before commercial use. No Tesseract, no
  Poppler, no PyMuPDF (AGPL).
- **Loads once** at startup and stays resident; requests reuse the models.

## Requirements

- An NVIDIA GPU. Validated on an **RTX 5080 (16 GB, Blackwell/sm_120)**. Resident
  at idle: **~9 GB VRAM** (Surya OCR server ~3.4 GB + Qwen3-4B ~4.8 GB at the
  default 8192-token context; set `STRUCTURE_N_CTX` to change it). ALLaM (~5 GB)
  loads on demand per `/proofread` into the free headroom and is freed after.
- A CUDA build of PyTorch (torch cu130 validated) for Surya's layout/detection.
- Surya weights download automatically on first run into the Hugging Face cache:
  the OCR VLM GGUF (`surya-2.gguf` ~1.3 GB + mmproj ~0.2 GB) and the layout model.
  The Qwen3-4B GGUF (~4.3 GB) is read from `STRUCTURE_MODEL_PATH`; the ALLaM GGUF
  from the `models/` dir (git-ignored).

Install (torch must be the CUDA build — install it first from the official index):

```powershell
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
pip install -r requirements.txt
```

Then fetch the GPU runtime (the official llama.cpp CUDA 13.3 build — it supports
Blackwell; the pip `llama-cpp-python` CUDA wheels are 12.4 and crash on sm_120).
This one binary serves **both** the Surya OCR VLM and the Qwen3/ALLaM stages:

```powershell
./fetch_llama_server.ps1
```

The structuring model path defaults to a local Qwen3-4B GGUF; override with the
`STRUCTURE_MODEL_PATH` environment variable. Set `STRUCTURE_GPU_LAYERS=0` to run
the structurer on CPU instead of the GPU (much slower — ~30 s/page).

## Run

```powershell
cd D:\Yousef\Arabic_Text_Extraction
./run.ps1
```

`run.ps1` checks the dependencies and starts the server with the system Python
(which has the CUDA torch stack — a fresh venv would install a CPU-only torch).
Then open:

```
http://127.0.0.1:8100
```

Drop a PDF or image on the rightmost panel, or press **رفع**. Extraction starts
by itself — there is no separate button — and the other three panels fill in as
each stage finishes. If the models are still warming up, the file waits and the
run begins the moment they are ready.

> First launch loads the models (~1–2 minutes, plus a one-time Surya weight
> download). Warm timings: OCR ~7–9 s/page, proofread ~10–40 s, classification
> ~2 s, structuring ~10 s, chat replies stream in ~1–3 s.

### The four panels

The interface is right-to-left, and so is the flow: work moves from the rightmost
panel to the leftmost. Any panel collapses to a spine with the **›** button, and
that choice is remembered in the browser. Below ~1100 px the panels reflow to a
2×2 grid, and below ~680 px they stack.

| Panel | Arabic | What it does |
|---|---|---|
| 1 (rightmost) | **تحويل مستند** | Drop zone, upload, and the document itself. Three views: the original (PDF or image), **النص المستخرج** (the OCR text, one addressable line per row), and **بعد التدقيق** (the proofread text with each change marked). Zoom applies to whichever view is showing. |
| 2 | **التدقيق اللغوي** | One card per proofreading suggestion: the change, a guess at its kind (همزة, تاء مربوطة, صرفي …), and **قبول** / **رفض**. Anything the guard could not classify as a plain spelling fix is flagged **✻ يتطلب مراجعة**. **قبول الكل** accepts the lot. **تحميل Word** exports the result — rejected suggestions revert to the deed's original wording, and the page is rebuilt from the original: see *Formatted Word export* below. |
| 3 | **تحليل المستند** | The classifier's verdict, the required fields the document does **not** carry, then every extracted value as a card with its **مصدر** citation. Repeatable groups appear as one card per heir/witness. **مقارنة** reveals the quoted source line under every value. |
| 4 (leftmost) | **شات و Q&A** | A summary of what was extracted, three suggested questions generated from the document's own fields, and the chat. Citations in an answer render as clickable `ص١ س١٢` chips. |

Values keep the deed's own digits, and a token like `١٨٦٥-٠٠٠٠٢١٣٤` is pinned to
the order the deed prints it (see *Arabic numbers and bidi* below).

### Stopping

Press **Ctrl+C** in the terminal running `run.ps1`. The app's shutdown handler
reaps all three llama.cpp servers (Qwen 8123, ALLaM 8124, Surya OCR). If it was
force-killed and GPU memory is stuck, reap orphans manually:

```powershell
foreach($p in 8100,8123,8124){ $c=Get-NetTCPConnection -LocalPort $p -State Listen -EA SilentlyContinue|Select -First 1; if($c){ Stop-Process -Id $c.OwningProcess -Force } }; Get-Process llama-server -EA SilentlyContinue | Stop-Process -Force
```

## API

The full HTTP reference — request/response schemas, status codes, streaming
format, limits, and curl/Python examples — is in **[`API.md`](API.md)**. Quick
map:

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Model/engine readiness |
| `GET`  | `/progress` | Live OCR progress (page N of M) |
| `POST` | `/extract` | PDF or image → per-page OCR text + layout, and `full_text` |
| `POST` | `/proofread` | OCR text → ALLaM proofread + freeze-guard report |
| `POST` | `/classify` | OCR text → which registry template this deed is |
| `POST` | `/structure` | OCR text → section-grouped fields, each with its source citation |
| `POST` | `/chat` | Document-grounded Q&A (NDJSON stream) |
| `POST` | `/export/docx` | OCR text → plain RTL Word `.docx` |
| `POST` | `/export/layout-docx` | PDF/image + page texts + page layouts → a `.docx` that rebuilds the original page |

```bash
curl -s -F file=@document.pdf http://127.0.0.1:8100/extract
```

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI server + the JSON/stream API. Serves `ui.html` at `/`. Warms the OCR + structurer at startup. |
| `ui.html` | The whole front end: four RTL panels (تحويل مستند → التدقيق اللغوي → تحليل المستند → شات و Q&A), one inline stylesheet and one inline script, no build step and no external assets. |
| `extract.py` | Surya OCR engine: render pages (pypdfium2) → full-page OCR each page (via llama.cpp) → the text, and the layout (blocks with label, box and lines). |
| `llm.py` | Owns the two llama.cpp `llama-server` instances (Qwen3 STRUCT + ALLaM PROOF; GPU, api-key) + `chat_json` / `chat_text` / `chat_stream` helpers. |
| `classify.py` | Classification stage: rules tier + Qwen3 vote → one template, with a corroboration floor and a review flag. |
| `registry.py` | Loads and validates `templates/*.yaml`; owns the Arabic `Normalizer` (and `with_index`, which provenance is built on). |
| `templates/ksa_deeds.yaml` | The template registry: ten deed types, their fields, aliases, enums, anchor phrases and thresholds. |
| `structure.py` | Structuring stage: raw OCR text → template fields + repeatable rows, each verified against the text and cited. |
| `provenance.py` | `Locator`: finds a value in the OCR text and reports `{page, line, start, end, quote}`. |
| `proofread.py` | Proofread stage: page-chunked ALLaM cleanup, gated by the freeze-guard. |
| `guard.py` | Deterministic value freeze-guard (protects digits/dates/emails/IBANs). |
| `chat.py` | Chat stage: DATA-fenced grounding prompt + `[صN سM]` citation tags + history windowing + streaming. |
| `docx_export.py` | Plain RTL Word export (`build_docx`, python-docx). |
| `layout_docx.py` | Formatted Word export (`build_layout_docx`): the page image measured against its layout — sizes, weight, colour, shading, rules, tables, pictures, spacing — with the text written in. |
| `test_layout_docx.py` | Tests for the formatted export on the three sample documents (`python -m unittest test_layout_docx`). |
| `fetch_llama_server.ps1` | Downloads the official llama.cpp CUDA build into `vendor/`. |
| `vendor/llama-cuda/` | Vendored `llama-server.exe` + CUDA runtime (git-ignored). |
| `requirements.txt` | Python dependencies. |
| `run.ps1` | Launcher (system Python + uvicorn). |

## Formatted Word export

**تحميل Word** produces an editable `.docx` that looks like the original page.
It is **rebuilt**, not converted: the text written into it is the reader's
(the OCR with every accepted correction), and the format is measured from the
original page image, block by block, using the layout the OCR already found.

| Carried over | How it is read |
|---|---|
| Paper size, margins | the PDF's page size; the page's ink box |
| Headings, title | Surya's `SectionHeader` label; the title is the line naming the deed type (both appear in Word's navigation pane) |
| Alignment | where each line sits between the margins — right, left, centred, justified |
| Font size | the height of the tall letters above the baseline, and the line's width against the same text in Arial — the smaller of the two, so no line wraps that didn't |
| Bold, colour | stroke thickness against the size; the core colour of the letters |
| Shaded bands and cells | the colour behind the text — including white text on a filled band |
| Accent bars, rules | a thin bar beside a heading; horizontal lines between blocks |
| Tables and forms | Surya's tables with their own column widths, rules and shaded header rows; label/value rows split where the page shows a column gap; blocks side by side as one row |
| Pictures | logos, stamps, signatures and QR codes cropped from the page and placed where they were |
| Spacing | each line placed at the height it had on the page |

**Not carried over:** the original typeface (everything is set in Arial), text
inside images (a stamp stays a picture), and free-floating positions — Word
flows the content, so an element that overlapped another is placed after it.

For the best size estimates the server needs **Pillow with libraqm** (it shapes
Arabic to measure line widths) and an **Arial** font file; without them sizes
come from letter height alone (±10%). On Windows, Pillow's libraqm needs
`fribidi` on the DLL path; `LAYOUT_ARIAL` / `LAYOUT_ARIAL_BOLD` point at the font
files if they are not in the usual place. An API caller that sends no layout
gets the older text-only formatting (see `API.md`).

## Arabic numbers and bidi

A deed prints its death-certificate number as `١٨٦٥-٠٠٠٠٢١٣٤`. Left to the
browser, that renders **backwards** — `٠٠٠٠٢١٣٤-١٨٦٥` — in every base direction,
`rtl`, `ltr` and `auto` alike, and an isolate does not help. The two digit runs
are bidi class AN and the `-` between them is class ES; UAX #9 rule W4 only folds
a separator into a number between two *European* digits, so between two AN runs
the hyphen stays neutral and rule N1 resolves it right-to-left, swapping the
halves. Dates were never affected because `/` is class CS, which W4 does fold.

Every surface that prints a value therefore wraps each numeric token in
`<bdo dir="ltr">`, which pins it to memory order — the order the deed prints.
The stored string is never touched, so Copy JSON, the chat prompt and the Word
export all still carry the original. When editing `ui.html`, keep values flowing
through `escNum()` (HTML strings) or `appendNumText()` (text nodes); anything that
sets `.textContent` directly will print deed numbers backwards.

> The same class of bug still applies to the **Word export**, where `<bdo>` has no
> OOXML equivalent — a separator inside a number needs `U+200E` on both sides.
> Tooltips (`title=`) and the chat input cannot carry the override either.

## Notes & known limits

- **Best on modern printed documents** — contracts, forms, reports. On these it
  is highly faithful (names, IDs, and English fields transcribed exactly).
- **A citation is evidence, not proof of correctness.** It shows the characters a
  value was read from. If the OCR misread those characters, the citation faithfully
  points at the misreading — compare against the original page, which is what
  **مقارنة** and the document view are for.
- **An uncorroborated classification can still escape review.** The rules tier and
  the model disagreeing costs 20% of the model's confidence, so a confident model
  (≥ 0.875) lands above the 0.70 `review_threshold` and is not flagged. Check the
  document type on the analysis panel when the rules score is low.
- **Struggles on heavily degraded / handwritten scans** — out of distribution;
  can return garbled output.
- Upload ceiling 100 MB. Binds to `127.0.0.1` (local only); to expose on a LAN
  start uvicorn with `--host 0.0.0.0` on a trusted network **and add auth** (the
  app has none of its own).
- OCR model: [`datalab-to/surya-ocr-2`](https://huggingface.co/datalab-to/surya-ocr-2)
  (GGUF served via llama.cpp; check the model card for weight licensing terms).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the runtime topology, VRAM lifecycle,
and the Blackwell/llama.cpp GPU notes.

## Editing the UI

`ui.html` is served as-is; there is no bundler, no framework and no external font
or script (the page's own CSP forbids them). `app.py` reads it **once at import**,
so restart `run.ps1` after an edit — `run.ps1` deliberately runs uvicorn without
`--reload`.

Three rules worth keeping when you change it:

- Untrusted text (OCR output, model answers, filenames) never reaches `innerHTML`
  unescaped. Use `esc()`/`escNum()` for HTML strings, `appendNumText()`/
  `setChatText()` for nodes.
- Numeric values go through `escNum()`/`appendNumText()` so the bidi override is
  applied — see *Arabic numbers and bidi*.
- Arabic-Indic digits in the **interface** (counts, page numbers, zoom) come from
  `arNum()`. Never run a value from the document through it: values must be shown
  exactly as the deed prints them.

## QR Bot integration

QR links are processed by a separate local service on port **8101**, with its own
Python environment and Docker sandbox. Start Docker's Linux engine, then in a
separate terminal run:

```powershell
cd QR_Code_Scanner
.\.venv\Scripts\python.exe -m qrbot_service
```

Start/restart this application normally with `run.ps1`. After selecting a document,
expand **رموز QR والصفحات المرتبطة** below the upload area and choose
**اكتشاف QR وفحص الأمان**. Every non-blocked link waits for **Get content** or **Skip**;
scanning alone never visits the destination. Captures appear as images in a separate
preview area with PDF/report downloads. Only **Use capture in OCR** makes a capture
the active document for the existing proofreading, fields, exports and chat flow.
The original document can be restored from the preview area.
This integration is for the existing local, single-operator
application; the QR credential stays in the backend.

See [QR API documentation](QR_Code_Scanner/API.md) for authentication, job persistence,
endpoint details, and installation on another machine.


cd "D:\Yousef\Arabic_Text_Extraction"
.\run.ps1

cd "D:\Yousef\Arabic_Text_Extraction\QR_Code_Scanner"
.\.venv\Scripts\python.exe -m qrbot_service




The current Temprature settings are:

| Model / task | Temperature |
|---|---:|
| **Qwen3 — document classification and field extraction** | **0.0** |
| **Qwen3 — document chat / Q&A** | **0.2** |
| **ALLaM — proofreading** | **0.0** |
| **Surya — OCR** | Not explicitly set in the project; uses the library default |

These are configured in [llm.py](D:/Yousef/Arabic_Text_Extraction/llm.py:333). All three Qwen/ALLaM paths use `top_p = 1.0`; extraction and proofreading also set `seed = 0`.