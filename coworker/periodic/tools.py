"""Periodic / Pisano-Fibonacci tools registered into engines so the swarm's
reviewer (and any worker) can catch arithmetic & recurrence hallucinations with
an exact lookup instead of trusting the model (dev-plan T1)."""

from __future__ import annotations

from typing import Any


def periodic_tools() -> list[Any]:
    """Returns ai.tool-wrapped functions (registry format, like text_stats_tool)."""

    def fpa_verify(sequence: str, modulus: int = 10, predict: int = 0) -> dict[str, Any]:
        """Verify a numeric sequence against the Fibonacci-Pisano recurrence
        a_n = (a_{n-1} + a_{n-2}) mod m, and optionally predict the next terms.

        Use this BEFORE a deliverable claims a Fibonacci / Pisano / modulo pattern:
        it is an exact arithmetic check (never a model guess).

        Args:
          sequence: comma-separated integers, e.g. "1,1,2,3,5,8,13"
          modulus: m for the mod-m recurrence (default 10, Pisano period 60)
          predict: how many following terms to compute (0 = none)
        """
        from .fpa_table import predict_next, verify_fpa_sequence

        try:
            nums = [int(x.strip()) for x in sequence.split(",") if x.strip()]
        except ValueError as exc:
            return {"ok": False, "error": f"sequence must be comma-separated integers: {exc}"}
        if not nums:
            return {"ok": False, "error": "empty sequence"}
        ver = verify_fpa_sequence(nums, modulus)
        out: dict[str, Any] = {"ok": ver["ok"], "modulus": ver["modulus"]}
        if ver.get("pisano") is not None:
            out["pisano_period"] = ver["pisano"]  # π(m): period of 1,1,0,1,1,0… mod m
        if ver.get("seed") is not None:
            out["seed"] = list(ver["seed"])
        if ver.get("period") is not None:
            out["fpa_period"] = ver["period"]  # period of THIS seed's state machine
        if not ver["ok"]:
            out["first_violation_index"] = ver["first_bad"]
            out["note"] = (
                f"sequence breaks the recurrence at index {ver['first_bad']}: "
                f"expected (prev+prevprev) mod {ver['modulus']}"
            )
            return out
        if predict > 0:
            try:
                out["next"] = predict_next(nums, modulus, count=int(predict))
            except ValueError as exc:
                out["error"] = str(exc)
        return out

    def pisano_lookup(modulus: int, n: int = -1) -> dict[str, Any]:
        """Exact Pisano-period facts for a modulus.

        Args:
          modulus: positive integer m
          n: optional index — if >= 0, return F_n mod m (fast doubling)
        """
        from .fpa_table import fib_mod, pisano_period

        m = int(modulus)
        if m <= 0:
            return {"ok": False, "error": "modulus must be positive"}
        out: dict[str, Any] = {"ok": True, "modulus": m, "pisano_period": pisano_period(m)}
        if n >= 0:
            out["fib_n_mod_m"] = fib_mod(int(n), m)
        return out

    def forecast_series(values: str, steps: int = 5) -> dict[str, Any]:
        """Extend a comma-separated numeric time series using the periodic
        skeleton + residual-branch forecaster (T6): detects the dominant period,
        builds the per-phase skeleton, and predicts ahead. Use to sanity-check or
        extend periodic series (schedules, metrics, daily/weekly patterns).

        Args:
          values: comma-separated numbers, e.g. "10,12,10,13,11,14,12"
          steps: how many values to predict ahead
        """
        from .forecaster import forecast_series as _fs

        try:
            nums = [float(x.strip()) for x in values.split(",") if x.strip()]
        except ValueError as exc:
            return {"ok": False, "error": f"values must be comma-separated numbers: {exc}"}
        if len(nums) < 4:
            return {"ok": False, "error": "need at least 4 values"}
        return _fs(nums, steps=int(steps))

    import aisuite as ai

    return [
        ai.tool(
            fpa_verify,
            metadata=ai.ToolMetadata(
                category="periodic", risk_level="low", capabilities=["periodic"]
            ),
        ),
        ai.tool(
            pisano_lookup,
            metadata=ai.ToolMetadata(
                category="periodic", risk_level="low", capabilities=["periodic"]
            ),
        ),
        ai.tool(
            forecast_series,
            metadata=ai.ToolMetadata(
                category="periodic", risk_level="low", capabilities=["periodic"]
            ),
        ),
    ]
