# LeRobot Windows 适配报告

> 基准：`huggingface/lerobot` 主线 `3f797de`（v0.6.2，2026-10-30）
> 本机：Windows 11（10.0.26300）· 6 核 CPU · 无 NVIDIA 显卡 · Python 3.12.16 · torch 2.11.0+cpu
> 结论：**在本机能装、能跑、能采数据、能读回视频**；机械臂与相机链路已按 SO-101 流程准备好，实机插上即可用（本机当前只插了蓝牙串口，没有 USB 串口适配器与相机，所以这两项走的是"跳过 + 提示"分支）。

---

## 1. 现在到底能跑到什么程度

自检脚本 `verify-lerobot-windows.py` 的结果（11/11）：

| 项目 | 结果 |
| --- | --- |
| Python ≥ 3.12 | OK 3.12.16 (AMD64) |
| torch | OK 2.11.0+cpu，`cuda=False` |
| lerobot 导入 | OK 0.6.2 |
| 硬件栈（pyserial / pynput / rustypot） | OK `sts3215` 已知 |
| 视频解码后端 | OK **torchcodec**（找到 FFmpeg 共享 DLL） |
| ffmpeg CLI | OK n7.1.1（本机另有 8.1.2 静态版在 PATH） |
| 视频编码 + 解码回环 | OK 6 帧 → `(2, 3, 48, 64)` |
| 数据集写入 + 读回（多进程 image writer） | OK 4 帧，图像 `(3, 48, 64)` |
| 串口枚举 | OK 识别出两个**蓝牙** COM 口并标注 non-USB |
| 相机 | SKIP 无相机（扫描 0.3 s） |
| CLI 入口 | OK 9 个 `.exe` |

测试：`tests/cameras` + `tests/utils/test_keyboard_input.py` + `tests/utils/test_replay_buffer.py` + `tests/utils/test_process.py`
→ **99 passed, 7 skipped**（跳过的都是 SIGHUP/SIGQUIT 或 async_read 0 超时的既有跳过项）。

---

## 2. 这台机器上必须先绕开的三个"系统级"坑

它们**不是 LeRobot 的问题**，但会让人以为 LeRobot 装不上，这里记下来免得下次再踩。

1. **Schannel 证书栈坏了**：`curl`、`Invoke-WebRequest`、`git`（默认 schannel 后端）全部报
   `SEC_E_NO_CREDENTIALS (0x8009030e)`。可用的是 Python 自带的 OpenSSL 栈，以及
   `git -c http.sslBackend=openssl`。
2. **MSI 装 Python 被拒**：`python-3.12.x-amd64.exe /quiet` 返回 exit 5（写 `C:\Windows\Installer` 与注册表被挡）。
   改用 **uv 下载 standalone CPython 3.12 到工作目录**，不碰系统、不需要管理员。
3. **`tempfile.mkdtemp` 建的目录写不进去**：沙箱令牌下 mode `0o700` 的目录 ACL 只挂了当前用户，
   而该令牌里用户 SID 是 deny-only → 任何 `TemporaryDirectory()`（setuptools 构建、timm/torch 临时目录）
   都会 `PermissionError: [WinError 5]`。
   处理：在 venv 的 `site-packages/sitecustomize.py` 里把 `mkdtemp` 改成 `0o777`（**只是本沙箱的权宜之计，
   普通 Windows 机器不需要**，删掉该文件即恢复）。

---

## 3. 安装

### 3.1 一键脚本（推荐）

```powershell
# CPU 版（本机这套），默认装 feetech,hardware,dataset,viz
.\setup-lerobot-windows.ps1

# 有 NVIDIA 显卡时
.\setup-lerobot-windows.ps1 -Torch cuda128

# 只想训练/推理，不接硬件
.\setup-lerobot-windows.ps1 -Extras "dataset"
```

脚本做的事：装 uv 到 `<Root>\bin` → 下载 CPython 3.12 到 `<Root>\pythons` → 在仓库里建 `.venv`
→ 装 CPU/CUDA 版 torch+torchvision → `pip install -e .[extras]` → 找 FFmpeg 共享 DLL 并设
`LEROBOT_FFMPEG_DLL_DIR` → 跑 `lerobot-info`。

### 3.2 手动步骤（本次实际走的路径）

