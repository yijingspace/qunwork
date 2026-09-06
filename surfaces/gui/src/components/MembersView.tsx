import { useEffect, useState } from "react";
import { useT } from "../i18n";
import {
  addAgent,
  inviteTeamMember,
  joinTeam,
  listAgents,
  listMembers,
  listTeamRoles,
  removeAgent,
  removeMember,
  updateMember,
  type AgentInstance,
  type Member,
} from "../api";

// 方案A 角色统一: 下拉从 /v1/team/roles 拉取 (与权限矩阵/团队校验同一来源);
// 团队模块未加载时回退到这份与注册表一致的静态清单。
const FALLBACK_ROLES: Array<Member["role"]> = [
  "chairman",
  "board",
  "general_manager",
  "scheduler",
  "reviewer",
  "auditor",
  "worker",
  "critic",
  "compliance",
  "risk",
];

/**
 * Members page: member roster + Agent instance pool — **now interactive**.
 *
 * Members:
 *   - Add member (name + role dropdown) → POST /v1/team/members
 *   - Remove member → DELETE /v1/team/members/:id
 *   - Change role inline via select → PATCH /v1/team/members/:id
 *
 * Agents:
 *   - Scale up (role select → add) → POST /v1/team/agents
 *   - Remove/downsize agent → DELETE /v1/team/agents/:id
 *
 * Every mutation optimistically updates state + then re-fetches to reconcile,
 * since the server may choose to sanitize names/IDs.
 */
