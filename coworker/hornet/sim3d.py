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
