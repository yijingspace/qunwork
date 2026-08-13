"""P2P 团队同步引擎（设计方案第六章）。

原则：
- **本地优先**：数据先写本地 TeamStore，再异步同步；
- **端到端加密**：Ed25519 签名（作者不可抵赖 + 完整性）+ AES-GCM 对称加密
  （内容对中继/中间人不可见；同队成员共享同步密钥）；
- **LWW 合并**：每个实体带全局唯一 change_id（去重）+ ts（时间戳，高者胜）；
- **可选 peer 直连**：HTTP 拉取模式（本机暴露 outbox/ingest，peer 之间
  push/pull），relay（DERP 式）留接口。

同步范围（MVP）：members + task_groups（TeamStore 自身，LWW 清晰）+
knowledge（追加合并）。skill/template 为扩展点。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


def _now() -> float:
    return time.time()


class SyncSecrets:
    """Ed25519 signing keypair + shared AES-256 key (persisted next to team.db)."""

    def __init__(self, sign_key: ed25519.Ed25519PrivateKey, aes_key: bytes) -> None:
        self.sign_key = sign_key
        self.aes_key = aes_key

    @property
    def public_key_hex(self) -> str:
        return self.sign_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()

    def sign(self, data: bytes) -> bytes:
        return self.sign_key.sign(data)

    def verify(self, data: bytes, signature: bytes, peer_public_key_hex: str) -> bool:
        try:
            pub = ed25519.Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(peer_public_key_hex)
            )
            pub.verify(signature, data)
            return True
        except Exception:
            return False

    def encrypt(self, plaintext: bytes) -> dict:
        nonce = uuid.uuid4().bytes[:12]
        ct = AESGCM(self.aes_key).encrypt(nonce, plaintext, None)
        return {
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ct).decode(),
            "author_pub": self.public_key_hex,
        }

    def decrypt(self, envelope: dict) -> bytes:
        nonce = base64.b64decode(envelope["nonce"])
        ct = base64.b64decode(envelope["ciphertext"])
        return AESGCM(self.aes_key).decrypt(nonce, ct, None)


def load_or_create_sync_secrets(path: str | Path) -> SyncSecrets:
    """Load (or create) the team's sync secrets: Ed25519 key + AES key file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sign_path = path.with_suffix(".sign.pem")
    aes_path = path.with_suffix(".aes")
    if sign_path.exists() and aes_path.exists():
        sign_key = serialization.load_pem_private_key(
            sign_path.read_bytes(), password=None
        )
        aes_key = aes_path.read_bytes()
        if len(aes_key) != 32:
            aes_key = aes_key[:32].ljust(32, b"\0")
        return SyncSecrets(sign_key, aes_key)
    sign_key = ed25519.Ed25519PrivateKey.generate()
    sign_path.write_bytes(
        sign_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    aes_key = __import__("os").urandom(32)  # AES-256 key, 32 bytes
    aes_path.write_bytes(aes_key)
    return SyncSecrets(sign_key, aes_key)


class TeamSync:
    """P2P sync engine: snapshot collect → encrypt → push/pull → LWW merge."""

    def __init__(
        self,
        store: Any,
        *,
        secrets_path: str | Path,
        author: str = "local",
        knowledge_upsert: Optional[Callable[[dict], Any]] = None,
    ) -> None:
        self.store = store
        self.secrets = load_or_create_sync_secrets(secrets_path)
        self.author = author
        # knowledge_upsert(payload) -> apply one knowledge change (追加合并).
        self._knowledge_upsert = knowledge_upsert
        self._lock = threading.Lock()

    # ── outbox 收集 ──────────────────────────────────────────────────────────
    def collect_snapshot_changes(self, *, force: bool = False) -> list[dict]:
        """Turn the current local entities into upsert changes. Skips entities
        already recorded (by entity_id) unless `force`."""
        with self._lock:
            known = {
                c["entity_id"]
                for c in self.store.list_sync_changes(limit=10_000)
            }
        changes: list[dict] = []
        for m in self.store.list_members():
            if not force and m["id"] in known:
                continue
            ts = _now()
            cid = self.store.record_sync_change(
                "member", m["id"], "upsert", m, author=self.author, ts=ts
            )
            changes.append(
                {
                    "change_id": cid, "entity_type": "member",
                    "entity_id": m["id"], "op": "upsert",
                    "payload": m, "ts": ts, "author": self.author,
                }
            )
        for g in self.store.list_task_groups(include_dissolved=True):
            if not force and g["id"] in known:
                continue
            ts = _now()
            cid = self.store.record_sync_change(
                "task_group", g["id"], "upsert", g, author=self.author, ts=ts
            )
            changes.append(
                {
                    "change_id": cid, "entity_type": "task_group",
                    "entity_id": g["id"], "op": "upsert",
                    "payload": g, "ts": ts, "author": self.author,
                }
            )
        return changes

    def pending(self) -> list[dict]:
        return self.store.pending_sync_changes()

    # ── 加密传输 ─────────────────────────────────────────────────────────────
    def pack_for_transport(self, changes: list[dict]) -> list[dict]:
        """Serialize + sign + encrypt each change (AES-GCM; author sig over plaintext)."""
        out = []
        for c in changes:
            body = json.dumps(c, ensure_ascii=False).encode()
            sig = self.secrets.sign(body)
            env = self.secrets.encrypt(body)
            env["author_sig"] = base64.b64encode(sig).decode()
            out.append(env)
        return out

    def unpack_from_transport(self, envelopes: list[dict]) -> list[dict]:
        """Decrypt + verify + deserialize incoming changes. Malformed or
        tampered envelopes are skipped (never crash the merge)."""
        out = []
        for env in envelopes:
            try:
                body = self.secrets.decrypt(env)
            except Exception:
                logger.warning("dropping undecryptable change (bad/tampered envelope)")
                continue
            sig = base64.b64decode(env.get("author_sig", ""))
            if not self.secrets.verify(body, sig, env.get("author_pub", "")):
                logger.warning("dropping change with bad signature from %s", env.get("author_pub"))
                continue
            out.append(json.loads(body.decode()))
        return out

    # ── 合并 (LWW) ───────────────────────────────────────────────────────────
    def merge_changes(self, changes: list[dict]) -> dict:
        """Idempotent merge: dedup by change_id, LWW by ts, dispatch by type."""
        applied = skipped = 0
        for c in changes:
            cid = str(c.get("change_id") or "")
            etype = str(c.get("entity_type") or "")
            eid = str(c.get("entity_id") or "")
            op = str(c.get("op") or "upsert")
            payload = c.get("payload") or {}
            ts = float(c.get("ts") or 0)
            if not cid or not eid:
                continue
            # 去重: change_id 已存在 → skip
            if not self.store.ingest_sync_change(c):
                skipped += 1
                continue
            # LWW: 本地实体已有且本地更新 → skip (本地 ts 更高)
            if not self._lww_wins(etype, eid, ts, op):
                skipped += 1
                continue
            self._apply(etype, eid, op, payload)
            applied += 1
        return {"applied": applied, "skipped": skipped}

    def _lww_wins(self, etype: str, eid: str, remote_ts: float, op: str) -> bool:
        if op == "delete":
            return True
        if etype == "member":
            return remote_ts >= (self.store.get_member(eid) or {}).get("last_seen", 0)
        if etype == "task_group":
            g = self.store.get_task_group(eid)
            if g is None:
                return True
            return remote_ts >= g.get("created_at", 0)
        return True  # knowledge: 追加合并, 无冲突

    def _apply(self, etype: str, eid: str, op: str, payload: dict) -> None:
        try:
            if etype == "member":
                if op == "delete":
                    self.store.remove_member(eid)
                elif self.store.get_member(eid) is None:
                    self.store.add_member(
                        str(payload.get("name") or eid),
                        role=str(payload.get("role") or "worker"),
                        persona_id=payload.get("persona_id"),
                    )
                else:
                    self.store.update_member(
                        eid,
                        name=str(payload.get("name") or eid),
                        role=str(payload.get("role") or "worker"),
                        status=str(payload.get("status") or "offline"),
                    )
            elif etype == "task_group":
                if op == "delete":
                    self.store.update_task_group_state(eid, "dissolved")
                elif self.store.get_task_group(eid) is None:
                    self.store.create_task_group(
                        str(payload.get("goal") or ""),
                        owner_member=payload.get("owner_member"),
                        member_ids=payload.get("member_ids"),
                        agent_ids=payload.get("agent_ids"),
                        group_id=eid,
                    )
                else:
                    st = payload.get("state")
                    if st in ("active", "reviewing", "dissolved"):
                        self.store.update_task_group_state(eid, st)
            elif etype == "knowledge" and self._knowledge_upsert is not None:
                self._knowledge_upsert(payload)
        except Exception:
            logger.exception("merge apply failed %s/%s", etype, eid)

    # ── push / pull ──────────────────────────────────────────────────────────
    async def push(self, peer_url: str) -> dict:
        """Push pending local changes to a peer's ingest endpoint."""
        import httpx

        changes = self.pending()
        if not changes:
            return {"pushed": 0}
        envelopes = self.pack_for_transport(changes)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{peer_url.rstrip('/')}/v1/team/sync/ingest",
                    json={"envelopes": envelopes},
                )
            if r.status_code != 200:
                return {"pushed": 0, "error": f"peer ingest {r.status_code}"}
            self.store.mark_sync_changes_synced([c["change_id"] for c in changes])
            return {"pushed": len(changes)}
        except Exception as exc:
            return {"pushed": 0, "error": str(exc)}

    async def pull(self, peer_url: str) -> dict:
        """Pull a peer's outbox, decrypt, verify, merge."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{peer_url.rstrip('/')}/v1/team/sync/outbox")
            if r.status_code != 200:
                return {"pulled": 0, "error": f"peer outbox {r.status_code}"}
            envelopes = (r.json() or {}).get("envelopes", [])
        except Exception as exc:
            return {"pulled": 0, "error": str(exc)}
        changes = self.unpack_from_transport(envelopes)
        if not changes:
            return {"pulled": 0, "merged": 0}
        result = self.merge_changes(changes)
        return {"pulled": len(changes), **result}

    async def run(self, peer_url: Optional[str] = None) -> dict:
        """One sync round: push local → pull remote → merge."""
        url = peer_url or self.store.sync_config_get("peer_url")
        if not url:
            return {"ok": False, "error": "no peer_url configured"}
        pushed = await self.push(url)
        pulled = await self.pull(url)
        self.store.sync_config_set("last_sync", str(_now()))
        return {"ok": True, "peer_url": url, "push": pushed, "pull": pulled}
