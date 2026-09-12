import { useCallback, useEffect, useRef, useState } from "react";
import {
  getUsage,
  getCacheWarmStatus,
  setCacheWarmEnabled,
  triggerCacheWarm,
  getRoiConfig,
  setRoiConfig,
  getRoiReport,
  generateRoiReport,
  type CacheWarmStatus,
  type SteadyStats,
  type RoiReport,
} from "../api";
import { useT } from "../i18n";
import { Toggle } from "./Toggle";
import { Icon } from "./Icon";

const FIELD_LABEL = "text-[12.5px] font-medium text-ink";

// Field-level degradation for the cache-warm card: the render path reads `warm.week.calls`,
// `warm.min_hit_rate` and friends unguarded, so a partial/older payload (or a body that isn't
// the expected shape at all) used to throw through the ErrorBoundary and blank the whole
// Settings page. Normalizing here keeps a bad payload from becoming a dead page.
function normalizeWarm(w: Partial<CacheWarmStatus> | null | undefined): CacheWarmStatus {
  return {
    enabled: w?.enabled ?? false,
    min_hit_rate: w?.min_hit_rate ?? 0.5,
    max_items: w?.max_items ?? 20,
    interval_hours: w?.interval_hours ?? 6,
    last_warm_at: w?.last_warm_at ?? 0,
    week: {
      prompt_tokens: w?.week?.prompt_tokens ?? 0,
      cached_tokens: w?.week?.cached_tokens ?? 0,
      calls: w?.week?.calls ?? 0,
    },
    org_hit_rate: w?.org_hit_rate ?? 0,
  };
}

interface UsageAgg {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
  turns: number;
}
interface DayRow {
  day: string;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
}
interface SessionRow {
  session_id: string;
  model: string;
  turns: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
}

const fmt = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);

function Stat({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="flex-1 min-w-[120px] rounded-xl border border-line bg-panel/60 px-4 py-3">
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className={`text-[22px] font-semibold tabular-nums mt-0.5 ${accent ?? ""}`}>{value}</div>
      {sub ? <div className="text-[11px] text-muted mt-0.5">{sub}</div> : null}
    </div>
  );
}

function shortSession(id: string) {
  if (!id) return "—";
  if (id.startsWith("__run__")) return `⏰ ${id.slice(7, 19)}…`;
  return id.length > 24 ? `${id.slice(0, 12)}…${id.slice(-6)}` : id;
}

