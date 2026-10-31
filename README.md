# LeRobot on Windows

Working notes, patches and scripts for running [LeRobot](https://github.com/huggingface/lerobot) on Windows:
what actually breaks, why, and the fixes — all verified on a real Windows 11 machine, not just written down.

Upstream PRs opened from this work (draft until the human contributor reviews them):

| PR | Area |
| --- | --- |
| [#5177](https://github.com/huggingface/lerobot/pull/5177) | `lerobot-find-port`: reliable USB serial port discovery |
| [#5178](https://github.com/huggingface/lerobot/pull/5178) | Cameras: Windows enumeration + 4-channel (BGRA) frames |
| [#5179](https://github.com/huggingface/lerobot/pull/5179) | torchcodec: find FFmpeg's shared DLLs before import |
| [#5180](https://github.com/huggingface/lerobot/pull/5180) | Recording controls from a Windows console (`msvcrt`) |
| [#5181](https://github.com/huggingface/lerobot/pull/5181) | Replay buffer: no more silent deadlock on failed prefetch |
| [#5182](https://github.com/huggingface/lerobot/pull/5182) | Test suite: collectible and runnable on Windows |
| [#5183](https://github.com/huggingface/lerobot/pull/5183) | CI: a `windows-latest` lane for `tests/utils` + `tests/cameras` |

They are drafts on purpose: this project's `AI_POLICY.md` asks a human to review AI-assisted
work before it invites maintainer review. Flipping one to *Ready for review* is the last step.

With an arm and a camera plugged in, [docs/hardware-checklist.md](docs/hardware-checklist.md)
lists what to run and which evidence belongs on which PR.


## Quick start

```powershell
# 1. install (CPU torch by default; use -Torch cuda128 on an NVIDIA box)
.\scripts\setup-lerobot-windows.ps1 -Repo C:\path\to\lerobot

# 2. check the runtime without any hardware
.venv\Scripts\python.exe .\scripts\verify-lerobot-windows.py
```

`setup-lerobot-windows.ps1` downloads a standalone CPython 3.12 through [uv] into a directory you own
(no MSI, no admin, no changes to a system Python), installs torch from the CPU or CUDA index, installs
LeRobot in editable mode with the extras the SO-101 workflow needs, and points `LEROBOT_FFMPEG_DLL_DIR`
at an FFmpeg *shared* build if one is present.

## Is this machine supported? Run the diagnosis

```powershell
.venv\Scripts\python.exe scripts\diagnose-windows.py --md my-report.md
```

Six sections, ~30 checks, no hardware needed: environment (build, code page, long paths, temp dirs),
paths (Chinese/spaced directories through dataset + video round trips), timing (sleep quantisation and
`precise_sleep` jitter at 30/60 Hz), processes (spawn cost with and without torch), video stack
(ffmpeg CLI, FFmpeg shared DLLs, torchcodec backend, encode/decode), serial (USB vs Bluetooth vs
virtual, and whether each port can be opened), cameras (enumeration, backend, and with `--camera-probe`
a matrix of the modes that actually deliver frames). Every `WARN`/`FAIL` prints the fix.

[`docs/baseline-windows11.md`](docs/baseline-windows11.md) is the reference report from a healthy
Windows 11 box with no hardware attached — compare yours against it.

## Windows support status

What is verified on this platform, and what is deliberately out of scope:

| Area | Status | Notes |
| --- | --- | --- |
| Install (CPU / CUDA torch, extras) | ✅ verified | `scripts/setup-lerobot-windows.ps1`, no MSVC needed |
| `lerobot-info`, `find-port`, `find-cameras`, `calibrate`, `record`, `replay`, `dataset-viz` | ✅ verified (no hardware) | arm/camera paths exercised through their "nothing attached" branches |
| Dataset write + read, multiprocessing image writer | ✅ verified | 4 frames round trip, video via torchcodec |
| Video encode/decode | ✅ verified | libsvtav1 software encoding; torchcodec with FFmpeg **shared** DLLs, PyAV otherwise |
| Chinese / spaced / long paths | ✅ verified | `LongPathsEnabled` required above 260 chars |
| UVC cameras via OpenCV | ⚠️ works, limited | `backend: dshow`, `fourcc: MJPG` for stable 30 fps; **no exposure/gain control** (OpenCV exposes no UVC control surface on Windows) |
| Intel RealSense | ⚠️ wheel exists | `pip install 'lerobot[intelrealsense]'`; untested here (no device) |
| Training | ⚠️ CPU only here | no GPU on the reference machine; use `-Torch cuda128` on an NVIDIA box |
| Hardware video encoding (NVENC/QSV/AMF) | ❌ not used | LeRobot encodes with libsvtav1; adding hardware encoders would break cross-platform reproducibility |
| Unattended / Windows service mode | ❌ not supported | the global keyboard hook and the console fallback both need an interactive desktop session |
| `torch.compile` acceleration | ❌ needs MSVC | without VS Build Tools the RL buffer keeps the eager path instead of failing |


## What breaks, and what is fixed here

The patch in [`patches/`](patches) is the same content as the six PRs, applied to one tree.

### Serial port discovery (`lerobot-find-port`)

The helper diffs the serial port list before/after unplugging the bus. On Windows that list also holds
Bluetooth and virtual COM ports (`hwid` starting with `BTHENUM\`), which come and go on their own, and the
driver needs a moment to publish or retire the port. Result: "more than one port", or "no difference" plus
a raw traceback. Now only USB serial adapters are diffed (with a fallback when none is present), the list
is polled until it changes (5 s budget), and the messages name the real causes: missing CH340/CP210x driver,
port held by another program, adapter not powered.

### Cameras (`OpenCVCamera`)

* Enumeration probed all 60 indices even after the last camera was found. Index scanning now stops after 10
  consecutive misses, reports the backend that opened the device, and `lerobot-find-cameras` reuses it.
* `_postprocess_image()` rejected any 4-channel frame. On Windows that is normal (Media Foundation streams
  BGRA for some UVC cameras; OpenCV 4.13's image backend returns RGBA for RGBA inputs) and it made every
  camera test in the repo fail here. The alpha channel is dropped instead.
* `RealSenseCamera.find_cameras()` said `'NoneType' object has no attribute 'context'` when `pyrealsense2`
  was missing; it now raises the documented `ImportError`.

### Video decoding (`torchcodec`)

The torchcodec wheel does not ship FFmpeg: its native library links against the *shared* `avcodec-*.dll`.
Most Windows ffmpeg installs are static (`ffmpeg.exe` only), so `import torchcodec` fails, LeRobot silently
falls back to the slower PyAV decoder, and `lerobot-info` shows `Torchcodec version: ... (import failed)`.
`prepare_torchcodec_ffmpeg_dlls()` searches for a directory containing `avcodec-*.dll`
(`LEROBOT_FFMPEG_DLL_DIR`, then the `ffmpeg` on `PATH`, then the usual install roots), registers it with
`os.add_dll_directory`, and runs before every torchcodec import. With an FFmpeg 7.1 shared build the default
backend switches from `pyav` to `torchcodec`.

### Recording controls in a Windows console

`TerminalKeyListener` — the fallback when the global `pynput` hook is unavailable — only implemented the
POSIX termios path, so on Windows `start()` was a no-op and the →/←/Esc, n/r/q controls did nothing. It now
polls the console with `msvcrt.kbhit()`/`getwch()` and decodes the Windows arrow-key encoding
(NUL/0xE0 prefix + H/P/K/M).

### Silent hang in the replay buffer

`ReplayBuffer._get_async_iterator` swallowed exceptions raised while sampling and left the consumer blocked
on an unbounded `queue.get()`. The process sat at 0 % CPU forever with no error. Exceptions are now
re-raised in the consumer. It was reachable on Windows because the default image augmentation used
`torch.compile`, which needs MSVC `cl.exe` there: without a C++ toolchain the first batch raised
`InductorError: Compiler: cl is not found`. `_maybe_compile()` keeps the eager function on Windows when
`cl.exe` is absent.

### Test suite

`signal.SIGHUP`/`SIGQUIT` were referenced eagerly in `pytest.param`/fixtures (collection `AttributeError` on
Windows), signals were delivered with `os.kill(os.getpid(), SIGINT)` (that is `TerminateProcess` on Windows —
it kills pytest), and `pytest.importorskip("torchcodec")` could not handle an installed-but-unloadable
torchcodec (collection error instead of a skip). All three fixed.

## Verified results

Windows 11 (10.0.26300), 6-core CPU, no NVIDIA GPU, Python 3.12.16, torch 2.11.0+cpu:

| Check | Result |
| --- | --- |
| `verify-lerobot-windows.py` | 11/11 checks (hardware checks SKIP without a device) |
| `pytest tests/cameras tests/utils/test_keyboard_input.py tests/utils/test_replay_buffer.py tests/utils/test_process.py` | 99 passed, 7 skipped |
| `pytest --collect-only tests` | 4519 tests collected |
| Dataset write + read (multiprocess image writer) | 4 frames, video decode via torchcodec |
| `lerobot-info` | torch 2.11.0+cpu, Torchcodec 0.11.1 loaded |

## Environment notes

Host quirks that cost hours on the machine this was developed on — broken Schannel, refused MSI
installers, unwritable `tempfile.mkdtemp` directories under a restricted token, Bluetooth COM
ports masquerading as a robot, no GPU — are collected in
[docs/host-notes.md](docs/host-notes.md), with the workaround for each. None of them are LeRobot
bugs, and a normal Windows 11 account hits at most one.

## Hardware walkthrough (SO-101)

```powershell
lerobot-find-port                                        # prints the USB serial adapter, e.g. COM5
lerobot-setup-motors --robot.type=so101_follower --robot.port=COM5
lerobot-calibrate   --robot.type=so101_follower --robot.port=COM5
lerobot-calibrate   --teleop.type=so101_leader  --teleop.port=COM6
lerobot-teleoperate --robot.type=so101_follower --robot.port=COM5 --teleop.type=so101_leader --teleop.port=COM6

lerobot-record `
  --robot.type=so101_follower --robot.port=COM5 `
  --robot.cameras="{ top: {type: opencv, index_or_path: 0, width: 640, height: 480, fps: 30, backend: dshow, fourcc: MJPG} }" `
  --teleop.type=so101_leader --teleop.port=COM6 `
  --dataset.repo_id=<you>/so101_test --dataset.num_episodes=3 --dataset.single_task="pick and place"
```

Pitfalls worth the two minutes:

* **Driver**: CH340 (the adapter in most SO-101 kits) needs its driver; the port only exists once Windows
  shows `COMx` in Device Manager.
* **Exclusive access**: serial ports are exclusive. Arduino IDE's monitor, a vendor tool, or a stale process
  holding the port looks like "cannot open port".
* **Cameras**: Media Foundation (OpenCV's default) is slow to start and drops frames; `backend: dshow` with
  `fourcc: MJPG` is usually the stable 30 fps combination. A camera open in Teams/Zoom is invisible to
  enumeration.
* **Data location**: defaults to `%USERPROFILE%\.cache\huggingface\lerobot`; set `HF_LEROBOT_HOME` to move it.
* **Port numbers move** between USB plugs — re-run `lerobot-find-port` rather than trusting an old `COMx`.

## Repository contents

```
patches/lerobot-windows.patch   # combined diff against huggingface/lerobot @ 3f797de (v0.6.2)
scripts/setup-lerobot-windows.ps1
scripts/verify-lerobot-windows.py
scripts/diagnose-windows.py     # six-section support diagnosis, no hardware needed
docs/baseline-windows11.md      # reference report from a healthy Windows 11 box
docs/hardware-checklist.md      # what to run once an arm + camera are attached, and what to report
docs/host-notes.md              # host quirks (Schannel, MSI, tempfile ACLs, Bluetooth COM, no GPU)
README.md                       # this file
README.zh-CN.md                 # 中文说明（含逐文件改动原因与排查记录）
```

Apply the combined patch to a checkout of upstream:

```powershell
git -C C:\path\to\lerobot apply patches\lerobot-windows.patch
```

## License

The scripts and documents here are offered under Apache-2.0, matching LeRobot. The patch modifies LeRobot
source and carries LeRobot's copyright headers.

[uv]: https://github.com/astral-sh/uv
