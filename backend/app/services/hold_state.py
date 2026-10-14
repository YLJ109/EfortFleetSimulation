# -*- coding: utf-8 -*-
"""E5′「固定相机反用法」唯一实现：把判色那一帧**第二次**用起来 = 持件/吸空的观测证据。

为什么这条能成立（现场约束反推出来的，不是抄来的）：
现场只有**一只固定朝上、只对 C 检测位**的相机 —— 它看不见料区，所以做不了抓取引导
（原方案 E5 因此作废）；但每次送检判色本来就要在 C 位连抓 N 帧，
而 §7 明文**现场没有真空传感器**：今天"手上有没有件"完全是流程推断。
于是那几帧白捡到一个此前拿不到的量：**镜头前到底有没有东西**。

★★ 三条硬边界（方案 §10.2 收缩后的最终口径，改代码前先读）：
  1. **单向收紧**：证据只用来「支持」或「报矛盾」。证据缺失/弱 → 退回 `inferred`（仅推断），
     **绝不**因为"帧里没看到"就放行任何新动作，也**绝不**自动暂停 / 重试 / 急停
     —— 刹车必须人点（AGENTS.md §5.1）。
  2. **不新增拍摄、不新增移动**：只消费 `_capture` 本来就已经抓到的那几帧（纯后处理）。
  3. **未知 ≠ 确认**（§5.5 铁律③）：相机没开 / HTTP 失败 / 结构异常 → `inferred`，
     永远不塌成 `confirmed`。

★ 为什么"有候选"不等于"确认吸住"：现场**红色吸盘**自己就可能被检成一个候选
  （见 `api/sorting.py` 的 `_HINT_NO_PIECE` 注释）。所以 `confirmed` 的判据是
  「**判色结论成立** + **多帧面积一致**」两件事同时成立，界面文案也写成
  「有视觉证据**支持**」而不是"真空传感器读数"。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from app.core.config import get_config
from app.core.logger import get_logger

log = get_logger("hold_state")

# ---- 三档（界面/事件/审计只用这三个词，别在外面另造一套说法）----
CONFIRMED = "confirmed"              # 有视觉证据支持"流程所说的持件状态"
INFERRED = "inferred"                # 没有可用证据 —— 仅按流程推断（今天的默认口径）
CONTRADICTION = "contradiction"      # 证据与流程不符（吸空 / 放件后镜头前仍有件）
STATES = (CONFIRMED, INFERRED, CONTRADICTION)

STATE_LABEL = {
    CONFIRMED: "有证据支持",
    INFERRED: "仅流程推断",
    CONTRADICTION: "证据与流程矛盾",
}

DEFAULTS = {"enabled": True, "history_max": 40, "area_ratio_max": 2.0}

NOTE = ("持件/吸空证据：只复用送检那几帧做后处理，不新增拍摄、不新增移动；"
        "矛盾只发一条 warn 事件并在界面提示，**不会自动暂停、不会重试、不会急停**"
        "（现场没有真空传感器，本卡给的是\"有证据支持 / 仅推断 / 矛盾\"三档，不是传感器读数）。")


def _cfg() -> Dict[str, Any]:
    raw = get_config().get("hold_state", default=None) or {}
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw and raw[k] is not None:
                cfg[k] = raw[k]
    try:
        cfg["area_ratio_max"] = max(1.0, float(cfg["area_ratio_max"]))
    except (TypeError, ValueError):
        cfg["area_ratio_max"] = DEFAULTS["area_ratio_max"]
    try:
        cfg["history_max"] = max(4, int(cfg["history_max"]))
    except (TypeError, ValueError):
        cfg["history_max"] = DEFAULTS["history_max"]
    cfg["enabled"] = bool(cfg["enabled"])
    return cfg


def classify_frame(f: Any) -> Dict[str, Any]:
    """**一帧** `/vision/detect` 原始返回 → 镜头前"有没有东西"。

    返回 {"kind": present|absent|unknown, "area": float, "name": str, "conf": float,
          "reason": str}

    ★ 与"判色"是**两件不同的事**：判色问"这块是什么颜色"，这里只问"镜头前有没有一块东西"。
      所以无效色（橙/黄/青/紫/黑）也算 present —— 它照样证明吸盘前有个件。
    ★ `absent` 只在**相机明确回了空结果**（results == []）时给出；
      传输失败 / 结构异常一律 `unknown`（不许把"没读到"说成"没有"）。
    """
    if not isinstance(f, dict) or not f:
        return {"kind": "unknown", "area": 0.0, "name": "", "conf": 0.0,
                "reason": "无返回（相机未开启/视觉服务未响应）"}
    if f.get("ok") is False:
        return {"kind": "unknown", "area": 0.0, "name": "", "conf": 0.0,
                "reason": str(f.get("error") or "视觉服务返回失败")}

    results = f.get("results")
    if isinstance(results, list):
        if not results:
            return {"kind": "absent", "area": 0.0, "name": "", "conf": 0.0,
                    "reason": "画面里没有候选"}
        best, best_area = None, -1.0
        for r in results:
            if not isinstance(r, dict):
                continue
            try:
                a = float(r.get("area") or 0.0)
            except (TypeError, ValueError):
                a = 0.0
            if a > best_area:
                best, best_area = r, a
        if best is None:
            return {"kind": "unknown", "area": 0.0, "name": "", "conf": 0.0,
                    "reason": "候选结构异常"}
        c = best.get("color") or {}
        try:
            conf = float(c.get("conf") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if best_area <= 0.0:
            return {"kind": "unknown", "area": 0.0, "name": str(c.get("name") or ""),
                    "conf": conf, "reason": "候选无面积（不可用于持件判定）"}
        return {"kind": "present", "area": round(best_area, 1),
                "name": str(c.get("name") or ""), "conf": conf, "reason": ""}

    # 扁平结构（旧调用方/单测直接喂 {"name","area"}）—— 有名字或有面积就算有候选
    name = str(f.get("name") or "").strip()
    try:
        area = float(f.get("area") or 0.0)
    except (TypeError, ValueError):
        area = 0.0
    if name or area > 0:
        return {"kind": "present", "area": round(area, 1), "name": name,
                "conf": float(f.get("conf") or 0.0) if f.get("conf") else 0.0,
                "reason": ""}
    return {"kind": "unknown", "area": 0.0, "name": "", "conf": 0.0,
            "reason": "无候选字段（既不是真实形状也不是扁平结构）"}


def presence_from_frames(frames: Optional[List[Any]]) -> Dict[str, Any]:
    """N 帧 → 一个**保守**的"镜头前有没有东西"结论。

    规则（单向收紧：能升上去的证据才升，其余一律留在 unknown）：
      · 任一帧 present            → present（有件；至于是不是吸盘，交给 `judge_supported` 判）
      · 全部帧 absent             → absent（相机正常、镜头前确实什么都没有）
      · 只有 absent + unknown     → unknown（有帧没读到，不能说"确定是空"）
      · 没有任何帧                → unknown
    """
    kinds: List[str] = []
    areas: List[float] = []
    names: List[str] = []
    why: List[str] = []
    for f in (frames or []):
        one = classify_frame(f)
        kinds.append(one["kind"])
        if one["kind"] == "present":
            areas.append(float(one["area"]))
            if one["name"]:
                names.append(one["name"])
        elif one["reason"] and len(why) < 3:
            why.append(one["reason"])

    n = len(kinds)
    present = kinds.count("present")
    absent = kinds.count("absent")
    unknown = n - present - absent
    if present:
        val: Optional[bool] = True
    elif n and absent == n:
        val = False
    else:
        val = None

    # 面积一致性：件在 C 位停稳后连续几帧面积应当接近；差得多 = 抖动/只拍到局部
    ratio = 1.0
    if len(areas) >= 2:
        lo, hi = min(areas), max(areas)
        ratio = round(hi / lo, 3) if lo > 0 else float("inf")

    return {"present": val, "frames": n, "present_frames": present,
            "absent_frames": absent, "unknown_frames": unknown,
            "areas": areas, "area_ratio": ratio,
            "names": list(dict.fromkeys(names))[:4],
            "reason": "" if present else ("" if val is False else
                                          ("；".join(why) or "没有可用帧"))}


def judge_supported(presence: Dict[str, Any], vote_ok: bool,
                    area_ratio_max: Optional[float] = None) -> bool:
    """「有视觉证据支持持件」的判据（唯一实现）：判色结论成立 **且** 多帧面积一致。

    ★ 为什么不只看"帧里有候选"：现场**红色吸盘**自己可能成一个候选
      —— 单靠"有候选"就把"仅推断"升成"有证据支持"，就是假显示。
    ★ 所以证据不足时**退回仅推断**（宁可少说，不多说）。
    """
    if not presence or presence.get("present") is not True:
        return False
    cap = float(area_ratio_max if area_ratio_max is not None
                else _cfg()["area_ratio_max"])
    if float(presence.get("area_ratio") or 1.0) > cap:
        return False
    return bool(vote_ok)


def hold_state_of(expect_present: bool, presence: Dict[str, Any],
                  vote_ok: bool) -> Dict[str, Any]:
    """三档判定的唯一入口（纯函数，不发消息、不动机器人）。

    @param expect_present 流程认为"镜头前应该有个件"吗 ——
        True = 送检/自证（流程说手上吸着件）；False = 放件后自检（流程说手上应该空）。
        ★ 今天所有采集点都在**手上应有件**时发生（放件后不去 C 位）→ 实际只会传 True；
          False 分支保留是为了"证据与流程不符"这条判据本身完整（有单测覆盖，
          等现场真有"放件后复拍"动作时直接接，不再改判据）。
    """
    supported = judge_supported(presence, vote_ok)
    val = (presence or {}).get("present")
    if val is None:
        return {"state": INFERRED,
                "note": (presence or {}).get("reason") or
                        "没有可用帧 —— 仅按流程推断",
                "supported": False}
    if val is True:
        if expect_present:
            if supported:
                return {"state": CONFIRMED,
                        "note": "判色结论成立且多帧面积一致 —— 有视觉证据支持\"手上有件\"",
                        "supported": True}
            return {"state": INFERRED,
                    "note": "帧里有候选，但不足以证实（判色未成立或面积抖动大；"
                            "红色吸盘自身也可能成一个候选）—— 仍按流程推断",
                    "supported": False}
        return {"state": CONTRADICTION,
                "note": "流程认为手上已空，但镜头前仍有件 —— 疑似放料失败/件还吸着",
                "supported": False}
    # val is False：相机明确回了"画面里没有候选"
    if expect_present:
        return {"state": CONTRADICTION,
                "note": "流程认为手上有件，但相机明确什么都没见过 —— 疑似吸空/掉件",
                "supported": False}
    return {"state": CONFIRMED,
            "note": "相机明确没有候选 —— 有视觉证据支持\"手上已空\"",
            "supported": True}


class _Record:
    __slots__ = ("id", "at", "source", "expect_present", "state", "note",
                 "supported", "presence", "vote_ok", "color", "conf")

    def __init__(self, rid: int, at: float, source: str, expect_present: bool,
                 verdict: Dict[str, Any], presence: Dict[str, Any],
                 vote_ok: bool, color: str = "", conf: float = 0.0) -> None:
        self.id = rid
        self.at = at
        self.source = source
        self.expect_present = expect_present
        self.state = verdict["state"]
        self.note = verdict["note"]
        self.supported = verdict["supported"]
        self.presence = presence
        self.vote_ok = vote_ok
        self.color = color
        self.conf = conf

    def snap(self) -> Dict[str, Any]:
        p = self.presence
        return {
            "id": self.id,
            "at": round(self.at, 3),
            "source": self.source,
            "expect_present": self.expect_present,
            "state": self.state,
            "state_label": STATE_LABEL.get(self.state, self.state),
            "note": self.note,
            "supported": self.supported,
            "vote_ok": self.vote_ok,
            "color": self.color,
            "conf": round(float(self.conf or 0.0), 3),
            "frames": p["frames"],
            "present": p["present"],
            "present_frames": p["present_frames"],
            "absent_frames": p["absent_frames"],
            "unknown_frames": p["unknown_frames"],
            "area_ratio": p["area_ratio"],
            "area_px": p["areas"][0] if p["areas"] else 0.0,
            "reason": p["reason"],
        }


class HoldState:
    """进程内**只读观测**登记处：最新一条 + 环形历史 + 矛盾计数。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hist: Deque[_Record] = deque(maxlen=int(DEFAULTS["history_max"]))
        self._seq = 0
        self._contradictions = 0
        self._events_enabled = True

    # ---------------- 写侧（只有 record，没有任何动作）----------------
    def record(self, source: str, frames: Optional[List[Any]], vote: Optional[Dict] = None,
               expect_present: bool = True, at: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """送检那一轮判色之后登记一条证据。**返回快照，不产生任何动作。**"""
        cfg = _cfg()
        if not cfg["enabled"]:
            return None
        pres = presence_from_frames(frames)
        v = vote if isinstance(vote, dict) else {}
        ok = bool(v.get("ok")) and not v.get("placeholder")
        verdict = hold_state_of(expect_present, pres, ok)
        now = time.time() if at is None else float(at)
        with self._lock:
            self._seq += 1
            rec = _Record(self._seq, now, str(source or "送检"), bool(expect_present),
                          verdict, pres, ok,
                          str(v.get("color") or ""), float(v.get("conf") or 0.0))
            if self._hist.maxlen != cfg["history_max"]:      # 配置改了 → 环形跟着缩
                self._hist = deque(self._hist, maxlen=cfg["history_max"])
            self._hist.append(rec)
            snap = rec.snap()
            if verdict["state"] == CONTRADICTION:
                self._contradictions += 1
        if verdict["state"] == CONTRADICTION:
            self._emit(snap)
        else:
            log.debug("持件证据 %s：%s（%s）", snap["state"], snap["note"], snap["source"])
        return snap

    # ---------------- 只读侧 ----------------
    def current(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._hist[-1].snap() if self._hist else None

    def recent(self, n: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.snap() for r in list(self._hist)[-max(1, int(n)):]][::-1]

    def state(self) -> Dict[str, Any]:
        cfg = _cfg()
        with self._lock:
            contradicts = self._contradictions
            total = len(self._hist)
        return {
            "enabled": cfg["enabled"],
            "area_ratio_max": cfg["area_ratio_max"],
            "states": list(STATES),
            "state_label": dict(STATE_LABEL),
            "current": self.current(),
            "recent": self.recent(10),
            "contradictions": contradicts,
            "observations": total,
            "note": NOTE,
        }

    def reset(self) -> None:
        """测试夹具用：清掉进程内状态，避免用例之间串味。"""
        with self._lock:
            self._hist.clear()
            self._seq = 0
            self._contradictions = 0

    # ---------------- 内部 ----------------
    def _emit(self, snap: Dict[str, Any]) -> None:
        """矛盾 → **一条** warn 事件（不自动动作；重复矛盾各记一条）。"""
        if not self._events_enabled:
            return
        try:
            from app.services.events import emit as emit_event
            emit_event("safety", "warn", "hold_state_contradiction",
                       "持件证据与流程矛盾（%s）：%s" % (snap["source"], snap["note"]),
                       detail={"state": snap["state"], "frames": snap["frames"],
                               "absent_frames": snap["absent_frames"],
                               "present": snap["present"],
                               "expect_present": snap["expect_present"],
                               "id": snap["id"]})
        except Exception as e:                   # noqa: BLE001 —— 观测件绝不带崩主控
            log.debug("持件证据事件发射失败（已忽略）: %s", e)


hold_state = HoldState()
