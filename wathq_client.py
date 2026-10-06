"""Outbound HTTPS client for Wathq (developer.wathq.sa). No FastAPI imports.

This is the app's only call to the internet. Every other service in the
project talks to a process on this machine; this one sends a 10-digit
commercial number to api.wathq.sa and pays for the answer, so it is fenced
more tightly than the local clients:

* The host is fixed. Callers pick a product and a validated number, never a
  URL, so nothing upstream of here can point the key at another server.
* The key is read on every call (WATHQ_API_KEY, or the file named by
  WATHQ_API_KEY_FILE), sent only as Wathq's `apiKey` header, attached as an
  unredirected header, and never logged or put in an exception. Only a key
  passed to the constructor (the tests do) is kept on the object. Redirects
  are refused outright. (urllib title-cases header names, so the wire shows
  `Apikey`; Wathq's gateway reads it case-insensitively — checked.)
* TLS is always verified against the Windows certificate store, plus the
  root in WATHQ_CA_BUNDLE when a corporate network inspects TLS. There is no
  switch to turn verification off.
* Environment proxies (HTTPS_PROXY…) are ignored like the other clients do;
  a network that needs one names it in WATHQ_PROXY_URL.
* Errors carry an Arabic message, an HTTP status for the app's own answer,
  and at most Wathq's dotted error code (e.g. 404.2.1) — never the body.

A 2xx answer is turned into JSON by ONE function, `decode_answer`, which the
app and `wathq_probe.py` both call, so the diagnostic can't disagree with the
app. When an answer can't be used, `describe` says what it LOOKED like —
sizes, types, encodings, a parser's complaint — and never what it said.

Endpoints follow Wathq's published Swagger files: Company Contracts v2.8.0
(basePath /company-contract) and Commercial Registration v6.15.0 (basePath
/commercial-registration, used only to turn an old CR number into the
unified 700 number the contracts API requires).
"""

from __future__ import annotations

import codecs
import http.client
import json
import os
import re
import socket
import ssl
import threading
import zlib
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import (HTTPRedirectHandler, HTTPSHandler, ProxyHandler,
                            Request, build_opener)

HOST = "api.wathq.sa"
BASES = {
    "production": f"https://{HOST}",
    "sandbox": f"https://{HOST}/sandbox",
}
CONTRACTS = "/company-contract"
REGISTRATION = "/commercial-registration"

MAX_RESPONSE = 4 * 1024 * 1024       # a contract with long articles is ~100 KB
MAX_ERROR_BODY = 4 * 1024            # enough for {"code","message"}
DEFAULT_TIMEOUT = 20.0               # per socket operation (urllib semantics)

UNIFIED = re.compile(r"70[0-9]{8}\Z")          # الرقم الوطني الموحد
# Old CR numbers start with the issuing office's code (1010 Riyadh, 2050
# Dammam, 4030 Jeddah, 5850 Abha…); the 7 series is the unified number. A
# 71…–79… number is neither, and sending it to the paid conversion only
# buys a 400.1.8 ("The ID must be commercial registration number").
LEGACY_CR = re.compile(r"[1-6][0-9]{9}\Z")      # رقم السجل التجاري
_CODE = re.compile(r"[1-5][0-9]{2}\.[0-9]{1,2}\.[0-9]{1,2}\Z")
_KEY = re.compile(r"[\x21-\x7e]{8,512}\Z")       # printable ASCII, no spaces
_LANGS = ("ar", "en")

