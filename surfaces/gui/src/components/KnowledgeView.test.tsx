import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import KnowledgeView from "./KnowledgeView";

type Call = { url: string; method: string; body: any };

function stubFetch(routes: { match: string; method?: string; json: any }[]) {
  const calls: Call[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method || "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    for (const r of routes) {
      if (url.includes(r.match) && (!r.method || r.method === method)) {
        return { ok: true, json: async () => r.json } as Response;
      }
    }
    return { ok: true, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const ITEM = {
  id: 1,
  kind: "manual",
  source_path: null,
  title: "固态电池",
  created_at: 1,
  updated_at: 2,
};

const FILE_ITEM = {
  id: 2,
  kind: "file",
  source_path: "C:/docs/guide.md",
  title: "guide",
  created_at: 1,
  updated_at: 2,
};

const HIT = {
  item_id: 1,
  chunk_index: 0,
  content: "固态电池以固态电解质替代液态电解液。",
  score: 0.9,
  kind: "manual",
  title: "固态电池",
  source_path: null,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("KnowledgeView", () => {
  it("shows the empty state", async () => {
    stubFetch([{ match: "/v1/knowledge", method: "GET", json: { items: [] } }]);
    render(<KnowledgeView />);
    expect(
      await screen.findByText("No entries yet — scan the workspace above, or add manually."),
    ).toBeTruthy();
  });

  it("renders indexed items (manual + file) with sources", async () => {
    stubFetch([{ match: "/v1/knowledge", method: "GET", json: { items: [ITEM, FILE_ITEM] } }]);
    render(<KnowledgeView />);
    expect(await screen.findByText("固态电池")).toBeTruthy();
    expect(screen.getByText("guide")).toBeTruthy();
    expect(screen.getByText("C:/docs/guide.md")).toBeTruthy();
    expect(screen.getByText("Manual entry")).toBeTruthy();
  });

  it("searches the knowledge base and renders hits", async () => {
    const calls = stubFetch([
      { match: "/v1/knowledge", method: "GET", json: { items: [] } },
      {
        match: "/v1/knowledge/search",
        method: "GET",
        json: { ok: true, results: [HIT] },
      },
    ]);
    render(<KnowledgeView />);
    await screen.findByText(/No entries yet/);

    const box = screen.getByPlaceholderText("Search knowledge base…");
    fireEvent.change(box, { target: { value: "固态电池" } });
    fireEvent.keyDown(box, { key: "Enter", code: "Enter" });

    await waitFor(() => {
      const search = calls.find((c) => c.url.includes("/knowledge/search"));
      expect(search).toBeTruthy();
      expect(search!.url).toContain(encodeURIComponent("固态电池"));
    });
    expect(await screen.findByText("固态电池以固态电解质替代液态电解液。")).toBeTruthy();
    expect(screen.getByText("90%")).toBeTruthy();
  });

  it("adds a manual entry", async () => {
    const calls = stubFetch([
      { match: "/v1/knowledge", method: "GET", json: { items: [] } },
      { match: "/v1/knowledge", method: "POST", json: { ok: true, id: 9 } },
    ]);
    render(<KnowledgeView />);
    await screen.findByText(/No entries yet/);

    fireEvent.change(screen.getByPlaceholderText("Title"), { target: { value: "新知识" } });
    fireEvent.change(screen.getByPlaceholderText("Content (chunked and vectorized for semantic search)"), {
      target: { value: "这是一段新知识内容。" },
    });
    fireEvent.click(screen.getByText("Add"));

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/v1/knowledge"));
      expect(post).toBeTruthy();
      expect(post!.body).toMatchObject({ title: "新知识", content: "这是一段新知识内容。" });
    });
  });

  it("scans the workspace and reports added/unchanged counts", async () => {
    const calls = stubFetch([
      { match: "/v1/knowledge", method: "GET", json: { items: [] } },
      {
        match: "/v1/knowledge/scan",
        method: "POST",
        json: { ok: true, added: 2, updated: 0, skipped: 1, failed: 0 },
      },
    ]);
    render(<KnowledgeView />);
    await screen.findByText(/No entries yet/);

    fireEvent.click(screen.getByText("Scan workspace"));
    await waitFor(() => {
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/scan"))).toBe(true);
    });
    expect(await screen.findByText(/Scan complete/)).toBeTruthy();
  });
});
