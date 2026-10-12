# -*- coding: utf-8 -*-
"""★★★ 2026-10-11 「J6 旋转管控」回归钉（**默认不旋转 · 白名单授权 · 面值逐点派生**）。

用户口径（原话，按时间序）：
  · 「觉得不能乱旋转，修改所有的旋转，**如果测试点到原位回到正上位突然旋转卡住了，不能旋转，
     回家，去摄像头点位也 J6 旋转一下这个绝对不允许**……**其他时间一律不允许旋转**，
     **正面就是正面，反面就是反面，不要写死**，不能出现乱转动修复这个致命的 bug」
  · ★★ 2026-10-12 **终版**：「**所有点的 J6 跟之前的恢复过来，正面就是之前录的 J6，
    反面就是当前点 J6 的值减掉 180 度**」「**去掉 J6正面和反面录的 0 和 180 的值，
    只显示正面和反面进行旋转的按钮，不用录制正反面**」

本文件把**终版口径**逐条钉死：
  1. **默认不旋转**：未授权时，**任何点** + 任何 face → 降级成 keep（不翻面）；
  2. **授权才翻**：`with S.allow_rotate():` 或 `rotate=True` 时 front/back 才生效；
  3. **原料位禁转不放松**：授权后去 18 个原料位仍被强制 keep（禁的是跨面翻面）；
  4. **面值逐点各算，绝无全局基准**（`bases` 参数已删，三版演进：锁实时值 → 漂移吸进目标；
     对齐示教基准 → A1上/A3上偏差；终版 = 逐点派生）：
       · 正面 = **该点自身录制 J6**；
       · 反面 = **该点自身 J6 − 180**（`back_j6` 在两个姿态等价表示里取少绕线的一侧）；
       · keep = 按**当前在哪一面**取该点自身的对应变体（环形距离，绝不翻面）；
  5. **面判定固定约定**（`face_of_j6` 单参数）：折算到 ±180 后 |值| ≤ 90° → 正面；
  6. `J6正面`/`J6反面` = **虚拟切面命令**：点名驱动、基姿态取当前实时关节、
     已在目标面 → 原地零动作；要翻 → 当前 J6 − 180；**不再读库、不再可示教**。
"""
from __future__ import annotations

from app.services import sorting as S


def _joints(j6=0.0):
    return [1.0, 2.0, 3.0, 4.0, 5.0, float(j6)]


# ---------------------------------------------------------------- ① 默认不旋转
def test_no_authorization_means_no_rotation_for_every_point():
    """★★★ 默认（未授权）：**任何点名 + 任何 face** → 都降级成 keep（不翻面）。

    这是本轮修的**致命 bug**：以前默认 `face="front"` → 到正上位/C 检测/起点都把 J6
    拉到"该点录制值" → 猛转/卡住。
    """
    names = ["A1上", "A2上", "B1上", "C检测", "开始/结束点", "J6正面", "我的自定义点", "A3"]
    for name in names:
        for want in ("front", "back", "auto", "keep"):
            eff = S.effective_face(name, want)          # 未授权（上下文默认 False）
            assert eff == S.FACE_KEEP, (
                "未授权时「%s」想要 %s 必须被降级成 keep（否则就是乱转）" % (name, want))


