# -*- coding: utf-8 -*-
"""Stage C：有状态仿真机（SimRobot）回归。

验收口径：
- 模拟模式下没有指令 → 体位静止在原地（不再无意义扫掠）；
- 指令后 → 体位按基准角速度走向目标（tracking=True），到位停住（tracking=False）；
- /api/pose 与 WS 帧携带 cmd / cmd_tcp / tracking 指令通道字段；
- /control/move 下发后，仿真体位确实走到目标（端到端）。
"""
from __future__ import annotations

import time

import pytest

from app.services.sim_robot import SimRobot, sim_robot


@pytest.fixture(autouse=True)
def _reset_sim():
    sim_robot.reset()
    yield
    sim_robot.reset()


# ---------------- 单元 ----------------

def test_idle_stays_at_zero_no_sweep():
    """没有指令：体位恒为零位，绝不自己动（扫掠已废除）。"""
    r = SimRobot()
    q1, tr1 = r.step()
    time.sleep(0.15)
    q2, tr2 = r.step()
    assert tr1 is False and tr2 is False
    assert q1 == q2 == [0.0] * 6


def test_command_moves_toward_target_then_stops():
    r = SimRobot()
    target = [30, -20, 10, 0, 5, -8]
    r.on_command(target, speed_pct=100)
    q_mid, tr_mid = r.step()
    assert tr_mid is True
    assert all(abs(q_mid[i]) <= abs(target[i]) + 1e-6 for i in range(6))
    # 时长足够走完：max|delta|=30° → 30/45 ≈ 0.67s
    time.sleep(0.9)
    q_end, tr_end = r.step()
    assert tr_end is False
    assert all(abs(q_end[i] - target[i]) < 1e-6 for i in range(6))


def test_second_command_starts_from_reached_pose():
    """连续指令：从上一个目标继续走（不是从零位跳变）。"""
    r = SimRobot()
    r.on_command([10, 0, 0, 0, 0, 0], 100)
    time.sleep(0.8)
    r.on_command([20, 0, 0, 0, 0, 0], 100)
    # step() 按"距上次 step 的 dt"推进；紧跟在 on_command 后面步进时 dt≈0，
    # 拿到的就是新轨迹的起点本身，看不出"有没有往前走"。让时间先流一秒再步一次。
    time.sleep(0.1)
    q_mid, tr = r.step()
    assert tr is True
    assert 10.0 < q_mid[0] < 20.0        # 起点≈10（上一目标已到），正在向 20 走


def test_min_duration_and_speed_scaling():
    r = SimRobot()
    r.on_command([90, 0, 0, 0, 0, 0], 100)      # 90° → 2.0s（> 最短时长）
    assert r.state()["target"] is not None
    q_slow, _ = r.step()
    assert q_slow is not None
    # 最短时长兜底：极小位移也按 MIN_DURATION_S 走位（不会瞬间完成）
    r.on_command([0.001, 0, 0, 0, 0, 0], 100)
    _, tr = r.step()
    assert tr is True                            # 仍在 MIN_DURATION_S 内走位


def test_reset_clears_target():
    sim_robot.on_command([10, 0, 0, 0, 0, 0], 100)
    sim_robot.reset()
    q, tr = sim_robot.step()
    assert q == [0.0] * 6 and tr is False
    assert sim_robot.state()["target"] is None


# ---------------- 端到端（经 /control/move 全链路） ----------------

def test_move_endpoint_drives_sim_pose(client):
    """下发 → 仿真体位走到目标；/api/pose 带指令通道字段。"""
    r = client.post("/api/auth/login", json={"password": "test1234"})
    tok = r.json()["token"]
    h = {"X-Control-Token": tok}

    target = [12, -6, 8, 0, 4, -2]
    mv = client.post("/api/control/move",
                     json={"joints": target, "speed_pct": 100}, headers=h)
    assert mv.status_code == 200, mv.text
    assert mv.json()["ok"] is True

    time.sleep(1.2)   # 12°/45°ps ≈ 0.27s → 按 0.4s 最短时长，1.2s 必然到位
    pose = client.get("/api/pose").json()
    for i, key in enumerate(("j1", "j2", "j3", "j4", "j5", "j6")):
        assert abs(float(pose[key]) - target[i]) < 0.5, (key, pose[key], target[i])
    assert pose["cmd"] is not None
    assert abs(pose["cmd"][0] - target[0]) < 0.5
    assert pose["tracking"] is False           # 已到位停住
    assert isinstance(pose.get("cmd_tcp"), dict)
