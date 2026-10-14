# -*- coding: utf-8 -*-
"""E2 到位自证（段级残差带 + 时间窗判据）—— **只读观测，不参与任何控制**。

★ 为什么不是"轨迹跟踪精度"（方案第五轮自查把原稿当场降级，这里是终版口径）
  1) **没有期望轨迹的时间基准**：执行者是控制器里的常驻程序 210（AGENTS.md §4 机理 1），
     PC 只写"目标数据 + 触发位"，拿不到速度曲线/加减速规划 → 无法逐拍算"此刻应该在哪"。
     能算的只有**段级**量：`|q_actual − q_target|`（目标点已知、回读确认过）随时间的收敛过程。
  2) **采样上限 = 10Hz**（`config/robot.yaml: sampling.read_hz`）→ 一拍 100ms，
     本身就带 ±100ms 的时延不确定度 → 必须显式建模成"不确定带"，
     **不能把采集滞后说成"机器不准"**（AGENTS.md §5.5 铁律③）。
  ⇒ 因此这里只做四件事：① 下发后**动没动**（stuck）② **峰值残差 + 收敛耗时**（settle_ms）
     ③ 到位后**回弹不回**（overshoot）④ **超时无进展**（not_reached）。

★ 硬边界（改动前先读）
  · **绝不下发、绝不打断真机流程**：本模块不 import motion/modbus 的写路径，
    不产生任何"自动暂停/自动重试/急停"的动作 —— 超差只发一条 warn 事件 + 界面提示，
    **刹车必须人点**（§5.1「绝不在真机分拣运行期间做任何可能打断它的操作」）。
  · 残差是**关节空间**的度(°)，不是 mm；界面与文案一律写"到位自证"，不写"轨迹跟踪精度"。
  · 时间由**调用方传入**（`at`）→ 可确定性重放；一帧实测都没拿到 → `no_data`，**不猜"到位"**。
"""
from __future__ import annotations

import math
import time
from threading import Lock
from typing import Any, Dict, List, Optional

from app.core.config import get_config
from app.core.logger import get_logger

log = get_logger("twin_residual")

#: 观测口径（config/robot.yaml 的 twin_residual 段可覆盖；下面是现场默认）
DEFAULTS = {
    "enabled": True,
    "tol_deg": 1.0,               # 到位容差（°）—— 与 CAM_ARRIVE_TOL_DEG(3.0) 同量级、更严一档
    "start_timeout_ms": 1500.0,   # 下发后这么久还没"动起来" = stuck
    "settle_hold_ms": 300.0,      # 连续落在带内这么久才算"到位"
    "slow_ms": 8000.0,            # 到位但耗时超过它 → 如实标 slow（不当成"顺利到位"）
    "overshoot_factor": 2.0,      # 到位后又弹出 > 带×该系数 且不再回来 = overshoot
    "band_max_deg": 5.0,          # ★ 采样延迟带来的不确定度**封顶**（宁欠判到位，不多判）
    "max_ms": 30000.0,            # 单段最长观测窗口（对齐 rc_jog_execute 的 30s 上限）
    "profile_max": 240,           # 残差曲线最多留多少点（A5 取证包会带出去）
}

VERDICTS = ("pending", "moving", "arrived", "slow", "overshoot",
            "stuck", "not_reached", "no_data", "aborted")
#: 进入这些判据的段**定案**：后续帧只继续攒曲线，不再改结论（结论反复 = 现场没法信）
_TERMINAL = ("stuck", "not_reached", "overshoot")


def _cfg() -> Dict[str, Any]:
    raw = get_config().get("twin_residual", default=None) or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in out:
            if raw.get(k) is not None:
                out[k] = raw[k]
    return out


def ring_diff_deg(a: float, b: float) -> float:
    """两角度的**最短弧**差（J6 是转动轴：359° 与 −1° 是同一姿态，不是 360° 残差）。"""
    d = (float(a) - float(b)) % 360.0
    if d > 180.0:
        d -= 360.0
    return abs(d)


