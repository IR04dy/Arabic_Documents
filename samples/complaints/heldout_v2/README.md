# Held-out evaluation set v2 (CMS)

Fourteen more plain-text complaints (UTF-8, no BOM, LF), each with labels.
They are used only to **measure** how well the CMS pipeline structures and
classifies complaints it was never tuned on. All of them reached the
complaints desk of إمارة منطقة الرياض, in the forms the desk really receives:

- seven letters: to the Prince, the Deputy Prince, the Deputy, the Assistant
  Deputy, and one to a governorate's governor
- three e-mails, one of them mostly in English
- a message through the web portal
- a web-form dump
- a WhatsApp message transcribed by staff
- a phone call transcribed by staff, in Najdi dialect

This set complements [`../heldout/`](../heldout/README.md) and follows the same
rules. Report its scores separately from v1's, and also combined with them.

## Written blind

The texts and labels were written **without opening `complaints.py`,
`complaints_taxonomy.py` or any prompt text**, and without looking at any
pipeline output. The author used only these sources:

- `templates/complaints_taxonomy.yaml`, for the ids, the category
  descriptions, the priority definitions, the signal patterns and the place
  lists
- the v1 held-out README and `expected.json`, for the format and to avoid
  reusing their scenarios
- the visible samples' README and `expected.json`, to avoid their scenarios
- the CMS specification's short description of the addressee and
  outside-jurisdiction checks

When this set was written, none of these texts had been run through the
pipeline. That independence is the whole value of the set, and it is lost as
soon as anything is tuned on it.

> **Everything here is fictional.** People, IDs, phone numbers, e-mail
> addresses (`@example.com`), case, ticket and reference numbers, shops,
> cafés, buildings and recruitment offices were all made up. Government bodies
> and platforms (ناجز) are named only as a citizen would name them.

## The rule: never tune on this set

- Do not change prompts, category descriptions, signal patterns, place lists
  or any other rule because of what these texts say or how they are labelled.
- Do not paste them, or paraphrases of them, into a prompt, and do not use
  them as few-shot examples.
- Do not process them in a store whose reviewer corrections feed the
  pipeline. `recent_corrections()` turns corrected complaints into
  precedents, so run them against a throwaway `CMS_DATA_DIR`. Never use the
  shared dev instance.
- When a failure here shows a real weakness, reproduce it with a **new**
  sample in the main set (`samples/complaints/`), fix it there, and only then
  re-measure here.
- A held-out item that has been used for tuning is burnt. Move it to the main
  set and write a fresh one to replace it.

## Contents

| File | Form | Place (governorate) | Category / ministry | Priority | What makes it hard |
|---|---|---|---|---|---|
| `v01_letter.txt` | letter to the Prince | المزاحمية (muzahmiyah) | water_sewage / mewa | critical | real emergency written in understated words, with no signal phrases |
| `v02_email.txt` | e-mail | الغاط (ghat) | digital_services / justice | medium | a platform (ناجز) owned by a specific ministry |
| `v03_whatsapp.txt` | WhatsApp, transcribed | الزلفي (zulfi) | consumer_protection / commerce | low | shouting and threats about a 1-riyal price rise |
| `v04_letter.txt` | letter to the Prince | الرس (outside: qassim) | environment / mewa | medium | the sender lives in Riyadh; the problem is in another region |
| `v05_phone_call.txt` | phone call, transcribed | السيح (kharj) | labor / hrsd | medium | Najdi dialect; only the town is named |
| `v06_letter.txt` | petition to the Deputy Prince | مرات (marat) | security_safety / interior | high | drifting; a secondary municipal request; «مرات» as a place and as a word |
| `v07_portal_message.txt` | web-portal message | الرياض (riyadh_city) | tourism / tourism | medium | the sender lives in الخبر (eastern), but the apartment is in Riyadh, so no outside_jurisdiction |
| `v08_letter.txt` | letter to the Deputy | تمير (majmaah) | education / education | critical | only the town is named; critical goes beyond the rule floor |
| `v09_english_email.txt` | English e-mail | Shaqra (shaqra) | telecom / mcit | medium | mostly English; the place appears only in Latin script |
| `v10_form.txt` | web-form dump | الخماسين (wadi_dawasir) | electricity / energy | medium | wrong self-selected category; only the town is named; the bare label «المدينة» |
| `v11_letter.txt` | letter to a governor | ثادق (thadiq) | health_services / health | high | addressed to «محافظ ثادق»: a governor of the region, so addressed to the Emirate (policy of 2026-09-25) |
| `v12_letter.txt` | letter to the Assistant Deputy | رماح (rumah) | transport / transport | high | highway between cities; the road's name contains «الرياض» |
| `v13_email.txt` | e-mail | الأفلاج (aflaj) | any (not a complaint) | low | an inquiry; «يستغرق» contains «غرق»; the sender's name «بدر» is also a Madinah city |
| `v14_letter.txt` | letter to the Prince | العيينة (diriyah) | municipal_services / municipal | high | only the town is named; a child was bitten |

