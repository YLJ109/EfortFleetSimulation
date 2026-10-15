# -*- coding: utf-8 -*-
"""机房控制层回归：作用域会话 + 下发闸门 + 仿真执行。

★ 这一层是全站唯一"能让机器人动"的入口，所以每条拒绝路径都要有测试：
  漏一条的后果不是报错，而是"本该被拦下的那一发真的发了出去"。
  真机分支现在一律 501 —— 这条也要钉住，防止哪天有人顺手把闸门打开。

全部对着本机仿真驱动跑，不需要任何设备。
"""
from __future__ import annotations

import os
import time

import pytest

from app.core.fleet_config import FleetConfig, get_fleet
from app.core.fleet_session import (IDLE_S, ScopeSession, build_scope_ids,
                                    fleet_sessions)
from app.services.drivers import SimSwingDriver, build_driver
from app.services.fleet import FleetManager, Station

ROBOTS = [
    {"id": "A1-1", "order": 1, "host": "192.0.2.10", "port": 502, "driver": "sim_swing", "name": "A区-下组-1号", "zone": "A", "group": "lower", "slot": 1,
     "brand": "EFORT", "model": "efort_er8_700h", "pos": {"x": -1.32, "y": -4.6, "yaw": 0}},
    {"id": "A1-2", "order": 2, "host": "192.0.2.10", "port": 502, "driver": "sim_swing", "name": "A区-下组-2号", "zone": "A", "group": "lower", "slot": 2,
     "brand": "EFORT", "model": "efort_er8_700h", "pos": {"x": -1.32, "y": -2.96, "yaw": 0}},
    {"id": "B1-1", "order": 3, "host": "192.0.2.10", "port": 502, "driver": "sim_swing", "name": "B区-下组-1号", "zone": "B", "group": "lower", "slot": 1,
     "brand": "HUAYAN", "model": "huayan_elfin", "pos": {"x": 1.32, "y": -4.6, "yaw": 0}},
    {"id": "B2-1", "order": 4, "host": "192.0.2.10", "port": 502, "driver": "sim_swing", "name": "B区-上组-1号", "zone": "B", "group": "upper", "slot": 1,
     "brand": "HUAYAN", "model": "huayan_elfin", "pos": {"x": 1.32, "y": -1.32, "yaw": 0}},
]


@pytest.fixture(autouse=True)
def _clean_sessions():
    fleet_sessions.release("")          # 单例状态跨用例残留会让"谁持权"的断言随机失败
    yield
    fleet_sessions.release("")


# ======================================================================
# 1) 仿真总闸
# ======================================================================
def test_simulate_defaults_off_and_env_wins():
    os.environ.pop("EFORT_FLEET_SIM", None)
    cfg = get_fleet(reload=True)
    assert cfg.simulate is False        # 出厂默认关：默认就有数据会让人误判链路已通
    os.environ["EFORT_FLEET_SIM"] = "1"
    try:
        assert get_fleet(reload=True).simulate is True
    finally:
        os.environ.pop("EFORT_FLEET_SIM", None)


def test_simulate_is_independent_of_enabled():
    """仿真不能替"要不要启动采集"背书 —— 两个闸各管各的。"""
    raw = dict(get_fleet(reload=True).raw)
    raw["fleet"] = {"enabled": False, "simulate": True}
    assert FleetConfig(raw).simulate is True
    assert FleetConfig(raw).enabled is False


# ======================================================================
# 2) 作用域 id 由服务端现算，不信前端传来的列表
# ======================================================================
def test_build_scope_ids_per_kind():
    assert build_scope_ids("single", robot_id="A1-1", robots=ROBOTS) == ["A1-1"]
    assert build_scope_ids("zone", zone="B", robots=ROBOTS) == ["B1-1", "B2-1"]
    assert build_scope_ids("row", group="lower", robots=ROBOTS) == ["A1-1", "A1-2", "B1-1"]
    assert build_scope_ids("all", robots=ROBOTS) == ["A1-1", "A1-2", "B1-1", "B2-1"]
    # 前端塞一个名单里没有的 id：解不出作用域，而不是"顺手放行"
    assert build_scope_ids("single", robot_id="GHOST-9", robots=ROBOTS) == []
    assert build_scope_ids("zone", zone="Z", robots=ROBOTS) == []


def test_unknown_scope_kind_rejected():
    with pytest.raises(ValueError, match="未知作用域"):
        fleet_sessions.acquire({"kind": "everything", "ids": ["A1-1"]}, "admin")
    with pytest.raises(ValueError, match="没有一台"):
        fleet_sessions.acquire({"kind": "all", "ids": []}, "admin")


