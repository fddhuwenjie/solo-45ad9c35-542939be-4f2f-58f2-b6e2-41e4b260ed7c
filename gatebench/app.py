"""HTTP 服务：原生页面 + JSON API（仅标准库）。

  GET  /                                工作台页面
  GET  /api/gates                       闸门与禁振带
  POST /api/gates/{id}/maintenance      {locked: bool} 检修锁定切换
  GET  /api/plans                       方案列表
  POST /api/plans                       新建方案
  GET  /api/plans/{id}                  方案详情（含步骤）
  POST /api/plans/{id}/solve            {base_version} 推演（保留锁定前缀）
  POST /api/plans/{id}/lock             {seq, base_version} 锁定某一步
  POST /api/plans/{id}/seal             {base_version} 封定
  GET  /api/plans/{id}/replay/{seq}     逐步回放（含稳定摘要）

并发控制：所有写操作携带 base_version，与 plans.version 不一致时返回 409，
后提交者收到版本冲突。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, plans

STATIC_DIR = Path(__file__).parent / "static"


def make_handler(conn, db_lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GateBench/1.0"

        # ---------- 工具 ----------
        def _json(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n == 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def _call(self, fn, *args):
            """在写锁内执行业务调用并做统一异常映射。

            fn 返回 (code, obj) 或裸 obj（视为 200）。
            """
            with db_lock:
                try:
                    res = fn(*args)
                    return res if isinstance(res, tuple) else (200, res)
                except db.VersionConflict as e:
                    return 409, {"error": "version_conflict",
                                 "current_version": e.current_version,
                                 "message": "方案已被他人修改，请刷新后基于最新版本重试。"}
                except db.SealedError:
                    return 409, {"error": "sealed",
                                 "message": "方案已封定，不可修改。"}
                except KeyError:
                    return 404, {"error": "not_found"}
                except ValueError as e:
                    return 400, {"error": "bad_request", "message": str(e)}

        def log_message(self, *a):  # 静默
            pass

        # ---------- 路由 ----------
        def do_GET(self):
            m = self.path
            if m == "/" or m == "/index.html":
                data = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if m == "/api/gates":
                rows = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
                bands = conn.execute("SELECT * FROM bands").fetchall()
                return self._json(200, {
                    "gates": [dict(r) for r in rows],
                    "bands": [dict(b) for b in bands]})
            if m == "/api/plans":
                rows = conn.execute(
                    "SELECT * FROM plans ORDER BY id DESC").fetchall()
                return self._json(200, {"plans": [dict(r) for r in rows]})
            g = re.fullmatch(r"/api/plans/(\d+)", m)
            if g:
                with db_lock:
                    plan = conn.execute("SELECT * FROM plans WHERE id=?",
                                        (int(g[1]),)).fetchone()
                    if plan is None:
                        return self._json(404, {"error": "not_found"})
                    steps = plans.get_steps(conn, plan["id"])
                return self._json(200, {
                    "plan": dict(plan),
                    "steps": [{"seq": s["seq"], "openings": json.loads(s["openings"]),
                               "total_q": s["total_q"]} for s in steps]})
            g = re.fullmatch(r"/api/plans/(\d+)/replay/(\d+)", m)
            if g:
                code, out = self._call(plans.replay, conn,
                                       int(g[1]), int(g[2]))
                return self._json(code, out)
            return self._json(404, {"error": "not_found"})

        def do_POST(self):
            m = self.path
            body = self._body()
            g = re.fullmatch(r"/api/gates/(\d+)/maintenance", m)
            if g:
                def _toggle():
                    cur = conn.execute("SELECT id FROM gates WHERE id=?",
                                       (int(g[1]),)).fetchone()
                    if cur is None:
                        raise KeyError(g[1])
                    with conn:
                        conn.execute("UPDATE gates SET locked=? WHERE id=?",
                                     (1 if body.get("locked") else 0, int(g[1])))
                    return 200, {"id": int(g[1]), "locked": bool(body.get("locked"))}
                code, out = self._call(_toggle)
                return self._json(code, out)
            if m == "/api/plans":
                def _create():
                    required = ["name", "head", "target_q", "ramp",
                                "max_adj_diff", "start_openings"]
                    if any(k not in body for k in required):
                        raise ValueError("missing fields: " + ",".join(required))
                    with conn:
                        cur = conn.execute(
                            "INSERT INTO plans(name,head,target_q,ramp,max_adj_diff,"
                            "start_openings,created_by,created_at)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (body["name"], float(body["head"]), float(body["target_q"]),
                             float(body["ramp"]), float(body["max_adj_diff"]),
                             json.dumps(body["start_openings"]),
                             body.get("created_by", ""), db.now()))
                        pid = cur.lastrowid
                    return 201, {"id": pid, "version": 1}
                code, out = self._call(_create)
                return self._json(code, out)
            g = re.fullmatch(r"/api/plans/(\d+)/solve", m)
            if g:
                code, out = self._call(plans.solve, conn, int(g[1]),
                                       int(body.get("base_version", -1)))
                return self._json(code, out)
            g = re.fullmatch(r"/api/plans/(\d+)/lock", m)
            if g:
                code, out = self._call(plans.lock_step, conn, int(g[1]),
                                       int(body.get("seq", -1)),
                                       int(body.get("base_version", -1)))
                return self._json(code, out)
            g = re.fullmatch(r"/api/plans/(\d+)/seal", m)
            if g:
                code, out = self._call(plans.seal, conn, int(g[1]),
                                       int(body.get("base_version", -1)))
                return self._json(code, out)
            return self._json(404, {"error": "not_found"})

    return Handler


def serve(db_path: str = "gatebench.db", host: str = "127.0.0.1", port: int = 8077):
    conn = db.connect(db_path)
    db.seed_if_empty(conn)
    db_lock = threading.Lock()
    httpd = ThreadingHTTPServer((host, port), make_handler(conn, db_lock))
    print(f"闸门群推演工作台: http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8077
    db_path = sys.argv[2] if len(sys.argv) > 2 else "gatebench.db"
    serve(db_path=db_path, port=port)
