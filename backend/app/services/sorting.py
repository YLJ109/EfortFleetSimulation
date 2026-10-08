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
                     column_slots: Optional[Dict[str, Sequence[str]]] = None
                     ) -> Tuple[Optional[str], str]:
    """按颜色取该列的**下一个空行**。

    返回 (槽位名 | None, 原因)。
      槽位名非 None → 可放；None 时 reason ∈ {"未知颜色","列已满","颜色不支持"}
    """
    cmap = column_slots or DEFAULT_COLUMN_SLOTS
    if color not in cmap:
        return None, "未知颜色"
    slots = list(cmap[color])
    used = placement.get(color) or []
    free = [s for s in slots if s not in used]
    if not free:
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
def vote_color(frames: Sequence[Dict], min_conf: float = 0.3,
               accept_ratio: float = 0.5) -> Dict:
    """多帧判定 → 采信结论。

    frames: 每次 `GET /vision/detect` 的返回（含 name / conf）
    规则（现场口径："检测不到就直接停"，所以宁可判不出也不硬猜）：
      1) 逐帧过滤：name 属于 红/绿/蓝 且 conf ≥ min_conf 才算"有效帧"；
      2) 取有效帧里出现次数最多的颜色为结论；
      3) 命中帧数须 ≥ ceil(帧数 × accept_ratio)（默认 3 帧取 2 帧多数）；
      4) 无有效帧 / 票数不够 / 票数并列 → 判"未知"（→ 由上层停止并回原点）。

    返回 {"ok", "color", "conf", "votes", "valid", "frames", "reason"}
    """
    names: List[str] = []
    confs: List[float] = []
    for f in (frames or []):
        if not isinstance(f, dict):
            continue
        n = str(f.get("name") or "").strip()
        try:
            c = float(f.get("conf") or 0.0)
        except (TypeError, ValueError):
            c = 0.0
        if n in COLORS and c >= float(min_conf):
            names.append(n)
            confs.append(c)

    total = len(frames or [])
    votes = {c: names.count(c) for c in COLORS}
    if not names:
        return {"ok": False, "color": COLOR_UNKNOWN, "conf": 0.0, "votes": votes,
                "valid": 0, "frames": total,
                "reason": "无有效判定（全部未知或置信度低于 %.2f）" % min_conf}

    top = max(votes.values())
    winners = [c for c in COLORS if votes[c] == top]
    need = max(1, int(round(total * float(accept_ratio) + 1e-9)))
    if len(winners) > 1:
        return {"ok": False, "color": COLOR_UNKNOWN, "conf": 0.0, "votes": votes,
                "valid": len(names), "frames": total,
                "reason": "多帧结论不一致（%s）" % " / ".join(
                    "%s %d 票" % (c, votes[c]) for c in COLORS)}
    if top < need:
        return {"ok": False, "color": COLOR_UNKNOWN, "conf": 0.0, "votes": votes,
                "valid": len(names), "frames": total,
                "reason": "有效帧不足（%d/%d 帧命中，需 ≥%d）" % (top, total, need)}

    color = winners[0]
    hit = [confs[i] for i in range(len(names)) if names[i] == color]
    return {"ok": True, "color": color, "conf": round(sum(hit) / len(hit), 3),
            "votes": votes, "valid": len(names), "frames": total, "reason": ""}


# ---------------------------------------------------------------- 流程编排（纯函数）
def build_piece_plan(placement: Dict, color: str,
                     column_slots: Optional[Dict[str, Sequence[str]]] = None
                     ) -> Dict:
    """判色之后 → 这一件的落料计划（纯计算，不执行）。

    返回 {"ok", "slot", "reason"}；ok=False 时 reason 即"停止原因"。
    """
    slot, why = pick_target_slot(placement, color, column_slots)
    if slot is None:
        return {"ok": False, "slot": None, "reason": why,
                "detail": ("%s 列已放满 %d 件" % (color, COLUMN_CAPACITY)) if why == "列已满" else why}
    return {"ok": True, "slot": slot, "reason": "", "detail": ""}