# ======================================================================
# 3) 同一时刻只有一个作用域持权
# ======================================================================
def test_two_actors_cannot_hold_at_once():
    a = fleet_sessions.acquire({"kind": "single", "ids": ["A1-1"], "label": "单台 A1-1"}, "admin")
    assert a["ok"] is True
    b = fleet_sessions.acquire({"kind": "zone", "ids": ["B1-1"], "label": "B 区"}, "operator")
    assert b["ok"] is False and b["taken_by"] == "admin"
    # 第三人不能把别人的控制权抢走
    assert fleet_sessions.current().actor == "admin"


def test_same_actor_can_narrow_its_scope():
    fleet_sessions.acquire({"kind": "all", "ids": [r["id"] for r in ROBOTS]}, "admin")
    r = fleet_sessions.acquire({"kind": "single", "ids": ["A1-1"]}, "admin")
    assert r["ok"] is True
    assert fleet_sessions.current().ids == ["A1-1"]


def test_idle_session_is_reaped():
    fleet_sessions.acquire({"kind": "single", "ids": ["A1-1"]}, "admin")
    fleet_sessions._cur.last_seen = time.time() - IDLE_S - 1
    assert fleet_sessions.current() is None                       # 过期即失效
    assert fleet_sessions.status()["active"] is False
    # 过期后别人就能拿到，而不是被一个"幽灵持有者"永久挡住
    assert fleet_sessions.acquire({"kind": "single", "ids": ["B1-1"]}, "operator")["ok"] is True


def test_touch_validates_membership():
    fleet_sessions.acquire({"kind": "single", "ids": ["A1-1"]}, "admin")
    assert fleet_sessions.touch("A1-1") is True
    assert fleet_sessions.touch("B1-1") is False                  # 隔壁台不在作用域里
    assert fleet_sessions.current().touches("B1-1") is False


# ======================================================================
# 4) 下发：闸门与参数校验
# ======================================================================
def _manager(simulate: bool = True) -> FleetManager:
    raw = dict(get_fleet(reload=True).raw)
    raw["fleet"] = {"enabled": True, "simulate": simulate}
    cfg = FleetConfig(raw)
    mgr = FleetManager(cfg)
    mgr._running = True                     # 只测 move 的判据，不起采集线程
    for e in ROBOTS:
        drv = (SimSwingDriver(e["id"], {"brand": e["brand"]}) if simulate
               else build_driver(e["id"], "efort_modbus", {"host": "192.0.2.1"}))
        mgr.stations.append(Station(e, drv))
    return mgr


def test_move_rejects_unknown_station():
    r = _manager().move("NOPE", [0] * 6, 5)
    assert r["ok"] is False and r["code"] == 404


def test_move_rejects_when_collector_stopped():
    mgr = _manager()
    mgr._running = False
    r = mgr.move("A1-1", [0] * 6, 5)
    assert r["ok"] is False and r["code"] == 503


def test_move_rejects_bad_joint_array():
    m = _manager()
    assert m.move("A1-1", [1, 2, 3], 5)["code"] == 400
    assert m.move("A1-1", [float("nan")] * 6, 5)["code"] == 400


def test_move_rejects_outside_soft_limits_not_clamped():
    """越界必须拒，不能悄悄夹到限位上 —— 夹紧等于替操作员改了他的目标。"""
    r = _manager().move("A1-1", [9999, 0, 0, 0, 0, 0], 5)
    assert r["ok"] is False and r["code"] == 400 and "软限位" in r["reason"]


def test_move_refuses_real_driver_with_501():
    """★ 真机分支的拒绝必须是明确错误码，不能"返回 200 但其实什么都没做"。"""
    r = _manager(simulate=False).move("A1-1", [0, 0, 0, 0, 0, 0], 5)
    assert r["ok"] is False and r["code"] == 501


def test_move_holds_sim_target():
    mgr = _manager()
    target = [10.0, -20.0, 30.0, -5.0, 12.0, -45.0]
    r = mgr.move("A1-1", target, 5)
    assert r["ok"] is True and r["simulated"] is True
    # 本测试没起采集线程（_manager 只把 _running 置真来走判据），
    # 所以手动替它poll 一帧 —— 断言的是"驱动把目标接住了"，不是线程调度。
    st = mgr.stations[0]
    st.frame = st.driver.read()
    snap = st.snapshot()
    assert [round(v, 2) for v in snap["joints"]] == target
    assert snap["simulated"] is True                  # 卡片必须还挂"仿真"标
    assert snap["status"].get("running") is False     # 到位了就不该报"运动中"


