# -*- coding: utf-8 -*-
"""P1 机房设置：登记表覆盖层（core/fleet_settings.py）+ /api/fleet/config 读写。

钉三件事：
  1) **稀疏**：界面写进去的只有"与 fleet.yaml 不同的项"，改回默认自动消失；
     fleet.yaml 原文一个字都不动（它的注释是真机联调的依据）。
  2) **改不了的东西真的改不了**：id / driving / write_allowed / 每排台数。
     允许界面写这两个闸，等于把"只有改代码才能开写"降级成"点一下就能开写"。
  3) **先验后写**：跨条目冲突（两台踩同一位置、编号重复）必须在落盘之前被拦下。
     存了半张错地图再让 12 条线程去连，比保存失败糟糕得多。
"""
from __future__ import annotations

import io
import json
import os

import pytest

from app.core import fleet_settings as fs
from app.core.config import project_root
from app.core.fleet_config import FleetConfig, get_fleet

YAML_PATH = os.path.join(project_root(), "config", "fleet.yaml")


@pytest.fixture(autouse=True)
def _clean_overlay():
    """覆盖层是**全局文件状态**：tests/test_fleet.py 那批用例断言"出厂 12 台"，
    这里留下任何改动都会让它们读到被改过的 IP / 编号。"""
    fs.reset_overlay()
    get_fleet(reload=True)
    yield
    fs.reset_overlay()
    get_fleet(reload=True)


def _login(client) -> str:
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ======================================================================
# 1) 纯函数层
# ======================================================================
def test_base_registry_is_the_pristine_yaml():
    base = fs.base_registry()
    assert len(base.get("robots") or []) == 12
    a1 = [r for r in base["robots"] if r["id"] == "A1-1"][0]
    assert a1["host"] == "192.168.1.12"
    # 派生字段（port/pos/brand/driver）不属于 yaml 原文 —— 覆盖层的基准必须是干净的，
    # 否则第一次校验烙进 raw 的端口会被当成"单台显式值"，驱动段的改动再也不生效。
    assert "port" not in a1 and "pos" not in a1


def test_merge_is_sparse_and_leaves_yaml_alone():
    base = fs.base_registry()
    ov = fs.build_overlay(base, [{"id": "A1-1", "host": "10.0.0.9"},
                                 {"id": "B1-1", "name": "  B 区一号机 "}])
    assert ov["robots"]["A1-1"] == {"host": "10.0.0.9"}
    assert ov["robots"]["B1-1"] == {"name": "B 区一号机"}     # 首尾空白自己吃掉
    merged = fs.merge_raw(base, ov)
    assert [r["host"] for r in merged["robots"] if r["id"] == "A1-1"] == ["10.0.0.9"]
    # 没改的那 10 台一个字节都没动
    assert sum(1 for r in merged["robots"] if r.get("overwritten")) == 0
    assert io.open(YAML_PATH, encoding="utf-8").read().count("192.168.1.12") >= 1


def test_port_equal_to_driver_default_is_not_an_override():
    """填回驱动段默认端口 = 没有覆盖（否则覆盖层里堆满"看着像改过"的噪音）。"""
    base = fs.base_registry()
    assert fs._default_port(base, "efort_er8_700h") == 502
    ov = fs.build_overlay(base, [{"id": "A1-1", "port": 502}, {"id": "B1-1", "port": 10004}])
    assert not (ov.get("robots") or {})
    ov2 = fs.build_overlay(base, [{"id": "A1-1", "port": 1502}])
    assert ov2["robots"]["A1-1"] == {"port": 1502}


def test_stale_overlay_id_does_not_break_the_registry():
    base = fs.base_registry()
    ov = {"robots": {"ZZ-9": {"host": "10.0.0.1"}}, "schema_version": 1}
    merged = fs.merge_raw(base, ov)
    assert len(merged["robots"]) == 12
    assert all(r["id"] != "ZZ-9" for r in merged["robots"])


def test_check_updates_reports_every_problem_at_once():
    base = fs.base_registry()
    errs = fs.check_updates(base, [
        {"id": "A1-1", "host": "999.1.1.1"},
        {"id": "NOPE", "host": "10.0.0.1"},
        {"id": "A1-2", "slot": 9},
        {"id": "A1-3", "model": "elfin99"},
        {"id": "A1-4", "zone": "C"},
        {"id": "A1-5", "group": "middle"},
        {"id": "A1-6", "name": "   "},
        {"id": "A1-1", "order": 0},
    ], {"group_size": 4})
    assert len(errs) >= 8, errs          # 一次报全：逐次弹窗让人怀疑哪条才是真拦我的
    assert any("IP" in e for e in errs)
    assert any("group_size" in e for e in errs)


