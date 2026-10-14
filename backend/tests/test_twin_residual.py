# -*- coding: utf-8 -*-
"""E2 到位自证（段级残差带）的**口径**钉 —— 全部离线、确定性（时间由 `at=` 传入）。

这个功能存在的意义就是"每一步都能自证"，所以钉的全是**它不许说谎**：
- 采集滞后 / 环形 J6 **不许**被判成"机器不准"（方案点名的两条误报）；
- 一帧实测都没拿到 → `no_data`，**不许**塌成"已到位"（§5.5 铁律③）；
- 被打断（急停 / 新指令顶掉）→ `aborted`，既不许冒充到位、也不许算"机器没到位"；
- **只发一条 warn 事件，绝不产生任何动作**（不自动暂停 / 不重试 / 不急停 —— AGENTS.md §5.1）；
- 观测件坏了不许影响下发。
"""
from __future__ import annotations

import inspect
import io
import tokenize

import pytest

from app.services import twin_residual as TR
from app.services.twin_residual import TwinResidual, residuals, ring_diff_deg


def _code_tokens(fn_or_src) -> str:
    """源码压成**只剩可执行 token** 的串（丢掉 docstring / 注释 / 字符串字面量）。

    ★ 静态钉只判"真的写了什么调用"：本模块的说明文字里满篇"不会自动暂停、不会急停"，
      不剥字符串的话钉会把自己判红（同 `test_point_audit.py` 的处理）。
    """
    src = fn_or_src if isinstance(fn_or_src, str) else inspect.getsource(fn_or_src)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return "".join(out)


@pytest.fixture()
def obs(monkeypatch):
    """一个**独立**的观测器：口径与采样周期钉死，不受现场 robot.yaml 影响。"""
    monkeypatch.setattr(TR, "_cfg", lambda: dict(TR.DEFAULTS))
    monkeypatch.setattr(TR, "_sample_period", lambda: 0.1)   # 10Hz → 一拍 100ms
    monkeypatch.setattr(TR, "_sample_hz", lambda: 10.0)
    o = TwinResidual()
    o._events_enabled = False        # 默认静音；要看事件的用例自己打开
    return o


def _feed(o, frames, t0=1000.0, step=0.1):
    """按固定节拍喂一串关节角，返回最后一帧的快照。"""
    snap = None
    for i, q in enumerate(frames):
        snap = o.observe(q, at=t0 + i * step)
    return snap


# ---------------------------------------------------------------- ① 环形 J6

def test_j6_ring_difference_is_shortest_arc():
    """J6 是转动轴：359° 与 −1° 是**同一姿态**，不是 360° 残差。

    2026-10-11 现场事故（J6 录成 −360 → 下发被拒 → 整条流程终止）之后，
    环形差是把"整圈等价"算对的前提 —— 否则每次过 ±180 都会报一次"没到位"。
    """
    assert ring_diff_deg(359.0, -1.0) == 0.0            # 同一姿态（转过整圈）
    assert ring_diff_deg(1.0, -1.0) == pytest.approx(2.0)
    assert ring_diff_deg(-179.0, 179.0) == pytest.approx(2.0)
    assert ring_diff_deg(10.0, 10.0) == 0.0
    r = residuals([0, 0, 0, 0, 0, 170.0], [0, 0, 0, 0, 0, -170.0])
    assert r[5] == pytest.approx(20.0)          # 不是 340
    assert max(r) == pytest.approx(20.0)


def test_j6_crossing_360_counts_as_arrived(obs):
    """目标 J6 = 170，实际停在 −190（**转过整圈 = 同一姿态**）→ 判到位，不报超差。

    2026-10-11 事故口径：J6 录成 −360 会被 encode_angle 拒掉、整条流程终止；
    观测端如果把"整圈等价"当成 190° 残差，就会每次过 ±180 都喊"没到位"。
    """
    obs.begin([0, 0, 0, 0, 0, 170.0], source="过整圈的段", at=1000.0)
    last = _feed(obs, [[0, 0, 0, 0, 0, -190.0]] * 6)
    assert last["verdict"] == "arrived"
    assert last["peak_deg"] == 0.0
    assert last["moved"] is False               # 同一姿态 = 没有"偷偷转了一圈"


