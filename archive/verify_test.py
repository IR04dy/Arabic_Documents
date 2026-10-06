"""Fast test: structured data in, one question out.

For each sample document:
  1. OCR text (cached) -> the app's own classify + structure -> key: value pairs.
  2. One question to the local model: the extracted pairs + the list of services
     (service name + the data it returns). Answer: one service name, or None.
  3. Remove the chosen service and ask again, until None.
No Wathq call anywhere.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
CACHE = os.path.join(os.environ.get("CACHE_DIR", os.path.join(ROOT, "archive", "cache")))
OCR_DIR = os.path.join(CACHE, "ocr")
STRUCT_DIR = os.path.join(CACHE, "struct")
os.makedirs(STRUCT_DIR, exist_ok=True)

SAMPLES = [
    "01_expired_wakalah.pdf", "02_expiring_soon_wakalah.pdf",
    "03_valid_clause_only_wakalah.pdf", "04_cancelled_wakalah.pdf",
    "01_shisha_cafe_license_request.pdf", "Sak.pdf",
    "ksa-ar-t003-0001.pdf", "ksa-ar-t003-0002.pdf",
    "رخصة_نشاط_مقهى_تبغ_النسخة_أ.pdf", "رخصة_نشاط_مقهى_تبغ_النسخة_ب.pdf",
]

# The menu: service name + the data the service returns. Nothing else.
SERVICES = {
    "commercial_registration": ("السجل التجاري",
        "اسم المنشأة، رقم السجل، الرقم الوطني الموحد، حالة السجل، تاريخ الإصدار، تاريخ انتهاء السجل، نوع الكيان، "
        "رأس المال، المدينة، الأنشطة، الشركاء، المديرون، الفروع"),
    "company_contract": ("عقد تأسيس الشركة",
        "اسم الشركة، تاريخ العقد، مدة الشركة، رأس المال، الشركاء وحصصهم، المديرون وصلاحياتهم، بنود العقد"),
    "national_address": ("العنوان الوطني للمنشأة",
        "اسم المنشأة، رقم المبنى، الشارع، الحي، المدينة، الرمز البريدي، الرقم الإضافي"),
    "power_of_attorney": ("الوكالة",
        "رقم الوكالة، حالة الوكالة، تاريخ الإصدار، تاريخ الانتهاء، اسم الموكل ورقم هويته، اسم الوكيل ورقم هويته، بنود الوكالة"),
    "real_estate_deed": ("الصك العقاري",
        "رقم الصك، تاريخ الصك، حالة الصك، اسم المالك ورقم هويته، حصة الملكية، المساحة، المدينة، الحي، رقم القطعة، رقم المخطط"),
    "employee": ("الموظف في التأمينات الاجتماعية",
        "اسم الموظف، رقم هويته، الجنسية، اسم المنشأة، حالة الاشتراك، الراتب الأساسي، البدلات"),
    "foreign_investor": ("ترخيص المستثمر الأجنبي",
        "اسم المنشأة، الرقم الموحد، دولة المنشأ، حالة الترخيص، الشركاء ونسبهم، المفوض"),
    "drug": ("الدواء المسجل في هيئة الغذاء والدواء",
        "رقم التسجيل، الاسم التجاري، حالة التسجيل، السعر، تصنيف الصرف"),
}


def structured_pairs(name: str) -> list[str]:
    """Run the app's classify + structure once per document (cached) and flatten to key: value."""
    cache = os.path.join(STRUCT_DIR, name + ".json")
    if os.path.exists(cache):
        data = json.load(open(cache, encoding="utf-8"))
    else:
        from classify import classify as classify_document
        from registry import get_registry
        from structure import parse_structure, parse_structure_for_template
        text = open(os.path.join(OCR_DIR, name + ".txt"), encoding="utf-8").read()
        reg = get_registry()
        t0 = time.time()
        verdict = classify_document(text[:40000], reg).to_dict()
        tid = verdict.get("template_id") or ""
        template = None
        if tid:
            try:
                template = reg.get(tid)
            except Exception:
                template = None
        result = parse_structure_for_template(text, template, reg) if template else parse_structure(text)
        data = result.to_dict()
        data["_classify"] = verdict
        json.dump(data, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"  structured {name}: template={tid or '-'} in {time.time()-t0:.0f}s", flush=True)
    pairs = []
    for sec in data.get("sections", []):
        for f in sec.get("fields", []):
            if (f.get("value") or "").strip():
                pairs.append(f"{f['label']}: {f['value']}")
        for i, row in enumerate(sec.get("records", []), 1):
            label = sec.get("record_label") or sec.get("title") or ""
            for f in row:
                if (f.get("value") or "").strip():
                    pairs.append(f"{label} {i} - {f['label']}: {f['value']}")
    return pairs


def ask(llm, pairs: list[str], remaining: list[str]) -> str:
    services = "\n".join(f"- {s}: {SERVICES[s][0]} — يعيد: {SERVICES[s][1]}" for s in remaining)
    prompt = ("البيانات المستخرجة من المستند:\n" + "\n".join(pairs) +
              "\n\nخدمات التحقق المتاحة (اسم الخدمة — ما تعيده من بيانات):\n" + services +
              "\n\nأي خدمة يمكن أن تتحقق من بيانات هذا المستند؟ أجب باسم خدمة واحدة، أو None إن لم تنطبق أي خدمة.")
    schema = {"type": "object", "properties": {"service": {"type": "string", "enum": remaining + ["None"]}},
              "required": ["service"]}
    content, _ = llm.chat_json([{"role": "user", "content": prompt}], schema, max_tokens=20)
    return json.loads(content)["service"], prompt


def main() -> None:
    only = sys.argv[1:]
    import llm
    docs = {}
    for name in SAMPLES:
        if only and name not in only:
            continue
        docs[name] = structured_pairs(name)
    llm.STRUCT.ensure_loaded()
    shown = False
    for name, pairs in docs.items():
        remaining, chosen = list(SERVICES), []
        t0 = time.time()
        while remaining:
            answer, prompt = ask(llm, pairs, remaining)
            if not shown:                       # show exactly what the model gets, once
                print("=" * 70 + f"\nPROMPT ({name}):\n" + prompt + "\n" + "=" * 70, flush=True)
                shown = True
            if answer not in remaining:
                break
            chosen.append(answer)
            remaining.remove(answer)
        print(f"{name}: {len(pairs)} pairs -> {chosen or 'None'}  ({time.time()-t0:.1f}s)", flush=True)


if __name__ == "__main__":
    main()
