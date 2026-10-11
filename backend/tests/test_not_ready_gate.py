# -*- coding: utf-8 -*-
"""统一"未就绪"闸门回归（2026-10-11）。

用户口径：「如果没有上电，执行点到点、移到什么的，全部提醒机器人没有就绪，所有的。」

钉死四件事：
  A. 逐入口：伺服未上电 → **真实下发被拒绝** + **统一文案**（参数化覆盖所有入口）；
  B. 空跑(dry_run) / 模拟模式 必须放行（只拦真实下发）；
  C. 就绪流程本身不被拦（防死锁）；
  D. 读不到状态时按"未就绪"拒绝（fail-safe）。

全部纯逻辑/接口级：conftest 已强制 EFORT_REAL_MOTION=0 + EFORT_SIMULATE=always，
用例内再 monkeypatch 成"看起来像真机、但伺服没上电/读不到"，**绝不连真机**。
"""
from __future__ import annotations

import inspect
import time

import pytest

from app.services.motion import SERVO_NOT_READY_MSG


def _h(client):
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


def _msg(r):
    """取统一错误响应体里的 message（全局异常处理器把 HTTPException.detail → message）。"""
    try:
        return (r.json() or {}).get("message", "") or ""
    except Exception:
        return ""


@pytest.fixture(autouse=True)
def _reset_globals():
    """围栏/急停/点动都是模块级全局状态，测试间必须复位，否则串味。"""
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


# ---------------------------------------------------------------- 假 Modbus
class _MB:
    """假 Modbus：只实现闸门用到的 rc_snapshot。servo=0 表示伺服未上电。"""

    def __init__(self, servo=0, raises=None, none=False):
        self.servo = servo
        self.raises = raises
        self.none = none

    def rc_snapshot(self):
        if self.raises is not None:
            raise self.raises
        if self.none:
            return None, "连接超时"
        return {"bits": {"servo": self.servo, "alarm": 0, "estop": 0}}, None


def _arm_link(monkeypatch, mb, real_modules=()):
    """把运行环境伪装成"真机链路"（双闸已开、采集器非模拟），并用假 modbus 替换。

    ★ 只改**闸门判据**所需的开关：motion.real_write_enabled + motion.modbus +
      collector.simulated。有些入口还各有自己的"真实下发未开启"前置检查（读的是
      该 api 模块命名空间里的 real_write_enabled），由 real_modules 显式列出一起打开。
    """
    import importlib

    import app.services.motion as M
    from app.services import collector as C

    monkeypatch.setattr(M, "real_write_enabled", lambda: True)
    monkeypatch.setattr(M.motion, "modbus", mb, raising=False)
    monkeypatch.setattr(C.collector, "simulated", False)
    monkeypatch.setenv("EFORT_SIMULATE", "auto")
    for name in real_modules:
        mod = importlib.import_module(name)
        monkeypatch.setattr(mod, "real_write_enabled", lambda: True, raising=False)


# =====================================================================
# D / B —— 纯逻辑：判定与豁免
# =====================================================================
def test_servo_off_returns_unified_msg(monkeypatch):
    _arm_link(monkeypatch, _MB(servo=0))
    from app.services.motion import servo_not_ready_reason
    assert servo_not_ready_reason() == SERVO_NOT_READY_MSG


def test_servo_on_passes(monkeypatch):
    _arm_link(monkeypatch, _MB(servo=1))
    from app.services.motion import servo_not_ready_reason
    assert servo_not_ready_reason() is None


def test_dry_run_passes_even_when_servo_off(monkeypatch):
    """B：空跑（dry=True）不下发真实运动 → 必须放行（否则没法预演）。"""
    _arm_link(monkeypatch, _MB(servo=0))
    from app.services.motion import servo_not_ready_reason
    assert servo_not_ready_reason(dry=True) is None


def test_sim_mode_passes(monkeypatch):
    """B：双闸未开（模拟模式）→ 放行（由 motion.command 播放仿真，不写控制器）。"""
    import app.services.motion as M
    monkeypatch.setattr(M, "real_write_enabled", lambda: False)
    from app.services.motion import servo_not_ready_reason
    assert servo_not_ready_reason() is None


def test_read_failure_treated_as_not_ready(monkeypatch):
    """D：读不到状态（抛异常）→ 按"未就绪"拒绝（fail-safe，宁可拒绝不蒙）。"""
    _arm_link(monkeypatch, _MB(raises=RuntimeError("通讯炸了")))
    from app.services.motion import servo_not_ready_reason
    r = servo_not_ready_reason()
    assert r and r.startswith("机器人没有就绪")