def test_j6_half_turn_apart_is_not_arrived(obs):
    """反证：目标 170、实际 −170 是**真的差 20°**（不是等价角）→ 绝不判到位。

    环形差算松了就会把"差半圈"说成"到位"，那比误报更危险。
    """
    obs.begin([0, 0, 0, 0, 0, 170.0], at=1000.0)
    last = _feed(obs, [[0, 0, 0, 0, 0, -170.0]] * 8)
    assert last["verdict"] == "moving"
    assert last["peak_deg"] == pytest.approx(20.0)


# ---------------------------------------------------------------- ② stuck

def test_stuck_arm_is_detected(obs):
    """下发了、机器人**根本没动** → 到 start_timeout_ms 必须判 stuck 并留一条记录。

    这是这个功能最值钱的一条：控制器收了目标、完成位却永远等不到（档位不对、
    常驻程序 210 没在 WAIT、被外部暂停），过去只能靠"人盯着看它没动"发现。
    """
    obs.begin([30.0, 0, 0, 0, 0, 0], source="测试下发", at=1000.0)
    still = [0.0] * 6
    snaps = [obs.observe(still, at=1000.0 + i * 0.2) for i in range(12)]
    assert "stuck" in [s["verdict"] for s in snaps]
    assert snaps[-1]["moved"] is False
    assert snaps[-1]["peak_deg"] == pytest.approx(30.0)
    # 定案后**不再改判**（结论反复 = 现场没法信）
    assert obs.observe(still, at=1200.0)["verdict"] == "stuck"


def test_zero_displacement_is_not_stuck(obs):
    """目标 == 当前位置（零位移的幂等下发）：首帧就在带内 → 判到位，**不许**判 stuck。"""
    obs.begin([5.0, 5, 5, 5, 5, 5], at=1000.0)
    last = _feed(obs, [[5.0, 5, 5, 5, 5, 5]] * 6)
    assert last["verdict"] == "arrived"
    assert last["moved"] is False               # 没动是事实，但没动 ≠ 卡住


def test_sampling_lag_alone_is_not_flagged(obs):
    """★★ 采集只有 10Hz：机器人**正在正常收敛**时，每拍读到的残差天然含
    "这一拍还没走完的路"（rate × 0.1s）。这部分必须并进"带"里，
    否则就是拿上位机自己的采样延迟冒充"机器不准"（§5.5 铁律③）。
    """
    obs.begin([30.0, 0, 0, 0, 0, 0], source="正常收敛", at=1000.0)
    # 20°/s 匀速收敛 → 每拍残差降 2° → 带 = 容差 1° + 2° = 3°
    snaps = [obs.observe([min(28.0, 2.0 * i), 0, 0, 0, 0, 0], at=1000.0 + i * 0.1)
             for i in range(1, 25)]
    bad = [s["verdict"] for s in snaps
           if s["verdict"] in ("stuck", "not_reached", "overshoot")]
    assert bad == []                             # 一次误报都不许有
    assert snaps[-1]["verdict"] == "arrived"
    assert snaps[-1]["band_deg"] > snaps[-1]["tol_deg"]     # 带确实含采样不确定度
    assert snaps[-1]["uncertainty_deg"] == pytest.approx(
        round(snaps[-1]["band_deg"] - snaps[-1]["tol_deg"], 3))
    # ★ 关键反证：有一帧残差 2° > 容差 1°，但它落在带内 → 没被当成超差
    assert any(s["last_deg"] > s["tol_deg"] for s in snaps)


def test_band_does_not_stay_wide_from_previous_segment(obs):
    """上一段走得再快，新段的带也从**容差**起步（否则"大位移秒判到位"= 假自证）。"""
    obs.begin([60.0, 0, 0, 0, 0, 0], at=1000.0)
    _feed(obs, [[6.0 * i, 0, 0, 0, 0, 0] for i in range(1, 11)])      # 60°/s 把带撑宽
    obs.end("done", at=1002.0)
    obs.begin([0.0, 60.0, 0, 0, 0, 0], at=2000.0)
    first = obs.observe([0.0, 0.0, 0, 0, 0, 0], at=2000.1)
    assert first["band_deg"] == pytest.approx(TR.DEFAULTS["tol_deg"])
    assert first["verdict"] != "arrived"


