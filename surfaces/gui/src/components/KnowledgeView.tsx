import { useCallback, useEffect, useState } from "react";
import {
  addKnowledge,
  deleteKnowledge,
  listKnowledge,
  scanKnowledge,
  searchKnowledge,
  type KnowledgeHit,
  type KnowledgeItem,
} from "../api";
import { useT } from "../i18n";

export default function KnowledgeView() {
  const t = useT();
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [hits, setHits] = useState<KnowledgeHit[]>([]);
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState(false);
  const [notice, setNotice] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [adding, setAdding] = useState(false);
  const [scanning, setScanning] = useState(false);

  const flash = (msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(""), 4000);
  };

  const refresh = useCallback(async () => {
    try {
      const data = await listKnowledge();
      setItems(data.items ?? []);
    } catch {
      setItems([]);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleScan = async () => {
    setScanning(true);
    try {
      const res = await scanKnowledge();
      if (res.ok)
        flash(t("Scan complete") + `: +${res.added ?? 0} ${t("added")}, ${res.skipped ?? 0} ${t("unchanged")}`);
      else flash(t("Scan failed") + (res.error ? `: ${res.error}` : ""));
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
        <button className="btn-secondary" disabled={scanning} onClick={handleScan}>
          {scanning ? t("Scanning…") : t("Scan workspace")}
        </button>
      </div>

      {notice && <div className="mb-3 px-3 py-2 rounded-lg bg-surface border border-line text-[12.5px]">{notice}</div>}

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
      <div className="text-[12.5px] text-muted mb-2">
        {t("Entries")} ({items.length})
      </div>
      {items.length === 0 ? (
        <div className="text-[12.5px] text-muted">{t("No entries yet — scan the workspace above, or add manually.")}</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {items.map((item) => (
            <div key={item.id} className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2">
              <span className="text-[11px] px-1.5 py-0.5 rounded bg-surface-2 text-faint shrink-0">
                {item.kind === "file" ? "📄" : "✍️"}
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium truncate">{item.title}</div>
                <div className="text-[11px] text-faint truncate">
                  {item.kind === "file" ? item.source_path : t("Manual entry")}
                </div>
              </div>
              <button
                className="text-[11.5px] text-muted hover:text-red-500 shrink-0"
                onClick={() => {
                  if (window.confirm(`${t("Delete entry")} “${item.title}”?`)) handleDelete(item.id);
                }}
              >
                {t("Delete")}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
