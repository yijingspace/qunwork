"""Shared pytest fixtures.

`fake_slack` boots the in-process FakeSlack harness on an ephemeral port and points the Slack
adapter at it via `SLACK_API_URL`, so the real `SlackAdapter` / `slack_bolt` stack runs
end-to-end with no network, tokens, or the Slack app console. See
`coworker.testing.fake_slack` and `platform/docs/FAKE-SLACK-SPEC.md`.
"""

from __future__ import annotations

import ast
# Python 3.14+ compatibility: ast.NameConstant/Num/Str were removed (merged into ast.Constant).
# docstring_parser (used by aisuite) still references them at import time.
if not hasattr(ast, "NameConstant"):
    ast.NameConstant = ast.Constant
if not hasattr(ast, "Num"):
    ast.Num = ast.Constant
if not hasattr(ast, "Str"):
    ast.Str = ast.Constant

import pytest
import pytest_asyncio

from coworker.testing.fake_slack import FakeSlack


@pytest.fixture(scope="session", autouse=True)
def _utf8_logging():
    """Windows console logging uses GBK by default; an emoji in a log message
    (e.g. the 🔔 framing of a Slack alert) makes logging's StreamHandler raise
    UnicodeEncodeError, which can kill a background turn mid-test (ui_refresh_e2e
    has ~60% flake on Windows for exactly this, independent of any code change).
    Rebuild the root handlers with UTF-8 + errors=replace so logging can never
    break application logic."""
    import logging
    import sys

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    try:
        # errors=replace: emoji and other non-GBK chars degrade, not raise.
        handler.setStream(_Utf8Replace(sys.stderr))
    except Exception:
        pass
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    yield


class _Utf8Replace:
    """Minimal stderr wrapper that encodes with errors='replace'."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        try:
            self._stream.write(s)
        except UnicodeEncodeError:
            enc = getattr(self._stream, "encoding", None) or "utf-8"
            self._stream.write(s.encode(enc, errors="replace").decode(enc, errors="replace"))

    def flush(self):
        self._stream.flush()


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """EVERY test gets an isolated SecretStore/state dir. Without this, any test that builds
    a SessionManager reads the developer's real machine-global state — including their cloud
    sign-in, which made test session creation emit REAL telemetry to prod (found 2026-07-03
    as burst noise in the ocw-connect-telemetry-events table)."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "coworker-state"))
    monkeypatch.delenv("COWORKER_API_TOKEN", raising=False)


@pytest_asyncio.fixture
async def fake_slack(monkeypatch):
    """A running FakeSlack control object; `SLACK_API_URL` is set to it for the test's duration."""
    fake = FakeSlack()
    await fake.start()
    monkeypatch.setenv("SLACK_API_URL", fake.api_url)
    try:
        yield fake
    finally:
        await fake.stop()
