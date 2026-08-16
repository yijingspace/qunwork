import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SwarmView } from "./SwarmView";

// Mock the API layer — the lessons block is driven by listSwarmLessons /
// deleteSwarmLesson; the rest of the panel is out of scope for this test.
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getHealth: vi.fn().mockResolvedValue({ default_workspace: "/ws" }),
    getOrchestrateHistory: vi.fn().mockResolvedValue({ runs: [] }),
    listSwarmTemplates: vi.fn().mockResolvedValue({ templates: [] }),
    listSwarmLessons: vi.fn().mockResolvedValue({
      lessons: [
        {
          id: 1,
          kind: "lesson",
          title: "[成功] 报告任务策略",
          body: "先调研再起草再评审",
          source_run_id: "run_1",
          use_count: 3,
          version: 2,
        },
        {
          id: 2,
          kind: "skill_hint",
          title: "[可技能化] 修复任务流程",
          body: "修复类任务重复出现 2 次",
          source_run_id: "run_2",
          use_count: 1,
          version: 1,
        },
        {
          id: 3,
          kind: "task_template",
          title: "[模板] 市场报告任务拆解",
          body: "t0 调研 / t1 起草 / t2 评审",
          source_run_id: "run_3",
          use_count: 0,
          version: 1,
        },
      ],
    }),
    deleteSwarmLesson: vi.fn().mockResolvedValue({ ok: true }),
  };
});

afterEach(cleanup);

describe("SwarmView — 蜂群经验 (Refine 机制学习成果)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const renderView = () => render(<SwarmView onBack={() => {}} />);

  it("renders the lessons block with distilled experience", async () => {
    renderView();
    // block rendered (data-testid) — i18n text may be key names in test env
    const block = await screen.findByTestId("swarm-lessons");
    expect(block).toBeTruthy();
    // three distilled lessons (titles are data, not i18n keys)
    expect(screen.getByText("[成功] 报告任务策略")).toBeTruthy();
    expect(screen.getByText("[可技能化] 修复任务流程")).toBeTruthy();
    expect(screen.getByText("[模板] 市场报告任务拆解")).toBeTruthy();
    // bodies
    expect(screen.getByText("先调研再起草再评审")).toBeTruthy();
    // reuse count + version shown (i18n suffix may be key name in test env)
    expect(block.textContent).toContain("3");
    expect(block.textContent).toContain("v2");
  });

  it("filters lessons by kind via the dropdown", async () => {
    const { listSwarmLessons } = await import("../api");
    renderView();
    await screen.findByTestId("swarm-lessons");
    const select = screen.getByLabelText("Filter lessons");
    fireEvent.change(select, { target: { value: "skill_hint" } });
    await waitFor(() => {
      // kind first, limit second, workspace (undefined when none) third
      expect(listSwarmLessons).toHaveBeenLastCalledWith("skill_hint", 50, undefined);
    });
  });

  it("deletes a lesson via the ✕ button", async () => {
    const { deleteSwarmLesson } = await import("../api");
    renderView();
    await screen.findByTestId("swarm-lessons");
    const deleteButtons = screen.getAllByLabelText("Delete lesson");
    fireEvent.click(deleteButtons[0]);
    await waitFor(() => {
      expect(deleteSwarmLesson).toHaveBeenCalledWith(1, undefined);
    });
    // optimistic removal
    await waitFor(() => {
      expect(screen.queryByText("[成功] 报告任务策略")).toBeNull();
    });
  });

  it("shows empty-state hint when no lessons exist", async () => {
    const { listSwarmLessons } = await import("../api");
    (listSwarmLessons as ReturnType<typeof vi.fn>).mockResolvedValue({ lessons: [] });
    renderView();
    // block still renders with (0) count and a hint, even with no lessons
    await screen.findByTestId("swarm-lessons");
    expect(screen.getByText("(0)")).toBeTruthy();
    expect(
      screen.getByText(/No swarm lessons yet/),
    ).toBeTruthy();
  });
});
