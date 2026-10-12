# -*- coding: utf-8 -*-
"""序列编辑器（「程序执行」页四类操作）的回归测试。

覆盖：
  1. 后端是操作类型的唯一入口（**只有五类**：标记点/吸气/停止吸气/放气/等待）：
     循环/条件/子程序/未知类型一律 400
  2. 参数校验：空序列、标记点缺 point_id、等待时长越界
  3. 名称与路径穿越防护
  4. 保存 → 载入 回环（落盘格式与既有 _steps_from 兼容）
  5. run-file 的 items 模式（编辑器缓冲直接跑）+ dry_run 不下发
  6. 进度/暂停/继续 接口
  7. 暂停语义：等待步**冻结计时**；停止能打断
  8. 互锁：序列执行期间手动点动/吸放被拒

★ 全程不写真机：conftest 已把真实下发总闸关掉、Modbus 指向不可达端口；
  写盘用 EFORT_PROGRAM_DIR 指到临时目录，绝不污染仓库 programs/。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import app.api.control as C


def _admin(client) -> dict:
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


@pytest.fixture
def seq_dir(tmp_path, monkeypatch):
    """把可执行程序目录指到临时目录 —— 保存/载入测试不污染仓库。"""
    d = tmp_path / "programs"
    d.mkdir()
    monkeypatch.setenv("EFORT_PROGRAM_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clean_runner_state():
    """★ 执行器状态是**进程级全局**（进行中登记 / 进度 / 暂停开关 / 最近 run_id）。

    不复位就会串味：跑过一个用例后 `_LAST_RUN_ID` 已有值，`/control/run-state`
    会返回上一次的终态，于是"空态应为 None"这类断言随机红。
    （与 conftest._reset_settings_overlay 同一套思路：用例之间不继承差异。）
    """

    def _clean():
        with C._RUN_CANCEL_LOCK:
            C._RUN_ACTIVE.clear()
            C._RUN_CANCEL.clear()
            C._RUN_PAUSE.clear()
            C._RUN_PROGRESS.clear()
        C._LAST_RUN_ID = None

    _clean()
    yield
    _clean()


# ---------------------------------------------------------------- 1. 只四类
@pytest.mark.parametrize("bad", [
    {"type": "loop"}, {"type": "if"}, {"type": "call"}, {"type": "goto"},
    {"type": "setvar"}, {"type": "unknown"},
])
def test_seq_rejects_non_allowed_types(client, seq_dir, bad):
    """★ 五类之外的操作一律拒绝 —— 这是"不要乱添加别的东西"的后端保证。"""
    r = client.post("/api/control/seq", json={"name": "t1", "items": [bad]}, headers=_admin(client))
    assert r.status_code == 400, r.text
    assert "只允许" in r.text


def test_seq_accepts_all_five_types(client, seq_dir):
    """五类各一条 → 全部接受，且落盘格式与 _steps_from 兼容。"""
    items = [{"type": "point", "point_id": 1}, {"type": "suck"},
             {"type": "release"}, {"type": "blow"}, {"type": "wait", "seconds": 1.5}]
    r = client.post("/api/control/seq", json={"name": "four", "items": items},
                    headers=_admin(client))
    assert r.status_code == 200, r.text
    with open(seq_dir / "four.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["items"] == [
        {"point_id": 1}, {"op": "suck"}, {"op": "release"}, {"op": "blow"},
        {"op": "wait", "dwell_ms": 1500},
    ]
    # 四条 io 步骤必须能被既有 _steps_from 解析成 suck/release/blow/wait
    #（点位步需要数据库会话，这里单独验它的归一化字段即可）
    steps = C._steps_from(data["items"][1:], None)
    assert [s.get("op") for s in steps] == ["suck", "release", "blow", "wait"]
    assert [s.get("name") for s in steps] == ["吸", "停止吸", "放", "等待"]
    assert [s.get("index") for s in steps] == [1, 2, 3, 4]
    assert steps[3]["dwell_ms"] == 1500
    assert all(s.get("ok") is not False for s in steps), steps
    # ★ 兼容 XPL 解析器的老写法：{"op":"suck","on":false} = 停止吸气
    old = C._steps_from([{"op": "suck", "on": True}, {"op": "suck", "on": False}], None)
    assert [s.get("op") for s in old] == ["suck", "release"]
    # ★ 放气（2026-09-29 新增）：显式 blow 与其别名都要归一化为 blow
    bl = C._steps_from([{"op": "blow"}, {"op": "blowoff"}, {"op": "purge"}], None)
    assert [s.get("op") for s in bl] == ["blow", "blow", "blow"]
    assert bl[0]["name"] == "放" and bl[0]["ok"] is not False
    # ★ 停止放（unblow）也要能归一化（2026-09-29 增加第四路气路）
    ub = C._steps_from([{"op": "unblow"}, {"op": "stop_blow"}], None)
    assert [s.get("op") for s in ub] == ["unblow", "unblow"]
    assert ub[0]["name"] == "停止放"
    # ★ 真正未知的 op 必须显式标失败（不能静默当成功）
    unk = C._steps_from([{"op": "purge_xyz"}, {"op": "loop"}], None)
    assert unk[0]["ok"] is False and unk[0]["op"] == "unknown"
    assert unk[1]["ok"] is False


# ---------------------------------------------------------------- 2/3. 校验
def test_seq_input_validation(client, seq_dir):
    h = _admin(client)
    cases = [
        ({"name": "x", "items": []}, "空序列"),
        ({"name": "x", "items": [{"type": "point"}]}, "标记点缺 point_id"),
        ({"name": "x", "items": [{"type": "point", "point_id": "abc"}]}, "point_id 非数字"),
        ({"name": "x", "items": [{"type": "wait", "seconds": 0}]}, "等待过短"),
        ({"name": "x", "items": [{"type": "wait", "seconds": 99999}]}, "等待过长"),
        ({"name": "x", "items": [{"type": "wait", "seconds": "abc"}]}, "等待非数字"),
        # 名称长度由 Pydantic 边界挡（422），语义上同样是"拒绝"
        ({"name": "", "items": [{"op": "suck"}]}, "空名称"),
        ({"name": "a" * 41, "items": [{"op": "suck"}]}, "名称过长"),
    ]
    for body, label in cases:
        r = client.post("/api/control/seq", json=body, headers=h)
        assert r.status_code in (400, 422), "%s 未被拒: %s" % (label, r.text)


def test_seq_name_rejects_path_traversal(client, seq_dir):
    """名称里的路径片段必须被拒（且不能逃出程序目录）。"""
    h = _admin(client)
    for bad in ["../../evil", "a/b", "..\\evil", "..", "/etc/passwd"]:
        r = client.post("/api/control/seq",
                        json={"name": bad, "items": [{"op": "suck"}]}, headers=h)
        assert r.status_code == 400, "%r 未被拒: %s" % (bad, r.text)
    # 目录里不应出现任何越界文件
    assert not list(seq_dir.parent.glob("evil*"))


def test_seq_path_helper_blocks_escape(seq_dir):
    assert C._seq_path("../evil") in (None,) or \
        str(C._seq_path("../evil")).startswith(str(seq_dir))
    p = C._seq_path("ok-name")
    assert p and p.endswith("ok-name.json") and p.startswith(str(seq_dir))


# ---------------------------------------------------------------- 4. 回环
def test_seq_save_load_roundtrip(client, seq_dir):
    h = _admin(client)
    items = [{"type": "point", "point_id": 7}, {"type": "wait", "seconds": 2.5},
             {"type": "suck"}]
    r = client.post("/api/control/seq",
                    json={"name": "中文 序列-1", "items": items, "speed_pct": 5}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["steps"] == 3 and d["file"] == "中文 序列-1.json"

    r2 = client.get("/api/control/seq", params={"name": "中文 序列-1"}, headers=h)
    assert r2.status_code == 200, r2.text
    got = r2.json()
    assert got["name"] == "中文 序列-1"
    assert got["items"] == [{"point_id": 7}, {"op": "wait", "dwell_ms": 2500},
                            {"op": "suck"}]

    # 覆盖保存要如实回报
    r3 = client.post("/api/control/seq",
                     json={"name": "中文 序列-1", "items": items}, headers=h)
    assert r3.json().get("existed") is True
    # overwrite=False 时拒绝
    r4 = client.post("/api/control/seq",
                     json={"name": "中文 序列-1", "items": items, "overwrite": False}, headers=h)
    assert r4.status_code == 409


def test_seq_load_missing(client, seq_dir):
    r = client.get("/api/control/seq", params={"name": "__none__"}, headers=_admin(client))
    assert r.status_code == 404


# ---------------------------------------------------------------- 5. items 模式
def _wait_result(client, h, run_id, timeout=10.0):
    """★ 2026-10-10（B1 异步化）：`/run-file` 立即返回，**结果改从 `/run-state` 取**。"""
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        r = client.get("/api/control/run-state", params={"run_id": run_id}, headers=h)
        assert r.status_code == 200, r.text
        st = (r.json() or {}).get("state") or {}
        if st.get("result"):
            return st["result"]
        _t.sleep(0.05)
    raise AssertionError("等待执行结果超时：%s" % run_id)


def test_runfile_items_mode_dry_run(client, seq_dir):
    """编辑器缓冲可直接执行（items 模式），dry_run 只解析不下发。

    ★ 2026-10-10（B1 性能根治）：`/run-file` 已**异步化** —— POST 只回
      `{ok, started, run_id, kind}`；逐步结果去 `/control/run-state` 的
      `state.result` 取（字段与老的响应体一致）。同步的 404/400 语义不变。
    """
    body = {"items": [{"type": "point", "point_id": 1}, {"type": "suck"},
                      {"type": "wait", "seconds": 0.2}, {"type": "release"}],
            "name": "ed", "dry_run": True, "speed_pct": 5}
    r = client.post("/api/control/run-file", json=body, headers=_admin(client))
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["ok"] is True and start["started"] is True and start["run_id"]
    assert start["kind"] == "program"
    assert "steps" not in start, "异步化后 POST 不再带逐步结果（应去 /run-state 取）"
    d = _wait_result(client, _admin(client), start["run_id"])
    assert d["kind"] == "program" and d["count"] == 4 and d["readonly"] is True
    assert [s.get("op") for s in d["steps"]] == [None, "suck", "wait", "release"]


def test_runfile_needs_a_target(client, seq_dir):
    r = client.post("/api/control/run-file", json={"dry_run": True}, headers=_admin(client))
    assert r.status_code == 422, r.text


def test_runfile_items_rejects_unknown_op(client, seq_dir):
    r = client.post("/api/control/run-file",
                    json={"items": [{"type": "loop"}], "dry_run": True},
                    headers=_admin(client))
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------- 6/7. 暂停/停止
def test_run_state_empty(client):
    r = client.get("/api/control/run-state", headers=_admin(client))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["state"] is None


def test_pause_resume_without_run(client):
    h = _admin(client)
    r = client.post("/api/control/run-pause", json={}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is False
    r = client.post("/api/control/run-resume", json={}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is False


def test_sleep_pausable_freezes_and_cancels():
    """★ 暂停时**冻结剩余时间**（不是"暂停完发现已经等过了"）；停止能立刻打断。"""
    rid = "unit-sleep-1"
    try:
        # 暂停中：3 秒的等待不会推进
        C._pause_event(rid).set()
        t0 = time.time()
        th = threading.Thread(target=C._sleep_pausable, args=(3.0, rid), daemon=True)
        th.start()
        time.sleep(0.6)
        assert th.is_alive(), "暂停中不该结束"
        C._pause_event(rid).clear()
        th.join(timeout=5.0)
        assert not th.is_alive()
        # 解除暂停后仍要把 3 秒走完（≈3s，而不是 0.6s 就结束）
        assert time.time() - t0 >= 2.5, "剩余时间被吞了：暂停没有冻结计时"

        # 停止：立即返回 True
        rid2 = "unit-sleep-2"
        with C._RUN_CANCEL_LOCK:
            C._RUN_CANCEL[rid2] = time.time()
        t1 = time.time()
        assert C._sleep_pausable(5.0, rid2) is True
        assert time.time() - t1 < 0.5, "停止没有立刻打断等待"
    finally:
        with C._RUN_CANCEL_LOCK:
            C._RUN_PAUSE.pop(rid, None)
            C._RUN_PAUSE.pop("unit-sleep-2", None)
            C._RUN_CANCEL.pop(rid2, None)


def test_wait_if_paused_returns_cancelled():
    rid = "unit-wait-1"
    try:
        C._pause_event(rid).set()
        with C._RUN_CANCEL_LOCK:
            C._RUN_CANCEL[rid] = time.time()
        t0 = time.time()
        assert C._wait_if_paused(rid) is True
        assert time.time() - t0 < 0.5
    finally:
        with C._RUN_CANCEL_LOCK:
            C._RUN_PAUSE.pop(rid, None)
            C._RUN_CANCEL.pop(rid, None)


def test_pause_progress_registry():
    rid = "unit-prog-1"
    try:
        C._set_progress(rid, name="p", running=True, total=4, index=2, paused=False)
        with C._RUN_CANCEL_LOCK:
            st = dict(C._RUN_PROGRESS[rid])
        assert st["total"] == 4 and st["index"] == 2 and st["running"] is True
        # paused 由 Event 决定（不是直接写字段）
        C._pause_event(rid).set()
        assert C._is_paused(rid) is True
    finally:
        with C._RUN_CANCEL_LOCK:
            C._RUN_PROGRESS.pop(rid, None)
            C._RUN_PAUSE.pop(rid, None)


# ---------------------------------------------------------------- 8. 互锁
def test_manual_jog_rejected_while_run_active(client):
    """序列执行期间手动点动被拒（共用 40135 触发位，不能交错）。"""
    h = _admin(client)
    rid = "unit-active-1"
    with C._RUN_CANCEL_LOCK:
        C._RUN_ACTIVE[rid] = {"filename": "unit", "started_at": time.time()}
    try:
        r = client.post("/api/control/jog/step", json={"joint": 6, "dir": 1, "amount": 1},
                        headers=h)
        assert r.status_code == 409, r.text
        assert "正在执行" in r.text
        r = client.post("/api/control/vacuum", json={"action": "suck"}, headers=h)
        assert r.status_code in (409, 403), r.text
    finally:
        with C._RUN_CANCEL_LOCK:
            C._RUN_ACTIVE.pop(rid, None)


def test_jog_stop_not_blocked_by_run(client):
    """★ 停永远要能停：/jog/stop 不被互锁挡。"""
    h = _admin(client)
    rid = "unit-active-2"
    with C._RUN_CANCEL_LOCK:
        C._RUN_ACTIVE[rid] = {"filename": "unit", "started_at": time.time()}
    try:
        r = client.post("/api/control/jog/stop", json={}, headers=h)
        assert r.status_code == 200, r.text
    finally:
        with C._RUN_CANCEL_LOCK:
            C._RUN_ACTIVE.pop(rid, None)


# ---------------------------------------------------------------- 放气动作闸门


def test_vacuum_accepts_blow_action(client):
    """★ /control/vacuum 必须接受 suck / release / **blow**，其余入参一律 422。

    坑（2026-09-29 差点漏掉）：VacuumIn.action 的正则是 `^(suck|release)$`，
    新增的放气动作会被 Pydantic 直接 422 挡掉 —— 后端逻辑全对、前端按钮也画出来了，
    但一按就"请求非法"。本条把这个入参闸门钉死。
    """
    h = _admin(client)
    # ★★ 2026-10-11：动作集扩到 8 个（含反面那一路）—— 正则必须全部放行，否则前端点了就 422。
    for act in ("suck", "release", "blow", "unblow",
                "suck_back", "release_back", "blow_back", "unblow_back"):
        r = client.post("/api/control/vacuum", json={"action": act}, headers=h)
        assert r.status_code != 422, "%s 不该被入参拒：%s" % (act, r.text)
        # 测试环境真实下发总闸关闭 → 403；有令牌但闸关是预期行为
        assert r.status_code in (403, 409, 502), "%s 意外状态 %s" % (act, r.status_code)
    r = client.post("/api/control/vacuum", json={"action": "purge"}, headers=h)
    assert r.status_code == 422, "未知动作必须 422：%s" % r.text


def test_vacuum_blow_maps_to_bit3(client):
    """★ 位号必须与用户权威位表一致：下板 1~4 位 / 反面 5~8 位。

    2026-10-11 起位号收敛到 `modbus.VAC_ACTIONS`（**单一映射口**）——
    本钉改为**查表断言**（不再在 rc_vacuum 源码里找字面量，那正是要消灭的"散落硬编码"）。
    """
    from app.services.modbus import VAC_ACTIONS
    bits = {k: v[0] for k, v in VAC_ACTIONS.items()}
    assert bits["suck"] == 0x0002 and bits["release"] == 0x0004
    assert bits["blow"] == 0x0008 and bits["unblow"] == 0x0010
    assert bits["suck_back"] == 0x0020 and bits["release_back"] == 0x0040
    assert bits["blow_back"] == 0x0080 and bits["unblow_back"] == 0x0100
    import inspect
    import app.services.modbus as M
    src = inspect.getsource(M.ModbusRobot.rc_vacuum)
    assert "VAC_ACTIONS" in src, "rc_vacuum 必须走单一映射口 VAC_ACTIONS（不得再写死位号）"
