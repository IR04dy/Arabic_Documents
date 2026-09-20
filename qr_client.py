"""Local HTTP adapter; Arabic_Text_Extraction never imports QR Bot or Docker."""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as URLRequest, build_opener

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

MAX_UPLOAD = 101 * 1024 * 1024
MAX_ARTIFACT = 256 * 1024 * 1024
IDS = re.compile(r"qr_[0-9a-f]{32}\Z")
ARTIFACTS = re.compile(r"(?:report|pdf-[1-9][0-9]*|preview-[1-9][0-9]*-[1-9][0-9]*)\Z")


class QRServiceError(Exception):
    def __init__(self, message: str, status: int = 503):
        self.message, self.status = message, status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QRClient:
    def __init__(self, base_url: str | None = None, token_file: Path | None = None):
        self.base_url = (base_url or os.environ.get("QRBOT_API_URL", "http://127.0.0.1:8101")).rstrip("/")
        parts = urlsplit(self.base_url)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parts.username or parts.password or parts.path or parts.query or parts.fragment):
            raise ValueError("QRBOT_API_URL must be an HTTP URL on localhost")
        self.token_file = token_file or Path(os.environ.get(
            "QRBOT_API_TOKEN_FILE", Path(__file__).parent / "QR_Code_Scanner" / ".qrbot-api" / "service-token.txt"))
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, body: bytes | None = None,
                content_type: str = "application/json") -> tuple[bytes, str]:
        try:
            token = os.environ.get("QRBOT_API_TOKEN") or self.token_file.read_text(encoding="ascii").strip()
        except OSError:
            raise QRServiceError("Start the QR service with QR_Code_Scanner/run-api.ps1") from None
        request = URLRequest(self.base_url + path, data=body, method=method,
                             headers={"Authorization": "Bearer " + token, "Content-Type": content_type})
        limit = MAX_ARTIFACT if "/artifacts/" in path else 16 * 1024 * 1024
        try:
            with self.opener.open(request, timeout=120) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise QRServiceError("QR response exceeds the download limit", 413)
                return data, response.headers.get_content_type()
        except HTTPError as exc:
            try:
                message = json.loads(exc.read(16 * 1024)).get("error", "QR service rejected the request")
            except (ValueError, AttributeError):
                message = "QR service rejected the request"
            raise QRServiceError(message, exc.code if 400 <= exc.code <= 599 else 502) from None
        except (URLError, OSError):
            raise QRServiceError("QR service is unreachable; start QR_Code_Scanner/run-api.ps1") from None

    def submit(self, data: bytes, filename: str) -> dict:
        boundary = "qrbot" + secrets.token_hex(16)
        filename = re.sub(r'[\x00-\x1f"\\/]', "_", filename)
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        body = prefix + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        response, _ = self.request("POST", "/v1/jobs", body, f"multipart/form-data; boundary={boundary}")
        return json.loads(response)

    def get(self, job_id: str) -> dict:
        self.validate(job_id)
        data, _ = self.request("GET", f"/v1/jobs/{job_id}")
        return json.loads(data)

    def approve(self, job_id: str, link_ids: list[str]) -> dict:
        self.validate(job_id)
        data, _ = self.request("POST", f"/v1/jobs/{job_id}/approvals", json.dumps({"link_ids": link_ids}).encode())
        return json.loads(data)

    def artifact(self, job_id: str, artifact_id: str) -> tuple[bytes, str]:
        self.validate(job_id, artifact_id)
        return self.request("GET", f"/v1/jobs/{job_id}/artifacts/{artifact_id}")

    def skip(self, job_id: str, link_ids: list[str]) -> dict:
        self.validate(job_id)
        data, _ = self.request("POST", f"/v1/jobs/{job_id}/skips", json.dumps({"link_ids": link_ids}).encode())
        return json.loads(data)

    @staticmethod
    def validate(job_id: str, artifact_id: str | None = None) -> None:
        if not IDS.fullmatch(job_id) or (artifact_id is not None and not ARTIFACTS.fullmatch(artifact_id)):
            raise QRServiceError("QR job or artifact not found", 404)


