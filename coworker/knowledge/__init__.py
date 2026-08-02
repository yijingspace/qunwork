"""Knowledge file library — tool-level entry for the agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import aisuite as ai

from .store import KnowledgeStore


def knowledge_tools(
    workspace: str,
    db_path: str | Path,
    embedder: Optional[Any] = None,
) -> list:
    """Expose `knowledge_search` to engines that have a workspace: the agent can
    retrieve relevant passages from the workspace's knowledge library (indexed
    documents + manual entries) instead of re-reading files from scratch."""

    def knowledge_search(query: str, k: int = 3) -> dict:
        """Search the workspace knowledge library for passages relevant to `query`.
        The library contains indexed workspace documents (md/txt) and manually
        added entries. Returns the top-k matching passages with their source.
        Use this when you need facts/context from the project's documents."""
        store = KnowledgeStore(db_path, embedder=embedder, workspace=workspace)
        try:
            hits = store.search(query, k=int(k), workspace=workspace)
        except Exception as exc:  # pragma: no cover - defensive
            return {"error": str(exc), "results": []}
        if not hits:
            return {
                "query": query,
                "results": [],
                "note": "no matches — try scanning the workspace via the knowledge API",
            }
        return {
            "query": query,
            "results": [
                {
                    "title": h["title"],
                    "source": h["source_path"] or "(manual entry)",
                    "kind": h["kind"],
                    "score": h["score"],
                    "content": h["content"][:800],
                }
                for h in hits
            ],
        }

    return [
        ai.tool(
            knowledge_search,
            metadata=ai.ToolMetadata(
                category="knowledge",
                risk_level="low",
                capabilities=["knowledge_search"],
            ),
        )
    ]
