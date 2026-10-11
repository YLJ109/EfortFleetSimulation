# -*- coding: utf-8 -*-
"""/api/tictactoe 对局接口回归（不碰真机）。

钉住的点：
  1. **不是权限后门**：所有接口裸调 401；
  2. 没有对局时 /move、/reset 拒绝、/state 返回空、/replay 404；
  3. 点位不齐 → /start 400（不给"跑了但做不了"的假象）；
  4. 空跑整局能从头跑到终局（棋谱 ≥5 手），结果 ∈ {user_win,robot_win,draw}；
  5. **互斥**：对局进行中不许开始分拣（共用 40135 触发位）。

运行： cd backend && .venv/Scripts/python.exe -m pytest tests/test_tictactoe_api.py -q
"""
from __future__ import annotations

import threading
import time

import pytest

from app.api import tictactoe as ttt_api
from app.db.crud import create_point, delete_point, list_points
from app.db.database import SessionLocal
from app.services import sorting as S
from app.services import tictactoe as TT
from app.services.motion import SERVO_NOT_READY_MSG


def _token(c) -> str:
    r = c.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _set_job(state=None, thread=None):
    """直接摆弄模块级运行登记（内存态）—— 让"复盘"用例可确定地构造局面。"""
    with ttt_api._gate:
        ttt_api._job.update({"thread": thread, "ctx": None, "state": state,
                             "q": None, "started_at": 0.0})


def _ctx_cls():
    """最小 RunContext 替身：不暂停、可取消、sleep 立即返回。"""
    class _Ctx:
        def cancelled(self): return False
        def wait_pause(self): return False
        def sleep(self, s): return False
        def finish(self, phase): self.phase = phase
        def set_thread(self, th): pass
    return _Ctx()


def _seed_points():
    """播种井字棋要用的 37 点（= 分拣 38 点去掉 C 检测位）。返回建好的 point id 列表。"""
    names = [s for s in S.A_SLOTS] + [s + "上" for s in S.A_SLOTS] \
        + [s for s in S.B_SLOTS] + [s + "上" for s in S.B_SLOTS] + [S.START_POINT]
    db = SessionLocal()
    ids = []
    try:
        for i, n in enumerate(names):
            row = create_point(db, name=n, joints_json="[%d,0,0,0,0,0]" % i)
            ids.append(row.id)
    finally:
        db.close()
    return ids


def _drop(ids):
    db = SessionLocal()
    try:
        for pid in ids:
            delete_point(db, pid)
    finally:
        db.close()


# ---------------------------------------------------------------- 权限
def test_all_endpoints_require_token(client):
    assert client.post("/api/tictactoe/start", json={}).status_code == 401
    assert client.get("/api/tictactoe/state").status_code == 401
    assert client.post("/api/tictactoe/move", json={"cell": 0}).status_code == 401
    assert client.post("/api/tictactoe/stop").status_code == 401
    assert client.post("/api/tictactoe/reset").status_code == 401
    assert client.get("/api/tictactoe/replay").status_code == 401
    assert client.post("/api/tictactoe/replay").status_code == 401   # ★ 真实复盘也要令牌


# ---------------------------------------------------------------- 空态
def test_state_without_game(client):
    h = {"X-Control-Token": _token(client)}
    d = client.get("/api/tictactoe/state", headers=h).json()
    assert d["ok"] is True and d["running"] is False


def test_move_without_game_409(client):
    h = {"X-Control-Token": _token(client)}
    assert client.post("/api/tictactoe/move", json={"cell": 0}, headers=h).status_code == 409


def test_reset_without_game_400(client):
    h = {"X-Control-Token": _token(client)}
    assert client.post("/api/tictactoe/reset", headers=h).status_code == 400


def test_replay_without_game_404(client):
    h = {"X-Control-Token": _token(client)}
    assert client.get("/api/tictactoe/replay", headers=h).status_code == 404


def test_start_missing_points_400(client):
    """测试库默认没有点位 → 必须 400 且说清缺哪些（不是"跑了但做不了"）。

    ★ 全局 HTTPException 处理器把 dict 型 detail 压成 `message` 文本（见 core/exceptions.py），
      所以这里断言 message 里能看到"点位不齐"与缺失点位名。
    """
    h = {"X-Control-Token": _token(client)}
    r = client.post("/api/tictactoe/start", json={"difficulty": "normal", "dry_run": True},
                    headers=h)
    assert r.status_code == 400, r.text
    msg = r.json()["message"]
    assert "点位不齐" in msg and "A1" in msg


