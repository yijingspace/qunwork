import { useState, useCallback } from "react";
import { localSearch } from "../api";
import { useT } from "../i18n";

interface SearchResult {
  title: string;
  url: string;
  snippet: string;
  source: string;
  score: number;
}

export function LocalSearch() {
  const t = useT();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cached, setCached] = useState(false);

  const handleSearch = useCallback(async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await localSearch(query.trim(), 10);
      if (res.ok) {
        setResults(res.results || []);
        setCached(!!res.cached);
      } else {
        setError(res.error || t("Search failed"));
      }
    } catch {
      setError(t("Network error"));
    } finally {
      setLoading(false);
    }
  }, [query]);

  return (
    <div className="local-search">
      <div className="flex gap-2 mb-4">
        <input
          className="input flex-1"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder={t("Search the web…")}
          disabled={loading}
        />
        <button className="btn primary" onClick={handleSearch} disabled={loading || !query.trim()}>
          {loading ? t("Searching…") : t("Search")}
        </button>
      </div>
      {error && <div className="text-[12px] text-danger mb-3">{error}</div>}
      {cached && results.length > 0 && (
        <div className="text-[11px] text-faint mb-2">{t("Results from cache")}</div>
      )}
      {results.map((r, i) => (
        <div key={i} className="mb-3 p-3 rounded-lg border border-line bg-surface">
          <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-[13px] font-medium text-accent hover:underline">
            {r.title}
          </a>
          <p className="text-[12px] text-muted mt-1 leading-snug">{r.snippet}</p>
          <div className="flex gap-3 text-[11px] text-faint mt-1">
            <span>{r.source}</span>
            <span>{t("Relevance")}: {(r.score * 100).toFixed(0)}%</span>
          </div>
        </div>
      ))}
      {results.length === 0 && !loading && query && (
        <div className="text-center text-[13px] text-muted py-8">{t("No results found")}</div>
      )}
    </div>
  );
}
