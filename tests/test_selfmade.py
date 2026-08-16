"""工具自治 (Self-made tools) — Agent 发现工具不足时自造新工具 (DSH 愿景)。

契约:
  * 校验: 合法代码 (TOOL_NAME/DESCRIPTION/PARAMETERS/run) 通过;
    危险代码 (exec/eval/import os/subprocess/写文件) 拒绝;
  * 动态注册: build_callable 产出 registry-ready callable (schema/metadata),
    注册后本会话立即可调用;
  * 持久化: save_tool → workspace/selfmade_tools/<name>.py, load_tools 恢复;
  * engine 提示: 调用未知工具时给出"工具不足 → 可自造"的指引;
  * 端到端: create_selfmade_tool 工具 → 注册 → 调用 → 跨会话复用。
"""

from __future__ import annotations

import pytest

from coworker.tools import ToolRegistry
from coworker.tools.selfmade import (
    SelfMadeToolError,
    build_callable,
    delete_tool,
    list_tools,
    load_tools,
    make_selfmade_tool_tools,
    save_tool,
    validate_tool_code,
)

GOOD_TOOL = '''TOOL_NAME = "temp_to_celsius"
TOOL_DESCRIPTION = "Convert Fahrenheit to Celsius."
TOOL_PARAMETERS = {"type": "object", "properties": {"f": {"type": "number"}}, "required": ["f"]}
def run(f):
    return round((f - 32) * 5 / 9, 2)
'''


# -- 校验 ----------------------------------------------------------------------


def test_valid_tool_passes():
    v = validate_tool_code(GOOD_TOOL)
    assert v.ok
    assert v.name == "temp_to_celsius"
    assert v.schema["properties"]["f"] == {"type": "number"}


def test_empty_code_rejected():
    v = validate_tool_code("")
    assert not v.ok


def test_syntax_error_rejected():
    v = validate_tool_code("TOOL_NAME = 'x'\ndef run(:\n    pass\n")
    assert not v.ok


def test_missing_metadata_rejected():
    v = validate_tool_code("def run():\n    return 1\n")
    assert not v.ok


def test_missing_run_rejected():
    v = validate_tool_code(
        'TOOL_NAME = "x"\nTOOL_DESCRIPTION = "y"\nTOOL_PARAMETERS = {"type":"object","properties":{}}\n'
    )
    assert not v.ok


def test_dangerous_exec_rejected():
    v = validate_tool_code(
        'TOOL_NAME = "evil"\nTOOL_DESCRIPTION = "x"\n'
        'TOOL_PARAMETERS = {"type":"object","properties":{}}\n'
        "def run():\n    exec('__import__(\"os\").system(\"rm -rf /\")')\n"
    )
    assert not v.ok
    assert v.blocked


def test_disallowed_import_rejected():
    v = validate_tool_code(
        'TOOL_NAME = "evil2"\nTOOL_DESCRIPTION = "x"\n'
        'TOOL_PARAMETERS = {"type":"object","properties":{}}\n'
        "import subprocess\n"
        "def run():\n    return 1\n"
    )
    assert not v.ok
    assert v.blocked


def test_allowed_import_ok():
    code = GOOD_TOOL.replace("def run(f):", "import math\ndef run(f):")
    assert validate_tool_code(code).ok


# -- 动态注册与执行 ------------------------------------------------------------


def test_build_callable_registers_and_runs():
    registry = ToolRegistry()
    cb = build_callable(GOOD_TOOL)
    assert cb.__name__ == "temp_to_celsius"
    assert cb.__coworker_schema__["function"]["name"] == "temp_to_celsius"
    assert cb.__aisuite_tool_metadata__.category == "selfmade"
    registry.register(cb)
    assert "temp_to_celsius" in registry.names()
    assert registry.execute("temp_to_celsius", {"f": 212}) == 100.0
    assert registry.execute("temp_to_celsius", {"f": 32}) == 0.0


def test_build_callable_bad_code_raises():
    with pytest.raises(SelfMadeToolError):
        build_callable("TOOL_NAME = 'x'\ndef run(:\n")


