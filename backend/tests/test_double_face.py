# -*- coding: utf-8 -*-
"""双面（一趟两件）判色回归 —— **按面分组投票**，两件各自独立结论。

用户口径（2026-10-10，2026-10-11 更正）：
  · J6 工装两块板**各自独立气路**（下板 1~4 位 / 反面 5~8 位），靠 **J6 翻转**用上另一面
    → **一趟两面各吸一件**；
    ★ 更正：原记"两块板共用一路真空"是**错的** —— 反面那一路此前从未被驱动（详见 modbus.VAC_ACTIONS）。
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


# ★★ 2026-10-11 口径变更（用户原话）：
#   「J6 反正不能在**原位**进行旋转！其他的时候可以旋转」
#   —— 「原位」= **原料位（A 区 9 格 + B 区 9 格，共 18 个）**。
#   ⇒ 判据由旧的"**白名单**（只允许 正上位/摄像头/起点）"改为
#      "**黑名单**：**只有原料位禁止旋转，其余一切允许**"。
#   ★ 旧白名单的两条断言因此**必须翻案**：`A1上`（正上位）与 `J6反面`（双面翻转目标）
#     旧值都是 False，正是把双面流程拦死、与设计自相矛盾的根源。
#   ★★ 本轮扩围（用户原话：「18 个正上位，摄像头点位，起点可以旋转……
#        18 个原位不能旋转」）：黑名单**由"只禁 A 区格位"扩到 A+B 共 18 个原位** ——
#      旧实现 `^A\d+` 把 B 区格位（B1…B9）当"允许"，与用户口径直接冲突，故 B 区原位一并禁止。
@pytest.mark.parametrize("name,allowed", [
    # —— 原料位（A 区 9 格 + B 区 9 格，共 18 个）→ **禁止** ——
    ("A1", False), ("A9", False), ("A3", False),
    ("B1", False), ("B5", False), ("B9", False),        # ★ 本轮新增：B 区原位同样禁止
    # 大小写 / 空格 / 带后缀写法：归一后仍是"看着像格位且不带 '上'" → 保守按原料位拒
    ("a1", False), ("A1 ", False), (" a 1 ", False), ("A1(原料)", False),
    ("b5", False), ("B5 ", False), ("B5(原料)", False),  # ★ 本轮新增：B 区同口径
    ("", False),                                        # 空名字 → 保守拒绝（沿用旧行为）
    # —— 其余一切 → **允许** ——
    ("A1上", True), ("正上位", True),                    # A 区正上位（旧白名单反而误禁，自相矛盾）
    ("A9上", True), ("B1上", True), ("B5上", True), ("B9上", True),  # ★ 18 个正上位 → 允许
    ("J6正面", True), ("J6反面", True),                 # ★ 双面翻转目标点（旧白名单误禁 → 流程跑不起来）
    ("C检测", True), ("C检测位", True), ("摄像头位", True),
    (S.START_POINT, True), ("起点", True), ("home", True),
])
def test_rotation_blacklist_pure(name, allowed):
    """★ 方案 §6-1（2026-10-11 更新）：旋转判据是**黑名单**（只禁 A/B 共 18 个原料位），不是白名单。

    用户原话：「J6 反正不能在**原位**进行旋转！其他的时候可以旋转」；
              「18 个正上位，摄像头点位，起点可以旋转……18 个原位不能旋转」。
    """
    assert S.rotation_point_allowed(name) is allowed
    # 非空名字：被禁止 ⟺ 被判为原料位（空名字由 rotation_point_allowed 单独"保守拒绝"，不算原料位）
    if name:
        assert S.is_raw_material_slot(name) is (not allowed)


def test_rotation_blacklist_covers_all_18_raw_slots():
    """★★ 本轮回归钉：**18 个原位（A1..A9 + B1..B9）全部禁止**，18 个正上位全部允许。

    逐字覆盖"18 个原位不能旋转 / 18 个正上位可以旋转" —— 防止再次退回"只禁 A 区"。
    """
    for i in range(1, 10):
        assert S.is_raw_material_slot("A%d" % i) is True, "A%d 原位必须禁止旋转" % i
        assert S.is_raw_material_slot("B%d" % i) is True, "B%d 原位必须禁止旋转" % i
        assert S.rotation_point_allowed("A%d" % i) is False
        assert S.rotation_point_allowed("B%d" % i) is False
        assert S.rotation_point_allowed("A%d上" % i) is True, "A%d上 正上位必须允许" % i
        assert S.rotation_point_allowed("B%d上" % i) is True, "B%d上 正上位必须允许" % i


def test_rotation_guard_rejects_raw_slot_before_motion():
    """★★ 回归钉：**在原料位请求带 J6 翻转的下发 → 下发之前就拒绝**（不写下去才后悔）。

    `_move_flip` 先过 `_rotation_guard`：原料位（A/B 区格位本身，共 18 个）→ `_Stop`（ctx 都没被用到）；
    非原料位点位 → 不抛错。
    """
    from app.api import sorting as API

    with pytest.raises(API._Stop):
        API._rotation_guard("A3")
    with pytest.raises(API._Stop):
        API._rotation_guard("B5")        # ★ B 区原位同样拒（2026-10-11 扩围）
    API._rotation_guard("正上位")        # 不抛
    API._rotation_guard("B5上")          # 不抛（B 区正上位允许）
    API._rotation_guard(S.CAMERA_POINT)
    with pytest.raises(API._Stop):
        # ctx=None / pmap={} 也照样拒绝（说明拒绝发生在 _move 之前）
        API._move_flip(None, {}, "A3", 5, False, "翻转")


# ------------------------------------------------ ★ 取第 2 件：发"反面那一路吸气"（2026-10-11 口径）
#   ★★ 前提更正（用户权威位表）：两面板**各自独立气路** ——
#     下板 = 40135.Bit1~4（索引 1~4）；反面/上板 = Bit5~8（索引 5~8）。
#     用户原话：「5 是吸反面的气，6 停止吸反面，7 是放气反面的，8 是停止放气了」。
#   根因（已证实）：项目此前按"两面共用一路真空（io.DOut[8]）"实现 —— 我们发「吸气」(Bit1)
#     驱动的是**下板那一路**，**反面的那一路从未被驱动** → 反面吸不到。
#     （上一轮误判为"没有 off→on 边沿"、据此加的"停止吸 → 立刻吸"**已删除**：
#      两面独立、放一件不影响另一件，不需要造边沿、也不会让第 1 件失真空。）
#   ⇒ 取第 2 件**只发** `_air(ctx,"suck",dry,"取第2件·吸反面",face=S.FACE_BACK)`（→ Bit5）。
def _second_piece_block(src):
    """切出 ③ 取第 2 件那一段源码（从 `second = trip[1]` 到 `# ④` 之前）。"""
    i = src.index("second = trip[1]")
    j = src.index("# ④", i)
    return src[i:j]


def _code_only(s):
    """去掉整行注释（注释里会写"以前是 xxx / 为什么这么写"，不该被当成还在用）。"""
    return "\n".join(l for l in s.splitlines() if not l.strip().startswith("#"))


def test_second_piece_air_sequence_source_pin():
    """★★ 顺序钉：第 2 件（上板/反面）序列必须是
       上方(移动) → 下落(移动) → **吸反面** → [等吸牢] → 抬起(移动)。

    "吸反面" = `face=S.FACE_BACK`（→ Bit5，见 test_vacuum_actions_bit_map）。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    blk = _code_only(_second_piece_block(src))
    i_up = blk.index('"取料·上方"')
    i_down = blk.index('"取料·下落"')
    i_suck = blk.index('"取第2件·吸反面"')
    i_lift = blk.index('"抬起"')
    assert i_up < i_down < i_suck < i_lift, \
        "取第 2 件顺序被改动（必须：上方→下落→吸反面→抬起）"
    assert "face=S.FACE_BACK" in blk, "取第 2 件未走反面那一路（反面吸不到的老问题会复现）"


def test_second_piece_no_release_no_resuck_source_pin():
    """★★ 反证钉：取第 2 件**不许**再出现「停止吸 / 复吸」—— 那是"共用一路真空"的旧前提。

    两面**独立气路** → 发反面吸气(Bit5) 不碰下板那一路，第 1 件不受影响 →
    无需造 off→on 边沿、也无需"停止吸→立刻吸"。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    blk = _code_only(_second_piece_block(src))
    assert '"release"' not in blk, "取第 2 件块内仍出现停止吸（共用一路真空的旧做法）"
    assert "复吸" not in blk and "断真空" not in blk, "复吸/断真空四步应已删除（两面独立）"


def test_second_piece_air_stage_self_evident_and_dry_same_order():
    """★ stage 名可自证（一眼看出是"取第 2 件时的反面吸气"）；且**空跑走同一顺序**。

    判据：这步 `_air` 把 `dry` 透传进来（dry 时由 `_air` 内部只 `_set` 记阶段、不下发），
    因此空跑与真机**共用同一段编排**（不会"真机发反面、空跑不发"地分叉）。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    blk = _code_only(_second_piece_block(src))
    assert '_air(ctx, "suck", dry, "取第2件·吸反面", face=S.FACE_BACK)' in blk, \
        "取第 2 件未发「反面吸气(Bit5)」，或 stage 名不可自证 / 未透传 dry"
    # 空跑分支：`_air` 在 dry 时只 `_set`、不下发（沿用既有语义，未改动）
    air = inspect.getsource(API._air)
    assert "if dry:" in air and "return" in air, "_air 空跑分支被改动（应只记阶段、不下发）"


