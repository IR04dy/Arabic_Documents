"""Bounded NDJSON comparison endpoint with disconnect-aware cancellation."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from comparison import Cancelled, Document, compare

MAX_BODY = 1024 * 1024


def create_router(comparator=compare):
    router = APIRouter(prefix="/comparison", tags=["comparison"])
    slot = threading.BoundedSemaphore(1)
    here = Path(__file__).parent

    @router.get("/ui.js")
    def script():
        return FileResponse(here / "comparison_ui.js", media_type="text/javascript")

    @router.get("/ui.css")
    def stylesheet():
        return FileResponse(here / "comparison.css", media_type="text/css")

    @router.post("/run")
    async def run(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return JSONResponse({"error": "JSON مطلوب."}, status_code=415)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "حجم الطلب أكبر من الحد المسموح."}, status_code=413)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("بيانات الطلب غير صالحة.")
            a, b = Document.parse(payload.get("a")), Document.parse(payload.get("b"))
        except (ValueError, UnicodeError) as exc:
            message = str(exc) if not isinstance(exc, json.JSONDecodeError) else "JSON غير صالح."
            return JSONResponse({"error": message}, status_code=400)
        if not slot.acquire(blocking=False):
            return JSONResponse({"error": "توجد مقارنة قيد التنفيذ. انتظر انتهاء الجولة الحالية ثم أعد المحاولة."}, status_code=409)

        async def events():
            cancelled = threading.Event()
            messages = queue.Queue()

            def emit(event):
                if not cancelled.is_set():
                    messages.put(event)

            def work():
                try:
                    report = comparator(a, b, emit, cancelled)
                    emit({"event": "result", "report": report})
                except Cancelled:
                    pass
                except Exception:
                    emit({"event": "error", "message": "تعذرت المقارنة. تحقق من جاهزية النموذج المحلي ثم أعد المحاولة."})
                finally:
                    messages.put(None)
                    slot.release()

            started = False
            try:
                threading.Thread(target=work, daemon=True, name="document-comparison").start()
                started = True
                ticks = 0
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = messages.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.1)
                        ticks += 1
                        if ticks % 100 == 0:
                            yield '{"event":"heartbeat"}\n'
                        continue
                    if event is None:
                        break
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            finally:
                cancelled.set()
                if not started:
                    slot.release()

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return router
