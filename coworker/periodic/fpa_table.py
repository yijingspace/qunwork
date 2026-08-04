"""Fibonacci-Pisano Atom (FPA) table — the "deterministic lookup" core of the
DPNN periodic closed-loop research (see dev-plan T1).

Only the mathematically provable parts are implemented (per the plan's red line):
- Pisano periods π(m) and Fibonacci mod-m sequences (exact, cached).
- FPA state machines a_n = (a_{n-1} + a_{n-2}) mod m: verify a given sequence
  against the recurrence, report the state-machine period, and predict ahead.

Nothing here trains a model or makes unverified performance claims — it is a
table + recurrence checker the swarm's reviewer can use to catch arithmetic /
date / recurrence hallucinations before a deliverable is accepted.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional, Sequence


@lru_cache(maxsize=256)
def pisano_period(m: int) -> int:
    """The Pisano period π(m): the length of the Fibonacci sequence mod m.

    The mod-m Fibonacci sequence is periodic and always repeats from (0, 1);
    π(m) is the index where that happens. Verified identities used for tests:
    π(2)=3, π(3)=8, π(5)=20, π(10)=60, and π(m·n)=lcm(π(m),π(n)) when coprime.
    """
    if m <= 0:
        raise ValueError("modulus must be positive")
    if m == 1:
        return 1
    a, b = 0, 1
    for i in range(1, 6 * m * m + 2):  # π(m) ≤ 6m for all m
        a, b = b, (a + b) % m
        if a == 0 and b == 1:
            return i
    raise ValueError(f"period not found for m={m}")


def fib_mod(n: int, m: int) -> int:
    """F_n mod m via fast doubling (O(log n)), exact for huge n."""
    if n < 0:
        raise ValueError("n must be non-negative")
    m = abs(m) or 1

    def _fib(k: int):
        # returns (F_k mod m, F_{k+1} mod m)
        if k == 0:
            return 0, 1
        a, b = _fib(k >> 1)
        c = a * ((2 * b - a) % m) % m
        d = (a * a + b * b) % m
        if k & 1:
            return d, (c + d) % m
        return c, d

    return _fib(n)[0]


@lru_cache(maxsize=512)
def fpa_state_period(a0: int, a1: int, m: int) -> int:
    """Period of the FPA(a0, a1, m) state machine: the recurrence
    a_n = (a_{n-1} + a_{n-2}) mod m is ultimately periodic; the state
    (prev, cur) must revisit its seed for a pure cycle. Returns the cycle
    length, or 0 if the state is a pre-periodic tail (seed not on the cycle)."""
    if m <= 0:
        raise ValueError("modulus must be positive")
    a0, a1 = a0 % m, a1 % m
    seen: dict[tuple[int, int], int] = {(a0, a1): 0}
    p, c = a0, a1
    for i in range(1, m * m + 2):
        nxt = (p + c) % m
        p, c = c, nxt
        state = (p, c)
        if state in seen:
            # cycle length = i - first_occurrence
            return i - seen[state]
        seen[state] = i
    return 0  # unreachable for m ≥ 1, but keep it total


def verify_fpa_sequence(
    sequence: Sequence[int], modulus: int = 10
) -> dict:
    """Check `sequence` against the FPA recurrence a_n=(a_{n-1}+a_{n-2}) mod m.

    Returns a dict with:
      - ok: True if every element from index 2 on satisfies the recurrence
      - modulus, pisano: π(m) for the classic Fibonacci seed
      - seed: (a0, a1) used
      - period: the FPA state-machine period for that seed
      - first_bad: index of the first violation, if any
    """
    m = abs(modulus) or 10
    nums = [int(x) for x in sequence]
    if len(nums) < 3:
        return {
            "ok": True,
            "modulus": m,
            "pisano": pisano_period(m),
            "seed": (nums[0] % m, nums[1] % m) if nums else None,
            "period": fpa_state_period(nums[0] % m, nums[1] % m, m) if len(nums) >= 2 else None,
            "note": "sequence too short to verify (need ≥3 terms)",
        }
    first_bad = None
    for i in range(2, len(nums)):
        expect = (nums[i - 1] + nums[i - 2]) % m
        if nums[i] % m != expect:
            first_bad = i
            break
    return {
        "ok": first_bad is None,
        "modulus": m,
        "pisano": pisano_period(m),
        "seed": (nums[0] % m, nums[1] % m),
        "period": fpa_state_period(nums[0] % m, nums[1] % m, m),
        "first_bad": first_bad,
    }


def predict_next(sequence: Sequence[int], modulus: int = 10, count: int = 3) -> list[int]:
    """Extend a verified FPA sequence by `count` terms (or raise ValueError if the
    supplied terms violate the recurrence)."""
    nums = [int(x) for x in sequence]
    if len(nums) < 2:
        raise ValueError("need at least 2 terms to predict")
    m = abs(modulus) or 10
    ver = verify_fpa_sequence(nums, m)
    if not ver["ok"]:
        raise ValueError(f"sequence violates FPA recurrence at index {ver['first_bad']}")
    out: list[int] = []
    a, b = nums[-2] % m, nums[-1] % m
    for _ in range(count):
        nxt = (a + b) % m
        out.append(nxt)
        a, b = b, nxt
    return out
