import { useEffect, useState } from "react";
import { useT } from "../i18n";
import {
  createTaskGroup,
  dissolveTaskGroup,
  getInbox,
  getOrchestrateHistory,
  getTeam,
  listKnowledge,
  listMembers,
  listMemories,
  listSkills,
  listSwarmTemplates,
  listTaskGroups,
  rhythmForecast,
  transitionTaskGroup,
  type Member,
  type OrchestrationHistoryItem,
  type RhythmForecast,
  type TaskGroup,
  type TeamInfo,
} from "../api";
import {
  OrgAssetsCard,
  RhythmCard,
  TeamMemoryCard,
  TeamWorkspaceCard,
} from "./SettingsView";
import { SyncIndicator } from "./SyncIndicator";

const NEXT_STATE: Record<string, string[]> = {
  forming: ["active", "dissolved"],
  active: ["reviewing", "dissolved"],
  reviewing: ["active", "dissolved"],
  fault: ["active", "dissolved"],
  dissolved: [],
};

/**
 * Organization page (home nav): the org's asset network at a glance.
 * Everything here also lives in Settings ▸ General — this page is the trust
 * surface: "your organization runs here, and here is what it remembers."
 *
 * P2 (不再是占位符): Active Swarms 卡片支持:
 *   - 创建新蜂群 (goal → forming)
 *   - 状态 transition (forming → active → reviewing → dissolved)
 *   - 解散蜂群 (dissolve, 释放 agent + 归档到知识)
 */
