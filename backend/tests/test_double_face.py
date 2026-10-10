# -*- coding: utf-8 -*-
"""双面（一趟两件）判色回归 —— **按面分组投票**，两件各自独立结论。

用户口径（2026-10-10）：
  · J6 工装两块板共用**一路真空**，靠 **J6 翻转**用上另一面 → **一趟两面各吸一件**；
  · 送检 **正面 2 帧 + 反面 2 帧 = 4 帧**，两面**各自投票**；
  · 两件颜色**可以不同**，也**可能有无效颜色**（黑）。

本文件只测**纯逻辑**（`app/services/sorting.py` 的 `vote_color_per_face` /
`both_faces_ok`），不连真机、不碰相机。
详见 docs/方案-J6双面吸盘（一趟两件·靠J6翻转用另一面）.md
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.services import sorting as S


def _f(name, conf=0.9):
    """扁平帧形状（旧调用方/单测直接喂 {"name","conf"}）。"""
    return {"name": name, "conf": conf}


# ---------------------------------------------------------------- 分组投票
def test_two_faces_vote_independently():
    """★ 两件**不同色**：分组后各得结论，不能被混成一份。"""
    lower = [_f("红"), _f("红")]
    upper = [_f("蓝"), _f("蓝")]
    vs = S.vote_color_per_face([lower, upper], 0.3)
    assert [v["slot"] for v in vs] == ["lower", "upper"]
    assert vs[0]["ok"] is True and vs[0]["color"] == "红"
    assert vs[1]["ok"] is True and vs[1]["color"] == "蓝"


def test_mixing_four_frames_into_one_vote_would_confuse_them():
    """★ 反证：把 4 帧**一把投**会把两件混成一份（所以必须分组）——这是本函数存在的理由。"""
    mixed = S.vote_color(list([_f("红"), _f("红"), _f("蓝"), _f("蓝")]), 0.3)
    assert mixed["ok"] is False, "4 帧混投必然并列/不一致 → 判不出（正是要避免的）"


def test_one_invalid_color_does_not_affect_the_other_face():
    """★ 一件有效、一件**无效色（黑）**：互不影响 —— 上层据此"先放有效的那件"。"""
    vs = S.vote_color_per_face([[_f("绿"), _f("绿")], [_f("黑", 0.5), _f("黑", 0.5)]], 0.3)
    assert vs[0]["ok"] is True and vs[0]["color"] == "绿"
    assert vs[1]["ok"] is False and vs[1]["color"] == S.COLOR_UNKNOWN
    assert S.both_faces_ok(vs) is False


def test_both_faces_ok_requires_every_face():
    ok2 = S.vote_color_per_face([[_f("红")] * 2, [_f("蓝")] * 2], 0.3)
    assert S.both_faces_ok(ok2) is True
    assert S.both_faces_ok([]) is False


# ---------------------------------------------------------------- 判据如实（不许掩饰）
def test_two_frames_need_only_one_hit():
    """★ 如实记录口径：每组 2 帧、`accept_ratio=0.5` → **1 帧命中就算数**。

    现场要知道这一点：改成每面 2 帧后，**比原来的 3 帧更容易通过**（门槛变松）。
    如果现场要求更严，应把每面帧数调回 3（即 3+3=6 帧）。
    """
    vs = S.vote_color_per_face([[_f("红"), _f("未知", 0.0)]], 0.3)
    assert vs[0]["ok"] is True and vs[0]["color"] == "红"
    assert vs[0]["frames"] == 2


def test_empty_groups_return_empty_and_no_crash():
    assert S.vote_color_per_face([], 0.3) == []
    assert S.vote_color_per_face(None, 0.3) == []
    vs = S.vote_color_per_face([[], []], 0.3)
    assert len(vs) == 2 and all(v["ok"] is False for v in vs)


def test_valid_colors_still_apply_per_face():
    """★ 有效色集仍然逐面生效（"用户没提到的颜色是无效色"这条不能被双面逻辑绕过）。"""
    vs = S.vote_color_per_face([[_f("青")] * 2, [_f("红")] * 2], 0.3, valid_colors=("红",))
    assert vs[0]["ok"] is False                       # 青：用户没提 → 无效
    assert vs[1]["ok"] is True and vs[1]["color"] == "红"


# ================================================================================
# ★★ 阶段 2 + 阶段 3（一趟两件编排 + 安全护栏）回归钉（2026-10-10）
#   全部**离线可测**：纯函数单测 + 源码位置断言。不连真机、不碰相机、不真下发。
# ================================================================================

# ---------------------------------------------------------------- 默认零回归
def test_dual_face_switch_defaults_off():
    """★ 铁律：双面能力必须由 `StartIn.dual_face` 显式打开，**默认 False**。"""
    from app.api.sorting import StartIn

    assert StartIn().dual_face is False
    assert StartIn(dual_face=True).dual_face is True


def test_original_single_piece_path_is_untouched():
    """★★ 铁律：默认（dual_face=False）走**原单件编排**，那段一行未改。

    判据用**源码位置**：原循环体（`for idx, a_slot in enumerate(S.A_SLOTS…`）与"放料·正上方"
    等关键段落仍在，且双面分支是在它**之前**的早返回（默认不会命中）。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting)
    assert "for idx, a_slot in enumerate(S.A_SLOTS" in src, "原单件循环体被改动"
    assert "放料·正上方" in src, "原放料段落被改动"
    assert 'dual_face' in src, "双面分支没接进 _run_sorting"
    assert src.index("_run_sorting_dual_face") < src.index(
        "for idx, a_slot in enumerate(S.A_SLOTS"), "双面分支必须是**早返回**"