# ---------------------------------------------------------------- ③ 其它判据

def test_not_reached_after_window(obs):
    """一直在动、但观测窗口（30s）用完仍未收敛到带内 → not_reached（如实说没到位）。"""
    obs.begin([100.0, 0, 0, 0, 0, 0], at=1000.0)
    snaps = [obs.observe([0.2 * i, 0, 0, 0, 0, 0], at=1000.0 + 0.2 * i)
             for i in range(1, 160)]            # 1°/s，永远追不上
    assert snaps[-1]["verdict"] == "not_reached"
    assert snaps[-1]["moved"] is True            # 动了，但没到位 —— 两种情况不混


def test_overshoot_is_detected_and_terminal(obs):
    """到位后又弹出去且不再回来 → overshoot（定案，不改回 arrived）。"""
    obs.begin([10.0, 0, 0, 0, 0, 0], at=1000.0)
    _feed(obs, [[10.0, 0, 0, 0, 0, 0]] * 6)                 # 到位
    assert obs.current()["verdict"] == "arrived"
    assert obs.observe([10.0, 0, 0, 0, 0, 0], at=1001.0)["verdict"] == "arrived"
    s2 = obs.observe([4.0, 0, 0, 0, 0, 0], at=1001.2)       # 弹回 6° > 带×2
    assert s2["verdict"] == "overshoot"
    assert obs.observe([4.0, 0, 0, 0, 0, 0], at=1001.4)["verdict"] == "overshoot"


def test_slow_but_arrived_is_reported_as_slow(obs):
    """走了很久才到位 → 如实标 slow：既不写成"顺利到位"，也不写成"没到位"。"""
    obs.begin([10.0, 0, 0, 0, 0, 0], at=1000.0)
    snaps = [obs.observe([0.2 * i, 0, 0, 0, 0, 0], at=1000.0 + 0.2 * i)
             for i in range(0, 50)]                          # 1°/s，~9s 才到
    assert snaps[-1]["verdict"] == "slow"
    assert snaps[-1]["settled"] is True


def test_no_frames_is_no_data_not_arrived(obs):
    """一帧实测都没拿到 → no_data。不知道就说不知道，**不许**塌成"到位"。"""
    obs.begin([1.0, 0, 0, 0, 0, 0], at=1000.0)
    row = obs.end("done", at=1000.5)
    assert row["verdict"] == "no_data"
    assert row["samples"] == 0


def test_interrupted_is_aborted_not_failed(obs):
    """被人叫停（急停）→ aborted：既不是"到位"，也不是"机器没到位"。"""
    obs.begin([10.0, 0, 0, 0, 0, 0], at=1000.0)
    _feed(obs, [[1.0 * i, 0, 0, 0, 0, 0] for i in range(1, 6)])
    row = obs.end("estop", at=1001.0)
    assert row["verdict"] == "aborted"
    assert row["end_reason"] == "estop"
    assert row["settled"] is False


def test_superseded_micro_segment_is_dropped_from_history(obs):
    """点动逐拍下发顶掉的**微段**（不足 2 帧）不进历史 —— 否则 40 条环形会被刷屏。"""
    obs.begin([0.0] * 6, at=1000.0)
    obs.observe([0.0] * 6, at=1000.05)
    obs.begin([1.0] * 6, at=1000.1)             # 顶掉上一段
    assert obs.recent() == []
    assert obs.current()["samples"] == 0


# ---------------------------------------------------------------- ④ 事件与硬边界