# ---------------------------------------------------------------- 空跑整局
def test_full_dry_game_runs_to_end(client, monkeypatch):
    h = {"X-Control-Token": _token(client)}
    ids = _seed_points()
    # ★ 把两次"等吸牢/吹气"归零，让整局在测试里跑得足够快（只测流程，不测节拍）
    monkeypatch.setattr(ttt_api, "SUCK_HOLD_MS", 0)
    monkeypatch.setattr(ttt_api, "BLOW_MS", 0)
    try:
        r = client.post("/api/tictactoe/start",
                        json={"difficulty": "hard", "dry_run": True}, headers=h)
        assert r.status_code == 200, r.text
        st0 = r.json()["state"]
        assert st0["board"] == [None] * 9
        assert st0["first"] in ("user", "robot")
        # 先手执圆：先手 = O
        if st0["first"] == "user":
            assert st0["user_sym"] == TT.O and st0["robot_sym"] == TT.X
        else:
            assert st0["robot_sym"] == TT.O and st0["user_sym"] == TT.X

        # 驱动：轮到用户就下一手空格；直到终局
        deadline = time.time() + 20
        while time.time() < deadline:
            d = client.get("/api/tictactoe/state", headers=h).json()
            st = d["state"]
            if st and st.get("finished"):
                break
            if d["running"] and st.get("turn") == "user":
                free = [i for i, v in enumerate(st["board"]) if v is None]
                if free:
                    client.post("/api/tictactoe/move", json={"cell": free[0]}, headers=h)
            time.sleep(0.03)

        d = client.get("/api/tictactoe/state", headers=h).json()
        st = d["state"]
        assert st["finished"] is True, st
        assert st["result"] in ("user_win", "robot_win", "draw")
        assert len(st["moves"]) >= 5            # 井字棋最少 5 手分胜负
        assert st["turn"] == "none"
        # 棋谱里每一手都有来源格（复位要用）
        assert all(m.get("src") for m in st["moves"])
        # 复盘接口能取到同一份棋谱
        rp = client.get("/api/tictactoe/replay", headers=h)
        assert rp.status_code == 200 and len(rp.json()["moves"]) == len(st["moves"])
    finally:
        client.post("/api/tictactoe/stop", headers=h)
        time.sleep(0.05)
        _drop(ids)


def test_game_blocks_sorting_start(client, monkeypatch):
    """对局进行中不许开始分拣（共用 40135，交错下发会互相清触发位）。"""
    h = {"X-Control-Token": _token(client)}
    ids = _seed_points()
    monkeypatch.setattr(ttt_api, "SUCK_HOLD_MS", 0)
    monkeypatch.setattr(ttt_api, "BLOW_MS", 0)
    try:
        r = client.post("/api/tictactoe/start",
                        json={"difficulty": "hard", "dry_run": True}, headers=h)
        assert r.status_code == 200, r.text
        # 立刻（对局还没跑完）尝试开始分拣 → 必须 409
        r2 = client.post("/api/sorting/start", json={"dry_run": True}, headers=h)
        assert r2.status_code == 409, r2.text
    finally:
        client.post("/api/tictactoe/stop", headers=h)
        time.sleep(0.05)
        _drop(ids)


# ---------------------------------------------------------------- 速度口径
def test_carry_uses_global_speed(monkeypatch):
    """★ 用户口径（2026-10-10）：**右上角多少，机器人速度多少** ——
    `_carry` 里每一段运动都必须用 `st["speed_pct"]`，不许再写死 5。"""
    speeds = []
    monkeypatch.setattr(ttt_api, "_move",
                        lambda ctx, pmap, name, sp, dry, stage, st=None, face=None:
                        speeds.append(sp))
    monkeypatch.setattr(ttt_api, "_air",
                        lambda ctx, action, dry, stage, st=None, face=None: None)
    monkeypatch.setattr(ttt_api, "SUCK_HOLD_MS", 0)
    monkeypatch.setattr(ttt_api, "BLOW_MS", 0)

    class _Ctx:
        def wait_pause(self): return False
        def sleep(self, s): return False

    ttt_api._carry(_Ctx(), {}, {"holding": False, "speed_pct": 40}, True, "A1", "B5",
                   "你", via_start=True)
    assert speeds and set(speeds) == {40}, speeds

    # 没带 speed_pct 时回落安全档 5（不是崩、不是 0）
    speeds.clear()
    ttt_api._carry(_Ctx(), {}, {"holding": False}, True, "A1", "B5", "你")
    assert set(speeds) == {ttt_api.DEFAULT_SPEED_PCT}, speeds


