"""Pluggable LLM providers for the complaint pipeline.

The pipeline (complaints.py) only needs "send these messages, get JSON back
under this schema". This module hides WHICH model answers:

* "qwen"   — Qwen3-4B on the app's resident llama.cpp server (llm.STRUCT),
             shared with /structure and /chat. The default.
* "allam"  — ALLaM-7B on its lazy llama.cpp server (llm.PROOF). It needs ~5 GB
             of a 16 GB GPU, so it is released after a batch. /proofread uses
             the same server; both go through its ServerLease, so neither
             stops it under the other's request.
* "openai" — any OpenAI-compatible /v1/chat/completions endpoint (vLLM, Ollama,
             another llama-server, a hosted API), configured by env.

Privacy: complaints are personal data. An OpenAI-compatible endpoint must be on
loopback unless the operator sets CMS_LLM_ALLOW_REMOTE=1, requests never go
through an environment proxy and never follow redirects (either would hand the
document to a host nobody configured), and neither API keys nor document text
are ever logged. Exceptions carry safe Arabic messages only: they may reach an
API response or the complaint's stored error.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.request
import weakref
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 8 * 1024 * 1024   # a structuring answer is a few KB; cap the read
STATUS_TTL = 10.0                      # seconds a remote health probe is trusted
STATUS_TIMEOUT = 3
MIN_N_CTX = 4096                       # below this the pipeline's prompts do not fit
STATUSES = ("ready", "loading", "not_loaded", "error", "unconfigured")

MSG_BUSY = "النموذج مشغول حالياً؛ أعد المحاولة لاحقاً"
MSG_LOAD = "تعذر تشغيل النموذج المحلي"
MSG_UNREACHABLE = "تعذر الاتصال بخدمة النموذج"
MSG_FAILED = "تعذر تنفيذ طلب النموذج"
MSG_INVALID = "استجابة النموذج غير صالحة"
MSG_TOO_LARGE = "استجابة النموذج أكبر من الحد المسموح"


class ProviderError(RuntimeError):
    """A model request failed. str() is a safe Arabic message, never a detail."""


class ProviderBusy(ProviderError):
    """The single inference slot is taken; the caller may retry later."""


class ProviderUnavailable(ProviderError):
    """Not configured, cannot load, or unreachable."""


def _safe(exc: BaseException) -> str:
    """Log line for an exception. Only types whose text is known not to carry
    model output or request content are printed in full."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError {exc.code}"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        # ascii(), not repr(): a path in an OSError may hold Arabic, and the
        # app's console is often cp1252, where printing it would raise.
        return ascii(exc)
    return type(exc).__name__


class ServerLease:
    """Shared use of one lazily loaded llama-server by independent callers.

    ALLaM (llm.PROOF) serves both app.py's /proofread and the "allam"
    complaints provider, and each wants it stopped when IT is done, to give
    the 16 GB GPU back. llm.Server.stop() takes no inference lock and
    terminates the process, so either side would kill the other's request
    mid-answer. Every user therefore holds the lease for as long as it uses
    the server; a stop asked for by any of them happens when the last holder
    lets go. The stop runs under the lease's lock, so a caller acquiring
    meanwhile waits and then loads a fresh server instead of posting to a
    dying one. Get the lease of a server with server_lease(server)."""

    def __init__(self, server):
        self._server = server
        self._lock = threading.Lock()
        self._holders = 0
        self._stop_pending = False

    @property
    def holders(self) -> int:
        with self._lock:
            return self._holders

    def acquire(self) -> None:
        with self._lock:
            self._holders += 1

    def release(self, *, stop: bool = False) -> None:
        """Let go; with stop=True, also ask for the server to be stopped once
        nobody holds it (now, if this was the last holder)."""
        with self._lock:
            self._holders = max(0, self._holders - 1)
            self._stop_pending = self._stop_pending or stop
            if self._stop_pending and not self._holders:
                self._stop_pending = False
                self._stop()

    def stop_when_idle(self) -> None:
        """Stop the server now if nobody holds it, else when the last holder lets go."""
        with self._lock:
            if self._holders:
                self._stop_pending = True
                return
            self._stop_pending = False
            self._stop()

    @contextmanager
    def hold(self, *, stop: bool = False):
        self.acquire()
        try:
            yield self
        finally:
            self.release(stop=stop)

    def _stop(self) -> None:
        try:
            self._server.stop()
        except Exception as exc:
            print("complaints llm release error:", _safe(exc))


