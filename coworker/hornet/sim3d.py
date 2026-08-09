"""HORNET 3D — simulation module (研究原型, not production retrieval).

Replicates the spec's 3D Kelvin-cell wave simulation: cells on a 3D lattice with
12 geometric channels (XY plane / Z+ projection / Z- traceback), each channel a
directional decay; a probe wave injected at a seed cell diffuses across the
lattice and we report per-zone energy + a dominant-period FFT of the wave over
time. Pure numpy, deterministic seed — a teaching/research baseline like
coworker/periodic/dpnn_baseline.py. Run:
    python -m coworker.hornet.sim3d --cells 64 --steps 40
"""
from __future__ import annotations

import argparse
import math

import numpy as np

# 12 channel directions (unit vectors) — Kelvin-cell abstraction:
# XY plane (G0..G3), Z+ projection (G4..G7), Z- traceback (G8..G11).
CHANNEL_DIRS = [
    (1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0),      # G0..G3 XY 平面
    (1, 1, 1), (-1, 1, 1), (1, -1, 1), (-1, -1, 1),    # G4..G7 Z+ 推演
    (1, 1, -1), (-1, 1, -1), (1, -1, -1), (-1, -1, -1),# G8..G11 Z- 溯源
]
ZONE_DECAY = {"xy": 0.72, "z+": 0.55, "z-": 0.90}  # Z+ diffuses, Z- converges


def channel_decay(channel: int) -> float:
    zone = "z+" if channel >= 4 and channel <= 7 else "z-" if channel >= 8 else "xy"
    return ZONE_DECAY[zone]


def run_simulation(cells: int = 64, steps: int = 40, seed: int = 0, freq: float = 0.20) -> dict:
    rng = np.random.default_rng(seed)
    size = int(round(cells ** (1 / 3)))
    size = max(2, size)
    # 3D lattice of oscillators with per-cell natural frequency (topological phase)
    grid = np.zeros((size, size, size), dtype=np.float64)
    freq_grid = np.full((size, size, size), freq, dtype=np.float64)
    freq_grid += rng.normal(0, 0.02, size=(size, size, size))
    # adjacency: each cell has 12 neighbors (channel directions)
    neighbors: dict[tuple[int, int, int], list[tuple[tuple[int, int, int], float]]] = {}
    for x in range(size):
        for y in range(size):
            for z in range(size):
                nbs = []
                for ch, (dx, dy, dz) in enumerate(CHANNEL_DIRS):
                    nx, ny, nz = (x + dx) % size, (y + dy) % size, (z + dz) % size
                    nbs.append(((nx, ny, nz), channel_decay(ch)))
                neighbors[(x, y, z)] = nbs

    # seed a probe wave at the origin cell
    grid[0, 0, 0] = 1.0
    history: list[float] = []
    energy_by_zone = {"xy": 0.0, "z+": 0.0, "z-": 0.0}
    for _t in range(steps):
        new = grid.copy()
        for (x, y, z), nbs in neighbors.items():
            # wave update: coupled oscillators, damping per channel zone
            acc = 0.0
            for (nx, ny, nz), decay in nbs:
                acc += decay * (grid[nx, ny, nz] - grid[x, y, z])
            new[x, y, z] += 0.06 * acc
            new[x, y, z] *= math.cos(2 * math.pi * freq_grid[x, y, z] / steps)
        grid = new * 0.96  # global damping keeps the wave from exploding
        history.append(float(np.abs(grid).sum()))

    # per-zone energy: channels carry energy preferentially along their zone
    for (x, y, z), nbs in neighbors.items():
        v = grid[x, y, z]
        for (nx, ny, nz), decay in nbs:
            dz = nz - z
            if dz > 0:
                energy_by_zone["z+"] += decay * abs(v)
            elif dz < 0:
                energy_by_zone["z-"] += decay * abs(v)
            else:
                energy_by_zone["xy"] += decay * abs(v)

    # FFT: dominant period of the total wave energy over time
    fft = np.abs(np.fft.rfft(np.asarray(history) - np.mean(history)))
    freqs = np.fft.rfftfreq(len(history), d=1.0)
    dom = int(np.argmax(fft[1:])) + 1 if len(fft) > 1 else 0
    dominant_period = 1.0 / freqs[dom] if dom < len(freqs) and freqs[dom] > 0 else 0.0
    return {
        "lattice": (size, size, size),
        "steps": steps,
        "channels": 12,
        "zone_decay": ZONE_DECAY,
        "energy_by_zone": {k: round(v, 4) for k, v in energy_by_zone.items()},
        "dominant_period": round(float(dominant_period), 2),
        "wave_history_tail": [round(h, 4) for h in history[-6:]],
    }


