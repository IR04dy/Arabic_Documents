# Related regulation clauses

After text extraction, the **استرجاع المواد ذات الصلة** button appears enabled in the document-analysis panel. It searches the original extracted text. Accepted proofreading changes do not change this input.

The results dialog groups candidate clauses by regulation. Each result includes unchanged source text, source pages, review/amendment flags, up to three best matching passages from the uploaded text, and an action to highlight that passage. The source link opens the local regulation PDF at the cited page. Closing an in-progress dialog or pressing Cancel aborts the browser request. Loading another document invalidates previous results and in-flight responses.

## Services

- Extraction UI and adapter: `http://127.0.0.1:8100`.
- Regulations service: `http://127.0.0.1:8765` (override using `REGULATIONS_API_URL`, which must be a localhost HTTP origin).
- PostgreSQL sample: 8 documents, 190 chunks; local Ollama embeddings.

Start the regulations service from `D:\Yousef\Riyadh_Regulations` using `./run-rag.ps1`. PostgreSQL and Ollama must be running. Start the extraction app normally using its existing environment. Restart the app and refresh the browser after changing `ui.html` because the application reads it at startup.

## API

`POST /regulations/related`, JSON `{"text":"--- Page 1 ---\n..."}`, returns newline-delimited JSON:

- `type: progress`: preparation/search stage, completed passages, total passages.
- `type: result`: all qualifying unique clauses, corpus scope, coverage, vector status, and relevance-policy version.
- `type: error`: the stream failed; the UI offers Retry.

The adapter forwards to `POST /related` on the regulations service. Empty input returns 400; text over 200,000 Unicode characters or requests over 1 MiB return 413 without searching or truncating. One retrieval runs at a time; concurrent requests receive 429. Service outages return an actionable 503 message.

`GET /regulations/source/{source_index}` streams a PDF from the active corpus. It never accepts a filesystem path or arbitrary URL. Removed document 02 returns 404.

## Retrieval policy

The service splits at page/paragraph boundaries into passages of at most 1,000 characters with 120-character overlap. Physical page numbers come from the extractor's `--- Page N ---` markers; text without markers has unknown page numbers. Exact source offsets are returned both as Python character offsets and browser UTF-16 offsets.

Every passage is compared with all 190 active chunks using local multilingual embeddings and lexical overlap weighted by corpus frequency. No top-k cap is applied to qualifying clauses. Duplicate clauses are removed; results are ranked, while each retains its best three input matches and total matching-passage count. Keyword matching considers all query terms rather than just the first 64.

`sample-relevance-v1` admits exact substantial excerpts, or a cosine similarity of at least 0.50 and a cosine-plus-lexical score of at least 0.60. The lexical contribution is capped at 0.12. With embeddings unavailable, lexical-only results require at least two shared meaningful terms and a lexical score of 0.40; the UI marks these results as partial and offers Retry. The numerical scores are ranking signals, not confidence percentages or legal-applicability judgments.

These initial cutoffs were checked against synthetic Arabic positive queries and unrelated negative queries. They remain a development baseline, not an exhaustive recall benchmark. “All” refers to all clauses qualifying under this policy in the active sample, not all legally applicable regulations. Existing unresolved amendments in documents 05 and 09 stay visible.

Uploaded text is not written to a retrieval cache or database. Requests are streamed through local services. The corpus fingerprint is checked before returning results; source review revisions must match indexed revisions.

## Verification

```powershell
# In Arabic_Text_Extraction, using its existing environment:
.\.venv\Scripts\python.exe -m unittest test_regulations_client -v

# In Riyadh_Regulations, using its configured Python:
python -m unittest discover -s tests -v
python -m rag.cli validate
```

Tests cover blank/oversized requests, progress/error streams, outage handling, PDF routing, passage coverage and page markers, late query terms, exact original text, and low-relevance rejection. End-to-end checks also exercise long text with a relevant final page, Unicode highlighting offsets, all-clause coverage and source PDF access.
