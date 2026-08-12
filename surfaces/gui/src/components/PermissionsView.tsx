import { useEffect, useState } from "react";
import { useT } from "../i18n";
import { getPermissions, type PermissionMatrix } from "../api";

/**
 * Permissions page: role × capability matrix + amount thresholds.
 *
 * P2 (不再是占位符):
 *   - 角色和能力动态从 matrix.roles 生成（不再依赖前端硬编码列表）
 *   - 当 matrix 为空时回退到空状态 + 文本说明
 */
export function PermissionsView() {
  const t = useT();
  const [matrix, setMatrix] = useState<PermissionMatrix | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getPermissions()
      .then((m) => { if (alive) setMatrix(m ?? null); })
      .catch(() => { if (alive) setMatrix(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  // Derive roles + capabilities dynamically from the matrix.
  const roleNames = matrix?.roles ? Object.keys(matrix.roles) : [];
  const capSet = new Set<string>();
  if (matrix?.roles) {
    for (const r of roleNames) {
      for (const c of Object.keys(matrix.roles[r])) capSet.add(c);
    }
  }
  const capabilities = Array.from(capSet);

  return (
    <div className="h-full overflow-y-auto">
    <div className="max-w-4xl mx-auto px-6 py-6">
      <h1 className="text-[22px] font-semibold tracking-tight flex items-center gap-2.5">
        <span className="text-[20px]">🔐</span>
        {t("Permissions")}
      </h1>
      <p className="text-[13px] text-muted mt-1.5 leading-relaxed">
        {t("Role-based access control for your AI swarm organization.")}
      </p>

      {/* 权限矩阵表 */}
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
                {roleNames.map((r) => (
                  <th key={r} className="py-1.5 font-medium text-center">{t(r)}</th>
                ))}
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
                    const title =
                      scope && max ? `${scope} · ≤ ¥${max.toLocaleString()}`
                        : scope ? scope
                        : max ? `≤ ¥${max.toLocaleString()}`
                        : undefined;
                    return (
                      <td key={r} className="py-2 text-center">
                        {allowed === undefined ? (
                          <span className="text-faint">—</span>
                        ) : allowed ? (
                          <span className="text-ok" title={title}>
                            {scope ? scope : "✅"}
                          </span>
                        ) : (
                          <span className="text-faint">❌</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 金额阈值 */}
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
      </div>
    </div>
  );
}
