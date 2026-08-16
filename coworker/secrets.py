"""Secret store — one canonical, file-backed store for connector/MCP credentials.

Design (from OpenClaw): secrets **never enter the model's context, prompts, or traces**.
The store holds profiles keyed by `connector[:account]`; values may be literals OR
`${ENV_VAR}` references resolved at read time from the process env / `~/.config/coworker/.env`.

v1 is a `0600` JSON file behind this interface; the interface is what callers depend on, so
a Keychain / age-encrypted backend can swap in later without touching them.

S1 密钥托管 (安全架构级修复): on Windows the store is additionally encrypted at
rest with DPAPI (CryptProtectData, scoped to the current user) — a plaintext
secrets.json on disk was the "密钥落盘" gap (H2/G12). On other platforms it
falls back to `0600` file permissions (macOS/Linux keychains remain a TODO).
The format is versioned so an existing plaintext store migrates transparently.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_IS_WINDOWS = sys.platform == "win32"

# S1: secrets.json 静态加密版本标记 — 文件头写入此标记, _read 据此解密。
_SECRETS_MAGIC = "qunwork-secrets-v1:"
_DPAPI_AVAILABLE = _IS_WINDOWS


def _dpapi_protect(plaintext: bytes) -> bytes:
    """Windows DPAPI CryptProtectData — encrypt for the current user only."""
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    blob_in = DATA_BLOB(len(plaintext), ctypes.cast(
        ctypes.create_string_buffer(plaintext), ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptProtectData failed")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return raw
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    """Windows DPAPI CryptUnprotectData."""
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    blob_in = DATA_BLOB(len(ciphertext), ctypes.cast(
        ctypes.create_string_buffer(ciphertext), ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return raw
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def secrets_at_rest_encrypted() -> bool:
    """当前平台 secrets.json 是否静态加密 (S1 状态展示)。"""
    return _DPAPI_AVAILABLE


def state_dir() -> Path:
    """Where coworker keeps its state — the one cross-platform source of truth.

    Resolution order:
    1. `$COWORKER_STATE_DIR` — explicit override on any OS (used by tests/sidecars).
    2. Windows: `%APPDATA%\\coworker` (e.g. `C:\\Users\\You\\AppData\\Roaming\\coworker`),
       the native per-user app-data location.
    3. macOS / Linux: `~/.config/coworker` (XDG-style, unchanged from prior behavior).
    """
    base = os.environ.get("COWORKER_STATE_DIR")
    if base:
        return Path(base).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "coworker"
    return Path.home() / ".config" / "coworker"


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _restrict_to_user(path: Path, *, is_dir: bool) -> None:
    """Restrict a path so only the current user can access it.

    POSIX expresses this with mode bits (0700 dir / 0600 file). Windows has no such bits —
    `os.chmod` there only toggles the read-only flag, so a 0600 chmod is a silent no-op and
    the file inherits broad ACLs (SYSTEM, Administrators, …). Use an ACL instead: strip
    inherited entries and grant the current user alone. Best-effort on Windows so a transient
    icacls failure never blocks saving a key."""
    if _IS_WINDOWS:
        user = os.environ.get("USERNAME")
        if not user:
            return
        domain = os.environ.get("USERDOMAIN")
        account = f"{domain}\\{user}" if domain else user
        # A directory grant MUST be inheritable — (OI) object-inherit for files, (CI)
        # container-inherit for subdirs — so everything created inside (the SQLite stores,
        # conversations, …) inherits the user's access. Without these flags, /inheritance:r
        # leaves the directory with a non-inheritable ACE and any child file ends up with an
        # empty DACL → sqlite3 "unable to open database file", crashing the server on launch.
        grant = f"{account}:(OI)(CI)F" if is_dir else f"{account}:F"
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
        return
    os.chmod(path, 0o700 if is_dir else 0o600)


def write_private_text(path: str | Path, content: str) -> Path:
    """Atomically write a user-only text file using the SecretStore's OS protections."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _restrict_to_user(target.parent, is_dir=True)
    except OSError:
        pass
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    _restrict_to_user(tmp, is_dir=False)
    os.replace(tmp, target)
    return target


class SecretStore:
    """File-backed secret store. Reads resolve `${VAR}` refs; status never leaks values."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path).expanduser() if path else state_dir() / "secrets.json"
        self._dotenv_path = self.path.parent / ".env"
        self._lock = threading.Lock()

    # -- reads ------------------------------------------------------------------
    def get(self, profile: str) -> Optional[dict[str, Any]]:
        """Return a profile with `${VAR}` refs resolved, or None if absent."""
        data = self._read().get(profile)
        if data is None:
            return None
        return self.resolve(data)

    def resolve(self, value: Any) -> Any:
        """Resolve `${VAR}` refs in a value (recursively) from env + the local `.env`."""
        env = _load_dotenv(self._dotenv_path)

        def _walk(v: Any) -> Any:
            if isinstance(v, str):
                return _REF.sub(
                    lambda m: os.environ.get(m.group(1))
                    or env.get(m.group(1))
                    or m.group(0),
                    v,
                )
            if isinstance(v, dict):
                return {k: _walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [_walk(x) for x in v]
            return v

        return _walk(value)

    def status(self) -> list[dict[str, Any]]:
        """Profile metadata only — **never** the secret values themselves."""
        out: list[dict[str, Any]] = []
        for profile, data in self._read().items():
            data = data if isinstance(data, dict) else {}
            expires = data.get("expires")
            expired = isinstance(expires, (int, float)) and expires < time.time()
            out.append(
                {
                    "profile": profile,
                    "type": data.get("type"),
                    "account": data.get("account_id"),
                    "expired": bool(expired),
                }
            )
        return out

    # -- writes -----------------------------------------------------------------
    def put(self, profile: str, data: dict[str, Any]) -> None:
        with self._lock:
            store = self._read()
            store[profile] = data
            self._write(store)

    def delete(self, profile: str) -> bool:
        with self._lock:
            store = self._read()
            if profile not in store:
                return False
            del store[profile]
            self._write(store)
            return True

    # -- internals --------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = self.path.read_bytes()
            if raw.startswith(_SECRETS_MAGIC.encode()):
                # S1: DPAPI 加密存储 → 解密后解析
                if not _DPAPI_AVAILABLE:
                    return {}  # 平台无 DPAPI 但文件已加密 → 无法读取
                payload = base64.b64decode(raw[len(_SECRETS_MAGIC):].decode())
                try:
                    decrypted = _dpapi_unprotect(payload)
                except OSError:
                    return {}  # 解密失败 (不同用户/机器) → 视为空
                return json.loads(decrypted.decode("utf-8"))
            return json.loads(raw.decode("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def _write(self, store: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _restrict_to_user(self.path.parent, is_dir=True)
        except OSError:
            pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(store, indent=2).encode("utf-8")
        if _DPAPI_AVAILABLE:
            # S1: 静态加密落盘 (DPAPI, 仅当前用户可解)
            try:
                enc = _dpapi_protect(payload)
                data = (_SECRETS_MAGIC + base64.b64encode(enc).decode()).encode()
            except OSError:
                data = payload  # DPAPI 失败回退明文 (文件权限仍保护)
        else:
            data = payload
        tmp.write_bytes(data)
        _restrict_to_user(tmp, is_dir=False)
        os.replace(tmp, self.path)
