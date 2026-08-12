import { useEffect, useState } from "react";
import { useT } from "../i18n";
import { getSyncStatus, type SyncStatus } from "../api";

/**
 * P2P sync indicator for the top bar.
 * In single-machine mode (status === "single" or API unavailable), renders nothing.
 * In team mode, shows a colored dot + last-sync time.
 */
export function SyncIndicator() {
  const t = useT();
  const [sync, setSync] = useState<SyncStatus | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = () => {
      getSyncStatus().then((s) => { if (alive) setSync(s); }).catch(() => {});
    };
    poll();
    const id = setInterval(poll, 30000); // refresh every 30s
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (!sync || sync.status === "single") return null;

  const dotColor =
    sync.status === "connected" ? "bg-ok" :
    sync.status === "connecting" ? "bg-accent animate-pulse" :
    "bg-faint";

  const label =
    sync.status === "connected" ? `${t("Connected")} · ${timeAgo(sync.last_sync)}` :
    sync.status === "connecting" ? t("Connecting…") :
    t("Offline");

  return (
    <div className="flex items-center gap-1.5 text-[11px] text-muted" title={label}>
      <span className={`w-1.5 h-1.5 rounded-full ${dotColor} shrink-0`} />
      {label}
    </div>
  );
}

function timeAgo(ts: number | null): string {
  if (!ts) return "";
  const secs = Math.max(0, Math.floor((Date.now() / 1000 - ts)));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h`;
}
