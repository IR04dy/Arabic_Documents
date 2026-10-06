"""Sequential multiple-choice checklist (user's design):
  Q1..  which service does the document need? numbered 1-8, 0 = none/stop; repeat until 0.
  Q2    per chosen service: which of its numbered items are written in the document? -> numbers.
  Q3    per selected item: copy the value as printed (+ the line).
The document sits first in the system prompt of every call so llama-server reuses the cached prefix.
Uses the cached OCR from run_checklists.py. No Wathq call anywhere."""
import json
import os
import re
import sys
import time

import yaml

ROOT = r"D:\Yousef\Arabic_Text_Extraction"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "checklists")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)
from run_checklists import SAMPLES, THINGS, FENCE_OPEN, FENCE_CLOSE, GUARD, fold  # noqa: E402

menu = yaml.safe_load(open(os.path.join(ROOT, "templates", "wathq", "menu.yaml"), encoding="utf-8"))
LABEL, ITEMS = {}, {}
for t in menu["things"]:
    LABEL[t["id"]] = t["label_ar"]
    seen = []
    for k in t.get("keys", []) + t.get("facts", []):
        p = k.get("prompt_ar")
        if p and p not in seen:
            seen.append(p)
    ITEMS[t["id"]] = seen

# one line per service: name + the group of information it gives (from the compact menu)
menu_text = open(os.path.join(ROOT, "templates", "wathq", "menu_prompt.ar.txt"), encoding="utf-8").read()
sections = re.split(r"\n(?=\d\) )", menu_text.split("\n----")[0])[1:]
SERVICE_LINE = {}
for tid, sec in zip(THINGS, sections):
    title = sec.split("\n")[0].split(") ", 1)[1]
    need = next((l for l in sec.split("\n") if l.startswith("نحتاج:")), "")
    give = next((l for l in sec.split("\n") if l.startswith("يمكننا مقارنة:")), "")
    give_short = "، ".join(re.sub(r"\[.*?\]", "", give[len("يمكننا مقارنة:"):]).split("،")[:8]).strip()
    SERVICE_LINE[tid] = f"{title} — {need.strip()} — يعطينا: {give_short}…"


TRACE = []


def call(llm, system, user, schema, max_tokens):
    TRACE.append({"system_chars": len(system), "question": user, "schema": schema})
    payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "temperature": 0.0, "top_p": 1.0, "seed": 0, "max_tokens": max_tokens,
               "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}}}
    t0 = time.time()
    llm.STRUCT._acquire(150)
    try:
        resp = llm.STRUCT._post("/v1/chat/completions", payload, 300)
    finally:
        llm.STRUCT._infer_lock.release()
    ch = resp["choices"][0]
    TRACE[-1]["answer"] = ch["message"]["content"]
    return ch["message"]["content"], ch.get("finish_reason"), resp.get("usage", {}), time.time() - t0


ANSWER_BY = sys.argv[1] if len(sys.argv) > 1 else "number"   # "number" | "name"


