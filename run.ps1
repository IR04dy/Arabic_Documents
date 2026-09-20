# Arabic PDF Text Extraction (Surya OCR) — launcher.
# Uses the system Python, which already has the CUDA torch stack. A fresh venv
# would pull a CPU-only torch, so we do NOT create one here.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "Checking dependencies..." -ForegroundColor Cyan
python -c "import torch, surya, pypdfium2, PIL, fastapi, uvicorn; print('deps OK; CUDA:', torch.cuda.is_available())"

# Both the Surya OCR VLM and the Qwen3 structurer run on the vendored llama.cpp
# CUDA server, so fetch it if missing.
if (-not (Test-Path (Join-Path $PSScriptRoot "vendor\llama-cuda\llama-server.exe"))) {
  Write-Host "llama.cpp runtime missing; fetching CUDA build..." -ForegroundColor Cyan
  & (Join-Path $PSScriptRoot "fetch_llama_server.ps1")
}

Write-Host ""
Write-Host "Server: http://127.0.0.1:8100" -ForegroundColor Green
Write-Host "OCR: Surya 2 (GPU via llama.cpp) · Structurer: Qwen3-4B (GPU)." -ForegroundColor DarkGray
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""
python -m uvicorn app:app --host 127.0.0.1 --port 8100
