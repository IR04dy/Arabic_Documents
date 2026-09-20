# Fetches the official llama.cpp CUDA build used for the structured-data stage.
#
# The structurer (structure.py) runs Qwen3-4B on the GPU via the official
# llama.cpp `llama-server.exe`. We use the CUDA 13.3 build because it supports
# Blackwell / sm_120 (RTX 50-series); the prebuilt llama-cpp-python wheels are
# CUDA 12.4 and crash on sm_120. This downloads that build + its CUDA runtime
# into vendor/llama-cuda/ (which is git-ignored — ~1.5 GB extracted).
#
# Re-run to (re)install; it skips the download if llama-server.exe is present.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$build = "b10919"                       # pinned llama.cpp release
$cuda  = "13.3"                         # supports Blackwell sm_120
$dest  = Join-Path $PSScriptRoot "vendor\llama-cuda"
$exe   = Join-Path $dest "llama-server.exe"

if (Test-Path $exe) {
  Write-Host "llama-server already present at $exe" -ForegroundColor Green
  exit 0
}

New-Item -ItemType Directory -Force -Path $dest | Out-Null
$base = "https://github.com/ggml-org/llama.cpp/releases/download/$build"
$tmp  = Join-Path $env:TEMP "llama-cuda-$build"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

foreach ($name in @(
    "llama-$build-bin-win-cuda-$cuda-x64.zip",
    "cudart-llama-bin-win-cuda-$cuda-x64.zip")) {
  $zip = Join-Path $tmp $name
  Write-Host "Downloading $name ..." -ForegroundColor Cyan
  Invoke-WebRequest -Uri "$base/$name" -OutFile $zip
  Write-Host "Extracting $name ..." -ForegroundColor Cyan
  Expand-Archive -Path $zip -DestinationPath $dest -Force
}

if (Test-Path $exe) {
  Write-Host "Done. llama-server.exe ready at $dest" -ForegroundColor Green
} else {
  throw "Extraction finished but llama-server.exe is missing in $dest"
}
