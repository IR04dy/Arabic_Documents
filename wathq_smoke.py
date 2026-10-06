"""Try every Wathq service once and say which answer and which refuse.

    python wathq_smoke.py            # asks for the numbers (not shown on screen)
    python wathq_smoke.py --yes      # don't ask before sending
    python wathq_smoke.py --services company,drug

It asks for: a company number (old CR or 70…), a power-of-attorney number
with the principal's or the agent's ID, a deed number with the owner's ID, an
employee ID, an investor's 70… number, and a drug registration number. Leave
any of them blank to skip that service; the drug number defaults to the public
example in Wathq's own spec. Every number sent is a billed request: the plan
and the count are shown first.

For each service it prints OK with the SKELETON of the answer (field names
and types, never a value) or the refusal: the app's message, Wathq's code and
Wathq's own wording (digit runs redacted). Nothing personal is printed, so
the output can be pasted back for diagnosis. The same client, key and
settings as the app are used (WATHQ_API_KEY / WATHQ_API_KEY_FILE, WATHQ_ENV,
WATHQ_PROXY_URL, WATHQ_CA_BUNDLE, WATHQ_DEBUG).
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from wathq_catalog import get_catalog
from wathq_client import WathqClient, WathqError, code_in, error_for
from wathq_probe import skeleton
from wathq_suggest import fold

# service -> (endpoint id, how its inputs come from the asked values)
SERVICES = {
    "company": [("cr.info", {"id": "company"}), ("cr.status", {"id": "company"})],
    "address": [("national_address.info", {"crNumber": "company"})],
    "contract": [("contracts.info", {"crNationalNumber": "company"})],
    "attorney": [("attorney.info", {"code": "wakalah", "principalId": "principal", "agentId": "agent"})],
    "deed": [("real_estate.deed", {"deedNumber": "deed", "idNumber": "owner", "idType": "owner_type"})],
    "employee": [("employee.info", {"id": "employee"})],
    "investor": [("investor.fullinfo", {"id": "investor"})],
    "drug": [("drug.status", {"id": "drug"})],
}
QUESTIONS = [
    ("company", "Company number, old CR or 70… (blank = skip company, address, contract)"),
    ("wakalah", "Power-of-attorney number (blank = skip)"),
    ("principal", "  principal's ID (blank if you give the agent's)"),
    ("agent", "  agent's ID (blank if you gave the principal's)"),
    ("deed", "Real-estate deed number (blank = skip)"),
    ("owner", "  owner's ID (national ID, iqama or 70… CR)"),
    ("employee", "Employee ID / iqama (blank = skip)"),
    ("investor", "Investor 70… number (blank = skip)"),
    ("drug", "Drug registration number (blank = Wathq's public example 2208240355)"),
]
DRUG_EXAMPLE = "2208240355"


def plan(values: dict, catalog, env: str, only=None) -> list:
    """(service, endpoint id, inputs) for every service that has its numbers,
    plus (service, endpoint id, None, reason) for the ones skipped."""
    values = {k: fold(v) for k, v in values.items()}
    if not values.get("drug"):
        values["drug"] = DRUG_EXAMPLE
    owner = values.get("owner", "")
    values["owner_type"] = ("National_ID" if owner.startswith("1") else "Resident_ID" if owner.startswith("2")
                            else "CR_NO" if owner.startswith("7") else "")
    out = []
    for service, calls in SERVICES.items():
        if only and service not in only:
            continue
        for eid, mapping in calls:
            inputs = {name: values[src] for name, src in mapping.items() if values.get(src)}
            ep = catalog.endpoints[eid]
            if not catalog.available(ep, env):
                out.append((service, eid, None, "not available in the sandbox"))
                continue
            try:
                catalog.request(eid, inputs, "ar", env=env)
            except ValueError as exc:
                out.append((service, eid, None, str(exc) if inputs else "no number given"))
                continue
            out.append((service, eid, inputs, ""))
    return out


def run(planned: list, client, catalog, out=sys.stdout) -> list:
    """Send the planned calls; return (service, endpoint, outcome, text) rows."""
    rows, converted = [], {}
    env = client.env
    for service, eid, inputs, reason in planned:
        if inputs is None:
            rows.append((service, eid, "skipped", reason))
            continue
        ep = catalog.endpoints[eid]
        print(f"\n== {service}: {eid} ({ep.label})", file=out)
        try:
            req = catalog.request(eid, inputs, "ar", env=env)
            unified = None
            legacy = req.conversion_value()
            if legacy:
                if legacy not in converted:
                    converted[legacy] = client.national_number(legacy)
                    print("   old CR converted to the unified number (1 request)", file=out)
                unified = converted[legacy]
            data = client.call(catalog.base(ep, env), req.path(unified), req.query(), req.headers(), what=eid)
            code = code_in(data)
            if code:
                exc = error_for(None, code)
                said = data.get("message") if isinstance(data, dict) else ""
                raise WathqError(exc.message, exc.status, code, detail=str(said or "")[:300])
            print("   OK · skeleton (field names and types only):", file=out)
            print("   " + json.dumps(skeleton(data), ensure_ascii=True, indent=2).replace("\n", "\n   "), file=out)
            rows.append((service, eid, "ok", ""))
        except WathqError as exc:
            text = exc.message + (f" · Wathq code {exc.code}" if exc.code else "") + (f" · Wathq says: {exc.detail}" if exc.detail else "")
            print("   FAILED:", text, file=out)
            rows.append((service, eid, "failed", text))
        except ValueError as exc:
            print("   invalid input:", exc, file=out)
            rows.append((service, eid, "invalid", str(exc)))
    return rows


def _ask(prompt: str) -> str:
    try:
        if not sys.stdin.isatty():              # piped answers (a script, a test): plain lines
            sys.stderr.write(prompt + ": ")
            sys.stderr.flush()
            return sys.stdin.readline().strip()
        return getpass.getpass(prompt + ": ")   # a terminal: typed numbers are not echoed
    except (EOFError, KeyboardInterrupt):
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Try every Wathq service once; show which answer and which refuse.")
    ap.add_argument("--services", help="comma-separated subset: " + ",".join(SERVICES))
    ap.add_argument("--yes", action="store_true", help="don't ask before sending")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    only = {s.strip() for s in args.services.split(",")} if args.services else None
    if only and only - set(SERVICES):
        print("unknown service:", ", ".join(sorted(only - set(SERVICES))))
        return 2
    try:
        client = WathqClient()
    except ValueError as exc:
        print("invalid setting:", exc if str(exc).startswith("WATHQ_") else type(exc).__name__)
        return 2
    problem = client.key_problem()
    if problem:
        print("key problem:", problem)
        print("hint: this terminal must have the key; run\n"
              "  $env:WATHQ_API_KEY = [Environment]::GetEnvironmentVariable('WATHQ_API_KEY','User')")
        return 2
    catalog = get_catalog()

    print("Numbers are not shown as you type. Leave blank to skip a service.")
    needed = {src for s in (only or SERVICES) for _, mapping in SERVICES[s] for src in mapping.values()}
    values = {}
    for key, prompt in QUESTIONS:
        if key in needed:                       # ask only for what the chosen services use
            values[key] = _ask(prompt)
    planned = plan(values, catalog, client.env, only)
    sending = [p for p in planned if p[2] is not None]
    legacy = sum(1 for p in sending if catalog.endpoints[p[1]].needs_conversion_input
                 and fold(values.get("company", "")).startswith(tuple("123456")))
    print(f"\nenvironment: {client.env}")
    for service, eid, inputs, reason in planned:
        print(f"  {service:9} {eid:24} " + ("send" if inputs is not None else f"skip ({reason})"))
    total = len(sending) + (1 if legacy else 0)
    print(f"\nThis sends {total} billed request(s) to Wathq" + (" (one is the CR conversion)." if legacy else "."))
    if not total:
        print("nothing to send.")
        return 1
    if not args.yes:
        sys.stderr.write("Send? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            print("nothing sent.")
            return 1
    rows = run(planned, client, catalog)
    print("\n== summary")
    for service, eid, outcome, text in rows:
        print(f"  {outcome:8} {service:9} {eid:24} {text}")
    print(f"\nrequests sent to Wathq: {client.sent}")
    return 0 if all(r[2] in ("ok", "skipped") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