def test_layout_merge_only_touches_editable_keys():
    base = fs.base_registry()
    ov = fs.build_overlay(base, [], {"cell_pitch_m": 1.9, "yaw_deg": 90})
    merged = fs.merge_raw(base, ov)
    assert merged["layout"]["cell_pitch_m"] == 1.9
    assert merged["layout"]["yaw_deg"] == 90
    assert merged["layout"]["group_size"] == base["layout"]["group_size"]   # 拓扑不可改


# ======================================================================
# 2) HTTP 层：读
# ======================================================================
def test_api_config_ships_effective_values(client):
    r = client.get("/api/fleet/config")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["robots"]) == 12
    assert d["overlay_keys"] == []
    assert d["group_size"] == 3
    a1 = [x for x in d["robots"] if x["id"] == "A1-1"][0]
    b1 = [x for x in d["robots"] if x["id"] == "B1-1"][0]
    # 端口显示的是**生效值**（来自驱动段），不是"yaml 里没写所以空白"
    assert a1["port"] == 502 and a1["brand"] == "EFORT"
    assert b1["port"] == 10004 and b1["brand"] == "HUAYAN"
    assert a1["overridden"] == []
    assert {m["key"] for m in d["models"]} == {"efort_er8_700h", "huayan_elfin"}


def test_api_config_route_is_not_swallowed_by_robot_id(client):
    """★ GET /api/fleet/config 必须排在 /api/fleet/{robot_id} 之前。

    路由顺序写反了不会报错，只会返回"机房名单里没有这台: config" ——
    设置页打不开，而后端日志一片干净。
    """
    r = client.get("/api/fleet/config")
    assert r.status_code == 200
    assert "robots" in r.json()


# ======================================================================
# 3) HTTP 层：写的闸门
# ======================================================================
def test_api_config_write_requires_admin(client):
    assert client.put("/api/fleet/config", json={"robots": []}).status_code in (401, 403)
    assert client.post("/api/fleet/config/reset").status_code in (401, 403)


def test_api_config_rejects_reserved_fields_loudly(client):
    tok = _login(client)
    # driving / write_allowed 不在模型里，且 extra=forbid → 必须被拒而不是被丢掉
    r = client.put("/api/fleet/config", headers={"X-Control-Token": tok},
                   json={"robots": [{"id": "A1-1", "driving": True}]})
    assert r.status_code >= 400, r.text
    assert not os.path.isfile(fs.OVERLAY_PATH)


def test_api_config_rejects_bad_values_without_writing(client):
    tok = _login(client)
    r = client.put("/api/fleet/config", headers={"X-Control-Token": tok},
                   json={"robots": [{"id": "A1-1", "host": "10.0.0.256"}]})
    assert r.status_code == 400, r.text
    body = r.json()
    assert "IP" in (body.get("message") or body.get("detail") or "")
    assert not os.path.isfile(fs.OVERLAY_PATH)


def test_api_config_rejects_position_collision_before_writing(client):
    """A1-1 挪到 A1-2 的工位 → 两台踩同一位置，必须在落盘前被 FleetConfig 拦下。"""
    tok = _login(client)
    r = client.put("/api/fleet/config", headers={"X-Control-Token": tok},
                   json={"robots": [{"id": "A1-1", "zone": "A",
                                     "group": "lower", "slot": 2}]})
    assert r.status_code == 400, r.text
    msg = r.json().get("message") or r.json().get("detail") or ""
    assert "同一个位置" in msg
    assert not os.path.isfile(fs.OVERLAY_PATH)


