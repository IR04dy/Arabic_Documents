# Synthetic complaint samples (CMS tab)

Fourteen Arabic, Saudi-government-style documents used to demo and test the
«إدارة الشكاوى» tab end to end: OCR → structuring → classification and
priority → register and analytics.

The CMS is the complaints desk of **إمارة منطقة الرياض** (Riyadh Region
Principality). So every document is addressed to the Emirate, and the task is
to decide which ministry the Emirate refers it to. Twelve documents concern a
place in one of the region's governorates. Two are deliberately outside the
Emirate's jurisdiction. In #13, a Jeddah resident writes about Jeddah. In
#14, a family that lives in مدينة الرياض writes about a park in الطائف.

> **Everything here is fictional.** All people, national/iqama IDs, phone
> numbers, e-mail addresses (`@example.com`), reference, account and record
> numbers, schools, hospitals, shops and companies were made up for testing.
> Any resemblance to a real person or business is coincidental. Government
> bodies are named only as the addressee a citizen would write to; the
> documents are not theirs.

Two separate held-out evaluation sets of plain-text complaints live in
[`heldout/`](heldout/README.md) and [`heldout_v2/`](heldout_v2/README.md).
Both were written without reading the prompts. Prompts must never be tuned on
either of them. The samples in this folder, #14 included, may be used for
tuning.

## Contents

| File | Scenario | Addressee | Category / ministry | Priority | Governorate |
|---|---|---|---|---|---|
| `01_sewage_overflow_school.pdf` | Sewage overflow outside a primary school, حي النسيم | Prince | water_sewage / mewa | high | riyadh_city |
| `02_medical_error_child_kharj.pdf` | ER misdiagnosis at مستشفى الخرج العام, child now in ICU | Prince | health_services / health | critical | kharj |
| `03_unpaid_wages_riyadh.pdf` | The Emirate's table-style «نموذج تقديم شكوى»: 4 months unpaid wages, حي السلي | form | labor / hrsd | high | riyadh_city |
| `04_pothole_majmaah.pdf` | Pothole that damaged tyres, حي الفيصلية | Deputy | municipal_services / municipal | medium | majmaah |
| `05_cafe_noise_diriyah.pdf` | Café noise until dawn, حي العودة | Prince | municipal_services / municipal | low | diriyah |
| `06_power_outage_oxygen_zulfi.pdf` | Repeated outages, elderly father on home oxygen | Prince (cc Ministry of Energy) | electricity / energy | critical | zulfi |
| `07_online_store_fraud.pdf` | E-mail printout: online order never delivered, no refund | e-mail | consumer_protection / commerce | medium | riyadh_city |
| `08_school_bullying_dawadmi.pdf` | Repeated bullying, school did not act | Deputy | education / education | high | dawadmi |
| `09_internet_billing_wadi_dawasir.pdf` | Home internet billed twice | Prince | telecom / mcit | medium | wadi_dawasir |
| `10_sewage_repeat_riyadh.pdf` | Second submission (للمرة الثانية) of #1 | Prince | water_sewage / mewa | high | riyadh_city |
| `11_scanned_housing_support_shaqra.pdf` | **Image-only scan**: housing support not paid | Deputy | housing / municipal | medium | shaqra |
| `12_thank_you_letter.pdf` | **Not a complaint**: thanks to a Riyadh hospital's team, sent to the Emirate | Prince | any | low | riyadh_city |
| `13_outside_jurisdiction_jeddah.pdf` | **Outside jurisdiction**: flooded underpass in جدة | Prince | municipal_services / municipal | high | unknown (region makkah) |
| `14_riyadh_sender_park_taif.pdf` | **Outside jurisdiction, sender in Riyadh**: a broken, dangerous playground in a public park in الطائف (child cut his hand) | Prince | municipal_services / municipal | high | unknown (region makkah) |

Addressee forms: *Prince* = «صاحب السمو الملكي أمير منطقة الرياض حفظه الله»,
*Deputy* = «سعادة وكيل إمارة منطقة الرياض المحترم», *form* = the header
«نموذج تقديم شكوى — إمارة منطقة الرياض», *e-mail* = «إلى: إدارة الشكاوى – إمارة
منطقة الرياض <complaints@example.com>».

`expected.json` holds the labels as `[{file, category, subcategory, ministry,
priority, acceptable_priorities, region, governorate, addressed_to_entity,
expected_review_reasons, is_complaint, complainant_name, national_id, notes}]`.