def test_start_rejects_out_of_range_speed(client):
    """速度只收 5~100（与分拣同口径）：3% / 101% 直接 422。"""
    h = {"X-Control-Token": _token(client)}
    ids = _seed_points()
    try:
        r1 = client.post("/api/tictactoe/start",
                         json={"dry_run": True, "speed_pct": 3}, headers=h)
        r2 = client.post("/api/tictactoe/start",
                         json={"dry_run": True, "speed_pct": 101}, headers=h)
        assert r1.status_code == 422 and r2.status_code == 422, (r1.text, r2.text)
    finally:
        _drop(ids)


def test_session_reports_speed(monkeypatch):
    """开局 state 里要带上本局速度（界面显示"速度 X%（跟顶栏）"用）。"""
    monkeypatch.setattr(ttt_api, "SUCK_HOLD_MS", 0)
    monkeypatch.setattr(ttt_api, "BLOW_MS", 0)
    st = ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 40)
    assert st["speed_pct"] == 40


# ---------------------------------------------------------------- 搬运行程
# ---------------------------------------------------------------- 放回原位（统一口径）
class _Ctx:
    """最小 ctx：不暂停、可取消、sleep 立即返回。"""
    def wait_pause(self): return False
    def cancelled(self): return False
    def sleep(self, s): return False


def _patch_carry(monkeypatch, cap):
    """把 _carry 的运动/吸放/放回全部换成记录器（不动机器人）。"""
    calls = {"move": [], "air": [], "put": []}
    monkeypatch.setattr(ttt_api, "_move",
                        lambda ctx, pmap, name, sp, dry, stage, st=None, face=None:
                        calls["move"].append(name))
    monkeypatch.setattr(ttt_api, "_air",
                        lambda ctx, act, dry, stage, st=None, face=None: calls["air"].append(act))
    monkeypatch.setattr(ttt_api, "_capture", lambda ctx, n, mc, stage, st=None: cap)
    monkeypatch.setattr(ttt_api, "_put_back",
                        lambda ctx, pmap, src, sp, dry, **kw: calls["put"].append(src))
    monkeypatch.setattr(ttt_api, "SUCK_HOLD_MS", 0)
    monkeypatch.setattr(ttt_api, "VERIFY_DWELL_MS", 0)
    monkeypatch.setattr(ttt_api, "BLOW_MS", 0)
    return calls


def _st(verify=True):
    return {"holding": False, "speed_pct": 40, "min_conf": 0.3, "verify_color": verify}


def test_carry_verify_unrecognized_puts_back_then_stops(monkeypatch):
    """★ 2026-10-10 用户口径：自证**判不出色 → 原路放回取料格 + 停止**（不跳过、不继续）。"""
    calls = _patch_carry(monkeypatch,
                         {"ok": False, "reason": "无有效判定（无画面）",
                          "color": "", "conf": 0.0, "votes": {}})
    with pytest.raises(ttt_api._Stop) as ei:
        ttt_api._carry(_Ctx(), {}, _st(), False, "A1", "B5", "你", via_start=True,
                       verify=True, expect_color="红", min_conf=0.3)
    assert "已放回原位 A1" in ei.value.reason, ei.value.reason
    assert calls["put"] == ["A1"]                     # 确实走了"放回原格"
    assert ttt_api.S.CAMERA_POINT in calls["move"]    # 确实送检过（去了 C 检测位）
    assert "B5上" not in calls["move"]                # ★ 关键：没有去落子


