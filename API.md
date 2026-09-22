# Arabic PDF Pipeline — HTTP API Guide

**Audience:** teams integrating with the Arabic PDF Pipeline service.
**App version:** 3.4.0 · **OCR engine:** Surya 2 (GPU via llama.cpp).

The related-regulations integration adds `POST /regulations/related` (streamed document-level retrieval) and `GET /regulations/source/{source_index}` (local source PDFs). See [REGULATIONS_INTEGRATION.md](REGULATIONS_INTEGRATION.md) for request limits, events, scope and startup.

This service turns an Arabic (or mixed Arabic/English) **PDF or image** into: OCR
text, an optional proofread version, structured `label → value` fields, and a
document-grounded chat. Everything runs locally on the host's GPU.

---

## 1. Base URL & access

| | |
|---|---|
| **Base URL** | `http://127.0.0.1:8100` |
| **Binding** | `127.0.0.1` (localhost only) by default |
| **Auth** | **None** on the HTTP API |
| **Content type (JSON endpoints)** | `application/json`, UTF-8 (Arabic returned literally, not `\u`-escaped) |

> **To call it from another machine:** the app must be started bound to a
> reachable interface (`uvicorn app:app --host 0.0.0.0 --port 8100`) **and placed
> behind auth** (reverse proxy / gateway) — the app has no authentication or rate
> limiting of its own, and documents are processed in clear. Only expose it on a
> trusted network. Prefer calling it from the same host.

**Processing is serialized.** Each model server runs one request at a time
(`--parallel 1`). Concurrent callers are queued, not run in parallel — size your
client timeouts accordingly (see §7).

---

## 2. Conventions

- **Success:** `200 OK`. JSON endpoints return a JSON object; export endpoints
  return a binary `.docx`; `/chat` returns a streamed NDJSON body.
- **Errors:** non-2xx status with a JSON body `{"error": "<message>"}`.
- **Encoding:** send request JSON as UTF-8. Arabic in responses is real UTF-8.

### Error status codes

| Status | Meaning |
|---|---|
| `400` | Bad request (missing/empty text, malformed JSON, bad `pages`, last chat message not from `user`) |
| `413` | Payload too large (see limits in §7) |
| `415` | Unsupported media type (uploaded file is not a PDF) |
| `500` | Processing failed (OCR/structure/proofread/export error) |
| `503` | A required model is not ready yet (still loading, or failed to load) |

---

## 3. Quick start

```bash
# 1) Check the service is up and models are ready
curl -s http://127.0.0.1:8100/health

# 2) OCR a PDF
curl -s -F file=@contract.pdf http://127.0.0.1:8100/extract > ocr.json

# 3) Structure the OCR text
python - <<'PY'
import json, requests
text = json.load(open("ocr.json"))["full_text"]
r = requests.post("http://127.0.0.1:8100/structure", json={"text": text})
print(json.dumps(r.json(), ensure_ascii=False, indent=2))
PY
```

---

## 4. Endpoint reference

### `GET /health`
Readiness of the three models. Poll this before sending work; `status` per model
is one of `not_loaded` · `loading` · `ready` · `error`.

**200 response**
```json
{
  "status": "ok",
  "ocr_engine": "surya",
  "engine":      { "status": "ready", "error": null, "device": "cuda (llama.cpp)",
                   "model": "datalab-to/surya-ocr-2 (GGUF · llama.cpp)", "gpu": true, "engine": "surya" },
  "structurer":  { "status": "ready", "error": null, "model": "Qwen3-4B-Instruct-2507-Q8_0.gguf", "device": "gpu" },
  "proofreader": { "status": "not_loaded", "error": null, "model": "ALLaM-…-Q4_K_M.gguf", "device": "gpu" }
}
```
`engine` = the OCR engine; `proofreader` is normally `not_loaded` until the first
`/proofread` (it is loaded on demand and freed after).

---

### `GET /progress`
Live progress of the current OCR job (for progress bars). Poll while `/extract`
is running.

**200 response**
```json
{ "active": true, "page": 3, "total": 8, "filename": "contract.pdf" }
```
`active` is `false` when no OCR is in flight.

---

### `POST /extract`
OCR a document: every page of a **PDF**, or a **single image** (one page).

**Request:** `multipart/form-data` with one field:

| Field | Type | Notes |
|---|---|---|
| `file` | file | A **PDF** (`.pdf` / `%PDF-` header) **or a raster image** — PNG, JPEG, WEBP, TIFF, BMP (detected by magic bytes, with an extension fallback). An image is OCR'd as a single page. Max 100 MB. |