def residuals(target: List[float], actual: List[float]) -> List[float]:
    """逐轴残差（度）：J6 走环形最短弧，其余轴取绝对差。"""
    out = []
    for i in range(6):
        v = (ring_diff_deg(target[i], actual[i]) if i == 5
             else abs(float(target[i]) - float(actual[i])))
        out.append(round(v, 3))
    return out


def _finite6(q: Any) -> Optional[List[float]]:
    try:
        arr = [float(x) for x in q]
    except (TypeError, ValueError):
        return None
    if len(arr) != 6:
        return None
    return None if any(math.isnan(v) or math.isinf(v) for v in arr) else arr


def _sample_hz() -> float:
    try:
        return float(get_config().sampling.get("read_hz", 10.0) or 10.0)
    except Exception:
        return 10.0


def _sample_period() -> float:
    hz = _sample_hz()
    return 1.0 / hz if hz > 0 else 0.1


class _Segment:
    __slots__ = ("id", "target", "source", "speed_pct", "t0", "tol", "band", "peak",
                 "first_res", "last_res", "moved", "t_arrive", "hold_from", "t_last",
                 "rate_est", "overshoot", "verdict", "profile", "n", "closed",
                 "end_reason", "warned")

    def __init__(self, sid: int, target: List[float], source: str,
                 speed_pct: Optional[int], tol: float, at: float, band: float) -> None:
        self.id = sid
        self.target = target
        self.source = source or ""
        self.speed_pct = speed_pct
        self.t0 = at
        self.tol = tol
        self.band = band                   # 带 = 容差 + 一拍采样延迟的不确定度（逐帧更新）
        self.peak = 0.0
        self.first_res = None              # type: Optional[float]
        self.last_res = None               # type: Optional[float]
        self.moved = False
        self.t_arrive = None               # type: Optional[float]
        self.hold_from = None              # type: Optional[float]
        self.t_last = at
        self.rate_est = 0.0                # 观测到的收敛速率（°/s）→ 不确定带
        self.overshoot = False
        self.verdict = "pending"
        self.profile = []                  # type: List[List[float]]
        self.n = 0
        self.closed = False
        self.end_reason = ""
        self.warned = False


#: WS 位姿帧里捎带的残差字段（**不含 profile** —— 曲线由 REST 取，
#:   每 50ms 推 240 个点会把 WS 变成下载通道）
WS_KEYS = ("id", "source", "target", "speed_pct", "tol_deg", "band_deg",
           "uncertainty_deg", "peak_deg", "last_deg", "moved", "settled",
           "settle_ms", "elapsed_ms", "samples", "verdict")