export function MembersView() {
  const t = useT();
  const [members, setMembers] = useState<Member[] | null>(null);
  const [agents, setAgents] = useState<AgentInstance[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // 方案A: 角色清单从单一注册表拉取 (失败回退静态清单)
  const [roles, setRoles] = useState<Array<string>>(FALLBACK_ROLES);

  // --- member form ---
  const [newName, setNewName] = useState("");
  const [newRole, setNewRole] = useState<Member["role"]>("worker");
  const [savingMember, setSavingMember] = useState(false);
  // --- 方案C: invite code / join ---
  const [inviteCode, setInviteCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [joinCode, setJoinCode] = useState("");
  const [joinName, setJoinName] = useState("");
  const [joining, setJoining] = useState(false);
  const [joinMsg, setJoinMsg] = useState<string | null>(null);

  // --- agent form ---
  const [newAgentRole, setNewAgentRole] = useState<AgentInstance["role"]>("worker");
  const [savingAgent, setSavingAgent] = useState(false);

  useEffect(() => {
    let alive = true;
    listTeamRoles()
      .then((defs) => {
        if (alive && defs.length > 0) setRoles(defs.map((d) => d.name));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const reload = () => {
    Promise.allSettled([listMembers(), listAgents()]).then(([m, a]) => {
      if (m.status === "fulfilled") setMembers(m.value);
      else setMembers([]);
      if (a.status === "fulfilled") setAgents(a.value);
      else setAgents([]);
      if (m.status === "rejected" && a.status === "rejected")
        setErr(t("Team features require the team module (Phase 1)."));
    });
  };

  useEffect(() => {
    let alive = true;
    Promise.allSettled([listMembers(), listAgents()]).then(([m, a]) => {
      if (!alive) return;
      if (m.status === "fulfilled") setMembers(m.value);
      else setMembers([]);
      if (a.status === "fulfilled") setAgents(a.value);
      else setAgents([]);
      if (m.status === "rejected" && a.status === "rejected")
        setErr(t("Team features require the team module (Phase 1)."));
    });
    return () => { alive = false; };
  }, [t]);

  const onAddMember = async (e: React.FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name || savingMember) return;
    setSavingMember(true);
    setJoinMsg(null);
    try {
      // 方案C: 邀请 = 预分配槽位 + 生成组织钥匙 (邀请码), 对方用码 join。
      const r = await inviteTeamMember(name, newRole);
      if (r.ok && r.invite_code) {
        setInviteCode(r.invite_code);
        setCopied(false);
        setNewName("");
      } else if (r.error) {
        setErr(r.error);
      }
    } finally {
      setSavingMember(false);
      reload();
    }
  };

  const onCopyInvite = async () => {
    if (!inviteCode) return;
    try {
      await navigator.clipboard.writeText(inviteCode);
      setCopied(true);
    } catch {
      /* clipboard 不可用时用户可手动全选复制 */
    }
  };

  const onJoin = async (e: React.FormEvent) => {
    e.preventDefault();
    const code = joinCode.trim();
    if (!code || joining) return;
    setJoining(true);
    setJoinMsg(null);
    try {
      const r = await joinTeam(code, joinName.trim());
      if (r.ok) {
        setJoinMsg(t("Joined. The roster is syncing."));
        setJoinCode("");
      } else {
        setJoinMsg(r.error || "join failed");
      }
    } finally {
      setJoining(false);
      reload();
    }
  };

  const onRemoveMember = async (id: string) => {
    await removeMember(id);
    reload();
  };

  const onChangeMemberRole = async (id: string, role: Member["role"]) => {
    await updateMember(id, { role });
    reload();
  };

  const onAddAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (savingAgent) return;
    setSavingAgent(true);
    try {
      await addAgent(newAgentRole);
    } finally {
      setSavingAgent(false);
      reload();
    }
  };

  const onRemoveAgent = async (id: string) => {
    await removeAgent(id);
    reload();
  };

  return (
    <div className="h-full overflow-y-auto">
    <div className="max-w-4xl mx-auto px-6 py-6">
      <h1 className="text-[22px] font-semibold tracking-tight flex items-center gap-2.5">
        <span className="text-[20px]">👥</span>
        {t("Members")}
      </h1>
      <p className="text-[13px] text-muted mt-1.5 leading-relaxed">
        {t("Manage team members and Agent instance pool.")}
      </p>

      {err && (
        <div className="mt-4 rounded-xl2 border border-line bg-panel p-4 text-[13px] text-muted">
          {err}
        </div>
      )}

      {/* 成员列表 */}
      <div className="mt-5 rounded-xl2 border border-line bg-panel p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="text-[14px] font-semibold">{t("Team Members")}</div>
        </div>

        {/* Invite form */}
        <form onSubmit={onAddMember} className="mb-4 flex gap-2">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder={t("Member name")}
            className="flex-1 min-w-0 rounded-lg border border-line bg-paper px-3 py-1.5 text-[13px] outline-none focus:border-accent"
          />
          <select
            value={newRole}
            onChange={(e) => setNewRole(e.target.value as Member["role"])}
            className="rounded-lg border border-line bg-paper px-2 py-1.5 text-[13px] outline-none focus:border-accent"
          >
            {roles.map((r) => (
              <option key={r} value={r}>{t(r)}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={!newName.trim() || savingMember}
            className="rounded-lg bg-accent text-white px-3 py-1.5 text-[12.5px] font-medium disabled:opacity-40"
          >
            {savingMember ? "…" : t("Invite")}
          </button>
        </form>

        {/* 方案C: 生成的邀请码 (组织钥匙) — 复制交给被邀请人 */}
        {inviteCode && (
          <div className="mb-4 rounded-lg border border-accent/40 bg-accent/5 p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-[12px] font-medium text-accent">{t("Invite code")}</div>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={onCopyInvite}
                  className="rounded-md border border-line px-2 py-0.5 text-[11.5px] hover:bg-paper"
                >
                  {copied ? t("Copied") : t("Copy invite code")}
                </button>
                <button
                  onClick={() => setInviteCode(null)}
                  className="rounded-md px-1.5 py-0.5 text-[11.5px] text-faint hover:text-muted"
                  title={t("Dismiss")}
                >
                  ✕
                </button>
              </div>
            </div>
            <code className="mt-1.5 block break-all rounded-md bg-paper border border-line px-2 py-1.5 text-[11px] leading-relaxed select-all">
              {inviteCode}
            </code>
            <div className="mt-1.5 text-[11px] text-muted leading-relaxed">
              {t("The invite code is the key to your org — anyone holding it can join. Share it over a trusted channel.")}
            </div>
          </div>
        )}

        {/* 方案C: 用邀请码加入真实团队 */}
        <form onSubmit={onJoin} className="mb-4 flex gap-2">
          <input
            type="text"
            value={joinCode}
            onChange={(e) => setJoinCode(e.target.value)}
            placeholder={t("Paste invite code")}
            className="flex-[2] min-w-0 rounded-lg border border-line bg-paper px-3 py-1.5 text-[13px] outline-none focus:border-accent"
          />
          <input
            type="text"
            value={joinName}
            onChange={(e) => setJoinName(e.target.value)}
            placeholder={t("Your display name")}
            className="flex-1 min-w-0 rounded-lg border border-line bg-paper px-3 py-1.5 text-[13px] outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={!joinCode.trim() || joining}
            className="rounded-lg border border-accent text-accent px-3 py-1.5 text-[12.5px] font-medium hover:bg-accent/10 disabled:opacity-40"
          >
            {joining ? "…" : t("Join with invite code")}
          </button>
        </form>
        {joinMsg && <div className="mb-3 text-[12px] text-muted">{joinMsg}</div>}

        {!members ? (
          <div className="text-[13px] text-faint">{t("Loading…")}</div>
        ) : members.length === 0 ? (
          <div className="text-[13px] text-faint">
            {t("No team members yet. Invite colleagues to join your swarm.")}
          </div>
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-faint text-left border-b border-line">
                <th className="py-1.5 font-medium">{t("Name")}</th>
                <th className="py-1.5 font-medium">{t("Role")}</th>
                <th className="py-1.5 font-medium">{t("Status")}</th>
                <th className="py-1.5 font-medium">{t("Current task")}</th>
                <th className="py-1.5 font-medium w-[70px]" />
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.id} className="border-b border-line/50">
                  <td className="py-2">{m.name}</td>
                  <td className="py-2">
                    <select
                      value={m.role}
                      onChange={(e) =>
                        onChangeMemberRole(m.id, e.target.value as Member["role"])
                      }
                      className="rounded-md border border-line bg-paper px-1.5 py-0.5 text-[12px] outline-none focus:border-accent"
                    >
                      {roles.map((r) => (
                        <option key={r} value={r}>{t(r)}</option>
                      ))}
                    </select>
                  </td>
                  <td className="py-2">
                    {m.status === "online" ? (
                      <span className="text-ok">● {t("Online")}</span>
                    ) : m.status === "invited" ? (
                      <span className="text-accent">◌ {t("invited")}</span>
                    ) : (
                      <span className="text-faint">○ {t("Offline")}</span>
                    )}
                  </td>
                  <td className="py-2 text-muted">{m.current_task_group || "—"}</td>
                  <td className="py-2 text-right">
                    <button
                      onClick={() => onRemoveMember(m.id)}
                      className="rounded-md border border-line px-2 py-0.5 text-[11.5px] text-danger hover:bg-danger/10"
                      title={t("Remove")}
                    >
                      {t("Remove")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Agent 实例池 */}
      <div className="mt-4 rounded-xl2 border border-line bg-panel p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="text-[14px] font-semibold">{t("Agent Pool")}</div>
        </div>

        <form onSubmit={onAddAgent} className="mb-4 flex gap-2">
          <select
            value={newAgentRole}
            onChange={(e) => setNewAgentRole(e.target.value as AgentInstance["role"])}
            className="flex-1 min-w-0 rounded-lg border border-line bg-paper px-3 py-1.5 text-[13px] outline-none focus:border-accent"
          >
            {roles.map((r) => (
              <option key={r} value={r}>{t("Add Agent")} · {t(r)}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={savingAgent}
            className="rounded-lg bg-accent text-white px-3 py-1.5 text-[12.5px] font-medium disabled:opacity-40"
          >
            {savingAgent ? "…" : t("Scale up")}
          </button>
        </form>

        {!agents ? (
          <div className="text-[13px] text-faint">{t("Loading…")}</div>
        ) : agents.length === 0 ? (
          <div className="text-[13px] text-faint">
            {t("No Agent instances. Agent pool will be available in Phase 1.")}
          </div>
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-faint text-left border-b border-line">
                <th className="py-1.5 font-medium">ID</th>
                <th className="py-1.5 font-medium">{t("Role")}</th>
                <th className="py-1.5 font-medium">{t("State")}</th>
                <th className="py-1.5 font-medium">{t("Load")}</th>
                <th className="py-1.5 font-medium w-[70px]" />
              </tr>
            </thead>
            <tbody>
              {agents.map((a) => (
                <tr key={a.id} className="border-b border-line/50">
                  <td className="py-2 font-mono text-[12px]">{a.id}</td>
                  <td className="py-2">{t(a.role)}</td>
                  <td className="py-2">
                    <span className={
                      a.state === "idle" ? "text-faint" :
                      a.state === "working" ? "text-accent" : "text-danger"
                    }>
                      {t(a.state === "idle" ? "Idle" : a.state === "working" ? "Working" : "Fault")}
                    </span>
                  </td>
                  <td className="py-2">{Math.round(a.load * 100)}%</td>
                  <td className="py-2 text-right">
                    <button
                      onClick={() => onRemoveAgent(a.id)}
                      disabled={a.state === "working"}
                      className="rounded-md border border-line px-2 py-0.5 text-[11.5px] text-danger hover:bg-danger/10 disabled:opacity-40"
                      title={t("Remove")}
                    >
                      {t("Remove")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      </div>
    </div>
  );
}
