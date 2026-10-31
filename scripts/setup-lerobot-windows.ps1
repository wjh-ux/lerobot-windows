<#
.SYNOPSIS
    Install LeRobot on Windows without touching the system Python or the registry.

.DESCRIPTION
    The official instructions assume Linux/macOS and a working `uv`/`pip` on PATH. On a
    plain Windows box three things get in the way, and this script handles all of them:

    1. LeRobot needs Python >= 3.12 (the Microsoft Store shim and older Pythons fail).
       uv downloads a standalone CPython into a directory you own, so nothing global
       changes and no installer (MSI) or administrator rights are needed.
    2. `pip install torch` from PyPI pulls the CUDA build (~2.5 GB) even on a machine
       without an NVIDIA GPU. CPU wheels are installed by default here; use
       -Torch cuda128 to get the CUDA build instead.
    3. torchcodec (video decoding for datasets) needs FFmpeg *shared* DLLs, while the
       usual Windows ffmpeg download is a static build with only .exe files. The script
       points LeRobot at a shared build if one is present, and tells you where to get one
       otherwise.

.PARAMETER Root
    Installation root. Defaults to "$env:LOCALAPPDATA\lerobot-win".

.PARAMETER Repo
    Path to the LeRobot checkout to install in editable mode. Defaults to the parent of
    this script when it lives inside the checkout, otherwise the current directory.

.PARAMETER Torch
    cpu (default) or cuda128.

.PARAMETER Extras
    pip extras to install. The default covers the standard SO-101 workflow:
    feetech (motor bus), hardware (pyserial + pynput + deepdiff), dataset (datasets,
    video codecs) and viz (rerun).

.EXAMPLE
    .\setup-lerobot-windows.ps1 -Torch cuda128

.EXAMPLE
    # A minimal install for training/eval only (no motor bus, no cameras)
    .\setup-lerobot-windows.ps1 -Extras "dataset"
#>
[CmdletBinding()]
param(
    [string]$Root = "",
    [string]$Repo = "",
    [ValidateSet("cpu", "cuda128")]
    [string]$Torch = "cpu",
    [string]$Extras = "feetech,hardware,dataset,viz",
    [string]$PythonVersion = "3.12",
    [string]$FFmpegDllDir = ""
)

$ErrorActionPreference = "Stop"

function Write-Step($message) { Write-Host "`n=== $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "    $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }

if (-not $Root) { $Root = Join-Path $env:LOCALAPPDATA "lerobot-win" }
if (-not $Repo) {
    $candidate = Split-Path -Parent $PSScriptRoot
    if (Test-Path (Join-Path $candidate "pyproject.toml")) { $Repo = $candidate }
    elseif (Test-Path (Join-Path (Get-Location) "pyproject.toml")) { $Repo = (Get-Location).Path }
    else { throw "Cannot locate the LeRobot checkout: pass -Repo <path to lerobot>" }
}
$Repo = (Resolve-Path $Repo).Path

Write-Step "LeRobot -> $Repo"
Write-Ok "install root: $Root"
Write-Ok "torch build: $Torch   extras: $Extras"

$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root "pythons"
$env:UV_CACHE_DIR = Join-Path $Root "uv-cache"
$env:UV_LINK_MODE = "copy"
$env:UV_HTTP_TIMEOUT = "300"
New-Item -ItemType Directory -Force -Path $Root, $env:UV_PYTHON_INSTALL_DIR, $env:UV_CACHE_DIR | Out-Null

Write-Step "1/5 ensuring uv"
$uv = Join-Path $Root "bin\uv.exe"
if (-not (Test-Path $uv)) {
    $zip = Join-Path $Root "uv.zip"
    $url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
    Write-Ok "downloading $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    } catch {
        # Invoke-WebRequest goes through schannel: on a host whose certificate store is
        # broken (SEC_E_NO_CREDENTIALS) Python's own TLS stack still works.
        Write-Warn "Invoke-WebRequest failed ($($_.Exception.Message)); retrying through Python"
        $py = Get-Command python -ErrorAction SilentlyContinue
        if (-not $py) { throw "Download failed and python is not on PATH as a fallback." }
        & $py.Source -c "import sys,urllib.request;urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" $url $zip
    }
    Expand-Archive -Path $zip -DestinationPath (Join-Path $Root "bin") -Force
    Remove-Item $zip -Force
}
& $uv --version
Write-Ok "uv: $uv"

