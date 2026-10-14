# -*- coding: utf-8 -*-
"""★ E1 意图编译器（方案 §10.2 **收缩版**）的回归钉。

收缩版逐字口径：「**只做「意图 → 段列表 + 每段闸门结论」的展示卡（先不接执行，仍走现有入口）**」。
所以这些钉子盯的是三件事，**少一件都算没落地**：

  ① **指令没有控制权**：AST 里根本没有速度 / 气路位号 / 面 / 绕闸开关 ——
     出现即丢弃并如实登记（三样的唯一口径分别是 `ui_speed` / `VAC_ACTIONS` / `effective_face`）；
  ② **结论不冒充**：没校验过的闸门不许盖绿章（§5.5 铁律③ 未知不得塌成布尔）；
     影子模式的几何预检**只记录不拦**，所以不得谎称"执行时会被拒"（否则就是新假报警）；
  ③ **绝不碰下发**：编译链不 import motion/modbus/db、不调 `_move`；执行仍走 `/exec`
     （统一未就绪闸门 + 围栏互锁一个都不少）。

运行： cd backend && .venv/Scripts/python.exe -m pytest tests/test_intent_plan.py -q
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import ai as AI
from app.services import intent_ast as IA
from app.services import intent_plan as IP
from app.services import sorting as S

CAM = S.CAMERA_POINT
START = S.START_POINT
# 够用的"点位库"（角度只为能解算/扫掠，不代表真机示教值）
POSES = {
    "A1": [0.0, -60.0, 60.0, 0.0, 30.0, -14.1],
    "A1上": [0.0, -60.0, 60.0, 0.0, 30.0, -14.1],
    "B1": [120.0, -60.0, 60.0, 0.0, 30.0, 20.0],
    "B1上": [120.0, -60.0, 60.0, 0.0, 30.0, 20.0],
    CAM: [60.0, -50.0, 55.0, 0.0, 25.0, 0.0],
    START: [90.0, -40.0, 40.0, 0.0, 20.0, 0.0],
}
READY_OK = {"ok": True, "reason": ""}
GUARD_OK = {"ok": True, "reason": ""}


def _move_plan(src="A1", dst="B1", **extra):
    p = {"action": "move_one",
         "from": {"kind": "slot", "zone": "A", "slot": src},
         "to": {"kind": "slot", "zone": "B", "slot": dst}}
    p.update(extra)
    return p


_UNSET = object()          # `live` 的"没传"与"传了 None（拿不到姿态）"是两件事，别混


def _compile(plan=None, live=_UNSET, poses=None, ready=READY_OK, guard=GUARD_OK, **kw):
    ast = IA.ast_of(plan if plan is not None else _move_plan(), source="test")
    return ast, IP.compile_plan(ast, live=list(POSES["A1"]) if live is _UNSET else live,
                                poses=POSES if poses is None else poses,
                                ready=ready, guard=guard, **kw)


@pytest.fixture(autouse=True)
def _quiet_geometry(monkeypatch):
    """默认把几何扫掠stub成全程安全：本文件要盯的是**闸门与口径**，不是包络数值。

    ★ 单独有钉子（`test_shadow_mode_...` / `test_enforce_...`）自己把 sweep 换回危险值。
    """
    def _safe(q0, q1, n=16, cfg=None, dz=0.0):
        return {"ok": True, "worst_state": "safe", "worst_mm": 500.0, "worst_zone": "",
                "samples": n, "curve": [], "note": "stub"}
    monkeypatch.setattr(IP.SE, "sweep", _safe)
    monkeypatch.setattr(IP.SE, "effective_mode", lambda: ("enforce", "stub：参与拦截"))


# ============================================================ ① 指令没有控制权
def test_llm_never_emits_speed_or_vacuum_bit():
    """速度 / 气路位号 / 面 / 绕闸开关 **不在 AST 里**：出现即丢弃 + 如实登记。"""
    plan = _move_plan(speed=80, speed_pct=80, vac_bit=1, bit=5, register="40135",
                      program=210, face="back", allow_rotate=True, bypass_guard=True,
                      joints=[1, 2, 3, 4, 5, 6], dry_run=True)
    ast = IA.ast_of(plan, source="llm")
    for k in ("speed", "speed_pct", "vac_bit", "bit", "register", "program",
              "face", "allow_rotate", "bypass_guard", "joints", "dry_run"):
        assert k in ast["dropped"], k
        assert k not in ast, k
    assert ast["warnings"], "丢弃必须**说出来**，不能静默吞掉"
    assert any("不由指令决定" in w for w in ast["warnings"])
    # 白名单里也**没有**这些字段（不是"过滤掉"，而是"根本没有"）
    assert not (set(IA.ALLOWED_KEYS) & set(IA.FORBIDDEN_KEYS))


def test_three_authorities_stay_single_source():
    """三条唯一口径的**归属**写进文案里，免得后来者以为指令能给。"""
    ast = IA.ast_of(_move_plan(speed=50, vacuum="suck", face="front"), source="rule")
    w = "；".join(ast["warnings"])
    assert "右上角滑块" in w and "VAC_ACTIONS" in w and "sorting" in w


def test_missing_field_is_not_backfilled():
    """★ 铁律③：字段缺就是缺 —— 记进 `missing`，**绝不补默认值**。"""
    ast = IA.ast_of({"action": "move_one",
                     "to": {"kind": "slot", "zone": "B", "slot": "B1"}}, source="rule")
    assert "from" in ast["missing"]
    assert ast["ok"] is False and "缺少必要字段" in ast["error"]
    assert ast["src"] == ""                       # 没补 A1，也没补"第一个格"
    _a, res = _compile({"action": "move_one",
                        "to": {"kind": "slot", "zone": "B", "slot": "B1"}})
    assert any("意图不完整" in n for n in res["notes"])


def test_unknown_slot_stays_unknown():
    """颜色列/整区要**执行时判色**才定格 → 记 `unknown`，不许预检时瞎挑一格当真。"""
    ast = IA.ast_of({"action": "move_one",
                    "from": {"kind": "slot", "zone": "A", "slot": "A1"},
                    "to": {"kind": "color_col", "zone": "B", "color": "红"}},
                   source="rule")
    assert ast["dst"] == "" and ast["unknown"], ast
    assert any("执行时" in u for u in ast["unknown"])


# ============================================================ ② 段与闸门
def test_auto_preface_segments_come_from_safe_path():
    """自动补段（原位→正上位→…）**只能**来自 `raw_slot_safe_path`，本卡不自己排。"""
    _a, res = _compile()
    seq = [s["point"] for s in res["segments"]]
    # 起点就在 A1 → 取料位那一步「无需移动」（如实记 note，不假装走了一段）
    assert any("已经在这一格" in n for n in res["notes"])
    assert "A1上" in seq and seq.index("A1上") < seq.index(CAM)
    assert "B1上" in seq and seq.index("B1上") < seq.index("B1")
    assert seq[-1] == START
    autos = [s for s in res["segments"] if s["auto_preface"]]
    assert autos and all("自动补段" in a["purpose"] for a in autos)
    assert all(not a["auto_preface"] for a in res["segments"]
               if a["point"] in (CAM, "B1", START))   # 指令点自己不算补段


def test_plan_marks_infeasible_segment_before_execution():
    """目标格没示教 → **整卡标红 + 指出是第几段**（而不是跑到那儿再失败）。"""
    poses = dict(POSES)
    del poses["B1"]
    _a, res = _compile(poses=poses)
    bad = [s for s in res["segments"] if s["point"] == "B1"]
    assert bad and bad[0]["gate"]["verdict"] == IP.VERDICT_BLOCKED
    assert "尚未示教" in bad[0]["gate"]["reason"]
    assert res["blocked_at"] == bad[0]["i"]
    assert res["ok"] is False and res["verdict"] == IP.VERDICT_BLOCKED


def test_not_ready_blocks_every_move_segment():
    """未就绪闸门：真下发会被 409 → 本卡必须**如实**说"执行时会被拒"，同一条文案。"""
    reason = "机器人没有就绪：伺服未上电，请先点「一键就绪」"
    _a, res = _compile(ready={"ok": False, "reason": reason})
    assert res["verdict"] == IP.VERDICT_BLOCKED
    for s in res["segments"]:
        assert reason in s["gate"]["reason"]


def test_unchecked_gate_never_gives_green():
    """★ 铁律③ 三态：闸门**没校验过**（None）≠ 校验过且通过（ok）→ 不许盖绿章。"""
    _a, res = _compile(ready=None, guard=None)
    assert res["verdict"] != IP.VERDICT_OK
    assert res["checks"]["ready"] == "未校验"
    assert res["checks"]["guard"] == "未校验"
    assert any("没有校验" in w for w in res["warnings"])


def test_shadow_mode_does_not_claim_refusal():
    """影子模式：后端评估**只记录不拦** → 危险包络只能报"有隐患"，不能谎称会被拒。"""
    def _danger(q0, q1, n=16, cfg=None, dz=0.0):
        return {"ok": False, "worst_state": "danger", "worst_mm": 12.0,
                "worst_zone": "z1", "samples": n, "curve": [], "note": "stub"}
    IP.SE.sweep = _danger                       # 不用 monkeypatch：本例结束后由夹具复原
    try:
        IP.SE.effective_mode = lambda: ("shadow", "服务端评估只记录，不拦截")
        _a, res = _compile()
        assert res["verdict"] == IP.VERDICT_WARN, res["segments"][0]["gate"]
        assert not any(s["gate"]["verdict"] == IP.VERDICT_BLOCKED for s in res["segments"])
        assert any("影子模式" in s["gate"]["reason"] for s in res["segments"])
    finally:
        IP.SE.sweep = lambda q0, q1, n=16, cfg=None, dz=0.0: {
            "ok": True, "worst_state": "safe", "worst_mm": 500.0, "worst_zone": "",
            "samples": n, "curve": [], "note": "stub"}
        IP.SE.effective_mode = lambda: ("enforce", "stub")


def test_enforce_mode_does_claim_refusal():
    """enforce（且已标定）：同一段危险包络**才**升级成"执行时会被拒"。"""
    def _hit(q0, q1, n=16, cfg=None, dz=0.0):
        return {"ok": False, "worst_state": "hit", "worst_mm": -3.0,
                "worst_zone": "z1", "samples": n, "curve": [], "note": "stub"}
    IP.SE.sweep = _hit
    try:
        _a, res = _compile()
        assert res["verdict"] == IP.VERDICT_BLOCKED
        assert res["blocked_at"] == 1
    finally:
        IP.SE.sweep = lambda q0, q1, n=16, cfg=None, dz=0.0: {
            "ok": True, "worst_state": "safe", "worst_mm": 500.0, "worst_zone": "",
            "samples": n, "curve": [], "note": "stub"}


def test_no_live_pose_refuses_like_the_real_move_path():
    """拿不到实时姿态 → keep **判不了面** → 与 `_move` 真机分支同一结论：**会被拒**（不冒充安全）。

    ★ 空跑不会拒（它根本不下发）—— 所以卡面另有 `NOTE_DRY` 如实说明，两者不混。
    """
    _a, res = _compile(live=None)
    assert res["checks"]["live"] is False
    assert res["verdict"] == IP.VERDICT_BLOCKED
    for s in res["segments"]:
        assert s["clearance"]["state"] == "unknown"        # 未做几何预检（不塌成 safe）
        assert "拒绝" in s["gate"]["reason"], s["gate"]
    assert any("空跑" in n for n in res["notes"])


def test_no_eta_and_no_vacuum_bit_in_output():
    """不给 ETA / 不给位号：返回体的**字段名**里不能有时间与 40135 位号类东西
       （§4 关键机理 1：PC 只写目标 + 触发位，任何 ETA 都是编的）。"""
    _a, res = _compile()
    keys = set(res.keys()) | set(res["checks"].keys())
    for s in res["segments"]:
        keys |= set(s.keys()) | set(s["gate"].keys()) | set(s["clearance"].keys())
    for k in ("eta", "duration", "seconds", "sec", "ms", "vac_bit", "bit", "bits",
              "addr", "register", "speed", "speed_pct", "action_bit"):
        assert k not in keys, k
    assert "40135" not in json.dumps(res, ensure_ascii=False)
    assert IP.NOTE_TIME in res["notes"] and IP.NOTE_AIR in res["notes"]


def test_face_is_derived_never_taken_from_the_sentence():
    """面：本卡**不给旋转授权** → 与 AI 单件链路同口径一律 keep（不翻面）。"""
    _a, res = _compile()
    assert res["segments"], res
    for s in res["segments"]:
        assert s["face"] == S.FACE_KEEP, s


# ============================================================ ③ 绝不碰下发
def test_modules_never_import_write_path():
    """编译层（intent_ast / intent_plan）**不 import** motion / modbus / db。"""
    for m in (IA, IP):
        src = __import__("inspect").getsource(m)
        for banned in ("app.services.motion", "app.services.modbus", "app.db",
                       "SessionLocal", "motion.command", "real_write_enabled"):
            assert banned not in src, "%s 里出现了 %s" % (m.__name__, banned)


def test_execute_entry_still_passes_existing_gate():
    """执行入口没被换掉：`/exec` 仍过统一未就绪闸门，`/plan` 函数体里**一个下发都没有**。"""
    import inspect
    exec_src = inspect.getsource(AI.api_exec)
    assert "reject_if_not_ready" in exec_src
    plan_src = inspect.getsource(AI.api_plan)
    for banned in ("_move(", "_air(", "motion.command", "api_start", "requests.post",
                   "reject_if_not_ready("):
        assert banned not in plan_src, banned
    # 真实下发的咽喉是 `api/sorting.py::_move`（那里才有围栏互锁 + motion.command）：
    #   本卡的源码里既没有它，也没有任何寄存器写法 —— 编译链与下发链是**两条路**。
    from app.api import sorting as SORT
    assert "guard_check()" in inspect.getsource(SORT._move)
    assert "motion.command" in inspect.getsource(SORT._move)


def test_plan_endpoint_requires_token(client: TestClient):
    """不是权限后门：预检接口同样要控制令牌（与 /parse / /exec 一致）。"""
    assert client.post("/api/ai/plan", json={"text": "把A1放到B1"}).status_code == 401


def test_plan_endpoint_compiles_and_starts_nothing(client: TestClient, monkeypatch):
    """接口通：**只编译不动作** —— 返回段表，且分拣没有在跑、下发函数被打成炸弹。"""
    h = {"X-Control-Token": client.post(
        "/api/auth/login", json={"password": "test1234"}).json()["token"]}
    from app.services.motion import motion

    def _boom(*a, **k):
        raise AssertionError("预检接口绝不允许下发")
    monkeypatch.setattr(motion, "command", _boom)
    r = client.post("/api/ai/plan", json={"text": "把A区第1行第1列放到B区第1行第1列"},
                    headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["compiled"] is True and d["segments"]
    assert d["ast"]["action"] == "move_one"
    assert any("不下发任何动作" in n for n in d["notes"])
    st = client.get("/api/sorting/state", headers=h).json()
    assert st.get("running") is False


def test_plan_endpoint_reports_dropped_control_fields(client: TestClient, monkeypatch):
    """大模型把速度/位号塞进 plan 时，接口必须**如实回显"已丢弃"**（不静默、不照做）。"""
    h = {"X-Control-Token": client.post(
        "/api/auth/login", json={"password": "test1234"}).json()["token"]}
    pid = client.post("/api/ai/parse", json={"text": "把A1放到B1"},
                      headers=h).json()["plan_id"]
    with AI._gate:
        AI._plans[pid]["plan"]["speed_pct"] = 90
        AI._plans[pid]["plan"]["vac_bit"] = 3
    d = client.post("/api/ai/plan", json={"plan_id": pid}, headers=h).json()
    assert set(["speed_pct", "vac_bit"]) <= set(d["ast"]["dropped"]), d["ast"]
    assert any("不由指令决定" in w for w in d["warnings"])


def test_plan_endpoint_is_honest_when_parse_fails(client: TestClient):
    """没听懂就没有段 —— 如实回错误，**绝不硬编一段路径**装成计划。"""
    h = {"X-Control-Token": client.post(
        "/api/auth/login", json={"password": "test1234"}).json()["token"]}
    d = client.post("/api/ai/plan", json={"text": "今天天气怎么样"}, headers=h).json()
    assert d["ok"] is False and d["compiled"] is False
    assert d["segments"] == [] and d["error"]


def test_plan_source_is_cached_from_parse(client: TestClient):
    """缓存里带上"这句话是谁解析的"，预检卡才能如实标注大模型/规则（不冒充）。"""
    h = {"X-Control-Token": client.post(
        "/api/auth/login", json={"password": "test1234"}).json()["token"]}
    r = client.post("/api/ai/parse", json={"text": "把A1放到B1"}, headers=h).json()
    with AI._gate:
        assert AI._plans[r["plan_id"]]["source"] in ("rule", "llm", "rule-degraded",
                                                     "safety")