def test_first_piece_and_single_trip_sequence_unchanged_source_pin():
    """★★ 零回归钉：**第 1 件（下板）动作序列逐字不变**；单件趟不走取第 2 件的块。

    · 第 1 件块：上方 → 下落 → 吸 → [等吸牢] → 抬起，且**不得出现停止吸/放气**；
    · ③ 取第 2 件块被 `if len(trip) >= 2:` 守住 → 单件趟（len==1）与第 1 件**零回归**。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    i1 = src.index("first = trip[0]")
    j1 = src.index("if len(trip) >= 2:", i1)     # ② 翻转那段（两件趟才需要）
    first_blk = _code_only(src[i1:j1])
    assert '"release"' not in first_blk and '"blow"' not in first_blk, \
        "第 1 件取料段混入了停真空/放气（会掉件）"
    assert (first_blk.index('"取料·上方"') < first_blk.index('"取料·下落"')
            < first_blk.index('"suck"') < first_blk.index('"抬起"')), \
        "第 1 件动作序列（上方→下落→吸→抬起）被改动"
    assert "suck_hold_ms" in first_blk, "第 1 件的等吸牢被改动"
    # ③ 取第 2 件块仍被 `if len(trip) >= 2:` 守住 → 单件趟不会执行取第 2 件
    i_second = src.index("second = trip[1]")
    i_guard3 = src.rindex("if len(trip) >= 2:", 0, i_second)
    assert i_guard3 < i_second, "取第 2 件没有被 `if len(trip) >= 2:` 守住（单件趟会误走）"
    assert "second = trip[1]" not in first_blk, "第 1 件块混进了取第 2 件的代码"


# ---------------------------------------------------------------- 放件三步：顺序 + 只碰该面那一路
def test_dual_drop_three_steps_order_and_face_param():
    """★★ 放件三步 `停止吸 → 放 → 停止放`，**只碰该面那一路**。

    ★★ 2026-10-11：两面独立 → **删除"复吸"**（那是"共用一路真空"的旧做法，前提已被推翻），
       故由四步变三步；`face` 决定这一趟只动哪一路（front→1~4 位 / back→5~8 位）。
    """
    from app.api import sorting as API

    d = inspect.getsource(API._dual_drop)
    i_rel = d.index('"release"')
    i_blow = d.index('"blow"')
    i_unblow = d.index('"unblow"')
    assert i_rel < i_blow < i_unblow, "放件三步顺序被改动（顺序即工艺）"
    assert '"suck"' not in d, "复吸（发「吸」）应已删除（两面独立，放一件不影响另一件）"
    assert d.count("_air(") == 3, "放件只应是三步（停止吸→放→停止放）"
    assert d.count("face=face") == 3, "三条气路动作都必须透传 face（只动该面那一路、不越界）"
    ps = inspect.signature(API._dual_drop).parameters
    assert "face" in ps and "keep" not in ps, "`keep`（复吸开关）应已移除、改由 `face` 选路"


# ---------------------------------------------------------------- ★ 8 动作位图 / 面隔离 / 零回归
#   用户权威位表（2026-10-11，原话）：
#     「0 是点位移动，1 是吸气，2 是停止吸气，3 是放气，4 是停止放气，
#       新添加了 5 是吸反面的气，6 停止吸反面，7 是放气反面的，8 是停止放气了」
def test_vacuum_actions_bit_map():
    """★★ 位图钉：8 个动作 → 40135 位号**逐一对齐用户表**（索引 1~8），且 **0（移动）未被占用**。"""
    from app.services.modbus import VAC_ACTIONS

    expect = {
        "suck": 0x0002, "release": 0x0004, "blow": 0x0008, "unblow": 0x0010,
        "suck_back": 0x0020, "release_back": 0x0040, "blow_back": 0x0080,
        "unblow_back": 0x0100,
    }
    assert {k: v[0] for k, v in VAC_ACTIONS.items()} == expect, "位号与用户 210 位表不符"
    # 位值必须 = 1 << 索引（索引 1~8）；0（点位移动）**不在本表**（走点动通道，不许改）
    for name, (bit, _label) in VAC_ACTIONS.items():
        idx = bit.bit_length() - 1
        assert 1 <= idx <= 8, "%s 的位数 %d 越界（只允许 1~8）" % (name, idx)
        assert bit == (1 << idx)
    assert 0x0001 not in {bit for bit, _ in VAC_ACTIONS.values()}, \
        "Bit0（点位移动）被气路动作占用了（移动位不许改）"
    # 移动位仍走点动通道：rc_jog_execute 置 40135 = 0x0001（与气路位互不重叠）
    import app.services.modbus as M
    jog_src = inspect.getsource(M.ModbusRobot.rc_jog_execute)
    assert "0x0001" in jog_src, "点动触发位（Bit0）被改动"


def test_air_action_face_mapping_no_cross():
    """★★ 唯一映射口钉：`_air_action(动作, 面)` → 210 动作名，**互不越界**。

    front → 下板那路（suck/release/blow/unblow）；back → 反面那路（…_back）。
    """
    from app.api.sorting import _air_action

    assert _air_action("suck", S.FACE_FRONT) == "suck"
    assert _air_action("release", S.FACE_FRONT) == "release"
    assert _air_action("blow", S.FACE_FRONT) == "blow"
    assert _air_action("unblow", S.FACE_FRONT) == "unblow"
    assert _air_action("suck", S.FACE_BACK) == "suck_back"
    assert _air_action("release", S.FACE_BACK) == "release_back"
    assert _air_action("blow", S.FACE_BACK) == "blow_back"
    assert _air_action("unblow", S.FACE_BACK) == "unblow_back"
    # 已显式给的完整动作名原样保留（避免二次加后缀）
    assert _air_action("suck_back", S.FACE_FRONT) == "suck_back"


def test_drop_face_isolation_no_cross_bits():
    """★★ 独立钉：放正面件**只**动 1~4 位、放反面件**只**动 5~8 位（两集合不相交）。

    `_dual_drop(face=…)` 把 face 透传给三条 `_air`；位号由 `modbus.VAC_ACTIONS` 唯一决定。
    """
    from app.services.modbus import VAC_ACTIONS
    from app.api import sorting as API

    front_bits = {VAC_ACTIONS[a][0] for a in ("suck", "release", "blow", "unblow")}
    back_bits = {VAC_ACTIONS[a][0] for a in
                 ("suck_back", "release_back", "blow_back", "unblow_back")}
    assert front_bits == {0x0002, 0x0004, 0x0008, 0x0010}
    assert back_bits == {0x0020, 0x0040, 0x0080, 0x0100}
    assert not (front_bits & back_bits), "正面/反面位集合发生越界（会互相干扰）"
    # 双面流程里放料一律 `_dual_drop(..., face=face)` —— 由持件槽位取面，不混用两路
    flow = inspect.getsource(API._run_sorting_dual_face)
    assert "_dual_drop(ctx, dry, face=face, st=state)" in flow, \
        "放料未按面选路（会同时动两路）"


def test_single_piece_air_stays_front_default_zero_regression():
    """★★ 零回归钉（★ 2026-10-11 本轮**翻案**）：单件趟（`_run_sorting`）的**气路** `_air` 仍**不带 face**
    → 默认正面 → 与旧行为逐字一致。

    ★ **翻案说明**：原钉断言 `_run_sorting` 里**任何** `face=` 都不许出现 —— 它编码了
      "单件路径整体不带 face 参数"的旧行为。本轮按统一口径「**去原料位一律 keep、绝不翻面**」，
      给单件路径的「取料·下落」（目标 = A{i} **原料位**）加了 `face=S.face_for_target(...)`（=keep）
      → 那条"一刀切禁 face"的断言与新口径**直接冲突**，故**收窄**为"**气路 `_air` 不带 face**"
      （气路工艺一字未改），并另由 `test_single_piece_at_a_slot_uses_keep_equivalent_to_front`
      钉住"该 keep 行为**等价正面**（零回归）"。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting)
    # 气路（吸/停吸/放气/停放）四步的**真实调用**仍不带 face → 默认正面（单件全程正面）
    for call in ('_air(ctx, "suck", dry, "取料·吸")',
                 '_air(ctx, "release", dry, "放料·停止吸")',
                 '_air(ctx, "blow", dry, "放料·放气")',
                 '_air(ctx, "unblow", dry, "放料·停止放气")'):
        assert call in src, "单件路径气路被改动（应仍为默认正面）：%s" % call
    assert inspect.signature(API._air).parameters["face"].default == S.FACE_FRONT, \
        "_air 的 face 默认值被改动（必须默认正面）"


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
    """★（2026-10-11 **更新口径**）落料顺序 = **先正面那件、再反面那件**（不再"先有效后无效"）。

    用户原话：「先送正面，然后送反面……顺序不能混乱」→ 顺序由**面**决定；
    有效/无效只决定"放到哪"（B 槽 / 原路回自己 A 原位），**不改变先后**。
    这里只测上层用的**排序函数**与 vote_color_per_face 的结论（编排本身靠源码钉）。
    """
    vs = S.vote_color_per_face([[_f("绿")] * 2, [_f("黑", 0.5)] * 2], 0.3)   # slot: lower, upper
    assert [v["slot"] for v in vs] == ["lower", "upper"]
    ordered = S.order_for_drop(vs)
    assert ordered[0]["slot"] == "lower" and ordered[0]["ok"] is True
    assert ordered[-1]["slot"] == "upper" and ordered[-1]["ok"] is False
    # 配额只对有效件扣：无效件不进 build_piece_plan/place
    pl = S.new_placement()
    plan = S.build_piece_plan(pl, ordered[0]["color"])
    assert plan["ok"] is True
    S.place(pl, ordered[0]["color"], plan["slot"])
    assert sum(len(v) for v in pl.values()) == 1, "只有 1 件进了配额表"


