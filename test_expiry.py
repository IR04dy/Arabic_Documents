# -*- coding: utf-8 -*-
import sys, json
sys.path.insert(0, "/private/tmp/claude-501/-Users-yosef-Desktop-TSS-QR-Scanning/ed560fec-78cc-4dde-94c2-360aa8e019e8/scratchpad/exp")
import expiry
from datetime import date, timedelta

ok = bad = 0
def eq(name, got, want):
    global ok, bad
    if got == want: ok += 1; print("  ok  ", name)
    else: bad += 1; print("  FAIL", name, "\n        got :", repr(got), "\n        want:", repr(want))

print("== hijri conversion ==")
eq("uses hijridate when installed", expiry.hijri_to_gregorian(1447,1,1)[1], "hijridate")
eq("1 Muharram 1445", expiry.hijri_to_gregorian(1445,1,1)[0], date(2023,7,19))
eq("1 Muharram 1447", expiry.hijri_to_gregorian(1447,1,1)[0], date(2025,6,26))
eq("1450/01/01",      expiry.hijri_to_gregorian(1450,1,1)[0], date(2028,5,25))
rt = all(expiry.gregorian_to_hijri(expiry.hijri_to_gregorian(y,m,15)[0])[0] == (y,m,15)
         for y in range(1400,1490) for m in (1,6,12))
eq("roundtrip 1400-1490", rt, True)

print("\n== date parsing (text = numeric span only, era kept aside) ==")
def ds(t):
    return [(p.text, p.era, p.calendar, p.iso,
             p.gregorian.isoformat() if p.gregorian else None, p.ambiguous)
            for p in expiry.parse_dates(expiry.norm(t))]
eq("year-first + هـ", ds("تاريخ انتهاء الوكالة ١٤٤٧/٠٣/٢٩هـ"),
   [("1447/03/29","ه","hijri","1447-03-29","2025-09-21",False)])
eq("day-first + space هـ", ds("تنتهي بتاريخ 29/03/1447 هـ"),
   [("29/03/1447","ه","hijri","1447-03-29","2025-09-21",False)])
eq("iso gregorian", ds("valid until 2026-12-31"),
   [("2026-12-31","","gregorian","2026-12-31","2026-12-31",False)])
eq("month in words, no day -> ambiguous",
   [d[3:] for d in ds("في شهر رجب لعام 1447هـ")], [("1447-07-01", None, True)])
eq("day + month in words", ds("29 رجب 1447")[0][3:], ("1447-07-29","2026-01-18",False))
eq("two-digit year rejected", ds("بتاريخ 12/03/47"), [])
eq("deed number is not a date", ds("رقم الصك 310209018123"), [])

print("\n== label scan ==")
def sc(t):
    r = expiry.scan(expiry.norm(t))
    return None if not r else (r["label"], r["date"].text, r["date"].iso, r["rank"])
eq("wakala expiry header",
   sc("رقم الوكالة 438219005112\nتاريخ انتهاء الوكالة ١٤٤٧/٠٣/٢٩هـ"),
   ("تاريخ انتهاء الوكاله", "1447/03/29", "1447-03-29", 3))
eq("clause: تنتهي هذه الوكالة بتاريخ",
   sc("المادة الخامسة: تنتهي هذه الوكالة بتاريخ 15/07/1448هـ ما لم تُلغَ قبل ذلك.")[1:],
   ("15/07/1448", "1448-07-15", 3))
eq("clause: تنتهي الوكالة في  (was missed in v1)",
   sc("تنتهي الوكالة في 10/05/1449هـ")[1:], ("10/05/1449", "1449-05-10", 3))
eq("ينتهي هذا العقد بتاريخ",
   sc("ينتهي هذا العقد بتاريخ 01/01/2028م")[1:], ("01/01/2028", "2028-01-01", 3))
eq("party ID expiry ignored", sc("الاسم: محمد\nتاريخ انتهاء الهوية 1449/01/01هـ"), None)
eq("party iqama expiry ignored", sc("تاريخ انتهاء الإقامة 1449/01/01هـ"), None)
eq("passport expiry ignored", sc("تاريخ الانتهاء للجواز 1449/01/01"), None)
eq("iqama, attached preposition", sc("تاريخ انتهاء بالإقامة 1449/01/01"), None)
eq("licence IS the instrument, not excluded",
   sc("تاريخ انتهاء الرخصة 1449/01/01هـ")[3], 3)
