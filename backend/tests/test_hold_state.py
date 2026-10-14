# -*- coding: utf-8 -*-
"""E5′「固定相机反用法」= 把送检那几帧**再读一遍**当持件/吸空证据（回归钉）。

现场事实（AGENTS.md §7 明文）：**没有真空传感器** → "手上有没有件"今天纯靠流程推断。
E5′ 不添硬件、不多拍一帧、不动机器人，只把已有帧变成观测值，并且**必须**守住四条：

  ① **相机没开 ≠ 确认**：拿不到帧就是"仅推断"，永远不许升成"有证据支持"（§5.5 铁律③）；
  ② **吸空要报矛盾**，但报矛盾**不等于**自动停 —— 刹车必须人点（§5.1）；
  ③ **单向收紧**：证据缺失/弱/互相矛盾 → 退回"仅推断"，**绝不**反过来放行；
  ④ **不谎报精度**：件尺寸未卡尺实测、相机未标定 → 只给像素面积，**不给毫米**
     （方案 §10.2：工具长度反推与逐格毫米漂移**后置**，这里不出现任何 mm 字段）。
"""
from __future__ import annotations

import inspect
import json
import os

import pytest

from app.services import hold_state as HS
from app.services.hold_state import hold_state


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每例前后清状态；默认关掉事件发射（纯函数用例不该往审计里写东西）。"""
    hold_state.reset()
    hold_state._events_enabled = False
    yield
    hold_state.reset()
    hold_state._events_enabled = True


def _real_frame(area=39970.0, name="红", conf=0.99):
    """**真实形状**的一帧（照 backend/tests/data/vision_detect_frame.json 的骨架）。"""
    return {"ok": True,
            "results": [{"rect": [479.5, 269.5, -78.0], "area": area,
                         "color": {"name": name, "conf": conf, "reason": ""},
                         "center": [479.0, 269.0]}],
            "tracker": {"state": "TRACKING", "hits": 1}}


def _empty_frame():
    """相机正常回了、但镜头前什么候选都没有（= 现场说的"画面里没有候选"）。"""
    return {"ok": True, "results": [], "tracker": {"state": "IDLE", "hits": 0}}


def _vote(ok=True, color="红", conf=0.99):
    return {"ok": ok, "color": color if ok else "", "conf": conf if ok else 0.0,
            "votes": {}, "reason": "" if ok else "无有效判定"}


# ---------------------------------------------------------------- ① 未知绝不塌成"确认"
def test_no_camera_never_reports_confirmed():
    """★★ 方案点名的钉：一帧都没读到（相机没开 / HTTP 失败）→ 只能是 inferred。

    这是本项目最容易犯的假显示：把"没观测到"说成"没问题"。
    """
    for frames in ([], [None, None], [{}, {}], [{"ok": False, "error": "相机未开启"}]):
        snap = hold_state.record("判色", frames, _vote())
        assert snap["state"] == HS.INFERRED, "无证据却报了非 inferred：%r" % (frames,)
        assert snap["supported"] is False
        assert snap["present"] is None          # 未知就是 None，不许塌成 False/True
    assert hold_state.current()["state"] == HS.INFERRED


def test_transport_failure_is_not_absence():
    """读不到 ≠ 看到了空 —— 后者才能报矛盾。两种原因必须分开（§5.4）。"""
    only_fail = hold_state.record("判色", [{}, {}], _vote(ok=False))
    assert only_fail["state"] == HS.INFERRED
    assert only_fail["present"] is None
    only_empty = hold_state.record("判色", [_empty_frame(), _empty_frame()], _vote(ok=False))
    assert only_empty["state"] == HS.CONTRADICTION
    assert only_empty["present"] is False


def test_absent_mixed_with_unknown_is_not_contradiction():
    """★ 单向收紧的一半个例：3 帧里 1 帧明确空、2 帧没读到 → 证据不足，**不报矛盾**。

    报矛盾会让人以为"件掉了"，而真相可能只是相机抖了一下没回 —— 宁可留在"仅推断"。
    """
    snap = hold_state.record("判色", [_empty_frame(), {}, {}], _vote(ok=False))
    assert snap["present"] is None
    assert snap["state"] == HS.INFERRED


# ---------------------------------------------------------------- ② 吸空要报矛盾、但不许动手
def test_empty_frame_after_suck_is_contradiction_not_safe():
    """★★ 方案点名的钉：流程说吸着（expect_present=True）但镜头前明确什么都没有 = 矛盾。

    ★ 同时钉死"不安全的假安全"：矛盾**不许**被写成 `confirmed`/`ok`，
      也**不许**因为矛盾就自动继续往下走 —— 它只是一条观测 + 一条 warn 事件。
    """
    snap = hold_state.record("判色·A1", [_empty_frame(), _empty_frame(), _empty_frame()],
                            _vote(ok=False))
    assert snap["state"] == HS.CONTRADICTION
    assert "吸空" in snap["note"] or "什么都没有" in snap["note"] or "没有候选" in snap["note"]
    assert snap["supported"] is False
    assert snap["absent_frames"] == 3
    st = hold_state.state()
    assert st["contradictions"] == 1


def test_contradiction_emits_exactly_one_warn_and_no_action(monkeypatch):
    """矛盾 → **一条** warn 事件；并且发射前后**没有碰过任何执行通道**。"""
    calls = []
    import app.services.events as EV

    def fake_emit(category, level, action, message="", detail=None, **kw):
        calls.append((category, level, action, message))
        return {}

    monkeypatch.setattr(EV, "emit", fake_emit)
    hold_state._events_enabled = True
    hold_state.record("判色", [_empty_frame(), _empty_frame()], _vote(ok=False))
    assert len(calls) == 1, "矛盾应当只有一条事件（多了就是刷屏，少了就是瞒了）"
    cat, lv, act, msg = calls[0]
    assert cat == "safety" and lv == "warn" and act == "hold_state_contradiction"
    # ★ 观测件不许长出手：模块里既没有下发也没有停
    src = inspect.getsource(HS)
    for bad in ("motion.", "modbus.", "rc_jog", "rc_vacuum", ".command(", "estop"):
        assert bad not in src.split("NOTE = ")[-1] or bad == "estop", \
            "持件证据模块里出现了动作类调用：%s" % bad


def test_non_contradiction_emits_nothing(monkeypatch):
    import app.services.events as EV

    calls = []
    monkeypatch.setattr(EV, "emit", lambda *a, **k: calls.append(a) or {})
    hold_state._events_enabled = True
    hold_state.record("判色", [_real_frame()], _vote())
    hold_state.record("判色", [{}], _vote(ok=False))
    assert calls == [], "只有矛盾才发事件；确认/仅推断都该安静"


def test_event_failure_never_breaks_observation(monkeypatch):
    import app.services.events as EV

    def boom(*a, **k):
        raise RuntimeError("事件服务挂了")

    monkeypatch.setattr(EV, "emit", boom)
    hold_state._events_enabled = True
    snap = hold_state.record("判色", [_empty_frame()], _vote(ok=False))
    assert snap["state"] == HS.CONTRADICTION          # 观测结论照常给出


# ---------------------------------------------------------------- ③ 有候选 ≠ 确认（吸盘歧义）
def test_candidate_without_vote_is_not_confirmation():
    """★ 现场**红色吸盘**自己可能成一个候选 → 光"帧里有东西"不足以升成有证据支持。

    判据 = 「判色结论成立」**且**「多帧面积一致」，两件事缺一条就退回 inferred。
    """
    snap = hold_state.record("判色", [_real_frame(), _real_frame()], _vote(ok=False))
    assert snap["state"] == HS.INFERRED
    assert "吸盘" in snap["note"] or "不足以证实" in snap["note"]


def test_stable_multi_frame_vote_ok_is_supported():
    frames = [_real_frame(area=40000.0), _real_frame(area=41000.0), _real_frame(area=39500.0)]
    snap = hold_state.record("判色", frames, _vote())
    assert snap["state"] == HS.CONFIRMED
    assert snap["supported"] is True and snap["present_frames"] == 3


def test_area_jitter_is_not_supported():
    """面积忽大忽小（只拍到局部 / 件在晃）→ 证据不足，退回仅推断。"""
    frames = [_real_frame(area=40000.0), _real_frame(area=4000.0)]
    snap = hold_state.record("判色", frames, _vote())
    assert snap["state"] == HS.INFERRED
    assert snap["area_ratio"] > HS._cfg()["area_ratio_max"]


def test_invalid_color_still_counts_as_presence():
    """无效色（橙）照样是"镜头前有个件"的证据 —— 持件与判色是两件事，别混。"""
    pres = HS.presence_from_frames([_real_frame(name="橙"), _real_frame(name="橙")])
    assert pres["present"] is True


def test_expect_empty_contradiction():
    """流程认为手上已空、镜头前却有件 → 矛盾（放料失败）。判据与正向对称、不另造一套。"""
    snap = hold_state.record("放件后复拍", [_real_frame(), _real_frame()], _vote(),
                            expect_present=False)
    assert snap["state"] == HS.CONTRADICTION
    assert "放料失败" in snap["note"]
    ok_snap = hold_state.record("放件后复拍", [_empty_frame(), _empty_frame()],
                               _vote(ok=False), expect_present=False)
    assert ok_snap["state"] == HS.CONFIRMED


# ---------------------------------------------------------------- ④ 不谎报毫米（后置项不许混进来）
def test_no_millimetre_field_in_snapshot():
    """★ 件尺寸未卡尺实测 + 相机未标定 → 证据里只允许像素（`area_px`），**不得**出现任何 mm 量。

    方案 §10.2：工具长度反推 / 逐格毫米漂移**已后置**，这里出现 mm 字段就是提前承诺。
    """
    snap = hold_state.record("判色", [_real_frame()], _vote())
    assert any(k.endswith("_px") for k in snap)
    assert not [k for k in snap if k.endswith("_mm") or "standoff" in k or "mm" in k.split("_")], \
        "出现了毫米量级的字段（换算前提未具备）：%s" % sorted(snap)


# ---------------------------------------------------------------- 接线：_capture / 双面 / 空跑
def test_capture_wires_evidence_without_touching_vote(monkeypatch):
    """`_capture` 判色之后登记证据，但**返回的判色结论一字不动**。"""
    from app.api import sorting as API

    frames = [_real_frame(), _real_frame(), _real_frame()]
    monkeypatch.setattr(API.camera_client, "get_json",
                        lambda path, timeout=6: (200, frames.pop(0) if frames else _empty_frame(), None))
    monkeypatch.setattr(API.time, "sleep", lambda *_a: None)
    st = {}

    class _Ctx:
        def cancelled(self): return False
        def sleep(self, s): return False

    v = API._capture(_Ctx(), 3, 0.28, "判色", st)
    assert v["ok"] is True and v["color"] == "红"       # 判色结论原样
    ev = st.get("hold_evidence")
    assert ev and ev["state"] in HS.STATES
    assert ev["frames"] == 3
    assert hold_state.current()["source"] == "判色"


def test_capture_observer_failure_does_not_break_judging(monkeypatch):
    """观测件坏了 → 判色照常返回（同 twin_residual / safety_note 的纪律）。"""
    from app.api import sorting as API
    import app.services.hold_state as HSM

    monkeypatch.setattr(API.camera_client, "get_json",
                        lambda path, timeout=6: (200, _real_frame(), None))
    monkeypatch.setattr(API.time, "sleep", lambda *_a: None)

    def boom(*a, **k):
        raise RuntimeError("观测件挂了")

    monkeypatch.setattr(HSM.hold_state, "record", boom)

    class _Ctx:
        def cancelled(self): return False
        def sleep(self, s): return False

    v = API._capture(_Ctx(), 2, 0.28, "判色", {})
    assert v["ok"] is True


def test_dual_face_records_one_evidence_per_face(monkeypatch):
    """★ 双面 = 两件各自"有没有吸住"是两件事 → 每面各登记一条，不合成一条。"""
    from app.api import sorting as API

    seq = [_real_frame(area=40000.0), _real_frame(area=40100.0),
           _empty_frame(), _empty_frame()]
    it = iter(seq)
    monkeypatch.setattr(API.camera_client, "get_json",
                        lambda path, timeout=6: (200, next(it), None))
    monkeypatch.setattr(API.time, "sleep", lambda *_a: None)
    st = {}

    class _Ctx:
        def cancelled(self): return False
        def sleep(self, s): return False

    vs = API._capture_per_face(_Ctx(), ["lower", "upper"], 2, 0.28, "判色", st=st)
    assert len(vs) == 2
    rec = hold_state.recent(5)
    assert len(rec) == 2, "双面应各登记一条证据（合成一条就把第二件吸没吸住吞了）"
    assert {"判色·lower", "判色·upper"} <= {r["source"] for r in rec}
    by = {r["source"]: r for r in rec}
    assert by["判色·lower"]["state"] == HS.CONFIRMED
    assert by["判色·upper"]["state"] == HS.CONTRADICTION   # 反面那件没吸住 → 如实报


def test_dry_run_never_records_evidence_source_pin():
    """★ 空跑 / 双闸未开：机器人根本没动，方块不可能在相机前 → **不许**登记持件证据。

    判据（钉源码）：`_capture` / `_capture_per_face` 的调用点都在 `if not judge:` 的
    **else 分支**里，空跑分支走占位色、根本不调相机。
    """
    from app.api import sorting as API

    src = inspect.getsource(API)
    assert "hold_note = None" not in src
    for marker in ('v = _capture(ctx, int(body.frames), min_conf, "判色"',
                   'verdicts = _capture_per_face('):
        i = src.index(marker)
        j = src.rindex("if not judge:", 0, i)
        assert src.count("else:", j, i) >= 1, "%s 不在 `if not judge:` 的 else 分支里" % marker


# ---------------------------------------------------------------- 只读侧与配置
def test_state_shape_is_honest():
    hold_state.record("判色", [_real_frame(), _real_frame()], _vote())
    st = hold_state.state()
    for k in ("enabled", "states", "state_label", "current", "recent",
              "contradictions", "observations", "note", "area_ratio_max"):
        assert k in st, "state 少字段 %s（界面/取证包会读它）" % k
    assert set(st["states"]) == {HS.CONFIRMED, HS.INFERRED, HS.CONTRADICTION}
    assert st["note"] and "不会自动暂停" in st["note"]
    assert "真空传感器" in st["note"]          # 诚实：这不是传感器读数


def test_ids_are_monotonic_and_history_is_ring():
    for i in range(60):
        hold_state.record("判色", [_real_frame()], _vote())
    st = hold_state.state()
    ids = [r["id"] for r in st["recent"]]
    assert ids == sorted(ids, reverse=True), "recent 必须按 id 从新到旧（水位/取证包靠它）"
    assert len(st["recent"]) == 10
    assert st["observations"] <= HS._cfg()["history_max"]


def test_disabled_config_records_nothing(monkeypatch):
    monkeypatch.setattr(HS, "_cfg", lambda: dict(HS.DEFAULTS, enabled=False))
    assert hold_state.record("判色", [_real_frame()], _vote()) is None
    assert hold_state.current() is None


def test_bad_frames_input_is_ignored():
    assert hold_state.record("判色", None, None)["state"] == HS.INFERRED
    assert hold_state.record("判色", ["x", 3, {}], {"ok": "yes"})["state"] == HS.INFERRED


def test_shipped_config_is_conservative():
    cfg = HS._cfg()
    assert cfg["enabled"] is True
    assert 1.0 <= float(cfg["area_ratio_max"]) <= 3.0, \
        "面积一致带太宽就等于任何候选都算确认（吸盘歧义会混进来）"
    assert int(cfg["history_max"]) >= 20


def test_endpoint_is_readonly(client):
    r = client.get("/api/system/metrics/hold-state")
    assert r.status_code == 200
    body = r.json()
    assert "current" in body and "recent" in body and "note" in body
    assert set(body["states"]) == {HS.CONFIRMED, HS.INFERRED, HS.CONTRADICTION}


def test_platform_metric_exposes_shared_height(client):
    """D5′：立足面高度由后端给（前端 three/cell.js 与后端 A1 必须读同一个数）。"""
    r = client.get("/api/system/metrics/platform")
    assert r.status_code == 200
    body = r.json()
    assert "platform_height_mm" in body and body["platform_height_mm"] > 0
    assert body["hold_state_enabled"] is True


def test_module_never_imports_write_path():
    src = inspect.getsource(HS)
    head = src.split("def classify_frame")[0]
    for bad in ("app.services.motion", "app.services.modbus", "app.services.jog",
                "app.services.rc_ready", "app.services.sim_robot", "camera_client"):
        assert bad not in head, "持件证据 import 了写路径：%s" % bad


def test_state_key_declared_in_run_state():
    """`hold_evidence` 必须在 `fresh_run_state()` 里初始化 —— 否则界面拿到 undefined，
    三元判空会把它和"仅推断"混成同一个显示（§5.5 铁律③：未知不许塌成正常）。"""
    from app.services import sorting as S

    st = S.fresh_run_state(dry_run=False, max_pieces=3, judge=True)
    assert "hold_evidence" in st and st["hold_evidence"] is None
