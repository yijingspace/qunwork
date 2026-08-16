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
