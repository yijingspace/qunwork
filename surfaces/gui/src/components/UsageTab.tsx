import { useEffect, useState } from "react";
import {
  getUsage,
  getCacheWarmStatus,
  setCacheWarmEnabled,
  triggerCacheWarm,
  type CacheWarmStatus,
} from "../api";
import { useT } from "../i18n";
import { Toggle } from "./Toggle";

const FIELD_LABEL = "text-[12.5px] font-medium text-ink";

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
  const [totals, setTotals] = useState<UsageAgg | null>(null);
  const [days, setDays] = useState<DayRow[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [err, setErr] = useState("");
  // P0 建议1: cache warm-up toggle + this-week stats
  const [warm, setWarm] = useState<CacheWarmStatus | null>(null);
  const [warming, setWarming] = useState(false);
  const [warmNote, setWarmNote] = useState("");

  useEffect(() => {
    getUsage()
      .then((r) => {
        setTotals(r.totals ?? null);
        setDays(r.by_day ?? []);
        setSessions(r.by_session ?? []);
      })
      .catch(() => setErr(t("Failed to load usage data.")));
    getCacheWarmStatus().then(setWarm).catch(() => setWarm(null));
  }, [t]);

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
      getCacheWarmStatus().then(setWarm).catch(() => {});
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
      <div className="flex items-center gap-2 mb-3">
        <h3 className="text-[15px] font-semibold">{t("Token usage")}</h3>
        <span className="text-[11px] text-muted">· {t("Cache hit rate")}: </span>
        <span className={`text-[13px] font-semibold tabular-nums ${hitColor}`}>
          {(hit * 100).toFixed(1)}%
        </span>
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
    </section>
  );
}
