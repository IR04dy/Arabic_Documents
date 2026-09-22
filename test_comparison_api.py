import asyncio
import json
import threading
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from comparison_api import MAX_BODY, create_router

PAYLOAD = {"a": {"name": "A", "text": "نص أول"}, "b": {"name": "B", "text": "نص ثان"}}


class ComparisonAPITests(unittest.TestCase):
    def client(self, compare=None):
        app = FastAPI()
        app.include_router(create_router(compare or (lambda a, b, emit, cancel: {"findings": []})))
        return TestClient(app)

    def test_stream_preserves_source_and_returns_progress_then_result(self):
        def compare(a, b, emit, cancel):
            self.assertEqual(a.text, PAYLOAD["a"]["text"])
            emit({"event": "progress", "done": 0, "total": 1})
            return {"findings": [], "complete": True}
        response = self.client(compare).post("/comparison/run", json=PAYLOAD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([json.loads(line)["event"] for line in response.text.splitlines()], ["progress", "result"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_invalid_and_oversized_bodies(self):
        client = self.client()
        self.assertEqual(client.post("/comparison/run", content="text").status_code, 415)
        for value in [[], {}, {"a": {"name": "a", "text": " "}}]:
            self.assertEqual(client.post("/comparison/run", json=value).status_code, 400)
        self.assertEqual(client.post("/comparison/run", content=b"x" * (MAX_BODY + 1),
                                     headers={"Content-Type": "application/json"}).status_code, 413)
        self.assertEqual(client.post("/comparison/run", content="{", headers={"Content-Type": "application/json"}).status_code, 400)

    def test_exception_is_safe_stream_error_and_releases_slot(self):
        def fail(*args):
            raise RuntimeError("private document contents")
        client = self.client(fail)
        for _ in range(2):
            response = client.post("/comparison/run", json=PAYLOAD)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.text)["event"], "error")
            self.assertNotIn("private document", response.text)

    def test_second_run_busy_until_worker_finishes(self):
        started, release = threading.Event(), threading.Event()
        def blocked(a, b, emit, cancel):
            started.set()
            release.wait(5)
            return {"findings": []}
        client = self.client(blocked)
        results = []
        thread = threading.Thread(target=lambda: results.append(client.post("/comparison/run", json=PAYLOAD)))
        thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertEqual(client.post("/comparison/run", json=PAYLOAD).status_code, 409)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(client.post("/comparison/run", json=PAYLOAD).status_code, 200)

    def test_assets_available(self):
        client = self.client()
        self.assertIn("selectTab", client.get("/comparison/ui.js").text)
        self.assertIn("workspace-tabs", client.get("/comparison/ui.css").text)

    def test_disconnect_signals_worker_and_releases_comparison_slot(self):
        stopped = threading.Event()
        def worker(a, b, emit, cancel):
            emit({"event": "progress", "done": 0, "total": 1})
            if cancel.wait(3):
                stopped.set()
            return {"findings": []}
        app = FastAPI()
        app.include_router(create_router(worker))
        async def scenario():
            incoming = asyncio.Queue()
            await incoming.put({"type": "http.request", "body": json.dumps(PAYLOAD).encode(), "more_body": False})
            async def send(message):
                if message["type"] == "http.response.body" and b'progress' in message.get("body", b''):
                    await incoming.put({"type": "http.disconnect"})
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                     "http_version": "1.1", "method": "POST", "scheme": "http",
                     "path": "/comparison/run", "raw_path": b"/comparison/run", "query_string": b"",
                     "root_path": "", "headers": [(b"content-type", b"application/json")],
                     "server": ("localhost", 80), "client": ("127.0.0.1", 1000)}
            await asyncio.wait_for(app(scope, incoming.get, send), timeout=5)
        asyncio.run(scenario())
        self.assertTrue(stopped.wait(2))
        # The worker can release the slot just after setting the observation event.
        response = TestClient(app).post("/comparison/run", json=PAYLOAD)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