def test_carry_verify_mismatch_puts_back_then_stops(monkeypatch):
    """★ 判出色但**不是该格应有的颜色** → 同样放回 + 停，但文案必须点明"不符"。

    两句话必须分清：一个说"判不出"（查相机），一个说"不符"（查装料）。
    """
    calls = _patch_carry(monkeypatch,
                         {"ok": True, "reason": "", "color": "蓝", "conf": 0.9, "votes": {}})
    with pytest.raises(ttt_api._Stop) as ei:
        ttt_api._carry(_Ctx(), {}, _st(), False, "A1", "B5", "你", via_start=True,
                       verify=True, expect_color="红", min_conf=0.3)
    assert "自证不符" in ei.value.reason and "红" in ei.value.reason, ei.value.reason
    assert calls["put"] == ["A1"]
    assert "B5上" not in calls["move"]


def test_carry_verify_ok_places_normally(monkeypatch):
    """自证通过 → 照常经起点去 B 落子，绝不放回。"""
    calls = _patch_carry(monkeypatch,
                        {"ok": True, "reason": "", "color": "红", "conf": 0.9, "votes": {}})
    ttt_api._carry(_Ctx(), {}, _st(), False, "A1", "B5", "你", via_start=True,
                   verify=True, expect_color="红", min_conf=0.3)
    assert calls["put"] == []
    assert "B5上" in calls["move"] and ttt_api.S.START_POINT in calls["move"]


def test_carry_default_mode_never_judges(monkeypatch):
    """★ 默认模式（verify=False）**绝不调相机、绝不去 C 位** —— 与现状一致，节拍不变。"""
    calls = _patch_carry(monkeypatch, {"ok": True, "color": "红", "conf": 1.0, "votes": {}})
    ttt_api._carry(_Ctx(), {}, _st(verify=False), False, "A1", "B5", "你", via_start=True,
                   verify=False, expect_color="", min_conf=0.3)
    assert calls["put"] == []
    assert ttt_api.S.CAMERA_POINT not in calls["move"]


def test_verify_mode_needs_camera_point():
    """★ 自证模式比默认模式**多要 C 检测位**：点位体检必须把它算进去。"""
    assert ttt_api.S.CAMERA_POINT not in ttt_api._needed_points(False)
    assert ttt_api.S.CAMERA_POINT in ttt_api._needed_points(True)


def test_start_defaults_to_default_mode():
    """两种模式的默认值：**默认模式**（不判色）—— 不显式要求就不拍照。"""
    assert ttt_api.StartIn().verify_color is False
    st = ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 40)
    assert st["verify_color"] is False


def test_carry_sends_play_via_start(monkeypatch):
    """★ 用户口径（2026-10-10）：A→B 下棋**不许「正上方 → 正上方」直接平移** ——
    抬起后必须先回 **开始/结束点（起点）** 再去 B 正上方落子；复位（B→A）保持原路。

    做法：把 `_move`/`_air` 换成记录器，直接断言"走了哪几个点、按什么顺序"。
    """
    calls = []
    monkeypatch.setattr(ttt_api, "_move",
                        lambda ctx, pmap, name, sp, dry, stage, st=None, face=None:
                        calls.append(name))
    monkeypatch.setattr(ttt_api, "_air",
                        lambda ctx, action, dry, stage, st=None, face=None: None)
    monkeypatch.setattr(ttt_api, "SUCK_HOLD_MS", 0)
    monkeypatch.setattr(ttt_api, "BLOW_MS", 0)

    class _Ctx:
        def wait_pause(self): return False
        def sleep(self, s): return False

    st = {"holding": False}
    # A→B 下棋：抬起(A1上) → 起点 → B5上
    ttt_api._carry(_Ctx(), {}, st, True, "A1", "B5", "你", via_start=True)
    assert calls == ["A1上", "A1", "A1上", S.START_POINT, "B5上", S.START_POINT], calls
    assert not (calls[2] == "A1上" and calls[3] == "B5上"), "抬起后不许直接横移到 B 正上方"

    # 复位 B→A：保持原路（不经起点）
    calls.clear()
    ttt_api._carry(_Ctx(), {}, st, True, "B5", "A1", "复位", via_start=False)
    assert calls == ["B5上", "B5", "B5上", "A1上", S.START_POINT], calls


# ================================================================================
# ★★ 2026-10-11（方案 §D）井字棋「正常 / 快速」两模式 + 「只剩一个空位免等待」
#   全部离线：纯函数 / 源码位置断言 / fake ctx，不连真机、不真下发。
# ================================================================================
def test_fast_defaults_off():
    """★ 铁律：快速模式必须显式打开，**默认正常**（一趟一子，零回归）。"""
    assert ttt_api.StartIn().fast is False
    assert ttt_api.StartIn(fast=True).fast is True
    assert ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 40)["fast"] is False