class TwinResidual:
    """一个"在飞的期望段" + 最近若干段的历史（进程内，环形上限）。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._seg: Optional[_Segment] = None
        self._history: List[Dict[str, Any]] = []
        self._seq = 0
        self._rate_est = 0.0
        #: 测试隔离：pytest 里会话级采集线程每拍会喂仿真位姿 ——
        #:   纯函数测试请 monkeypatch `collector.twin_observe`（同 safety_update_server 先例）。
        self._events_enabled = True

    # ---------------- 写入侧（只有这两处，都不产生任何动作）----------------
    def begin(self, target: Any, source: str = "",
              speed_pct: Optional[int] = None, at: Optional[float] = None) -> Optional[int]:
        """登记"正在飞的那一段"。目标不可用 / 观测关闭 → 返回 None（静默，不影响下发）。"""
        q = _finite6(target)
        cfg = _cfg()
        if q is None or not cfg["enabled"]:
            return None
        now = time.time() if at is None else float(at)
        with self._lock:
            if self._seg is not None and not self._seg.closed:
                # 上一段没收尾就来了新目标：如实收尾（aborted），不留僵尸段
                self._close_locked("superseded", now)
            self._seq += 1
            tol = float(cfg["tol_deg"])
            self._seg = _Segment(self._seq, q, source, speed_pct, tol, now, tol)
            return self._seq

    def observe(self, joints: Any, at: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """采集器每拍喂一次实时关节角。**只更新观测，绝不返回"该做什么"**。"""
        q = _finite6(joints)
        if q is None:
            return None
        cfg = _cfg()
        now = time.time() if at is None else float(at)
        period = _sample_period()
        with self._lock:
            seg = self._seg
            if seg is None or seg.closed:
                return None
            dt = max(0.0, now - seg.t_last)
            seg.t_last = now
            res = residuals(seg.target, q)
            worst = max(res)
            prev = seg.last_res
            seg.last_res = worst
            seg.n += 1
            if seg.first_res is None:
                seg.first_res = worst
                seg.t0 = now
            seg.peak = max(seg.peak, worst)
            if len(seg.profile) < int(cfg["profile_max"]):
                seg.profile.append([round(now - seg.t0, 3), worst])
            # 采样延迟的不确定度：这一拍最多还能走 rate×period → **显式并进"带"**，
            # 免得把"读得慢"说成"机器不准"（铁律③）
            if prev is not None and dt > 0 and prev > worst:
                seg.rate_est = max(seg.rate_est, (prev - worst) / dt)
            # ★ 带宽 = 容差 + **本段自己**观测到的收敛速率 × 一拍，并且**封顶**。
            #   不用跨段的单调 max：那会让某一次快下发把带永久撑宽 → 大位移也能"秒判到位"
            #   = 假自证。封顶后宁可少判到位（走 not_reached/stuck 如实呈现），不多判。
            seg.band = round(seg.tol + min(seg.rate_est * period,
                                           float(cfg["band_max_deg"])), 3)
            band = seg.band

            if seg.verdict in _TERMINAL:
                return self._snap_seg(seg)          # 已定案，只继续攒曲线

            # ① 动没动：残差相对首帧有明显下降（> 0.2° 且 > 带的 1/3）才算"动了"
            if not seg.moved and (seg.first_res - worst) > max(0.2, band / 3.0):
                seg.moved = True
            # ② 到位：连续 settle_hold_ms 落在带内
            if worst <= band:
                if seg.hold_from is None:
                    seg.hold_from = now
                if seg.t_arrive is None and (now - seg.hold_from) * 1000.0 >= float(cfg["settle_hold_ms"]):
                    seg.t_arrive = now
                    elapsed_ms = (now - seg.t0) * 1000.0
                    seg.verdict = "slow" if elapsed_ms > float(cfg["slow_ms"]) else "arrived"
            else:
                seg.hold_from = None
                # ③ 过冲不回：已经到了、又弹出去且不再回来
                if seg.t_arrive is not None and worst > band * float(cfg["overshoot_factor"]):
                    if not seg.overshoot:
                        seg.overshoot = True
                        seg.verdict = "overshoot"
                        self._warn(seg, "到位后回弹到 %.2f°（带 %.2f°）且未再收敛" % (worst, band))
            # ④ 卡住 / 超时无进展（首帧就在带内 = 零位移，不算 stuck）
            elapsed_ms = (now - seg.t0) * 1000.0
            if (not seg.moved and seg.first_res is not None and seg.first_res > band
                    and elapsed_ms >= float(cfg["start_timeout_ms"])):
                seg.verdict = "stuck"
                self._warn(seg, "下发后 %.1fs 内没有移动（残差仍是 %.2f°）"
                           % (elapsed_ms / 1000.0, worst))
                return self._snap_seg(seg)
            if seg.t_arrive is None and elapsed_ms >= float(cfg["max_ms"]):
                seg.verdict = "not_reached"
                self._warn(seg, "观测窗口 %.0fs 用完仍未收敛到带内（当前 %.2f°）"
                           % (elapsed_ms / 1000.0, worst))
                return self._snap_seg(seg)
            if seg.verdict == "pending":
                seg.verdict = "moving"
            return self._snap_seg(seg)

    def end(self, reason: str = "done", at: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """段结束（下发返回 / 流程收尾）。结果压进历史，**不产生任何动作**。"""
        now = time.time() if at is None else float(at)
        with self._lock:
            return self._close_locked(reason, now)

    # ---------------- 只读侧 ----------------
    def current(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._snap_seg(self._seg) if self._seg else None

    def ws_view(self) -> Optional[Dict[str, Any]]:
        """给 WS 位姿帧的**紧凑**快照（只在有在飞的段时非空；不含曲线）。"""
        seg = self.current()
        return None if seg is None else {k: seg[k] for k in WS_KEYS if k in seg}

    def recent(self, n: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._history[-max(1, int(n)):]))

    def state(self) -> Dict[str, Any]:
        """给 REST / WS / A5 取证包的快照（同屏同源：判据只有这一份）。"""
        cfg = _cfg()
        return {
            "enabled": bool(cfg["enabled"]),
            "tol_deg": cfg["tol_deg"],
            "sample_hz": _sample_hz(),
            "current": self.current(),
            "recent": self.recent(10),
            "verdicts": list(VERDICTS),
            "note": ("段级到位自证：只观测、不参与控制；超差只发一条 warn 事件并在界面提示，"
                     "**不会自动暂停、不会自动重试、不会急停**（刹车必须人点）。"
                     "残差是关节角差（°）不是 mm；带 = 到位容差 + 一拍采样延迟的不确定度。"),
        }

    def reset(self) -> None:
        """测试夹具用：清掉进程内状态，避免用例之间串味。"""
        with self._lock:
            self._seg = None
            self._history = []
            self._seq = 0
            self._rate_est = 0.0

    # ---------------- 内部 ----------------
    def _close_locked(self, reason: str, now: float) -> Optional[Dict[str, Any]]:
        seg = self._seg
        if seg is None:
            return None
        seg.closed = True
        seg.end_reason = reason
        if seg.verdict in ("pending", "moving"):
            if seg.n == 0:
                seg.verdict = "no_data"        # 一帧实测都没拿到 → 如实说"不知道"
            elif seg.t_arrive is not None:
                seg.verdict = "arrived"
            else:
                seg.verdict = "not_reached"
        if reason in ("superseded", "aborted", "estop") and seg.t_arrive is None:
            seg.verdict = "aborted"            # 被人/新指令打断，不是"机器没到位"
        row = self._snap_seg(seg)
        # ★ 点动逐拍下发（每 50~100ms 一段）会把上一段"顶掉"（superseded）——
        #   一帧都没采到的微段进历史只会把真正有意义的分拣段挤出去（环形只有 40 条），
        #   所以**直接丢弃**（不进历史、不发事件）；界面/事件不会为它编故事。
        if reason == "superseded" and seg.n < 2:
            self._seg = None
            return None
        self._history.append(row)
        if len(self._history) > 40:
            self._history = self._history[-40:]
        self._seg = None
        return row

    @staticmethod
    def _snap_seg(seg: _Segment) -> Dict[str, Any]:
        return {
            "id": seg.id,
            "source": seg.source,
            "target": seg.target,
            "speed_pct": seg.speed_pct,
            "tol_deg": round(seg.tol, 3),
            "band_deg": seg.band,
            "uncertainty_deg": round(max(0.0, seg.band - seg.tol), 3),
            "peak_deg": round(seg.peak, 3),
            "first_deg": seg.first_res,
            "last_deg": seg.last_res,
            "moved": seg.moved,
            "settled": seg.t_arrive is not None,
            "settle_ms": None if seg.t_arrive is None else round((seg.t_arrive - seg.t0) * 1000.0, 1),
            "elapsed_ms": round((seg.t_last - seg.t0) * 1000.0, 1),
            "samples": seg.n,
            "verdict": seg.verdict,
            "closed": seg.closed,
            "end_reason": seg.end_reason,
            "profile": [list(v) for v in seg.profile],
        }

    def _warn(self, seg: _Segment, why: str) -> None:
        """超差 → 一条 warn 事件（**每段最多一条**，绝不据此产生任何动作）。"""
        if seg.warned or not self._events_enabled:
            return
        seg.warned = True
        try:
            from app.services.events import emit as emit_event
            emit_event("safety", "warn", "twin_residual_over",
                       "到位自证：第 %d 段「%s」%s" % (seg.id, seg.source or "未标注来源", why),
                       detail={"segment": self._snap_seg(seg)})
        except Exception as e:                 # 观测件绝不把主流程带崩
            log.debug("到位自证事件发射失败（已忽略）: %s", e)


twin_residual = TwinResidual()
