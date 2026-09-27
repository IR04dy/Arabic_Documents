import contextlib
import io
import json
import os
import random
import socket
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import complaints_llm
from complaints_llm import (LlamaServerProvider, OpenAICompatibleProvider, Provider,
                            ProviderBusy, ProviderError, ProviderRegistry, ProviderUnavailable,
                            build_default_registry, get_registry)

QWEN_PATH = r"D:\models\Qwen3-4B-Instruct-2507-Q8_0.gguf"
ALLAM_PATH = r"D:\models\ALLaM-AI_ALLaM-7B-Instruct-preview-Q4_K_M.gguf"
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"],
          "additionalProperties": False}
MESSAGES = [{"role": "system", "content": "extract"}, {"role": "user", "content": "نص الشكوى"}]
SECRET = "نص سري من المستند 1098765432"
KEY = "sk-test-DO-NOT-LOG-123"
STATUS_KEYS = {"id", "label", "model", "n_ctx", "local", "status"}


class FakeServer:
    """Stands in for llm.Server: same attributes and methods, no process."""

    def __init__(self, n_ctx=8192, model_path=QWEN_PATH):
        self.n_ctx, self.model_path = n_ctx, model_path
        self.state = "not_loaded"
        self.load_exc = self.chat_exc = None
        self.reply = ('{"a": 1}', "stop")
        self.calls, self.loads, self.stops = [], 0, 0
        self.entered, self.gate = threading.Event(), None
        self.error_on_chat = False

    def ensure_loaded(self):
        self.loads += 1
        if self.load_exc:
            self.state = "error"
            raise self.load_exc
        self.state = "ready"

    def status(self):
        return {"status": self.state, "error": None, "model": os.path.basename(self.model_path),
                "device": "gpu"}

    def chat_json(self, messages, schema, max_tokens, temperature=0.0):
        self.calls.append((messages, schema, max_tokens, temperature))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error_on_chat:
            self.state = "error"
        if self.chat_exc:
            raise self.chat_exc
        return self.reply

    def stop(self):
        self.stops += 1
        self.state = "not_loaded"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class LlamaServerProviderTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.qwen = LlamaServerProvider("qwen", "Qwen3-4B (محلي)", self.server)

    def test_attributes_come_from_the_server(self):
        allam = LlamaServerProvider("allam", "ALLaM", FakeServer(4096, ALLAM_PATH), release_after_batch=True)
        self.assertEqual((self.qwen.model, self.qwen.n_ctx, self.qwen.local, self.qwen.release_after_batch),
                         ("Qwen3-4B-Instruct-2507-Q8_0.gguf", 8192, True, False))
        self.assertEqual((allam.n_ctx, allam.release_after_batch), (4096, True))
        self.assertIsInstance(self.qwen, Provider)

    def test_chat_json_loads_then_passes_arguments_through(self):
        self.assertEqual(self.qwen.chat_json(MESSAGES, SCHEMA, 900, 0.0), ('{"a": 1}', "stop"))
        self.assertEqual(self.server.loads, 1)
        self.assertEqual(self.server.calls, [(MESSAGES, SCHEMA, 900, 0.0)])

    def test_busy_slot_maps_to_provider_busy(self):
        self.server.chat_exc = RuntimeError("model is busy; please retry")
        with self.assertRaises(ProviderBusy) as cm:
            self.qwen.chat_json(MESSAGES, SCHEMA, 100)
        self.assertIsInstance(cm.exception, ProviderError)
        self.assertNotIn("busy", str(cm.exception))            # safe Arabic message

    def test_load_failures_map_to_unavailable(self):
        for exc in (FileNotFoundError("model not found"), RuntimeError("llama-server exited early (code 1)"),
                    TimeoutError("not ready within 180s")):
            server = FakeServer()
            server.load_exc = exc
            provider = LlamaServerProvider("allam", "ALLaM", server)
            with quiet(), self.assertRaises(ProviderUnavailable):
                provider.ensure_ready()
            with quiet(), self.assertRaises(ProviderUnavailable):
                provider.chat_json(MESSAGES, SCHEMA, 100)
            self.assertEqual(server.calls, [], "no request after a failed load")

    def test_reload_failure_inside_the_request_is_unavailable(self):
        self.server.error_on_chat = True
        self.server.chat_exc = RuntimeError("llama-server[structurer] exited early (code 3)")
        with quiet(), self.assertRaises(ProviderUnavailable):
            self.qwen.chat_json(MESSAGES, SCHEMA, 100)

    def test_transport_errors(self):
        cases = [(urllib.error.HTTPError("http://127.0.0.1:8123/v1/chat/completions", 503, "Loading",
                                         {}, io.BytesIO(b"")), ProviderBusy),
                 (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), ProviderUnavailable),
                 (ConnectionResetError(10054, "reset"), ProviderUnavailable)]
        for exc, expected in cases:
            self.server.chat_exc = exc
            with quiet(), self.assertRaises(expected):
                self.qwen.chat_json(MESSAGES, SCHEMA, 100)

    def test_other_failures_are_plain_provider_errors_and_never_leak(self):
        for exc in (RuntimeError(SECRET), KeyError(SECRET), ValueError(SECRET),
                    urllib.error.HTTPError("http://x", 400, SECRET, {}, io.BytesIO(SECRET.encode()))):
            self.server.chat_exc = exc
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(ProviderError) as cm:
                self.qwen.chat_json(MESSAGES, SCHEMA, 100)
            self.assertIs(type(cm.exception), ProviderError)
            self.assertNotIn(SECRET, str(cm.exception))
            self.assertNotIn(SECRET, out.getvalue())
            self.assertIsNone(cm.exception.__cause__)
            self.assertTrue(cm.exception.__suppress_context__)

    def test_release_only_when_release_after_batch(self):
        self.qwen.release()
        self.assertEqual(self.server.stops, 0, "the shared Qwen server must never be stopped")
        server = FakeServer(4096, ALLAM_PATH)
        allam = LlamaServerProvider("allam", "ALLaM", server, release_after_batch=True)
        allam.release()
        self.assertEqual(server.stops, 1)

    def test_release_during_a_request_is_deferred_until_it_returns(self):
        server = FakeServer(4096, ALLAM_PATH)
        server.gate = threading.Event()
        allam = LlamaServerProvider("allam", "ALLaM", server, release_after_batch=True)
        result = {}
        worker = threading.Thread(target=lambda: result.update(r=allam.chat_json(MESSAGES, SCHEMA, 50)))
        worker.start()
        self.assertTrue(server.entered.wait(5))
        allam.release()
        self.assertEqual(server.stops, 0, "stopped mid-answer")
        server.gate.set()
        worker.join(5)
        self.assertEqual(result["r"], ('{"a": 1}', "stop"))
        self.assertEqual(server.stops, 1)
        server.gate = None
        allam.chat_json(MESSAGES, SCHEMA, 50)
        self.assertEqual(server.stops, 1, "a served release is not repeated")

    def test_proofread_holding_allam_defers_the_providers_release(self):
        # app.py's /proofread holds ALLaM's lease for its whole run.
        server = FakeServer(4096, ALLAM_PATH)
        allam = LlamaServerProvider("allam", "ALLaM", server, release_after_batch=True)
        proofread = complaints_llm.server_lease(server)
        proofread.acquire()
        self.assertEqual(allam.chat_json(MESSAGES, SCHEMA, 50), ('{"a": 1}', "stop"))
        allam.release()                                  # the complaint queue drained
        self.assertEqual(server.stops, 0, "stopped under a running /proofread")
        proofread.release(stop=True)                     # /proofread's own «free ALLaM»
        self.assertEqual(server.stops, 1)

    def test_proofread_finishing_waits_for_the_complaint_batch(self):
        server = FakeServer(4096, ALLAM_PATH)
        server.gate = threading.Event()
        allam = LlamaServerProvider("allam", "ALLaM", server, release_after_batch=True)
        worker = threading.Thread(target=lambda: allam.chat_json(MESSAGES, SCHEMA, 50))
        worker.start()
        self.assertTrue(server.entered.wait(5))
        with complaints_llm.server_lease(server).hold(stop=True):   # a whole /proofread meanwhile
            pass
        self.assertEqual(server.stops, 0, "stopped mid-answer")
        server.gate.set()
        worker.join(5)
        self.assertEqual(server.stops, 0, "stopped between two requests of the batch")
        allam.release()                                  # the batch is over
        self.assertEqual(server.stops, 1)
        self.assertEqual(complaints_llm.server_lease(server).holders, 0)

    def test_a_lease_is_shared_per_server_and_waits_out_a_stop(self):
        a, b = FakeServer(), FakeServer()
        self.assertIs(complaints_llm.server_lease(a), complaints_llm.server_lease(a))
        self.assertIsNot(complaints_llm.server_lease(a), complaints_llm.server_lease(b))
        stopping, finish = threading.Event(), threading.Event()

        def slow_stop():
            stopping.set()
            finish.wait(5)
            a.stops += 1
        a.stop = slow_stop
        lease = complaints_llm.server_lease(a)
        stopper = threading.Thread(target=lease.stop_when_idle)
        stopper.start()
        self.assertTrue(stopping.wait(5))
        acquired = threading.Event()
        user = threading.Thread(target=lambda: (lease.acquire(), acquired.set()))
        user.start()
        self.assertFalse(acquired.wait(0.2), "a new user got in while the server was being stopped")
        finish.set()
        stopper.join(5)
        user.join(5)
        self.assertTrue(acquired.is_set())
        self.assertEqual((a.stops, lease.holders), (1, 1))

    def test_release_swallows_stop_errors(self):
        server = FakeServer(4096, ALLAM_PATH)
        server.stop = lambda: (_ for _ in ()).throw(OSError("access denied"))
        with quiet():
            LlamaServerProvider("allam", "ALLaM", server, release_after_batch=True).release()

    def test_os_error_log_line_survives_a_cp1252_console(self):
        # a model path under an Arabic folder name ends up in the OSError
        line = complaints_llm._safe(FileNotFoundError(2, "No such file", r"D:\نماذج\qwen.gguf"))
        line.encode("cp1252")                      # repr() would raise UnicodeEncodeError here
        self.assertIn("FileNotFoundError", line)

    def test_status_shape_and_mapping(self):
        for state, expected in (("ready", "ready"), ("loading", "loading"), ("not_loaded", "not_loaded"),
                                ("error", "error"), ("exploded", "error")):
            self.server.state = state
            status = self.qwen.status()
            self.assertEqual(set(status), STATUS_KEYS)
            self.assertEqual(status["status"], expected)
        self.assertEqual(status, {"id": "qwen", "label": "Qwen3-4B (محلي)",
                                  "model": "Qwen3-4B-Instruct-2507-Q8_0.gguf", "n_ctx": 8192,
                                  "local": True, "status": "error"})
        self.server.status = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        with quiet():
            self.assertEqual(self.qwen.status()["status"], "error")


