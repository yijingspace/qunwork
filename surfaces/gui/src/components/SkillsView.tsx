import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  checkSkillCompatibility,
  deleteSkill,
  exportSkill,
  generateSkillLock,
  getSkillSecurity,
  hornetEmergenceToSkill,
  importSkill,
  listSkills,
  rateSkill,
  type SkillCompatibilityReport,
  type SkillInfo,
  type SkillSecurityReport,
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
  // P1-8: 正在调用 emergence → skill
  const [generating, setGenerating] = useState(false);

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

  useEffect(() => {
    refresh();
  }, [refresh]);

  const flash = (msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(""), 4000);
  };

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
        <div className="flex items-center gap-2">
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
          <button className="btn-primary" onClick={() => flash(t("Tip: ask the swarm to create_skill during a chat, then export it here"))}>
            {t("How to create skills")}
          </button>
        </div>
      </div>

      {notice && <div className="mb-3 px-3 py-2 rounded-lg bg-surface border border-line text-[12.5px]">{notice}</div>}

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
  flash: (msg: string) => void;
}

function SkillCard({ skill, onRate, onExport, onDelete, flash }: SkillCardProps) {
  const t = useT();
  const [sec, setSec] = useState<SkillSecurityReport | null>(null);
  const [compat, setCompat] = useState<SkillCompatibilityReport | null>(null);
  const [showDetail, setShowDetail] = useState(false);
  const [busy, setBusy] = useState<"lock" | "sec" | "compat" | null>(null);

  const handleLock = async () => {
    setBusy("lock");
    try {
      const r = await generateSkillLock(skill.name);
      if (r.ok) {
        flash(t("Lock generated") + (r.lock_path ?? ""));
      } else {
        flash(t("Lock failed") + (r.error ? `: ${r.error}` : ""));
      }
    } finally {
      setBusy(null);
    }
  };

  const handleSec = async () => {
    setBusy("sec");
    try {
      const r = await getSkillSecurity(skill.name);
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

  const isEmergence = skill.source === "hornet_emergence";

  return (
    <div className="rounded-xl border border-line bg-surface p-3.5 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="font-semibold text-[13.5px] truncate">{skill.name}</span>
            {skill.draft && (
              <span className="text-[10px] px-1.5 py-px rounded-full bg-amber-600/30 text-amber-100 border border-amber-500/40">
                {t("Draft")}
              </span>
            )}
            {isEmergence && (
              <span className="text-[10px] px-1.5 py-px rounded-full bg-violet-600/30 text-violet-100 border border-violet-500/40" title={t("Auto-generated from HORNET emergence")}>
                🧬
              </span>
            )}
          </div>
          <div className="text-[12px] text-faint">
            v{skill.version}
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

      {/* P1-6: 安全评分徽章 (有数据时显示) */}
      {sec && sec.ok && (
        <div
          className={
            "rounded-md border px-2 py-1 text-[11px] flex items-center gap-2 " +
            (SECURITY_LEVEL_STYLE[sec.level] ?? "")
          }
          data-testid={`skill-security-${skill.name}`}
        >
          <span className="font-mono">{t("Security")}: {sec.score}/100 · {sec.level}</span>
          {sec.findings.length > 0 && (
            <span className="opacity-80">· {sec.findings.length} {t("findings")}</span>
          )}
        </div>
      )}

      {/* P1-6: 兼容性徽章 (有数据时显示) */}
      {compat && compat.ok && (
        <div
          className={
            "rounded-md border px-2 py-1 text-[11px] flex items-center gap-2 " +
            (compat.compatible
              ? "bg-emerald-600/15 text-emerald-200 border-emerald-600/30"
              : "bg-rose-500/20 text-rose-100 border-rose-500/40")
          }
          data-testid={`skill-compat-${skill.name}`}
        >
          {compat.compatible
            ? `✓ ${t("Compatible")}`
            : `✗ ${t("Incompatible")}: ${(compat.missing_tools ?? []).length + (compat.changed_tools ?? []).length} ${t("issues")}`}
          {!compat.has_lock && (
            <span className="opacity-70">· {t("no lock")}</span>
          )}
        </div>
      )}

      {/* P1-6: 详细诊断 (折叠区) */}
      {showDetail && (sec || compat) && (
        <div className="rounded-md border border-line bg-panel p-2 text-[11.5px] space-y-1.5" data-testid={`skill-detail-${skill.name}`}>
          {sec && sec.ok && sec.findings.length > 0 && (
            <div>
              <div className="font-semibold text-[11px] mb-1">{t("Security findings")}</div>
              <ul className="space-y-0.5 ml-3">
                {sec.findings.slice(0, 5).map((f, i) => (
                  <li key={i} className="text-faint">
                    <span className="text-ink">{f.label}</span> ×{f.count} (w{f.weight})
                  </li>
                ))}
              </ul>
              <p className="text-[10.5px] text-faint mt-1">{sec.recommendation}</p>
            </div>
          )}
          {compat && compat.ok && !compat.compatible && (
            <div>
              <div className="font-semibold text-[11px] mb-1">{t("Compatibility issues")}</div>
              {(compat.missing_tools ?? []).length > 0 && (
                <div className="text-faint">
                  {t("Missing")}: <span className="font-mono">{(compat.missing_tools ?? []).join(", ")}</span>
                </div>
              )}
              {(compat.changed_tools ?? []).length > 0 && (
                <div className="text-faint">
                  {t("Changed")}: <span className="font-mono">{(compat.changed_tools ?? []).join(", ")}</span>
                </div>
              )}
              {compat.recommendation && (
                <p className="text-[10.5px] text-faint mt-1">{compat.recommendation}</p>
              )}
            </div>
          )}
          <button
            className="text-[10.5px] text-faint hover:text-ink"
            onClick={() => setShowDetail(false)}
          >
            {t("Hide")}
          </button>
        </div>
      )}

      <div className="flex items-center gap-1.5 mt-auto flex-wrap">
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
        {/* P1-6: 版本化/安全/兼容性 按钮 */}
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
          onClick={handleSec}
          title={t("Static security analysis")}
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