_LEASES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_LEASES_LOCK = threading.Lock()


def server_lease(server) -> ServerLease:
    """The one ServerLease of `server`, shared by everyone who uses it."""
    with _LEASES_LOCK:
        try:
            lease = _LEASES.get(server)
            if lease is None:
                lease = _LEASES[server] = ServerLease(server)
        except TypeError:                   # not weak-referenceable or hashable
            lease = ServerLease(server)
        return lease


class Provider:
    """Base provider. Subclasses implement chat_json (and usually status)."""

    id: str = ""
    label: str = ""
    model: str = ""
    n_ctx: int = 8192
    local: bool = True
    release_after_batch: bool = False

    def ensure_ready(self) -> None:
        """Load/check the model; raise ProviderUnavailable if it cannot serve."""

    def release(self) -> None:
        """Free resources after a batch. Default: nothing to free."""

    def status(self) -> dict:
        return self._describe("ready")

    def chat_json(self, messages: list[dict], schema: dict, max_tokens: int,
                  temperature: float = 0.0) -> tuple[str, str | None]:
        """(content, finish_reason) for a JSON-schema-constrained completion."""
        raise NotImplementedError

    def _describe(self, status: str) -> dict:
        return {"id": self.id, "label": self.label, "model": self.model,
                "n_ctx": self.n_ctx, "local": self.local,
                "status": status if status in STATUSES else "error"}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id!r} model={self.model!r}>"