**200 response**
```json
{
  "filename": "contract.pdf",
  "page_count": 2,
  "pages": [
    { "page": 1, "text": "عقد عمل …", "chars": 1777,
      "layout": { "blocks": [
        { "label": "SectionHeader", "bbox": [0.43, 0.13, 0.57, 0.15], "lines": ["عقد عمل"] },
        { "label": "Picture",       "bbox": [0.05, 0.03, 0.18, 0.11], "lines": [] },
        { "label": "Table",         "bbox": [0.08, 0.24, 0.92, 0.39],
          "lines": ["الاسم | محمد", "رقم الهوية | ١٠٢٣٤٥٦٧٨٩"] }
      ] } },
    { "page": 2, "text": "…", "chars": 640, "layout": { "blocks": [ … ] } }
  ],
  "full_text": "--- Page 1 ---\nعقد عمل …\n\n--- Page 2 ---\n…"
}
```
- `full_text` joins pages with `--- Page N ---` markers — feed this to
  `/structure` and `/chat`, or `pages[].text` to `/export/layout-docx`.
- `pages[].layout` is what Surya found on the page, in reading order: each
  block's layout `label` (`Text`, `SectionHeader`, `PageHeader`, `PageFooter`,
  `Table`, `Form`, `Picture`, `Caption`, …), its `bbox` as fractions of the page
  (`[x0, y0, x1, y1]`, origin top-left) and the `lines` of `text` it produced.
  Joining every block's `lines` with `\n` gives `text` back exactly; `Picture`,
  `Figure` and `Diagram` blocks (logos, stamps, QR codes) have no lines. It is
  `null` when a text block came back without a usable box. Pass the array of
  them to `/export/layout-docx` to have the Word export rebuild the page.
- A table row is one line with its cells separated by ` | `.
- **Errors:** `413` (>100 MB), `400` (empty file), `415` (not a PDF), `500`.
- **Timing:** warm ~7–9 s/page. The **first** call after startup cold-spawns the
  Surya OCR server (~60–90 s) — allow a generous timeout on the first request.

```bash
curl -s -F file=@contract.pdf http://127.0.0.1:8100/extract
```

---

### `POST /proofread`
Proofread the OCR text with ALLaM, gated by a deterministic freeze-guard.
**This is a reading aid** — it does **not** change what `/structure` or `/chat`
see. Use it only when you want a cleaned-up Arabic rendering.

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `text` | string | OCR text. Truncated to **40 000 chars**. |

**200 response**
```json
{
  "allam": {
    "backend": "allam",
    "corrected": "…proofread text…",
    "reverted_pages": 0,
    "page_count": 2,
    "protected_count": 7,
    "changed_values": [],
    "edits": [ { "before": "اشهر", "after": "أشهر" } ],
    "ms": 7900
  }
}
```
- `changed_values` lists any high-value tokens ALLaM tried to alter; if non-empty
  for a page, that page is **reverted to raw OCR** (`reverted_pages` counts them).
  `edits` is a capped (≤60) before→after diff for display.
- **Errors:** `400` (no text), `503` (model not ready), `500`.
- **Timing:** ~10–40 s. Adds a few seconds on the first call after idle (ALLaM
  loads on demand, then is freed).

---

### `POST /classify`
Decide which registry template the document is, before structuring it. Advisory:
if it fails or abstains, call `/structure` without a `template_id` and you get the
free-form structurer.

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `text` | string | OCR text (usually `full_text`). Truncated to **40 000 chars**. |

**200 response**
```json
{
  "template_id": "sakk_hasr_warathah",
  "name_ar": "صك حصر ورثة",
  "name_en": "Inheritance / heirs inventory deed",
  "family": "estate",
  "confidence": 0.9,
  "source": "llm+rules",
  "generic": false,
  "needs_review": false,
  "rules_score": 0.9,
  "corroborated": true,
  "evidence": ["وثيقة ورثة متوفى", "وانحصر ورثته في"],
  "rules": [ { "template_id": "sakk_hasr_warathah", "name_ar": "صك حصر ورثة",
               "score": 0.9, "raw": 9, "max_raw": 10,
               "hits": ["حالة الوارث", "صلة القرابة"], "negatives": [] } ],
  "llm": { "template_id": "sakk_hasr_warathah", "confidence": 0.92, "evidence": ["…"] },
  "error": null,
  "ms": 1840
}
```

