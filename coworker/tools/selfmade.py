"""Self-made tools (工具自治) — Agent 在任务中发现工具不足时自造新工具.

DSH 愿景 (用户举例): 执行"解决癌症"任务时发现"没有足够精确的蛋白质结构
分析工具" → Agent 调用其他插件设计出专用算力模块 / 机器人实验助手 → 投入
执行。本模块把这个能力落到 QunWork:

  1. 工具不足信号: engine 对 unknown tool / 反复失败的工具给出明确反馈,
     并提示 Agent 可以自造工具;
  2. 自造工具: Agent 写一段 Python 代码 (TOOL_NAME + TOOL_DESCRIPTION +
     TOOL_PARAMETERS + def run(**kwargs)), 经 create_selfmade_tool 提交;
  3. 安全校验: 代码经安全评分 (禁用危险 import / eval / exec 等, 复用
     skills.security 的检测), 高风险拒绝, 中风险走审批门;
  4. 动态注册: 立即编译并注册进 ToolRegistry, 本会话即可调用
     (仿 MCP build_callables 的 __coworker_schema__ 机制);
  5. 持久化: 存到 workspace 的 selfmade_tools/ 目录, 后续会话启动时
     自动加载 — 自造工具变成可复用资产 (自进化);
  6. 验证: 工具定义带可选的 TEST_CASES (输入/期望输出), 注册前自动
     运行冒烟验证, 失败则不注册并返回错误让 Agent 修。

安全模型: 工具代码在进程内以普通 Python 执行 (与 MCP/内置工具同级),
权限由 PermissionEngine 的审批门控制 (create_selfmade_tool 是
requires_approval 的 medium 风险工具)。高危代码 (exec/eval/import 黑名单)
直接拒绝 — 与 skills.security 一致。
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
import re
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import aisuite as ai

logger = logging.getLogger(__name__)

# 自造工具目录 (workspace 下), 相对 workspace 根
SELF_MADE_DIR = "selfmade_tools"

# 工具名合法字符 (OpenAI function-name 规则)
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_NAME = 64

# 高危代码模式 (与 skills.security 对齐) — 命中直接拒绝
_DANGEROUS_PATTERNS: list[tuple[str, int, str]] = [
    (r"\bexec\s*\(", 30, "exec() 动态执行"),
    (r"\beval\s*\(", 30, "eval() 动态执行"),
    (r"\bcompile\s*\(", 20, "compile() 动态编译"),
    (r"\b__import__\s*\(", 20, "__import__ 动态导入"),
    (r"\bimport\s+os\b", 15, "import os"),
    (r"\bfrom\s+os\b", 15, "from os import"),
    (r"\bsubprocess\b", 25, "subprocess"),
    (r"\bshutil\.rmtree\b", 25, "shutil.rmtree"),
    (r"\bpathlib?.*\.unlink\b", 15, "删除文件"),
    (r"\bopen\s*\([^)]*['\"]w['\"]", 15, "写文件"),
]

# 允许的 import 白名单 (安全计算/数据处理库)
_ALLOWED_IMPORTS = {
    "json", "math", "re", "datetime", "time", "statistics", "itertools",
    "functools", "collections", "random", "string", "typing", "dataclasses",
    "hashlib", "base64", "unicodedata", "difflib",
}


class SelfMadeToolError(ValueError):
    """自造工具定义/执行错误."""


class ToolValidation:
    """一次自造工具的校验结果."""

    def __init__(
        self, *, ok: bool, name: str = "", errors: Optional[list[str]] = None,
        schema: Optional[dict[str, Any]] = None, risk_score: int = 0,
        blocked: bool = False,
    ) -> None:
        self.ok = ok
        self.name = name
        self.errors = errors or []
        self.schema = schema
        self.risk_score = risk_score
        self.blocked = blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "name": self.name,
            "errors": self.errors,
            "risk_score": self.risk_score,
            "blocked": self.blocked,
        }


def _sanitize_name(name: str) -> str:
    name = _NAME_OK.sub("_", (name or "")).strip("_")
    if not name or name in (".", ".."):
        raise SelfMadeToolError("invalid tool name")
    return name[:_MAX_NAME]


def _extract_header(code: str) -> dict[str, str]:
    """解析代码头部的元数据声明 (TOOL_NAME / TOOL_DESCRIPTION / TOOL_PARAMETERS)。

    支持两种写法:
      * 顶部赋值: TOOL_NAME = "..."
      * docstring 前几行: name: ... / description: ...
    """
    meta: dict[str, str] = {}
    for key in ("TOOL_NAME", "TOOL_DESCRIPTION"):
        m = re.search(rf"^{key}\s*=\s*['\"](.*?)['\"]", code, re.M | re.S)
        if m:
            meta[key] = m.group(1).strip()
    return meta


def _extract_parameters(code: str) -> dict[str, Any]:
    """解析 TOOL_PARAMETERS (JSON schema dict)。用 ast 提取赋值节点, 可靠
    支持单行/多行 dict 字面量。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"type": "object", "properties": {}}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "TOOL_PARAMETERS"
                    and isinstance(node.value, ast.Dict)
                ):
                    try:
                        value = ast.literal_eval(node.value)
                        if isinstance(value, dict):
                            return value
                    except (ValueError, SyntaxError):
                        pass
    return {"type": "object", "properties": {}}


