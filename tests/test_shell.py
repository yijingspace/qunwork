"""P3 gate tests — persistent shell executor.

The executor drives the OS-native shell (bash on POSIX, PowerShell on Windows), so the
command strings here are parameterized per-OS. The behavior under test (cwd/env persistence,
exit codes, timeout-and-recover, truncation) is identical across both.
"""

from __future__ import annotations

import sys
import time

import pytest

from coworker.permissions import PermissionEngine
from coworker.tools import ToolRegistry
from coworker.tools.shell import LocalExecutor, shell_tools

_WIN = sys.platform == "win32"

# Per-OS command snippets exercising the same behavior in the native shell.
SET_ENV = "$env:GREETING='hello_world'" if _WIN else "export GREETING=hello_world"
ECHO_ENV = "echo $env:GREETING" if _WIN else "echo $GREETING"
EXIT_OK = "cmd /c exit 0" if _WIN else "true"
EXIT_FAIL = "cmd /c exit 1" if _WIN else "false"
SLEEP_5 = "Start-Sleep -Seconds 5" if _WIN else "sleep 5"
PRINT_1000 = (
    'foreach ($i in 1..1000) { "line$i" }'
    if _WIN
    else "for i in $(seq 1 1000); do echo line$i; done"
)


@pytest.fixture
def executor(tmp_path):
    ex = LocalExecutor(cwd=tmp_path, default_timeout=10)
    yield ex
    ex.close()


def test_cwd_persists_across_calls(executor, tmp_path):
    (tmp_path / "sub").mkdir()
    executor.run("cd sub")
    result = executor.run("pwd")
    assert result["exit_code"] == 0
    assert "sub" in result["output"]
    assert executor.cwd.endswith("sub")


def test_env_persists_across_calls(executor):
    executor.run(SET_ENV)
    result = executor.run(ECHO_ENV)
    assert "hello_world" in result["output"]


def test_exit_code_captured(executor):
    assert executor.run(EXIT_OK)["exit_code"] == 0
    assert executor.run(EXIT_FAIL)["exit_code"] == 1


def test_timeout_kills_command(executor):
    start = time.monotonic()
    result = executor.run(SLEEP_5, timeout=1)
    elapsed = time.monotonic() - start
    assert result["timed_out"] is True
    assert elapsed < 4.0  # did not block for the full sleep
    # session survives the timeout — still usable (POSIX keeps the shell; Windows respawns)
    assert executor.run("echo alive")["output"].strip().endswith("alive")


def test_large_output_truncated_keeps_tail(tmp_path):
    ex = LocalExecutor(cwd=tmp_path, max_output_chars=200, default_timeout=10)
    try:
        result = ex.run(PRINT_1000)
        assert result["truncated"] is True
        assert len(result["output"]) <= 200
        # the END survives (where test/build verdicts live), the head is dropped
        assert "line1000" in result["output"]
        assert "line1\n" not in result["output"]
    finally:
        ex.close()


def test_shell_tool_integration(executor, tmp_path):
    reg = ToolRegistry()
    reg.register_all(shell_tools(executor))
    assert {"run_shell", "shell_task_output", "shell_task_kill"} <= set(reg.names())

    spec = reg.get("run_shell")
    assert spec.metadata.requires_approval is True
    # polling/killing the agent's own background tasks doesn't need approval
    assert reg.get("shell_task_output").metadata.requires_approval is False
    assert reg.get("shell_task_kill").metadata.requires_approval is False

    eng = PermissionEngine(workspace_root=tmp_path)
    decision = eng.evaluate("run_shell", {"command": "echo hi"}, spec.metadata)
    assert not decision.allowed and decision.needs_user  # high-risk → asks

    out = reg.execute("run_shell", {"command": "echo hi"})
    assert "hi" in out["output"]


def test_run_shell_accepts_description_and_clamped_timeout(executor):
    reg = ToolRegistry()
    reg.register_all(shell_tools(executor))
    # `description` rides along for approval prompts/audit; it must not break execution.
    out = reg.execute(
        "run_shell",
        {"command": "echo ok", "description": "Say ok", "timeout_seconds": 99999},
    )
    assert out["exit_code"] == 0 and "ok" in out["output"]


# -- background tasks ------------------------------------------------------------

ECHO_THEN_SLEEP = (
    "Write-Output started; Start-Sleep -Seconds 30"
    if _WIN
    else "echo started; sleep 30"
)
QUICK_ECHO = "Write-Output quick_done" if _WIN else "echo quick_done"


def _poll_output(reg, task_id, *, until_status=None, deadline=10.0):
    """Poll shell_task_output, accumulating output until a status is reached."""
    acc = ""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        res = reg.execute("shell_task_output", {"task_id": task_id})
        acc += res["output"]
        if until_status is None or res["status"] == until_status:
            if until_status is None and not acc:
                time.sleep(0.1)
                continue
            return acc, res
        time.sleep(0.1)
    return acc, res


def test_background_task_runs_and_exits(executor):
    reg = ToolRegistry()
    reg.register_all(shell_tools(executor))
    started = reg.execute(
        "run_shell", {"command": QUICK_ECHO, "run_in_background": True}
    )
    assert started["status"] == "running" and started["task_id"]

    acc, res = _poll_output(reg, started["task_id"], until_status="exited")
    assert res["status"] == "exited"
    assert res["exit_code"] == 0
    assert "quick_done" in acc

    # output reads are incremental: a second read returns nothing new
    again = reg.execute("shell_task_output", {"task_id": started["task_id"]})
    assert again["output"] == ""


