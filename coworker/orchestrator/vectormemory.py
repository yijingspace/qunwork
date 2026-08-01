"""Lightweight vector memory for QunWork orchestration (Phase 3).

A minimal in-process memory layer so workers can retrieve relevant prior results
(shared blackboard / episodic memory) without a vector database. Embeddings come
from an injectable `embedder` (text -> vector); without one, retrieval falls back
to character-similarity ranking (difflib) so the MVP works out of the box.

Also powers the governance drift metric when an embedder is supplied.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

Embedder = Callable[[str], list[float]]


@dataclass
class MemoryItem:
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    vector: Optional[list[float]] = None


@dataclass
class MemoryHit:
    text: str
    score: float
    meta: dict[str, Any] = field(default_factory=dict)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


class VectorMemory:
    """In-process episodic memory with similarity retrieval."""

    def __init__(self, embedder: Optional[Embedder] = None) -> None:
        self.embedder = embedder
        self.items: list[MemoryItem] = []

    def add(self, text: str, **meta: Any) -> None:
        vec = None
        if self.embedder is not None:
            try:
                vec = self.embedder(text)
            except Exception:
                vec = None
        self.items.append(MemoryItem(text=text, meta=meta, vector=vec))

    def search(self, query: str, k: int = 3) -> list[MemoryHit]:
        if not self.items:
            return []
        scored: list[MemoryHit] = []
        for item in self.items:
            if item.vector is not None and self.embedder is not None:
                try:
                    qv = self.embedder(query)
                    score = cosine(qv, item.vector)
                except Exception:
                    score = difflib.SequenceMatcher(None, query, item.text).ratio()
            else:
                score = difflib.SequenceMatcher(None, query, item.text).ratio()
            scored.append(MemoryHit(text=item.text, score=score, meta=item.meta))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]

    def __len__(self) -> int:
        return len(self.items)
