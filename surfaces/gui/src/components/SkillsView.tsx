import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  buildSkillAutofix,
  checkAllSkillsCompatibility,
  checkSkillCompatibility,
  deleteSkill,
  exportSkill,
  generateSkillLock,
  getSkillLock,
  getSkillsPath,
  getSkillSecurity,
  getSkillVersions,
  hornetEmergenceToSkill,
  importSkill,
  listSkills,
  pickFolderViaServer,
  promoteSkill,
  rateSkill,
  rescanSkillSecurity,
  setSkillsPath as apiSetSkillsPath,
  type SkillCompatibilityReport,
  type SkillInfo,
  type SkillLockInfo,
  type SkillSecurityReport,
  type SkillVersions,
} from "../api";
import { useT } from "../i18n";

export default function SkillsView() {
  const t = useT();
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string>("");
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const [skillsError, setSkillsError] = useState(false);
  // 批量兼容扫描 / 自动修复
  const [scanningAll, setScanningAll] = useState(false);
  // 技能目录路径配置
  const [skillsPath, setSkillsPath] = useState<string | null>(null);
  const [skillsPathBusy, setSkillsPathBusy] = useState(false);
  const loadSkillsPath = useCallback(async () => {
    try {
      const r = await getSkillsPath();
      if (r.ok && r.path) setSkillsPath(r.path);
    } catch {
      /* 引擎不可用时不显示路径 */
    }
  }, []);
  useEffect(() => { loadSkillsPath(); }, [loadSkillsPath]);
  const [allReports, setAllReports] = useState<SkillCompatibilityReport[]>([]);
  const [autofixPanel, setAutofixPanel] = useState<{
    title?: string;
    intent?: string;
    rubric?: Array<[string, string]>;
    steps?: Array<{ skill_name: string; action: string; note?: string }>;
  } | null>(null);
  // P1-8: 正在调用 emergence → skill
  const [generating, setGenerating] = useState(false);

  const flashRef = useRef<(msg: string) => void>(() => {});
  const flash = useCallback((msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(""), 4000);
  }, []);
  useEffect(() => {
    flashRef.current = flash;
  }, [flash]);

  const refresh = useCallback(async () => {
    try {
      const data = await listSkills();
      setSkills(data.skills ?? []);
      setSkillsError(false);
    } catch {
      setSkills([]);
      setSkillsError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  const runBatchCompat = useCallback(async () => {
    setScanningAll(true);
    try {
      const r = await checkAllSkillsCompatibility();
      if (r.ok) {
        setAllReports(r.reports ?? []);
        flashRef.current(
          t("{count} skills scanned", { count: r.reports?.length ?? 0 }) +
            " · " +
            t("{count} incompatible", {
              count: (r.reports ?? []).filter((x) => !x.compatible).length,
            }),
        );
      }
    } finally {
      setScanningAll(false);
    }
  }, [t]);

  const runAutofix = useCallback(async (names?: string[]) => {
    const r = await buildSkillAutofix(names);
    if (r.ok) {
      setAutofixPanel({
        title: r.title,
        intent: r.intent,
        rubric: r.reviewer_rubric,
        steps: r.step_plan,
      });
      flashRef.current(
        t("Autofix plan built") + (r.affected_skills?.length ? ` · ${r.affected_skills.length}` : ""),
      );
    } else {
      flashRef.current(t("Autofix failed"));
    }
  }, [t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleRate = async (name: string, score: number) => {
    const res = await rateSkill(name, score);
    if (res.ok) flash(t("Rated"));
    else flash(t("Rating failed") + (res.error ? `: ${res.error}` : ""));
    refresh();
  };

  const handleExport = async (name: string) => {
    const res = await exportSkill(name);
    if (!res.ok || !res.zip_base64) {
      flash(t("Export failed") + (res.error ? `: ${res.error}` : ""));
      return;
    }
    const bin = atob(res.zip_base64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name}.zip`;
    a.click();
    URL.revokeObjectURL(url);
    flash(t("Exported") + ` ${name}.zip`);
  };

  const handleImportFile = async (file: File) => {
    setImporting(true);
    try {
      const base64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      const res = await importSkill(base64);
      if (res.ok) flash(t("Installed") + (res.name ? ` ${res.name}` : ""));
      else flash(t("Import failed") + (res.error ? `: ${res.error}` : ""));
      refresh();
    } catch {
      flash(t("Import failed"));
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const handleDelete = async (name: string) => {
    const res = await deleteSkill(name);
    if (res.ok) flash(t("Deleted") + ` ${name}`);
    else flash(t("Delete failed"));
    refresh();
  };

  // P1-8 转正链: 涌现草稿 → 正式技能
  const handlePromote = async (name: string) => {
    const res = await promoteSkill(name);
    if (res.ok) flash(t("Promoted") + ` ${name}`);
    else flash(t("Promote failed") + (res.error ? `: ${res.error}` : ""));
    refresh();
  };

  // P1-8: 把最近的 HORNET 涌现转成 Draft Skill
  const handleEmergenceToSkill = async () => {
    setGenerating(true);
    try {
      const r = await hornetEmergenceToSkill(-1);
      if (r.ok && r.skill) {
        flash(t("Generated draft skill") + `: ${r.skill}`);
        refresh();
      } else {
        flash(t("Generation failed") + (r.error ? `: ${r.error}` : ""));
      }
    } catch (e) {
      flash(t("Generation failed") + `: ${String(e)}`);
    } finally {
      setGenerating(false);
    }
  };

  // P1-6/P1-8: 把 skills 拆成 draft (涌现待审核) 与正式两组
  const { drafts, published } = useMemo(() => {
    const d: SkillInfo[] = [];
    const p: SkillInfo[] = [];
    for (const s of skills) {
      if (s.draft) d.push(s);
      else p.push(s);
    }
    return { drafts: d, published: p };
  }, [skills]);

  return (
    <div className="h-full flex flex-col px-5 py-4 overflow-y-auto">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-[15px] font-semibold">{t("Skill marketplace")}</h1>
          <p className="text-[12px] text-muted mt-0.5">
            {t("Create, import, export and install reusable skills")}
            {skills.length > 0 && (
              <>
                {" · "}
                <span className="tabular-nums">
                  {t("{count} skills", { count: skills.length })}
                </span>
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            ref={fileRef}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleImportFile(f);
            }}
          />
          <button
            className="btn-secondary"
            disabled={generating}
            onClick={handleEmergenceToSkill}
            title={t("Convert the latest HORNET emergence into a draft skill")}
          >
            {generating ? t("Generating…") : `🧬 ${t("From emergence")}`}
          </button>
          <button
            className="btn-secondary"
            disabled={importing}
            onClick={() => fileRef.current?.click()}
          >
            {t("Import skill")}
          </button>
          <button
            className="btn-secondary"
            disabled={scanningAll}
            onClick={runBatchCompat}
            title={t("Run compatibility check for every installed skill")}
          >
            {scanningAll ? t("Scanning…") : `🛡 ${t("Scan compatibility")}`}
          </button>
          <button
            className="btn-secondary"
            onClick={() => runAutofix(undefined)}
            title={t("Build a Swarm reviewer autofix plan for all incompatible skills")}
          >
            🔧 {t("Autofix plan")}
          </button>
          <button className="btn-primary" onClick={() => flash(t("Tip: ask the swarm to create_skill during a chat, then export it here"))}>
            {t("How to create skills")}
          </button>
        </div>
      </div>

      {notice && <div className="mb-3 px-3 py-2 rounded-lg bg-surface border border-line text-[12.5px]">{notice}</div>}

      {/* 技能目录路径配置 */}
      {skillsPath && (
        <div className="mb-3 px-3 py-2 rounded-lg bg-surface border border-line">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0 flex-1">
              <span className="text-[11px] text-faint">{t("Skills directory")}</span>
              <div className="text-[12px] truncate font-mono mt-0.5" title={skillsPath}>{skillsPath}</div>
            </div>
            <button
              className="btn-secondary text-[12px] shrink-0"
              disabled={skillsPathBusy}
              onClick={async () => {
                const p = await pickFolderViaServer();
                if (!p) return;
                if (!window.confirm(t("Save this path? The app will restart to move skills."))) return;
                setSkillsPathBusy(true);
                try {
                  const r = await apiSetSkillsPath(p, true);
                  if (r.ok) {
                    setNotice(t("Path saved. The app will restart to move skills…"));
                    setSkillsPath(r.path ?? skillsPath);
                    setTimeout(() => window.location.reload(), 2000);
                  } else {
                    setNotice(r.error || t("Could not save the path."));
                  }
                } catch (e) {
                  const msg = String(e);
                  if (msg.includes("Failed to fetch")) {
                    setNotice(t("Could not connect to the server. Please restart the app and try again."));
                  } else {
                    setNotice(t("Could not save the path.") + " " + msg);
                  }
                } finally {
                  setSkillsPathBusy(false);
                }
              }}
            >
              {skillsPathBusy ? t("Saving…") : t("Move to another folder")}
            </button>
          </div>
        </div>
      )}

      {/* 批量兼容扫描摘要 */}
      {allReports.length > 0 && (
        <div className="mb-3 rounded-xl border border-line bg-panel px-3 py-2.5" data-testid="compat-summary">
          <div className="flex items-center justify-between mb-1.5">
            <div className="text-[12.5px] font-semibold">
              {t("Compatibility summary")}: {allReports.length}
            </div>
            <button className="text-[11px] text-faint hover:text-ink" onClick={() => setAllReports([])}>
              {t("Hide")}
            </button>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {allReports
              .filter((r) => !r.compatible)
              .map((r) => (
                <span
                  key={r.skill_name}
                  className={
                    "px-2 py-0.5 rounded text-[11px] border " +
                    (r.severity === "high"
                      ? "bg-rose-500/15 text-rose-100 border-rose-500/40"
                      : "bg-amber-500/15 text-amber-100 border-amber-500/40")
                  }
                >
                  {r.skill_name} · {r.severity}
                </span>
              ))}
            {allReports.every((r) => r.compatible) && (
              <span className="text-[11.5px] text-emerald-200">✓ {t("All compatible")}</span>
            )}
          </div>
        </div>
      )}

      {/* Swarm reviewer autofix plan */}
      {autofixPanel && (
        <div className="mb-3 rounded-xl border border-violet-600/40 bg-violet-600/10 px-3.5 py-3 text-[12.5px]" data-testid="autofix-plan">
          <div className="flex items-start justify-between mb-2">
            <div className="font-semibold text-violet-100">
              🧐 {autofixPanel.title ?? t("Reviewer autofix plan")}
            </div>
            <button className="text-[11px] text-faint hover:text-ink" onClick={() => setAutofixPanel(null)}>
              {t("Hide")}
            </button>
          </div>
          {autofixPanel.intent && (
            <p className="text-[12px] text-faint whitespace-pre-wrap mb-2 max-h-28 overflow-y-auto">
              {autofixPanel.intent}
            </p>
          )}
          {autofixPanel.rubric && autofixPanel.rubric.length > 0 && (
            <div className="mb-2">
              <div className="text-[11px] font-semibold text-faint mb-1">{t("Reviewer rubric")}</div>
              <ul className="space-y-0.5 ml-3">
                {autofixPanel.rubric.map((r, i) => (
                  <li key={i} className="text-[11.5px]">
                    <span className="text-violet-200">• {r[0]}</span>
                    <span className="text-faint"> — {r[1]}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {autofixPanel.steps && autofixPanel.steps.length > 0 && (
            <div>
              <div className="text-[11px] font-semibold text-faint mb-1">{t("Step plan")}</div>
              <ol className="space-y-0.5 ml-4 list-decimal">
                {autofixPanel.steps.map((s, i) => (
                  <li key={i} className="text-[11.5px]">
                    <span className="font-mono">{s.skill_name}</span>:{" "}
                    <span>{s.action}</span>
                    {s.note && <span className="text-faint"> · {s.note}</span>}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      )}

      {loading ? (
        <div className="text-[13px] text-muted">{t("Loading…")}</div>
      ) : skillsError ? (
        <div
          className="rounded-lg border border-warnInk/30 bg-warnSoft/60 px-3 py-2 text-[12.5px] text-warnInk flex items-center gap-2"
          role="alert"
          data-testid="skills-load-error"
        >
          <span>⚠</span>
          <span className="flex-1">
            {t("Couldn't reach the local engine — this may be connection trouble, not missing skills.")}
          </span>
          <button className="btn-secondary text-[11.5px]" onClick={() => refresh()}>
            {t("Retry")}
          </button>
        </div>
      ) : skills.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-muted">
          <div className="text-[13px]">{t("No skills yet")}</div>
          <div className="text-[12px] mt-1 max-w-[380px] text-center">
            {t("Ask a swarm worker to create_skill when it hits repetitive work, then come back here to export and share.")}
          </div>
        </div>
      ) : (
        <div className="space-y-5">
          {/* P1-8: 涌现待审核草稿区 */}
          {drafts.length > 0 && (
            <section>
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[13px]">🧬</span>
                <h2 className="text-[13.5px] font-semibold">{t("Draft skills from emergence")}</h2>
                <span className="text-[11.5px] text-faint">
                  {t("Auto-generated from HORNET emergent findings — review before promoting")}
                </span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {drafts.map((s) => (
                  <SkillCard
                    key={s.name}
                    skill={s}
                    onRate={handleRate}
                    onExport={handleExport}
                    onDelete={handleDelete}
                    onPromote={handlePromote}
                    flash={flash}
                  />
                ))}
              </div>
            </section>
          )}

          {/* 正式技能 */}
          <section>
            {drafts.length > 0 && (
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[13px]">✅</span>
                <h2 className="text-[13.5px] font-semibold">{t("Published skills")}</h2>
              </div>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {published.map((s) => (
                <SkillCard
                  key={s.name}
                  skill={s}
                  onRate={handleRate}
                  onExport={handleExport}
                  onDelete={handleDelete}
                  onPromote={handlePromote}
                  flash={flash}
                />
              ))}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

// -- SkillCard: 单个技能卡片, 集成 P1-6 安全评分/兼容性/版本化 ----------------

const SECURITY_LEVEL_STYLE: Record<string, string> = {
  low: "bg-emerald-600/15 text-emerald-200 border-emerald-600/30",
  medium: "bg-amber-500/20 text-amber-100 border-amber-500/40",
  high: "bg-rose-500/20 text-rose-100 border-rose-500/40",
  critical: "bg-rose-700/30 text-rose-100 border-rose-700/50",
};

interface SkillCardProps {
  skill: SkillInfo;
  onRate: (name: string, score: number) => void;
  onExport: (name: string) => void;
  onDelete: (name: string) => void;
  onPromote: (name: string) => void;
  flash: (msg: string) => void;
}

const COMPAT_SEVERITY_STYLE: Record<string, string> = {
  none: "bg-emerald-600/15 text-emerald-200 border-emerald-600/30",
  low: "bg-sky-600/15 text-sky-200 border-sky-600/30",
  medium: "bg-amber-500/20 text-amber-100 border-amber-500/40",
  high: "bg-rose-500/20 text-rose-100 border-rose-500/40",
};

function SkillCard({ skill, onRate, onExport, onDelete, onPromote, flash }: SkillCardProps) {
  const t = useT();
  const [sec, setSec] = useState<SkillSecurityReport | null>(null);
  const [compat, setCompat] = useState<SkillCompatibilityReport | null>(null);
  const [lockInfo, setLockInfo] = useState<SkillLockInfo | null>(null);
  const [versions, setVersions] = useState<SkillVersions | null>(null);
  const [showDetail, setShowDetail] = useState(false);
  const [busy, setBusy] = useState<"lock" | "sec" | "compat" | "versions" | "promote" | null>(null);

  const handlePromote = async () => {
    setBusy("promote");
    try {
      onPromote(skill.name);
    } finally {
      setBusy(null);
    }
  };

  const handleLock = async () => {
    setBusy("lock");
    try {
      const r = await generateSkillLock(skill.name);
      if (r.ok) {
        flash(t("Lock generated"));
        const li = await getSkillLock(skill.name);
        setLockInfo(li);
      } else {
        flash(t("Lock failed") + (r.error ? `: ${r.error}` : ""));
      }
    } finally {
      setBusy(null);
    }
  };

  const handleSec = async (forceRescan = false) => {
    setBusy("sec");
    try {
      const r = forceRescan ? await rescanSkillSecurity(skill.name) : await getSkillSecurity(skill.name);
      setSec(r);
      setShowDetail(true);
    } finally {
      setBusy(null);
    }
  };

  const handleCompat = async () => {
    setBusy("compat");
    try {
      const r = await checkSkillCompatibility(skill.name);
      setCompat(r);
      setShowDetail(true);
    } finally {
      setBusy(null);
    }
  };

  const handleVersions = async () => {
    setBusy("versions");
    try {
      const v = await getSkillVersions(skill.name);
      setVersions(v);
      setShowDetail(true);
    } finally {
      setBusy(null);
    }
  };

  const isEmergence = skill.source === "hornet_emergence";
  const hasSecurity = typeof skill.security_score === "number";
  const hasCompatBadge = skill.compatible === true || (skill.compat_severity && skill.compat_severity !== "none");

  return (
    <div className="rounded-xl border border-line bg-surface p-3.5 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-semibold text-[13.5px] truncate">{skill.name}</span>
            {skill.draft && (
              <span className="text-[10px] px-1.5 py-px rounded-full bg-amber-600/30 text-amber-100 border border-amber-500/40">
                {t("Draft")}
              </span>
            )}
            {isEmergence && (
              <span
                className="text-[10px] px-1.5 py-px rounded-full bg-violet-600/30 text-violet-100 border border-violet-500/40"
                title={t("Auto-generated from HORNET emergence")}
              >
                🧬
              </span>
            )}
            {/* 信任徽章: lock */}
            {skill.lock_exists ? (
              <span
                className="text-[10px] px-1.5 py-px rounded-full bg-emerald-600/20 text-emerald-200 border border-emerald-600/30"
                title={t("Skill.lock present")}
              >
                🔒 lock
              </span>
            ) : (
              <span
                className="text-[10px] px-1.5 py-px rounded-full bg-slate-500/20 text-slate-200 border border-slate-500/30 opacity-70"
                title={t("No skill.lock — run Lock first")}
              >
                ⊘ lock
              </span>
            )}
            {/* 信任徽章: 安全评分 */}
            {hasSecurity && (
              <span
                className={
                  "text-[10px] px-1.5 py-px rounded-full border font-mono " +
                  (SECURITY_LEVEL_STYLE[skill.security_level ?? "low"] ?? "")
                }
                title={`${t("Security score")} ${skill.security_score}/100 (${skill.security_level})`}
              >
                🛡 {skill.security_score}
              </span>
            )}
            {/* 信任徽章: 兼容性 severity */}
            {hasCompatBadge && (
              <span
                className={
                  "text-[10px] px-1.5 py-px rounded-full border " +
                  (skill.compatible
                    ? COMPAT_SEVERITY_STYLE.none
                    : COMPAT_SEVERITY_STYLE[skill.compat_severity ?? "medium"])
                }
                title={skill.compatible ? t("Compatible") : t(`Severity: ${skill.compat_severity}`)}
              >
                {skill.compatible ? "✓ compat" : `⚠ ${skill.compat_severity}`}
              </span>
            )}
          </div>
          <div className="text-[12px] text-faint">
            v{skill.version}
            {skill.available_versions && skill.available_versions.length > 1 && (
              <> · {t("{count} versions", { count: skill.available_versions.length })}</>
            )}
            {skill.category ? ` · ${skill.category}` : ""}
            {skill.author ? ` · ${skill.author}` : ""}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className="text-[12px] tabular-nums">
            {t("Installs")} {skill.install_count}
          </div>
          <div className="text-[12px] tabular-nums text-faint">
            {skill.rating != null ? `★ ${skill.rating} (${skill.rating_count})` : "—"}
          </div>
        </div>
      </div>

      <p className="text-[12.5px] text-muted leading-snug line-clamp-2">{skill.description}</p>

      {skill.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {skill.tags.map((tag) => (
            <span key={tag} className="px-1.5 py-0.5 rounded bg-surface-2 text-[11px] text-faint">
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* 点击按钮后的详细诊断 (安全评分 / 兼容 / lock / 版本市场) */}
      {sec && sec.ok && (
        <div
          className={
            "rounded-md border px-2 py-1 text-[11px] flex items-center gap-2 " +
            (SECURITY_LEVEL_STYLE[sec.level] ?? "")
          }
          data-testid={`skill-security-${skill.name}`}
        >
          <span className="font-mono">
            {t("Security")}: {sec.score}/100 · {sec.level}
          </span>
          {sec.findings.length > 0 && <span className="opacity-80">· {sec.findings.length} {t("findings")}</span>}
          {sec.scanned_files != null && <span className="opacity-70">· {sec.scanned_files} {t("files")}</span>}
        </div>
      )}

      {compat && compat.ok && (
        <div
          className={
            "rounded-md border px-2 py-1 text-[11px] flex items-center gap-2 " +
            (compat.compatible ? COMPAT_SEVERITY_STYLE.none : COMPAT_SEVERITY_STYLE[compat.severity] ?? "")
          }
          data-testid={`skill-compat-${skill.name}`}
        >
          {compat.compatible
            ? `✓ ${t("Compatible")}`
            : `⚠ ${t("Severity")}: ${compat.severity} · ${
                (compat.missing_tools ?? []).length + (compat.changed_tools ?? []).length
              } ${t("issues")}`}
          {!compat.has_lock && <span className="opacity-70">· {t("no lock")}</span>}
        </div>
      )}

      {lockInfo && lockInfo.ok && (
        <div
          className={
            "rounded-md border px-2 py-1 text-[11px] flex items-center gap-2 " +
            (lockInfo.lock_exists
              ? lockInfo.integrity_ok
                ? "bg-emerald-600/15 text-emerald-200 border-emerald-600/30"
                : "bg-rose-500/20 text-rose-100 border-rose-500/40"
              : "bg-slate-500/15 text-slate-200 border-slate-500/30")
          }
        >
          {lockInfo.lock_exists
            ? lockInfo.integrity_ok
              ? `🔒 ${t("Lock integrity OK")} · ${t("{count} tools", { count: lockInfo.lock?.tools?.length ?? 0 })}`
              : `✗ ${t("Lock integrity mismatch")}`
            : `⊘ ${t("No lock file")}`}
        </div>
      )}

      {/* 详细诊断折叠区 */}
      {showDetail && (sec || compat || versions) && (
        <div
          className="rounded-md border border-line bg-panel p-2 text-[11.5px] space-y-2"
          data-testid={`skill-detail-${skill.name}`}
        >
          {sec && sec.ok && sec.findings.length > 0 && (
            <div>
              <div className="font-semibold text-[11px] mb-1">
                {t("Security findings")} ({sec.score}/100 · {sec.level})
              </div>
              <ul className="space-y-0.5 ml-3">
                {sec.findings.slice(0, 8).map((f, i) => (
                  <li key={i} className="text-faint">
                    <span className="text-ink">{f.label}</span>
                    {f.file && <span className="opacity-70"> ({f.file})</span>}
                    <span className="opacity-70"> ×{f.count} · w{f.weight}</span>
                  </li>
                ))}
              </ul>
              {sec.breakdown && Object.keys(sec.breakdown).length > 0 && (
                <div className="mt-1 text-[10.5px] text-faint">
                  {Object.entries(sec.breakdown).map(([k, v]) => (
                    <span key={k} className="mr-2">
                      {k}: {v}
                    </span>
                  ))}
                </div>
              )}
              <p className="text-[10.5px] text-faint mt-1">{sec.recommendation}</p>
            </div>
          )}

          {compat && compat.ok && !compat.compatible && (
            <div>
              <div className="font-semibold text-[11px] mb-1">{t("Compatibility issues")}</div>
              {(compat.missing_tools ?? []).length > 0 && (
                <div className="text-faint">
                  <span className="text-rose-200">{t("Missing")}:</span>{" "}
                  {(compat.missing_tools ?? [])
                    .map((x) => (typeof x === "string" ? x : x.name))
                    .join(", ")}
                </div>
              )}
              {(compat.changed_tools ?? []).length > 0 && (
                <div className="text-faint">
                  <span className="text-amber-200">{t("Changed")}:</span>{" "}
                  {(compat.changed_tools ?? []).map((x) => x.name).join(", ")}
                </div>
              )}
              {(compat.new_tools ?? []).length > 0 && (
                <div className="text-faint">
                  <span className="text-sky-200">{t("New available")}:</span>{" "}
                  {(compat.new_tools ?? []).map((x) => x.name).join(", ")}
                </div>
              )}
              {compat.summary && <p className="text-[10.5px] text-ink mt-1">{compat.summary}</p>}
              {compat.recommendation && <p className="text-[10.5px] text-faint mt-1">{compat.recommendation}</p>}
            </div>
          )}

          {versions && versions.ok && (
            <div>
              <div className="font-semibold text-[11px] mb-1">
                {t("Version marketplace stats")} · {t("total installs")}: {versions.aggregate.total_install_count}
                {versions.aggregate.weighted_rating != null && (
                  <> · ★ {versions.aggregate.weighted_rating}</>
                )}
              </div>
              <ul className="space-y-0.5 ml-3">
                {versions.versions.map((v) => (
                  <li key={v.version} className="text-[11px] text-faint">
                    <span className="font-mono text-ink">v{v.version}</span> ·{" "}
                    {t("Installs")} {v.install_count}
                    {v.rating != null ? ` · ★ ${v.rating} (${v.rating_count})` : ""}
                  </li>
                ))}
                {versions.versions.length === 0 && (
                  <li className="text-[11px] text-faint">{t("Only this version recorded")}</li>
                )}
              </ul>
            </div>
          )}

          <button className="text-[10.5px] text-faint hover:text-ink" onClick={() => setShowDetail(false)}>
            {t("Hide")}
          </button>
        </div>
      )}

      <div className="flex items-center gap-1.5 mt-auto flex-wrap">
        {skill.draft && (
          <button
            className="btn-primary text-[11.5px] py-1 px-2.5"
            disabled={busy !== null}
            onClick={handlePromote}
            title={t("Promote this draft into a published skill")}
          >
            {busy === "promote" ? "…" : `✓ ${t("Promote")}`}
          </button>
        )}
        {[1, 2, 3, 4, 5].map((star) => (
          <button
            key={star}
            className="text-[13px] text-muted hover:text-amber-500"
            title={`${t("Rate")} ${star}`}
            onClick={() => onRate(skill.name, star)}
          >
            {skill.rating != null && star <= Math.round(skill.rating) ? "★" : "☆"}
          </button>
        ))}
        <span className="flex-1" />
        <button
          className="btn-secondary text-[11px] py-1 px-2"
          disabled={busy !== null}
          onClick={handleVersions}
          title={t("See install/rating stats across versions")}
        >
          {busy === "versions" ? "…" : t("Versions")}
        </button>
        <button
          className="btn-secondary text-[11px] py-1 px-2"
          disabled={busy !== null}
          onClick={handleLock}
          title={t("Generate skill.lock with tool schema hashes")}
        >
          {busy === "lock" ? "…" : t("Lock")}
        </button>
        <button
          className="btn-secondary text-[11px] py-1 px-2"
          disabled={busy !== null}
          onClick={() => handleSec(false)}
          onContextMenu={(e) => {
            e.preventDefault();
            void handleSec(true);
          }}
          title={t("Static security analysis (right-click to rescan)")}
        >
          {busy === "sec" ? "…" : t("Security")}
        </button>
        <button
          className="btn-secondary text-[11px] py-1 px-2"
          disabled={busy !== null}
          onClick={handleCompat}
          title={t("Check tool compatibility against current registry")}
        >
          {busy === "compat" ? "…" : t("Compat")}
        </button>
        <button className="btn-secondary text-[11.5px] py-1 px-2" onClick={() => onExport(skill.name)}>
          {t("Export")}
        </button>
        <button
          className="btn-danger text-[11.5px] py-1 px-2"
          onClick={() => {
            if (window.confirm(`${t("Delete skill")} ${skill.name}?`)) onDelete(skill.name);
          }}
        >
          {t("Delete")}
        </button>
      </div>
    </div>
  );
}