def test_move_speed_is_capped_at_the_slowest_gear():
    """真机红线：机房下发锁最慢档。前端拖到 100 也只会拿到 5。"""
    assert _manager().move("A1-1", [0] * 6, 100)["speed_pct"] == 5
    assert _manager().move("B1-1", [0] * 6, 88)["speed_pct"] == 5


def test_sim_driver_keeps_brand_specific_status():
    """两牌子状态字段名不同：华沿那台必须报 enabled/inpos，不是 servo_on。"""
    d = SimSwingDriver("B1-1", {"brand": "HUAYAN"})
    d.set_target([0] * 6)
    f = d.read()
    assert "enabled" in f.status and "servo_on" not in f.status
    assert f.simulated is True and f.online is True


def test_snapshot_carries_zone_group_slot():
    """★ 采集启动后 snapshot 必须仍带 zone/group/slot。

    翻车过一次：registry-only 那条路（没启动）返回的 robots[] 有这三个字段，
    启动后走 Station.snapshot() 却漏了 —— 于是"不启动时大屏分页正常、一启动
    12 台全掉进未知分组"。这种"只在真跑起来才坏"的错最难在开发机上发现。
    """
    mgr = _manager(simulate=True)
    snap = mgr.stations[0].snapshot()          # 还没采到帧的那条分支
    assert snap["zone"] == "A" and snap["group"] == "lower" and snap["slot"] == 1
    mgr.stations[0].frame = mgr.stations[0].driver.read()
    snap2 = mgr.stations[0].snapshot()         # 有帧了，走另一条分支
    assert snap2["zone"] == "A" and snap2["group"] == "lower" and snap2["slot"] == 1,         "两条分支的字段必须一致，否则前端作用域会在采集启动那一刻崩掉"


# ======================================================================
# 5) HTTP 层：令牌 + 作用域 + 能力位
# ======================================================================
def _login(client) -> str:
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_api_move_requires_control_token(client):
    assert client.post("/api/fleet/A1-1/move", json={"joints": [0] * 6}).status_code == 401


def test_api_move_requires_active_scope(client):
    tok = _login(client)
    r = client.post("/api/fleet/A1-1/move", json={"joints": [0] * 6},
                    headers={"X-Control-Token": tok})
    # 全局异常处理器把 FastAPI 的 detail 包成了 {code, message}，断言要看 message
    assert r.status_code == 403
    assert "作用域" in (r.json().get("message") or r.json().get("detail") or "")


def test_api_scope_then_move(client, monkeypatch):
    from app.api import fleet as fleet_api
    mgr = _manager(simulate=True)
    monkeypatch.setattr(fleet_api.fleet, "stations", mgr.stations)
    monkeypatch.setattr(fleet_api.fleet, "_running", True)
    monkeypatch.setattr(fleet_api.fleet.cfg, "raw",
                        dict(fleet_api.fleet.cfg.raw, fleet={"enabled": True, "simulate": True}))
    tok = _login(client)
    h = {"X-Control-Token": tok}
    s = client.post("/api/fleet/scope", json={"kind": "single", "robot_id": "A1-1"}, headers=h)
    assert s.status_code == 200, s.text
    assert s.json()["ids"] == ["A1-1"]
    ok = client.post("/api/fleet/A1-1/move", json={"joints": [0] * 6, "speed_pct": 5}, headers=h)
    assert ok.status_code == 200, ok.text
    # 作用域只有 A1-1：隔壁 B1-1 必须被拒
    other = client.post("/api/fleet/B1-1/move", json={"joints": [0] * 6}, headers=h)
    assert other.status_code == 403
    assert "作用域" in (other.json().get("message") or "")
    assert client.delete("/api/fleet/scope", headers=h).status_code == 200
    assert client.post("/api/fleet/A1-1/move", json={"joints": [0] * 6}, headers=h).status_code == 403


def test_api_ships_write_capability(client):
    """前端"执行"能不能按，必须和后端真实闸门同源（否则会出现按下去才 501 的界面）。"""
    d = client.get("/api/fleet").json()
    w = d["write"]
    assert set(w) >= {"ready", "mode", "reason"}
    assert w["ready"] is False and w["mode"] == "off"      # 出厂默认没开仿真


def test_scope_status_is_public(client):
    assert client.get("/api/fleet/scope").json()["active"] is False


