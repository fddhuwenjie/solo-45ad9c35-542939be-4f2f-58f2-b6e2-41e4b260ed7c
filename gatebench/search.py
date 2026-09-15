"""闸门群开度状态搜索（A*）。

状态：各闸门开度离散到 GRID 网格后的整数元组。
约束（任何中间状态都不得违反）：
  1. 禁振开度带：状态不得落在带内；由于单步变幅远小于带宽，带把每门开度
     范围分割为互不连通的区间，搜索前按起点把每门钳制到所在连通区间，
     从而天然杜绝"穿越禁振带"的转移。
  2. 总泄量爬升率：相邻两步总泄量之差不得超过 ramp。
  3. 单门速率限制：单步开度变化不得超过 rate（米/步）。
  4. 相邻门开度差：任意相邻两门开度差不得超过 max_adj_diff。
  5. 检修锁定：锁定门开度全程固定。
目标泄量落在钳制后可达泄量区间之外时即时判定失败；否则 A* 搜索。
失败时逐一松弛约束做诊断，报告首个不可满足约束与最近可达状态。
"""
from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field

GRID = 0.1          # 开度离散步长（米）
Q_TOL = 5.0         # 目标泄量容差（m³/s）
MAX_EXPAND = 60_000  # 单次搜索最大扩展节点数


@dataclass
class Gate:
    id: int
    name: str
    max_opening: float      # 最大开度（米）
    rate: float             # 每步最大变幅（米/步）
    coef: float             # 流量系数，Q = coef * e * sqrt(head)
    locked: bool = False    # 检修锁定


@dataclass
class Band:
    gate_id: int
    head_lo: float
    head_hi: float
    open_lo: float          # 禁振开度下界（米）
    open_hi: float          # 禁振开度上界（米）

    def applies(self, head: float) -> bool:
        return self.head_lo <= head <= self.head_hi


def discharge(openings, head: float, gates: list[Gate]) -> float:
    """总泄量 Q = Σ coef_i * e_i * sqrt(head)。"""
    return sum(g.coef * e * math.sqrt(head) for g, e in zip(gates, openings))


def _to_grid(openings) -> tuple[int, ...]:
    return tuple(int(round(e / GRID)) for e in openings)


def _to_meters(state: tuple[int, ...]) -> tuple[float, ...]:
    return tuple(round(s * GRID, 4) for s in state)


@dataclass
class SearchResult:
    ok: bool
    path: list[tuple[float, ...]] = field(default_factory=list)   # 含起点的开度序列
    discharges: list[float] = field(default_factory=list)
    expanded: int = 0
    # 失败诊断
    nearest_state: list[float] | None = None      # 距目标泄量最近的可达状态
    nearest_q: float | None = None
    first_violated: str | None = None             # 首个不可满足约束
    detail: str = ""