```powershell
$env:UV_PYTHON_INSTALL_DIR = "D:\lerobot\pythons"; $env:UV_CACHE_DIR = "D:\lerobot\uv-cache"
.\bin\uv.exe python install 3.12 --no-bin
.\bin\uv.exe venv --python 3.12 .venv
.\bin\uv.exe pip install --python .venv\Scripts\python.exe --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 torchvision==0.26.0
.\bin\uv.exe pip install --python .venv\Scripts\python.exe --no-build-isolation -e ".[feetech,hardware,dataset,viz]"
```

两个要点：

* **`--no-build-isolation`**：本沙箱里隔离构建用的临时目录不可写；普通机器可省略。
* **先装 CPU 版 torch 再装 lerobot**：否则 PyPI 上的 `torch` 是 CUDA 构建（约 2.5 GB），
  没显卡的机器白下。装完 `pip install -e .` 不会再动它（版本已满足 `>=2.7,<2.12`）。

---

## 4. 代码适配清单（11 个文件，484 行改动）

### 4.1 `scripts/lerobot_find_port.py` — COM 口发现（Windows 专属逻辑）

**问题**：原实现把机器上**所有**串口都拿来前后做差集。Windows 上蓝牙串口（`COM4`/`COM5`，
hwid 以 `BTHENUM` 开头）会自己上下线，机械臂插拔一次往往差出好几个口；
而且驱动发布/回收 COM 口是异步的，原来固定 `sleep(0.5)` 经常读不到变化；失败时直接抛
`OSError` 带 traceback，用户只看到"没检测到"。

**改法**：`is_usb_serial_port()` 按 hwid 判定 USB 串口（排除 BTHENUM/RFCOMM/Bluetooth）；
`_candidate_devices()` 只对 USB 串口做差集；`_wait_for_devices_to_change()` 轮询到变化或超时
（Windows 5 s，POSIX 保持 0.5 s 语义）；`describe_port()` 打印 `COM3 — USB-SERIAL CH340 (USB)`；
没有 USB 串口时明确提示"上面的口是蓝牙/虚拟口，先插线并装 CH340/CP210x 驱动"；
失败信息补上 Windows 常见原因（别的程序占着口：Arduino IDE、舵机调试工具、上一次没退干净的 lerobot）。

### 4.2 `cameras/opencv/camera_opencv.py` — 相机枚举与 4 通道帧

* `_probe_camera()`：把"打开一个目标并读元数据"抽出来，`release()` 用 `finally` 保证。
* `_find_cameras_windows()`：Windows 没有 `/dev/video*`，只能按索引试。找到第一台后连续
  `_WINDOWS_MAX_CONSECUTIVE_MISSES=10` 个空索引就停（原来死扫 60 个）；元数据里带上
  `backend`（`backend_api` 实测值）。
* **4 通道帧**：`_postprocess_image` 遇到 `channels=4` 直接抛 `RuntimeError`。Windows 上这很常见
  （Media Foundation 对部分 UVC 相机给 BGRA；OpenCV 4.13 的 CV_IMAGES 后端读 RGBA PNG 也是 4 通道），
  这是本仓库自带相机测试在 Windows 上全红的原因。现在先把 alpha 丢掉（`COLOR_BGRA2BGR`）再走后续流程。

### 4.3 `scripts/lerobot_find_cameras.py`

发现阶段测到的 backend 透传回 `OpenCVCameraConfig(backend=...)`，避免"发现时能开、采集时打不开"；
没有相机时补一条 Windows 提示（被 Teams/Zoom/Camera 应用占用、设备管理器报错、隐私设置里禁止桌面应用访问相机）。

### 4.4 `cameras/realsense/camera_realsense.py`

没装 `pyrealsense2` 时 `find_cameras()` 会 `rs.context()` 崩成
`'NoneType' object has no attribute 'context'`，把"未安装"这条真实信息盖掉了。加 `require_package` 前置检查。

### 4.5 `utils/import_utils.py` + `datasets/video_utils.py` + `streaming/episode_cache.py` — torchcodec 的 FFmpeg DLL

