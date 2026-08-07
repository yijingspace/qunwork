"""Lightweight vector memory for QunWork orchestration (Phase 3).

A minimal in-process memory layer so workers can retrieve relevant prior results
(shared blackboard / episodic memory) without a vector database. Embeddings come
from an injectable `embedder` (text -> vector); without one, retrieval falls back
to character-similarity ranking (difflib) so the MVP works out of the box.

Also powers the governance drift metric when an embedder is supplied.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

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

    def _new_item(self, text: str, meta: dict[str, Any]) -> MemoryItem:
        vec = None
        if self.embedder is not None:
            try:
                vec = self.embedder(text)
            except Exception:
                logger.debug("embedder failed on add", exc_info=True)
                vec = None
        return MemoryItem(text=text, meta=meta, vector=vec)

    def add(self, text: str, **meta: Any) -> None:
        self.items.append(self._new_item(text, meta))

    def search(
        self, query: str, k: int = 3, phase: Optional[int] = None
    ) -> list[MemoryHit]:
        # `phase` is a no-op here (T5) — the in-memory store has no phase index;
        # PersistentVectorMemory implements phase-preferring retrieval.
        if not self.items:
            return []
        scored: list[MemoryHit] = []
        # Owner-audit 2026-08-07 (bug #15): cache the query embedding once
        # instead of recomputing it inside the loop for every item.
        qv: Optional[list[float]] = None
        if self.embedder is not None:
            try:
                qv = self.embedder(query)
            except Exception:
                logger.debug("embedder failed on query", exc_info=True)
                qv = None
        for item in self.items:
            if item.vector is not None and qv is not None:
                score = cosine(qv, item.vector)
            else:
                score = difflib.SequenceMatcher(None, query, item.text).ratio()
            scored.append(MemoryHit(text=item.text, score=score, meta=item.meta))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]

    def __len__(self) -> int:
        return len(self.items)
