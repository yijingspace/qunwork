"""Structurally lossless trimming (P0, from GuaAgent research doc #112).

QunWork 知识库 GuaAgent 框架研究(结构无损剪枝 / structurally lossless trimming):
在保留 100% 用户消息和助手回复原文的前提下, 通过剥离工具输出、base64 图片、
元数据等"机械性膨胀"内容, 平均减少 20% 的 token 消耗, 极端场景可达 86%.

This module implements exactly that contract at the *provider feed* layer:

  * user / assistant messages are NEVER touched — their text is 100% preserved;
  * only ``role == "tool"`` messages (the tool results) are trimmed;
  * a tool result is trimmed only when it is mechanically bloated:
      - an oversized body → keep head (verdict/status) + tail (payload tail),
        replace the middle with an explicit ``…[trimmed]…`` marker;
      - inline base64 data URIs → collapsed to a short placeholder (the model
        already receives real image parts via ``image_url`` parts, so a base64
        echo inside tool text is pure bloat);
      - oversized metadata-ish JSON blocks → elided with a marker.

The trim is applied at send time in ``engine._outbound_messages`` — the persisted
``self.messages`` (and any audit trail) always keep the FULL tool output, so
nothing is ever lost on disk; only what crosses the wire to the LLM is reduced.
Switching the feature off (config ``trim_tool_outputs = false``) restores the
old feed byte-for-byte.

Everything here is pure (no I/O, no state) so it is trivially unit-testable.
"""

from __future__ import annotations

import re
from typing import Any

# -- thresholds ----------------------------------------------------------------
# A tool body shorter than this is never touched (short outputs are not bloat).
TOOL_TRIM_MIN_LEN = 1200
# Keep the first N chars (usually carries status/verdict/headlines) …
TOOL_TRIM_HEAD = 800
# … and the last N chars (usually carries the payload tail / error / summary).
TOOL_TRIM_TAIL = 400
# A base64 data URI whose encoded payload is at least this long is collapsed.
BASE64_MIN_LEN = 512
# A JSON value (rendered inline inside tool text) longer than this is elided.
META_MIN_LEN = 900
# Trimming only pays off when it actually saves something meaningful: the
# ``…[trimmed]…`` marker itself costs ~75 chars, so a body that only barely
# crosses TOOL_TRIM_MIN_LEN would come out LONGER after trimming. We require a
# net saving of at least this many chars before applying the head/tail cut
# (base64 collapse / meta elision apply unconditionally — they always shrink).
MIN_NET_SAVING = 200

# 豁免裁剪的工具 (S3 上下文预算管理实证修复, 蜂群自造 read_file_plain 的根因):
# 这些工具的输出是模型"显式请求的完整内容" (文件内容/知识检索/搜索结果),
# 裁剪会让模型读到残缺数据 — 静默信息丢失。只裁机械性膨胀输出
# (base64/元数据/超大日志), 不裁"内容请求"类结果。前缀匹配:
# 以 `read_` / `knowledge_search` / `search_` / `web_fetch` 开头的工具,
# 其输出视为内容本身, 不做 head/tail 裁剪 (base64/元数据折叠仍生效)。
TRIM_EXEMPT_TOOLS: tuple[str, ...] = (
    "read_file",
    "read_file_plain",
    "knowledge_search",
    "web_fetch",
    "web_search",
    "grep",
    "list_files",
)

# Matches inline data URIs: data:image/png;base64,<payload> (also pdf/audio/etc).
# The payload class is deliberately STRICT (no whitespace): base64 payloads in
# tool output are single-line, and allowing \s made the regex swallow following
# prose ("and more text" is almost all base64-alphabet letters + spaces).
_BASE64_URI_RE = re.compile(
    r"data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]{"
    + str(BASE64_MIN_LEN)
    + r",}"
)


