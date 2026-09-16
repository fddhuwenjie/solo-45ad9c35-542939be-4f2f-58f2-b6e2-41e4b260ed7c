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
                 inflow: float = math.inf, relax: str | None = None,
                 escape: bool = False):
        self.gates = gates
        self.head = head
        self.ramp = ramp
        self.max_adj_diff = max_adj_diff
        self.relax = relax
        self.inflow = math.inf if relax == "inflow" else inflow
        self.sqrt_h = math.sqrt(head)
        self.max_grid = [int(round(g.max_opening / GRID)) for g in gates]
        # 速率换算为网格步数必须向下取整：任何闸门都不得因 0.1m 网格
        # 而突破自身速率限制；速率为 0（或不足一个网格步）时步数为 0，
        # 该门全程保持原开度。
        self.rate_steps = [max(0, int(math.floor(g.rate / GRID + 1e-9)))
                           for g in gates]

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

        # 把每门钳制到起点所在的连通区间（禁振带不可穿越）；
        # 检修锁定门与零速率门（无法移动）直接钳制到起点。
        # 逃逸模式（起点本身非法）不做区间钳制：可动门全范围移动，
        # 合法性改由逃逸搜索的目标判定负责。
        self.lo = [0] * len(gates)
        self.hi = list(self.max_grid)
        for k in range(len(gates)):
            fixed = ((gates[k].locked and relax != "maintenance")
                     or (self.rate_steps[k] == 0 and relax != "rate"))
            if fixed:
                self.lo[k] = self.hi[k] = start[k]
                continue
            if escape:
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
        # 诊断松弛速率时按 4 倍速率近似"取消速率限制"，零速率门给 4 步）。
        # 细步长同样逐门受速率约束，零速率门在任何组合中都保持不动。
        r_of = [self.rate_steps[k] if relax != "rate"
                else (self.rate_steps[k] * 4 if self.rate_steps[k] > 0 else 4)
                for k in range(len(gates))]

        def build(movesets):
            return [c for c in itertools.product(*movesets)
                    if any(d != 0 for d in c)]

        self._coarse_combos = build([(-r, 0, r) for r in r_of])
        self._fine_combos = (self._coarse_combos
                             + build([(min(1, r), 0, -min(1, r))
                                      for r in r_of]))
        # 相邻门开度差检查的门对（网格步数上限）
        self._pairs = [(k, k + 1) for k in range(len(gates) - 1)]
        self._max_adj_grid = max_adj_diff / GRID
        self.escape = escape

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
        """生成满足全部单步约束的后继（泄量爬升率、机组来流上限、
        相邻开度差在此内联检查）。"""
        combos = (self._fine_combos
                  if abs(q - target_q) <= 2 * self.ramp else self._coarse_combos)
        lo, hi, qc = self.lo, self.hi, self.q_contrib
        ramp = self.ramp if self.relax != "ramp" else math.inf
        # 逃逸模式下来流上限只约束目标状态，不约束途经状态
        # （当前物理状态的泄量本身可能已超来流）
        inflow = math.inf if self.escape else self.inflow
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
            if not ok or abs(nq - q) > ramp + 1e-9 or nq > inflow + 1e-9:
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
    ("inflow", "机组来流：目标泄量超过机组来流可供能力"),
    ("maintenance", "检修锁定：锁定门开度固定，目标泄量在当前可调范围内无法达成"),
    ("band", "禁振开度带：目标泄量对应开度落在禁振带内，或起终点被禁振带隔离"),
    ("adj_diff", "相邻门开度差限制：满足开度差约束的状态空间内不存在可达目标的路径"),
    ("ramp", "总泄量爬升率限制：在扩展节点上限内爬升率约束使路径不可达"),
    ("rate", "单门速率限制：单门变幅不足以在限定的状态空间内到达目标"),
]