# ---------------------------------------------------------------- ★ 固定顺序（本轮）
def test_capture_faces_order_is_back_then_front():
    """★★（本轮）拍照面序固定：**先反面、后正面**（用户口径：顺序不能乱）。

    · 两件趟 → ["upper"(反面), "lower"(正面)]；
    · 单件趟 → ["lower"]（只有正面那件）。
    """
    assert S.faces_for_capture(2) == ["upper", "lower"], "两件趟必须「先反面后正面」"
    assert S.faces_for_capture(1) == ["lower"], "单件趟只有正面那件"
    assert S.faces_for_capture(0) == []
    # 面序唯一口径：lower < upper
    assert S.face_rank("lower") == 0 and S.face_rank("upper") == 1
    assert S.face_rank("middle") == 2 and S.face_rank(None) == 2


def test_drop_order_is_front_then_back_regardless_of_validity():
    """★★（本轮）落料顺序固定：**先正面那件、再反面那件**（顺序不能乱）。

    有效/无效**不改变先后**（只决定放到 B 槽还是原路回 A 原位）。
    """
    vs = [{"slot": "upper", "ok": True}, {"slot": "lower", "ok": True}]
    assert [v["slot"] for v in S.order_for_drop(vs)] == ["lower", "upper"]
    # 正面那件无效 → 也仍先处理它
    vs2 = [{"slot": "upper", "ok": True}, {"slot": "lower", "ok": False}]
    assert [v["slot"] for v in S.order_for_drop(vs2)] == ["lower", "upper"]
    # 不改入参顺序（返回新列表）
    assert [v["slot"] for v in vs] == ["upper", "lower"]


def test_dual_face_flow_uses_fixed_orders_source():
    """★★ 编排钉：送检拍照面序、落料次序**都必须走唯一口径函数**（不许在流程里另排一遍排乱）。"""
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    assert "faces_for_capture" in src, "送检拍照面序没走唯一口径（先反面后正面）"
    assert "order_for_drop" in src, "落料次序没走唯一口径（先正面后反面）"


# ---------------------------------------------------------------- ★ 送检到达面（本轮优化）
#   用户指令：「那就优化一下」—— 把送检**到达 C 位时的面**从"正面"改成"首个采集面"，
#   从而"到达即开拍"、省掉两件趟里那次多余翻面。拍照/放料次序口径**不变**。
def _cam_face_flips(arrival, faces, face_of_slot):
    """模拟流程里 `_set_cam_face` 的"面变了才翻"：返回**到达后**逐面拍照所需的翻面次数。

    与 `_run_sorting_dual_face._set_cam_face` 同口径（`_face_of_slot` 返回的已是归一化面，直接比较即可）。
    """
    cur, n = arrival, 0
    for f in faces:
        tgt = face_of_slot(f)
        if tgt != cur:
            n += 1
            cur = tgt
    return n


def test_dual_face_arrival_face_is_back_and_first_group_is_back():
    """★★（本轮优化）双面（两件）趟：送检**到达面 = 反面**，且到达后**第一组帧就是反面**。

    · 到达面取"首个采集面"（`faces_for_capture(2)[0]` = upper → 反面）→ 到达即在反面；
    · 故"到达面 == 首个采集面" → 首组帧**不需要额外翻面**。
    """
    from app.api import sorting as API

    faces = S.faces_for_capture(2)
    assert faces[0] == "upper", "两件趟首个采集面必须是反面(upper)"
    arrival = API._face_of_slot(faces[0])
    assert arrival == S.FACE_BACK, "两件趟送检到达面必须是反面（省掉那次多余翻面）"
    assert API._face_of_slot(faces[0]) == arrival, \
        "到达面必须 == 首个采集面（到达即开拍 → 第一组帧不用额外翻面）"


def test_dual_face_flip_count_after_arrival_reduced_by_one():
    """★★（本轮优化）到 C 后的**翻面次数**：新实现 = **1**（只用于从反面切回正面），旧实现 = 2。

    旧实现：到 C 停在正面 → 先翻反面(upper) → 再翻回正面(lower) = 2 次；
    新实现：到 C 已在反面 → 首组帧不翻 → 只在切回正面时翻 1 次 → **比旧实现少 1 次**。
    """
    from app.api import sorting as API

    faces = S.faces_for_capture(2)
    new = _cam_face_flips(API._face_of_slot(faces[0]), faces, API._face_of_slot)
    old = _cam_face_flips(S.FACE_FRONT, faces, API._face_of_slot)   # 旧实现：到 C 停在正面
    assert new == 1, "双面流程到 C 后只应翻 1 次（反面 → 正面），实际 %d" % new
    assert old == 2, "旧实现是 2 次（正面 → 反面 → 正面），实际 %d" % old
    assert new == old - 1, "新实现应比旧实现少 1 次翻面"


def test_single_trip_arrival_face_unchanged_front():
    """★（零回归）单件趟：首个采集面 = 正面(lower) → 到达面仍是**正面**，翻面次数新旧都为 0。"""
    from app.api import sorting as API

    faces = S.faces_for_capture(1)
    assert faces == ["lower"], "单件趟只有正面那件（唯一口径不变）"
    assert API._face_of_slot(faces[0]) == S.FACE_FRONT, "单件趟到达面必须仍是正面（行为不变）"
    assert _cam_face_flips(API._face_of_slot(faces[0]), faces, API._face_of_slot) == 0


def test_dual_face_arrival_face_comes_from_first_capture_source():
    """★★ 编排钉：送检到达面必须**取自首个采集面**（不许退回固定正面，也不许另造下发路径）。"""
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    assert "arrival_face = _face_of_slot(faces[0])" in src, \
        "送检到达面必须取「首个采集面」（到达即开拍、首组帧无需翻面）"
    assert '"送检", face=arrival_face' in src, "送检必须用到达面下发"
    assert '"送检", face=S.FACE_FRONT' not in src, "送检到达面退回固定正面了（会多一次翻面）"


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


# ================================================================================
# ★★ 翻转**只覆盖 J6**（用户 2026-10-11 反馈）
#   用户："J6 的坐标不对，你应该只存储 J6 的坐标，别的五个点不用啊，
#         现在你把两个正反面的两个点所有轴的坐标都存储了"
#   事实：点位库**只存 6 关节**（未指定轴时把当时实时整组存下），所以翻转时若直接
#         下发该点的完整 6 关节，就会把录点当时的 J1~J5 也带进动作里 → 现场看到"J6 不对"。
#   口径：翻转时 基姿态 = **当前实时关节**，**只把第 6 轴**换成该点的 joints[5]；J1~J5 不动。
#   全部离线：stub 掉 `_live_joints` / `motion.command` / `guard_check`，不连真机、不真下发。
# ================================================================================

class _FlipCtx:
    """极简 ctx：翻转链路只用到 `wait_pause()` / `cancelled()`。"""
    def wait_pause(self):
        return False

    def cancelled(self):
        return False

    def sleep(self, _s):  # 兼容潜在调用
        return None


class _PointRow:
    """模拟点位库行对象（`_joints_of` 只取 `.joints`）。"""
    def __init__(self, joints):
        self.joints = joints


def test_flip_only_overrides_j6_keeps_other_five(monkeypatch):
    """★★ 核心回归钉：翻转时 **J1~J5 与基姿态逐轴相同，只有 J6 变**。

    断言传给 `motion.command` 的：第 1~5 个值 == 基姿态 1~5；第 6 个 == J6反面 点的 joints[5]。
    ★ 这里把旋转**黑名单** `_rotation_guard` 打桩成放行 —— 本用例只验证**"只覆盖 J6"这条纯逻辑**；
      **原料位**拒绝由另一条独立钉子 `test_flip_j6_rejects_raw_slot_before_motion` 负责
      （否则本条会被黑名单先拦下，测不到 J6 覆盖逻辑）。
    """
    from app.api import sorting as API

    monkeypatch.setattr(API, "_rotation_guard", lambda name: None)
    base = [11.0, 22.0, 33.0, 44.0, 55.0, 66.0]        # 当前实时基姿态
    monkeypatch.setattr(API, "_live_joints", lambda: list(base))
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(API.motion, "command", _fake_command)
    monkeypatch.setattr(API, "guard_check", lambda: (True, "", None))
    # J6反面 点里存的是"录点时那一整组"（J1~J5 与 base 不同 —— 正是被误带过去的那 5 个）
    pmap = {API.DUAL_FACE_FLIP_POINT: _PointRow([1.0, 2.0, 3.0, 4.0, 5.0, -180.0])}

    API._move_flip_j6(_FlipCtx(), pmap, API.DUAL_FACE_FLIP_POINT, 5, False,
                      "翻转·J6反面（仅J6）")

    assert len(cap["joints"]) == 6, "仍必须传 6 个值（motion.command 强制 len==6）"
    assert cap["joints"][:5] == base[:5], "J1~J5 必须 = 当前实时基姿态（不许被录点时的值顶替）"
    assert cap["joints"][5] == -180.0, "第 6 轴 = J6反面 点里存的 joints[5]"


