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

import time

from app.api import tictactoe as ttt_api
from app.db.crud import create_point, delete_point, list_points
from app.db.database import SessionLocal
from app.services import sorting as S
from app.services import tictactoe as TT


def _token(c) -> str:
    r = c.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


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
                        lambda ctx, pmap, name, sp, dry, stage, st=None: speeds.append(sp))
    monkeypatch.setattr(ttt_api, "_air",
                        lambda ctx, action, dry, stage, st=None: None)
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
def test_carry_sends_play_via_start(monkeypatch):
    """★ 用户口径（2026-10-10）：A→B 下棋**不许「正上方 → 正上方」直接平移** ——
    抬起后必须先回 **开始/结束点（起点）** 再去 B 正上方落子；复位（B→A）保持原路。

    做法：把 `_move`/`_air` 换成记录器，直接断言"走了哪几个点、按什么顺序"。
    """
    calls = []
    monkeypatch.setattr(ttt_api, "_move",
                        lambda ctx, pmap, name, sp, dry, stage, st=None: calls.append(name))
    monkeypatch.setattr(ttt_api, "_air",
                        lambda ctx, action, dry, stage, st=None: None)
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
