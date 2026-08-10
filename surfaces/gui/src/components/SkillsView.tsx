import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteSkill,
  exportSkill,
  importSkill,
  listSkills,
  rateSkill,
  type SkillInfo,
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
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {skills.map((s) => (
            <div key={s.name} className="rounded-xl border border-line bg-surface p-3.5 flex flex-col gap-2">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="font-semibold text-[13.5px] truncate">{s.name}</div>
                  <div className="text-[12px] text-faint">
                    v{s.version}
                    {s.category ? ` · ${s.category}` : ""}
                    {s.author ? ` · ${s.author}` : ""}
                  </div>
                </div>
                <div className="text-right shrink-0">
                  <div className="text-[12px] tabular-nums">
                    {t("Installs")} {s.install_count}
                  </div>
                  <div className="text-[12px] tabular-nums text-faint">
                    {s.rating != null ? `★ ${s.rating} (${s.rating_count})` : "—"}
                  </div>
                </div>
              </div>

              <p className="text-[12.5px] text-muted leading-snug line-clamp-2">{s.description}</p>

              {s.tags.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {s.tags.map((tag) => (
                    <span key={tag} className="px-1.5 py-0.5 rounded bg-surface-2 text-[11px] text-faint">
                      {tag}
                    </span>
                  ))}
                </div>
              )}

              <div className="flex items-center gap-1.5 mt-auto">
                {[1, 2, 3, 4, 5].map((star) => (
                  <button
                    key={star}
                    className="text-[13px] text-muted hover:text-amber-500"
                    title={`${t("Rate")} ${star}`}
                    onClick={() => handleRate(s.name, star)}
                  >
                    {s.rating != null && star <= Math.round(s.rating) ? "★" : "☆"}
                  </button>
                ))}
                <span className="flex-1" />
                <button className="btn-secondary text-[11.5px] py-1 px-2" onClick={() => handleExport(s.name)}>
                  {t("Export")}
                </button>
                <button
                  className="btn-danger text-[11.5px] py-1 px-2"
                  onClick={() => {
                    if (window.confirm(`${t("Delete skill")} ${s.name}?`)) handleDelete(s.name);
                  }}
                >
                  {t("Delete")}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
