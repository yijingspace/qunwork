import { useEffect, useState } from "react";
import { useT } from "../i18n";
import { getPermissions, type PermissionMatrix } from "../api";

const ROLES = ["Chairman", "Board", "General Manager", "Scheduler", "Worker", "Auditor", "Critic"];
const CAPABILITIES = [
  "Read shared memory",
  "Write shared memory",
  "Dispatch commands",
  "Fund approval",
  "Create group",
  "Human escalation",
];

/**
 * Permissions page (team swarm section): role × capability matrix + amount thresholds.
 * Phase 0: read-only display. Phase 2 will add editing + threshold configuration.
 */
export function PermissionsView() {
  const t = useT();
  const [matrix, setMatrix] = useState<PermissionMatrix | null>(null);

  useEffect(() => {
    let alive = true;
    getPermissions()
      .then((m) => { if (alive) setMatrix(m); })
      .catch(() => { if (alive) setMatrix(null); });
    return () => { alive = false; };
  }, []);

  return (
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
        <table className="w-full text-[12.5px] min-w-[600px]">
          <thead>
            <tr className="text-faint text-left border-b border-line">
              <th className="py-1.5 font-medium">{t("Capability")}</th>
              {ROLES.map((r) => (
                <th key={r} className="py-1.5 font-medium text-center">{t(r)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {CAPABILITIES.map((cap) => (
              <tr key={cap} className="border-b border-line/50">
                <td className="py-2 font-medium">{t(cap)}</td>
                {ROLES.map((r) => {
                  const cell = matrix?.roles?.[r]?.[cap];
                  const allowed = cell?.allowed;
                  const scope = cell?.scope;
                  return (
                    <td key={r} className="py-2 text-center">
                      {allowed === undefined ? (
                        <span className="text-faint">—</span>
                      ) : allowed ? (
                        <span className="text-ok" title={scope}>{scope ? scope : "✅"}</span>
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
      </div>

      {/* 金额阈值 */}
      <div className="mt-4 rounded-xl2 border border-line bg-panel p-4">
        <div className="text-[14px] font-semibold mb-3">{t("Approval Thresholds")}</div>
        {matrix?.thresholds?.length ? (
          <div className="space-y-2">
            {matrix.thresholds.map((th, i) => (
              <div key={i} className="flex items-center gap-3 text-[13px]">
                <span className="text-muted">
                  {th.min_amount.toLocaleString()} — {th.max_amount ? th.max_amount.toLocaleString() : "∞"}
                </span>
                <span className="text-faint">→</span>
                <span>{t(th.approver_role)}</span>
                {th.require_human && <span className="text-accent">+ {t("Human review")}</span>}
                {th.require_board && <span className="text-accent">+ {t("Board review")}</span>}
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
  );
}
