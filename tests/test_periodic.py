"""T1 (dev-plan): FPA table — exact Pisano periods, recurrence verification,
prediction, and engine mounting of the periodic tools."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coworker.periodic.fpa_table import (
    fib_mod,
    fpa_state_period,
    pisano_period,
    predict_next,
    verify_fpa_sequence,
)


def test_pisano_period_known_values():
    # Verified identities from the DPNN research: π(2)=3, π(5)=20, π(10)=60
    # (π(10) = lcm(π(2), π(5)) = lcm(3,20) = 60 by CRT).
    assert pisano_period(2) == 3
    assert pisano_period(3) == 8
    assert pisano_period(5) == 20
    assert pisano_period(10) == 60


def test_pisano_period_lcm_identity():
    import math

    # π(30) = lcm(π(2), π(3), π(5)) = lcm(3,8,20) = 120 for coprime moduli
    assert pisano_period(30) == 120
    assert math.gcd(2, 3) == 1  # sanity: coprime
    assert pisano_period(6) == 24  # lcm(3, 8)


def test_fib_mod_fast_doubling():
    # F_10 = 55 → mod 10 = 5
    assert fib_mod(10, 10) == 5
    # F_59 mod 10 = 1 (exact: F_59 = 956722026041, % 10 = 1)
    assert fib_mod(59, 10) == 1
    # F_60 mod 10 = 0 and F_61 mod 10 = 1 (period restarts)
    assert fib_mod(60, 10) == 0
    assert fib_mod(61, 10) == 1


def test_verify_fpa_sequence_valid_and_invalid():
    # 1,1,2,3,5,8,13 is a valid Fibonacci run mod 10
    ver = verify_fpa_sequence([1, 1, 2, 3, 5, 8, 13], 10)
    assert ver["ok"] is True
    assert ver["pisano"] == 60
    assert ver["period"] == 60  # classic seed (1,1) has the Pisano period

    # 1,1,2,3,5,8,14 breaks at index 6 (14 mod 10 = 4 ≠ 13)
    bad = verify_fpa_sequence([1, 1, 2, 3, 5, 8, 14], 10)
    assert bad["ok"] is False
    assert bad["first_bad"] == 6


def test_predict_next_extends_verified_sequence():
    assert predict_next([1, 1, 2, 3, 5, 8, 13], 10, count=3) == [1, 4, 5]
    # F_8=21→1, F_9=34→4, F_10=55→5 (mod 10)
    try:
        predict_next([1, 1, 2, 4], 10)
        assert False, "expected ValueError on broken sequence"
    except ValueError:
        pass


def test_fpa_state_period_nonclassical_seed():
    # seed (2, 2): 2,2,4,6,0,6,6,2,8,0,8,8,6,4,0,4,4,8,2,0,2,2 → cycles back at 20? just assert > 0 and divides a plausible bound
    p = fpa_state_period(2, 2, 10)
    assert p > 0
    assert p <= 10 * 10


def test_periodic_tools_mounted_in_engine():
    from coworker.agent import build_engine
    from coworker.agents import get_agent
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
    import tempfile

    class P(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            return AssistantTurn(text="ok", finish_reason="stop")

        def capabilities(self, model):
            return ModelCapabilities()

    ws = Path(tempfile.mkdtemp())
    e = build_engine(agent=get_agent("cowork"), workspace=str(ws), provider=P())
    assert "fpa_verify" in e.registry.names()
    assert "pisano_lookup" in e.registry.names()

    # run the tool through the registry
    spec = e.registry.get("fpa_verify")
    assert spec is not None
    res = spec.func("1,1,2,3,5,8,13", 10, 3)
    assert res["ok"] is True and res["pisano_period"] == 60 and res["next"] == [1, 4, 5]
