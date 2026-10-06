# Verifying documents against the official registers — concept for approval

**The flow.** After OCR and structuring, the local model rereads the document with a *verification menu* and returns a checklist: which registers the document touches, which keys and facts it prints, each value copied exactly as printed, and where; it lists only what it found. A review screen shows it as a form; one button runs every check whose key is present; the model then compares document against register fact by fact; everything is shown.

**The menu.** What the eight registers can verify, in plain language, grouped by the *thing* a document is about (company, contract, national address, attorney, deed, employee, foreign investor, drug), not by endpoint. Per thing: the keys that open the register and the comparable facts, each with Wathq's Arabic label and comparison kind. Generated from the specs and the query catalogue: a new field is a menu entry, not code. The full menu drives review, run and comparison; a compact Arabic rendering goes into the prompt, rarely printed facts left register-only. Long texts (attorney clauses, contract articles) are never asked of the model, only compared with what the user pastes in review. Annex: `VERIFICATION_MENU_TABLE.md`.

**The review screen.**
- Each line shows item, copied value and quote or page/line, accepted as is.
- A misread value is corrected in place, but only to text that appears in the document.
- A missing key is named in plain words next to a field to type it.
- Unticked checks are neither sent nor compared.
- CR and unified numbers are asked once across things; a person's ID is not (a company's partner and an employee are different people).

**Run.** Code maps "thing + facts present" to the queries behind the menu; the user never picks an endpoint. An old CR number is silently converted to the unified number; a struck-off record is retried with the old one. A person's ID for the company register comes from the partner or manager line, its type from the word beside the number; a passport also needs the nationality. Everything that has a key is looked up — acceptance in review is the consent; query cost is ignored. A register error is worded as a fact about the lookup, never as a judgement on the document.

**Compare and show.** The model gives the verdict per fact from a fixed set: *matches, partly matches, differs, not in the document, not in the register*. Code first normalises digits, calendars and separators, so numbers and dates are judged on exact equality; names, free text and lists by the model. A difference is always worded as "differs from the register". Shown: the full register answers, the comparison table (document value, register value, verdict, place) and failed lookups with reasons. Identity numbers are masked; their exact comparison runs server-side first.

**Local, licensed, protected.** Everything runs locally and starts automatically after structuring; only the keys (a CR, deed or ID number) leave the machine, sent to Wathq under the user's own licence, never logged or echoed.

**Open points.** Where the review screen lives (recommended: the verification tab's first screen); the Arabic wording of verdicts and failed lookups.

**Order of work.** Menu step and checklist; review screen; run and compare; report in results, chat and export.