eq("ID ignored, deed expiry still taken",
   sc("تاريخ انتهاء الهوية 1449/01/01هـ\nتاريخ انتهاء الوكالة 1447/03/29هـ")[1], "1447/03/29")
eq("english header", sc("Expiry Date: 2027-01-15")[1:], ("2027-01-15","2027-01-15",3))
eq("صالحة حتى", sc("هذه الشهادة صالحة حتى 1448/05/10هـ")[1:], ("1448/05/10","1448-05-10",2))
eq("no expiry anywhere", sc("صك ملكية رقم 310209018123 بتاريخ 1445/03/12هـ"), None)
eq("issue date alone is not an expiry", sc("تاريخ الوكالة 1445/03/12هـ"), None)
eq("date on the NEXT line ranks lower but is taken",
   sc("تاريخ انتهاء الوكالة\n1447/03/29هـ")[3], 2)

print("\n== declared state ==")
mk = lambda v: [{"fields":[{"key":"wakala_status","label":"حالة الوكالة","value":v}]}]
eq("ملغاة -> cancelled",  expiry.declared_state(mk("ملغاة"))[0], "cancelled")
eq("موقوفة -> suspended", expiry.declared_state(mk("موقوفة"))[0], "suspended")
eq("منتهية -> declared_expired", expiry.declared_state(mk("منتهية"))[0], "declared_expired")
eq("سارية -> no override", expiry.declared_state(mk("سارية"))[0], "")

print("\n== detect() end to end, today = 2026-09-20 ==")
T = date(2026,9,20)
secs = [{"title":"بيانات الوكالة","fields":[
    {"key":"wakala_status","label":"حالة الوكالة","value":"ملغاة"},
    {"key":"wakala_expiry_date_hijri","label":"تاريخ انتهاء الوكالة","value":"١٤٥٠/٠١/٠١",
     "source":{"page":1,"line":9,"start":100,"end":112,"quote":"تاريخ انتهاء الوكالة ١٤٥٠/٠١/٠١"}}]}]
r = expiry.detect("تاريخ انتهاء الوكالة ١٤٥٠/٠١/٠١", sections=secs, use_model=False, today=T)
print(json.dumps({k: r[k] for k in ("status","status_ar","state_override","state_override_ar",
    "date_text","date_normalized","calendar","date_gregorian","days_remaining","basis",
    "confidence","verified","converter")}, ensure_ascii=False, indent=2))
eq("revoked doc: status and override stay separate",
   (r["status"], r["state_override"]), ("valid", "cancelled"))
eq("template field keeps its existing citation", r["source"]["line"], 9)

txt = "وكالة شرعية\nرقم الوكالة 438219005112\nتاريخ انتهاء الوكالة ١٤٤٧/٠٣/٢٩هـ\n"
r = expiry.detect(txt, sections=[], use_model=False, today=T)
eq("expired via label scan", (r["status"], r["basis"], r["date_gregorian"]),
   ("expired", "label", "2025-09-21"))
for iso, want in (("1448/03/29","expired"), ("1448/06/25","expiring_soon"),
                  ("1449/01/01","valid")):
    r = expiry.detect("تاريخ انتهاء الوكالة %sهـ" % iso, sections=[], use_model=False, today=T)
    eq("%s -> %s" % (iso, want), r["status"], want)
    print("       ", r["date_gregorian"], r["days_remaining"], "days")

r = expiry.detect("صك ملكية رقم 310209018123", sections=[], use_model=False, today=T)
eq("nothing found -> unknown", (r["status"], r["basis"]), ("unknown", ""))
print("        note:", r["note"])
r = expiry.detect("تنتهي الوكالة في شهر رجب لعام 1449هـ", sections=[], use_model=False, today=T)
eq("month-only -> unknown + note", r["status"], "unknown")
print("        note:", r["note"], "| date_normalized:", r["date_normalized"])
r = expiry.detect("وكالة ملغاة", sections=mk("ملغاة"), use_model=False, today=T)
eq("no date but revoked", (r["status"], r["state_override"]), ("unknown","cancelled"))
print("        note:", r["note"])

