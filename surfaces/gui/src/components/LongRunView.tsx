import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  archiveLongrunAlerts,
  exportLongrunAudit,
  exportLongrunChannelHealth,
  getLongrunAggregationStats,
  getLongrunAlertAggregations,
  getLongrunAlertChannels,
  getLongrunAlerts,
  getLongrunAlertSettings,
  getLongrunArchivedAlerts,
  getLongrunAudit,
  getLongrunChannelHealth,
  getLongrunCheckpointDetail,
  getLongrunCheckpoints,
  getLongrunHealth,
  getLongrunProbeSchedule,
  getLongrunStorage,
  getLongrunTelemetry,
  getLongrunWeekCompare,
  getMeshMode,
  getPheromoneStatus,
  probeLongrunAlertChannels,
  pruneProbeHistory,
  restoreLongrunArchivedAlert,
  restoreLongrunCheckpoint,
  rollbackLongrunCheckpoint,
  runLongrunMaintenance,
  setLongrunAlertChannels,
  setLongrunAlertSettings,
  setLongrunProbeSchedule,
  setMeshMode as setMeshModeApi,
  testLongrunAlertChannels,
  type AggregationStats,
  type AlertChannelConfig,
  type AlertSettings,
  type ChannelHealth,
  type LongrunAlert,
  type LongrunAlertAggregation,
  type LongrunAuditEntry,
  type LongrunCheckpointDetail,
  type LongrunCheckpointSession,
  type LongrunHealth,
  type LongrunStorage,
  type PheromoneStatus,
  type LongrunTelemetry,
  type ProbeSchedule,
  type WeekCompare,
} from "../api";
import { useT } from "../i18n";

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="flex items-center justify-between mb-2.5">
        <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold">{title}</div>
        {right}
      </div>
      {children}
    </div>
  );
}

/** A: 心跳总览 */
function HealthPanel({ health }: { health: LongrunHealth | null }) {
  const t = useT();
  if (!health) return <div className="text-[12px] text-faint">{t("Loading…")}</div>;
  const hb = health.heartbeat ?? {};
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
      <div className="rounded-lg border border-line bg-paper px-3 py-2">
        <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Heartbeat tasks")}</div>
        <div className="text-[20px] font-semibold" data-testid="health-heartbeat-tasks">{hb.tasks ?? 0}</div>
        <div className="text-[11px] text-muted">
          {(hb.alive ?? []).length} alive · {(hb.unhealthy ?? []).length} stalled
        </div>
      </div>
      <div className="rounded-lg border border-line bg-paper px-3 py-2">
        <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Automations")}</div>
        <div className="text-[20px] font-semibold" data-testid="health-automations">{health.automation?.enabled ?? 0}/{health.automation?.total ?? 0}</div>
        <div className="text-[11px] text-muted">{health.automation?.failed_recent ?? 0} failed · {(health.automation?.run_count_total ?? 0)} runs</div>
      </div>
      <div className="rounded-lg border border-line bg-paper px-3 py-2">
        <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Wakes")}</div>
        <div className="text-[20px] font-semibold" data-testid="health-wakes">{health.wakes?.pending ?? 0}</div>
        <div className="text-[11px] text-muted">{health.wakes?.due ?? 0} due now</div>
      </div>
      <div className="rounded-lg border border-line bg-paper px-3 py-2">
        <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Stall detection")}</div>
        <div className="text-[20px] font-semibold" data-testid="health-detection">
          {health.detection_time_seconds != null ? `${Math.round(health.detection_time_seconds)}s` : "—"}
        </div>
        <div className="text-[11px] text-muted">{t("within one tick")}</div>
      </div>
      {(hb.unhealthy ?? []).length > 0 && (
        <div className="col-span-full text-[11.5px] text-amber-600">
          ⚠ {t("Stalled tasks")}: {(hb.unhealthy ?? []).join(", ")}
        </div>
      )}
    </div>
  );
}