def create_router(extractor=None, client: QRClient | None = None) -> APIRouter:
    client = client or QRClient()

    async def local_request(request: Request):
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            from fastapi import HTTPException
            raise HTTPException(403, "QR integration is local-only")
        if request.method == "POST":
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if request.headers.get("x-qrbot-request") != "1" or (origin and origin.rstrip("/") != expected):
                from fastapi import HTTPException
                raise HTTPException(403, "Same-origin QR request required")

    router = APIRouter(prefix="/qr", tags=["QR"], dependencies=[Depends(local_request)])

    async def bounded_body(request: Request, limit: int) -> bytes:
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise QRServiceError("Request is too large", 413)
        return bytes(data)

    async def forward(method: str, path: str, body=None, content_type="application/json", status=200):
        try:
            data, media_type = await run_in_threadpool(client.request, method, path, body, content_type)
            return Response(data, status_code=status, media_type=media_type,
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except QRServiceError as exc:
            return JSONResponse({"error": exc.message}, exc.status)

    @router.get("/health")
    async def health():
        return await forward("GET", "/health")

    @router.get("/ui.js")
    def ui_script():
        return Response(Path(__file__).with_name("qr_ui.js").read_text(encoding="utf-8"),
                        media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @router.get("/flow.js")
    def flow_script():
        return Response(Path(__file__).with_name("qr_flow.js").read_text(encoding="utf-8"),
                        media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @router.post("/jobs")
    async def submit(request: Request):
        try:
            data = await bounded_body(request, MAX_UPLOAD)
        except QRServiceError as exc:
            return JSONResponse({"error": exc.message}, exc.status)
        return await forward("POST", "/v1/jobs", data, request.headers.get("content-type", ""), 202)

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        if not IDS.fullmatch(job_id):
            return JSONResponse({"error": "Job not found"}, 404)
        return await forward("GET", f"/v1/jobs/{job_id}")

    @router.post("/jobs/{job_id}/approvals")
    async def approve(job_id: str, request: Request):
        if not IDS.fullmatch(job_id):
            return JSONResponse({"error": "Job not found"}, 404)
        try:
            data = await bounded_body(request, 16 * 1024)
        except QRServiceError as exc:
            return JSONResponse({"error": exc.message}, exc.status)
        return await forward("POST", f"/v1/jobs/{job_id}/approvals", data, status=202)

    @router.post("/jobs/{job_id}/skips")
    async def skip(job_id: str, request: Request):
        if not IDS.fullmatch(job_id):
            return JSONResponse({"error": "Job not found"}, 404)
        try:
            data = await bounded_body(request, 16 * 1024)
        except QRServiceError as exc:
            return JSONResponse({"error": exc.message}, exc.status)
        return await forward("POST", f"/v1/jobs/{job_id}/skips", data)

    @router.get("/jobs/{job_id}/artifacts/{artifact_id}")
    async def artifact(job_id: str, artifact_id: str):
        try:
            data, media_type = await run_in_threadpool(client.artifact, job_id, artifact_id)
            preview = artifact_id.startswith("preview-")
            if preview and media_type != "image/png":
                raise QRServiceError("Artifact is not a preview image", 415)
            filename = "report.json" if artifact_id == "report" else artifact_id + (".png" if preview else ".pdf")
            disposition = "inline" if preview else "attachment"
            return Response(data, media_type=media_type, headers={
                "Content-Disposition": f'{disposition}; filename="{filename}"', "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff"})
        except QRServiceError as exc:
            return JSONResponse({"error": exc.message}, exc.status)

    @router.post("/jobs/{job_id}/artifacts/{artifact_id}/extract")
    async def extract_attachment(job_id: str, artifact_id: str):
        if extractor is None:
            return JSONResponse({"error": "OCR attachment extraction unavailable"}, 503)
        try:
            job = await run_in_threadpool(client.get, job_id)
            source = next((a for a in job["artifacts"] if a["id"] == artifact_id and a["media_type"] == "application/pdf"), None)
            if source is None:
                raise QRServiceError("PDF artifact not found", 404)
            data, media_type = await run_in_threadpool(client.artifact, job_id, artifact_id)
            if media_type != "application/pdf":
                raise QRServiceError("Artifact is not a PDF", 415)
            extracted = await run_in_threadpool(extractor, data, artifact_id + ".pdf")
            return {"job_id": job_id, "artifact_id": artifact_id, "source_url": source["source_url"],
                    "found_on_pages": source["found_on_pages"], "extraction": extracted.to_dict()}
        except QRServiceError as exc:
            return JSONResponse({"error": exc.message}, exc.status)
        except Exception:
            return JSONResponse({"error": "Attachment OCR failed; the original document is unchanged"}, 500)

    return router