| Field | Meaning |
|---|---|
| `source` | `llm+rules` (both tiers agree — the strong case) · `llm` · `rules` · `fallback` |
| `corroborated` | Both tiers picked the same template **and** the rules score cleared the corroboration floor. Agreement on a near-zero rules score does not count. |
| `generic` | No template matched; `template_id` is the fallback and the document gets free-form extraction. |
| `needs_review` | `confidence < review_threshold` (0.70). **Note:** an uncorroborated model answer is only penalised to `0.8 × confidence`, so a very confident model can still land above the threshold — treat a low `rules_score` as a review signal in its own right. |
| `rules` | Top 3 rule-tier candidates with the Arabic anchor phrases that fired. |

- **Errors:** `400` (no text), `503` (registry unavailable), `500`.
- **Timing:** ~2 s (rules tier alone is instant; the model vote dominates).

---

### `POST /structure`
Reorganise OCR text into fields — the template's fields when you pass a
`template_id`, otherwise the document's own sections. JSON-schema-constrained; the
model never sees the image. **Every value carries a citation back to the OCR text.**

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `text` | string | OCR text (usually `full_text`). Truncated to **12 000 chars**; `truncated` flags it. |
| `template_id` | string | Optional. From `/classify`. Empty, `generic_document`, or an unknown id all mean free-form extraction. |

**200 response**
```json
{
  "document_type": "صك حصر ورثة",
  "template_id": "sakk_hasr_warathah",
  "template_name_ar": "صك حصر ورثة",
  "truncated": false,
  "output_capped": false,
  "extras": 2,
  "passes": 3,
  "missing_required": [
    { "key": "attesting_officer_name", "label_ar": "كاتب العدل", "label_en": "Attesting notary or judge" }
  ],
  "sections": [
    { "title": "بيانات المتوفى",
      "fields": [
        { "label": "اسم المتوفى", "value": "نافع سبيل عوده الحربي",
          "key": "deceased_name", "required": true,
          "status": "ok", "origin": "aligned",
          "source": { "page": 1, "line": 7, "start": 143, "end": 164,
                      "quote": "اسم المتوفى | نافع سبيل عوده الحربي |" } }
      ] },
    { "title": "بيانات الورثة", "record_label": "الوارث", "fields": [],
      "records": [
        [ { "label": "اسم الوارث", "value": "ناصر نافع سبيل الحربي",
            "key": "heir_name", "required": true, "status": "ok", "origin": "aligned",
            "source": { "page": 1, "line": 15, "start": 408, "end": 429, "quote": "الابن | ناصر نافع سبيل الحربي | …" } },
          { "label": "صلة القرابة", "value": "الابن", "key": "heir_relation", "status": "ok", "origin": "aligned",
            "source": { "page": 1, "line": 15, "start": 400, "end": 405, "quote": "الابن | ناصر نافع سبيل الحربي | …" } } ]
      ] }
  ]
}
```

**Field properties**

| Key | Meaning |
|---|---|
| `key` | The registry field id (absent for free-form and for extras). |
| `required` | The template says this deed type must carry it. |
| `status` | `ok` — the value is a verbatim span of the OCR text · `unverified` — not verbatim, or it carries a number/date the text does not · `mismatch` — it fails the field's enum or format. Absent for extras. |
| `origin` | `document` · `model` · `aligned` (the model's value was realigned to the text, e.g. digits restored to the deed's Arabic-Indic form). |
| `source` | Where the value was read from, or **absent** when it could not be located. |

**`source`**

| Key | Meaning |
|---|---|
| `page`, `line` | 1-based. Derived from the `--- Page N ---` markers in `full_text`: a marker starts its page, the lines after it count from 1 (blank lines included), and the marker line itself is never cited. |
| `start`, `end` | Character offsets into **the exact `text` you posted**, so `text[start:end]` is the value as the document prints it. |
| `quote` | The source line, windowed to ~200 chars around the hit if it is long. |
| `approx` | Present and `true` when the value is not verbatim and was cited on its strongest token (its number or date) instead. |

**Repeatable groups** (heirs, witnesses, boundaries) arrive as `records`: a list of
rows, each row a list of the same fields in the same order. A section has `fields`,
`records`, or both. Every cell in a row is cited on that row's own line, so the
fifth heir's relation never points at the first heir's.

- **Errors:** `400` (no text), `500`.
- **Timing:** ~10 s.

