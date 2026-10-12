# -*- coding: utf-8 -*-
"""「界面滑块」登记处（`services/ui_speed.py`）+ 各流程接线 —— 回归钉。

现场原话（按时间）：
  ①「点位执行的速度正常啊…**但是智能分拣不行啊，还有游戏**」→ 长流程"开始那一刻定速"，中途改滑块不生效；
  ②「**应该电脑的速度去执行**」→ 要"运行中拖滑块 → 下一步生效"；
  ③「快速分拣为什么又变成 20%，我调试的 50%？**偶尔出现这个问题啊**」
     → 上一版让**每个流程自报**速度 → "谁最后说谁说了算" → 别的流程/后台一自报就把 50% 顶成 20%。

本版口径：**本模块只表示"界面滑块现在的位置"，只由前端拖动来写**；
流程"是否跟随"按**时间戳**判断（流程跑起来之后人有没有动过滑块）。
"""
from __future__ import annotations

import inspect
import time

import pytest

from app.services import ui_speed


@pytest.fixture(autouse=True)
def _clean_ui_speed():
    """每个用例前后都清空登记 —— 这是**进程级全局状态**，不清会串味到别的用例。"""
    ui_speed.reset()
    yield
    ui_speed.reset()


# ---------------------------------------------------------------- 纯函数
def test_apply_falls_back_when_never_published():
    """★★ 零回归钉：**从没推送过** → `apply` 原样返回传入值（既有路径行为一字不变）。"""
    assert ui_speed.apply(30, {}) == 30
    assert ui_speed.apply(5, {"a": 1}) == 5
    assert ui_speed.apply(100) == 100          # st 都不给 → 同样原样返回


def test_first_call_does_not_follow_it_establishes_the_baseline():
    """★★ 基准钉：流程**第一次**取速度时**不跟随**历史值（只建基准）——

    否则"界面以前拖过的值"会覆盖"流程本次请求里的速度"（脚本场景尤其致命）。
    """
    ui_speed.publish(55)                      # 人以前拖过 55
    st = {}
    assert ui_speed.apply(30, st) == 30, "首次必须用流程自己的值（并建基准）"
    assert st[ui_speed.BASE_KEY] is not None, "首次必须记下基准时刻"
    assert ui_speed.apply(30, st) == 30, "没人再动滑块 → 继续用流程自己的值"


def test_follows_only_after_someone_moves_the_slider_during_the_flow():
    """★★ 核心钉（用户口径「应该电脑的速度去执行」）：
    流程跑起来**之后**人再拖滑块 → **下一步起用新值**。"""
    ui_speed.publish(10)
    st = {}
    assert ui_speed.apply(10, st) == 10        # 建基准（此刻滑块=10）
    time.sleep(0.005)                          # 让时间戳能分清先后（避免同 µs 的抖动）
    ui_speed.publish(50)                       # 人把滑块拖到 50
    assert ui_speed.apply(10, st) == 50, "流程跑动中人动过滑块 → 跟随（下一步生效）"
    assert ui_speed.apply(10, st) == 50


def test_other_flow_publishing_cannot_hijack_a_running_flow():
    """★★★ **事故复现钉**（用户：「快速分拣为什么又变成 20%，我调试的 50%？偶尔出现」）。

    根因：上一版每个流程开始时都 `publish` 自己的速度 → **谁最后说谁说了算**，
    于是"分拣跑到一半，别的流程 / 后台视觉规则一自报"就把操作员的 50% 顶成 20%。
    本版：**能写登记的只有界面拖动那一条路**（见下面的源码钉），且流程首次只建基准。
    """
    ui_speed.publish(50)                       # 操作员拖到 50
    st = {}
    assert ui_speed.apply(50, st) == 50
    # 模拟"另一个流程带着 20% 启动"——本版**不会再 publish**（源码钉见下），
    # 所以这里的登记值必须**仍然是 50**，分拣也照样按 50 跑。
    assert ui_speed.peek()["speed_pct"] == 50, "别的流程不许改写界面滑块值"
    assert ui_speed.apply(50, st) == 50


def test_apply_clamps_and_handles_bad_input():
    """★ 安全钉：一律夹到 [5,100]（滑块拖到 1 也不许下去；与后端 SPEED_MIN 同源）。"""
    assert ui_speed.publish(1) == 5
    assert ui_speed.publish(0) == 5
    assert ui_speed.publish(-20) == 5
    assert ui_speed.publish(200) == 100
    assert ui_speed.publish(None) == 5          # 非法值 → 安全下限，绝不抛给调用方
    assert ui_speed.publish("abc") == 5
    ui_speed.reset()
    assert ui_speed.apply("bad") == 5           # fallback 非法 → 夹到安全下限


