"""端到端测试：搜索约束、失败诊断、锁定前缀、版本冲突、封定回放。"""
import json
import math
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from gatebench import db, plans, search
from gatebench.app import make_handler

HEAD = 15.0
RAMP = 30.0
ADJ = 2.0


def make_gates(locked=()):
    return [search.Gate(id=i, name=f"{i}#", max_opening=10.0, rate=0.2,
                        coef=12.5, locked=(i in locked)) for i in range(1, 5)]


def make_bands():
    return [search.Band(gate_id=g, head_lo=10.0, head_hi=20.0,
                        open_lo=lo, open_hi=hi)
            for g, lo, hi in [(1, 2.0, 3.2), (2, 2.4, 3.6),
                              (3, 1.8, 3.0), (4, 2.2, 3.4)]]


def q_of(openings):
    return sum(12.5 * e * math.sqrt(HEAD) for e in openings)


class SearchTest(unittest.TestCase):
    def check_path(self, res, start, target):
        self.assertTrue(res.ok)
        path, qs = res.path, res.discharges
        self.assertEqual(list(path[0]), list(start))
        self.assertAlmostEqual(qs[-1], target, delta=search.Q_TOL)
        bands = make_bands()
        for k, (opens, q) in enumerate(zip(path, qs)):
            # 禁振带：任何状态不得落入带内
            for i, e in enumerate(opens):
                for b in bands:
                    if b.gate_id == i + 1:
                        self.assertFalse(b.open_lo <= e <= b.open_hi,
                                         f"step {k} gate {i+1} in band: {e}")
            # 相邻门开度差
            for i in range(len(opens) - 1):
                self.assertLessEqual(abs(opens[i] - opens[i + 1]), ADJ + 1e-6)
            if k == 0:
                continue
            prev = path[k - 1]
            # 单门速率
            for a, b in zip(prev, opens):
                self.assertLessEqual(abs(a - b), 0.2 + 1e-6)
            # 总泄量爬升率
            self.assertLessEqual(abs(q - qs[k - 1]), RAMP + 1e-6)
            # 不得一步跨越整个禁振带
            for i, (a, b) in enumerate(zip(prev, opens)):
                for band in bands:
                    if band.gate_id == i + 1:
                        lo, hi = min(a, b), max(a, b)
                        self.assertFalse(lo < band.open_lo and hi > band.open_hi)

    def test_feasible_path(self):
        res = search.find_path(make_gates(), make_bands(), HEAD,
                               [4.0, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ)
        self.check_path(res, [4.0, 4.0, 4.0, 4.0], 900.0)

    def test_band_blocks(self):
        # 目标泄量 405 对应开度约 2.1m，位于禁振带（约 1.8~3.6m）内，
        # 而各门起点 4.0m 在带上方且不得穿越 → 首报禁振带约束
        res = search.find_path(make_gates(), make_bands(), HEAD,
                               [4.0, 4.0, 4.0, 4.0], 405.0, RAMP, ADJ)
        self.assertFalse(res.ok)
        self.assertEqual(res.first_violated, "band")
        self.assertIsNotNone(res.nearest_state)
        # 最近可达状态不得落入禁振带
        for i, e in enumerate(res.nearest_state):
            for b in make_bands():
                if b.gate_id == i + 1:
                    self.assertFalse(b.open_lo <= e <= b.open_hi)

    def test_maintenance_lock_blocks(self):
        # 全部检修锁定 → 任何不同目标都不可达，首报 maintenance
        res = search.find_path(make_gates(locked=(1, 2, 3, 4)), make_bands(),
                               HEAD, [4.0, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ)
        self.assertFalse(res.ok)
        self.assertEqual(res.first_violated, "maintenance")
        self.assertEqual(res.nearest_state, [4.0, 4.0, 4.0, 4.0])

    def test_target_beyond_capacity(self):
        res = search.find_path(make_gates(), make_bands(), HEAD,
                               [1.0, 1.0, 1.0, 1.0], 99999.0, RAMP, ADJ)
        self.assertFalse(res.ok)
        self.assertEqual(res.first_violated, "target")


class InflowTest(unittest.TestCase):
    """机组来流必须实际参与路径计算。"""

    def test_inflow_blocks_target(self):
        # 来流 880 < 目标 900（机组能力足够）：首报机组来流，最近状态 ≤ 来流
        res = search.find_path(make_gates(), make_bands(), HEAD,
                               [4.0, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ,
                               inflow=880.0)
        self.assertFalse(res.ok)
        self.assertEqual(res.first_violated, "inflow")
        self.assertIsNotNone(res.nearest_state)
        self.assertLessEqual(res.nearest_q, 880.0 + 1e-6)

    def test_inflow_feasible_respected(self):
        # 来流 950 ≥ 目标 900：路径可行，且每一步泄量都不得超越来流
        res = search.find_path(make_gates(), make_bands(), HEAD,
                               [4.0, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ,
                               inflow=950.0)
        self.assertTrue(res.ok)
        for q in res.discharges:
            self.assertLessEqual(q, 950.0 + 1e-6)


class RateGridTest(unittest.TestCase):
    """速率取整：任何闸门不得因 0.1m 网格突破自身速率限制。"""

    def test_zero_rate_keeps_opening(self):
        gates = make_gates()
        gates[1].rate = 0.0  # 2#门零速率：全程必须保持原开度
        res = search.find_path(gates, make_bands(), HEAD,
                               [4.0, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ,
                               inflow=5000.0)
        self.assertTrue(res.ok)
        for opens in res.path:
            self.assertAlmostEqual(opens[1], 4.0, places=9)

    def test_sub_grid_rate_means_fixed(self):
        gates = make_gates()
        gates[0].rate = 0.05  # 不足一个网格步：不允许进位成 0.1m/步
        res = search.find_path(gates, make_bands(), HEAD,
                               [4.0, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ,
                               inflow=5000.0)
        self.assertTrue(res.ok)
        for opens in res.path:
            self.assertAlmostEqual(opens[0], 4.0, places=9)

    def test_rate_floor_not_round(self):
        gates = make_gates()
        gates[0].rate = 0.15  # 网格化后每步最多 0.1m，不得进位到 0.2m
        res = search.find_path(gates, make_bands(), HEAD,
                               [4.0, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ,
                               inflow=5000.0)
        self.assertTrue(res.ok)
        for prev, cur in zip(res.path, res.path[1:]):
            self.assertLessEqual(abs(cur[0] - prev[0]), 0.15 + 1e-9)


class BandInteriorStartTest(unittest.TestCase):
    """当前开度落在禁振带内：返回最近合法可达状态，诊断字段齐全。"""

    def test_start_inside_band(self):
        res = search.find_path(make_gates(), make_bands(), HEAD,
                               [2.6, 4.0, 4.0, 4.0], 900.0, RAMP, ADJ,
                               inflow=5000.0)  # 1#门 2.6m 处于禁振带 2.0~3.2 内
        self.assertFalse(res.ok)
        self.assertEqual(res.first_violated, "start")
        self.assertIsNotNone(res.nearest_state)
        self.assertIsNotNone(res.nearest_q)
        # 最近合法状态不得落在任何适用禁振带内
        for i, e in enumerate(res.nearest_state):
            for b in make_bands():
                if b.gate_id == i + 1:
                    self.assertFalse(b.open_lo <= e <= b.open_hi,
                                     f"gate {i+1} still in band: {e}")
        # 且应满足相邻门开度差
        for i in range(len(res.nearest_state) - 1):
            self.assertLessEqual(
                abs(res.nearest_state[i] - res.nearest_state[i + 1]), ADJ + 1e-6)


class ServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect(":memory:")
        db.seed_if_empty(cls.conn)
        cls.lock = threading.Lock()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        make_handler(cls.conn, cls.lock))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def api(self, path, method="GET", body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def new_plan(self, target=900.0, inflow=5000.0):
        code, d = self.api("/api/plans", "POST", {
            "name": "t", "head": HEAD, "target_q": target, "inflow": inflow,
            "ramp": RAMP, "max_adj_diff": ADJ,
            "start_openings": [4.0, 4.0, 4.0, 4.0], "created_by": "tester"})
        self.assertEqual(code, 201)
        return d["id"]


class PlanFlowTest(ServerTestBase):
    def test_solve_lock_prefix_and_conflict(self):
        pid = self.new_plan()
        # 两个"用户"同时拿到 v1
        code, r1 = self.api(f"/api/plans/{pid}/solve", "POST", {"base_version": 1})
        self.assertEqual(code, 200)
        self.assertTrue(r1["ok"])
        v_after_first = r1["version"]
        # 后提交者基于过期版本 → 409 版本冲突
        code, r2 = self.api(f"/api/plans/{pid}/solve", "POST", {"base_version": 1})
        self.assertEqual(code, 409)
        self.assertEqual(r2["error"], "version_conflict")
        self.assertEqual(r2["current_version"], v_after_first)

        # 锁定第 2 步后重新推演：前缀必须保留
        code, lk = self.api(f"/api/plans/{pid}/lock", "POST",
                            {"seq": 2, "base_version": v_after_first})
        self.assertEqual(code, 200)
        prefix_before = [s["openings"] for s in r1["steps"][:3]]
        code, r3 = self.api(f"/api/plans/{pid}/solve", "POST",
                            {"base_version": lk["version"]})
        self.assertEqual(code, 200)
        self.assertTrue(r3["ok"])
        self.assertEqual(r3["locked_prefix"], 3)
        self.assertEqual([s["openings"] for s in r3["steps"][:3]], prefix_before)

    def test_seal_replay_digest_stable(self):
        pid = self.new_plan()
        code, r = self.api(f"/api/plans/{pid}/solve", "POST", {"base_version": 1})
        self.assertTrue(r["ok"])
        n = len(r["steps"])
        code, sealed = self.api(f"/api/plans/{pid}/seal", "POST",
                                {"base_version": r["version"]})
        self.assertEqual(code, 200)
        digest = sealed["digest"]
        # 封定后禁止再推演
        code, r2 = self.api(f"/api/plans/{pid}/solve", "POST",
                            {"base_version": sealed["version"]})
        self.assertEqual(code, 409)
        self.assertEqual(r2["error"], "sealed")
        # 逐步回放：摘要稳定
        for seq in (0, n // 2, n - 1):
            code, rp = self.api(f"/api/plans/{pid}/replay/{seq}")
            self.assertEqual(code, 200)
            self.assertEqual(rp["digest"], digest)
            self.assertTrue(rp["digest_stable"])
            self.assertEqual(rp["seq"], seq)
        code, _ = self.api(f"/api/plans/{pid}/replay/{n}")
        self.assertEqual(code, 400)

    def test_failure_diagnostics_via_api(self):
        pid = self.new_plan(target=99999.0)
        code, r = self.api(f"/api/plans/{pid}/solve", "POST", {"base_version": 1})
        self.assertEqual(code, 200)
        self.assertFalse(r["ok"])
        self.assertEqual(r["first_violated"], "target")
        self.assertIn("nearest_state", r)
        self.assertIn("nearest_q", r)

    def test_inflow_via_api(self):
        # 机组来流贯通到方案数据与求解：来流 880 < 目标 900 → 首报 inflow
        pid = self.new_plan(target=900.0, inflow=880.0)
        code, r = self.api(f"/api/plans/{pid}/solve", "POST", {"base_version": 1})
        self.assertEqual(code, 200)
        self.assertFalse(r["ok"])
        self.assertEqual(r["first_violated"], "inflow")
        self.assertLessEqual(r["nearest_q"], 880.0 + 1e-6)
        # 方案详情应携带来流字段
        code, detail = self.api(f"/api/plans/{pid}")
        self.assertEqual(detail["plan"]["inflow"], 880.0)

    def test_maintenance_toggle(self):
        code, g = self.api("/api/gates/1/maintenance", "POST", {"locked": True})
        self.assertEqual(code, 200)
        self.assertTrue(g["locked"])
        code, gates = self.api("/api/gates")
        self.assertTrue(gates["gates"][0]["locked"])
        self.api("/api/gates/1/maintenance", "POST", {"locked": False})


if __name__ == "__main__":
    unittest.main()
