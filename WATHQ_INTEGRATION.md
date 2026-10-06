# Verify data (التحقق من البيانات) — Wathq integration

The **التحقق من البيانات** tab queries [Wathq](https://developer.wathq.sa)
(واثق), the Saudi government-data API marketplace run by Thiqah for the
Ministry of Commerce, the Ministry of Justice, SPL, GOSI, MISA, SFDA and other
data owners. It offers **every product Wathq publishes**, 8 products and 47
queries, from one tab: pick a product, pick a query (its price is on it), fill
the form the tab builds for that query, and press **استعلام من وثق**.

This is the app's **only call to the internet**, and most queries are **paid**.
Everything else in the project runs on this machine.

## Services

Prices are Wathq's prepaid prices per successful call, without VAT. *personal*
marks an input that identifies a person: it is sent to Wathq but never echoed
back, logged or shown in the result.

### السجل التجاري — Commercial Registration

Base path `/commercial-registration` · sandbox: yes

| Query | Inputs | Price |
|---|---|---|
| البيانات الكاملة للسجل التجاري (`/fullinfo/{id}`) | unified or old CR number | 12 SAR |
| البيانات الأساسية للسجل التجاري (`/info/{id}`) | unified or old CR number | 5 SAR |
| فروع السجل التجاري (`/branches/{id}`) | unified or old CR number | 5 SAR |
| حالة السجل التجاري (`/status/{id}`) | unified or old CR number | 2 SAR |
| حالة السجل التجاري مع التواريخ (`/status/{id}`) | unified or old CR number | 5 SAR |
| رأس مال السجل التجاري (`/capital/{id}`) | unified or old CR number | 5 SAR |
| المديرون وأعضاء مجلس الإدارة (`/managers/{id}`) | unified or old CR number | 5 SAR |
| الملاك والشركاء وحصصهم (`/owners/{id}`) | unified or old CR number | 5 SAR |
| السجلات التجارية المرتبطة بهوية (`/v2/related`) | ID number · personal, ID type, nationality code (required for passports and foreign CRs) | 5 SAR |
| التحقق من امتلاك هوية لسجل تجاري (`/v2/owns`) | ID number · personal, ID type, nationality code (required for passports and foreign CRs) | 2 SAR |
| الرقم الوطني الموحد من رقم السجل (`/crNationalNumber/{id}`) | CR or unified number | 2 SAR |
| المستفيدون الحقيقيون (`/beneficiary/{id}`) | unified or old CR number | free |

Reference lists (16): حالات السجل التجاري، أنواع الكيان التجاري، أشكال الشركات، صفات الشركات، أنواع العلاقة بالسجل، مناصب المديرين، أنواع الإثبات، أشكال الهيكل الإداري، أنواع الشركاء، أنواع الشراكة، الجنسيات، الأنشطة التجارية (ISIC)، المدن، العملات، أنواع المستفيدين الحقيقيين، أنواع الأوصياء. Price: 5 SAR / free each.

### عقود الشركات — Company Contracts

Base path `/company-contract` · sandbox: yes

| Query | Inputs | Price |
|---|---|---|
| بيانات عقد تأسيس الشركة (`/info/{crNationalNumber}`) | unified or old CR number, contract copy (optional) | 12 SAR |
| بيانات المديرين (`/management/{crNationalNumber}`) | unified or old CR number | 5 SAR |
| بيانات وصلاحيات مدير (`/manager/{crNationalNumber}/{id}/{idType}`) | unified or old CR number, ID number · personal, ID type, permission (optional) | 12 SAR |

Reference lists (3): أبواب البنود النصية، قرارات الشركاء، طرق ممارسة الصلاحيات. Price: 5 SAR each.

### العنوان الوطني — National Address

Base path `/spl/national/address` · sandbox: no

| Query | Inputs | Price |
|---|---|---|
| العنوان الوطني للمنشأة (`/info/{crNumber}`) | CR or unified number | 2 SAR |

### الوكالات الشرعية — Power of Attorney

Base path `/v1/attorney` · sandbox: no

| Query | Inputs | Price |
|---|---|---|
| التحقق من بيانات الوكالة الشرعية (`/info/{code}`) | agency number, ID number (optional) · personal, ID number (optional) · personal, one of the IDs required | 5 SAR |

Reference lists (1): معرفات نصوص الوكالات الشرعية. Price: 5 SAR each.

### الصكوك العقارية — Real Estate Deeds

Base path `/moj/real-estate` · sandbox: no

| Query | Inputs | Price |
|---|---|---|
| بيانات الصك العقاري (`/deed/{deedNumber}/{idNumber}/{idType}`) | deed number, ID number · personal, ID type | 5 SAR |

### معلومات الموظفين — Employee Information

Base path `/masdr/employee` · sandbox: no

| Query | Inputs | Price |
|---|---|---|
| استرجاع بيانات الموظف (`/v2/info`) | ID number · personal | 7 SAR |

### المستثمرون (وزارة الاستثمار) — Investors (MISA)

Base path `/investor` · sandbox: no

| Query | Inputs | Price |
|---|---|---|
| بيانات المستثمر الكاملة (`/fullinfo/{id}`) | investor unified number | 50 SAR |

Reference lists (3): حالات تسجيل المستثمر، أنواع الشريك، أنواع هوية المفوَّض. Price: free each.

### معلومات الأدوية — Drug Information

Base path `/drugs` · sandbox: no

| Query | Inputs | Price |
|---|---|---|
| سعر الدواء (`/price/{id}`) | SFDA registration number | free |
| البدائل الدوائية (`/alternatives/{id}`) | SFDA registration number | free |
| حالة تسجيل الدواء (`/status/{id}`) | SFDA registration number | free |
| الصرف الدوائي (الوصفة الطبية) (`/dispensing/{id}`) | SFDA registration number | free |

The commercial-registration status is two queries because Wathq bills them
differently: without dates (2 SAR) and with dates (5 SAR). One priced lookup
is left out on purpose: Wathq lists `/lookup/managerPermissions`
for Company Contracts at 5 SAR, but its published spec doesn't describe it.

## Setup

1. **Subscription.** On developer.wathq.sa, under *My Apps*, the app that owns
   your key must be subscribed to each product you query. Typing an old
   commercial-registration number where the unified number is needed also uses
   *السجل التجاري (التشريعات الجديدة)*, which converts it. The free trial covers
   every product: 100 requests over 30 days, 5 per second.
2. **The key.** Only the API key is needed. The portal password never goes in
   the app. Store the key where the code can read it but git, logs and the
   browser cannot. The recommended way saves it to your Windows user
   environment without echoing it to the screen or the shell history:

   ```powershell
   $k = Read-Host "Wathq API key" -AsSecureString; [Environment]::SetEnvironmentVariable("WATHQ_API_KEY", [Net.NetworkCredential]::new("", $k).Password, "User")
   ```

   Open a **new** terminal afterwards, then start the app with `run.ps1`. Don't
   put the key in `run.ps1`, `ui.html`, a tracked file, or a plain
   `$env:WATHQ_API_KEY = '…'` line (PowerShell records that in its history).
   A key file outside the repository works too: set `WATHQ_API_KEY_FILE` to its
   absolute path. The file is re-read on every lookup.
3. **Check.** Open the tab. A green line reading *متصل بوثق (بيئة الإنتاج)*
   means the key is set. The first real lookup confirms the subscription.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `WATHQ_API_KEY` | — | The key from *My Apps*. Sent only as Wathq's `apiKey` header. |
| `WATHQ_API_KEY_FILE` | — | Alternative to the variable: a file holding the key. The variable wins when both are set. |
| `WATHQ_ENV` | `production` | `production` or `sandbox`. Only Commercial Registration and Company Contracts have a sandbox; the tab greys out the other products' queries there. |
| `WATHQ_TIMEOUT` | `20` | Seconds per network operation, 3–120. |
| `WATHQ_PROXY_URL` | — | `http://host:port` when the network needs a proxy. `HTTPS_PROXY` and friends are deliberately ignored. |
| `WATHQ_CA_BUNDLE` | — | A PEM file with an extra root certificate, for networks that inspect TLS. It is added to the Windows certificate store, not used instead of it. A missing or invalid file disables only this tab, with a message. Verification can't be turned off. |
| `WATHQ_CACHE_SECONDS` | `900` | How long an answer stays in memory, so repeating a query costs nothing. Reference lists are kept a day. `0` turns all caching off. Max one day. |
| `WATHQ_DEBUG` | off | `1` also logs the start of every error body Wathq returns (content type, length, first 500 characters with digit runs redacted), to see why a service refused. Error wording Wathq sends is shown in the tab in any case. |
| `WATHQ_ALLOW_REMOTE` | off | `1` lets machines other than this one run lookups when `APP_ALLOWED_HOSTS` opens the app to a network. The app has no login, so leave it off unless the network is trusted. Without it, a lookup must come from this machine's own connection; a forged `Host` header isn't enough. |

A wrong setting (`WATHQ_ENV=staging`, a malformed proxy URL) doesn't stop the
app. The tab shows the problem instead.

## Using it

1. Pick a **product** in the bar at the top of the tab.
2. Pick a **query**. Each card shows what it returns and its price; reference
   lists are folded under **قوائم مرجعية**. A query Wathq doesn't offer in the
   current environment is greyed out.
3. Fill the form. Arabic-Indic digits, spaces and dashes are fine. Where the
   unified number is needed you may type the old CR number instead: it is
   converted first, for one extra 2 SAR request, and the button shows the total.
   In the Commercial Registration product, an old number with no unified
   number is a struck-off record, which Wathq finds only by the old number, so
   the tab then asks once more with the old number and says so in the result.
   Some inputs depend on others: a passport or foreign-CR holder needs a
   nationality code, and a national ID or iqama must be 10 digits starting
   with 1 or 2.
4. Press **استعلام من وثق**. A query of 20 SAR or more (investor full info,
   50 SAR) asks for a second press to confirm.

The status line counts the requests this app has sent to Wathq since it
started, including failed ones that may still be billed. Attempts that never
left the machine (no DNS, connection refused, a certificate the app rejects)
aren't counted. Wathq's own balance is on the portal. Repeating the same query
with the same inputs within the cache window costs nothing and says so.

Results are labelled in Arabic from Wathq's own field descriptions. Company
Contracts' main query keeps its dedicated card (capital, partners and shares,
management, activities, decisions, clauses); every other query is shown as
grouped fields, tables and lists.

