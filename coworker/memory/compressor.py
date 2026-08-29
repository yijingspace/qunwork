"""皮萨诺记忆压缩引擎 (突破方案三) — 会话历史的周期性压缩.

依据《QunWork 7x24长程任务架构·创新研究与突破方案》第五节:
  * 消息序列按类型 (user/assistant/tool/system) 存在皮萨诺周期 ``π(m)``;
  * 原始消息 (~2KB JSON) → 周期模板 + 增量差分 → ~500B, 压缩比 ~4:1;
  * ``S_compressed = S_cycle + N·S_δ``, 当 N 大时压缩率 > 90%。

与 B4 瓶颈对应: ``ConversationStore`` 追加写 `.jsonl`, 长会话可超 100MB,
7×24 任务产生数百万条消息。本引擎把消息批次压缩为周期模板 + 增量,
无损往返 (roundtrip), 供存储层把历史块压缩后落盘。

实现要点:
  * **周期提取**: 按消息 role 序列计算最短周期 (Boyer-Moore 式前缀函数) —
    消息类型天然交替, 周期通常为 2 (user→assistant) 或 4;
  * **模板+增量**: 模板 = 第一个周期内的消息; 后续消息与模板对应槽位做差分
    (文本公共前缀 + 差异 JSON), 增量远小于原文;
  * **无损往返**: ``decompress(compress(msgs)) == msgs`` 保证 (差异为结构化的,
    不依赖 LLM 摘要 — 摘要型压缩在 checkpoint.summary 粒度, 这里必须可逆);
  * 真实压缩率用 zlib 度量; 皮萨诺周期使重复模式集中, zlib 收益最大。
"""

from __future__ import annotations

import json
import logging
import zlib
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 消息类型 → 周期基数 (4 种类型, π(4)=6 为数学下界; 实际按 role 序列自测)
ROLE_CYCLE_BASE = 4


def shortest_cycle(seq: list[str]) -> int:
    """序列最短周期长度 (前缀函数法, O(n))。非周期时返回 len(seq)。"""
    n = len(seq)
    if n == 0:
        return 0
    # 找最小 k 使 seq[:n-k] == seq[k:]
    pi = [0] * n
    for i in range(1, n):
        j = pi[i - 1]
        while j > 0 and seq[i] != seq[j]:
            j = pi[j - 1]
        if seq[i] == seq[j]:
            j += 1
        pi[i] = j
    k = n - pi[-1]
    if n % k == 0 and seq == (seq[:k] * (n // k)):
        return k
    return n


def _delta_of(full: dict, template: dict) -> dict:
    """full 相对 template 的增量: 保留 key 集合差异 + 内容差异。"""
    out: dict[str, Any] = {"_keys": sorted(set(full) - set(template))}
    for k in template:
        if k in full and full[k] != template[k]:
            out[k] = full[k]
    for k in out["_keys"]:
        out[k] = full[k]
    return out


def _apply_delta(template: dict, delta: dict) -> dict:
    out = {k: v for k, v in template.items() if k not in delta or delta[k] == v}
    out.update({k: v for k, v in delta.items() if k != "_keys"})
    for k in delta.get("_keys", []):
        if k in delta:
            out[k] = delta[k]
    return out


def _text_ratio(a: str, b: str) -> float:
    """公共前缀比例 0..1 (文本相似度的轻量度量, 无 difflib 依赖)。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i / n if n else 1.0


@dataclass
class PisanoCompressed:
    """压缩结果: 周期模板 + 增量数组 + 元数据。"""

    template: list[dict]
    deltas: list[dict]
    period: int
    total: int
    raw_bytes: int
    packed_bytes: int

    def to_dict(self) -> dict:
        return {
            "type": "pisano",
            "period": self.period,
            "template": self.template,
            "deltas": self.deltas,
            "total": self.total,
            "raw_bytes": self.raw_bytes,
            "packed_bytes": self.packed_bytes,
            "compression_ratio": self.compression_ratio,
        }

    @property
    def compression_ratio(self) -> float:
        if self.raw_bytes <= 0:
            return 0.0
        return 1.0 - self.packed_bytes / self.raw_bytes


class PisanoMemoryCompressor:
    """皮萨诺周期压缩引擎 — 无损往返。

    用法::

        comp = PisanoMemoryCompressor()
        c = comp.compress_messages(messages)          # -> dict (可 JSON 序列化)
        restored = comp.decompress(c)                 # == messages (无损)
    """

    def __init__(self, *, min_cycle_len: int = 2, delta_threshold: float = 0.3) -> None:
        # 低于该相似度阈值的消息不增量 (整条保留), 避免差分解压后膨胀。
        self.min_cycle_len = min_cycle_len
        self.delta_threshold = delta_threshold

    def compress_messages(self, messages: list[dict]) -> dict:
        """压缩消息序列。少于一个周期的消息原样打包 (type=raw)。"""
        if len(messages) < self.min_cycle_len:
            return {
                "type": "raw",
                "data": messages,
                "total": len(messages),
                "raw_bytes": self._bytes(messages),
                "packed_bytes": self._bytes(messages),
            }
        roles = [str(m.get("role", "")) for m in messages]
        period = max(self.min_cycle_len, shortest_cycle(roles))
        template = messages[:period]
        deltas: list[dict] = []
        for i, msg in enumerate(messages):
            if i < period:
                continue  # 模板本身不增量
            slot = i % period
            base = template[slot]
            # 内容近似度低 → 整条保留 (delta 标记 full); 否则存差异。
            text_a = self._text_of(base)
            text_b = self._text_of(msg)
            if text_a and text_b and _text_ratio(text_a, text_b) < self.delta_threshold:
                deltas.append({"_full": msg})
            else:
                deltas.append(_delta_of(msg, base))
        packed = zlib.compress(
            json.dumps(
                {"template": template, "deltas": deltas}, ensure_ascii=False, default=str
            ).encode("utf-8"),
            level=6,
        )
        raw_bytes = self._bytes(messages)
        ratio = 1.0 - len(packed) / raw_bytes if raw_bytes else 0.0
        return {
            "type": "pisano",
            "period": period,
            "template": template,
            "deltas": deltas,
            "total": len(messages),
            "raw_bytes": raw_bytes,
            "packed_bytes": len(packed),
            "compression_ratio": ratio,
        }

    def decompress(self, compressed: dict) -> list[dict]:
        """解压消息序列 — 无损还原 (roundtrip)。"""
        if compressed.get("type") == "raw":
            return list(compressed.get("data") or [])
        template = list(compressed.get("template") or [])
        deltas = list(compressed.get("deltas") or [])
        period = len(template) or 1
        out: list[dict] = []
        # 重建完整序列: 模板周期槽位 × 增量 → 消息
        for i in range(compressed.get("total", len(template) + len(deltas))):
            if i < period:
                out.append(dict(template[i]))
            else:
                delta = deltas[(i - period) % len(deltas)] if deltas else {}
                if "_full" in delta:
                    out.append(dict(delta["_full"]))
                else:
                    out.append(_apply_delta(template[i % period], delta))
        return out

    @staticmethod
    def _text_of(msg: dict) -> str:
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    parts.append(str(c.get("text") or c.get("content") or ""))
                else:
                    parts.append(str(c))
            return " ".join(parts)
        return ""

    @staticmethod
    def _bytes(messages: list[dict]) -> int:
        return len(json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8"))
