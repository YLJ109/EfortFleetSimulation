# -*- coding: utf-8 -*-
"""
★ B1（B 批·可观测性）：进程内最小指标登记处 —— 只累数，不做事。

为什么单独一个模块、为什么不用 prometheus：
  现场需要回答「这一秒到底跑了多少、延迟多少、队列堆了多少」，而项目此前
  只有事件总线（事后追溯）没有实时读数。生产环境不给小功能加新依赖
  （与 camera_client「只用标准库」是同一条纪律），这里就是一把锁 + 一个字典。

三条铁律（对齐 §5.5「静默失效与假显示」）：
  1. **值 + 单位 + 时间戳 + 来源** 四元组：每一项都必须能指回「哪个函数记的」。
     没采到过样的指标不是 0，是 **null + note（没测）** —— 前端必须能区分它们
     （`snapshot()` 里 `measured=False` 的条目 value 恒为 None）。
  2. **永不抛异常**：指标是观测件，绝不能把主流程（Modbus 读、采集循环、
     WS 广播、事件落库）带崩。所有写入点都吞掉非法输入。
  3. **不编派生值**：没有历史样本池就不给 p50/p95（如实只给
     last/max/min/avg/count，并在 note 里写明）。

调用点（唯一埋点清单，新增埋点请同步这里的 CATALOG）：
  collector._loop_impl      collector.loop_period_ms / loop_ms / read_fail_streak
  ModbusRobot.read_pose     modbus.read_pose_rt_ms / read_pose_fail
  ModbusRobot.read_regs     modbus.read_regs_rt_ms / read_regs_fail
  hub.broadcast             ws.broadcast_enqueue_ms / broadcast_frames
  camera_client._request    vision.http_rt_ms / http_calls / http_fail / last_path / last_error
  events.emit               event.write_ms / writes / write_fail

测试隔离：`reset()` 清空全部样本（tests 里按用例前后各调一次，避免进程级串味，
做法与 conftest 的 ui_speed.reset 夹具同一条路）。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_lock = threading.Lock()
_entries: Dict[str, Dict[str, Any]] = {}

# 指标目录：key -> (kind, unit, source, desc)。
# kind: gauge=瞬时值 | stat=样本统计(last/max/min/avg/count) | counter=累计计数 | text=最近一次文本
CATALOG: Dict[str, tuple] = {
    "collector.loop_period_ms":  ("stat", "ms",    "collector._loop_impl",
                                  "相邻两拍采集循环的间隔（含阻塞与睡眠）"),
    "collector.loop_ms":         ("stat", "ms",    "collector._loop_impl",
                                  "单拍采集循环体耗时（不含睡眠）"),
    "collector.read_fail_streak": ("gauge", "次",  "collector._loop_impl",
                                  "Modbus 连续读取失败次数（成功即归 0）"),
    "modbus.read_pose_rt_ms":    ("stat", "ms",    "ModbusRobot.read_pose（IO 往返段）",
                                  "关节角单帧 FC3 读往返耗时"),
    "modbus.read_pose_fail":     ("counter", "次", "ModbusRobot.read_pose",
                                  "关节角读失败次数（连接/超时）"),
    "modbus.read_regs_rt_ms":    ("stat", "ms",    "ModbusRobot.read_regs（IO 往返段）",
                                  "任意寄存器区单次 FC3 读往返耗时"),
    "modbus.read_regs_fail":     ("counter", "次", "ModbusRobot.read_regs",
                                  "寄存器区连接/超时失败次数（协议错误帧不计）"),
    "ws.broadcast_enqueue_ms":   ("stat", "ms",    "hub.broadcast",
                                  "单帧序列化 + 入队耗时（★ 不含事件循环里的实际 send）"),
    "ws.broadcast_frames":       ("counter", "帧", "hub.broadcast",
                                  "广播调用次数（每次调用对每个订阅客户端覆盖一帧）"),
    "vision.http_rt_ms":         ("stat", "ms",    "camera_client._request",
                                  "视觉服务单次 HTTP 往返耗时（仅成功返回的调用）"),
    "vision.http_calls":         ("counter", "次", "camera_client._request",
                                  "视觉服务 HTTP 调用总数"),
    "vision.http_fail":          ("counter", "次", "camera_client._request",
                                  "视觉服务连接失败数（status=0：连不上/超时）"),
    "vision.last_path":          ("text", "-",     "camera_client._request",
                                  "最近一次调用的路径"),
    "vision.last_error":         ("text", "-",     "camera_client._request",
                                  "最近一次失败的原因（截断 200 字）"),
    "event.write_ms":            ("stat", "ms",    "events.emit（落库段）",
                                  "审计事件同步写库耗时"),
    "event.writes":              ("counter", "条", "events.emit",
                                  "审计事件成功落库总数"),
    "event.write_fail":          ("counter", "条", "events.emit",
                                  "审计事件落库失败总数（广播/日志不受影响）"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _slot(key: str) -> Optional[Dict[str, Any]]:
    meta = CATALOG.get(key)
    if meta is None:
        return None                       # 目录外的 key 一律拒收：埋点必须先进 CATALOG
    e = _entries.get(key)
    if e is None:
        kind, unit, source, desc = meta
        e = {"kind": kind, "unit": unit, "source": source, "desc": desc,
             "count": 0, "last": None, "max": None, "min": None,
             "sum": 0.0, "ts": None, "note": ""}
        _entries[key] = e
    return e


def _num(value: Any) -> Optional[float]:
    """任何非有限数值都当作「没测到」，绝不写进统计（指标不许造假）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def stat(key: str, value: Any, note: str = "") -> None:
    """样本统计：count/last/max/min/sum（avg = sum/count，派生自真实样本）。"""
    try:
        v = _num(value)
        if v is None:
            return
        with _lock:
            e = _slot(key)
            if e is None or e["kind"] != "stat":
                return
            e["count"] += 1
            e["last"] = v
            e["max"] = v if e["max"] is None else max(e["max"], v)
            e["min"] = v if e["min"] is None else min(e["min"], v)
            e["sum"] += v
            e["ts"] = _now_iso()
            if note:
                e["note"] = note
    except Exception:                     # noqa: BLE001 —— 观测件绝不带崩主流程
        pass