def test_scope_session_dict_shape():
    sess = ScopeSession({"kind": "zone", "ids": ["B1-1", "B2-1"], "label": "B 区"}, "admin")
    d = sess.as_dict()
    assert d["count"] == 2 and d["kind"] == "zone" and d["ttl_s"] <= IDLE_S


# ======================================================================
# 6) P6 按台化围栏互锁
# ======================================================================
def _guard():
    from app.services import safety_guard
    return safety_guard


def test_fence_interlock_is_per_robot():
    """★ B 区那台撞围栏，不许拦 A 区这台的下发；反过来必须拦。

    原来全站只有一份围栏状态（_latest 是单个 dict），机房 12 台共用 ——
    症状是"隔壁台子报警，这台按执行没反应"，或者更糟：大屏只在机房模式上报
    全场最严重的一档，于是任何一台危险 = 全部锁死。
    """
    g = _guard()
    g.reset()
    g.update("danger", "z1", "B 区 1 号围栏", 0.0, 0.0, "B1-1")
    g.update("safe", "z2", "A 区 1 号围栏", 0.5, 0.2, "A1-1")

    ok_a, why_a, _ = g.check("A1-1")
    ok_b, why_b, _ = g.check("B1-1")
    assert ok_a is True, why_a
    assert ok_b is False and "danger" in why_b
    assert g.snapshot("A1-1")["state"] == "safe"
    assert g.snapshot("B1-1")["state"] == "danger"
    # 不带 id = 单机链路（"MAIN"），**不能**回落到刚上报过的某一台
    assert g.snapshot()["robot_id"] == g.SINGLE_ID
    assert g.snapshot()["reported"] is False


def test_move_is_blocked_by_that_station_only(client, monkeypatch):
    from app.api import fleet as fleet_api

    g = _guard()
    g.reset()
    mgr = _manager(simulate=True)
    monkeypatch.setattr(fleet_api.fleet, "stations", mgr.stations)
    monkeypatch.setattr(fleet_api.fleet, "_running", True)
    monkeypatch.setattr(fleet_api.fleet.cfg, "raw",
                        dict(fleet_api.fleet.cfg.raw, fleet={"enabled": True, "simulate": True}))
    tok = _login(client)
    h = {"X-Control-Token": tok}
    assert client.post("/api/fleet/scope", json={"kind": "all"}, headers=h).status_code == 200

    g.update("danger", "", "", 0.0, 0.0, "A1-2")
    bad = client.post("/api/fleet/A1-2/move", json={"joints": [0] * 6}, headers=h)
    assert bad.status_code == 409, bad.text
    assert "围栏" in (bad.json().get("message") or "")
    good = client.post("/api/fleet/A1-1/move", json={"joints": [0] * 6}, headers=h)
    assert good.status_code == 200, good.text
    g.reset()


def test_live_report_accepts_all_twelve_in_one_post(client):
    """大屏每秒一次上报 12 台（一次请求，不是 12 次）。"""
    tok = _login(client)
    _guard().reset()
    h = {"X-Control-Token": tok}
    ids = [r["id"] for r in get_fleet().robots]
    assert len(ids) == 12
    body = {"items": [{"robot_id": i, "state": "safe"} for i in ids]}
    r = client.post("/api/safety/live", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 12
    assert set(_guard().reported_ids()) == set(ids)


def test_live_report_rejects_unknown_robot(client):
    """不认识的 robot_id 必须 400。

    凭空造一台"id 随便填、永远 safe"的记录，等于给互锁开了个后门；
    而且键由客户端给，不设限就能把内存刷爆。
    """
    tok = _login(client)
    r = client.post("/api/safety/live", headers={"X-Control-Token": tok},
                    json={"robot_id": "GHOST-1", "state": "safe"})
    assert r.status_code == 400, r.text
    assert "GHOST-1" in (r.json().get("message") or "")


def test_single_field_report_shape_unchanged(client):
    """单机口径（不带 items）的响应形状不能变 —— 老前端与既有测试都读顶层字段。"""
    _guard().reset()                 # 上一条用例上报过全场，这里断言"A1-1 没记录"必须从干净态起
    tok = _login(client)
    r = client.post("/api/safety/live", headers={"X-Control-Token": tok},
                    json={"state": "warn", "zone_id": "z", "zone_name": "n",
                          "clearance": 0.12, "ratio": 0.8})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state"] == "warn" and d["robot_id"] == _guard().SINGLE_ID
    assert abs(d["clearance"] - 0.12) < 1e-9
    assert client.get("/api/safety/live", params={"robot_id": "A1-1"}).json()["reported"] is False
    _guard().reset()
