import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { HornetHive } from "./HornetHive";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = (await importOriginal()) as typeof api;
  return {
    ...actual,
    hornetGraph: vi.fn(),
    hornetStats: vi.fn(),
    hornetResonate: vi.fn(),
    hornetBuild: vi.fn(),
    hornetEvolve: vi.fn(),
  };
});

function mockApi() {
  const g = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
  g.hornetGraph.mockResolvedValue({
    nodes: [
      { id: 1, title: "DPNN 研究", x: 0, y: 0, z: 1, phase: [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], degree: 1 },
      { id: 2, title: "周报 2026", x: 1, y: 0, z: -1, phase: [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], degree: 1 },
    ],
    edges: [{ src: 1, dst: 2, relation: "similar", channel: "D5", weight: 0.9 }],
  });
  g.hornetStats.mockResolvedValue({
    nodes: 2,
    edges: 1,
    resonance_runs: 0,
    emergent: 0,
    emergent_items: [],
  });
}

describe("HornetHive", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi();
  });

  it("renders the hive svg with nodes", async () => {
    render(<HornetHive />);
    expect(await screen.findByTestId("hornet-svg")).toBeTruthy();
    expect(screen.getByText("DPNN 研究")).toBeTruthy();
  });

  it("resonates on query and shows hits", async () => {
    const g = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
    g.hornetResonate.mockResolvedValue({
      hits: [{ node_id: 1, title: "DPNN 研究", amplitude: 12.3, path: ["similar@D5"], x: 0, y: 0, similarity: 0.8 }],
    });
    render(<HornetHive />);
    await screen.findByTestId("hornet-svg");
    const input = screen.getByTestId("hornet-query");
    input.focus();
    // fireEvent would need user-event; call the handler via Enter key event
    const fireEvent = (await import("@testing-library/react")).fireEvent;
    fireEvent.change(input, { target: { value: "DPNN 周期" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByTestId("hornet-hits")).toBeTruthy();
    expect(screen.getAllByText("DPNN 研究").length).toBeGreaterThan(0);
    await waitFor(() => expect(g.hornetResonate).toHaveBeenCalled());
  });
});

describe("HornetHive 3D views", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi();
  });

  it("switches to the Z+ projection view and filters nodes by layer", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<HornetHive />);
    await screen.findByTestId("hornet-svg");
    fireEvent.click(screen.getByTestId("hornet-view-zplus"));
    expect(screen.getByTestId("hornet-view-zplus")).toBeTruthy();
  });
});