def run_hybrid_sim(
    cells: int = 64,
    steps: int = 40,
    seed: int = 0,
    freq: float = 0.20,
    sync_threshold: float = 0.12,
) -> dict:
    """Hybrid-ODE-Sim (spec §Neural-ODE 轻量化): the bulk of the lattice keeps
    the cheap discrete iteration; only local 3×3×3 sub-blocks whose sync rate
    exceeds the threshold upgrade to a continuous RK4 ODE step (fine wave /
    phase interference where resonance is actually emerging). Returns the share
    of cells that ran continuous vs discrete per step."""
    rng = np.random.default_rng(seed)
    size = max(2, int(round(cells ** (1 / 3))))
    grid = np.zeros((size, size, size), dtype=np.float64)
    freq_grid = np.full((size, size, size), freq, dtype=np.float64)
    freq_grid += rng.normal(0, 0.02, size=(size, size, size))
    grid[0, 0, 0] = 1.0
    continuous_cells_total = 0
    discrete_cells_total = 0
    wave_history: list[float] = []

    def alpha_for(x: int, y: int, z: int) -> float:
        # zone decay: Z+ diffuses (small α), Z- converges (large α)
        mid = size // 2
        return 0.9 if z < mid - 1 else 0.55 if z > mid + 1 else 0.72

    def rk4_step(block: np.ndarray, fgrid: np.ndarray, h: float = 0.25) -> np.ndarray:
        """One RK4 step of dA/dt = -α(z)A + β·laplacian(A) on a small sub-block."""
        def rhs(A: np.ndarray) -> np.ndarray:
            lap = np.zeros_like(A)
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                lap += np.roll(np.roll(np.roll(A, dx, 0), dy, 1), dz, 2)
            lap -= 6 * A
            return -A * alpha_for(0, 0, 0) + 0.06 * lap

        k1 = rhs(block)
        k2 = rhs(block + h / 2 * k1)
        k3 = rhs(block + h / 2 * k2)
        k4 = rhs(block + h * k3)
        return block + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)

    for _t in range(steps):
        new = grid.copy()
        # 1) detect hot sub-blocks: local sync rate = -std of neighbor amplitudes
        hot: set[tuple[int, int, int]] = set()
        for x in range(size):
            for y in range(size):
                for z in range(size):
                    nbs = [
                        grid[(x + dx) % size, (y + dy) % size, (z + dz) % size]
                        for dx, dy, dz in ((1, 0, 0), (0, 1, 0), (0, 0, 1), (-1, 0, 0), (0, -1, 0), (0, 0, -1))
                    ]
                    if float(np.std(nbs)) < sync_threshold and abs(grid[x, y, z]) > 1e-3:
                        hot.add((x, y, z))
        # 2) continuous RK4 on 3×3×3 blocks around hot cells; discrete elsewhere
        updated_continuous: set[tuple[int, int, int]] = set()
        for (x, y, z) in hot:
            sub = grid[max(0, x - 1):x + 2, max(0, y - 1):y + 2, max(0, z - 1):z + 2]
            if sub.shape != (3, 3, 3):
                continue
            fg = freq_grid[max(0, x - 1):x + 2, max(0, y - 1):y + 2, max(0, z - 1):z + 2]
            new[max(0, x - 1):x + 2, max(0, y - 1):y + 2, max(0, z - 1):z + 2] = rk4_step(sub, fg)
            for i in range(-1, 2):
                for j in range(-1, 2):
                    for k in range(-1, 2):
                        updated_continuous.add((max(0, x - 1) + i, max(0, y - 1) + j, max(0, z - 1) + k))
        # 3) discrete update for the rest
        for x in range(size):
            for y in range(size):
                for z in range(size):
                    if (x, y, z) in updated_continuous:
                        continue
                    acc = 0.0
                    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                        acc += alpha_for(x, y, z) * (grid[(x + dx) % size, (y + dy) % size, (z + dz) % size] - grid[x, y, z])
                    new[x, y, z] += 0.06 * acc
        grid = new * 0.96
        continuous_cells_total += len(updated_continuous)
        discrete_cells_total += size ** 3 - len(updated_continuous)
        wave_history.append(float(np.abs(grid).sum()))

    total = continuous_cells_total + discrete_cells_total
    return {
        "lattice": (size, size, size),
        "steps": steps,
        "sync_threshold": sync_threshold,
        "continuous_share": round(continuous_cells_total / max(1, total), 4),
        "discrete_share": round(discrete_cells_total / max(1, total), 4),
        "hot_blocks_seen": continuous_cells_total,
        "wave_history_tail": [round(h, 4) for h in wave_history[-4:]],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="HORNET 3D Kelvin-cell wave simulation")
    ap.add_argument("--cells", type=int, default=64)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--freq", type=float, default=0.20)
    args = ap.parse_args()
    import json

    print(json.dumps(run_simulation(args.cells, args.steps, args.seed, args.freq), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
