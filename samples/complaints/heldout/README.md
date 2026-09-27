# Held-out evaluation set (CMS)

Fourteen plain-text complaints (UTF-8, no BOM) with labels, used only to
**measure** how well the CMS pipeline structures and classifies complaints it
was never tuned on. All of them reached the complaints desk of إمارة منطقة
الرياض, in the forms the desk really receives: formal letters, short e-mails,
a web-portal message, a web-form dump, a WhatsApp message transcribed by
staff, and one e-mail mostly in English.

They were written from the taxonomy and the priority definitions alone,
without reading the prompts in `complaints.py`. That independence is the whole
value of the set.

> **Everything here is fictional.** People, IDs, phone numbers, e-mail
> addresses (`@example.com`), reference numbers, shops and companies were made
> up. Government bodies and platforms are named only as a citizen would name
> them.

## The rule: never tune on this set

- Do not change prompts, category descriptions, signal patterns or any other
  rule because of what these texts say or how they are labelled.
- Do not paste them, or paraphrases of them, into a prompt, and do not use
  them as few-shot examples.
- Do not process them in a store whose reviewer corrections feed the
  pipeline. `recent_corrections()` turns corrected complaints into
  precedents, so run them against a throwaway `CMS_DATA_DIR`.
- When a failure here shows a real weakness, reproduce it with a **new**
  sample in the main set (`samples/complaints/`), fix it there, and only then
  re-measure here.
- A held-out item that has been used for tuning is burnt. Move it to the main
  set and write a fresh one to replace it.

## Contents

| File | Form | Place (governorate) | Category / ministry | Priority | What makes it hard |
|---|---|---|---|---|---|
| `h01_letter.txt` | letter to the Prince | الدلم (kharj) | water_sewage / mewa | high | alias place; consequences that look like other categories |
| `h02_email.txt` | short e-mail | حوطة سدير (majmaah) | digital_services / interior | medium | ambiguous ministry: an Absher failure |
| `h03_letter.txt` | letter to the Deputy | ليلى (aflaj) | education / education | high | two problems; the primary one decides; «ليلى» is also a name |
| `h04_email.txt` | short e-mail | حريملاء (huraymila) | religious_affairs / islamic_affairs | low | «عاجل جداً جداً!!!» about a trivial matter |
| `h05_letter.txt` | letter to the Prince | القويعية (quwayiyah) | electricity / energy | critical | real emergency written calmly, no signal phrases |
| `h06_portal_form.txt` | web-portal message | العلا (outside: madinah) | tourism / tourism | medium | outside jurisdiction while the sender lives in Riyadh |
| `h07_letter.txt` | letter to another body | ساجر (dawadmi) | consumer_protection / commerce | medium | addressed to the Ministry of Commerce branch |
| `h08_form.txt` | web-form dump | none (unknown) | social_support / hrsd | high | no location at all; «عدة مرات» is not the governorate مرات |
| `h09_english_email.txt` | English e-mail | Riyadh (riyadh_city) | housing / municipal | medium | mostly English; addressee and places in Latin script |
| `h10_whatsapp.txt` | WhatsApp, transcribed | ضرما (dhurma) | security_safety / interior | high | heavy Najdi dialect |
| `h11_letter.txt` | letter to the Prince | عفيف (afif) | health_services / health | high | a second governorate (الدوادمي) is named in the body |
| `h12_letter.txt` | letter to the Deputy | حوطة بني تميم (hotat_bani_tamim) | transport / transport | critical | transport vs municipal roads; critical beyond the rule floor |
| `h13_letter.txt` | letter to the Prince | السليل (sulayyil) | labor / hrsd | medium | rights violation without danger |
| `h14_letter.txt` | letter to the Deputy | الحريق (hareeq) | municipal_services / municipal | low | the place name «الحريق» contains the fire signal «حريق» |

Coverage: 14 different categories, 12 governorates plus two unknowns, and
priorities split 2 critical, 5 high, 5 medium and 2 low.

## Labels — `expected.json`

`[{file, category, subcategory, ministry, priority, acceptable_priorities,
region, governorate, is_complaint, expected_review_reasons, notes}]`, using the
ids in `templates/complaints_taxonomy.yaml`.

- `priority` is the best answer under the priority definitions.
  `acceptable_priorities` adds the neighbours those definitions make
  defensible. Score both.
- `region` and `governorate` are where the **complained-about problem** is,
  not the sender's address (see h06). `governorate` is `unknown` outside the
  Riyadh region and when no place is named.
- `expected_review_reasons` lists the reasons that must be present. Other
  reasons may appear too. But `outside_jurisdiction` and `addressed_elsewhere`
  must be absent unless they are listed.
- `notes` explains each call, names acceptable alternative categories,
  subcategories or ministries, and describes the trap, if there is one. A
  scorer that wants to be lenient reads the alternatives from there.

## Running it

Upload the `.txt` files to a CMS instance started with a throwaway
`CMS_DATA_DIR` (or paste each text into the text intake). Then compare each
item's `category`, `subcategory`, `ministry`, `priority`, `region` and
`governorate`, and `analysis.structured.is_complaint` and
`analysis.review_reasons`, with `expected.json`. Report accuracy per field,
and report priority both exactly and against `acceptable_priorities`.