def test_fast_mode_needs_j6_face_points():
    """★ 快速模式比正常模式**多要 J6正面/J6反面** 两个姿态（缺了不让开局）。"""
    base = ttt_api._needed_points(False)
    assert "J6正面" not in base and "J6反面" not in base
    fast = ttt_api._needed_points(False, fast=True)
    assert "J6正面" in fast and "J6反面" in fast


def test_fast_start_rejects_verify_combo(client):
    """★ 本轮**明确拒绝**「快速 + 拍照自证」的组合（不悄悄忽略其一）。"""
    h = {"X-Control-Token": _token(client)}
    ids = _seed_points()
    try:
        r = client.post("/api/tictactoe/start",
                        json={"dry_run": True, "fast": True, "verify_color": True}, headers=h)
        assert r.status_code == 400, r.text
        assert "二选一" in r.json().get("message", ""), r.text
    finally:
        _drop(ids)


def test_fast_mode_source_pins():
    """★★ 源码钉：快速模式一趟取两子 + 用面（机器人子正面、用户子反面）+ 免等待。"""
    import inspect

    src = inspect.getsource(ttt_api._play_two)
    assert "_move_flip_j6" in src, "快速模式必须复用 J6 翻转原语"
    assert "S.FACE_FRONT" in src and "S.FACE_BACK" in src, "两子必须用不同面（正面/反面）"
    assert "_dual_drop" in src, "必须复用双面放件原语"
    run = inspect.getsource(ttt_api._run)
    assert "一趟取两子" in run and "_play_two(" in run
    assert "免等待" in run, "缺少「只剩一个空位免等待」分支"


def test_fast_mode_does_not_force_user_first():
    """★★（本轮）快速模式**不再强制用户先行** —— 先手按**原有逻辑**随机（用户口径）。

    上一轮为了"先等你落子"把快速模式的先手硬改成 user；本轮取消：
    正常/快速都 `random.choice(["user","robot"])`。机器人先手时改走"提前显示 + 等用户 + 一趟两子"。
    """
    import inspect

    src = inspect.getsource(ttt_api.api_start)
    assert '"user" if fast' not in src and "'user' if fast" not in src, \
        "快速模式仍被强制成用户先行（本轮应取消）"
    assert "random.choice" in src, "先手必须按原有逻辑随机"


def test_fast_mode_robot_first_pre_displays_then_one_trip(monkeypatch):
    """★★（本轮）快速 + **机器人先手**：
      ① 机器人**先决定并提前把落子显示出来**（后端棋盘先写上 → 2D/3D 与棋盘同源）；
      ② 然后**等用户落子**（等待时棋盘上已能看到机器人那一手，文案如实标「已显示，待搬子」）；
      ③ 用户落子到位后 **一趟两子**（机器人子 + 用户子）一次完成。
    全部离线：`_move/_air/_play/_play_two/_wait_move` 全打桩，不连真机、不真下发。
    """
    calls = {"two": [], "play": [], "waits": []}
    monkeypatch.setattr(ttt_api, "_move", lambda *a, **k: None)
    monkeypatch.setattr(ttt_api, "_air", lambda *a, **k: None)

    def _fake_play(ctx, pmap, st_, dry, sym, cell):
        st_["board"][cell] = sym                       # 与真实 _play 同源：落子进棋盘
        st_["moves"].append({"cell": int(cell), "sym": sym, "src": "A?"})
        calls["play"].append((sym, cell))

    def _fake_two(ctx, pmap, st_, dry, rc, uc):
        st_["board"][rc] = st_["robot_sym"]
        st_["board"][uc] = st_["user_sym"]
        st_["moves"].append({"cell": int(rc), "sym": st_["robot_sym"], "src": "A?"})
        st_["moves"].append({"cell": int(uc), "sym": st_["user_sym"], "src": "A?"})
        calls["two"].append((rc, uc))

    monkeypatch.setattr(ttt_api, "_play", _fake_play)
    monkeypatch.setattr(ttt_api, "_play_two", _fake_two)

    st = ttt_api._new_state(True, "hard", TT.X, TT.O, "robot", 40, fast=True)
    board = st["board"]

    def _wait(ctx, q):
        calls["waits"].append([v for v in board])      # 记录"等待时"的棋盘
        empt = [i for i in range(9) if board[i] is None]
        return empt[0] if empt else None

    monkeypatch.setattr(ttt_api, "_wait_move", _wait)

    class _Ctx:
        def cancelled(self): return False
        def wait_pause(self): return False
        def finish(self, phase): self.phase = phase
        def set_thread(self, th): pass

    ttt_api._run(_Ctx(), None, st, True, {})

    assert calls["waits"], "机器人先手快速模式却从没等过用户"
    pre = calls["waits"][0]
    assert sum(1 for v in pre if v == TT.O) == 1, \
        "等用户时棋盘上应已「提前显示」机器人那一手（后端棋盘与 2D/3D 同源）"
    assert calls["two"], "用户落子后应走「一趟两子」"
    assert st.get("finished") is True
    assert st.get("result") in ("user_win", "robot_win", "draw")


