# Host quirks that cost time (not LeRobot bugs)

These are properties of one particular Windows machine this work was developed on. They are
recorded because they are easy to misread as "LeRobot does not install on Windows", and each
one has a workaround that applies to any similarly locked-down host. A normal Windows 11
account with admin rights will not hit most of them.

## 1. Schannel is broken → every HTTPS client except Python fails

Symptom:

```
curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030e)
git: fatal: unable to access 'https://github.com/...': schannel: AcquireCredentialsHandle failed
Invoke-WebRequest: 基础连接已经关闭: 接收时发生错误
```

The Windows certificate stack (`schannel`) cannot acquire credentials, so `curl.exe`,
`Invoke-WebRequest`, `winget`-style downloads and `git` with its default backend all fail,
while Python's bundled OpenSSL works fine.

Workarounds used here:

```powershell
# git: use the OpenSSL backend (works for clone/fetch/push)
git -c http.sslBackend=openssl clone https://github.com/huggingface/lerobot.git

# downloads: use Python instead of curl/IWR
python -c "import sys,urllib.request;urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" <url> <dest>
```

`setup-lerobot-windows.ps1` tries `Invoke-WebRequest` first and falls back to the Python
one-liner for exactly this case.

## 2. MSI installers are refused (exit code 5)

Running `python-3.12.x-amd64.exe /quiet InstallAllUsers=0 ...` exits with `5` (access denied)
because the Windows Installer writes its cache to `C:\Windows\Installer` and touches the
registry. Instead of fighting it, install CPython through `uv`, which unpacks a standalone
build into a directory you own:

```powershell
$env:UV_PYTHON_INSTALL_DIR = "D:\lerobot\pythons"
uv python install 3.12 --no-bin
uv venv --python 3.12 .venv
```

No administrator rights, no system Python touched, and `uv python list` shows what is where.

## 3. `tempfile.mkdtemp()` directories are unwritable under a restricted token

Symptom: every `TemporaryDirectory()` — setuptools build directories, PyTorch/Timm scratch
dirs — fails with

```
PermissionError: [WinError 5] 拒绝访问。: '...\tmpXXXXXXXX\sub'
```

Cause: `mkdtemp` creates the directory with mode `0o700`, which on Windows leaves a DACL that
names only the owning user SID. When the process runs under a restricted (sandboxed) token,
that SID is present deny-only, so the process cannot write into the directory it just made.
`os.mkdir(path, 0o777)` produces a DACL that also names `Users` and does work.

Workaround (only for such sandboxes): a `sitecustomize.py` in the venv's `site-packages` that
re-implements `tempfile.mkdtemp` with `0o777`. It must patch before anything creates a temp
dir, which is why it lives in `sitecustomize` rather than in a script:

```python
# .venv/Lib/site-packages/sitecustomize.py
import os as _os
import tempfile as _tempfile
from tempfile import _RandomNameSequence

def _mkdtemp(suffix=None, prefix=None, dir=None):
    prefix, suffix, dir, output_type = _tempfile._sanitize_params(prefix, suffix, dir)
    names = _RandomNameSequence()
    while True:
        name = _os.path.join(dir, prefix + next(names) + suffix)
        try:
            _os.mkdir(name, 0o777)
            return output_type(name)
        except FileExistsError:
            continue

if not getattr(_tempfile, "_dsh_sandbox_patched", False):
    _tempfile.mkdtemp = _mkdtemp
    _tempfile._dsh_sandbox_patched = True
```

Side effect worth knowing: `pytest`'s `tmp_path` fixture creates its directories the same way,
so the two `tmp_path`-based tests in `tests/utils/test_replay_buffer.py` fail on such a host
even with the workaround. They are not Windows failures and pass on a normal account.

## 4. Two Bluetooth COM ports looked like a robot

A machine with paired Bluetooth devices exposes `COM4`/`COM5` (`hwid` starting with
`BTHENUM\...`, description "标准串行 over Bluetooth link"). `lerobot-find-port` used to list
and diff them, which produced "more than one port was found" even when no arm was attached.
That is what motivated the USB-only diff in the `find-port` fix; the helper now prints
`(non-USB)` next to such ports.

## 5. No GPU

`torch.cuda.is_available()` is `False` and there is no `nvidia-smi`. Everything in this repo
was therefore verified on CPU: recording, replay, dataset reading, video encoding/decoding and
`lerobot-info`. Training was only checked at the CLI/config level — CPU training of ACT or
SmolVLA is not a realistic workflow on six cores.
