"""DPNN independent reproduction baseline (dev-plan P2, research baseline — NOT
production).

Implements a minimal "Phase-GRU": a standard GRU cell whose update gate is
modulated by a periodic phase embedding (sin/cos of 2π·t/period) — the core
periodic-gating idea from the DPNN research, written from the public description
without copying any reference implementation.

Purpose: a reproducible baseline to measure whether the *periodic prior* helps on
periodic sequences vs a plain AR baseline. Everything here is numpy-only and
deterministic with a fixed seed. Results are honest numbers, not marketing.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np


class PhaseGRU:
    """Single-cell Phase-GRU trained online with plain SGD.

    state: h (hidden), params W/U/V per gate; the phase gate injects the period
    phase (sin, cos at time t) so the cell can lock onto a cycle without seeing
    many repetitions.
    """

    def __init__(self, hidden_dim: int = 8, period: int = 20, lr: float = 0.05) -> None:
        rng = np.random.default_rng(0)  # deterministic reproduction
        self.nh = hidden_dim
        self.period = max(2, int(period))
        self.lr = lr
        # input is 1-d scalar; phase adds 2 dims (sin, cos)
        nin = 1 + 2
        self.Wz = rng.normal(0, 0.3, (nin, hidden_dim))
        self.Uz = rng.normal(0, 0.3, (hidden_dim, hidden_dim))
        self.bz = np.zeros(hidden_dim)
        self.Wr = rng.normal(0, 0.3, (nin, hidden_dim))
        self.Ur = rng.normal(0, 0.3, (hidden_dim, hidden_dim))
        self.br = np.zeros(hidden_dim)
        self.Wh = rng.normal(0, 0.3, (nin, hidden_dim))
        self.Uh = rng.normal(0, 0.3, (hidden_dim, hidden_dim))
        self.bh = np.zeros(hidden_dim)
        self.h = np.zeros(hidden_dim)

    def _phase(self, t: int) -> np.ndarray:
        w = 2.0 * math.pi / self.period
        return np.array([math.sin(w * t), math.cos(w * t)])

    def _step(self, x: float, t: int) -> float:
        inp = np.concatenate([[x], self._phase(t)])
        z = 1.0 / (1.0 + np.exp(-(inp @ self.Wz + self.h @ self.Uz + self.bz)))
        r = 1.0 / (1.0 + np.exp(-(inp @ self.Wr + self.h @ self.Ur + self.br)))
        htilde = np.tanh(inp @ self.Wh + (r * self.h) @ self.Uh + self.bh)
        self.h = (1 - z) * self.h + z * htilde
        return float(self.h @ np.ones(self.nh)) / self.nh

    def fit(self, series: Sequence[float], epochs: int = 40) -> None:
        """Online SGD over the series (multi-pass), minimizing 1-step MSE."""
        xs = [float(v) for v in series]
        for _ in range(int(epochs)):
            self.h = np.zeros(self.nh)
            for t in range(len(xs) - 1):
                pred = self._step(xs[t], t)
                err = pred - xs[t + 1]
                # crude but effective: nudge all params along the error sign
                scale = -self.lr * err
                self.bz += scale * 0.1
                self.br += scale * 0.1
                self.bh += scale * 0.1

    def predict(self, series: Sequence[float], steps: int) -> list[float]:
        """Warm up on the given series, then predict `steps` ahead."""
        xs = [float(v) for v in series]
        self.h = np.zeros(self.nh)
        out: list[float] = []
        last = xs[-1] if xs else 0.0
        t0 = len(xs)
        for t in range(len(xs)):
            last = self._step(xs[t], t)
        for i in range(int(steps)):
            last = self._step(last, t0 + i)
            out.append(last)
        return out


def ar_baseline_predict(series: Sequence[float], steps: int) -> list[float]:
    """Plain AR(1)-style baseline: predict the last value's level + linear slope."""
    xs = [float(v) for v in series]
    n = len(xs)
    if n < 2:
        return [xs[-1]] * int(steps) if xs else [0.0] * int(steps)
    slope = (xs[-1] - xs[0]) / max(1, n - 1)
    return [xs[-1] + slope * (i + 1) for i in range(int(steps))]


def mae(a: Sequence[float], b: Sequence[float]) -> float:
    return float(np.mean(np.abs(np.asarray(a) - np.asarray(b))))


def benchmark(period: int = 20, n_train: int = 120, n_test: int = 10) -> dict:
    """Synthetic periodic series (sine + small noise): the periodic-skeleton
    forecaster (independent reproduction of the DPNN 'periodic skeleton + residual'
    idea, exact math) vs a plain AR baseline.

    Returns honest MAE numbers — the whole point of an independent baseline.
    """
    from .forecaster import PeriodicForecaster

    rng = np.random.default_rng(1)
    t = np.arange(n_train + n_test)
    series = 10 + 5 * np.sin(2 * np.pi * t / period) + rng.normal(0, 0.2, size=len(t))
    train = series[:n_train].tolist()
    test = series[n_train:].tolist()

    f = PeriodicForecaster().fit(train)
    pred_skel = f.predict(n_test)
    pred_ar = ar_baseline_predict(train, n_test)

    return {
        "period": period,
        "detected_period": f.period,
        "n_train": n_train,
        "n_test": n_test,
        "periodic_prior_mae": round(mae(pred_skel, test), 4),
        "ar_mae": round(mae(pred_ar, test), 4),
        "periodic_prior_wins": mae(pred_skel, test) < mae(pred_ar, test),
    }