export function UsageTab() {
  const t = useT();
  const [tab, setTab] = useState<"usage" | "roi">("usage");
  const [totals, setTotals] = useState<UsageAgg | null>(null);
  const [steady, setSteady] = useState<SteadyStats | null>(null);
  const [days, setDays] = useState<DayRow[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [err, setErr] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  // P0 建议1: cache warm-up toggle + this-week stats
  const [warm, setWarm] = useState<CacheWarmStatus | null>(null);
  const [warming, setWarming] = useState(false);
  const [warmNote, setWarmNote] = useState("");
  const pollRef = useRef<number | null>(null);
  // Request sequencing: a slow older reload must not overwrite a newer one
  // (10s poll + manual refresh + visibilitychange can overlap). Stale
  // responses are dropped.
  const reqSeq = useRef(0);

  const reload = useCallback(async () => {
    const seq = ++reqSeq.current;
    setRefreshing(true);
    try {
      const r = await getUsage();
      if (seq !== reqSeq.current) return; // a newer reload started — drop stale data
      setTotals(r.totals ?? null);
      setSteady(r.steady ?? null);
      setDays(r.by_day ?? []);
      setSessions(r.by_session ?? []);
      setErr("");
    } catch {
      if (seq === reqSeq.current) setErr(t("Failed to load usage data."));
    } finally {
      if (seq === reqSeq.current) setRefreshing(false);
    }
  }, [t]);

  useEffect(() => {
    reload();
    getCacheWarmStatus()
      .then((w) => setWarm(normalizeWarm(w)))
      .catch(() => setWarm(null));
    pollRef.current = window.setInterval(reload, 10_000);
    const onVisible = () => { if (document.visibilityState === "visible") reload(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload]);

  const toggleWarm = async (next: boolean) => {
    setWarm((w) => (w ? { ...w, enabled: next } : w));
    try {
      const r = await setCacheWarmEnabled(next);
      setWarm((w) => (w ? { ...w, enabled: r.enabled } : w));
    } catch {
      setWarmNote(t("Failed to update cache warm-up."));
    }
  };

  const warmNow = async () => {
    setWarming(true);
    setWarmNote("");
    try {
      const r = await triggerCacheWarm();
      setWarmNote(
        r.ok
          ? t("Warmed {n} knowledge prefixes ({p} prompt tokens).", {
              n: r.warmed ?? 0,
              p: r.prompt_tokens ?? 0,
            })
          : t("Warm-up skipped: {reason}", { reason: (r as any).reason ?? (r as any).error ?? "?" })
      );
      getCacheWarmStatus()
        .then((w) => setWarm(normalizeWarm(w)))
        .catch(() => {});
    } catch {
      setWarmNote(t("Warm-up failed."));
    } finally {
      setWarming(false);
    }
  };

  const hit = totals?.cache_hit_rate ?? 0;
  const hitColor = hit >= 0.6 ? "text-green-500" : hit >= 0.3 ? "text-amber-500" : "text-red-500";
  const maxDay = days.reduce((m, d) => Math.max(m, d.prompt_tokens + d.completion_tokens), 0) || 1;

  return (
    <section>
      <div className="flex items-center gap-1 mb-3">
        {(["usage", "roi"] as const).map((tb) => (
          <button
            key={tb}
            className={"px-3 py-1.5 rounded-lg text-[12.5px] border " +
              (tab === tb ? "bg-panel border-lineStrong text-ink" : "border-transparent text-faint hover:text-ink")}
            onClick={() => setTab(tb)}
            data-testid={`usage-tab-${tb}`}
          >
            {tb === "usage" ? t("Usage") : t("ROI")}
          </button>
        ))}
      </div>

      {tab === "usage" ? (
        <>
      <div className="flex items-center gap-2 mb-3">
        <h3 className="text-[15px] font-semibold">{t("Token usage")}</h3>
        <span className="text-[11px] text-muted">· {t("Cache hit rate")}: </span>
        <span className={`text-[13px] font-semibold tabular-nums ${hitColor}`}>
          {(hit * 100).toFixed(1)}%
        </span>
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => reload()}
          disabled={refreshing}
          className="shrink-0 text-[12px] px-2.5 py-1.5 rounded-md border border-line bg-paper hover:border-lineStrong text-muted hover:text-ink disabled:opacity-40 inline-flex items-center gap-1.5"
          title={t("Refresh usage")}
        >
          <Icon name="refresh" size={13} className={refreshing ? "animate-spin" : ""} />
          {t("Refresh")}
        </button>
      </div>
      <p className={FIELD_LABEL + " mb-3"}>
        {t(
          "Every model call reports its prompt/completion tokens plus how much of the prompt was served from the provider's automatic context cache. A high hit rate means cheaper, faster repeats of long sessions."
        )}
      </p>

      {err ? <div className="text-[12.5px] text-red-500">{err}</div> : null}

      {totals ? (
        <div className="flex flex-wrap gap-2.5 mb-4">
          <Stat label={t("Total tokens")} value={fmt(totals.total_tokens)} sub={`${t("turns")}: ${totals.turns}`} />
          <Stat label={t("Prompt (in)")} value={fmt(totals.prompt_tokens)} sub={`${t("cached")} ${fmt(totals.cached_tokens)}`} />
          <Stat label={t("Completion (out)")} value={fmt(totals.completion_tokens)} />
          <Stat
            label={t("Cache hit rate")}
            value={`${(hit * 100).toFixed(1)}%`}
            accent={hitColor}
            sub={totals.cached_tokens ? `${fmt(totals.cached_tokens)} ${t("cached")}` : undefined}
          />
          {steady && steady.turns > 0 && (
            <Stat
              label={t("Steady hit rate")}
              value={`${(steady.cache_hit_rate * 100).toFixed(1)}%`}
              accent={steady.cache_hit_rate >= 0.85 ? "text-green-500" : steady.cache_hit_rate >= 0.6 ? "text-amber-500" : "text-red-500"}
              sub={t("Warm rounds only — excludes prefix rebuilds")}
            />
          )}
        </div>
      ) : (
        <div className="text-[12px] text-muted mb-4">{t("Loading…")}</div>
      )}

      {warm && (
        <div
          className="rounded-xl border border-line bg-panel/60 px-4 py-3 mb-4"
          data-testid="cache-warm-card"
        >
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-semibold">{t("Cache warm-up")}</span>
            <Toggle checked={warm.enabled} onChange={toggleWarm} title={t("Warm cold knowledge prefixes in the background")} />
          </div>
          <p className="text-[11.5px] text-muted mt-1.5">
            {t(
              "When the cache hit rate drops below {rate}%, QunWork re-injects the coldest HORNET knowledge prefixes (max_tokens=1) so later turns hit instead of miss.",
              { rate: Math.round(warm.min_hit_rate * 100) }
            )}
          </p>
          <div className="flex items-center gap-4 mt-2 text-[11.5px] text-muted">
            <span>{t("This week")}: {warm.week.calls} {t("calls")} · {fmt(warm.week.prompt_tokens)} {t("prompt tokens")}</span>
            <span>{t("Org cache hit rate")}: {(warm.org_hit_rate * 100).toFixed(1)}%</span>
          </div>
          <div className="flex items-center gap-2 mt-2.5">
            <button
              className="text-[11.5px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent"
              onClick={warmNow}
              disabled={warming || !warm.enabled}
            >
              {warming ? t("Warming…") : t("Warm now")}
            </button>
            {warmNote && <span className="text-[11.5px] text-muted">{warmNote}</span>}
          </div>
        </div>
      )}

      {days.length > 0 ? (
        <div className="rounded-xl border border-line bg-panel/60 px-4 py-3 mb-4">
          <div className="text-[12px] font-semibold mb-2">{t("Last 14 days")}</div>
          <div className="flex items-end gap-[3px] h-[72px]">
            {days.map((d) => {
              const total = d.prompt_tokens + d.completion_tokens;
              const h = Math.max(3, Math.round((total / maxDay) * 68));
              return (
                <div key={d.day} className="flex-1 flex flex-col items-center gap-1" title={`${d.day}: ${fmt(total)} ${t("Tokens")}, ${(d.cache_hit_rate * 100).toFixed(0)}% ${t("Cache hit")}`}>
                  <div className="w-full rounded-sm flex flex-col-reverse" style={{ height: h }}>
                    <div className="w-full bg-blue-500/85" style={{ height: `${(d.completion_tokens / Math.max(1, total)) * 100}%` }} />
                    <div className="w-full bg-blue-400/45" style={{ height: `${(d.prompt_tokens / Math.max(1, total)) * 100}%` }} />
                  </div>
                  <div className="text-[9.5px] text-muted tabular-nums">{d.day.slice(5)}</div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {sessions.length > 0 ? (
        <div className="rounded-xl border border-line bg-panel/60 px-4 py-3">
          <div className="text-[12px] font-semibold mb-2">{t("By session")}</div>
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-muted text-left">
                <th className="py-1 pr-2 font-medium">{t("Session")}</th>
                <th className="py-1 pr-2 font-medium text-right">{t("Turns")}</th>
                <th className="py-1 pr-2 font-medium text-right">{t("Tokens")}</th>
                <th className="py-1 font-medium text-right">{t("Cache hit")}</th>
              </tr>
            </thead>
            <tbody>
              {sessions.slice(0, 10).map((s) => (
                <tr key={s.session_id} className="border-t border-line/60">
                  <td className="py-1.5 pr-2 text-muted">{shortSession(s.session_id)}</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{s.turns}</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{fmt(s.prompt_tokens + s.completion_tokens)}</td>
                  <td className="py-1.5 text-right tabular-nums">{(s.cache_hit_rate * 100).toFixed(0)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
        </>
      ) : (
        <RoiPanel />
      )}
    </section>
  );
}

// -- ROI 价值归因 (建议10: AI 团队账本) --------------------------------------
function RoiPanel() {
  const t = useT();
  const [report, setReport] = useState<RoiReport | null>(null);
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const [ratesText, setRatesText] = useState("");
  const [hourly, setHourly] = useState("");
  const [msg, setMsg] = useState("");

  const fmtMoney = (n: number) => `¥${n.toFixed(2)}`;

  useEffect(() => {
    getRoiConfig().then((c) => {
      setRatesText(JSON.stringify(c.rates ?? {}, null, 1));
      setHourly(String(c.hourly_rate ?? 0));
    }).catch(() => {});
  }, []);

  useEffect(() => {
    getRoiReport(month).then(setReport).catch(() => setReport(null));
  }, [month]);

  const saveConfig = async () => {
    try {
      const rates = JSON.parse(ratesText || "{}");
      await setRoiConfig(rates, Number(hourly) || 0);
      setMsg(t("Rates saved."));
    } catch {
      setMsg(t("Invalid rates JSON."));
    }
  };

  const genHtml = async () => {
    const r = await generateRoiReport(month);
    setMsg(r.ok ? t("Report written to {path}", { path: r.path ?? "" }) : t("Report failed."));
  };

  return (
    <div data-testid="roi-panel">
      <div className="flex items-center gap-2 mb-3">
        <h3 className="text-[15px] font-semibold">{t("AI team ledger · ROI")}</h3>
        <span className="text-[11px] text-faint">{t("本地计算, 数据不出机器")}</span>
      </div>

      {/* 费率配置 */}
      <div className="rounded-xl border border-line bg-panel/60 px-4 py-3 mb-4">
        <div className="text-[12px] font-semibold mb-1">{t("API rate config (per-million tokens)")}</div>
        <div className="text-[11px] text-faint mb-2">
          {t('JSON: {"model": {"prompt_ppm": 2, "completion_ppm": 8}}')}
        </div>
        <textarea
          className="w-full h-20 rounded-lg border border-line bg-paper px-2.5 py-1.5 text-[12px] font-mono outline-none focus:border-lineStrong"
          value={ratesText}
          onChange={(e) => setRatesText(e.target.value)}
          data-testid="roi-rates"
        />
        <div className="flex items-center gap-2 mt-2">
          <label className="text-[12px] text-faint">{t("Hourly rate (¥)")}</label>
          <input
            type="number" value={hourly}
            onChange={(e) => setHourly(e.target.value)}
            className="w-28 rounded-lg border border-line bg-paper px-2 py-1 text-[12px] outline-none focus:border-lineStrong"
            data-testid="roi-hourly"
          />
          <button className="btn-primary sm" onClick={() => void saveConfig()} data-testid="roi-save">
            {t("Save rates")}
          </button>
          <span className="text-[11.5px] text-muted">{msg}</span>
        </div>
      </div>

      {/* 月度报告 */}
      <div className="flex items-center gap-2 mb-2">
        <input
          type="month" value={month}
          onChange={(e) => setMonth(e.target.value)}
          className="rounded-lg border border-line bg-paper px-2 py-1 text-[12px] outline-none focus:border-lineStrong"
          data-testid="roi-month"
        />
        <button className="text-[11.5px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent"
          onClick={() => void genHtml()} data-testid="roi-html">
          {t("Generate HTML report")}
        </button>
      </div>

      {!report ? (
        <div className="text-[12px] text-muted">{t("Loading…")}</div>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2.5">
            <Stat label={t("Total cost")} value={fmtMoney(report.total_cost)} sub={report.missing_rates.length ? `${t("missing rates")}: ${report.missing_rates.join(", ")}` : undefined} />
            <Stat label={t("Cache savings")} value={fmtMoney(report.cache_saved)} />
            <Stat label={t("Labor saved")} value={`${report.labor_hours.toFixed(1)}h`} sub={fmtMoney(report.labor_saved)} />
          </div>
          {report.per_model.length > 0 && (
            <div className="rounded-xl border border-line bg-panel/60 px-4 py-3">
              <div className="text-[12px] font-semibold mb-1.5">{t("By model")}</div>
              <table className="w-full text-[12px]">
                <thead><tr className="text-faint text-left">
                  <th className="py-1">{t("Model")}</th><th>{t("Prompt")}</th>
                  <th>{t("Completion")}</th><th>{t("Cached")}</th>
                  <th>{t("Cost")}</th><th>{t("Cache saved")}</th>
                </tr></thead>
                <tbody>
                  {report.per_model.map((m) => (
                    <tr key={m.model} className="border-t border-line/60">
                      <td className="py-1">{m.model}</td>
                      <td className="tabular-nums">{m.prompt_tokens.toLocaleString()}</td>
                      <td className="tabular-nums">{m.completion_tokens.toLocaleString()}</td>
                      <td className="tabular-nums">{m.cached_tokens.toLocaleString()}</td>
                      <td className="tabular-nums">{fmtMoney(m.cost)}</td>
                      <td className="tabular-nums">{fmtMoney(m.cache_saved)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {Object.keys(report.by_tag).length > 0 && (
            <div className="rounded-xl border border-line bg-panel/60 px-4 py-3">
              <div className="text-[12px] font-semibold mb-1.5">{t("By value tag")}</div>
              <div className="space-y-1">
                {Object.entries(report.by_tag).map(([tag, b]) => (
                  <div key={tag} className="flex items-center gap-2 text-[12px]">
                    <span className="text-ink flex-1 truncate">{tag}</span>
                    <span className="text-faint">{b.runs} {t("runs")} · {b.hours.toFixed(1)}h</span>
                    <span className="tabular-nums">{fmtMoney(b.cost)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="text-[11.5px] text-faint">
            {t("Skill reuse saved {s} minutes this month.", { s: ((report.skill_reuse?.total_saved_seconds ?? 0) / 60).toFixed(1) })}
          </div>
        </div>
      )}
    </div>
  );
}
