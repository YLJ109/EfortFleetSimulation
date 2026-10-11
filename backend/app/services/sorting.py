# -*- coding: utf-8 -*-
"""三色方块分拣 —— 纯逻辑层（不碰硬件、不联网 → 可单测）。

职责边界：
  · 这里只做"算"：38 点位清单体检、红/绿/蓝 → B 区列行映射、本轮占位表、
    多帧判色的投票与采信判据。
  · 真正的运动/IO 下发与流程编排在 api/sorting.py（那里才有取消/进度/围栏上下文）。

现场口径（用户 2026-10-08 确认）：
  A 区原料 3×3 随机摆红/绿/蓝各 3 个 → 逐件取 → 送 C 区摄像头停顿判色
  → 按色放 B 区：**第 1 列红、第 2 列绿、第 3 列蓝**；异常（吸不到/空跑/检测不到/列满）
  **直接停止并回到开始/结束点**；不做任何自动恢复、不引入真空反馈硬件。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 常量
# ★★ 2026-10-10 七色重构（用户口径："只要用户说的颜色是有效颜色，用户没提到的颜色是无效颜色"）：
#   · RECOGNIZED_COLORS = **检测器能报出的全部颜色名**（与 camera/vision_color.PALETTE_NAMES 对齐）；
#   · COLORS            = **默认有效色集**（向后兼容：不分拣 7 色时仍是红/绿/蓝）；
#   · `vote_color(..., valid_colors=...)` 可把"有效色"换成**用户这句话提到的颜色** ——
#     调色板内但不在 valid_colors 的颜色 → 与黑一样按**无效色**处理（原路放回原位）。
RECOGNIZED_COLORS: Tuple[str, ...] = ("红", "橙", "黄", "绿", "青", "蓝", "紫")
COLORS: Tuple[str, str, str] = ("红", "绿", "蓝")
COLOR_UNKNOWN = "未知"

# B 区 3×3 的槽位命名（按行优先：B1..B9）
B_SLOTS = tuple("B%d" % i for i in range(1, 10))
A_SLOTS = tuple("A%d" % i for i in range(1, 10))

# 默认列→槽位映射：第 1 列 = B1/B4/B7，第 2 列 = B2/B5/B8，第 3 列 = B3/B6/B9
# （即"按列分组"，B1..B9 是行优先编号，故第 1 列的三个槽是 1/4/7）
DEFAULT_COLUMN_SLOTS: Dict[str, Tuple[str, str, str]] = {
    "红": ("B1", "B4", "B7"),
    "绿": ("B2", "B5", "B8"),
    "蓝": ("B3", "B6", "B9"),
}

# 每列容量（3 行）
COLUMN_CAPACITY = 3

START_POINT = "开始/结束点"     # 一个点：开头去、结尾回、异常也回这里
CAMERA_POINT = "C检测"

# ---------------------------------------------------------------- 可选附加点（J6 双面示教）
# ★★ 2026-10-10 用户口径：分拣页「点位示教」里要能**录入** J6 工装的「正面 / 反面」两个姿态，
#   用于 J6 双面分拣 —— 用同一位置覆盖左右两侧，靠在这两个姿态之间切换实现。
#   用户："两个都是一个参数，只是 J6 的" = 两姿态**只有 J6 不同**，其余 5 轴一样。
# ★ 为什么单列成"可选附加点"而**不并入** expected_points()：
#   expected_points() 是"开跑**必备**的 38 个点位"，
#   `tests/test_sorting.py::test_expected_points_is_38` 把数量与清单钉死；
#   这两个点**不是开跑必备**（体检允许缺失、不影响启动），只需**允许**被存点/到点/删除。
#   所以示教白名单 = "38 必备 ∪ 这 2 个"，而 38 的清单与数量**一个字不动**。
# ★ 名字必须与界面 `frontend/src/components/SortingView.vue` 的 extraSlots、
#   以及 `api/sorting.py` 的 DUAL_FACE_FLIP_POINT 完全一致（回归钉）。
OPTIONAL_EXTRA_POINTS: Tuple[str, ...] = ("J6正面", "J6反面")


def allowed_teach_points() -> set:
    """示教白名单集合 = 38 个必备点 ∪ 可选附加点（J6 正/反面）。

    ★ 为什么要有这个并集：存点/到点/删除都按名字校验（见 api/sorting.py 的 _WHITELIST）。
      J6 双面分拣要能录入这两个点，但又**不能**去动"38"这个数字（见 OPTIONAL_EXTRA_POINTS）。
    """
    return {p["name"] for p in expected_points()} | set(OPTIONAL_EXTRA_POINTS)


# ---------------------------------------------------------------- J6 正/反面的"只用 J6"唯一口径
# ★★ 2026-10-11 用户反馈（原话）：
#   「现在正反面没有在原地啊，每次回去起点过程在执行啊，我在摄像头点位测试的，
#     你这个还没有解决吗？ 正面和反面的的别的五个掉移除掉啊，只J6就行了」
#   现象：在**摄像头点位**测切面，J6 正/反面却每次都"跑回起点"。
#   根因：这两个点被当成**普通点位**做了**整姿态下发**（`/sorting/goto`、`/control/move`
#        按 point_id）—— 而它们是**在起点录的**，于是把录点当时的 J1~J5 也一起带过去。
#   ⇒ 口径（本模块是**唯一判定口**，禁止任何别处再散落硬编码这两个名字）：
#     · 「切面」= **基姿态（当前实时关节）只覆盖第 6 轴**，J1~J5 原样不动；
#     · 这两个点名**禁止**走"整姿态到点"路径（见 api 层护栏：api/sorting.py 的 api_goto、
#       api/control.py 的 api_move / run-file 步骤，一律转成"原地只改 J6"）。
#     · 消费方**只读 `joints[5]`**；存点时可把其余 5 轴写 0（点位库格式强制 6 值，
#       见 api/points.py::_norm_joints）+ 在 note 里标注"仅 J6 有效，其余轴不使用"。
def is_j6_face_point(name: str) -> bool:
    """该点名是否 = J6 正/反面（**只看 J6** 的两点）→ 禁止整姿态下发，只允许"原地切面"。"""
    return str(name or "").strip() in OPTIONAL_EXTRA_POINTS


def compose_j6_only(base, j6) -> List[float]:
    """**纯函数**：把基姿态 `base` 的第 6 轴覆盖为 `j6`，J1~J5 原样不动，返回新的 6 值列表。

    ★ 这是"切面 = 原地只改 J6"的**单一实现**：`_move_flip_j6` 与所有"到点"护栏都复用它，
      避免"只覆盖第 6 轴"这段逻辑分叉成多份、慢慢走样。
    ★ `motion.command` 强制 len==6，故这里恒返回 6 值（保持"点位库/下发都是 6 值"的格式铁律）。
    """
    t = [float(v) for v in base]
    if len(t) != 6:
        raise ValueError("基姿态必须为 6 个数值")
    t[5] = float(j6)
    return t


# ---------------------------------------------------------------- J6「面」取值语义（正面/反面）
# ★★ 2026-10-11 用户口径（本轮核心；见 docs/方案-J6面语义与分拣／AI／游戏双面模式…md §A）：
#   · 「**正面**」= **该点位自身录的 J6**（**不写死 0**，现状即如此，保持）；
#   · 「**反面**」= **该点位自身 J6 ∓ 180**（**不写死 −180**）：
#       两个方向对夹爪的"面"完全等价（相差 360°），归一化到 J6 软限位后，
#       取**绝对值更近 180 的那一侧**；**有当前实时 J6 时优先取离它更近的一侧**（少绕线、少转动）。
#   · `J6正面`/`J6反面` 两个点**降级**为「**原地切面**的能力标记 / 旧数据兼容」：
#       它们自身的 J6 值**不再作为任何点位的"面值"基准**（只是"在当前位置直接切到 J6=该值"的快捷动作）。
#   · **规则化、逐点各算**：绝不把某个点的值当全局面值套到别的点。
#   ★ 修根因（用户报"从反面切回正面"）：过去"点到点移动"整姿态下发该点自带值 →
#     每次都把 J6 拉回该点默认正面，把上一段的翻转顶掉。现在 `resolve_target_joints` 带上 `face`，
#     所有下发路径共用它 → 移动时能"保持面"。
FACE_FRONT = "front"
FACE_BACK = "back"
_FACE_ZH = {FACE_FRONT: "正面", FACE_BACK: "反面"}
# J6 软限位兜底（与 config/robot.yaml 的 joint_limits.J6 一致；读不到配置才用它）
_J6_LIMIT_FALLBACK: Tuple[float, float] = (-360.0, 360.0)


def j6_limits() -> Tuple[float, float]:
    """取 J6 软限位 (min, max)；**优先**用 config/robot.yaml 的值，读不到才兜底 (−360, 360)。

    ★ 为什么读配置而不是写死：软限位是"现场可调"的机器参数（`config/robot.yaml` 的
      `joint_limits.J6`）；写死会在现场改限位后与真机不一致。
    """
    try:
        from app.core.config import get_config
        for lim in (get_config().get("joint_limits", default=[]) or []):
            if str((lim or {}).get("name") or "").strip().upper() == "J6":
                return float(lim.get("min")), float(lim.get("max"))
    except Exception:  # noqa: BLE001 —— 配置读不到 → 兜底，不因它抛错
        pass
    return _J6_LIMIT_FALLBACK


def normalize_j6(v) -> float:
    """把任意 J6 角**按 360° 等价**折进软限位 `[min, max]` 内（**物理姿态不变**）。

    ★ 为什么可等价：J6 是**转动轴**，±360° 是同一个姿态；折进软限位只是为了给控制器
      一个合法范围内的目标值（避免下发超限）。
    """
    lo, hi = j6_limits()
    x = float(v)
    # hi−lo 是 360 的整数倍（现场为 720），故下面两个循环必然收敛。
    while x > hi:
        x -= 360.0
    while x < lo:
        x += 360.0
    return x


def back_j6(front_j6, current=None) -> float:
    """**该点位自身 J6** → 「**反面**」J6（**规则化、不写死**）。

    规则：
      1) 反面 = 自身 J6 ∓ 180（两个方向对夹爪"面"等价，相差 360°）；
      2) **归一化**到 J6 软限位内（`normalize_j6`）；
      3) 两侧都落限位内时：**有当前实时 J6 → 取离它更近的一侧**（少转动/少绕线）；
         **拿不到当前姿态 → 取绝对值更近 180 的那一侧**（更"标准"的翻半圈表示）。
    ★ 基准恒为**传入的该点自身 J6**（front_j6）—— 绝不用某个全局值去顶替别的点。
    """
    v = float(front_j6)
    cands = [normalize_j6(v - 180.0), normalize_j6(v + 180.0)]
    uniq = list(dict.fromkeys(round(c, 6) for c in cands))   # 去重保序（通常两个不同）
    if current is not None:
        cur = float(current)
        return min(uniq, key=lambda c: (abs(c - cur), abs(abs(c) - 180.0)))
    return min(uniq, key=lambda c: (abs(abs(c) - 180.0), abs(c)))


def norm_face(face) -> str:
    """把任意"面"写法归一成 `"front"` / `"back"`（非法/缺省 → `"front"`，**默认正面**）。"""
    f = str(face or FACE_FRONT).strip().lower()
    return FACE_BACK if f in (FACE_BACK, "back", "反", "反面", "b") else FACE_FRONT


def face_zh(face) -> str:
    """面 → 中文（界面/日志如实标注用）。"""
    return _FACE_ZH.get(norm_face(face), "正面")


def face_j6_of(front_j6, face=FACE_FRONT, current=None) -> float:
    """按「面」取 J6：**正面 = 该点自身值**；**反面 = 该点自身值 ∓180 归一**。"""
    return back_j6(front_j6, current) if norm_face(face) == FACE_BACK else float(front_j6)


# ---------------------------------------------------------------- 面语义 keep（保持当前面、不旋转）
# ★★ 2026-10-11 用户口径（本轮 · 原话，逐条落地）：
#   「1.对，但是原位禁止旋转」
#   「如果正面保持正面，如果反面保持反面，如果都不是调整接近哪个点就那个」
#   ⇒ 新增面语义 `face="keep"`：**去原料位时"保持当前面、不旋转"** ——
#     与"18 个原料位（A1…A9 + B1…B9）严禁旋转"**完全相容**（**不是放开禁转**，禁转口径一个字未改）。
#   · 当前面 = **正面** → 保持正面（J6 = **该点自身 J6**，不写死 0）；
#   · 当前面 = **反面** → 保持反面（J6 = **该点自身 J6 ∓180 归一**，不写死 −180）；
#   · **都不是**（当前面未知/判不出）→ 取**离当前实时 J6 更近**的那一面（"调整接近哪个点就那个"）。
#   ★ 为什么需要它：18 个原料位严禁旋转，所以"把反面的件送回原料位"不能靠"到点整姿态"
#     （那会把面翻回该点的默认正面）。`keep` 让 J6 沿用当前面 → 一路不产生**主动翻面**。
FACE_KEEP = "keep"


def is_keep_face(face) -> bool:
    """该「面」写法是否 = `"keep"`（保持当前面、不旋转）。"""
    return str(face or "").strip().lower() == FACE_KEEP


def keep_j6(front_j6, current=None) -> float:
    """「保持当前面」的 J6（**纯函数**）：在{正面值, 反面值}里取当前所在的那一面 —— **不主动翻面**。

    · 当前 J6 == 该点自身 J6（正面）→ 正面值（该点自身 J6）；
    · 当前 J6 == 该点自身 J6 ∓180（反面）→ 反面值；
    · 都不是（判不出面）→ 取**离当前实时 J6 更近**的那一侧（用户原话"调整接近哪个点就那个"）。
    ★ 拿不到当前 J6（`current=None`）→ 保守取**正面**（该点自身 J6）：没有"当前面"可保持时，正面最安全。
    ★ 用户原话：「如果正面保持正面，如果反面保持反面，如果都不是调整接近哪个点就那个」。
    """
    v = float(front_j6)
    if current is None:
        return v
    cur = float(current)
    back = back_j6(v, cur)
    return v if abs(cur - v) <= abs(cur - back) else back


def keep_face(front_j6, current=None) -> str:
    """`keep` 时**实际保持的面**（front/back）—— 供 state/日志**如实记录**（绝不编故事）。"""
    return FACE_FRONT if keep_j6(front_j6, current) == float(front_j6) else FACE_BACK


def resolve_face(face, front_j6, current=None) -> str:
    """任意「面」写法 → 本次下发**实际生效**的面（`"front"`/`"back"`），供 state 如实记录。

    · `"keep"` → 按当前 J6 判出实际保持的面（`keep_face`）；
    · 其余 → `norm_face`（front/back；缺省 front）。
    """
    if is_keep_face(face):
        return keep_face(front_j6, current)
    return norm_face(face)


def face_for_target(name, face=FACE_FRONT) -> str:
    """★★ **统一口径（2026-10-11 用户原话）**：目的地是**原料位**的移动**一律 `keep`、绝不翻面**。

    用户报的现象（本轮修复的核心）：「**我在正上位反面，去原料区是变成了正面**」。
    根因：过去凡"点到点移动"要么按**该点自身正面**下发（把反面件翻回正面），
      要么按该件**反面**下发（在原料位做 ±180 翻转，**违反"18 个原料位严禁旋转"**）——
      两条路都不对，所以第三种面语义 `keep`（保持当前面、不旋转）才是唯一正确的。

    ⇒ 判据（**唯一**）：凡目标点 = **原料位**（`is_raw_material_slot`：A1…A9 / B1…B9，共 18 个）→
      返回 `FACE_KEEP`，**无视调用方原本要的正面/反面**（原料位只能"保持当前面"）。
      **要某个面，必须在"允许旋转的位置"（正上位 `X上` / `C检测` / 开始结束点 / `J6正反面`）
      先翻好，再保持着去原料位**；**绝不允许**靠"到达原料位时按该点自身 J6 重算"来实现
      —— 那正是在原料位翻面 / 翻回正面的根源。
    · 非原料位（含 A{i}上 / B{i}上 / C检测 / 开始结束点 / J6正反面）→ 原样返回调用方给的面。

    ★ 与"18 原料位严禁旋转"（黑名单，**一个字节未改**）**完全相容**：`keep` 走的是**普通 `_move`**
      （不经过 `_rotation_guard`），天然不产生任何"翻转下发"动作。
    ★ 本函数是"去原料位 → keep"的**唯一判据**：界面到点（`/sorting/goto`）、`/control/move`、
      程序/序列执行步骤、后台视觉规则、井字棋、AI 执行、单件分拣路径 —— 所有"目标点可能是原料位"
      的入口都调它，避免各写一份、慢慢走样。

    ★★ **双重保险（2026-10-11 本轮）**：本函数 = **入口层的显式声明**（谁想去原料位就在入口声明 keep）；
      与之配对的**第二层**是**原语层兜底** —— `resolve_target_joints` 内部对原料位**再强制一次 keep**
      （见那里的注释）。两层**都对非原料位原样透传**，所以是纯加固、零回归：
      **即便未来新增的下发路径漏调本函数**，也只会在原语层被兜底成 keep，**到不了原料位翻面**。
    """
    if is_raw_material_slot(name):
        return FACE_KEEP
    return face


def _current_j6_from(base_provider):
    """从 `base_provider` 取"当前实时 J6"（6 值里的第 6 个）；拿不到/异常/形状不对 → None（**不抛**）。

    ★ 与 `back_j6` 里那段内联读取同源：只是"保持/选更近面"的参考值，读不到就走确定性规则、不影响下发。
    """
    if not callable(base_provider):
        return None
    try:
        b = base_provider()
    except Exception:      # noqa: BLE001 —— provider 失败 = 拿不到当前姿态
        return None
    if b is None:
        return None
    try:
        bl = list(b)
        if len(bl) != 6:
            return None
        return float(bl[5])
    except (TypeError, ValueError):
        return None


def resolve_target_joints(name, joints, base_provider=None, face=FACE_FRONT):
    """**点位 → 将要下发的 6 值目标**（*唯一入口*；残影预演与实际下发**同源**）。

    返回 `(target, j6_only, reason)`：
      · 普通点位 + face="front"（默认） → `(joints 原样 6 值, False, "")`
      · 普通点位 + face="back"           → `([该点自身 J1~J5] + [J6=该点自身 J6 ∓180 归一], False, "")`
      · 普通点位 + face="keep"           → `([该点自身 J1~J5] + [J6=保持当前面(不主动翻面)], False, "")`
      · **原料位**（A1…A9/B1…B9）+ **任意 face** → **原语兜底强制 keep**（见下 ★★，同上一行）
      · `J6正面`/`J6反面` + 有基姿态     → `(compose_j6_only(base, joints[5]), True, "")`
      · `J6正面`/`J6反面` + 无基姿态     → `(None, True, "…已拒绝…")`

    ★★ 2026-10-11 用户反馈（原话）「现在残影对不上」：
      · **根因**：残影预演（ghost）过去直接用**点位自带的 6 轴**摆残影，而实际下发对
        `J6正面`/`J6反面` 用的是**"原地切面"**（当前实时基姿态只覆盖第 6 轴）——
        两者**不同源** → 残影停在"录点时的整姿态"（例如起点），实发却只转 J6 → 对不上。
      · **根治**：把"点位 → 目标"这段决策收敛成**这一个函数**，**残影与实发都调它**
        （残影见 `api/control.py` 的只读接口 `/control/point-target`）。
    ★★ 2026-10-11「面」语义（本轮新增 `face`）：
      · 普通点位的两面都**只动 J6**（J1~J5 用**该点自身**的值）—— 与用户口径"像点位执行那样，只旋转 J6"一致；
      · `J6正面`/`J6反面` **无论 face 取什么**都走既有"原地切面"（它们已是"切面能力标记"，J6 值不作面值基准）。
    ★ `base_provider`：零参可调用，返回"基姿态"（6 值）或 None。
      · `J6正面`/`J6反面`：**必须**拿得到（拿不到 → 拒绝）；
      · 普通点位 face="front"：**不调用**（避免无谓的采集依赖，普通路径零回归）；
      · 普通点位 face="back"/"keep"：**可选**调用（只取"当前 J6"选更近的一侧 / 判当前面；拿不到 → 走确定性规则）。
    ★★ 2026-10-11「keep」语义（本轮 · 用户原话「如果正面保持正面，如果反面保持反面，
      如果都不是调整接近哪个点就那个」）：**去原料位保持当前面、不旋转** ——
      J1~J5 用该点自身，J6 = `keep_j6(该点自身 J6, 当前实时 J6)`（**绝不主动翻面**），
      与"18 个原料位严禁旋转"完全相容（禁转黑名单一个字节未改）。
    ★ 这是纯决策逻辑（"用不用基姿态、怎么覆盖"），副作用只在 `base_provider` 上；
      便于离线单测钉死"残影目标 == 实发目标"。
    """
    vals = [float(v) for v in (joints or [])]
    if len(vals) != 6:
        return None, False, "目标关节角必须为 6 个数值"
    # ★★ 原语级兜底（2026-10-11 本轮「双重保险」的**第二层**）：目标点是**原料位**
    #   （A1…A9 / B1…B9，判据 `is_raw_material_slot`）→ **强制 `face = FACE_KEEP`**，
    #   **无视**调用方传入的 `front` / `back`，再按 keep 解算（J1~J5 用该点自身、J6 保持当前面）。
    #   · 为什么（用户原话）：「**我在正上位反面，去原料区是变成了正面**」—— 原料位本身
    #     **严禁旋转**（见 `is_raw_material_slot` 黑名单，一个字节未改），故到原料位只能"保持当前面"，
    #     绝不按该点自身 J6 重算（那正是在原料位翻面 / 把反面件翻回正面）。
    #   · 与入口层 `face_for_target` 的关系 = **入口显式声明 + 原语兜底**：
    #     入口那层是"谁去原料位就声明 keep"（显式、可读）；这里是**最后一道兜底** ——
    #     **即便未来新增的下发路径漏调入口层**，也只会在原语层被兜成 keep，**到不了原料位翻面**。
    #   · 两层对**非原料位都原样透传**（`front` 仍是原样 6 值、`back` 仍是 ∓180、
    #     `J6正面/反面` 仍是原地切面）→ **零回归**。
    if is_raw_material_slot(name):
        face = FACE_KEEP
    if is_j6_face_point(name):
        # 既有"原地切面"语义**保持不变**（与 face 参数无关）——
        # 这两点已降级为"切面能力标记 / 旧数据兼容"，其 J6 值不再参与任何点位的"面值"计算。
        base = None
        if callable(base_provider):
            try:
                base = base_provider()
            except Exception:      # noqa: BLE001 —— provider 失败 = 拿不到基姿态 → 拒绝
                base = None
        if base is None or len(list(base)) != 6:
            return None, True, (
                "「%s」是 J6 正/反面，只能「原地切面」（基姿态取当前实时关节）；"
                "现在拿不到当前实时姿态，**已拒绝下发**，以避免整姿态跑到别处" % name)
        return compose_j6_only(base, vals[5]), True, ""
    if is_keep_face(face):
        # ★★ keep（2026-10-11 用户口径）：**去原料位保持当前面、不旋转**。
        #   J1~J5 用**该点自身**；J6 = `keep_j6(该点自身 J6, 当前实时 J6)` ——
        #   正面→保持正面 / 反面→保持反面 / 判不出→取更近的一面；**绝不主动翻面**
        #   （与"原料位严禁旋转"相容：这里不产生 ±180 的面翻转动作）。
        cur = _current_j6_from(base_provider)
        tgt = [vals[0], vals[1], vals[2], vals[3], vals[4], keep_j6(vals[5], cur)]
        return tgt, False, ""
    if norm_face(face) != FACE_BACK:
        return vals, False, ""      # 正面 = 该点自身 6 值（**与旧行为逐字一致**）
    # 反面：J1~J5 用**该点自身**；J6 = 该点自身 J6 ∓180（可选参考当前 J6 取更近的一侧）
    cur = _current_j6_from(base_provider)
    tgt = [vals[0], vals[1], vals[2], vals[3], vals[4], back_j6(vals[5], cur)]
    return tgt, False, ""


def expected_points() -> List[Dict[str, str]]:
    """期望的 38 个点位（名称 + 角色）。用于页面体检与后端前置校验。

    38 = A 区 9 槽 × 2（槽位 + 正上方）18
       + B 区 9 槽 × 2（槽位 + 正上方）18
       + C 检测 1
       + 开始/结束点 1
    """
    out: List[Dict[str, str]] = []
    for s in A_SLOTS:
        out.append({"name": s, "role": "A槽位", "zone": "A"})
        out.append({"name": s + "上", "role": "A正上方", "zone": "A"})
    for s in B_SLOTS:
        out.append({"name": s, "role": "B槽位", "zone": "B"})
        out.append({"name": s + "上", "role": "B正上方", "zone": "B"})
    out.append({"name": CAMERA_POINT, "role": "C检测位", "zone": "C"})
    out.append({"name": START_POINT, "role": "开始/结束点", "zone": "S"})
    return out


def total_expected() -> int:
    return len(expected_points())


def check_points(have: Iterable[str]) -> Dict:
    """点位体检：把"数据库里已有的点位名"与期望清单比对。

    返回 {"ok", "missing": [...], "extra": [...], "found": n, "total": 38}
    ★ 缺任何一个都不允许开跑 —— 目的位缺失会变成"下发到某个旧目标"这种危险动作，
      宁可拒绝启动并列出缺哪些，也不猜。
    """
    have_set = {str(n).strip() for n in (have or []) if str(n).strip()}
    exp = expected_points()
    missing = [e["name"] for e in exp if e["name"] not in have_set]
    exp_names = {e["name"] for e in exp}
    a_like = set(A_SLOTS) | set(B_SLOTS)
    extra = sorted(n for n in have_set - exp_names if n.startswith(("A", "B")))
    return {
        "ok": not missing,
        "missing": missing,
        "extra": extra,
        "found": len(exp) - len(missing),
        "total": len(exp),
        "a_slots": list(A_SLOTS),
        "b_slots": list(B_SLOTS),
        "camera_point": CAMERA_POINT,
        "start_point": START_POINT,
        "column_slots": {k: list(v) for k, v in DEFAULT_COLUMN_SLOTS.items()},
    }


# ---------------------------------------------------------------- 占位表
def new_placement(column_slots: Optional[Dict[str, Sequence[str]]] = None) -> Dict:
    """本轮占位表：颜色 → 已占用槽位列表（按放入顺序）。

    ★ 为什么用内存占位表而不是视觉判空满：视觉看的是"这个槽里有没有东西"，
      受光照/遮挡/相机视角影响，判空满不可靠；而本轮放了几件是我方**确知**的事实。
      "对满了停"直接由它判定。
    """
    cmap = column_slots or DEFAULT_COLUMN_SLOTS
    return {c: [] for c in cmap}


def pick_target_slot(placement: Dict, color: str,
                     column_slots: Optional[Dict[str, Sequence[str]]] = None,
                     skip_slots: Optional[Sequence[str]] = None
                     ) -> Tuple[Optional[str], str]:
    """按颜色取该列的**下一个空行**。

    ★ `skip_slots`（2026-10-10 用户口径"某格不能放"）：落位时**跳过被禁的格**。
      默认 None → 与原来逐字一致（**分拣流程/井字棋完全不受影响**）。
      跳过导致列内无位时，原因要能分清是"天然满了"还是"被禁放占掉了" ——
      两者现场处理方式完全不同（一个加料盘、一个改约束）。

    返回 (槽位名 | None, 原因)。
      槽位名非 None → 可放；None 时 reason 说明为什么放不下
    """
    cmap = column_slots or DEFAULT_COLUMN_SLOTS
    if color not in cmap:
        return None, "未知颜色"
    slots = list(cmap[color])
    used = placement.get(color) or []
    skip = set(skip_slots or ())
    free = [s for s in slots if s not in used and s not in skip]
    if not free:
        banned = [s for s in slots if s not in used and s in skip]
        if banned:
            return None, "列内可用格已用尽（禁放了 %d 个：%s）" % (
                len(banned), "、".join(banned))
        return None, "列已满"
    return free[0], ""


def place(placement: Dict, color: str, slot: str) -> Dict:
    """登记一次放料（原地修改并返回）。"""
    placement.setdefault(color, []).append(slot)
    return placement


def placement_summary(placement: Dict,
                      column_slots: Optional[Dict[str, Sequence[str]]] = None) -> List[Dict]:
    """给界面用的三列占用概览。"""
    cmap = column_slots or DEFAULT_COLUMN_SLOTS
    out = []
    for color in cmap:
        slots = list(cmap[color])
        used = placement.get(color) or []
        out.append({"color": color, "slots": slots, "used": used,
                    "filled": len(used), "capacity": len(slots),
                    "full": len(used) >= len(slots)})
    return out


def all_full(placement: Dict,
             column_slots: Optional[Dict[str, Sequence[str]]] = None) -> bool:
    return all(c["full"] for c in placement_summary(placement, column_slots))


# ---------------------------------------------------------------- 判色投票
def fresh_run_state(dry_run: bool, max_pieces: int, judge: bool) -> Dict:
    """**一轮全新运行**的初始状态（每次「开始」都必须用它重建）。

    ★ 用户口径（2026-10-08）："每次运行必须从头开始"。
      —— 不是"接着上次跑"：进度、占位表、判色、停止原因、夹持/疑似标记**全部清零**，
         并且引擎从 A1 第一格重新开始（见 api/sorting.py 的执行循环）。
    ★ 为什么单列成函数：它同时是"开始"接口的**契约**，能被单元测试直接钉住 ——
      以前这段字典写在接口里，只有整条 HTTP 链路才能测，容易悄悄长出一个残留字段。
    """
    return {
        "dry_run": bool(dry_run),
        "piece": 0,                 # 第几件（0 = 还没开始）
        "total": int(max_pieces),
        "stage": "准备",
        "color": "", "conf": 0.0, "target": "",
        # ★ 2026-10-09：抓帧明细（每帧颜色/置信度/是否有效）与"置信度最高的那一帧"下标。
        #   每轮都清零 —— 否则新一轮开头会显示上一轮抓到的颜色（"每次运行从头开始"口径）。
        "frame_detail": [], "best_frame": None,
        "votes": {},
        "placement": placement_summary(new_placement()),   # B 区三列全空
        "error": "", "finished": False, "ok": None,
        "judge": bool(judge),        # 本轮是否会真的判色（空跑/双闸未开 = False）
        "sim_color": False,
        # 3D 可视化用（按流程推断，非真空传感器实测）
        "holding": False, "suspect_pick": False,
        # ★★ 2026-10-10 双面（一趟两件）新增：两个吸盘槽（下板 lower / 上板 upper），
        #   各为 None 或 {"piece": 来源A格, "color": str, "ok": bool, "conf": float}。
        #   ★ **现场没有真空传感器 → 槽里有什么全是"流程推断"**，界面/3D 必须如实标注。
        #   ★ 初始两槽都是 None；`holding`（旧字段）仍为 False，**既有前端/3D 零回归**。
        #     两槽内容由下面的 `face_slot_put/take` 与 `held_slots` 维护。
        "hold_lower": None, "hold_upper": None,
    }


def frame_color(f: Dict, min_conf: float = 0.0,
                names: Sequence[str] = COLORS) -> Dict:
    """从**一帧** `/vision/detect` 的返回里取出"这一帧看到什么颜色"（归一化）。

    ★★ 2026-10-08 P0 修复（现场报"已停止：检测不到（无有效判定（全部未知或置信度低于 0.28））"）：
      `/vision/detect` 的真实返回是
        {"ok":true, "results":[{"area":.., "color":{"name":"红","conf":0.99,"reason":""}}], "tracker":{...}}
      —— 颜色**嵌在 `results[i].color` 里**；而 `vote_color` 原先读的是**顶层** `name/conf`，
      永远读不到 → 每一帧都被判成"未知" → **必然**报"检测不到"。
      更糟的是：`results:[]`（画面里没东西）与"读法写错"产生**同一条**文案，
      现场根本无法区分 —— 这就是那句提示一直误导人的原因。
      ★ 22 条分拣单元测试全绿也掩盖了它：用例喂的是**自己造的扁平结构** `{"name":"红","conf":.9}`，
        与真实接口对不上。回归夹具已改为**真实抓取**：`tests/data/vision_detect_frame.json`。

    本函数是唯一的读法：**两种形状都接受**（真实形状优先），并选出"这一帧的那一个目标"
    （与实时画面同一口径：先看"名次有效且过 min_conf"，再比面积）。
    返回 {"name","conf","reason","area"}；判不出时 reason 一定非空（供上层如实回报原因）。
    """
    if not isinstance(f, dict):
        return {"name": "", "conf": 0.0, "reason": "无返回", "area": 0.0}
    if f.get("ok") is False:
        return {"name": "", "conf": 0.0,
                "reason": str(f.get("error") or "视觉服务返回失败"), "area": 0.0}

    results = f.get("results")
    if isinstance(results, list):
        if not results:
            # 画面里没有候选：与"读法写错"区分开，明确说明是"没看到东西"
            return {"name": "", "conf": 0.0,
                    "reason": "画面里没有候选（未检测到红/绿/蓝矩形：可能太小、太斜或被挡住）",
                    "area": 0.0}

        def _rank(r):
            c = (r.get("color") or {}) if isinstance(r, dict) else {}
            n = str(c.get("name") or "").strip()
            try:
                cf = float(c.get("conf") or 0.0)
            except (TypeError, ValueError):
                cf = 0.0
            ok = 1 if (n in names and cf >= float(min_conf)) else 0
            try:
                ar = float((r or {}).get("area") or 0.0)
            except (TypeError, ValueError):
                ar = 0.0
            return (ok, ar, cf)

        best = max([r for r in results if isinstance(r, dict)], key=_rank, default=None)
        if best is None:
            return {"name": "", "conf": 0.0, "reason": "候选结构异常", "area": 0.0}
        c = best.get("color") or {}
        n = str(c.get("name") or "").strip()
        try:
            cf = float(c.get("conf") or 0.0)
        except (TypeError, ValueError):
            cf = 0.0
        try:
            ar = float(best.get("area") or 0.0)
        except (TypeError, ValueError):
            ar = 0.0
        return {"name": n, "conf": cf, "reason": str(c.get("reason") or "").strip(),
                "area": ar}

    # ---- 扁平形状（旧调用方/单元测试直接喂 {"name","conf"}）----
    n = str(f.get("name") or "").strip()
    try:
        cf = float(f.get("conf") or 0.0)
    except (TypeError, ValueError):
        cf = 0.0
    return {"name": n, "conf": cf, "reason": str(f.get("reason") or "").strip(),
            "area": 0.0}


def vote_color(frames: Sequence[Dict], min_conf: float = 0.3,
               accept_ratio: float = 0.5,
               valid_colors: Optional[Sequence[str]] = None) -> Dict:
    """多帧判定 → 采信结论。

    frames: 每次 `GET /vision/detect` 的**原始返回**（颜色在 results[i].color 里；
            也兼容 {"name","conf"} 扁平结构 —— 见 `frame_color`）。
    规则（现场口径："检测不到就直接停"，所以宁可判不出也不硬猜）：
      1) 逐帧过滤：name 属于**有效色集**（默认 红/绿/蓝；可由 `valid_colors` 覆盖为
         "用户这句话提到的颜色"）且 conf ≥ min_conf 才算"有效帧"；
      2) 取有效帧里出现次数最多的颜色为结论；
      3) 命中帧数须 ≥ ceil(帧数 × accept_ratio)（默认 3 帧取 2 帧多数）；
      4) 无有效帧 / 票数不够 / 票数并列 → 判"未知"（→ 由上层停止并回原点）。

    返回 {"ok", "color", "conf", "votes", "valid", "frames", "reason"}
    """
    # ★★ 2026-10-10：**有效色集**（默认红/绿/蓝 → 向后兼容）。传"用户这句话提到的颜色"
    #   即实现"没提到的颜色 = 无效色"：调色板内但不在此集合的颜色 → 与黑一样按无效处理。
    #   ★ 去重但保序（重复项会让 winners 出现两条同色 → 误判"结论不一致"）。
    valid_set: Tuple[str, ...] = (tuple(dict.fromkeys(valid_colors))
                                  if valid_colors else COLORS)
    names: List[str] = []
    confs: List[float] = []
    # ★ 逐帧的"未判定原因"（服务端给什么就记什么：非矩形 / 太小 / 非红绿蓝三色 / 低置信…）
    #   用途：判不出时把**真原因**写进停止文案 —— 过去只有"无有效判定（全部未知…）"，
    #   现场看不出到底是"太小"还是"太斜"还是"颜色不对"，只能瞎调旋钮。
    why: List[str] = []
    # ★ 2026-10-09 新增（用户口径："捕捉3张，只显示一张置信度最高的"）：
    #   逐帧明细，供界面把抓到的 3 帧**如实**列出来并突出置信度最高的那张。
    #   以前 votes 只有"每色几票"，现场看不到"这 3 帧各自看到什么" ——
    #   判不出时无法判断是"三帧都低"还是"两帧对一帧错"。
    detail_frames: List[Dict] = []
    for f in (frames or []):
        if not isinstance(f, dict):
            why.append("无返回（相机服务未响应/未开启）")
            detail_frames.append({"color": "", "conf": 0.0, "ok": False,
                                  "reason": "无返回（相机服务未响应/未开启）"})
            continue
        got = frame_color(f, min_conf, valid_set)
        valid = got["name"] in valid_set and got["conf"] >= float(min_conf)
        detail_frames.append({"color": got["name"] if got["name"] in valid_set else "",
                              "conf": round(float(got["conf"]), 3),
                              "ok": bool(valid),
                              "reason": "" if valid else (got["reason"] or "")})
        if valid:
            names.append(got["name"])
            confs.append(got["conf"])
        else:
            why.append(got["reason"] or ("未知（置信度 %.2f 低于阈值 %.2f）"
                                         % (got["conf"], float(min_conf))))

    total = len(frames or [])
    votes = {c: names.count(c) for c in valid_set}
    if not names:
        detail = "；".join(dict.fromkeys(why)) if why else "全部未知或置信度低于 %.2f" % min_conf
        return {"ok": False, "color": COLOR_UNKNOWN, "conf": 0.0, "votes": votes,
                "valid": 0, "frames": total, "frame_detail": detail_frames,
                "reason": "无有效判定（%s）" % detail}

    top = max(votes.values())
    winners = [c for c in valid_set if votes[c] == top]
    need = max(1, int(round(total * float(accept_ratio) + 1e-9)))
    if len(winners) > 1:
        return {"ok": False, "color": COLOR_UNKNOWN, "conf": 0.0, "votes": votes,
                "valid": len(names), "frames": total, "frame_detail": detail_frames,
                "reason": "多帧结论不一致（%s）" % " / ".join(
                    "%s %d 票" % (c, votes[c]) for c in valid_set)}
    if top < need:
        return {"ok": False, "color": COLOR_UNKNOWN, "conf": 0.0, "votes": votes,
                "valid": len(names), "frames": total, "frame_detail": detail_frames,
                "reason": "有效帧不足（%d/%d 帧命中，需 ≥%d）" % (top, total, need)}

    color = winners[0]
    hit = [confs[i] for i in range(len(names)) if names[i] == color]
    # ★ best = 命中帧里置信度最高的那一张（用户口径："只显示一张置信度最高的"）
    best = max((i for i in range(len(names)) if names[i] == color),
               key=lambda i: confs[i])
    return {"ok": True, "color": color, "conf": round(sum(hit) / len(hit), 3),
            "votes": votes, "valid": len(names), "frames": total, "reason": "",
            "best_frame": int(best), "frame_detail": detail_frames}


# ---------------------------------------------------------------- 双面（一趟两件）
# ★★ 2026-10-10 用户口径：J6 工装是**两块板**（下板 −x / 上板 +x），共用**同一路真空**，
#   靠 **J6 翻转**用上另一面 → **一趟两面各吸一件**。
#   送检时：**正面 2 帧 + 反面 2 帧 = 4 帧**，两面**各自投票**、给出**两个独立结论**
#   （可以是不同颜色，也可以有一件是"无效色/判不出"）。
#
#   ⚠ 为什么不能用现在的 `vote_color(4帧)` 一把投：那样会把两件的颜色**混成一份结论**
#     （多数票会把另一件吃掉），两件就分不清谁是谁了。
#
#   面序（固定，与"先吸哪件"一致）：
#     slot 0 = "lower"  → **下板**（-x，先吸的那件；也是 3D 里挂在下方那件）
#     slot 1 = "upper"  → **上板**（+x，J6 翻转后才吸的那件）
#   详见 docs/方案-J6双面吸盘（一趟两件·靠J6翻转用另一面）.md
FACE_SLOTS: Tuple[str, str] = ("lower", "upper")


def vote_color_per_face(groups: Sequence[Sequence[Dict]], min_conf: float = 0.3,
                        valid_colors: Optional[Sequence[str]] = None) -> List[Dict]:
    """**按面**投票：每个面各传一组帧（各 2 帧），返回**逐面**的结论（顺序=传入顺序）。

    ★ 语义：**只做"分组"，判据仍完全复用 `vote_color`** —— 不新增第二套投票逻辑，
      避免出现"两套判色口径"（那正是本项目最忌讳的分叉）。

    返回长度与 `groups` 相同；每项 = `vote_color` 的原样结果 + 一个 `"slot"` 字段
    （`"lower"` = 下板/先吸的那件，`"upper"` = 上板/翻转后吸的那件）。
    """
    out: List[Dict] = []
    for i, frames in enumerate(groups or []):
        slot = FACE_SLOTS[i] if i < len(FACE_SLOTS) else ("face%d" % i)
        v = vote_color(list(frames or []), min_conf, valid_colors=valid_colors)
        v = dict(v)
        v["slot"] = slot
        out.append(v)
    return out


def both_faces_ok(verdicts: Sequence[Dict]) -> bool:
    """两个面**都判出了有效色**才算 True（任一面无效 → False，供上层决定"先放有效的那件"）。"""
    vs = list(verdicts or [])
    return bool(vs) and all(bool(v.get("ok")) for v in vs)


# ---------------------------------------------------------------- 顺序（固定，不许混乱）
# ★★ 2026-10-11 用户口径（原话）：
#   「送检，先反面拍照，后前面拍照，然后先送正面，然后送反面，根据拍照来，顺序不能混乱」
#   ⇒ 两个**固定顺序**（把"顺序"收敛成**唯一判据**，避免流程里两处各排一遍排乱）：
#     · 拍照面序：**先反面、后正面**；
#     · 放料次序：**先正面那件、再反面那件**。
#   注意：两者**不是同一顺序**（拍照反着来、放料正着来）—— 这是用户明确要求的，别"顺手统一"。
def face_rank(slot) -> int:
    """槽位的**固定面序**（数值小者在前）：lower（下板/正面）=0、upper（上板/反面）=1；未知 → 2。

    ★ 只此一处定义"面序"：拍照面序与放料次序都基于它，避免两处各写一遍。
    """
    try:
        return FACE_SLOTS.index(slot)
    except (ValueError, TypeError):
        return len(FACE_SLOTS)


def faces_for_capture(count: int) -> List[str]:
    """送检**拍照面序**（固定）：**先反面、后正面**（用户 2026-10-11 口径：顺序不能乱）。

    · `count=2` → `["upper", "lower"]`（先拍上板/反面，再拍下板/正面）；
    · `count=1`（单件趟）→ `["lower"]`（只有下板/正面那一件）。
    ★ 为什么"反着排"：用户明确"先反面拍照、后正面拍照"——与上一轮"先正后反"**相反**
      （回归钉见 tests/test_double_face.py）。
    """
    n = max(0, int(count))
    return list(reversed(list(FACE_SLOTS[:n])))


def order_for_drop(verdicts: Sequence[Dict]) -> List[Dict]:
    """落料**固定次序**：**先正面那件、再反面那件**（用户 2026-10-11 口径：顺序不能乱）。

    ★ 只按**面**排序（lower/正面在前）—— 有效/无效只决定"放到哪"（B 槽 / 原路回 A 原位），
      **不改先后**。返回**新列表**（不改入参顺序）。
    """
    return sorted(list(verdicts or []), key=lambda v: face_rank((v or {}).get("slot")))


# ---------------------------------------------------------------- 双槽夹持（一趟两件）
# ★★ 2026-10-10 用户口径："J6 工具上下两面都能吸" → 一趟两件时**同时**有两件在手上。
#   但**现场没有真空传感器**，所以"哪面有件"只能是**流程推断**（界面/3D 必须如实标注）。
#   · 槽位名沿用 FACE_SLOTS："lower"（下板，先吸）/ "upper"（上板，J6 翻转后吸）；
#   · 每槽记 `{piece(来源A格), color, ok, conf}`；
#   · `holding`（旧字段）**由两槽推导** —— 默认路径与既有前端/3D 零回归。
_FACE_SLOT_KEYS: Dict[str, str] = {"lower": "hold_lower", "upper": "hold_upper"}


def _face_key(slot) -> Optional[str]:
    """把槽位标识（"lower"/"upper"，或 0/1 下标）归一成 state 里的键名；非法 → None。"""
    if isinstance(slot, int) and not isinstance(slot, bool):
        slot = FACE_SLOTS[slot] if 0 <= slot < len(FACE_SLOTS) else None
    if slot is None:
        return None
    return _FACE_SLOT_KEYS.get(str(slot))


def held_slots(state: Dict) -> List[str]:
    """当前**有件的面**（按 FACE_SLOTS 顺序）：[] / ["lower"] / ["upper"] / ["lower","upper"]。"""
    if not isinstance(state, dict):
        return []
    return [s for s in FACE_SLOTS if state.get(_FACE_SLOT_KEYS[s]) is not None]


def face_slot_put(state: Dict, slot, **kw) -> Optional[Dict]:
    """把一个工件登记到某一面的槽位（**纯逻辑**，不碰硬件）。

    kw：`piece`（来源 A 格）/ `color` / `ok` / `conf`（缺省 None / "" / False / 0.0）。
    ★ 重复 put 同一槽 = **覆盖**（同一面不可能同时挂两件）。
    ★ **就地**刷新 `holding`（旧字段）= 任一面有件 → True，兼容既有前端/3D。
    ★ slot 非法 / state 不是 dict → 返回 None 且**不改状态**（不静默写到错的地方）。
    """
    key = _face_key(slot)
    if key is None or not isinstance(state, dict):
        return None
    info = {
        "piece": kw.get("piece"),
        "color": str(kw.get("color") or ""),
        "ok": bool(kw.get("ok", False)),
        "conf": float(kw.get("conf") or 0.0),
    }
    state[key] = info
    state["holding"] = bool(held_slots(state))
    return info


def face_slot_take(state: Dict, slot) -> Optional[Dict]:
    """取走某一面的件（放开/落料后调用），返回被取走的登记；**空槽 → None（不报错）**。

    ★ 与 put 对称：无论槽空不空，都**就地**按剩余槽刷新 `holding`。
    """
    key = _face_key(slot)
    if key is None or not isinstance(state, dict):
        return None
    info = state.get(key)
    state[key] = None
    state["holding"] = bool(held_slots(state))
    return info


# ---------------------------------------------------------------- 旋转黑名单（安全护栏）
# ★★ 2026-10-11 用户口径变更（**原话**，两条合并）：
#   ① 「J6 反正不能在**原位**进行旋转！其他的时候可以旋转」
#   ② 「只要 J6 正反面自己能无论在哪儿我测试必须旋转，18 个正上位，摄像头点位，
#       起点可以旋转，也可以从 A 到 B 或者 A 到 C……18 个原位不能旋转，只要能实现这个可以！！」
#   —— 「原位」= **原料位**：**A 区 9 个格位（A1…A9）+ B 区 9 个格位（B1…B9）= 共 18 个**。
#   ⇒ 判据由旧的"**白名单**（只允许 正上位/摄像头点位/起点）"改为
#      "**黑名单**：**只有原料位禁止旋转，其余一切允许**"。
#   ★ 为什么**必须**改（旧白名单的两个反效果）：
#     · `J6反面`（= 双面翻转的目标点）被判 False → `_rotation_guard` 直接拒绝 →
#       **双面流程根本跑不起来**（这正是用户报的"J6 不对"背后的真障碍）；
#     · 连 `A1上`（A 区**正上位**）也被判 False，与"在正上位翻转"的设计**自相矛盾**。
#   ★ 2026-10-11 **本轮扩围**（用户原话见上）：黑名单**由"只禁 A 区格位"扩到 A+B 共 18 个原位**。
#     原实现 `^A\d+` **把 B 区格位（B1…B9）当成"允许"** —— 与用户"18 个原位不能旋转"
#     直接冲突，故一并禁止（B 区格位也是"原位/原料位"，只是放的是不同颜色）。
#
# ★ 本护栏**只守一件事**：**带 J6 翻转的下发不许落到原料位（A/B 区格位本身，共 18 个）**。
#   命名依据（**对齐项目既有点名的实际生成规则，未臆测**）：
#     · 后端 `expected_points()`：A 区格位 = `"A%d"`（A1..A9），正上方 = `"A%d上"`（A1上..A9上）；
#       B 区格位 = `"B%d"`（B1..B9），正上方 = `"B%d上"`（B1上..B9上）；
#     · 前端 `SortingView.vue` 的 aCells/bCells 同构：`{name: s+"上", label:"正上位"}`
#       与 `{name: s, label:"原料位"}`。
#   ⇒ 所以"原料位"= 以 `A` 或 `B` + 数字开头、且**不带正上位标记 `上`** 的写法。
#
# ★ 判据（**保守取向**）：把名字归一（去所有空白 + 转大写）后 ——
#   · 以 `A`/`B` + 数字开头、且不含 `上` → **原料位 → 禁止**；
#     ⚠ "保守"在此的含义：`A1` / `b1` / `A1 ` / `B1(原料)` 这类**大小写/空格/带后缀**写法，
#       只要"看着像格位又不带正上位标记"，就**一律按原料位拒绝**（宁可错拒，不可放行）。
#   · 带 `上`（A1上…B9上）→ **不是**原料位本身 → 允许；
#   · 不以 `A/B+数字` 开头（正上位 / J6正面 / J6反面 / C检测 / 摄像头位 / 开始/结束点 / 起点 / home）
#     → **不是**原料位 → 允许。
_RAW_SLOT_RE = re.compile(r"^[AB]\d+")


def is_raw_material_slot(name: str) -> bool:
    """该点位是否 = **原料位**（A/B 区格位本身，如 A1…A9、B1…B9，共 18 个）→ **唯一**禁止旋转的位置。

    ★ 这是**黑名单**判据（**不是**白名单）：本函数返回 True 的才禁止，其余一切（含 A1上、J6反面…）允许。
    ★ 归一化：去所有空白 + 转大写 → 兼容 `a1` / `B1 ` / ` b 1 `。
    ★ **保守取向（有意为之）**：凡"以 A/B+数字开头、又不带正上位标记 `上`"的写法
      （如 `A1` / `B1(原料)`）**一律按原料位处理 = 拒绝** —— 判不准时宁可拒绝，不冒险放行。
    ★ 用户原话：「18 个原位不能旋转」（A 区 9 个 + B 区 9 个）。
    """
    norm = re.sub(r"\s+", "", str(name or "")).upper()
    if not _RAW_SLOT_RE.match(norm):
        return False          # 不是格位形态（正上位/J6反面/C检测/开始/结束点…）→ 不禁止
    return "上" not in norm   # A1上/B1上（正上位）→ 不是原料位；其余 A/B+数字（A1/B5…）→ 原料位


def rotation_point_allowed(name: str) -> bool:
    """★ 方案 §6-1（**2026-10-11 更新口径**）：该点位是否**允许**出现"带 J6 翻转的下发"。

    **黑名单**：**只有原料位（A 区 + B 区格位本身，共 18 个）禁止旋转，其余一切允许**。
    用户原话：「J6 反正不能在**原位**进行旋转！其他的时候可以旋转」；
              「18 个正上位，摄像头点位，起点可以旋转……18 个原位不能旋转」。
    空名字（`""` / None）→ False（**保守**：没有点名就不放行，沿用旧行为）。
    """
    n = str(name or "").strip()
    if not n:
        return False
    return not is_raw_material_slot(n)


# ---------------------------------------------------------------- 流程编排（纯函数）
def plan_trips(slots: Sequence[str], per_trip: int = 2) -> List[List[str]]:
    """把待取的 A 区格位按"一趟两件"分趟（方案 §2：9 件 = **4 趟×2 + 最后 1 趟×1**）。

    ★ 为什么单独成函数：这是纯可测的**节拍**；放在流程里就只能靠整跑真机验证。
    ★ per_trip <= 0 → 按 1 处理（避免死循环）。
    """
    seq = [str(s) for s in (slots or [])]
    n = int(per_trip) if int(per_trip) > 0 else 1
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def build_piece_plan(placement: Dict, color: str,
                     column_slots: Optional[Dict[str, Sequence[str]]] = None,
                     skip_slots: Optional[Sequence[str]] = None
                     ) -> Dict:
    """判色之后 → 这一件的落料计划（纯计算，不执行）。

    返回 {"ok", "slot", "reason"}；ok=False 时 reason 即"停止原因"。
    """
    slot, why = pick_target_slot(placement, color, column_slots, skip_slots)
    if slot is None:
        return {"ok": False, "slot": None, "reason": why,
                "detail": ("%s 列已放满 %d 件" % (color, COLUMN_CAPACITY)) if why == "列已满" else why}
    return {"ok": True, "slot": slot, "reason": "", "detail": ""}
