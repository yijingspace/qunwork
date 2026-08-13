"""Tests for P2P 团队同步 (设计方案第六章).

Covers: secrets (Ed25519 sig + AES-GCM), outbox collect, encrypted transport
round-trip, LWW merge + dedup, and a two-node API integration where node B
ingests node A's outbox over the real endpoints.
"""

from __future__ import annotations

import pytest

from coworker.team.sync import TeamSync, load_or_create_sync_secrets


def _sync(tmp_path, store=None, name="sync") -> TeamSync:
    from coworker.team.store import TeamStore

    st = store or TeamStore(tmp_path / f"{name}.db")
    return TeamSync(
        st,
        secrets_path=tmp_path / f"{name}_secrets",
        author=name,
    )


# -- secrets ------------------------------------------------------------------
def test_secrets_load_or_generate_and_roundtrip(tmp_path):
    a = load_or_create_sync_secrets(tmp_path / "secrets")
    b = load_or_create_sync_secrets(tmp_path / "secrets")  # reload
    assert a.public_key_hex == b.public_key_hex  # persisted, same keypair
    data = b"hello team"
    sig = a.sign(data)
    assert b.verify(data, sig, a.public_key_hex) is True
    assert b.verify(b"tampered", sig, a.public_key_hex) is False  # tamper rejected

    env = a.encrypt(data)
    assert b.decrypt(env) == data  # AES-GCM round-trip (shared key)
    # decrypted payload differs from plaintext on the wire
    assert b.decrypt(env) != env["ciphertext"].encode()


# -- outbox + transport -------------------------------------------------------
def test_collect_snapshot_and_pack_unpack(tmp_path):
    s = _sync(tmp_path)
    s.store.ensure_team()
    s.store.add_member("张总", role="gm")
    s.store.create_task_group("goal-x")
    changes = s.collect_snapshot_changes()
    assert len(changes) == 3  # chairman Me + 张总 + task_group
    assert {c["entity_type"] for c in changes} == {"member", "task_group"}

    envelopes = s.pack_for_transport(changes)
    assert len(envelopes) == len(changes)
    unpacked = s.unpack_from_transport(envelopes)
    assert {c["entity_id"] for c in unpacked} == {c["entity_id"] for c in changes}

    # 篡改 payload → 签名校验失败被丢弃
    tampered = [dict(envelopes[0])]
    tampered[0]["ciphertext"] = tampered[0]["ciphertext"][:-2] + "AA"
    assert s.unpack_from_transport(tampered) == []


# -- merge (LWW + dedup) ------------------------------------------------------
def test_merge_applies_remote_member_and_dedups(tmp_path):
    a = _sync(tmp_path, name="a")
    a.store.ensure_team()
    a.store.add_member("张总", role="gm")
    changes = a.collect_snapshot_changes()

    b = _sync(tmp_path, name="b")  # fresh store, no members yet
    b.store.ensure_team()
    result = b.merge_changes(changes)
    assert result["applied"] == len(changes)
    # 张总 同步到了 B; A 的 chairman(Me) 也作为独立成员同步过来
    b_names = {m["name"] for m in b.store.list_members()}
    a_names = {m["name"] for m in a.store.list_members()}
    assert "张总" in b_names
    assert a_names <= b_names  # A 的成员全部在 B (各机 chairman 独立 id)
    # 幂等: 重复 ingest 全部去重 (change_id 已存在)
    result2 = b.merge_changes(changes)
    assert result2["applied"] == 0


def test_merge_lww_older_change_loses(tmp_path):
    a = _sync(tmp_path, name="a")
    a.store.ensure_team()
    m = a.store.add_member("王工", role="worker")
    # 本地新变更 (ts 高)
    a.store.update_member(m["id"], role="reviewer", last_seen=2000.0)
    # 远端旧变更 (ts 低) — 应被 LWW 拒绝
    old_change = {
        "change_id": "remote:old1",
        "entity_type": "member",
        "entity_id": m["id"],
        "op": "upsert",
        "payload": {"name": "王工", "role": "worker", "status": "offline"},
        "ts": 1000.0,
        "author": "remote",
    }
    res = a.merge_changes([old_change])
    assert res["applied"] == 0  # 本地更新 (last_seen=2000) 胜出
    assert a.store.get_member(m["id"])["role"] == "reviewer"


# -- 双节点 API 集成 ----------------------------------------------------------
def test_two_node_sync_over_api(tmp_path, monkeypatch):
    """Node A's outbox is ingested by Node B through the real endpoints —
    encrypted on the wire, verified, decrypted, merged."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    import shutil

    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    mgr_a = SessionManager(data_dir=tmp_path / "a")
    # 模拟团队同步密钥分发: B 复用 A 的 sync_secrets(同队共享密钥)。
    b_dir = tmp_path / "b"
    b_dir.mkdir(exist_ok=True)
    for suffix in (".sign.pem", ".aes"):
        shutil.copy(tmp_path / "a" / f"sync_secrets{suffix}", b_dir / f"sync_secrets{suffix}")
    mgr_b = SessionManager(data_dir=b_dir)
    ca = TestClient(create_app(mgr_a))
    cb = TestClient(create_app(mgr_b))

    # A: 建 team + 加成员 + 加任务组 → 收集快照
    assert ca.get("/v1/team").status_code == 200  # ensure_team 建 chairman Me
    mgr_a.team_sync.collect_snapshot_changes()
    assert ca.get("/v1/team/sync/status").json()["status"] == "single"

    # A 的 outbox (加密) → B 通过真实端点 ingest
    outbox = ca.get("/v1/team/sync/outbox").json()["envelopes"]
    assert outbox, "A 应有待同步变更"
    r = cb.post("/v1/team/sync/ingest", json={"envelopes": outbox})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["applied"] >= 1

    # B 现在拥有 A 的成员 (chairman Me)
    b_names = {m["name"] for m in mgr_b.team_store.list_members()}
    assert "Me" in b_names

    # 反向: B 加成员 → A 拉取合并
    mgr_b.team_store.add_member("B成员", role="worker")
    mgr_b.team_sync.collect_snapshot_changes()
    outbox_b = cb.get("/v1/team/sync/outbox").json()["envelopes"]
    r2 = ca.post("/v1/team/sync/ingest", json={"envelopes": outbox_b})
    assert r2.json()["applied"] >= 1
    a_names = {m["name"] for m in mgr_a.team_store.list_members()}
    assert "B成员" in a_names

    # 配置 peer 后 status 变为 connected
    assert mgr_a.team_sync_config("http://node-b:9999")["ok"] is True
    assert ca.get("/v1/team/sync/status").json()["status"] == "connected"


def test_sync_status_and_run_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(mgr))
    # 未配置 peer → run 返回友好错误 dict (不抛异常)
    import asyncio

    result = asyncio.run(mgr.team_sync_run())
    assert result["ok"] is False and "no peer_url" in result["error"]
    # 配置缺 peer_url → error
    assert mgr.team_sync_config("")["ok"] is False
