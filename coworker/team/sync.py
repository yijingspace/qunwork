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

    @property
    def shared_key_b64(self) -> str:
        """团队共享 AES 密钥 (base64) — 邀请码携带它, 新成员 import_sync_secrets
        导入后即拿到全队解密能力。泄露此值 = 泄露组织入场券。"""
        return base64.b64encode(self.aes_key).decode("ascii")

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
        # M11: os.urandom(12) — uuid4 bytes carry fixed version/variant bits
        # (~92 bits of entropy after truncation); urandom gives the full 96.
        # The author public key is bound as AAD so a tamper that swaps the
        # author field also breaks GCM authentication (not just the outer
        # Ed25519 check).
        import os as _os

        nonce = _os.urandom(12)
        author_pub = self.public_key_hex
        ct = AESGCM(self.aes_key).encrypt(
            nonce, plaintext, author_pub.encode("ascii")
        )
        return {
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ct).decode(),
            "author_pub": author_pub,
        }

    def decrypt(self, envelope: dict) -> bytes:
        nonce = base64.b64decode(envelope["nonce"])
        ct = base64.b64decode(envelope["ciphertext"])
        # AAD must match the encrypt side (author_pub) or GCM rejects.
        aad = str(envelope.get("author_pub") or "").encode("ascii")
        return AESGCM(self.aes_key).decrypt(nonce, ct, aad)


def load_or_create_sync_secrets(path: str | Path) -> SyncSecrets:
    """Load (or create) the team's sync secrets: Ed25519 key + AES key file.

    Both files carry user-only permissions (0600 / icacls) via
    write_private_text — the AES-256 shared key and the Ed25519 signing key
    must never be world-readable (H2)."""
    from ..secrets import write_private_text

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sign_path = path.with_suffix(".sign.pem")
    aes_path = path.with_suffix(".aes")
    if sign_path.exists() and aes_path.exists():
        sign_key = serialization.load_pem_private_key(
            sign_path.read_bytes(), password=None
        )
        raw_aes = aes_path.read_bytes().strip()
        # New layout stores the 32-byte key base64-encoded (text writer); older
        # files hold the raw bytes — accept both.
        import base64 as _b64

        if len(raw_aes) != 32:
            try:
                aes_key = _b64.b64decode(raw_aes)
            except Exception:
                aes_key = raw_aes
        else:
            aes_key = raw_aes
        if len(aes_key) != 32:
            aes_key = aes_key[:32].ljust(32, b"\0")
        return SyncSecrets(sign_key, aes_key)
    sign_key = ed25519.Ed25519PrivateKey.generate()
    write_private_text(
        sign_path,
        sign_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
    )
    aes_key = __import__("os").urandom(32)  # AES-256 key, 32 bytes
    # write_private_text is a TEXT writer — carry the binary key base64 so a
    # round-trip through UTF-8 cannot corrupt it (H2).
    import base64 as _b64

    write_private_text(aes_path, _b64.b64encode(aes_key).decode("ascii"))
    return SyncSecrets(sign_key, aes_key)