```bash
curl -s -H "Content-Type: application/json" \
  -d '{"text":"صك حصر ورثة\nاسم المتوفى: نافع سبيل عوده الحربي","template_id":"sakk_hasr_warathah"}' \
  http://127.0.0.1:8100/structure
```

---

### `POST /chat`
Document-grounded Q&A, streamed. The answer is grounded ONLY in the `context` you
pass (structured fields + document text), fenced as data with an injection guard.

**Request:** `application/json`
```json
{
  "messages": [ { "role": "user", "content": "ما هي وظيفة العامل؟" } ],
  "context": {
    "document_type": "عقد عمل",
    "sections": [ { "title": "…", "fields": [ { "label": "…", "value": "…" } ] } ],
    "full_text": "--- Page 1 ---\n…"
  }
}
```
- `messages`: only `user`/`assistant` roles are used (a client `system` role is
  ignored — the server builds the sole system prompt). The **last message must be
  `user`** (else `400`). History is windowed to the last 40 turns, 4 000 chars each.
- `context.full_text` is capped to 24 000 chars; `sections` are size-bounded.
  Pass the `/structure` output as `sections` and the OCR `full_text` for best
  grounding.

**200 response:** `application/x-ndjson; charset=utf-8` — one JSON object per line:

| Frame | Meaning |
|---|---|
| `{"delta": "<chunk>"}` | A piece of the answer. Concatenate `delta`s in order. |
| `{"done": true, "truncated": false}` | End of stream. `truncated` = the answer hit the token cap. |
| `{"error": "<message>"}` | Generation failed (e.g. assistant busy). |

- **Errors (before streaming):** `400` (last message not from user), `503`
  (assistant not ready). Once streaming starts, failures arrive as an `{"error"}`
  frame.

**Python (streaming):**
```python
import json, requests
body = {
    "messages": [{"role": "user", "content": "ما هي وظيفة العامل؟"}],
    "context": {"document_type": "", "sections": [], "full_text": ocr_full_text},
}
with requests.post("http://127.0.0.1:8100/chat", json=body, stream=True, timeout=300) as r:
    answer = ""
    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        ev = json.loads(line)
        if "delta" in ev:
            answer += ev["delta"]
        elif ev.get("error"):
            raise RuntimeError(ev["error"])
    print(answer)
```

---

### `POST /export/docx`
Plain, editable **RTL Arabic** Word document from OCR text.

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `text` | string | The text to export (usually `full_text`). |
| `filename` | string | Base name (sanitised to ASCII). Optional; default `document`. |

Request body max **16 MB**.

**200 response:** binary `.docx`
(`Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document`,
`Content-Disposition: attachment; filename="<name>.docx"`).
**Errors:** `413` (body too large), `400` (invalid/empty), `500`.

```bash
curl -s -H "Content-Type: application/json" \
  -d "{\"text\":\"$(cat ocr.txt)\",\"filename\":\"contract\"}" \
  http://127.0.0.1:8100/export/docx -o contract.docx
```

---

### `POST /export/layout-docx`
**Formatted** Word document that rebuilds the original page: paper size and
margins, headings, alignment, font sizes, bold, colours, shaded bands and
cells, accent bars, horizontal rules, ruled tables with their column widths,
label/value forms, blocks side by side, pictures (logos, stamps, QR codes) and
the vertical spacing — all measured from the page image, with the text you
send written into it. The text itself is never read from the image.

**Request:** `multipart/form-data`

| Field | Type | Notes |
|---|---|---|
| `file` | file | The **original PDF or image**, re-rendered locally for measuring. Max 100 MB. |
| `pages` | string | JSON **array of per-page text strings** (e.g. `[p.text for p in pages]`; one element for an image). ≤500 pages, ≤100 000 chars/page. The text may differ from the OCR — proofreading — and is matched back to the OCR lines. |
| `layout` | file | *Optional.* A **JSON file part** holding the array of `pages[].layout` values from `/extract` (one per page, `null` allowed). ≤32 MB. A file part rather than a field because it carries every line's text, and a long document's would exceed the 1 MB form-field limit. |
| `filename` | string | Base name. Optional; default `document`. |

- **With `layout`**, each page is rebuilt from its blocks. A page whose layout is
  `null` or malformed (a block without a valid `bbox`, `lines` not strings) is
  formatted from its text alone instead — the export never fails over it.
