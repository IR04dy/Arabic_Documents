# Wathq API specifications

Wathq's own Swagger 2.0 files, as published on developer.wathq.sa (one per
product, production versions). The verify-data tab reads them at startup for
two things only: every response field's label (the Arabic half of Wathq's
bilingual descriptions) and a check that each endpoint the catalog calls
exists here with the parameters it sends. They are never served to the
browser and never change at runtime.

To update a product, download its newer YAML from its page on
https://developer.wathq.sa/en/apis, replace the file here, and run
`python -m unittest test_wathq_catalog`: the catalog test fails loudly if an
endpoint or a parameter the app uses has moved.

| File | Product | Spec version |
|---|---|---|
| `cr.yaml` | Commercial Registration (New Legislation) — السجل التجاري | 6.15.0 (info 6.14.0) |
| `contracts.yaml` | Company Contracts (New Legislation) — عقود الشركات | 2.8.0 (info 2.0.0) |
| `national_address.yaml` | National Address (SPL) — العنوان الوطني | 1.0.0 |
| `attorney.yaml` | Power of Attorney (MOJ) — الوكالات الشرعية | 1.0.0 |
| `real_estate.yaml` | Real Estate Deeds (MOJ) — الصكوك العقارية | 1.0.0 |
| `employee.yaml` | Employee Information (MASDR) — معلومات الموظفين | 1.0.1 |
| `drug.yaml` | Drug Information — معلومات الأدوية | 1.0.0 |
| `investor.yaml` | Ministry of Investment — المستثمرون | 6.14.0 |

`catalog.yaml` sits beside them: the 47 queries the tab offers, each with its
Arabic label, price, inputs, the response fields that hold personal data, and
Arabic labels for fields these specs describe only in English. It was built
from these files by reading every endpoint and its schema, then re-checked
against them; `wathq_catalog.load_catalog()` re-checks it on every start.