def test_keep_takes_point_own_variant_of_current_face():
    """★★★ 2026-10-12 **终版**：keep = 按**当前在哪一面**取**该点自身**的对应变体。

    （三版演进史，都别再走回头路：逐字锁实时值 → 漂移被吸进目标；一律对齐示教基准 →
      A1上/A3上偏差（每点自身 J6 才是对位姿态，单一基准盖不住全场）；
      终版 = **正面变体 = 自身录制值 / 反面变体 = 自身 − 180**，三版病灶全排除。）
    """
    # 自身 30°（正面）、当前 12°（正面）→ 同面 → 用**该点自己录的 30**
    # （不是带漂移的实时 12，也不是任何全局基准 —— 两版旧实现的偏差源都被排除）
    tgt, j6_only, why = S.resolve_target_joints(
        "A2上", _joints(30.0), lambda: _joints(12.0), face=S.FACE_KEEP)
    assert why == "" and j6_only is False
    assert tgt[:5] == [1.0, 2.0, 3.0, 4.0, 5.0], "J1~J5 = 该点自身（必须，否则到不了该点）"
    assert tgt[5] == 30.0, "同面 → 用该点自身录制 J6（对位准；绝不锁漂移的实时值）"
    # 自身 30°（正面）、当前 168°（反面）→ 跨面 → **反面变体 = 30 − 180**
    # （keep_j6 取环形更近的等价表示 210；归一化后仍 210 —— 与 −150 同一姿态）
    tgt2, _, _ = S.resolve_target_joints(
        "A2上", _joints(30.0), lambda: _joints(168.0), face=S.FACE_AUTO)
    assert tgt2[5] == 210.0, "跨面 → 反面变体（30−180 的等价表示），绝不翻回正面 30"
    # 环形：自身 350°（= −10° 姿态）、当前 12°（正面）→ 同面 → 自身 350 折进限位 → −10
    tgt3, _, _ = S.resolve_target_joints(
        "C检测", _joints(350.0), lambda: _joints(12.0), face=S.FACE_KEEP)
    assert tgt3[5] == -10.0, "350° 与 −10° 同一姿态（折进可表示范围）；判面按环形距离 → 正面"
    # 原料位下落同理（原语兜底 keep 也走同一条规则）
    tgt4, _, _ = S.resolve_target_joints(
        "A2", _joints(30.0), lambda: _joints(12.0), face=S.FACE_KEEP)
    assert tgt4[5] == 30.0
    # 反面姿态录的点（own=−170）、当前 12°：两面变体 {−170, 10} → 取离当前更近的 10（只动 2°，
    # 绝不做 180° 大翻）—— 「调整接近哪个点就那个」（用户原话）的环形版
    tgt5, _, _ = S.resolve_target_joints(
        "A3上", _joints(-170.0), lambda: _joints(12.0), face=S.FACE_KEEP)
    assert tgt5[5] == 10.0


def test_no_current_pose_is_refused_not_guessed():
    """拿不到当前实时 J6 → **拒绝**（判不了当前面 = 保证不了"不跨面"，绝不猜）。"""
    tgt, _j6_only, why = S.resolve_target_joints(
        "A2上", _joints(30.0), lambda: None, face=S.FACE_KEEP)
    assert tgt is None and why, "拿不到当前姿态必须拒绝（用户口径：「离线、空跑不会下达指令」）"


# ---------------------------------------------------------------- ② 授权才翻
def test_authorization_enables_front_back():
    """授权（`allow_rotate()` 上下文）→ `front` / `back` 才真的生效。"""
    assert S.rotate_allowed() is False, "默认（上下文外）必须是不授权"
    with S.allow_rotate():
        assert S.rotate_allowed() is True
        assert S.effective_face("A2上", "back") == "back"
        assert S.effective_face("A2上", "front") == "front"
    assert S.rotate_allowed() is False, "出了 with 必须恢复「不授权」（fail-safe）"


def test_explicit_rotate_flag_overrides_context():
    """HTTP 入口的显式 `rotate=` 覆盖上下文（`/sorting/goto`、`/control/move` 的 allow_rotate）。"""
    assert S.effective_face("A2上", "back", rotate=True) == "back"
    assert S.effective_face("A2上", "front", rotate=False) == S.FACE_KEEP
    with S.allow_rotate():
        # 显式 False 在授权上下文里也要**压得住**（调用方说"这次别转"就真的别转）
        assert S.effective_face("A2上", "back", rotate=False) == S.FACE_KEEP


def test_authorized_front_uses_own_j6_and_back_subtracts_180():
    """★★★ 终版核心：授权后 `front` = **该点自身录制 J6**；`back` = **自身 − 180**（就近表示）。

    （用户原话「正面就是之前录的 J6，反面就是当前点 J6 的值减掉 180 度」——
      反面基准是**该点自身值 − 180**，不是常量 −180；back_j6 在 {own−180, own+180}
      两个**姿态等价**的表示里取离当前实时 J6 更近的一侧 = 少转动/少绕线。）
    """
    # front：own=42 → 原样 42（不写死 0、不掺任何基准）
    tgt2, _, _ = S.resolve_target_joints(
        "A2上", _joints(42.0), lambda: _joints(0.0), face=S.FACE_FRONT)
    assert tgt2[5] == 42.0, "front = 该点自身录制 J6（用户口径「正面就是之前录的 J6」）"
    # back：own=0 → {−180, +180} 等价 → 就近取 −180
    tgt, _, _ = S.resolve_target_joints(
        "A2上", _joints(0.0), lambda: _joints(0.0), face=S.FACE_BACK)
    assert tgt[5] in (-180.0, 180.0), "授权 + back → J6 真的翻（自身 − 180）"
    # back：own=−170 → 反面变体 = −170 − 180 = −350 ≡ +10（归一化表示）
    tgt3, _, _ = S.resolve_target_joints(
        "A2上", _joints(-170.0), lambda: _joints(0.0), face=S.FACE_BACK)
    assert tgt3[5] == 10.0, "back 的基准是**该点自身 −180**（−350≡10），不是常量 −180"
    # back 且拿不到当前姿态 → 取绝对值更近 180 的"标准"表示（own=0 → −180）
    tgt4, _, _ = S.resolve_target_joints(
        "A2上", _joints(0.0), None, face=S.FACE_BACK)
    assert tgt4[5] in (-180.0, 180.0)


