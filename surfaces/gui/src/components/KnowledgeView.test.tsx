import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import KnowledgeView from "./KnowledgeView";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = (await importOriginal()) as typeof api;
  return {
    ...actual,
    listKnowledge: vi.fn(),
    scanKnowledge: vi.fn().mockResolvedValue({ ok: true }),
  };
});

describe("KnowledgeView", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.clearAllMocks();
    (api as unknown as { listKnowledge: ReturnType<typeof vi.fn> }).listKnowledge.mockResolvedValue({
      items: [{ id: 1, kind: "file", title: "doc", updated_at: 0 }],
      total: 1,
    });
  });

  it("shows a load-error alert (not an empty state) when the engine is unreachable", async () => {
    (api as unknown as { listKnowledge: ReturnType<typeof vi.fn> }).listKnowledge.mockRejectedValue(
      new Error("refused"),
    );
    render(<KnowledgeView />);
    expect(await screen.findByTestId("knowledge-load-error")).toBeTruthy();
    expect(screen.queryByText(/No entries yet/)).toBeNull();
  });

  it("renders entries when the engine responds", async () => {
    render(<KnowledgeView />);
    await waitFor(() => expect(screen.getByText("doc")).toBeTruthy());
    expect(screen.queryByTestId("knowledge-load-error")).toBeNull();
  });
});