def test_snapshot_none_treated_as_not_ready(monkeypatch):
    """D：快照为 None（无响应）→ 按"未就绪"拒绝。"""
    _arm_link(monkeypatch, _MB(none=True))
    from app.services.motion import servo_not_ready_reason
    r = servo_not_ready_reason()
    assert r and "机器人没有就绪" in r


# =====================================================================
# A —— 逐入口：伺服未上电 → 真实下发被拒绝 + 统一文案
# =====================================================================
# (名称, 方法, 路径, body, 需要额外打开 real_write 的模块)
_ENTRIES = [
    ("控制·点到点/移动", "POST", "/api/control/move",
     {"joints": [0, 0, 0, 0, 0, 0]}, ()),
    ("控制·文件/序列执行", "POST", "/api/control/run-file",
     {"items": [{"type": "wait", "seconds": 0.2}], "dry_run": False}, ()),
    ("控制·增量点动", "POST", "/api/control/jog/step",
     {"joint": 1, "dir": 1, "angle_deg": 1, "speed_pct": 5}, ()),
    ("控制·连续点动", "POST", "/api/control/jog/start",
     {"joint": 1, "dir": 1, "speed_pct": 5}, ()),
    ("控制·吸放", "POST", "/api/control/vacuum",
     {"action": "suck"}, ("app.api.control",)),
    ("分拣·开始(真实)", "POST", "/api/sorting/start",
     {"dry_run": False}, ("app.api.sorting",)),
    ("分拣·到点", "POST", "/api/sorting/goto",
     {"name": "A1"}, ()),
    ("井字棋·开局(真实)", "POST", "/api/tictactoe/start",
     {"dry_run": False}, ("app.api.tictactoe",)),
]


@pytest.mark.parametrize("name,method,path,body,mods", _ENTRIES,
                         ids=[e[0] for e in _ENTRIES])
def test_entry_rejected_when_servo_off(client, monkeypatch, name, method, path, body, mods):
    """A：每个会让机器人动的真实下发入口，伺服未上电都必须 409 + 统一文案。"""
    _arm_link(monkeypatch, _MB(servo=0), real_modules=mods)
    h = _h(client)
    r = client.request(method, path, json=body, headers=h)
    assert r.status_code == 409, "「%s」未就绪竟返回 %d：%s" % (name, r.status_code, r.text)
    assert _msg(r) == SERVO_NOT_READY_MSG, "「%s」文案不统一：%r" % (name, _msg(r))


def test_ai_exec_rejected_when_servo_off(client, monkeypatch):
    """A：/ai/exec（真实下发）伺服未上电 → 409 + 统一文案。"""
    import app.api.ai as ai

    _arm_link(monkeypatch, _MB(servo=0))
    h = _h(client)
    pid = "unit-test-plan-1234"
    ai._plans[pid] = {"plan": {"action": "move_one",
                               "from": {"kind": "slot", "slot": "A1"},
                               "to": {"kind": "color_col", "zone": "B", "color": "红"}},
                      "raw": "把A1放到B区红列", "at": time.time()}
    try:
        r = client.post("/api/ai/exec",
                        json={"plan_id": pid, "confirm": True, "dry_run": False},
                        headers=h)
    finally:
        ai._plans.pop(pid, None)
    assert r.status_code == 409, r.text
    assert _msg(r) == SERVO_NOT_READY_MSG


