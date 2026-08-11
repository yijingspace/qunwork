import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { UsageTab } from "./UsageTab";
import * as api from "../api";
import { LanguageProvider } from "../i18n";

vi.mock("../api", async (importOriginal) => {
  const actual = (await importOriginal()) as typeof api;
  return {
    ...actual,
    getUsage: vi.fn(),
    getCacheWarmStatus: vi.fn().mockResolvedValue({
      enabled: true,
      min_hit_rate: 0.3,
      max_items: 5,
      interval_hours: 6,
      last_warm_at: 0,
      week: { prompt_tokens: 420, cached_tokens: 0, calls: 4 },
      org_hit_rate: 0.7667,
    }),
    setCacheWarmEnabled: vi.fn().mockResolvedValue({ ok: true, enabled: true }),
    triggerCacheWarm: vi.fn().mockResolvedValue({ ok: true, warmed: 5, prompt_tokens: 420 }),
  };
});

function mockUsage() {
  const g = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
  g.getUsage.mockResolvedValue({
    totals: {
      prompt_tokens: 300,
      completion_tokens: 50,
      total_tokens: 350,
      cached_tokens: 230,
      cache_hit_rate: 0.7667,
      turns: 2,
    },
    by_day: [
      { day: "2026-08-07", prompt_tokens: 100, completion_tokens: 20, cached_tokens: 80, cache_hit_rate: 0.8 },
      { day: "2026-08-08", prompt_tokens: 200, completion_tokens: 30, cached_tokens: 150, cache_hit_rate: 0.75 },
    ],
    by_session: [
      { session_id: "sess-abc-123", model: "deepseek:deepseek-v4-flash", turns: 2, prompt_tokens: 300, completion_tokens: 50, cached_tokens: 230, cache_hit_rate: 0.7667 },
    ],
  });
}

describe("UsageTab", () => {
  beforeEach(() => {
    mockUsage();
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders totals and the cache-hit rate", async () => {
    render(
      <LanguageProvider>
        <UsageTab />
      </LanguageProvider>
    );
    expect((await screen.findAllByText("76.7%")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("350").length).toBeGreaterThan(0); // total tokens 350
    expect(screen.getAllByText(/300/).length).toBeGreaterThan(0); // prompt in
  });

  it("renders per-day bars and per-session rows", async () => {
    render(
      <LanguageProvider>
        <UsageTab />
      </LanguageProvider>
    );
    expect(await screen.findByText(/sess-abc-123/)).toBeTruthy();
    expect(screen.getByText("08-07")).toBeTruthy();
    expect(screen.getByText("08-08")).toBeTruthy();
  });

  it("shows the cache warm-up card with this-week stats", async () => {
    render(
      <LanguageProvider>
        <UsageTab />
      </LanguageProvider>
    );
    expect(await screen.findByTestId("cache-warm-card")).toBeTruthy();
    expect(screen.getByText("Cache warm-up")).toBeTruthy();
    expect(screen.getByText(/420/)).toBeTruthy(); // this-week injected tokens
    expect(screen.getByRole("switch")).toBeTruthy(); // the warm-up toggle
  });
});
