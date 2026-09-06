import { useCallback, useEffect, useState } from "react";
import {
  getOirLongrunConfig,
  getOirLongrunTelemetry,
  setOirLongrunConfig,
  type OirLongrunConfig,
  type OirLongrunTelemetrySnapshot,
} from "../api";
import { useT } from "../i18n";

/**
 * OIR longrun 集成面板（QunWork 7×24 握手任务消费入口）。
 *
 * QunWork 原生 Scheduler tick 驱动 OIR long_horizon：本面板开关/配置
 * "持续概念索引增长"共享任务（doc_dir/glob/batch），并每 30s 回读 OIR 侧
 * 任务状态与趋势（经网关遥测回显 longrun_telemetry.json）。
 */
export function OirLongrunPanel() {
  const t = useT();
  const [cfg, setCfg] = useState<OirLongrunConfig | null>(null);
  const [snap, setSnap] = useState<OirLongrunTelemetrySnapshot | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ enabled: false, doc_dir: "研究文档", glob: "*.md", batch: 3 });

  const load = useCallback(async () => {
    try {
      const c = await getOirLongrunConfig();
      setCfg(c);
      setForm({ enabled: c.enabled, doc_dir: c.doc_dir, glob: c.glob, batch: c.batch });
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  // 遥测回读：经后端 /v1/7x24/oir-longrun/telemetry 读网关 reflect_telemetry 落盘文件
  const loadSnapshot = useCallback(async () => {
    try {
      const snap = await getOirLongrunTelemetry();
      setSnap(snap);
    } catch {
      /* best-effort：网关未写遥测时静默 */
    }
  }, []);

  useEffect(() => {
    void load();
    void loadSnapshot();
    const timer = setInterval(() => void loadSnapshot(), 30_000);
    return () => clearInterval(timer);
  }, [load, loadSnapshot]);

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      const c = await setOirLongrunConfig({
        enabled: form.enabled,
        doc_dir: form.doc_dir.trim() || "研究文档",
        glob: form.glob.trim() || "*.md",
        batch: Math.max(1, Number(form.batch) || 3),
      });
      setCfg(c);
      setForm({ enabled: c.enabled, doc_dir: c.doc_dir, glob: c.glob, batch: c.batch });
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  };

  const task = snap?.task;
  const gr = snap?.growth_report;
  const days = snap?.trend?.days ? Object.keys(snap.trend.days).length : 0;

  return (
    <div className="grid gap-3">
      <div className="rounded-lg border border-line bg-paper px-3 py-2.5">
        <div className="text-[10.5px] uppercase tracking-wide text-faint mb-1.5">
          {t("OIR longrun — shared 7×24 task driven by QunWork scheduler")}
        </div>
        {err && <div className="text-[12px] text-red-400 mb-2">{err}</div>}
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-[12.5px] text-muted">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
              data-testid="oir-longrun-enabled"
            />
            {t("Enabled (per tick: submit/advance/complete)")}
          </label>
          <label className="text-[12.5px] text-muted">
            {t("Doc dir")}:
            <input
              className="ml-1 rounded border border-line bg-base px-1.5 py-0.5 text-[12.5px]"
              value={form.doc_dir}
              onChange={(e) => setForm((f) => ({ ...f, doc_dir: e.target.value }))}
            />
          </label>
          <label className="text-[12.5px] text-muted">
            {t("Glob")}:
            <input
              className="ml-1 rounded border border-line bg-base px-1.5 py-0.5 text-[12.5px]"
              value={form.glob}
              onChange={(e) => setForm((f) => ({ ...f, glob: e.target.value }))}
            />
          </label>
          <label className="text-[12.5px] text-muted">
            {t("Batch")}:
            <input
              type="number"
              min={1}
              className="ml-1 w-14 rounded border border-line bg-base px-1.5 py-0.5 text-[12.5px]"
              value={form.batch}
              onChange={(e) => setForm((f) => ({ ...f, batch: Number(e.target.value) }))}
            />
          </label>
          <button
            className="rounded-md border border-line bg-paper px-2.5 py-1 text-[12.5px] hover:bg-base disabled:opacity-50"
            disabled={saving}
            onClick={() => void save()}
            data-testid="oir-longrun-save"
          >
            {saving ? t("Saving…") : t("Save")}
          </button>
        </div>
        {cfg?.goal_id && (
          <div className="mt-1.5 text-[11px] text-faint">
            {t("Shared task")}: {cfg.goal_id}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Phase")}</div>
          <div className="text-[18px] font-semibold" data-testid="oir-longrun-phase">
            {task?.phase ?? "—"}
          </div>
        </div>
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Documents")}</div>
          <div className="text-[18px] font-semibold">
            {task?.completed_documents ?? 0}/{task?.total_documents ?? "—"}
          </div>
        </div>
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Trend days")}</div>
          <div className="text-[18px] font-semibold">{days}</div>
        </div>
        <div className="rounded-lg border border-line bg-paper px-3 py-2">
          <div className="text-[10.5px] uppercase tracking-wide text-faint">{t("Growth")}</div>
          <div className="text-[18px] font-semibold">{gr?.direction ?? "—"}</div>
          {gr?.note && <div className="text-[10.5px] text-faint">{gr.note}</div>}
        </div>
      </div>
    </div>
  );
}