def gauge(key: str, value: Any, note: str = "") -> None:
    """瞬时值：最近一次「它现在是多少」。"""
    try:
        v = _num(value)
        if v is None:
            return
        with _lock:
            e = _slot(key)
            if e is None or e["kind"] != "gauge":
                return
            e["count"] += 1
            e["last"] = v
            e["ts"] = _now_iso()
            if note:
                e["note"] = note
    except Exception:                     # noqa: BLE001
        pass


def incr(key: str, by: int = 1) -> None:
    """累计计数。"""
    try:
        n = int(by)
        with _lock:
            e = _slot(key)
            if e is None or e["kind"] != "counter":
                return
            e["count"] += n
            e["last"] = float(e["count"])
            e["ts"] = _now_iso()
    except Exception:                     # noqa: BLE001
        pass


def text(key: str, value: Any) -> None:
    """最近一次文本（路径/错误），只保留 200 字。"""
    try:
        with _lock:
            e = _slot(key)
            if e is None or e["kind"] != "text":
                return
            e["last"] = str(value if value is not None else "")[:200]
            e["count"] += 1
            e["ts"] = _now_iso()
    except Exception:                     # noqa: BLE001
        pass


def _view(key: str, e: Dict[str, Any]) -> Dict[str, Any]:
    """四元组输出：值 + 单位 + 时间戳 + 来源。没采到过 → value=None + note（没测）。"""
    kind = e["kind"]
    measured = e["count"] > 0
    out: Dict[str, Any] = {
        "kind": kind,
        "value": (e["last"] if measured else None),
        "unit": e["unit"],
        "ts": e["ts"],
        "source": e["source"],
        "desc": e["desc"],
        "measured": measured,
    }
    if not measured:
        out["note"] = e["note"] or "没测：本进程启动以来没有采到过样本（不是 0）"
    elif e["note"]:
        out["note"] = e["note"]
    if kind == "stat" and measured:
        out["count"] = e["count"]
        out["max"] = round(e["max"], 3) if e["max"] is not None else None
        out["min"] = round(e["min"], 3) if e["min"] is not None else None
        out["avg"] = round(e["sum"] / e["count"], 3) if e["count"] else None
        # 没有历史样本池 → 不给 p50/p95（§5.5：不许编）
        out["percentiles"] = None
        out.setdefault("note", "")
        if not out["note"]:
            out["note"] = "无历史样本池：只有 last/max/min/avg/count，p50/p95 未采集"
        if kind == "stat" and isinstance(out["value"], float):
            out["value"] = round(out["value"], 3)
    if kind == "gauge" and measured and isinstance(out["value"], float):
        out["value"] = round(out["value"], 3)
    return out


def snapshot() -> Dict[str, Dict[str, Any]]:
    """全部目录项的当前读数（含从未采样的 —— 输出 null 并注明「没测」）。"""
    out: Dict[str, Dict[str, Any]] = {}
    with _lock:
        for key in CATALOG:
            e = _entries.get(key)
            if e is None:
                kind, unit, source, desc = CATALOG[key]
                e = {"kind": kind, "unit": unit, "source": source, "desc": desc,
                     "count": 0, "last": None, "max": None, "min": None,
                     "sum": 0.0, "ts": None, "note": ""}
            out[key] = _view(key, e)
    return out


def reset() -> None:
    """清空全部样本（仅供测试隔离用；生产不调）。"""
    with _lock:
        _entries.clear()
