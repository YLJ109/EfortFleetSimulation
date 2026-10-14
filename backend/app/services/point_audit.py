# -*- coding: utf-8 -*-
"""D2 点位数值体检（**离线 · 只读 · 纯计算**）。

★ 这一条为什么留下（而"摆放体检"整批撤了）：负责人确认**现场摆放没问题**，
  但**点位数值本身**出过真事故 —— 点位库里 J6 录成 −360 → `encode_angle` 拒绝
  → **整条流程被终止**（AGENTS.md §5.3 已固化）。体检防的是这一类
  "**看着是合法姿态、其实下发不出去**"的数据，与摆得好不好无关。

做什么（只三样，全是纯函数）：
  ① **±327 可表示性**：控制器角度走 int16×100，超出 ±327.67° 直接被拒；
  ② **软限位裕度**：离限位还剩几度（限位已由 `limits.load_limits` 收窄到可表示范围）；
  ③ **粗略奇异指示**：空间雅可比条件数（只用来把点排在一起比较，**不当严格判据**）。

★ 不做什么（红线，改动前先读）：
  · **绝不回写点位库**、绝不"顺手把 −360 折成 0"—— 那是替操作员改数据；
    要改只输出**建议**，由人决定重录还是改点（`advice` 字段）。
  · **绝不下发任何运动**：不 import `motion` / `modbus` 的写路径、不碰 40101/40103/40135。
  · **不做 ETA / 时间推断**（同 A2 扫掠口径）。
  · 数据坏 / 轴数不对 / NaN → `evaluable=false` + 原因，**不许塌成"没问题"**（§5.5 铁律③）。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from app.services.limits import ENCODE_LIMIT_DEG, bounds_of, load_limits

#: 离限位小于这个度数就算"紧"（提示重录时换个更舒展的姿态，别卡在限位边上）
MARGIN_WARN_DEG = 5.0
#: 条件数超过它标"接近奇异"（正常六轴位姿通常 1e2~1e3；腕部奇异会冲到 1e7+）
COND_WARN = 1e4

AXIS_NAMES = ("J1", "J2", "J3", "J4", "J5", "J6")

GOOD, WARN, BAD, UNKNOWN = "ok", "warn", "bad", "unknown"


def _finite6(q: Any) -> Optional[List[float]]:
    """能变成 6 个**有限**浮点就返回 list，否则返回 None（不猜、不补 0）。"""
    try:
        arr = [float(x) for x in q]
    except (TypeError, ValueError):
        return None
    if len(arr) != 6:
        return None
    for v in arr:
        if math.isnan(v) or math.isinf(v):
            return None
    return arr


def _limits(limits: Optional[Sequence[Any]]) -> List[Any]:
    return list(limits) if limits is not None and len(list(limits)) == 6 else load_limits()


def _encodability(q: List[float]) -> List[Dict[str, Any]]:
    """逐轴判"能不能编码"（与 `modbus.encode_angle` 同一口径，但**不调用它**）。"""
    out = []
    for name, v in zip(AXIS_NAMES, q):
        if abs(v) > ENCODE_LIMIT_DEG:
            out.append({"axis": name, "value": round(v, 3),
                        "encodable_limit_deg": ENCODE_LIMIT_DEG,
                        "over_by_deg": round(abs(v) - ENCODE_LIMIT_DEG, 3)})
    return out


def _limit_margins(q: List[float], limits: List[Any]) -> Dict[str, Any]:
    axes, violations, tight = [], [], []
    worst: Optional[float] = None
    for i, (name, v) in enumerate(zip(AXIS_NAMES, q)):
        lo, hi = bounds_of(limits[i])
        low_gap, high_gap = v - lo, hi - v
        gap = min(low_gap, high_gap)
        axes.append({"axis": name, "value": round(v, 3),
                     "min": lo, "max": hi,
                     "low_gap_deg": round(low_gap, 3),
                     "high_gap_deg": round(high_gap, 3),
                     "gap_deg": round(gap, 3),
                     "toward": "low" if low_gap <= high_gap else "high"})
        if gap < 0:
            violations.append({"axis": name, "value": round(v, 3), "min": lo, "max": hi,
                               "over_by_deg": round(-gap, 3)})
        elif gap < MARGIN_WARN_DEG:
            tight.append(name)
        worst = gap if worst is None else min(worst, gap)
    return {"axes": axes, "violations": violations,
            "tight_axes": tight, "worst_margin_deg": None if worst is None else round(worst, 3)}


def _singularity(q: List[float]) -> Dict[str, Any]:
    """空间雅可比条件数 —— **粗略**奇异指示，只在点与点之间横向比较用。

    ★ 诚实口径：J 的行列混合了 mm/deg 与 rad/deg，条件数不是无量纲的"奇异度"，
      所以这里只给"接近/不接近"的**相对信号**，并如实带上 note；
      算不出来（运动学模块不可用）→ `measured=false`，**不返回 0 冒充"不奇异"**。
    """
    out: Dict[str, Any] = {"measured": False, "cond": None, "near": None,
                           "note": "未计算（运动学模块不可用）"}
    try:
        import numpy as np
        from app.services.kinematics import jacobian_space
        J = jacobian_space(q)
        cond = float(np.linalg.cond(np.asarray(J, dtype=float)))
        bad = not math.isfinite(cond)
        out.update({"measured": not bad,
                    "cond": None if bad else round(cond, 1),
                    "near": (True if bad else cond > COND_WARN),
                    "note": ("条件数发散（数值上已不可逆）" if bad
                             else "相对指标：只用于点位之间横向比较，不是严格奇异判据")})
    except Exception as e:
        out["note"] = f"条件数算不出来：{str(e)[:80]}"
    return out


def audit_point(name: str, joints: Any, limits: Optional[Sequence[Any]] = None,
                kind: str = "joint") -> Dict[str, Any]:
    """单个点位的数值体检。`kind="cartesian"` 时关节值不是执行目标 → 如实 not_applicable。"""
    res: Dict[str, Any] = {
        "name": name, "kind": kind, "joints": None,
        "evaluable": False, "grade": UNKNOWN,
        "applicable": True,
        "reasons": [], "unencodable": [], "violations": [], "tight_axes": [],
        "axes": [], "worst_margin_deg": None,
        "singularity": {"measured": False, "cond": None, "near": None,
                        "note": "未计算（点位数据不可用）"},
        "tcp": None,
        "advice": "",
    }
    q = _finite6(joints)
    if q is None:
        res["reasons"].append("关节值不是 6 个有限数值（缺轴 / 非数值 / NaN / Inf）"
                              " ⇒ **无法体检**，不是「没问题」")
        res["advice"] = "重录或修正该点数据后再体检"
        return res
    res.update({"joints": [round(v, 3) for v in q], "evaluable": True})

    if kind != "joint":
        res["applicable"] = False
        res["grade"] = UNKNOWN
        res["reasons"].append("笛卡尔点：执行目标是 TCP（由控制器 IK 反解），"
                              "库里那串关节值不是下发目标 ⇒ 数值体检**不适用**")
        res["advice"] = ("要体检这类点，请用它的**实际下发姿态**（执行后的 6 轴回读）"
                        "重新录一个关节点")
        return res

    lim = _limits(limits)
    res["unencodable"] = _encodability(q)
    mg = _limit_margins(q, lim)
    res.update({"axes": mg["axes"], "violations": mg["violations"],
                "tight_axes": mg["tight_axes"],
                "worst_margin_deg": mg["worst_margin_deg"]})
    res["singularity"] = _singularity(q)
    try:
        from app.services.kinematics import tcp_of
        x, y, z = tcp_of(q)
        res["tcp"] = [round(float(x), 2), round(float(y), 2), round(float(z), 2)]
    except Exception:
        res["tcp"] = None          # FK 算不出就留 null，不编一个坐标

    if res["unencodable"]:
        res["grade"] = BAD
        u = ", ".join(f"{d['axis']}={d['value']}°" for d in res["unencodable"])
        res["reasons"].append(
            f"超出控制器可表示范围（±{ENCODE_LIMIT_DEG}°）：{u} ⇒ **下发必被拒绝**")
        res["advice"] = "重录该点（J6 是转动轴，转过整圈请录成等价角；不要把 −360 当 0 存）"
    elif res["violations"]:
        res["grade"] = BAD
        v = ", ".join(f"{d['axis']}={d['value']}°（限 {d['min']}~{d['max']}）"
                      for d in res["violations"])
        res["reasons"].append(f"越出软限位：{v} ⇒ 会被限位校验拒掉")
        res["advice"] = "重录该点或调整软限位（**本工具不会自动改点**）"
    elif (res["tight_axes"]
          or res["singularity"].get("near") is True):
        res["grade"] = WARN
        if res["tight_axes"]:
            res["reasons"].append(
                f"离限位不足 {MARGIN_WARN_DEG}°：{', '.join(res['tight_axes'])}"
                "（现场一点漂移就可能顶到限位）")
        if res["singularity"].get("near") is True:
            res["reasons"].append(
                f"雅可比条件数 {res['singularity']['cond']:g} > {COND_WARN:g}"
                " ⇒ 接近奇异构型，到位校验/微调试探在这里最容易失真")
        res["advice"] = "建议重录成更舒展的姿态（离限位远一点、避开腕部共线）"
    else:
        res["grade"] = GOOD
        res["advice"] = "无需处理"
    return res


def audit_rows(rows: Sequence[Any], limits: Optional[Sequence[Any]] = None
               ) -> Dict[str, Any]:
    """批量体检（`rows` = 点位 ORM/字典，只要有 `name` / `joints` / `kind`）。

    ★ 排序 = **最该重录的在前**（bad → warn → unknown → ok），同级按余量小者优先。
      这样界面不用做筛选，第一行就是最该处理的点。
    """
    import json
    out: List[Dict[str, Any]] = []
    for r in rows:
        get = (lambda k: r.get(k)) if isinstance(r, dict) else (lambda k: getattr(r, k, None))
        raw = get("joints")
        try:
            joints = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            joints = None
        res = audit_point(str(get("name") or ""), joints,
                          limits=limits, kind=str(get("kind") or "joint"))
        # ★ 带 id/group 出去：界面按 **id** 对齐列表（同名点位不能靠名字映射）
        res["id"] = get("id")
        res["group"] = get("group")
        out.append(res)
    order = {BAD: 0, WARN: 1, UNKNOWN: 2, GOOD: 3}
    out.sort(key=lambda d: (order.get(d["grade"], 9),
                            d["worst_margin_deg"] if d["worst_margin_deg"] is not None else 1e9))
    by_grade = {g: 0 for g in (GOOD, WARN, BAD, UNKNOWN)}
    for d in out:
        by_grade[d["grade"]] = by_grade.get(d["grade"], 0) + 1
    return {
        "count": len(out),
        "by_grade": by_grade,
        "encode_limit_deg": ENCODE_LIMIT_DEG,
        "margin_warn_deg": MARGIN_WARN_DEG,
        "points": out,
        # ★ 诚实边界：这三样**不在**本工具判定范围（别让它看着像"点位全绿灯=能干活"）
        "not_checked": [
            "点位摆得对不对 / 料位间距是否合理（现场已确认，无此需求）",
            "从当前位置**能不能到**（那是 A2 路径扫掠的前瞻口径，静态体检给不出）",
            "会不会撞围栏（由 safety_guard / safety_eval 实时判，本工具不复制一套）",
        ],
        "note": "只读体检：不写库、不下发、不改点；给出的都是**建议**，改不改由人决定。",
    }