MSG_NOT_CONFIGURED = "لم يُضبط مفتاح وثق. عيّن المتغير WATHQ_API_KEY ثم أعد تشغيل التطبيق."
MSG_KEY_FILE = "تعذّرت قراءة ملف مفتاح وثق المحدد في WATHQ_API_KEY_FILE."
MSG_KEY_FORMAT = "قيمة مفتاح وثق غير صالحة: يجب أن تكون نصًا لاتينيًا بلا مسافات."
MSG_BAD_KEY = "رفضت وثق مفتاح الربط. تحقّق من قيمة WATHQ_API_KEY في حسابك على بوابة وثق."
MSG_FORBIDDEN = "اشتراكك في وثق لا يشمل هذه الخدمة. أضفها إلى تطبيقك في بوابة وثق ثم أعد المحاولة."
MSG_NOT_FOUND = "لا توجد بيانات لهذا الرقم في وثق."
MSG_INVALID_INPUT = "رفضت وثق الرقم المُرسل. تأكد أنه الرقم الوطني الموحد المكوّن من ١٠ أرقام."
MSG_RATE = "تجاوزت حد الاستعلامات المسموح في باقتك على وثق. انتظر قليلًا ثم أعد المحاولة."
MSG_UPSTREAM = "خدمة وثق لا تستجيب كما ينبغي الآن. أعد المحاولة لاحقًا."
# Observed live (2026-10-06): several products answer a wrong or unknown number
# with HTTP 500 and an empty JSON body instead of a 404.
MSG_EMPTY_500 = ("ردّت وثق بخطأ داخلي (500) بلا أي تفاصيل. يحدث هذا في عدة خدمات عندما لا يوجد سجل "
                 "مطابق للمدخلات؛ تأكد من الرقم ثم أعد المحاولة.")
MSG_UNREACHABLE = "تعذّر الاتصال بخدمة وثق. تحقّق من اتصال الجهاز بالإنترنت أو من إعداد WATHQ_PROXY_URL."
MSG_TIMEOUT = "انتهت مهلة انتظار ردّ وثق. أعد المحاولة."
MSG_TLS = "تعذّر التحقق من شهادة خدمة وثق. إن كانت شبكتك تفحص الاتصالات المشفّرة فعيّن WATHQ_CA_BUNDLE."
MSG_TLS_HANDSHAKE = "تعذّر إنشاء اتصال آمن مع وثق. أعد المحاولة، وإن تكرر فتحقّق من إعدادات الشبكة أو الوكيل."
MSG_INVALID = "ردّ وثق غير مفهوم. أعد المحاولة لاحقًا."
MSG_TOO_LARGE = "ردّ وثق أكبر من الحد المسموح."
MSG_EMPTY = "أعادت وثق ردًا فارغًا لهذا الطلب. قد لا تتوفر لديها بيانات لهذا الرقم."
MSG_CONVERT_FORBIDDEN = ("تحويل رقم السجل التجاري القديم يحتاج اشتراكًا في منتج «السجل التجاري "
                         "(التشريعات الجديدة)» في وثق. أضفه إلى تطبيقك، أو أدخل الرقم الوطني "
                         "الموحد الذي يبدأ بـ ٧٠٠ مباشرة.")
MSG_CONVERT_REJECTED = ("رفضت وثق رقم السجل التجاري هذا. أدخل الرقم الوطني الموحد الذي يبدأ "
                        "بـ ٧٠٠ مباشرة.")
MSG_CONVERT_NOT_FOUND = "لم تجد وثق رقمًا وطنيًا موحدًا لرقم السجل التجاري هذا."
MSG_NO_SANDBOX_CONVERSION = ("تحويل رقم السجل القديم غير متاح في البيئة الاختبارية لوثق. "
                             "أدخل الرقم الوطني الموحد الذي يبدأ بـ ٧٠٠.")

_PROXY_HINT = "WATHQ_PROXY_URL must look like http://proxy.example:8080"


class WathqError(Exception):
    """A failed Wathq call: a safe Arabic message, the status the app should
    answer with, and Wathq's own error code when it sent a recognisable one."""

    def __init__(self, message: str, status: int = 502, code: str = "", detail: str = ""):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code
        # Wathq's own words for the error (digit runs redacted), for the user
        # to see why a service refused; "" when it sent none.
        self.detail = detail


class UnusableAnswer(WathqError):
    """A 2xx answer that isn't usable JSON. `reason` is a fixed English phrase
    and `detail` comes from describe(): both are safe to log or print."""

    def __init__(self, message: str, reason: str, detail: str, status: int = 502):
        super().__init__(message, status)
        self.reason, self.detail = reason, detail


class Answer(NamedTuple):
    """A 2xx answer as it arrived: status, headers, the bytes on the wire."""
    status: int
    headers: object
    wire: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                       # urllib raises HTTPError for the 3xx