**问题**（Windows 上最常见的坑）：torchcodec 的 wheel 里没有 FFmpeg，它加载的
`libtorchcodec_core<N>.dll` 需要**共享版**的 `avcodec-*.dll`；而大家装的 ffmpeg.exe 基本都是**静态**版
（只有 exe，没有 DLL），于是 `import torchcodec` 报
`Could not load this library: ...libtorchcodec_core8.dll`，整条数据集管线悄悄退化到较慢的 PyAV。

**改法**：新增 `prepare_torchcodec_ffmpeg_dlls()`（`import_utils.py`）：按
`LEROBOT_FFMPEG_DLL_DIR` / `FFMPEG_DLL_DIR` / `FFMPEG_BIN` → PATH 上 ffmpeg 所在目录 →
常见安装位置（`%PROGRAMFILES%\ffmpeg`、`C:\ffmpeg`、`~\ffmpeg`，含下一层 `bin`）顺序找含
`avcodec-*.dll` 的目录，用 `os.add_dll_directory()` 注册（句柄常驻，否则 GC 掉就失效了）并前置到 PATH；
在所有 `from torchcodec.decoders import VideoDecoder` 之前调用。本机验证：注册后后端从 `pyav` 变成
`torchcodec`，编解码回环正常。

> 实测：BtbN 的 `ffmpeg-n7.1.x-win64-gpl-shared` 可用（avcodec-61 → core7）；
> master 分支的共享包是 avcodec-**63**，torchcodec 0.11 还不认。

### 4.6 `utils/keyboard_input.py` — Windows 控制台键盘后端

`TerminalKeyListener` 原来只有 termios 一条路，Windows 上 `start()` 直接 no-op：一旦 `pynput`
不可用（服务会话、禁了全局钩子、没装 pynput），录制时的 →/←/Esc、n/r/q 就全静默失效。
现在同一个类内部按平台分流：Windows 走 `msvcrt.kbhit()/getwch()` 轮询（无需改终端状态），
并解析 Windows 方向键的 `\x00`/`\xe0` + `H/P/K/M` 扫描码；POSIX 仍走 termios cbreak。

### 4.7 `rl/buffer.py` — "CPU 0%，进程卡死"（见第 6 节）

* `_maybe_compile()`：Windows 上没有 MSVC `cl.exe` 时**不** `torch.compile`，直接用 eager 版图像增强。
* 预取线程的异常不再被吞：原代码 `except Exception: shutdown_event.set()`，消费者随后**永久阻塞**在
  `queue.get(block=True)` 上——没 CPU、没日志、没退出。现在异常存进 `failures` 由消费者 `raise`，
  消费者用带超时的 `get` 轮询，线程名也起好了（`ReplayBufferProducer`）。

### 4.8 测试套件的 Windows 修正（`tests/`）

* `tests/utils/test_process.py`
  - `signal.SIGHUP`/`SIGQUIT` 直接写在 `pytest.param(...)` 与 autouse fixture 里，Windows 上
    **collection 阶段**就 `AttributeError`（根本轮不到 `skipif`）。改成 `getattr(signal, name)`。
  - `os.kill(os.getpid(), SIGINT)` 在 Windows 上不是"给自己发信号"：除
    `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` 外走 `TerminateProcess`，会把 pytest 自己杀掉（本沙箱里则是 permission denied）。
    新增 `_send_signal_to_self()`：Windows 用 `signal.raise_signal()`（实测能进 handler、进程不死），POSIX 仍用 `os.kill`。
* `tests/datasets/test_video_decoder_cache.py`：`pytest.importorskip("torchcodec")` 只吞 `ImportError`，
  而"装上了但 DLL 加载失败"抛的是 `OSError/RuntimeError` → 整个 collection error。
  改为先 `prepare_torchcodec_ffmpeg_dlls()` 再 try/except `pytest.skip(allow_module_level=True)`。

---

## 5. 接真实机械臂 + 相机（SO-101，Windows 版命令）