def test_fast_mode_robot_first_source_pins():
    """★★ 源码钉：机器人先手必须**提前显示**并**如实标注**时间差（不许含糊）。"""
    import inspect

    run = inspect.getsource(ttt_api._run)
    assert "机器人已下棋完成" in run, "缺少「机器人已下棋完成」提示"
    assert "待搬子" in run, "必须如实标注「已显示，待搬子」（提前显示与真搬子有时间差）"
    assert "board[cell_r] = robot_sym" in run, "提前显示：必须先写进后端棋盘（与 2D/3D 同源）"


def test_run_last_empty_no_wait(monkeypatch):
    """★★ 「只剩一个空位」→ **不管轮到谁都免等待、直接完成**（不调 `_wait_move`）。

    棋盘只留 cell 8、轮到用户：`_run` 应**直接**落该格，绝不阻塞等用户。
    """
    calls = {"play": [], "wait": 0}
    monkeypatch.setattr(ttt_api, "_move", lambda *a, **k: None)
    monkeypatch.setattr(ttt_api, "_air", lambda *a, **k: None)
    monkeypatch.setattr(ttt_api, "_play",
                        lambda ctx, pmap, st_, dry, sym, cell: calls["play"].append((sym, cell)))
    monkeypatch.setattr(ttt_api, "_wait_move",
                        lambda ctx, q: (calls.__setitem__("wait", calls["wait"] + 1) or 0))
    st = ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 40)
    st["board"] = ["X", "O", "X", "X", "O", "O", "O", "X", None]   # 只剩 cell 8
    st["turn"] = "user"

    class _Ctx:
        def cancelled(self): return False
        def wait_pause(self): return False
        def finish(self, phase): self.phase = phase
        def set_thread(self, th): pass

    ttt_api._run(_Ctx(), None, st, True, {})
    assert calls["wait"] == 0, "只剩一个空位却还在等用户落子（免等待失效）"
    assert calls["play"] == [(TT.O, 8)], calls["play"]
    assert st.get("finished") is True