def _check_proxy(url: str) -> str:
    # Every failure says the same fixed thing: urlsplit's own ValueError quotes
    # the netloc, which would put a proxy password in the log and the UI.
    url = url.strip()
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port      # .port validates the number
    except ValueError:
        raise ValueError(_PROXY_HINT) from None
    if (parts.scheme not in ("http", "https") or not host
            or any(ch.isspace() or ord(ch) < 32 for ch in url)
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError(_PROXY_HINT)
    return url


def _timeout(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("WATHQ_TIMEOUT must be a number of seconds") from None
    if not 3 <= value <= 120:
        raise ValueError("WATHQ_TIMEOUT must be between 3 and 120 seconds")
    return value


def _log(*parts) -> None:
    # Only fixed words, HTTP statuses, Wathq's dotted codes, describe()'s
    # shape-only summaries and Wathq's redacted error wording reach the log:
    # never the key, the number queried, or a value from a response.
    print("wathq:", *parts)


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_MESSAGE_JSON = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.){1,400})"')
_MESSAGE_XML = re.compile(r"<message>\s*([^<]{1,400}?)\s*</message>")


def public_message(text, limit: int = 300) -> str:
    """Wathq's error wording made safe to show and log: one line, control
    characters and tags dropped, any run of 9+ digits (an ID, a CR number)
    kept only by its last four."""
    text = " ".join(str(text or "").split())
    text = re.sub(r"<[^>]{0,200}>", " ", text).translate(_ARABIC_DIGITS)
    text = re.sub(r"\d{9,}", lambda m: "•" * (len(m.group()) - 4) + m.group()[-4:], text)
    return " ".join(text.split())[:limit]


