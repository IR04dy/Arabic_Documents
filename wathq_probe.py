"""Show what Wathq actually sends back, without showing any of the data.

    python wathq_probe.py                 # asks for the number (not shown on screen)
    python wathq_probe.py --language en

A unified number (70…) costs one billed request; an old CR number costs two
(conversion, then the contract). It asks before sending anything.

It uses the same client, key and settings as the app (WATHQ_API_KEY or
WATHQ_API_KEY_FILE, WATHQ_ENV, WATHQ_PROXY_URL, WATHQ_CA_BUNDLE) and the same
decoding (`wathq_client.decode_answer`), so its verdict is the app's verdict.
For every request it prints:

* status, content type and encoding, sizes, and Wathq's request reference;
* for an answer that isn't usable: why, and what it looked like (HTML page
  title with data stripped, XML root element, the JSON parser's complaint);
* for a JSON answer: its SKELETON — field names with their types and list
  lengths, merged over the list's items, never a value. Field names that look
  like data (digits, Arabic) are replaced by their length.

The output is meant to be pasted back for diagnosis: it holds no names, no ID
numbers, no company number and no key.

Exit codes: 0 usable JSON received · 1 nothing sent, or the call failed ·
2 bad input or settings · 3 an answer arrived but isn't usable.
Pass --yes to skip the question (needed when input isn't a terminal).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from wathq_client import (MSG_NO_SANDBOX_CONVERSION, UnusableAnswer, WathqClient, WathqError,
                          contract_path, conversion_error, conversion_path, decode_answer,
                          describe, safe_key, unified_from)
from wathq_verify import KIND_CR, parse_number

MAX_DEPTH = 8
MAX_KEYS = 80
LIST_SAMPLE = 50


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str" if value else "str(empty)"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def skeleton(value, depth: int = 0):
    """The shape of a JSON value: every leaf replaced by its type, every list
    by the merged shape of (up to LIST_SAMPLE of) its items."""
    if depth >= MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        out: dict = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= MAX_KEYS:
                out["…"] = f"{len(value) - MAX_KEYS} more keys"
                break
            name = safe_key(key)
            while name in out:                 # two masked keys can share a label
                name += "'"
            out[name] = skeleton(item, depth + 1)
        return out
    if isinstance(value, list):
        if not value:
            return "list[0]"
        kinds = sorted({_kind(v) for v in value})
        label = f"list[{len(value)}] of {'/'.join(kinds)}"
        sample = value[:LIST_SAMPLE]
        objects = [v for v in sample if isinstance(v, dict)]
        if objects:
            return {label: _merge([skeleton(v, depth + 1) for v in objects])}
        lists = [v for v in sample if isinstance(v, list)]
        if lists:
            return {label: _merge([skeleton(v, depth + 1) for v in lists])}
        return label
    return _kind(value)


def _merge(shapes: list):
    """One shape for many: the union of keys (annotated when a key is missing
    from some items) with differing leaf types joined by '|'."""
    if not shapes:
        return "…"
    if all(isinstance(s, dict) for s in shapes):
        order: list = []
        for s in shapes:
            for k in s:
                if k not in order:
                    order.append(k)
        out = {}
        for k in order:
            present = [s[k] for s in shapes if k in s]
            label = k if len(present) == len(shapes) else f"{k} (in {len(present)} of {len(shapes)})"
            out[label] = _merge(present)
        return out
    texts = []
    for s in shapes:
        text = s if isinstance(s, str) else json.dumps(s, ensure_ascii=True, sort_keys=True)
        if text not in texts:
            texts.append(text)
    if len(texts) == 1:
        return shapes[0]
    if any(isinstance(s, dict) for s in shapes):
        dicts = [s for s in shapes if isinstance(s, dict)]
        others = sorted({s if isinstance(s, str) else "?" for s in shapes if not isinstance(s, dict)})
        return {"|".join(others) + "|object": _merge(dicts)}
    return "|".join(texts)


def show(label: str, answer) -> tuple:
    """Print what one answer looked like. Returns (usable, data)."""
    print(f"\n== {label}")
    try:
        data, notes = decode_answer(answer)
    except UnusableAnswer as exc:
        print("   ", exc.reason + ":", exc.detail)
        print("    -> the app shows this as:", exc.message)
        return False, None
    print("   ", describe(answer))
    if notes:
        print("    decoded with:", ", ".join(notes))
    print("    skeleton (field names and types only, no values):")
    print("    " + json.dumps(skeleton(data), ensure_ascii=True, indent=2).replace("\n", "\n    "))
    return True, data


def _ask_number():
    try:
        return getpass.getpass("Company number (70… or old CR), not shown: ")
    except (EOFError, KeyboardInterrupt):
        return None


def _confirm(prompt: str) -> bool:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        answer = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Show the shape of Wathq's answer, never its data.")
    ap.add_argument("number", nargs="*",
                    help="optional; when omitted you are asked for it and it isn't shown")
    ap.add_argument("--language", choices=("ar", "en"), default="ar")
    ap.add_argument("--yes", action="store_true", help="don't ask before sending")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    raw = "".join(args.number) if args.number else _ask_number()
    if raw is None:
        print("nothing sent.")
        return 1
    try:
        number, kind = parse_number(raw)
    except ValueError as exc:
        print("invalid number:", exc)
        return 2
    try:
        client = WathqClient()
    except ValueError as exc:
        print("invalid setting:", exc if str(exc).startswith("WATHQ_") else type(exc).__name__)
        return 2
    problem = client.key_problem()
    if problem:
        print("key problem:", problem)
        if "WATHQ_API_KEY" in problem:
            print("hint: this terminal must have the key; run\n"
                  "  $env:WATHQ_API_KEY = [Environment]::GetEnvironmentVariable('WATHQ_API_KEY','User')")
        return 2
    if kind == KIND_CR and client.env != "production":
        print(MSG_NO_SANDBOX_CONVERSION)
        print("(the sandbox has no CR-to-unified conversion: use the 70… number)")
        return 2

    calls = 2 if kind == KIND_CR else 1
    what = "an old CR number" if kind == KIND_CR else "a unified number"
    print(f"environment: {client.env}; {what}; this sends {calls} billed request(s) to Wathq.")
    if not args.yes and not _confirm("Send? [y/N] "):
        print("nothing sent.")
        return 1

    step = "contract"
    try:
        national = number
        if kind == KIND_CR:
            step = "conversion"
            try:
                answer = client._fetch(conversion_path(number), what="conversion")
            except WathqError as exc:
                raise conversion_error(exc) from None
            usable, data = show("conversion (commercial-registration/crNationalNumber)", answer)
            if not usable:
                return 3
            national = unified_from(data)
            if not national:
                print("    -> no unified number in this answer; the contract call was not sent.")
                return 3
            print("    -> converted to a unified number (not shown).")
            step = "contract"
        answer = client._fetch(contract_path(national), {"language": args.language}, what="contract")
        usable, _ = show("contract (company-contract/info)", answer)
        return 0 if usable else 3
    except WathqError as exc:
        print(f"\n== {step} call failed: {exc.message}" + (f" (Wathq code {exc.code})" if exc.code else ""))
        return 1
    finally:
        print(f"\nrequests sent to Wathq: {client.sent}")


if __name__ == "__main__":
    sys.exit(main())