# ---- a local OpenAI-compatible stub ------------------------------------------

class Stub:
    def __init__(self):
        self.requests = []
        self.routes = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append({"method": self.command, "path": self.path,
                                      "headers": dict(self.headers.items()), "body": body})
                status, payload, headers = stub.routes.get((self.command, self.path),
                                                           stub.default(self.command))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    @staticmethod
    def completion(content='{"a": 1}', finish="stop"):
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": content},
                                        "finish_reason": finish}]}).encode()

    def default(self, method):
        if method == "GET":
            return 200, b'{"data": [{"id": "m"}]}', {"Content-Type": "application/json"}
        return 200, self.completion(), {"Content-Type": "application/json"}

    def paths(self):
        return [r["path"] for r in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenAICompatibleProviderTests(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.addCleanup(self.stub.close)

    def provider(self, base=None, **kw):
        return OpenAICompatibleProvider("openai", "vLLM", base or self.stub.base, "qwen2.5-7b", **kw)

    def test_payload_shape_and_authorization(self):
        content, finish = self.provider(api_key=KEY).chat_json(MESSAGES, SCHEMA, 900)
        self.assertEqual((content, finish), ('{"a": 1}', "stop"))
        req = self.stub.requests[-1]
        self.assertEqual((req["method"], req["path"]), ("POST", "/v1/chat/completions"))
        self.assertEqual(json.loads(req["body"]), {
            "model": "qwen2.5-7b", "messages": MESSAGES, "temperature": 0.0, "top_p": 1.0,
            "max_tokens": 900,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "complaint", "schema": SCHEMA, "strict": True}}})
        self.assertEqual(req["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(req["headers"]["Content-Type"], "application/json")

    def test_no_authorization_header_without_a_key(self):
        self.provider().chat_json(MESSAGES, SCHEMA, 10, temperature=0.3)
        req = self.stub.requests[-1]
        self.assertNotIn("authorization", {k.lower() for k in req["headers"]})
        self.assertEqual(json.loads(req["body"])["temperature"], 0.3)

    def test_endpoint_with_and_without_v1(self):
        for suffix, path in (("", "/v1/chat/completions"), ("/", "/v1/chat/completions"),
                             ("/v1", "/v1/chat/completions"), ("/v1/", "/v1/chat/completions"),
                             ("/openai", "/openai/v1/chat/completions"),
                             ("/api/v1", "/api/v1/chat/completions")):
            self.provider(self.stub.base + suffix).chat_json(MESSAGES, SCHEMA, 10)
            self.assertEqual(self.stub.paths()[-1], path, suffix)
        remote = self.provider("https://api.example.com/v1", allow_remote=True)
        self.assertEqual(remote.endpoint, "https://api.example.com/v1/chat/completions")
        self.assertEqual(remote.models_url, "https://api.example.com/v1/models")

    def test_finish_reason_is_passed_through(self):
        self.stub.routes[("POST", "/v1/chat/completions")] = (200, Stub.completion('{"a":', "length"), {})
        self.assertEqual(self.provider().chat_json(MESSAGES, SCHEMA, 10), ('{"a":', "length"))

    def test_http_status_mapping_never_leaks_the_error_body(self):
        for code, expected in ((429, ProviderBusy), (503, ProviderBusy), (500, ProviderError),
                               (401, ProviderError), (400, ProviderError)):
            self.stub.routes[("POST", "/v1/chat/completions")] = (code, SECRET.encode(), {})
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(expected) as cm:
                self.provider(api_key=KEY).chat_json(MESSAGES, SCHEMA, 10)
            self.assertIs(type(cm.exception), expected, code)
            for leaked in (SECRET, KEY):
                self.assertNotIn(leaked, str(cm.exception) + out.getvalue())

    def test_redirects_are_not_followed(self):
        self.stub.routes[("POST", "/v1/chat/completions")] = (307, b"", {"Location": "/elsewhere"})
        with quiet(), self.assertRaises(ProviderError):
            self.provider().chat_json(MESSAGES, SCHEMA, 10)
        self.assertNotIn("/elsewhere", self.stub.paths())

    def test_connection_refused_is_unavailable(self):
        with quiet(), self.assertRaises(ProviderUnavailable):
            self.provider(f"http://127.0.0.1:{free_port()}").chat_json(MESSAGES, SCHEMA, 10)

        class Refusing:                     # the same failure without another 2 s Windows connect
            def open(self, req, timeout=None):
                raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

        provider = self.provider("http://127.0.0.1:9", opener=Refusing())
        with self.assertRaises(ProviderUnavailable):
            provider.ensure_ready()
        self.assertEqual(provider.status()["status"], "error")

    def test_environment_proxy_is_ignored(self):
        proxy = Stub()                                  # records what an env proxy would see
        self.addCleanup(proxy.close)
        env = {"HTTP_PROXY": proxy.base, "http_proxy": proxy.base, "HTTPS_PROXY": proxy.base,
               "https_proxy": proxy.base, "NO_PROXY": "", "no_proxy": ""}
        with patch.dict(os.environ, env):
            self.assertEqual(self.provider().chat_json(MESSAGES, SCHEMA, 10)[0], '{"a": 1}')
            self.assertEqual(proxy.requests, [])
            # control: the stock opener WOULD have handed the request to the proxy
            urllib.request.build_opener().open(self.stub.base + "/v1/models", timeout=3).close()
        self.assertEqual(len(proxy.requests), 1)
        self.assertEqual(self.stub.paths(), ["/v1/chat/completions"])
        # ProxyHandler({}) registers no methods, so it is absent from the
        # handler list — what matters is that no proxying handler is present.
        handlers = self.provider()._opener.handlers
        self.assertFalse([h for h in handlers if isinstance(h, urllib.request.ProxyHandler)])
        redirects = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([type(h).__name__ for h in redirects], ["_NoRedirect"])

    def test_bad_or_oversized_responses(self):
        route = ("POST", "/v1/chat/completions")
        for body in (b"not json", b'{"choices": []}', b'{"choices": [{"message": {"content": null}}]}',
                     b'{"choices": [{"message": {"content": "{}"}, "finish_reason": 7}]}'):
            self.stub.routes[route] = (200, body, {})
            with quiet(), self.assertRaises(ProviderError) as cm:
                self.provider().chat_json(MESSAGES, SCHEMA, 10)
            self.assertEqual(str(cm.exception), complaints_llm.MSG_INVALID, body)
        self.stub.routes[route] = (200, Stub.completion("x" * 500), {})
        with patch.object(complaints_llm, "MAX_RESPONSE_BYTES", 200), quiet(), \
                self.assertRaises(ProviderError) as cm:
            self.provider().chat_json(MESSAGES, SCHEMA, 10)
        self.assertEqual(str(cm.exception), complaints_llm.MSG_TOO_LARGE)

    def test_base_url_validation(self):
        for url in ("http://10.0.0.5:8000", "https://api.example.com/v1", "http://127.0.0.1.nip.io:8000",
                    "http://0.0.0.0:8000"):
            with self.assertRaises(ValueError, msg=url):
                self.provider(url)
        remote = self.provider("http://10.0.0.5:8000", allow_remote=True)
        self.assertFalse(remote.local)
        for url in ("http://user:pw@127.0.0.1:8000", "http://127.0.0.1:8000/v1?key=x",
                    "http://127.0.0.1:8000/v1#top", "ftp://127.0.0.1/v1", "file:///C:/models",
                    "http://:8000", "http://127.0.0.1:99999", "http://127.0.0.1:80 00", "", "   "):
            with self.assertRaises(ValueError) as cm:
                OpenAICompatibleProvider("openai", "x", url, "m", allow_remote=True)
            self.assertNotIn("pw", str(cm.exception))
        for url in ("http://localhost:8000", "http://[::1]:8000", "http://127.0.0.2:1234",
                    "HTTP://LOCALHOST:8000/v1"):
            self.assertTrue(self.provider(url).local, url)
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider("openai", "x", self.stub.base, " ")
        with self.assertRaises(ValueError):
            self.provider(n_ctx=2048)

    def test_status_is_probed_cached_and_shaped(self):
        provider = self.provider(api_key=KEY)
        now = [1000.0]
        provider._clock = lambda: now[0]
        status = provider.status()
        self.assertEqual(status, {"id": "openai", "label": "vLLM", "model": "qwen2.5-7b",
                                  "n_ctx": 8192, "local": True, "status": "ready"})
        probe = self.stub.requests[-1]
        self.assertEqual((probe["method"], probe["path"]), ("GET", "/v1/models"))
        self.assertEqual(probe["headers"]["Authorization"], f"Bearer {KEY}")
        now[0] += 5
        provider.status()
        self.assertEqual(self.stub.paths().count("/v1/models"), 1, "cached for 10 s")
        self.stub.routes[("GET", "/v1/models")] = (500, b"", {})
        now[0] += 6
        self.assertEqual(provider.status()["status"], "error")
        self.assertEqual(self.stub.paths().count("/v1/models"), 2)
        self.assertNotIn(KEY, json.dumps(status) + repr(provider))

    def test_ensure_ready_tolerates_gateways_without_models(self):
        self.stub.routes[("GET", "/v1/models")] = (404, b"", {})
        self.provider().ensure_ready()                           # the real request decides
        self.assertEqual(self.provider().status()["status"], "error")

    def test_injected_opener_is_used(self):
        seen = []

        class Opener:
            def open(self, req, timeout=None):
                seen.append((req.full_url, timeout))
                return io.BytesIO(Stub.completion())

        provider = self.provider("http://127.0.0.1:9", opener=Opener(), timeout=42)
        self.assertEqual(provider.chat_json(MESSAGES, SCHEMA, 5)[0], '{"a": 1}')
        self.assertEqual(seen, [("http://127.0.0.1:9/v1/chat/completions", 42)])


# ---- registry ------------------------------------------------------------------

def fake_llm_module():
    module = types.ModuleType("llm")
    module.STRUCT = FakeServer(8192, QWEN_PATH)
    module.PROOF = FakeServer(4096, ALLAM_PATH)
    return module


def no_process(*args, **kwargs):
    raise AssertionError("a process was started")


class RegistryTests(unittest.TestCase):
    def build(self, env):
        self.llm = fake_llm_module()
        out = io.StringIO()
        with patch.dict(sys.modules, {"llm": self.llm}), patch("subprocess.Popen", no_process), \
                contextlib.redirect_stdout(out):
            registry = build_default_registry(env)
        self.log = out.getvalue()
        return registry

    def test_default_registry_constructs_without_loading_anything(self):
        registry = self.build({})
        self.assertEqual(registry.ids(), ["qwen", "allam"])
        self.assertEqual(registry.active().id, "qwen")
        self.assertEqual((self.llm.STRUCT.loads, self.llm.PROOF.loads), (0, 0))
        qwen, allam = registry.get("qwen"), registry.get("allam")
        self.assertEqual((qwen.label, qwen.n_ctx, qwen.release_after_batch), ("Qwen3-4B (محلي)", 8192, False))
        self.assertEqual((allam.label, allam.n_ctx, allam.release_after_batch), ("ALLaM-7B (محلي)", 4096, True))
        self.assertEqual([s["status"] for s in registry.list()], ["not_loaded", "not_loaded"])
        self.assertTrue(all(set(s) == STATUS_KEYS for s in registry.list()))

    def test_default_provider_from_env_and_fallback(self):
        self.assertEqual(self.build({"CMS_LLM_PROVIDER": "allam"}).active().id, "allam")
        self.assertEqual(self.build({"CMS_LLM_PROVIDER": "gpt-9"}).active().id, "qwen")
        self.assertEqual(self.build({"CMS_LLM_PROVIDER": "openai"}).active().id, "qwen")

    def test_openai_only_when_fully_configured(self):
        self.assertEqual(self.build({"CMS_OPENAI_BASE_URL": "http://127.0.0.1:8000"}).ids(), ["qwen", "allam"])
        self.assertEqual(self.build({"CMS_OPENAI_MODEL": "llama3"}).ids(), ["qwen", "allam"])
        registry = self.build({"CMS_OPENAI_BASE_URL": "http://127.0.0.1:8000/v1", "CMS_OPENAI_MODEL": "llama3",
                               "CMS_OPENAI_N_CTX": "16384", "CMS_LLM_PROVIDER": "openai"})
        openai = registry.get("openai")
        self.assertEqual(registry.active(), openai)
        self.assertEqual((openai.label, openai.model, openai.n_ctx, openai.local),
                         ("llama3 (OpenAI-compatible)", "llama3", 16384, True))
        labelled = self.build({"CMS_OPENAI_BASE_URL": "http://127.0.0.1:8000", "CMS_OPENAI_MODEL": "llama3",
                               "CMS_OPENAI_LABEL": "Ollama المحلي"}).get("openai")
        self.assertEqual((labelled.label, labelled.n_ctx), ("Ollama المحلي", 8192))

    def test_openai_key_reaches_the_endpoint(self):
        stub = Stub()
        self.addCleanup(stub.close)
        registry = self.build({"CMS_OPENAI_BASE_URL": stub.base, "CMS_OPENAI_MODEL": "m",
                               "CMS_OPENAI_API_KEY": KEY})
        registry.get("openai").chat_json(MESSAGES, SCHEMA, 5)
        self.assertEqual(stub.requests[-1]["headers"]["Authorization"], f"Bearer {KEY}")

    def test_remote_endpoint_needs_explicit_opt_in(self):
        env = {"CMS_OPENAI_BASE_URL": "https://llm.example.com/v1", "CMS_OPENAI_MODEL": "allam-13b",
               "CMS_OPENAI_API_KEY": KEY, "CMS_LLM_PROVIDER": "openai"}
        registry = self.build(env)
        self.assertEqual((registry.ids(), registry.active().id), (["qwen", "allam"], "qwen"))
        self.assertIn("CMS_LLM_ALLOW_REMOTE", self.log)
        self.assertNotIn(KEY, self.log)
        self.assertEqual(self.build({**env, "CMS_LLM_ALLOW_REMOTE": "true"}).ids(), ["qwen", "allam"])
        registry = self.build({**env, "CMS_LLM_ALLOW_REMOTE": "1"})
        self.assertEqual(registry.active().id, "openai")
        self.assertFalse(registry.active().local)

    def test_bad_openai_config_is_logged_not_fatal(self):
        base = {"CMS_OPENAI_MODEL": "m"}
        for env, needle in (({**base, "CMS_OPENAI_BASE_URL": "http://127.0.0.1:8000", "CMS_OPENAI_N_CTX": "big"},
                             "CMS_OPENAI_N_CTX"),
                            ({**base, "CMS_OPENAI_BASE_URL": "http://admin:hunter2@127.0.0.1:8000"},
                             "credentials")):
            self.assertEqual(self.build(env).ids(), ["qwen", "allam"])
            self.assertIn(needle, self.log)
            self.assertNotIn("hunter2", self.log)

    def test_get_registry_is_cached(self):
        get_registry.cache_clear()
        self.addCleanup(get_registry.cache_clear)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("CMS_")}
        with patch.dict(sys.modules, {"llm": fake_llm_module()}), patch.dict(os.environ, clean, clear=True):
            first = get_registry()
            self.assertIs(get_registry(), first)
        self.assertEqual(first.active().id, "qwen")


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.qwen_server, self.allam_server = FakeServer(), FakeServer(4096, ALLAM_PATH)
        self.qwen = LlamaServerProvider("qwen", "Qwen", self.qwen_server)
        self.allam = LlamaServerProvider("allam", "ALLaM", self.allam_server, release_after_batch=True)
        self.registry = ProviderRegistry([self.qwen, self.allam], "qwen")

    def test_construction_errors(self):
        with self.assertRaises(ValueError):
            ProviderRegistry([], "qwen")
        with self.assertRaises(ValueError):
            ProviderRegistry([self.qwen, LlamaServerProvider("qwen", "again", FakeServer())], "qwen")
        with self.assertRaises(KeyError):
            ProviderRegistry([self.qwen], "allam")

    def test_lookup_and_list_order(self):
        self.assertEqual(self.registry.ids(), ["qwen", "allam"])
        self.assertIs(self.registry.get("allam"), self.allam)
        self.assertEqual([s["id"] for s in self.registry.list()], ["qwen", "allam"])
        for bad in ("gpt", None, ["qwen"]):
            with self.assertRaises(KeyError):
                self.registry.get(bad)

    def test_set_active_releases_only_the_lazy_provider_being_left(self):
        self.assertIs(self.registry.set_active("allam"), self.allam)
        self.assertEqual(self.allam_server.stops + self.qwen_server.stops, 0)
        self.registry.set_active("allam")
        self.assertEqual(self.allam_server.stops, 0, "re-selecting is not leaving")
        self.registry.set_active("qwen")
        self.assertEqual(self.allam_server.stops, 1)
        self.registry.set_active("qwen")
        self.registry.set_active("allam")
        self.assertEqual((self.allam_server.stops, self.qwen_server.stops), (1, 0))

    def test_unknown_id_leaves_the_active_provider(self):
        self.registry.set_active("allam")
        with self.assertRaises(KeyError):
            self.registry.set_active("openai")
        self.assertIs(self.registry.active(), self.allam)
        self.assertEqual(self.allam_server.stops, 0)

    def test_concurrent_switching_is_consistent(self):
        errors, switches_away = [], [0]
        lock = threading.Lock()

        def switcher(seed):
            rnd = random.Random(seed)
            try:
                for _ in range(300):
                    target = rnd.choice(["qwen", "allam"])
                    self.registry.set_active(target)
                    if target == "qwen":
                        with lock:
                            switches_away[0] += 1
            except Exception as exc:                            # pragma: no cover - reported below
                errors.append(exc)

        def reader():
            try:
                for _ in range(2000):
                    self.assertIn(self.registry.active().id, ("qwen", "allam"))
            except Exception as exc:                            # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=switcher, args=(i,)) for i in range(6)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertIn(self.registry.active().id, ("qwen", "allam"))
        self.assertLessEqual(self.allam_server.stops, switches_away[0])


class RealLlmModuleTests(unittest.TestCase):
    """The real llm.Server objects fit the provider interface. Skipped when the
    llama-server key file does not exist yet (importing llm would create it)."""

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "vendor", ".llama-server-key")),
                         "llm key file absent")
    def test_build_default_registry_with_real_llm_starts_nothing(self):
        with patch("subprocess.Popen", no_process), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network used")), \
                warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)    # llm.py's own key-file read
            import llm
            registry = build_default_registry({})
            statuses = registry.list()
        self.assertEqual([s["id"] for s in statuses], ["qwen", "allam"])
        self.assertEqual(registry.get("allam").n_ctx, llm.PROOF.n_ctx)
        self.assertEqual(registry.get("qwen").model, os.path.basename(llm.STRUCT.model_path))
        self.assertTrue(all(set(s) == STATUS_KEYS for s in statuses))


if __name__ == "__main__":
    unittest.main()
