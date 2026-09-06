"""P0 结构无损裁剪 (GuaAgent 研究文档 #112) — 模块级单元测试。

契约:
  * user / assistant 消息原文 100% 保留 (engine 层保证, 见 test_engine.py);
  * tool 消息只裁剪"机械性膨胀"内容: 超大输出 / base64 数据 / 元数据块;
  * 裁剪只发生在 provider feed, 持久化历史不被修改;
  * 关闭开关 (trim_tool_outputs=False) 后逐字节恢复旧行为。
"""

from __future__ import annotations

from coworker.trim import (
    TOOL_TRIM_MIN_LEN,
    TRIM_EXEMPT_TOOLS,
    trim_tool_content,
)


def _big_text(n: int = TOOL_TRIM_MIN_LEN * 3, filler: str = "x") -> str:
    """A mechanically bloated tool body: long, repetitive, no semantics."""
    return filler * n


# -- 基础裁剪 ------------------------------------------------------------------


def test_short_output_untouched():
    text = "all good" * 10
    assert trim_tool_content(text) is text or trim_tool_content(text) == text
    assert trim_tool_content(text) == text


def test_long_output_head_tail_kept_middle_marked():
    text = "STATUS: OK\n" + _big_text() + "\nPAYLOAD_TAIL: done"
    out = trim_tool_content(text)
    assert out != text
    assert "STATUS: OK" in out
    assert "PAYLOAD_TAIL: done" in out
    assert "trimmed" in out and "session history" in out
    assert len(out) < len(text)


def test_long_output_never_truncates_below_marker_plus_ends():
    text = _big_text()
    out = trim_tool_content(text)
    assert len(out) < len(text)
    # 头尾各保留阈值, 中间是显式标记 —— 不会把内容裁没
    assert len(out) > 100


def test_barely_over_threshold_not_lengthened():
    """刚过阈值但头尾裁剪节省 < MIN_NET_SAVING 时, 不应用 head/tail 裁剪
    (否则 marker 会让输出比原文还长 —— 荒谬且误导模型)。"""
    text = "A" * (TOOL_TRIM_MIN_LEN + 1)
    out = trim_tool_content(text)
    assert len(out) <= len(text)  # 绝不比原文更长
    assert "trimmed" not in out


# -- base64 数据 ---------------------------------------------------------------


def test_base64_uri_collapsed():
    payload = "A" * 4096  # way over BASE64_MIN_LEN
    text = f"here is the file: data:image/png;base64,{payload}\nand more text"
    out = trim_tool_content(text)
    assert "base64,A" * 1 not in out
    assert "data:image/png;base64," not in out
    assert "collapsed" in out
    assert "and more text" in out


def test_small_base64_untouched():
    payload = "AA=="  # tiny icon-like data URI
    text = f"icon: data:image/png;base64,{payload}"
    assert trim_tool_content(text) == text


def test_base64_within_short_body_untouched():
    # 整个 body 未达裁剪阈值时, 即便含 base64 也不动 (body 短 ≠ 膨胀)
    text = "ok " + "data:image/png;base64," + "B" * 700
    assert len(text) < TOOL_TRIM_MIN_LEN
    assert trim_tool_content(text) == text


# -- 元数据块 ------------------------------------------------------------------


def test_oversized_meta_json_elided():
    big = '"metadata": "' + "m" * 5000 + '"'
    text = '{"ok": true, ' + big + ', "result": 42}'
    out = trim_tool_content(text)
    assert "elided metadata block" in out
    assert '"result": 42' in out
    assert "m" * 5000 not in out


def test_small_json_untouched():
    text = '{"ok": true, "result": 42, "meta": {"a": 1}}'
    assert trim_tool_content(text) == text


# -- content-parts (OpenAI 风格) ----------------------------------------------


