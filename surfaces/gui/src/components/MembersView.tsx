import { useEffect, useState } from "react";
import { useT } from "../i18n";
import { listMembers, listAgents, type Member, type AgentInstance } from "../api";

/**
 * Members page (team swarm section): member roster + Agent instance pool.
 * Phase 0: read-only display with graceful empty-state (single-machine mode
 * shows "no team members yet"). Phase 1 will add add/remove/scale controls.
 */
export function MembersView() {
  const t = useT();
  const [members, setMembers] = useState<Member[] | null>(null);
  const [agents, setAgents] = useState<AgentInstance[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

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
        <div className="text-[14px] font-semibold mb-3">{t("Team Members")}</div>
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
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.id} className="border-b border-line/50">
                  <td className="py-2">{m.name}</td>
                  <td className="py-2">{t(m.role)}</td>
                  <td className="py-2">
                    <span className={m.status === "online" ? "text-ok" : "text-faint"}>
                      {m.status === "online" ? "● " : "○ "}
                      {t(m.status === "online" ? "Online" : "Offline")}
                    </span>
                  </td>
                  <td className="py-2 text-muted">{m.current_task_group || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Agent 实例池 */}
      <div className="mt-4 rounded-xl2 border border-line bg-panel p-4">
        <div className="text-[14px] font-semibold mb-3">{t("Agent Pool")}</div>
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