class _Problem:
    """一次搜索的约束上下文；relax 用于诊断时松弛某类约束。"""

    def __init__(self, gates, bands, head, ramp, max_adj_diff, start,
                 relax: str | None = None):
        self.gates = gates
        self.head = head
        self.ramp = ramp
        self.max_adj_diff = max_adj_diff
        self.relax = relax
        self.sqrt_h = math.sqrt(head)
        self.max_grid = [int(round(g.max_opening / GRID)) for g in gates]
        self.rate_steps = [max(1, int(round(g.rate / GRID))) for g in gates]

        # 每门网格级流量贡献与禁振带标记
        self.q_contrib = [[g.coef * i * GRID * self.sqrt_h
                           for i in range(self.max_grid[k] + 1)]
                          for k, g in enumerate(gates)]
        self.in_band = [[False] * (self.max_grid[k] + 1)
                        for k in range(len(gates))]
        if relax != "band":
            for b in bands:
                if not b.applies(head):
                    continue
                for k, g in enumerate(gates):
                    if g.id != b.gate_id:
                        continue
                    lo = int(math.ceil(b.open_lo / GRID - 1e-9))
                    hi = int(math.floor(b.open_hi / GRID + 1e-9))
                    for i in range(max(lo, 0), min(hi, self.max_grid[k]) + 1):
                        self.in_band[k][i] = True

        # 把每门钳制到起点所在的连通区间（禁振带不可穿越）
        self.lo = [0] * len(gates)
        self.hi = list(self.max_grid)
        for k in range(len(gates)):
            if gates[k].locked and relax != "maintenance":
                self.lo[k] = self.hi[k] = start[k]
                continue
            s = start[k]
            lo, hi = 0, self.max_grid[k]
            for i in range(s - 1, -1, -1):
                if self.in_band[k][i]:
                    lo = i + 1
                    break
            for i in range(s + 1, self.max_grid[k] + 1):
                if self.in_band[k][i]:
                    hi = i - 1
                    break
            self.lo[k], self.hi[k] = lo, hi

        # 预构建与状态无关的步长组合（远离目标用粗步长；接近目标时
        # 粗步长 ∪ ±1 细步长——泄量分辨率足够，组合数从 5^n 降到约 2·3^n；
        # 诊断松弛速率时按 4 倍速率近似"取消速率限制"）
        r_of = [self.rate_steps[k] if relax != "rate" else self.rate_steps[k] * 4
                for k in range(len(gates))]

        def build(movesets):
            return [c for c in itertools.product(*movesets)
                    if any(d != 0 for d in c)]

        self._coarse_combos = build([(-r, 0, r) for r in r_of])
        self._fine_combos = (self._coarse_combos
                             + build([(-1, 0, 1)] * len(gates)))
        # 相邻门开度差检查的门对（网格步数上限）
        self._pairs = [(k, k + 1) for k in range(len(gates) - 1)]
        self._max_adj_grid = max_adj_diff / GRID

    def q_of(self, state: tuple[int, ...]) -> float:
        return sum(self.q_contrib[k][state[k]] for k in range(len(state)))

    def state_ok(self, state: tuple[int, ...]) -> bool:
        if self.relax != "adj_diff":
            for i in range(len(state) - 1):
                if abs(state[i] - state[i + 1]) * GRID > self.max_adj_diff + 1e-9:
                    return False
        return True  # 禁振带已由区间钳制保证

    def q_range(self) -> tuple[float, float]:
        """钳制后可达泄量的外 bounds（忽略相邻差耦合）。"""
        lo = sum(self.q_contrib[k][self.lo[k]] for k in range(len(self.lo)))
        hi = sum(self.q_contrib[k][self.hi[k]] for k in range(len(self.hi)))
        return lo, hi

    def neighbors(self, state: tuple[int, ...], q: float, target_q: float):
        """生成满足全部单步约束的后继（泄量爬升率、相邻开度差在此内联检查）。"""
        combos = (self._fine_combos
                  if abs(q - target_q) <= 2 * self.ramp else self._coarse_combos)
        lo, hi, qc = self.lo, self.hi, self.q_contrib
        ramp = self.ramp if self.relax != "ramp" else math.inf
        check_adj = self.relax != "adj_diff"
        max_adj = self._max_adj_grid
        for deltas in combos:
            nq = q
            nxt = []
            ok = True
            for k, d in enumerate(deltas):
                n = state[k] + d
                if n < lo[k] or n > hi[k]:
                    ok = False
                    break
                nxt.append(n)
                nq += qc[k][n] - qc[k][state[k]]
            if not ok or abs(nq - q) > ramp + 1e-9:
                continue
            if check_adj:
                for a, b in self._pairs:
                    if abs(nxt[a] - nxt[b]) > max_adj + 1e-9:
                        ok = False
                        break
            if ok:
                yield tuple(nxt), nq


