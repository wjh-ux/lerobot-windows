#!/usr/bin/env python
"""Diagnose whether LeRobot can actually run on this Windows machine, and why not.

The script answers the questions that dominate Windows issue reports, with measurements
instead of guesses:

* environment: OS build, console code page, long-path policy, admin rights, temp dirs;
* paths: does a Chinese/spaced directory survive dataset + video round trips;
* timing: what a 30 Hz control loop really gets (sleep quantisation, ``precise_sleep``);
* processes: what a spawned worker costs when there is no ``fork()``;
* video stack: ffmpeg CLI, FFmpeg *shared* DLLs for torchcodec, encode/decode round trip;
* serial: which ports are USB adapters, which are Bluetooth/virtual, which can be opened;
* cameras: which indices enumerate, through which backend, and which modes really open.

Nothing here needs hardware, and nothing writes outside ``--workdir``.

Usage:
    python scripts\\diagnose-windows.py                  # human readable summary
    python scripts\\diagnose-windows.py --md report.md   # paste-able support report
    python scripts\\diagnose-windows.py --json out.json  # raw results
    python scripts\\diagnose-windows.py --quick          # skip the slow round trips
    python scripts\\diagnose-windows.py --camera-probe   # try every camera mode (slow)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
IS_WINDOWS = sys.platform == "win32"


# ── result plumbing ────────────────────────────────────────────────────────────
class Check:
    """One row of the report."""

    def __init__(self, name: str, status: str, detail: str = "", hint: str = "") -> None:
        self.name = name
        self.status = status
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


class Report:
    """Ordered sections of :class:`Check` rows."""

    def __init__(self) -> None:
        self.sections: list[tuple[str, list[Check]]] = []

    def add(self, section: str, check: Check) -> Check:
        for name, checks in self.sections:
            if name == section:
                checks.append(check)
                return check
        self.sections.append((section, [check]))
        return check

    def counts(self) -> dict[str, int]:
        result = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for _, checks in self.sections:
            for check in checks:
                result[check.status] = result.get(check.status, 0) + 1
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sections": {name: [check.as_dict() for check in checks] for name, checks in self.sections},
            "counts": self.counts(),
        }

    def print_console(self) -> None:
        symbols = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}
        for name, checks in self.sections:
            print(f"\n=== {name}")
            for check in checks:
                print(f"{symbols.get(check.status, '[????]')} {check.name}: {check.detail}")
                if check.hint and check.status in (WARN, FAIL):
                    for line in check.hint.splitlines():
                        print(f"         -> {line}")
        counts = self.counts()
        print(f"\nsummary: {counts[OK]} ok, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skipped")

    def to_markdown(self) -> str:
        lines = [
            "# LeRobot Windows support report",
            "",
            f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- host: {platform.platform()}",
            f"- python: {platform.python_version()} ({platform.machine()})",
            "",
        ]
        for name, checks in self.sections:
            lines.append(f"## {name}")
            lines.append("")
            lines.append("| check | status | detail |")
            lines.append("| --- | --- | --- |")
            for check in checks:
                detail = check.detail.replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {check.name} | {check.status} | {detail} |")
            hints = [check for check in checks if check.hint and check.status in (WARN, FAIL)]
            if hints:
                lines.append("")
                for check in hints:
                    hint = " ".join(check.hint.split())
                    lines.append(f"- **{check.name}** ({check.status}): {hint}")
            lines.append("")
        counts = self.counts()
        lines.append(
            f"**Summary:** {counts[OK]} OK, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skipped."
        )
        return "\n".join(lines) + "\n"


REPORT = Report()


def run_section(name: str, fn: Callable[[], None]) -> None:
    """Run one section; a crash inside it becomes a FAIL row instead of killing the report."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        REPORT.add(name, Check("section crashed", FAIL, f"{type(exc).__name__}: {exc}"))


# ── small helpers ──────────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return completed.returncode, (completed.stdout or completed.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _console_code_page() -> int | None:
    if not IS_WINDOWS:
        return None
    import ctypes

    try:
        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:  # noqa: BLE001
        return None


def _long_paths_enabled() -> bool | None:
    if not IS_WINDOWS:
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except OSError:
        return False