```powershell
# 0) 先确认环境
.venv\Scripts\python.exe verify-lerobot-windows.py

# 1) 找口：拔掉 USB → 回车 → 插回，工具会打印 USB 串口（蓝牙口会被标注 non-USB 忽略）
lerobot-find-port
#    拿到例如 COM5

# 2) 写舵机 ID / 波特率（只做一次）
lerobot-setup-motors --robot.type=so101_follower --robot.port=COM5

# 3) 标定（主臂同理，端口换成主臂那个）
lerobot-calibrate --robot.type=so101_follower --robot.port=COM5
lerobot-calibrate --teleop.type=so101_leader --teleop.port=COM6

# 4) 遥操作自检（先别录数据）
lerobot-teleoperate --robot.type=so101_follower --robot.port=COM5 --teleop.type=so101_leader --teleop.port=COM6

# 5) 录一批数据（相机用索引 0；高帧率建议显式给 backend 与 MJPG）
lerobot-record `
  --robot.type=so101_follower --robot.port=COM5 `
  --robot.cameras="{ top: {type: opencv, index_or_path: 0, width: 640, height: 480, fps: 30, backend: dshow, fourcc: MJPG} }" `
  --teleop.type=so101_leader --teleop.port=COM6 `
  --dataset.repo_id=你的账号/so101_test --dataset.num_episodes=3 --dataset.single_task="pick and place"

# 6) 回放 / 可视化 / 训练
lerobot-replay --robot.type=so101_follower --robot.port=COM5 --dataset.repo_id=你的账号/so101_test --dataset.episode=0
lerobot-dataset-viz --dataset.repo_id=你的账号/so101_test
lerobot-train --dataset.repo_id=你的账号/so101_test --policy.type=act --output_dir=outputs/train/act_so101
```

Windows 实机注意点：

* **驱动**：CH340（多数 SO-101 套装里的 USB 转串口）需要装驱动；设备管理器里能看到 `COMx` 才算通。
  插上不出现 → 换线/换口，别用只能充电的线。
* **独占**：串口是独占资源。Arduino IDE 串口监视器、厂商上位机、上一次没退干净的进程都会占住口，
  表现为 `PermissionError` / 打不开。
* **相机**：Windows 上 Media Foundation（OpenCV 默认）容易首帧慢、掉帧；`backend: dshow` + `fourcc: MJPG`
  通常能稳定 30 fps。相机被 Teams/Zoom 占着时枚举直接看不到。
* **路径**：数据默认落在 `%USERPROFILE%\.cache\huggingface\lerobot`。想放 D 盘就设
  `HF_LEROBOT_HOME=D:\lerobot-data`（本仓库会读这个环境变量）。
* **端口号会变**：同一条线插不同 USB 口，COM 号可能变，所以每次先 `lerobot-find-port` 或用设备管理器的口。

---

## 6. 关于"CPU 没跑满"

先说结论：**你看到的那次不是"没跑满"，而是根本没在跑**——进程卡在死锁上，CPU 自然是 0%。

### 6.1 这次的真实原因（已修）

`tests/utils/test_replay_buffer.py` 的异步预取用例挂在 `queue.get(block=True)`：

1. 生产线程里 `self.sample()` → 图像增强走 `torch.compile` → Windows 上需要 MSVC `cl.exe` 编译内核；
2. 本机没装 VS Build Tools → 抛 `InductorError: Compiler: cl is not found`；
3. 那段 `except Exception: shutdown_event.set()` 把异常吞了，生产线程退出；
4. 消费者仍阻塞在无超时的 `queue.get()` 上 → 永久等待，0% CPU，无日志。

修法见 4.7：Windows 无 `cl.exe` 就不 compile（eager 兜底），并且把预取线程的异常抛回消费者。
修完该用例组 17 项全过。

### 6.2 真正跑起来时 CPU 是用得上的

本机 6 个逻辑核，`torch.get_num_threads()` 默认就是 6。实测 2000×2000 矩阵乘：

| torch 线程数 | 5 次 matmul 耗时 | 吞吐 |
| --- | --- | --- |
| 1 | 1236 ms | 64.7 GFLOPS |
| 3 | 750 ms | 106.7 GFLOPS |
| 6 | 399 ms | 200.3 GFLOPS |

即 1→6 线程加速 3.1×，所以"线程被限制住"这个问题在这台机器上不存在。

### 6.3 但 CPU 想"吃满"要看不平衡在哪

LeRobot 在这台机器上主要跑三件事，瓶颈完全不同：