- **Without `layout`**, every page is formatted from its text alone: lines are
  classified by shape (headings, `label: value` rows, pipe tables), forms are
  rebuilt from the template registry's labels, and the heading colours are
  sampled once with Surya's layout model on the CPU. Alignment, sizes and
  pictures need the layout.

**200 response:** binary `.docx` (as above).
**Errors:** `413` (file >100 MB, `layout` >32 MB), `400` (invalid `pages` / no
text / empty file / `layout` not a JSON array), `415` (not a PDF or image), `500`.

```python
import json, requests
pages   = [p["text"] for p in ocr["pages"]]
layouts = [p.get("layout") for p in ocr["pages"]]
files = {"file":   ("contract.pdf", open("contract.pdf", "rb"), "application/pdf"),
         "layout": ("layout.json", json.dumps(layouts), "application/json")}
data  = {"pages": json.dumps(pages), "filename": "contract"}
r = requests.post("http://127.0.0.1:8100/export/layout-docx", files=files, data=data, timeout=300)
open("contract_formatted.docx", "wb").write(r.content)
```

---

### `GET /`
Returns the browser UI (HTML). Not an integration endpoint.

---

## 5. Typical integration flow

```
          ┌────────── /extract (PDF → OCR) ──────────┐
 PDF ────►│  full_text, pages[]                       │
          └──────────────┬───────────────────────────┘
                         │ full_text
         ┌───────────────┼─────────────────────────────┐
         ▼               ▼                               ▼
   /structure       /chat (with context)          /export/*  (Word)
   fields[]         streamed answer               .docx bytes
         │
         └─(optional, display only) /proofread
```

