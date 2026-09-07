import { useEffect, useMemo, useRef, useState } from "react";
import {
  addSwarmTemplate,
  deleteSwarmTemplate,
  deleteSwarmLesson,
  getCoordinationReport,
  getHealth,
  getOrchestrateHistory,
  dissolveRun,
  getOrchestrateRun,
  listSwarmLessons,
  listSwarmTemplates,
  orchestrate,
  orchestrateControl,
  type CoordinationReport,
  type ConvergenceReport,
  type DecisionTraceEntry,
  type OrchestrationDegradation,
  type OrchestrationHistoryItem,
  type OrchestrationRunSnapshot,
  type SwarmLesson,
  type SwarmTemplate,
} from "../api";
import { useT } from "../i18n";
import { DecisionReplayTimeline, type SwarmDecisionRow } from "./DecisionReplay";

/** Fallback workspace hint: the server's configured default, if any. */
async function defaultWorkspaceHint(): Promise<string | undefined> {
  try {
    const h = await getHealth();
    return h.default_workspace ?? undefined;
  } catch {
    return undefined;
  }
}

// P0 建议3 保留分支 A/B: flatten a run snapshot's plan + outcomes into a
// per-task table, so two branches of the same parent can be compared side by side.
interface TaskOutcome {
  id: string;
  status: string;
  confidence: number;
  result: string;
}

function extractTaskOutcomes(snap: OrchestrationRunSnapshot): TaskOutcome[] {
  const byId = new Map<string, TaskOutcome>();
  for (const ev of snap.events) {
    const p = ev.payload as Record<string, unknown>;
    if (ev.kind === "plan_ready") {
      for (const raw of (p.tasks as Record<string, unknown>[]) ?? []) {
        byId.set(String(raw.id), { id: String(raw.id), status: "pending", confidence: 0, result: "" });
      }
    } else if (ev.kind === "task_done") {
      const t = byId.get(String(p.id));
      if (t) {
        t.status = String(p.status ?? "");
        t.confidence = Number(p.confidence ?? 0);
      }
    } else if (ev.kind === "task_result") {
      const t = byId.get(String(p.id));
      if (t && typeof p.result === "string") t.result = p.result;
    }
  }
  return Array.from(byId.values());
}

