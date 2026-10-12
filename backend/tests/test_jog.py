# -*- coding: utf-8 -*-
"""阶段 5：点动（示教）引擎 + API 测试。

覆盖：鉴权门 / 增量点动 / 限位夹紧 / 连续点动 + 看门狗 / 急停与围栏拦截 / 急停停点动。

测试确定性技巧：点动基准姿态取自 motion.last_target（优先于实时采集），因此先用
/api/control/move 把引擎目标钉在 [0,0,0,0,0,0]，点动结果就可精确预测，
不受模拟采集漂移影响。
"""
from __future__ import annotations

import time

import pytest

import app.services.jog as jogmod

HOME = [0.0] * 6


@pytest.fixture(autouse=True)
def _reset_state():
    """点动/执行引擎/围栏互锁都是模块级全局状态，测试间必须复位，否则串味。"""
    from app.services import safety_guard
    from app.services.jog import jog
    from app.services.motion import motion

    def _clean():
        jog.stop("user")
        motion.reset_estop()
        motion.last_target = None
        safety_guard.update("safe", "", "", 0.0, 1.0)

    _clean()
    yield
    _clean()


def _h(client):
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


def _home(client, h):
    """把执行引擎目标钉到原点，作为点动的确定性基准。"""
    r = client.post("/api/control/move", json={"joints": HOME}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text


# ---------------------------------------------------------------- 鉴权
def test_jog_requires_token(client):
    assert client.get("/api/control/jog").status_code == 401
    assert client.post("/api/control/jog/step",
                       json={"joint": 1, "dir": 1}).status_code == 401
    assert client.post("/api/control/jog/start",
                       json={"joint": 1, "dir": 1}).status_code == 401


# ---------------------------------------------------------------- 状态
def test_jog_state(client):
    r = client.get("/api/control/jog", headers=_h(client))
    assert r.status_code == 200
    d = r.json()
    assert d["active"] is False
    assert d["max_speed_dps"] == 30.0          # 来自 robot.yaml motion.jog.max_speed_dps
    assert d["mode"] == "sim"


# ---------------------------------------------------------------- 增量点动
def test_jog_step_moves_only_target_joint(client):
    h = _h(client)
    _home(client, h)
    r = client.post("/api/control/jog/step",
                    json={"joint": 2, "dir": 1, "angle_deg": 5, "speed_dps": 10},
                    headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["joint"] == 2 and d["dir"] == 1
    assert d["angle_deg"] == 5.0
    # ★ 2026-09-29 速度口径统一：权威字段是 speed_pct（右下/右上角同一个 %）。
    #   旧入参 speed_dps=10 只作兼容折算：10/30×100 = 33%。
    assert d["speed_pct"] == 33
    assert abs(d["speed_dps_est"] - 9.9) < 0.05   # 仅估算显示用
    # duration 是按**估算 °/s** 算的：10°/s → 33% → 估回 9.9°/s → 505ms。
    # 旧的 speed_dps 入参经 % 往返有精度损失（这也是要用 % 当唯一口径的原因之一）。
    assert abs(d["duration_ms"] - 505) <= 5, d["duration_ms"]
    # 只有 J2 从 0 走到 +5
    assert d["target"] == [0.0, 5.0, 0.0, 0.0, 0.0, 0.0]
    assert d["limit_clamped"] is False


def test_jog_step_negative_direction(client):
    h = _h(client)
    _home(client, h)
    d = client.post("/api/control/jog/step",
                    json={"joint": 4, "dir": -1, "angle_deg": 1, "speed_dps": 5},
                    headers=h).json()
    assert d["ok"] is True and d["dir"] == -1
    assert d["target"] == [0.0, 0.0, 0.0, -1.0, 0.0, 0.0]


def test_jog_step_speed_clamped_to_max(client):
    """★ 速度的唯一上限是**工程边界 100%**（不再是 30°/s）。

    口径变更（2026-09-29）：点动速度改为与右上角同一份**百分比**；
    旧的 60°/s 折算成 200% → 被 clamp 到 100%（对应的估算 °/s 恰为 30）。
    """
    h = _h(client)
    _home(client, h)
    d = client.post("/api/control/jog/step",
                    json={"joint": 1, "dir": 1, "angle_deg": 10, "speed_dps": 60},
                    headers=h).json()
    assert d["ok"] is True
    assert d["speed_pct"] == 100          # 夹到工程上限
    assert abs(d["speed_dps_est"] - 30.0) < 0.05


def test_jog_step_limit_clamp(client):
    """J1 上限 170°：从 0 一步 +180° 应夹到 170° 并给出警告。"""
    h = _h(client)
    _home(client, h)
    d = client.post("/api/control/jog/step",
                    json={"joint": 1, "dir": 1, "angle_deg": 180, "speed_dps": 30},
                    headers=h).json()
    assert d["ok"] is True
    assert d["limit_clamped"] is True
    assert d["target"][0] == 170.0
    assert d.get("warnings")


def test_jog_step_bad_joint(client):
    h = _h(client)
    assert client.post("/api/control/jog/step",
                       json={"joint": 7, "dir": 1, "angle_deg": 1},
                       headers=h).status_code == 422     # schema 校验
    assert client.post("/api/control/jog/step",
                       json={"joint": 1, "dir": 0, "angle_deg": 1},
                       headers=h).status_code == 400     # dir 语义校验


# ---------------------------------------------------------------- 连续点动
def test_jog_continuous_keepalive_stop(client):
    h = _h(client)
    _home(client, h)
    r = client.post("/api/control/jog/start",
                    json={"joint": 3, "dir": -1, "speed_dps": 8}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    assert r.json()["joint"] == 3 and r.json()["dir"] == -1

    time.sleep(0.35)                            # 8°/s → 约 -2.8°
    st = client.get("/api/control/jog", headers=h).json()
    assert st["active"] is True
    assert st["target"][2] < -0.5               # J3 确实在往负方向走

    # keepalive 刷新看门狗
    k = client.post("/api/control/jog/keepalive", headers=h).json()
    assert k["ok"] is True and k["action"] == "keepalive"

    s = client.post("/api/control/jog/stop", headers=h).json()
    assert s["action"] == "stop"
    assert s["active"] is False
    assert s["stopped_motion"] is True


def test_jog_keepalive_without_motion(client):
    h = _h(client)
    k = client.post("/api/control/jog/keepalive", headers=h).json()
    assert k["ok"] is False and k["active"] is False


def test_jog_watchdog_auto_stop(client, monkeypatch):
    """死人开关：超过 watchdog_ms 未 keepalive → 自动停（对应示教器松开使能）。"""
    monkeypatch.setattr(jogmod, "_cfg",
                        lambda k, d: {"watchdog_ms": 150, "tick_ms": 40,
                                      "max_speed_dps": 30}.get(k, d))
    h = _h(client)
    _home(client, h)
    r = client.post("/api/control/jog/start",
                    json={"joint": 5, "dir": 1, "speed_dps": 5}, headers=h)
    assert r.json()["active"] is True
    time.sleep(0.5)
    st = client.get("/api/control/jog", headers=h).json()
    assert st["active"] is False
    assert st["reason"] == "watchdog"


# ---------------------------------------------------------------- 安全关
def test_jog_blocked_by_estop(client):
    h = _h(client)
    client.post("/api/control/estop", headers=h)
    r = client.post("/api/control/jog/step",
                    json={"joint": 1, "dir": 1, "angle_deg": 1, "speed_dps": 5},
                    headers=h)
    assert r.status_code == 409
    # 统一错误格式：HTTPException 的 detail 被中间件置空，原因在 message
    assert r.json()["code"] == "HTTP_ERROR"
    assert "急停" in r.json()["message"]


def test_jog_blocked_by_fence(client):
    from app.services import safety_guard
    safety_guard.update("danger", "z1", "危险区", 0.0, 0.05)
    h = _h(client)
    r = client.post("/api/control/jog/step",
                    json={"joint": 1, "dir": 1, "angle_deg": 1, "speed_dps": 5},
                    headers=h)
    assert r.status_code == 409
    assert r.json()["code"] == "HTTP_ERROR"
    assert "围栏" in r.json()["message"]


def test_estop_stops_running_jog(client):
    h = _h(client)
    _home(client, h)
    assert client.post("/api/control/jog/start",
                       json={"joint": 2, "dir": 1, "speed_dps": 5},
                       headers=h).json()["active"] is True
    client.post("/api/control/estop", headers=h)
    st = client.get("/api/control/jog", headers=h).json()
    assert st["active"] is False
    assert st["reason"] == "estop"


# =====================================================================
# ★★ 2026-09-29 速度口径统一（现场报"点动速度突然飘升到 34%"）
# =====================================================================




def test_jog_speed_pct_is_authoritative_and_passthrough(client):
    """★★ 点动速度唯一口径 = **百分比**，且必须原样直通（不回绕、不翻倍）★★

    现场事故：点动页曾有独立"角速度 5°/s"旋钮 → 5/30×100 = 17% →
    再被 v50perc 反算成 40103 = 34% ⇒ **示教器显示 34%、右上角 5%**，
    操作员看到"速度突然飘升到 34%"。现在前端只传 speed_pct，后端直通。
    """
    h = _h(client)
    _home(client, h)
    d = client.post("/api/control/jog/step",
                    json={"joint": 6, "dir": 1, "angle_deg": 1, "speed_pct": 17},
                    headers=h).json()
    assert d["ok"] is True, d
    assert d["speed_pct"] == 17, "权威口径必须原样返回，不得折算：%s" % d
    # 估算 °/s 仅用于显示/耗时：100% 参考 30°/s × 17% ≈ 5.1
    assert abs(d["speed_dps_est"] - 5.1) < 0.05, d


def test_jog_speed_pct_boundaries(client):
    """工程边界 [5,100]：入参越界由 Pydantic 拦（5 与 100 必须放行）。"""
    h = _h(client)
    _home(client, h)
    for pct in (5, 100):
        d = client.post("/api/control/jog/step",
                        json={"joint": 6, "dir": 1, "angle_deg": 1, "speed_pct": pct},
                        headers=h).json()
        assert d["ok"] is True and d["speed_pct"] == pct, d
    for bad in (0, 4, 101):
        r = client.post("/api/control/jog/step",
                        json={"joint": 6, "dir": 1, "angle_deg": 1, "speed_pct": bad},
                        headers=h)
        assert r.status_code == 422, "speed_pct=%s 应被拒：%s" % (bad, r.text)


def test_speed_vperc_matches_program():
    """★★ 速度显示一致性的前提：常驻程序用 `v100perc`，config 必须同步为 100 ★★

    vperc≠100 时上位机要把 40103 写成本身的倍数（设 17% → 写 34%），
    示教器显示与右上角就对不上 —— 这正是现场"速度飘升到 34%"的根因。
    本用例把"config 与控制器程序里的速度常量必须一致且为 100"钉死。
    """
    import os
    import yaml as _yaml
    from app.core.config import project_root

    with open(os.path.join(project_root(), "config", "robot.yaml"), "r",
              encoding="utf-8") as f:
        cfg = _yaml.safe_load(f)
    vperc = (cfg.get("motion") or {}).get("jog", {}).get("service_program_vperc")
    assert float(vperc) == 100.0, (
        "service_program_vperc 必须是 100（程序内 v100perc）——"
        "否则 40103 会被反算成倍数，示教器显示与右上角不一致。实测=%r" % vperc)

    # 控制器程序源码里也必须写 v100perc（两者不一致会差一倍）
    pgm = os.path.join(project_root(), "programs", "210_service_merged.pgm")
    with open(pgm, "r", encoding="utf-8") as f:
        src = f.read()
    # ★ 只认 MJOINT(...) 实际传进去的速度常量（注释里可以提到别的名字）
    import re as _re
    m = _re.search(r"MJOINT\(.*?\)\s*,\s*(\w+perc)", src, _re.S)
    assert m, "没在 210 程序里找到 MJOINT 的速度常量"
    assert m.group(1) == "v100perc", (
        "MJOINT 的速度常量必须是 v100perc（实测 %s）—— 与 config 不一致会差一倍" % m.group(1))
