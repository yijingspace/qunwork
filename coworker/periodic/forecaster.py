"""Periodic primitive forecaster (dev-plan T6) — the "periodic skeleton + residual
branch" idea from the DPNN periodic-topology research, implemented with exact,
reproducible math (no trained model, no unverified claims):

1. detrend the series (linear least squares);
2. find the dominant period via autocorrelation peaks in 2..N//2;
3. build the periodic skeleton (mean value per phase slot);
4. the residual (detrended − skeleton) is extrapolated with an EMA of its tail;

prediction = trend + skeleton[phase] + residual-tail.

This is used by workers to sanity-check / extend periodic series (schedules,
metrics, daily/weekly patterns) without hallucinating the pattern.
"""

from __future__ import annotations

import statistics
from typing import Optional, Sequence


def detrend(values: list[float]) -> tuple[list[float], float, float]:
    """Remove the best-fit linear trend; returns (residuals, slope, intercept)."""
    n = len(values)
    if n < 2:
        return list(values), 0.0, (values[0] if values else 0.0)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    resid = [y - (slope * x + intercept) for x, y in zip(xs, values)]
    return resid, slope, intercept


def dominant_period(values: list[float], max_period: Optional[int] = None) -> int:
    """Dominant period via autocorrelation: the lag (2..N//2) maximizing the
    mean-subtracted correlation. Returns 0 if no reliable period (N too short
    or flat)."""
    n = len(values)
    if n < 4:
        return 0
    hi = min(n // 2, max_period or n // 2)
    if hi < 2:
        return 0
    mean = sum(values) / n
    centered = [v - mean for v in values]
    var = sum(c * c for c in centered) or 1e-12
    best_lag, best_score = 0, -1.0
    for lag in range(2, hi + 1):
        a = centered[: n - lag]
        b = centered[lag:]
        num = sum(x * y for x, y in zip(a, b))
        score = num / (var + 1e-12)
        if score > best_score:
            best_lag, best_score = lag, score
    return best_lag if best_score > 0.15 else 0


class PeriodicForecaster:
    """Trend + periodic-skeleton + EMA-residual forecaster (T6)."""

    def __init__(self, min_period: int = 2, max_period: Optional[int] = None) -> None:
        self.min_period = min_period
        self.max_period = max_period
        self.slope = 0.0
        self.intercept = 0.0
        self.period = 0
        self.skeleton: list[float] = []
        self.residual_tail: list[float] = []
        self._fitted = False

    def fit(self, values: Sequence[float]) -> "PeriodicForecaster":
        vals = [float(v) for v in values]
        if len(vals) < 4:
            self._fitted = False
            return self
        self._n = len(vals)
        resid, slope, intercept = detrend(vals)
        self.slope, self.intercept = slope, intercept
        self.period = dominant_period(resid, self.max_period)
        if self.period >= self.min_period:
            # skeleton: mean residual per phase slot
            slots: list[list[float]] = [[] for _ in range(self.period)]
            for i, r in enumerate(resid):
                slots[i % self.period].append(r)
            self.skeleton = [
                sum(s) / len(s) if s else 0.0 for s in slots
            ]
            self.residual_tail = resid[-self.period * 2 :]
        else:
            self.period = 0
            self.skeleton = []
            self.residual_tail = resid[-8:]
        self._fitted = True
        return self

    @property
    def fitted(self) -> bool:
        return self._fitted

    def predict(self, steps: int) -> list[float]:
        if not self._fitted:
            return []
        out: list[float] = []
        n0 = getattr(self, "_n", len(self.residual_tail))
        # residual branch: the skeleton already absorbed the periodic pattern, so
        # the residual is noise — continue it toward its own mean (≈0), not the
        # last point, to avoid compounding a single sample's noise.
        tail = list(self.residual_tail) or [0.0]
        tail_mean = sum(tail) / len(tail)
        ema = tail_mean
        alpha = 0.5
        for i in range(int(steps)):
            t = n0 + i
            trend = self.slope * t + self.intercept
            skel = 0.0
            if self.period and self.skeleton:
                skel = self.skeleton[(n0 + i) % self.period]
            ema = alpha * tail_mean + (1 - alpha) * ema
            out.append(trend + skel + ema)
        return out

    def explain(self) -> dict:
        """Machine-readable fit summary (for worker tool output)."""
        return {
            "fitted": self._fitted,
            "period": self.period,
            "skeleton_phases": len(self.skeleton),
            "slope": round(self.slope, 6),
            "intercept": round(self.intercept, 6),
            "residual_tail_len": len(self.residual_tail),
        }


def forecast_series(
    values: Sequence[float], steps: int = 5, max_period: Optional[int] = None
) -> dict:
    """One-call API: fit + predict, returns the summary dict (tool-friendly).
    Accepts a sequence of numbers or a comma-separated string."""
    if isinstance(values, str):
        try:
            values = [float(x.strip()) for x in values.split(",") if x.strip()]
        except ValueError as exc:
            return {"ok": False, "error": f"values must be comma-separated numbers: {exc}"}
    values = list(values)
    if len(values) < 4:
        return {"ok": False, "error": "need at least 4 values"}
    f = PeriodicForecaster(max_period=max_period).fit(values)
    if not f.fitted:
        return {"ok": False, "error": "need at least 4 values"}
    return {
        "ok": True,
        **f.explain(),
        "prediction": [round(v, 6) for v in f.predict(steps)],
        "steps": int(steps),
    }
