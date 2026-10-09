# -*- coding: utf-8 -*-
"""AI 执行「接管」机制回归（2026-10-09 新增）。

用户口径（原话）：
> "如果触发过程当中用户让它停，或者下达了别的指令，**必须回到起点再开始执行用户的命令**，
>  保证丝滑完成任务"

实现要点（本文件把这三条钉死）：
  ① 执行中再来指令 → **不 409 拒绝**，而是"排队 + 请求取消"（步边界生效、随后回起点）；
  ② 旧任务收尾（`_run` 的 finally）时**自动接力**启动排队的新指令 ——
     那是唯一可靠的"已经回完起点"时刻；
  ③ **急停永远不排队**（它在接管分支之前就返回，立即执行）。

★ 为什么这几条必须有回归：
  接管是本页唯一"旧任务还没停、新任务就要起来"的地方，写错的后果是
  **两个任务同时下发指令**（真机上是不可接受的）。而且它天然难手工复现
  （要正好卡在"执行中来新指令"的时机）—— 只能靠测试钉住。
"""
from __future__ import annotations

import inspect
import time

from app.api import ai as A


# ---------------------------------------------------------------- A. 接力（_relay_pending）
def test_relay_starts_pending_job(monkeypatch):
    """★ 有排队时：接力必须真的把新任务启动起来。"""
    started = {}

    def _fake_start(rp, pmap, st, dry, speed):
        started.update({"rp": rp, "dry": dry, "speed": speed})
        return type("C", (), {"run_id": "x"})()

    monkeypatch.setattr(A, "_start_job", _fake_start)
    monkeypatch.setattr(A, "_load_pmap", lambda: {"A1": object()})
    monkeypatch.setitem(A._job, "pending",
                        {"rp": {"action": "move_one"}, "dry": True, "speed": 7,
                         "raw": "把A1放到B1", "at": time.time()})
    A._relay_pending()
    assert started.get("rp") == {"action": "move_one"}
    assert started.get("dry") is True and started.get("speed") == 7
    assert A._job.get("pending") is None, "接力后必须清空 pending（否则会重复执行）"


def test_relay_noop_without_pending(monkeypatch):
    """没有排队 → 什么都不做（绝不能凭空启动任务）。"""
    called = {"n": 0}
    monkeypatch.setattr(A, "_start_job", lambda *a, **k: called.__setitem__("n", 1))
    monkeypatch.setitem(A._job, "pending", None)
    A._relay_pending()
    assert called["n"] == 0


def test_relay_drops_expired_pending(monkeypatch):
    """★ 排队过期（用户早走了/改主意了）→ **不执行**，并如实记事件。

    这条比"执行"更重要：让机器在几分钟后突然自己动起来，比不执行危险得多。
    """
    called = {"n": 0, "events": []}
    monkeypatch.setattr(A, "_start_job", lambda *a, **k: called.__setitem__("n", 1))
    monkeypatch.setattr(A, "emit_event",
                        lambda *a, **k: called["events"].append(a))
    monkeypatch.setitem(A._job, "pending",
                        {"rp": {"action": "move_one"}, "dry": True, "speed": 5,
                         "raw": "旧指令", "at": time.time() - (A._PENDING_TTL + 30)})
    A._relay_pending()
    assert called["n"] == 0, "过期排队不许执行"
    assert any("relay_skip" in str(e) for e in called["events"]), "必须留下事件痕迹"


def test_relay_survives_start_failure(monkeypatch):
    """接力启动失败 → 不能把异常抛回 `_run` 的 finally（那会吞掉原任务的收尾）。"""
    def _boom(*_a, **_k):
        raise RuntimeError("注入：启动失败")

    monkeypatch.setattr(A, "_start_job", _boom)
    monkeypatch.setattr(A, "_load_pmap", lambda: {})
    monkeypatch.setattr(A, "emit_event", lambda *a, **k: None)
    monkeypatch.setitem(A._job, "pending",
                        {"rp": {"action": "move_one"}, "dry": True, "speed": 5,
                         "raw": "x", "at": time.time()})
    A._relay_pending()          # 不抛 = 通过


def test_pending_ttl_is_bounded():
    """排队有效期必须存在且是个"合理的人还在现场"的量级。"""
    assert 10 <= A._PENDING_TTL <= 600, "太长（人走了机器自己动）/ 太短（来不及回起点）都不行"


# ---------------------------------------------------------------- B. 源码位置钉子
def test_relay_called_at_end_of_finally():
    """★ 接力必须挂在 `_run` 的 **finally 的最后**：那是唯一可靠的"已回起点"时刻。

    用源码位置断言（运行时不好卡这个时机）：finally 之后、函数结束之前要有 _relay_pending()。
    """
    src = inspect.getsource(A._run)
    i_fin = src.index("finally:")
    i_relay = src.index("_relay_pending()")
    assert i_relay > i_fin, "接力必须在 finally 里（且要在收尾状态写完之后）"
    # 收尾的 stage="结束" 必须先写，接力再启动新任务去覆盖它
    assert src.index('stage="结束"') < i_relay


def test_estop_returns_before_takeover_branch():
    """★★ 急停必须在接管分支**之前**返回 —— 绝不能"排队等一会儿再急停"。"""
    src = inspect.getsource(A.api_exec)
    i_estop = src.index('act == "estop"')
    i_takeover = src.index("_job[\"pending\"]")
    assert i_estop < i_takeover, "急停的分支必须排在接管逻辑之前"


def test_takeover_records_pending_and_requests_cancel():
    """★ 接管分支本身：记 pending + 请求取消 + 返回 queued（三件事都要做全）。"""
    src = inspect.getsource(A.api_exec)
    assert "_job[\"pending\"] = {" in src, "必须把已校验的计划记进 pending"
    assert "request_cancel()" in src, "必须请求取消当前任务（步边界 → 回起点）"
    assert '"queued": True' in src, "必须告诉前端'已排队'，而不是假装已执行"
    # ★ 明确区分 takeover=false 的旧行为（仍然 409）
    assert "if not body.takeover" in src and "409" in src, "不支持接管时要保持原来的拒绝语义"


def test_cancel_clears_pending():
    """★ 用户说"停"就是要全停：必须把排队的接管计划一起丢掉。

    否则会出现"点了停止，当前任务停了，几秒后机器又自己动起来"—— 现场会认为停不掉。
    """
    src = inspect.getsource(A.api_cancel)
    assert "_job[\"pending\"] = None" in src
    assert "request_cancel()" in src
    assert '"dropped"' in src, "要如实回报'顺手丢掉了哪条排队指令'"


# ---------------------------------------------------------------- C. 数据结构
def test_exec_in_has_takeover_flag():
    """`/exec` 必须带 takeover 开关（默认 True），且能被显式关掉。"""
    assert A.ExecIn(plan_id="p" * 8).takeover is True
    assert A.ExecIn(plan_id="p" * 8, takeover=False).takeover is False


def test_new_state_shape_matches_old():
    """`_new_state` 与原来写在接口里的那段必须是同一份口径（接管接力靠它）。"""
    st = A._new_state(True)
    for k in ("dry_run", "stage", "piece", "total", "color", "conf", "target",
              "src", "holding", "suspect_pick", "error", "finished", "ok",
              "judge", "sim_color"):
        assert k in st, "缺字段 %s" % k
    assert st["dry_run"] is True and st["stage"] == "准备"
    assert st["judge"] is False, "空跑不许判色"
