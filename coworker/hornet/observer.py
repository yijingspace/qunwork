"""HORNET 2D — observer (涌现进化).

The meta-cognition layer: watches resonance patterns and grows new structure.

  1. hypernode (模式压缩): cells that co-resonate frequently become a super-node
     summary (title merged).
  2. attractor  (异常放大): high-similarity pairs whose contents diverge sharply
     (near-duplicate titles, very different bodies) are flagged as strange
     attractors — potential contradictions / innovation seeds.
  3. gap        (自我增殖): isolated cells (degree 0) or under-connected tiles
     are reported as knowledge gaps to fill.
  4. fission    (蜂胞分裂): high-load cells split into Z+/Z- child cells.
  5. cavity     (立体空洞): cells never resonated → topological cavities.
  6. conflict   (分层冲突): Z+ projection vs Z- traceback phase opposition.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .store import HornetStore, ngram_vector, similarity
from .builder import _phase_from_content_3d, _opposite_hint

try:
    from .dpnn_phase import cell_period
    _DPNN = True
except ImportError:
    _DPNN = False


class HornetObserver:
    def __init__(
        self, store: HornetStore, *, co_occurrence: int = 2, fission_threshold: float = 0.25,
        llm_synthesize: Optional[Callable[[str, str, str], str]] = None,
    ) -> None:
        self.store = store
        self.co_occurrence = co_occurrence
        self.fission_threshold = fission_threshold
        # L2 大模型裂变合成: callable(父内容, 维度标签, 提示) -> 子知识文本。
        # 热点节点 fission 时优先用 LLM 生成真正的新知识; 失败/低频回退规则拆分。
        self.llm_synthesize = llm_synthesize

    def _known_titles(self) -> list[str]:
        return [n["title"] for n in self.store.list_nodes()]

    def evolve(self, *, limit: int = 20) -> dict[str, Any]:
        emerged: list[dict[str, Any]] = []
        counts = {"hypernode": 0, "attractor": 0, "gap": 0, "fission": 0, "cavity": 0, "conflict": 0}

        # -- cell fission (spec §三维蜂胞分裂自扩展) -------------------------
        # A cell that resonates far more than its peers has a high load_factor —
        # it 1+6 SPLITS (水平扩展优先, 原文锁定): 以父为中心在六边形 6 方位生成
        # 6 个语义维子胞(实体/属性/关系/事件/规则/证据), 继承方位连接 + 缝隙补全;
        # 再向对向 zone 垂直生长一个 Z 子胞。子内容 = 规则按维度拆分(含 cue 句)
        # 或 LLM 合成(真正裂变出新知识)。
        stats = self.store.node_hit_stats(200)
        nodes = {n["id"]: n for n in self.store.list_nodes()}
        if stats:
            known_titles = [n["title"] for n in nodes.values()]
            hot = sorted(stats.items(), key=lambda kv: -kv[1]["load_factor"])
            fissioned: set[int] = set()
            for nid, st in hot[:6]:
                if st["load_factor"] < self.fission_threshold or nid in fissioned:
                    continue
                n = nodes.get(nid)
                if not n or not (n["content"] or "").strip():
                    continue
                # MEDIUM 修复 (security-review): 裂变增殖上限 —
                # ①深度: 母标题每含一个「·」即为上一级裂变产物, ≥2 级不再裂变;
                # ②总量: 节点数超上限不再裂变 (防无限增殖 + LLM 成本失控)。
                if (n["title"] or "").count("·") >= 2:
                    continue
                if self.store.node_count() >= 5000:
                    break
                # skip if this mother cell already fissioned (same title child exists)
                title = n["title"]
                if any(e.startswith(f"{title} · ") for e in known_titles):
                    continue

                # 1+6 水平裂变: 6 个语义维子胞 (六边形 6 方位)
                children: list[int] = []
                for dim_key, dim_label, cue_idx, (dx, dy), hint in _SEM_DIMS:
                    child_title = f"{title[:36]} · {dim_label}"
                    if any(e.startswith(child_title) for e in known_titles):
                        continue
                    # 子内容: LLM 合成优先 → 规则按维度拆分 → 提示兜底
                    child_content = ""
                    if self.llm_synthesize is not None:
                        try:
                            child_content = (self.llm_synthesize(
                                n["content"], dim_label, hint
                            ) or "").strip()
                        except Exception:
                            child_content = ""
                    if len(child_content) < 40:  # LLM 空壳/未配置 → 规则拆分
                        picked = _split_dimension(n["content"], _DIM_CUES[cue_idx])
                        if not picked:
                            # L3 质量闭环: 无实质内容(LLM 空 + 规则无匹配) → 丢弃,
                            # 不生成占位空壳污染知识网。
                            continue
                        child_content = f"[{dim_label}] " + "；".join(picked[:6])[:2000]
                    cid = self.store.add_node(
                        child_title, child_content[:2000],
                        kb_item_id=n["kb_item_id"],
                        vec=ngram_vector(f"{child_title} {child_content}"),
                        phase=_phase_from_content_3d(f"{child_title} {child_content}"),
                        x=n["x"] + dx, y=n["y"] + dy, z=n["z"],
                    )
                    # 继承父的方位连接 (G0-G5 对应 6 维通道)
                    self.store.add_edge(nid, cid, "similar", weight=0.7, channel=f"G{cue_idx}")
                    # 缝隙补全: 相邻子胞之间弱边 (六边形平铺无空洞)
                    if children:
                        self.store.add_edge(
                            children[-1], cid, "similar", weight=0.3, channel="G3"
                        )
                    children.append(cid)
                    emerged.append(
                        {"kind": "fission", "title": child_title,
                         "detail": {"mother": nid, "child": cid,
                                    "dim": dim_key, "load_factor": st["load_factor"]}}
                    )
                    counts["fission"] += 1
                    if len(emerged) >= limit:
                        break

                # 垂直生长 (Z 对向 zone, 保留原文自相似扩展) — 独立于 6 维子胞
                # (6 维可能全被 L3 丢弃, 但 Z 子胞始终有实质内容)
                if len(emerged) < limit:
                    z_child = "推演延伸" if n["z"] <= 0 else "溯源锚点"
                    z_title = f"{title[:40]} · {z_child}"
                    if not any(e.startswith(z_title) for e in known_titles):
                        z_content = _derive_child(n["content"], z_child)
                        cid = self.store.add_node(
                            z_title, z_content[:2000], kb_item_id=n["kb_item_id"],
                            vec=ngram_vector(f"{z_title} {z_content}"),
                            phase=_phase_from_content_3d(f"{z_title} {z_content}"),
                            x=n["x"], y=n["y"], z=1 if z_child == "推演延伸" else -1,
                        )
                        self.store.add_edge(nid, cid, "similar", weight=0.8, channel="G3")
                        emerged.append(
                            {"kind": "fission", "title": z_title,
                             "detail": {"mother": nid, "child": cid,
                                        "dim": "z", "load_factor": st["load_factor"]}}
                        )
                        counts["fission"] += 1

                fissioned.add(nid)
                if len(emerged) >= limit:
                    break

        # -- hypernodes from co-resonance -------------------------------
        pairs: dict[tuple[int, int], int] = {}
        for rec in self.store.recent_resonance(100):
            hit_ids = [h.get("node_id") for h in rec.get("hits", []) if h.get("node_id")]
            for i in range(len(hit_ids)):
                for j in range(i + 1, len(hit_ids)):
                    key = tuple(sorted((hit_ids[i], hit_ids[j])))
                    pairs[key] = pairs.get(key, 0) + 1
        for (a, b), cnt in sorted(pairs.items(), key=lambda kv: -kv[1]):
            if cnt < self.co_occurrence:
                break
            na, nb = nodes.get(a), nodes.get(b)
            if not na or not nb:
                continue
            title = f"{na['title'][:24]} ⊕ {nb['title'][:24]}"
            if any(em.get("title") == title for em in emerged):
                continue
            emerged.append(
                {
                    "kind": "hypernode",
                    "title": title,
                    "detail": {
                        "members": [a, b],
                        "co_occurrences": cnt,
                        "summary": _merge_summary(na["content"], nb["content"]),
                    },
                }
            )
            counts["hypernode"] += 1
            if len(emerged) >= limit:
                break

        # -- attractors: similar titles, divergent bodies ----------------
        edges = self.store.list_edges()
        seen: set[tuple[int, int]] = set()
        for e in edges:
            if e["relation"] != "similar":
                continue
            key = tuple(sorted((e["src"], e["dst"])))
            if key in seen:
                continue
            seen.add(key)
            na, nb = nodes.get(e["src"]), nodes.get(e["dst"])
            if not na or not nb:
                continue
            ta, tb = (na["content"] or ""), (nb["content"] or "")
            if not ta or not tb:
                continue
            sim = similarity(ta[:600], tb[:600])
            # same-ish title, low body similarity → suspicious divergence
            if _titles_overlap(na["title"], nb["title"]) and sim < 0.15 and len(ta) > 80 and len(tb) > 80:
                emerged.append(
                    {
                        "kind": "attractor",
                        "title": f"奇异吸引子: {na['title'][:28]} ↔ {nb['title'][:28]}",
                        "detail": {"nodes": [na["id"], nb["id"]], "body_similarity": round(sim, 4)},
                    }
                )
                counts["attractor"] += 1
                if len(emerged) >= limit:
                    break

        # -- gaps: topological cavities (spec §立体空洞挖掘) -----------------
        # Not just isolated cells: cells that NEVER resonate (zero amplitude in
        # the resonance history) form knowledge cavities — the system flags them
        # and suggests a probe wave to actively fill the void.
        degree: dict[int, int] = {}
        for e in edges:
            degree[e["src"]] = degree.get(e["src"], 0) + 1
            degree[e["dst"]] = degree.get(e["dst"], 0) + 1
        hit_ids = set(stats.keys())
        for nid, n in nodes.items():
            if degree.get(nid, 0) == 0 and (n["content"] or ""):
                emerged.append(
                    {
                        "kind": "gap",
                        "title": f"知识空洞: {n['title'][:40]}",
                        "detail": {"node_id": nid, "hint": "该节点没有语义连接,建议补充关联或合并"},
                    }
                )
                counts["gap"] += 1
                if len(emerged) >= limit:
                    break
        for nid, n in nodes.items():
            if len(emerged) >= limit:
                break
            if nid in hit_ids or degree.get(nid, 0) == 0 or not (n["content"] or ""):
                continue
            emerged.append(
                {
                    "kind": "cavity",
                    "title": f"拓扑腔体: {n['title'][:40]}",
                    "detail": {
                        "node_id": nid,
                        "hint": "该蜂房从未被任何探针波共振命中,建议定向探测补全",
                        "probe": n["title"][:60],
                    },
                }
            )
            counts["cavity"] += 1

        # -- layered conflict (spec §分层冲突涌现) ---------------------------
        # Z+ projection cells whose content contradicts Z- traceback cells:
        # phase opposition across zones marks a high-grade strange attractor
        # that deserves human attention first.
        zplus = [n for n in nodes.values() if n.get("z", 0) > 0]
        zminus = [n for n in nodes.values() if n.get("z", 0) < 0]
        for zp in zplus:
            if len(emerged) >= limit:
                break
            for zm in zminus:
                if _opposite_hint(zp["title"], zm["title"]):
                    emerged.append(
                        {
                            "kind": "conflict",
                            "title": f"分层冲突: {zp['title'][:24]} ↔ {zm['title'][:24]}",
                            "detail": {
                                "z_plus": zp["id"],
                                "z_minus": zm["id"],
                                "hint": "推演层与溯源层相位对冲,优先人工介入",
                            },
                        }
                    )
                    counts["conflict"] += 1
                    break

        saved = 0
        for em in emerged:
            eid, is_new = self.store.add_emergent(em["kind"], em["title"], em["detail"])
            em["id"] = eid
            em["is_new"] = is_new
            if is_new:
                saved += 1
        return {"emerged": saved, "counts": counts, "items": emerged}

    def freshness_pass(self, *, stale_threshold: float = 0.30) -> dict[str, Any]:
        """A: 周期驱动的知识保鲜与遗忘.

        Each cell has a natural period (DPNN Pisano period of its content fingerprint).
        High-load cells with short periods are "fresh" (actively resonating knowledge).
        Low-load cells with long periods decay — their freshness drops each pass.
        When freshness falls below stale_threshold, the cell is downgraded to the
        Z- traceback layer (evidence/archive zone), making room for active knowledge
        in the XY/Z+ layers.

        Returns a summary of decayed/refreshed/downgraded counts.
        """
        if not _DPNN:
            return {"decayed": 0, "refreshed": 0, "downgraded": 0, "note": "dpnn unavailable"}
        stats = self.store.node_hit_stats(200)
        nodes = self.store.list_nodes()
        if not nodes:
            return {"decayed": 0, "refreshed": 0, "downgraded": 0}
        decayed = refreshed = downgraded = 0
        fresh_updates: dict[int, float] = {}
        for n in nodes:
            nid = n["id"]
            period = cell_period(n["title"], n.get("content", ""))
            load = stats.get(nid, {}).get("load_factor", 0.0)
            current = n.get("freshness", 1.0)
            # short period + high load → fresh (actively resonating)
            if load > 0.15 and period <= 60:
                if current < 1.0:
                    fresh_updates[nid] = 1.0
                    refreshed += 1
                continue
            # long period + low load → decay
            decay_rate = 0.12 if period > 60 else 0.06
            if load < 0.05:
                decay_rate *= 1.5  # never hit → faster decay
            new_fresh = current - decay_rate
            if new_fresh <= 0:
                new_fresh = 0.0
            if new_fresh < current:
                fresh_updates[nid] = new_fresh
                decayed += 1
            # Stale knowledge is marked by freshness alone (reversible — a later
            # resonance refresh restores it). We do NOT permanently rewrite the
            # z layout here: that would fight the cross-layer damping and the
            # prior review fix (reversible failure feedback).
            if new_fresh < stale_threshold:
                downgraded += 1  # semantically stale (Z- "traceback" state)
        if fresh_updates:
            self.store.batch_freshness(fresh_updates)
        return {"decayed": decayed, "refreshed": refreshed, "downgraded": downgraded}


def _derive_child(content: str, kind: str) -> str:
    """Self-similar growth: a child cell inherits the core text plus a zone
    cue. Z+ projection appends forward-looking prompts; Z- traceback keeps the
    factual core for anchoring."""
    core = (content or "").strip()[:1200]
    if kind == "推演延伸":
        return core + "\n[推演延伸] 该知识的未来发展方向与潜在推演:需要补充预测与规划。"
    return core + "\n[溯源锚点] 该知识的起源与历史证据锚定:需要补充来源与背景。"


# -- L1 1+6 水平裂变: 六维语义子胞 (6 维语义基, 六边形 6 方位) ---------------
# (dim_key, 中文标签, cue 索引, hex 邻居偏移, 生成提示)
_SEM_DIMS = [
    ("entity", "实体", 5, (1, 0), "识别该知识涉及的核心实体/名称/模型/系统/组件"),
    ("attribute", "属性", 3, (0, 1), "提炼该知识的属性/特征/参数/关键指标"),
    ("relation", "关系", 4, (-1, 1), "梳理该知识与其他概念的依赖/关联/协作关系"),
    ("event", "事件", 0, (-1, 0), "归纳该知识涉及的事件/时间线/过程步骤"),
    ("rule", "规则", 1, (0, -1), "总结该知识的规则/标准/约束/必须禁止项"),
    ("evidence", "证据", 2, (1, -1), "整理支撑该知识的数据/来源/引用/实验证据"),
]

# 各维度 cue 关键词 (与 builder._phase_from_content 一致)
_DIM_CUES: list[tuple[str, ...]] = [
    ("时间", "日期", "年", "月", "周报", "日报"),          # 0 event/temporal
    ("规则", "标准", "流程", "必须", "禁止"),              # 1 rule
    ("证据", "数据", "来源", "引用", "实验"),              # 2 evidence
    ("属性", "特征", "参数", "尺寸", "颜色"),              # 3 attribute
    ("关系", "依赖", "关联", "协作"),                       # 4 relation
    ("实体", "名称", "模型", "系统", "软件"),               # 5 entity
]


def _split_dimension(content: str, cue_kws: tuple[str, ...]) -> list[str]:
    """按语义维度从父内容抽取相关句(含 cue 关键词的句子)。"""
    import re

    sentences = re.split(r"[。；;\n]", content or "")
    return [
        s.strip() for s in sentences
        if s.strip() and any(kw in s for kw in cue_kws)
    ]


def _titles_overlap(a: str, b: str) -> bool:
    """Crude title kinship: share a >=4-char token or one contains the other."""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    sa = {t for t in a.split() if len(t) >= 4}
    sb = {t for t in b.split() if len(t) >= 4}
    return bool(sa & sb)


def _merge_summary(ca: str, cb: str) -> str:
    a = (ca or "").strip()
    b = (cb or "").strip()
    return (a[:160] + " … | … " + b[:160]).replace("\n", " ")


def generate_hypothetical_questions(topic: str) -> list[str]:
    """F: 自我提问式探索 — generate hypothetical questions about a cavity topic.

    From a single topic string, produce 3 exploratory questions that probe
    different epistemic directions: definition, relation, and origin. These
    become search tasks that fill the knowledge void."""
    t = (topic or "").strip()[:60]
    if not t:
        return []
    return [
        f"什么是{t}？请解释其核心定义与关键特征。",
        f"{t}与哪些概念存在关联或依赖关系？请梳理其知识图谱。",
        f"{t}的起源与演变历程是什么？请追溯其发展脉络。",
    ]
