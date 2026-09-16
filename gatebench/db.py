"""SQLite 持久化：库表、种子数据、方案版本与摘要。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS gates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    max_opening REAL NOT NULL,
    rate REAL NOT NULL,          -- 米/步
    coef REAL NOT NULL,          -- 流量系数
    locked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id INTEGER NOT NULL REFERENCES gates(id),
    head_lo REAL NOT NULL, head_hi REAL NOT NULL,
    open_lo REAL NOT NULL, open_hi REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    head REAL NOT NULL,
    target_q REAL NOT NULL,
    inflow REAL NOT NULL DEFAULT 1000000,  -- 机组来流 m³/s（泄量上限）
    ramp REAL NOT NULL,          -- 总泄量爬升率 m³/s/步
    max_adj_diff REAL NOT NULL,  -- 相邻门开度差上限（米）
    start_openings TEXT NOT NULL,  -- JSON 数组
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | sealed
    version INTEGER NOT NULL DEFAULT 1,
    locked_seq INTEGER NOT NULL DEFAULT -1, -- 已锁定前缀的最后一步序号
    digest TEXT,                 -- 封定时计算的摘要
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_steps (
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    seq INTEGER NOT NULL,
    openings TEXT NOT NULL,      -- JSON 数组
    total_q REAL NOT NULL,
    PRIMARY KEY (plan_id, seq)
);
"""

SEED_GATES = [
    (1, "1#泄洪闸", 10.0, 0.2, 12.5, 0),
    (2, "2#泄洪闸", 10.0, 0.2, 12.5, 0),
    (3, "3#泄洪闸", 10.0, 0.2, 12.5, 0),
    (4, "4#泄洪闸", 10.0, 0.2, 12.5, 0),
]
# 不同水头区间下的禁振开度带（示例值）
SEED_BANDS = [
    (1, 10.0, 20.0, 2.0, 3.2), (1, 20.0, 35.0, 4.0, 5.5),
    (2, 10.0, 20.0, 2.4, 3.6), (2, 20.0, 35.0, 4.2, 5.8),
    (3, 10.0, 20.0, 1.8, 3.0), (3, 20.0, 35.0, 3.8, 5.2),
    (4, 10.0, 20.0, 2.2, 3.4), (4, 20.0, 35.0, 4.4, 6.0),
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """对既有库做就地迁移：plans 增加机组来流列（默认足够大，不改变旧方案语义）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
    if "inflow" not in cols:
        conn.execute(
            "ALTER TABLE plans ADD COLUMN inflow REAL NOT NULL DEFAULT 1000000")
        conn.commit()


def seed_if_empty(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM gates").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO gates(id,name,max_opening,rate,coef,locked) VALUES(?,?,?,?,?,?)",
            SEED_GATES)
        conn.executemany(
            "INSERT INTO bands(gate_id,head_lo,head_hi,open_lo,open_hi) VALUES(?,?,?,?,?)",
            SEED_BANDS)
        conn.commit()


def plan_digest(plan: sqlite3.Row, steps: list[sqlite3.Row]) -> str:
    """封定摘要：对方案参数与全部步骤的规范化表示取 SHA-256。

    只依赖封定时的参数与步骤内容，不含时间戳，因此回放时重算结果稳定。
    """
    payload = {
        "name": plan["name"],
        "head": plan["head"],
        "target_q": plan["target_q"],
        "inflow": plan["inflow"],
        "ramp": plan["ramp"],
        "max_adj_diff": plan["max_adj_diff"],
        "start_openings": json.loads(plan["start_openings"]),
        "steps": [
            {"seq": s["seq"], "openings": json.loads(s["openings"]),
             "total_q": round(s["total_q"], 3)}
            for s in sorted(steps, key=lambda r: r["seq"])
        ],
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class VersionConflict(Exception):
    def __init__(self, current_version: int):
        super().__init__("version conflict")
        self.current_version = current_version


class SealedError(Exception):
    pass


def check_version(conn: sqlite3.Connection, plan_id: int, base_version: int) -> sqlite3.Row:
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise KeyError(plan_id)
    if plan["version"] != base_version:
        raise VersionConflict(plan["version"])
    return plan


def bump_version(conn: sqlite3.Connection, plan_id: int) -> int:
    conn.execute("UPDATE plans SET version=version+1 WHERE id=?", (plan_id,))
    return conn.execute("SELECT version FROM plans WHERE id=?", (plan_id,)).fetchone()[0]


def now() -> float:
    return time.time()
