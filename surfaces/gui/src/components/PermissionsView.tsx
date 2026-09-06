import { useEffect, useMemo, useState } from "react";
import { useT } from "../i18n";
import {
  getConnectorScopes,
  getPermissions,
  getPermissionsHeatmap,
  getPersonaScopes,
  listTeamAudit,
  resetTeamMatrix,
  setTeamMatrixCapability,
  setPersonaScopes,
  type AuditEvent,
  type ConnectorScopeMatrix,
  type PermissionHeatmapCell,
  type PermissionMatrix,
} from "../api";

/**
 * Permissions page (P2 + P1-5 + 方案D).
 *
 * 四个区块:
 *  1. 角色 × 能力 矩阵 — **可编辑** (单格点击 grant/revoke, 组织覆盖层随
 *     P2P 同步传播全团队; override 角标 + 列级重置)
 *  2. P1-5 零信任能力袋: 角色 × 连接器 × scope 等级 配置面板
 *  3. P1-5 权限审计热力图: persona × connector × tool 调用次数 / 越权升级次数
 *  4. 方案D 治理审计日志: 矩阵变更 / 成员生命周期 / 组织门禁拦截事件流
 */
export function PermissionsView() {
  const t = useT();
  const [matrix, setMatrix] = useState<PermissionMatrix | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyCell, setBusyCell] = useState<string | null>(null);
  const [cellError, setCellError] = useState<string | null>(null);

  const reload = () =>
    getPermissions()
      .then((m) => setMatrix(m ?? null))
      .catch(() => setMatrix(null));

  useEffect(() => {
    let alive = true;
    getPermissions()
      .then((m) => { if (alive) setMatrix(m ?? null); })
      .catch(() => { if (alive) setMatrix(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Derive roles + capabilities dynamically from the matrix.
  const roleNames = matrix?.roles ? Object.keys(matrix.roles) : [];
  const capabilities = matrix?.capabilities ?? (() => {
    const capSet = new Set<string>();
    if (matrix?.roles) {
      for (const r of roleNames) {
        for (const c of Object.keys(matrix.roles[r])) capSet.add(c);
      }
    }
    return Array.from(capSet);
  })();

  const onToggleCell = async (role: string, cap: string, grant: boolean) => {
    const key = `${role}:${cap}`;
    if (busyCell) return;
    setBusyCell(key);
    setCellError(null);
    try {
      const r = await setTeamMatrixCapability(role, cap, grant);
      if (!r.ok && r.error) setCellError(r.error);
      await reload();
    } finally {
      setBusyCell(null);
    }
  };

  const onResetRole = async (role: string) => {
    if (busyCell) return;
    setBusyCell(`reset:${role}`);
    try {
      await resetTeamMatrix(role);
      await reload();
    } finally {
      setBusyCell(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-5xl mx-auto px-6 py-6">
        <h1 className="text-[22px] font-semibold tracking-tight flex items-center gap-2.5">
          <span className="text-[20px]">🔐</span>
          {t("Permissions")}
        </h1>
        <p className="text-[13px] text-muted mt-1.5 leading-relaxed">
          {t("Role-based access control for your AI swarm organization.")}
        </p>

        {/* 1. 权限矩阵表 */}
        <div className="mt-5 rounded-xl2 border border-line bg-panel p-4 overflow-x-auto">
          <div className="text-[14px] font-semibold mb-3">{t("Role Capability Matrix")}</div>
          {loading ? (
            <div className="text-[13px] text-faint">{t("Loading…")}</div>
          ) : !matrix || roleNames.length === 0 || capabilities.length === 0 ? (
            <div className="text-[13px] text-faint">
              {t("Team features require the team module (Phase 1).")}
            </div>
          ) : (
            <table className="w-full text-[12.5px] min-w-[600px]">
              <thead>
                <tr className="text-faint text-left border-b border-line">
                  <th className="py-1.5 font-medium">{t("Capability")}</th>
                  {roleNames.map((r) => {
                    const hasOverride = !!(matrix.overrides?.[r]?.add?.length || matrix.overrides?.[r]?.remove?.length);
                    return (
                      <th key={r} className="py-1.5 font-medium text-center">
                        {t(r)}
                        {hasOverride && (
                          <button
                            onClick={() => onResetRole(r)}
                            disabled={busyCell === `reset:${r}`}
                            className="ml-1 text-[10px] text-accent hover:underline"
                            title={t("Reset this role to the default matrix")}
                          >
                            ✎
                          </button>
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {capabilities.map((cap) => (
                  <tr key={cap} className="border-b border-line/50">
                    <td className="py-2 font-medium">{t(cap)}</td>
                    {roleNames.map((r) => {
                      const cell = matrix.roles?.[r]?.[cap];
                      const allowed = cell?.allowed;
                      const scope = cell?.scope;
                      const max = cell?.max_amount;
                      const ov = matrix.overrides?.[r];
                      const ovAdd = !!ov?.add?.includes(cap);
                      const ovRemove = !!ov?.remove?.some((x) => x === cap || x.startsWith(cap + ":"));
                      const title =
                        scope && max ? `${scope} · ≤ ¥${max.toLocaleString()}`
                          : scope ? scope
                          : max ? `≤ ¥${max.toLocaleString()}`
                          : undefined;
                      const key = `${r}:${cap}`;
                      return (
                        <td key={r} className="py-2 text-center">
                          <button
                            onClick={() => onToggleCell(r, cap, !allowed)}
                            disabled={busyCell !== null || allowed === undefined}
                            className={
                              "px-1.5 py-0.5 rounded-md text-[12px] transition disabled:cursor-default " +
                              (allowed === undefined
                                ? "text-faint"
                                : allowed
                                  ? "text-ok hover:bg-ok/10"
                                  : "text-faint hover:bg-line/40 hover:text-muted")
                            }
                            title={
                              busyCell === key
                                ? "…"
                                : [
                                    title,
                                    allowed
                                      ? t("Click to revoke for this role (synced to the org)")
                                      : t("Click to grant this capability (synced to the org)"),
                                  ].filter(Boolean).join(" · ")
                            }
                          >
                            {busyCell === key ? "…" : allowed === undefined ? "—" : allowed ? (scope ? scope : "✅") : "❌"}
                            {ovAdd && <sup className="text-accent text-[9px]">＋</sup>}
                            {ovRemove && <sup className="text-danger text-[9px]">－</sup>}
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {cellError && (
            <div className="mt-2 px-3 py-1.5 rounded-lg border border-warnInk/30 bg-warnSoft/60 text-[12px] text-warnInk" role="alert">
              {cellError}
            </div>
          )}
        </div>

        {/* 2. 金额阈值 */}
        <div className="mt-4 rounded-xl2 border border-line bg-panel p-4">
          <div className="text-[14px] font-semibold mb-3">{t("Approval Thresholds")}</div>
          {loading ? (
            <div className="text-[13px] text-faint">{t("Loading…")}</div>
          ) : matrix?.thresholds?.length ? (
            <div className="space-y-2">
              {matrix.thresholds.map((th, i) => (
                <div key={i} className="flex items-center gap-3 text-[13px]">
                  <span className="text-muted font-mono">
                    ¥{th.min_amount.toLocaleString()} — {th.max_amount ? `¥${th.max_amount.toLocaleString()}` : "∞"}
                  </span>
                  <span className="text-faint">→</span>
                  <span className="font-medium">{t(th.approver_role)}</span>
                  {th.require_human && <span className="text-accent text-[12px]">+ {t("Human review")}</span>}
                  {th.require_board && <span className="text-danger text-[12px]">+ {t("Board review")}</span>}
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[13px] text-faint">
              {t("Approval thresholds will be configurable in Phase 2.")}
            </div>
          )}
        </div>

        {/* 3. P1-5 零信任能力袋 scope 配置 */}
        <ScopeConfigPanel roleNames={roleNames.length ? roleNames : ["default"]} />

        {/* 4. P1-5 权限审计热力图 */}
        <PermissionsHeatmapPanel />

        {/* 5. 方案D 治理审计日志 */}
        <GovernanceAuditPanel />
      </div>
    </div>
  );
}

// -- 方案D 治理审计日志面板 ---------------------------------------------------

const AUDIT_ACTION_LABELS: Record<string, string> = {
  "matrix.grant": "矩阵授权",
  "matrix.revoke": "矩阵回收",
  "matrix.reset": "矩阵重置",
  "member.add": "成员添加",
  "member.update": "成员变更",
  "member.remove": "成员移除",
  "member.invited": "发出邀请",
  "member.joined": "加入团队",
  "org_gate.deny": "门禁拦截",
  "org_gate.fund_deny": "资金拦截",
};

function fmtAuditDetail(detail: Record<string, unknown>): string {
  const parts: string[] = [];
  if (detail.capability) parts.push(String(detail.capability));
  if (detail.role) parts.push(String(detail.role));
  if (detail.name) parts.push(String(detail.name));
  if (detail.tool) parts.push(String(detail.tool));
  if (typeof detail.amount === "number") parts.push(`¥${detail.amount.toLocaleString()}`);
  if (detail.reason) parts.push(String(detail.reason));
  return parts.join(" · ");
}

function GovernanceAuditPanel() {
  const t = useT();
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const fetchPage = async (beforeSeq = 0) => {
    setLoadingMore(true);
    try {
      const r = await listTeamAudit({ limit: 50, beforeSeq });
      setEvents((prev) => (beforeSeq ? [...(prev ?? []), ...r.events] : r.events));
    } catch {
      setEvents((prev) => prev ?? []);
    } finally {
      setLoadingMore(false);
    }
  };

  useEffect(() => {
    void fetchPage(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="mt-4 rounded-xl2 border border-line bg-panel p-4">
      <div className="flex items-center gap-3 mb-3">
        <div className="text-[14px] font-semibold">{t("Governance Audit Log")}</div>
        <span className="text-[11.5px] text-faint">
          {t("Matrix changes · member lifecycle · org-gate denials — the org's paper trail")}
        </span>
      </div>
      {!events ? (
        <div className="text-[13px] text-faint">{t("Loading…")}</div>
      ) : events.length === 0 ? (
        <div className="text-[13px] text-faint">{t("No governance events yet.")}</div>
      ) : (
        <>
          <div className="space-y-1">
            {events.map((e) => {
              const deny = e.action.startsWith("org_gate.");
              return (
                <div key={e.seq} className="flex items-baseline gap-2.5 text-[12px] leading-relaxed">
                  <span className="font-mono text-faint shrink-0">
                    {new Date(e.ts * 1000).toLocaleString()}
                  </span>
                  <span
                    className={
                      "shrink-0 rounded-md px-1.5 py-px text-[11px] border " +
                      (deny
                        ? "border-danger/40 text-danger bg-danger/5"
                        : e.action.startsWith("matrix.")
                          ? "border-accent/40 text-accent bg-accent/5"
                          : "border-line text-muted")
                    }
                  >
                    {AUDIT_ACTION_LABELS[e.action] ?? e.action}
                  </span>
                  <span className="font-medium shrink-0">
                    {e.target.length > 14 ? `${e.target.slice(0, 8)}…${e.target.slice(-4)}` : t(e.target)}
                  </span>
                  <span className="text-muted truncate" title={fmtAuditDetail(e.detail)}>
                    {fmtAuditDetail(e.detail)}
                  </span>
                </div>
              );
            })}
          </div>
          {events.length >= 50 && (
            <button
              onClick={() => void fetchPage(events[events.length - 1].seq)}
              disabled={loadingMore}
              className="mt-2 text-[12px] text-accent hover:underline disabled:opacity-50"
            >
              {loadingMore ? "…" : t("Load more")}
            </button>
          )}
        </>
      )}
    </div>
  );
}

// -- P1-5 零信任能力袋 scope 配置面板 ----------------------------------------

const LEVELS = ["read", "write", "admin"] as const;
type ScopeLevel = (typeof LEVELS)[number];

const CONNECTOR_LABELS: Record<string, { name: string; brand: string }> = {
  github: { name: "GitHub", brand: "#6e7681" },
  gitlab: { name: "GitLab", brand: "#fc6d26" },
  slack: { name: "Slack", brand: "#4a154b" },
  telegram: { name: "Telegram", brand: "#0088cc" },
  jira: { name: "Jira", brand: "#0052cc" },
  notion: { name: "Notion", brand: "#000000" },
  linear: { name: "Linear", brand: "#5e6ad2" },
  asana: { name: "Asana", brand: "#f06a6a" },
  // 国内连接器
  wecom: { name: "企业微信", brand: "#07c160" },
  dingtalk: { name: "钉钉", brand: "#1677ff" },
  feishu: { name: "飞书", brand: "#00d6b9" },
};

function ScopeConfigPanel({ roleNames }: { roleNames: string[] }) {
  const t = useT();
  const [matrix, setMatrix] = useState<ConnectorScopeMatrix | null>(null);
  const [personaId, setPersonaId] = useState<string>(roleNames[0] ?? "default");
  const [granted, setGranted] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState<string | null>(null); // connector being saved
  const [notice, setNotice] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    getConnectorScopes()
      .then((r) => { if (alive) setMatrix(r.connectors ?? null); })
      .catch(() => { if (alive) setMatrix(null); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!personaId) return;
    let alive = true;
    setErr("");
    getPersonaScopes(personaId)
      .then((r) => { if (alive) setGranted(r.scopes ?? {}); })
      .catch((e) => { if (alive) setErr(String(e)); });
    return () => { alive = false; };
  }, [personaId]);

  // 把 connector 下声明的 resources 抽出来, 给每个 resource 配置等级
  const connectors = useMemo(() => {
    if (!matrix) return [];
    return Object.keys(matrix).sort();
  }, [matrix]);

  // 对每个 connector, 抽出 resource 列表 (read:repo → repo)
  const connectorResources = useMemo(() => {
    const out: Record<string, string[]> = {};
    if (!matrix) return out;
    for (const c of connectors) {
      const resSet = new Set<string>();
      for (const scopes of Object.values(matrix[c] ?? {})) {
        for (const s of scopes) {
          const idx = s.indexOf(":");
          if (idx > 0) resSet.add(s.slice(idx + 1));
        }
      }
      out[c] = Array.from(resSet).sort();
    }
    return out;
  }, [matrix, connectors]);

  /** 当前 persona 在某 connector × resource 上被授予的最高等级 (-1 = 无) */
  function grantedLevel(connector: string, resource: string): number {
    const list = granted[connector] ?? [];
    let max = -1;
    for (const s of list) {
      const idx = s.indexOf(":");
      if (idx <= 0) continue;
      const lvl = s.slice(0, idx);
      const res = s.slice(idx + 1);
      if (res !== resource) continue;
      const n = LEVELS.indexOf(lvl as ScopeLevel);
      if (n > max) max = n;
    }
    return max;
  }

  async function saveConnector(connector: string) {
    setBusy(connector);
    setNotice("");
    setErr("");
    try {
      // 把所有 resource 的等级展开成 scope 列表
      const scopes: string[] = [];
      for (const res of connectorResources[connector] ?? []) {
        const lvl = grantedLevel(connector, res);
        if (lvl >= 0) scopes.push(`${LEVELS[lvl]}:${res}`);
      }
      await setPersonaScopes(personaId, connector, scopes);
      setGranted((g) => ({ ...g, [connector]: scopes }));
      setNotice(`${t("Saved")} ${CONNECTOR_LABELS[connector]?.name ?? connector}`);
      window.setTimeout(() => setNotice(""), 3000);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  function toggleLevel(connector: string, resource: string, level: ScopeLevel) {
    const current = grantedLevel(connector, resource);
    const newLevel = LEVELS.indexOf(level) === current ? -1 : LEVELS.indexOf(level);
    // 把该 resource 的所有 scope 滤掉, 然后加上新的 (如果有)
    const list = (granted[connector] ?? []).filter((s) => {
      const idx = s.indexOf(":");
      return idx <= 0 || s.slice(idx + 1) !== resource;
    });
    if (newLevel >= 0) list.push(`${LEVELS[newLevel]}:${resource}`);
    setGranted((g) => ({ ...g, [connector]: list }));
  }

  return (
    <div className="mt-4 rounded-xl2 border border-line bg-panel p-4">
      <div className="flex items-center gap-3 mb-3 flex-wrap">
        <div className="text-[14px] font-semibold">{t("Connector Scope Configuration")}</div>
        <span className="text-[11.5px] text-faint">
          {t("Zero-trust least-privilege scopes per persona · write/admin needs explicit grant")}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <label className="text-[11.5px] text-faint">{t("Persona")}</label>
          <select
            className="px-2 py-1 rounded-lg border border-line bg-surface text-[12.5px] outline-none"
            value={personaId}
            onChange={(e) => setPersonaId(e.target.value)}
          >
            {roleNames.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </div>
      </div>

      {err && (
        <div className="mb-3 px-3 py-1.5 rounded-lg border border-warnInk/30 bg-warnSoft/60 text-[12px] text-warnInk" role="alert">
          {err}
        </div>
      )}
      {notice && (
        <div className="mb-3 px-3 py-1.5 rounded-lg border border-line bg-surface text-[12px]" role="status">
          ✓ {notice}
        </div>
      )}

      {!matrix ? (
        <div className="text-[13px] text-faint">{t("Loading…")}</div>
      ) : connectors.length === 0 ? (
        <div className="text-[13px] text-faint">
          {t("No connector scopes declared — connect a tool first.")}
        </div>
      ) : (
        <div className="space-y-3">
          {connectors.map((c) => {
            const meta = CONNECTOR_LABELS[c];
            const resources = connectorResources[c] ?? [];
            if (resources.length === 0) return null;
            const isDomestic = c === "wecom" || c === "dingtalk" || c === "feishu";
            return (
              <div key={c} className="rounded-lg border border-line bg-surface p-3">
                <div className="flex items-center gap-2 mb-2">
                  <span
                    className="inline-block w-2 h-2 rounded-full"
                    style={{ background: meta?.brand ?? "#64748b" }}
                  />
                  <span className="text-[13px] font-semibold">{meta?.name ?? c}</span>
                  <span className="text-[10.5px] text-faint font-mono">{c}</span>
                  {isDomestic && (
                    <span className="text-[10px] px-1.5 py-px rounded-full bg-emerald-700 text-emerald-50">
                      {t("Domestic")}
                    </span>
                  )}
                  <button
                    className="ml-auto btn-secondary text-[11.5px] py-1 px-2"
                    disabled={busy === c}
                    onClick={() => saveConnector(c)}
                  >
                    {busy === c ? t("Saving…") : t("Save")}
                  </button>
                </div>
                <div className="space-y-1.5">
                  {resources.map((res) => {
                    const lvl = grantedLevel(c, res);
                    return (
                      <div key={res} className="flex items-center gap-3 text-[12px]">
                        <span className="font-mono text-muted w-32 shrink-0 truncate" title={res}>
                          {res}
                        </span>
                        <div className="flex gap-1">
                          {LEVELS.map((level, i) => {
                            const active = lvl >= i;
                            const isExact = lvl === i;
                            return (
                              <button
                                key={level}
                                onClick={() => toggleLevel(c, res, level)}
                                className={
                                  "px-2 py-0.5 rounded-full text-[11px] border transition " +
                                  (isExact
                                    ? "bg-accentSoft text-accent border-accent"
                                    : active
                                      ? "bg-surface-2 text-ink border-line"
                                      : "text-faint border-line hover:text-ink")
                                }
                                title={level === "read" ? t("Read — minimal access") : level === "write" ? t("Write — explicit grant required") : t("Admin — highest privilege")}
                              >
                                {level}
                              </button>
                            );
                          })}
                        </div>
                        <span className="text-[10.5px] text-faint">
                          {lvl < 0 ? t("no access (will escalate)") : lvl === 0 ? t("read (default)") : lvl === 1 ? t("write granted") : t("admin granted")}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// -- P1-5 权限审计热力图 ----------------------------------------------------

function levelLabel(level: number): string {
  if (level <= 1) return "read";
  if (level === 2) return "write";
  return "admin";
}

function levelColor(level: number, escalations: number): string {
  if (escalations > 0) return "bg-rose-500/30 text-rose-200 border-rose-500/50";
  if (level >= 3) return "bg-amber-500/25 text-amber-100 border-amber-500/40";
  if (level === 2) return "bg-sky-500/20 text-sky-100 border-sky-500/40";
  return "bg-emerald-600/15 text-emerald-100 border-emerald-600/30";
}

function PermissionsHeatmapPanel() {
  const t = useT();
  const [data, setData] = useState<{ matrix: PermissionHeatmapCell[]; total_tools_tracked: number } | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getPermissionsHeatmap()
      .then((r) => { if (alive) setData(r); })
      .catch((e) => { if (alive) setErr(String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const personaRows = useMemo(() => {
    if (!data) return [];
    const map = new Map<string, PermissionHeatmapCell[]>();
    for (const cell of data.matrix) {
      const list = map.get(cell.persona) ?? [];
      list.push(cell);
      map.set(cell.persona, list);
    }
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [data]);

  const totalEscalations = useMemo(
    () => data?.matrix.reduce((n, c) => n + (c.scope_escalations ?? 0), 0) ?? 0,
    [data],
  );

  return (
    <div className="mt-4 rounded-xl2 border border-line bg-panel p-4">
      <div className="flex items-center gap-3 mb-3 flex-wrap">
        <div className="text-[14px] font-semibold">{t("Permission Audit Heatmap")}</div>
        <span className="text-[11.5px] text-faint">
          {t("persona × connector × tool — call counts and scope escalations")}
        </span>
        {data && (
          <div className="ml-auto flex items-center gap-3 text-[11.5px]">
            <span className="text-faint">
              {t("Tracked")}: <span className="tabular-nums text-ink">{data.total_tools_tracked}</span>
            </span>
            <span className={totalEscalations > 0 ? "text-rose-300" : "text-emerald-300"}>
              {t("Escalations")}: <span className="tabular-nums">{totalEscalations}</span>
            </span>
          </div>
        )}
      </div>

      {loading ? (
        <div className="text-[13px] text-faint">{t("Loading…")}</div>
      ) : err ? (
        <div className="text-[12.5px] text-faint">
          {t("No audit data yet — heatmap populates as Personas invoke connector tools.")}
        </div>
      ) : !data || data.matrix.length === 0 ? (
        <div className="text-[12.5px] text-faint">
          {t("No audit data yet — heatmap populates as Personas invoke connector tools.")}
        </div>
      ) : (
        <div className="space-y-3 overflow-x-auto">
          {personaRows.map(([persona, cells]) => (
            <div key={persona}>
              <div className="text-[12px] font-semibold mb-1.5 text-ink">{persona}</div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-1.5">
                {cells.map((cell) => {
                  const toolName = cell.tool.split("__").pop() ?? cell.tool;
                  const meta = CONNECTOR_LABELS[cell.connector];
                  return (
                    <div
                      key={`${persona}-${cell.tool}`}
                      className={
                        "rounded-lg border px-2.5 py-1.5 text-[11.5px] " +
                        levelColor(cell.max_scope_level, cell.scope_escalations)
                      }
                      title={cell.tool}
                    >
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono truncate flex-1">{toolName}</span>
                        <span className="text-[10px] uppercase opacity-70">{levelLabel(cell.max_scope_level)}</span>
                      </div>
                      <div className="flex items-center gap-2 mt-0.5 text-[10.5px] opacity-80">
                        <span className="truncate" title={meta?.name ?? cell.connector}>
                          {meta?.name ?? cell.connector}
                        </span>
                        <span className="ml-auto tabular-nums">×{cell.call_count}</span>
                        {cell.scope_escalations > 0 && (
                          <span className="tabular-nums">↑{cell.scope_escalations}</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
