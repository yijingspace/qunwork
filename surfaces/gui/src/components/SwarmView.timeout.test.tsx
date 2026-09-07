import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SwarmView } from "./SwarmView";

// 只需要渲染能力, 不发真实请求 — 面板初始态依赖这些端点。
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getHealth: vi.fn().mockResolvedValue({ default_workspace: "/ws" }),
    getOrchestrateHistory: vi.fn().mockResolvedValue({ runs: [] }),
    listSwarmTemplates: vi.fn().mockResolvedValue({ templates: [] }),
    listSwarmLessons: vi.fn().mockResolvedValue({ lessons: [] }),
  };
});

afterEach(cleanup);

describe("SwarmView — 不限制超时开关", () => {
  // 用户 2026-09-07: 复杂任务不该被超时腰斩。勾选"不限制超时"后, 数值输入框应禁用
  // (置灰), 取消勾选恢复可编辑。真正的"0 → 两层超时全 None"由后端
  // _parse_swarm_timeout 测试锁定。
  it("disables the timeout field while 'No timeout' is checked", async () => {
    render(<SwarmView onBack={() => {}} />);
    const checkbox = await screen.findByTestId("swarm-no-timeout");
    const timeoutInput = screen.getByLabelText(/Timeout/);

    expect(checkbox).toHaveProperty("checked", false);
    expect(timeoutInput).toHaveProperty("disabled", false);

    fireEvent.click(checkbox);
    expect(checkbox).toHaveProperty("checked", true);
    expect(timeoutInput).toHaveProperty("disabled", true);

    fireEvent.click(checkbox);
    expect(checkbox).toHaveProperty("checked", false);
    expect(timeoutInput).toHaveProperty("disabled", false);
  });
});