def _memory_gb() -> float | None:
    if not IS_WINDOWS:
        return None
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return round(status.ullTotalPhys / 2**30, 1)


def _is_admin() -> bool | None:
    if not IS_WINDOWS:
        return None
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return None


def _sleep_overshoot(period_ms: float, samples: int = 15) -> tuple[float, float]:
    overshoots = []
    for _ in range(samples):
        start = time.perf_counter()
        time.sleep(period_ms / 1000)
        overshoots.append((time.perf_counter() - start) * 1000 - period_ms)
    return statistics.mean(overshoots), max(overshoots)


def _spawn_noop() -> None:
    """Module level on purpose: a closure cannot be pickled to a spawned child."""
    return


def _import_module_child(module: str) -> None:  # pragma: no cover - runs in the child
    """Import one module in a spawned child, to price a worker's start-up."""
    __import__(module)


def _spawned_import_cost(module: str) -> tuple[float, bool]:
    """Time a spawned child that imports ``module``; returns (seconds, success)."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    started = time.perf_counter()
    proc = ctx.Process(target=_import_module_child, args=(module,))
    proc.start()
    proc.join()
    return time.perf_counter() - started, proc.exitcode == 0


# ── sections ───────────────────────────────────────────────────────────────────
def section_environment() -> None:
    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        REPORT.add("environment", Check(name, status, detail, hint))

    version = sys.getwindowsversion() if IS_WINDOWS else None
    add(
        "windows build",
        OK if IS_WINDOWS else SKIP,
        f"{platform.platform()} (build {version.build if version else 'n/a'})",
    )
    free_threaded = "t" in getattr(sys, "abiflags", "")
    add(
        "python",
        OK if sys.version_info >= (3, 12) else FAIL,
        f"{platform.python_version()} {platform.machine()} {'free-threaded' if free_threaded else 'GIL'}",
        "" if sys.version_info >= (3, 12) else "LeRobot needs Python >= 3.12; install one through uv.",
    )
    admin = _is_admin()
    add("admin rights", OK, "elevated" if admin else "standard user (nothing here needs elevation)")

    memory = _memory_gb()
    add("cpu", OK, f"{os.cpu_count()} logical cores" + (f", {memory} GB RAM" if memory else ""))

    free = shutil.disk_usage(Path.cwd())
    add(
        "free disk",
        OK if free.free / 2**30 > 20 else WARN,
        f"{free.free / 2**30:.1f} GB free on the working drive",
        "" if free.free / 2**30 > 20 else "A dataset fills fast (a few GB per 100 episodes with video).",
    )

    code_page = _console_code_page()
    add(
        "console code page",
        OK if code_page in (65001, None) or os.environ.get("PYTHONUTF8") == "1" else WARN,
        f"cp{code_page} (stdout encoding {sys.stdout.encoding})",
        "Non-ASCII device names print as mojibake. Use `chcp 65001` or set PYTHONUTF8=1; the port "
        "descriptions from lerobot-find-port are the usual casualty.",
    )

    torch_version, cuda, threads = "not installed", "n/a", "n/a"
    try:
        import torch

        torch_version = torch.__version__
        threads = str(torch.get_num_threads())
        cuda = f"cuda available: {torch.cuda.is_available()}"
    except ImportError:
        pass
    add(
        "torch",
        OK if torch_version != "not installed" else SKIP,
        f"{torch_version}, {cuda}, threads {threads}",
    )

    for name in ("HF_HOME", "HF_LEROBOT_HOME", "LEROBOT_FFMPEG_DLL_DIR"):
        value = os.environ.get(name)
        if value:
            exists = Path(value).exists()
            add(name, OK if exists else WARN, f"{value} ({'exists' if exists else 'MISSING'})")


def section_paths(workdir: Path, quick: bool) -> None:
    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        REPORT.add("paths", Check(name, status, detail, hint))

    try:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "sub"
            probe.mkdir()
            (probe / "file.txt").write_text("ok", encoding="utf-8")
        add("temp directory writable", OK, str(Path(tempfile.gettempdir())))
    except Exception as exc:  # noqa: BLE001
        add(
            "temp directory writable",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "TemporaryDirectory() cannot be written into. Under a restricted (sandboxed) token, "
            "mkdtemp() creates a 0o700 DACL that only names the user SID; a sitecustomize that "
            "creates temp dirs with 0o777 works around it (see docs/host-notes.md).",
        )

    enabled = _long_paths_enabled()
    if enabled is None:
        add("long paths (>260 chars)", SKIP, "not applicable")
    else:
        long_dir = workdir / ("nested/" + "/".join(["segment-0123456789"] * 12))
        try:
            long_dir.mkdir(parents=True, exist_ok=True)
            (long_dir / "file.txt").write_text("ok", encoding="utf-8")
            deep_ok = True
        except OSError:
            deep_ok = False
        add(
            "long paths (>260 chars)",
            OK if deep_ok else WARN,
            f"LongPathsEnabled={enabled}, wrote a {len(str(long_dir))}-char path: {deep_ok}",
            ""
            if deep_ok
            else "Long repo ids/episode names can exceed 260 chars. Enable LongPathsEnabled or keep "
            "HF_LEROBOT_HOME shallow (C:\\lerobot-data).",
        )

    non_ascii = workdir / "中文目录 带空格" / "数据集"
    non_ascii.mkdir(parents=True, exist_ok=True)
    if quick:
        add("non-ASCII video round trip", SKIP, "--quick")
        add("non-ASCII dataset round trip", SKIP, "--quick")
        return

    try:
        import numpy as np
        from PIL import Image

        from lerobot.datasets.video_utils import decode_video_frames, encode_video_frames

        frames_dir = non_ascii / "frames"
        frames_dir.mkdir(exist_ok=True)
        for index in range(4):
            Image.fromarray(np.full((48, 64, 3), index * 60, dtype=np.uint8)).save(
                frames_dir / f"frame-{index:06d}.png"
            )
        video = non_ascii / "clip.mp4"
        video.unlink(missing_ok=True)
        encode_video_frames(frames_dir, video, fps=30)
        frames = decode_video_frames(video, [0.0, 0.05], tolerance_s=0.2)
        add("non-ASCII video round trip", OK, f"encode+decode -> {tuple(frames.shape)}")
    except Exception as exc:  # noqa: BLE001
        add(
            "non-ASCII video round trip",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "Video encode/decode fails under a non-ASCII path. Keep HF_LEROBOT_HOME ASCII-only until "
            "this is fixed, and report it with this report attached.",
        )

    try:
        import numpy as np

        import lerobot.datasets.utils as dataset_utils
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        root = non_ascii / "dataset"
        shutil.rmtree(root, ignore_errors=True)
        features = {
            "observation.state": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
            "action": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
            "observation.images.top": {
                "dtype": "image",
                "shape": (48, 64, 3),
                "names": ["height", "width", "channels"],
            },
        }
        dataset = LeRobotDataset.create(repo_id="dummy/repo", features=features, fps=30, root=root)
        for _ in range(3):
            dataset.add_frame(
                {
                    "observation.state": np.zeros(2, dtype=np.float32),
                    "action": np.ones(2, dtype=np.float32),
                    "observation.images.top": np.zeros((48, 64, 3), dtype=np.uint8),
                    "task": "diagnose",
                }
            )
        dataset.save_episode()
        dataset.finalize()
        original = dataset_utils.get_safe_version
        dataset_utils.get_safe_version = lambda repo_id, revision, *a, **k: revision
        try:
            reloaded = LeRobotDataset("dummy/repo", root=root)
            item = reloaded[0]
        finally:
            dataset_utils.get_safe_version = original
        add(
            "non-ASCII dataset round trip",
            OK,
            f"{len(reloaded)} frames, image {tuple(item['observation.images.top'].shape)}",
        )
    except Exception as exc:  # noqa: BLE001
        add(
            "non-ASCII dataset round trip",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "Dataset write/read fails under a non-ASCII path; point HF_LEROBOT_HOME at an ASCII path.",
        )


def section_timing() -> None:
    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        REPORT.add("timing", Check(name, status, detail, hint))

    mean, worst = _sleep_overshoot(1.0)
    add(
        "sleep(1 ms) quantisation",
        OK if worst < 5 else WARN,
        f"overshoot mean {mean:.2f} ms, worst {worst:.2f} ms",
        "" if worst < 5 else "Old timer resolution behaviour: call timeBeginPeriod(1) for the process.",
    )

    if IS_WINDOWS:
        import ctypes

        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
        try:
            mean, worst = _sleep_overshoot(33.3)
        finally:
            winmm.timeEndPeriod(1)
        add(
            "timer resolution 1 ms",
            OK,
            f"sleep(33.3 ms) overshoot mean {mean:.2f} ms, worst {worst:.2f} ms",
        )

    try:
        from lerobot.utils.robot_utils import precise_sleep

        for hz in (30, 60):
            period = 1 / hz
            overshoots = []
            for _ in range(20):
                start = time.perf_counter()
                precise_sleep(period)
                overshoots.append((time.perf_counter() - start - period) * 1000)
            mean = statistics.mean(overshoots)
            add(
                f"precise_sleep at {hz} Hz",
                OK if abs(mean) < period * 100 else WARN,
                f"mean overshoot {mean:.2f} ms of a {period * 1000:.1f} ms budget, "
                f"worst {max(overshoots):.2f} ms",
            )
    except ImportError:
        add("precise_sleep", SKIP, "lerobot not importable in this interpreter")

    try:
        import torch

        size = 1200
        matrix_a = torch.randn(size, size)
        matrix_b = torch.randn(size, size)
        results = {}
        for threads in (1, os.cpu_count() or 1):
            torch.set_num_threads(threads)
            matrix_a @ matrix_b
            start = time.perf_counter()
            for _ in range(3):
                matrix_a @ matrix_b
            results[threads] = (time.perf_counter() - start) / 3 * 1000
        single = results[1]
        best = min(results.values())
        add(
            "torch thread scaling",
            OK if single / best > 1.5 else WARN,
            ", ".join(f"{threads} threads: {ms:.0f} ms" for threads, ms in results.items())
            + f" (speedup {single / best:.1f}x)",
            ""
            if single / best > 1.5
            else "torch is not using the cores it was given; check OMP_NUM_THREADS/MKL_NUM_THREADS.",
        )
    except ImportError:
        add("torch thread scaling", SKIP, "torch not installed")


def section_processes() -> None:
    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        REPORT.add("processes", Check(name, status, detail, hint))

    try:
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        started = time.perf_counter()
        processes = [ctx.Process(target=_spawn_noop) for _ in range(3)]
        for process in processes:
            process.start()
        for process in processes:
            process.join()
        add("spawn() empty process", OK, f"{(time.perf_counter() - started) / 3 * 1000:.0f} ms each")
    except Exception as exc:  # noqa: BLE001
        add("spawn() empty process", SKIP, f"{type(exc).__name__}: {exc}")

    for module, label in (("torch", "torch"), ("av", "PyAV")):
        try:
            seconds, ok = _spawned_import_cost(module)
            add(
                f"spawn() + import {label}",
                OK if ok else WARN,
                f"{seconds:.2f} s",
                "" if ok else f"the child could not import {label}; workers would fail at start-up",
            )
        except Exception as exc:  # noqa: BLE001
            add(f"spawn() + import {label}", SKIP, f"{type(exc).__name__}: {exc}")


def section_video(workdir: Path, quick: bool) -> None:
    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        REPORT.add("video", Check(name, status, detail, hint))

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        add(
            "ffmpeg CLI",
            FAIL,
            "not on PATH",
            "Dataset video encoding needs ffmpeg.exe. Install a build and add its bin/ to PATH.",
        )
    else:
        code, output = _run([ffmpeg, "-version"])
        first_line = output.splitlines()[0] if output else "no output"
        add("ffmpeg CLI", OK if code == 0 else WARN, f"{ffmpeg} — {first_line}")

    shared_dirs: list[str] = []
    try:
        from lerobot.utils.import_utils import prepare_torchcodec_ffmpeg_dlls

        shared_dirs = prepare_torchcodec_ffmpeg_dlls()
    except ImportError:
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if entry and list(Path(entry).glob("avcodec-*.dll")):
                shared_dirs.append(entry)
    add(
        "FFmpeg shared DLLs (torchcodec)",
        OK if shared_dirs else WARN,
        ", ".join(shared_dirs) if shared_dirs else "none found",
        "torchcodec's native library links against avcodec-*.dll. A static ffmpeg build (only .exe "
        "files) is not enough: install a *shared* build (BtbN ffmpeg-n7.1-win64-gpl-shared) and "
        "point LEROBOT_FFMPEG_DLL_DIR at its bin/.",
    )

    try:
        from lerobot.utils.import_utils import get_safe_default_video_backend

        backend = get_safe_default_video_backend()
        add(
            "video decode backend",
            OK if backend == "torchcodec" else WARN,
            backend,
            "" if backend == "torchcodec" else "Falling back to PyAV: correct but slower.",
        )
    except ImportError:
        add("video decode backend", SKIP, "lerobot not importable")

    if quick:
        add("encode/decode round trip", SKIP, "--quick")
        return
    try:
        import numpy as np
        from PIL import Image

        from lerobot.datasets.video_utils import decode_video_frames, encode_video_frames

        frames_dir = workdir / "video_probe" / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        for index in range(6):
            array = np.random.default_rng(index).integers(0, 255, (48, 64, 3), dtype=np.uint8)
            Image.fromarray(array).save(frames_dir / f"frame-{index:06d}.png")
        video = workdir / "video_probe" / "clip.mp4"
        video.unlink(missing_ok=True)
        started = time.perf_counter()
        encode_video_frames(frames_dir, video, fps=30)
        encode_s = time.perf_counter() - started
        frames = decode_video_frames(video, [0.0, 0.05], tolerance_s=0.2)
        add(
            "encode/decode round trip",
            OK,
            f"6 frames encoded in {encode_s:.2f}s, decoded {tuple(frames.shape)}",
        )
    except Exception as exc:  # noqa: BLE001
        add("encode/decode round trip", FAIL, f"{type(exc).__name__}: {exc}")


def _serial_is_openable(device: str) -> bool:
    try:
        import serial

        handle = serial.Serial(device, timeout=0.1)
        handle.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def section_serial() -> None:
    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        REPORT.add("serial", Check(name, status, detail, hint))

    try:
        from serial.tools import list_ports
    except ImportError:
        add("pyserial", SKIP, "not installed (pip install 'lerobot[hardware]')")
        return

    ports = list(list_ports.comports())
    if not ports:
        add(
            "serial ports",
            SKIP,
            "none",
            "No COM port at all: the USB-serial adapter is not enumerated. Install the CH340/CP210x "
            "driver, try a data cable, and check Device Manager for a warning triangle.",
        )
        return

    def is_usb(port) -> bool:
        hwid = (port.hwid or "").upper()
        return "USB" in hwid and "BTHENUM" not in hwid

    for port in sorted(ports, key=lambda item: not is_usb(item)):
        kind = "USB" if is_usb(port) else "non-USB"
        opened = _serial_is_openable(port.device)
        add(
            f"port {port.device}",
            OK if kind == "USB" else WARN,
            f"{port.description or 'unknown'} ({kind}, {'opens' if opened else 'will not open'})",
            ""
            if kind == "USB"
            else "Bluetooth/virtual port, not a motor bus. It also flaps in and out of existence, so "
            "ignore it in device lists.",
        )
        if kind == "USB" and not opened:
            add(
                f"port {port.device} cannot be opened",
                WARN,
                "exclusive open failed",
                "Another program holds the port (Arduino IDE, a servo tool, a stale lerobot process) "
                "or the driver is broken. Close them, unplug/replug, then retry.",
            )

    if not any(is_usb(port) for port in ports):
        add(
            "USB serial adapter",
            WARN,
            "none detected",
            "Only Bluetooth/virtual ports are present. Plug the arm's USB cable in and install the "
            "CH340 driver (Device Manager -> Ports).",
        )


def section_cameras(probe: bool) -> None:
    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        REPORT.add("cameras", Check(name, status, detail, hint))

    try:
        import cv2
    except ImportError:
        add("opencv", SKIP, "not installed")
        return

    opencvcam = None
    try:
        from lerobot.cameras.opencv.camera_opencv import OpenCVCamera

        opencvcam = OpenCVCamera
    except ImportError:
        pass

    started = time.perf_counter()
    if opencvcam is not None:
        cameras = opencvcam.find_cameras()
    else:  # generic fallback without lerobot
        cameras = []
        for index in range(10):
            capture = cv2.VideoCapture(index)
            if capture.isOpened():
                cameras.append(
                    {
                        "id": index,
                        "backend_api": capture.getBackendName(),
                        "default_stream_profile": {
                            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                            "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                            "fps": capture.get(cv2.CAP_PROP_FPS),
                            "fourcc": "",
                        },
                    }
                )
            capture.release()
    scan_s = time.perf_counter() - started

    if not cameras:
        add(
            "camera enumeration",
            WARN,
            f"none found in {scan_s:.1f}s",
            "No UVC camera enumerated. Close Teams/Zoom/Camera app (they hold the device "
            "exclusively), allow desktop apps in Settings > Privacy > Camera, check Device Manager.",
        )
        return

    for camera in cameras:
        profile = camera.get("default_stream_profile", {})
        add(
            f"camera {camera['id']}",
            OK,
            f"{camera.get('backend_api', '?')} {profile.get('width')}x{profile.get('height')} "
            f"@{profile.get('fps')} fourcc={profile.get('fourcc')}",
        )

    if probe:
        for camera in cameras:
            _probe_camera_modes(camera["id"], add)


def _probe_camera_modes(index: Any, add: Callable[..., None]) -> None:
    """Try the modes people actually use and report which ones delivered frames."""
    import cv2

    modes: list[tuple[str, int]] = [("MJPG", cv2.CAP_DSHOW), ("YUYV", cv2.CAP_DSHOW), ("default", 0)]
    sizes = [(640, 480), (1280, 720), (1920, 1080)]
    opened: list[str] = []
    for fourcc_name, backend in modes:
        for width, height in sizes:
            for fps in (30, 60):
                capture = cv2.VideoCapture(index, backend) if backend else cv2.VideoCapture(index)
                if not capture.isOpened():
                    capture.release()
                    continue
                if fourcc_name != "default":
                    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc_name))
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                capture.set(cv2.CAP_PROP_FPS, fps)
                got = 0
                for _ in range(3):
                    ok, _frame = capture.read()
                    got += int(ok)
                actual = (
                    int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    round(capture.get(cv2.CAP_PROP_FPS), 1),
                )
                capture.release()
                if got >= 2:
                    opened.append(
                        f"{fourcc_name} {width}x{height}@{fps} -> {actual[0]}x{actual[1]}@{actual[2]}"
                    )
    add(
        f"camera {index} modes",
        OK if opened else WARN,
        "; ".join(opened) if opened else "no probed mode produced frames",
        ""
        if opened
        else "The camera opens but no standard mode delivered frames; try a different USB port "
        "(USB 2 vs 3) or a powered hub.",
    )


# ── entry point ────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--workdir", type=Path, default=None, help="where probe files are written")
    parser.add_argument("--md", type=Path, default=None, help="write a markdown report here")
    parser.add_argument("--json", type=Path, default=None, help="write raw results here")
    parser.add_argument("--quick", action="store_true", help="skip the slow round-trip checks")
    parser.add_argument("--camera-probe", action="store_true", help="probe every camera mode (slow)")
    args = parser.parse_args()

    if not IS_WINDOWS:
        print("This script targets Windows; the sections that apply will still run.", file=sys.stderr)

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="lerobot-diagnose-"))
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(workdir / "hf"))
    os.environ.setdefault("HF_LEROBOT_HOME", str(workdir / "hf" / "lerobot"))
    # The environment section reports whether these exist; create what we just pointed at.
    Path(os.environ["HF_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["HF_LEROBOT_HOME"]).mkdir(parents=True, exist_ok=True)
    # Hugging Face `datasets` draws progress bars on stdout, which reads as noise in a report.
    os.environ.setdefault("HF_DATASETS_DISABLE_PROGRESS_BARS", "1")

    print(f"workdir: {workdir}")
    run_section("environment", section_environment)
    run_section("paths", lambda: section_paths(workdir, args.quick))
    run_section("timing", section_timing)
    run_section("processes", section_processes)
    run_section("video", lambda: section_video(workdir, args.quick))
    run_section("serial", section_serial)
    run_section("cameras", lambda: section_cameras(args.camera_probe))

    REPORT.print_console()

    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(REPORT.to_markdown(), encoding="utf-8")
        print(f"\nmarkdown report: {args.md}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(REPORT.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"json report: {args.json}")

    return 1 if REPORT.counts()[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
