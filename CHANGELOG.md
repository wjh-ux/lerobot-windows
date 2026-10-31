# Changelog

All notable changes to this Windows support kit. The version tracks the LeRobot release it patches,
with a `-win.N` suffix for kit revisions of that same upstream release.

## v0.6.2-win.1 — 2026-10-31

First release. Targets LeRobot 0.6.2 (upstream commit
[`3f797de`](https://github.com/huggingface/lerobot/commit/3f797de5952432284a7c77ad2760218cb74e2f26)).

### Added

- `scripts/setup-lerobot-windows.ps1` — standalone CPython 3.12 through uv (no MSI, no admin rights),
  CPU or CUDA torch, LeRobot extras for the SO-101 workflow, and FFmpeg shared-DLL discovery so
  torchcodec loads instead of silently falling back to PyAV.
- `scripts/verify-lerobot-windows.py` — 11 runtime checks including a dataset write/read through the
  multiprocessing image writer and a video encode/decode round trip.
- `scripts/diagnose-windows.py` — six sections, ~30 checks, no hardware required: environment, paths,
  timing, processes, video stack, serial and cameras. `--md` / `--json` output for issue reports;
  `--camera-probe` adds a matrix of the camera modes that actually deliver frames.
- `docs/baseline-windows11.md` — reference diagnosis from a healthy Windows 11 machine with no hardware.
- `docs/hardware-checklist.md` — what to run once an arm and a camera are attached, and which evidence
  belongs on which upstream PR.
- `docs/host-notes.md` — host quirks that look like LeRobot bugs: broken Schannel, refused MSI
  installers, unwritable `tempfile.mkdtemp` under a restricted token, Bluetooth COM ports, no GPU.
- `patches/lerobot-windows.patch` — the six fixes as one diff.
- Support matrix in the README: what is verified, what is limited, what is deliberately out of scope.

### Fixed (in the patch)

- `lerobot-find-port`: diff only USB serial adapters (Bluetooth/virtual ports flap in and out), poll the
  port list instead of one fixed sleep, print port descriptions, and explain the Windows failure causes.
- `OpenCVCamera`: bounded Windows enumeration, reuse the discovered backend, and accept 4-channel
  (BGRA) frames instead of raising — this is what makes the repo's own camera tests pass on Windows.
- `torchcodec`: locate FFmpeg's *shared* DLLs (`LEROBOT_FFMPEG_DLL_DIR`, then PATH, then the usual
  install roots) and register them before import.
- Recording controls in a Windows console through `msvcrt`, including the arrow-key scan codes.
- `ReplayBuffer`: a failing prefetch thread raises in the consumer instead of deadlocking the process at
  0 % CPU; `torch.compile` falls back to eager on Windows without MSVC `cl.exe`.
- Test suite: `signal.SIGHUP` collection crash, `os.kill(os.getpid(), SIGINT)` going through
  `TerminateProcess`, and an unloadable torchcodec aborting collection.

### Verified

Windows 11 (build 26300), 6-core CPU, no NVIDIA GPU, Python 3.12.16, torch 2.11.0+cpu, torchcodec
0.11.1, FFmpeg 7.1 shared DLLs:

- `diagnose-windows.py` — 26 OK, 4 WARN (all "no hardware attached"), 0 FAIL
- `verify-lerobot-windows.py` — 11/11
- `pytest tests/cameras tests/utils/test_keyboard_input.py tests/utils/test_replay_buffer.py tests/utils/test_process.py` — 99 passed, 7 skipped
- `pytest --collect-only tests` — 4519 collected

### Known limitations

Hardware video encoding (NVENC/QSV/AMF) is not used; unattended/service mode is not supported (needs an
interactive desktop session); UVC exposure/gain control is unavailable on Windows; `torch.compile`
requires MSVC Build Tools.