def test_content_parts_list_trimmed_text_only():
    parts = [
        {"type": "text", "text": _big_text()},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    out = trim_tool_content(parts)
    assert isinstance(out, list)
    assert out[0]["type"] == "text"
    assert "trimmed" in out[0]["text"]
    # 非文本 part 原样保留
    assert out[1] == parts[1]


def test_content_parts_short_text_untouched():
    parts = [{"type": "text", "text": "short"}]
    assert trim_tool_content(parts) == parts


# -- 未知形状安全 --------------------------------------------------------------


def test_unknown_shapes_untouched():
    assert trim_tool_content(None) is None
    assert trim_tool_content(12345) == 12345
    assert trim_tool_content({"a": 1}) == {"a": 1}


# -- S3 实证修复: 显式内容请求工具豁免 head/tail 裁剪 -------------------------


def test_read_file_output_not_head_tail_trimmed():
    """read_file 是"显式内容请求" — 输出是模型要用的完整内容, 不裁剪中间
    (蜂群 worker 曾因内容被裁而自造 read_file_plain 绕开 — S3 实证)。"""
    text = "STATUS: line1\n" + _big_text() + "\nTAIL: last"
    out = trim_tool_content(text, tool_name="read_file")
    assert out == text  # 完整保留 (base64/元数据折叠除外, 此处无)


def test_read_file_still_collapses_base64():
    """豁免工具的降级: base64 折叠仍生效 (那是真·机械性膨胀)。"""
    text = "data:image/png;base64," + "Q" * 3000 + "\nrest"
    out = trim_tool_content(text, tool_name="read_file")
    assert "collapsed" in out
    assert "rest" in out


def test_knowledge_search_output_not_trimmed():
    text = _big_text()
    assert trim_tool_content(text, tool_name="knowledge_search") == text


def test_other_tools_still_trimmed():
    """非豁免工具 (如 run_shell 输出) 保持 head/tail 裁剪。"""
    text = _big_text()
    out = trim_tool_content(text, tool_name="run_shell")
    assert "trimmed" in out
    assert len(out) < len(text)


def test_no_tool_name_defaults_to_trim():
    """未传 tool_name (未知工具) 保持裁剪 — 保守默认。"""
    text = _big_text()
    out = trim_tool_content(text)
    assert "trimmed" in out


def test_exempt_list_contains_content_tools():
    assert "read_file" in TRIM_EXEMPT_TOOLS
    assert "knowledge_search" in TRIM_EXEMPT_TOOLS
    assert "web_fetch" in TRIM_EXEMPT_TOOLS


# -- 2026-09-06: load_skill 豁免 (oir-delegate 会话实证) -----------------------


def test_load_skill_full_body_preserved():
    """load_skill 的 SKILL.md 全文是模型显式请求的完整内容 — 中文正文
    (如 oir-delegate 5.6KB) 被裁到 head+tail 后 agent 读不到铁律/使用步骤,
    只能写辅助脚本绕道 run_shell 折腾十几轮 (2026-09-06 主会话实证)。
    豁免后必须原文送达。"""
    skill_body = (
        "---\nname: oir-delegate\n---\n# OIR 委托\n\n## 铁律\n"
        + "1. 先握手再委托：网关不可达时报告原因，不要重试轰炸。" * 60
        + "\n## 使用步骤\n"
        + "python oir_delegate.py <文档路径> --max-terms 40\n" * 30
        + "\n## 已知边界\n网关是试点进程, 重启后需手动重启。"
    )
    assert len(skill_body) > TOOL_TRIM_MIN_LEN * 2  # 确实是会触发裁剪的长度
    out = trim_tool_content(skill_body, tool_name="load_skill")
    assert out == skill_body  # 全文原样
    assert "trimmed" not in out


def test_load_skill_still_collapses_base64_but_keeps_long_strings():
    """豁免的降级语义 (2026-09-06 第二次实证后收紧): load_skill 输出里的
    base64 仍折叠; 但元数据 elide 不再作用于豁免工具 — engine 对 dict
    结果 json.dumps 后技能正文整体是单个超长引号字符串, elide 它 = 折叠正文。"""
    text = (
        "body text\n"
        + "data:image/png;base64," + "Z" * 3000 + "\n"
        + '"meta": "' + "m" * 2000 + '"'
    )
    out = trim_tool_content(text, tool_name="load_skill")
    assert "collapsed" in out
    assert "elided metadata block" not in out
    assert "body text" in out
    assert "m" * 100 in out  # 长字符串值原样保留


def test_load_skill_json_envelope_body_preserved():
    """生产形态回归: engine 对 dict 工具结果 json.dumps (engine.py:1492),
    load_skill 的 instructions 成为单个超长 JSON 引号字符串 — 豁免工具
    必须原样送达 (2026-09-06 主会话实证: 正文被元数据 elide 整段替换,
    agent 读不到技能正文, 只能写辅助脚本绕道折腾十几轮浪费大量 token)。"""
    import json

    skill_body = (
        "---\nname: oir-longrun\n---\n# OIR 长程任务\n\n## 工作流\n"
        + "curl -s http://127.0.0.1:8787/api/longrun/health  # 先握手再提交\n" * 40
        + "\n## 控制接口\nPOST /api/longrun/pause|resume|complete"
    )
    assert len(skill_body) > 900  # 超过 META_MIN_LEN, 正是事发条件
    envelope = json.dumps(
        {
            "name": "oir-longrun",
            "instructions": skill_body,
            "resources_path": r"C:\some\skills\oir-longrun",
        },
        ensure_ascii=False,
    )
    out = trim_tool_content(envelope, tool_name="load_skill")
    assert out == envelope  # JSON 信封逐字节原样: 正文一字不丢
    assert "elided metadata block" not in out


def test_read_file_json_envelope_content_preserved():
    """同类回归: read_file 返回 dict → json.dumps 后 content 是超长引号
    字符串 — 豁免工具的文件内容同样不得被元数据 elide 折叠。"""
    import json

    envelope = json.dumps(
        {"path": "notes.md", "content": "第一章 中文正文内容\n" * 300, "total_lines": 300},
        ensure_ascii=False,
    )
    out = trim_tool_content(envelope, tool_name="read_file")
    assert out == envelope


def test_read_file_lines_exempt_via_prefix():
    """注释声称的前缀匹配真正落地: read_file_lines (read_ 前缀) 豁免 —
    scan_result.json 这类多行内容分段读取时同样不该被裁。"""
    text = "\n".join(f'{{"row": {i}, "title": "中文条目{i}"}}' for i in range(200))
    assert len(text) > TOOL_TRIM_MIN_LEN
    out = trim_tool_content(text, tool_name="read_file_lines")
    assert out == text


def test_search_tool_prefix_exempt_but_shell_not():
    """前缀语义边界: search_* 豁免; run_shell 不因任何前缀误豁免。"""
    text = _big_text()
    assert trim_tool_content(text, tool_name="search_knowledge") == text
    out = trim_tool_content(text, tool_name="run_shell")
    assert "trimmed" in out
