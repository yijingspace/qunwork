"""邀请码 — 名册→真组织的激活凭证 (方案C, 2026-09-06)。

语义: 邀请码 ≈ Wi-Fi 密码二维码 —— 持有码即可加入组织。码内携带:

- team 身份 (id/name) 与 peer_url (邀请者可达地址)
- 共享 AES 密钥 (sync_key_b64) — 导入后新成员能解密全队变更
- 邀请者 Ed25519 公钥 — TOFU 预登记进信任白名单
- 预分配的 roster 槽位 (member_id + role) — 两端同一行 id, LWW 合并天然对上

零新协议: 传输/加密/合并全部复用 sync.py 既有引擎。邀请码本身不做加密
(它携带密钥); 防伪靠"密钥不对 → GCM 全解密失败 → 变更被丢弃"。

格式: ``QWTEAM1.<base64url(json)>``。base64url 便于复制粘贴; GUI 可加
二维码渲染 (同一字符串)。
"""

from __future__ import annotations

import base64
import json
from typing import Any

_INVITE_PREFIX = "QWTEAM1."

# 必需的字符串字段 — 缺任何一项都是畸形码。
_REQUIRED = (
    "team_id", "team_name", "peer_url", "sync_key", "inviter_pub",
    "member_id", "role",
)


class InviteError(ValueError):
    """邀请码畸形 / 版本不认识 / 字段缺失。"""


def make_invite(
    *,
    team_id: str,
    team_name: str,
    peer_url: str,
    sync_key_b64: str,
    inviter_pub: str,
    member_id: str,
    role: str,
    invited_by: str = "",
    note: str = "",
) -> str:
    payload: dict[str, Any] = {
        "v": 1,
        "team_id": str(team_id),
        "team_name": str(team_name),
        "peer_url": str(peer_url).rstrip("/"),
        "sync_key": str(sync_key_b64),
        "inviter_pub": str(inviter_pub),
        "member_id": str(member_id),
        "role": str(role),
    }
    if invited_by:
        payload["invited_by"] = str(invited_by)
    if note:
        payload["note"] = str(note)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return _INVITE_PREFIX + b64


def parse_invite(code: str) -> dict[str, Any]:
    """Decode + structurally validate an invite code. Raises InviteError."""
    code = str(code or "").strip()
    if not code.startswith(_INVITE_PREFIX):
        raise InviteError("not a team invite code (missing QWTEAM1. prefix)")
    body = code[len(_INVITE_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise InviteError(f"invite code is corrupted: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise InviteError(
            f"unsupported invite version {payload.get('v') if isinstance(payload, dict) else '?'}"
            " — update QunWork and ask for a fresh code"
        )
    for field in _REQUIRED:
        val = payload.get(field)
        if not isinstance(val, str) or not val.strip():
            raise InviteError(f"invite code missing field: {field}")
    return payload
