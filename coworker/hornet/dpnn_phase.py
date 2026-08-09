"""HORNET × DPNN — phase memory from Pisano periods.

Each hive cell gets a natural frequency derived from its semantic content:
  content → n-gram fingerprint → modulus m → Pisano period π(m) → ω = 2π/π(m)

A query wave has its own frequency. When the cell's frequency matches the
query's, the wave sustains amplitude across hops (constructive interference).
When they differ, the interference oscillates and averages to zero — only
frequency-matched cells resonate. This is the DPNN "periodic phase memory"
idea applied to HORNET's hive topology: the cell doesn't just decay with
distance, it *responds* to the probe wave based on their phase relationship.

The modulus mapping is bounded (2..97 primes) so periods stay small (π(97)=196)
and the computation is instant (pisano_period is lru_cached).
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

from ..periodic.fpa_table import pisano_period

_PRIMES = [
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71,
    73, 79, 83, 89, 97,
]


def _content_modulus(text: str) -> int:
    """Map text to a prime modulus via SHA-256 fingerprint.

    The hash gives a uniform distribution over the prime table; using primes
    ensures the Pisano periods are diverse (π(p) varies irregularly with p)."""
    if not text:
        return 10  # π(10) = 60, the orchestrator's canonical period
    h = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
    return _PRIMES[h % len(_PRIMES)]


def cell_period(title: str, content: str = "") -> int:
    """Natural period of a hive cell: π(m) where m is derived from the cell's
    semantic fingerprint. This is the cell's oscillation period — it resonates
    with query waves of the same frequency."""
    text = f"{title} {content}"[:600]
    m = _content_modulus(text)
    return pisano_period(m)


def query_frequency(title: str) -> float:
    """Natural frequency of a query probe wave: ω = 2π/π(m)."""
    m = _content_modulus(title)
    period = pisano_period(m)
    return 2.0 * math.pi / period


def interference(
    cell_omega: float,
    query_omega: float,
    hop: int,
    phase_dist: float,
) -> float:
    """Wave interference factor at hop t.

    Phase matching dominates: cos(π·d_phi) ∈ [-1, 1] — constructive when the
    12-dim phase vectors align, destructive when they oppose.

    Frequency matching is only a light modulation (0.1·cos(Δω·t)). The cell
    modulus comes from a SHA-256 hash over a 25-prime table, so exact frequency
    match is a 1/25 collision — letting it dominate would multiply amplitudes by
    random noise and mask the real similarity ranking (review fix).
    """
    phase_factor = math.cos(math.pi * min(phase_dist, 1.0))
    freq_factor = math.cos((cell_omega - query_omega) * hop)
    return phase_factor + 0.1 * freq_factor


def cell_omega(title: str, content: str = "") -> float:
    """Natural angular frequency of a cell: ω = 2π/π(m)."""
    period = cell_period(title, content)
    return 2.0 * math.pi / period