print("\n== restore()/verify() plumbing ==")
raw = "تاريخ انتهاء الوكالة ١٤٤٧/٠٣/٢٩هـ"
seen = {}
def locate(v, labels=()): seen["locate"]=(v,list(labels)); return {"page":1,"line":3,"start":21,"end":31,"quote":raw}
def verify(v): seen["verify"]=v; return True
def restore(v): return v.replace("1","١").replace("4","٤").replace("7","٧").replace("0","٠").replace("3","٣").replace("2","٢").replace("9","٩")
r = expiry.detect(raw, sections=[], locate=locate, verify=verify, restore=restore, use_model=False, today=T)
eq("locate got the numeric date only", seen["locate"][0], "1447/03/29")
eq("locate got the label as a hint", seen["locate"][1], ["تاريخ انتهاء الوكاله"])
eq("date_text restored to the document's digits", r["date_text"], "١٤٤٧/٠٣/٢٩")
eq("verified + high confidence", (r["verified"], r["confidence"]), (True, "high"))

print("\n== classify boundaries (SOON_DAYS=%d) ==" % expiry.SOON_DAYS)
for d in (-1, 0, 1, 89, 90, 91):
    eq("%+d days" % d, expiry.classify(T + timedelta(days=d), T)[0],
       "expired" if d < 0 else ("expiring_soon" if d <= 90 else "valid"))

print("\n== duration (opt-in) ==")
for t, want in (("مدة الوكالة سنة واحدة من تاريخ تحريرها", 365),
                ("لمدة 6 أشهر", 180), ("مدتها سنتين", 730),
                ("مدة العقد ثلاثة أشهر", 90), ("لا مدة هنا", 0)):
    eq(t, expiry.find_duration(t)[0], want)

print("\n== chunking ==")
big = "\n".join("سطر رقم %d من المستند" % i for i in range(1, 2000))
ch = expiry._chunks(big, 9000, 4)
eq("bounded to MAX_CHUNKS", len(ch), 4)
eq("windows overlap", ch[0][-200:] in ch[1][:600], True)
eq("short text -> one chunk", len(expiry._chunks("قصير", 9000, 4)), 1)

print("\n== never raises ==")
for junk in ("", None, "\x00﻿", "1/1/1", "٢٩/٠٣/١٤٤٧"*500):
    try:
        expiry.detect(junk, sections=None, use_model=False, today=T)
    except Exception as e:
        bad += 1; print("  FAIL raised on", repr(junk)[:30], e)
    else: ok += 1
print("  ok   junk inputs survived")

print("\n== generic documents (fields carry no registry key) ==")
g = lambda lab, val: [{"title": "", "fields": [{"label": lab, "value": val,
      "origin": "document", "source": {"page": 1, "line": 7, "start": 10,
      "end": 20, "quote": lab + " " + val}}]}]
r = expiry.detect("x", sections=g("تاريخ انتهاء الوكالة", "١٤٤٨/٠٦/٢٥هـ"),
                  use_model=False, today=T)
eq("printed label matched, citation reused", (r["basis"], r["status"], r["source"]["line"]),
   ("template_field", "expiring_soon", 7))
eq("label with a trailing colon still matches",
   expiry.detect("x", sections=g("تاريخ انتهاء الوكالة :", "١٤٤٨/٠٦/٢٥هـ"),
                 use_model=False, today=T)["basis"], "template_field")
eq("issue-date label is not an expiry",
   expiry.detect("x", sections=g("تاريخ الوكالة", "١٤٤٧/٠١/١٥هـ"),
                 use_model=False, today=T)["status"], "unknown")
eq("a party's ID expiry field is not the instrument's",
   expiry.detect("x", sections=g("تاريخ انتهاء الهوية", "١٤٥٢/٠٨/١١هـ"),
                 use_model=False, today=T)["status"], "unknown")

print("\n%d ok, %d failed" % (ok, bad))
sys.exit(1 if bad else 0)