**Coverage.** 13 categories plus one inquiry that is not a complaint.
Thirteen Riyadh-region governorates plus one place outside the region.
Priorities split 2 critical, 4 high, 6 medium and 2 low.

**Hard cases, by kind:**

- **Where the problem is, not where the sender is.** In v04 the sender lives
  in Riyadh and the problem is in القصيم. v07 is the mirror image: the sender
  lives in الخبر and the problem is in Riyadh.
- **Only a town names the place.** Four texts name a town but never its
  governorate: السيح (v05), تمير (v08), الخماسين (v10) and العيينة (v14).
- **The addressee.** v11 is addressed to the governor of one of the region's
  governorates, which counts as the Emirate since the policy decision below,
  so addressed_elsewhere must be absent. v06 is addressed to the Deputy Prince
  and v09 to the Emirate in English, so addressed_elsewhere must be absent in
  both too.
- **Routing.** v02 is about a platform, so the ministry is the platform's
  owner (ناجز → justice). In v10 the citizen picked a category that the
  content contradicts. v12 is a road between cities, which is transport, not
  municipal. In v06 the drifting goes to interior, although the petition also
  asks the municipality for speed bumps.
- **Tone against substance.** v01 describes a critical situation calmly. v03
  shouts about a trivial one.
- **Words that matchers can misread:**
  - «يستغرق» contains the signal «غرق» (v13)
  - the name «بدر» is also a Madinah city (v13)
  - the form label «المدينة» is not the Madinah region (v10)
  - the road name «رماح – الرياض» contains «الرياض» (v12)
  - «مرات كثيرة» sits next to the real place «مرات» (v06)
  - «الرياض» appears in every addressee line, but it is not where the
    problem is

## Labels: `expected.json`

The format is the same as v1:

```
[{file, category, subcategory, ministry, priority, acceptable_priorities,
  region, governorate, is_complaint, expected_review_reasons, notes}]
```

It uses the ids in `templates/complaints_taxonomy.yaml`.

- `null` in `category`, `subcategory` or `ministry` means any value is
  accepted. This applies only to v13, which is not a complaint.
- `priority` is the best answer under the priority definitions.
  `acceptable_priorities` adds the neighbours those definitions make
  defensible. Score both.
- `region` and `governorate` are where the **complained-about problem** is,
  not the sender's address (see v04 and v07). `governorate` is `unknown`
  outside the Riyadh region.
- `expected_review_reasons` lists the reasons that must be present. Other
  reasons may appear too, for example `priority_floor_applied`. But
  `outside_jurisdiction` and `addressed_elsewhere` must be absent unless they
  are listed.
- `notes` explains each call and names acceptable alternative categories,
  subcategories or ministries. It also lists the rule-signal phrases present
  (so a raised floor can be told apart from the model's own call) and
  describes the trap, if there is one. A scorer that wants to be lenient
  reads the alternatives from there.

One label rests on a policy decision. v11 first expected
`addressed_elsewhere`, because a governorate is a separate office from the
Emirate, even though it reports to it.

**Policy decision, 2026-09-25:** governorates report to the Emirate, so a
letter addressed to the governor or the office of one of the Riyadh region's
governorates counts as addressed to the Emirate. The pipeline's addressee
rule and the taxonomy's `addressed_elsewhere` label were changed for it, and
v11's label with them, deliberately: v11 now expects `addressed_to_entity`
true and no `addressed_elsewhere`. It was not changed because of a score, and
no other label changed. A letter to another body of a governorate («بلدية
محافظة الخرج») or to a governor of another region is still addressed
elsewhere. This set has no `addressed_to_entity` field: an item is addressed
to the Emirate unless it lists `addressed_elsewhere`.

## Running it

Start a CMS instance with a throwaway `CMS_DATA_DIR`. Upload the `.txt` files,
or paste each text into the text intake. For each item, compare these fields
with `expected.json`:

- `category`, `subcategory`, `ministry` and `priority`
- `region` and `governorate`
- `analysis.structured.is_complaint`
- `analysis.review_reasons`

Report accuracy per field. Report priority both exactly and against
`acceptable_priorities`. Also report the three review-reason checks: required
reasons present, and `outside_jurisdiction` and `addressed_elsewhere` absent
where they are not listed.