* **`lerobot-record`**：瓶颈常在相机/串口 I/O，不在算力。CPU 只用几个线程是正常的。
  想压满：`--dataset.num_image_writer_processes=2`、`--dataset.num_image_writer_threads_per_camera=2`，
  视频编码线程 `--dataset.encoder_threads`（SVT-AV1 默认就用多线程，本机日志里是 `Level of Parallelism: 4`）。
* **`lerobot-train`（CPU）**：`--num_workers` 提 dataloader 并行度、`--batch_size` 提单步算力密度；
  但 6 核纯 CPU 训 ACT/SmolVLA 这类策略非常慢，**建议上 GPU**，这是这台机器的硬限制（`cuda=False`）。
* **视频编解码/数据集搬运**：`torchcodec` 比 PyAV 快不少，所以第 4.5 条修好 DLL 之后这条链路也顺了。

观测手段：任务管理器看总占用；或
`Get-Counter '\Processor(_Total)\% Processor Time'`；或
`.venv\Scripts\python.exe -c "import psutil;print(psutil.cpu_percent(1))"`。

---

## 7. Windows 支持现状（实测矩阵）

把"能不能用"拆开看，而不是一个笼统的"支持/不支持"。这张表是这台机器上的实测结论，也是 `scripts/diagnose-windows.py` 每次会重新测一遍的东西。

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 安装（CPU / CUDA torch + extras） | ✅ 已验证 | 一键脚本，不需要 MSVC、不需要管理员 |
| `lerobot-info` / `find-port` / `find-cameras` / `calibrate` / `record` / `replay` / `dataset-viz` | ✅ 已验证（无硬件） | 机械臂/相机链路走的是"未接入"分支，命令与参数解析全通 |
| 数据集写入 + 读回（多进程 image writer） | ✅ 已验证 | 4 帧往返，图像 `(3,48,64)` |
| 视频编解码 | ✅ 已验证 | 软件编码 libsvtav1；解码 torchcodec（需 FFmpeg **共享** DLL）或 PyAV 回退 |
| 中文 / 空格 / 长路径 | ✅ 已验证 | 超过 260 字符需 `LongPathsEnabled`，否则数据目录要浅 |
| UVC 相机（OpenCV） | ⚠️ 可用但受限 | `backend: dshow` + `fourcc: MJPG` 才稳 30 fps；**没有曝光/增益控制**（Windows 上 OpenCV 不暴露 UVC 控制面），光照变化会毁数据 |
| Intel RealSense | ⚠️ 有 wheel，未验证 | `pip install 'lerobot[intelrealsense]'`，本机无设备 |
| 训练 | ⚠️ 仅 CPU | 本机无 GPU（`cuda=False`）；换机器用 `-Torch cuda128` |
| 硬件编码（NVENC/QSV/AMF） | ❌ 未接入 | LeRobot 用 libsvtav1 软编；接硬件编码会牺牲跨平台可复现性，应作为 opt-in |
| 无人值守 / 服务化（无桌面会话） | ❌ 不支持 | 全局键盘钩子与 msvcrt 兜底都依赖交互桌面会话 |
| `torch.compile` 加速 | ❌ 需要 MSVC | 没装 VS Build Tools 时 RL buffer 自动走 eager（已修，但拿不到加速收益） |

### 怎么知道自己这台机器行不行

```powershell
.venv\Scripts\python.exe scripts\diagnose-windows.py --md my-report.md
```

六个分区、约 30 项检查，全部不需要硬件：环境（构建号/代码页/长路径/临时目录/线程数）、路径（中文+空格目录下的数据集与视频往返）、时序（睡眠量化、30/60 Hz 的 `precise_sleep` 抖动）、进程（spawn 成本、子进程 import torch/PyAV 的成本）、视频（ffmpeg CLI、共享 DLL、torchcodec 后端、编解码往返）、串口（USB/蓝牙/虚拟口分类 + 能否打开）、相机（枚举 + 后端，`--camera-probe` 再加一张"真正出帧的模式"矩阵）。

每个 `WARN`/`FAIL` 都带修复建议；`--md` 出 Markdown 报告、`--json` 出原始结果，提 issue 时直接贴。