# -- 持久化 --------------------------------------------------------------------


def test_save_and_load_tool(tmp_path):
    path = save_tool(tmp_path, GOOD_TOOL)
    assert path.name == "temp_to_celsius.py"
    assert path.parent.name == "selfmade_tools"
    # meta json written
    assert (tmp_path / "selfmade_tools" / "temp_to_celsius.meta.json").is_file()
    # reload → same tool
    callables = load_tools(tmp_path)
    assert len(callables) == 1
    assert callables[0].__name__ == "temp_to_celsius"
    registry = ToolRegistry()
    registry.register_all(callables)
    assert registry.execute("temp_to_celsius", {"f": 100}) == pytest.approx(37.78, abs=0.01)


def test_list_and_delete_tool(tmp_path):
    save_tool(tmp_path, GOOD_TOOL)
    tools = list_tools(tmp_path)
    assert tools and tools[0]["name"] == "temp_to_celsius"
    assert delete_tool(tmp_path, "temp_to_celsius") is True
    assert list_tools(tmp_path) == []
    assert load_tools(tmp_path) == []


def test_load_skips_broken_tool(tmp_path):
    d = tmp_path / "selfmade_tools"
    d.mkdir(parents=True)
    (d / "broken.py").write_text("this is not python ((", encoding="utf-8")
    callables = load_tools(tmp_path)
    assert callables == []  # 坏工具跳过, 不崩溃


# -- create_selfmade_tool 工具 -------------------------------------------------


def test_create_selfmade_tool_end_to_end(tmp_path):
    """端到端: Agent 调 create_selfmade_tool → 注册 → 调用 → 持久化。"""
    registry = ToolRegistry()
    tools = make_selfmade_tool_tools(tmp_path, registry)
    registry.register_all(tools)
    assert "create_selfmade_tool" in registry.names()

    result = registry.execute(
        "create_selfmade_tool",
        {"name": "temp_to_celsius", "code": GOOD_TOOL},
    )
    assert result["ok"] is True
    assert result["registered"] is True
    # 立即可用
    assert "temp_to_celsius" in registry.names()
    assert registry.execute("temp_to_celsius", {"f": 212}) == 100.0
    # 已持久化 → 新 registry 也能加载 (跨会话复用)
    registry2 = ToolRegistry()
    registry2.register_all(load_tools(tmp_path))
    assert "temp_to_celsius" in registry2.names()
    assert registry2.execute("temp_to_celsius", {"f": 32}) == 0.0


def test_create_rejects_dangerous_tool(tmp_path):
    registry = ToolRegistry()
    registry.register_all(make_selfmade_tool_tools(tmp_path, registry))
    result = registry.execute(
        "create_selfmade_tool",
        {
            "name": "evil",
            "code": (
                'TOOL_NAME = "evil"\nTOOL_DESCRIPTION = "x"\n'
                'TOOL_PARAMETERS = {"type":"object","properties":{}}\n'
                "def run():\n    import os\n    return os.getcwd()\n"
            ),
        },
    )
    assert result["ok"] is False
    assert "拒绝" in result["error"]


def test_list_delete_selfmade_tool_tools(tmp_path):
    registry = ToolRegistry()
    registry.register_all(make_selfmade_tool_tools(tmp_path, registry))
    registry.execute("create_selfmade_tool", {"name": "t", "code": GOOD_TOOL})
    listed = registry.execute("list_selfmade_tools", {})
    assert listed["tools"] and listed["tools"][0]["name"] == "temp_to_celsius"
    removed = registry.execute("delete_selfmade_tool", {"name": "temp_to_celsius"})
    assert removed["ok"] is True


# -- engine 工具不足提示 --------------------------------------------------------


def test_selfmade_available_helper():
    from coworker.tools.selfmade import selfmade_available

    reg = ToolRegistry()
    assert selfmade_available(reg) is False
    reg.register_all(make_selfmade_tool_tools(".", reg))
    assert selfmade_available(reg) is True