# ================================================================================
# ★★ 2026-10-11（本轮）**真实复盘**：机器人真的动，逐子把棋子搬回原位（来源 A 格）。
#   全部离线：`_carry`/`_move` 打桩 / 假 ctx / 假 pmap，不连真机、不真下发。
# ================================================================================
def test_replay_moves_each_piece_back_in_reverse_order(monkeypatch):
    """★ 复盘**真的逐子搬回**：搬子调用次数 = 已落子数，且**按落子逆序**，
    每个目标 = 该子的**原位**（来源 A 格）。

    顺序固定为 **逆序（LIFO）** —— 与既有 `_reset_run`（复位）同一口径，
    语义 = "把整局倒着撤销"（最后一手先回）。
    """
    moves = [
        {"cell": 0, "sym": "O", "by": "user", "src": "A1"},
        {"cell": 4, "sym": "X", "by": "robot", "src": "A6"},
        {"cell": 8, "sym": "O", "by": "user", "src": "A2"},
    ]
    st = ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 40)
    st["moves"] = list(moves)
    st["board"] = [None] * 9
    for m in moves:
        st["board"][m["cell"]] = m["sym"]
    st["used"] = [m["src"] for m in moves]
    st["finished"] = True

    got = {"carry": [], "move": []}
    monkeypatch.setattr(ttt_api, "_carry",
                        lambda ctx, pmap, s, dry, a, b, who, **k:
                        got["carry"].append((a, b, dry, who)))
    monkeypatch.setattr(ttt_api, "_move",
                        lambda ctx, pmap, name, sp, dry, stage, st=None, **k:
                        got["move"].append(name))

    ttt_api._replay_run(_ctx_cls(), st, {})

    assert len(got["carry"]) == len(moves), got["carry"]        # 每子一趟
    assert got["move"] and got["move"][0] == S.START_POINT      # 先回开始/结束点
    exp = list(reversed(moves))                                 # 逆序：最后一手先搬
    for (a, b, dry, who), mv in zip(got["carry"], exp):
        assert a == TT.cell_to_slot(mv["cell"]), (a, mv)        # 从棋盘 B{i+1} 取
        assert b == mv["src"], (b, mv)                          # 搬回**原位**（来源 A 格）
        assert dry is True                                      # 空跑局：不下发真实运动
        assert who == "复盘"
    # 终态如实：子都回原位 → 棋盘清空 / 复位到初始
    assert st["board"] == [None] * 9 and st["moves"] == [] and st["used"] == []
    assert st["result"] == "" and st["win_line"] == [] and st["piece_no"] == 0
    assert st["finished"] is True and st.get("ok") is True


def test_replay_dry_never_dispatches_real_motion(monkeypatch):
    """★ 空跑复盘：**只走预演路径、不下发任何真实运动**（但流程/终态与真实一致）。

    用真 `_carry`/`_move`/`_air` + 假 pmap，把 `motion.command` 换成记录器：
    空跑时它**一次都不该被调用**（若 dry 没透传下去，这里立刻红）。
    """
    import types as _types

    import app.services.motion as M

    names = ([s for s in S.A_SLOTS] + [s + "上" for s in S.A_SLOTS]
             + [s for s in S.B_SLOTS] + [s + "上" for s in S.B_SLOTS] + [S.START_POINT])
    pmap = {n: _types.SimpleNamespace(joints="[0,0,0,0,0,0]") for n in names}
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(a), {"ok": True})[1])

    st = ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 40)   # dry_run=True
    st["moves"] = [{"cell": 0, "sym": "O", "by": "user", "src": "A1"},
                   {"cell": 4, "sym": "X", "by": "robot", "src": "A6"}]
    st["board"] = ["O", None, None, None, "X", None, None, None, None]

    ttt_api._replay_run(_ctx_cls(), st, pmap)
    assert cmds == [], "空跑复盘竟下发了真实运动：%r" % cmds
    assert st["moves"] == [] and st["board"] == [None] * 9   # 终态照样清空


def test_replay_real_uses_live_dry_flag(monkeypatch):
    """★ 真实局（dry_run=False）→ `_carry` 收到 dry=False（会真下发）；
    空跑局 → dry=True。复盘**继承本局的 dry 口径**（绝不把真实局当空跑跑掉、
    否则界面说"已搬回"而实物还在棋盘上 —— 那就是编故事）。"""
    seen = []
    monkeypatch.setattr(ttt_api, "_carry",
                        lambda ctx, pmap, s, dry, a, b, who, **k: seen.append(dry))
    monkeypatch.setattr(ttt_api, "_move", lambda *a, **k: None)
    for dry in (False, True):
        seen.clear()
        st = ttt_api._new_state(dry, "normal", TT.O, TT.X, "user", 40)
        st["moves"] = [{"cell": 0, "sym": "O", "by": "user", "src": "A1"}]
        st["board"] = ["O", None, None, None, None, None, None, None, None]
        ttt_api._replay_run(_ctx_cls(), st, {})
        assert seen == [dry], (dry, seen)


def test_replay_without_moves_400(client):
    """没有可复盘的棋子 → 400（不是"跑了但没得复"）。"""
    _set_job(None)
    h = {"X-Control-Token": _token(client)}
    assert client.post("/api/tictactoe/replay", headers=h).status_code == 400