def _flag_on(environ, name: str) -> bool:
    return str(environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


class WathqClient:
    """Calls Wathq for one environment (production or sandbox).

    `opener` is injectable for tests; everything else comes from the
    environment (or explicit arguments, which win). Construction does no I/O
    and does not require a key: `configured()` reports whether one is set,
    and a call without one fails with MSG_NOT_CONFIGURED.
    """

    def __init__(self, *, env: str | None = None, api_key: str | None = None,
                 key_file: str | os.PathLike | None = None, timeout=None,
                 proxy: str | None = None, ca_bundle: str | None = None,
                 opener=None, environ=None):
        environ = os.environ if environ is None else environ
        env = (env or environ.get("WATHQ_ENV") or "production").strip().lower()
        if env not in BASES:
            raise ValueError("WATHQ_ENV must be 'production' or 'sandbox'")
        self.env = env
        self._base = BASES[env]
        self._environ = environ
        # WATHQ_DEBUG=1: also log the start of every error body (digits
        # redacted), to see why a service refused. Off by default.
        self.debug = _flag_on(environ, "WATHQ_DEBUG")
        self._explicit_key = api_key
        key_file = key_file or environ.get("WATHQ_API_KEY_FILE") or None
        self._key_file = Path(key_file) if key_file else None
        self.timeout = _timeout(timeout if timeout is not None
                                else environ.get("WATHQ_TIMEOUT", DEFAULT_TIMEOUT))
        if opener is None:
            proxy = proxy or environ.get("WATHQ_PROXY_URL") or ""
            ca_bundle = ca_bundle or environ.get("WATHQ_CA_BUNDLE") or None
            context = ssl.create_default_context()      # the Windows store
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if ca_bundle:
                try:
                    context.load_verify_locations(cafile=ca_bundle)
                except OSError:                           # missing, a folder, not PEM
                    raise ValueError("WATHQ_CA_BUNDLE could not be loaded as a PEM "
                                     "certificate file") from None
            opener = build_opener(
                ProxyHandler({"https": _check_proxy(proxy)} if proxy else {}),
                HTTPSHandler(context=context), _NoRedirect())
        self._opener = opener
        self._lock = threading.Lock()
        self._sent = 0

    def __repr__(self) -> str:                      # never the key
        return f"WathqClient(env={self.env!r})"

    # ---- configuration ----------------------------------------------------
    @property
    def sent(self) -> int:
        """Requests this process has sent to Wathq. Each may be billed."""
        with self._lock:
            return self._sent

    def configured(self) -> bool:
        return not self.key_problem()

    def key_problem(self) -> str:
        """'' when a usable key is set, else the Arabic reason. Never the key."""
        try:
            self._key()
            return ""
        except WathqError as exc:
            return exc.message

    def _key(self) -> str:
        key = self._explicit_key
        if not key:
            key = self._environ.get("WATHQ_API_KEY") or ""
        if not key and self._key_file is not None:
            try:
                raw = self._key_file.read_bytes()
                # Windows PowerShell 5.1's `>` and Out-File write UTF-16.
                key = (raw.decode("utf-16") if raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
                       else raw.decode("utf-8-sig"))
            except (OSError, UnicodeError):
                raise WathqError(MSG_KEY_FILE, 503) from None
        key = key.strip()
        if not key:
            raise WathqError(MSG_NOT_CONFIGURED, 503)
        if not _KEY.fullmatch(key):
            raise WathqError(MSG_KEY_FORMAT, 503)
        return key

    # ---- transport --------------------------------------------------------
    def _fetch(self, path: str, query: dict | None = None, what: str = "request",
               headers: dict | None = None, absolute: bool = False) -> Answer:
        """One GET to Wathq. Returns the 2xx answer undecoded; raises
        WathqError for every transport or HTTP failure. `headers` carries
        parameters Wathq takes as headers (a person's id for /v2/info): like
        the key they are unredirected and never logged."""
        key = self._key()                          # before anything is counted
        extra = {}
        for name, value in (headers or {}).items():
            if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,40}", str(name))
                    or not re.fullmatch(r"[!-~]{1,100}", str(value))
                    or str(name).lower() in ("apikey", "host", "authorization", "cookie")):
                raise ValueError("invalid header parameter")
            extra[str(name)] = str(value)
        # A catalog path already names its environment (/sandbox/… or not);
        # the two built-in helpers' paths get this client's prefix.
        url = (f"https://{HOST}" if absolute else self._base) + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v not in (None, "")})
        request = Request(url, method="GET", headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",          # decode_answer copes if ignored
        })
        request.add_unredirected_header("apiKey", key)
        for name, value in extra.items():
            request.add_unredirected_header(name, value)
        del key
        with self._lock:
            self._sent += 1
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or 200
                headers = getattr(response, "headers", None) or {}
                wire = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            raise self._http_error(exc) from None
        except URLError as exc:
            raise self._transport_error(exc.reason) from None
        except http.client.IncompleteRead as exc:
            _log(what, "answer cut off in transit after", len(exc.partial), "bytes")
            raise WathqError(MSG_UPSTREAM, 502) from None
        except ssl.SSLError:
            _log("TLS error while reading")
            raise WathqError(MSG_TLS_HANDSHAKE, 502) from None
        except (TimeoutError, socket.timeout):
            _log("timeout while reading")
            raise WathqError(MSG_TIMEOUT, 504) from None
        except (http.client.HTTPException, OSError) as exc:
            _log("connection error:", type(exc).__name__)
            raise WathqError(MSG_UNREACHABLE, 503) from None
        answer = Answer(status, headers, wire)
        if len(wire) > MAX_RESPONSE:
            _log(what, "answer over", MAX_RESPONSE, "bytes")
            raise WathqError(MSG_TOO_LARGE, 502)
        declared = _int(_header(headers, "Content-Length"))
        if (declared is not None and declared > len(wire)
                and not _header(headers, "Transfer-Encoding")):
            _log(what, "answer cut off:", describe(answer))
            raise WathqError(MSG_UPSTREAM, 502)
        return answer

    def _transport_error(self, reason) -> WathqError:
        # These fail before the request is written — no DNS, nobody listening,
        # a TLS handshake or a proxy tunnel that never opened — so nothing
        # reached Wathq and nothing can have been billed.
        before_send = (isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLError))
                       or (isinstance(reason, OSError)
                           and str(reason).startswith("Tunnel connection failed")))
        if before_send:
            with self._lock:
                self._sent -= 1
        if isinstance(reason, ssl.SSLCertVerificationError):
            _log("TLS certificate verification failed")
            return WathqError(MSG_TLS, 502)
        if isinstance(reason, ssl.SSLError):
            _log("TLS handshake failed:", type(reason).__name__)
            return WathqError(MSG_TLS_HANDSHAKE, 502)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            _log("timeout while connecting")
            return WathqError(MSG_TIMEOUT, 504)
        _log("unreachable:", type(reason).__name__)
        return WathqError(MSG_UNREACHABLE, 503)

    def _get(self, path: str, query: dict | None = None, what: str = "request",
             headers: dict | None = None, absolute: bool = False):
        answer = self._fetch(path, query, what, headers, absolute)
        try:
            data, notes = decode_answer(answer)
        except UnusableAnswer as exc:
            _log(what, exc.reason + ":", exc.detail)
            raise
        if notes:
            _log(what, "answer decoded with:", ", ".join(notes))
        return data

    def _http_error(self, exc: HTTPError) -> WathqError:
        status = exc.code
        code, detail, raw, ctype = "", "", "", ""
        try:
            ctype = (exc.headers.get("Content-Type") or "").split(";")[0].strip() if exc.headers else ""
            raw = exc.read(MAX_ERROR_BODY).decode("utf-8", "replace")
            match = (re.search(r'"code"\s*:\s*"([0-9.]{5,10})"', raw)
                     or re.search(r"<code>\s*([0-9.]{5,10})\s*</code>", raw))
            if match and _CODE.fullmatch(match.group(1)):
                code = match.group(1)
            said = _MESSAGE_JSON.search(raw) or _MESSAGE_XML.search(raw)
            if said:
                detail = public_message(said.group(1).encode().decode("unicode_escape", "replace"))
        except Exception:
            pass
        finally:
            exc.close()
        _log("HTTP", status, code or "-", *(("|", detail) if detail else ()))
        if self.debug:
            _log("error body:", ctype or "no content-type", f"{len(raw)} chars:",
                 public_message(raw, 500) or "(empty)")
        error = error_for(status, code)
        if status == 500 and not code and not raw.strip():
            error = WathqError(MSG_EMPTY_500, 502)
        error.detail = detail
        return error

    # ---- generic ----------------------------------------------------------
    def call(self, base: str, path: str, query: dict | None = None,
             headers: dict | None = None, what: str = "request"):
        """GET base+path on this environment and return the decoded JSON.
        `base` is a product's base path from the catalog (never user input);
        `path` was built by the catalog from validated, quoted values."""
        if not re.fullmatch(r"(?:/[A-Za-z0-9._-]+)+", base or "") or not path.startswith("/"):
            raise ValueError("invalid Wathq path")
        if ".." in path or "//" in path or "?" in path or "#" in path:
            raise ValueError("invalid Wathq path")
        return self._get(base + path, query, what, headers, absolute=True)

    # ---- products ---------------------------------------------------------
    def company_contract(self, national_number: str, language: str = "ar",
                         copy_number: str | None = None):
        """GET /company-contract/info/{crNationalNumber}: the articles of
        association, partners, management, activities and clauses."""
        _require_unified(national_number)
        if language not in _LANGS:
            raise ValueError("language must be 'ar' or 'en'")
        if copy_number is not None and not re.fullmatch(r"[0-9]{1,5}", str(copy_number)):
            raise ValueError("copy_number must be 1 to 5 digits")
        return self._get(contract_path(national_number),
                         {"language": language, "copyNumber": copy_number}, what="contract")

    def national_number(self, cr_number: str) -> str:
        """GET /commercial-registration/crNationalNumber/{id}: the unified
        700 number for an old 10-digit CR number. Production only — the
        sandbox spec has no such endpoint."""
        if not LEGACY_CR.fullmatch(cr_number or ""):
            raise ValueError("cr_number must be 10 digits")
        if self.env != "production":
            raise WathqError(MSG_NO_SANDBOX_CONVERSION, 400)
        try:
            data = self._get(conversion_path(cr_number), what="conversion")
        except UnusableAnswer:
            raise
        except WathqError as exc:
            raise conversion_error(exc) from None
        value = unified_from(data)
        if not value:
            _log("conversion answer without a unified number; keys:", safe_keys(data))
            raise WathqError(MSG_INVALID, 502)
        return value


