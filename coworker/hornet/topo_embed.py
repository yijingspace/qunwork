"""HORNET — topological soft-constraint embedding (CPU, numpy only).

Low-compute adaptation of the spec's 3D-GNN + persistence-homology embedder:
  - a 2-layer shallow GCN (numpy matrix ops — no torch), message passing over
    the hive's geometric edges with top-8 neighbor truncation;
  - a topology proxy loss: neighbor-distance regularizer + triangle-cycle
    constraint (semantic rings must not collapse), NO gudhi full complex —
    the cycle constraint is the cheap proxy for 0-dim/1-dim persistence.

The output is a dense manifold embedding `E (n x out_dim)` that keeps the
semantic content (recon against the PCA base) while injecting topology as a
soft bias. Layout + resonance seed can consume it; retrieval semantics stay on
2-gram coverage. Deterministic (fixed seed), small epochs, batch = all cells
(698 cells is trivial for numpy matmul).
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np


def _normalized_adjacency(edges: list[tuple[int, int]], n: int) -> np.ndarray:
    """D^-1/2 A D^-1/2 of the edge graph (self-loops added)."""
    A = np.zeros((n, n), dtype=np.float32)
    for a, b in edges:
        if 0 <= a < n and 0 <= b < n and a != b:
            A[a, b] = 1.0
            A[b, a] = 1.0
    A = A + np.eye(n)
    deg = A.sum(axis=1)
    d_inv_sqrt = np.where(deg > 0, 1.0 / np.sqrt(deg), 0.0)
    return (d_inv_sqrt[:, None] * A) * d_inv_sqrt[None, :]


def _pca_base(x: np.ndarray, dim: int = 48) -> np.ndarray:
    """Project the 2-gram sparse features to a dense PCA base (semantic anchor
    that the GCN must not destroy)."""
    x = x - x.mean(axis=0, keepdims=True)
    u, s, _ = np.linalg.svd(x, full_matrices=False)
    k = min(dim, u.shape[1])
    return u[:, :k] * s[:k]


def _triangle_cycle_loss(E: np.ndarray, triples: list[tuple[int, int, int]]) -> float:
    """Ring constraint: triangle cells on the hive must not collapse into one
    cluster — penalize when all three embeddings are closer than a floor.
    Cheap proxy for 1-dim persistence (rings survive)."""
    if not triples:
        return 0.0
    loss = 0.0
    for i, j, k in triples:
        d_ij = float(np.linalg.norm(E[i] - E[j]))
        d_jk = float(np.linalg.norm(E[j] - E[k]))
        d_ik = float(np.linalg.norm(E[i] - E[k]))
        min_d = min(d_ij, d_jk, d_ik)
        loss += max(0.0, 1.0 - min_d / 0.25)  # all-three collapse → penalty
    return loss / len(triples)


def _find_triangles(edges: list[tuple[int, int]], n: int, cap: int = 600) -> list[tuple[int, int, int]]:
    """Bounded triangle enumeration via neighbor sets (cycle regularizer input)."""
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    triples: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    for i in range(n):
        for j in adj.get(i, ()):
            if j <= i:
                continue
            for k in adj.get(i, ()) & adj.get(j, ()):
                key = tuple(sorted((i, j, k)))
                if key in seen:
                    continue
                seen.add(key)
                triples.append(key)
                if len(triples) >= cap:
                    return triples
    return triples


def topo_embed(
    features: np.ndarray,
    edges: list[tuple[int, int]],
    *,
    out_dim: int = 16,
    hid_dim: int = 32,
    epochs: int = 15,
    lr: float = 0.05,
    lam_topo: float = 0.1,
    seed: int = 0,
    top_k: int = 8,
) -> dict[str, Any]:
    """Train the 2-layer GCN with the topology proxy loss. Returns the dense
    embedding E (n x out_dim) plus training diagnostics.

    Loss = recon (vs PCA base) + lam_topo * (neighbor distance + cycle).
    """
    rng = np.random.default_rng(seed)
    n = features.shape[0]
    if n < 2:
        return {"embedding": np.zeros((n, out_dim), dtype=np.float32), "loss": 0.0}

    X = _pca_base(features, min(48, features.shape[1] if features.ndim > 1 else 48))
    # neighbor truncation: keep top-k strongest edges per node (sparsify the graph)
    strength = {(a, b): float(np.dot(X[a], X[b])) for a, b in edges}
    keep: set[tuple[int, int]] = set()
    per_node: dict[int, list[tuple[float, int]]] = {i: [] for i in range(n)}
    for (a, b), s in strength.items():
        per_node[a].append((s, b))
        per_node[b].append((s, a))
    for i, nbs in per_node.items():
        for _s, j in sorted(nbs, reverse=True)[:top_k]:
            keep.add(tuple(sorted((i, j))))
    edges_keep = [e for e in edges if tuple(sorted(e)) in keep]
    A = _normalized_adjacency(edges_keep, n)
    triples = _find_triangles(edges_keep, n)

    # 2-layer GCN: H = A relu(A X W1) W2. W1 is frozen after init (feature
    # transform); only W2 is trained — 512 params, so numerical gradients stay
    # cheap on CPU (~seconds per run).
    d_in = X.shape[1]
    W1 = rng.normal(0, 0.1, size=(d_in, hid_dim)).astype(np.float32)
    W2 = rng.normal(0, 0.1, size=(hid_dim, out_dim)).astype(np.float32)
    edge_pairs = np.array(edges_keep, dtype=np.int64).reshape(-1, 2) if edges_keep else np.zeros((0, 2), dtype=np.int64)

    def forward(backward: bool = False):
        h1 = np.maximum(A @ X @ W1, 0.0)
        h2 = A @ h1 @ W2
        k = min(out_dim, X.shape[1])
        recon_loss = float(np.mean((h2[:, :k] - X[:, :k]) ** 2))
        neighbor_loss = 0.0
        if len(edge_pairs):
            diff = h2[edge_pairs[:, 0]] - h2[edge_pairs[:, 1]]
            neighbor_loss = float(np.mean(diff ** 2))
        cycle_loss = _triangle_cycle_loss(h2, triples)
        total = recon_loss + lam_topo * (neighbor_loss + cycle_loss)
        return h2, total, (recon_loss, neighbor_loss, cycle_loss)

    best: Optional[np.ndarray] = None
    best_loss = float("inf")
    for _ep in range(epochs):
        h2, total, parts = forward()
        if total < best_loss:
            best_loss = total
            best = h2.copy()
        # hand-rolled gradient step on W2 (finite differences — small matrix)
        step = 1e-3
        grad = np.zeros_like(W2)
        for idx in np.ndindex(W2.shape):
            orig = W2[idx]
            W2[idx] = orig + step
            _, l_plus, _ = forward()
            W2[idx] = orig - step
            _, l_minus, _ = forward()
            W2[idx] = orig
            grad[idx] = (l_plus - l_minus) / (2 * step)
        W2 -= lr * grad

    E = (best if best is not None else forward()[0]).astype(np.float32)
    # L2-normalize rows so cosine between embeddings is well-defined
    norms = np.linalg.norm(E, axis=1, keepdims=True)
    E = E / np.where(norms > 0, norms, 1.0)
    return {
        "embedding": E,
        "loss": round(best_loss, 4),
        "parts": {"recon": round(parts[0], 4), "neighbor": round(parts[1], 4), "cycle": round(parts[2], 4)},
        "nodes": n,
        "edges_kept": len(edges_keep),
        "triangles": len(triples),
    }