def test_replay_running_409(client):
    """对局/复盘进行中 → 409（共用 40135，不许交错下发）。"""
    th = threading.Thread(target=lambda: time.sleep(0.4))
    th.start()
    st = ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 40)
    st["moves"] = [{"cell": 0, "sym": "O", "by": "user", "src": "A1"}]
    _set_job(st, thread=th)
    h = {"X-Control-Token": _token(client)}
    try:
        assert client.post("/api/tictactoe/replay", headers=h).status_code == 409
    finally:
        th.join()
        _set_job(None)


def _msg(r):
    try:
        return (r.json() or {}).get("message", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def test_replay_rejected_when_servo_off(client, monkeypatch):
    """★ 未就绪闸门：真实复盘（非空跑）时伺服未上电 → 409 + **统一文案**
    （与 /start、/sorting/* 同一句，绝不各写一份）。空跑复盘不受拦（另测）。"""
    import app.services.motion as M
    from app.services import collector as C

    class _MB0:
        def rc_snapshot(self):
            return {"bits": {"servo": 0, "alarm": 0, "estop": 0}}, None

    monkeypatch.setattr(M, "real_write_enabled", lambda: True)
    monkeypatch.setattr(M.motion, "modbus", _MB0(), raising=False)
    monkeypatch.setattr(C.collector, "simulated", False)
    monkeypatch.setenv("EFORT_SIMULATE", "auto")

    st = ttt_api._new_state(False, "normal", TT.O, TT.X, "user", 5)     # 真实局
    st["moves"] = [{"cell": 0, "sym": "O", "by": "user", "src": "A1"}]
    st["board"] = ["O", None, None, None, None, None, None, None, None]
    _set_job(st)
    h = {"X-Control-Token": _token(client)}
    try:
        r = client.post("/api/tictactoe/replay", headers=h)
        assert r.status_code == 409, r.text
        assert _msg(r) == SERVO_NOT_READY_MSG, _msg(r)
    finally:
        _set_job(None)


def test_replay_dry_not_blocked_by_gate(client, monkeypatch):
    """★ 空跑复盘不被"未就绪"闸门拦（否则没法预演/排练流程）。"""
    import app.services.motion as M
    from app.services import collector as C

    class _MB0:
        def rc_snapshot(self):
            return {"bits": {"servo": 0, "alarm": 0, "estop": 0}}, None

    monkeypatch.setattr(M, "real_write_enabled", lambda: True)
    monkeypatch.setattr(M.motion, "modbus", _MB0(), raising=False)
    monkeypatch.setattr(C.collector, "simulated", False)
    monkeypatch.setenv("EFORT_SIMULATE", "auto")
    # 空跑复盘的 pmap 需要点位可解算 → 用离线打桩的 _carry，避免真运动
    monkeypatch.setattr(ttt_api, "_carry", lambda *a, **k: None)
    monkeypatch.setattr(ttt_api, "_move", lambda *a, **k: None)

    st = ttt_api._new_state(True, "normal", TT.O, TT.X, "user", 5)      # 空跑局
    st["moves"] = [{"cell": 0, "sym": "O", "by": "user", "src": "A1"}]
    _set_job(st)
    h = {"X-Control-Token": _token(client)}
    try:
        r = client.post("/api/tictactoe/replay", headers=h)
        assert r.status_code == 200, r.text
        assert r.json().get("dry_run") is True
    finally:
        # ★ 等复盘线程收工（否则打桩撤掉后后台线程会用真函数跑，干扰后续用例）
        with ttt_api._gate:
            th = ttt_api._job.get("thread")
        if th is not None:
            th.join(2.0)
        _set_job(None)


def test_replay_endpoint_reuses_existing_primitives():
    """★ 源码钉：真实复盘**复用既有搬子原语**（`_carry`），不新造下发通道。"""
    import inspect

    src = inspect.getsource(ttt_api._replay_run)
    assert "_carry(" in src, "复盘必须复用 _carry（= 分拣同一套 _move/_air）"
    assert "motion.command(" not in src, "复盘不得绕过 _move/_air 直接调 motion.command 下发"
    assert "reversed(moves)" in src, "复盘必须按落子逆序（LIFO）搬回（与复位同口径）"
    assert "复盘：搬回第" in src, "缺少逐子回执（第几个子 / 去向）"
    # 端点必须过统一闸门
    api = inspect.getsource(ttt_api.api_replay_run)
    assert "reject_if_not_ready(" in api, "真实复盘未接统一未就绪闸门"
