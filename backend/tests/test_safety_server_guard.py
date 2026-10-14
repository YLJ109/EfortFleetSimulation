# -*- coding: utf-8 -*-
"""双源互锁（A1/A6）：后端独立评估怎么进、什么时候才准拦、停摆时怎么如实说。

钉住的现场风险：
  ① **零回归**：默认 shadow —— 后端算出 hit 也**不拦**（数模反解的胶囊包络比真实本体更保守，
     没跑顺就参与拦截 = 现场看到"没碰到却被拒"）。
  ② enforce 才拦，且**算不出状态时如实拒**（enforce 的语义就是"后端必须自己算得出"）。
  ③ 停摆/没数据**不许塌成"安全"**（§5.5 铁律③）：三态 never/fresh/stale 要一路传到界面。
  ④ 前端那一路的既有口径一个字没改（过期且最后为 danger/hit 仍拒绝）。
"""
from __future__ import annotations

import time

import pytest

from app.services import safety_eval as E
from app.services import safety_guard as G


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    G.reset_server()
    G.update("safe", "", "", 1.0, 1.0)         # 前端一路：默认安全（本文件只验后端一路）
    yield
    G.reset_server()


def _server_result(state="hit", clearance=-0.05, zone="地面"):
    return {"state": state, "ratio": 0.0, "clearance": clearance, "zone_id": "ground",
            "zone_name": zone, "measured": True, "calibrated": True,
            "ground": {"clearance_mm": clearance * 1000.0}}


def test_shadow_records_but_never_blocks():
    """★ 默认口径：后端算出碰撞只记录，不拦截（不改现场既有行为）。"""
    G.update_server(_server_result("hit"))
    allow, reason, snap = G.check()
    assert allow is True
    assert snap["mode"] == "shadow"
    assert snap["server"]["state"] == "hit"
    # 但**必须看得见**：合并结论按"谁严重听谁"，界面据此显示"后端也算出危险"
    assert snap["merged"]["state"] == "hit"
    assert snap["merged"]["source"] == "server"
    assert snap["merged"]["blocking_server"] is False


def test_enforce_blocks_server_danger(monkeypatch):
    monkeypatch.setattr(E, "effective_mode", lambda: ("enforce", "测试：已标定"))
    G.update_server(_server_result("danger", 0.02))
    allow, reason, snap = G.check()
    assert allow is False
    assert "后端独立评估" in reason and "danger" in reason
    assert snap["merged"]["blocking_server"] is True


def test_enforce_without_any_evaluation_refuses_honestly(monkeypatch):
    """enforce 但后端一次都没算出来（采集没跑）→ 拒，且原因**如实**说是算不出。"""
    monkeypatch.setattr(E, "effective_mode", lambda: ("enforce", "测试"))
    G.reset_server()
    allow, reason, snap = G.check()
    assert allow is False
    assert "还没算出" in reason
    assert snap["server"]["freshness"] == "never"


def test_stale_server_keeps_last_unsafe(monkeypatch):
    """后端评估停摆（>2s 没更新）且停摆前是 hit → 继续拒（不给"拔采集线程解除互锁"的捷径）。"""
    monkeypatch.setattr(E, "effective_mode", lambda: ("enforce", "测试"))
    G.update_server(_server_result("hit"))
    with G._lock:
        G._server["ts"] = time.time() - 30.0
    allow, reason, snap = G.check()
    assert allow is False and snap["server"]["freshness"] == "stale"
    assert "停摆" in reason


def test_enforce_still_allows_when_server_safe(monkeypatch):
    monkeypatch.setattr(E, "effective_mode", lambda: ("enforce", "测试"))
    G.update_server(_server_result("safe", 0.4))
    allow, reason, _ = G.check()
    assert allow is True


def test_no_data_is_unknown_not_safe():
    """★ 铁律③：两路都没有结论时必须是 unknown，不能显示"安全"。"""
    with G._lock:
        G._latest = None
    G.reset_server()
    snap = G.snapshot()
    assert snap["state"] == "unknown"
    assert snap["merged"]["state"] == "unknown"
    assert snap["merged"]["source"] == "none"


def test_client_stale_unsafe_still_blocks():
    """前端那一路的既有 fail-safe 不能被双源改造弄丢。"""
    G.update("danger", "z1", "危险区", 0.01, 0.05)
    with G._lock:
        G._latest["ts"] = time.time() - 30.0
    allow, reason, _ = G.check()
    assert allow is False and "过期" in reason


def test_nan_from_evaluate_cannot_poison_snapshot():
    """评估模块给的 NaN/inf 绝不能进快照（JSON 里 NaN 会让前端 JSON.parse 直接抛）。"""
    G.update_server({"state": "safe", "clearance": float("nan"),
                     "ratio": float("inf"), "ground": {"clearance_mm": float("nan")},
                     "measured": True, "calibrated": False})
    snap = G.snapshot()
    assert snap["server"]["clearance"] == 0.0
    assert snap["server"]["ratio"] == 1.0
    assert snap["server"]["clearance_mm"] == 0.0
    import json
    json.dumps(snap)                          # 不抛 = 真的可下发给前端


def test_collector_is_wired_to_server_evaluation():
    """接线钉：采集循环里必须真的调 `safety_evaluate` + `update_server`（不接线=假功能）。"""
    import inspect
    src = inspect.getsource(__import__("app.services.collector", fromlist=["Collector"]))
    assert "safety_evaluate(joints)" in src, "采集循环没有调用服务端独立评估"
    assert "safety_update_server(" in src, "评估结果没有喂给互锁"
    assert "read_stale" in src.split("safety_evaluate(joints)")[0][-300:], \
        "stale 帧也在评估 —— 冻结的旧姿态会算出假的'安全'"