def import_sync_secrets(path: str | Path, shared_key_b64: str) -> SyncSecrets:
    """方案C join: 用邀请码里的共享 AES 密钥覆盖本机 .aes 文件 (Ed25519 签名
    keypair 保持本机独立)。密钥不匹配则后续所有解密失败 — 伪邀请码自然失效,
    不产生"半加入"状态。

    注意不能直接走 load_or_create_sync_secrets — 本地无 .sign.pem 时它会生成
    **整套**新密钥 (顺带覆盖刚导入的 .aes); 这里只补缺失的签名 keypair,
    共享密钥以导入值为准。"""
    import base64 as _b64

    from ..secrets import write_private_text

    try:
        key = _b64.b64decode(shared_key_b64.encode("ascii"))
    except Exception as exc:
        raise ValueError(f"invalid shared key: {exc}") from exc
    if len(key) != 32:
        raise ValueError(f"shared key must be 32 bytes, got {len(key)}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sign_path = path.with_suffix(".sign.pem")
    aes_path = path.with_suffix(".aes")
    write_private_text(aes_path, _b64.b64encode(key).decode("ascii"))
    if sign_path.exists():
        sign_key = serialization.load_pem_private_key(
            sign_path.read_bytes(), password=None
        )
    else:
        sign_key = ed25519.Ed25519PrivateKey.generate()
        write_private_text(
            sign_path,
            sign_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii"),
        )
    return SyncSecrets(sign_key, key)


class TeamSync:
    """P2P sync engine: snapshot collect → encrypt → push/pull → LWW merge."""

    def __init__(
        self,
        store: Any,
        *,
        secrets_path: str | Path,
        author: str = "local",
        knowledge_upsert: Optional[Callable[[dict], Any]] = None,
        peer_public_keys: Optional[set[str]] = None,
    ) -> None:
        self.store = store
        self.secrets = load_or_create_sync_secrets(secrets_path)
        self.author = author
        # knowledge_upsert(payload) -> apply one knowledge change (追加合并).
        self._knowledge_upsert = knowledge_upsert
        self._lock = threading.Lock()
        # H3: optional peer allow-list. When set, inbound envelopes whose
        # author_pub is NOT in this set are dropped (the envelope's self-declared
        # key alone is not enough — anyone holding the shared AES key could claim
        # another author). Empty/None = accept any valid signature (legacy).
        # S1 架构级修复: 白名单从 store 的 sync_config 自动加载 (记录过的
        # peer 公钥), 使 P2P 拉取默认只信任已建立信任关系的 peer。
        self._peer_public_keys = (
            set(peer_public_keys) if peer_public_keys is not None else None
        )
        if self._peer_public_keys is None:
            recorded = self._load_recorded_peer_keys()
            if recorded:
                self._peer_public_keys = recorded

    # ── S1: peer 公钥信任管理 ────────────────────────────────────────────────
    def _load_recorded_peer_keys(self) -> Optional[set[str]]:
        """从 store 读取所有记录的 peer 公钥 (sync_config peer_pubkey_*)。
        有记录 → 返回白名单; 无记录 → None (保持 legacy 兼容, 首次握手 TOFU)。"""
        try:
            keys = set()
            for i in range(64):
                v = self.store.sync_config_get(f"peer_pubkey_{i}")
                if not v:
                    break
                keys.add(v.strip())
            return keys if keys else None
        except Exception:
            return None

    def record_peer_public_key(self, peer_public_key: str) -> bool:
        """记录一个 peer 公钥到信任白名单 (S1: 首次握手 TOFU + 可重复覆盖)。
        返回是否新增 (True) 或已是已知 peer (False)。"""
        key = (peer_public_key or "").strip()
        if not key:
            return False
        try:
            # 校验是合法 Ed25519 公钥 hex (64 字符)
            if len(key) != 64:
                logger.warning("refusing invalid peer public key length %d", len(key))
                return False
            bytes.fromhex(key)  # raises on bad hex
        except (ValueError, TypeError):
            logger.warning("refusing malformed peer public key")
            return False
        with self._lock:
            known = self._load_recorded_peer_keys() or set()
            if key in known:
                return False
            idx = len(known)
            self.store.sync_config_set(f"peer_pubkey_{idx}", key)
            if self._peer_public_keys is not None:
                self._peer_public_keys.add(key)
            else:
                self._peer_public_keys = {key}
        return True

    def trusted_peer_keys(self) -> set[str]:
        """当前信任白名单 (S1 状态展示)。"""
        if self._peer_public_keys is not None:
            return set(self._peer_public_keys)
        recorded = self._load_recorded_peer_keys()
        return recorded or set()

    def allow_list_active(self) -> bool:
        """白名单是否生效 (True = 只信任记录过的 peer; False = legacy 接受
        任何有效签名 — 不安全, GUI 应提示登记)。"""
        return self._peer_public_keys is not None

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
            author_pub = str(env.get("author_pub") or "")
            # H3: when a peer allow-list is configured, the author must be in
            # it — the self-declared key is not proof of identity by itself.
            if self._peer_public_keys is not None and author_pub not in self._peer_public_keys:
                logger.warning("dropping change from non-peer author %s", author_pub[:16])
                continue
            sig = base64.b64decode(env.get("author_sig", ""))
            if not self.secrets.verify(body, sig, author_pub):
                logger.warning("dropping change with bad signature from %s", author_pub)
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
        # H3: delete must participate in LWW too — an unconditional "delete wins"
        # let any envelope with a valid shared-key signature erase members/groups
        # even when the local side has newer data. A delete only applies when the
        # remote change is at least as new as the entity's local last-write time.
        if op == "delete":
            if etype == "member":
                local_ts = (self.store.get_member(eid) or {}).get("last_seen", 0) or 0
            elif etype == "task_group":
                g = self.store.get_task_group(eid)
                local_ts = (g or {}).get("dissolved_at") or (g or {}).get("created_at", 0) or 0
            else:
                local_ts = 0
            return remote_ts >= local_ts
        if etype == "member":
            # `or 0` 防 last_seen=None (新行/邀请槽位从不心跳) — .get(key, 0)
            # 只兜缺键不兜 NULL 值, 直接比较会 float >= None TypeError。
            return remote_ts >= ((self.store.get_member(eid) or {}).get("last_seen") or 0)
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
                    # member_id=eid: 两端 roster 对齐靠同 id (task_group 分支同
                    # 理传 group_id)。历史版本让 add_member 自生成 id → 同一远端
                    # 成员在本机换了主键, 后续 LWW/去重全部错位。
                    self.store.add_member(
                        str(payload.get("name") or eid),
                        role=str(payload.get("role") or "worker"),
                        persona_id=payload.get("persona_id"),
                        member_id=eid,
                        status=str(payload.get("status") or "offline"),
                    )
                else:
                    # 方案C 合并语义 (纯 ts-LWW 在 join 竞态下会倒退):
                    # 1) 本机身份行本地说了算 — 远端旧快照不得覆盖本人选的
                    #    显示名/激活状态 (角色由邀请方管理, 走名册 API 变更)。
                    # 2) status 单调 (invited < offline < online) — 激活终态
                    #    不被先到后合的邀请快照回退。
                    my_id = (self.store.get_team() or {}).get("my_member_id")
                    if eid == my_id:
                        return
                    existing = self.store.get_member(eid) or {}
                    new_status = str(payload.get("status") or "offline")
                    rank = {"invited": 0, "offline": 1, "busy": 1, "online": 2}
                    if rank.get(new_status, 1) < rank.get(str(existing.get("status") or "offline"), 1):
                        new_status = str(existing.get("status"))
                    self.store.update_member(
                        eid,
                        name=str(payload.get("name") or eid),
                        role=str(payload.get("role") or "worker"),
                        status=new_status,
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
