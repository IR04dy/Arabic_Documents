"""Freeze-and-protect guard for the proofreading stage.

A text-only proofreader (ALLaM, AraT5, …) never sees the page image, so it must
NOT be trusted to rewrite high-value tokens — commercial-registration / ID
numbers, IBANs, emails, dates. It can "normalise" them into something wrong and
we'd have no way to know.

Policy (user's choice: "Freeze & protect"): proofread freely for prose, but if
the corrected text drops or alters ANY high-value token that was in the OCR
original — or invents a new one — reject the correction and keep the OCR text
verbatim for that unit. Grammar/spelling fixes are only accepted when every
protected value survives byte-for-byte.

`guard()` works on whatever unit the caller passes (we call it per page). It is
deterministic and model-agnostic, so it needs no GPU and is unit-testable.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

# Order matters: most-specific patterns first so e.g. a date isn't split into
# bare digit runs. Arabic-Indic digits (٠-٩) are treated like ASCII.
_PATTERNS = [
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",          # email
    r"[A-Z]{2}[0-9]{2}[A-Za-z0-9]{10,}",                          # IBAN
    r"[0-9٠-٩]{1,4}[/\-.][0-9٠-٩]{1,2}"
    r"[/\-.][0-9٠-٩]{1,4}",                              # date d/m/y
    r"[0-9٠-٩]{3,}",                                     # long digit run
]
_HIVAL = re.compile("|".join(f"(?:{p})" for p in _PATTERNS))


def protected_tokens(text: str) -> list[str]:
    """Every high-value token in `text`, in order of appearance (with repeats)."""
    return _HIVAL.findall(text or "")


def map_tokens(text: str, fn) -> str:
    """Rewrite every protected token of `text` through fn(token) -> token.

    One pass, each token independently, so a short digit run is never
    rewritten inside a longer one it happens to be a substring of. Used by the
    structurer to put a number the model converted to the other digit system
    back into the document's own form.
    """
    return _HIVAL.sub(lambda m: fn(m.group(0)), text or "")


@dataclass
class GuardResult:
    text: str            # the text to use downstream (proofed if safe, else original)
    reverted: bool       # True => proofed touched a value, so we kept the original
    changed: list        # protected tokens that differ between original and proofed
    protected_count: int # how many high-value tokens were guarded


def guard(original: str, proofed: str) -> GuardResult:
    """Accept `proofed` only if it preserves every protected token of `original`.

    A correction is rejected (and `original` kept) when the multiset of protected
    tokens changes at all — a value was altered, dropped, or invented.
    """
    orig_tokens = protected_tokens(original)
    proof_tokens = protected_tokens(proofed)
    co, cp = Counter(orig_tokens), Counter(proof_tokens)
    if co == cp:
        return GuardResult(proofed, False, [], len(orig_tokens))
    # Symmetric difference of the multisets = what the proofreader changed.
    changed = sorted((co - cp).keys() | (cp - co).keys())
    return GuardResult(original, True, changed, len(orig_tokens))


if __name__ == "__main__":                       # quick self-test: python guard.py
    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)

    # 1) prose fixed, all values intact -> accepted
    o = "عقد عمل غير محدد المدة\nرقم 1128927371 بتاريخ 12/05/2024"
    p = "عقد عمل غير محدّد المدة\nرقم 1128927371 بتاريخ 12/05/2024"
    r = guard(o, p)
    check("prose fix accepted", r.text == p and not r.reverted and r.protected_count == 2)

    # 2) a digit altered -> reverted to original
    o2 = "السجل التجاري 1128927371"
    p2 = "السجل التجاري 11289273371"          # model changed the CR number
    r2 = guard(o2, p2)
    check("altered number reverted", r2.text == o2 and r2.reverted
          and "1128927371" in r2.changed and "11289273371" in r2.changed)

    # 3) Arabic-Indic digits protected
    o3 = "الهاتف ٠٥٥١٢٣٤٥٦٧"
    p3 = "الهاتف ٠٥٥١٢٣٤٥٦٨"
    check("arabic-indic protected", guard(o3, p3).reverted)

    # 4) email + IBAN preserved -> accepted even with prose edits
    o4 = "البريد a.b@x.com والحساب SA0380000000608010167519 صحيح"
    p4 = "البريد a.b@x.com والحساب SA0380000000608010167519 صحيحٌ"
    check("email+iban preserved", not guard(o4, p4).reverted)

    # 5) invented number -> reverted
    check("invented number reverted",
          guard("لا يوجد رقم هنا", "الرقم 123456").reverted)

    # 6) no protected tokens -> prose freely accepted
    check("pure prose accepted",
          not guard("جملة فيها خطاء", "جملة فيها خطأ").reverted)
