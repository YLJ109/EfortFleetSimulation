# -*- coding: utf-8 -*-
"""
围栏实时状态守卫（阶段 4 安全加固 · 2026-10-12 升级为**双源**）。

背景：安全围栏的余量原本**只在前端 3D 场景**里逐帧算（three/safety.js），后端
看不见这个状态，导致"/api/control/move"的下发拦截只能靠前端自觉 —— 一旦换成脚本/第三方
客户端调用接口，围栏互锁就形同虚设。

两个来源，谁严重听谁（fail-safe）：
  ① **client**：前端每 ~1s 上报（POST /api/safety/live）—— 它量的是画面里的真实数模，
      最贴合，但**会停帧**（页面刷新/崩溃/浏览器最小化）。
  ② **server**：后端**自己**每 0.2s 用 `safety_eval.evaluate(实时关节)` 独立算一遍
      （`services/collector.py` 采集循环节流调用 `update_server`）—— 不依赖浏览器活着，
      脚本直连接口也躲不掉。
      ★ 是否**参与拦截**由 `config/robot.yaml: safety.server_guard` 决定：
        off(不算) / shadow(算+记录+可见，不拦) / enforce(参与互锁)。
        enforce 还要求包络已标定（`safety_eval.effective_mode()` 会拒绝未标定的 enforce
        并降级成 shadow，同时把降级原因如实回给界面）。

失败策略：client 侧维持原口径（无记录/过期 → 放行但告警，除非 motion.require_live_safety）；
过期时**保留最后状态的方向性**（最后一下是 danger/hit 就继续拒），不给"让前端掉线解除互锁"的捷径。
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from app.core.config import get_config

STALE_SEC = 5.0           # 超过这个时间没有上报，认为状态失效
SERVER_STALE_SEC = 2.0    # 服务端自己算的，节拍 0.2s ⇒ 2s 没更新就是采集/评估断了
UNSAFE = ("danger", "hit")

<<<<<<< HEAD
_latest: Optional[Dict[str, Any]] = None
_server: Optional[Dict[str, Any]] = None
_server_prev: Optional[Dict[str, Any]] = None     # 上一条服务端结论（判"变严重/恢复"用）
=======
# 单机链路的状态存在这个键下。★ 与 db.crud.SINGLE_ROBOT_ID 同一个口径（"MAIN"），
#   但这里不 import 过去：安全守卫不该依赖数据库层。两处若分家，
#   tests/test_safety_api.py 会红。
SINGLE_ID = "MAIN"

# ★ P6 按台化：机房 12 台各有各的围栏，**不能**共用一份全局状态。
#   原来只有一份 `_latest`，后果是"B 区某台撞了围栏 → A 区那台也被拦"，
#   而反过来（大屏只在机房模式下上报全场最严重的一档）会拦错机器。
#   字典键 = robot_id，单机用 SINGLE_ID。
_latest: Dict[str, Dict[str, Any]] = {}
>>>>>>> 5481859 (feat: add fleet simulation and project interface)
_lock = Lock()

def _freshness(ts: Optional[float], stale_sec: float) -> str:
    """三态（★铁律③：不许把"没数据"塌成"安全"）：never | fresh | stale"""
    if ts is None:
        return "never"
    return "fresh" if (time.time() - ts) <= stale_sec else "stale"


def _key(robot_id: Optional[str]) -> str:
    rid = str(robot_id or "").strip()
    return rid or SINGLE_ID


def update(state: str, zone_id: str = "", zone_name: str = "",
           clearance: float = 0.0, ratio: float = 1.0,
           robot_id: str = "") -> Dict[str, Any]:
    """上报当前围栏状态（前端每帧评估后节流调用）。按台存，见 _latest 注释。"""
    # ★ 审计修复 P3：clearance/ratio 可能是 NaN —— `float('nan') or 0.0` 是
    #   truthy，`or` 拦不住，NaN 会一路进快照再进 JSON（前端 JSON.parse 抛错）。
    try:
        c = float(clearance)
    except (TypeError, ValueError):
        c = 0.0
    if c != c or c in (float("inf"), float("-inf")):
        c = 0.0
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        r = 1.0
    if r != r or r in (float("inf"), float("-inf")):
        r = 1.0
    rec = {
        "robot_id": _key(robot_id),
        "state": str(state or "safe"),
        "zone_id": str(zone_id or ""),
        "zone_name": str(zone_name or ""),
        "clearance": c,
        "ratio": r,
        "ts": time.time(),
    }
    with _lock:
<<<<<<< HEAD
        prev = (_latest or {}).get("state")
        _latest = rec
    # ★ A5：前端那一路的 danger/hit 上升沿同样出取证包（浏览器停帧时后端那一路还会补上）
    if _rank(rec["state"]) > _rank(prev) and rec["state"] in UNSAFE:
        _capture("client", prev, rec)
    return snapshot()


def snapshot() -> Dict[str, Any]:
    """返回当前快照 + 是否新鲜（供 API/前端诊断互锁是否生效）。

    ★ 兼容口径：顶层扁平键**仍然只描述前端上报**（老代码/老测试读的就是这几个键）。
      双源的完整信息在 `client` / `server` / `merged` 三块里，界面要用这三块，
      别拿 `snap["state"]` 当"整机安全结论"——那只是浏览器那一路说的。
    """
    with _lock:
        rec, srv = _latest, _server
    mode, reason = _mode()

    client = {
        "reported": bool(rec),
        "freshness": _freshness(rec["ts"] if rec else None, STALE_SEC),
        "age_sec": round(time.time() - rec["ts"], 2) if rec else None,
        "state": (rec or {}).get("state", "unknown"),
        "zone_id": (rec or {}).get("zone_id", ""),
        "zone_name": (rec or {}).get("zone_name", ""),
        "clearance": (rec or {}).get("clearance", 0.0),
        "ratio": (rec or {}).get("ratio", 1.0),
=======
        _latest[rec["robot_id"]] = rec
        # 键由上报方给，必须封顶：认证客户端刷随机 id 就能把内存吃光
        if len(_latest) > 64:
            for k in sorted(_latest, key=lambda x: _latest[x]["ts"])[:len(_latest) - 64]:
                _latest.pop(k, None)
    return snapshot(robot_id)


def snapshot(robot_id: str = "") -> Dict[str, Any]:
    """返回某一台的快照 + 是否新鲜（供 API/前端诊断互锁是否生效）。

    robot_id 省略 = 单机链路（"MAIN"）。**不会**回落到"最近上报的那台"——
    把隔壁机器的围栏状态当成本机的判据，正是 P6 要根除的那类错。
    """
    key = _key(robot_id)
    with _lock:
        rec = _latest.get(key)
    if not rec:
        return {"reported": False, "fresh": False, "state": "unknown",
                "age_sec": None, "robot_id": key}
    age = time.time() - rec["ts"]
    return {
        "reported": True,
        "fresh": age <= STALE_SEC,
        "age_sec": round(age, 2),
        "robot_id": key,
        "state": rec["state"],
        "zone_id": rec["zone_id"],
        "zone_name": rec["zone_name"],
        "clearance": rec["clearance"],
        "ratio": rec["ratio"],
        "interlocked": _interlocked_from_config(),
>>>>>>> 5481859 (feat: add fleet simulation and project interface)
    }
    server = {
        "evaluated": bool(srv),
        "freshness": _freshness(srv["ts"] if srv else None, SERVER_STALE_SEC),
        "age_sec": round(time.time() - srv["ts"], 2) if srv else None,
        "state": (srv or {}).get("state", "unknown"),
        "zone_id": (srv or {}).get("zone_id", ""),
        "zone_name": (srv or {}).get("zone_name", ""),
        "clearance": (srv or {}).get("clearance", 0.0),
        "clearance_mm": (srv or {}).get("clearance_mm", 0.0),
        "ratio": (srv or {}).get("ratio", 1.0),
        "measured": (srv or {}).get("measured", False),
        "calibrated": (srv or {}).get("calibrated", False),
    }
    merged = _merge(client, server, mode)
    out = {
        "reported": bool(rec),
        "fresh": client["freshness"] == "fresh",
        "age_sec": client["age_sec"],
        "interlocked": _interlocked_from_config(),
        "client": client,
        "server": server,
        "merged": merged,
        "mode": mode,
        "mode_reason": reason,
    }
    if rec:
        out.update({k: rec[k] for k in ("state", "zone_id", "zone_name", "clearance", "ratio")})
    else:
        out.update({"state": "unknown", "zone_id": "", "zone_name": "",
                    "clearance": 0.0, "ratio": 1.0})
    return out


def _mode() -> Tuple[str, str]:
    """服务端评估的实际生效模式（含"请求 enforce 但没标定 → 降级 shadow"的原因）。"""
    try:
        from app.services.safety_eval import effective_mode
        return effective_mode()
    except Exception as e:                    # 评估模块坏了也不能让互锁读不到状态
        return "off", f"服务端评估模块不可用：{str(e)[:80]}"


def _merge(client: Dict[str, Any], server: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """两路结论取更严重（谁严重听谁）。

    ★ shadow 模式下服务端结果**也参与这里的合并显示** —— 影子的意义就是让现场先看见
      "如果按后端算的口径，此刻会被拦"；真正**参与拦截**的分流在 `check()` 里按 mode 走。
    """
    parts = []
    if client["reported"]:
        parts.append(("client", client))
    if server["evaluated"]:
        parts.append(("server", server))
    top: Optional[Tuple[str, Dict[str, Any]]] = None
    for name, rec in parts:
        if top is None or _worse(top[1], rec) is rec:
            top = (name, rec)
    src, rec = top if top else ("none", {})
    return {
        "state": rec.get("state", "unknown"),
        "zone_id": rec.get("zone_id", ""),
        "zone_name": rec.get("zone_name", ""),
        "clearance": rec.get("clearance", 0.0),
        "source": src,
        "blocking_server": mode == "enforce",
        "freshness": rec.get("freshness", "never"),
    }


def _worse(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    rank = {"safe": 0, "warn": 1, "danger": 2, "hit": 3}
    ra, rb = rank.get(a.get("state", "safe"), 0), rank.get(b.get("state", "safe"), 0)
    if rb > ra:
        return b
    if ra > rb:
        return a
    return b if float(b.get("clearance", 0.0)) < float(a.get("clearance", 0.0)) else a


def update_server(res: Dict[str, Any]) -> Dict[str, Any]:
    """记录**后端自己算出来**的围栏结论（`safety_eval.evaluate()` 的输出）。

    调用方 = 采集循环（`services/collector.py`，约每 0.2s 一次）。与前端上报的关系：
      · 两路各存各的时间戳 —— 前端停帧不影响后端读数，后端采集断了也不会假装前端还在报；
      · 是否参与拦截由 `safety.server_guard` 决定（见 `check()`），默认 shadow 只记录。
    """
    global _server, _server_prev
    state = str((res or {}).get("state") or "unknown")
    rec = {
        "state": state,
        "zone_id": str((res or {}).get("zone_id") or ""),
        "zone_name": str((res or {}).get("zone_name") or ""),
        "clearance": _finite((res or {}).get("clearance"), 0.0),
        "clearance_mm": _finite((res or {}).get("ground", {}).get("clearance_mm")
                               if isinstance((res or {}).get("ground"), dict) else 0.0, 0.0),
        "ratio": _finite((res or {}).get("ratio"), 1.0),
        "measured": bool((res or {}).get("measured")),
        "calibrated": bool((res or {}).get("calibrated")),
        "ts": time.time(),
    }
    with _lock:
        prev = (_server_prev or {}).get("state")
        _server, _server_prev = rec, rec
    # ★ 只在"变严重 / 恢复"时写事件（每拍都写会把库刷爆），且不能因为事件总线出错挡住采集
    if _rank(state) > _rank(prev) and state in UNSAFE:
        _emit(state, rec, "后端独立评估")
        _capture("server", prev, rec)
    elif prev in UNSAFE and state not in UNSAFE:
        _emit(state, rec, "后端独立评估")
    return snapshot()


_RANK = {"unknown": -1, "safe": 0, "warn": 1, "danger": 2, "hit": 3}


def _rank(s: Optional[str]) -> int:
    return _RANK.get(str(s or ""), -1)


def _finite(v: Any, d: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    return f if f == f and f not in (float("inf"), float("-inf")) else d


def _capture(source: str, from_state: Optional[str], rec: Dict[str, Any]) -> None:
    """danger/hit **上升沿** → 落一盘取证包（A5）。

    ★★ 只排队、不落盘：调用方是**采集线程**（server 那一路）或 HTTP 线程（client 那一路），
      写盘要读库、抓相机图，最坏几百毫秒 —— 在采集循环里做就是丢帧。
    ★ 取证出错**绝不影响互锁与采集**（整个函数吞掉一切异常）。
    """
    try:
        from app.services import safety_evidence
        state = str(rec.get("state") or "")
        safety_evidence.request_capture({
            "source": source,
            "from_state": from_state,
            "to_state": state,
            "zone_id": rec.get("zone_id", ""),
            "zone_name": rec.get("zone_name", ""),
            "clearance": rec.get("clearance", 0.0),
            "clearance_mm": rec.get("clearance_mm", 0.0),
            "ratio": rec.get("ratio", 1.0),
            "word": f"{from_state or '未评估'}→{state}（{source}）",
            # 同一次越界只出一个包（两路同时报 danger 时，第二个同键请求会被并掉）
            "dedupe_key": f"{state}:{rec.get('zone_id') or rec.get('zone_name') or ''}",
        })
    except Exception:
        pass


def _emit(state: str, rec: Dict[str, Any], src: str) -> None:
    try:
        from app.services.events import emit as emit_event
        level = {"danger": "warn", "hit": "critical", "clear": "info",
                 "safe": "info", "warn": "warn"}.get(state, "info")
        zone = rec.get("zone_name") or rec.get("zone_id") or "未命名区域"
        msg = {"danger": f"{src}：接近/危险 {zone}",
               "hit": f"{src}：碰撞报警 {zone}",
               "warn": f"{src}：接近 {zone}"}.get(state, f"{src}：已恢复安全（{zone}）")
        emit_event("safety", level, f"safety.server_{state}", msg, {
            "zone_id": rec.get("zone_id", ""), "zone_name": rec.get("zone_name", ""),
            "clearance": rec.get("clearance", 0.0), "source": "server",
        })
    except Exception:
        pass


def reset_server() -> None:
    """清空服务端评估记录（测试用；也供现场"复位互锁"时手动清）。"""
    global _server, _server_prev
    with _lock:
        _server, _server_prev = None, None


def reported_ids() -> list:
    """现在有几台在上报（诊断用：大屏开着应该是 12 + 单机 1）。"""
    with _lock:
        return sorted(_latest)


def reset() -> None:
    """清空全部上报记录（测试与"配置变更后重建采集"用）。"""
    with _lock:
        _latest.clear()


def _interlocked_from_config() -> bool:
    """是否强制要求"必须有实时围栏状态"才允许下发。"""
    try:
        return bool(get_config().get("motion", "require_live_safety", default=False))
    except Exception:
        return False


<<<<<<< HEAD
def check() -> Tuple[bool, str, Dict[str, Any]]:
    """下发前的安全校验。@return (是否允许, 原因, 快照)

    两路来源的分流（★ 前端那一路的既有口径**一个字节没改**，零回归）：
      · client：见下面的原有逻辑；
      · server：只有 `safety.server_guard=enforce`（且包络已标定，见 `safety_eval.effective_mode`）
        才参与拦截。理由：后端用的是**数模反解的胶囊包络**，比画面里的真实本体更保守，
        默认就拦会让现场看到"明明没碰到却被拒"——先把影子跑顺，再由人主动打开 enforce。
    """
    snap = snapshot()
    allow, reason = _check_client(snap)
    if not allow:
        return allow, reason, snap
    srv_ok, srv_reason = _check_server(snap)
    if not srv_ok:
        return False, srv_reason, snap
    return allow, reason, snap


def _check_server(snap: Dict[str, Any]) -> Tuple[bool, str]:
    srv = snap.get("server") or {}
    if snap.get("mode") != "enforce":
        return True, ""
    if not srv.get("evaluated") or srv.get("freshness") == "never":
        # enforce 的语义就是"后端必须自己算得出"；算不出还放行 = 空头互锁
        return False, ("围栏互锁（后端独立评估）已启用，但后端还没算出围栏状态"
                       "（采集未运行 / 拿不到实时姿态），拒绝下发")
    if srv.get("state") in UNSAFE and srv.get("freshness") == "fresh":
        zone = srv.get("zone_name") or srv.get("zone_id") or "未命名区域"
        return False, (f"后端独立评估：围栏状态为 {srv['state']}（{zone}，"
                       f"余量 {float(srv.get('clearance', 0.0)) * 1000:.0f}mm），已拦截下发")
    if srv.get("state") in UNSAFE and srv.get("freshness") == "stale":
        return False, (f"后端评估已停摆({srv.get('age_sec')}s 未更新)，且停摆前最后结论为 "
                       f"{srv['state']}，拒绝下发")
    return True, ""


def _check_client(snap: Dict[str, Any]) -> Tuple[bool, str]:
    if not snap["reported"]:
        if snap.get("interlocked"):
            return False, "围栏互锁已启用，但尚未收到前端实时状态，拒绝下发"
        return True, "未收到围栏实时状态（互锁未生效）"
=======
def check(robot_id: str = "") -> Tuple[bool, str, Dict[str, Any]]:
    """下发前的安全校验。**只看这一台**的围栏状态。@return (是否允许, 原因, 快照)"""
    snap = snapshot(robot_id)
    if not snap["reported"]:
        if snap.get("interlocked"):
            return False, "围栏互锁已启用，但尚未收到该台的实时状态，拒绝下发", snap
        return True, "未收到围栏实时状态（互锁未生效）", snap
>>>>>>> 5481859 (feat: add fleet simulation and project interface)

    if not snap["fresh"]:
        # ★ 审计修复 P2：过期时必须**保留最后状态的方向性**。
        #   原实现"过期就放行"——浏览器崩溃前最后一帧是 danger，5s 后照常放行，
        #   等于给了一条"只要让前端掉线就能解除互锁"的捷径。
        #   现在：最后一帧是 danger/hit 且已过期 → 一律拒绝（fail-safe）；
        #   恢复方式是前端恢复上报（安全态会覆盖掉），或操作员复位。
        if snap.get("state") in UNSAFE:
            return False, (f"围栏状态已过期({snap['age_sec']}s)，"
                           f"且过期前最后状态为 {snap['state']}，拒绝下发")
        if snap.get("interlocked"):
            return False, f"围栏状态已过期({snap['age_sec']}s)，拒绝下发"
        return True, f"围栏状态已过期({snap['age_sec']}s)，互锁未生效"

    if snap["state"] in UNSAFE:
        return False, f"围栏状态为 {snap['state']}，已拦截下发"

    return True, "围栏状态安全"