# ---------------------------------------------------------------- ③ 原料位禁转不放松
def test_raw_slots_stay_keep_even_when_authorized():
    """★★ 授权后去**原料位**（A1…A9 / B1…B9，共 18 个）**仍被强制 keep** —— 禁转黑名单不放松。

    ★ 终版口径下的 keep = **按当前面取该点自身变体**（本例：own=0、当前 123° 在反面 →
      下发反面变体 180）—— 禁的是**跨面翻面**（绝不翻到正面 0），不是"纹丝不动"。
    """
    for name in ("A1", "A9", "B1", "B9"):
        assert S.effective_face(name, "back", rotate=True) == S.FACE_KEEP, \
            "原料位「%s」永远 keep（禁止翻面）" % name
        tgt, _, _ = S.resolve_target_joints(
            name, _joints(0.0), lambda: _joints(123.0), face=S.FACE_BACK)
        assert tgt[5] == 180.0, "原料位即便传 back 也只能保持当前面（反面变体 180），绝不翻到正面 0"


# ---------------------------------------------------------------- ④ 环形距离 / ⑤ 面判定（固定约定）
def test_ring_dist_across_wrap():
    """J6 是转动轴：±360 是同一姿态 → 环形距离。"""
    assert S.ring_dist(0.0, 0.0) == 0.0
    assert S.ring_dist(10.0, -10.0) == 20.0
    assert S.ring_dist(350.0, -10.0) == 0.0, "350° 与 −10° 是同一姿态（差 360）"
    assert S.ring_dist(179.0, -179.0) == 2.0
    assert S.ring_dist(0.0, 180.0) == 180.0


def test_face_of_j6_is_fixed_convention_not_taught():
    """★★ 面判定（2026-10-12 终版 = **固定约定**，不再依赖示教基准）：
    当前 J6 折算到 ±180 后 **|值| ≤ 90° → 正面**，否则反面；平局（正好 90°）归正面。
    """
    assert S.face_of_j6(0.0) == S.FACE_FRONT
    assert S.face_of_j6(15.0) == S.FACE_FRONT
    assert S.face_of_j6(90.0) == S.FACE_FRONT, "正好 90°（平局）→ 归正面（保守：不无谓翻面）"
    assert S.face_of_j6(160.0) == S.FACE_BACK, "靠近 ±180 → 反面"
    assert S.face_of_j6(175.0) == S.FACE_BACK
    assert S.face_of_j6(-180.0) == S.FACE_BACK
    assert S.face_of_j6(-90.0) == S.FACE_FRONT
    assert S.face_of_j6(350.0) == S.FACE_FRONT, "350° 折算 = −10°（环形）→ 正面"


def test_keep_face_records_actual_variant():
    """`state.face` 的记录口径：**与 keep_j6 同源**（keep_j6 返回自身 → 正面，否则反面）。"""
    assert S.keep_face(0.0, 12.0) == S.FACE_FRONT      # 离自身 0 近 → 正面
    assert S.keep_face(0.0, 168.0) == S.FACE_BACK      # 离反面变体 180 近 → 反面
    assert S.keep_face(0.0, 5.0) == S.FACE_FRONT
    assert S.keep_face(0.0, 178.0) == S.FACE_BACK
    assert S.keep_face(0.0, None) == S.FACE_FRONT      # 拿不到当前 J6 → 保守记正面
    assert S.resolve_face(S.FACE_KEEP, 0.0, 168.0) == S.FACE_BACK
    assert S.resolve_face("back", 0.0, 168.0) == S.FACE_BACK
    assert S.resolve_face("front", 0.0, 168.0) == S.FACE_FRONT


