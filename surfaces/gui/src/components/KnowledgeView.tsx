import { useCallback, useEffect, useState } from "react";
import { HornetHive } from "./HornetHive";import {
  addKnowledge,
  deleteKnowledge,
  importKnowledgeFolder,
  listKnowledge,
  pickFolderViaServer,
  scanKnowledge,
  searchKnowledge,
  type KnowledgeHit,
  type KnowledgeItem,
} from "../api";
import { useT } from "../i18n";

interface KnowledgeViewProps {
  onResume?: (payload: { title: string; content: string; source?: string; id?: number }) => void;
}

export default function KnowledgeView({ onResume }: KnowledgeViewProps) {
  const t = useT();
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [total, setTotal] = useState(0);
  const [hits, setHits] = useState<KnowledgeHit[]>([]);
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState(false);
  const [notice, setNotice] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [adding, setAdding] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [importingFolder, setImportingFolder] = useState(false);
  const [view, setView] = useState<"list" | "hive">("list");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [detail, setDetail] = useState<{ title: string; content: string; source_path?: string | null } | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  // 排序需求: 更新时间倒序(默认)/正序 / 名称 A→Z / 名称 Z→A。
  const [sortBy, setSortBy] = useState<"updated_desc" | "updated_asc" | "title_asc" | "title_desc">(
    "updated_desc",
  );

  const handleExpand = async (id: number) => {
    if (expanded === id) {
      setExpanded(null);
      setDetail(null);
      return;
    }
    setExpanded(id);
    setDetailBusy(true);
    try {
      const { authedFetch } = await import("../api");
      const res = await authedFetch(`${(globalThis as any).__COWORKER_HTTP__ || "http://127.0.0.1:8765"}/v1/knowledge/${id}/detail`);
      const data = await res.json();
      if (data.ok) {
        setDetail({
          title: data.item.title,
          content: data.item.content || "",
          source_path: data.item.source_path,
        });
      }
    } catch {
      setDetail(null);
    } finally {
      setDetailBusy(false);
    }
  };

  const handleResumeById = async (id: number) => {
    // One-click research pack: full body + source + resonance context, so the
    // agent never gets an empty knowledge pack or has to hunt for the file.
    try {
      const { knowledgeResumePack } = await import("../api");
      const res = await knowledgeResumePack(id);
      if (res.ok && res.pack) {
        onResume?.({
          title: res.pack.title,
          content: res.pack.content,
          source: res.pack.source ?? undefined,
          id,
        });
      }
    } catch {
      /* fall through */
    }
  };

  const handleReveal = async (path: string) => {
    const { revealKnowledgeSource } = await import("../api");
    await revealKnowledgeSource(path);
  };

  const flash = (msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(""), 4000);
  };

  const [loadError, setLoadError] = useState(false);

  const sortItems = useCallback(
    (list: KnowledgeItem[]): KnowledgeItem[] => {
      const arr = [...list];
      switch (sortBy) {
        case "updated_asc":
          return arr.sort((a, b) => (a.updated_at ?? 0) - (b.updated_at ?? 0));
        case "title_asc":
          return arr.sort((a, b) =>
            (a.title || "").localeCompare(b.title || "", "zh-Hans-CN"),
          );
        case "title_desc":
          return arr.sort((a, b) =>
            (b.title || "").localeCompare(a.title || "", "zh-Hans-CN"),
          );
        default:
          return arr.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
      }
    },
    [sortBy],
  );

  const refresh = useCallback(async () => {
    try {
      const data = await listKnowledge();
      setItems(sortItems(data.items ?? []));
      setTotal(data.total ?? 0);
      setLoadError(false);
    } catch {
      setItems([]);
      setTotal(0);
      setLoadError(true);
    }
  }, [sortItems]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const loadMore = async () => {
    const data = await listKnowledge(100, items.length);
    setItems((prev) => sortItems([...prev, ...(data.items ?? [])]));
  };

  const handleScan = async () => {
    setScanning(true);
    try {
      const res = await scanKnowledge();
      if (res.ok) {
        let msg = `${t("Scan complete")}: +${res.added ?? 0} ${t("added")}, ${res.skipped ?? 0} ${t("skipped")}, ${res.failed ?? 0} ${t("failed")}`;
        const ws = (res as any).workspaces_scanned;
        if (typeof ws === "number" && ws > 1) msg += ` (${ws} ${t("workspaces")})`;
        flash(msg);
        // 优化: 展示具体失败原因 (如无文本层的 PDF)
        const failures = (res as any).failures as { path?: string; reason?: string }[] | undefined;
        if (failures && failures.length > 0) {
          const reasons = new Map<string, number>();
          for (const f of failures) {
            const r = f.reason || "unknown";
            reasons.set(r, (reasons.get(r) ?? 0) + 1);
          }
          const top = [...reasons.entries()].slice(0, 3);
          const detail = top.map(([r, c]) => `${r} (${c})`).join("; ");
          setNotice(`${t("Scan issues")}: ${detail}`);
          window.setTimeout(() => setNotice(""), 8000);
        }
      } else flash(t("Scan failed") + (res.error ? `: ${res.error}` : ""));
      refresh();
    } finally {
      setScanning(false);
    }
  };

  const handleAdd = async () => {
    if (!title.trim() || !content.trim()) return;
    setAdding(true);
    try {
      const res = await addKnowledge(title.trim(), content);
      if (res.ok) {
        flash(t("Added"));
        setTitle("");
        setContent("");
        refresh();
      } else flash(t("Add failed") + (res.error ? `: ${res.error}` : ""));
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (id: number) => {
    const res = await deleteKnowledge(id);
    if (res.ok) flash(t("Deleted"));
    refresh();
  };

  const handleImportFolder = async () => {
    try {
      const path = await pickFolderViaServer();
      if (!path) {
        flash(t("No folder selected"));
        return;
      }
      setImportingFolder(true);
      const res = await importKnowledgeFolder(path);
      if (res.ok) {
        let msg = `${t("Folder imported")}: +${res.added ?? 0} ${t("files")}, ${res.skipped ?? 0} ${t("skipped")}, ${res.failed ?? 0} ${t("failed")}`;
        if (res.truncated) msg += ` — ${t("stopped early (size/file cap)")}`;
        const failures = (res as any).failures as { path?: string; reason?: string }[] | undefined;
        if (failures?.length) {
          const reasons = new Map<string, number>();
          for (const f of failures) {
            const r = (f.reason || "unknown").split(":")[0].slice(0, 60);
            reasons.set(r, (reasons.get(r) ?? 0) + 1);
          }
          const top = [...reasons.entries()]
            .sort((a, b) => b[1] - a[1])
            .slice(0, 3)
            .map(([r, n]) => `${n}x ${r}`)
            .join(", ");
          if (top) msg += ` — ${t("Failures")}: ${top}`;
        }
        flash(msg);
      } else flash(t("Folder import failed") + (res.error ? `: ${res.error}` : ""));
      refresh();
    } catch {
      flash(t("Folder import failed"));
    } finally {
      setImportingFolder(false);
    }
  };

  const handleSearch = async () => {
    if (!query.trim()) return;
    const res = await searchKnowledge(query.trim());
    setHits(res.results ?? []);
    setSearched(true);
  };

  return (
    <div className="h-full flex flex-col px-5 py-4 overflow-y-auto">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-[15px] font-semibold">{t("Knowledge library")}</h1>
          <p className="text-[12px] text-muted mt-0.5">
            {t("Workspace docs indexed automatically + manual entries — searchable by the swarm and chat")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            className={"btn-secondary " + (view === "hive" ? "ring-1 ring-accent text-accent" : "")}
            onClick={() => setView(view === "hive" ? "list" : "hive")}
            data-testid="hornet-toggle"
          >
            🐝 {t("HORNET hive")}
          </button>
          <button className="btn-secondary" disabled={importingFolder} onClick={handleImportFolder}>
            {importingFolder ? t("Importing…") : t("Import folder")}
          </button>
          <button className="btn-secondary" disabled={scanning} onClick={handleScan}>
            {scanning ? t("Scanning…") : t("Scan workspace")}
          </button>
        </div>
      </div>

      {notice && <div className="mb-3 px-3 py-2 rounded-lg bg-surface border border-line text-[12.5px]">{notice}</div>}

      {view === "hive" ? (
        <HornetHive onResume={onResume} />
      ) : (
        <>
      {/* search */}
      <div className="mb-4">
        <div className="flex gap-2">
          <input
            className="input flex-1"
            placeholder={t("Search knowledge base…")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          />
          <button className="btn-primary" onClick={handleSearch}>
            {t("Search")}
          </button>
        </div>
        {searched && (
          <div className="mt-2 flex flex-col gap-2">
            {hits.length === 0 ? (
              <div className="text-[12.5px] text-muted">{t("No matches")}</div>
            ) : (
              hits.map((h, i) => (
                <div key={i} className="rounded-lg border border-line bg-surface p-3">
                  <div className="flex items-center gap-2 text-[12px]">
                    <span className="font-semibold">{h.title}</span>
                    <span className="text-faint">
                      {h.kind === "file" ? h.source_path : t("Manual entry")}
                    </span>
                    <span className="text-faint ml-auto tabular-nums">{Math.round(h.score * 100)}%</span>
                  </div>
                  <p className="text-[12.5px] text-muted mt-1 leading-snug line-clamp-3">{h.content}</p>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      {/* manual add */}
      <div className="mb-4 rounded-xl border border-line bg-surface p-3.5">
        <div className="text-[13px] font-semibold mb-2">{t("Add knowledge entry")}</div>
        <input
          className="input mb-2"
          placeholder={t("Title")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <textarea
          className="input mb-2 h-[70px] resize-none"
          placeholder={t("Content (chunked and vectorized for semantic search)")}
          value={content}
          onChange={(e) => setContent(e.target.value)}
        />
        <button className="btn-primary" disabled={adding || !title.trim() || !content.trim()} onClick={handleAdd}>
          {adding ? t("Adding…") : t("Add")}
        </button>
      </div>

      {/* items */}
      <div className="flex items-center gap-2 mb-2">
        <div className="text-[12.5px] text-muted">
          {t("Entries")} ({items.length}/{total})
        </div>
        {/* 排序需求: 更新时间 / 名称 */}
        <select
          className="ml-auto input !py-1 !text-[11.5px] w-auto"
          value={sortBy}
          onChange={(e) => {
            setSortBy(e.target.value as typeof sortBy);
            setItems((prev) => sortItems(prev));
          }}
          data-testid="knowledge-sort"
        >
          <option value="updated_desc">{t("Newest first")}</option>
          <option value="updated_asc">{t("Oldest first")}</option>
          <option value="title_asc">{t("Title A→Z")}</option>
          <option value="title_desc">{t("Title Z→A")}</option>
        </select>
      </div>
      {items.length > 0 && items.length < total && (
        <button className="btn-secondary mb-2 self-start" onClick={loadMore}>
          {t("Load more")} ({total - items.length})
        </button>
      )}
      {loadError ? (
        <div
          className="rounded-lg border border-warnInk/30 bg-warnSoft/60 px-3 py-2 text-[12.5px] text-warnInk flex items-center gap-2"
          role="alert"
          data-testid="knowledge-load-error"
        >
          <span>⚠</span>
          <span className="flex-1">
            {t("Couldn't reach the local engine — this may be connection trouble, not missing data.")}
          </span>
          <button className="btn-secondary text-[11.5px]" onClick={() => refresh()}>
            {t("Retry")}
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="text-[12.5px] text-muted">{t("No entries yet — scan the workspace above, or add manually.")}</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {items.map((item) => (
            <div key={item.id}>
              <div className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2">
                <span className="text-[11px] px-1.5 py-0.5 rounded bg-surface-2 text-faint shrink-0">
                  {item.kind === "file" ? "📄" : "✍️"}
                </span>
                <button
                  className="min-w-0 flex-1 text-left"
                  onClick={() => handleExpand(item.id)}
                  title={t("Click to view content")}
                  data-testid={`knowledge-item-${item.id}`}
                >
                  <div className="text-[13px] font-medium truncate hover:text-accent">{item.title}</div>
                  <div className="text-[11px] text-faint truncate">
                    {item.kind === "file" ? item.source_path : t("Manual entry")}
                  </div>
                </button>
                <button
                  className="text-[11.5px] text-accent hover:underline shrink-0"
                  onClick={() => {
                    if (!item.title) return;
                    handleResumeById(item.id);
                  }}
                  data-testid={`knowledge-resume-${item.id}`}
                >
                  🧠 {t("Research")}
                </button>
                <button
                  className="text-[11.5px] text-muted hover:text-red-500 shrink-0"
                  onClick={() => {
                    if (window.confirm(`${t("Delete entry")} “${item.title}”?`)) handleDelete(item.id);
                  }}
                >
                  {t("Delete")}
                </button>
              </div>
              {expanded === item.id && (
                <div className="mt-1 rounded-lg border border-line bg-surface-2 px-3 py-2 text-[12px]" data-testid={`knowledge-detail-${item.id}`}>
                  {detailBusy ? (
                    <span className="text-muted">{t("Loading…")}</span>
                  ) : detail ? (
                    <>
                      <div className="whitespace-pre-wrap max-h-48 overflow-y-auto hairline-scroll text-ink">
                        {detail.content || t("No content")}
                      </div>
                      {detail.source_path && (
                        <div className="mt-1.5 flex items-center gap-2 text-[11px] text-faint">
                          <span>🔗</span>
                          {/^https?:\/\//i.test(detail.source_path) ? (
                            <a href={detail.source_path} target="_blank" rel="noreferrer" className="text-accent hover:underline break-all">
                              {detail.source_path}
                            </a>
                          ) : (
                            <>
                              <span className="break-all">{detail.source_path}</span>
                              <button
                                className="ml-2 text-accent hover:underline shrink-0"
                                onClick={() => handleReveal(detail.source_path as string)}
                                title={t("Open in file explorer")}
                              >
                                📂 {t("Open")}
                              </button>
                            </>
                          )}
                        </div>
                      )}
                      <div className="mt-1.5">
                        <button
                          className="btn-primary text-[11px]"
                          onClick={() => handleResumeById(item.id)}
                          data-testid={`knowledge-resume-detail-${item.id}`}
                        >
                          🧠 {t("Continue research / creation")}
                        </button>
                      </div>
                    </>
                  ) : (
                    <span className="text-muted">{t("Couldn't load detail")}</span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
        </>
      )}
    </div>
  );
}