def _astar(prob: _Problem, start: tuple[int, ...], target_q: float):
    """返回 (path_states, expanded, nearest_state, nearest_q)；失败时 path 为 None。"""
    start_q = prob.q_of(start)
    h = lambda q: abs(q - target_q) / prob.ramp if prob.ramp > 0 else 0.0
    counter = itertools.count()
    open_heap = [(h(start_q), 0.0, next(counter), start, start_q)]
    best_g = {start: 0.0}
    came: dict[tuple[int, ...], tuple[int, ...] | None] = {start: None}
    nearest, nearest_q = start, start_q
    expanded = 0

    while open_heap and expanded < MAX_EXPAND:
        _, g, _, state, q = heapq.heappop(open_heap)
        if g > best_g.get(state, math.inf):
            continue
        expanded += 1
        if abs(q - target_q) < abs(nearest_q - target_q):
            nearest, nearest_q = state, q
        if abs(q - target_q) <= Q_TOL:
            path = []
            s: tuple[int, ...] | None = state
            while s is not None:
                path.append(s)
                s = came[s]
            path.reverse()
            return path, expanded, nearest, nearest_q
        for nxt, nq in prob.neighbors(state, q, target_q):
            ng = g + 1.0
            if ng < best_g.get(nxt, math.inf):
                best_g[nxt] = ng
                came[nxt] = state
                heapq.heappush(open_heap, (ng + h(nq), ng, next(counter), nxt, nq))
    return None, expanded, nearest, nearest_q


# 诊断时的松弛顺序：先报告的即"首个"不可满足约束
_RELAX_ORDER = [
    ("maintenance", "检修锁定：锁定门开度固定，目标泄量在当前可调范围内无法达成"),
    ("band", "禁振开度带：目标泄量对应开度落在禁振带内，或起终点被禁振带隔离"),
    ("adj_diff", "相邻门开度差限制：满足开度差约束的状态空间内不存在可达目标的路径"),
    ("ramp", "总泄量爬升率限制：在扩展节点上限内爬升率约束使路径不可达"),
    ("rate", "单门速率限制：单门变幅不足以在限定的状态空间内到达目标"),
]


def _diagnose(gates, bands, head, start, target_q, ramp, max_adj_diff):
    """逐一松弛约束，返回 (首个不可满足约束key, 说明)。"""
    for key, msg in _RELAX_ORDER:
        prob = _Problem(gates, bands, head, ramp, max_adj_diff, start, relax=key)
        lo, hi = prob.q_range()
        if not (lo - Q_TOL <= target_q <= hi + Q_TOL):
            continue  # 松弛后泄量区间仍不含目标，换下一约束
        path, _, _, _ = _astar(prob, start, target_q)
        if path is not None:
            return key, msg
    return "target", "即使松弛全部运行约束仍不可达：目标泄量超出机组来流能力范围。"


def find_path(gates: list[Gate], bands: list[Band], head: float,
              start_openings, target_q: float, ramp: float,
              max_adj_diff: float) -> SearchResult:
    start = _to_grid(start_openings)
    prob = _Problem(gates, bands, head, ramp, max_adj_diff, start)
    if any(prob.in_band[k][start[k]] for k in range(len(start))):
        return SearchResult(ok=False, first_violated="start",
                            detail="当前开度处于禁振带内，请先人工调整出禁振区。")
    if not prob.state_ok(start):
        return SearchResult(ok=False, first_violated="start",
                            detail="当前开度违反相邻门开度差约束，请先人工调整。")

    lo_q, hi_q = prob.q_range()
    if not (lo_q - Q_TOL <= target_q <= hi_q + Q_TOL):
        # 快速失败：目标在可达泄量区间之外，最近可达状态为区间端点
        below = target_q < lo_q
        nearest = tuple(prob.lo[k] if below else prob.hi[k]
                        for k in range(len(start)))
        key, msg = _diagnose(gates, bands, head, start, target_q,
                             ramp, max_adj_diff)
        return SearchResult(
            ok=False, first_violated=key, detail=msg,
            nearest_state=list(_to_meters(nearest)),
            nearest_q=round(prob.q_of(nearest), 3))

    path, expanded, nearest, nearest_q = _astar(prob, start, target_q)
    if path is not None:
        return SearchResult(
            ok=True, expanded=expanded,
            path=[_to_meters(s) for s in path],
            discharges=[round(prob.q_of(s), 3) for s in path])
    key, msg = _diagnose(gates, bands, head, start, target_q, ramp, max_adj_diff)
    return SearchResult(
        ok=False, expanded=expanded, first_violated=key, detail=msg,
        nearest_state=list(_to_meters(nearest)), nearest_q=round(nearest_q, 3))