1. `POST /extract` → keep `full_text` and `pages` (each page's `text` and `layout`).
2. `POST /structure` with `{"text": full_text}` → `sections`.
3. `POST /chat` with `messages` + `context={document_type, sections, full_text}`.
4. Optional: `POST /proofread` for a cleaned reading copy;
   `POST /export/docx` or `/export/layout-docx` (with the pages' `layout`) for Word output.

Poll `GET /health` first; only send work once `engine` and `structurer` are
`ready`.

---

## 6. Full example (end to end, Python)

```python
import json, requests
BASE = "http://127.0.0.1:8100"

# wait for readiness
import time
for _ in range(60):
    h = requests.get(f"{BASE}/health", timeout=5).json()
    if h["engine"]["status"] == "ready" and h["structurer"]["status"] == "ready":
        break
    time.sleep(2)

# OCR
ocr = requests.post(f"{BASE}/extract",
                    files={"file": ("doc.pdf", open("doc.pdf", "rb"), "application/pdf")},
                    timeout=600).json()
full_text = ocr["full_text"]

# Structure
structure = requests.post(f"{BASE}/structure", json={"text": full_text}, timeout=300).json()

# Chat (grounded)
body = {"messages": [{"role": "user", "content": "لخّص العقد في ثلاث نقاط."}],
        "context": {"document_type": structure["document_type"],
                    "sections": structure["sections"], "full_text": full_text}}
with requests.post(f"{BASE}/chat", json=body, stream=True, timeout=300) as r:
    for line in r.iter_lines(decode_unicode=True):
        if line:
            ev = json.loads(line)
            print(ev.get("delta", ""), end="")
```

---

## 7. Limits & timeouts

| Endpoint | Input limit | Typical time | Suggested client timeout |
|---|---|---|---|
| `/extract` | 100 MB PDF | ~7–9 s/page (warm) | 600 s (first call cold-spawns the OCR server, ~60–90 s) |
| `/proofread` | 40 000 chars | ~10–40 s | 120 s |
| `/structure` | 12 000 chars (rest ignored, `truncated=true`) | ~10 s | 120 s |
| `/chat` | 40 turns · 4 000 chars/msg · 24 000 chars context | streams in 1–3 s | 300 s |
| `/export/docx` | 16 MB body | <1 s | 60 s |
| `/export/layout-docx` | 100 MB PDF · 500 pages · 100 000 chars/page · 32 MB layout | ~1 s/page with `layout` (CPU); without it, one Surya layout pass for the document | 300 s |

- **One at a time:** the models serialize requests; a second caller waits.
- **Determinism:** OCR, proofread and structuring run greedy/`temp=0`, so the
  same input gives the same output. Chat uses a small temperature.

---

## 8. Notes

- **Arabic / RTL:** all text is UTF-8. Word exports set correct RTL direction
  (Arabic hugs the right; titles centred). Send/receive UTF-8 throughout.
- **Numbers are protected in proofread** (freeze-guard): IDs, dates, IBANs and
  long digit runs can never be silently altered by ALLaM.
- **Privacy:** documents are processed on the host GPU and are not sent anywhere
  external. There is no built-in retention — nothing is persisted server-side by
  these endpoints.
- **Versioning:** breaking changes will bump the app version (see `GET /health`
  is not versioned; the version is in the app title / this doc).

_Questions or access requests: contact the pipeline maintainer._

## Document comparison

The **مقارنة المستندات** tab has independent uploads, extraction state and results.
It reuses `/extract` for PDFs/images and reads UTF-8 `.txt` directly. It does not
replace the document in the analysis/chat workspace. DOCX and visual comparison
are not supported by this first version.

`POST /comparison/run` accepts `application/json`:

```json
{"a":{"name":"first.txt","text":"First document text","page_count":0,"empty_pages":[]},
 "b":{"name":"second.txt","text":"Second document text","page_count":0,"empty_pages":[]}}
```

- Each text is required and limited to **40,000 Unicode characters**, with a
  1 MiB request-body cap. Oversized documents are rejected, never silently clipped.
- For OCR, send the original `full_text` with its `--- Page N ---` markers,
  `page_count`, and any page numbers whose extracted text is empty. Plain text
  uses `page_count: 0` and citations show line numbers.
- The response is newline-delimited JSON: `progress`, optional `heartbeat`, then
  `result` containing `report`, or `error`. Errors after streaming starts are
  stream events; validation errors use HTTP 400/413/415, busy uses 409.
- Each finding includes an aspect, status, explanation and verified `a`/`b`
  citations (source quotes, page/line, Unicode and UTF-16 character offsets).
  `source` distinguishes local LLM interpretation from deterministic text checks.
- Reuses Qwen3 through `llm.chat_json`, with a dedicated schema and prompt;
  single-document `/chat` restrictions and context clipping do not apply.
  Decoding constrains quotations to actual source spans and requires evidence
  on both sides of paired findings. Validation rejects fabricated quotes and
  false similarity claims for matching phrases with changed digits; one bounded
  repair pass attempts to recover rejected findings before reporting gaps.
- Small inputs compare every pair of chunks. Larger inputs use the existing
  local Ollama `qwen3-embedding:0.6b` at port 11434 for bidirectional passage
  alignment, falling back to word overlap with an explicit warning. Every chunk
  gets at least one comparison; this is not exhaustive all-pairs comparison.
- The report includes pass failures, rejected findings, unreadable pages and
  per-document reviewed-chunk counts. `complete` means all selected passes
  succeeded with valid evidence and no known empty pages; it is **not** a
  guarantee that every difference or every OCR error was detected.
- Interpretations remain model-generated even when quotes are verified. A
  one-sided finding means no counterpart in the compared excerpt, not proven
  absence from the whole other document. Visual layout/signatures are excluded.
- Only one comparison runs per application process. Disconnect/cancel stops
  subsequent passes; an already-running OCR/LLM/embedding call may finish first.
  Existing OCR and LLM locks serialize access with other tabs. No documents,
  reports or temporary embeddings are persisted by this endpoint. The user can
  explicitly download a JSON report from the tab.

Run comparison regression tests with `python -m unittest test_comparison test_comparison_api`.
The assets are served as `/comparison/ui.js` and
`/comparison/ui.css`.

## QR Bot integration

The main backend exposes `/qr/health`, `POST /qr/jobs`, `GET /qr/jobs/{job_id}`,
`POST /qr/jobs/{job_id}/approvals`, `POST /qr/jobs/{job_id}/skips`, and
`GET /qr/jobs/{job_id}/artifacts/{artifact_id}`. These forward to the separate QR
service on localhost port 8101. Browser POSTs require `X-QRBot-Request: 1` and
same-origin requests; the service credential is never exposed to the browser.

`POST /qr/jobs/{job_id}/artifacts/{artifact_id}/extract` runs the existing OCR on a
captured PDF and returns a separate `extraction` object with `source_url` and
`found_on_pages` provenance. The endpoint alone does not change UI state. Explicitly
choosing **Use capture in OCR** activates the returned extraction in the normal
document workflow. PNG previews use `preview-N-P` artifact IDs and are served inline.

This remains a local single-operator integration. Review decisions are recorded
under the configured QR service account, not a client-supplied name.
See the complete [QR service API contract](QR_Code_Scanner/API.md).
