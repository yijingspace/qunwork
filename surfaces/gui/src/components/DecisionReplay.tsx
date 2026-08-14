// -- 13 Agent 影子模式: 决策回放时间轴 (共享组件: 蜂群 SwarmView + 主会话 Transcript) --
import { useEffect, useState } from "react";
import type { DecisionTraceEntry } from "../api";
import { useT } from "../i18n";

export interface SwarmDecisionRow {
  worker: string;
  task_id: string;
  agent_id?: string;
  entry: DecisionTraceEntry;
}

const DECISION_KIND_META: Record<string, { icon: string; label: string; color: string }> = {
  tool_selection: { icon: "🔧", label: "Tool selection", color: "text-sky-300" },
  permission: { icon: "🛡", label: "Permission", color: "text-emerald-300" },
  scope_escalation: { icon: "⚠", label: "Scope escalation", color: "text-amber-300" },
  approval_resolution: { icon: "✓", label: "Approval", color: "text-violet-300" },
  plan_decision: { icon: "📋", label: "Plan", color: "text-cyan-300" },
  directory_decision: { icon: "📁", label: "Directory", color: "text-rose-300" },
  question_decision: { icon: "❓", label: "Question", color: "text-fuchsia-300" },
};

export function DecisionReplayTimeline({ decisions }: { decisions: SwarmDecisionRow[] }) {
  const t = useT();
  const [cursor, setCursor] = useState(0);
  // 当新决策追加时, 自动跟随到最新一条 (除非用户手动拖到中间)。
  const [userScrubbing, setUserScrubbing] = useState(false);
  const total = decisions.length;

  useEffect(() => {
    if (!userScrubbing && total > 0) setCursor(total - 1);
  }, [total, userScrubbing]);

  if (total === 0) return null;
  const safeCursor = Math.min(cursor, total - 1);
  const row = decisions[safeCursor];
  const entry = row.entry;
  const meta = DECISION_KIND_META[entry.kind] ?? { icon: "•", label: entry.kind, color: "text-faint" };

  return (
    <div className="rounded-xl border border-line bg-panel px-3.5 py-3 mb-4" data-testid="decision-replay">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold">
          {t("Decision replay")}
        </span>
        <span className="text-[10.5px] text-faint">
          {t("Scrub the timeline to see what each worker saw, considered, and chose")}
        </span>
        <span className="ml-auto text-[11px] text-faint tabular-nums">
          {safeCursor + 1} / {total}
        </span>
      </div>

      {/* 时间滑块 + 上下文切换 */}
      <div className="flex items-center gap-2 mb-3">
        <button
          className="text-[12px] text-faint hover:text-ink px-1.5 py-0.5 rounded border border-line"
          onClick={() => { setUserScrubbing(true); setCursor(Math.max(0, safeCursor - 1)); }}
          disabled={safeCursor === 0}
          aria-label={t("Previous decision")}
        >
          ←
        </button>
        <input
          type="range"
          min={0}
          max={Math.max(0, total - 1)}
          value={safeCursor}
          onChange={(e) => { setUserScrubbing(true); setCursor(Number(e.target.value)); }}
          onMouseUp={() => setTimeout(() => setUserScrubbing(false), 1500)}
          onTouchEnd={() => setTimeout(() => setUserScrubbing(false), 1500)}
          className="flex-1 accent-accent"
          data-testid="decision-replay-slider"
          aria-label={t("Decision timeline")}
        />
        <button
          className="text-[12px] text-faint hover:text-ink px-1.5 py-0.5 rounded border border-line"
          onClick={() => { setUserScrubbing(true); setCursor(Math.min(total - 1, safeCursor + 1)); }}
          disabled={safeCursor >= total - 1}
          aria-label={t("Next decision")}
        >
          →
        </button>
        <button
          className="text-[10.5px] text-faint hover:text-accent px-1.5 py-0.5 rounded border border-line"
          onClick={() => { setUserScrubbing(false); setCursor(total - 1); }}
          title={t("Jump to latest")}
        >
          {t("Latest")}
        </button>
      </div>

      {/* 当前决策详情 */}
      <div className="rounded-lg border border-line bg-surface p-2.5 text-[12px]">
        <div className="flex items-center gap-2 mb-1.5 flex-wrap">
          <span className={"text-[13px] " + meta.color}>{meta.icon}</span>
          <span className="font-semibold">{t(meta.label)}</span>
          <span className="text-[10.5px] text-faint">
            iter {entry.iteration} · {row.worker}{row.task_id ? ` · ${row.task_id}` : ""}{entry.agent ? ` · ${entry.agent}` : ""}
          </span>
          <span className="text-[10.5px] text-faint ml-auto tabular-nums">
            {new Date(entry.ts * 1000).toLocaleTimeString()}
          </span>
        </div>

        {/* tool_selection: 显示可选工具清单 + 候选 Top-K + 实际选择 */}
        {entry.kind === "tool_selection" && (
          <div className="space-y-1.5">
            {entry.available_tools && entry.available_tools.length > 0 && (
              <div>
                <div className="text-[10.5px] text-faint mb-0.5">{t("Available tools")} ({entry.available_tools.length})</div>
                <div className="flex flex-wrap gap-1 max-h-16 overflow-y-auto">
                  {entry.available_tools.slice(0, 30).map((n) => (
                    <span key={n} className="px-1.5 py-px rounded bg-surface-2 text-[10.5px] text-faint font-mono">{n}</span>
                  ))}
                  {entry.available_tools.length > 30 && <span className="text-[10.5px] text-faint">+{entry.available_tools.length - 30}</span>}
                </div>
              </div>
            )}
            {entry.candidates && entry.candidates.length > 0 && (
              <div>
                <div className="text-[10.5px] text-faint mb-0.5">{t("Candidates considered")} (Top-{entry.candidates.length})</div>
                <div className="space-y-0.5">
                  {entry.candidates.map((c, i) => {
                    const chosen = entry.choice?.includes(c.name);
                    return (
                      <div key={i} className={"flex items-center gap-1.5 text-[11px] " + (chosen ? "text-accent" : "text-faint")}>
                        <span>{chosen ? "▸" : "·"}</span>
                        <span className="font-mono">{c.name}</span>
                        <span className="truncate text-faint">
                          {Object.entries(c.arguments ?? {}).slice(0, 2).map(([k, v]) => `${k}=${typeof v === "string" ? v.slice(0, 30) : String(v).slice(0, 30)}`).join(" ")}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            {entry.reason && (
              <div className="text-[11px] text-muted italic">{t("Reason")}: {entry.reason}</div>
            )}
          </div>
        )}

        {/* permission / approval_resolution: 显示工具 + 是否允许 + 原因 + 规则 */}
        {(entry.kind === "permission" || entry.kind === "approval_resolution") && (
          <div className="space-y-1 text-[11.5px]">
            {entry.tool && (
              <div className="flex items-center gap-2">
                <span className="text-faint">{t("Tool")}:</span>
                <span className="font-mono text-ink">{entry.tool}</span>
              </div>
            )}
            <div className="flex items-center gap-2">
              <span className="text-faint">{t("Decision")}:</span>
              {entry.allowed ? (
                <span className="text-emerald-300">✓ {t("allowed")}</span>
              ) : (
                <span className="text-rose-300">✗ {t("denied")}</span>
              )}
              {entry.outcome && <span className="text-faint">· {entry.outcome}</span>}
              {entry.needs_user && <span className="text-amber-300">· {t("needs user")}</span>}
            </div>
            {entry.rule && (
              <div className="text-faint">{t("Rule")}: <span className="font-mono">{entry.rule}</span></div>
            )}
            {entry.reason && (
              <div className="text-muted italic">{t("Reason")}: {entry.reason}</div>
            )}
          </div>
        )}

        {/* scope_escalation: 显示连接器 + 所需 scope vs 已授 scope */}
        {entry.kind === "scope_escalation" && (
          <div className="space-y-1 text-[11.5px]">
            {entry.tool && (
              <div className="flex items-center gap-2">
                <span className="text-faint">{t("Tool")}:</span>
                <span className="font-mono text-ink">{entry.tool}</span>
              </div>
            )}
            {entry.connector && (
              <div className="flex items-center gap-2">
                <span className="text-faint">{t("Connector")}:</span>
                <span className="text-ink">{entry.connector}</span>
              </div>
            )}
            {entry.required_scopes && entry.required_scopes.length > 0 && (
              <div className="flex items-center gap-2">
                <span className="text-faint">{t("Required scopes")}:</span>
                {entry.required_scopes.map((s) => (
                  <span key={s} className="px-1.5 py-px rounded bg-rose-500/20 text-rose-200 text-[10.5px] font-mono">{s}</span>
                ))}
              </div>
            )}
            {entry.granted_scopes && entry.granted_scopes.length > 0 && (
              <div className="flex items-center gap-2">
                <span className="text-faint">{t("Granted scopes")}:</span>
                {entry.granted_scopes.map((s) => (
                  <span key={s} className="px-1.5 py-px rounded bg-emerald-600/15 text-emerald-200 text-[10.5px] font-mono">{s}</span>
                ))}
              </div>
            )}
            {entry.reason && (
              <div className="text-amber-200 italic">{t("Reason")}: {entry.reason}</div>
            )}
          </div>
        )}
      </div>

      {/* 迷你时间轴标记 */}
      <div className="flex items-center gap-px mt-2 overflow-x-auto hairline-scroll">
        {decisions.map((d, i) => {
          const m = DECISION_KIND_META[d.entry.kind] ?? { icon: "•", color: "text-faint", label: d.entry.kind };
          return (
            <button
              key={i}
              onClick={() => { setUserScrubbing(true); setCursor(i); }}
              className={"shrink-0 w-5 h-5 rounded text-[10px] flex items-center justify-center transition " + (i === safeCursor ? "bg-accentSoft border border-accent" : "hover:bg-surface-2 border border-transparent")}
              title={`${i + 1}. ${m.label} · ${d.worker}`}
              data-testid={`decision-tick-${i}`}
            >
              <span className={m.color}>{m.icon}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
