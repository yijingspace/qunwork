import { useEffect, useRef, useState } from "react";
import {
  getHealth,
  getOrchestrateHistory,
  getOrchestrateRun,
  orchestrate,
  type OrchestrationHistoryItem,
  type OrchestrationRunSnapshot,
} from "../api";
import { useT } from "../i18n";

/** Fallback workspace hint: the server's configured default, if any. */
async function defaultWorkspaceHint(): Promise<string | undefined> {
  try {
    const h = await getHealth();
    return h.default_workspace ?? undefined;
  } catch {
    return undefined;
  }
}

/** Swarm (multi-agent) panel — real-time progress:
 *  POST (async) → poll the run store → stream the event feed:
 *  plan DAG, per-task status, worker chain-of-thought, governance commands,
 *  plus a history list of past runs. */

interface TaskView {
  id: string;
  description: string;
  deps: string[];
  status: string;
  confidence: number;
  result: string;
}

interface Thought {
  worker: string;
  task_id: string;
  text: string;
}

const STATUS_MARK: Record<string, string> = {
  done: "✓",
  needs_human: "⚠",
  running: "…",
  pending: "○",
};

export function SwarmView({ onBack, workspace }: { onBack: () => void; workspace?: string }) {
  const t = useT();
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [tasks, setTasks] = useState<TaskView[]>([]);
  const [thoughts, setThoughts] = useState<Thought[]>([]);
  const [governance, setGovernance] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<OrchestrationHistoryItem[]>([]);
  const mounted = useRef(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const loadHistory = async () => {
    try {
      const h = await getOrchestrateHistory();
      if (mounted.current) setHistory(h.runs ?? []);
    } catch {
      /* best-effort */
    }
  };

  useEffect(() => {
    loadHistory();
  }, []);

  const applySnapshot = (snap: OrchestrationRunSnapshot) => {
    setStatus(snap.status);
    const tasks: TaskView[] = [];
    const thoughts: Thought[] = [];
    const gov: string[] = [];
    for (const ev of snap.events) {
      const p = ev.payload as Record<string, any>;
      if (ev.kind === "plan_ready") {
        for (const raw of (p.tasks as any[]) ?? []) {
          tasks.push({ id: raw.id, description: raw.description, deps: raw.deps, status: "pending", confidence: 0, result: "" });
        }
      } else if (ev.kind === "task_started") {
        const td = tasks.find((x) => x.id === p.id);
        if (td) td.status = "running";
      } else if (ev.kind === "task_result") {
        const td = tasks.find((x) => x.id === p.id);
        if (td && typeof p.result === "string") td.result = p.result;
      } else if (ev.kind === "task_done") {
        const td = tasks.find((x) => x.id === p.id);
        if (td) {
          td.status = p.status;
          td.confidence = Number(p.confidence ?? 0);
        }
      } else if (ev.kind === "worker_thought") {
        thoughts.push({ worker: String(p.worker ?? ""), task_id: String(p.task_id ?? ""), text: String(p.text ?? "") });
      } else if (ev.kind === "governance") {
        gov.push(`[step ${p.step}] ${p.action}: ${p.reason}`);
      }
    }
    setTasks(tasks);
    setThoughts(thoughts);
    setGovernance(gov);
  };

  const run = async () => {
    const goal = intent.trim();
    if (!goal || busy) return;
    setBusy(true);
    setError(null);
    setRunId(null);
    setStatus("running");
    setTasks([]);
    setThoughts([]);
    setGovernance([]);
    try {
      const ws = workspace?.trim() || (await defaultWorkspaceHint());
      const res = await orchestrate(goal, { workspace: ws || undefined, maxParallel: 2 });
      if (!mounted.current) return;
      if (!res.ok || !res.run_id) {
        setError(res.error || "orchestration failed to start");
        setBusy(false);
        return;
      }
      setRunId(res.run_id);
      // poll the run store until it leaves "running"
      const rid = res.run_id;
      pollRef.current = setInterval(async () => {
        try {
          const snap = await getOrchestrateRun(rid);
          if (!mounted.current) return;
          applySnapshot(snap);
          if (snap.status !== "running") {
            if (pollRef.current) clearInterval(pollRef.current);
            setBusy(false);
            loadHistory();
          }
        } catch {
          if (pollRef.current) clearInterval(pollRef.current);
          setBusy(false);
        }
      }, 1000);
    } catch (e) {
      if (mounted.current) {
        setError(String(e));
        setBusy(false);
      }
    }
  };

  const openRun = async (rid: string) => {
    setBusy(true);
    setError(null);
    setRunId(rid);
    setStatus("running");
    try {
      const snap = await getOrchestrateRun(rid);
      if (!mounted.current) return;
      applySnapshot(snap);
      setStatus(snap.status);
      setIntent(snap.intent);
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
          {status && (
            <span className={"text-[12px] " + (status === "completed" ? "text-ok" : status === "failed" ? "text-danger" : "text-muted")}>
              {t("Status")}: {status}
              {runId && <span className="text-faint"> · {runId}</span>}
            </span>
          )}
        </div>
        {error && <div className="text-[12px] text-danger mt-2">{error}</div>}
      </div>

      <div className="flex-1 overflow-y-auto hairline-scroll px-5 py-4">
        {!runId && !busy && history.length === 0 && (
          <p className="text-[13px] text-faint">
            {t("Send a goal above — the swarm will split it into tasks, run them, validate and converge.")}
          </p>
        )}

        {tasks.length > 0 && (
          <div className="mb-4">
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("Task plan")}
            </div>
            {tasks.map((task) => (
              <div key={task.id} className="rounded-xl border border-line bg-panel px-3.5 py-2.5 mb-2">
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
                  <pre className="text-[11.5px] text-muted mt-1.5 whitespace-pre-wrap font-sans leading-snug max-h-20 overflow-y-auto">
                    {task.result}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}

        {thoughts.length > 0 && (
          <div className="mb-4">
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("Worker thoughts")}
            </div>
            {thoughts.map((th, i) => (
              <div key={i} className="mb-1.5 pl-2 border-l-2 border-line">
                <div className="text-[10.5px] text-faint uppercase tracking-wide">
                  {th.worker}
                  {th.task_id ? ` · ${th.task_id}` : ""}
                </div>
                <div className="text-[12px] text-muted whitespace-pre-wrap">{th.text}</div>
              </div>
            ))}
          </div>
        )}

        {governance.length > 0 && (
          <div className="mb-4">
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("Governance report")}
            </div>
            {governance.map((g, i) => (
              <div key={i} className="text-[11px] text-faint font-mono mb-0.5">{g}</div>
            ))}
          </div>
        )}

        {history.length > 0 && (
          <div>
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("History")}
            </div>
            {history.map((h) => (
              <button
                key={h.run_id}
                className="w-full text-left rounded-lg border border-line bg-panel px-3 py-2 mb-1.5 hover:border-lineStrong"
                onClick={() => openRun(h.run_id)}
              >
                <div className="flex items-center gap-2">
                  <span className={"text-[12px] " + (h.status === "completed" ? "text-ok" : h.status === "failed" ? "text-danger" : "text-muted")}>
                    {h.status}
                  </span>
                  <span className="text-[12.5px] flex-1 truncate">{h.intent}</span>
                  <span className="text-[10.5px] text-faint">{new Date(h.created_at * 1000).toLocaleString()}</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
