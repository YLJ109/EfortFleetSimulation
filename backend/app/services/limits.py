# -*- coding: utf-8 -*-
"""关节限位的**唯一事实来源** —— ★ 审计修复 P1-E1。

原先有 4 份互相独立的实现，而且**已经出现事实分叉**（审计文档 §3 P1-E1）：

    位置                      返回类型     缺省限位   挡 NaN    长度不等时
    api/control.py            List[dict]   ±180       否        zip 静默截断 → 漏查
    services/motion.py        List[tuple]  ±180       是        zip 静默截断 → 漏查
    services/jog.py           List[tuple]  ±180       —         下标越界 / 漏查
    services/vision_rules.py  List[dict]   **±360**   否        按 range(6) 索引 → 短 q 直接 IndexError

    两条最要命的分叉：
    ① 缺省限位不一致：同样是"配置缺失"，视觉自动执行会比手动控制多走一倍的量；
    ② 判据不一致：预演（control）与下发（motion）对**同一个目标**可能给出相反结论
      —— 界面提示"通过"、下发被拒还算好的；反过来才是安全问题。

本模块把判据固定成一句话，任何调用点都不许再自己写一遍：

        **先看长度 → 再看有限性 → 再看区间**

细节约定（改动前先读）：
  * 缺配置 / 单条配置写坏 → 退到 ±180（保守方向），而不是崩溃或整段失效；
  * `in_limits` 长度不等一律 False —— 用 zip 会静默漏查后面的轴（老实现的真 bug）；
  * NaN / Inf 一律 False —— 浮点比较里 NaN 与任何数比较都是 False，
    写成 `not (v < lo or v > hi)` 就会被判成"在限内"，等于给 NaN 发通行证；
  * `clamp` 对非有限值**保持原样不夹**（夹紧等于凭空捏一个位置，比报错更危险），
    交给 `in_limits` 在下发前拒掉；`in_limits` 返回 False 的输入里
    非有限值必然被拒，所以这条路是 fail-safe 的。

用法：
    load_limits()  -> list[dict]     带名字，给 UI / 错误信息用
    load_ranges()  -> list[tuple]    (lo, hi)，给求解器 / 夹紧用
    in_limits(q)   -> bool           判定
    clamp(q)       -> (list, bool)   夹紧，返回 (新值列表, 是否真的发生夹紧)
    violations(q)  -> list[dict]     要"哪个轴越界、越了多少"时用它，别再手写循环
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from app.core.config import get_config

#: 关节数（ER8-700H 六轴；多给的截断、少给的补默认，保证长度恒定）
JOINT_COUNT = 6
#: 缺配置 / 单条写坏时的兜底限位（统一取更保守的 ±180 —— 以前 vision 是 ±360）
DEFAULT_MIN = -180.0
DEFAULT_MAX = 180.0
#: 判定容差（角度 deg）。控制在 1e-6，纯粹为了吃掉浮点尾巴，不影响任何真实边界。
TOL = 1e-6
#: ★★ 控制器**角度表示范围**（有符号 int16 ×100）：±327.67° → 取 327.0 留余量。
#   见 `services/modbus.py::ModbusRobot.encode_angle` —— **超出它会被直接拒绝下发**。
#   2026-10-11 现场事故：`joint_limits.J6` 写成 ±360，**比可表示范围宽** →
#   归一化后放行 −360 → 下发被拒 → **整个流程（井字棋/分拣）被终止**。
#   故限位本身必须**收窄到可表示范围**（收窄永远是安全方向：
#   它只会让更多目标被拒/被夹，绝不会凭空放行一个下发不出去的目标）。
#   J6 是转动轴，去掉整圈是**同一姿态**，物理上无副作用、且更不容易绕线。
ENCODE_LIMIT_DEG = 327.0

#: 限位条目：{"name": "J1", "min": -170.0, "max": 170.0}
Limit = Dict[str, Any]
#: 限位区间：(-170.0, 170.0)
Range = Tuple[float, float]
#: 调用方既可能传 dict 形式，也可能传 (lo, hi) 元组形式 —— 一律用 bounds_of() 取区间
LimitsLike = Union[Sequence[Limit], Sequence[Range]]


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def load_limits(n: int = JOINT_COUNT) -> List[Limit]:
    """读配置 → 归一化成 `[{"name","min","max"}, ...]`，长度恒为 n。

    ★ 不合法的单条配置**只让该条退到默认值**，不整份作废：
      限位是安全数据，整份作废（或直接抛异常）会让"某一个字段写错"
      变成"整个限位系统下线"，反而更危险。
    """
    # `or []`：yaml 里 `joint_limits:` 留空时 get() 返回 None（键存在但不取值），
    # 直接切片会 TypeError —— 这是原 jog.py 注释里踩过的坑。
    raw = get_config().get("joint_limits", default=[]) or []
    out: List[Limit] = []
    for i, entry in enumerate(list(raw)[:n]):
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip() or f"J{i + 1}"
            try:
                lo = float(entry["min"])
                hi = float(entry["max"])
            except (KeyError, TypeError, ValueError):
                lo, hi = DEFAULT_MIN, DEFAULT_MAX
        else:
            # 整条不是 dict（比如有人写成 `- -170, 170` 这种列表）→ 退回默认
            name, lo, hi = f"J{i + 1}", DEFAULT_MIN, DEFAULT_MAX
        # ★★ 收窄到控制器可表示范围（见 ENCODE_LIMIT_DEG 的事故说明）——
        #   现场 `J6: -360~360` 正是越界源头；收窄后 J6 = ±327，不会再算出下发不出去的目标。
        if lo < -ENCODE_LIMIT_DEG:
            lo = -ENCODE_LIMIT_DEG
        if hi > ENCODE_LIMIT_DEG:
            hi = ENCODE_LIMIT_DEG
        if hi < lo:                       # 收窄后塌陷（配置太离谱）→ 退到默认保守区间
            lo, hi = DEFAULT_MIN, DEFAULT_MAX
        out.append({"name": name, "min": lo, "max": hi})
    while len(out) < n:
        out.append({"name": f"J{len(out) + 1}", "min": DEFAULT_MIN, "max": DEFAULT_MAX})
    return out


def load_ranges(n: int = JOINT_COUNT) -> List[Range]:
    """同 load_limits，但只要 `[(lo, hi), ...]`（求解器 / 夹紧用，省掉 dict 查表）。"""
    return [(e["min"], e["max"]) for e in load_limits(n)]


def bounds_of(rng: Union[Limit, Range]) -> Range:
    """从 dict 或 (lo, hi) 元组里取区间 —— 4 处实现的入参形态不一样，统一在这一层吸收。"""
    if isinstance(rng, dict):
        return float(rng["min"]), float(rng["max"])
    return float(rng[0]), float(rng[1])


def _normalise(limits: Optional[LimitsLike]) -> List[Union[Limit, Range]]:
    """入参归一：没传就读配置；传了就原样用（调用方可能刚拿到 config 的 dict）。"""
    if limits is None:
        return load_limits()
    return list(limits)


def _as_floats(q: Any) -> Optional[List[float]]:
    """逐项转 float；有任何一项转不动（None / "abc" / 对象）→ None = 非法输入。"""
    try:
        return [float(v) for v in q]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 判定 / 夹紧 / 报告
# --------------------------------------------------------------------------- #
def in_limits(q: Any, limits: Optional[LimitsLike] = None, tol: float = TOL) -> bool:
    """`q` 是否在限位内。**长度不齐 / 含 NaN·Inf / 任一轴越界 → False。**

    顺序不能调换：
      1. 转不成 float → False（非法输入）；
      2. 长度与限位不等 → False —— 老实现用 zip，q 比限位短时**后面的轴根本没查**；
      3. 非有限值 → False —— NaN 比较为 False，最容易在这里被放行；
      4. 超区间（留 tol 吃浮点尾巴）→ False。
    """
    lim = _normalise(limits)
    vals = _as_floats(q)
    if vals is None:
        return False
    if len(vals) != len(lim):
        return False
    for v, rng in zip(vals, lim):
        if not math.isfinite(v):
            return False
        lo, hi = bounds_of(rng)
        if v < lo - tol or v > hi + tol:
            return False
    return True


def clamp(q: Any, limits: Optional[LimitsLike] = None) -> Tuple[List[float], bool]:
    """把越界轴夹到最近的限位边界。

    返回 `(新值列表, 是否真的发生过夹紧)`。

    ★ 与原实现（jog._step_joint 内联 if/elif）逐字等价：
      * 判据是**严格小于 / 严格大于**（不带 tol）—— 值正好落在边界上不算夹紧，
        否则 `limit_clamped` 会在"贴着限位停住"的正常场景里误报 True；
      * 非有限值保持原样、且**不**标记为已夹紧 —— 夹紧 = 凭空捏一个位置下发，
        比让它在 `in_limits` 那里被明确拒掉危险得多（见模块顶部注释）。
    """
    lim = _normalise(limits)
    try:
        items = list(q)
    except TypeError:
        # 连迭代都不是：没东西可夹，交由 in_limits 判 False（fail-safe）
        return [], False
    vals = _as_floats(items)
    if vals is None:
        # 有项转不成 float（None / 字符串）：一个字都不动地原样返回、不标记夹紧 ——
        # 夹紧等于凭空捏一个位置，比让它在 in_limits 那里被明确拒掉危险得多。
        return items, False
    out: List[float] = []
    clamped = False
    for i, v in enumerate(vals):
        if i >= len(lim) or not math.isfinite(v):
            # 非有限值保持原样且不标记夹紧：留给 in_limits 在下发前拒掉
            out.append(v)
            continue
        lo, hi = bounds_of(lim[i])
        if v < lo:
            out.append(lo)
            clamped = True
        elif v > hi:
            out.append(hi)
            clamped = True
        else:
            out.append(v)
    return out, clamped


def violations(q: Any, limits: Optional[LimitsLike] = None,
               tol: float = TOL) -> List[Dict[str, Any]]:
    """列出所有不合规的轴：`[{"joint","value","min","max","reason"}, ...]`。

    判据与 `in_limits()` **完全一致**（同一份代码），因此恒有
    `violations(q) == []` ⇔ `in_limits(q) is True` —— 这正是原来两处手写循环
    各查各的时做不到的一致性（老实现里 NaN 会被 `_in_limits` 拒、却在
    violations 里被漏报，于是 `solver.in_limits` 与 `violations` 互相打脸）。
    """
    lim = _normalise(limits)
    vals = _as_floats(q)
    if vals is None:
        return [{"joint": "?", "value": q, "min": None, "max": None,
                 "reason": "非法值(无法转成 float)"}]
    out: List[Dict[str, Any]] = []
    if len(vals) != len(lim):
        out.append({"joint": "?", "value": vals, "min": None, "max": None,
                    "reason": f"长度 {len(vals)} ≠ 限位数 {len(lim)}"})
        return out
    for i, (v, rng) in enumerate(zip(vals, lim)):
        lo, hi = bounds_of(rng)
        name = _name_of(rng, i)
        if not math.isfinite(v):
            out.append({"joint": name, "value": v, "min": lo, "max": hi,
                        "reason": "非法值(NaN/Inf)"})
        elif v < lo - tol or v > hi + tol:
            out.append({"joint": name, "value": round(v, 2), "min": lo, "max": hi,
                        "reason": "超限"})
    return out


def _name_of(rng: Union[Limit, Range], index: int) -> str:
    if isinstance(rng, dict):
        return str(rng.get("name") or "") or f"J{index + 1}"
    return f"J{index + 1}"
