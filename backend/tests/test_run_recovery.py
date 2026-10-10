# -*- coding: utf-8 -*-
"""执行登记**自愈** + **统一场景状态** 回归（2026-10-10 现场死机修复）。

现场三条现象（用户原话）：
  "程序正在执行中，但是没跑代码，就绪和取消都成功，无法下达指令"

根因：`_RUN_ACTIVE` 里残留了一条**没有活线程**的登记 →
  · `_reject_if_run_active()` 只看它非空 → **永久**拒绝所有手动下发；
  · 而 `/ready`、`/ready/cancel` 根本不看 `_RUN_ACTIVE` → 就绪/取消照样"成功"。
三条现象全对上。修复分三层：① finally 里 `ctx.finish()` 置顶不可跳过；
② 闸门自愈（死登记当场清掉）；③ `/control/force-release` 兜底。
"""
from __future__ import annotations

import inspect
import threading
import time

import pytest
from fastapi import HTTPException

from app.api import control as C


@pytest.fixture(autouse=True)
def _clean():
    """每条用例前后清干净全局登记（它们是模块级共享状态，会串味）。"""
    def _reset():
        with C._RUN_CANCEL_LOCK:
            C._RUN_ACTIVE.clear()
            C._RUN_CANCEL.clear()
            C._RUN_PAUSE.clear()
            C._RUN_PROGRESS.clear()
    _reset()
    yield
    _reset()


def _register(run_id, *, thread=None, age=0.0, kind="ai"):
    """照真实运行的样子登记：`_RUN_ACTIVE` + 一条 `_RUN_PROGRESS`（running=True）。"""
    with C._RUN_CANCEL_LOCK:
        C._RUN_ACTIVE[run_id] = {"filename": "unit", "kind": kind,
                                 "started_at": time.time() - age,
                                 "heartbeat": time.time() - age,
                                 "thread": thread}
        C._RUN_PROGRESS[run_id] = {"run_id": run_id, "running": True, "paused": False}


# ---------------------------------------------------------------- ② 闸门自愈
def test_dead_run_is_reaped_and_manual_is_allowed_again():
    """★ 死登记（线程已结束）→ 自愈清掉 → 手动下发不再被拒（现场死机的主修复）。"""
    th = threading.Thread(target=lambda: None)
    th.start()
    th.join()                        # 线程结束 = 已死
    _register("run-dead", thread=th)
    assert C._purge_dead_runs() == ["run-dead"]
    C._reject_if_run_active("点动")    # 不抛 = 通过
    assert "run-dead" not in C._RUN_ACTIVE
    # 终态也要如实落下（否则界面一直显示"执行中"）
    st = C._RUN_PROGRESS.get("run-dead") or {}
    assert st.get("running") is False and st.get("phase") == "reaped"


def test_live_run_still_blocks_manual():
    """★ 活登记（线程仍在跑）→ **绝不清**，手动下发照旧被拒
    （绝不能把正在跑的机器人交给手动指令）。"""
    ev = threading.Event()
    th = threading.Thread(target=lambda: ev.wait(3.0))
    th.start()
    try:
        _register("run-live", thread=th)
        assert C._purge_dead_runs() == []
        with pytest.raises(HTTPException) as ei:
            C._reject_if_run_active("点动")
        assert ei.value.status_code == 409
        assert "run-live" in C._RUN_ACTIVE
    finally:
        ev.set()
        th.join()


def test_no_thread_falls_back_to_heartbeat_and_spares_paused():
    """无线程登记的兜底口径：心跳新鲜不杀；停摆才杀；**暂停中**一律不杀。"""
    _register("run-fresh", thread=None, age=0.0)
    C._pause_event("run-fresh").set()                       # 暂停（正常状态）
    _register("run-stale", thread=None, age=C._RUN_STALE_S + 10)
    C._pause_event("run-stale").set()
    assert C._purge_dead_runs() == []                       # 两条都因"暂停"豁免
    C._RUN_PAUSE["run-stale"].clear()                       # 取消暂停
    assert C._purge_dead_runs() == ["run-stale"]            # 停摆那条被清
    assert "run-fresh" in C._RUN_ACTIVE


# ---------------------------------------------------------------- ① 收尾顺序
def test_finish_is_first_statement_of_finally_in_every_executor():
    """★ 源码位置钉子：三个执行器的 finally 里，**第一件事**必须是 `ctx.finish(...)`。

    否则收尾里任何一步（关气 / 写结束态 / 接管接力 / 写事件）抛异常，
    注销就永远执行不到 → `_RUN_ACTIVE` 残留 → 手动指令被永久拒绝。
    """
    from app.api import ai as A
    from app.api import sorting as S
    from app.api import tictactoe as T

    targets = [(A._run, "ai._run"),
               (S._run_sorting, "sorting._run_sorting"),
               (T._run, "tictactoe._run"),
               (T._reset_run, "tictactoe._reset_run")]
    for fn, label in targets:
        src = inspect.getsource(fn)
        body = src[src.index("finally:"):]
        # 去掉注释/空行后看第一条真实语句
        lines = [ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        assert lines[0] == "finally:", label
        assert lines[1].startswith("ctx.finish("), (
            "%s：finally 的第一件事必须是 ctx.finish(...)（现为 %r）" % (label, lines[1]))


# ---------------------------------------------------------------- ③ 兜底复位
def test_force_release_only_reaps_dead_and_refuses_when_alive():
    """★ `/control/force-release` 的口径：只清已死的；还有活执行就拒绝。"""
    th = threading.Thread(target=lambda: None)
    th.start()
    th.join()
    _register("run-dead", thread=th)

    reaped = C._purge_dead_runs()
    with C._RUN_CANCEL_LOCK:
        alive = list(C._RUN_ACTIVE.keys())
    assert reaped == ["run-dead"] and alive == []           # 已死 → 清掉、无活执行

    ev = threading.Event()
    th2 = threading.Thread(target=lambda: ev.wait(3.0))
    th2.start()
    try:
        _register("run-live", thread=th2)
        with C._RUN_CANCEL_LOCK:
            alive2 = list(C._RUN_ACTIVE.keys())
        assert alive2 == ["run-live"], "有活执行时必须如实报告（据此拒绝复位）"
    finally:
        ev.set()
        th2.join()


# ---------------------------------------------------------------- 统一场景状态
def test_scene_source_registry_roundtrip():
    """★ 统一场景状态：登记过的源能被 `_SCENE_SRC` 取出（前端 3D 只认这一个口）。"""
    C.register_scene_source("unit-kind", lambda: {"stage": "取料", "piece": 2})
    _register("run-x", thread=None, kind="unit-kind")
    with C._RUN_CANCEL_LOCK:
        items = [(rid, dict(v)) for rid, v in C._RUN_ACTIVE.items()]
    src = str(items[-1][1].get("kind"))
    fn = C._SCENE_SRC.get(src)
    assert callable(fn) and fn()["stage"] == "取料"


def test_both_sorting_and_ai_register_a_scene_source():
    """★ 分拣与 AI **都**必须登记场景源 —— 否则某种执行在跑时 3D 又会一动不动。"""
    from app.api import ai as A          # noqa: F401  （导入即注册）
    from app.api import sorting as S     # noqa: F401
    assert callable(C._SCENE_SRC.get("sorting"))
    assert callable(C._SCENE_SRC.get("ai"))
