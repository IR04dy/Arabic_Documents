"""Shared local LLM runtime: owns the llama.cpp servers and generic completions.

There are TWO GGUF text models, each on its own llama.cpp `llama-server.exe`
(official CUDA 13.3 build, Blackwell/sm_120):

* STRUCT — Qwen3-4B-Instruct-2507 (port 8123). Serves structuring (JSON-schema)
  and document chat. It is the strong, reliable structurer, so it was restored
  here after ALLaM-Q4 proved weak at schema extraction.
* PROOF  — ALLaM-7B-Instruct (port 8124). Serves the Arabic proofreading pass.

Both plus the Surya OCR server must share a 16 GB GPU, so PROOF runs a small
context with a quantized KV cache (ALLaM is MHA — its KV is large). Set env
overrides if needed.

Design points (kept from the hardened single-server version):
* Each server has its own inference lock (llama-server runs --parallel 1). Non-
  stream helpers acquire with a timeout and raise "busy"; the streaming generator
  holds the lock for the whole answer and releases it in a finally.
* chat_stream is a PLAIN SYNC generator handed straight to StreamingResponse.
* Servers start with a generated --api-key (persisted, shared by both), closing
  the otherwise-open surface on their ports.

Module-level shims keep the old call sites working:
  chat_json / chat_stream / N_CTX / ensure_loaded / status  -> STRUCT (Qwen3)
  chat_text                                                 -> PROOF  (ALLaM)
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---- model paths -----------------------------------------------------------
_QWEN3 = r"D:\Yousef\PII-Detector\Machine-2.2\models\Qwen3-4B-Instruct-2507-Q8_0.gguf"
_ALLAM = os.path.join(_HERE, "models", "ALLaM-AI_ALLaM-7B-Instruct-preview-Q4_K_M.gguf")

STRUCT_MODEL_PATH = os.environ.get("STRUCTURE_MODEL_PATH", _QWEN3)
PROOF_MODEL_PATH = os.environ.get("PROOF_MODEL_PATH", _ALLAM)

SERVER_EXE = os.environ.get(
    "STRUCTURE_SERVER_EXE",
    os.path.join(_HERE, "vendor", "llama-cuda", "llama-server.exe"),
)
HOST = os.environ.get("STRUCTURE_HOST", "127.0.0.1")

# Contexts sized to fit both models + the OCR server in 16 GB. Qwen3 (GQA) has a small KV,
# so 8192 is affordable; ALLaM (MHA) has a large KV, so it runs short + quantized.
STRUCT_PORT = int(os.environ.get("STRUCTURE_PORT", "8123"))
STRUCT_N_CTX = int(os.environ.get("STRUCTURE_N_CTX", "8192"))
PROOF_PORT = int(os.environ.get("PROOF_PORT", "8124"))
PROOF_N_CTX = int(os.environ.get("PROOF_N_CTX", "4096"))

N_GPU_LAYERS = int(os.environ.get("STRUCTURE_GPU_LAYERS", "99"))
STARTUP_TIMEOUT = int(os.environ.get("STRUCTURE_STARTUP_TIMEOUT", "180"))

# Quantize ALLaM's KV cache (needs flash-attn) to halve its VRAM footprint.
PROOF_EXTRA = ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--flash-attn", "on"]

REQUEST_TIMEOUT = 300          # per non-stream request (single-shot)
STREAM_TIMEOUT = 300           # socket timeout for streaming reads
INFER_ACQUIRE_TIMEOUT = 150    # wait for the single inference slot (non-stream)
STREAM_ACQUIRE_TIMEOUT = 120   # wait for the slot before a chat stream

# N_CTX is imported by chat.py for its context budget -> the structurer's ctx.
N_CTX = STRUCT_N_CTX


def _load_or_make_key() -> str:
    """A stable API key persisted next to the binary so restarts / reused
    servers share it (vendor/ is git-ignored, so it is never committed)."""
    keyfile = os.path.join(_HERE, "vendor", ".llama-server-key")
    try:
        if os.path.exists(keyfile):
            k = open(keyfile, encoding="utf-8").read().strip()
            if k:
                return k
    except Exception:
        pass
    k = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(keyfile), exist_ok=True)
        with open(keyfile, "w", encoding="utf-8") as f:
            f.write(k)
    except Exception:
        pass
    return k


API_KEY = _load_or_make_key()
_HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}


class Server:
    """One llama-server process serving one GGUF model on one port."""

    def __init__(self, name: str, model_path: str, port: int, n_ctx: int,
                 extra_args: list | None = None):
        self.name = name
        self.model_path = model_path
        self.port = port
        self.n_ctx = n_ctx
        self.extra_args = extra_args or []
        self.base = f"http://{HOST}:{port}"
        self._proc: "subprocess.Popen | None" = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._state: dict = {"status": "not_loaded", "error": None}

    # ---- health / status --------------------------------------------------
    def status(self) -> dict:
        return {
            "status": self._state["status"],
            "error": self._state["error"],
            "model": os.path.basename(self.model_path),
            "device": "gpu" if N_GPU_LAYERS != 0 else "cpu",
        }

    def _server_ready(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/health", timeout=3) as r:
                return r.status == 200 and json.loads(r.read() or b"{}").get("status") == "ok"
        except Exception:
            return False

    def _server_authed(self) -> bool:
        """True only if a running server accepts OUR api-key AND is serving the
        model we expect (so we never reuse a server left on the other model)."""
        try:
            req = urllib.request.Request(f"{self.base}/v1/models", headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status != 200:
                    return False
                data = json.loads(r.read() or b"{}")
        except Exception:
            return False
        want = os.path.basename(self.model_path)
        ids = [str((m or {}).get("id", "")) for m in (data.get("data") or [])]
        return any(want in mid or os.path.basename(mid) == want for mid in ids) or not ids

    # ---- lifecycle --------------------------------------------------------
    def _start(self) -> None:
        if self._server_ready() and self._server_authed():
            return
        if not os.path.exists(SERVER_EXE):
            raise FileNotFoundError(f"llama-server not found at {SERVER_EXE}")
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"model not found at {self.model_path}")

        log = open(os.path.join(_HERE, "vendor", f"llama-{self.name}.log"), "ab")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [SERVER_EXE, "-m", self.model_path,
             "--n-gpu-layers", str(N_GPU_LAYERS),
             "--ctx-size", str(self.n_ctx),
             "--parallel", "1",
             "--host", HOST, "--port", str(self.port),
             "--api-key", API_KEY,
             "--jinja", "--no-webui", *self.extra_args],
            cwd=os.path.dirname(SERVER_EXE),
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server[{self.name}] exited early (code {self._proc.returncode})")
            if self._server_ready():
                return
            time.sleep(1.0)
        raise TimeoutError(f"llama-server[{self.name}] not ready within {STARTUP_TIMEOUT}s")

    def stop(self) -> None:
        with self._load_lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except Exception:
                    self._proc.kill()
            self._proc = None
            self._state["status"] = "not_loaded"

    def ensure_loaded(self) -> None:
        if self._state["status"] == "ready" and self._server_ready():
            return
        with self._load_lock:
            if self._state["status"] == "ready" and self._server_ready():
                return
            try:
                self._state["status"] = "loading"
                self._state["error"] = None
                self._start()
                self._state["status"] = "ready"
            except Exception as exc:
                print(f"llm[{self.name}] load error:", repr(exc))  # detail -> log
                self._state.update(status="error", error="model_unavailable")
                raise

    # ---- completions ------------------------------------------------------
    def _post(self, path: str, payload: dict, timeout: int) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"), headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _acquire(self, timeout: int) -> None:
        if not self._infer_lock.acquire(timeout=timeout):
            raise RuntimeError("model is busy; please retry")

    def chat_json(self, messages: list, schema: dict, max_tokens: int,
                  temperature: float = 0.0) -> tuple:
        self.ensure_loaded()
        payload = {
            "messages": messages, "temperature": temperature, "top_p": 1.0,
            "seed": 0, "max_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "document", "schema": schema}},
        }
        self._acquire(INFER_ACQUIRE_TIMEOUT)
        try:
            resp = self._post("/v1/chat/completions", payload, REQUEST_TIMEOUT)
        finally:
            self._infer_lock.release()
        choice = resp["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason")

    def chat_text(self, messages: list, max_tokens: int,
                  temperature: float = 0.0) -> tuple:
        self.ensure_loaded()
        payload = {"messages": messages, "temperature": temperature, "top_p": 1.0,
                   "seed": 0, "max_tokens": max_tokens}
        self._acquire(INFER_ACQUIRE_TIMEOUT)
        try:
            resp = self._post("/v1/chat/completions", payload, REQUEST_TIMEOUT)
        finally:
            self._infer_lock.release()
        choice = resp["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason")

    def chat_stream(self, messages: list, max_tokens: int, temperature: float = 0.2):
        self.ensure_loaded()
        payload = {"messages": messages, "temperature": temperature, "top_p": 1.0,
                   "max_tokens": max_tokens, "stream": True}
        if not self._infer_lock.acquire(timeout=STREAM_ACQUIRE_TIMEOUT):
            yield {"error": "assistant is busy; please retry"}
            return
        resp = None
        try:
            req = urllib.request.Request(
                self.base + "/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"), headers=_HEADERS)
            resp = urllib.request.urlopen(req, timeout=STREAM_TIMEOUT)
            finish = None
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    d = json.loads(body)
                except Exception:
                    continue
                if d.get("error"):
                    yield {"error": "assistant error"}
                    return
                choice = (d.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    yield {"delta": delta}
            yield {"done": True, "truncated": finish == "length"}
        except Exception:
            yield {"error": "assistant request failed"}
        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass
            self._infer_lock.release()


# ---- the two servers -------------------------------------------------------
STRUCT = Server("structurer", STRUCT_MODEL_PATH, STRUCT_PORT, STRUCT_N_CTX)
PROOF = Server("proofreader", PROOF_MODEL_PATH, PROOF_PORT, PROOF_N_CTX, PROOF_EXTRA)

# Back-compat: STRUCTURE_MODEL_PATH used to be the single model. If a caller set
# it, honour it for MODEL_PATH reporting.
MODEL_PATH = STRUCT_MODEL_PATH


# ---- module-level shims (preserve old import sites) ------------------------
def ensure_loaded() -> None:          # structurer + chat (Qwen3)
    STRUCT.ensure_loaded()


def status() -> dict:                  # structurer status
    return STRUCT.status()


def ensure_proof() -> None:            # proofreader (ALLaM) — lazy-loaded
    PROOF.ensure_loaded()


def stop_proof() -> None:
    """Free ALLaM's ~5 GB after a proofread so the OCR pass and chat never
    collide with it on the 16 GB GPU. ALLaM reloads in a few seconds on the next
    /proofread (GGUF stays in the OS cache)."""
    PROOF.stop()


def proof_status() -> dict:
    return PROOF.status()


def stop_server() -> None:
    STRUCT.stop()
    PROOF.stop()


def chat_json(messages: list, schema: dict, max_tokens: int, temperature: float = 0.0):
    return STRUCT.chat_json(messages, schema, max_tokens, temperature)


def chat_stream(messages: list, max_tokens: int, temperature: float = 0.2):
    return STRUCT.chat_stream(messages, max_tokens, temperature)


def chat_text(messages: list, max_tokens: int, temperature: float = 0.0):
    return PROOF.chat_text(messages, max_tokens, temperature)   # proofreading -> ALLaM