def test_rotation_whitelist_pure():
    """★ 方案 §6-1：旋转只允许 正上位 / 摄像头点位 / 起点；原料位 → 拒绝。"""
    assert S.rotation_point_allowed("A3") is False
    assert S.rotation_point_allowed("A1上") is False
    assert S.rotation_point_allowed("B5") is False
    assert S.rotation_point_allowed("") is False
    assert S.rotation_point_allowed("正上位") is True
    assert S.rotation_point_allowed("C检测位") is True          # ★ 方案叫法
    assert S.rotation_point_allowed(S.CAMERA_POINT) is True      # "C检测"
    assert S.rotation_point_allowed(S.START_POINT) is True       # "开始/结束点"


def test_rotation_guard_rejects_raw_slot_before_motion():
    """★★ 回归钉：**在原料位请求带 J6 翻转的下发 → 下发之前就拒绝**（不写下去才后悔）。

    `_move_flip` 先过 `_rotation_guard`：A 区格位 → `_Stop`（ctx 都没被用到）；
    白名单点位 → 不抛错。
    """
    from app.api import sorting as API

    with pytest.raises(API._Stop):
        API._rotation_guard("A3")
    API._rotation_guard("正上位")        # 不抛
    API._rotation_guard(S.CAMERA_POINT)
    with pytest.raises(API._Stop):
        # ctx=None / pmap={} 也照样拒绝（说明拒绝发生在 _move 之前）
        API._move_flip(None, {}, "A3", 5, False, "翻转")


