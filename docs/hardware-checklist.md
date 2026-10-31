# Hardware verification checklist

Everything in this repository was verified **without** hardware: the machine that produced
the patches had no USB-serial adapter and no camera, so the motor-bus and camera checks run
through their "nothing attached" branches. This file is what to do the moment an arm and a
camera are plugged in, and what evidence to attach to each upstream PR.

Nothing here is destructive; `setup-motors` writes servo IDs, everything else only reads.

## 0. Prerequisites

- SO-101 (or SO-100) leader + follower, powered, with the USB-C cable that carries data
  (not a charge-only cable).
- The CH340 driver installed (Device Manager should show a `USB-SERIAL CH340` under
  *Ports (COM & LPT)*; the port number is what you pass to `--robot.port`).
- A USB camera (any UVC webcam works) — close Teams / Zoom / the Camera app first, they hold
  the device exclusively on Windows.

## 1. Re-run the runtime check (USB serial + camera should stop being SKIP)

```powershell
.venv\Scripts\python.exe scripts\verify-lerobot-windows.py
```

Expected before / after:

| Check | No hardware | With hardware |
| --- | --- | --- |
| serial ports | `[SKIP]`-ish list of Bluetooth ports marked `(non-USB)` | `COM5 — USB-SERIAL CH340 (USB)` |
| cameras | `[SKIP] no camera attached` | `#0 via MSMF` (or `DSHOW`) |

Capture the whole output — it is the single most useful artifact for the PRs, because it
shows the Windows-specific discovery code running against real devices.

## 2. Serial port discovery — evidence for [#5177](https://github.com/huggingface/lerobot/pull/5177)

```powershell
lerobot-find-port
# prints the ports, then: Remove the USB cable ... press Enter
# unplug the arm's USB cable, press Enter, wait, replug
```

- [ ] Only the adapter appears in the diff (Bluetooth ports do not confuse it any more)
- [ ] The reported port is printed with its description, e.g. `COM5` + `(COM5 — USB-SERIAL CH340 (USB))`
- [ ] Unplug → Enter returns within a couple of seconds even though the driver is slow
- [ ] Ctrl-C / wrong cable gives the new guidance message rather than a bare traceback

Paste the console output into a comment on #5177, plus: adapter chip (CH340/CP210x), whether
the COM number changed between USB ports, and whether the port disappeared from Device
Manager on unplug.

## 3. Camera path — evidence for [#5178](https://github.com/huggingface/lerobot/pull/5178)

```powershell
lerobot-find-cameras opencv
```

- [ ] The camera is found, `Backend Api` says `MSMF` or `DSHOW`, and the scan takes well under a second
- [ ] `--output-dir` capture writes a PNG per camera
- [ ] 30 fps at 640x480 works with `backend: dshow, fourcc: MJPG` (this is the combination that
      avoids the Media Foundation frame drops)

Then a 30-second recording to prove the numpy path end to end:

```powershell
lerobot-record `
  --robot.type=so101_follower --robot.port=COM5 `
  --robot.cameras="{ top: {type: opencv, index_or_path: 0, width: 640, height: 480, fps: 30, backend: dshow, fourcc: MJPG} }" `
  --teleop.type=so101_leader --teleop.port=COM6 `
  --dataset.repo_id=<you>/so101_win_smoke --dataset.num_episodes=1 --dataset.single_task="smoke test"
```

Paste `lerobot-info` output plus the resolved `lerobot-record` summary into #5178. If the
camera streams **BGRA** (very common for UVC devices on Windows), that is exactly the case the
4-channel fix covers — mention it explicitly, it is the strongest evidence for that PR.

## 4. Windows CI lane — evidence for [#5183](https://github.com/huggingface/lerobot/pull/5183)

The lane runs `tests/utils` + `tests/cameras`. Hardware does not change its result, but a
green run on a fork (Actions enabled) is what turns the PR from "should work" into "does
work":

```powershell
# from your fork, with Actions enabled in the repo settings
git push origin ci/windows
# then open the "Fast Tests (Windows)" run under Actions
```

Also useful: `pytest -q tests/utils -m "not gpu"` on the real machine, to confirm the two
`tmp_path` tests that fail on a sandboxed host pass on a normal account.

## 5. Full arm flow (optional, but this is the payoff)

```powershell
lerobot-setup-motors --robot.type=so101_follower --robot.port=COM5
lerobot-calibrate   --robot.type=so101_follower --robot.port=COM5
lerobot-calibrate   --teleop.type=so101_leader  --teleop.port=COM6
lerobot-teleoperate --robot.type=so101_follower --robot.port=COM5 --teleop.type=so101_leader --teleop.port=COM6
```

While teleoperating, check the recording controls on Windows: in a console where `pynput`
cannot capture keys, the `msvcrt` backend (#5180) is what makes `n`/`r`/`q` and the arrow keys
work. If they do nothing, `pytest -q tests/utils/test_keyboard_input.py` is green but the
console path is the thing to look at — report it on #5180 with the console host you used
(Windows Terminal / conhost / VS Code terminal behave differently).

## What to send back

For each PR, one comment with:

1. the `verify-lerobot-windows.py` output (hardware lines now `OK`),
2. the exact `lerobot-info` block,
3. one line about the device (chip, camera model, USB port),
4. anything that behaved differently from this checklist.

That is enough for a maintainer to trust the Windows paths without owning the hardware.
