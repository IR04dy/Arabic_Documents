"""Routes under /wathq: the "Verify data" (التحقق من البيانات) tab's backend.

    GET  /wathq/ui.js, /wathq/ui.css     the tab's assets (any allowed host)
    GET  /wathq/status                   configured? which environment? calls sent
    POST /wathq/company-contract         {"number": "...", "language": "ar"|"en"}
    GET  /wathq/catalog                  every product and query, with prices
    POST /wathq/query                    {"endpoint": "cr.info", "inputs": {...}}
    POST /wathq/suggest                  {"struct": <the /structure result>} ->
                                         the services the local model ranks for
                                         this document, inputs pre-filled; no
                                         Wathq call
    POST /wathq/compare                  {"struct": ..., "result": <a /query answer>}
                                         -> a verdict per register line; no
                                         Wathq call

Every lookup is a paid call to an outside service, and the app has no login,
so the data routes are fenced:

* local-only: they answer only when the TCP peer is this machine AND the
  request is addressed to 127.0.0.1 / localhost — the Host header alone can be
  forged by a LAN client when APP_ALLOWED_HOSTS opens the app to a network —
  unless WATHQ_ALLOW_REMOTE=1 says otherwise;
* same-origin: a lookup must come from this app's own page — another site
  open in the same browser cannot spend the quota (Sec-Fetch-Site, Origin,
  and a custom header that forces a CORS preflight nobody answers);
* POST with the number in the body, so it never lands in an access log;
* one lookup at a time (409 while one runs), and answers are cached in memory
  for WATHQ_CACHE_SECONDS (default 900) so re-opening the same company does
  not pay twice. Nothing is written to disk.

Without a key the tab still loads and says what to set; the rest of the app
never depends on this router.
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from wathq_catalog import Catalog, CatalogError, get_catalog
from wathq_client import WathqClient, WathqError, code_in, error_for, public_message, safe_keys
from wathq_compare import compare as compare_answer
from wathq_suggest import suggest as suggest_services
from wathq_verify import KIND_CR, parse_number, shape_contract
from wathq_view import build_view

MAX_BODY = 4 * 1024
RIYADH = timezone(timedelta(hours=3))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

MSG_LOCAL_ONLY = "التحقق عبر وثق متاح من هذا الجهاز فقط."
MSG_CROSS_SITE = "رُفض الطلب لأنه لم يصدر من صفحة التطبيق نفسها."
MSG_BUSY = "يوجد استعلام قيد التنفيذ. انتظر انتهاءه ثم أعد المحاولة."
MSG_BAD_REQUEST = "طلب غير صالح."
MSG_TOO_LARGE = "حجم الطلب أكبر من الحد المسموح."
MSG_JSON = "يجب إرسال الطلب بصيغة JSON."
MSG_SHAPE = "ردّ وثق لا يحتوي على بيانات عقد يمكن عرضها."
MSG_FAILED = "تعذّر إكمال الاستعلام. أعد المحاولة."
MSG_CATALOG = "تعذّر تحميل قائمة خدمات وثق (templates/wathq/catalog.yaml). راجع سجل التطبيق."
MSG_NOT_IN_SANDBOX = "هذه الخدمة غير متاحة في البيئة الاختبارية لوثق. عيّن WATHQ_ENV=production لاستخدامها."
MSG_VIEW = "تعذّر عرض ردّ وثق لهذه الخدمة."
MSG_MODEL = "النموذج المحلي غير متاح الآن لترتيب خدمات التحقق. أعد المحاولة بعد قليل."
MSG_COMPARE_MODEL = "النموذج المحلي غير متاح الآن للمقارنة. أعد المحاولة بعد قليل."
MAX_QUERY_BODY = 8 * 1024
MAX_SUGGEST_BODY = 256 * 1024          # a structured result, never the OCR text
MAX_COMPARE_BODY = 512 * 1024          # a structured result plus one register answer
LOOKUP_TTL = 24 * 3600
MSG_CA_BUNDLE = "تعذّر تحميل ملف الشهادة المحدد في WATHQ_CA_BUNDLE. تحقّق من المسار وأنه بصيغة PEM."


def _flag(environ, name: str) -> bool:
    return (environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def _cache_seconds(environ) -> int:
    try:
        value = int(environ.get("WATHQ_CACHE_SECONDS", "900"))
    except ValueError:
        return 900
    return max(0, min(value, 24 * 3600))


class _Cache:
    """A small in-memory TTL cache. Values are the shaped (masked) answers."""

    def __init__(self, ttl: int, size: int = 64, clock=time.monotonic):
        self.ttl, self.size, self.clock = ttl, size, clock
        self._items: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        if not self.ttl:
            return None
        with self._lock:
            hit = self._items.get(key)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
            self._items.pop(key, None)
            return None

    def put(self, key, value) -> None:
        if not self.ttl:
            return
        with self._lock:
            if len(self._items) >= self.size:
                oldest = min(self._items, key=lambda k: self._items[k][0])
                self._items.pop(oldest, None)
            self._items[key] = (self.clock(), value)


def _error(message: str, status: int, code: str = "", sent: int | None = None,
           detail: str = "") -> JSONResponse:
    body = {"error": message}
    if code:
        body["code"] = code
    if detail:                        # Wathq's own words, digits redacted
        body["detail"] = detail
    if sent is not None:              # a failed call may still have been billed
        body["sent"] = sent
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _ask_local_model(prompt: str, options: list) -> str:
    """One multiple-choice question to the structurer (Qwen): the answer is
    forced to one of ``options`` by the JSON schema."""
    import llm
    schema = {"type": "object", "required": ["service"],
              "properties": {"service": {"type": "string", "enum": list(options)}}}
    content, _ = llm.chat_json([{"role": "user", "content": prompt}], schema, max_tokens=20)
    return json.loads(content).get("service")


def _judge_local_model(prompt: str, schema: dict, max_tokens: int) -> dict:
    """One constrained question to the structurer; the parsed JSON answer."""
    import llm
    content, _ = llm.chat_json([{"role": "user", "content": prompt}], schema, max_tokens=max_tokens)
    return json.loads(content)


def create_router(client: WathqClient | None = None, *, environ=None, clock=time.monotonic,
                  catalog: Catalog | None = None, ask=None, judge=None) -> APIRouter:
    environ = os.environ if environ is None else environ
    ask = ask or _ask_local_model
    judge = judge or _judge_local_model
    here = Path(__file__).parent
    router = APIRouter(prefix="/wathq", tags=["wathq"])
    setup_error = ""
    if client is None:
        try:
            client = WathqClient(environ=environ)
        except ValueError as exc:            # a bad WATHQ_* setting: say so, don't break the app
            print("wathq: configuration error:", exc if str(exc).startswith("WATHQ_") else type(exc).__name__)
            # Our own messages all name the setting (WATHQ_…); anything else
            # (a library's ValueError) could quote a value, so it isn't shown.
            text = str(exc)
            if "CA_BUNDLE" in text:
                setup_error = MSG_CA_BUNDLE
            elif text.startswith("WATHQ_"):
                setup_error = "إعداد وثق غير صالح: " + text
            else:
                setup_error = "إعداد وثق غير صالح."
        except OSError as exc:               # backstop: never let this tab stop the app
            print("wathq: configuration error:", type(exc).__name__)
            setup_error = MSG_CA_BUNDLE
    allow_remote = _flag(environ, "WATHQ_ALLOW_REMOTE")
    cache = _Cache(_cache_seconds(environ), clock=clock)
    # Reference lists change rarely and each read is billed: keep them a day
    # (unless caching is off altogether).
    lookup_cache = _Cache(LOOKUP_TTL if cache.ttl else 0, clock=clock)
    slot = threading.BoundedSemaphore(1)
    router.client = client
    catalog_error = ""
    if catalog is None:
        try:
            catalog = get_catalog()
        except CatalogError as exc:                            # a precise, safe message
            print("wathq: catalog unavailable:", str(exc)[:300])
            catalog_error = MSG_CATALOG
        except Exception as exc:                               # never let this tab stop the app
            print("wathq: catalog unavailable:", type(exc).__name__)
            catalog_error = MSG_CATALOG
    router.catalog = catalog

    def is_local(request: Request) -> bool:
        if (request.url.hostname or "").lower() not in LOCAL_HOSTS:
            return False
        # request.url comes from the Host header, which any client can set. The
        # TCP peer can't be faked from the LAN (uvicorn honours X-Forwarded-For
        # only from 127.0.0.1 by default).
        peer = request.client.host if request.client else ""
        try:
            return ipaddress.ip_address(peer).is_loopback
        except ValueError:
            return False

    def guard(request: Request, *, write: bool) -> JSONResponse | None:
        if not allow_remote and not is_local(request):
            return _error(MSG_LOCAL_ONLY, 403)
        if not write:
            return None
        site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return _error(MSG_CROSS_SITE, 403)
        origin = request.headers.get("origin")
        if origin is not None:
            own = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin.strip().rstrip("/").lower() != own.lower():
                return _error(MSG_CROSS_SITE, 403)
        if request.headers.get("x-wathq-request") != "1":
            return _error(MSG_CROSS_SITE, 403)
        return None

    @router.get("/ui.js")
    def script():
        return FileResponse(here / "wathq_ui.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @router.get("/ui.css")
    def stylesheet():
        return FileResponse(here / "wathq.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @router.get("/status")
    def status(request: Request):
        refused = guard(request, write=False)
        if refused:
            return refused
        body = {"configured": False, "env": "", "sent": 0,
                "cache_seconds": cache.ttl, "reason": setup_error}
        if client is not None:
            problem = client.key_problem()
            body.update(env=client.env, sent=client.sent, configured=not problem, reason=problem)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @router.post("/company-contract")
    async def company_contract(request: Request):
        refused = guard(request, write=True)
        if refused:
            return refused
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(MSG_JSON, 415)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_BODY:
                return _error(MSG_TOO_LARGE, 413)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            return _error(MSG_BAD_REQUEST, 400)
        language = payload.get("language", "ar")
        if language not in ("ar", "en"):
            return _error(MSG_BAD_REQUEST, 400)
        try:
            number, kind = parse_number(payload.get("number"))
        except ValueError as exc:
            return _error(str(exc), 400)
        if client is None:
            return _error(setup_error or MSG_FAILED, 503)
        if not slot.acquire(blocking=False):
            return _error(MSG_BUSY, 409)
        try:
            return await run_in_threadpool(_lookup, number, kind, language)
        finally:
            slot.release()

    def _lookup(number: str, kind: str, language: str) -> JSONResponse:
        sent_before = client.sent
        try:
            national, converted = number, False
            if kind == KIND_CR:
                cached = cache.get((client.env, "convert", number))
                if cached:
                    national = cached
                else:
                    national = client.national_number(number)
                    cache.put((client.env, "convert", number), national)
                converted = True
            key = (client.env, "contract", national, language)
            shaped = cache.get(key)
            cached = shaped is not None
            if not cached:
                answer = client.company_contract(national, language)
                code = code_in(answer)
                if code:                      # a 2xx carrying Wathq's {code, message}
                    print("wathq: contract answer is an error body:", code)
                    exc = error_for(None, code)
                    return _error(exc.message, exc.status, code, sent=client.sent,
                                  detail=public_message(answer.get("message")))
                try:
                    shaped = shape_contract(answer)
                    shaped["fetched_at"] = datetime.now(RIYADH).isoformat(timespec="seconds")
                    # Must survive serialisation before it is cached: a value the
                    # response can't encode would otherwise fail on every repeat.
                    json.dumps(shaped, ensure_ascii=False).encode("utf-8")
                except (ValueError, UnicodeError):
                    print("wathq: contract answer has no usable entity; keys:", safe_keys(answer))
                    return _error(MSG_SHAPE, 502, sent=client.sent)
                cache.put(key, shaped)
        except WathqError as exc:
            return _error(exc.message, exc.status, exc.code, sent=client.sent, detail=exc.detail)
        except ValueError:
            return _error(MSG_BAD_REQUEST, 400, sent=client.sent)
        body = {
            "source": "wathq",
            "product": "company_contract",
            "env": client.env,
            "query": {"input": number, "kind": kind, "national_number": national,
                      "converted": converted},
            "cached": cached,
            "calls_used": client.sent - sent_before,
            "sent": client.sent,
            **shaped,
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ every product
    @router.get("/catalog")
    def catalog_route(request: Request):
        refused = guard(request, write=False)
        if refused:
            return refused
        if catalog is None:
            return _error(catalog_error or MSG_CATALOG, 503)
        env = client.env if client is not None else "production"
        return JSONResponse(catalog.public(env), headers={"Cache-Control": "no-store"})

    @router.post("/query")
    async def query(request: Request):
        refused = guard(request, write=True)
        if refused:
            return refused
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(MSG_JSON, 415)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_QUERY_BODY:
                return _error(MSG_TOO_LARGE, 413)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            return _error(MSG_BAD_REQUEST, 400)
        if catalog is None:
            return _error(catalog_error or MSG_CATALOG, 503)
        try:
            req = catalog.request(payload.get("endpoint"), payload.get("inputs") or {},
                                  payload.get("language", "ar"),
                                  env=client.env if client is not None else "production")
        except ValueError as exc:
            return _error(str(exc), 400)
        if client is None:
            return _error(setup_error or MSG_FAILED, 503)
        if not catalog.available(req.endpoint, client.env):
            return _error(MSG_NOT_IN_SANDBOX, 400)
        if not slot.acquire(blocking=False):
            return _error(MSG_BUSY, 409)
        try:
            return await run_in_threadpool(_run_query, req)
        finally:
            slot.release()

    @router.post("/suggest")
    async def suggest_route(request: Request):
        """Which services can verify this document, in the local model's order."""
        refused = guard(request, write=True)
        if refused:
            return refused
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(MSG_JSON, 415)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_SUGGEST_BODY:
                return _error(MSG_TOO_LARGE, 413)
        try:
            payload = json.loads(raw)
            struct = payload.get("struct") if isinstance(payload, dict) else None
            if not isinstance(struct, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            return _error(MSG_BAD_REQUEST, 400)
        try:
            result = await run_in_threadpool(suggest_services, struct, ask)
        except Exception as exc:              # model busy, not loaded, or an odd answer
            print("wathq: suggest failed:", type(exc).__name__)
            return _error(MSG_MODEL, 503)
        env = client.env if client is not None else "production"
        described = {}
        if catalog is not None:
            for p in catalog.public(env)["products"]:
                for e in p["endpoints"]:
                    described[e["id"]] = e
        for s in result["services"]:
            e = described.get(s["endpoint"])
            if e is None:
                s.update(endpoint_label=s["label"], price=-1, available=False, inputs_spec=[],
                         one_of=[], converts=False)
            else:
                s.update(endpoint_label=e["label"], price=e["price"], available=e["available"],
                         inputs_spec=e["inputs"], one_of=e.get("one_of") or [],
                         converts=bool(e.get("converts")))
        result["env"] = env
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/compare")
    async def compare_route(request: Request):
        """Document lines against one register answer: a verdict per line."""
        refused = guard(request, write=True)
        if refused:
            return refused
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(MSG_JSON, 415)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_COMPARE_BODY:
                return _error(MSG_TOO_LARGE, 413)
        try:
            payload = json.loads(raw)
            struct = payload.get("struct") if isinstance(payload, dict) else None
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(struct, dict) or not isinstance(result, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            return _error(MSG_BAD_REQUEST, 400)
        try:
            body = await run_in_threadpool(compare_answer, struct, result, judge)
        except Exception as exc:              # model busy, not loaded, or an odd answer
            print("wathq: compare failed:", type(exc).__name__)
            return _error(MSG_COMPARE_MODEL, 503)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    def _run_query(req) -> JSONResponse:
        ep = req.endpoint
        sent_before = client.sent
        store = lookup_cache if ep.lookup else cache
        base = catalog.base(ep, client.env)
        unified, legacy_used = None, False
        try:
            legacy = req.conversion_value()
            if legacy:
                unified = cache.get((client.env, "convert", legacy))
                if not unified:
                    try:
                        unified = client.national_number(legacy)
                        cache.put((client.env, "convert", legacy), unified)
                    except WathqError as exc:
                        # No unified number: for the CR product that is a
                        # struck-off record, which Wathq finds by the old number.
                        if not (req.legacy_ok and exc.status == 404):
                            raise
                        unified = None
            key = req.cache_key(client.env) + (unified or "",)
            payload = store.get(key)
            cached = payload is not None
            if not cached:
                try:
                    data = client.call(base, req.path(unified), req.query(), req.headers(), what=ep.id)
                except WathqError as exc:
                    if not (legacy and unified and req.legacy_ok and exc.status == 404):
                        raise
                    data = client.call(base, req.path(None), req.query(), req.headers(), what=ep.id)
                    unified = None
                legacy_used = bool(legacy and not unified)
                code = code_in(data)
                if code:                      # a 2xx carrying Wathq's {code, message}
                    print("wathq:", ep.id, "answer is an error body:", code)
                    exc = error_for(None, code)
                    said = data.get("message") if isinstance(data, dict) else ""
                    return _error(exc.message, exc.status, code, sent=client.sent,
                                  detail=public_message(said))
                try:
                    if ep.view == "contract":
                        payload = {"view_type": "contract", **shape_contract(data)}
                    else:
                        payload = {"view_type": "generic",
                                   "view": build_view(data, labels=ep.labels(), personal=ep.personal,
                                                      overrides=ep.overrides, title=ep.label,
                                                      redact=ep.redact, tidy=not ep.lookup)}
                    payload["fetched_at"] = datetime.now(RIYADH).isoformat(timespec="seconds")
                    payload["legacy_used"] = legacy_used
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")
                except (ValueError, UnicodeError, RecursionError):
                    print("wathq:", ep.id, "answer could not be shown; keys:", safe_keys(data))
                    return _error(MSG_SHAPE if ep.view == "contract" else MSG_VIEW, 502, sent=client.sent)
                store.put(key, payload)
        except WathqError as exc:
            return _error(exc.message, exc.status, exc.code, sent=client.sent, detail=exc.detail)
        except ValueError:
            return _error(MSG_BAD_REQUEST, 400, sent=client.sent)
        body = {
            "source": "wathq", "endpoint": ep.id, "label": ep.label,
            "product": catalog.product(ep).label, "env": client.env, "price": ep.price,
            # Company numbers may be echoed; a person's ID never is.
            "query": {"inputs": req.public_inputs(), "converted": bool(unified),
                      "national_number": unified or "",
                      "legacy_used": bool(payload.get("legacy_used"))},
            "cached": cached, "calls_used": client.sent - sent_before, "sent": client.sent,
            **{k: v for k, v in payload.items() if k != "legacy_used"},
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return router
