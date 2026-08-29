"""QunMesh M2: 六边形网格与邻域感知批选择 (丝瓜络拓扑·执行面)。

HexGrid 把 executor agent 编址到轴向坐标六边形上 (螺旋放置, 同进程逻辑坐标,
无需网络编址); select_batch_grid 是网格感知的批选择——替换 orchestrator
`_select_batch` 的 `ready[:max_parallel]` 截断:

1. **负载梯度引导**: 每任务按其 agent 的 load 信息素升序稳定排序 — 任务
   流向低负载 (空闲) 邻域, 而非按列表顺序挤同一热点;
2. **邻域分散**: 同一 agent 在批内最多占半数名额 (有 ≥2 个不同 agent 时),
   强制把名额让给其他邻域 — 批内热点减半;
3. **退化安全**: 无 pheromone / 单任务 / 任务全属同一 agent 时逐级退回
   旧截断行为。

模块级纯函数 + 注入负载读取, 符合 stub 兼容约定 (测试无需 orchestrator 实例)。
"""

from __future__ import annotations

from math import ceil
from typing import Any, Callable, Iterable

# axial 坐标六方向 (q, r) — 丝瓜络六邻接
HEX_DIRS: tuple[tuple[int, int], ...] = (
    (1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1),
)


def hex_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    """axial 坐标欧氏 hex 距离。"""
    dq = a[0] - b[0]
    dr = a[1] - b[1]
    ds = -dq - dr
    return max(abs(dq), abs(dr), abs(ds))


class HexGrid:
    """agent → 六边形格位编址。螺旋放置 (中心向外), 邻接查询 O(placed)。"""

    def __init__(self, radius: int = 4) -> None:
        self.radius = radius
        self._cells: list[tuple[int, int]] = self._spiral(radius)
        self._pos: dict[str, tuple[int, int]] = {}
        self._next = 0

    @staticmethod
    def _spiral(radius: int) -> list[tuple[int, int]]:
        """环序展开: 中心 + 半径 1..radius 的每环 (保证放置连续紧凑)。"""
        cells: list[tuple[int, int]] = [(0, 0)]
        for k in range(1, radius + 1):
            # 环 k: 从 (k, -k) 出发沿六方向各走 k 步
            q, r = k, -k
            for dq, dr in HEX_DIRS:
                for _ in range(k):
                    cells.append((q, r))
                    q, r = q + dq, r + dr
        return cells

    def place(self, name: str) -> tuple[int, int]:
        """为 agent 分配下一个格位 (重复 place 幂等)。"""
        if name in self._pos:
            return self._pos[name]
        if self._next >= len(self._cells):
            self._cells.extend(self._spiral(self.radius + 1)[1:])
        pos = self._cells[self._next]
        self._next += 1
        self._pos[name] = pos
        return pos

    def pos(self, name: str) -> tuple[int, int] | None:
        return self._pos.get(name)

    def neighbors(self, name: str, ring: int = 1) -> list[str]:
        """距离 ≤ ring 的已放置 agent (不含自身), 按距离升序。"""
        origin = self._pos.get(name)
        if origin is None:
            return []
        out: list[tuple[int, str]] = []
        for other, pos in self._pos.items():
            if other == name:
                continue
            d = hex_distance(origin, pos)
            if d <= ring:
                out.append((d, other))
        return [n for _, n in sorted(out)]

    def spread_score(self, names: Iterable[str]) -> float:
        """批内 agent 的平均两两 hex 距离 — 分散度 (M2 遥测: 越大越散)。"""
        pos = [self._pos[n] for n in names if n in self._pos]
        if len(pos) < 2:
            return 0.0
        total, cnt = 0, 0
        for i in range(len(pos)):
            for j in range(i + 1, len(pos)):
                total += hex_distance(pos[i], pos[j])
                cnt += 1
        return total / cnt


def agent_loads(pheromone: Any, agents: list[str]) -> dict[str, float]:
    """读 load 信道各 agent 忙闲 (容错: 信号缺失按 0)。"""
    loads: dict[str, float] = {}
    for a in agents:
        try:
            loads[a] = float(pheromone.level(a))
        except Exception:
            loads[a] = 0.0
    return loads


def select_batch_grid(
    ready: list,
    pheromone: Any,
    max_parallel: int,
    *,
    executor_agent: str = "cowork",
    grid: HexGrid | None = None,
    spread_ratio: float = 0.5,
) -> list:
    """邻域感知批选择 (网格调度核心)。

    - 负载梯度: agent load 信息素低者任务优先 (空闲邻域先领活);
    - 邻域分散: ≥2 个不同 agent 时, 同 agent 批内名额上限为
      ceil(n * spread_ratio), 强制跨邻域分摊 (GuaClaw 拥堵规避的单机等价);
    - 稳定排序保持 ready 原相对序 (同负载不抖动)。
    返回选中的任务子列表 (不改 ready 本身)。
    """
    n = max(1, int(max_parallel))
    if len(ready) <= 1 or pheromone is None:
        return ready[:n]
    if grid is None:
        grid = HexGrid()

    names: list[str] = []
    for t in ready:
        name = str(getattr(t, "agent", "") or executor_agent)
        if name not in names:
            names.append(name)
            grid.place(name)
    loads = agent_loads(pheromone, names)

    # 负载梯度稳定排序 (key 带 index 防跨同负载抖动)
    indexed = sorted(
        enumerate(ready),
        key=lambda pair: (loads.get(str(getattr(pair[1], "agent", "") or executor_agent), 0.0), pair[0]),
    )
    if len(names) < 2:
        return [t for _, t in indexed[:n]]

    # 邻域分散: 同 agent 名额上限
    per_agent_cap = max(1, ceil(n * spread_ratio))
    used: dict[str, int] = {}
    batch: list = []
    for _i, t in indexed:
        if len(batch) >= n:
            break
        name = str(getattr(t, "agent", "") or executor_agent)
        if used.get(name, 0) >= per_agent_cap:
            continue
        used[name] = used.get(name, 0) + 1
        batch.append(t)
    # 全部撞上限时放宽 (避免批空转: cap 导致可选不足)
    if len(batch) < n:
        chosen_ids = {id(t) for t in batch}
        for _i, t in indexed:
            if len(batch) >= n:
                break
            if id(t) not in chosen_ids:
                batch.append(t)
                chosen_ids.add(id(t))
    return batch