Write-Step "2/5 ensuring Python $PythonVersion"
& $uv python install $PythonVersion --no-bin
$venv = Join-Path $Repo ".venv"
if (Test-Path (Join-Path $venv "Scripts\python.exe")) {
    Write-Ok "reusing existing venv: $venv"
} else {
    & $uv venv --python $PythonVersion $venv
}
$python = Join-Path $venv "Scripts\python.exe"
& $python -V
Write-Ok "python: $python"

Write-Step "3/5 installing torch ($Torch)"
if ($Torch -eq "cpu") {
    $index = "https://download.pytorch.org/whl/cpu"
    $torchSpec = @("torch==2.11.0", "torchvision==0.26.0")
} else {
    $index = "https://download.pytorch.org/whl/cu128"
    $torchSpec = @("torch==2.11.0", "torchvision==0.26.0")
}
& $uv pip install --python $python --index-url $index @torchSpec
# Re-running this line is safe: pip/uv skip satisfied requirements, and re-running with a
# different -Torch needs --reinstall to actually swap the wheels.
Write-Ok "torch installed (CPU build if -Torch cpu)"

Write-Step "4/5 installing LeRobot ($Extras)"
& $uv pip install --python $python --no-build-isolation -e "$Repo[$Extras]"
Write-Ok "lerobot installed in editable mode"

Write-Step "5/5 FFmpeg"
$ffmpegExe = (Get-Command ffmpeg -ErrorAction SilentlyContinue)
if ($ffmpegExe) { Write-Ok "ffmpeg on PATH: $($ffmpegExe.Source)" } else { Write-Warn "ffmpeg not on PATH: dataset video encoding needs it (https://www.gyan.dev/ffmpeg/builds/)" }

if (-not $FFmpegDllDir) {
    # A shared build keeps avcodec-*.dll next to ffmpeg.exe (BtbN "shared" zips,
    # gyan.dev "full_build_shared"). A static build has no DLLs at all.
    $candidates = @()
    if ($ffmpegExe) { $candidates += Split-Path -Parent $ffmpegExe.Source }
    $candidates += (Get-ChildItem -Path $Root -Recurse -Filter "avcodec-*.dll" -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object { $_.DirectoryName })
    $candidates += @("C:\ffmpeg\bin", (Join-Path $env:ProgramFiles "ffmpeg\bin"), (Join-Path $env:LOCALAPPDATA "ffmpeg\bin"))
    # A shared build keeps avcodec-*.dll next to ffmpeg.exe (BtbN "shared" zips,
    # gyan.dev "full_build_shared"); a static build has no DLLs at all.
    $FFmpegDllDir = $candidates |
        Where-Object { $_ -and (Get-ChildItem -Path $_ -Filter "avcodec-*.dll" -ErrorAction SilentlyContinue | Select-Object -First 1) } |
        Select-Object -First 1
}
if ($FFmpegDllDir) {
    Write-Ok "FFmpeg shared DLLs: $FFmpegDllDir (torchcodec will use them)"
    [Environment]::SetEnvironmentVariable("LEROBOT_FFMPEG_DLL_DIR", $FFmpegDllDir, "User")
    Write-Ok "set LEROBOT_FFMPEG_DLL_DIR for the current user"
} else {
    Write-Warn "No FFmpeg shared DLLs found; videos will decode through PyAV (slower but correct)."
    Write-Warn "For torchcodec, unzip a *shared* build (BtbN: ffmpeg-n7.1-latest-win64-gpl-shared-7.1.zip)"
    Write-Warn "and re-run: .\setup-lerobot-windows.ps1 -FFmpegDllDir <that>\bin"
    $env:LEROBOT_FFMPEG_DLL_DIR = ""
}

Write-Step "verification"
& (Join-Path $venv "Scripts\lerobot-info.exe")
Write-Host ""
Write-Host "Done. Activate the environment with:" -ForegroundColor Cyan
Write-Host "    & `"$venv\Scripts\Activate.ps1`"" -ForegroundColor White
Write-Host "Then, with an SO-101 arm plugged in:" -ForegroundColor Cyan
Write-Host "    lerobot-find-port        # COM port of the USB serial adapter" -ForegroundColor White
Write-Host "    lerobot-setup-motors --robot.type=so101_follower --robot.port=COM5" -ForegroundColor White
Write-Host "    lerobot-calibrate --robot.type=so101_follower --robot.port=COM5" -ForegroundColor White
