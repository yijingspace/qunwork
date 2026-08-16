"""Tests for the SecretStore (C0)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

from coworker.secrets import SecretStore


def test_put_get_round_trip(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.put("slack:default", {"type": "token", "bot_token": "xoxb-123"})
    assert store.get("slack:default") == {"type": "token", "bot_token": "xoxb-123"}
    assert store.get("missing") is None


def test_env_ref_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOK", "from-env")
    store = SecretStore(tmp_path / "secrets.json")
    store.put("slack:default", {"type": "token", "bot_token": "${MY_TOK}"})
    assert store.get("slack:default")["bot_token"] == "from-env"


def test_dotenv_ref_resolution(tmp_path):
    (tmp_path / ".env").write_text('DOCS_TOKEN = "shhh"\n', encoding="utf-8")
    store = SecretStore(tmp_path / "secrets.json")
    store.put("docs:default", {"headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}})
    assert store.get("docs:default")["headers"]["Authorization"] == "Bearer shhh"


def test_unresolved_ref_left_intact(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.put("x", {"v": "${NOPE_NOT_SET}"})
    assert store.get("x")["v"] == "${NOPE_NOT_SET}"


def test_status_hides_values(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.put(
        "gmail:default",
        {
            "type": "oauth",
            "access": "secret",
            "account_id": "me@x.com",
            "expires": time.time() - 10,
        },
    )
    store.put("slack:default", {"type": "token", "bot_token": "xoxb"})
    status = {row["profile"]: row for row in store.status()}
    assert status["gmail:default"]["type"] == "oauth"
    assert status["gmail:default"]["account"] == "me@x.com"
    assert status["gmail:default"]["expired"] is True
    assert status["slack:default"]["expired"] is False
    # No secret material anywhere in the status payload.
    blob = str(store.status())
    assert "secret" not in blob and "xoxb" not in blob


def test_secrets_file_is_restricted(tmp_path):
    """The secrets file must be restricted to the current user. POSIX expresses this as mode
    0600; Windows has no such bits, so we assert the ACL instead (inheritance stripped, only
    the current user granted)."""
    path = tmp_path / "secrets.json"
    SecretStore(path).put("x", {"a": 1})
    if sys.platform == "win32":
        out = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True
        ).stdout
        user = os.environ.get("USERNAME", "")
        assert user and user in out  # current user is granted
        # Inherited broad principals must be gone after /inheritance:r.
        assert "NT AUTHORITY\\SYSTEM" not in out
        assert "BUILTIN\\Administrators" not in out
    else:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_delete(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.put("x", {"a": 1})
    assert store.delete("x") is True
    assert store.delete("x") is False
    assert store.get("x") is None


# -- S1 密钥托管: 静态加密 (DPAPI on Windows) ----------------------------------

def test_secrets_at_rest_encrypted_on_windows(tmp_path):
    """S1: Windows 上 secrets.json 应以 DPAPI 加密落盘 (带版本标记),
    磁盘明文不含密钥材料; 重载后仍可读取 (迁移 + 解密透明)。"""
    from coworker.secrets import _DPAPI_AVAILABLE, _SECRETS_MAGIC

    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.put("slack:default", {"type": "token", "bot_token": "xoxb-secret-123"})

    raw = path.read_bytes()
    if _DPAPI_AVAILABLE:
        # 加密标记存在, 且明文密钥不出现在磁盘
        assert raw.startswith(_SECRETS_MAGIC.encode())
        assert b"xoxb-secret-123" not in raw
    else:
        # 非 Windows: 明文 + 文件权限 (0600), 无加密标记
        assert not raw.startswith(_SECRETS_MAGIC.encode())
        assert b"xoxb-secret-123" in raw

    # 重载 (模拟重启) → 透明解密
    store2 = SecretStore(path)
    assert store2.get("slack:default")["bot_token"] == "xoxb-secret-123"


def test_secrets_plaintext_store_migrates(tmp_path):
    """S1: 旧版明文 secrets.json (无加密标记) 读取兼容, 再写入后升级为加密。"""
    from coworker.secrets import _DPAPI_AVAILABLE, _SECRETS_MAGIC

    path = tmp_path / "secrets.json"
    # 模拟旧版明文文件
    path.write_text(
        '{"slack:default": {"type": "token", "bot_token": "legacy-tok"}}',
        encoding="utf-8",
    )
    store = SecretStore(path)
    assert store.get("slack:default")["bot_token"] == "legacy-tok"  # 可读

    # 写操作触发迁移 → 之后文件带加密标记 (Windows)
    store.put("new:default", {"k": "v"})
    if _DPAPI_AVAILABLE:
        assert path.read_bytes().startswith(_SECRETS_MAGIC.encode())
    # 仍可读
    assert SecretStore(path).get("slack:default")["bot_token"] == "legacy-tok"
