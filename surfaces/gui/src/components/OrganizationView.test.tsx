import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { OrganizationView } from "./OrganizationView";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = (await importOriginal()) as typeof api;
  return {
    ...actual,
    listKnowledge: vi.fn().mockResolvedValue({ items: [], total: 3 }),
    listSkills: vi.fn().mockResolvedValue({ skills: [{ id: "a" }, { id: "b" }] }),
    listSwarmTemplates: vi.fn().mockResolvedValue({ templates: [{ id: 1 }] }),
    listMemories: vi.fn().mockResolvedValue({ memory: [{ id: 1 }] }),
    getOrchestrateHistory: vi.fn().mockResolvedValue({ runs: [{ run_id: "r1" }] }),
    rhythmForecast: vi.fn().mockResolvedValue({
      rhythm: "weekly",
      period_days: 7,
      upcoming: [
        { id: "t1", title: "Weekly report", cron: "0 9 * * 1", next_run: 1800000000 },
      ],
    }),
    rhythmRecommendations: vi.fn().mockResolvedValue({
      period_days: 7,
      rhythm: "weekly",
      recommendations: [
        {
          task_id: "t1",
          title: "Weekly report",
          priority: "low",
          cron: "0 9 * * 1",
          current_hour: 9,
          recommended_hour: 3,
          valley_share: 0.9,
          runs: 12,
          reason: "12 runs — fewest at 03:00 (share 10%)",
        },
      ],
    }),
  };
});

describe("OrganizationView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api as unknown as { listKnowledge: ReturnType<typeof vi.fn> }).listKnowledge = vi
      .fn()
      .mockResolvedValue({ items: [], total: 3 });
  });

  it("shows the org asset overview tiles and section cards", async () => {
    render(<OrganizationView />);
    expect(await screen.findByText("Organization")).toBeTruthy();
    // Stat tiles render once the batch settles
    expect(await screen.findByTestId("org-stat-knowledge")).toBeTruthy();
    expect(screen.getByTestId("org-stat-skills")).toBeTruthy();
    expect(screen.getByTestId("org-stat-templates")).toBeTruthy();
    expect(screen.getByTestId("org-stat-team-memory")).toBeTruthy();
    expect(screen.getByTestId("org-stat-swarm-runs")).toBeTruthy();
  });

  it("shows per-automation best trigger times (rhythm × automation)", async () => {
    render(<OrganizationView />);
    // RhythmCard surfaces the learned valley hour for the automation
    expect(await screen.findByTestId("rhythm-recommendations")).toBeTruthy();
    expect(screen.getAllByText("Weekly report").length).toBeGreaterThan(0);
    // current 09:00 → recommended 03:00
    expect(screen.getAllByText(/09:00 → 03:00/).length).toBeGreaterThan(0);
  });
});
