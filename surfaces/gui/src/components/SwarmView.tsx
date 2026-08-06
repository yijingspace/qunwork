import { useEffect, useMemo, useRef, useState } from "react";
import {
  addSwarmTemplate,
  deleteSwarmTemplate,
  getCoordinationReport,
  getHealth,
  getOrchestrateHistory,
  getOrchestrateRun,
  listSwarmTemplates,
  orchestrate,
  orchestrateControl,
  type CoordinationReport,
  type OrchestrationHistoryItem,
  type OrchestrationRunSnapshot,
  type SwarmTemplate,
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

const STATUS_COLOR: Record<string, string> = {
  done: "#16a34a",
  needs_human: "#d97706",
  running: "#2563eb",
  pending: "#cbd5e1",
};

function fmtDuration(sec: number, t: (k: string) => string): string {
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m > 0 ? `${m}${t("m")}${s}${t("s")}` : `${s}${t("s")}`;
}

/** Simple vertical DAG: each task is a row; dependency arrows link parent -> child. */
function DAGDiagram({ tasks }: { tasks: TaskView[] }) {
  const ROW = 64;
  const PAD = 26;
  const W = 280;
  const H = Math.max(ROW + PAD * 2, tasks.length * ROW + PAD * 2);
  const y = (i: number) => PAD + i * ROW + ROW / 2;
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="shrink-0">
      <defs>
        <marker id="dag-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#94a3b8" />
        </marker>
      </defs>
      {tasks.map((t, i) =>
        t.deps.map((d) => {
          const di = tasks.findIndex((x) => x.id === d);
          if (di < 0) return null;
          return (
            <line
              key={`${t.id}-${d}`}
              x1={24}
              y1={y(di) + 14}
              x2={24}
              y2={y(i) - 14}
              stroke="#94a3b8"
              strokeWidth={1.5}
              markerEnd="url(#dag-arrow)"
            />
          );
        }),
      )}
      {tasks.map((t, i) => (
        <g key={t.id}>
          <circle cx={24} cy={y(i)} r={11} fill={STATUS_COLOR[t.status] ?? "#cbd5e1"} />
          <text x={44} y={y(i) + 4} fontSize={12} fill="currentColor">
            {t.id}
          </text>
        </g>
      ))}
    </svg>
  );
}

