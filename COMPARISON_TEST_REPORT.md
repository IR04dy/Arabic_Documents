# Document comparison verification

Completed: 22 September 2026.

## Automated regression tests

Command (project virtual environment):

```powershell
.\.venv\Scripts\python.exe -m unittest test_comparison test_comparison_api test_regulations_client -q
```

**35 tests passed.** Checks cover long-document tail coverage, page markers,
UTF-16 citation offsets, Arabic numbers, exact numeric edits, identical text,
unrelated documents, invalid quotations, constrained output schemas, one repair
attempt, partial/truncated model output, empty OCR pages, cancellation,
reordered sections, lexical fallback, input limits, stream errors, concurrent
requests, disconnect cleanup, assets and the existing regulations integration.

Python compilation, JavaScript syntax validation and `git diff --check` passed.

## Live integration checks

| Check | Result |
|---|---|
| Changed amount (1000 → 1500), duration (30 → 45) and permission/negation | All three differences detected with verified source evidence; no failed passes or rejected findings in the final run |
| Unrelated recipe and printer instructions | Recognized as different subjects; no invented conflict; final report complete with seven supported findings |
| PDF → existing OCR → comparison | One-page sample extracted 1,079 characters; identical-text comparison passed with page-1 citations |
| Reordered sections using real local Ollama embeddings | All four sections matched their reordered counterparts; full coverage on both sides |

The final running endpoint was retested after the app restart. It uses the
existing local Qwen3 model and local `qwen3-embedding:0.6b` for larger alignments.

## Browser checks

- Separate Arabic comparison tab and two independent uploads work.
- TXT inputs produce a completed report through the live backend.
- Source links open the correct document and highlight the exact quotation.
- Result filters update the displayed findings.
- Switching tabs preserves comparison state and leaves the analysis workspace independent.
- JSON download was verified by parsing the actual downloaded file and checking
  its fixture names and all eight expected findings. The browser automation's
  download-event notification timed out, but the file was downloaded correctly.
- Cancellation restores controls and suppresses late results from the cancelled run.
- A subsequent comparison completes normally after cancellation.
- No browser JavaScript errors observed.

## Scope

These checks validate the tested workflows and examples, not exhaustive LLM
accuracy. Quote provenance is checked mechanically; interpretation still needs
review. Comparison covers extracted text, not images, signatures or visual layout.
Larger documents use candidate passage alignment rather than exhaustive all-pairs
comparison. PDFs, supported raster images and UTF-8 TXT are accepted; the text
limit is 40,000 characters per document. No separate live image-upload or mobile
viewport test was performed in this verification.