## Privacy and terms

- **What leaves the machine:** the inputs of the query you run, and nothing
  else. No document text or file is sent. Some queries need a person's ID
  (employee information, the owner of a deed, a principal or agent of a power
  of attorney, the related-records and ownership checks). Wathq takes some of
  these in the URL and some as headers. The app never logs them and never
  echoes them back to the page.
- **What comes back** can include personal data: names, nationalities, identity
  numbers, phone numbers, e-mail addresses and dates of birth. Before the answer
  reaches the browser the server masks identity and phone numbers to their last
  four digits, an e-mail to its first letter and domain, and a date of birth to
  its year. The match tolerates Wathq sending a list as one object, or a key in
  different case. In free text (a deed's wording, an agency's text, a clause,
  any sentence-like field) every run of nine or more digits keeps only its last
  four; dates, amounts and short numbers stay readable. Masked answers stay in
  memory for the cache window and are never written to disk; a person's ID in
  a cache key is held only as a keyed hash that dies with the process.
- **Employee and deed data are sensitive.** Query a person only with a lawful
  basis, such as their consent or a contract that requires it. The Saudi
  Personal Data Protection Law applies.
- **Wathq's terms** (v1.2, 25/01/2026) limit use to your own business. They
  forbid letting a third party benefit from the data without Thiqah's explicit
  approval, and the FAQ adds that the data must not be used for commercial or
  political purposes. Before offering this to customers, get that approval in
  writing. Data accuracy is the data owner's responsibility. When something
  doesn't match, go back to the Ministry of Commerce.

