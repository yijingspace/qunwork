import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ModelChecklist } from "./ModelChecklist";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = (await importOriginal()) as typeof api;
  return {
    ...actual,
    addModel: vi.fn(),
    removeModel: vi.fn(),
    setDefaultModel: vi.fn(),
    getSettings: vi.fn(),
    listProviderModels: vi.fn(),
  };
});

const listProviderModels = api.listProviderModels as ReturnType<typeof vi.fn>;
const addModel = api.addModel as ReturnType<typeof vi.fn>;

// owner-hit 2026-09-13: curated 矩阵只有一两个模型, 接好一家后要能看到这家**实际提供**的模型。
function renderChecklist(over: Partial<Parameters<typeof ModelChecklist>[0]> = {}) {
  return render(
    <ModelChecklist
      provider="zai"
      knownProviders={["zai", "openai"]}
      suggested={["glm-5.2"]}
      curated={["zai:glm-5.2"]}
      defaultModel="zai:glm-5.2"
      onChanged={() => {}}
      {...over}
    />,
  );
}

describe("ModelChecklist live model list", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    addModel.mockResolvedValue({ ok: true, models: ["zai:glm-5.2"], model: "zai:glm-5.2" });
  });
  afterEach(cleanup);

  it("auto-fetches the provider's live list on mount and shows its count", async () => {
    listProviderModels.mockResolvedValue({
      ok: true,
      models: ["glm-4.6", "glm-4.5"],
      count: 2,
    });
    renderChecklist();
    expect(listProviderModels).toHaveBeenCalledWith("zai");
    expect(await screen.findByTestId("model-live-count")).toBeTruthy();
    expect(screen.getByTestId("model-live-count").textContent).toContain("2");
    // 实时区出现 curated 之外的模型(去重: glm-5.2 不重复列出)
    expect(screen.getByText("glm-4.6")).toBeTruthy();
    expect(screen.getByText("glm-4.5")).toBeTruthy();
    expect(screen.queryAllByText("glm-5.2").length).toBe(1);
  });

  it("ticking a live model adds it WITH the provider prefix", async () => {
    listProviderModels.mockResolvedValue({ ok: true, models: ["glm-4.6"], count: 1 });
    renderChecklist();
    const row = await screen.findByText("glm-4.6");
    fireEvent.click(row.closest("label")!.querySelector("input")!);
    await waitFor(() => expect(addModel).toHaveBeenCalledWith("zai:glm-4.6"));
  });

  it("degrades to a hint (manual add stays) when the gateway has no model list", async () => {
    listProviderModels.mockResolvedValue({
      ok: false,
      error: "This service does not expose a model list — add models by hand below.",
    });
    renderChecklist();
    expect(await screen.findByTestId("model-live-error")).toBeTruthy();
    // 手动添加行仍在, 且带前缀提交
    fireEvent.change(screen.getByPlaceholderText(/Add another model/), {
      target: { value: "glm-x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(addModel).toHaveBeenCalledWith("zai:glm-x"));
  });

  it("offers a filter once the live list gets long, and filtering narrows rows", async () => {
    listProviderModels.mockResolvedValue({
      ok: true,
      models: Array.from({ length: 20 }, (_, i) => `glm-${i}`),
      count: 20,
    });
    renderChecklist();
    expect(await screen.findByTestId("model-live-filter")).toBeTruthy();
    expect(screen.getByText("glm-19")).toBeTruthy();
    expect(screen.getByText("glm-1")).toBeTruthy();
    fireEvent.change(screen.getByTestId("model-live-filter"), { target: { value: "glm-19" } });
    expect(screen.getByText("glm-19")).toBeTruthy();
    expect(screen.queryByText("glm-1")).toBeNull();
  });

  it("says so when every served model is already listed", async () => {
    listProviderModels.mockResolvedValue({ ok: true, models: ["glm-5.2"], count: 1 });
    renderChecklist();
    await waitFor(() =>
      expect(screen.getByText(/already listed above/)).toBeTruthy(),
    );
  });
});