# ---------------------------------------------------------------- products

def contract_path(national_number: str) -> str:
    return f"{CONTRACTS}/info/{quote(national_number, safe='')}"


def conversion_path(cr_number: str) -> str:
    return f"{REGISTRATION}/crNationalNumber/{quote(cr_number, safe='')}"


def unified_from(data) -> str:
    """The unified number in a conversion answer, or ''."""
    value = data.get("crNationalNumber") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value).strip()
    return value if UNIFIED.fullmatch(value) else ""


def error_for(status: int | None, code: str = "") -> WathqError:
    """The app's error for a Wathq status and/or dotted code. The code wins:
    Wathq lists 404.2.1 under its 400 answers, and a 2xx can carry one too."""
    kind = code.split(".", 1)[0] if code else str(status or "")
    if kind == "404":
        return WathqError(MSG_NOT_FOUND, 404, code)
    if kind == "401":
        return WathqError(MSG_BAD_KEY, 502, code)
    if kind == "403":
        return WathqError(MSG_FORBIDDEN, 502, code)
    if kind == "429":
        return WathqError(MSG_RATE, 429, code)
    if kind == "400":
        return WathqError(MSG_INVALID_INPUT, 400, code)
    return WathqError(MSG_UPSTREAM, 502, code)


def code_in(data) -> str:
    """Wathq's dotted error code when a 2xx body is really an error, else ''."""
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else ""