export function OrganizationView() {
  const t = useT();
  const [stats, setStats] = useState<{
    knowledge: number;
    skills: number;
    templates: number;
    memories: number;
    runs: number;
    rhythm: RhythmForecast | null;
  } | null>(null);
  const [team, setTeam] = useState<TeamInfo | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [taskGroups, setTaskGroups] = useState<TaskGroup[]>([]);
  const [pendingInbox, setPendingInbox] = useState(0);

  // Create swarm form state
  const [newGoal, setNewGoal] = useState("");
  const [creating, setCreating] = useState(false);
  // Which swarm is having the dissolve-confirmation shown?
  const [dissolvingId, setDissolvingId] = useState<string | null>(null);
  // busy transitioning {groupId: true}
  const [busyGroups, setBusyGroups] = useState<Record<string, boolean>>({});

  const reloadGroups = () => {
    listTaskGroups().then((tg) => {
      setTaskGroups(tg.filter((g) => g.state !== "dissolved"));
    }).catch(() => setTaskGroups([]));
  };

  useEffect(() => {
    let alive = true;
    Promise.allSettled([
      listKnowledge(1, 0).then((r) => r.total ?? 0),
      listSkills().then((r) => r.skills?.length ?? 0),
      listSwarmTemplates().then((r) => r.templates?.length ?? 0),
      listMemories().then((r) => r.memory?.length ?? 0),
      getOrchestrateHistory().then((r) => (r.runs ?? []).length ?? 0),
      rhythmForecast().catch(() => null),
    ]).then(([k, s, tm, m, rn, rh]) => {
      if (!alive) return;
      setStats({
        knowledge: (k.status === "fulfilled" ? k.value : 0) as number,
        skills: (s.status === "fulfilled" ? s.value : 0) as number,
        templates: (tm.status === "fulfilled" ? tm.value : 0) as number,
        memories: (m.status === "fulfilled" ? m.value : 0) as number,
        runs: (rn.status === "fulfilled" ? rn.value : 0) as number,
        rhythm: rh.status === "fulfilled" ? rh.value : null,
      });
    });
    // Team data (Phase 0: gracefully empty when team module not yet loaded)
    getTeam().then((ti) => { if (alive) setTeam(ti); }).catch(() => {});
    listMembers().then((ms) => { if (alive) setMembers(ms); }).catch(() => {});
    listTaskGroups().then((tg) => { if (alive) setTaskGroups(tg.filter((g) => g.state !== "dissolved")); }).catch(() => {});
    getInbox().then((items) => { if (alive) setPendingInbox(items?.length ?? 0); }).catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const onCreateSwarm = async (e: React.FormEvent) => {
    e.preventDefault();
    const goal = newGoal.trim();
    if (!goal || creating) return;
    setCreating(true);
    try {
      await createTaskGroup({ goal });
      setNewGoal("");
    } finally {
      setCreating(false);
      reloadGroups();
    }
  };

  const onTransition = async (gid: string, next: string) => {
    if (busyGroups[gid]) return;
    setBusyGroups((b) => ({ ...b, [gid]: true }));
    try {
      if (next === "dissolved") {
        await dissolveTaskGroup(gid);
      } else {
        await transitionTaskGroup(gid, next);
      }
    } finally {
      setBusyGroups((b) => { const n = { ...b }; delete n[gid]; return n; });
      reloadGroups();
    }
  };

  const rhythmLabel = (r: string) =>
    r === "weekly" ? t("Weekly rhythm") : r === "daily" ? t("Daily rhythm") : r === "monthly" ? t("Monthly rhythm") : r === "irregular" ? t("Irregular — no dominant cadence yet") : r;

  const tiles = [
    { label: t("Knowledge"), value: stats?.knowledge, icon: "📚" },
    { label: t("Skills"), value: stats?.skills, icon: "🧩" },
    { label: t("Templates"), value: stats?.templates, icon: "🧬" },
    { label: t("Team memory"), value: stats?.memories, icon: "🧠" },
    { label: t("Swarm runs"), value: stats?.runs, icon: "🐝" },
  ];

  return (
    <div className="h-full overflow-y-auto">
    <div className="max-w-4xl mx-auto px-6 py-6">
      <div className="mb-5">
        <h1 className="text-[22px] font-semibold tracking-tight flex items-center gap-2.5">
          <span className="text-[20px]">🏢</span>
          {t("Organization")}
        </h1>
        <div className="flex items-center gap-3 mt-2">
          <SyncIndicator />
          {team && (
            <span className="text-[12px] text-faint">
              {team.name} · {t("Members")} {team.member_count} · {t("Online")} {team.online_count}
            </span>
          )}
        </div>
        <p className="text-[13px] text-muted mt-1.5 leading-relaxed">
          {t(
            "Your organizational asset network — knowledge, skills, templates, team memory and swarm runs. Everything your swarm has learned lives here and gets reused automatically.",
          )}
        </p>
        {stats?.rhythm && (
          <div className="inline-flex items-center gap-2 mt-2 text-[12px] text-faint border border-line rounded-full px-3 py-1">
            <span>🗓</span>
            {stats.rhythm.period_days > 0
              ? t("Period detected: {n} days", { n: stats.rhythm.period_days })
              : t("No period detected yet")}
            <span className="text-muted">·</span>
            {rhythmLabel(stats.rhythm.rhythm)}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2.5 mb-5">
        {tiles.map((tile) => (
          <div
            key={tile.label}
            className="rounded-xl2 border border-line bg-panel px-3.5 py-3 flex items-center gap-2.5"
            data-testid={`org-stat-${tile.label.toLowerCase().replace(/\s+/g, "-")}`}
          >
            <span className="text-[16px]">{tile.icon}</span>
            <div className="min-w-0">
              <div className="text-[17px] font-semibold leading-tight">
                {tile.value ?? "…"}
              </div>
              <div className="text-[11px] text-faint truncate">{tile.label}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Active Swarms — interactive */}
      <div className="rounded-xl2 border border-line bg-panel p-4 mb-4">
        <div className="text-[14px] font-semibold mb-2.5 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span>🐝</span> {t("Active Swarms")}
          </div>
        </div>

        {/* Create new swarm form */}
        <form onSubmit={onCreateSwarm} className="mb-4 flex gap-2">
          <input
            type="text"
            value={newGoal}
            onChange={(e) => setNewGoal(e.target.value)}
            placeholder={t("Description of what this swarm should achieve")}
            className="flex-1 min-w-0 rounded-lg border border-line bg-paper px-3 py-2 text-[13px] outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={!newGoal.trim() || creating}
            className="rounded-lg bg-accent text-white px-3 py-2 text-[12.5px] font-medium disabled:opacity-40 shrink-0"
          >
            {creating ? "…" : t("Create new swarm")}
          </button>
        </form>

        {taskGroups.length === 0 ? (
          <div className="text-[13px] text-faint">{t("No active swarms.")}</div>
        ) : (
          <div className="space-y-2.5">
            {taskGroups.map((g) => {
              const groupId = g.group_id;
              const busy = busyGroups[groupId];
              const nextStates = NEXT_STATE[g.state] ?? [];
              return (
                <div key={groupId} className="border border-line rounded-lg px-3 py-2">
                  <div className="flex items-center gap-2 text-[13px]">
                    <span className={
                      "shrink-0 px-1.5 py-px rounded text-[10.5px] " +
                      (g.state === "active" ? "bg-accentSoft text-accent" :
                       g.state === "forming" ? "bg-warnSoft text-warnInk" :
                       "bg-faint/20 text-faint")
                    }>
                      {busy ? t("Dissolving…") : g.state}
                    </span>
                    <span className="truncate text-ink flex-1 min-w-0">{g.goal}</span>
                    <span className="text-faint shrink-0 font-mono text-[10.5px]">
                      {g.member_ids.length} {t("Members")}
                    </span>
                  </div>
                  {/* State transition actions */}
                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    {nextStates.filter((s) => s !== "dissolved").map((next) => (
                      <button
                        key={next}
                        onClick={() => onTransition(groupId, next)}
                        disabled={busy}
                        className="rounded-md border border-line px-2 py-0.5 text-[11.5px] hover:bg-paper disabled:opacity-40"
                      >
                        → {next}
                      </button>
                    ))}
                    {nextStates.includes("dissolved") && dissolvingId !== groupId && (
                      <button
                        onClick={() => setDissolvingId(groupId)}
                        disabled={busy}
                        className="rounded-md border border-line px-2 py-0.5 text-[11.5px] text-danger hover:bg-danger/10 disabled:opacity-40"
                      >
                        {t("Dissolve swarm")}
                      </button>
                    )}
                    {dissolvingId === groupId && (
                      <>
                        <span className="text-[11.5px] text-danger">
                          {t("Dissolve this swarm?")}
                        </span>
                        <button
                          onClick={() => onTransition(groupId, "dissolved")}
                          disabled={busy}
                          className="rounded-md bg-danger text-white px-2 py-0.5 text-[11.5px] disabled:opacity-40"
                        >
                          {busy ? "…" : t("Dissolve")}
                        </button>
                        <button
                          onClick={() => setDissolvingId(null)}
                          disabled={busy}
                          className="rounded-md border border-line px-2 py-0.5 text-[11.5px] disabled:opacity-40"
                        >
                          ×
                        </button>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Team Members snapshot */}
      <div className="rounded-xl2 border border-line bg-panel p-4 mb-4">
        <div className="text-[14px] font-semibold mb-2.5 flex items-center gap-2">
          <span>👥</span> {t("Team Members")}
        </div>
        {members.length === 0 ? (
          <div className="text-[13px] text-faint">
            {t("No team members yet. Invite colleagues to join your swarm.")}
          </div>
        ) : (
          <div className="space-y-1.5">
            {members.slice(0, 5).map((m) => (
              <div key={m.id} className="flex items-center gap-2 text-[13px]">
                <span className={m.status === "online" ? "text-ok" : "text-faint"}>
                  {m.status === "online" ? "●" : "○"}
                </span>
                <span className="truncate text-ink">{m.name}</span>
                <span className="text-faint text-[12px]">({t(m.role)})</span>
                <span className="ml-auto text-faint text-[12px] truncate max-w-[200px]">
                  {m.current_task_group || "—"}
                </span>
              </div>
            ))}
            {members.length > 5 && (
              <div className="text-[12px] text-faint">+ {members.length - 5}</div>
            )}
          </div>
        )}
      </div>

      {/* Pending Approvals */}
      <div className="rounded-xl2 border border-line bg-panel p-4 mb-4">
        <div className="text-[14px] font-semibold mb-2.5 flex items-center gap-2">
          <span>📋</span> {t("Pending Approvals")}
        </div>
        {pendingInbox === 0 ? (
          <div className="text-[13px] text-faint">{t("No pending approvals.")}</div>
        ) : (
          <div className="text-[13px] text-accent">
            {pendingInbox} {t("items need your attention")}
          </div>
        )}
      </div>

      <OrgAssetsCard />
      <RhythmCard />
      <TeamMemoryCard />
      <TeamWorkspaceCard />
      </div>
    </div>
  );
}

export function OrganizationRuns({ items }: { items: OrchestrationHistoryItem[] }) {
  const t = useT();
  if (!items || items.length === 0) return null;
  const recent = items.slice(0, 8);
  return (
    <div className="rounded-xl2 border border-line bg-panel p-4 mb-4" data-testid="org-runs">
      <div className="text-[12.5px] font-medium text-ink mb-2">{t("Recent swarm runs")}</div>
      <div className="space-y-1">
        {recent.map((r) => (
          <div key={r.run_id} className="flex items-center gap-2 text-[12px]">
            <span
              className={
                "shrink-0 px-1.5 py-px rounded text-[10.5px] " +
                (r.status === "completed"
                  ? "bg-ok/15 text-ok"
                  : r.status === "running"
                    ? "bg-accentSoft text-accent"
                    : "bg-warnSoft text-warnInk")
              }
            >
              {r.status}
            </span>
            <span className="truncate text-ink">{(r.intent || "").slice(0, 80)}</span>
            <span className="ml-auto text-faint shrink-0 font-mono text-[10.5px]">
              {r.run_id?.slice(0, 12)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
