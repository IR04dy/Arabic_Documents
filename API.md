# Arabic PDF Pipeline — HTTP API Guide

**Audience:** teams integrating with the Arabic PDF Pipeline service.
**App version:** 3.4.0 · **OCR engine:** Surya 2 (GPU via llama.cpp).

The related-regulations integration adds `POST /regulations/related` (streamed document-level retrieval) and `GET /regulations/source/{source_index}` (local source PDFs). See [REGULATIONS_INTEGRATION.md](REGULATIONS_INTEGRATION.md) for request limits, events, scope and startup.

The complaint-management tab (إدارة الشكاوى) adds the `/complaints` endpoints: see [Complaints (CMS)](#complaints-cms) below, and [COMPLAINTS.md](COMPLAINTS.md) for what the pipeline does.

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

> **Host header:** every request must name `127.0.0.1` or `localhost` as its
> host (any port), or a name listed in the comma-separated `APP_ALLOWED_HOSTS`
> environment variable; anything else gets `400 Invalid host header`. This
> blocks DNS rebinding. `http://[::1]:8100` is refused too — use
> `127.0.0.1`. Add the name or address clients use when exposing the app on a
> LAN.

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
is running. The complaints worker's OCR is not reported here.

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
  these endpoints. The `/complaints` endpoints are the exception: they keep a
  register of complaints and their files (see [Complaints (CMS)](#complaints-cms)).
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

## Complaints (CMS)

The API of the **إدارة الشكاوى** tab, under `/complaints`. It is a register
with a background worker: uploads return at once with queued items, and one
worker processes them a complaint at a time (OCR → two LLM calls → rules).
What the pipeline does, the priority model and the review reasons are in
[COMPLAINTS.md](COMPLAINTS.md). This section is the HTTP contract.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/complaints/config` | Taxonomy, model providers, the active one, limits |
| `PUT` | `/complaints/provider` | Switch the active model |
| `POST` | `/complaints/upload` | Queue files (PDF, image, UTF-8 `.txt`) |
| `POST` | `/complaints/text` | Queue pasted text |
| `GET` | `/complaints/items` | The register: filter, search, sort, page |
| `GET` | `/complaints/items/{id}` | One complaint in full, with its analysis and history |
| `GET` | `/complaints/items/{id}/file` | The original upload |
| `POST` | `/complaints/items/{id}/reprocess` | Queue it again (optionally re-running OCR) |
| `POST` | `/complaints/items/{id}/feedback` | Reviewer verdict and corrections |
| `POST` | `/complaints/items/{id}/fields` | Accept or change one extracted field |
| `POST` | `/complaints/items/{id}/status` | Follow-up status |
| `DELETE` | `/complaints/items/{id}` | Erase it |
| `GET` | `/complaints/queue` | Worker and queue state |
| `GET` | `/complaints/analytics` | Dashboard aggregates |
| `POST` | `/complaints/insights` | Model-written insights and recommendations |
| `GET` | `/complaints/export.csv` | The register as CSV |
| `GET` | `/complaints/ui.js`, `/complaints/ui.css` | The tab's assets |

### Conventions

- **Errors** are `{"error": "<Arabic message>"}`. A message never carries
  document text, model output or exception details.
- **Ids** in paths are the numeric complaint id (1–18 digits); anything else
  is `404`. Complaints also have a reference, `CMP-<year>-<id>`, which is for
  people, not paths.
- **Taxonomy values** (category, subcategory, ministry, priority, region,
  governorate, status) are ids from `GET /complaints/config`. An unknown id is
  refused with `400`.
- **Timestamps** are ISO UTC (`2026-09-24T18:02:11Z`). Days (the trend, the
  year in a reference) are counted in Riyadh time, UTC+3.
- **JSON bodies** need `Content-Type: application/json` (else `415`), even
  when the body is empty or optional; an empty body then counts as `{}`. They
  are capped at 64 KB, or 244 096 bytes for pasted text, before parsing
  (`413`).
- **Cross-site requests are refused.** Every `PUT`, `POST` and `DELETE` gets
  `403` when the browser labels it cross-site: a `Sec-Fetch-Site` other than
  `same-origin` or `none`, or an `Origin` that is not the app's own. Requests
  without either header (curl, scripts) pass.
- **Service down.** If the complaints database cannot be opened (locked by
  another program, damaged, or written by newer code), or the taxonomy fails
  to load, every `/complaints` route except the two assets answers `503`. The
  rest of the app works.
- **Logging.** The access log shows `/complaints` paths with their query
  string replaced by `?[redacted]`: a register search is a name or a national
  ID.

| Status | Meaning here |
|---|---|
| `400` | Invalid body, parameter or taxonomy id; no files in an upload |
| `403` | Cross-site state change |
| `404` | No such complaint, or no original file |
| `409` | The complaint is being processed (reprocess, delete), or is not processed yet (feedback, field review); insights with no processed complaint, or a run already going |
| `413` | Body, file or text too large; more than 20 files |
| `415` | JSON endpoint called without `application/json` |
| `500` | Saving failed; insights failed unexpectedly |
| `502` | Insights: the model gave no usable answer |
| `503` | Complaints service unavailable; insights: model busy or unavailable |

---

### `GET /complaints/config`
The vocabularies the UI and a client need, and the model providers.

**200 response**
```json
{
  "taxonomy": {
    "version": 1,
    "receiving_entity": { "id": "riyadh_emirate", "label_ar": "إمارة منطقة الرياض",
                          "label_en": "Riyadh Region Principality", "region": "riyadh",
                          "desk_ar": "إدارة الشكاوى" },
    "priorities": [ { "id": "critical", "label_ar": "حرجة", "label_en": "Critical", "rank": 4,
                      "sla_hours": 24, "description_ar": "…" } ],
    "ministries": [ { "id": "mewa", "label_ar": "وزارة البيئة والمياه والزراعة", "label_en": "…" } ],
    "categories": [ { "id": "water_sewage", "label_ar": "المياه والصرف الصحي", "label_en": "Water and sewage",
                      "ministry": "mewa", "description_ar": "…",
                      "subcategories": [ { "id": "sewage_overflow", "label_ar": "طفح الصرف الصحي" } ] } ],
    "regions": [ { "id": "riyadh", "label_ar": "منطقة الرياض", "label_en": "Riyadh Region" } ],
    "governorates": [ { "id": "kharj", "label_ar": "الخرج", "label_en": "Al-Kharj" } ],
    "statuses": [ { "id": "new", "label_ar": "جديدة", "label_en": "New", "open": true } ],
    "factors": [ … ], "scopes": [ … ], "tones": [ … ],
    "signals": [ { "id": "life_safety", "label_ar": "خطر على الحياة أو السلامة", "floor": "high" } ],
    "review_reasons": { "not_a_complaint": "المستند لا يبدو شكوى" }
  },
  "providers": [
    { "id": "qwen",  "label": "Qwen3-4B (محلي)", "model": "Qwen3-4B-Instruct-2507-Q8_0.gguf",
      "n_ctx": 8192, "local": true, "status": "ready" },
    { "id": "allam", "label": "ALLaM-7B (محلي)", "model": "ALLaM-…-Q4_K_M.gguf",
      "n_ctx": 4096, "local": true, "status": "not_loaded" }
  ],
  "active_provider": "qwen",
  "limits": { "max_files": 20, "max_bytes": 104857600, "max_text_chars": 40000 }
}
```
- Lists are shortened here. City and place lists and signal patterns are not
  published: they are matching internals.
- A provider's `status` is `ready`, `loading`, `not_loaded`, `error` or
  `unconfigured`. `local: false` means the model is not on this host.
- An `openai` provider appears only when it is configured (see COMPLAINTS.md).
  Its status is a network probe, cached for 10 s.

---

### `PUT /complaints/provider`
Choose the model the worker and insights use.

**Request:** `{"provider": "allam"}`, one of the ids in `providers`.

- The choice is process-wide and applies from the next complaint.
- It is not saved: a restart returns to `CMS_LLM_PROVIDER`.
- Leaving ALLaM stops its server, which can take a few seconds.

**200 response:** `{"active_provider": "allam", "providers": [ … ]}`.
**Errors:** `400` (unknown provider), `403`, `413`, `415`.

---

### `POST /complaints/upload`
Queue one or more complaint files.

**Request:** `multipart/form-data`, with the files in repeated `files` parts.

| Limit | Value |
|---|---|
| Files per request | 20 (more: `413`) |
| File size | 100 MB each |
| Whole body | 20 × 100 MB + 1 MB, checked while it streams |
| Other form fields | 16 |

- **Types** are decided by magic bytes, not names: PDF (a `%PDF-` header, or
  one within the first 1 KB when the name ends in `.pdf`), PNG, JPEG, TIFF,
  BMP and WEBP.
- **Text.** A part whose name ends in `.txt`, or sent as `text/plain`, is text.
  It must be strict UTF-8 (a BOM is dropped), with no NUL, not blank, and at
  most 40 000 characters.
- **Duplicates.** The same bytes as an existing complaint return that
  complaint with `duplicate: true`. Nothing is queued again.

**200 response:** one entry per file, in order. A file can fail while the
others are queued.
```json
{"items": [
  {"filename": "01_sewage_overflow_school.pdf", "id": 42, "ref": "CMP-2026-000042", "stage": "queued", "duplicate": false},
  {"filename": "scan.pdf", "id": 17, "ref": "CMP-2026-000017", "stage": "done", "duplicate": true},
  {"filename": "notes.docx", "error": "نوع الملف غير مدعوم (PDF أو صورة أو نص)"}
]}
```
- **Per-file errors:** an empty file, over 100 MB, an unsupported type, text
  that is not UTF-8, blank text, text over 40 000 characters, or a file that
  could not be saved.
- **`filename`** is the display name: the last path component, without control
  or bidi characters, at most 200 characters. The file is stored as
  `<id>.<ext>`, never under this name.

**Errors (whole request):** `400` (not multipart, no `files` part, unreadable
form), `403`, `413`.

```bash
curl -s -F files=@01_sewage_overflow_school.pdf -F files=@h01_letter.txt \
  http://127.0.0.1:8100/complaints/upload
```

---

### `POST /complaints/text`
Queue a pasted complaint.

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `text` | string | Required. At most 40 000 characters (after line endings are normalised). |
| `title` | string | Optional. Shown as the file name in the register (at most 200 characters after cleaning). |

**200 response:**
`{"item": {"id": 43, "ref": "CMP-2026-000043", "stage": "queued", "duplicate": false, "filename": "…"}}`.
The same text as an existing complaint returns that complaint with
`duplicate: true`.

**Errors:** `400` (missing or blank text, title not a string), `413` (text
over 40 000 characters, body over 244 096 bytes), `415`, `403`, `500`.

---

### `GET /complaints/items`
The register. Every query parameter is optional.

| Parameter | Values |
|---|---|
| `status`, `category`, `ministry`, `priority`, `region`, `governorate` | a taxonomy id |
| `stage` | `queued` · `ocr` · `structuring` · `classifying` · `done` · `error` |
| `needs_review` | `1` / `true` or `0` / `false` |
| `q` | Up to 200 characters. A substring of the reference, subject, summary, complainant name, file name or national ID, matched after the Arabic folds (احمد finds أحمد, ١٠٩٨ finds 1098). A national ID also matches typed with spaces or dashes. |
| `sort` | `created` (default) · `priority` · `due` · `updated` |
| `order` | `desc` (default) · `asc` |
| `limit` | Default 200, at most 500 |
| `offset` | Default 0 |

- `sort=priority` breaks ties newest first.
- `sort=due` lists open processed complaints first, by deadline, then the
  rest.

**200 response:** `{"items": [ <summary>, … ], "total": 57}`. `total` counts
every match, not just the page.

```json
{
  "id": 42, "ref": "CMP-2026-000042",
  "created_at": "2026-09-24T18:02:11Z", "updated_at": "2026-09-24T18:02:25Z",
  "source": "upload", "filename": "01_sewage_overflow_school.pdf", "file_kind": "pdf", "page_count": 1,
  "stage": "done", "error": null, "status": "new",
  "subject": "طفح الصرف الصحي أمام مدرسة ابتدائية", "summary": "…", "complainant_name": "…",
  "category": "water_sewage", "subcategory": "sewage_overflow", "ministry": "mewa", "priority": "high",
  "region": "riyadh", "governorate": "riyadh_city",
  "model_category": "water_sewage", "model_ministry": "mewa", "model_priority": "high",
  "model_governorate": "riyadh_city",
  "needs_review": false, "review_reasons": [], "reviewed": false, "pending_fields": 0,
  "due_at": "2026-09-27T18:02:11Z", "provider": "qwen", "model": "Qwen3-4B-Instruct-2507-Q8_0.gguf",
  "outside_jurisdiction": false
}
```

| Key | Meaning |
|---|---|
| `stage` | Processing: `queued` → `ocr` → `structuring` → `classifying` → `done`, or `error` with the Arabic reason in `error`. |
| `status` | Follow-up, set by people: a taxonomy status id; it starts at `new`. |
| `category` … `governorate` | The **effective** values: the pipeline's until the complaint is reviewed, the reviewer's after. |
| `model_*` | The pipeline's latest output. For priority, that is after the rule floors. |
| `source`, `file_kind` | `upload` or `text`; `pdf`, `image`, `txt`, or `null` for pasted text. |
| `needs_review`, `review_reasons`, `reviewed` | `needs_review` is true while a field is pending, or while there is a reason other than `fields_unverified` and no verdict yet (or a re-analysis found `empty_text`, `not_a_complaint` or `input_truncated` the reviewer had not seen). A verdict sets `reviewed`. The reasons stay as the analysis recorded them, but `fields_unverified` is listed only while `pending_fields > 0`. |
| `pending_fields` | How many extracted fields wait for a reviewer's accept or change (`POST …/fields`): a value the text does not carry verbatim and no review yet. `0` until processed. |
| `due_at` | Received time + the priority's response time; `null` until processed. |
| `outside_jurisdiction` | The effective region is known and is not the entity's (while the region is unknown, the reason the analysis recorded). |

---

### `GET /complaints/items/{id}`
One complaint: every summary key above, plus:

| Key | Meaning |
|---|---|
| `text` | The extracted text, with `--- Page N ---` markers for OCR'd files. Citations index this string. |
| `analysis` | The pipeline's output (below); `null` until processed. |
| `timings` | `{ocr_s, structure_s, classify_s}` of the last run. |
| `national_id` | The national ID in force (the extracted one, or a reviewer's change), normalised to ASCII digits, or `null`. `complainant_name` follows a reviewer's change the same way. |
| `model_region`, `processed_at`, `attempts` | The pipeline's region; when it last finished; how many times it was claimed. |
| `file_available` | The original file can be downloaded. |
| `feedback` | `[{id, created_at, verdict, changes: {field: {from, to}}, note, reviewer}]`, newest first. |
| `events` | `[{at, kind, detail}]`, newest first. `kind` is `created`, `stage`, `processed`, `error`, `feedback`, `field_review`, `status` or `reprocess`. A `field_review` detail is `{key, action, from, to}`: the field's value before and after, which can be a name or an ID. |
| `acknowledgment` | The reply draft (Arabic, plain text), once `stage` is `done`. |

**`analysis`**
```json
{
  "structured": {
    "is_complaint": true, "subject": "…", "summary": "…", "key_facts": ["…"],
    "fields": [
      { "key": "addressed_to", "label_ar": "الجهة الموجّه إليها الخطاب",
        "value": "صاحب السمو الملكي أمير منطقة الرياض", "verified": true,
        "source": { "page": 1, "line": 3, "start": 41, "end": 76, "quote": "…" } },
      { "key": "incident_location", "label_ar": "موقع المشكلة", "value": "", "verified": false, "source": null },
      { "key": "incident_date", "label_ar": "تاريخ الواقعة", "value": "يوليو 2026م وأغسطس 2026م",
        "verified": false, "source": null, "pending": true,
        "near_source": { "page": 1, "line": 7, "start": 160, "end": 214, "quote": "…", "approx": true } },
      { "key": "against_entity", "label_ar": "الجهة المشتكى عليها", "value": "أمانة محافظة جدة",
        "verified": false, "source": null, "near_source": { … },
        "review": { "action": "change", "reviewer": "خالد", "at": "2026-09-25T09:14:02Z",
                    "from": "الجهة المختصة", "note": "" } }
    ],
    "reference_numbers": [ { "value": "…", "verified": true, "source": { … } } ],
    "region": { "id": "riyadh", "source": "place_map" },
    "governorate": { "id": "riyadh_city", "source": "place_map" },
    "addressed_to_entity": true, "input_chars": 1830, "truncated": false
  },
  "classification": {
    "category": "water_sewage", "subcategory": "sewage_overflow", "ministry": "mewa",
    "model_priority": "high", "priority": "high", "priority_source": "llm", "floors_applied": [],
    "priority_factors": ["health_risk", "vulnerable_person"], "affected_scope": "community",
    "tone": "upset", "confidence": "high", "rationale": "… الأولوية: عالية",
    "evidence": [ { "quote": "…", "verified": true,
                    "source": { "page": 1, "line": 9, "start": 212, "end": 251, "quote": "…", "approx": true } },
                  { "quote": "…", "verified": false, "source": null } ],
    "evidence_dropped": 0,
    "signals": [ { "id": "health_risk", "label_ar": "خطر صحي", "floor": "medium", "quotes": [ { … } ] } ],
    "repeat_count": 0, "input_chars": 1830, "truncated": false
  },
  "needs_review": false, "review_reasons": [], "warnings": [],
  "provider": "qwen", "model": "Qwen3-4B-Instruct-2507-Q8_0.gguf",
  "timings": { "structure_s": 3.4, "classify_s": 3.1 }
}
```
- **`fields`** always come in this order: `addressed_to`, `complainant_name`,
  `national_id`, `phone`, `email`, `city`, `district_or_address`,
  `incident_location`, `incident_date`, `submission_date`, `against_entity`,
  `requested_action`. `verified: true` means `value` is the document's own
  characters at `source`. A non-empty value with `verified: false` is the
  model's, and was not found in the text verbatim.
- **Fields come merged with the reviews.** The store returns them with the
  reviewers' decisions applied:
  - `pending: true` marks a non-empty value with `verified: false` and no
    review: it waits for `POST …/fields`. The key is absent otherwise.
  - `near_source` (on an unverified, non-empty value; absent from analyses
    stored before field reviews) is the value's probable place: a clause of
    the text the model saw holding at least half of its words, as a `source`
    with `approx: true`, or `null`. It points; it never replaces the value.
  - `review` is on every reviewed field: `{action, reviewer, at, from, note}`,
    with `from` the value before the decision. After a `change`, `value` is the
    reviewer's; `source` and `verified` stay the analysis's and describe the
    model's value.
- **`evidence`** holds every quote the model gave, in its order, identical
  ones once, each `{quote, source, verified}`. Verified: `quote` is the
  document's characters at `source`. Not verified: `quote` is the model's
  wording (at most 300 characters) and `source` its probable place
  (`approx: true`) or `null`. Items stored before the flag existed have no
  `verified` and were all verified. `evidence_dropped` is kept for older
  clients and is always `0`.
- **`source`** has the shape `/structure` uses: `page` and `line` are 1-based
  and come from the markers; `start` and `end` are UTF-16 offsets into `text`.
  `approx` marks a span matched on a clause of the text rather than verbatim:
  for a verified quote, `quote` is then the document's clause; for a
  `near_source` or an unverified quote's `source`, it is only the probable
  place.
- **`region.source` / `governorate.source`** is `place_map`, `city_map`, `llm`
  or `none`.
- **`priority_source`** is `llm`, `rule_floor` or `rule_cap`. `floors_applied`
  lists the signals whose floor set the priority. `model_priority` here is the
  model's own answer, before the rules.
- **`warnings`** are Arabic notes: clipping, a failed repeat-complainant
  lookup, too little text. Unmatched fields and quotes raise none (they are
  `pending` and `verified: false`). Analyses stored before field reviews may
  still carry the two warnings that named them; the UI does not show those.
- **`analysis.review_reasons`** leaves out `fields_unverified` once no field
  is pending, like the summary's list. `analysis.needs_review` is the
  pipeline's own answer; the complaint's `needs_review` is the one to use.
- **Empty text.** A complaint with too little text to read has the same shape,
  filled with defaults (`other` / `other` / `low`) and the reason
  `empty_text`; no model was called.

**Errors:** `404`.

---

### `GET /complaints/items/{id}/file`
The original upload, byte for byte. It is served inline, with
`Content-Disposition: inline; filename="complaint-<id>.<ext>"`, a
`Content-Type` from the stored extension, and `Cache-Control: no-store`.
**Errors:** `404` for a pasted complaint (its text is in the detail) or when
the file is gone.

---

### `POST /complaints/items/{id}/reprocess`
Queue the complaint again, on the active provider with the current
precedents.

**Request:** `{}` or `{"ocr": true}`, with `Content-Type: application/json`.
`{"ocr": true}` re-runs OCR first. That needs the original PDF or image on
disk; for a `.txt` upload or pasted text it is the same as `false`. The
default, `false`, analyses the stored text again. The previous text stays until
a new OCR result replaces it. An OCR pass that reads nothing keeps the old text
and analysis, but ends the attempt in stage `error` («لم تستخرج إعادة القراءة
أي نص؛ بقي النص السابق كما هو»); reprocess without OCR to bring it back to
`done`.

A reviewed complaint keeps the reviewer's values and deadline; only `model_*`
change. Field reviews carry over when the analysis is saved: a `change`
always, an `accept` only while the new analysis gives exactly the accepted
value. A stale accept is dropped for good, and the field is pending again if
its new value is still not in the text.

**200 response:** `{"item": <summary>}` (stage `queued`).
**Errors:** `409` (being processed), `404`, `400` (`ocr` not a boolean),
`403`, `415`.

---

### `POST /complaints/items/{id}/feedback`
A reviewer's verdict, with optional corrections.

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `verdict` | string | Required: `confirm` or `correct`. |
| `changes` | object | Optional. Any of `category`, `subcategory`, `ministry`, `priority`, `region`, `governorate`, as taxonomy ids. `subcategory` may be `null` or `""` to clear it. |
| `note` | string | Optional, at most 1 000 characters. |
| `reviewer` | string | Optional, at most 100 characters. Free text, not authenticated. |

- Allowed only once `stage` is `done` (else `409`).
- Changes apply with either verdict. Only values that differ are recorded, as
  `{from, to}`.
- A subcategory must belong to the resulting category (`400`). Changing the
  category drops a subcategory of the old one.
- Naming a governorate without a region sets the region to the entity's.
  Another region clears the governorate, and naming a governorate together
  with another region is `400` («المحافظة لا تتبع المنطقة المختارة»).
- Region `unknown` also clears the governorate, even when the region already
  was unknown, and naming a governorate together with it is the same `400`.
- A priority change recomputes `due_at` (received time + the new response
  time).
- The complaint becomes `reviewed`, and `needs_review` is cleared unless a
  field is still pending: a verdict does not settle fields.
- A `correct` verdict that changed something makes the complaint a few-shot
  precedent for the complaints processed after it.

**200 response:** the updated complaint, as `GET /complaints/items/{id}`.
**Errors:** `400`, `403`, `404`, `409`, `413`, `415`.

```bash
curl -s -H "Content-Type: application/json" \
  -d '{"verdict":"correct","changes":{"category":"transport","ministry":"transport"},"reviewer":"سارة","note":"طريق بين مدينتين"}' \
  http://127.0.0.1:8100/complaints/items/42/feedback
```

---

### `POST /complaints/items/{id}/fields`
A reviewer's decision on one extracted field: accept the value as it is, or
change it. Meant for the pending fields (`pending: true`), the values the
model worded its own way; nothing is sent to the model.

**Request:** `application/json`

| Field | Type | Notes |
|---|---|---|
| `key` | string | Required. One of the field keys of `analysis.structured.fields` (the twelve listed above). Reference numbers are not fields. |
| `action` | string | Required: `accept` or `change`. |
| `value` | string | `change` only, and then required. Trimmed; at most 500 characters; `""` clears the field. Ignored with `accept`. |
| `reviewer` | string | Optional, at most 100 characters. Free text, not authenticated. |
| `note` | string | Optional, at most 1 000 characters. |

- Allowed only once `stage` is `done` (else `409`, also while the complaint is
  queued again for reprocessing).
- `accept` records the field's current value as right; `change` records
  `value`. Either way `from` is the value in force before (`""` when there was
  none). The decision replaces any earlier one on the same field, and it is
  stored as `{action, value, from, reviewer, note, at}`.
- Any of the fields can be reviewed, a verified or an empty one too; the UI
  offers it only for pending ones.
- An event `field_review` `{key, action, from, to}` is added.
- `complainant_name` and `national_id` follow the new value in the register
  (the ID normalised to ASCII digits without spaces), so search, the CSV and
  the reply draft use it, and repeat detection on other complaints finds it.
  The region and governorate do not move with a place field; correct them
  with `…/feedback`.
- `pending_fields` is recomputed, and with it `needs_review` and whether
  `fields_unverified` is listed. A field review does not set `reviewed`.

**200 response:** the updated complaint, as `GET /complaints/items/{id}`,
with `acknowledgment`.

**Errors:**

| Status | When |
|---|---|
| `400` | `key` missing, empty or not a string; a key the analysis has no field for («الحقل غير موجود في بيانات الشكوى»); `action` not `accept` or `change`; `change` without a string `value`; `value` over 500 characters; `reviewer` or `note` not a string or too long; invalid JSON or not an object |
| `403` | Cross-site: a foreign `Origin`, or `Sec-Fetch-Site` other than `same-origin` or `none` |
| `404` | Unknown or non-numeric id |
| `409` | Not processed yet («لا يمكن مراجعة الشكوى قبل اكتمال معالجتها») |
| `413` | Body over 64 KB |
| `415` | Not `Content-Type: application/json` |

No refused request changes anything.

```bash
curl -s -H "Content-Type: application/json" \
  -d '{"key":"against_entity","action":"change","value":"أمانة محافظة جدة","reviewer":"خالد"}' \
  http://127.0.0.1:8100/complaints/items/13/fields
curl -s -H "Content-Type: application/json" \
  -d '{"key":"incident_location","action":"accept","reviewer":"خالد"}' \
  http://127.0.0.1:8100/complaints/items/13/fields
```

---

### `POST /complaints/items/{id}/status`
Set the follow-up status, at any stage.

**Request:** `{"status": "referred", "note": "…"}`. `status` is a taxonomy
status id; `note` is optional, at most 1 000 characters. Both are recorded in
`events`. Setting `referred` sends nothing anywhere.

**200 response:** the updated complaint.
**Errors:** `400`, `403`, `404`, `413`, `415`.

---

### `DELETE /complaints/items/{id}`
Erase the complaint: its row, feedback, events and stored file. SQLite's secure
delete and a WAL checkpoint remove the old copies from the database files. It
cannot be undone.

**200 response:** `{"deleted": true}`.
**Errors:** `409` (being processed), `404`, `403`.

---

### `GET /complaints/queue`
**200 response**
```json
{"queued": 3,
 "processing": {"id": 44, "ref": "CMP-2026-000044", "stage": "classifying", "filename": "…"},
 "errors": 1, "worker": "running", "active_provider": "qwen"}
```
- `processing` is `null` when the worker is idle.
- `errors` counts the complaints in stage `error`.
- `worker` is `stopped` when this process does not run the worker: another app
  owns the same data directory, or this one is shutting down.

---

### `GET /complaints/analytics`
The dashboard's aggregates, counted from the register.

| Key | Content |
|---|---|
| `generated_at` | When they were computed. |
| `totals` | `all`, `done`, `queued`, `processing`, `error`, `open`, `closed`, `needs_review`, `fields_pending` (processed complaints with `pending_fields > 0`), `reviewed`, `overdue`, `critical_open`, `outside_jurisdiction` |
| `by_category`, `by_ministry`, `by_region`, `by_governorate`, `by_status`, `by_tone`, `by_scope` | `[{id, count}]`, most frequent first |
| `by_priority` | `[{id, count}]` for `critical`, `high`, `medium`, `low`, zeros included |
| `trend` | `[{date, count}]`: one row per Riyadh day over the last 30 days, counting every received complaint |
| `category_priority` | `[{category, critical, high, medium, low}]` |
| `sla` | `{on_track, due_soon, overdue}` over open processed complaints; `due_soon` is within 24 hours |
| `processing` | `{avg_total_s, avg_ocr_s, avg_structure_s, avg_classify_s}` |
| `model_quality` | `reviewed`; `category_agreement`, `ministry_agreement`, `priority_agreement`, `region_agreement`, `governorate_agreement` (0–1, or `null` when nothing is reviewed); `top_corrections: [{field, from, to, count, refs}]` (at most 10, with up to 5 refs each, newest reviewed first) |
| `providers` | `[{id, count}]`: which model processed how many |
| `signals` | `[{id, count}]`: complaints per rule signal |

- Only `totals.all`, `queued`, `processing`, `error` and `trend` include
  unprocessed complaints; the rest cover processed ones.
- `top_corrections` is net: per reviewed, processed complaint, the model's
  latest value against the effective value. `count` is the number of
  complaints, and a correction later reverted counts nothing. A complaint
  being reprocessed is left out until it is done again.

---

### `POST /complaints/insights`
Insights and recommendations for the Emirate's leadership, written by the
active model from pre-computed figures and up to 30 recent complaints (see
COMPLAINTS.md).

**Request:** `{}` with `Content-Type: application/json`. A body without that
header gets `415`.

The call is synchronous and holds the model for one long answer, so use a
generous client timeout. One run at a time.

**200 response**
```json
{"insights": {"headline": "…",
              "insights": [{"title": "…", "detail": "…", "refs": ["CMP-2026-000012"]}],
              "recommendations": [{"ministry": "mewa", "action": "…", "priority": "high"}],
              "watch": ["…"], "provider": "qwen"},
 "provider": "qwen", "generated_at": "2026-09-24T20:15:03Z"}
```
- At most 5 insights, 5 recommendations and 4 watch items.
- `refs` only ever name complaints that were sent to the model.
- The result is not stored.

**Errors:** `409` (no processed complaint yet, or a run in progress), `503`
(model busy or unavailable), `502` (no usable answer), `500`, `403`, `415`.

---

### `GET /complaints/export.csv`
Every complaint matching the same filter, search and sort parameters as
`/complaints/items`, with no paging.

- **Format:** UTF-8 with a BOM (so Excel reads the Arabic), CRLF rows,
  `Content-Disposition: attachment; filename="complaints.csv"`,
  `Cache-Control: no-store`. `X-Total-Count` gives the number of rows.
- **Columns:** المرجع، تاريخ الاستلام، الموضوع، مقدم الشكوى، التصنيف، التصنيف
  الفرعي، الجهة المختصة، الأولوية، المنطقة، المحافظة، الحالة، المهلة، تحتاج
  مراجعة، مصدر الأولوية، الملف.
- **Values** are Arabic labels, not ids. Times are Riyadh local time
  (`YYYY-MM-DD HH:MM`).
- **Formulas are defused.** A cell that starts with `=`, `+`, `-`, `@`, a tab
  or a CR is prefixed with `'`, so a spreadsheet will not run it as a formula.

**Errors:** `400` (an invalid filter).

---

### Limits and timing

| What | Limit |
|---|---|
| Files per upload | 20 |
| File size | 100 MB |
| Text (`.txt` or pasted) | 40 000 characters |
| PDF pages | 30 (`CMS_MAX_PAGES`). Checked by the worker before OCR: a longer PDF fails with «عدد صفحات الملف … يتجاوز الحد المسموح». |
| JSON body | 64 KB; pasted text 244 096 bytes |
| `note` / `reviewer` / `q` / file name or title | 1 000 / 100 / 200 / 200 characters |
| Changed field value (`…/fields`) | 500 characters |
| Register page | 500 rows (default 200) |

- **Processing is asynchronous.** Poll `GET /complaints/queue` or the item
  until `stage` is `done` or `error`.
- **Timing.** A complaint takes about 6–7 s with Qwen, plus OCR for a PDF or
  image (~7–9 s a page warm).
- **One at a time.** The worker processes one complaint at a time and shares
  the Qwen and Surya slots with the rest of the API, so interactive calls and
  the queue slow each other down.

```python
import time, requests
BASE = "http://127.0.0.1:8100/complaints"
text = open("h01_letter.txt", encoding="utf-8").read()
item = requests.post(f"{BASE}/text", json={"text": text}, timeout=30).json()["item"]
while True:
    d = requests.get(f"{BASE}/items/{item['id']}", timeout=30).json()
    if d["stage"] in ("done", "error"):
        break
    time.sleep(2)
print(d["ref"], d["category"], d["ministry"], d["priority"], d["review_reasons"], d.get("error"))
```

Run the complaint tests (`test_complaints_ui` needs node on `PATH`), then
`node --check` on each JS file separately (see COMPLAINTS.md):

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -m unittest test_complaints_taxonomy test_complaints_llm test_complaints test_complaints_store test_complaints_api test_complaints_ui test_pdfium_lock test_comparison test_comparison_api test_regulations_client -q
```


## Data verification (Wathq)

The **التحقق من البيانات** tab's backend. It queries every product
[Wathq](https://developer.wathq.sa) publishes: 8 products and 47 queries,
listed by `GET /wathq/catalog` and run through `POST /wathq/query`.
These are the app's only routes that reach the internet, and **every lookup that
reaches Wathq is billed** to the key's package. Setup, settings, costs and terms
are in [`WATHQ_INTEGRATION.md`](WATHQ_INTEGRATION.md).

### Conventions

- **Local only.** `/wathq/status` and the lookup answer `403` unless the
  connection comes from this machine (a loopback peer) *and* the request is
  addressed to `127.0.0.1`, `localhost` or `::1`. The `Host` header alone is not
  trusted, because a LAN client can set it when `APP_ALLOWED_HOSTS` opens the app
  to a network. `WATHQ_ALLOW_REMOTE=1` lifts this. The assets (`/wathq/ui.js`,
  `/wathq/ui.css`) load from any allowed host.
- **Same origin.** A lookup needs `X-Wathq-Request: 1` and
  `Content-Type: application/json`. It is refused (`403`) when `Sec-Fetch-Site`
  is anything but `same-origin` or `none`, or when `Origin` is not the app's
  own. The custom header forces a CORS preflight that the app never answers, so
  another website open in the same browser cannot trigger a paid lookup.
- **Errors** are `{"error": "<Arabic message>"}`, plus `"code"` (Wathq's dotted
  error code, e.g. `404.2.1`) when Wathq sent one, and `"detail"`: Wathq's own
  wording for the error when it sent any (one line, any run of 9+ digits kept
  only by its last four). Once a request has gone to Wathq, the body also
  carries `"sent"`, because a failed call may still be billed. Responses are
  `Cache-Control: no-store`.
- **The key** never appears in a response, a log line or an error.

### `GET /wathq/status`

```json
{"configured": true, "env": "production", "sent": 3, "cache_seconds": 900, "reason": ""}
```

| Field | Meaning |
|---|---|
| `configured` | A usable key is set (`WATHQ_API_KEY`, or the file in `WATHQ_API_KEY_FILE`). |
| `env` | `production` or `sandbox` (`WATHQ_ENV`). Empty when the settings are invalid. |
| `sent` | Requests this process has sent to Wathq since it started, failed ones included. Each may be billed. Wathq's own balance is on the portal. |
| `cache_seconds` | How long an answer is reused (`WATHQ_CACHE_SECONDS`). |
| `reason` | Why `configured` is false, in Arabic: no key, an unreadable key file, a malformed key, or an invalid setting. |

### `GET /wathq/catalog`

Every product and query the tab offers, from `templates/wathq/catalog.yaml`,
for the current environment. Same local-only rule as `/wathq/status`.

```json
{"env": "production", "products": [{
  "id": "cr", "label": "السجل التجاري", "label_en": "Commercial Registration", "sandbox": true,
  "endpoints": [{
    "id": "cr.fullinfo", "label": "البيانات الكاملة للسجل التجاري", "description": "…",
    "price": 12.0, "lookup": false, "view": "generic", "available": true, "language": true,
    "converts": true, "legacy_ok": true, "one_of": [],
    "inputs": [{"name": "id", "kind": "company_number", "label": "…", "required": true,
                "personal": false, "choices": [], "required_if": {}, "pattern_by": {},
                "hint": ""}]}]}]}
```

| Field | Meaning |
|---|---|
| `price` | Wathq's prepaid SAR per successful call; `0` free, `-1` not listed. |
| `lookup` | A reference list (code table) rather than data about one entity. |
| `available` | `false` when the product or query has no sandbox and `WATHQ_ENV=sandbox`. |
| `language` | The query takes `language` (`ar`/`en`). |
| `converts` | An old CR number may be typed for the unified number; it costs one extra conversion request. |
| `legacy_ok` | Commercial Registration only: when an old CR number has no unified number (a struck-off record), the query is retried once with the old number. |
| `one_of` | Input names of which at least one is required. |
| `inputs[].required_if` | `{other input: [values]}`: required when the other input has one of these values (a nationality for passports). |
| `inputs[].pattern_by` | `{other input: {value: regex}}`: a stricter pattern for some values of the other input (a national ID is `1` + 9 digits). |
| `inputs[].kind` | The validator: `unified_number`, `company_number`, `cr_number_any`, `person_or_entity_id`, `id_type`, `deed_number`, `attorney_code`, `drug_id`, `investor_id`, `permission_id`, `copy_number`, `nationality`, `boolean`. |
| `inputs[].personal` | The value identifies a person: never echoed back or logged. |
| `inputs[].choices` | The allowed values (`value`, Arabic `label`) of an enum, for the current environment (the sandbox accepts fewer ID types). |

The catalog never includes which response fields are masked.

### `POST /wathq/query`

Runs one catalog query. Same guards as the company-contract lookup
(`X-Wathq-Request: 1`, same origin, local only, JSON, one at a time). Body at
most 8 KB:

```json
{"endpoint": "cr.fullinfo", "inputs": {"id": "7001272475"}, "language": "ar"}
```

`inputs` holds the catalog input names only; an unknown name is a `400`.
Values are cleaned as the catalog says (Arabic digits folded, separators
dropped, enums checked), and an old CR number is converted first where the
query converts. Header parameters (`employee.info`'s `id`, `cr.related` and
`cr.owns`) are sent to Wathq as headers.

Response `200`:

```json
{
  "source": "wathq", "endpoint": "cr.fullinfo", "label": "البيانات الكاملة للسجل التجاري",
  "product": "السجل التجاري", "env": "production", "price": 12.0,
  "query": {"inputs": {"id": "7001272475"}, "converted": false, "national_number": "",
            "legacy_used": false},
  "cached": false, "calls_used": 1, "sent": 7, "fetched_at": "2026-10-03T23:29:00+03:00",
  "view_type": "generic",
  "view": [
    {"type": "field", "label": "رقم السجل التجاري", "value": "1010711252"},
    {"type": "group", "label": "بيانات الاتصال", "children": [
      {"type": "field", "label": "رقم الجوال", "value": "•••••1101"}]},
    {"type": "cards", "label": "قائمة الشركاء / مالك المؤسسة", "items": [
      {"type": "group", "label": "قائمة الشركاء / مالك المؤسسة (1)", "children": ["…"]}]},
    {"type": "list", "label": "قائمة أنشطة السجل التجاري", "items": ["…"]},
    {"type": "table", "label": "…", "columns": ["…"], "rows": [["…"]]}
  ]
}
```

- **`view`** is the answer as display nodes: `field` (label, value), `group`
  (label, children), `list` (label, items), `table` (label, columns, rows) and
  `cards` (label, items of groups). Labels are Arabic, taken from Wathq's spec.
  Every value is a string; booleans read نعم / لا.
- **Personal data is masked on the server**: identity, iqama, passport, border
  and phone numbers keep their last four characters, an e-mail its first letter
  and domain, a date of birth its year. In free text every run of nine or more
  digits keeps its last four. A list cut at 300 items carries `truncated: true`
  and `total`.
- **`query.inputs`** echoes only non-personal inputs. A person's ID never comes
  back. `legacy_used` is true when a struck-off record was found by its old CR
  number.
- **`language`** is ignored (and doesn't split the cache) for queries that don't
  take it.
- **`contracts.info`** answers with `view_type: "contract"` and the dedicated
  company-contract shape of `POST /wathq/company-contract` instead of `view`.
- Reference lists are cached for a day, other answers for `WATHQ_CACHE_SECONDS`.

Errors are as for the company-contract lookup, plus `400` for an unknown
endpoint, an unknown input, a missing required input, a missing one-of input,
or a query unavailable in the sandbox.

### `POST /wathq/suggest`

Which Wathq services can verify this document. The page calls it as soon as
`/structure` succeeds, with the structured result; no Wathq call is made.

```json
{"struct": { "sections": [ ... ] }}
```

The structured result is flattened to `label: value` lines and the local model
(the structurer, Qwen) is asked one question: these lines, the list of services
(name + the data each returns), which service can verify the document, one
name or `None`. The chosen service is removed and the question is repeated
until `None`. The order of the answers is the ranking: the tab ticks the first
and lists the rest unticked; the user approves before anything is sent.

```json
{"pairs": 23, "env": "production",
 "services": [
  {"id": "power_of_attorney", "label": "الوكالة", "endpoint": "attorney.info",
   "endpoint_label": "التحقق من بيانات الوكالة الشرعية", "price": 5, "available": true,
   "inputs": {"code": "4317608", "principalId": "1023456789"},
   "inputs_spec": [ ... the endpoint's inputs as in /wathq/catalog ... ],
   "one_of": ["principalId", "agentId"], "converts": false}]}
```

- `inputs` are read from the document's own lines by label (رقم الوكالة, رقم
  هوية الموكل, السجل التجاري…), digits folded; nothing is invented, and a key
  the document lacks is simply absent so the tab asks for it.
- The same local-only and same-origin guards as `/wathq/query` apply; the
  body is capped at 256 KB.
- `503` with a plain message when the local model is busy or not loaded;
  `400` when `struct` is not an object.

### `POST /wathq/compare`

What the document says against what one register answered. The tab calls it
after each query it ran from the suggestions block; no Wathq call is made.

```json
{"struct": { "sections": [ ... ] }, "result": { ...a /wathq/query answer... }}
```

Both sides are flattened to `label: value` lines. Numbers, dates and masked
IDs (last four digits) are compared exactly by code; the remaining register
lines go to the local model in one question, which answers only with line ids
and a verdict. When the model links a numeric or date line to a document line,
the two values are still compared exactly by code.

```json
{"document_lines": 23,
 "counts": {"matches": 5, "partly_matches": 1, "differs": 0, "not_in_document": 3, "not_compared": 2},
 "rows": [
  {"label": "حالة الوكالة", "register": "سارية", "document": "سارية", "document_label": "حالة الوكالة",
   "verdict": "matches", "verdict_ar": "يطابق", "by": "model"},
  {"label": "جهة الإصدار", "register": "كتابة العدل الأولى", "document": "", "document_label": "",
   "verdict": "not_in_document", "verdict_ar": "غير وارد في المستند", "by": "model"}]}
```

- Verdicts: `matches` يطابق · `partly_matches` يطابق جزئيًا · `differs` يختلف
  عن سجل وثق · `not_in_document` غير وارد في المستند · `not_compared` لم تتم
  مقارنته (the model gave no ruling). `by` is `code` or `model`.
- The same guards as `/wathq/query`; body capped at 512 KB; `503` when the
  model is unavailable, `400` when `struct` or `result` is not an object.

### `POST /wathq/company-contract`

Request (at most 4 KB):

```json
{"number": "7001272124", "language": "ar"}
```

| Field | Meaning |
|---|---|
| `number` | The unified national number (10 digits starting with `70`) or an old commercial-registration number (10 digits starting with `1`–`6`, the issuing office's code; `71…`–`79…` is refused before any call). Arabic-Indic and Persian digits, spaces, dashes, dots and bidi marks are accepted and removed. |
| `language` | `ar` (default) or `en`: the language Wathq answers in. |

An old CR number is first converted with the Commercial Registration product
(`GET /commercial-registration/crNationalNumber/{cr}`), then the contract is
fetched (`GET /company-contract/info/{crNationalNumber}`). That is two billed
requests; a unified number needs one. The conversion is production-only,
because Wathq's sandbox has no such endpoint. Answers and conversions are cached
in memory per environment, number and language.

Response `200`:

```json
{
  "source": "wathq", "product": "company_contract", "env": "production",
  "query": {"input": "1023236575", "kind": "cr", "national_number": "7001272124", "converted": true},
  "cached": false, "calls_used": 2, "sent": 5,
  "fetched_at": "2026-10-02T15:40:12+03:00",
  "contract": {"copy_number": "1", "date": "2023-01-24"},
  "entity": {"national_number": "7001272124", "cr_number": "1023236575", "name": "…",
             "name_language": "اللغة العربية", "entity_type": "شركة", "legal_form": "ذات مسؤولية محدودة",
             "characters": ["شخص واحد"], "duration": "25", "headquarters": "الرياض",
             "license_based": false, "license_issuer": ""},
  "capital": {"currency": "ريال سعودى",
              "contribution": {"type": "نقدي", "cash": "100000", "in_kind": "0", "share_value": "1000",
                               "cash_shares": "100", "in_kind_shares": "0"}},
  "fiscal_year": {"first": false, "calendar": "ميلادي", "end": "2024/12/31"},
  "parties": [{"name": "…", "type": "فرد سعودي", "id_masked": "••••••0001", "id_type": "هوية وطنية",
               "nationality": "السعودية", "roles": ["شريك"], "cash_shares": "100", "in_kind_shares": "0",
               "total_shares": "100", "profit_pct": "100", "loss_pct": "100", "cr_number": "", "license_no": ""}],
  "management": {"structure": "مدير", "dismissal": "",
                 "managers": [{"name": "…", "type": "سعودى", "id_masked": "••••••0003", "id_type": "هوية وطنية",
                               "nationality": "السعودية", "positions": ["مدير"], "licensed": false}]},
  "activities": [{"code": "4711", "name": "…"}],
  "notification_channels": ["رسائل نصية"],
  "decisions": [{"name": "زيادة رأس مال الشركة", "approve_pct": "75", "note": ""}],
  "decisions_note": "",
  "profit_set_aside": {"pct": "10", "purpose": "…"},
  "articles": [{"part": "", "title": "", "text": "…"}]
}
```

- **Values are strings**, booleans or `null`, whatever type Wathq used.
  Amounts and counts are plain digits (`"100000"`), and `"0"` is kept.
- **Identity numbers of people** (partners, managers, guardians) are masked to
  their last four digits on the server. The full number never reaches the
  browser. A partner that is itself a company keeps its `cr_number`.
- **A whitelist.** Fields not listed here, such as Wathq's internal ids or the
  board details, are dropped. Text is capped (a clause at 6,000 characters, 300
  clauses, 200 rows per list), and Swagger placeholder values (`"string"`) are
  removed.
- **The capital** has `contribution` (shares), `stock` (with `stocks[]`), or
  both, depending on the legal form. A party may carry a `guardian` object
  (`name`, `id_masked`, `id_type`, `nationality`, `is_father`).
- **`calls_used`** is the number of requests this lookup sent to Wathq. `0`
  means it was answered from the cache. `cached` refers to the contract alone.

| Status | When |
|---|---|
| `400` | `number` is not a unified or CR number, `language` is not `ar`/`en`, the body isn't a JSON object, or Wathq rejected the number. In the sandbox, an old CR number is refused because conversion is unavailable there. |
| `403` from the conversion | The key's app isn't subscribed to Commercial Registration, which old CR numbers need. Answered as `502` with a message naming that product. |
| `403` | Not local, cross-site, or missing `X-Wathq-Request: 1`. |
| `404` | Wathq has no data for the number (`code` `404.2.1`). |
| `409` | Another lookup is running. One runs at a time. |
| `413` / `415` | The body is over 4 KB, or isn't JSON. |
| `429` | Wathq's rate limit or quota was hit. |
| `502` | Wathq refused the key (`401.1.1`), the key's app isn't subscribed to the product (`403`), the TLS certificate failed verification, or the answer was unusable. |
| `503` | No key, invalid settings, or Wathq is unreachable. |
| `504` | Wathq didn't answer within `WATHQ_TIMEOUT`. |

```bash
curl -s -X POST http://127.0.0.1:8100/wathq/company-contract -H "Content-Type: application/json" -H "X-Wathq-Request: 1" -d '{"number":"7001272124"}'
```

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
