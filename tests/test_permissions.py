

def test_find_exec_flags_never_allowlisted():
    """Upstream #281: `find . -exec cmd {} +` must not auto-run just because
    `find` is on the allowlist."""
    from coworker.permissions import Mode, PermissionEngine

    e = PermissionEngine(
        workspace_root="/tmp", allowed_commands=["find"], mode=Mode.INTERACTIVE
    )
    for evil in [
        "find . -exec rm -rf {} +",
        "find . -execdir sh -c 'touch pwned' {} +",
        "find . -ok cp /etc/passwd /tmp/x {} ;",
        "find . -delete",
    ]:
        d = e.evaluate("run_shell", {"command": evil})
        assert d.allowed is False, f"{evil} must require approval"
    # benign find still matches the allowlist
    ok = e.evaluate("run_shell", {"command": "find . -name '*.py'"})
    assert ok.allowed is True


def test_write_tool_override_cannot_bypass_workspace_scope():
    """Upstream #116: overriding write_file's risk to 'read' must NOT skip the
    workspace path check (path scoping keys off the tool name)."""
    from coworker.permissions import Mode, PermissionEngine
    from coworker.risk import RiskClass

    e = PermissionEngine(workspace_root="/tmp/work", mode=Mode.AUTO)
    e.risk_overrides = lambda name: RiskClass.READ if name == "write_file" else None
    d = e.evaluate("write_file", {"path": "/tmp/escape.md"})
    assert d.allowed is False
    assert "writable directory" in d.reason
    d2 = e.evaluate("write_file", {"path": "inside.md"})
    assert d2.allowed is True