# ---------------------------------------------------------------------------
# QunMesh M3: 角色邻域化 (就近评审 + swarm_bft 邻域投票)
# ---------------------------------------------------------------------------

def claim_idle_agent(pool: Any, want_role: str, *, task_id: str, task_group_id: Any = None) -> tuple[Any, str]:
    """M3 动态领取 (纯函数): 同 role 无空闲 → 跨 role 取低负载实例。

    丝瓜络「谁近谁领」: 节点无角色 (实例只是执行槽位), persona 由任务携带。
    返回 (领取的实例, 事件 dict)；领取失败返回 (None, None)。失败安全 —
    任何池异常都吞掉回退 (调用方保持旧行为: 新建 engine)。
    """
    try:
        idle = sorted(
            (a for a in pool.list() if getattr(a, "is_available", False)),
            key=lambda a: (getattr(a, "load", 0.0), getattr(a, "created_at", 0.0)),
        )
        for inst in idle:
            if inst.role == want_role:
                continue
            cand = pool.acquire(inst.role, task_group_id=task_group_id, task_id=task_id)
            if cand is not None:
                event = {
                    "want_role": want_role,
                    "claimed_role": getattr(cand, "role", ""),
                    "agent": getattr(cand, "id", ""),
                    "load": getattr(cand, "load", None),
                }
                return cand, event
    except Exception:
        return None, None
    return None, None


def review_neighborhood(pheromone: Any, *, k: int = 4) -> str:
    """就近评审的邻域上下文 (纯函数, 容错; 非 StigmergyBus / 无信号 → 空串)。

    从四信道组装「评审员能看到的邻域」: result 信道 top(k) = 最近完成的任务
    及其产物摘要 (跨任务一致性对照); risk 信道 top(k) = 邻域当前风险梯度
    (评审重点提示); load 总量 = 当前并发。全部读失败返回空串 (旧行为)。
    """
    if pheromone is None:
        return ""
    try:
        lines: list[str] = []
        results = pheromone.top("result", k)
        if results:
            parts: list[str] = []
            for key, _v in results:
                payload = ""
                try:
                    payload = str(pheromone.payload_of(key, channel="result") or "")[:100]
                except Exception:
                    payload = ""
                parts.append(f"[{key}] {payload}")
            lines.append("Recent results nearby (cross-check consistency): " + " | ".join(parts))
        risks = pheromone.top("risk", k)
        if risks:
            parts2: list[str] = []
            for key, v in risks:
                reason = ""
                try:
                    reason = str(pheromone.payload_of(key, channel="risk") or "")[:80]
                except Exception:
                    reason = ""
                parts2.append(f"[{key}({v:.2f})] {reason}")
            lines.append("Risk gradient nearby (scrutinize these areas): " + " | ".join(parts2))
        if lines:
            try:
                lines.append(f"Current concurrency: {pheromone.total_load():.1f}")
            except Exception:
                pass
        return "\n".join(lines)
    except Exception:
        return ""


def bft_vote(verdicts: list) -> tuple[Any, dict]:
    """swarm_bft 邻域投票 (prepare → commit): 聚合多个 reviewer 的独立裁决。

    - accepted = 多数派结论 (平票时保守拒绝 — 宁可重跑不可放过);
    - confidence = 通过时均值 / 拒绝时取 min (保守);
    - 分歧时 reason 记录投票分布; needs_human = 任一投票方要求。
    返回 (聚合 ReviewVerdict, 遥测 dict: votes/accepted_count/consensus)。
    空 verdicts → (accepted=True, confidence=0.3) 兜底 (不阻塞蜂群)。
    """
    from .models import ReviewVerdict

    if not verdicts:
        return ReviewVerdict(accepted=True, reason="bft: no votes (fallback)", confidence=0.3), {
            "votes": 0, "accepted_count": 0, "consensus": 1.0,
        }
    accepted = [v for v in verdicts if v.accepted]
    acc_n, total = len(accepted), len(verdicts)
    # 多数决; 平票保守拒绝 (n 为偶数时可能发生)
    majority_accepted = acc_n * 2 > total
    if majority_accepted:
        conf = sum(float(v.confidence or 0) for v in accepted) / acc_n
    else:
        conf = min(float(v.confidence or 0) for v in verdicts)
    consensus = max(acc_n, total - acc_n) / total
    needs_human = any(bool(v.needs_human) for v in verdicts)
    if acc_n == total:
        reason = f"bft: unanimous accept ({acc_n}/{total})"
    elif acc_n == 0:
        reason = f"bft: unanimous reject ({total}/{total})"
    else:
        reason = f"bft: split vote accept={acc_n} reject={total - acc_n}"
    merged = ReviewVerdict(
        accepted=bool(majority_accepted),
        reason=reason,
        confidence=round(conf, 3),
        needs_human=bool(needs_human),
    )
    return merged, {"votes": total, "accepted_count": acc_n, "consensus": round(consensus, 2)}
