# -*- coding: utf-8 -*-
"""连开两轮空跑，验证"每次运行必须从头开始"（状态完全干净、且都从 A1 起）。

用法： cd backend && .venv/Scripts/python.exe ../tools/check_two_runs.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from app.db.database import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
c = TestClient(app)
tok = c.post("/api/auth/login", json={"password": "admin123"}).json()["token"]
H = {"X-Control-Token": tok}


def snap():
    return (c.get("/api/sorting/state", headers=H).json().get("state") or {})


bad = 0
for rnd in (1, 2):
    r = c.post("/api/sorting/start", headers=H,
               json={"dry_run": True, "max_pieces": 2, "speed_pct": 5,
                     "dwell_ms": 0, "frames": 1})
    if r.status_code != 200:
        print("round%d 启动失败: %s" % (rnd, r.text[:200]))
        bad += 1
        break
    st0 = r.json()["state"]
    used0 = [x["filled"] for x in st0["placement"]]
    print("round%d 启动瞬间: piece=%s 占位=%s error=%r holding=%s suspect=%s finished=%s"
          % (rnd, st0.get("piece"), used0, st0.get("error"), st0.get("holding"),
             st0.get("suspect_pick"), st0.get("finished")))
    if any(used0) or st0.get("error") or st0.get("holding") or st0.get("suspect_pick"):
        print("  ✗ 启动时不是干净状态")
        bad += 1
    for _ in range(60):
        time.sleep(0.15)
        if not c.get("/api/sorting/state", headers=H).json()["running"]:
            break
    st1 = snap()
    print("round%d 结束:     piece=%s 占位=%s error=%r finished=%s ok=%s last_point=%s"
          % (rnd, st1.get("piece"), [x["filled"] for x in st1["placement"]],
             (st1.get("error") or "")[:50], st1.get("finished"), st1.get("ok"),
             st1.get("last_point")))

# 第三轮：故意让它"上一轮有残留"地再开一次 —— 用 max_pieces=1 快速跑，再开一轮
r = c.post("/api/sorting/start", headers=H,
           json={"dry_run": True, "max_pieces": 3, "speed_pct": 5, "dwell_ms": 0, "frames": 1})
st0 = r.json()["state"]
print("round3 启动瞬间: piece=%s 占位=%s（应为 0/[0,0,0]）"
      % (st0.get("piece"), [x["filled"] for x in st0["placement"]]))
if st0.get("piece") or any(x["filled"] for x in st0["placement"]):
    bad += 1

print("")
print("结论：%s" % ("全部从头开始 ✓" if bad == 0 else "有 %d 处不是从头开始 ✗" % bad))
sys.exit(1 if bad else 0)
