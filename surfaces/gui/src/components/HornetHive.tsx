import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useT } from "../i18n";
import {
  hornetBuild,
  hornetEvolve,
  hornetGraph,
  hornetHealth,
  hornetResonate,
  hornetStats,
  type HornetEdge,
  type HornetHit,
  type HornetNode,
} from "../api";

const RELATION_COLORS: Record<string, string> = {
  cause: "#d97706",
  similar: "#0ea5e9",
  opposite: "#ef4444",
  contains: "#10b981",
  temporal: "#8b5cf6",
  attribute: "#f59e0b",
};

const HEX_R = 26; // hex radius in px
const EDGE_REL_LABEL: Record<string, string> = {
  cause: "因果",
  similar: "相似",
  opposite: "对立",
  contains: "包含",
  temporal: "时序",
  attribute: "属性",
};

// #5 Audible knowledge topology — pentatonic scale (C, D, E, G, A) for
// consonant harmonics. Each hit maps to a scale degree; amplitude → volume;
// hits play in sequence to trace the wave propagation path.
const PENTATONIC = [261.63, 293.66, 329.63, 392.00, 440.00]; // C4, D4, E4, G4, A4

function nodeToFreq(nodeId: number): number {
  const degree = Math.abs(nodeId) % PENTATONIC.length;
  const octave = Math.floor(Math.abs(nodeId) / PENTATONIC.length) % 3;
  return PENTATONIC[degree] * Math.pow(2, octave);
}

function useHornetAudio() {
  const ctxRef = useRef<AudioContext | null>(null);
  const ensureCtx = useCallback(() => {
    if (!ctxRef.current) {
      const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      if (Ctor) ctxRef.current = new Ctor();
    }
    return ctxRef.current;
  }, []);
  const playResonance = useCallback((hitList: HornetHit[]) => {
    const ctx = ensureCtx();
    if (!ctx) return;
    if (ctx.state === "suspended") void ctx.resume();
    const now = ctx.currentTime;
    hitList.forEach((h, i) => {
      const freq = nodeToFreq(h.node_id);
      const vol = Math.min(0.3, Math.max(0.05, h.amplitude / 300));
      const start = now + i * 0.15;
      const dur = 0.4;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = i === 0 ? "sine" : "triangle";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(vol, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, start + dur);
      osc.connect(gain).connect(ctx.destination);
      osc.start(start);
      osc.stop(start + dur);
    });
  }, [ensureCtx]);
  return { playResonance };
}

function axialToPixel(q: number, r: number, R = HEX_R): [number, number] {
  return [R * Math.sqrt(3) * (q + r / 2), R * 1.5 * r];
}

/** Isometric 3D projection: XY hex plane + z height (Z+ up, Z- down). */
function isoToPixel(x: number, y: number, z: number, R = HEX_R): [number, number] {
  const sx = (x - z) * R * Math.sqrt(3) * 0.866;
  const sy = (x + z) * R * 0.5 + y * R * -1.2;
  return [sx, sy];
}

type ViewMode = "iso" | "top" | "zminus" | "zplus";


/** B: 知识场热力图 — color by resonance temperature.
 *  Cold (cavity/never hit/low freshness) → blue/purple
 *  Warm (moderate amplitude) → amber
 *  Hot (high amplitude/fission source) → red
 *  When no active query, freshness drives the gradient instead. */
function tempColor(z: number, freshness: number, amplitude: number | undefined): string {
  if (amplitude !== undefined && amplitude > 0) {
    const t = Math.min(1, amplitude);
    if (t > 0.66) return "#ef4444"; // hot — red
    if (t > 0.33) return "#f59e0b"; // warm — amber
    return "#3b82f6"; // cool — blue
  }
  const f = freshness ?? 1.0;
  if (f < 0.3) return "#4c1d95"; // stale — deep purple (cavity)
  if (f < 0.6) return "#1e3a5f"; // cooling — dark blue
  if (z > 0) return "#1d4ed8"; // Z+ fresh — blue
  if (z < 0) return "#7c3aed"; // Z- — purple
  return "#1e293b"; // XY — slate
}