def main():
    import llm
    llm.STRUCT.ensure_loaded()
    results = {}
    only = os.environ.get("ONLY")
    for name in SAMPLES:
        if only and name != only:
            continue
        TRACE.clear()
        text = open(os.path.join(OUT, "ocr", name + ".txt"), encoding="utf-8").read()
        ftext = fold(text)
        system = (GUARD + "\n\n" + FENCE_OPEN + "\n" + text + "\n" + FENCE_CLOSE
                  + "\n\nستُسأل أسئلة متتابعة عن هذا المستند. أجب بالأرقام أو بنسخ حرفي من النص فقط.")
        rec = {"services": [], "items": {}, "calls": 0, "seconds": 0.0, "tokens_out": 0}

        # ---- Q1: services, one at a time, 0 stops
        remaining = list(THINGS)
        chosen = []
        while remaining:
            opts = "\n".join(f"{i+1}) {SERVICE_LINE[t]}" for i, t in enumerate(remaining))
            q = ("أي خدمة تحقق يحتاجها هذا المستند" + (" غير ما اخترته سابقاً" if chosen else "")
                 + "؟ الخدمة مناسبة إذا كان المستند يذكر مفتاحها (الرقم الذي تُفتح به) أو عدة معلومات من مجموعتها، "
                 "مهما كان نوع المستند أو الكلمات المستعملة فيه. أجب برقم واحد.\n" + opts
                 + "\n0) لا شيء" + (" آخر" if chosen else " مما سبق ينطبق"))
            if ANSWER_BY == "number":
                schema = {"type": "object", "properties": {"choice": {"type": "integer", "enum": list(range(len(remaining) + 1))}},
                          "required": ["choice"]}
            else:   # the same question, answered with the service's name instead of its number
                schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": remaining + ["none"]}},
                          "required": ["choice"]}
                q += "\n(أجب باسم الخدمة بالإنجليزية كما في القائمة: " + "، ".join(remaining) + "، أو none)"
                opts_named = "\n".join(f"{t}) {SERVICE_LINE[t]}" for t in remaining)
                q = q.replace(opts, opts_named).replace("أجب برقم واحد", "أجب بخيار واحد").replace("0) لا شيء", "none) لا شيء")
            content, finish, usage, dt = call(llm, system, q, schema, 12)
            rec["calls"] += 1; rec["seconds"] += dt; rec["tokens_out"] += usage.get("completion_tokens", 0)
            try:
                ans = json.loads(content)["choice"]
            except Exception:
                ans = 0
            if ANSWER_BY == "number":
                c = int(ans) if isinstance(ans, int) else 0
                if c == 0 or c > len(remaining):
                    break
                chosen.append(remaining.pop(c - 1))
            else:
                if ans not in remaining:
                    break
                chosen.append(remaining.pop(remaining.index(ans)))
        rec["services"] = chosen
        print(f"{name}: services -> {chosen or 'none'}", flush=True)

        # ---- Q2 + Q3 per service
        for tid in chosen:
            items = ITEMS[tid]
            opts = "\n".join(f"{i+1}) {it}" for i, it in enumerate(items))
            q = (f"خدمة «{LABEL[tid]}». أي البنود التالية مكتوب في المستند بقيمة ظاهرة؟ أجب بأرقام البنود الموجودة فقط "
                 "(قائمة فارغة إن لم يوجد شيء). اعتمد على المعنى لا على الاسم (هوية/إقامة/سجل مدني/ID = رقم هوية؛ "
                 "س.ت/C.R. = رقم سجل تجاري). لا تختر بنداً لمجرد تشابه الكلمات: حالة رخصة ليست حالة سجل تجاري، "
                 "وتاريخ انتهاء رخصة ليس تاريخ تأكيد سنوي.\n" + opts)
            schema = {"type": "object", "properties": {"numbers": {"type": "array", "items": {
                "type": "integer", "enum": list(range(1, len(items) + 1))}, "maxItems": len(items)}},
                "required": ["numbers"]}
            content, finish, usage, dt = call(llm, system, q, schema, 120)
            rec["calls"] += 1; rec["seconds"] += dt; rec["tokens_out"] += usage.get("completion_tokens", 0)
            try:
                nums = [n for n in json.loads(content)["numbers"] if 1 <= n <= len(items)]
            except Exception:
                nums = []
            nums = list(dict.fromkeys(nums))
            rows = []
            for n in nums:
                it = items[n - 1]
                q3 = (f"خدمة «{LABEL[tid]}»، البند رقم {n}: «{it}». انسخ قيمته من المستند حرفياً كما طُبعت "
                      "(بأرقامها وفواصلها)، ولا تصحح ولا تكمل ولا تستنتج. إن تكررت القيمة (شريكان، وكيلان) فعنصر لكل قيمة. "
                      "مع كل قيمة اقتباس السطر الذي وردت فيه.")
                schema = {"type": "object", "properties": {"values": {"type": "array", "items": {
                    "type": "object", "properties": {"value": {"type": "string"}, "line": {"type": "string"}},
                    "required": ["value", "line"]}, "minItems": 1, "maxItems": 6}}, "required": ["values"]}
                content, finish, usage, dt = call(llm, system, q3, schema, 300)
                rec["calls"] += 1; rec["seconds"] += dt; rec["tokens_out"] += usage.get("completion_tokens", 0)
                try:
                    vals = json.loads(content)["values"]
                except Exception:
                    vals = []
                for v in vals:
                    val = (v.get("value") or "").strip()
                    if not val:
                        continue
                    rows.append({"n": n, "item": it, "value": val, "line": v.get("line", ""),
                                 "in_text": fold(val) in ftext})
            rec["items"][tid] = {"selected": nums, "rows": rows}
            print(f"   {tid}: {len(nums)} items -> {len(rows)} values", flush=True)
        rec["seconds"] = round(rec["seconds"], 1)
        if only:
            NL = "\n"
            lines = [f"# Trace of every model call for {name} (answer by {ANSWER_BY})" + NL,
                     "System prompt (identical in every call) = data guard + the OCR text inside the fence + one sentence; "
                     f"{len(system)} characters. Below: each question sent as the user message, the JSON schema the answer "
                     "was constrained to, and the model's raw answer." + NL]
            for i, t in enumerate(TRACE, 1):
                lines += [NL + f"## Call {i}" + NL, "**Question**" + NL, "```", t["question"], "```",
                          "**Schema**" + NL, "```json", json.dumps(t["schema"], ensure_ascii=False), "```",
                          "**Answer**" + NL, "```json", t.get("answer", ""), "```"]
            lines += [NL + "## System prompt" + NL, "```", system, "```"]
            open(os.path.join(ROOT, "VERIFY_PROMPT_TRACE.md"), "w", encoding="utf-8").write(NL.join(lines))
        print(f"   {rec['calls']} calls, {rec['seconds']} s, {rec['tokens_out']} output tokens", flush=True)
        results[name] = rec
    json.dump(results, open(os.path.join(OUT, "results_sequential.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    out = ["# Checklists, sequential multiple-choice prompt (Qwen3-4B) on the sample documents\n",
           "Q1: which service does the document need? numbered, 0 = none; asked again until 0. "
           "Q2: per chosen service, which numbered items are written in the document? (numbers). "
           "Q3: per selected item, copy the value as printed, with its line. The document is the shared prefix of every call. "
           "`in text` = value appears literally in the OCR text.\n"]
    for name, rec in results.items():
        out.append(f"\n## {name}\n")
        out.append(f"Services: {', '.join(rec['services']) or 'none'} · {rec['calls']} calls · {rec['seconds']} s · "
                   f"{rec['tokens_out']} output tokens\n")
        for tid, b in rec["items"].items():
            out.append(f"\n**{tid}** · items selected: {b['selected']} · {len(b['rows'])} values\n")
            if b["rows"]:
                out.append("| # | item | value | line | in text |\n|---|---|---|---|---|")
                for r in b["rows"]:
                    c = [str(r.get(k, "")).replace("|", "\\|").replace("\n", " ") for k in ("n", "item", "value", "line")]
                    out.append("| " + " | ".join(c) + f" | {'yes' if r['in_text'] else 'NO'} |")
    path = os.path.join(ROOT, "CHECKLISTS_SAMPLE_RUN_3.md" if ANSWER_BY == "number" else "CHECKLISTS_SAMPLE_RUN_3b.md")
    open(path, "w", encoding="utf-8").write("\n".join(out))
    print("wrote", path)


if __name__ == "__main__":
    main()