# =====================================================================
# B —— 入口级：空跑不受拦
# =====================================================================
def test_run_file_dry_run_not_blocked(client, monkeypatch):
    """B：空跑（dry_run=true）必须放行 —— 即便伺服未上电。"""
    _arm_link(monkeypatch, _MB(servo=0))
    h = _h(client)
    r = client.post("/api/control/run-file",
                    json={"items": [{"type": "wait", "seconds": 0.2}], "dry_run": True},
                    headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("started") is True


def test_sorting_dry_run_not_blocked_by_gate(client, monkeypatch):
    """B：分拣空跑不会被"未就绪"闸门拦（它可能因点位不齐 400，但绝不是 409 未就绪）。"""
    _arm_link(monkeypatch, _MB(servo=0), real_modules=("app.api.sorting",))
    h = _h(client)
    r = client.post("/api/sorting/start", json={"dry_run": True}, headers=h)
    assert r.status_code != 409 or _msg(r) != SERVO_NOT_READY_MSG, r.text


def test_tictactoe_dry_run_not_blocked_by_gate(client, monkeypatch):
    """B：井字棋空跑不会被"未就绪"闸门拦。"""
    _arm_link(monkeypatch, _MB(servo=0), real_modules=("app.api.tictactoe",))
    h = _h(client)
    r = client.post("/api/tictactoe/start", json={"dry_run": True}, headers=h)
    assert r.status_code != 409 or _msg(r) != SERVO_NOT_READY_MSG, r.text


# =====================================================================
# C —— 就绪流程本身不被拦（防死锁）
# =====================================================================
def test_ready_flow_not_blocked_by_gate(monkeypatch):
    """C：伺服未上电时，「一键就绪」的第一步仍能通过 —— 闸门绝不在就绪流程里。

    用一个会"随上电命令翻到 1"的假 Modbus：就绪流程应能跑完（自己把伺服上起来），
    绝不会返回闸门的"机器人没有就绪"文案（否则用户永远无法就绪 = 死锁）。
    """
    import app.services.rc_ready as R
    from app.services.modbus import CMD_SERVO

    monkeypatch.setattr(R, "real_write_enabled", lambda: True)

    class _FakeReadyMB:
        def __init__(self):
            self.servo = 0

        def rc_snapshot(self):
            return {
                "ok": True, "status_word": 0, "mode": "auto",
                "bits": {"servo": self.servo, "alarm": 0, "estop": 0,
                         "prog_loaded": 1, "run": 1, "manual": 0, "auto": 1, "remote": 0},
                "prog": R.jog_service_program(), "alarm1": 0, "alarm2": 0,
                "jog_trig": False, "speed_pct": 5, "joints": [0.0] * 6,
            }, None

        def rc_command(self, cmd):
            if cmd == CMD_SERVO:
                self.servo = 1        # 上电命令生效
            return 0, None

        def write_reg(self, addr, val):
            return val, None

    r = R.ReadinessService(modbus=_FakeReadyMB()).ready()
    assert SERVO_NOT_READY_MSG not in (r.get("error") or ""), \
        "就绪流程被闸门拦死了（死锁）: %s" % r
    assert r.get("ok") is True, "就绪流程应能自行把伺服上起来: %s" % r


def test_gate_not_wired_into_ready_module():
    """C：静态核对 —— 就绪模块（rc_ready.py）绝不引用闸门函数（防死锁的结构性保证）。"""
    import app.services.rc_ready as R
    src = inspect.getsource(R)
    assert "servo_not_ready_reason" not in src
    assert "reject_if_not_ready" not in src


# =====================================================================
# 覆盖清单（静态）：确认所有真实下发入口都接了统一闸门
# =====================================================================
def test_gate_wired_into_every_real_dispatch_entry():
    """逐个入口核对源码：都调用了统一闸门；就绪流程没有。"""
    import app.api.ai as ai
    import app.api.control as control
    import app.api.sorting as sorting
    import app.api.tictactoe as ttt
    import app.services.vision_rules as vr

    checks = {
        "control.api_move": control.api_move,
        "control.api_run_file": control.api_run_file,
        "control.api_jog_step": control.api_jog_step,
        "control.api_jog_start": control.api_jog_start,
        "control.api_vacuum": control.api_vacuum,
        "sorting.api_start": sorting.api_start,
        "sorting.api_goto": sorting.api_goto,
        "ai.api_exec": ai.api_exec,
        "tictactoe.api_start": ttt.api_start,
    }
    for name, fn in checks.items():
        assert "reject_if_not_ready(" in inspect.getsource(fn), "%s 未接统一闸门" % name
    # 后台视觉规则自动执行（无人值守）同样接闸门
    assert "servo_not_ready_reason(" in inspect.getsource(vr.execute_rule)
    # 就绪/取消流程绝不能接闸门
    assert "reject_if_not_ready(" not in inspect.getsource(control.api_clear_error)


def test_unified_msg_is_the_single_source():
    """文案的唯一来源：motion.SERVO_NOT_READY_MSG。"""
    import app.services.motion as M
    assert M.SERVO_NOT_READY_MSG.startswith("机器人没有就绪")
    assert "一键就绪" in M.SERVO_NOT_READY_MSG