# ---------------------------------------------------------------- ⑥ 与 keep/auto 的等价
def test_auto_and_keep_are_the_same_semantics():
    assert S.is_keep_face(S.FACE_AUTO) and S.is_keep_face(S.FACE_KEEP)
    assert S.is_keep_face("AUTO") and S.is_keep_face(" keep ")
    assert not S.is_keep_face(S.FACE_FRONT) and not S.is_keep_face(S.FACE_BACK)
    for want in (S.FACE_AUTO, S.FACE_KEEP):
        # own=5（正面）、当前 −77（正面）→ 同面 → 用自身 5
        #（★ 旧版这里断言 −77 = "锁当前值"；终版不再锁实时值 —— 漂移吸进目标正是被废弃的原因）
        tgt, _, _ = S.resolve_target_joints(
            "C检测", _joints(5.0), lambda: _joints(-77.0), face=want)
        assert tgt[5] == 5.0, "keep/auto = 当前面的**自身变体**（正面 → 自身 5），不锁实时 −77"


# ---------------------------------------------------------------- ⑦ J6 正/反面 = 虚拟切面命令（用户主动旋转）
def test_j6_face_points_are_virtual_in_place_commands():
    """★★★ 终版：`J6正面`/`J6反面` = **虚拟切面命令**（不读库、点名驱动、原地只改 J6）。

    · 已在目标面（`face_of_j6` 固定约定）→ **原地不动**（J6 = 当前值，如实零动作）；
    · 要翻 → **当前 J6 − 180**（折进软限位 ∩ 可表示范围）；
    · 库里那行的 J6（vals[5]）**被无视**（历史遗留行无消费方）。
    """
    base = [9.0, 8.0, 7.0, 6.0, 5.0, 33.0]
    # "J6正面" 且当前 33°（正面）→ 已在目标面 → 原地不动
    tgt, j6_only, why = S.resolve_target_joints(
        "J6正面", _joints(-170.0), lambda: list(base), face=S.FACE_FRONT)
    assert why == "" and j6_only is True
    assert tgt[:5] == base[:5], "J1~J5 = 当前实时姿态（原地切面）"
    assert tgt[5] == 33.0, "已在正面 → 原地零动作（如实，不来回翻）"
    # "J6反面" 且当前 33°（正面）→ 翻：33 − 180 = −153
    tgt2, j6_only2, _ = S.resolve_target_joints(
        "J6反面", _joints(0.0), lambda: list(base), face=S.FACE_FRONT)
    assert j6_only2 is True
    assert tgt2[:5] == base[:5]
    assert tgt2[5] == -147.0, "反面 = 当前 J6 − 180（33−180=−147）"
    # "J6正面" 且当前 −170°（反面）→ 翻回正面：−170 − 180 = −350 ≡ +10（归一化表示）
    tgt3, _, _ = S.resolve_target_joints(
        "J6正面", _joints(0.0), lambda: [9.0, 8.0, 7.0, 6.0, 5.0, -170.0], face=S.FACE_BACK)
    assert tgt3[5] == 10.0, "正面按钮：当前 −170（反面）→ 翻到正面（−350 ≡ 10）"
    # "J6反面" 且当前 −170°（反面）→ 已在目标面 → 原地不动
    tgt4, _, _ = S.resolve_target_joints(
        "J6反面", _joints(0.0), lambda: [9.0, 8.0, 7.0, 6.0, 5.0, -170.0], face=S.FACE_KEEP)
    assert tgt4[5] == -170.0


def test_j6_face_points_refused_without_live_base():
    """虚拟切面命令拿不到当前实时姿态 → **拒绝**（绝不整姿态跑到别处）。"""
    tgt, j6_only, why = S.resolve_target_joints(
        "J6反面", _joints(0.0), lambda: None, face=S.FACE_FRONT)
    assert tgt is None and j6_only is True and why
    assert "原地切面" in why and "拒绝" in why


def test_explicit_joints_bypass_is_not_rotation():
    """**显式 6 轴指令**（不经点位）走 `face=front` → 原样透传（= 用户在主动指定）。"""
    tgt, j6_only, why = S.resolve_target_joints(
        None, _joints(200.0), None, face=S.FACE_FRONT)
    assert why == "" and j6_only is False
    assert tgt[5] == 200.0, "显式 J6 必须原样（旋转管控管的是「到点」，不是「手动下发」）"