def test_reset_restores_zero_regression():
    """reset（仅测试用）→ 回到"没登记"状态 → apply 又原样返回 fallback。"""
    ui_speed.publish(66)
    assert ui_speed.apply(30, {}) == 30        # 首次仍用 fallback
    ui_speed.reset()
    assert ui_speed.apply(30, {}) == 30
    assert ui_speed.peek()["speed_pct"] is None and ui_speed.at() == 0.0


def test_peek_reports_value_and_note():
    """回显（供接口/自检）：值 + 来源备注 + 时间戳都在。"""
    ui_speed.publish(25, "前端滑块")
    got = ui_speed.peek()
    assert got["speed_pct"] == 25 and got["note"] == "前端滑块"
    assert isinstance(got["at"], float) and got["at"] > 0


# ---------------------------------------------------------------- 接线钉（源码级）
def test_move_is_the_single_chokepoint_and_passes_state():
    """★★ 唯一咽喉钉：`_move` 取"界面滑块"（并把 `st` 传进去做基准判断）——
    一处改动覆盖分拣（单件/双面）、井字棋、AI 执行。"""
    from app.api import sorting as API

    src = inspect.getsource(API._move)
    assert "ui_speed.apply(speed, st)" in src, \
        "`_move` 没按新口径取速度（必须是 apply(speed, st)）"


def test_tictactoe_reuses_sorting_move():
    """井字棋必须复用 `api.sorting._move`（否则它自己那条路不会跟随）。"""
    from app.api import tictactoe as TTT
    from app.api import sorting as API

    assert TTT._move is API._move, "井字棋的 _move 不是分拣那一份 → 速度跟随覆盖不到"


def test_only_the_frontend_slider_endpoint_may_publish():
    """★★★ **根因钉**：**除了 `/control/ui-speed`（前端拖滑块），任何地方都不许 publish** ——
    流程"自报"正是"偶尔变 20%"的根因（互相顶），已按用户口径全部删除。
    """
    from app.api import ai as AI
    from app.api import control as C
    from app.api import sorting as API
    from app.api import tictactoe as TTT
    from app.services import vision_rules as VR

    assert "ui_speed.publish" in inspect.getsource(C.api_ui_speed), "滑块端点必须能写登记"
    for mod, name in ((API, "api/sorting.py"), (TTT, "api/tictactoe.py"),
                      (AI, "api/ai.py"), (VR, "services/vision_rules.py")):
        assert "ui_speed.publish" not in inspect.getsource(mod), \
            "%s 不该再 publish（流程自报已被用户口径删除）" % name


def test_unattended_vision_rules_neither_read_nor_write_ui_speed():
    """★ 无人值守的视觉规则**不读也不写**界面滑块 —— 它必须用规则自己配的速度。"""
    from app.services import vision_rules as VR

    src = inspect.getsource(VR)
    assert "ui_speed" not in src, "视觉规则不该碰 ui_speed（读/写都会串味）"


def test_run_file_reads_latest_speed_per_step():
    """`/control/run-file` 不走 `_move` → 必须在**每一步**按同一口径取速度。"""
    from app.api import control as C

    assert "ui_speed.apply(sp, st)" in inspect.getsource(C), \
        "文件/序列执行每一步没按新口径取速度"


def test_ui_speed_route_requires_control_token():
    """★ 安全钉：`/control/ui-speed` 挂在**带 `require_control` 的路由器**下
    （router 级依赖）—— 没有控制令牌一律 401。"""
    from app.api import control as C

    paths = [getattr(r, "path", "") for r in C.router.routes]
    assert "/api/control/ui-speed" in paths, "缺 /control/ui-speed 路由"
    deps = [getattr(d, "dependency", None) for d in (C.router.dependencies or [])]
    assert any(getattr(d, "__name__", "") == "require_control" for d in deps), \
        "该路由必须继承 require_control（令牌）"
    # 端点本体**不下发任何运动**：源码里不许出现 motion.command
    assert "motion.command" not in inspect.getsource(C.api_ui_speed), \
        "/control/ui-speed 只能写内存登记，绝不能下发运动"
