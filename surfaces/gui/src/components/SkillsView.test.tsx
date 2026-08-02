import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import SkillsView from "./SkillsView";

// Hermetic fetch stub routing by URL substring + method (mirrors PersonaView.test.tsx).
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

const SKILL = {
  name: "pdf-tools",
  description: "Extract text from PDFs",
  version: "1.2.0",
  category: "document",
  author: "qunwork",
  tags: ["pdf", "extract"],
  updated_at: null,
  install_count: 3,
  rating: 4.5,
  rating_count: 2,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SkillsView", () => {
  it("shows the empty state when the catalog has no skills", async () => {
    stubFetch([{ match: "/v1/skills", method: "GET", json: { skills: [] } }]);
    render(<SkillsView />);
    expect(await screen.findByText("No skills yet")).toBeTruthy();
  });

  it("renders skills with metadata and marketplace stats", async () => {
    stubFetch([{ match: "/v1/skills", method: "GET", json: { skills: [SKILL] } }]);
    render(<SkillsView />);
    expect(await screen.findByText("pdf-tools")).toBeTruthy();
    expect(screen.getByText("Extract text from PDFs")).toBeTruthy();
    expect(screen.getByText(/v1\.2\.0/)).toBeTruthy();
    expect(screen.getByText(/document/)).toBeTruthy();
    expect(screen.getByText("Installs 3")).toBeTruthy();
    expect(screen.getByText("★ 4.5 (2)")).toBeTruthy();
    expect(screen.getByText("pdf")).toBeTruthy();
  });

  it("POSTs a rating when a star is clicked", async () => {
    const calls = stubFetch([
      { match: "/v1/skills", method: "GET", json: { skills: [SKILL] } },
      {
        match: "/v1/skills/pdf-tools/rate",
        method: "POST",
        json: { ok: true, rating: 5, rating_count: 3 },
      },
    ]);
    render(<SkillsView />);
    await screen.findByText("pdf-tools");
    fireEvent.click(screen.getByTitle("Rate 5"));
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/rate"));
      expect(post).toBeTruthy();
      expect(post!.body).toMatchObject({ score: 5 });
    });
  });

  it("exports a skill as a zip download", async () => {
    const createURL = vi.fn(() => "blob:fake");
    const clickSpy = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL: createURL, revokeObjectURL: vi.fn() });
    // jsdom anchor clicks navigate nowhere — stub so the download path completes.
    HTMLAnchorElement.prototype.click = clickSpy as any;

    const calls = stubFetch([
      { match: "/v1/skills", method: "GET", json: { skills: [SKILL] } },
      { match: "/v1/skills/export", method: "POST", json: { ok: true, zip_base64: "UEsDBBQAAAA=" } },
    ]);
    render(<SkillsView />);
    await screen.findByText("pdf-tools");
    fireEvent.click(screen.getByText("Export"));
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/export"));
      expect(post).toBeTruthy();
      expect(post!.body).toMatchObject({ name: "pdf-tools" });
      expect(createURL).toHaveBeenCalled();
      expect(clickSpy).toHaveBeenCalled();
    });
  });

  it("imports a skill zip and refreshes the catalog", async () => {
    const calls = stubFetch([
      { match: "/v1/skills", method: "GET", json: { skills: [] } },
      { match: "/v1/skills/import", method: "POST", json: { ok: true, name: "cool-skill" } },
    ]);
    render(<SkillsView />);
    await screen.findByText("No skills yet");

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["---\nname: cool-skill\n---\nbody"], "cool-skill.zip", {
      type: "application/zip",
    });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/import"));
      expect(post).toBeTruthy();
      expect(post!.body.zip_base64).toBeTruthy();
    });
  });
});