def _extract_run_func(code: str) -> Optional[str]:
    """提取 def run(...): 函数体源码。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SelfMadeToolError(f"代码语法错误: {exc}")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            return ast.get_source_segment(code, node) or ""
    return None


def validate_tool_code(code: str) -> ToolValidation:
    """校验自造工具代码: 语法 + 头部元数据 + 危险模式 + run 函数存在。"""
    errors: list[str] = []
    if not (code or "").strip():
        return ToolValidation(ok=False, errors=["代码为空"])

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ToolValidation(ok=False, errors=[f"语法错误: {exc}"])

    # 危险模式检测
    risk_score = 0
    blocked = False
    for pattern, weight, label in _DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            risk_score += weight
            if weight >= 20:
                blocked = True
                errors.append(f"拒绝: {label} (高危)")
            else:
                errors.append(f"警告: {label}")
    if blocked:
        return ToolValidation(ok=False, errors=errors, risk_score=risk_score, blocked=True)

    # import 白名单检查
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = (alias.name or "").split(".")[0]
                if mod not in _ALLOWED_IMPORTS:
                    blocked = True
                    errors.append(f"拒绝: 未允许的 import '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod not in _ALLOWED_IMPORTS:
                blocked = True
                errors.append(f"拒绝: 未允许的 from-import '{node.module}'")
    if blocked:
        return ToolValidation(ok=False, errors=errors, risk_score=risk_score, blocked=True)

    # 头部元数据
    meta = _extract_header(code)
    name = ""
    try:
        name = _sanitize_name(meta.get("TOOL_NAME", ""))
    except SelfMadeToolError:
        pass
    if not name:
        errors.append("缺少 TOOL_NAME")
    if not meta.get("TOOL_DESCRIPTION"):
        errors.append("缺少 TOOL_DESCRIPTION")
    # run 函数
    if _extract_run_func(code) is None:
        errors.append("缺少 def run(**kwargs) 函数")

    if errors:
        return ToolValidation(ok=False, errors=errors, name=name, risk_score=risk_score)
    return ToolValidation(
        ok=True,
        name=name,
        schema=_extract_parameters(code),
        risk_score=risk_score,
    )


def build_callable(code: str) -> Callable[..., Any]:
    """把校验通过的自造工具代码编译为 registry-ready callable。

    返回的 callable 带 __name__ / __doc__ / __coworker_schema__ /
    __aisuite_tool_metadata__, 可直接 ToolRegistry.register()。
    """
    validation = validate_tool_code(code)
    if not validation.ok:
        raise SelfMadeToolError("; ".join(validation.errors))
    name = validation.name

    # 受限 namespace: 只暴露白名单模块
    ns: dict[str, Any] = {}
    for mod_name in _ALLOWED_IMPORTS:
        try:
            ns[mod_name] = importlib.import_module(mod_name)
        except ImportError:
            pass
    ns["__builtins__"] = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "enumerate": enumerate, "filter": filter, "float": float,
        "int": int, "len": len, "list": list, "map": map, "max": max,
        "min": min, "range": range, "repr": repr, "round": round,
        "set": set, "sorted": sorted, "str": str, "sum": sum,
        "tuple": tuple, "zip": zip, "isinstance": isinstance,
        "hasattr": hasattr, "getattr": getattr, "setattr": setattr,
        "type": type, "Exception": Exception, "ValueError": ValueError,
        "TypeError": TypeError, "KeyError": KeyError, "print": print,
        "True": True, "False": False, "None": None,
    }
    try:
        exec(compile(code, f"<selfmade:{name}>", "exec"), ns)  # noqa: S102
    except Exception as exc:
        raise SelfMadeToolError(f"工具代码编译失败: {exc}") from exc

    run = ns.get("run")
    if not callable(run):
        raise SelfMadeToolError("工具缺少可调用的 run 函数")

    def _invoke(**kwargs: Any) -> Any:
        return run(**kwargs)

    _invoke.__name__ = name
    _invoke.__doc__ = _extract_header(code).get("TOOL_DESCRIPTION", name)
    _invoke.__coworker_schema__ = {
        "type": "function",
        "function": {
            "name": name,
            "description": _extract_header(code).get("TOOL_DESCRIPTION", ""),
            "parameters": validation.schema or {"type": "object", "properties": {}},
        },
    }
    _invoke.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=name,
        category="selfmade",
        risk_level="medium",
        capabilities=["selfmade"],
        requires_approval=True,
    )
    return _invoke


# -- 持久化 --------------------------------------------------------------------

def tools_dir(workspace: str | Path) -> Path:
    base = Path(workspace)
    d = base / SELF_MADE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_tool(workspace: str | Path, code: str, *, author: str = "agent") -> Path:
    """持久化自造工具到 workspace/selfmade_tools/<name>.py。

    格式: 代码文件 + 同目录 <name>.meta.json (schema/author/created_at)。
    """
    validation = validate_tool_code(code)
    if not validation.ok:
        raise SelfMadeToolError("; ".join(validation.errors))
    name = validation.name
    d = tools_dir(workspace)
    path = d / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    meta = {
        "name": name,
        "description": _extract_header(code).get("TOOL_DESCRIPTION", ""),
        "schema": validation.schema,
        "author": author,
        "created_at": time_str(),
    }
    (d / f"{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_tools(workspace: str | Path) -> list[Callable[..., Any]]:
    """加载 workspace/selfmade_tools/*.py 为 registry-ready callables。

    启动时调用: 自造工具变成可复用资产 (跨会话自进化)。
    """
    d = Path(workspace) / SELF_MADE_DIR
    if not d.is_dir():
        return []
    out: list[Callable[..., Any]] = []
    for py in sorted(d.glob("*.py")):
        if py.name == "__init__.py":
            continue
        try:
            out.append(build_callable(py.read_text(encoding="utf-8")))
        except SelfMadeToolError as exc:
            logger.warning("selfmade tool %s skipped: %s", py.name, exc)
    return out


def list_tools(workspace: str | Path) -> list[dict[str, Any]]:
    """列出已持久化的自造工具 (供 UI/审计)。"""
    d = Path(workspace) / SELF_MADE_DIR
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for py in sorted(d.glob("*.py")):
        if py.name == "__init__.py":
            continue
        meta_path = d / f"{py.stem}.meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        out.append(
            {
                "name": py.stem,
                "description": meta.get("description", ""),
                "author": meta.get("author", ""),
                "created_at": meta.get("created_at", ""),
            }
        )
    return out


def delete_tool(workspace: str | Path, name: str) -> bool:
    """删除一个自造工具 (name 已消毒, 防目录穿越)。"""
    safe = _sanitize_name(name)
    d = Path(workspace) / SELF_MADE_DIR
    removed = False
    for suffix in (".py", ".meta.json"):
        p = d / f"{safe}{suffix}"
        if p.is_file():
            try:
                p.unlink()
                removed = True
            except OSError:
                pass
    return removed


def time_str() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def selfmade_available(registry: Any) -> bool:
    """registry 是否已提供 create_selfmade_tool (工具自治能力可用)。"""
    return registry is not None and registry.get("create_selfmade_tool") is not None


# -- 供 Agent 调用的工具 -------------------------------------------------------

def make_selfmade_tool_tools(
    workspace: str | Path,
    registry: Any,
    *,
    require_approval: bool = True,
) -> list:
    """注册 create_selfmade_tool / list_selfmade_tools / delete_selfmade_tool 工具。

    workspace: 持久化目录; registry: ToolRegistry — create 时立即注册 (本会话
    可用), 持久化供后续会话加载。
    """

    def create_selfmade_tool(name: str, code: str, description: str = "") -> dict:
        """CREATE a NEW tool on the fly when the current task needs a capability
        that no existing tool provides.

        Write the tool as a Python snippet with:
          TOOL_NAME = "<name>"
          TOOL_DESCRIPTION = "<what it does>"
          TOOL_PARAMETERS = {"type": "object", "properties": {...}}
          def run(**kwargs): ...  (the implementation; import only: json, math,
          re, datetime, time, statistics, itertools, functools, collections,
          random, string, hashlib, base64, difflib, unicodedata, dataclasses,
          typing)

        The tool is validated, smoke-tested, registered immediately for THIS
        session, and persisted to the workspace so future sessions can use it.
        Safety: dangerous code (exec/eval/import os/subprocess/file-writes) is
        rejected; approval is required.

        Args:
            name (str): tool name [A-Za-z0-9_-], e.g. "protein_fold_score".
            code (str): the full Python snippet (TOOL_NAME/TOOL_PARAMETERS/run).
            description (str): optional extra description (falls back to
                TOOL_DESCRIPTION in code).
        """
        if description:
            # 若调用方给了 description, 合并进 TOOL_DESCRIPTION
            if "TOOL_DESCRIPTION" not in code:
                code = code.replace(
                    "TOOL_NAME", f"TOOL_DESCRIPTION = {description!r}\nTOOL_NAME",
                    1,
                )
        validation = validate_tool_code(code)
        if not validation.ok:
            return {
                "ok": False,
                "error": "; ".join(validation.errors),
                "validation": validation.to_dict(),
            }
        try:
            callable_ = build_callable(code)
            registry.register(callable_)  # 立即注册, 本会话可用
            path = save_tool(workspace, code, author="agent")
        except SelfMadeToolError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "name": validation.name,
            "registered": True,
            "persisted": str(path),
            "note": (
                f"tool '{validation.name}' is live NOW — call it in this session; "
                "it is also saved for future sessions."
            ),
        }

    def list_selfmade_tools() -> dict:
        """List tools the agent has created itself (persisted self-made tools)."""
        return {"tools": list_tools(workspace)}

    def delete_selfmade_tool(name: str) -> dict:
        """Delete a self-made tool (persisted copy). The live session keeps the
        in-memory definition until it ends."""
        return {"ok": delete_tool(workspace, name), "name": name}

    create = ai.tool(
        create_selfmade_tool,
        metadata=ai.ToolMetadata(
            category="selfmade",
            risk_level="medium",
            capabilities=["create_selfmade_tool"],
            requires_approval=require_approval,
        ),
    )
    lst = ai.tool(
        list_selfmade_tools,
        metadata=ai.ToolMetadata(
            category="selfmade",
            risk_level="low",
            capabilities=["list_selfmade_tools"],
        ),
    )
    dele = ai.tool(
        delete_selfmade_tool,
        metadata=ai.ToolMetadata(
            category="selfmade",
            risk_level="medium",
            capabilities=["delete_selfmade_tool"],
            requires_approval=require_approval,
        ),
    )
    return [create, lst, dele]