def _diagnose(gates, bands, head, start, target_q, ramp, max_adj_diff, inflow):
    """逐一松弛约束，返回 (首个不可满足约束key, 说明)。"""
    for key, msg in _RELAX_ORDER:
        prob = _Problem(gates, bands, head, ramp, max_adj_diff, start,
                        inflow=inflow, relax=key)
        lo, hi = prob.q_range()
        hi_eff = min(hi, prob.inflow)
        if not (lo - Q_TOL <= target_q <= hi_eff + Q_TOL):
            continue  # 松弛后泄量区间仍不含目标，换下一约束
        path, _, _, _ = _astar(prob, start, target_q)
        if path is not None:
            return key, msg
    return "target", "即使松弛全部运行约束仍不可达：目标泄量超出机组来流能力范围。"


def _escape_to_legal(prob: _Problem, start: tuple[int, ...],
                     max_expand: int = 50_000):
    """从非法起点（禁振带内/相邻差违规）搜索最近的合法状态。

    合法 = 各门出禁振带 + 相邻开度差 + 总泄量 ≤ 机组来流；
    移动受单门速率与检修锁定约束（不可动门保持原开度）。
    代价为 L1 开度距离（各门移动网格数之和），保证"最近"。
    返回 (state, q)；不存在满足全部约束的可达状态时返回 (None, None)。
    """
    def legal(state, q):
        if q > prob.inflow + 1e-9:
            return False
        if any(prob.in_band[k][state[k]] for k in range(len(state))):
            return False
        return prob.state_ok(state)

    def h(state):
        # 各在带门移到最近带外网格的距离之和（对 L1 代价可采纳）
        d = 0
        for k, s in enumerate(state):
            if not prob.in_band[k][s]:
                continue
            up = s
            while up <= prob.max_grid[k] and prob.in_band[k][up]:
                up += 1
            down = s
            while down >= 0 and prob.in_band[k][down]:
                down -= 1
            d += min(up - s if up <= prob.max_grid[k] else 10 ** 6,
                     s - down if down >= 0 else 10 ** 6)
        return float(d)

    start_q = prob.q_of(start)
    # 不可动门（锁定/零速率，lo==hi==start）本身在带内 → 必然无解
    for k in range(len(start)):
        if (prob.in_band[k][start[k]]
                and prob.lo[k] == prob.hi[k] == start[k]):
            return None, None
    if legal(start, start_q):
        return start, start_q
    counter = itertools.count()
    heap = [(h(start), 0.0, next(counter), start, start_q)]
    best = {start: 0.0}
    expanded = 0
    while heap and expanded < max_expand:
        _, g, _, state, q = heapq.heappop(heap)
        if g > best.get(state, math.inf):
            continue
        expanded += 1
        if legal(state, q):
            return state, q
        for nxt, nq in prob.neighbors(state, q, target_q=q):
            ng = g + sum(abs(a - b) for a, b in zip(nxt, state))
            if ng < best.get(nxt, math.inf):
                best[nxt] = ng
                heapq.heappush(heap, (ng + h(nxt), ng, next(counter), nxt, nq))
    return None, None


def _diagnose_escape(gates, bands, head, start, ramp, max_adj_diff, inflow):
    """逃逸失败时报告首个不可满足约束（确定性顺序）。"""
    probe = _Problem(gates, bands, head, ramp, max_adj_diff, start,
                     inflow=inflow, escape=True)
    # 最具体的原因优先：不可动门（检修锁定/零速率）本身就在禁振带内
    for k, g in enumerate(gates):
        if probe.in_band[k][start[k]]:
            if g.locked:
                return ("maintenance",
                        f"检修锁定：{g.name} 锁定在禁振带内，无法移出，"
                        "请先解除锁定或人工处置")
            if probe.rate_steps[k] == 0:
                return ("rate",
                        f"单门速率限制：{g.name} 速率为 0 且处于禁振带内，"
                        "无法自动移出，请人工处置")
    # 其次按统一松弛顺序诊断（来流、相邻差等）
    for key, msg in _RELAX_ORDER:
        prob = _Problem(gates, bands, head, ramp, max_adj_diff, start,
                        inflow=inflow, relax=key, escape=True)
        legal, _ = _escape_to_legal(prob, start)
        if legal is not None:
            return key, msg
    return "start", "不存在满足全部约束的可达合法状态，请人工检查闸门与来流。"