def _collapse_base64(text: str) -> str:
    """Replace oversized inline base64 data URIs with a compact placeholder.

    Only URIs whose encoded payload is large enough to matter are collapsed;
    small ones (e.g. a tiny inline icon) pass through untouched.
    """

    def _rep(m: re.Match[str]) -> str:
        payload_len = len(m.group(0))
        return (
            f"[base64 data collapsed: {payload_len} chars]"
            f" (original kept in session history)"
        )

    return _BASE64_URI_RE.sub(_rep, text)


def _elide_meta_json(text: str) -> str:
    """Elide oversized JSON string values that look like metadata blobs.

    A tool result rendered as JSON frequently embeds huge metadata/verbose
    dictionaries (``"metadata": {…}``, ``"meta": {…}``, ``"debug": {…}``).
    Rendering those verbatim is pure token bloat — the model needs the verdict
    keys, not the whole dump. We replace any string value that is itself longer
    than META_MIN_LEN with an explicit marker, keeping the JSON parseable.
    """
    if len(text) <= META_MIN_LEN:
        return text
    # Fast pre-check: only bother when a big quoted run exists at all.
    if '"' not in text:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != '"':
            out.append(ch)
            i += 1
            continue
        # find the closing unescaped quote
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"':
                break
            j += 1
        if j >= n:
            out.append(text[i:])
            break
        value = text[i : j + 1]
        if len(value) - 2 > META_MIN_LEN:  # minus the two quotes
            out.append('"[elided metadata block, original kept in history]"')
        else:
            out.append(value)
        i = j + 1
    return "".join(out)


def trim_tool_content(content: Any, *, tool_name: Optional[str] = None) -> Any:
    """Structurally lossless trim of ONE tool message's content.

    Accepts a plain string or a content-parts list (OpenAI-style). Returns the
    trimmed copy; the input is never mutated.

    `tool_name`: 触发该 tool 消息的工具名。对"显式内容请求"类工具
    (read_file/knowledge_search/web_fetch…, 见 TRIM_EXEMPT_TOOLS) 跳过
    head/tail 裁剪 — 它们的输出是模型要用的完整内容, 裁剪=静默信息丢失
    (S3 实证: 蜂群 worker 因 read_file 内容被裁而自造 read_file_plain)。
    base64/元数据折叠对所有工具仍生效 (那才是真·机械性膨胀)。
    """
    if tool_name in TRIM_EXEMPT_TOOLS:
        return _collapse_only(content)
    if isinstance(content, str):
        return _trim_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                trimmed = _trim_text(part.get("text", ""))
                out.append({**part, "text": trimmed})
            else:
                out.append(part)
        return out
    # Unknown shape — leave untouched (never break a provider feed).
    return content


def _collapse_only(content: Any) -> Any:
    """豁免工具的降级处理: 只折叠 base64/元数据膨胀, 不做 head/tail 裁剪。"""
    if isinstance(content, str):
        return _elide_meta_json(_collapse_base64(content))
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                collapsed = _elide_meta_json(_collapse_base64(part.get("text", "")))
                out.append({**part, "text": collapsed})
            else:
                out.append(part)
        return out
    return content


def _trim_text(text: str) -> str:
    if not isinstance(text, str) or len(text) < TOOL_TRIM_MIN_LEN:
        return text
    text = _collapse_base64(text)
    text = _elide_meta_json(text)
    if len(text) <= TOOL_TRIM_MIN_LEN:
        return text
    head = text[:TOOL_TRIM_HEAD]
    tail = text[-TOOL_TRIM_TAIL:]
    removed = len(text) - TOOL_TRIM_HEAD - TOOL_TRIM_TAIL
    # The marker costs ~75 chars; if the head/tail cut would not actually save
    # a meaningful amount, return the (possibly base64/meta-elided) body as-is
    # — a *longer* "trimmed" output would be absurd and confuse the model.
    if removed < MIN_NET_SAVING:
        return text
    return (
        f"{head}\n…[trimmed {removed} chars of tool output — "
        f"full result kept in session history]…\n{tail}"
    )