## Errors

| The tab says | Cause | What to do |
|---|---|---|
| لم يُضبط مفتاح وثق | No key in `WATHQ_API_KEY` / `WATHQ_API_KEY_FILE` | Set it (see *Setup*), restart the app. |
| رفضت وثق مفتاح الربط (401.1.1) | Wrong or revoked key | Copy the key again from *My Apps*. |
| اشتراكك في وثق لا يشمل هذه الخدمة (403) | The app isn't subscribed to that product | Subscribe it on the portal. |
| ردّت وثق بخطأ داخلي (500) بلا أي تفاصيل | Observed live for contracts, attorney, deed, investor and drug: Wathq answers an unknown number with an empty 500 instead of a 404 | Check the number; a company with no registered contract gets this on the contract service too. |
| هذه الخدمة غير متاحة في البيئة الاختبارية | `WATHQ_ENV=sandbox` and the product has no sandbox | Use production. |
| تحويل رقم السجل التجاري القديم يحتاج اشتراكًا… | An old CR number was typed, and the app isn't subscribed to Commercial Registration | Subscribe it, or type the unified number (70…) instead. |
| لا توجد بيانات لهذا الرقم في وثق (404.2.1) | Nothing registered for those inputs | Check the inputs. Wathq says a not-found answer is not charged on prepaid packages. |
| تجاوزت حد الاستعلامات (429) | Rate limit or exhausted balance | Wait, or check the balance on the portal. |
| تعذّر التحقق من شهادة خدمة وثق | TLS inspection on the network | Set `WATHQ_CA_BUNDLE`. |
| تعذّر الاتصال بخدمة وثق | No internet or a proxy is required | Set `WATHQ_PROXY_URL`. |

