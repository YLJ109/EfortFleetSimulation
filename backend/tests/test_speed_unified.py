# -*- coding: utf-8 -*-
"""速度统一回归钉（2026-10-11）：**一切下发都跟随右上角滑块**。

用户口径：速度全部跟随右上角滑块、1:1、不做百分比反算；启动默认 10%、下限 5%。

本文件覆盖三条后端钉子（全部离线、不连真机、不下发运动）：
  1. 连续点动 `POST /jog/start` **真正采用**请求里的 `speed_pct`
     （此前 `JogStartIn` 没有该字段 → Pydantic 静默丢弃 → 恒回退 `speed_dps=15`=50%）。
  2. `JogStepIn` / `JogStartIn` 的 `speed_pct` **不再重复定义**（源码解析）。
  3. DB/XPL 程序 item 自带的速度 **不再覆盖** 请求级 `speed_pct`
     （`_steps_from` 产出的步骤速度恒为 None → 执行端回落到 `body.speed_pct`）。
"""
from __future__ import annotations

import os
import re

import pytest

HOME = [0.0] * 6


@pytest.fixture(autouse=True)
def _reset_state():
    """点动/执行引擎/围栏互锁是模块级全局状态，测试间必须复位（与 test_jog.py 同款）。"""
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


# ---------------------------------------------------------------- 1. 连续点动读滑块
def test_jog_start_uses_slider_speed_pct(client):
    """★★ 连续点动必须采用滑块 speed_pct，而不是 speed_dps 折算值 ★★

    现场 BUG：前端 `startJog` 一直发 `speed_pct`（右上角滑块），但后端 `JogStartIn`
    **没有这个字段** → Pydantic 静默丢弃 → `_jog_speed_pct` 回落到 `speed_dps=15` →
    固定按 50% 下发。本用例同时给 speed_dps(折算=50%) 与 speed_pct(17)，
    权威字段 speed_pct 必须胜出。

    `jog.start` 的返回体里没有 speed_pct，只有**由它换算来的 speed_dps**
    （max_speed_dps=30）：17% → 5.1。若回退到 speed_dps=15（=50%）则会是 15.0。
    """
    h = _h(client)
    _home(client, h)
    r = client.post("/api/control/jog/start",
                    json={"joint": 3, "dir": 1, "speed_pct": 17, "speed_dps": 15},
                    headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("ok") is True, d
    # 17% × 30°/s = 5.1（而不是 speed_dps=15 → 50% → 15.0）
    assert abs(float(d["speed_dps"]) - 5.1) < 0.05, (
        "连续点动必须采用滑块 speed_pct=17（→5.1°/s），而不是 speed_dps 折算的 50%%：%s" % d)
    client.post("/api/control/jog/stop", headers=h)


def test_jog_start_speed_pct_boundaries(client):
    """连续点动 speed_pct 的工程边界 [5,100]：5/100 放行，越界由 Pydantic 拒（422）。"""
    h = _h(client)
    _home(client, h)
    for pct in (5, 100):
        r = client.post("/api/control/jog/start",
                        json={"joint": 3, "dir": 1, "speed_pct": pct}, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("ok") is True, d
        # 期望 °/s = pct% × max_speed_dps(30)
        assert abs(float(d["speed_dps"]) - pct * 0.3) < 0.05, (pct, d)
        client.post("/api/control/jog/stop", headers=h)
    for bad in (0, 4, 101):
        r = client.post("/api/control/jog/start",
                        json={"joint": 3, "dir": 1, "speed_pct": bad}, headers=h)
        assert r.status_code == 422, "speed_pct=%s 应被拒：%s" % (bad, r.text)
        client.post("/api/control/jog/stop", headers=h)


# ---------------------------------------------------------------- 2. 模型无重复字段
def test_jog_models_have_single_speed_pct():
    """`JogStepIn` / `JogStartIn` 里 `speed_pct` 只允许定义一次（源码解析）。

    2026-10-11 前 `JogStepIn` 里 speed_pct **重复定义了两行**（后定义覆盖前定义，
    结果相同但读起来像有两条口径）。这里从源码钉死"不再重复"。
    """
    from app.core.config import project_root
    path = os.path.join(project_root(), "backend", "app", "api", "control.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    def _cls_body(name):
        m = re.search(r"class %s\(BaseModel\):(.*?)(?:\nclass |\Z)" % name, src, re.S)
        assert m, "没找到 class %s" % name
        return m.group(1)

    for cls in ("JogStepIn", "JogStartIn"):
        body = _cls_body(cls)
        n = len(re.findall(r"^\s+speed_pct\s*:", body, re.M))
        assert n == 1, "%s 里 speed_pct 定义次数应为 1，实际 %d（重复定义要删）" % (cls, n)


# ---------------------------------------------------------------- 3. 请求级速度优先
def test_steps_from_ignores_item_speed():
    """★★ DB/XPL 程序 item 自带的速度**不再覆盖**请求级 speed_pct ★★

    执行端是 `sp = int(st.get("speed_pct") or body.speed_pct)`：
    只要步骤里带了 speed_pct，请求里的滑块值就被压过。
    2026-10-11 起 `_steps_from` 把步骤速度**恒置 None**，于是恒回落到请求值。
    """
    import app.api.control as C

    steps = C._steps_from([
        {"joints": [10, 0, 0, 0, 0, 0], "speed_pct": 50},   # item 自带速度
        {"tcp": {"x": 300, "y": 0, "z": 700}, "speed_pct": 5},
    ], None)
    assert len(steps) == 2, steps
    for st in steps:
        assert st.get("speed_pct") is None, (
            "步骤速度必须留空（None）→ 由请求级 speed_pct 决定：%s" % st)
    # 反向钉子：真带了 item 速度也照样被忽略（这正是"不再覆盖"的判据）
    assert C._steps_from([{"joints": [0] * 6, "speed_pct": 99}], None)[0]["speed_pct"] is None


# ---------------------------------------------------------------- 4. 到点（测试点）读滑块
def test_sorting_goto_uses_slider_speed_pct(client):
    """★★ 2026-10-11 用户口径：「测试点时不要限速，所有的速度必须右上角的滑轮同步」。

    `/sorting/goto`（到点/复核 = 测试点位）必须采用**请求体 speed_pct（= 右上角滑块）**；
    原实现无条件取 config 的 `control.default_speed_pct`（默认 5%）→ 每次到点都被限到 5%。
    只保留寄存器合法范围夹取（[5,100]），不再"额外压低"。
    """
    h = _h(client)
    # 先示教几个**非原料位**的点（正上位）：它们不走"原位安全路径"的补段/去重逻辑，
    # ★ 2026-10-12 起 `/sorting/goto` 对"已经在目标点"会如实 409「无需移动」，
    #   所以这里**每次到不同的点**，避免与"去重"耦合（本用例只测速度跟随）。
    for _nm, _j6 in (("A1上", 10), ("A2上", 20), ("A3上", 30)):
        r = client.post("/api/sorting/teach",
                        json={"name": _nm, "joints": [0, 0, 0, 0, 0, _j6]}, headers=h)
        assert r.status_code == 200, r.text
    # 带滑块速度到点 → 必须原样采用
    r = client.post("/api/sorting/goto", json={"name": "A1上", "speed_pct": 42}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["speed_pct"] == 42, "到点速度必须跟随滑块，不得回落到 config 的 5%%：%s" % r.text
    # 越界只被夹到寄存器合法范围（40103 物理范围），不是"额外限速"
    r = client.post("/api/sorting/goto", json={"name": "A2上", "speed_pct": 999}, headers=h)
    assert r.status_code == 200 and r.json()["speed_pct"] == 100, r.text
    r = client.post("/api/sorting/goto", json={"name": "A3上", "speed_pct": 1}, headers=h)
    assert r.status_code == 200 and r.json()["speed_pct"] == 5, r.text


def test_sorting_goto_model_accepts_speed_pct():
    """`NameIn` 必须带 `speed_pct` 字段（否则 Pydantic 会静默丢弃滑块的到点速度）。"""
    from app.api import sorting as API

    m = API.NameIn(name="A1", speed_pct=37)
    assert m.speed_pct == 37
    assert API.NameIn(name="A1").speed_pct is None      # 未带 → None（走兼容回落）
