import { useEffect, useRef, useState } from "react";
import { orchestrate, type OrchestrationResponse } from "../api";
import { useT } from "../i18n";

/** Swarm (multi-agent) panel: send a goal to the worker swarm and visualize the
 * converged task DAG, per-task outcomes and the governance report. */

const STATUS_MARK: Record<string, string> = {
  done: "✓",
  needs_human: "⚠",
  running: "…",
  pending: "○",
};

export function SwarmView({ onBack }: { onBack: () => void }) {
  const t = useT();
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<OrchestrationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = async () => {
    const goal = intent.trim();
    if (!goal || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await orchestrate(goal, { maxParallel: 2 });
      if (!mounted.current) return;
      if (!res.ok) {
        setError(res.error || "orchestration failed");
      } else {
        setResult(res);
      }
    } catch (e) {
      if (mounted.current) setError(String(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 px-5 pt-4 pb-3 border-b border-line shrink-0">
        <button className="text-[13px] text-muted hover:text-ink" onClick={onBack}>
          ←
        </button>
        <div>
          <div className="text-[15px] font-semibold">🐝 {t("Multi-agent swarm")}</div>
          <div className="text-[12px] text-muted">
            {t("Planner decomposes, executors work, reviewer validates, governance watches.")}
          </div>
        </div>
      </div>

      <div className="px-5 py-4 border-b border-line shrink-0">
        <textarea
          className="w-full rounded-lg border border-line bg-paper px-3 py-2 text-[13px] outline-none focus:border-lineStrong resize-none"
          rows={3}
          value={intent}
          onChange={(e) => setIntent(e.target.value)}
          placeholder={t("Describe the goal, e.g. Write a market report with research, draft and review steps…")}
        />
        <div className="flex items-center gap-2 mt-2">
          <button
            className="px-4 py-1.5 rounded-full bg-ink text-panel text-[13px] disabled:opacity-40"
            disabled={busy || !intent.trim()}
            onClick={run}
          >
            {busy ? t("Swarm running…") : t("Run swarm")}
          </button>
          {result && (
            <span className={"text-[12px] " + (result.status === "completed" ? "text-ok" : "text-warn")}>
              {t("Status")}: {result.status} · {result.runs} {t("steps")}
            </span>
          )}
        </div>
        {error && <div className="text-[12px] text-danger mt-2">{error}</div>}
      </div>

      <div className="flex-1 overflow-y-auto hairline-scroll px-5 py-4">
        {!result && !busy && (
          <p className="text-[13px] text-faint">
            {t("Send a goal above — the swarm will split it into tasks, run them, validate and converge.")}
          </p>
        )}
        {busy && <p className="text-[13px] text-muted">{t("Swarm working…")}</p>}

        {result?.tasks.map((task) => (
          <div key={task.id} className="rounded-xl border border-line bg-panel px-3.5 py-2.5 mb-2.5">
            <div className="flex items-center gap-2">
              <span className="text-[13px]">{STATUS_MARK[task.status] ?? "?"}</span>
              <span className="text-[13px] font-semibold flex-1">
                [{task.id}] {task.description}
              </span>
              {task.confidence > 0 && (
                <span className="text-[11px] text-faint">{(task.confidence * 100).toFixed(0)}%</span>
              )}
            </div>
            {task.deps.length > 0 && (
              <div className="text-[11px] text-faint mt-0.5">
                {t("depends on")}: {task.deps.join(", ")}
              </div>
            )}
            {task.result && (
              <pre className="text-[11.5px] text-muted mt-1.5 whitespace-pre-wrap font-sans leading-snug max-h-24 overflow-y-auto">
                {task.result}
              </pre>
            )}
          </div>
        ))}

        {result?.governance_report && (
          <div className="mt-3">
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("Governance report")}
            </div>
            <pre className="text-[11px] text-faint whitespace-pre-wrap font-sans leading-snug">
              {result.governance_report}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