def conversion_error(exc: WathqError) -> WathqError:
    """The contracts-worded messages, reworded for the conversion call — a
    different product with its own subscription and its own 400s."""
    if isinstance(exc, UnusableAnswer):
        return exc
    if exc.message is MSG_FORBIDDEN or exc.code.startswith("403"):
        return WathqError(MSG_CONVERT_FORBIDDEN, exc.status, exc.code)
    if exc.status == 400:
        return WathqError(MSG_CONVERT_REJECTED, 400, exc.code)
    if exc.status == 404:
        return WathqError(MSG_CONVERT_NOT_FOUND, 404, exc.code)
    return exc


# ---------------------------------------------------------------- decoding

def decode_answer(answer: Answer) -> tuple:
    """(JSON value, notes) for a 2xx answer, or UnusableAnswer.

    The one decoding path: the app (`_get`) and `wathq_probe.py` both call
    it. `notes` are fixed phrases saying what had to be undone (compression,
    a legacy charset, double encoding) — safe to log."""
    notes: list = []
    body = _decompress(answer, notes)
    if answer.status == 204 or not body.strip():
        raise UnusableAnswer(MSG_EMPTY, "answer is empty", describe(answer, body))
    try:
        data = json.loads(body)                    # UTF-8/16/32 and a BOM, detected
    except UnicodeDecodeError as first:
        # Not Unicode. Only now does a declared legacy charset get a say — a
        # label must never re-decode valid UTF-8 into mojibake.
        charset = _charset(_header(answer.headers, "Content-Type"))
        if not charset or charset.replace("_", "-").startswith("utf"):
            raise UnusableAnswer(MSG_INVALID, "answer is not JSON",
                                 describe(answer, body, first)) from None
        try:
            data = json.loads(body.decode(charset).lstrip("﻿"))
        except (ValueError, UnicodeError, LookupError, RecursionError) as exc:
            raise UnusableAnswer(MSG_INVALID, "answer is not JSON",
                                 describe(answer, body, exc)) from None
        notes.append(f"declared charset {charset}")
    except (ValueError, RecursionError) as exc:
        raise UnusableAnswer(MSG_INVALID, "answer is not JSON", describe(answer, body, exc)) from None
    if isinstance(data, str) and data.strip()[:1] in ("{", "["):
        try:
            data = json.loads(data)
            notes.append("double-encoded JSON")
        except (ValueError, RecursionError):
            pass
    return data, notes


def _header(headers, name: str) -> str:
    try:
        value = headers.get(name) or ""
    except Exception:
        return ""
    return str(value).strip()


def _int(text: str):
    return int(text) if text.isascii() and text.isdigit() else None


def _charset(content_type: str) -> str:
    match = re.search(r"charset\s*=\s*\"?([A-Za-z0-9._:-]{1,40})", content_type or "", re.I)
    return match.group(1).lower() if match else ""