[`docs/baseline-windows11.md`](https://github.com/wjh-ux/lerobot-windows/blob/main/docs/baseline-windows11.md) 是这台机器（无硬件）的基线报告，可用来对照。

## 8. 已知限制 / 没做的事

1. **没有 NVIDIA 显卡**：`cuda=False`，只能 CPU 训练/推理。要训练就换机器或接 eGPU。
2. **没接真实机械臂与相机**：本机只有两个蓝牙 COM 口、无相机。串口/相机实机链路需要插上设备后再跑一次
   `verify-lerobot-windows.py`（它会从 SKIP 变成 OK）以及第 5 节的命令。
3. **torchcodec 需要 FFmpeg 7.x/8.x 的共享 DLL**：装的是静态 ffmpeg 的话会自动退化到 PyAV（能用，慢一些）。
   脚本会把找到的目录写进 `LEROBOT_FFMPEG_DLL_DIR`，没找到会给出下载建议。
4. **本沙箱下 2 个用例报权限**：`test_replay_buffer.py::test_to_lerobot_dataset` /
   `test_from_lerobot_dataset` 用 pytest 的 `tmp_path`，其临时目录同样是 `0o700` ACL，属于第 2.3 条的沙箱限制，
   普通 Windows 机器上应通过。
5. **没装的 extras**：`placo`（运动学）、`libero`/`robocasa` 等仿真、`intelrealsense`（没有相机）——
   需要时按 `pyproject.toml` 的 extras 单独装。
6. **训练规模**：本次没有跑训练（CPU 上意义不大），`lerobot-train` 只验证了 CLI 与配置解析。

---

## 9. 建议给上游补一个 Windows job

上游 CI 目前 **只有 ubuntu-latest**（`.github/workflows/fast_tests.yml:67` 等），所以上面这些
Windows 专属问题（`signal.SIGHUP`、torchcodec 加载、4 通道帧）一直没人撞上。建议加：

```yaml
  fast_tests_windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv venv --python 3.12
      - run: uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
      - run: uv pip install -e ".[dataset,hardware,test]"
      - run: uv run pytest tests/utils tests/cameras -m "not gpu" -q
```

---

## 10. 交付物

| 文件 | 说明 |
| --- | --- |
| `lerobot-windows.patch` | 11 个文件、484 行改动的 `git diff`（可直接 `git apply`） |
| `setup-lerobot-windows.ps1` | 一键安装（uv + standalone CPython + torch CPU/CUDA + extras + FFmpeg DLL） |
| `verify-lerobot-windows.py` | 11 项无硬件自检 + 数据集/视频回环 |
| `LeRobot-Windows适配说明.md` | 本文 |

已开源发布：

* 独立仓库（英文 README + 本文、脚本、合并补丁）：<https://github.com/wjh-ux/lerobot-windows>
* 上游 PR（均为草稿，人工复核后点 “Ready for review” 再邀请审阅）：
  * [#5177](https://github.com/huggingface/lerobot/pull/5177) `fix(scripts): make lerobot-find-port reliable on Windows`
  * [#5178](https://github.com/huggingface/lerobot/pull/5178) `fix(cameras): enumerate cameras on Windows and accept 4-channel frames`
  * [#5179](https://github.com/huggingface/lerobot/pull/5179) `fix(datasets): find FFmpeg's shared DLLs before importing torchcodec on Windows`
  * [#5180](https://github.com/huggingface/lerobot/pull/5180) `feat(teleoperators): read recording controls from a Windows console`
  * [#5181](https://github.com/huggingface/lerobot/pull/5181) `fix(rl): surface replay-buffer prefetch failures instead of hanging`
  * [#5182](https://github.com/huggingface/lerobot/pull/5182) `test: make the suite collectible and runnable on Windows`
  * [#5183](https://github.com/huggingface/lerobot/pull/5183) `ci: run the utils and camera suites on windows-latest`（栈在 #5178 + #5182 之上，两者合并后会自动缩减为单个 workflow 文件）

实机接线后的验证清单（跑什么、每个 PR 贴什么证据）见仓库里的
`docs/hardware-checklist.md`；本机的环境坑（Schannel、MSI、`mkdtemp`、蓝牙 COM、无 GPU）整理在
`docs/host-notes.md`。

代码改动都在 `C:\Users\Administrator\Documents\deepseek-harness\default-workspace\lerobot-win`
（官方主线 clone，已在 `.venv` 里装好并验证），补丁基于 `HEAD=3f797de`。