def _nearest_under_inflow(prob: _Problem) -> tuple[tuple[int, ...], float]:
    """机组来流受限时的最近可达状态：从各门上限均衡下调至 Q ≤ 来流。"""
    n = len(prob.lo)
    st = [prob.hi[k] for k in range(n)]
    q = prob.q_of(tuple(st))
    if q <= prob.inflow + 1e-9:
        return tuple(st), q
    limit = prob._max_adj_grid
    while q > prob.inflow + 1e-9:
        moved = False
        # 优先降当前开度最大的门，保持各门均衡（不破坏相邻差）
        for k in sorted(range(n), key=lambda k: -st[k]):
            if st[k] <= prob.lo[k]:
                continue
            if prob.relax != "adj_diff" and any(
                    abs((st[k] - 1) - st[j]) > limit + 1e-9
                    for j in (k - 1, k + 1) if 0 <= j < n):
                continue
            st[k] -= 1
            q -= prob.q_contrib[k][st[k] + 1] - prob.q_contrib[k][st[k]]
            moved = True
            break
        if not moved:
            break
    return tuple(st), q


def find_path(gates: list[Gate], bands: list[Band], head: float,
              start_openings, target_q: float, ramp: float,
              max_adj_diff: float, inflow: float = math.inf) -> SearchResult:
    start = _to_grid(start_openings)
    prob = _Problem(gates, bands, head, ramp, max_adj_diff, start, inflow=inflow)
    start_bad = (any(prob.in_band[k][start[k]] for k in range(len(start)))
                 or not prob.state_ok(start))
    if start_bad:
        # 当前开度非法：在全部约束（速率/锁定/来流/相邻差/禁振带）下
        # 搜索最近的合法可达状态作为调整建议
        esc = _Problem(gates, bands, head, ramp, max_adj_diff, start,
                       inflow=inflow, escape=True)
        legal, legal_q = _escape_to_legal(esc, start)
        if legal is not None:
            return SearchResult(
                ok=False, first_violated="start",
                detail="当前开度处于禁振带内或违反相邻差约束，"
                       "请先调整至最近的合法开度。",
                nearest_state=list(_to_meters(legal)),
                nearest_q=round(legal_q, 3))
        # 不存在满足全部约束的可达合法状态：不得给出违规建议
        key, msg = _diagnose_escape(gates, bands, head, start,
                                    ramp, max_adj_diff, inflow)
        return SearchResult(ok=False, first_violated=key, detail=msg)

    lo_q, hi_q = prob.q_range()
    hi_eff = min(hi_q, prob.inflow)
    if not (lo_q - Q_TOL <= target_q <= hi_eff + Q_TOL):
        # 快速失败：目标在可达泄量区间之外，最近可达状态为区间端点
        if target_q < lo_q:
            nearest = tuple(prob.lo[k] for k in range(len(start)))
            nearest_q = prob.q_of(nearest)
        elif prob.inflow < hi_q:
            nearest, nearest_q = _nearest_under_inflow(prob)
        else:
            nearest = tuple(prob.hi[k] for k in range(len(start)))
            nearest_q = hi_q
        key, msg = _diagnose(gates, bands, head, start, target_q,
                             ramp, max_adj_diff, inflow)
        return SearchResult(
            ok=False, first_violated=key, detail=msg,
            nearest_state=list(_to_meters(nearest)),
            nearest_q=round(nearest_q, 3))

    path, expanded, nearest, nearest_q = _astar(prob, start, target_q)
    if path is not None:
        return SearchResult(
            ok=True, expanded=expanded,
            path=[_to_meters(s) for s in path],
            discharges=[round(prob.q_of(s), 3) for s in path])
    key, msg = _diagnose(gates, bands, head, start, target_q,
                         ramp, max_adj_diff, inflow)
    return SearchResult(
        ok=False, expanded=expanded, first_violated=key, detail=msg,
        nearest_state=list(_to_meters(nearest)), nearest_q=round(nearest_q, 3))