_GZIP_MAGIC = b"\x1f\x8b"


def _inflate(data: bytes, wbits: int, answer: Answer) -> bytes:
    """Decompress with a hard size cap. A stream that stops early is a
    truncated answer, not an empty one; a second gzip member is followed;
    anything else after the end is refused rather than silently dropped."""
    out = bytearray()
    rest = data
    while True:
        stream = zlib.decompressobj(wbits)
        out += stream.decompress(rest, MAX_RESPONSE + 1 - len(out))
        if len(out) > MAX_RESPONSE or stream.unconsumed_tail:
            raise UnusableAnswer(MSG_TOO_LARGE, "answer too large once decompressed", describe(answer))
        if not stream.eof:
            raise UnusableAnswer(MSG_UPSTREAM, "compressed answer is cut off", describe(answer))
        rest = stream.unused_data
        if not rest.strip(b" \t\r\n\x00"):
            return bytes(out)
        if wbits == 16 + zlib.MAX_WBITS and rest[:2] == _GZIP_MAGIC:
            continue                                   # another gzip member
        raise UnusableAnswer(MSG_INVALID, "bytes after the compressed answer", describe(answer))


def _decompress(answer: Answer, notes: list) -> bytes:
    """The body minus any compression. We ask for `identity`; a gateway that
    compresses anyway, gzips without saying so, or says so without doing it
    shouldn't turn a good answer into 'not JSON'."""
    body = answer.wire
    encoding = _header(answer.headers, "Content-Encoding").lower()
    if encoding not in ("", "identity", "gzip", "x-gzip", "deflate"):
        if body.lstrip()[:1] in (b"{", b"["):
            notes.append("ignored a Content-Encoding label on plain JSON")
            return body
        raise UnusableAnswer(MSG_INVALID, "answer uses an unsupported encoding", describe(answer))
    for layer in range(3):
        if layer == 0 and encoding == "deflate":
            kind = "deflate"
        elif (layer == 0 and encoding in ("gzip", "x-gzip")) or body[:2] == _GZIP_MAGIC:
            kind = "gzip"
        else:
            return body
        if layer == 2:
            raise UnusableAnswer(MSG_INVALID, "answer is compressed more than twice", describe(answer))
        try:
            if kind == "gzip":
                body = _inflate(body, 16 + zlib.MAX_WBITS, answer)
            else:
                try:
                    body = _inflate(body, zlib.MAX_WBITS, answer)       # zlib-wrapped, per the RFC
                except zlib.error:
                    body = _inflate(body, -zlib.MAX_WBITS, answer)      # raw deflate, as some send
        except zlib.error:
            if layer == 0 and body.lstrip()[:1] in (b"{", b"["):
                notes.append("ignored a Content-Encoding label on plain JSON")
                return body
            raise UnusableAnswer(MSG_INVALID, "answer could not be decompressed",
                                 describe(answer)) from None
        notes.append(kind)
    return body


# ---------------------------------------------------------------- describing

_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,59}\Z")


def safe_key(key) -> str:
    """A field name as-is when it looks like a field name; otherwise only its
    length and what it contains — a key can itself be an ID or a name."""
    text = str(key)
    digits = sum(ch.isdigit() for ch in text)
    if _SAFE_KEY.match(text) and digits < 6:
        return text
    extra = ", non-ascii" if not text.isascii() else ""
    return f"<{len(text)}-char key, {digits} digits{extra}>"


def safe_keys(data) -> str:
    if not isinstance(data, dict):
        return type(data).__name__
    return "[" + ", ".join(safe_key(k) for k in list(data)[:10]) + "]"


def _safe_header(headers, name: str, limit: int) -> str:
    value = _header(headers, name)
    value = "".join(ch for ch in value if " " <= ch <= "~")[:limit]
    return value


def _starts(body: bytes) -> str:
    text = body.lstrip()
    if not text:
        return "empty"
    first = text[:1]
    named = {b"<": "markup", b"{": "json-object", b"[": "json-array", b'"': "json-string"}
    if first in named:
        return named[first]
    head = text[:4]
    if head[:2] == _GZIP_MAGIC or any(b < 0x20 and b not in (9, 10, 13) for b in head):
        return "binary first-bytes=" + head.hex()      # a magic number, not text
    if first.isdigit() or first == b"-":
        return "digits"
    if first.isascii() and first.isalpha():
        return "ascii-text"
    return "non-ascii-text" if first[0] >= 0x80 else "other-text"