class LlamaServerProvider(Provider):
    """Wraps an llm.Server-like object (ensure_loaded, status, chat_json, stop,
    n_ctx, model_path)."""

    def __init__(self, id: str, label: str, server, *, release_after_batch: bool = False):
        self.id, self.label = id, label
        self._server = server
        self.model = os.path.basename(str(server.model_path))
        self.n_ctx = int(server.n_ctx)
        self.local = True
        self.release_after_batch = bool(release_after_batch)
        # Each request holds the server's lease, so a release (ours or
        # /proofread's) requested mid-answer waits until it returns. A
        # release_after_batch provider also holds it from its first use until
        # release(): a /proofread in between must not unload the model the
        # batch is still using.
        self._lease = server_lease(server)
        self._lock = threading.Lock()
        self._batch = False

    def _hold_batch(self) -> None:
        if not self.release_after_batch:
            return
        with self._lock:
            if not self._batch:
                self._lease.acquire()
                self._batch = True

    def ensure_ready(self) -> None:
        self._hold_batch()
        try:
            self._server.ensure_loaded()
        except Exception as exc:
            print(f"complaints llm[{self.id}] load error:", _safe(exc))
            raise ProviderUnavailable(MSG_LOAD) from None

    def status(self) -> dict:
        try:
            state = (self._server.status() or {}).get("status")
        except Exception as exc:
            print(f"complaints llm[{self.id}] status error:", _safe(exc))
            state = "error"
        return self._describe(state)

    def chat_json(self, messages, schema, max_tokens, temperature=0.0):
        with self._lease.hold():
            try:
                # Load first so a load failure is told apart from a request failure:
                # llm.Server.chat_json would load too, but its errors look alike.
                self.ensure_ready()
                return self._server.chat_json(messages, schema, max_tokens, temperature)
            except ProviderError:
                raise
            except RuntimeError as exc:
                if "busy" in str(exc).lower():          # llm.Server's single-slot timeout
                    raise ProviderBusy(MSG_BUSY) from None
                raise self._failure(exc) from None
            except Exception as exc:
                raise self._failure(exc) from None

    def _failure(self, exc: BaseException) -> ProviderError:
        print(f"complaints llm[{self.id}] request error:", _safe(exc))
        if isinstance(exc, urllib.error.HTTPError):
            code = exc.code
            exc.close()
            # llama-server answers 503 while (re)loading the model.
            return ProviderBusy(MSG_BUSY) if code in (429, 503) else ProviderError(MSG_FAILED)
        if isinstance(exc, (urllib.error.URLError, OSError)):
            return ProviderUnavailable(MSG_UNREACHABLE)   # the server died or never came up
        try:
            if (self._server.status() or {}).get("status") == "error":
                return ProviderUnavailable(MSG_LOAD)      # it failed to (re)load mid-call
        except Exception:
            pass
        return ProviderError(MSG_FAILED)

    def release(self) -> None:
        """Stop the server (ALLaM: frees ~5 GB of VRAM) as soon as no request
        of ours or of /proofread is using it. Never stops a shared, resident
        server (Qwen also serves /structure and /chat)."""
        if not self.release_after_batch:
            return
        with self._lock:
            held, self._batch = self._batch, False
            if held:
                self._lease.release(stop=True)
            else:
                self._lease.stop_when_idle()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None     # a 3xx surfaces as an HTTPError instead of being followed


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatibleProvider(Provider):
    """Any server speaking OpenAI's /v1/chat/completions with json_schema
    response_format (vLLM, Ollama, llama-server, hosted APIs)."""

    def __init__(self, id: str, label: str, base_url: str, model: str, *, api_key: str = "",
                 n_ctx: int = 8192, allow_remote: bool = False, timeout: float = 300,
                 opener=None):
        base_url = (base_url or "").strip()
        if not base_url or any(ch.isspace() or ord(ch) < 32 for ch in base_url):
            raise ValueError("CMS_OPENAI_BASE_URL must be a URL without spaces")
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError("CMS_OPENAI_BASE_URL must use http or https")
        if "@" in parts.netloc:
            raise ValueError("CMS_OPENAI_BASE_URL must not contain credentials; "
                             "use CMS_OPENAI_API_KEY")
        if parts.query or parts.fragment or "?" in base_url or "#" in base_url:
            raise ValueError("CMS_OPENAI_BASE_URL must not contain a query or fragment")
        try:
            host, _ = parts.hostname, parts.port      # .port validates the number
        except ValueError:
            raise ValueError("CMS_OPENAI_BASE_URL has an invalid port") from None
        if not host:
            raise ValueError("CMS_OPENAI_BASE_URL must name a host")
        loopback = _is_loopback(host)
        if not loopback and not allow_remote:
            raise ValueError("CMS_OPENAI_BASE_URL is not a loopback address; complaints "
                             "stay on this host unless CMS_LLM_ALLOW_REMOTE=1")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("CMS_OPENAI_MODEL must be set")
        if type(n_ctx) is not int or not MIN_N_CTX <= n_ctx <= 1_048_576:
            raise ValueError(f"CMS_OPENAI_N_CTX must be an integer >= {MIN_N_CTX}")

        self.id, self.label = id, label
        self.model = model.strip()
        self.n_ctx = n_ctx
        self.local = loopback
        self.release_after_batch = False
        self.timeout = timeout
        self.base = base_url.rstrip("/")
        api = self.base if self.base.endswith("/v1") else self.base + "/v1"
        self.endpoint = api + "/chat/completions"
        self.models_url = api + "/models"
        self._api_key = api_key or ""             # never logged, never in repr/status
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        self._clock = time.monotonic
        self._status_lock = threading.Lock()
        self._status_cache: tuple[float, str] | None = None

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # ---- health -------------------------------------------------------------
    def _probe(self) -> str:
        """Health: 'ready' (2xx from /models), 'error' (an HTTP error) or
        'unreachable' (no HTTP answer at all). Cached for STATUS_TTL."""
        req = urllib.request.Request(self.models_url, headers=self._headers(), method="GET")
        try:
            with self._opener.open(req, timeout=STATUS_TIMEOUT) as resp:
                resp.read(64 * 1024)
                state = "ready" if 200 <= resp.status < 300 else "error"
        except urllib.error.HTTPError as exc:
            exc.close()
            state = "error"
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            state = "unreachable"
        with self._status_lock:
            self._status_cache = (self._clock(), state)
        return state

    def status(self) -> dict:
        with self._status_lock:
            cached = self._status_cache
        if cached and self._clock() - cached[0] < STATUS_TTL:
            state = cached[1]
        else:
            state = self._probe()
        return self._describe("ready" if state == "ready" else "error")

    def ensure_ready(self) -> None:
        # Only an unreachable server fails here. An HTTP error from /models
        # (some gateways do not implement it) is left to the real request,
        # which maps its own status code.
        if self._probe() == "unreachable":
            raise ProviderUnavailable(MSG_UNREACHABLE)

    # ---- completions --------------------------------------------------------
    def chat_json(self, messages, schema, max_tokens, temperature=0.0):
        payload = {
            "model": self.model, "messages": messages, "temperature": temperature,
            "top_p": 1.0, "max_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "complaint", "schema": schema,
                                                "strict": True}},
        }
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode("utf-8"),
                                     headers=self._headers(), method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()                            # never read/log the body: it may echo the prompt
            print(f"complaints llm[{self.id}] HTTP error:", code)
            if code in (429, 503):
                raise ProviderBusy(MSG_BUSY) from None
            raise ProviderError(MSG_FAILED) from None
        except (urllib.error.URLError, OSError) as exc:
            print(f"complaints llm[{self.id}] connection error:", _safe(exc))
            raise ProviderUnavailable(MSG_UNREACHABLE) from None
        except http.client.HTTPException as exc:
            print(f"complaints llm[{self.id}] protocol error:", _safe(exc))
            raise ProviderError(MSG_FAILED) from None
        if len(body) > MAX_RESPONSE_BYTES:
            print(f"complaints llm[{self.id}] response over", MAX_RESPONSE_BYTES, "bytes")
            raise ProviderError(MSG_TOO_LARGE)
        try:
            choice = json.loads(body)["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except Exception as exc:
            print(f"complaints llm[{self.id}] invalid response:", _safe(exc))
            raise ProviderError(MSG_INVALID) from None
        if not isinstance(content, str) or not (finish is None or isinstance(finish, str)):
            raise ProviderError(MSG_INVALID)
        return content, finish


class ProviderRegistry:
    """The configured providers and the one the worker uses right now."""

    def __init__(self, providers: list[Provider], default_id: str):
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers: dict[str, Provider] = {}
        for p in providers:
            if p.id in self._providers:
                raise ValueError(f"duplicate provider id {p.id!r}")
            self._providers[p.id] = p
        if default_id not in self._providers:
            raise KeyError(f"unknown provider {default_id!r}")
        self._active = default_id
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        return [p.status() for p in self._providers.values()]

    def ids(self) -> list[str]:
        return list(self._providers)

    def get(self, id: str) -> Provider:
        try:
            return self._providers[id]
        except (KeyError, TypeError):
            raise KeyError(f"unknown provider {id!r}") from None

    def active(self) -> Provider:
        with self._lock:
            return self._providers[self._active]

    def set_active(self, id: str) -> Provider:
        new = self.get(id)
        with self._lock:
            old = self._providers[self._active]
            self._active = new.id
        # Outside the lock: stopping llama-server can take seconds and must not
        # block active() for the worker.
        if old is not new and old.release_after_batch:
            old.release()
        return new


def build_default_registry(env=os.environ) -> ProviderRegistry:
    """qwen + allam always; openai only when CMS_OPENAI_BASE_URL and
    CMS_OPENAI_MODEL are set. Constructs objects only — no model is loaded and
    no process is started here."""
    import llm        # lazily: importing it reads/creates the llama-server key file

    providers: list[Provider] = [
        LlamaServerProvider("qwen", "Qwen3-4B (محلي)", llm.STRUCT),
        LlamaServerProvider("allam", "ALLaM-7B (محلي)", llm.PROOF, release_after_batch=True),
    ]
    base = (env.get("CMS_OPENAI_BASE_URL") or "").strip()
    model = (env.get("CMS_OPENAI_MODEL") or "").strip()
    if base and model:
        try:
            n_ctx = int(env.get("CMS_OPENAI_N_CTX") or 8192)
        except ValueError:
            n_ctx = 0                              # rejected below with a clear message
        try:
            providers.append(OpenAICompatibleProvider(
                "openai", (env.get("CMS_OPENAI_LABEL") or "").strip() or f"{model} (OpenAI-compatible)",
                base, model,
                api_key=env.get("CMS_OPENAI_API_KEY") or "",
                n_ctx=n_ctx,
                allow_remote=env.get("CMS_LLM_ALLOW_REMOTE") == "1"))
        except ValueError as exc:
            # A bad endpoint must not take the app down; the local models still
            # work, and falling back to them keeps documents on this host. The
            # messages are ours and never echo the URL or the key.
            print("complaints llm config error:", exc)
    wanted = (env.get("CMS_LLM_PROVIDER") or "").strip()
    default = wanted if wanted in {p.id for p in providers} else "qwen"
    return ProviderRegistry(providers, default)


@lru_cache(maxsize=1)
def get_registry() -> ProviderRegistry:
    """Process-wide registry built from the environment on first use."""
    return build_default_registry()