def test_exactly_one_warn_event_per_segment(obs, monkeypatch):
    """超差**每段只发一条** warn 事件，并落进既有审计口径（category=safety）。"""
    sent = []

    def fake_emit(category, level, action, message="", detail=None, **kw):
        sent.append((category, level, action, message, detail))
    monkeypatch.setattr("app.services.events.emit", fake_emit)
    obs._events_enabled = True
    obs.begin([30.0, 0, 0, 0, 0, 0], source="卡住的段", at=1000.0)
    for i in range(20):
        obs.observe([0.0] * 6, at=1000.0 + i * 0.2)
    assert len(sent) == 1
    category, level, action, message, detail = sent[0]
    assert (category, level, action) == ("safety", "warn", "twin_residual_over")
    assert "卡住的段" in message
    assert detail["segment"]["verdict"] == "stuck"


def test_never_emits_auto_stop():
    """★★ 方案点名的红线钉：观测件**不产生任何动作**。

    源码静态断言：`twin_residual.py` 里没有急停 / 取消运动 / 下发 / 写寄存器一类的调用，
    也不 import 任何写路径模块；`observe()` 的返回里没有"该做什么"的字段。
    刹车必须人点（AGENTS.md §5.1「绝不在真机分拣运行期间做任何可能打断它的操作」）。
    """
    src = inspect.getsource(TR)
    code = _code_tokens(src)
    for forbidden in ("estop", "reset_estop", "cancel", "abort_check", "command(",
                      "write_reg", "rc_jog", "motion.", "modbus.", "sim_robot",
                      "jog.", "pause", "resume", "restart", "stop("):
        assert forbidden not in code, "到位自证不该出现动作类调用: %s" % forbidden
    imported = [ln.strip() for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    for ln in imported:
        mod = ln.split()[1] if len(ln.split()) > 1 else ""
        assert not mod.startswith(("app.services.motion", "app.services.modbus",
                                   "app.services.jog", "app.services.sim_robot",
                                   "app.services.rc_ready")), ln

    o = TwinResidual()
    o._events_enabled = False
    o.begin([0.0] * 6, at=1000.0)
    got = None
    for i in range(5):
        got = o.observe([0.0] * 6, at=1000.1 + i * 0.1)
    # 快照只有观测字段，没有任何"指令/建议动作"字段
    assert set(got) <= {"id", "source", "target", "speed_pct", "tol_deg", "band_deg",
                        "uncertainty_deg", "peak_deg", "first_deg", "last_deg", "moved",
                        "settled", "settle_ms", "elapsed_ms", "samples", "verdict",
                        "closed", "end_reason", "profile"}
    for k in got:
        assert k not in ("action", "should", "next", "directive", "cmd")
    assert o.end("done", at=1000.6)["verdict"] == "arrived"


def test_event_failure_never_breaks_observation(obs, monkeypatch):
    """事件系统坏了（落库抛异常）→ 观测照常，绝不把主流程带崩（§5.1 观测件纪律）。"""
    def boom(*a, **k):
        raise RuntimeError("落库炸了")
    monkeypatch.setattr("app.services.events.emit", boom)
    obs._events_enabled = True
    obs.begin([30.0, 0, 0, 0, 0, 0], at=1000.0)
    assert obs.observe([0.0] * 6, at=1000.0)["verdict"] == "moving"
    assert obs.observe([0.0] * 6, at=1002.0)["verdict"] == "stuck"


def test_bad_input_is_ignored(obs):
    """目标 / 实测不是 6 个有限数 → 静默忽略（不抛、不登记、不猜）。"""
    assert obs.begin([1, 2, 3], at=1000.0) is None
    assert obs.begin([float("nan"), 0, 0, 0, 0, 0], at=1000.0) is None
    assert obs.observe([0, 0, 0, 0, 0], at=1000.1) is None
    assert obs.current() is None


def test_state_shape_is_honest(obs):
    """REST/WS 快照的口径字段齐全（前端只渲染，不再自己算判据 —— §5.5 铁律④）。"""
    st = obs.state()
    assert st["enabled"] is True
    assert st["tol_deg"] == TR.DEFAULTS["tol_deg"]
    assert st["sample_hz"] == 10.0
    assert st["current"] is None
    assert "自动暂停" in st["note"]
    obs.begin([0.0] * 6, at=1000.0)
    v = obs.ws_view()
    assert "profile" not in v          # WS 不送曲线（每 50ms 推 240 点 = 下载通道）
    assert set(v) <= set(TR.WS_KEYS)


# ---------------------------------------------------------------- ⑤ 接线

def test_collector_feeds_only_fresh_frames():
    """采集器只在**非 stale** 帧喂帧，并把紧凑快照并进位姿帧（同屏同源）。

    拿冻结的旧姿态连续喂 = 把通讯故障说成"停止收敛"（误报的根源）。
    """
    from app.services import collector as C
    src = inspect.getsource(C.Collector._loop_impl)
    assert "twin_observe(joints, loop_start)" in src
    assert "if not read_stale:" in src
    assert 'payload["residual"] = residual_seg' in src
    assert "twin_residual.ws_view()" in src
    assert src.index("if not read_stale:") < src.index("twin_observe(joints, loop_start)")


def test_collector_observe_wraps_exceptions(monkeypatch):
    """喂帧那一步本身炸了也不许把采集线程带崩（同 safety_note 的纪律）。"""
    from app.services import collector as C

    def boom(*a, **k):
        raise RuntimeError("观测器炸了")
    monkeypatch.setattr(C.twin_residual, "observe", boom)
    C.twin_observe([0.0] * 6, 1000.0)          # 不抛 = 通过


def test_motion_registers_segment_and_survives_observer_failure(monkeypatch):
    """执行引擎在段边界登记 begin；**观测件抛异常照常下发**。

    ★ 这条是"接线不许改变控制语义"的保险：E2 是加上去的眼睛，不是新的一道闸
      （对照 §5.3：未就绪闸门刻意不塞进 motion.command，同一个道理）。
    """
    from app.services import motion as M

    calls = []

    class Fake:
        def begin(self, target, source="", speed_pct=None, at=None):
            calls.append(("begin", list(target), source, speed_pct))
            return 1

        def end(self, reason="done", at=None):
            calls.append(("end", reason))

    monkeypatch.setattr(M, "twin_residual", Fake())
    r = M.motion.command([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 30, 0, source="钉测试")
    assert r["ok"] is True
    assert calls and calls[0][0] == "begin"
    assert calls[0][2] == "钉测试" and calls[0][3] == 30
    # 模拟分支不收尾：让仿真机体位被逐拍观测到（下一个 begin 会如实顶掉它）
    assert all(c[0] != "end" for c in calls)

    class Boom:
        def begin(self, *a, **k):
            raise RuntimeError("观测器坏了")

        def end(self, *a, **k):
            raise RuntimeError("观测器坏了")

    monkeypatch.setattr(M, "twin_residual", Boom())
    assert M.motion.command([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 30, 0)["ok"] is True


def test_estop_closes_the_flying_segment(monkeypatch):
    """急停那一刻，在飞的那一段如实收尾为 estop（不留僵尸段、不猜到位）。"""
    from app.services import motion as M
    seen = []

    class Fake:
        def begin(self, *a, **k):
            return 1

        def end(self, reason="done", at=None):
            seen.append(reason)

    monkeypatch.setattr(M, "twin_residual", Fake())
    try:
        M.motion.estop()
    finally:
        M.motion.reset_estop()
    assert seen == ["estop"]


def test_endpoint_is_readonly(client):
    """GET /api/twin/residual 只读、**不需要控制令牌**（观测不属于控制）。"""
    r = client.get("/api/twin/residual")
    assert r.status_code == 200
    body = r.json()
    assert "current" in body and "recent" in body and "note" in body
    assert isinstance(body["verdicts"], list)


def test_shipped_config_is_conservative():
    """现场配置的默认必须"宁可少判、不可误判"：容差 ≥ 0.5°、窗口不超过下发上限。"""
    cfg = TR._cfg()
    assert 0.5 <= float(cfg["tol_deg"]) <= 5.0
    assert float(cfg["max_ms"]) <= 30000.0
    assert float(cfg["start_timeout_ms"]) >= 500.0
    assert float(cfg["band_max_deg"]) <= 10.0
    assert cfg["enabled"] is True