def test_background_task_kill(executor):
    reg = ToolRegistry()
    reg.register_all(shell_tools(executor))
    started = reg.execute(
        "run_shell", {"command": ECHO_THEN_SLEEP, "run_in_background": True}
    )
    acc, _ = _poll_output(reg, started["task_id"])
    assert "started" in acc  # it's alive and producing output

    killed = reg.execute("shell_task_kill", {"task_id": started["task_id"]})
    assert killed["status"] == "killed"

    res = reg.execute("shell_task_output", {"task_id": started["task_id"]})
    assert res["status"] == "exited"


def test_background_unknown_task_errors(executor):
    reg = ToolRegistry()
    reg.register_all(shell_tools(executor))
    assert (
        "unknown task"
        in reg.execute("shell_task_output", {"task_id": "bg-99"})["error"]
    )
    assert (
        "unknown task" in reg.execute("shell_task_kill", {"task_id": "bg-99"})["error"]
    )


# -- inline-escaping / script-file routing ---------------------------------------
# Windows swarm workers kept tripping the persistent REPL's inline parser on
# all-ASCII commands with nested quotes / multi-line blocks / `-f` format strings
# (2026-09-08 orchestrate run: t1/t4/t5 all fell back to writing helper scripts by
# hand). The fix routes anything "fragile for inline parsing" — not just non-ASCII —
# through a UTF-8 .ps1 file. These tests lock the predicate and the end-to-end route.

def test_fragile_inline_predicate():
    from coworker.tools.shell import _fragile_inline

    # Simple, quote-free, single-line → stay inline (faster, keep session state).
    assert not _fragile_inline("cd sub")
    assert not _fragile_inline("git status -s")
    assert _fragile_inline("$env:X='y'") is False  # single quotes only → safe
    assert not _fragile_inline("echo $PWD")
    assert not _fragile_inline("pytest -q")
    # The classes that kept breaking inline → route to a file.
    assert _fragile_inline('python -c "import os; print(1)"')  # double quote
    assert _fragile_inline('foreach ($i in 1..3) { "x$i" }')   # nested quotes
    assert _fragile_inline("Write-Output a\nWrite-Output b")    # multi-line
    assert _fragile_inline("Write-Output `tTabbed")             # backtick escape
    assert _fragile_inline("echo 中文")                          # non-ASCII (legacy)


@pytest.mark.skipif(not _WIN, reason="script-file routing is Windows-REPL-specific")
def test_fragile_command_is_routed_through_script_file(executor):
    # Multi-line + a nested double-quoted string: historically mangled when fed
    # inline to the REPL. Must run correctly AND prove the .ps1 route fired.
    cmd = "$x = 'there'\nWrite-Output \"hi $x\""
    r = executor.run(cmd)
    assert r["exit_code"] == 0
    assert ".ps1" in r["command"]            # routed through the file, not inline
    assert "hi there" in r["output"]


@pytest.mark.skipif(not _WIN, reason="script-file routing is Windows-REPL-specific")
def test_simple_command_stays_inline(executor):
    # A quote-free single-line command must NOT pay the temp-file cost and must
    # still persist state through the REPL (cwd carried by the inline path).
    r = executor.run("Write-Output plain")
    assert r["exit_code"] == 0
    assert ".ps1" not in r["command"]
    assert "plain" in r["output"]


# -- self-nested shell unwrapping -------------------------------------------------
# Task D (orch_83251fc258bc): a worker sent `powershell -NoProfile -ExecutionPolicy
# Bypass -Command "Write-Output \"批次D…\""` — its OWN nested shell spawn, with
# cmd-style `\"` that PowerShell doesn't use. My file-routing ran the *outer*
# command, but the inner script re-parsed through a *second* powershell.exe (5.1)
# whose parser split the `$()` subexpression into background jobs → still mangled.
# run() now unwraps the inner script and runs it once in this REPL.

def test_unwrap_nested_shell_predicate():
    from coworker.tools.shell import _unwrap_nested_shell

    # The exact Task-D #125 shape (cmd-style escaped double quotes) → inner script.
    assert _unwrap_nested_shell(
        r'powershell -NoProfile -ExecutionPolicy Bypass -Command "Write-Output \"hi there\""'
    ) == 'Write-Output "hi there"'
    # pwsh + single-quoted payload + `-c` short form also unwrap.
    assert _unwrap_nested_shell("pwsh -Command 'Get-Date'") == "Get-Date"
    assert _unwrap_nested_shell('& powershell.exe -c "1+1"') == "1+1"
    # Not a self-nested shell → leave untouched (None), so normal commands are safe.
    assert _unwrap_nested_shell("git status") is None
    assert _unwrap_nested_shell("Write-Output 'plain'") is None
    assert _unwrap_nested_shell('python -c "print(1)"') is None
    # Full path to a pwsh exe still unwraps.
    assert _unwrap_nested_shell(
        r'C:\Program Files\PowerShell\7\pwsh.exe -NoProfile -Command "Write-Output \"x\""'
    ) == 'Write-Output "x"'


@pytest.mark.skipif(not _WIN, reason="nested-shell unwrap is Windows-REPL-specific")
def test_self_nested_shell_command_runs_cleanly(executor):
    # A model that wraps its own command in `powershell -Command "…"` must get a
    # clean single-line result, not a split background-job / ParserError.
    cmd = r'powershell -NoProfile -ExecutionPolicy Bypass -Command "Write-Output \"unwrapped ok\""'
    r = executor.run(cmd)
    assert r["exit_code"] == 0
    assert "unwrapped ok" in r["output"]
    assert "ParserError" not in r["output"]
    assert "Job" not in r["output"]  # no background-job spillover from the double parse