export function SwarmView({ onBack, workspace }: { onBack: () => void; workspace?: string }) {
  const t = useT();
  const [intent, setIntent] = useState("");
  // The swarm runs INSIDE this workspace — its workers' file/read/grep tools are
  // scoped to it. Editing the path here lets you point the swarm at the real
  // project (the single most common failure: swarm working in the wrong folder).
  const [workspacePath, setWorkspacePath] = useState(workspace ?? "");
  const [maxParallel, setMaxParallel] = useState(4);
  const [timeoutSeconds, setTimeoutSeconds] = useState(300);
  const [executorAgent, setExecutorAgent] = useState<"cowork" | "code">("cowork");
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [tasks, setTasks] = useState<TaskView[]>([]);
  const [thoughts, setThoughts] = useState<Thought[]>([]);
  const [governance, setGovernance] = useState<string[]>([]);
  // 5.2.3 governance dashboard: tally watchdog actions (WARN/PAUSE/REVERT/…) per run.
  const govStats = useMemo(() => {
    const m = new Map<string, number>();
    for (const g of governance) {
      const hit = /^\[step \d+\] (\w+)/.exec(g);
      if (hit) m.set(hit[1], (m.get(hit[1]) ?? 0) + 1);
    }
    return m;
  }, [governance]);
  const [finalReport, setFinalReport] = useState<string>("");
  const [reportPath, setReportPath] = useState<string>("");
  const [elapsed, setElapsed] = useState(0);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<OrchestrationHistoryItem[]>([]);
  const [templates, setTemplates] = useState<SwarmTemplate[]>([]);
  const [savingTpl, setSavingTpl] = useState(false);
  const [tplTitle, setTplTitle] = useState("");
  const [tplError, setTplError] = useState<string | null>(null);
  const [showTemplateForm, setShowTemplateForm] = useState(false);
  // G2 command deck
  const [paused, setPaused] = useState(false);
  const [deckMsg, setDeckMsg] = useState("");
  const [deckError, setDeckError] = useState<string | null>(null);
  const [requeues, setRequeues] = useState<
    Array<{ task_id: string; attempt?: number; reason?: string }>
  >([]);
  // Benchmark showcase: coordination report for the current (completed) run
  const [report, setReport] = useState<CoordinationReport | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const mounted = useRef(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (pollRef.current) clearInterval(pollRef.current);
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const loadTemplates = () => {
    listSwarmTemplates()
      .then((r) => mounted.current && setTemplates(r.templates ?? []))
      .catch(() => {});
  };

  useEffect(() => {
    loadTemplates();
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

  const saveTemplate = async () => {
    setSavingTpl(true);
    setTplError(null);
    const res = await addSwarmTemplate(
      tplTitle.trim() || intent.trim().slice(0, 30) || "Swarm run",
      intent,
      tasks.map((t) => ({ id: t.id, description: t.description, deps: t.deps })),
    );
    setSavingTpl(false);
    if (!res.ok) {
      setTplError(res.error || "Failed to save template");
      return;
    }
    if (res.template) setTemplates((prev) => [res.template!, ...prev]);
    setTplTitle("");
    setShowTemplateForm(false);
  };

  const removeTemplate = async (id: number) => {
    const ok = await deleteSwarmTemplate(id);
    if (ok.ok) setTemplates((prev) => prev.filter((x) => x.id !== id));
  };

  const pendingTmplRef = useRef<number | null>(null);
  const applyTemplate = (tmpl: SwarmTemplate) => {
    setIntent(tmpl.intent);
    pendingTmplRef.current = tmpl.id; // reuse is tallied when the run finishes (5.2.1 track record)
  };

  const applySnapshot = (snap: OrchestrationRunSnapshot) => {
    setStatus(snap.status);
    if (snap.final) setFinalReport(snap.final);
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
      } else if (ev.kind === "task_requeue_waiting") {
        // G2: reviewer rejected — deck shows an approve/reject card for this task.
        setRequeues((prev) => {
          if (prev.some((r) => r.task_id === p.id)) return prev;
          return [...prev, { task_id: String(p.id), attempt: Number(p.attempt ?? 1), reason: String(p.reason ?? "") }];
        });
      } else if (ev.kind === "task_requeue_approved" || ev.kind === "task_requeue_declined") {
        setRequeues((prev) => prev.filter((r) => r.task_id !== p.id));
      }
    }
    setTasks(tasks);
    setThoughts(thoughts);
    setGovernance(gov);
  };

  // -- G2 command deck --------------------------------------------------------
  const togglePause = async () => {
    if (!runId) return;
    const res = await orchestrateControl(runId, paused ? "resume" : "pause");
    if (res.ok) setPaused(!paused);
    else setDeckError(res.error || "control failed");
  };

  const sendMessage = async () => {
    if (!runId || !deckMsg.trim()) return;
    const res = await orchestrateControl(runId, "message", { text: deckMsg.trim() });
    setDeckError(res.ok ? null : res.error || "message failed");
    if (res.ok) setDeckMsg("");
  };

  const decideRequeue = async (taskId: string, approve: boolean) => {
    if (!runId) return;
    await orchestrateControl(runId, approve ? "requeue_approve" : "requeue_reject", { task_id: taskId });
    setRequeues((prev) => prev.filter((r) => r.task_id !== taskId));
  };

  const loadReport = async () => {
    if (!runId) return;
    setReportBusy(true);
    const res = await getCoordinationReport(runId);
    setReportBusy(false);
    if (res.ok) setReport(res);
    else setDeckError(res.error || "report failed");
  };

  const run = async () => {
    const goal = intent.trim();
    if (!goal || busy) return;
    setBusy(true);
    setError(null);
    setStale(false);
    setRunId(null);
    setStatus("running");
    setTasks([]);
    setThoughts([]);
    setGovernance([]);
    setFinalReport("");
    setStartedAt(Date.now());
    setElapsed(0);
    timerRef.current = setInterval(() => {
      if (mounted.current) setElapsed((Date.now() - (startedAt ?? Date.now())) / 1000);
    }, 1000);
    try {
      const ws = workspacePath?.trim() || (await defaultWorkspaceHint());
      const res = await orchestrate(goal, {
        workspace: ws || undefined,
        maxParallel,
        timeoutSeconds,
        executorAgent,
        templateId: pendingTmplRef.current ?? undefined,
      });
      if (!mounted.current) return;
      if (!res.ok || !res.run_id) {
        setError(res.error || "orchestration failed to start");
        setBusy(false);
        if (timerRef.current) clearInterval(timerRef.current);
        return;
      }
      setReportPath(res.report_path ?? "");
      setRunId(res.run_id);
      const rid = res.run_id;
      // Poll until the run leaves "running" (heartbeat-aware: an orphaned run —
      // background task lost on server restart — is detected and reported).
      // Poll budget must cover the relaxed stale window: consolidation tasks can
      // run well past the nominal budget (soft-budget deadline + task timeout).
      const maxPolls = Math.ceil((Math.max(timeoutSeconds * 3, 600) + 90) / 1);
      let polls = 0;
      let lastActivity = Date.now();
      pollRef.current = setInterval(async () => {
        polls += 1;
        try {
          const snap = await getOrchestrateRun(rid);
          if (!mounted.current) return;
          applySnapshot(snap);
          const upd = (snap.updated_at ?? 0) * 1000;
          if (upd > lastActivity) lastActivity = upd;
          // Stale detection: long tool chains (consolidation tasks) can legitimately
          // go quiet for minutes; require max(10min, 3× the run budget) without a
          // heartbeat before declaring the run orphaned.
          const dead = upd > 0 && Date.now() - upd > Math.max(600, timeoutSeconds * 3) * 1000;
          if (snap.status !== "running" || polls > maxPolls || dead) {
            if (pollRef.current) clearInterval(pollRef.current);
            if (timerRef.current) clearInterval(timerRef.current);
            if (snap.status === "running") {
              setStatus(dead ? "stale" : "paused");
              setStale(true);
              setError(t("Run seems unresponsive (server restarted?). Try again."));
            } else {
              setElapsed((Date.now() - (startedAt ?? Date.now())) / 1000);
            }
            setBusy(false);
            loadHistory();
            // Template track record is now tallied server-side on completion
            // (asset loop) — here we only refresh the cards.
            pendingTmplRef.current = null;
            loadTemplates();
          }
        } catch {
          if (pollRef.current) clearInterval(pollRef.current);
          if (timerRef.current) clearInterval(timerRef.current);
          setBusy(false);
        }
      }, 1000);
    } catch (e) {
      if (mounted.current) {
        setError(String(e));
        setBusy(false);
        if (timerRef.current) clearInterval(timerRef.current);
      }
    }
  };

  const openRun = async (rid: string) => {
    setBusy(true);
    setError(null);
    setStale(false);
    setRunId(rid);
    setStatus("running");
    setStartedAt(Date.now());    // jump the content view back to the top so the loaded run is immediately visible
    requestAnimationFrame(() => {
      document.querySelector(".swarm-scroll")?.scrollTo({ top: 0 });
    });
    try {
      const snap = await getOrchestrateRun(rid);
      if (!mounted.current) return;
      applySnapshot(snap);
      setStatus(snap.status);
      setIntent(snap.intent);
      if (snap.created_at && snap.updated_at) {
        setElapsed(snap.updated_at - snap.created_at);
      }
    } catch (e) {
      if (mounted.current) setError(String(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  // -- derived stats --------------------------------------------------------
  const doneTasks = tasks.filter((x) => x.status === "done");
  const avgConf = doneTasks.length
    ? doneTasks.reduce((a, x) => a + x.confidence, 0) / doneTasks.length
    : 0;
  const estTokens = Math.round(
    (thoughts.reduce((a, th) => a + th.text.length, 0) +
      tasks.reduce((a, x) => a + (x.result || "").length, 0)) /
      4,
  );

  return (
    <div className="flex flex-col h-full min-w-0 overflow-hidden">
      <div className="flex items-center gap-3 px-5 pt-4 pb-3 border-b border-line shrink-0 min-w-0">
        <button
          className="shrink-0 flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-[12.5px] text-muted hover:text-ink hover:border-lineStrong"
          onClick={onBack}
          data-testid="swarm-back"
        >
          ← {t("Back to chat")}
        </button>
        <div className="min-w-0">
          <div className="text-[15px] font-semibold truncate">🐝 {t("Multi-agent swarm")}</div>
          <div className="text-[12px] text-muted truncate">
            {t("Planner decomposes, executors work, reviewer validates, governance watches.")}
          </div>
        </div>
      </div>

      <div className="px-5 py-4 border-b border-line shrink-0 min-w-0">
        <textarea
          className="w-full min-w-0 rounded-lg border border-line bg-paper px-3 py-2 text-[13px] outline-none focus:border-lineStrong resize-none break-words overflow-y-auto"
          wrap="soft"
          rows={3}
          value={intent}
          onChange={(e) => setIntent(e.target.value)}
          placeholder={t("Describe the goal, e.g. Write a market report with research, draft and review steps…")}
        />
        {/* workspace: the swarm works INSIDE this folder — keep it pointed at the real project */}
        <label className="flex items-center gap-1.5 text-[12px] text-muted mt-2">
          {t("Workspace")}
          <input
            type="text"
            value={workspacePath}
            onChange={(e) => setWorkspacePath(e.target.value)}
            placeholder={t("e.g. E:\\QunWork\\QunWork (蜂群在此目录内工作)")}
            className="flex-1 rounded border border-line bg-paper px-2 py-1 text-[12px] outline-none font-mono"
          />
        </label>
        {/* swarm config */}
        <div className="flex items-center gap-4 mt-2 flex-wrap">
          <label className="flex items-center gap-1.5 text-[12px] text-muted">
            {t("Executor")}
            <select
              value={executorAgent}
              onChange={(e) => setExecutorAgent(e.target.value === "code" ? "code" : "cowork")}
              className="rounded border border-line bg-paper px-1.5 py-0.5 text-[12px] outline-none"
            >
              <option value="cowork">{t("Generalist")}</option>
              <option value="code">{t("Code engineer")}</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[12px] text-muted">
            {t("Max parallel")}
            <input
              type="number"
              min={1}
              max={8}
              value={maxParallel}
              onChange={(e) => setMaxParallel(Math.max(1, Math.min(8, Number(e.target.value) || 1)))}
              className="w-14 rounded border border-line bg-paper px-1.5 py-0.5 text-[12px] outline-none"
            />
          </label>
          <label className="flex items-center gap-1.5 text-[12px] text-muted">
            {t("Timeout (s)")}
            <input
              type="number"
              min={60}
              max={900}
              step={30}
              value={timeoutSeconds}
              onChange={(e) => setTimeoutSeconds(Math.max(60, Math.min(900, Number(e.target.value) || 300)))}
              className="w-16 rounded border border-line bg-paper px-1.5 py-0.5 text-[12px] outline-none"
            />
          </label>
          <button
            className={
              "px-4 py-1.5 rounded-full text-[13px] transition-colors disabled:opacity-40 " +
              (busy
                ? "bg-accent text-white swarm-run-btn"
                : "bg-accent text-white hover:bg-accent/85")
            }
            disabled={busy || !intent.trim()}
            onClick={run}
          >
            {busy ? t("Swarm running…") : t("Run swarm")}
          </button>
          {status && (
            <span className={"text-[12px] " + (status === "completed" ? "text-ok" : status === "failed" ? "text-danger" : "text-muted")}>
              {t("Status")}: {status}
              {runId && <span className="text-faint"> · {runId}</span>}
              {elapsed > 0 && <span className="text-faint"> · {fmtDuration(elapsed, t)}</span>}
            </span>
          )}
          {status === "completed" && runId && !showTemplateForm && (
            <button
              className="ml-auto shrink-0 rounded-lg border border-line px-2.5 py-1 text-[12.5px] text-muted hover:text-ink hover:border-lineStrong"
              onClick={() => setShowTemplateForm(true)}
              data-testid="swarm-save-template"
            >
              💾 {t("Save as template")}
            </button>
          )}
          {status === "completed" && runId && (
            <button
              className="shrink-0 rounded-lg border border-accent/50 px-2.5 py-1 text-[12.5px] text-accent hover:border-accent"
              onClick={() => void loadReport()}
              disabled={reportBusy}
              data-testid="swarm-report"
            >
              {reportBusy ? t("Generating…") : "📄 " + t("Coordination report")}
            </button>
          )}
          {showTemplateForm && (
            <span className="ml-auto shrink-0 flex items-center gap-2">
              <input
                className="w-44 rounded border border-line bg-paper px-2 py-1 text-[12px] outline-none focus:border-lineStrong"
                placeholder={t("Template title")}
                value={tplTitle}
                onChange={(e) => setTplTitle(e.target.value)}
                autoFocus
              />
              <button
                className="rounded-lg bg-accent px-2.5 py-1 text-[12.5px] text-white disabled:opacity-50"
                disabled={savingTpl}
                onClick={() => void saveTemplate()}
              >
                {savingTpl ? t("Saving…") : t("Save")}
              </button>
              <button
                className="rounded-lg border border-line px-2.5 py-1 text-[12.5px] text-muted hover:text-ink"
                onClick={() => setShowTemplateForm(false)}
              >
                {t("Cancel")}
              </button>
            </span>
          )}
          {tplError && <div className="text-[12px] text-danger mt-2">{tplError}</div>}
        </div>
        {/* G2 command deck: pause/resume + operator message, live while the run is active */}
        {(status === "running" || status === "pending") && (
          <div className="flex items-center gap-2 mt-2 flex-wrap" data-testid="swarm-deck">
            <button
              className="rounded-lg border border-line px-2.5 py-1 text-[12.5px] text-muted hover:text-ink hover:border-lineStrong"
              onClick={() => void togglePause()}
              data-testid="swarm-pause"
            >
              {paused ? "▶ " + t("Resume") : "⏸ " + t("Pause")}
            </button>
            <input
              className="flex-1 min-w-40 rounded-lg border border-line bg-paper px-2.5 py-1 text-[12.5px] outline-none focus:border-lineStrong"
              placeholder={t("Message to the swarm…")}
              value={deckMsg}
              onChange={(e) => setDeckMsg(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void sendMessage()}
            />
            <button
              className="rounded-lg bg-accent px-2.5 py-1 text-[12.5px] text-white disabled:opacity-50"
              disabled={!deckMsg.trim()}
              onClick={() => void sendMessage()}
              data-testid="swarm-send-message"
            >
              {t("Send")}
            </button>
          </div>
        )}
        {requeues.length > 0 && (
          <div className="mt-2 space-y-1.5">
            {requeues.map((r) => (
              <div key={r.task_id} className="rounded-lg border border-warn bg-panel px-3 py-2 flex items-center gap-2" data-testid={`swarm-requeue-${r.task_id}`}>
                <span className="flex-1 min-w-0 text-[12.5px]">
                  <span className="font-medium">{r.task_id}</span>
                  {r.reason ? <span className="text-muted"> — {r.reason}</span> : null}
                </span>
                <button
                  className="rounded-lg bg-ok/90 px-2.5 py-1 text-[12px] text-white"
                  onClick={() => void decideRequeue(r.task_id, true)}
                >
                  {t("Approve rerun")}
                </button>
                <button
                  className="rounded-lg border border-line px-2.5 py-1 text-[12px] text-muted hover:text-ink"
                  onClick={() => void decideRequeue(r.task_id, false)}
                >
                  {t("Skip")}
                </button>
              </div>
            ))}
          </div>
        )}
        {report && report.markdown && (
          <div className="mt-3 rounded-lg border border-line bg-panel p-3" data-testid="swarm-report-body">
            <div className="text-[12px] font-semibold mb-1.5">{t("Coordination report")}</div>
            <pre className="text-[11.5px] leading-relaxed whitespace-pre-wrap text-muted max-h-64 overflow-y-auto">
              {report.markdown.slice(0, 4000)}
            </pre>
            {report.report_path && (
              <div className="text-[11.5px] text-faint mt-1.5 truncate">
                {report.report_path}
              </div>
            )}
          </div>
        )}
        {deckError && <div className="text-[12px] text-danger mt-2">{deckError}</div>}
        {error && <div className="text-[12px] text-danger mt-2">{error}</div>}
      </div>

      <div className="flex-1 overflow-y-auto hairline-scroll swarm-scroll px-5 py-4 min-w-0">
        {!runId && !busy && history.length === 0 && (
          <p className="text-[13px] text-faint">
            {t("Send a goal above — the swarm will split it into tasks, run them, validate and converge.")}
          </p>
        )}

        {/* DAG + stats */}
        {(tasks.length > 0 || runId) && (
          <div className="flex gap-4 mb-4">
            {tasks.length > 0 && (
              <div className="rounded-xl border border-line bg-panel px-3 py-3">
                <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1">
                  {t("Task DAG")}
                </div>
                <DAGDiagram tasks={tasks} />
              </div>
            )}
            <div className="flex-1 min-w-0">
              <div className="rounded-xl border border-line bg-panel px-3.5 py-3 mb-2">
                <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
                  {t("Run stats")}
                </div>
                <div className="grid grid-cols-3 gap-2 text-[12px]">
                  <div>
                    <div className="text-faint">{t("Elapsed")}</div>
                    <div className="font-semibold">{fmtDuration(elapsed, t)}</div>
                  </div>
                  <div>
                    <div className="text-faint">{t("Confidence")}</div>
                    <div className="font-semibold">{doneTasks.length ? `${(avgConf * 100).toFixed(0)}%` : "—"}</div>
                  </div>
                  <div>
                    <div className="text-faint">{t("Tokens (est)")}</div>
                    <div className="font-semibold">{estTokens.toLocaleString()}</div>
                  </div>
                </div>
                <div className="mt-1.5 text-[11px] text-faint">
                  {t("Tasks")}: {doneTasks.length}/{tasks.length}
                </div>
              </div>
              {stale && (
                <div className="rounded-xl border border-warn bg-amber-50 px-3.5 py-2.5 mb-2 text-[12px] text-amber-800">
                  {t("This run stopped updating (server may have restarted). The run record is kept; try running again.")}
                </div>
              )}
            </div>
          </div>
        )}

        {finalReport && (
          <div className="mb-4">
            <div className="flex items-center justify-between mb-1.5">
              <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold">
                {t("Final report")}
              </div>
              <button
                className="text-[11px] text-faint hover:text-ink"
                onClick={() => navigator.clipboard?.writeText(finalReport).catch(() => {})}
              >
                {t("Copy")}
              </button>
            </div>
            <div className="rounded-xl border border-lineStrong bg-panel px-4 py-3">
              <pre className="text-[12.5px] text-ink whitespace-pre-wrap break-words font-sans leading-relaxed max-h-[72vh] overflow-y-auto">
                {finalReport}
              </pre>
              {reportPath && (
                <div className="mt-2 pt-2 border-t border-line text-[11px] text-faint break-all">
                  📄 {t("Saved to")}: {reportPath}
                </div>
              )}
            </div>
          </div>
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
                  <pre className="text-[11.5px] text-muted mt-1.5 whitespace-pre-wrap break-words font-sans leading-snug max-h-20 overflow-y-auto">
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
                <div className="text-[12px] text-muted whitespace-pre-wrap break-words">{th.text}</div>
              </div>
            ))}
          </div>
        )}

        {governance.length > 0 && (
          <div className="mb-4">
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("Governance report")}
            </div>
            {govStats.size > 0 && (
              <div className="text-[11.5px] text-muted mb-1.5 flex flex-wrap gap-x-2.5">
                {[...govStats.entries()].map(([action, n]) => (
                  <span key={action} className="whitespace-nowrap">
                    🛡 {action} <span className="text-faint">×{n}</span>
                  </span>
                ))}
              </div>
            )}
            {governance.map((g, i) => (
              <div key={i} className="text-[11px] text-faint font-mono mb-0.5">{g}</div>
            ))}
          </div>
        )}

        {templates.length > 0 && (
          <div className="mb-3">
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("Templates")}
            </div>
            {templates.map((tmpl) => (
              <div
                key={tmpl.id}
                className="w-full text-left rounded-lg border border-line bg-panel px-3 py-2 mb-1.5 hover:border-lineStrong flex items-center gap-2"
              >
                <button className="flex-1 min-w-0" onClick={() => applyTemplate(tmpl)} data-testid={`swarm-template-${tmpl.id}`}>
                  <span className="block text-[12.5px] font-medium truncate">{tmpl.title}</span>
                  <span className="block text-[11.5px] text-faint truncate">{tmpl.intent}</span>
                  {(tmpl.runs_count ?? 0) > 0 && (
                    <span className="block text-[11px] text-muted truncate">
                      {(tmpl.runs_count ?? 0)} {t("runs")} · {(tmpl.success_count ?? 0)} {t("ok")}
                    </span>
                  )}
                </button>
                <button
                  className="shrink-0 text-[11px] text-muted hover:text-danger px-1.5 py-0.5 rounded"
                  title={t("Delete template")}
                  aria-label={t("Delete template")}
                  onClick={() => void removeTemplate(tmpl.id)}
                >
                  ✕
                </button>
              </div>
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
