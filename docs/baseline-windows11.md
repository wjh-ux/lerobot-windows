# Baseline: a healthy Windows 11 machine

Reference output of [`scripts/diagnose-windows.py`](../scripts/diagnose-windows.py) on the machine this
repository was developed and verified on, **without hardware attached** (no USB serial adapter, no
camera). Compare your own report against it: the `WARN` rows below are the expected "nothing plugged in"
state; everything else should match on a working Windows 11 + Python 3.12 + CPU torch setup.

```powershell
.venv\Scripts\python.exe scripts\diagnose-windows.py --md baseline.md
```

How to read a report:

* `FAIL` rows are real problems, and each one carries a `->` hint with the fix.
* `WARN` rows are either "not installed" (PyAV fallback, no GPU) or "no hardware attached".
* `SKIP` rows mean the check needed a component that is not installed.

Paths below are shortened to `<workdir>`, `<repo>` and `<ffmpeg-7.1-shared>`.

---

# LeRobot Windows support report

- generated: 2026-10-31 22:27:52
- host: Windows-11-10.0.26300-SP0
- python: 3.12.16 (AMD64)

## environment

| check | status | detail |
| --- | --- | --- |
| windows build | OK | Windows-11-10.0.26300-SP0 (build 26300) |
| python | OK | 3.12.16 AMD64 GIL |
| admin rights | OK | standard user (nothing here needs elevation) |
| cpu | OK | 6 logical cores, 29.9 GB RAM |
| free disk | OK | 52.4 GB free on the working drive |
| console code page | OK | cp65001 (stdout encoding utf-8) |
| torch | OK | 2.11.0+cpu, cuda available: False, threads 6 |
| HF_HOME | OK | <workdir>\hf (exists) |
| HF_LEROBOT_HOME | OK | <workdir>\hf\lerobot (exists) |
| LEROBOT_FFMPEG_DLL_DIR | OK | <ffmpeg-7.1-shared>\bin (exists) |

## paths

| check | status | detail |
| --- | --- | --- |
| temp directory writable | OK | <repo>\lerobot-win |
| long paths (>260 chars) | OK | LongPathsEnabled=True, wrote a 320-char path: True |
| non-ASCII video round trip | OK | encode+decode -> (2, 3, 48, 64) |
| non-ASCII dataset round trip | OK | 3 frames, image (3, 48, 64) |

## timing

| check | status | detail |
| --- | --- | --- |
| sleep(1 ms) quantisation | OK | overshoot mean 0.53 ms, worst 0.85 ms |
| timer resolution 1 ms | OK | sleep(33.3 ms) overshoot mean 0.31 ms, worst 0.53 ms |
| precise_sleep at 30 Hz | OK | mean overshoot 0.01 ms of a 33.3 ms budget, worst 0.01 ms |
| precise_sleep at 60 Hz | OK | mean overshoot 0.01 ms of a 16.7 ms budget, worst 0.01 ms |
| torch thread scaling | OK | 1 threads: 59 ms, 6 threads: 26 ms (speedup 2.2x) |

## processes

| check | status | detail |
| --- | --- | --- |
| spawn() empty process | OK | 73 ms each |
| spawn() + import torch | OK | 2.87 s |
| spawn() + import PyAV | OK | 0.26 s |

## video

| check | status | detail |
| --- | --- | --- |
| ffmpeg CLI | OK | <ffmpeg-7.1-shared>\bin\ffmpeg.EXE — ffmpeg version n7.1.1-6-g48c0f071d4-20250430 Copyright (c) 2000-2025 the FFmpeg developers |
| FFmpeg shared DLLs (torchcodec) | OK | <ffmpeg-7.1-shared>\bin |
| video decode backend | OK | torchcodec |
| encode/decode round trip | OK | 6 frames encoded in 0.10s, decoded (2, 3, 48, 64) |

## serial

| check | status | detail |
| --- | --- | --- |
| port COM4 | WARN | 蓝牙链接上的标准串行 (COM4) (non-USB, will not open) |
| port COM5 | WARN | 蓝牙链接上的标准串行 (COM5) (non-USB, opens) |
| USB serial adapter | WARN | none detected |

- **port COM4** (WARN): Bluetooth/virtual port, not a motor bus. It also flaps in and out of existence, so ignore it in device lists.
- **port COM5** (WARN): Bluetooth/virtual port, not a motor bus. It also flaps in and out of existence, so ignore it in device lists.
- **USB serial adapter** (WARN): Only Bluetooth/virtual ports are present. Plug the arm's USB cable in and install the CH340 driver (Device Manager -> Ports).

## cameras

| check | status | detail |
| --- | --- | --- |
| camera enumeration | WARN | none found in 0.5s |

- **camera enumeration** (WARN): No UVC camera enumerated. Close Teams/Zoom/Camera app (they hold the device exclusively), allow desktop apps in Settings > Privacy > Camera, check Device Manager.

**Summary:** 26 OK, 4 warn, 0 fail, 0 skipped.