- `null` means "any value is accepted" (#12).
- `acceptable_priorities` lists `priority` plus any neighbour the priority
  definitions make defensible.
- `governorate` is a governorate id from the taxonomy. It is `unknown` when
  the place is outside the Riyadh region (#13, #14).
- `addressed_to_entity` is true for all fourteen, including #13 and #14.
- `expected_review_reasons` lists the reasons that must be present. Other
  reasons may appear too, for example `priority_floor_applied`. But
  `outside_jurisdiction` and `addressed_elsewhere` must be absent unless they
  are listed.
- `national_id` is given in ASCII digits, whatever form the document uses.
- `notes` explains every borderline call against the priority definitions in
  `templates/complaints_taxonomy.yaml`.

#09's expected priority is **medium** (it was low). An overcharge that the
provider failed to correct is a service problem with financial harm, which is
the medium definition. Low remains acceptable because the amount is small.

## What each sample exercises

- **Addressee ≠ parties.** The Prince, the Deputy, the form header and the
  e-mail's «إلى:» line name the *receiving* entity. They are neither the
  complainant nor the party complained against. #06 also carries a «نسخة مع
  التحية إلى: وزارة الطاقة» line, and #12 copies the hospital director. A cc
  line does not make the letter addressed elsewhere.
- **Governorates.** The address field of each letter names a place in a
  different governorate: مدينة الرياض, الخرج, المجمعة, الدرعية, الزلفي,
  الدوادمي, وادي الدواسر or شقراء. The form (#03) and the e-mail (#07) name
  places too.
- **Outside jurisdiction.** #13 is addressed to the Emirate, but the
  complainant lives in Jeddah and writes about a Jeddah underpass. It must
  resolve to region `makkah` and governorate `unknown`, and carry
  `outside_jurisdiction`. The acknowledgment should say the complaint will be
  forwarded to the competent region's emirate. #14 is the harder form of the
  same case. The sender box and the first paragraph place the family in
  مدينة الرياض (حي العارض), but the complaint is about a park in حي الحوية,
  محافظة الطائف, which they visited on holiday. The problem's place decides:
  region `makkah`, governorate `unknown`, `outside_jurisdiction`. A pipeline
  that takes the sender's address answers riyadh/riyadh_city and misses the
  flag.
- **Digits and dates.** Arabic-Indic (#1, #4, #6, #11, #13), Western (#2, #3,
  #5, #7, #9, #10, #12, #14) and mixed (#8) digits. Dates are Hijri + Gregorian in
  2026, numeric or with month names (#6).
- **Repeat complainant.** #10 repeats #1's name and national ID, but #1 writes
  the ID in Arabic-Indic digits and #10 in Western ones. Detection only works
  if IDs are digit-normalised. After both are processed, #10 should carry
  `repeat_complainant` and the `repeated_unresolved` signal.
- **OCR only.** #11 has no text layer (150 dpi grey scan, slight skew, noise,
  JPEG, a tilted «الوارد» stamp). The other thirteen are born-digital with an
  Arabic text layer, although the pipeline OCRs every page either way.
- **Review flags.** #12 must end up `needs_review` with `not_a_complaint`. #13
  and #14 must end up with `outside_jurisdiction`.
- **Rule floors.** Signal phrases appear only where they are honest (#1, #2,
  #6, #10, #14). The low-priority samples (#5, #12) contain none, so a low
  model answer stays low. Neither do #9 and #13, so their priority is the
  model's own call. In #14, «الأطفال» and «طفل» floor it to medium only, so
  high must come from the model. Critical (#2, #6) can only come from the
  model, because rules never set it.

## Regenerating

```
python samples/complaints/make_samples.py            # rebuild everything, then verify
python samples/complaints/make_samples.py 03 11      # rebuild some, verify all
python samples/complaints/make_samples.py --verify   # verify only
```

The HTML sources live in `src/` (one standalone RTL page per sample, inline
CSS, `"Segoe UI", Tahoma, Arial`). The script prints them with Microsoft Edge
headless (`--print-to-pdf`, a throwaway `--user-data-dir`; set `EDGE_PATH` if
Edge is not in the default location). It then rasterises #11 with pypdfium2
and degrades it with Pillow. Any Python with `pypdfium2` and `Pillow` works.

Verification checks that all 14 PDFs exist, open with pypdfium2 and are under
400 KB. #11 must have no text layer. The other thirteen must have an Arabic text
layer that contains the complainant's name and, when `addressed_to_entity` is
true, the words «منطقة الرياض» of the addressee. pdfium returns RTL text in
visual order, so both are matched word by word. A PDF in the folder that
`expected.json` does not list fails the check, because it is probably left
over from a renamed sample.