function hexPoints(cx: number, cy: number, R = HEX_R): string {
  const pts: string[] = [];
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i - Math.PI / 6;
    pts.push(`${(cx + R * Math.cos(a)).toFixed(1)},${(cy + R * Math.sin(a)).toFixed(1)}`);
  }
  return pts.join(" ");
}

/** HORNET 2D hive: hexagonal cell grid + resonance wave highlighting. */
interface HornetHiveProps {
  onResume?: (payload: { title: string; content: string; source?: string; id?: number }) => void;
}

export function HornetHive({ onResume }: HornetHiveProps) {
  const t = useT();
  const [nodes, setNodes] = useState<HornetNode[]>([]);
  const [edges, setEdges] = useState<HornetEdge[]>([]);
  const [stats, setStats] = useState<{ nodes: number; edges: number; resonance_runs: number; emergent: number } | null>(null);
  const [emergents, setEmergents] = useState<{ id: number; kind: string; title: string }[]>([]);
  const [busy, setBusy] = useState<"build" | "evolve" | null>(null);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<HornetHit[] | null>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState<ViewMode>("iso");
  const [useTopo, setUseTopo] = useState(false);
  const [selEmergent, setSelEmergent] = useState<{ id: number; kind: string; title: string; detail?: string; kbId?: number } | null>(null);
  const [health, setHealth] = useState<{ score: number; rating: string; dimensions: { structure: number; dynamics: number; evolution: number } } | null>(null);
  const [soundOn, setSoundOn] = useState(false);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const { playResonance } = useHornetAudio();

  const refresh = useCallback(async () => {
    try {
      const [g, s, h] = await Promise.all([hornetGraph(), hornetStats(), hornetHealth().catch(() => null)]);
      setHealth(h);
      setNodes(g.nodes ?? []);
      setEdges(g.edges ?? []);
      setStats({
        nodes: s.nodes,
        edges: s.edges,
        resonance_runs: s.resonance_runs,
        emergent: s.emergent,
      });
      setEmergents((s.emergent_items ?? []).map((e) => ({ id: e.id, kind: e.kind, title: e.title })));
    } catch {
      setError(t("Couldn't reach the local engine — HORNET is unavailable."));
    }
  }, [t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const pos = useMemo(() => {
    const m = new Map<number, [number, number]>();
    for (const n of nodes) {
      if (view === "top") m.set(n.id, axialToPixel(n.x, n.y));
      else if (view === "zminus" && n.z >= 0) continue;
      else if (view === "zplus" && n.z <= 0) continue;
      else m.set(n.id, isoToPixel(n.x, n.y, n.z));
    }
    return m;
  }, [nodes, view]);

  const visibleNodes = useMemo(
    () => (view === "top" || view === "iso" ? nodes : nodes.filter((n) => (view === "zminus" ? n.z < 0 : n.z > 0))),
    [nodes, view],
  );

  const viewBox = useMemo(() => {
    if (!nodes.length) return "0 0 600 400";
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [, [x, y]] of pos) {
      minX = Math.min(minX, x - HEX_R);
      minY = Math.min(minY, y - HEX_R);
      maxX = Math.max(maxX, x + HEX_R);
      maxY = Math.max(maxY, y + HEX_R);
    }
    const pad = 40;
    return `${minX - pad} ${minY - pad} ${maxX - minX + pad * 2} ${maxY - minY + pad * 2}`;
  }, [pos, nodes.length]);

  const hitAmplitude = useMemo(() => {
    const m = new Map<number, number>();
    for (const h of hits ?? []) m.set(h.node_id, h.amplitude);
    return m;
  }, [hits]);

  const handleBuild = async () => {
    setBusy("build");
    setError("");
    try {
      const r = await hornetBuild(true, useTopo);
      void r;
      setQuery("");
      setHits(null);
      await refresh();
    } catch {
      setError(t("Build failed — check the engine connection."));
    } finally {
      setBusy(null);
    }
  };

  const handleEvolve = async () => {
    setBusy("evolve");
    setError("");
    try {
      await hornetEvolve();
      await refresh();
    } catch {
      setError(t("Evolve failed — check the engine connection."));
    } finally {
      setBusy(null);
    }
  };

  const handleResonate = async () => {
    if (!query.trim()) return;
    setError("");
    try {
      const r = await hornetResonate(query.trim());
      setHits(r.hits ?? []);
      if (soundOn && r.hits && r.hits.length > 0) {
        playResonance(r.hits);
      }
    } catch {
      setError(t("Resonance failed — check the engine connection."));
    }
  };

  return (
    <div className="rounded-xl2 border border-line bg-panel p-4 mb-4" data-testid="hornet-hive">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[15px]">🐝</span>
        <span className="text-[13.5px] font-semibold text-ink">{t("HORNET hive")}</span>
        <span className="text-[11px] text-faint">
          {stats ? `${stats.nodes} ${t("cells")} · ${stats.edges} ${t("edges")} · ${stats.resonance_runs} ${t("resonances")} · ${stats.emergent} ${t("emergent")}` : t("Loading…")}
          {stats && (stats as { emergent_unread?: number }).emergent_unread ? (
            <span className="ml-1.5 px-1.5 py-px rounded-full bg-emerald-700 text-emerald-50 text-[10px]" data-testid="hornet-unread-badge">
              🧬 {t("new")} {(stats as { emergent_unread?: number }).emergent_unread}
            </span>
          ) : null}
        </span>
        {health && (
          <span
            className="inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full border border-line"
            title={`${t("Structure")} ${health.dimensions.structure} · ${t("Dynamics")} ${health.dimensions.dynamics} · ${t("Evolution")} ${health.dimensions.evolution}`}
            data-testid="hornet-health"
          >
            <span className={"w-2 h-2 rounded-full " + (health.rating === "healthy" ? "bg-emerald-500" : health.rating === "sub-healthy" ? "bg-amber-400" : "bg-rose-500")} />
            {t("Health")} {health.score}/100 · {health.rating}
          </span>
        )}
        <span className="ml-auto flex gap-2">
          <button className="btn-secondary text-[11.5px]" onClick={handleBuild} disabled={busy !== null}>
            {busy === "build" ? t("Building…") : t("Build hive")}
          </button>
          <label className="inline-flex items-center gap-1 text-[11px] text-faint cursor-pointer" title={t("Topological soft-constraint embedding (numpy GCN + ring loss) — slower build, topology-aware layout & edges.")}>
            <input type="checkbox" checked={useTopo} onChange={(e) => setUseTopo(e.target.checked)} className="accent-accent" data-testid="hornet-topo-toggle" />
            {t("Topo embed")}
          </label>
          <button className="btn-secondary text-[11.5px]" onClick={handleEvolve} disabled={busy !== null}>
            {busy === "evolve" ? t("Evolving…") : t("Evolve")}
          </button>
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 mb-2">
        {(
          [
            ["iso", t("3D isometric")],
            ["top", t("XY top")],
            ["zminus", t("Z- traceback")],
            ["zplus", t("Z+ projection")],
          ] as [ViewMode, string][]
        ).map(([mode, label]) => (
          <button
            key={mode}
            className={
              "text-[11px] px-2 py-0.5 rounded-full border " +
              (view === mode ? "bg-accentSoft text-accent border-accent" : "text-faint border-line hover:text-ink")
            }
            onClick={() => setView(mode)}
            data-testid={`hornet-view-${mode}`}
          >
            {label}
          </button>
        ))}
      </div>
      <p className="text-[11px] text-faint mb-3">
        {t("Six semantic edges (cause/similar/opposite/contains/temporal/attribute) over hexagonal cells — resonance retrieval spreads like a wave and amplifies on phase match.")}
      </p>

      <div className="flex gap-2 mb-3">
        <input
          className="flex-1 px-3 py-1.5 rounded-lg border border-line bg-surface text-[13px] outline-none focus:border-accent"
          placeholder={t("Probe wave — ask the hive…")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleResonate()}
          data-testid="hornet-query"
        />
        <button className="btn-primary text-[12px]" onClick={handleResonate} disabled={!query.trim()}>
          {t("Resonate")}
        </button>
        <button
          className={"text-[12px] px-3 py-1.5 rounded-lg border " + (soundOn ? "bg-accentSoft text-accent border-accent" : "text-faint border-line hover:text-ink")}
          onClick={() => setSoundOn(!soundOn)}
          title={t("Audible resonance — hear the wave propagation as harmonics")}
          data-testid="hornet-sound-toggle"
        >
          {soundOn ? "🔊" : "🔈"}
        </button>
      </div>

      {error && (
        <div className="mb-3 px-3 py-2 rounded-lg border border-warnInk/30 bg-warnSoft/60 text-[12px] text-warnInk" role="alert">
          ⚠ {error}
        </div>
      )}

      {nodes.length === 0 && !busy ? (
        <div className="text-[12.5px] text-muted py-6 text-center">
          {t("Hive empty — click Build hive to map your knowledge into hexagonal cells.")}
        </div>
      ) : (
        <svg
          ref={svgRef}
          viewBox={viewBox}
          className="w-full h-[380px] rounded-lg border border-line bg-[#0b1020]"
          data-testid="hornet-svg"
        >
          {edges.map((e, i) => {
            const p1 = pos.get(e.src);
            const p2 = pos.get(e.dst);
            if (!p1 || !p2) return null;            const color = RELATION_COLORS[e.relation] || "#64748b";
            const active = (hitAmplitude.has(e.src) || hitAmplitude.has(e.dst)) && hits;
            return (
              <line
                key={i}
                x1={p1[0]} y1={p1[1]} x2={p2[0]} y2={p2[1]}
                stroke={active ? "#f43f5e" : color}
                strokeWidth={active ? 2 : 1}
                strokeOpacity={active ? 0.95 : 0.45}
                strokeDasharray={active ? "4 3" : undefined}
              >
                {active ? <animate attributeName="stroke-dashoffset" from="14" to="0" dur="0.8s" repeatCount="indefinite" /> : null}
              </line>
            );
          })}
          {visibleNodes.map((n) => {
            const [cx, cy] = pos.get(n.id) ?? [0, 0];
            const amp = hitAmplitude.get(n.id);
            const isHit = amp !== undefined;
            const scale = isHit ? Math.min(1.6, 1 + amp) : 1;  // amplitude is already 0..1
            const fill = isHit ? "#f43f5e" : tempColor(n.z, n.freshness ?? 1.0, undefined);
            return (
              <g key={n.id} transform={`translate(${cx} ${cy}) scale(${scale})`}>
                <polygon
                  points={hexPoints(0, 0)}
                  fill={fill}
                  stroke={isHit ? "#fda4af" : n.z !== 0 ? "#475569" : "#334155"}
                  strokeWidth={isHit ? 2 : 1}
                >
                  {isHit ? (
                    <animate attributeName="opacity" values="1;0.55;1" dur="1.1s" repeatCount="indefinite" />
                  ) : null}
                </polygon>
                <text
                  x={0} y={2}
                  textAnchor="middle"
                  fontSize={isHit ? 11 : 9}
                  fill={isHit ? "#fff" : "#94a3b8"}
                  style={{ pointerEvents: "none" }}
                >
                  {n.title.length > 10 ? n.title.slice(0, 10) + "…" : n.title}
                </text>
              </g>
            );
          })}
        </svg>
      )}

      <div className="flex flex-wrap gap-2 mt-3">
        {Object.entries(RELATION_COLORS).map(([rel, color]) => (
          <span key={rel} className="inline-flex items-center gap-1.5 text-[10.5px] text-faint">
            <span className="inline-block w-3 h-[2px] rounded" style={{ background: color }} />
            {EDGE_REL_LABEL[rel] || rel}
          </span>
        ))}
      </div>

      {hits && hits.length > 0 && (
        <div className="mt-3 border-t border-line pt-3" data-testid="hornet-hits">
          <div className="text-[12px] font-medium text-ink mb-1.5">{t("Resonance hits")}</div>
          <div className="flex flex-col gap-1">
            {hits.map((h) => (
              <div key={h.node_id} className="flex items-center gap-2 text-[12px]">
                <span className="text-[11px] text-rose-400">◉</span>
                <span className="truncate text-ink flex-1">{h.title}</span>
                <span className="text-[10.5px] text-faint font-mono">
                  {t("amp")} {h.amplitude.toFixed(1)}
                </span>
                <span className="text-[10px] text-faint shrink-0">
                  {(h.path ?? []).slice(-3).join(" → ")}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {emergents.length > 0 && (
        <div className="mt-3 border-t border-line pt-3" data-testid="hornet-emergent">
          <div className="text-[12px] font-medium text-ink mb-1.5">{t("Emerged structure")}</div>
          <div className="flex flex-col gap-1">
            {emergents.slice(0, 8).map((e) => (
              <div key={e.id} className="flex items-center gap-2 text-[12px]">
                <span className="text-[11px]">
                  {e.kind === "hypernode" ? "🧬" : e.kind === "attractor" ? "⚠️" : e.kind === "fission" ? "🌱" : e.kind === "cavity" ? "🌀" : e.kind === "conflict" ? "⚡" : "🕳"}
                </span>
                <button
                  className="truncate text-ink hover:text-accent text-left min-w-0 flex-1"
                  title={t("Click to view this emergent finding")}
                  onClick={async () => {
                    // Show detail (persisted knowledge body if any) in a modal;
                    // the user then decides to research it or dismiss.
                    const { listKnowledge, knowledgeResumePack } = await import("../api");
                    let body = `${t("Emergent kind")}: ${e.kind}
${t("Auto-surfaced by the hive")}`;
                    let kbId: number | undefined;
                    try {
                      const k = await listKnowledge(200, 0);
                      const hit = (k.items ?? []).find(
                        (x) => x.title === `[涌现] ${e.title}` || x.title === `[蜂胞分裂] ${e.title}`,
                      );
                      if (hit) {
                        const pk = await knowledgeResumePack(hit.id);
                        if (pk.ok && pk.pack) body = pk.pack.content || body;
                        kbId = hit.id;
                      }
                    } catch {
                      /* fall through */
                    }
                    setSelEmergent({ id: e.id, kind: e.kind, title: e.title, detail: body, kbId });
                  }}
                  data-testid={`emergent-resume-${e.id}`}
                >
                  {e.title}
                </button>
                <span className="text-[10px] text-faint shrink-0">{e.kind}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {selEmergent && (
        <div
          className="fixed inset-0 z-[60] bg-black/40 flex items-center justify-center p-6"
          onClick={() => setSelEmergent(null)}
          data-testid="emergent-modal"
        >
          <div
            className="max-w-xl w-full rounded-xl2 border border-line bg-panel p-4 shadow-xl"
            onClick={(ev) => ev.stopPropagation()}
          >
            <div className="flex items-center gap-2 mb-2">
              <span className="text-[14px]">
                {selEmergent.kind === "hypernode" ? "🧬" : selEmergent.kind === "attractor" ? "⚠️" : selEmergent.kind === "fission" ? "🌱" : selEmergent.kind === "cavity" ? "🌀" : selEmergent.kind === "conflict" ? "⚡" : "🕳"}
              </span>
              <span className="text-[13.5px] font-semibold text-ink flex-1">{selEmergent.title}</span>
              <button className="text-muted hover:text-ink shrink-0" onClick={() => setSelEmergent(null)} aria-label={t("Dismiss")}>
                ✕
              </button>
            </div>
            <div className="whitespace-pre-wrap max-h-72 overflow-y-auto hairline-scroll text-[12.5px] text-ink mb-3 bg-surface rounded-lg border border-line p-3">
              {selEmergent.detail || t("No content")}
            </div>
            <div className="flex justify-end gap-2">
              <button className="btn-secondary text-[12px]" onClick={() => setSelEmergent(null)}>
                {t("Dismiss")}
              </button>
              <button
                className="btn-primary text-[12px]"
                onClick={async () => {
                  if (selEmergent.kbId) {
                    const { knowledgeResumePack } = await import("../api");
                    try {
                      const pk = await knowledgeResumePack(selEmergent.kbId);
                      if (pk.ok && pk.pack) {
                        onResume?.({ title: pk.pack.title, content: pk.pack.content, source: pk.pack.source ?? undefined, id: selEmergent.kbId });
                        setSelEmergent(null);
                        return;
                      }
                    } catch {
                      /* fall through */
                    }
                  }
                  onResume?.({ title: selEmergent.title, content: selEmergent.detail ?? "", source: `${t("HORNET")} ${selEmergent.kind}` });
                  setSelEmergent(null);
                }}
                data-testid="emergent-modal-research"
              >
                🧠 {t("Research")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
