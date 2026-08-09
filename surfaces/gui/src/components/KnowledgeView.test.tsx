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
    knowledgeResumePack: vi.fn().mockResolvedValue({
      ok: true,
      pack: { title: "DPNN 白皮书", content: "核心内容", source: "E:/docs/dpnn.md", related: [] },
    }),
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

describe("KnowledgeView resume/detail", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.clearAllMocks();
    (api as unknown as { listKnowledge: ReturnType<typeof vi.fn> }).listKnowledge.mockResolvedValue({
      items: [{ id: 7, kind: "file", title: "DPNN 白皮书", source_path: "E:/docs/dpnn.md", updated_at: 0 }],
      total: 1,
    });
    (api as unknown as { knowledgeResumePack: ReturnType<typeof vi.fn> }).knowledgeResumePack = vi
      .fn()
      .mockResolvedValue({
        ok: true,
        pack: { title: "DPNN 白皮书", content: "核心内容", source: "E:/docs/dpnn.md", related: [] },
      });
  });

  it("expands a row to show detail and source, and resumes research into a session", async () => {
    const { fireEvent } = await import("@testing-library/react");
    const onResume = vi.fn();
    // mock the detail fetch (native fetch against the local sidecar)
    const originalFetch = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("/detail")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ ok: true, item: { id: 7, title: "DPNN 白皮书", content: "核心内容", source_path: "E:/docs/dpnn.md" } }),
        });
      }
      return originalFetch(input as RequestInfo, {} as RequestInit);
    }));
    const KnowledgeView = (await import("./KnowledgeView")).default;
    render(<KnowledgeView onResume={onResume} />);
    await screen.findByText("DPNN 白皮书");
    fireEvent.click(screen.getByTestId("knowledge-item-7"));
    expect(await screen.findByTestId("knowledge-detail-7")).toBeTruthy();
    expect(screen.getByText("核心内容")).toBeTruthy();
    fireEvent.click(screen.getByTestId("knowledge-resume-detail-7"));
    await waitFor(() => expect(onResume).toHaveBeenCalled());
    expect(onResume).toHaveBeenCalledWith(
      expect.objectContaining({ title: "DPNN 白皮书", content: "核心内容" }),
    );
    vi.unstubAllGlobals();
  });
});
