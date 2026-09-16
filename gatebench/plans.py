"""方案业务逻辑：求解（保留锁定前缀）、封定、回放。"""
from __future__ import annotations

import json
import sqlite3

from . import db, search


def load_problem(conn: sqlite3.Connection, plan: sqlite3.Row):
    gates = [
        search.Gate(id=g["id"], name=g["name"], max_opening=g["max_opening"],
                    rate=g["rate"], coef=g["coef"], locked=bool(g["locked"]))
        for g in conn.execute("SELECT * FROM gates ORDER BY id")
    ]
    bands = [
        search.Band(gate_id=b["gate_id"], head_lo=b["head_lo"], head_hi=b["head_hi"],
                    open_lo=b["open_lo"], open_hi=b["open_hi"])
        for b in conn.execute("SELECT * FROM bands")
    ]
    return gates, bands


def get_steps(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id=? ORDER BY seq", (plan_id,)))


def solve(conn: sqlite3.Connection, plan_id: int, base_version: int) -> dict:
    """重新推演。已锁定的前缀步骤原样保留，从锁定末态继续搜索。"""
    plan = db.check_version(conn, plan_id, base_version)
    if plan["status"] == "sealed":
        raise db.SealedError()
    gates, bands = load_problem(conn, plan)
    start = json.loads(plan["start_openings"])
    locked_seq = plan["locked_seq"]

    prefix: list[dict] = []
    if locked_seq >= 0:
        rows = get_steps(conn, plan_id)
        prefix = [r for r in rows if r["seq"] <= locked_seq]
        if len(prefix) != locked_seq + 1:
            # 锁定步尚无已存路径（例如先锁定后求解），拒绝以免破坏前缀语义
            raise ValueError("locked step has no stored path; solve before locking")
        start = json.loads(prefix[-1]["openings"])

    result = search.find_path(
        gates, bands, plan["head"], start, plan["target_q"],
        plan["ramp"], plan["max_adj_diff"], inflow=plan["inflow"])

    out: dict = {"ok": result.ok, "expanded": result.expanded,
                 "locked_prefix": locked_seq + 1}
    if not result.ok:
        out.update({
            "first_violated": result.first_violated, "detail": result.detail,
            "nearest_state": result.nearest_state, "nearest_q": result.nearest_q,
        })
        return out

    # 拼接：有锁定前缀时跳过与前缀末态重复的起点，否则保留完整路径（含当前开度）
    skip = 1 if locked_seq >= 0 else 0
    tail = [{"seq": locked_seq + 1 + i, "openings": list(o), "total_q": q}
            for i, (o, q) in enumerate(
                zip(result.path[skip:], result.discharges[skip:]))]
    all_steps = ([{"seq": r["seq"], "openings": json.loads(r["openings"]),
                   "total_q": r["total_q"]} for r in prefix] + tail)

    with conn:  # 替换未锁定后缀，前缀行原样重写（内容相同）
        conn.execute("DELETE FROM plan_steps WHERE plan_id=? AND seq>?",
                     (plan_id, locked_seq))
        for s in tail:
            conn.execute(
                "INSERT OR REPLACE INTO plan_steps(plan_id,seq,openings,total_q)"
                " VALUES(?,?,?,?)",
                (plan_id, s["seq"], json.dumps(s["openings"]), s["total_q"]))
        out["version"] = db.bump_version(conn, plan_id)
    out["steps"] = all_steps
    return out


def lock_step(conn: sqlite3.Connection, plan_id: int, seq: int,
              base_version: int) -> dict:
    """锁定第 seq 步：之后重新计算必须保留 0..seq 前缀。"""
    plan = db.check_version(conn, plan_id, base_version)
    if plan["status"] == "sealed":
        raise db.SealedError()
    steps = get_steps(conn, plan_id)
    if not any(s["seq"] == seq for s in steps):
        raise ValueError(f"step {seq} does not exist")
    with conn:
        conn.execute("UPDATE plans SET locked_seq=MAX(locked_seq, ?) WHERE id=?",
                     (seq, plan_id))
        version = db.bump_version(conn, plan_id)
    return {"locked_seq": max(seq, plan["locked_seq"]), "version": version}


def seal(conn: sqlite3.Connection, plan_id: int, base_version: int) -> dict:
    plan = db.check_version(conn, plan_id, base_version)
    if plan["status"] == "sealed":
        raise db.SealedError()
    steps = get_steps(conn, plan_id)
    if not steps:
        raise ValueError("no path to seal; solve first")
    digest = db.plan_digest(plan, steps)
    with conn:
        conn.execute("UPDATE plans SET status='sealed', digest=? WHERE id=?",
                     (digest, plan_id))
        version = db.bump_version(conn, plan_id)
    return {"digest": digest, "version": version, "steps": len(steps)}


def replay(conn: sqlite3.Connection, plan_id: int, seq: int) -> dict:
    """逐步回放已封定方案；每次重算摘要并与封定时摘要比对，保证摘要稳定。"""
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise KeyError(plan_id)
    if plan["status"] != "sealed":
        raise ValueError("plan is not sealed")
    steps = get_steps(conn, plan_id)
    if not (0 <= seq < len(steps)):
        raise ValueError(f"seq {seq} out of range 0..{len(steps)-1}")
    recomputed = db.plan_digest(plan, steps)
    s = steps[seq]
    return {
        "seq": s["seq"],
        "openings": json.loads(s["openings"]),
        "total_q": s["total_q"],
        "total_steps": len(steps),
        "digest": plan["digest"],
        "digest_stable": recomputed == plan["digest"],
    }