def test_flip_falls_back_and_logs_when_base_unavailable(monkeypatch):
    """★ 基姿态**取不到**（空跑/离线/读不到实时关节）：回退为该点自带 6 轴，**日志如实标注**。

    "不许悄悄用"：回退必须留一条可排查的日志（含「基姿态不可得」「改用点位自带姿态」）。
    ★ 同样把旋转**黑名单**打桩放行（见上一条说明），只验证回退 + 日志口径。
    """
    from app.api import sorting as API

    monkeypatch.setattr(API, "_rotation_guard", lambda name: None)

    def _boom():
        raise RuntimeError("拿不到当前姿态（机器人未连接/无实时数据），无法示教")

    monkeypatch.setattr(API, "_live_joints", _boom)
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(API.motion, "command", _fake_command)
    monkeypatch.setattr(API, "guard_check", lambda: (True, "", None))

    class _FakeLog:
        def __init__(self):
            self.warns = []

        def warning(self, fmt, *a):
            self.warns.append(fmt % a if a else fmt)

        def info(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

    fake = _FakeLog()
    monkeypatch.setattr(API, "log", fake)

    own = [1.0, 2.0, 3.0, 4.0, 5.0, -180.0]
    pmap = {API.DUAL_FACE_FLIP_POINT: _PointRow(own)}

    API._move_flip_j6(_FlipCtx(), pmap, API.DUAL_FACE_FLIP_POINT, 5, False,
                      "翻转·J6反面（仅J6）")

    assert cap["joints"] == own, "基姿态不可得时应回退为该点自带的完整 6 轴姿态"
    assert any("基姿态不可得" in m and "改用点位自带姿态" in m for m in fake.warns), \
        "回退必须在日志里如实标注（不许悄悄用）"


def test_flip_j6_rejects_raw_slot_before_motion():
    """★ **黑名单**钉子延续：新翻转函数 `_move_flip_j6` **同样先过旋转黑名单**。

    原料位（A/B 区格位本身，共 18 个）→ 在**下发之前**拒绝（ctx=None 也照样拒，说明拒绝发生在运动/取姿态之前）。
    ★ 2026-10-11 口径变更：`A1上`（A 区**正上位**）**不再被拒** —— 黑名单只禁原料位本身
      （用户原话：「J6 反正不能在**原位**进行旋转！其他的时候可以旋转」）。
    ★ 2026-10-11 本轮扩围：**B 区原位（B1…B9）也拒**（用户原话：「18 个原位不能旋转」）。
    """
    from app.api import sorting as API

    with pytest.raises(API._Stop):
        API._move_flip_j6(None, {}, "A3", 5, False, "翻转·J6反面（仅J6）")
    # 原料位本身（A/B + 数字、不带"上"）一律拒
    with pytest.raises(API._Stop):
        API._move_flip_j6(None, {}, "A9", 5, False, "翻转")
    with pytest.raises(API._Stop):
        API._move_flip_j6(None, {}, "B5", 5, False, "翻转")
    # ★ A1上（正上位）不再被拒（旧白名单会误拒 —— 见上方口径变更）
    API._rotation_guard("A1上")          # 不抛
    API._rotation_guard("B5上")          # 不抛（B 区正上位允许）


def test_dual_face_flip_target_passes_rotation_guard():
    """★★ 口径变更验收：双面翻转的目标点名（`DUAL_FACE_FLIP_POINT` = "J6反面"）必须**通过**旋转护栏。

    旧白名单会把它判 False → `_rotation_guard` 抛 `_Stop` → **双面流程根本跑不起来**
    （这正是用户报的"J6 不对"背后的真障碍）。
    这里用**纯函数直调**，不跑流程、不连真机。
    """
    from app.api import sorting as API

    assert S.rotation_point_allowed(API.DUAL_FACE_FLIP_POINT) is True
    API._rotation_guard(API.DUAL_FACE_FLIP_POINT)      # 不抛（= 双面翻转可下发）


def test_dual_face_flow_calls_j6_only_flip():
    """★ 编排钉：双面流程必须调用 `_move_flip_j6`（只覆盖 J6），不许退回会整点下发的 `_move_flip`。"""
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    assert "_move_flip_j6(" in src, "双面翻转没有用只覆盖 J6 的下发"
    assert "翻转·J6反面（仅J6）" in src, "翻转 stage 文案应与界面/日志对照一致"


# ================================================================================
# ★★ 2026-10-11 本轮反馈（用户原话）：
#   「现在正反面没有在原地啊，每次回去起点过程在执行啊，我在摄像头点位测试的，
#     你这个还没有解决吗？ 正面和反面的的别的五个掉移除掉啊，只J6就行了」
#   现象：在**摄像头点位**测切面，J6 正/反面却每次"跑回起点"。
#   根因：这两个点被当成**普通点位**做了**整姿态下发**（`/sorting/goto`、`/control/move`
#         按 point_id）—— 它们**在起点录的**，整姿态会把录点当时的 J1~J5 一起带过去。
#   口径（本组钉子钉死）：
#     ① 「切面」= **基姿态（当前实时关节）只覆盖第 6 轴**，J1~J5 原样不动；
#     ② 这两个点名**禁止**走"整姿态到点"路径 —— 所有入口一律转成"原地只改 J6"，
#        拿不到实时基姿态则**拒绝**（绝不用点位自带的 5 轴偷偷整姿态）；
#     ③ **只读 joints[5]**：即使点位库里那 5 个轴是脏值，切面结果也只受 joints[5] 影响；
#     ④ 存点时**只写 J6**（其余 5 轴写 0 + note 标注）。
#   全部离线/接口级（conftest 已 EFORT_SIMULATE=always + REAL_MOTION=0），不连真机、不真下发。
# ================================================================================

def _h(client):
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


@pytest.fixture
def face_points(client):
    """造/清理 `J6正面`/`J6反面` 两个点位（会话级 DB → 用完即删，避免污染其它用例）。"""
    h = _h(client)

    def _mk(name, joints):
        for p in client.get("/api/points").json():
            if p["name"] == name:
                client.delete(f"/api/points/{p['id']}", headers=h)
        r = client.post("/api/points",
                        json={"name": name, "group": "分拣", "kind": "joint", "joints": joints},
                        headers=h)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    yield _mk, h
    for p in client.get("/api/points").json():
        if p["name"] in ("J6正面", "J6反面"):
            client.delete(f"/api/points/{p['id']}", headers=h)


# ---------------------------------------------------------------- 纯函数：唯一判定口
def test_compose_j6_only_overrides_only_sixth_axis():
    """★ `compose_j6_only(base, j6)` 只覆盖第 6 轴，恒返回 6 值（motion.command 强制 len==6）。"""
    base = [11.0, 22.0, 33.0, 44.0, 55.0, 66.0]
    out = S.compose_j6_only(base, -180.0)
    assert out[:5] == base[:5], "J1~J5 必须原样不动"
    assert out[5] == -180.0
    assert len(out) == 6
    assert base[5] == 66.0, "不得原地改基姿态（纯函数，不改入参）"
    with pytest.raises(ValueError):
        S.compose_j6_only([1, 2, 3], 0.0)      # 非 6 值 → 明确报错


def test_is_j6_face_point_identifies_only_the_two():
    """★ `is_j6_face_point` 只认 `J6正面`/`J6反面`（含两侧空白），其余一律 False。"""
    assert S.is_j6_face_point("J6正面") is True
    assert S.is_j6_face_point("J6反面") is True
    assert S.is_j6_face_point(" J6反面 ") is True
    for other in ("A1", "A1上", "C检测", "开始/结束点", "起点", "", None):
        assert S.is_j6_face_point(other) is False, "%r 不该被判为 J6 正/反面" % other


# ---------------------------------------------------------------- 切面原地：/sorting/goto
def test_sorting_goto_face_point_is_in_place(client, monkeypatch, face_points):
    """★★ 核心钉子：界面上 `J6反面` 的「切面」→ 传给下发的第 1~5 值 == **当前实时基姿态**，
    第 6 值 == 该点 `joints[5]`（即使点位库里那 5 个轴是脏值，也只受 joints[5] 影响）。

    根因钉子：改前 `api_goto` 走 `motion.command(parse_joints(row.joints))` —— **整姿态**，
    于是会把录点当时的 J1~J5 一起带过去（= 跑回起点）。
    """
    from app.api import sorting as API

    _mk, h = face_points
    _mk("J6反面", [1, 2, 3, 4, 5, -180])          # 脏 J1~J5（录点时那一整组）
    base = [11.0, 22.0, 33.0, 44.0, 55.0, 66.0]   # 当前实时基姿态
    monkeypatch.setattr(API, "_live_joints", lambda: list(base))
    monkeypatch.setattr(API, "guard_check", lambda: (True, "", None))
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(API.motion, "command", _fake_command)
    r = client.post("/api/sorting/goto", json={"name": "J6反面", "speed_pct": 30}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("j6_only") is True, "该点必须走原地切面（j6_only=True）"
    assert cap["joints"][:5] == base[:5], "J1~J5 必须 = 当前实时基姿态（不许被录点时的脏值顶替）"
    assert cap["joints"][5] == -180.0, "第 6 轴 = 该点 joints[5]"


def test_sorting_goto_face_point_rejects_when_base_unavailable(client, monkeypatch, face_points):
    """★★ 拿不到实时基姿态 → **拒绝**（409），绝不退回该点自带的 5 轴整姿态下发。"""
    from app.api import sorting as API

    _mk, h = face_points
    _mk("J6反面", [1, 2, 3, 4, 5, -180])

    def _boom():
        raise RuntimeError("拿不到当前姿态（机器人未连接/无实时数据）")

    monkeypatch.setattr(API, "_live_joints", _boom)
    r = client.post("/api/sorting/goto", json={"name": "J6反面", "speed_pct": 5}, headers=h)
    assert r.status_code == 409, r.text
    assert "原地切面" in r.text


# ---------------------------------------------------------------- 切面原地：/control/move(point_id)
def test_control_move_by_point_id_face_point_is_in_place(client, monkeypatch, face_points):
    """★★ `/control/move` 按 point_id（点位执行页「执行」、序列程序步骤）同样**禁止整姿态**：
    一律转成"原地只改 J6"。这条封的是"除 /sorting/goto 之外的旁路"。
    """
    import app.api.control as C

    _mk, h = face_points
    pid = _mk("J6正面", [9, 9, 9, 9, 9, 0])       # 脏 J1~J5
    base = [11.0, 22.0, 33.0, 44.0, 55.0, 66.0]
    monkeypatch.setattr(C, "_live_joints_or_none", lambda: list(base))
    monkeypatch.setattr(C, "guard_check", lambda: (True, "", None))
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(C.motion, "command", _fake_command)
    r = client.post("/api/control/move", json={"point_id": pid, "speed_pct": 20}, headers=h)
    assert r.status_code == 200, r.text
    assert cap["joints"][:5] == base[:5], "按 point_id 也必须原地只改 J6"
    assert cap["joints"][5] == 0.0


def test_control_move_by_point_id_face_point_rejects_without_base(client, monkeypatch, face_points):
    """★ `/control/move` 拿不到实时基姿态 → 409 拒绝（不许整姿态）。"""
    import app.api.control as C

    _mk, h = face_points
    pid = _mk("J6正面", [9, 9, 9, 9, 9, 0])
    monkeypatch.setattr(C, "_live_joints_or_none", lambda: None)
    monkeypatch.setattr(C, "guard_check", lambda: (True, "", None))
    r = client.post("/api/control/move", json={"point_id": pid, "speed_pct": 20}, headers=h)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------- ★ 残影同源（本轮核心钉子）
# 用户原话：「现在残影对不上」。
# 根因：残影预演用**点位自带的 6 轴**摆残影，而实发对 J6 正/反面用**"原地切面"** → 不同源。
# 根治：把"点位 → 目标"收敛成唯一入口 `resolve_target_joints`，**残影与实发都调它**。
#   —— 下面两条断言把"残影目标 == 实发目标"钉死（J6 正/反面 + 普通点位各一例）。
def test_resolve_target_joints_pure_face_and_plain():
    """★★ 唯一解算入口的纯逻辑：
      · J6 正/反面 → 只用基姿态覆盖第 6 轴（J1~J5 = 基姿态，J6 = 点位里的值）；
      · 普通点位   → 原样返回点位自带 6 值，且**不读基姿态**（base_provider 不该被调用）；
      · J6 正/反面但拿不到基姿态 → **明确拒绝**（target=None + 原因），绝不退回整姿态。
    """
    base = [11.0, 22.0, 33.0, 44.0, 55.0, 66.0]
    dirty = [1.0, 2.0, 3.0, 4.0, 5.0, -180.0]     # 点位库里那 6 值（J1~J5 是"录点在起点"的脏值）

    tgt, j6, why = S.resolve_target_joints("J6反面", dirty, lambda: list(base))
    assert j6 is True and why == ""
    assert tgt[:5] == base[:5], "J1~J5 必须 = 基姿态（不许被录点时的脏值顶替）"
    assert tgt[5] == -180.0

    calls = []
    tgt2, j62, why2 = S.resolve_target_joints(
        "C检测", dirty, lambda: calls.append(1) or list(base))
    assert j62 is False and why2 == ""
    assert tgt2 == [float(v) for v in dirty], "普通点位必须原样返回点位自带 6 值"
    assert calls == [], "普通点位不应读实时姿态（base_provider 不该被调用）"

    tgt3, j63, why3 = S.resolve_target_joints("J6正面", [0, 0, 0, 0, 0, 0], lambda: None)
    assert tgt3 is None and j63 is True and "原地切面" in why3

    def _boom():
        raise RuntimeError("读不到实时姿态")

    tgt4, j64, why4 = S.resolve_target_joints("J6正面", [0] * 6, _boom)
    assert tgt4 is None and j64 is True and "原地切面" in why4, "provider 抛异常也应被吞掉并拒绝"


def test_ghost_target_equals_dispatch_target_face_point(client, monkeypatch, face_points):
    """★★★ 本轮核心：`/control/point-target`（**残影的数据源**）返回的目标，
    必须**逐值等于** `/control/move` 实际下发的目标 —— 对 `J6反面` 两者都是"原地切面"。
    """
    import app.api.control as C

    _mk, h = face_points
    pid = _mk("J6反面", [1, 2, 3, 4, 5, -180])       # 录点在"起点"的脏整姿态
    base = [11.0, 22.0, 33.0, 44.0, 55.0, 66.0]      # 当前实时基姿态
    monkeypatch.setattr(C, "_live_joints_or_none", lambda: list(base))

    # 残影数据源（只读接口；无控制令牌也能调）
    rp = client.post("/api/control/point-target", json={"point_id": pid})
    assert rp.status_code == 200, rp.text
    dp = rp.json()
    assert dp["ok"] is True and dp["j6_only"] is True
    assert dp.get("readonly") is True

    # 实际下发
    monkeypatch.setattr(C, "guard_check", lambda: (True, "", None))
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(C.motion, "command", _fake_command)
    rm = client.post("/api/control/move", json={"point_id": pid, "speed_pct": 20}, headers=h)
    assert rm.status_code == 200, rm.text

    assert dp["target"] == [round(v, 3) for v in cap["joints"]], \
        "★ 残影目标必须 == 实际下发目标（同源）；残影对不上就是这里分叉了"
    assert cap["joints"][:5] == base[:5] and cap["joints"][5] == -180.0


def test_ghost_target_unchanged_for_plain_point(client, monkeypatch, face_points):
    """★ 普通点位的残影目标 **逐字不变**（= 点位自带 6 值），且与实发一致 ——
    证明本轮"同源化"没有连带改动普通点位（回归保护）。
    """
    import app.api.control as C

    _mk, h = face_points        # 借它的控制令牌与"用完即删"的清理习惯
    name = "残影同源测试点"
    for p in client.get("/api/points").json():
        if p["name"] == name:
            client.delete(f"/api/points/{p['id']}", headers=h)
    r = client.post("/api/points",
                    json={"name": name, "group": "测试", "kind": "joint",
                          "joints": [10, 20, 30, 40, 50, 60]}, headers=h)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    try:
        rp = client.post("/api/control/point-target", json={"point_id": pid})
        assert rp.status_code == 200, rp.text
        dp = rp.json()
        assert dp["ok"] is True and dp["j6_only"] is False
        assert dp["target"] == [10.0, 20.0, 30.0, 40.0, 50.0, 60.0], \
            "普通点位残影目标必须逐字 == 点位自带 6 值"

        monkeypatch.setattr(C, "guard_check", lambda: (True, "", None))
        cap = {}

        def _fake_command(joints, speed, dwell=0, abort_check=None):
            cap["joints"] = [float(v) for v in joints]
            return {"ok": True}

        monkeypatch.setattr(C.motion, "command", _fake_command)
        rm = client.post("/api/control/move", json={"point_id": pid, "speed_pct": 20},
                         headers=h)
        assert rm.status_code == 200, rm.text
        assert dp["target"] == [round(v, 3) for v in cap["joints"]]
    finally:
        client.delete(f"/api/points/{pid}", headers=h)


# ---------------------------------------------------------------- 只读 J6：存点归一化
def test_sorting_teach_face_point_stores_only_j6(client, monkeypatch, face_points):
    """★★ 存点钉：`J6正面`/`J6反面` **只写 J6**（其余 5 轴写 0）+ note 明确标注"仅 J6 有效"。

    用户原话：「正面和反面的别的五个（轴）移除掉，只 J6 就行了」。
    """
    from app.api import sorting as API

    _mk, h = face_points
    monkeypatch.setattr(API, "_live_joints", lambda: [11.0, 22.0, 33.0, 44.0, 55.0, -175.5])
    r = client.post("/api/sorting/teach", json={"name": "J6正面"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["j6_only"] is True
    assert d["joints"][:5] == [0.0, 0.0, 0.0, 0.0, 0.0], "其余 5 轴必须写 0（「只留 J6」）"
    assert d["joints"][5] == -175.5
    # 库里也只写 J6 + note 标注
    row = next(p for p in client.get("/api/points").json() if p["name"] == "J6正面")
    assert row["joints"][:5] == [0.0] * 5 and row["joints"][5] == -175.5
    assert "仅 J6 有效" in row["note"], "note 必须明确标注「仅 J6 有效」（否则界面像在编故事）"


# ---------------------------------------------------------------- 无旁路：源码断言
def test_no_full_pose_entry_for_j6_face_points_source():
    """★★ 无旁路钉子：**每一个**把点位下发出去的入口，都必须走**唯一解算入口**
    `resolve_target_joints`（或直接 `compose_j6_only`）—— 堵死"把 J6 正/反面当普通整姿态点位"的旁路。

    ★ 2026-10-11 本轮（残影同源）：把"点位 → 目标"那段决策**收敛成一个函数**
      `services/sorting.py::resolve_target_joints`，四个下发入口全部改调它（不再各写一份 glue）。
      断言随之上移：入口必须引用 `resolve_target_joints`；**唯一实现**里才出现
      `is_j6_face_point` + `compose_j6_only`。
      —— 这不是放松，而是**更强**：以前是"五处各写一遍同样的 glue"（任一处漏写就分叉），
         现在是"五处共用一个函数"（不可能再分叉）。

    覆盖：
      · `api/sorting.py::api_goto`（分拣页「到点/切面」）
      · `api/control.py::api_move`（点位执行页「执行」、/control/move 按 point_id）
      · `api/control.py::_run_file_job`（序列程序里若点名 J6 正/反面）
      · `services/vision_rules.py::execute_rule`（视觉规则自动执行，按点名下发）
      · `api/sorting.py::_move_flip_j6`（分拣流程内翻转：直接 `compose_j6_only`，另有回退语义）
    """
    from app.api import sorting as API
    from app.api import control as C
    from app.services import vision_rules as VR

    # 四个"到点"入口必须调唯一解算入口
    for mod, fname in ((API, "api_goto"), (C, "api_move"),
                       (C, "_run_file_job"), (VR, "execute_rule")):
        src = inspect.getsource(getattr(mod, fname))
        assert "resolve_target_joints" in src, \
            "%s 未接唯一解算入口 resolve_target_joints（可能整姿态下发）" % fname
    # 分拣流程内翻转：直接 compose_j6_only（保留"基姿态不可得则回退整姿态"的既有语义）
    assert "compose_j6_only" in inspect.getsource(API._move_flip_j6)

    # 唯一实现：resolve_target_joints 里才出现"识别 + 只改 J6"两件
    rs = inspect.getsource(S.resolve_target_joints)
    assert "is_j6_face_point" in rs
    assert "compose_j6_only" in rs

    # 只读残影接口也必须复用同一入口（残影与实发同源）
    assert "resolve_target_joints" in inspect.getsource(C.api_point_target)


def test_flip_j6_face_note_constant_is_explicit():
    """★ 存点 note 的固定文案必须"明说"仅 J6 有效（界面/接口据此如实标注）。"""
    from app.api import sorting as API

    assert "仅 J6 有效" in API.FACE_J6_NOTE


# ================================================================================
# ★★ 2026-10-11（方案 §A）「面」取值语义回归钉 —— **不写死 0 / 不写死 −180**
#   口径：正面 = 该点自身 J6；反面 = 该点自身 J6 ∓180（归一化到 J6 软限位、取离当前更近的一侧）。
#   全部纯逻辑，不连真机、不碰相机。
# ================================================================================
def test_back_j6_is_own_minus180_normalized_and_nearest():
    """★★ 反面 = **该点自身 J6 ∓180**（不写死 0 或 −180）：归一化落软限位、取离当前更近的一侧。"""
    # 无当前 J6 → 取"绝对值更近 180"的那一侧（更标准的翻半圈表示）
    assert S.back_j6(0.0) == -180.0
    # 有当前 J6 → 优先取离它更近的一侧（少绕线/少转动）
    assert S.back_j6(0.0, current=-170.0) == -180.0
    assert S.back_j6(0.0, current=170.0) == 180.0
    # 归一化：结果必须落在 J6 软限位 [-360, 360] 内（物理姿态不变）
    lo, hi = S.j6_limits()
    for v in (-350.0, -180.0, 0.0, 179.0, 300.0, 359.0):
        assert lo <= S.back_j6(v, current=None) <= hi
    # 正面：逐字等于自身（不写死 0）；面归一化
    assert S.face_j6_of(12.0, S.FACE_FRONT) == 12.0
    assert S.face_j6_of(0.0, S.FACE_BACK) == -180.0
    assert S.norm_face("back") == "back" and S.norm_face("反面") == "back"
    assert S.norm_face("正面") == "front" and S.norm_face(None) == "front"
    assert S.face_zh("back") == "反面" and S.face_zh("front") == "正面"


def test_resolve_target_joints_face_back_uses_point_own_joints():
    """★★ 唯一解算入口 `face="back"`：J1~J5 = **该点自身**；J6 = 该点自身 J6 ∓180。

    正面（默认）→ 该点自身 6 值逐字不变（零回归）。
    """
    dirty = [1.0, 2.0, 3.0, 4.0, 5.0, 10.0]        # 点位自带 6 值（"该点自身"）
    tgt, j6_only, why = S.resolve_target_joints("C检测", dirty, None, face=S.FACE_BACK)
    assert why == "" and j6_only is False
    assert tgt[:5] == dirty[:5], "反面 J1~J5 必须用该点自身"
    assert tgt[5] == S.back_j6(10.0), "J6 = 该点自身 J6 ∓180"
    assert tgt[5] != dirty[5], "反面必须真的翻 J6（不能被自身值顶掉）"

    tgt2, j62, why2 = S.resolve_target_joints("C检测", dirty, None, face=S.FACE_FRONT)
    assert why2 == "" and j62 is False and tgt2 == dirty, "正面 = 该点自身 6 值（逐字）"


# ================================================================================
# ★★ 2026-10-11（方案 §B）分拣流程用面逻辑化回归钉
#   用面由**持件槽位**驱动（lower→正面 / upper→反面）；取第 2 件 / 送检 / 放料 **face-aware**，
#   不被点位默认正面顶掉。全部离线：纯函数 + 源码位置断言，不连真机、不真下发。
# ================================================================================
def test_slot_face_mapping():
    """★ 持件槽位 → 用面（唯一映射口）：lower=正面、upper=反面；未知 → 正面（保守）。"""
    from app.api import sorting as API

    assert API._face_of_slot("lower") == S.FACE_FRONT
    assert API._face_of_slot("upper") == S.FACE_BACK
    assert API._face_of_slot(0) == S.FACE_FRONT
    assert API._face_of_slot(1) == S.FACE_BACK
    assert API._face_of_slot("middle") == S.FACE_FRONT
    assert API._face_of_slot(None) == S.FACE_FRONT


def test_move_face_back_keeps_own_j1j5_and_flips_j6(monkeypatch):
    """★★ B 块核心机制：`_move(..., face="back")` 实发 = 该点自身 J1~J5 + 该点自身 J6∓180。

    这正是"点到点移动不再把 J6 拉回该点默认正面"的判据（A 的 face-aware 解算 + B 的调用）。
    """
    from app.api import sorting as API

    base = [11.0, 22.0, 33.0, 44.0, 55.0, 0.0]
    monkeypatch.setattr(API, "_live_joints_or_none", lambda: list(base))
    monkeypatch.setattr(API, "guard_check", lambda: (True, "", None))
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(API.motion, "command", _fake_command)
    pmap = {"A1上": _PointRow([1.0, 2.0, 3.0, 4.0, 5.0, 0.0])}
    st = S.fresh_run_state(True, 9, False)

    API._move(_FlipCtx(), pmap, "A1上", 5, False, "取料·上方", st=st, face=S.FACE_BACK)
    assert cap["joints"][:5] == [1.0, 2.0, 3.0, 4.0, 5.0], "J1~J5 = 该点自身"
    assert cap["joints"][5] in (-180.0, 180.0), "J6 = 该点自身 ∓180（真的翻了）"
    assert st.get("face") == "back", "运行时 state.face 必须如实记录当前面"

    # 正面（默认）→ 该点自身 6 值逐字不变（零回归）
    API._move(_FlipCtx(), pmap, "A1上", 5, False, "取料·上方", st=st, face=S.FACE_FRONT)
    assert cap["joints"] == [1.0, 2.0, 3.0, 4.0, 5.0, 0.0]
    assert st.get("face") == "front"


def test_dual_face_moves_are_face_aware_source():
    """★★ B 块编排钉：取第 2 件 / 送检 / 放料 各步必须 **face-aware**。

    · 取第 2 件（上板）→ `face=S.FACE_BACK`（否则被该点默认正面顶掉、翻转白做）；
    · 送检 → 传入 `set_face=_set_cam_face`（每面拍照前翻 J6，见下条）；
    · 放料 → `face=face`（用哪面取的就用哪面放）；无效件 `_put_back(..., face=face)`。
    """
    from app.api import sorting as API

    src = inspect.getsource(API._run_sorting_dual_face)
    assert "face=S.FACE_BACK" in src, "取第 2 件未带反面 → 会被点位默认正面顶掉"
    assert "set_face=_set_cam_face" in src, "送检未按面翻 J6（两组帧会取在同一姿态）"
    assert "_face_of_slot" in src, "落料未用「持件槽位 → 用面」映射"
    assert '"放料·正上方", face=face' in src, "放料未 face-aware"
    assert "face=face" in src, "无效件放回原位未带面"


def test_capture_per_face_flips_between_groups_source():
    """★★ B 块（送检真翻面）钉：`_capture_per_face` 必须支持"每面拍照前摆面"的回调。

    判据：函数体里出现 `set_face(`（在抓帧循环内、groups.append 之前），且仍复用
    `vote_color_per_face`（不新造第二套判色逻辑）。旧调用方不传 set_face → 行为逐字不变。
    """
    from app.api import sorting as API

    cs = inspect.getsource(API._capture_per_face)
    assert "set_face" in cs and "set_face(" in cs, "送检未按面摆 J6 的钩子"
    assert "vote_color_per_face" in cs, "判色必须复用按面投票（不新造第二套）"
    # 参数默认 None → 旧行为逐字不变（零回归）
    assert "set_face=None" in cs


def test_put_back_face_param_defaults_front():
    """★ `_put_back` 新增 `face`，默认正面 → 单件路径 / 井字棋逐字不变（零回归）。"""
    from app.api import sorting as API

    ps = inspect.signature(API._put_back).parameters
    assert "face" in ps and ps["face"].default == S.FACE_FRONT


# ================================================================================
# ★★ 2026-10-11（本轮）面语义 `keep`：**去原料位保持当前面、不旋转** 回归钉
#   用户原话：「1.对，但是原位禁止旋转」
#             「如果正面保持正面，如果反面保持反面，如果都不是调整接近哪个点就那个」
#   ⇒ 「去原料位」的移动一律用 `face="keep"`：J6 沿用当前面（正面/反面/判不出取更近），
#     **绝不主动翻面** —— 与"18 个原料位严禁旋转"（黑名单，一个字节未改）完全相容。
#   全部离线：纯函数 + 源码/行为断言，不连真机、不真下发。
# ================================================================================
@pytest.mark.parametrize("front,current,expect", [
    (0.0, 0.0, 0.0),          # 当前面 = 正面 → 保持正面（该点自身 J6）
    (0.0, -180.0, -180.0),    # 当前面 = 反面 → 保持反面（该点自身 J6 ∓180）
    (0.0, 30.0, 0.0),         # 都不是 → 取更近的（正面）
    (0.0, -150.0, -180.0),    # 都不是 → 取更近的（反面）
    (10.0, 10.0, 10.0),
    (10.0, -170.0, -170.0),
])
def test_keep_j6_three_cases(front, current, expect):
    """★ `keep_j6` 三种情形（参数化）：正面→正面 / 反面→反面 / 判不出→取离当前更近的一面。"""
    assert S.keep_j6(front, current) == expect
    # 拿不到当前 J6（判不出）→ 保守取正面（该点自身）
    assert S.keep_j6(front, None) == front


def test_keep_never_rotates_when_already_on_a_face():
    """★★ `keep` 全程无旋转：当前 J6 若已对准某一面 → 保持该面值**一字不动**（ΔJ6 = 0）。"""
    for f in (-350.0, -180.0, 0.0, 37.5, 170.0, 359.0):
        assert S.keep_j6(f, f) == f, "当前在正面 → 必须一字不动（Δ=0）"
        assert S.keep_face(f, f) == S.FACE_FRONT
        b = S.back_j6(f, f)
        assert S.keep_j6(f, b) == b, "当前在反面 → 必须一字不动（Δ=0）"
        assert S.keep_face(f, b) == S.FACE_BACK


def test_resolve_target_joints_keep_uses_point_own_joints():
    """★★ 唯一解算入口 `face="keep"`：J1~J5 = 该点自身；J6 = 保持当前面（不主动翻面）。"""
    dirty = [1.0, 2.0, 3.0, 4.0, 5.0, 10.0]
    # 当前 = 正面（该点自身 J6=10）→ 保持正面
    tgt, j6_only, why = S.resolve_target_joints(
        "A2", dirty, lambda: [0, 0, 0, 0, 0, 10.0], face=S.FACE_KEEP)
    assert why == "" and j6_only is False
    assert tgt[:5] == dirty[:5], "keep 的 J1~J5 必须用该点自身"
    assert tgt[5] == 10.0, "当前在正面 → 保持正面（该点自身 J6）"
    # 当前 = 反面 → 保持反面
    back = S.back_j6(10.0, 0.0)
    tgt2, _, _ = S.resolve_target_joints(
        "A2", dirty, lambda: [0, 0, 0, 0, 0, back], face=S.FACE_KEEP)
    assert tgt2[:5] == dirty[:5]
    assert tgt2[5] == back, "当前在反面 → 保持反面（该点自身 J6 ∓180）"
    # 拿不到当前姿态 → 保守取正面（不报错、不翻面）
    tgt3, _, _ = S.resolve_target_joints("A2", dirty, lambda: None, face=S.FACE_KEEP)
    assert tgt3[5] == 10.0


def test_move_keep_at_raw_slot_keeps_face_and_records_it(monkeypatch):
    """★★ 行为钉：`_move(原料位, face="keep")` **不把面翻回正面**，且 `state.face` 如实记录。

    · 当前是反面 → 去原料位 A2 时 J6 保持反面（≠ 该点默认正面 0）；
    · 当前是正面 → 保持正面；
    · 旋转黑名单**不变**：`_rotation_guard("A2")` 仍拒绝（keep 不是"翻转下发"，两者互不越界）。
    """
    from app.api import sorting as API

    monkeypatch.setattr(API, "guard_check", lambda: (True, "", None))
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(API.motion, "command", _fake_command)
    pmap = {"A2": _PointRow([1.0, 2.0, 3.0, 4.0, 5.0, 0.0])}   # 该点自身正面 J6 = 0
    st = S.fresh_run_state(True, 9, False)

    # 当前实时 = 反面（J6 = -180）→ keep 必须保持反面、不翻回正面
    monkeypatch.setattr(API, "_live_joints_or_none", lambda: [11, 22, 33, 44, 55, -180.0])
    API._move(_FlipCtx(), pmap, "A2", 5, False, "取料·下落", st=st, face=S.FACE_KEEP)
    assert cap["joints"][:5] == [1.0, 2.0, 3.0, 4.0, 5.0], "keep 的 J1~J5 = 该点自身"
    assert cap["joints"][5] == -180.0, "当前在反面 → 保持反面（**不翻回正面 0**）"
    assert st.get("face") == "back", "state.face 必须如实记录实际保持的面"

    # 当前实时 = 正面 → keep 保持正面
    monkeypatch.setattr(API, "_live_joints_or_none", lambda: [11, 22, 33, 44, 55, 0.0])
    API._move(_FlipCtx(), pmap, "A2", 5, False, "取料·下落", st=st, face=S.FACE_KEEP)
    assert cap["joints"][5] == 0.0
    assert st.get("face") == "front"

    # 旋转黑名单（18 原料位严禁旋转）**一个字节未改**：原料位"翻转下发"仍被拒（keep 走的是普通 _move）
    with pytest.raises(API._Stop):
        API._rotation_guard("A2")


def test_raw_slot_moves_use_keep_source():
    """★★ 源码钉：**去原料位**的移动一律用 `face=S.FACE_KEEP`（取料下落 ×2 + 放回原位下落）。"""
    from app.api import sorting as API

    flow = inspect.getsource(API._run_sorting_dual_face)
    assert flow.count("face=S.FACE_KEEP") == 2, \
        "双面流程两处「取料·下落」都必须用 keep（去原料位不许翻面）"
    pb = inspect.getsource(API._put_back)
    assert 'face=S.FACE_KEEP' in pb, "放回原位的「下落（去原料位）」必须用 keep"
    # keep 走的是普通 `_move`（不经过 `_rotation_guard`）→ 不产生任何"翻转下发"
    assert "_rotation_guard" not in pb, "`_put_back` 不应触发翻转护栏（keep 不是翻转）"


# ================================================================================
# ★★ 2026-10-11（本轮 · **全入口覆盖**）「去原料位一律 keep、绝不翻面」回归钉
#   用户原话（本轮报的现象）：「**我在正上位反面，去原料区是变成了正面**」——
#   上一轮只给分拣流程内 3 处「去原料位」加了 keep；**界面路径**（/sorting/goto、/control/move）、
#   **程序/序列执行步骤**、**后台视觉规则**、**井字棋**、**AI 执行**、**单件分拣路径**都**没覆盖**，
#   所以用户从界面去原料位时**仍会被翻回正面**。
#   ⇒ 本轮把**所有**"目标点可能是原料位"的入口补齐：凡目标=原料位（A1…A9/B1…B9），
#     传给解算/下发的 face **一律 `keep`**（保持当前面、绝不翻面）。
#   全部离线：纯函数 + 源码/行为断言，不连真机、不真下发。
# ================================================================================
def test_face_for_target_forces_keep_on_raw_slots():
    """★ 统一口径判据 `face_for_target`：**原料位 → 一律 keep**（无视调用方要的 front/back）；
    **非原料位 → 原样返回**（front/back/keep 不受影响，零回归）。"""
    # 原料位（A/B 区格位本身，共 18 个；含大小写/空格/带后缀的保守写法）→ keep
    for n in ("A1", "A9", "A5", "B1", "B5", "B9", "a3", " b5 ", "A1(原料)", "B5(原料)"):
        for f in (S.FACE_FRONT, S.FACE_BACK, S.FACE_KEEP, "back", None):
            assert S.face_for_target(n, f) == S.FACE_KEEP, (n, f)
    # 非原料位 → 原样返回（正面/反面/keep 都不受影响）
    assert S.face_for_target("A1上", S.FACE_FRONT) == S.FACE_FRONT
    assert S.face_for_target("A1上", S.FACE_BACK) == S.FACE_BACK
    assert S.face_for_target("B9上", "back") == "back"
    assert S.face_for_target("C检测", S.FACE_BACK) == S.FACE_BACK
    assert S.face_for_target(S.START_POINT, S.FACE_KEEP) == S.FACE_KEEP
    assert S.face_for_target("J6反面", S.FACE_FRONT) == S.FACE_FRONT
    # 缺省面 == 正面（与旧默认一致，零回归）
    assert S.face_for_target("A1上") == S.FACE_FRONT
    assert S.face_for_target("C检测") == S.FACE_FRONT


def test_face_for_target_neutralizes_front_and_back_at_raw_slot():
    """★★ **反证钉**：原料位收到 `front`/`back` 时，必须被**中性化成 keep** ——
    这正是"任何入口在目标为原料位时**不得**按该点自身重算 J6"的语义保证
    （重算 = 在原料位翻面 / 把反面件翻回正面，用户实测的正是这条）。"""
    assert S.face_for_target("A2", S.FACE_FRONT) == S.FACE_KEEP
    assert S.face_for_target("A2", S.FACE_BACK) == S.FACE_KEEP
    # 串联入口那一步：front → face_for_target → resolve_target_joints
    dirty = [1.0, 2.0, 3.0, 4.0, 5.0, 0.0]        # A2 自身正面 J6 = 0
    back = S.back_j6(0.0, 0.0)                      # -180
    cur_back = lambda: [0, 0, 0, 0, 0, back]        # 站"反面"
    f = S.face_for_target("A2", S.FACE_FRONT)
    tgt, _, _ = S.resolve_target_joints("A2", dirty, cur_back, face=f)
    assert tgt[5] == back, "站在反面去原料位 → 必须保持反面（不得重算回正面 0）"
    # ★★ 双重保险（本轮）：**即便跳过入口、直接按该点自身正面调原语**，原语级兜底也会把
    #   原料位强制成 keep → 仍然保持反面（**不再**变回正面 0）——
    #   这正是"未来新增路径漏调 face_for_target 也安全"的证据（原语层挡得住）。
    bad, _, _ = S.resolve_target_joints("A2", dirty, cur_back, face=S.FACE_FRONT)
    assert bad[5] == back, "原语兜底：原料位收 front 也必须保持当前面（双重保险）"


def test_resolve_target_joints_forces_keep_on_raw_slot():
    """★★ **原语兜底钉（本轮）**：`resolve_target_joints` 对**原料位**在**原语层**强制 keep ——
    **无视**调用方传入的 `front`/`back`，结果必须**都等于** `face="keep"` 的结果
    （即"传入 front/back 也保持当前面、不翻面"）。对 `A1` 与 `B5` 各钉一次。

    ★ 为什么要这层：入口层 `face_for_target` 是**显式声明**（谁去原料位就声明 keep），
      但它依赖"每个入口都记得调"；原语兜底是**最后一道保险** ——
      **未来新增的下发路径即便漏调入口层**，到原料位也只会被兜成 keep，**翻不了面**。
    """
    # ---- A1：站"反面"（当前 J6 = −180），A1 自身正面 J6 = 0 ----
    dirty_a = [1.0, 2.0, 3.0, 4.0, 5.0, 0.0]
    cur_back = lambda: [0, 0, 0, 0, 0, -180.0]
    keep = S.resolve_target_joints("A1", dirty_a, cur_back, face=S.FACE_KEEP)[0]
    front = S.resolve_target_joints("A1", dirty_a, cur_back, face=S.FACE_FRONT)[0]
    back = S.resolve_target_joints("A1", dirty_a, cur_back, face=S.FACE_BACK)[0]
    assert front == keep == back, "A1（原料位）：传入 front/back 必须被兜成 keep 的结果"
    assert front[:5] == dirty_a[:5], "keep 的 J1~J5 = 该点自身"
    assert front[5] == -180.0, "当前在反面 → 保持反面（**绝不翻回正面 0**）"

    # ---- B5：站"正面"（当前 J6 = 10），B5 自身正面 J6 = 10 ----
    dirty_b = [6.0, 7.0, 8.0, 9.0, 10.0, 10.0]
    cur_front = lambda: [0, 0, 0, 0, 0, 10.0]
    k5 = S.resolve_target_joints("B5", dirty_b, cur_front, face=S.FACE_KEEP)[0]
    f5 = S.resolve_target_joints("B5", dirty_b, cur_front, face=S.FACE_FRONT)[0]
    b5 = S.resolve_target_joints("B5", dirty_b, cur_front, face=S.FACE_BACK)[0]
    assert f5 == k5 == b5, "B5（原料位）：传入 front/back 必须被兜成 keep 的结果"
    assert f5[:5] == dirty_b[:5]
    assert f5[5] == 10.0, "当前在正面 → 保持正面（**不做 ±180 翻转**）"


def test_resolve_target_joints_non_raw_unaffected_by_fallback():
    """★ **零回归钉（本轮）**：原语兜底**只对原料位**生效 —— **非原料位**逐字不变：
      · `front` → 仍返回**该点自身 6 值原样**；
      · `back`  → 仍 = 该点自身 J1~J5 + 该点自身 J6 ∓180（归一）。
    用普通点 `C检测` 与**正上位** `A1上`（都不是原料位）各钉一次。
    """
    dirty = [1.0, 2.0, 3.0, 4.0, 5.0, 10.0]
    for n in ("C检测", "A1上"):
        assert S.is_raw_material_slot(n) is False, "前提：%s 必须不是原料位" % n
        t, j6, why = S.resolve_target_joints(n, dirty, None, face=S.FACE_FRONT)
        assert why == "" and j6 is False
        assert t == [float(v) for v in dirty], "非原料位 front = 该点自身 6 值（逐字不变）"
        tb, _, _ = S.resolve_target_joints(n, dirty, None, face=S.FACE_BACK)
        assert tb[:5] == dirty[:5]
        assert tb[5] == S.back_j6(10.0, None), "非原料位 back = 该点自身 J6 ∓180（逐字不变）"


def test_every_raw_slot_entry_passes_keep_source():
    """★★ **源码钉（逐入口）**：所有"会把目标点设为原料位"的入口，都必须把面归一成 keep。

    覆盖（含**用户实测命中的那条** `/sorting/goto`）：
      · `api/sorting.py::api_goto`            —— `/sorting/goto`（分拣页「到点」）= **用户实测那条**
      · `api/control.py::api_move`            —— `/control/move`（点位执行页「执行」，按 point_id）
      · `api/control.py::_run_file_job`       —— 程序/序列/文件执行的步骤
      · `api/control.py::api_point_target`    —— 残影预读（必须与实发**同源**）
      · `services/vision_rules.py::execute_rule` —— 后台视觉规则自动执行
      · `api/sorting.py::_run_sorting`        —— 单件分拣路径「取料·下落」
      · `api/tictactoe.py::_carry`            —— 井字棋取子/复位/真实复盘的「下落取料」
      · `api/tictactoe.py::_play_two`         —— 井字棋快速模式一趟两子
      · `api/ai.py::_piece`                   —— AI 执行「取料·下落」
    """
    from app.api import ai as AI
    from app.api import control as C
    from app.api import sorting as API
    from app.api import tictactoe as TTT
    from app.services import vision_rules as VR

    entries = (
        (API, "api_goto"), (C, "api_move"), (C, "_run_file_job"),
        (C, "api_point_target"), (VR, "execute_rule"),
        (API, "_run_sorting"), (TTT, "_carry"), (TTT, "_play_two"),
        (AI, "_piece"),
    )
    for mod, fname in entries:
        src = inspect.getsource(getattr(mod, fname))
        assert "face_for_target" in src, \
            "%s 未把面归一（去原料位可能被翻回正面，用户实测的现象会复现）" % fname


def test_single_piece_at_a_slot_uses_keep_equivalent_to_front(monkeypatch):
    """★ **零回归钉**：单件分拣路径「取料·下落」（去 A{i} 原料位）改用 keep；
    单件全程无翻面、此处当前=正面 → **行为与旧的"正面"逐字等价**（目标 == A{i} 自身 6 值）。"""
    from app.api import sorting as API

    monkeypatch.setattr(API, "guard_check", lambda: (True, "", None))
    cap = {}

    def _fake_command(joints, speed, dwell=0, abort_check=None):
        cap["joints"] = [float(v) for v in joints]
        return {"ok": True}

    monkeypatch.setattr(API.motion, "command", _fake_command)
    pmap = {"A1": _PointRow([1.0, 2.0, 3.0, 4.0, 5.0, 7.0])}
    # 当前实时 = 正上位 J6 = A1 自身 J6（正上位只抬 Z，J6 不变）→ keep 等价正面
    monkeypatch.setattr(API, "_live_joints_or_none",
                        lambda: [1.0, 2.0, 3.0, 4.0, 5.0, 7.0])
    st = S.fresh_run_state(True, 9, False)

    API._move(_FlipCtx(), pmap, "A1", 5, False, "取料·下落",
              st=st, face=S.face_for_target("A1", S.FACE_FRONT))
    assert cap["joints"] == [1.0, 2.0, 3.0, 4.0, 5.0, 7.0], "单件路径 keep 等价正面（零回归）"
    assert st.get("face") == "front"


def test_rotation_blacklist_and_guard_not_weakened_by_keep():
    """★ **禁转钉**：`face_for_target`（keep）**不削弱**旋转黑名单 —— 18 原料位仍严禁旋转。

    · `is_raw_material_slot` 对 18 个原位仍 True；
    · `_rotation_guard` 对原料位仍 `_Stop`（keep 走普通 `_move`，不经过该护栏）；
    · `face_for_target` 只把原料位的 front/back 换成 keep（**不产生翻转动作**）。
    """
    from app.api import sorting as API

    for i in range(1, 10):
        assert S.is_raw_material_slot("A%d" % i) is True
        assert S.is_raw_material_slot("B%d" % i) is True
    with pytest.raises(API._Stop):
        API._rotation_guard("A1")
    with pytest.raises(API._Stop):
        API._rotation_guard("B5")
    API._rotation_guard("A1上")        # 正上位允许（不抛）
    assert S.face_for_target("A1", S.FACE_BACK) == S.FACE_KEEP