interface TaskView {
  id: string;
  description: string;
  deps: string[];
  status: string;
  confidence: number;
  result: string;
  agent: string;
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

/** 7x24 长程任务 (突破二): LoopCoop 收敛曲线 — SVG 折线, 展示每轮收敛度。 */
function ConvergenceCurve({ report }: { report: ConvergenceReport }) {
  const curve = report.convergence_curve ?? [];
  const W = 280;
  const H = 64;
  const PAD = 6;
  const maxV = Math.max(1, ...curve, report.final_convergence ?? 0);
  const pts = curve
    .map((v, i) => {
      const x = curve.length <= 1 ? PAD : PAD + (i / (curve.length - 1)) * (W - 2 * PAD);
      const y = H - PAD - (v / maxV) * (H - 2 * PAD);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <div className="mb-3">
      <div className="flex items-center gap-2 text-[11px] text-muted mb-1">
        <span>🔁 LoopCoop</span>
        <span className="text-faint">|λ₂|={report.gap?.toFixed(4) ?? "?"}</span>
        <span className="text-faint">· 理论 {report.theoretical_rounds ?? "?"} 轮 → 99%</span>
        <span className="text-faint">· 实测 {report.iterations ?? "?"} 轮</span>
        <span className={report.converged ? "text-emerald-600" : "text-amber-600"}>
          {report.converged ? "✓ 收敛" : report.stalled ? "⚠ 停滞" : "… 进行中"}
        </span>
      </div>
      {curve.length >= 2 ? (
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="shrink-0" data-testid="convergence-curve">
          <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="#475569" strokeWidth="1" />
          <line x1={PAD} y1={PAD} x2={PAD} y2={H - PAD} stroke="#475569" strokeWidth="1" />
          <polyline points={pts} fill="none" stroke="#2563eb" strokeWidth="1.5" />
          {curve.map((v, i) => {
            const x = PAD + (i / (curve.length - 1)) * (W - 2 * PAD);
            const y = H - PAD - (v / maxV) * (H - 2 * PAD);
            return <circle key={i} cx={x} cy={y} r="2" fill="#2563eb" />;
          })}
        </svg>
      ) : (
        <div className="text-[11px] text-faint">{(report.final_convergence ?? 0).toFixed(2)}</div>
      )}
    </div>
  );
}

/** 7x24 长程任务 (突破五): 分形降级轨迹 — L1→L6 降级链一览。 */
function DegradationTrace({ rows }: { rows: OrchestrationDegradation[] }) {
  const actionLabel: Record<string, string> = {
    continue: "继续重试",
    downgrade_model: "降模型",
    narrow_scope: "缩范围",
    sync_mode: "同步(人工)",
    checkpoint_pause: "检查点暂停",
    alert_archive: "告警归档",
  };
  return (
    <div className="mt-1">
      <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">分形降级轨迹</div>
      <div className="space-y-1">
        {rows.map((d, i) => (
          <div key={i} className="flex items-center gap-2 text-[11px] font-mono">
            <span className="text-faint shrink-0">L{d.level}</span>
            <span className="text-muted shrink-0 w-20">{actionLabel[d.action] ?? d.action}</span>
            <span className="text-faint shrink-0 w-24 truncate">{d.task_id}</span>
            <span className="text-faint shrink-0">保真 {((d.fidelity ?? 0) * 100).toFixed(0)}%</span>
            {d.error && <span className="text-faint truncate flex-1">{d.error}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Simple vertical DAG: each task is a row; dependency arrows link parent -> child. */
function DAGDiagram({ tasks }: { tasks: TaskView[] }) {  const ROW = 64;
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
export function SwarmView({
  onBack,
  workspace,
  launch,
}: {
  onBack: () => void;
  workspace?: string;
  // 统一入口 (2026-09-07): 主会话 Composer 蜂群模式提交 → {text, seq} 递增触发。
  launch?: { text: string; seq: number };
}) {
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
  // Refine 机制: 蜂群经验 (自进化闭环的学习成果)
  const [lessons, setLessons] = useState<SwarmLesson[]>([]);
  const [lessonsOpen, setLessonsOpen] = useState(true);
  const [lessonKind, setLessonKind] = useState<string>("");
  // G2 command deck
  const [paused, setPaused] = useState(false);
  const [deckMsg, setDeckMsg] = useState("");
  const [deckError, setDeckError] = useState<string | null>(null);
  // P0 建议3: fork-a-sub-task form state
  const [showFork, setShowFork] = useState(false);
  const [forkId, setForkId] = useState("");
  const [forkDesc, setForkDesc] = useState("");
  const [forkDeps, setForkDeps] = useState("");
  const [forkAgent, setForkAgent] = useState("cowork");
  // P0 建议3 保留分支 A/B: side-by-side outcome comparison parent vs fork.
  const [compare, setCompare] = useState<{ parent: OrchestrationRunSnapshot; fork: OrchestrationRunSnapshot } | null>(null);
  const [requeues, setRequeues] = useState<
    Array<{ task_id: string; attempt?: number; reason?: string }>
  >([]);
  // Benchmark showcase: coordination report for the current (completed) run
  const [report, setReport] = useState<CoordinationReport | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  // 13 Agent 影子模式: 决策回放时间轴 (worker 报告的工具选择/权限/scope/审批)
  const [decisions, setDecisions] = useState<SwarmDecisionRow[]>([]);
  // 7x24 长程任务: 收敛曲线 (突破二 convergence_report) + 降级轨迹 (突破五)
  const [convergence, setConvergence] = useState<ConvergenceReport | null>(null);
  const [degradations, setDegradations] = useState<OrchestrationDegradation[]>([]);
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

  // Refine 机制: 蜂群经验 (自进化闭环学习成果)
  const loadLessons = () => {
    const ws = workspacePath?.trim() || undefined;
    listSwarmLessons(lessonKind || undefined, 50, ws)
      .then((r) => mounted.current && setLessons(r.lessons ?? []))
      .catch(() => {});
  };

  const removeLesson = async (id: number) => {
    const ws = workspacePath?.trim() || undefined;
    const ok = await deleteSwarmLesson(id, ws);
    if (ok.ok) setLessons((prev) => prev.filter((x) => x.id !== id));
  };

  useEffect(() => {
    loadTemplates();
    loadLessons();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // workspace 变化时刷新蜂群经验 (经验库按 workspace 隔离)。
  useEffect(() => {
    loadLessons();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspacePath]);

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

  // 统一入口: Composer 蜂群模式每次提交让 seq 递增 → 触发一次 run。
  const lastLaunchSeq = useRef(0);
  useEffect(() => {
    if (!launch || launch.seq === lastLaunchSeq.current) return;
    lastLaunchSeq.current = launch.seq;
    if (launch.text.trim()) void run(launch.text.trim());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [launch?.seq]);

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
    pendingTmplRef.current = tmpl.id; // reuse is tallied when the run finishes (5.2.1 track record)
    void run(tmpl.intent); // 统一入口: 应用模板即开跑 (旧「填入输入框再按 Run」并一步)
  };

  const applySnapshot = (snap: OrchestrationRunSnapshot) => {
    setStatus(snap.status);
    if (snap.final) setFinalReport(snap.final);
    const tasks: TaskView[] = [];
    const thoughts: Thought[] = [];
    const gov: string[] = [];
    const decisionsAcc: SwarmDecisionRow[] = [];
    for (const ev of snap.events) {
      const p = ev.payload as Record<string, unknown>;
      if (ev.kind === "plan_ready") {
        for (const raw of (p.tasks as Record<string, unknown>[]) ?? []) {
          tasks.push({ id: String(raw.id), description: String(raw.description ?? ""), deps: (raw.deps as string[]) ?? [], status: "pending", confidence: 0, result: "", agent: String(raw.agent ?? "") });
        }
      } else if (ev.kind === "task_injected") {
        // P0 建议3: a fork task joined the live plan.
        tasks.push({ id: String(p.id), description: String(p.description ?? ""), deps: (p.deps as string[]) ?? [], status: "pending", confidence: 0, result: "", agent: String(p.agent ?? "") });
      } else if (ev.kind === "task_retargeted") {
        const td = tasks.find((x) => x.id === p.id);
        if (td) td.agent = String(p.agent ?? "");
      } else if (ev.kind === "task_started") {
        const td = tasks.find((x) => x.id === p.id);
        if (td) td.status = "running";
      } else if (ev.kind === "task_result") {
        const td = tasks.find((x) => x.id === p.id);
        if (td && typeof p.result === "string") td.result = p.result;
      } else if (ev.kind === "task_done") {
        const td = tasks.find((x) => x.id === p.id);
        if (td) {
          td.status = String(p.status ?? "");
          td.confidence = Number(p.confidence ?? 0);
        }
      } else if (ev.kind === "worker_thought") {
        thoughts.push({ worker: String(p.worker ?? ""), task_id: String(p.task_id ?? ""), text: String(p.text ?? "") });
      } else if (ev.kind === "decision_trace") {
        // 13 影子模式: 累积 worker 报告的决策轨迹 (engine._record_decision 写入)
        const entry = p.entry as DecisionTraceEntry | undefined;
        if (entry) {
          decisionsAcc.push({
            worker: String(p.worker ?? ""),
            task_id: String(p.task_id ?? ""),
            agent_id: typeof p.agent_id === "string" ? p.agent_id : undefined,
            entry,
          });
        }
      } else if (ev.kind === "governance") {
        gov.push(`[step ${p.step}] ${p.action}: ${p.reason}`);
      } else if (ev.kind === "convergence_report") {
        // 7x24 长程任务 (突破二): LoopCoop 收敛报告 — 谱隙/理论轮数/收敛曲线。
        setConvergence(p as unknown as ConvergenceReport);
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
    setDecisions(decisionsAcc);
    // 7x24 长程任务 (突破五): 降级轨迹直接来自 run snapshot。
    setDegradations(snap.degradations ?? []);
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

  // -- P0 建议3: 蜂群指挥台 — fork a sub-task + retarget an agent -------------
  const retarget = async (taskId: string, agent: string) => {
    if (!runId) return;
    const res = await orchestrateControl(runId, "retarget", { task_id: taskId, agent });
    setDeckError(res.ok ? null : res.error || "retarget failed");
    if (res.ok) {
      setTasks((prev) => prev.map((t) => (t.id === taskId ? { ...t, agent } : t)));
    }
  };

  const injectTask = async () => {
    if (!runId || !forkDesc.trim()) return;
    const deps = forkDeps.split(",").map((s) => s.trim()).filter(Boolean);
    const res = await orchestrateControl(runId, "task_inject", {
      task_id: forkId.trim() || `fork-${Date.now().toString(36)}`,
      description: forkDesc.trim(),
      deps,
      agent: forkAgent,
    });
    setDeckError(res.ok ? null : res.error || "inject failed");
    if (res.ok) {
      setShowFork(false);
      setForkId("");
      setForkDesc("");
      setForkDeps("");
      setForkAgent("cowork");
    }
  };

  // P0 建议3 保留分支 A/B: fetch the parent run and compare outcomes side by side.
  const runCompare = async (parentId: string) => {
    if (!runId) return;
    setDeckError(null);
    try {
      const [parent, fork] = await Promise.all([
        getOrchestrateRun(parentId),
        getOrchestrateRun(runId),
      ]);
      setCompare({ parent, fork });
    } catch {
      setDeckError("compare failed");
    }
  };

  // -- P0 增量2: task-group lifecycle — dissolve a finished run ---------------
  const dissolve = async (rid: string) => {
    const res = await dissolveRun(rid);
    setDeckError(res.ok ? null : res.error || "dissolve failed");
    if (res.ok) await loadHistory();
  };

  const loadReport = async () => {
    if (!runId) return;
    setReportBusy(true);
    const res = await getCoordinationReport(runId);
    setReportBusy(false);
    if (res.ok) setReport(res);
    else setDeckError(res.error || "report failed");
  };

  const run = async (goalOverride?: string) => {
    const goal = (goalOverride ?? intent).trim();
    if (!goal) return;
    // 统一入口后允许在上一 run 观察期内直接提交新任务: 先收尾旧轮询再重开。
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (goalOverride) setIntent(goal); // 模板保存等下游逻辑仍以 intent 为源
    setBusy(true);
    setError(null);
    setStale(false);
    setRunId(null);
    setStatus("running");
    setTasks([]);
    setThoughts([]);
    setGovernance([]);
    setDecisions([]);
    setConvergence(null);
    setDegradations([]);
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
            // Refine 机制: run 结束后刷新蜂群经验 (本次运行蒸馏出的学习成果)。
            loadLessons();
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
        {/* 任务意图输入已迁往主会话 Composer (蜂群模式即发起入口) — 这里只留运行参数。 */}
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
          {/* 启动按钮已由主会话 Composer (蜂群模式) 取代 — 见 App.tsx launch 通路。 */}
          {!intent.trim() && !busy && (
            <span className="text-[12px] text-faint">
              ✍️ {t("Type a goal in the chat box below to launch the swarm.")}
            </span>
          )}
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
            <button
              className="rounded-lg border border-line px-2.5 py-1 text-[12.5px] text-muted hover:text-ink hover:border-lineStrong"
              onClick={() => setShowFork((v) => !v)}
              data-testid="swarm-fork-toggle"
            >
              {showFork ? t("Cancel") : "＋ " + t("Fork a task")}
            </button>
          </div>
        )}
        {showFork && (
          <div className="mt-2 rounded-lg border border-line bg-paper p-2.5 space-y-2" data-testid="swarm-fork-form">
            <div className="flex gap-2">
              <input
                className="flex-1 rounded-lg border border-line bg-paper px-2.5 py-1 text-[12.5px] outline-none focus:border-lineStrong"
                placeholder={t("New task description (e.g. Write the appendix)")}
                value={forkDesc}
                onChange={(e) => setForkDesc(e.target.value)}
              />
            </div>
            <div className="flex gap-2">
              <input
                className="flex-1 rounded-lg border border-line bg-paper px-2.5 py-1 text-[12.5px] outline-none focus:border-lineStrong"
                placeholder={t("Depends on (task ids, comma-separated — optional)")}
                value={forkDeps}
                onChange={(e) => setForkDeps(e.target.value)}
              />
              <select
                className="rounded-lg border border-line bg-paper px-2 py-1 text-[12.5px] outline-none"
                value={forkAgent}
                onChange={(e) => setForkAgent(e.target.value)}
              >
                <option value="cowork">cowork</option>
                <option value="code">code</option>
              </select>
              <button
                className="rounded-lg bg-accent px-2.5 py-1 text-[12.5px] text-white disabled:opacity-50 shrink-0"
                disabled={!forkDesc.trim()}
                onClick={() => void injectTask()}
                data-testid="swarm-fork-submit"
              >
                {t("Add to plan")}
              </button>
            </div>
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
                  {task.agent && (
                    <span className="text-[10.5px] text-faint border border-line rounded px-1.5 py-0.5" data-testid={`task-agent-${task.id}`}>
                      {task.agent}
                    </span>
                  )}
                  {task.confidence > 0 && (
                    <span className="text-[11px] text-faint">{(task.confidence * 100).toFixed(0)}%</span>
                  )}
                  {task.status === "pending" && (
                    <select
                      className="text-[11px] bg-panel border border-line rounded px-1 py-0.5 outline-none"
                      value={task.agent || "cowork"}
                      data-testid={`swarm-retarget-${task.id}`}
                      onChange={(e) => retarget(task.id, e.target.value)}
                    >
                      <option value="cowork">cowork</option>
                      <option value="code">code</option>
                    </select>
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

        {/* 13 影子模式: 决策回放时间轴 — 用户拖滑块看每一步 AI 看到什么/考虑什么/选了什么 */}
        <DecisionReplayTimeline decisions={decisions} />

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

        {/* 7x24 长程任务: LoopCoop 收敛曲线 (突破二) + 分形降级轨迹 (突破五) */}
        {(convergence || degradations.length > 0) && (
          <div className="mb-4 rounded-lg border border-line bg-panel p-3">
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-2">
              {t("7x24 long-run telemetry")}
            </div>
            {convergence && <ConvergenceCurve report={convergence} />}
            {degradations.length > 0 && <DegradationTrace rows={degradations} />}
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

        {/* Refine 机制: 蜂群经验 (自进化闭环的学习成果) — 始终显示,
            空状态提示"跑一次蜂群后自动沉淀" */}
        <div className="mb-3" data-testid="swarm-lessons">
          <div className="flex items-center gap-2 mb-1.5">
            <button
              className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold flex items-center gap-1"
              onClick={() => setLessonsOpen((v) => !v)}
              aria-expanded={lessonsOpen}
            >
              <span className={lessonsOpen ? "" : "rotate-90"} aria-hidden>▶</span>
              🧠 {t("Swarm lessons")}
              <span className="text-[10px] text-faint normal-case font-normal">
                ({lessons.length})
              </span>
            </button>
            <select
              value={lessonKind}
              onChange={(e) => {
                const kind = e.target.value;
                setLessonKind(kind);
                const ws = workspacePath?.trim() || undefined;
                listSwarmLessons(kind || undefined, 50, ws)
                  .then((r) => mounted.current && setLessons(r.lessons ?? []))
                  .catch(() => {});
              }}
              className="ml-auto text-[11px] bg-panel border border-line rounded px-1.5 py-0.5 text-muted"
              aria-label={t("Filter lessons")}
            >
              <option value="">{t("All")}</option>
              <option value="lesson">{t("Lessons")}</option>
              <option value="skill_hint">{t("Skill hints")}</option>
              <option value="task_template">{t("Task templates")}</option>
            </select>
          </div>
          {lessonsOpen && (
            <div className="space-y-1.5">
              {lessons.length === 0 ? (
                <div className="rounded-lg border border-dashed border-line bg-panel px-3 py-2.5 text-[11.5px] text-faint">
                  {t("No swarm lessons yet — run a swarm and its lessons (success strategies, pitfalls, self-made tools) will be distilled here automatically.")}
                </div>
              ) : (
                lessons.map((ls) => (
                  <div
                    key={ls.id}
                    className="w-full rounded-lg border border-line bg-panel px-3 py-2 flex items-start gap-2"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 mb-0.5">
                        <span
                          className={
                            "text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded " +
                            (ls.kind === "lesson"
                              ? "bg-emerald-500/10 text-emerald-600"
                              : ls.kind === "skill_hint"
                                ? "bg-indigo-500/10 text-indigo-500"
                                : "bg-amber-500/10 text-amber-600")
                          }
                        >
                          {ls.kind === "lesson"
                            ? t("Lesson")
                            : ls.kind === "skill_hint"
                              ? t("Skill")
                              : t("Template")}
                        </span>
                        <span className="text-[12px] font-medium truncate">{ls.title}</span>
                      </div>
                      <div className="text-[11.5px] text-muted whitespace-pre-wrap break-words">{ls.body}</div>
                      {(ls.use_count ?? 0) > 0 && (
                        <div className="text-[10.5px] text-faint mt-0.5">
                          🔁 {ls.use_count} {t("reuses")}
                          {ls.version && ls.version > 1 ? ` · v${ls.version}` : ""}
                          {ls.source_run_id ? ` · ${ls.source_run_id}` : ""}
                        </div>
                      )}
                    </div>
                    <button
                      className="shrink-0 text-[11px] text-muted hover:text-danger px-1.5 py-0.5 rounded"
                      title={t("Delete lesson")}
                      aria-label={t("Delete lesson")}
                      onClick={() => void removeLesson(ls.id)}
                    >
                      ✕
                    </button>
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        {compare && (
          <div className="rounded-xl border border-line bg-panel px-3.5 py-3 mb-3" data-testid="branch-compare">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-[12px] font-semibold">{t("Branch A/B comparison")}</span>
              <button className="text-[11px] text-faint hover:text-ink" onClick={() => setCompare(null)}>
                ✕ {t("Close")}
              </button>
            </div>
            <div className="flex items-center gap-3 text-[11px] text-faint mb-2">
              <span className="flex-1 truncate">{compare.parent.intent}</span>
              <span aria-hidden>→</span>
              <span className="flex-1 truncate">{compare.fork.intent}</span>
            </div>
            <div className="space-y-1">
              {(() => {
                const parent = extractTaskOutcomes(compare.parent);
                const fork = extractTaskOutcomes(compare.fork);
                const ids = Array.from(new Set([...parent.map((t) => t.id), ...fork.map((t) => t.id)]));
                const byId = (arr: TaskOutcome[], id: string) => arr.find((t) => t.id === id);
                return ids.map((id) => {
                  const p = byId(parent, id);
                  const f = byId(fork, id);
                  const diff = (p?.status ?? "—") !== (f?.status ?? "—");
                  return (
                    <div key={id} className="flex items-start gap-2 text-[12px]">
                      <span className="text-faint font-mono w-12 shrink-0">{id}</span>
                      <span className={"flex-1 truncate " + (diff ? "text-amber-500" : "")}>
                        {p ? `${p.status}${p.confidence ? ` ${(p.confidence * 100).toFixed(0)}%` : ""}` : "—"}
                      </span>
                      <span className={"flex-1 truncate " + (diff ? "text-amber-500" : "")}>
                        {f ? `${f.status}${f.confidence ? ` ${(f.confidence * 100).toFixed(0)}%` : ""}` : "—"}
                      </span>
                    </div>
                  );
                });
              })()}
            </div>
          </div>
        )}

        {history.length > 0 && (
          <div>
            <div className="text-[11px] uppercase tracking-[0.07em] text-faint font-semibold mb-1.5">
              {t("History")}
            </div>
            {history.map((h) => (
              <div
                key={h.run_id}
                className={"rounded-lg border border-line bg-panel px-3 py-2 mb-1.5 flex items-center gap-2" + (h.parent_run_id ? " ml-4 border-dashed" : "")}
              >
                <button
                  className="flex-1 min-w-0 text-left"
                  onClick={() => openRun(h.run_id)}
                  data-testid={`history-${h.run_id}`}
                >
                  <div className="flex items-center gap-2">
                    {h.parent_run_id && (
                      <span className="text-[10px] text-faint border border-line rounded px-1" title={h.parent_run_id}>
                        {t("branch")}
                      </span>
                    )}
                    <span className={"text-[12px] " + (h.status === "completed" ? "text-ok" : h.status === "failed" ? "text-danger" : h.status === "dissolved" ? "text-faint" : "text-muted")}>
                      {h.status}
                    </span>
                    <span className="text-[12.5px] flex-1 truncate">{h.intent}</span>
                    <span className="text-[10.5px] text-faint">{new Date(h.created_at * 1000).toLocaleString()}</span>
                  </div>
                </button>
                {(h.status === "completed" || h.status === "failed") && (
                  <button
                    className="text-[11px] text-faint border border-line rounded px-1.5 py-0.5 hover:text-ink hover:border-lineStrong shrink-0"
                    onClick={() => void dissolve(h.run_id)}
                    data-testid={`dissolve-${h.run_id}`}
                    title={t("Dissolve this finished run — release the task group")}
                  >
                    {t("Dissolve")}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {runId &&
          (() => {
            const parentId = history.find((h) => h.run_id === runId)?.parent_run_id;
            if (!parentId) return null;
            return (
              <button
                className="mt-2 rounded-lg border border-line px-2.5 py-1 text-[12px] text-muted hover:text-ink hover:border-lineStrong"
                onClick={() => void runCompare(parentId)}
                data-testid="swarm-compare-parent"
              >
                {t("Compare with parent branch")}
              </button>
            );
          })()}
      </div>
    </div>
  );
}
