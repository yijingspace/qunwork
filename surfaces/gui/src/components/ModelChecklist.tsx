import { useEffect, useMemo, useState } from "react";
import { addModel, getSettings, listProviderModels, removeModel, setDefaultModel } from "../api";
import { useT } from "../i18n";

// One provider's models as a checklist: tick = shown in the composer's model picker (the
// curated list), the black "default" badge marks the model new sessions use, and hovering any
// other row reveals "Make default". A free-type row below adds models by hand, so brand-new
// releases work without an app update. Shared by Onboarding and Manage → Configure Models.
//
// Live section (owner-hit 2026-09-13): the curated matrix only carries a handful of vetted
// models, so a user who connected six providers still saw three or four models in the picker.
// On mount we now fetch the provider's REAL model list (GET /models with the stored key) and
// offer every id it serves, with a filter box for the long lists (OpenAI-style vendors return
// hundreds). Gateways without a listing endpoint degrade to the manual add row, as before.
export function ModelChecklist({
  provider,
  knownProviders,
  suggested,
  curated,
  defaultModel,
  labels,
  onChanged,
}: {
  provider: string; // decides the id prefix; OpenAI models stay bare
  knownProviders: string[]; // all provider names, to parse prefixes in curated ids
  suggested: string[]; // bare model names suggested by the provider
  curated: string[]; // the full curated list (all providers, full ids)
  defaultModel: string;
  labels?: Record<string, string>; // curated display names (full id → label); raw id when absent
  onChanged: (next: { models: string[]; model: string }) => void;
}) {
  const t = useT();
  const [draft, setDraft] = useState("");
  // -- live model list (fetched on mount; null = 还没拉到/拉取失败) -------------------------
  const [live, setLive] = useState<string[] | null>(null);
  const [liveBusy, setLiveBusy] = useState(false);
  const [liveErr, setLiveErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  const fetchLive = async () => {
    setLiveBusy(true);
    setLiveErr(null);
    try {
      const r = await listProviderModels(provider);
      if (r.ok) setLive(r.models || []);
      else setLiveErr(r.error || t("Could not fetch the model list."));
    } catch {
      setLiveErr(t("Could not fetch the model list."));
    } finally {
      setLiveBusy(false);
    }
  };
  useEffect(() => {
    setLive(null);
    setLiveErr(null);
    setFilter("");
    void fetchLive();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  const provOf = (id: string) => {
    const i = id.indexOf(":");
    return i > 0 && knownProviders.includes(id.slice(0, i)) ? id.slice(0, i) : "openai";
  };
  const prefixed = (m: string) => (provider === "openai" || provOf(m) !== "openai" ? m : `${provider}:${m}`);
  const bare = (id: string) => (id.startsWith(`${provider}:`) ? id.slice(provider.length + 1) : id);

  const rows = useMemo(
    () =>
      [...suggested.map(prefixed), ...curated.filter((id) => provOf(id) === provider)].filter(
        (id, i, a) => a.indexOf(id) === i,
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [suggested, curated, provider, knownProviders],
  );
  // Live ids the vetted rows above don't already cover (those would be duplicates).
  const liveRows = useMemo(() => {
    if (!live) return [];
    const seen = new Set(rows);
    return (live || []).map(prefixed).filter((id) => !seen.has(id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, rows, provider]);
  const shownLive = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? liveRows.filter((id) => id.toLowerCase().includes(q)) : liveRows;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveRows, filter]);

  const checked = (id: string) => curated.includes(id);
  const refresh = async () => {
    const s = await getSettings();
    onChanged({ models: s.models, model: s.model });
  };

  const tick = async (id: string, on: boolean) => {
    const res = on ? await addModel(id) : await removeModel(id);
    if (res.ok) onChanged({ models: res.models, model: res.model });
  };
  const makeDefault = async (id: string) => {
    if (!checked(id)) await addModel(id); // defaulting an unticked row ticks it too
    await setDefaultModel(id);
    await refresh();
  };
  const add = async () => {
    const typed = draft.trim();
    if (!typed) return;
    const res = await addModel(prefixed(typed));
    if (res.ok) {
      setDraft("");
      onChanged({ models: res.models, model: res.model });
    }
  };

  const row = (id: string) => {
    const isDefault = id === defaultModel;
    return (
      <div className={"mlist-row" + (checked(id) ? "" : " off")} key={id}>
        <label className="mlist-main">
          <input
            type="checkbox"
            checked={checked(id)}
            disabled={isDefault}
            title={
              isDefault
                ? t("The default model is always shown — make another model default first")
                : undefined
            }
            onChange={(e) => tick(id, e.target.checked)}
          />
          <span className="mlist-name" title={id}>
            {labels?.[id] || bare(id)}
          </span>
        </label>
        {isDefault ? (
          <span className="mlist-default">{t("Default")}</span>
        ) : (
          <button className="mlist-make" onClick={() => makeDefault(id)}>
            {t("Make default")}
          </button>
        )}
      </div>
    );
  };

  return (
    <div>
      <div className="mlist">{rows.map(row)}</div>

      {/* -- 实时模型列表: 该服务商真正提供的模型 ---------------------------------- */}
      <div className="mt-3">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[11px] uppercase tracking-[0.05em] text-faint font-semibold">
            {t("From the provider")}
          </span>
          {liveBusy && <span className="text-[11.5px] text-faint">{t("Fetching…")}</span>}
          {live && !liveBusy && (
            <span className="text-[11.5px] text-faint" data-testid="model-live-count">
              {t("{n} models, fetched live", { n: live.length })}
            </span>
          )}
          <button
            className="text-[11.5px] text-muted hover:text-ink underline underline-offset-2"
            onClick={() => void fetchLive()}
            disabled={liveBusy}
            data-testid="model-live-refresh"
          >
            {t("Refresh")}
          </button>
        </div>
        {liveErr && (
          <p className="text-[11.5px] text-faint mt-1" data-testid="model-live-error">
            {liveErr}
          </p>
        )}
        {liveRows.length > 15 && (
          <input
            className="w-full mt-2 mb-1 px-2.5 py-1.5 rounded-lg border border-line bg-panel text-[12.5px] outline-none focus:border-accent"
            placeholder={t("Filter models…")}
            value={filter}
            spellCheck={false}
            onChange={(e) => setFilter(e.target.value)}
            data-testid="model-live-filter"
          />
        )}
        <div className="mlist max-h-64 overflow-y-auto">{shownLive.map(row)}</div>
        {live && !liveBusy && liveRows.length === 0 && !liveErr && (
          <p className="text-[11.5px] text-faint mt-1">
            {t("Everything this provider serves is already listed above.")}
          </p>
        )}
      </div>

      <div className="mlist-add">
        <input
          placeholder={t("Add another model…")}
          value={draft}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
        <button className="btn-primary sm" onClick={add} disabled={!draft.trim()}>
          {t("Add")}
        </button>
      </div>
    </div>
  );
}