_HTML_START = re.compile(rb"\s*(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html)[\s>]", re.I | re.S)
_TITLE = re.compile(rb"<title(?=[\s>/])[^>]*>(.{0,300}?)</title\s*>", re.I | re.S)
_ROOT = re.compile(rb"(?:\s*(?:<\?.*?\?>|<!--.*?-->|<!doctype[^>]*>))*\s*<([A-Za-z_][\w:.-]{0,40})",
                   re.I | re.S)


def _safe_title(raw: bytes) -> str:
    """An HTML page title, minus anything that could be data: non-ASCII is
    dropped and every run of 3+ digits becomes '#'. 'Request Rejected' and
    'Login' survive; an Arabic company name or an ID number doesn't."""
    text = raw.decode("utf-8", "replace")
    ascii_part = "".join(ch for ch in text if " " <= ch <= "~")
    ascii_part = re.sub(r"[0-9]{3,}", "#", ascii_part)
    ascii_part = re.sub(r"\s+", " ", ascii_part).strip()[:60]
    dropped = len(text.strip()) - len(ascii_part)
    return repr(ascii_part) + (f" (+{dropped} chars not shown)" if dropped > 0 else "")


def describe(answer: Answer, body: bytes | None = None, exc: Exception | None = None) -> str:
    """What an answer LOOKED like, never what it said: status, content type,
    encoding and lengths, how the body starts, an HTML page's title (data
    stripped), an XML root element's name, a JSON parser's complaint and
    position, and Wathq's per-request reference (a GUID their support can
    trace). Safe to log and to paste anywhere: no field values, no key."""
    wire = answer.wire
    body = wire if body is None else body
    parts = [f"status={answer.status}",
             f"type={_safe_header(answer.headers, 'Content-Type', 60) or '-'}",
             f"encoding={_safe_header(answer.headers, 'Content-Encoding', 20) or '-'}",
             f"length={_safe_header(answer.headers, 'Content-Length', 12) or '-'}",
             f"te={_safe_header(answer.headers, 'Transfer-Encoding', 20) or '-'}",
             f"wire-bytes={len(wire)}"]
    if body is not wire:
        parts.append(f"decoded-bytes={len(body)}")
    parts.append("starts=" + _starts(body))
    head = body[:20000]
    content_type = _header(answer.headers, "Content-Type").lower()
    if "html" in content_type or _HTML_START.match(head):
        title = _TITLE.search(head)
        parts.append("html-title=" + (_safe_title(title.group(1)) if title else "none"))
    elif body.lstrip()[:1] == b"<":
        root = _ROOT.match(head)
        if root:
            name = root.group(1).decode("ascii", "replace")     # _ROOT allows ASCII names only
            parts.append("xml-root=" + (name if sum(c.isdigit() for c in name) < 6 else safe_key(name)))
    if isinstance(exc, json.JSONDecodeError):
        # The parser's messages are fixed phrases ("Expecting value", "Extra
        # data"); the only input they ever quote is one escape character.
        parts.append(f"json-error={exc.msg[:40]!r}@{exc.pos}")
    elif isinstance(exc, UnicodeDecodeError):
        where = f"@{exc.start}"
        if isinstance(exc.object, (bytes, bytearray)) and 0 <= exc.start < len(exc.object):
            where += f" byte={exc.object[exc.start]:02x}"
        parts.append(f"error=UnicodeDecodeError({exc.encoding}){where}")
    elif exc is not None:
        parts.append("error=" + type(exc).__name__)
    ref = _header(answer.headers, "THIQAH-API-ApiMsgRef")
    if re.fullmatch(r"[0-9A-Fa-f-]{8,64}", ref):
        parts.append("wathq-ref=" + ref)
    return " ".join(parts)


def _require_unified(number: str) -> None:
    if not isinstance(number, str) or not UNIFIED.fullmatch(number):
        raise ValueError("a unified national number is 10 digits starting with 70")
