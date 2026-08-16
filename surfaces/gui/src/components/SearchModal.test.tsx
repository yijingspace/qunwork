import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SearchModal } from "./SearchModal";

// Mock the API: searchKnowledge returns a knowledge hit (蜂群报告等知识库产物).
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    searchKnowledge: vi.fn().mockResolvedValue({
      ok: true,
      results: [
        {
          item_id: 720,
          chunk_index: 0,
          content: "《中国人形机器人产业现状报告》已组装完毕",
          score: 1,
          kind: "swarm_report",
          title: "蜂群报告 orch_0a8",
          source_path: null,
        },
      ],
    }),
  };
});

const sessions = [
  {
    session_id: "s1",
    title: "日常聊天",
    workspace: "/ws",
    agent: "chat",
    model: "gpt-5.6-sol",
    mode: "interactive",
    messages: [] as unknown[],
    updated_at: "2026-08-16T00:00:00Z",
    pinned: false,
    archived: false,
  },
] as unknown as Parameters<typeof SearchModal>[0]["sessions"];

const noop = () => {};

afterEach(cleanup);

describe("SearchModal — 搜索广度扩展到知识库 (蜂群产物可搜到)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows knowledge library hits when typing a query", async () => {
    render(
      <SearchModal sessions={sessions} onSelect={noop} onClose={noop} />,
    );
    const input = screen.getByPlaceholderText("Search chats");
    fireEvent.change(input, { target: { value: "中国人形机器人" } });
    // 知识库命中区块出现
    expect(await screen.findByText("Knowledge library")).toBeTruthy();
    expect(screen.getByText("蜂群报告 orch_0a8")).toBeTruthy();
    const { searchKnowledge } = await import("../api");
    await waitFor(() => {
      expect(searchKnowledge).toHaveBeenCalledWith("中国人形机器人");
    });
  });

  it("clicking a knowledge hit calls onOpenKnowledge and closes", async () => {
    const opened = vi.fn();
    const closed = vi.fn();
    render(
      <SearchModal
        sessions={sessions}
        onSelect={noop}
        onClose={closed}
        onOpenKnowledge={opened}
      />,
    );
    const input = screen.getByPlaceholderText("Search chats");
    fireEvent.change(input, { target: { value: "人形机器人" } });
    const hit = await screen.findByText("蜂群报告 orch_0a8");
    fireEvent.click(hit);
    expect(opened).toHaveBeenCalledTimes(1);
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it("no knowledge hits when query too short", async () => {
    render(
      <SearchModal sessions={sessions} onSelect={noop} onClose={noop} />,
    );
    const input = screen.getByPlaceholderText("Search chats");
    fireEvent.change(input, { target: { value: "中" } }); // 单字不触发
    await act(async () => {
      await new Promise((r) => setTimeout(r, 200));
    });
    const { searchKnowledge } = await import("../api");
    expect(searchKnowledge).not.toHaveBeenCalled();
  });
});
