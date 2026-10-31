"""Check that this LeRobot checkout works on Windows, without needing hardware.

Run it with the venv's Python:

    .venv\\Scripts\\python.exe verify-lerobot-windows.py

Every line prints OK / SKIP / FAIL; the exit code is 0 only when nothing FAILed. Hardware
dependent checks (motor bus, cameras) SKIP when nothing is plugged in, which is the
expected state on a workstation.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Keep the check hermetic: LeRobot and Hugging Face default to ~/.cache, which a
# sandboxed or read-only home directory cannot serve.
os.environ.setdefault("HF_HOME", str(_HERE / "_verify_cache" / "hf"))
os.environ.setdefault("HF_LEROBOT_HOME", str(_HERE / "_verify_cache" / "lerobot"))

RESULTS: list[tuple[str, str, str]] = []


def check(name):
    def decorator(fn):
        try:
            detail = fn() or ""
            RESULTS.append((name, "OK", str(detail)))
        except SkipCheck as exc:
            RESULTS.append((name, "SKIP", str(exc)))
        except Exception as exc:  # noqa: BLE001
            RESULTS.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))
            if "-v" in sys.argv:
                traceback.print_exc()
        return fn

    return decorator


class SkipCheck(Exception):
    pass


@check("python >= 3.12")
def _python():
    version = sys.version_info
    if version < (3, 12):
        raise AssertionError(f"LeRobot needs Python >= 3.12, this is {platform.python_version()}")
    return f"{platform.python_version()} ({platform.machine()})"


@check("torch import + device")
def _torch():
    import torch

    cuda = torch.cuda.is_available()
    return f"torch {torch.__version__}, cuda={cuda}"


@check("lerobot import")
def _lerobot():
    import lerobot

    return f"lerobot {lerobot.__version__}"


@check("hardware stack (pyserial, pynput, rustypot)")
def _hardware():
    missing = [name for name in ("serial", "pynput", "rustypot") if importlib.util.find_spec(name) is None]
    if missing:
        raise SkipCheck(f"not installed: {', '.join(missing)} (pip install 'lerobot[feetech,hardware]')")
    import rustypot

    return f"sts3215 known to rustypot: {rustypot.find_model('sts3215') is not None}"


@check("video decoding backend")
def _video_backend():
    from lerobot.utils.import_utils import get_safe_default_video_backend, prepare_torchcodec_ffmpeg_dlls

    registered = prepare_torchcodec_ffmpeg_dlls()
    backend = get_safe_default_video_backend()
    if backend == "torchcodec":
        return f"torchcodec (ffmpeg DLLs: {', '.join(registered) or 'found via PATH'})"
    raise SkipCheck("pyav fallback (torchcodec cannot load FFmpeg shared DLLs; see LEROBOT_FFMPEG_DLL_DIR)")


@check("ffmpeg CLI")
def _ffmpeg():
    import shutil
    import subprocess

    exe = shutil.which("ffmpeg")
    if not exe:
        raise SkipCheck("ffmpeg not on PATH (needed to encode dataset videos)")
    out = subprocess.run([exe, "-version"], capture_output=True, text=True, check=True).stdout
    return out.splitlines()[0]


@check("video encode + decode round trip")
def _video_roundtrip():
    import numpy as np
    from PIL import Image

    from lerobot.datasets.video_utils import decode_video_frames, encode_video_frames

    root = Path(__file__).resolve().parent / "_verify_frames"
    frames_dir = root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        array = np.random.default_rng(index).integers(0, 255, (48, 64, 3), dtype=np.uint8)
        Image.fromarray(array).save(frames_dir / f"frame-{index:06d}.png")
    video = root / "clip.mp4"
    if video.exists():
        video.unlink()
    encode_video_frames(frames_dir, video, fps=30)
    decoded = decode_video_frames(video, [0.0, 0.1], tolerance_s=0.2)
    return f"6 frames -> {tuple(decoded.shape)}"


@check("dataset write + read (image writer processes)")
def _dataset_roundtrip():
    import shutil

    import numpy as np
    import torch

    import lerobot.datasets.lerobot_dataset as lrd
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    root = Path(__file__).resolve().parent / "_verify_dataset"
    if root.exists():
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
    dataset = LeRobotDataset.create(
        repo_id="dummy/repo", features=features, fps=30, root=root, use_videos=True
    )
    for _ in range(4):
        dataset.add_frame(
            {
                "observation.state": np.zeros(2, dtype=np.float32),
                "action": np.ones(2, dtype=np.float32),
                "observation.images.top": np.zeros((48, 64, 3), dtype=np.uint8),
                "task": "verify",
            }
        )
    dataset.save_episode()
    # Episode metadata (and the async image writes) are flushed here; lerobot-record does
    # the same when it stops.
    dataset.finalize()
    # Reading a purely local dataset still resolves a revision through the Hub; the test
    # suite stubs that out, and so do we, to keep this check offline.
    import lerobot.datasets.dataset_metadata as lrdm
    import lerobot.datasets.utils as lru

    stubbed = (lru, "get_safe_version", lru.get_safe_version)
    original_late = getattr(lrdm, "get_safe_version", None)
    lru.get_safe_version = lambda repo_id, revision, *args, **kwargs: revision
    if original_late is not None:
        lrdm.get_safe_version = lru.get_safe_version
    try:
        reloaded = LeRobotDataset("dummy/repo", root=root)
        item = reloaded[0]
    finally:
        lru.get_safe_version = stubbed[2]
        if original_late is not None:
            lrdm.get_safe_version = original_late
    shapes = {key: tuple(value.shape) for key, value in item.items() if isinstance(value, torch.Tensor)}
    return f"{len(reloaded)} frames, first sample {shapes}"


@check("serial ports")
def _serial_ports():
    from lerobot.scripts.lerobot_find_port import _snapshot, describe_port

    infos, devices = _snapshot()
    if not devices:
        raise SkipCheck("no serial ports at all")
    return "; ".join(describe_port(info) for info in infos)


@check("cameras")
def _cameras():
    from lerobot.cameras.opencv.camera_opencv import OpenCVCamera

    start = time.perf_counter()
    cameras = OpenCVCamera.find_cameras()
    elapsed = time.perf_counter() - start
    if not cameras:
        raise SkipCheck(f"no camera attached (scan took {elapsed:.1f}s)")
    return ", ".join(f"#{c['id']} via {c['backend_api']}" for c in cameras)


@check("CLI entry points")
def _cli():
    scripts_dir = Path(sys.executable).parent
    expected = [
        "lerobot-find-port",
        "lerobot-find-cameras",
        "lerobot-setup-motors",
        "lerobot-calibrate",
        "lerobot-teleoperate",
        "lerobot-record",
        "lerobot-replay",
        "lerobot-train",
        "lerobot-info",
    ]
    missing = [name for name in expected if not (scripts_dir / f"{name}.exe").exists()]
    if missing:
        raise AssertionError(f"missing: {missing}")
    return f"{len(expected)} scripts in {scripts_dir}"


def main() -> int:
    for name, status, detail in RESULTS:
        print(f"[{status:4}] {name}: {detail}")
    failures = [row for row in RESULTS if row[1] == "FAIL"]
    print()
    print(f"{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed on {platform.platform()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
