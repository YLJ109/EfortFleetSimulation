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


# ---------------------------------------------------------------- 旋转白名单（安全护栏）
# ★★ 方案 §6-1 / 用户口径："只允许在 正上位、摄像头点位、起点 旋转，**严禁原料位旋转**"。
#   这是**回归钉**：凡"带 J6 翻转的下发"落到原料位（A 区格位）→ 后端直接拒绝并写明原因。
# ★ 命名依据（对齐**项目既有点位**，不自己发明）：摄像头点位 = CAMERA_POINT（"C检测"）、
#   起点 = START_POINT（"开始/结束点"）；"正上位"是用户对"取料前抬高避让位"的口语叫法。
# ★ 用**子串白名单**（兼容 "C检测位"/"摄像头点位"/"正上位" 等叫法）；A 区原料格位
#   （A1..A9、A1上..）不含任一关键字 → **天然被拒**，不需要另写黑名单。
ROTATION_ALLOWED_TOKENS: Tuple[str, ...] = (
    "正上位", "摄像头", "C", "起点", "home", "开始",
)


def rotation_point_allowed(name: str) -> bool:
    """★ 方案 §6-1：该点位是否**允许**出现"带 J6 翻转的下发"。

    允许：正上位 / 摄像头点位 / 起点（见 ROTATION_ALLOWED_TOKENS 的命名依据）；
    原料位（A 区格位）→ False（**严禁原料位旋转**）。
    """
    n = str(name or "").strip()
    if not n:
        return False
    low = n.lower()
    return any((tok in n) or (tok.lower() in low) for tok in ROTATION_ALLOWED_TOKENS)


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