# ---------------------------------------------------------------- 取第 2 件之前绝不断真空
def test_no_release_before_second_piece_source_pin():
    """★★ 方案 §5.2 回归钉：**取第 2 件之前不得出现停止吸/放气**。

    全项目只有一路真空 → 取第 2 件时一断真空，第 1 件立刻掉。
    用源码位置钉住：从第 1 次吸气到第 2 次吸气之间的**整段**里，
    不许出现 `停止吸` / `放气` / release / blow / unblow。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    i1 = src.index('_air(ctx, "suck"')
    i2 = src.index('_air(ctx, "suck"', i1 + 1)
    window = src[i1:i2]
    for bad in ('"release"', '"blow"', '"unblow"', "停止吸", "放气"):
        assert bad not in window, "取第 2 件之前出现了 %s（会掉第 1 件）" % bad


# ---------------------------------------------------------------- 放件四步：顺序 + 原地
def test_dual_drop_four_steps_order_and_in_place():
    """★★ 方案 §5.2：放件四步 `停止吸 → 放 → 停止放 →（还有件则）立刻吸`。

    四步必须**原地连续**：之间不得插入任何 `_move(` 或 `ctx.sleep(`（窗口越短，悬挂件越安全）。
    """
    from app.api import sorting as API

    d = inspect.getsource(API._dual_drop)
    i_rel = d.index('"release"')
    i_blow = d.index('"blow"')
    i_unblow = d.index('"unblow"')
    i_suck = d.index('"suck"')
    assert i_rel < i_blow < i_unblow < i_suck, "放件四步顺序被改动（顺序即工艺）"
    window = d[i_rel:i_unblow]
    assert "_move(" not in window, "四步之间插入了移动"
    assert "ctx.sleep(" not in window, "四步之间插入了等待（悬挂件更危险）"


def test_dual_frames_per_face_within_half_second():
    """★ 每面 2 帧 = 正面 2 + 反面 2 = 4 帧、2 个独立结论；跨度须 ≤ 0.5s（C 位窗口）。

    并且判色必须**复用现有 vote_color**（经 vote_color_per_face），不许另造一套。
    """
    from app.api import sorting as API

    assert API.DUAL_FRAMES_PER_FACE == 2
    span = (API.DUAL_FRAMES_PER_FACE - 1) * API.FRAME_GAP_S
    assert span <= 0.5, "每面 2 帧跨度 %.2fs 超过 C 位停留窗口" % span
    cs = inspect.getsource(API._capture_per_face)
    assert "FRAME_GAP_S" in cs, "必须沿用统一帧间隔"
    assert "vote_color_per_face" in cs, "必须复用按面投票（不新造第二套判色逻辑）"


# ---------------------------------------------------------------- 双槽夹持状态
def test_face_slots_put_take_and_holding():
    """★ 阶段 2：双槽（hold_lower/hold_upper）置位/覆盖/清除，`holding` 由两槽推导。"""
    st = S.fresh_run_state(True, 9, False)
    assert st["hold_lower"] is None and st["hold_upper"] is None
    assert st["holding"] is False and S.held_slots(st) == []

    info = S.face_slot_put(st, "lower", piece="A1", color="红", ok=True, conf=0.9)
    assert info == {"piece": "A1", "color": "红", "ok": True, "conf": 0.9}
    assert st["holding"] is True and S.held_slots(st) == ["lower"]

    S.face_slot_put(st, "upper", piece="A2", ok=True)
    assert S.held_slots(st) == ["lower", "upper"]

    # 重复 put 同一槽 → 覆盖（同一面不可能同时挂两件）
    S.face_slot_put(st, "lower", piece="A5", color="蓝", ok=False)
    assert st["hold_lower"]["piece"] == "A5" and st["hold_lower"]["color"] == "蓝"
    assert S.held_slots(st) == ["lower", "upper"], "覆盖不应改变'有件的面'"

    # take 空槽 → None（不报错），且 holding 仍按剩余槽推导
    got = S.face_slot_take(st, "lower")
    assert got["piece"] == "A5"
    assert st["hold_lower"] is None and S.held_slots(st) == ["upper"]
    assert st["holding"] is True
    assert S.face_slot_take(st, "lower") is None

    S.face_slot_take(st, "upper")
    assert S.held_slots(st) == [] and st["holding"] is False


def test_face_slot_bad_slot_is_noop():
    """非法槽位 → 返回 None 且**不改状态**（不静默写到错的地方）；数字 0/1 等价 lower/upper。"""
    st = S.fresh_run_state(True, 9, False)
    assert S.face_slot_put(st, "middle", piece="X") is None
    assert S.face_slot_take(st, "middle") is None
    assert st["holding"] is False and S.held_slots(st) == []
    S.face_slot_put(st, 0, piece="A7")
    S.face_slot_put(st, 1, piece="A8")
    assert S.held_slots(st) == ["lower", "upper"]


def test_fresh_state_has_dual_slots_but_holding_false():
    """每轮开头两槽必须清空（否则上一轮残料会"串"到新一轮），旧字段 holding 仍为 False。"""
    st = S.fresh_run_state(False, 9, True)
    assert st["hold_lower"] is None and st["hold_upper"] is None
    assert st["holding"] is False


# ---------------------------------------------------------------- 节拍：9 = 4×2 + 1
def test_plan_trips_nine_pieces_is_four_double_plus_one_single():
    """★ 方案 §2：9 件 = **4 趟×2 + 最后 1 趟×1**（用户："第九次只需要一次"）。"""
    trips = S.plan_trips(S.A_SLOTS, per_trip=2)
    assert trips[0] == ["A1", "A2"] and trips[-1] == ["A9"]
    assert [len(t) for t in trips] == [2, 2, 2, 2, 1]
    assert sum(1 for t in trips if len(t) == 2) == 4
    assert sum(1 for t in trips if len(t) == 1) == 1
    # 偶数件：全是双件趟，没有单件尾巴
    assert [len(t) for t in S.plan_trips(list(S.A_SLOTS[:8]))] == [2, 2, 2, 2]
    assert S.plan_trips([]) == []


def test_plan_trips_per_trip_guard_avoids_infinite_loop():
    """per_trip<=0 → 按 1 处理（否则 range step=0 会炸）。"""
    assert S.plan_trips(["A1", "A2"], per_trip=0) == [["A1"], ["A2"]]


def test_invalid_piece_placed_last_and_not_counted():
    """★ 表达式层：落料顺序 = "先放有效件，再放无效件"；无效件**不消耗配额**。

    这里只测上层用的**排序表达式**与 vote_color_per_face 的结论（编排本身靠源码钉）。
    """
    vs = S.vote_color_per_face([[_f("绿")] * 2, [_f("黑", 0.5)] * 2], 0.3)
    assert [v["slot"] for v in vs] == ["lower", "upper"]
    ordered = ([v for v in vs if v["ok"]] + [v for v in vs if not v["ok"]])
    assert ordered[0]["slot"] == "lower" and ordered[0]["ok"] is True
    assert ordered[-1]["slot"] == "upper" and ordered[-1]["ok"] is False
    # 配额只对有效件扣：无效件不进 build_piece_plan/place
    pl = S.new_placement()
    plan = S.build_piece_plan(pl, ordered[0]["color"])
    assert plan["ok"] is True
    S.place(pl, ordered[0]["color"], plan["slot"])
    assert sum(len(v) for v in pl.values()) == 1, "只有 1 件进了配额表"


# ================================================================================
# ★★ J6 正/反面**示教点位白名单**回归钉（2026-10-10 用户口径）
#   用户："在「C 区 与 开始/结束点」分组里再加两个可录入的点，记 J6 工装的正面/反面；
#         两个都是一个参数，只是 J6 的" → 需要能存点/到点/删除，但 **38 不能动**。
#   全部离线：纯常量断言 + 前端源码比对，不连真机、不碰相机。
# ================================================================================

def test_j6_face_points_are_teachable():
    """★ `J6正面`/`J6反面` 是**可选附加点**：能存点/到点/删除（与 C检测 同等待遇）。

    存点/到点/删除共用 api/sorting.py 的 `_WHITELIST`（= 38 必备点 ∪ 附加点）。
    """
    from app.api import sorting as API

    assert "J6正面" in S.OPTIONAL_EXTRA_POINTS
    assert "J6反面" in S.OPTIONAL_EXTRA_POINTS
    assert "J6正面" in API._WHITELIST, "界面能录入的点，后端必须允许存点/到点/删除"
    assert "J6反面" in API._WHITELIST
    assert "C检测" in API._WHITELIST and API._WHITELIST == S.allowed_teach_points()


def test_optional_extra_point_does_not_change_the_38():
    """★★ 铁律：加这两个附加点**不得**动 38 个必备点的清单与数量。"""
    names = [p["name"] for p in S.expected_points()]
    assert len(names) == 38
    assert S.total_expected() == 38
    assert "J6正面" not in names and "J6反面" not in names, "附加点**不许**混进必备清单"
    # 附加点既不算"缺"、也不算"多余"（比对本就只针对 38 必备点）
    chk = S.check_points(["J6正面", "J6反面"])
    assert chk["extra"] == [] and chk["total"] == 38
    # 白名单 = 38 ∪ 2 = 40
    assert len(S.allowed_teach_points()) == 40


def test_flip_point_matches_ui_name():
    """★★ 回归钉：**界面录入用的名字**与后端常量必须一致（两边改名不同步立即挂）。

    `DUAL_FACE_FLIP_POINT` 若与界面 extraSlots 的名字不一致，双面翻转会去一个
    "界面根本录不进 / 看不到"的点位取数。直接读前端源码断言。
    """
    from app.api import sorting as API

    assert API.DUAL_FACE_FLIP_POINT == "J6反面"
    assert API.DUAL_FACE_FLIP_POINT in S.OPTIONAL_EXTRA_POINTS
    vue = (Path(__file__).resolve().parents[2]
           / "frontend" / "src" / "components" / "SortingView.vue")
    src = vue.read_text(encoding="utf-8")
    assert '"J6正面"' in src, "界面 extraSlots 缺少 J6正面（与后端不同步）"
    assert '"J6反面"' in src, "界面 extraSlots 缺少 J6反面（与后端不同步）"

