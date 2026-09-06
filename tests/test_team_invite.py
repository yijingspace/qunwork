"""方案C 邀请码激活: 码编解码 + 密钥导入 + 双节点 join 端到端 (2026-09-06)."""

from __future__ import annotations

import base64
import json

import pytest

from coworker.team.invite import InviteError, make_invite, parse_invite
from coworker.team.store import TeamStore
from coworker.team.sync import TeamSync, import_sync_secrets, load_or_create_sync_secrets


# -- 码编解码 --------------------------------------------------------------------


def test_invite_roundtrip_and_fields():
    code = make_invite(
        team_id="team-abc", team_name="研究组", peer_url="http://192.168.1.5:8765/",
        sync_key_b64="a2V5", inviter_pub="ab" * 32,
        member_id="member-x1", role="reviewer", invited_by="member-me",
    )
    assert code.startswith("QWTEAM1.")
    inv = parse_invite(code)
    assert inv["team_id"] == "team-abc"
    assert inv["peer_url"] == "http://192.168.1.5:8765"  # 尾斜杠归一
    assert inv["member_id"] == "member-x1"
    assert inv["role"] == "reviewer"
    assert json.loads(base64.urlsafe_b64decode(code[len("QWTEAM1."):] + "=="))["v"] == 1


def test_invite_rejects_garbage():
    with pytest.raises(InviteError):
        parse_invite("hello world")
    with pytest.raises(InviteError):
        parse_invite("QWTEAM1.###not-base64###")
    body = base64.urlsafe_b64encode(json.dumps({"v": 99}).encode()).decode().rstrip("=")
    with pytest.raises(InviteError):
        parse_invite(f"QWTEAM1.{body}")
    payload = {"v": 1, "team_id": "t"}  # 缺字段
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(InviteError):
        parse_invite(f"QWTEAM1.{body}")


# -- store 支持 -------------------------------------------------------------------


def test_add_member_with_explicit_id_and_upsert(tmp_path):
    s = TeamStore(tmp_path / "team.db")
    m = s.add_member("小王", "reviewer", member_id="member-slot", status="invited")
    assert m["id"] == "member-slot" and m["status"] == "invited"
    # join 激活: 同 id 原位更新, 不产生第二行
    m2 = s.add_member("小王改名", "reviewer", member_id="member-slot", status="online")
    assert m2["name"] == "小王改名" and m2["status"] == "online"
    assert len(s.list_members()) == 1


def test_ensure_team_join_mode_no_ghost_chairman(tmp_path):
    s = TeamStore(tmp_path / "team.db")
    t = s.ensure_team("远程团队", create_member=False, team_id="team-remote")
    assert t["id"] == "team-remote"
    assert s.list_members() == []  # 不造 Me/chairman 幽灵行
    s.set_my_member_id("member-slot")
    assert s.get_team()["my_member_id"] == "member-slot"


def test_import_sync_secrets_key_material(tmp_path):
    a = load_or_create_sync_secrets(tmp_path / "a")
    b = import_sync_secrets(tmp_path / "b", a.shared_key_b64)
    env = a.encrypt(b"team secret")
    assert b.decrypt(env) == b"team secret"  # 共享密钥一致
    assert b.public_key_hex != a.public_key_hex  # 签名 keypair 各自独立
    with pytest.raises(ValueError):
        import_sync_secrets(tmp_path / "c", base64.b64encode(b"short").decode())


# -- 双节点端到端: invite → join → 激活 → 名册合并 --------------------------------


def _node(tmp_path, store_name, sync_name):
    store = TeamStore(tmp_path / f"{store_name}.db")
    sync = TeamSync(store, secrets_path=tmp_path / f"{sync_name}_secrets", author=sync_name)
    return store, sync


async def test_invite_join_activation_end_to_end(tmp_path):
    """邀请方生成邀请码 → 加入方 parse+导入密钥+激活槽位 → 双向信封直连同步 →
    名册合并 (invited→online), 零 HTTP。"""
    store_a, sync_a = _node(tmp_path, "team_a", "a")
    team_a = store_a.ensure_team("研究蜂群")  # A 端: Me/chairman 行
    inviter_member = store_a.add_member("小王", "reviewer")
    store_a.update_member(inviter_member["id"], status="invited")

    code = make_invite(
        team_id=team_a["id"], team_name=team_a["name"],
        peer_url="http://127.0.0.1:9",  # 不可达端口: run 失败不致命
        sync_key_b64=sync_a.secrets.shared_key_b64,
        inviter_pub=sync_a.secrets.public_key_hex,
        member_id=inviter_member["id"], role="reviewer",
    )
    inv = parse_invite(code)

    # B 端 join 流程 (manager.team_join 的核心步骤, 本地直连不跑 HTTP run)
    store_b, sync_b = _node(tmp_path, "team_b", "b")
    store_b.ensure_team(inv["team_name"], create_member=False, team_id=inv["team_id"])
    sync_b.secrets = import_sync_secrets(tmp_path / "b_secrets", inv["sync_key"])
    store_b.sync_config_set("peer_url", inv["peer_url"])
    assert sync_b.record_peer_public_key(inv["inviter_pub"]) is not False
    mine = store_b.add_member("小王本人", inv["role"], member_id=inv["member_id"], status="online")
    store_b.set_my_member_id(mine["id"])
    # 同 manager.team_join: 槽位 entity 已被 A 的 ingest 记录在案,
    # collect 会跳过 → 激活变更必须显式登记 outbox 才推得出。
    store_b.record_sync_change("member", mine["id"], "upsert", mine, author="b")
    assert mine["status"] == "online" and mine["name"] == "小王本人"

    # A → B: A 推快照 (chairman Me + invited 槽位) → B 合并
    envs = sync_a.pack_for_transport(sync_a.collect_snapshot_changes())
    assert sync_b.merge_changes(sync_b.unpack_from_transport(envs))["applied"] >= 2

    # B → A: B 推激活后的槽位行 (显式登记的 outbox, LWW 高 ts) → A 的 invited 翻 online
    envs = sync_b.pack_for_transport(sync_b.pending())
    assert len(envs) >= 1  # 激活变更确实在 outbox 里
    merged = sync_a.merge_changes(sync_a.unpack_from_transport(envs))
    assert merged["applied"] >= 1
    slot_a = store_a.get_member(inv["member_id"])
    assert slot_a["status"] == "online"  # 名册激活完成: 待接受 → 已上线
    assert slot_a["name"] == "小王本人"  # joiner 自选显示名同步回邀请方

    # B 端不自造董事长幽灵行: 唯一可能的 "Me" 行是 A 的身份行同步过来 (id 对齐),
    # 且 B 自己的身份指向邀请槽位。
    me_rows = [m for m in store_b.list_members() if m["name"] == "Me"]
    assert all(m["id"] == team_a["my_member_id"] for m in me_rows)
    assert store_b.get_team()["my_member_id"] == inv["member_id"]


def test_wrong_key_join_yields_undecryptable(tmp_path):
    """伪邀请码 (密钥不对) 不产生半加入状态: 密钥导入后对方信封全部解密失败。"""
    store_a, sync_a = _node(tmp_path, "team_a", "a")
    store_a.ensure_team()
    envs = sync_a.pack_for_transport(sync_a.collect_snapshot_changes())

    store_b, sync_b = _node(tmp_path, "team_b", "b")
    fake_key = base64.b64encode(b"\x01" * 32).decode()
    sync_b.secrets = import_sync_secrets(tmp_path / "b_secrets", fake_key)
    assert sync_b.merge_changes(sync_b.unpack_from_transport(envs))["applied"] == 0
