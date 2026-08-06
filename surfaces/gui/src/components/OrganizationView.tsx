import { useEffect, useState } from "react";
import { useT } from "../i18n";
import {
  getOrchestrateHistory,
  listKnowledge,
  listMemories,
  listSkills,
  listSwarmTemplates,
  rhythmForecast,
  type OrchestrationHistoryItem,
  type RhythmForecast,
} from "../api";
import {
  OrgAssetsCard,
  RhythmCard,
  TeamMemoryCard,
  TeamWorkspaceCard,
} from "./SettingsView";

/**
 * Organization page (home nav): the org's asset network at a glance.
 * Everything here also lives in Settings ▸ General — this page is the trust
 * surface: "your organization runs here, and here is what it remembers."
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
    return () => {
      alive = false;
    };
  }, []);

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
    <div className="max-w-4xl mx-auto px-6 py-6">
      <div className="mb-5">
        <h1 className="text-[22px] font-semibold tracking-tight flex items-center gap-2.5">
          <span className="text-[20px]">🏢</span>
          {t("Organization")}
        </h1>
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

      <OrgAssetsCard />
      <RhythmCard />
      <TeamMemoryCard />
      <TeamWorkspaceCard />
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