## How it is built

| File | Role |
|---|---|
| `templates/wathq/*.yaml` | Wathq's own Swagger files, one per product (see that folder's README). Read at startup for field labels and to check the catalog; never served. |
| `templates/wathq/catalog.yaml` | Every query the tab offers: label, price, inputs (name and location exactly as Wathq spells them, validation kind, *personal* flag), at-least-one-of rules, the response paths that hold personal data, free-text paths to redact, and Arabic labels for fields the spec leaves in English. |
| `wathq_catalog.py` | Loads the catalog strictly at startup and cross-checks every path and parameter against the specs, so a typo fails at boot instead of costing a paid call. Validates a form into a request (digits folded, separators dropped, enums checked) and builds its path, query and headers. |
| `wathq_view.py` | Turns any answer into labelled display nodes (fields, groups, lists, tables, cards), masks personal data on the server, collapses code/name pairs and drops empty values. |
| `wathq_client.py` | The HTTPS client. The host is fixed and each product's base path comes from the catalog, never from the page. The key and any header parameters are read per call and attached as unredirected headers. Redirects are refused and TLS is always verified. One decoding path handles compression, charsets and empty or non-JSON answers. |
| `wathq_verify.py` | Company Contracts' dedicated shaping (`shape_contract`) and number parsing. |
| `wathq_api.py` | `/wathq/status`, `/wathq/catalog`, `POST /wathq/query` (and the older `POST /wathq/company-contract`), and the tab's assets. Queries are local-only and same-origin, with a custom header that forces a CORS preflight, so another website can't spend your balance. One query runs at a time; answers are cached in memory. |
| `wathq_ui.js`, `wathq.css` | The tab: product bar, query cards with prices, the generated form, the two-step confirm, and the renderers. Every value is rendered as text; numbers go through the page's `appendNumText` so they keep their order inside Arabic. |
| `wathq_probe.py` | A one-shot diagnostic for unreadable answers (see below). |
| `wathq_smoke.py` | Tries every service once with numbers you type (not echoed): which answer, which refuse, and Wathq's own wording for each refusal. `--services company,drug` limits it; `--yes` skips the confirmation. Every call is billed; the plan and the count are shown first. |

The API is described in [`API.md`](API.md#data-verification-wathq).

### Updating Wathq's specs

Download the product's newer YAML from its page on developer.wathq.sa, replace
`templates/wathq/<product>.yaml`, and run the tests. If an endpoint or a
parameter the catalog uses has moved, the catalog check fails and names it;
fix `catalog.yaml` to match.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest test_wathq_review test_wathq_catalog test_wathq_query test_wathq_answers test_wathq_client test_wathq_verify test_wathq_api -v
```

The tests never contact Wathq. They cover:

- the catalog's agreement with every spec, and input validation;
- every one of the 47 queries rendered from Wathq's own example answers (most
  have one) and from a schema-shaped answer with a fake ID planted in every
  personal field, proving none reaches the page unmasked and every label is
  Arabic;
- the request shape and headers, key handling and leak checks, every error
  mapping, TLS, proxy and redirect settings;
- the local-only and cross-site guards, caching, conversion and the single slot.

Not yet confirmed against a live answer, because each call is billed:

- whether `/manager` returns a list or one object;
- the exact type of `entity.crNumber`;
- the default contract copy when `copyNumber` is omitted.

`shape_contract` accepts every variant. Your first real lookup settles them.

## When an answer can't be read

If the tab says «ردّ وثق غير مفهوم» or «أعادت وثق ردًا فارغًا», the app's
terminal has a `wathq:` line describing the answer. It records only the shape,
never data: status, content type, compression, sizes, how the body starts, an
HTML page's title with data removed, and Wathq's request reference.

The client already handles gzip and deflate (even unlabelled or mislabelled),
a declared legacy charset such as windows-1256, a BOM, UTF-16, and JSON wrapped
in a string. A 2xx answer that carries Wathq's `{code, message}` is treated
like the matching HTTP error.

For a closer look, run the diagnostic from the repository folder. It asks for
the number without showing it, and asks before sending:

```powershell
python wathq_probe.py
```

Its output holds no names, ID numbers, company number or key, so it can be
shared for diagnosis. Exit codes: `0` usable answer, `1` nothing sent or the
call failed, `2` bad input or settings, `3` an answer arrived but isn't usable.

## Next steps

- A **تحقق من وثق** action in the analysis tab: compare a document's extracted
  fields (a company contract, a deed, a power of attorney) against the official
  record, each row linked back to its page and line.
- Use the reference lists to turn codes in answers into names without a call,
  once they have been fetched.