# ======================================================================
# 4) 保存 → 生效 → 重建 → 恢复
# ======================================================================
def test_api_config_save_applies_and_rebuilds(client, monkeypatch):
    from app.api import fleet as fleet_api

    calls = []
    monkeypatch.setattr(fleet_api.fleet, "reload",
                        lambda: calls.append(1) or {"stations": 12, "started": False,
                                                    "was_running": False, "simulate": False})
    tok = _login(client)
    r = client.put("/api/fleet/config", headers={"X-Control-Token": tok},
                   json={"robots": [{"id": "A1-1", "host": "10.0.0.9", "name": "一号位"},
                                    {"id": "B2-3", "port": 19999}]})
    assert r.status_code == 200, r.text
    assert calls == [1]                                  # 保存一定要触发整片重建
    assert r.json()["collector"]["started"] is False     # 没开总闸就照实说没起来
    assert os.path.isfile(fs.OVERLAY_PATH)

    # 磁盘上的覆盖层是稀疏的
    on_disk = json.loads(io.open(fs.OVERLAY_PATH, encoding="utf-8").read())
    assert on_disk["robots"] == {"A1-1": {"host": "10.0.0.9", "name": "一号位"},
                                  "B2-3": {"port": 19999}}
    # 单例登记表跟着生效（采集线程重建时读的就是它）
    cfg = get_fleet(reload=True)
    assert [x["host"] for x in cfg.robots if x["id"] == "A1-1"] == ["10.0.0.9"]
    assert [int(x["port"]) for x in cfg.robots if x["id"] == "B2-3"] == [19999]
    # 读接口把"哪几项被改过"如实标出来
    g = client.get("/api/fleet/config").json()
    a1 = [x for x in g["robots"] if x["id"] == "A1-1"][0]
    assert sorted(a1["overridden"]) == ["host", "name"]
    assert sorted(g["overlay_keys"]) == ["A1-1", "B2-3"]


def test_api_config_reset_deletes_overlay(client, monkeypatch):
    from app.api import fleet as fleet_api

    monkeypatch.setattr(fleet_api.fleet, "reload",
                        lambda: {"stations": 12, "started": False,
                                 "was_running": False, "simulate": False})
    tok = _login(client)
    client.put("/api/fleet/config", headers={"X-Control-Token": tok},
               json={"robots": [{"id": "A1-1", "host": "10.0.0.9"}]})
    assert os.path.isfile(fs.OVERLAY_PATH)
    r = client.post("/api/fleet/config/reset", headers={"X-Control-Token": tok})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert not os.path.isfile(fs.OVERLAY_PATH)           # 恢复默认 = 删文件，不是写回默认值
    assert [x["host"] for x in get_fleet(reload=True).robots if x["id"] == "A1-1"] \
        == ["192.168.1.12"]


def test_config_change_releases_control_scope(client, monkeypatch):
    """★ 登记表一变就收回控制权。

    作用域成员名单是按旧地图算出来的；继续让持有人握着那几台，等于
    "你把 B 区挪到了 A 区，他还在按挪动前的名单下发"。
    """
    from app.api import fleet as fleet_api

    monkeypatch.setattr(fleet_api.fleet, "reload",
                        lambda: {"stations": 12, "started": False,
                                 "was_running": False, "simulate": False})
    tok = _login(client)
    h = {"X-Control-Token": tok}
    assert client.post("/api/fleet/scope", json={"kind": "all"}, headers=h).status_code == 200
    r = client.put("/api/fleet/config", headers=h,
                   json={"robots": [{"id": "A1-1", "host": "10.0.0.9"}]})
    assert r.status_code == 200, r.text
    assert r.json()["scope_released"] is True
    assert client.get("/api/fleet/scope", headers=h).json().get("session") is None


def test_saved_registry_still_passes_full_validation(client, monkeypatch):
    """界面能保存的组合，必须能让 FleetConfig 整表建起来 —— 两侧判据同源。"""
    from app.api import fleet as fleet_api

    monkeypatch.setattr(fleet_api.fleet, "reload",
                        lambda: {"stations": 12, "started": False,
                                 "was_running": False, "simulate": False})
    tok = _login(client)
    # 整体换位：A 区上排三台搬到 B 区下排（B 区下排三台同时搬去 A 区上排）
    swaps = []
    for src_slot, dst in ((1, ("B", "lower", 1)), (2, ("B", "lower", 2)),
                          (3, ("B", "lower", 3))):
        swaps.append({"id": "A2-%d" % src_slot, "zone": dst[0], "group": dst[1], "slot": dst[2]})
    for src_slot, dst in ((1, ("A", "upper", 1)), (2, ("A", "upper", 2)),
                          (3, ("A", "upper", 3))):
        swaps.append({"id": "B1-%d" % src_slot, "zone": dst[0], "group": dst[1], "slot": dst[2]})
    r = client.put("/api/fleet/config", headers={"X-Control-Token": tok}, json={"robots": swaps})
    assert r.status_code == 200, r.text
    cfg = get_fleet(reload=True)
    FleetConfig(cfg.raw)                      # 能建起来 = 没有重叠/缺字段
    b11 = [x for x in cfg.robots if x["id"] == "B1-1"][0]
    assert (b11["zone"], b11["group"], b11["slot"]) == ("A", "upper", 1)
    # 换位后 brand/model 跟着原机器走（改的是工位，不是身份）
    assert b11["brand"] == "HUAYAN"