/** B: 降级轨迹 + 收敛历史 */
function TelemetryPanel({ telemetry }: { telemetry: LongrunTelemetry | null }) {
  const t = useT();
  const deg = telemetry?.degradations ?? [];
  const conv = telemetry?.convergence_history ?? [];
  const actionLabel: Record<string, string> = {
    continue: "retry", downgrade_model: "model↓", narrow_scope: "scope↓",
    sync_mode: "sync", checkpoint_pause: "pause", alert_archive: "archive",
  };
  return (
    <div className="space-y-3">
      <div>
        <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">
          {t("Degradation trail")} ({deg.length})
        </div>
        {deg.length === 0 ? (
          <div className="text-[11.5px] text-faint">{t("No degradations recorded")}</div>
        ) : (
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {deg.slice(-15).map((d, i) => (
              <div key={i} className="flex items-center gap-2 text-[11px] font-mono">
                <span className="text-faint shrink-0">L{d.level}</span>
                <span className="text-muted shrink-0 w-16">{actionLabel[d.action] ?? d.action}</span>
                <span className="text-faint w-24 truncate">{d.task_id}</span>
                <span className="text-faint">{(d.fidelity ?? 0) * 100}%</span>                {d.error && <span className="text-faint truncate flex-1">{d.error}</span>}
              </div>
            ))}
          </div>
        )}
      </div>
      <div>
        <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">
          {t("Convergence history")} ({conv.length})
        </div>
        {conv.length === 0 ? (
          <div className="text-[11.5px] text-faint">{t("No convergence reports yet")}</div>
        ) : (
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {conv.slice(-10).map((c, i) => (
              <div key={i} className="text-[11px] font-mono flex items-center gap-2">
                <span className={c.report?.converged ? "text-emerald-600" : "text-amber-600"}>
                  {c.report?.converged ? "✓" : "…"}
                </span>
                <span className="text-muted truncate flex-1">{c.intent}</span>
                <span className="text-faint shrink-0">{c.report?.iterations ?? 0} 轮</span>
                <span className="text-faint shrink-0">|λ₂|={c.report?.gap?.toFixed(3)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** C: 检查点浏览器 */
function CheckpointPanel({
  sessions,
  detail,
  onSelect,
  onRestore,
  onRestoreApply,
  onRollback,
  restoreBusy,
}: {
  sessions: LongrunCheckpointSession[];
  detail: LongrunCheckpointDetail | null;
  onSelect: (sid: string) => void;
  onRestore: (sid: string) => void;
  onRestoreApply: (sid: string) => void;
  onRollback: (sid: string) => void;
  restoreBusy: boolean;
}) {
  const t = useT();
  const granularityLabel: Record<string, string> = {
    full: "full", messages: "msgs", summary: "sum", diff: "diff", final: "final",
  };
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      <div>
        <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">
          {t("Sessions")} ({sessions.length})
        </div>
        <div className="space-y-1 max-h-64 overflow-y-auto">
          {sessions.map((s) => (
            <button
              key={s.session_id}
              onClick={() => onSelect(s.session_id)}
              className="w-full flex items-center gap-2 rounded-lg border border-line bg-paper px-2.5 py-1.5 text-left hover:border-lineStrong"
            >
              <span className="text-[11.5px] truncate flex-1">{s.title}</span>
              <span className="text-[10.5px] text-faint shrink-0">{s.message_count ?? 0} msgs</span>
              {s.archived && <span className="text-[10px] text-emerald-600 shrink-0">archived</span>}
            </button>
          ))}
        </div>
      </div>
      <div>
        <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">{t("Checkpoint chain")}</div>
        {!detail ? (
          <div className="text-[11.5px] text-faint">{t("Select a session to inspect its checkpoint chain")}</div>
        ) : !detail.ok ? (
          <div className="text-[11.5px] text-amber-600">{detail.error}</div>
        ) : (
          <div className="space-y-1 max-h-64 overflow-y-auto">
            {(detail.chain ?? []).slice(-12).map((cp) => (
              <div key={cp.seq} className="flex items-center gap-2 text-[11px] font-mono">
                <span className="text-faint shrink-0">#{cp.seq}</span>
                <span className="text-muted shrink-0">L{cp.n_layer}</span>
                <span className="text-faint shrink-0 w-10">{granularityLabel[cp.granularity] ?? cp.granularity}</span>
                <span className="text-faint truncate">
                  {new Date(cp.created_at * 1000).toLocaleString()}
                </span>
              </div>
            ))}
            <div className="text-[11px] text-muted pt-1">
              {detail.latest_restorable ? "✓ restorable" : "— no restorable state"} · {detail.count ?? 0} total
            </div>
            {detail.session_id && detail.latest_restorable && (
              <div className="mt-1.5 flex items-center gap-1.5">
                <button
                  className="text-[11px] rounded-lg border border-line bg-paper px-2.5 py-1 hover:border-emerald-500/60 hover:text-emerald-600 disabled:opacity-50"
                  onClick={() => onRestore(detail.session_id!)}
                  disabled={restoreBusy}
                  data-testid="restore-drill"
                >
                  {restoreBusy ? "Restoring…" : "▶ Restore drill (read-only)"}
                </button>
                <button
                  className="text-[11px] rounded-lg border border-amber-500/40 bg-paper px-2.5 py-1 hover:border-amber-500 hover:text-amber-600 disabled:opacity-50"
                  onClick={() => {
                    if (window.confirm(`Apply checkpoint to session ${detail.session_id}? This overwrites its current messages.`)) {
                      onRestoreApply(detail.session_id!);
                    }
                  }}
                  disabled={restoreBusy}
                  data-testid="restore-apply"
                >
                  {restoreBusy ? "Restoring…" : "⚡ Apply restore (write)"}
                </button>
                <button
                  className="text-[11px] rounded-lg border border-rose-500/40 bg-paper px-2.5 py-1 hover:border-rose-500 hover:text-rose-600 disabled:opacity-50"
                  onClick={() => {
                    if (window.confirm(`Roll back session ${detail.session_id} to its pre-restore backup?`)) {
                      onRollback(detail.session_id!);
                    }
                  }}
                  disabled={restoreBusy}
                  data-testid="rollback-btn"
                >
                  {restoreBusy ? "Restoring…" : "↩ Rollback to backup"}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** D: 存储健康 */
function StoragePanel({ storage }: { storage: LongrunStorage | null }) {
  const t = useT();
  if (!storage) return <div className="text-[12px] text-faint">{t("Loading…")}</div>;
  // 防御: 后端返回异常结构 (如 404/500 的 {detail}) 时兜底为全零,
  // 避免 storage.sessions 为 undefined 导致渲染崩溃。
  const s = storage.sessions ?? {
    count: 0,
    jsonl_total_bytes: 0,
    archived_sessions: 0,
    archive_bytes: 0,
    top: [],
  };
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Sessions")}</div>
          <div className="text-[20px] font-semibold">{s.count}</div>
        </div>
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("JSONL size")}</div>
          <div className="text-[20px] font-semibold">{fmtBytes(s.jsonl_total_bytes)}</div>
        </div>
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Archived")}</div>
          <div className="text-[20px] font-semibold">{s.archived_sessions}</div>
          <div className="text-[11px] text-muted">{fmtBytes(s.archive_bytes)}</div>
        </div>
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Memory items")}</div>
          <div className="text-[20px] font-semibold">{storage.memory?.count ?? 0}</div>
          <div className="text-[11px] text-muted">{(storage.memory?.stale ?? 0)} stale</div>
        </div>
      </div>
      {s.top.length > 0 && (
        <div>
          <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">{t("Largest sessions")}</div>
          <div className="space-y-1">
            {s.top.slice(0, 8).map((x) => (
              <div key={x.session_id} className="flex items-center gap-2 text-[11px] font-mono">
                <span className="text-muted truncate flex-1">{x.session_id}</span>
                <span className="text-faint shrink-0">{fmtBytes(x.jsonl_bytes)}</span>
                {x.archived && <span className="text-[10px] text-emerald-600 shrink-0">✓</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** QunMesh M1/M2: 信息素总线四信道总览 (load/task/result/risk)。 */
function PheromonePanel({
  status,
  meshMode,
  onModeChange,
}: {
  status: PheromoneStatus | null;
  meshMode: string;
  onModeChange: (m: string) => void;
}) {
  const t = useT();
  if (!status) return <div className="text-[12px] text-faint">{t("Loading…")}</div>;
  const channels = status.channels ?? {};
  const card = (name: string, label: string) => {
    const c = channels[name] ?? { signals: 0, intensity: 0 };
    return (
      <div className="rounded-lg border border-line bg-paper px-3 py-2">
        <div className="text-[10.5px] uppercase tracking-wide text-faint">{t(label)}</div>
        <div className="text-[20px] font-semibold">{c.signals ?? 0}</div>
        <div className="text-[11px] text-muted">Σ {c.intensity ?? 0}</div>
      </div>
    );
  };
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span
          data-testid="pheromone-bus-badge"
          className={
            "text-[10px] px-1.5 py-0.5 rounded " +
            (status.bus === "stigmergy"
              ? "bg-emerald-600/10 text-emerald-700"
              : "bg-paper text-faint border border-line")
          }
        >
          {status.bus === "stigmergy" ? t("StigmergyBus") : t("Legacy field")}
        </span>
        <span className="text-[11px] text-muted">
          {t("load")} ≈ {status.total_load ?? 0}
        </span>
        <span className="ml-auto flex items-center gap-1">
          {(["off", "serial", "hybrid", "full"] as const).map((m) => (
            <button
              key={m}
              data-testid={`mesh-mode-${m}`}
              onClick={() => m !== meshMode && onModeChange(m)}
              className={
                "text-[10px] px-1.5 py-0.5 rounded font-mono " +
                (m === meshMode
                  ? "bg-ink text-paper"
                  : "border border-line text-muted hover:opacity-80")
              }
            >
              {m}
            </button>
          ))}
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        {card("load", "Load signals")}
        <div data-testid="pheromone-task-signals">{card("task", "Task claims")}</div>
        {card("result", "Result notices")}
        {card("risk", "Risk gradient")}
      </div>
      {status.topology && (
        <div className="text-[11px] space-y-1" data-testid="mesh-topology">
          <div>
            <span className="text-faint uppercase tracking-wide text-[10.5px]">
              {t("Mesh λ₂ (algebraic connectivity)")}:
            </span>{" "}
            <span className="font-mono font-semibold">
              {status.topology.lambda2 ?? 0}
            </span>
            <span className="text-muted">
              {" "}
              · {status.topology.agents?.length ?? 0} {t("agents")} ·{" "}
              {status.topology.edges ?? 0} {t("edges")}
            </span>
          </div>
          {(status.topology.hotspots?.length ?? 0) > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10.5px] uppercase tracking-wide text-faint">
                {t("Hotspots")}:
              </span>
              {(status.topology.hotspots ?? []).map((h) => (
                <span
                  key={h}
                  className="text-[10px] px-1.5 py-0.5 rounded bg-amber-600/10 text-amber-700 font-mono"
                >
                  {h}
                </span>
              ))}
              {(status.topology.migrations ?? []).map((m) => (
                <span key={m.hotspot} className="text-[10px] text-muted font-mono">
                  {m.hotspot} → {m.target}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** ① 告警历史: 已持久化的 7x24 告警 (可回溯, 时间范围筛选 + 频率图)。 */
function AlertHistory({
  alerts,
  rangeLabel,
  onRange,
}: {
  alerts: LongrunAlert[];
  rangeLabel: string;
  onRange: (r: "1h" | "24h" | "7d" | "all") => void;
}) {
  const t = useT();
  // 告警频率图: 按小时聚合 (最近 24h)。
  const hours = useMemo(() => {
    const m = new Map<number, number>();
    for (const a of alerts) {
      const h = Math.floor(a.ts / 3600) * 3600;
      m.set(h, (m.get(h) ?? 0) + 1);
    }
    return Array.from(m.entries())
      .sort((a, b) => a[0] - b[0])
      .slice(-24);
  }, [alerts]);
  const maxCount = Math.max(1, ...hours.map(([, n]) => n));
  const ranges: { key: "1h" | "24h" | "7d" | "all"; label: string }[] = [
    { key: "1h", label: "1h" },
    { key: "24h", label: "24h" },
    { key: "7d", label: "7d" },
    { key: "all", label: "全部" },
  ];
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1.5">
        {ranges.map((r) => (
          <button
            key={r.key}
            className={
              "text-[10px] px-1.5 py-0.5 rounded " +
              (rangeLabel === r.key ? "bg-ink text-paper" : "text-faint hover:text-ink")
            }
            onClick={() => onRange(r.key)}
            data-testid={`alert-range-${r.key}`}
          >
            {r.label}
          </button>
        ))}
      </div>
      {hours.length >= 2 && (
        <div className="flex items-end gap-1 h-12" data-testid="alert-frequency-chart">
          {hours.map(([h, n]) => (
            <div key={h} className="flex-1 flex flex-col items-center justify-end gap-0.5" title={new Date(h * 1000).toLocaleString()}>
              <span className="text-[8px] text-faint">{n}</span>
              <div
                className="w-full rounded-t bg-rose-400/70"
                style={{ height: `${Math.max(2, (n / maxCount) * 36)}px` }}
              />
            </div>
          ))}
        </div>
      )}
      <div className="space-y-1 max-h-40 overflow-y-auto">
        {alerts.length === 0 ? (
          <div className="text-[11.5px] text-faint">{t("No alerts recorded")}</div>
        ) : (
          alerts.slice(0, 12).map((a) => (
            <div key={a.id} className="flex items-start gap-2 text-[11px] font-mono">
              <span className="text-amber-600 shrink-0">⚠</span>
              <span className="text-faint shrink-0">
                {new Date(a.ts * 1000).toLocaleTimeString()}
              </span>
              <span className="text-muted truncate flex-1">{a.message}</span>
              {a.task_id && <span className="text-faint shrink-0">{a.task_id}</span>}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

/** ③ 告警聚合: 同任务连续卡死合并为持续告警 (次数/时长/解决/静默状态)。 */
function AlertAggregations({
  aggs,
  stats,
  weekCompare,
}: {
  aggs: LongrunAlertAggregation[];
  stats: AggregationStats | null;
  weekCompare: WeekCompare | null;
}) {
  const t = useT();
  const now = Date.now() / 1000;
  const days = stats?.days ?? [];
  const maxDay = Math.max(1, ...days.map((d) => d.alerts));
  const wc = weekCompare;
  const maxWc = Math.max(1, ...(wc?.this_week ?? []), ...(wc?.last_week ?? []));
  return (
    <div className="space-y-2">
      {/* ② 聚合趋势图: 每日告警数柱状。 */}
      {days.length >= 2 && (
        <div>
          <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1">
            {t("Daily alert trend")}
          </div>
          <div className="flex items-end gap-1 h-12" data-testid="alert-trend-chart">
            {days.map((d, i) => (
              <div key={i} className="flex-1 flex flex-col items-center justify-end gap-0.5" title={`${d.alerts} alerts`}>
                {d.alerts > 0 && <span className="text-[8px] text-faint">{d.alerts}</span>}
                <div
                  className="w-full rounded-t bg-rose-400/70"
                  style={{ height: `${Math.max(2, (d.alerts / maxDay) * 36)}px` }}
                />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ③ 跨周对比: 本周 vs 上周每日告警 (双柱)。 */}
      {wc && wc.ok && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <div className="text-[10.5px] uppercase tracking-wide text-faint">
              {t("Week compare")} (本周 {wc.total_this} vs 上周 {wc.total_last})
            </div>
            <span
              className={
                "text-[10.5px] font-semibold " +
                (wc.delta_pct <= 0 ? "text-emerald-600" : "text-rose-600")
              }
            >
              {wc.delta_pct > 0 ? "▲" : "▼"} {Math.abs(wc.delta_pct).toFixed(1)}%
            </span>
          </div>
          <div className="flex items-end gap-1 h-12" data-testid="week-compare-chart">
            {wc.labels.map((label, i) => (
              <div key={i} className="flex-1 flex items-end gap-0.5 justify-center" title={`${label}: 本周${wc.this_week[i]} / 上周${wc.last_week[i]}`}>
                <div
                  className="w-1/2 rounded-t bg-rose-400/70"
                  style={{ height: `${Math.max(2, (wc.this_week[i] / maxWc) * 36)}px` }}
                />
                <div
                  className="w-1/2 rounded-t bg-slate-400/50"
                  style={{ height: `${Math.max(2, (wc.last_week[i] / maxWc) * 36)}px` }}
                />
              </div>
            ))}
          </div>
          <div className="flex items-center gap-3 text-[9px] text-faint mt-1">
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-rose-400/70 inline-block" /> {t("this week")}</span>
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-slate-400/50 inline-block" /> {t("last week")}</span>
          </div>
        </div>
      )}
      {aggs.length === 0 ? (
        <div className="text-[11.5px] text-faint">{t("No active alert aggregations")}</div>
      ) : (
        <div className="space-y-1">
          {aggs.slice(0, 8).map((a) => {
            const durationH = ((a.updated_at - a.started_at) / 3600).toFixed(1);
            const silenced = a.silenced_until != null && now < a.silenced_until;
            return (
              <div
                key={a.id}
                className="flex items-center gap-2 text-[11px] font-mono rounded-lg border border-line bg-paper px-2 py-1"
              >
                <span className={a.resolved ? "text-emerald-600" : "text-amber-600"}>
                  {a.resolved ? "✓" : "⚠"}
                </span>
                <span className="text-muted truncate flex-1">{a.task_id}</span>
                <span className="text-faint shrink-0">×{a.count ?? 0}</span>
                <span className="text-faint shrink-0">{durationH}h</span>
                {silenced && (
                  <span className="text-[10px] text-sky-600 shrink-0" data-testid={`silenced-${a.task_id}`}>
                    🔕 静默中
                  </span>
                )}
                <span className={"text-[10px] shrink-0 " + (a.resolved ? "text-emerald-600" : "text-amber-600")}>
                  {a.resolved ? t("resolved") : t("active")}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** ③ 操作审计列表 (回滚/恢复/渠道变更等)。 */
function AuditLog({
  audit,
  onExport,
}: {
  audit: LongrunAuditEntry[];
  onExport: (format: "csv" | "json") => void;
}) {
  const t = useT();
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <div className="text-[10.5px] uppercase tracking-wide text-faint">
          {t("Operation audit")} ({audit.length})
        </div>
        <div className="flex items-center gap-1">
          <button
            className="text-[10px] px-1.5 py-0.5 rounded border border-line hover:border-lineStrong"
            onClick={() => onExport("csv")}
            data-testid="audit-export-csv"
          >
            CSV
          </button>
          <button
            className="text-[10px] px-1.5 py-0.5 rounded border border-line hover:border-lineStrong"
            onClick={() => onExport("json")}
            data-testid="audit-export-json"
          >
            JSON
          </button>
        </div>
      </div>
      <div className="space-y-1 max-h-48 overflow-y-auto">
        {audit.length === 0 ? (
          <div className="text-[11.5px] text-faint">{t("No audit records yet")}</div>
        ) : (
          audit.slice(0, 12).map((a) => (
            <div key={a.id} className="flex items-start gap-2 text-[11px] font-mono">
              <span className="text-faint shrink-0">{new Date(a.ts * 1000).toLocaleString()}</span>
              <span className="text-muted truncate flex-1">{a.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

/** ① 告警多渠道通知配置 (邮件/Telegram/飞书/钉钉/企业微信)。 */
function AlertChannelsPanel({
  channels,
  enabled,
  onSave,
  onTest,
  onProbe,
  onExportProbe,
  busy,
  probeResults,
  health,
}: {
  channels: Record<string, AlertChannelConfig>;
  enabled: string[];
  onSave: (channels: Record<string, Partial<AlertChannelConfig>>) => void;
  onTest: () => void;
  onProbe: () => void;
  onExportProbe: (format: "csv" | "json") => void;
  busy: boolean;
  probeResults: Record<string, { ok: boolean; ms?: number; error?: string }> | null;
  health: Record<string, ChannelHealth> | null;
}) {
  const t = useT();
  const [draft, setDraft] = useState<Record<string, AlertChannelConfig>>(channels);
  useEffect(() => setDraft(channels), [channels]);
  const channelLabel: Record<string, string> = {
    email: "📧 邮件",
    telegram: "✈️ Telegram",
    feishu: "💬 飞书",
    dingtalk: "🔔 钉钉",
    wecom: "🏢 企业微信",
  };
  const set = (key: string, field: string, value: string | boolean | string[]) =>
    setDraft((prev) => ({
      ...prev,
      [key]: { ...(prev[key] ?? { enabled: false }), [field]: value },
    }));
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {Object.entries(channelLabel).map(([key, label]) => {
          const cfg = draft[key] ?? { enabled: false };
          return (
            <div key={key} className="rounded-lg border border-line bg-paper p-2.5">
              <label className="flex items-center gap-2 text-[12px] font-medium mb-1.5">
                <input
                  type="checkbox"
                  checked={Boolean(cfg.enabled)}
                  onChange={(e) => set(key, "enabled", e.target.checked)}
                  data-testid={`channel-toggle-${key}`}
                />
                {label}
                {enabled.includes(key) && (
                  <span className="text-[9px] text-emerald-600 ml-auto">✓ 启用中</span>
                )}
              </label>
              {/* ① 渠道健康分徽标 + 历史趋势 (按 rating 降级提示)。 */}
              {health?.[key] && (
                <div className="flex items-center gap-2 mb-1.5" data-testid={`channel-health-${key}`}>
                  <span
                    className={
                      "text-[10px] font-semibold px-1.5 py-0.5 rounded " +
                      (health[key].rating === "good"
                        ? "bg-emerald-500/10 text-emerald-600"
                        : health[key].rating === "warn"
                          ? "bg-amber-500/10 text-amber-600"
                          : "bg-rose-500/10 text-rose-600")
                    }
                  >
                    健康 {health[key].health_score}
                  </span>
                  <span className="text-[9px] text-faint">
                    {health[key].ok_count}/{health[key].total} 成功
                    {health[key].avg_ms != null ? ` · ${health[key].avg_ms}ms` : ""}
                  </span>
                  {health[key].rating !== "good" && (
                    <span
                      className={
                        "text-[9px] font-semibold " +
                        (health[key].rating === "warn" ? "text-amber-600" : "text-rose-600")
                      }
                      data-testid={`channel-degrade-${key}`}
                    >
                      {health[key].rating === "warn" ? "⚠ 需关注" : "⛔ 严重"}
                    </span>
                  )}
                  {health[key].trend.length >= 2 && (
                    <span className="flex items-end gap-0.5 h-3">
                      {health[key].trend.slice(-10).map((p, i) => (
                        <span
                          key={i}
                          className={"w-1 rounded-sm " + (p.ok ? "bg-emerald-500/70" : "bg-rose-500/70")}
                          style={{ height: p.ok ? "100%" : "40%" }}
                        />
                      ))}
                    </span>
                  )}
                </div>
              )}
              <div className="flex items-center gap-2 mb-1.5 text-[10px] text-faint">
                <span>{t("Levels")}:</span>
                {["critical", "warning", "info"].map((lv) => {
                  const levels = (cfg.levels as string[]) ?? [];
                  const on = levels.includes(lv);
                  return (
                    <label key={lv} className="flex items-center gap-0.5 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...levels, lv]
                            : levels.filter((x) => x !== lv);
                          set(key, "levels", next);
                        }}
                        data-testid={`channel-level-${key}-${lv}`}
                      />
                      {lv}
                    </label>
                  );
                })}
              </div>
              {/* ② 每渠道归档保留天数 (空 = 用全局默认)。 */}
              <div className="flex items-center gap-1.5 mb-1.5">
                <span className="text-[10px] text-faint">{t("Archive days")}:</span>
                <input
                  type="number"
                  min={1}
                  placeholder="默认"
                  className="w-16 text-[10.5px] bg-panel border border-line rounded px-1.5 py-0.5"
                  value={(cfg as AlertChannelConfig & { archive_keep_days?: number }).archive_keep_days ?? ""}
                  onChange={(e) => set(key, "archive_keep_days", e.target.value)}
                  data-testid={`channel-archive-${key}`}
                />
              </div>
              {key === "email" && (
                <div className="space-y-1">
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="SMTP host" value={cfg.smtp_host ?? ""} onChange={(e) => set(key, "smtp_host", e.target.value)} />
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="Port (465)" type="number" value={cfg.smtp_port ?? 465} onChange={(e) => set(key, "smtp_port", e.target.value)} />
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="Username" value={cfg.username ?? ""} onChange={(e) => set(key, "username", e.target.value)} />
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="Password" type="password" value={cfg.password ?? ""} onChange={(e) => set(key, "password", e.target.value)} />
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="To (comma separated)" value={(cfg.to ?? []).join(",")} onChange={(e) => set(key, "to", e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />
                </div>
              )}
              {key === "telegram" && (
                <div className="space-y-1">
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="Bot token" type="password" value={cfg.bot_token ?? ""} onChange={(e) => set(key, "bot_token", e.target.value)} />
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="Chat id" value={cfg.chat_id ?? ""} onChange={(e) => set(key, "chat_id", e.target.value)} />
                </div>
              )}
              {(key === "feishu" || key === "dingtalk" || key === "wecom") && (
                <div className="space-y-1">
                  <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="Webhook URL" value={cfg.webhook_url ?? ""} onChange={(e) => set(key, "webhook_url", e.target.value)} />
                  {key !== "wecom" && (
                    <input className="w-full text-[11px] bg-panel border border-line rounded px-2 py-1" placeholder="Secret (optional)" type="password" value={cfg.secret ?? ""} onChange={(e) => set(key, "secret", e.target.value)} />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="flex items-center gap-2">
        <button
          className="text-[11.5px] rounded-lg bg-ink text-paper px-3 py-1.5 hover:opacity-90 disabled:opacity-50"
          onClick={() => onSave(draft)}
          disabled={busy}
          data-testid="channels-save"
        >
          {t("Save channels")}
        </button>
        <button
          className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong disabled:opacity-50"
          onClick={onTest}
          disabled={busy}
          data-testid="channels-test"
        >
          {t("Send test alert")}
        </button>
        <button
          className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong disabled:opacity-50"
          onClick={onProbe}
          disabled={busy}
          data-testid="channels-probe"
        >
          {t("Health probe")}
        </button>
        <button
          className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong disabled:opacity-50"
          onClick={() => onExportProbe("csv")}
          disabled={busy}
          data-testid="channels-export-csv"
        >
          {t("Export probe history")}
        </button>
      </div>
      {/* ① 探针结果: 各渠道连通性 + 延迟。 */}
      {probeResults && Object.keys(probeResults).length > 0 && (
        <div className="mt-2 space-y-1" data-testid="probe-results">
          {Object.entries(probeResults).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 text-[11px] font-mono">
              <span className={v.ok ? "text-emerald-600" : "text-rose-600"}>
                {v.ok ? "✓" : "✗"}
              </span>
              <span className="text-muted w-24">{k}</span>
              <span className="text-faint">
                {v.ok ? `${v.ms ?? "?"}ms` : (v.error ?? "failed")}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** ③ 遥测趋势图: 收敛历史折线 (支持时间轴缩放) + 降级层级分布。 */
function TrendCharts({ telemetry }: { telemetry: LongrunTelemetry | null }) {
  const t = useT();
  const conv = telemetry?.convergence_history ?? [];
  const deg = telemetry?.degradations ?? [];
  // 时间轴缩放: 显示最近 N 条 (默认全量, 可切 5/10/全量)。
  const [windowSize, setWindowSize] = useState<number | "all">("all");
  const convWindow = windowSize === "all" ? conv.slice(-12) : conv.slice(-windowSize);
  const rounds = convWindow.map((c) => c.report.iterations ?? 0);

  // 降级层级分布 L1..L6。
  const levelCounts = [0, 0, 0, 0, 0, 0];
  for (const d of deg) {
    const l = Math.min(6, Math.max(1, d.level ?? 1)) - 1;
    levelCounts[l] += 1;
  }

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <div className="text-[10.5px] uppercase tracking-wide text-faint">
              {t("Convergence rounds trend")} ({convWindow.length})
            </div>
            <div className="flex items-center gap-1">
              {([5, 10, "all"] as const).map((w) => (
                <button
                  key={String(w)}
                  className={
                    "text-[10px] px-1.5 py-0.5 rounded " +
                    (windowSize === w ? "bg-ink text-paper" : "text-faint hover:text-ink")
                  }
                  onClick={() => setWindowSize(w)}
                  data-testid={`trend-window-${String(w)}`}
                >
                  {w === "all" ? "全部" : `${w}`}
                </button>
              ))}
            </div>
          </div>
          {rounds.length < 2 ? (
            <div className="text-[11.5px] text-faint">{t("Need at least 2 runs to draw a trend")}</div>
          ) : (
            <MiniLineChart
              values={rounds}
              height={56}
              color="#2563eb"
              yLabel={(v) => `${Math.round(v)} 轮`}
              testid="convergence-trend"
            />
          )}
        </div>
        <div>
          <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">
            {t("Degradation level distribution")} ({deg.length})
          </div>
          {deg.length === 0 ? (
            <div className="text-[11.5px] text-faint">{t("No degradations recorded")}</div>
          ) : (
            <div className="flex items-end gap-1.5 h-14">
              {levelCounts.map((n, i) => (
                <div key={i} className="flex-1 flex flex-col items-center gap-0.5" title={`L${i + 1} ×${n}`}>
                  <div
                    className="w-full rounded-t bg-amber-500/70"
                    style={{ height: `${Math.max(3, (n / Math.max(1, ...levelCounts)) * 40)}px` }}
                    data-testid={`degradation-bar-L${i + 1}`}
                  />
                  <span className="text-[9px] text-faint">L{i + 1}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function MiniLineChart({
  values,
  height,
  color,
  yLabel,
  testid,
}: {
  values: number[];
  height: number;
  color: string;
  yLabel: (v: number) => string;
  testid: string;
}) {
  const W = 260;
  const PAD = 6;
  const maxV = Math.max(1, ...values);
  const pts = values
    .map((v, i) => {
      const x = values.length <= 1 ? PAD : PAD + (i / (values.length - 1)) * (W - 2 * PAD);
      const y = height - PAD - (v / maxV) * (height - 2 * PAD);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={W} height={height} viewBox={`0 0 ${W} ${height}`} className="shrink-0" data-testid={testid}>
      <line x1={PAD} y1={height - PAD} x2={W - PAD} y2={height - PAD} stroke="#475569" strokeWidth="1" />
      <line x1={PAD} y1={PAD} x2={PAD} y2={height - PAD} stroke="#475569" strokeWidth="1" />
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
      {values.map((v, i) => {
        const x = values.length <= 1 ? PAD : PAD + (i / (values.length - 1)) * (W - 2 * PAD);
        const y = height - PAD - (v / maxV) * (height - 2 * PAD);
        return (
          <g key={i}>
            <circle cx={x} cy={y} r="2" fill={color} />
            <text x={x - 8} y={y - 4} fontSize="8" fill="#94a3b8" textAnchor="end">
              {yLabel(v)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** ① 卡死任务告警条: 从健康轮询数据检测, 首次出现时弹桌面通知。 */
function AlertBanner({
  health,
  notifiedRef,
}: {
  health: LongrunHealth | null;
  notifiedRef: React.MutableRefObject<Set<string>>;
}) {
  const t = useT();
  const unhealthy = health?.heartbeat?.unhealthy ?? [];
  // 首次见到的新卡死任务 → 桌面通知 (Notification API, 无权限时静默)。
  useEffect(() => {
    if (unhealthy.length === 0) return;
    const fresh = unhealthy.filter((tid) => !notifiedRef.current.has(tid));
    if (fresh.length === 0) return;
    fresh.forEach((tid) => notifiedRef.current.add(tid));
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "granted") {
        for (const tid of fresh) {
          new Notification("7×24 任务告警", {
            body: `任务 ${tid} 心跳停滞 — 请检查。`,
            tag: `7x24-${tid}`,
          });
        }
      }
    } catch {
      // 通知不可用 (非桌面环境) — 静默。
    }
  }, [unhealthy, notifiedRef]);

  if (unhealthy.length === 0) return null;
  return (
    <div
      className="mb-3 rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-700"
      data-testid="alert-banner"
      role="alert"
    >
      ⚠ {t("Stalled tasks")}: {unhealthy.join(", ")} — {t("sessions auto-woken for self-check")}
    </div>
  );
}

export function LongRunView({ onBack }: { onBack: () => void }) {
  const t = useT();
  const [health, setHealth] = useState<LongrunHealth | null>(null);
  const [telemetry, setTelemetry] = useState<LongrunTelemetry | null>(null);
  const [sessions, setSessions] = useState<LongrunCheckpointSession[]>([]);
  const [detail, setDetail] = useState<LongrunCheckpointDetail | null>(null);
  const [storage, setStorage] = useState<LongrunStorage | null>(null);
  const [pheromone, setPheromone] = useState<PheromoneStatus | null>(null);
  // QunMesh M4 后续项: mesh_mode 运行时档位。
  const [meshMode, setMeshMode] = useState<string>("off");
  const changeMeshMode = useCallback((m: string) => {
    setMeshMode(m);
    setMeshModeApi(m).catch(() => {});
  }, []);
  const [maintenanceBusy, setMaintenanceBusy] = useState(false);
  const [maintenanceResult, setMaintenanceResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // ① 已通知过的卡死任务 (去重桌面提醒)。
  const notifiedRef = useRef<Set<string>>(new Set());
  // ② 恢复演练结果。
  const [restoreResult, setRestoreResult] = useState<string | null>(null);
  const [restoreBusy, setRestoreBusy] = useState(false);
  // ① 告警历史。
  const [alerts, setAlerts] = useState<LongrunAlert[]>([]);
  const [alertRange, setAlertRange] = useState<"1h" | "24h" | "7d" | "all">("24h");
  // ① 告警渠道配置。
  const [channels, setChannels] = useState<Record<string, AlertChannelConfig>>({});
  const [channelsEnabled, setChannelsEnabled] = useState<string[]>([]);
  const [channelsBusy, setChannelsBusy] = useState(false);
  const [channelMsg, setChannelMsg] = useState<string | null>(null);
  // ① 渠道健康探针结果。
  const [probeResults, setProbeResults] = useState<Record<string, { ok: boolean; ms?: number; error?: string }> | null>(null);
  // ① 渠道健康分/趋势。
  const [channelHealth, setChannelHealth] = useState<Record<string, ChannelHealth> | null>(null);
  // ② 告警自动归档结果。
  const [archiveMsg, setArchiveMsg] = useState<string | null>(null);
  // ③ 归档数据查询/恢复。
  const [archivedAlerts, setArchivedAlerts] = useState<LongrunAlert[]>([]);
  const [archivedTotal, setArchivedTotal] = useState(0);
  const [archivedOpen, setArchivedOpen] = useState(false);
  // ② 探针定时化调度。
  const [probeSchedule, setProbeSchedule] = useState<ProbeSchedule>({ enabled: true, interval_minutes: 60 });
  // ③ 告警聚合 (同任务连续卡死)。
  const [aggregations, setAggregations] = useState<LongrunAlertAggregation[]>([]);
  // ② 聚合历史统计 (趋势)。
  const [aggStats, setAggStats] = useState<AggregationStats | null>(null);
  // ③ 操作审计。
  const [audit, setAudit] = useState<LongrunAuditEntry[]>([]);
  // ② 告警静默设置 (静默阈值/时长 + 归档天数 + 健康分阈值 + 探针历史保留窗口)。
  const [alertSettings, setAlertSettings] = useState<AlertSettings>({
    silence_after: 3,
    silence_seconds: 600,
    archive_keep_days: 30,
    health_thresholds: { good: 80, warn: 50 },
    probe_history_keep_days: 30,
    probe_history_keep_count: 10000,
  });
  const [settingsMsg, setSettingsMsg] = useState<string | null>(null);
  // ③ 跨周对比。
  const [weekCompare, setWeekCompare] = useState<WeekCompare | null>(null);
  const [exportMsg, setExportMsg] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getLongrunHealth().then(setHealth).catch(() => {});
    getLongrunTelemetry().then(setTelemetry).catch(() => {});
    getLongrunCheckpoints().then((r) => setSessions(r.sessions ?? [])).catch(() => {});
    getLongrunStorage().then(setStorage).catch(() => {});
    getPheromoneStatus().then(setPheromone).catch(() => {});
    getMeshMode().then((r) => r.ok && setMeshMode(r.mesh_mode)).catch(() => {});
    getLongrunAlertChannels()
      .then((r) => {
        if (r.ok) {
          setChannels(r.channels ?? {});
          setChannelsEnabled(r.enabled ?? []);
        }
      })
      .catch(() => {});
    getLongrunAlertAggregations()
      .then((r) => setAggregations(r.aggregations ?? []))
      .catch(() => {});
    getLongrunAggregationStats()
      .then((r) => r.ok && setAggStats(r))
      .catch(() => {});
    getLongrunAudit()
      .then((r) => setAudit(r.audit ?? []))
      .catch(() => {});
    getLongrunAlertSettings()
      .then((r) => r.ok && setAlertSettings(r.settings))
      .catch(() => {});
    getLongrunWeekCompare()
      .then((r) => r.ok && setWeekCompare(r))
      .catch(() => {});
    getLongrunArchivedAlerts(10)
      .then((r) => {
        if (r.ok) {
          setArchivedAlerts(r.archived ?? []);
          setArchivedTotal(r.total_archived ?? 0);
        }
      })
      .catch(() => {});
    getLongrunProbeSchedule()
      .then((r) => r.ok && setProbeSchedule(r.schedule))
      .catch(() => {});
    getLongrunChannelHealth()
      .then((r) => r.ok && setChannelHealth(r.channels))
      .catch(() => {});
  }, []);

  // ③ 按时间范围加载告警历史。
  const loadAlerts = useCallback((range: "1h" | "24h" | "7d" | "all") => {
    const now = Date.now() / 1000;
    const sinceMap: Record<string, number | undefined> = {
      "1h": now - 3600,
      "24h": now - 86400,
      "7d": now - 7 * 86400,
      all: undefined,
    };
    getLongrunAlerts(100, undefined, sinceMap[range])
      .then((r) => setAlerts(r.alerts ?? []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadAlerts(alertRange);
  }, [alertRange, loadAlerts]);

  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 30_000); // 30s 轮询
    return () => clearInterval(iv);
  }, [refresh]);

  const selectSession = (sid: string) => {
    getLongrunCheckpointDetail(sid).then(setDetail).catch(() => {});
  };

  const runMaintenance = async (dryRun: boolean) => {
    setMaintenanceBusy(true);
    setError(null);
    try {
      const res = await runLongrunMaintenance(dryRun);
      if (res && res.sessions_archive) {
        const a = res.sessions_archive as { archived?: unknown[]; skipped?: number };
        setMaintenanceResult(
          `archived ${(a.archived ?? []).length} session(s), skipped ${a.skipped ?? 0} · memories dedupe/decay/consolidate done`,
        );
      } else {
        setMaintenanceResult("maintenance done");
      }
      refresh();
    } catch {
      setError("maintenance failed");
    } finally {
      setMaintenanceBusy(false);
    }
  };

  // ② 检查点恢复: 演练 (只读) 或真实恢复 (写回会话)。
  const runRestore = async (sid: string, apply: boolean) => {
    setRestoreBusy(true);
    setError(null);
    try {
      const res = await restoreLongrunCheckpoint(sid, apply);
      if (res.ok && res.summary) {
        const s = res.summary;
        const prefix = res.applied ? "⚡ APPLIED" : "▸ drill";
        const backupNote = res.backup_key ? ` · 已备份原会话 (${res.backup_key}) 可回滚` : "";
        setRestoreResult(
          `${prefix} ${sid}: ${s.messages ?? 0} msgs · ${s.tasks ?? 0} tasks · ` +
            `result=${String(s.result ?? "—").slice(0, 40)} · ${res.restore_ms}ms${backupNote}`,
        );
      } else {
        setRestoreResult(`${sid}: ${res.error ?? "restore failed"}`);
      }
    } catch {
      setRestoreResult(`${sid}: restore failed`);
    } finally {
      setRestoreBusy(false);
    }
  };

  // ② 备份一键回滚 (撤销上次真实恢复)。
  const runRollback = async (sid: string) => {
    setRestoreBusy(true);
    setError(null);
    try {
      const res = await rollbackLongrunCheckpoint(sid);
      if (res.ok) {
        setRestoreResult(
          `↩ ROLLED BACK ${sid}: ${res.rolled_back_messages ?? 0} msgs restored from backup · ${res.restore_ms}ms`,
        );
      } else {
        setRestoreResult(`${sid}: ${res.error ?? "rollback failed"}`);
      }
    } catch {
      setRestoreResult(`${sid}: rollback failed`);
    } finally {
      setRestoreBusy(false);
    }
  };

  // ① 保存告警渠道配置。
  const saveChannels = async (draft: Record<string, Partial<AlertChannelConfig>>) => {
    setChannelsBusy(true);
    setChannelMsg(null);
    try {
      // 分渠道归档天数: 从 draft 收集 archive_keep_days → settings.channel_archive_keep_days。
      const chArchive: Record<string, number> = {};
      for (const [k, v] of Object.entries(draft)) {
        const days = Number((v as AlertChannelConfig & { archive_keep_days?: unknown }).archive_keep_days);
        if (days > 0) chArchive[k] = days;
      }
      const res = await setLongrunAlertChannels(draft);
      if (res.ok) {
        setChannels(res.channels ?? {});
        setChannelsEnabled(res.enabled ?? []);
        setChannelMsg(`已保存: 启用 ${(res.enabled ?? []).join(", ") || "无"}`);
      } else {
        setChannelMsg(res.error ?? "保存失败");
      }
      // 保存分渠道归档天数 (若配置了任何渠道)。
      if (Object.keys(chArchive).length > 0) {
        await setLongrunAlertSettings({ channel_archive_keep_days: chArchive } as unknown as Partial<AlertSettings>);
      }
    } catch {
      setChannelMsg("保存失败");
    } finally {
      setChannelsBusy(false);
    }
  };

  // ① 测试告警渠道。
  const testChannels = async () => {
    setChannelsBusy(true);
    setChannelMsg(null);
    try {
      const res = await testLongrunAlertChannels();
      const parts = Object.entries(res.results ?? {}).map(
        ([k, v]) => `${k}:${v.ok ? "✓" : "✗"}`,
      );
      setChannelMsg(`测试结果: ${parts.join("  ") || "无启用渠道"}`);
    } catch {
      setChannelMsg("测试失败");
    } finally {
      setChannelsBusy(false);
    }
  };

  // ② 导出探针历史 (CSV)。
  const exportProbeHistory = async (format: "csv" | "json") => {
    setChannelMsg(null);
    try {
      const content = await exportLongrunChannelHealth(format, 500);
      try {
        const blob = new Blob([content], { type: format === "csv" ? "text/csv" : "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `qunwork-channel-health.${format}`;
        a.click();
        URL.revokeObjectURL(url);
      } catch {
        /* jsdom 无 Blob URL — 仅提示 */
      }
      setChannelMsg(`已导出探针历史 ${format.toUpperCase()} (${content.length} 字符)`);
    } catch {
      setChannelMsg("导出失败");
    }
  };

  // ① 渠道健康探针。
  const probeChannels = async () => {
    setChannelsBusy(true);
    setChannelMsg(null);
    try {
      const res = await probeLongrunAlertChannels();
      setProbeResults(res.results ?? {});
      setChannelMsg(`探针完成: ${res.healthy?.length ?? 0} 个渠道健康`);
      // 刷新健康分/趋势 (探针已落库)。
      const h = await getLongrunChannelHealth();
      if (h.ok) setChannelHealth(h.channels);
    } catch {
      setChannelMsg("探针失败");
    } finally {
      setChannelsBusy(false);
    }
  };

  // ② 告警/审计自动归档。
  const archiveAlerts = async () => {
    setArchiveMsg(null);
    try {
      const res = await archiveLongrunAlerts(30);
      if (res.ok) {
        setArchiveMsg(`已归档 ${res.archived} 条 (保留 ${res.kept} 条, 累计归档 ${res.archived_total ?? "?"} 条)`);
        // 刷新归档列表。
        const ar = await getLongrunArchivedAlerts(10);
        if (ar.ok) {
          setArchivedAlerts(ar.archived ?? []);
          setArchivedTotal(ar.total_archived ?? 0);
        }
      } else {
        setArchiveMsg(res.error ?? "归档失败");
      }
    } catch {
      setArchiveMsg("归档失败");
    }
  };

  // ③ 加载归档历史 (查看归档)。
  const loadArchived = async () => {
    setArchivedOpen((v) => !v);
    if (!archivedOpen) {
      try {
        const res = await getLongrunArchivedAlerts(50);
        if (res.ok) {
          setArchivedAlerts(res.archived ?? []);
          setArchivedTotal(res.total_archived ?? 0);
        }
      } catch {
        /* ignore */
      }
    }
  };

  // ③ 恢复一条归档记录。
  const restoreArchived = async (id: number) => {
    try {
      const res = await restoreLongrunArchivedAlert(id);
      if (res.ok) {
        setArchiveMsg(`已恢复归档告警 #${id} 到活跃表`);
        const ar = await getLongrunArchivedAlerts(50);
        if (ar.ok) {
          setArchivedAlerts(ar.archived ?? []);
          setArchivedTotal(ar.total_archived ?? 0);
        }
      } else {
        setArchiveMsg(res.error ?? "恢复失败");
      }
    } catch {
      setArchiveMsg("恢复失败");
    }
  };

  // ② 保存探针定时化调度。
  const saveProbeSchedule = async () => {
    setArchiveMsg(null);
    try {
      const res = await setLongrunProbeSchedule({
        enabled: probeSchedule.enabled,
        interval_minutes: Number(probeSchedule.interval_minutes) || 60,
      });
      if (res.ok) {
        setProbeSchedule(res.schedule);
        setArchiveMsg(`探针调度已保存: ${res.schedule.enabled ? "每 " + res.schedule.interval_minutes + " 分钟" : "已停用"}`);
      } else {
        setArchiveMsg(res.error ?? "保存失败");
      }
    } catch {
      setArchiveMsg("保存失败");
    }
  };

  // ① 审计导出 (CSV/JSON) — 下载为文件 (jsdom/无 Blob URL 时降级提示)。
  const exportAudit = async (format: "csv" | "json") => {
    setExportMsg(null);
    try {
      const content = await exportLongrunAudit(format, 500);
      try {
        const blob = new Blob([content], {
          type: format === "csv" ? "text/csv" : "application/json",
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `qunwork-audit.${format}`;
        a.click();
        URL.revokeObjectURL(url);
      } catch {
        // 无 Blob URL (测试环境) — 仅提示内容已获取。
      }
      setExportMsg(`已导出 ${format.toUpperCase()} (${content.length} 字符)`);
    } catch {
      setExportMsg("导出失败");
    }
  };

  // ② 保存告警静默设置。
  const saveAlertSettings = async () => {
    setSettingsMsg(null);
    try {
      const res = await setLongrunAlertSettings({
        silence_after: Number(alertSettings.silence_after) || 3,
        silence_seconds: Number(alertSettings.silence_seconds) || 600,
        archive_keep_days: Number(alertSettings.archive_keep_days) || 30,
        health_thresholds: {
          good: Number(alertSettings.health_thresholds?.good) || 80,
          warn: Number(alertSettings.health_thresholds?.warn) ?? 50,
        },
        probe_history_keep_days: Number(alertSettings.probe_history_keep_days) || 30,
        probe_history_keep_count: Number(alertSettings.probe_history_keep_count) || 10000,
      });
      if (res.ok) {
        setAlertSettings(res.settings);
        const h = res.settings.health_thresholds ?? { good: 80, warn: 50 };
        setSettingsMsg(
          `已保存: 阈值 ${res.settings.silence_after} 次 / 时长 ${res.settings.silence_seconds}s / ` +
          `健康分 ${h.good}/${h.warn} / 探针保留 ${res.settings.probe_history_keep_days ?? 30}天×${res.settings.probe_history_keep_count ?? 10000}条`,
        );
      } else {
        setSettingsMsg(res.error ?? "保存失败");
      }
    } catch {
      setSettingsMsg("保存失败");
    }
  };

  // ③ 手动清理探针历史 (按配置保留窗口)。
  const pruneProbeHistoryNow = async () => {
    setSettingsMsg(null);
    try {
      const res = await pruneProbeHistory();
      if (res.ok) {
        setSettingsMsg(
          `探针历史已清理: 移除 ${res.removed ?? 0} 条 (保留 ${res.kept ?? 0} 条, 窗口 ${res.keep_days ?? 30}天/${res.keep_count ?? 10000}条)`,
        );
      } else {
        setSettingsMsg(res.error ?? "清理失败");
      }
    } catch {
      setSettingsMsg("清理失败");
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-5xl mx-auto px-6 py-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <div className="text-[15px] font-semibold">{t("7x24 long-run management")}</div>
            <div className="text-[11.5px] text-faint">
              {t("Heartbeat · checkpoints · telemetry · storage — one console for long-running tasks")}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong"
              onClick={() => void runMaintenance(true)}
              disabled={maintenanceBusy}
              data-testid="maintenance-dryrun"
            >
              {t("Maintenance (dry-run)")}
            </button>
            <button
              className="text-[11.5px] rounded-lg bg-ink text-paper px-3 py-1.5 hover:opacity-90"
              onClick={() => void runMaintenance(false)}
              disabled={maintenanceBusy}
              data-testid="maintenance-run"
            >
              {maintenanceBusy ? t("Running…") : t("Run maintenance")}
            </button>
            <button
              className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong"
              onClick={onBack}
            >
              {t("Back")}
            </button>
          </div>
        </div>

        {error && <div className="mb-3 text-[12px] text-danger">{error}</div>}
        {maintenanceResult && (
          <div className="mb-3 text-[11.5px] text-emerald-600 font-mono" data-testid="maintenance-result">
            ✓ {maintenanceResult}
          </div>
        )}

        {/* ① 卡死任务告警条 (桌面通知已由 AlertBanner 触发)。 */}
        <AlertBanner health={health} notifiedRef={notifiedRef} />

        <div className="space-y-4">
          <Card title={t("Health — heartbeat · automations · wakes")}>
            <HealthPanel health={health} />
          </Card>

          <Card title={t("Telemetry — degradation trail · convergence history")}>
            <TelemetryPanel telemetry={telemetry} />
            <div className="mt-3 border-t border-line pt-3">
              <TrendCharts telemetry={telemetry} />
            </div>
          </Card>

          <Card title={t("Checkpoints — session checkpoint chains")}>
            {restoreResult && (
              <div className="mb-2 text-[11px] text-muted font-mono" data-testid="restore-result">
                {restoreResult}
              </div>
            )}
            <CheckpointPanel
              sessions={sessions}
              detail={detail}
              onSelect={(s) => selectSession(s)}
              onRestore={(s) => void runRestore(s, false)}
              onRestoreApply={(s) => void runRestore(s, true)}
              onRollback={(s) => void runRollback(s)}
              restoreBusy={restoreBusy}
            />
          </Card>

          <Card title={t("Alert history")}>
            <div className="mb-2">
              <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">
                {t("Alert aggregations")} ({aggregations.length})
              </div>
              <AlertAggregations aggs={aggregations} stats={aggStats} weekCompare={weekCompare} />
            </div>
            <AlertHistory
              alerts={alerts}
              rangeLabel={alertRange}
              onRange={(r) => setAlertRange(r)}
            />
          </Card>

          <Card title={t("Alert silence settings")}>
            {settingsMsg && (
              <div className="mb-2 text-[11px] font-mono" data-testid="settings-msg">
                {settingsMsg}
              </div>
            )}
            <div className="flex items-center gap-3">
              <label className="text-[11.5px] text-muted">
                {t("Silence after")}:
                <input
                  type="number"
                  min={1}
                  className="ml-1.5 w-16 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={alertSettings.silence_after}
                  onChange={(e) => setAlertSettings({ ...alertSettings, silence_after: Number(e.target.value) })}
                  data-testid="silence-after-input"
                />
              </label>
              <label className="text-[11.5px] text-muted">
                {t("Silence seconds")}:
                <input
                  type="number"
                  min={1}
                  className="ml-1.5 w-20 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={alertSettings.silence_seconds}
                  onChange={(e) => setAlertSettings({ ...alertSettings, silence_seconds: Number(e.target.value) })}
                  data-testid="silence-seconds-input"
                />
              </label>
              <label className="text-[11.5px] text-muted">
                {t("Archive keep days")}:
                <input
                  type="number"
                  min={1}
                  className="ml-1.5 w-16 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={alertSettings.archive_keep_days ?? 30}
                  onChange={(e) => setAlertSettings({ ...alertSettings, archive_keep_days: Number(e.target.value) })}
                  data-testid="archive-keep-days-input"
                />
              </label>
              <button
                className="text-[11.5px] rounded-lg bg-ink text-paper px-3 py-1.5 hover:opacity-90"
                onClick={() => void saveAlertSettings()}
                data-testid="settings-save"
              >
                {t("Save")}
              </button>
            </div>
            <div className="flex items-center gap-3 mt-2">
              <label className="text-[11.5px] text-muted">
                {t("Health good ≥")}:
                <input
                  type="number"
                  min={1}
                  max={100}
                  className="ml-1.5 w-16 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={alertSettings.health_thresholds?.good ?? 80}
                  onChange={(e) =>
                    setAlertSettings({
                      ...alertSettings,
                      health_thresholds: { ...(alertSettings.health_thresholds ?? { good: 80, warn: 50 }), good: Number(e.target.value) },
                    })
                  }
                  data-testid="health-good-input"
                />
              </label>
              <label className="text-[11.5px] text-muted">
                {t("Health warn ≥")}:
                <input
                  type="number"
                  min={0}
                  max={100}
                  className="ml-1.5 w-16 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={alertSettings.health_thresholds?.warn ?? 50}
                  onChange={(e) =>
                    setAlertSettings({
                      ...alertSettings,
                      health_thresholds: { ...(alertSettings.health_thresholds ?? { good: 80, warn: 50 }), warn: Number(e.target.value) },
                    })
                  }
                  data-testid="health-warn-input"
                />
              </label>
              <label className="text-[11.5px] text-muted">
                {t("Probe keep days")}:
                <input
                  type="number"
                  min={1}
                  className="ml-1.5 w-16 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={alertSettings.probe_history_keep_days ?? 30}
                  onChange={(e) => setAlertSettings({ ...alertSettings, probe_history_keep_days: Number(e.target.value) })}
                  data-testid="probe-keep-days-input"
                />
              </label>
              <label className="text-[11.5px] text-muted">
                {t("Probe keep rows")}:
                <input
                  type="number"
                  min={1}
                  className="ml-1.5 w-20 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={alertSettings.probe_history_keep_count ?? 10000}
                  onChange={(e) => setAlertSettings({ ...alertSettings, probe_history_keep_count: Number(e.target.value) })}
                  data-testid="probe-keep-count-input"
                />
              </label>
              <button
                className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong"
                onClick={() => void pruneProbeHistoryNow()}
                data-testid="probe-prune"
              >
                {t("Prune probe history")}
              </button>
            </div>
            <div className="text-[10.5px] text-faint mt-1.5">
              {t("Same-task alerts merge into one; after N consecutive alerts within the window, further notifications are silenced until the cooldown passes.")}
            </div>
          </Card>

          <Card title={t("Operation audit")}>
            {exportMsg && (
              <div className="mb-2 text-[11px] font-mono" data-testid="export-msg">
                {exportMsg}
              </div>
            )}
            <AuditLog audit={audit} onExport={(f) => void exportAudit(f)} />
          </Card>

          <Card title={t("Alert channels — email · Telegram · Feishu · DingTalk · WeCom")}>
            {channelMsg && (
              <div className="mb-2 text-[11px] font-mono" data-testid="channel-msg">
                {channelMsg}
              </div>
            )}
            <AlertChannelsPanel
              channels={channels}
              enabled={channelsEnabled}
              onSave={(d) => void saveChannels(d)}
              onTest={() => void testChannels()}
              onProbe={() => void probeChannels()}
              onExportProbe={(f) => void exportProbeHistory(f)}
              busy={channelsBusy}
              probeResults={probeResults}
              health={channelHealth}
            />
          </Card>

          <Card title={t("Alert archive")}>
            {archiveMsg && (
              <div className="mb-2 text-[11px] font-mono" data-testid="archive-msg">
                {archiveMsg}
              </div>
            )}
            <div className="flex items-center gap-2">
              <button
                className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong"
                onClick={() => void archiveAlerts()}
                data-testid="archive-alerts-btn"
              >
                {t("Archive alerts older than 30 days")}
              </button>
              <button
                className="text-[11.5px] rounded-lg border border-line bg-panel px-3 py-1.5 hover:border-lineStrong"
                onClick={() => void loadArchived()}
                data-testid="view-archived-btn"
              >
                {t("View archived")} ({archivedTotal})
              </button>
              <span className="text-[10.5px] text-faint">
                {t("Moves old alerts/audit to the archive table (keeps history, trims active table)")}
              </span>
            </div>
            {/* ③ 归档列表 + 恢复。 */}
            {archivedOpen && (
              <div className="mt-2 space-y-1 max-h-48 overflow-y-auto" data-testid="archived-list">
                {archivedAlerts.length === 0 ? (
                  <div className="text-[11.5px] text-faint">{t("No archived alerts")}</div>
                ) : (
                  archivedAlerts.slice(0, 15).map((a) => (
                    <div key={a.id} className="flex items-center gap-2 text-[11px] font-mono">
                      <span className="text-faint shrink-0">{new Date(a.ts * 1000).toLocaleString()}</span>
                      <span className="text-muted truncate flex-1">{a.message}</span>
                      <button
                        className="text-[10px] text-sky-600 shrink-0 hover:underline"
                        onClick={() => void restoreArchived(a.id)}
                        data-testid={`restore-archived-${a.id}`}
                      >
                        {t("Restore")}
                      </button>
                    </div>
                  ))
                )}
              </div>
            )}
            {/* ② 探针定时化调度。 */}
            <div className="mt-3 pt-2 border-t border-line flex items-center gap-3 flex-wrap">
              <span className="text-[11.5px] text-muted">{t("Auto probe")}:</span>
              <label className="flex items-center gap-1 text-[11px] text-muted">
                <input
                  type="checkbox"
                  checked={probeSchedule.enabled}
                  onChange={(e) => setProbeSchedule({ ...probeSchedule, enabled: e.target.checked })}
                  data-testid="probe-enabled"
                />
                {t("enabled")}
              </label>
              <label className="text-[11.5px] text-muted">
                {t("Interval min")}:
                <input
                  type="number"
                  min={1}
                  className="ml-1 w-16 text-[11px] bg-panel border border-line rounded px-2 py-1"
                  value={probeSchedule.interval_minutes}
                  onChange={(e) => setProbeSchedule({ ...probeSchedule, interval_minutes: Number(e.target.value) })}
                  data-testid="probe-interval"
                />
              </label>
              <button
                className="text-[11.5px] rounded-lg bg-ink text-paper px-3 py-1 hover:opacity-90"
                onClick={() => void saveProbeSchedule()}
                data-testid="probe-schedule-save"
              >
                {t("Save")}
              </button>
            </div>
          </Card>

          <Card title={t("Storage — conversation growth · archives · memory")}>
            <StoragePanel storage={storage} />
          </Card>

          <Card title={t("Pheromone bus — QunMesh stigmergy channels")}>
            <PheromonePanel status={pheromone} meshMode={meshMode} onModeChange={changeMeshMode} />
          </Card>
        </div>
      </div>
    </div>
  );
}
