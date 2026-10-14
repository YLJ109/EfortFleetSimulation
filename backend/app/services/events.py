# -*- coding: utf-8 -*-
"""
统一事件总线 / 审计日志服务（阶段 3 时间线 + 阶段 4 审计）。

设计要点：
  - 一处入口 emit()：落库 + 实时广播给前端 + 同步写 Python 日志。
  - ★ 永不抛异常：事件系统是用来"观察和追溯"的，绝不能因为它把主流程（运动控制、
    数据采集、鉴权）带崩。任何环节失败都降级为一条 warning 日志。
  - ★ 线程安全：采集线程 (Collector) 会并发调用 emit()，这里每次用独立 Session，
    广播走 hub（内部 call_soon_threadsafe），不做共享状态写。

category: system | connection | auth | control | config | safety | vision
level:    debug | info | warn | error | critical
actor:    system | admin | operator | anonymous | vision
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core.logger import get_logger
from app.db.crud import insert_system_event
from app.db.database import SessionLocal
from app.services import metrics
from app.services.hub import hub

log = get_logger("events")

CATEGORIES = ("system", "connection", "auth", "control", "config", "safety", "vision")
LEVELS = ("debug", "info", "warn", "error", "critical")

_LEVEL_RANK = {lv: i for i, lv in enumerate(LEVELS)}


def _iso(dt: Optional[datetime]) -> str:
    try:
        return dt.isoformat() + "Z" if dt and dt.tzinfo is None else (dt.isoformat() if dt else "")
    except Exception:
        return ""


def emit(category: str, level: str, action: str, message: str = "",
         detail: Any = None, actor: str = "system", ip: str = "",
         broadcast: bool = True) -> Dict[str, Any]:
    """记录一条事件（审计/时间线）。

    @param detail 任意可 JSON 化的扩展信息（字典优先）
    @return 事件载荷 dict（失败时返回空 dict，绝不抛异常）
    """
    cat = str(category or "system").lower()
    lv = str(level or "info").lower()
    if lv not in _LEVEL_RANK:
        lv = "info"

    try:
        detail_txt = ""
        if detail is not None:
            detail_txt = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    except Exception:
        detail_txt = ""

    payload: Dict[str, Any] = {}
    # ★ B1 只读指标：emit 是**同步落库**（没有内存队列 → 不存在「积压」这一说，
    #   写入耗时就是背压的真实体现）。计时只包落库段，不含广播/日志。
    t0 = time.perf_counter()
    try:
        s = SessionLocal()
        try:
            row = insert_system_event(
                s, category=cat, level=lv, action=str(action or ""),
                message=str(message or ""), detail=detail_txt,
                actor=str(actor or "system"), ip=str(ip or ""),
            )
            payload = {
                "type": "event",
                "id": row.id,
                "timestamp": _iso(row.timestamp),
                "category": row.category,
                "level": row.level,
                "action": row.action,
                "actor": row.actor,
                "ip": row.ip,
                "message": row.message,
                "detail": row.detail,
            }
            metrics.incr("event.writes")
        finally:
            s.close()
    except Exception as e:   # 落库失败也不能影响调用方
        metrics.incr("event.write_fail")
        log.warning("事件落库失败(%s/%s): %s", cat, action, e)
        payload = {
            "type": "event",
            "id": 0,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "category": cat, "level": lv, "action": str(action or ""),
            "actor": str(actor or "system"), "ip": str(ip or ""),
            "message": str(message or ""), "detail": detail_txt,
        }
    metrics.stat("event.write_ms", (time.perf_counter() - t0) * 1000.0)

    # 实时广播（前端时间线可即时刷新）
    if broadcast:
        try:
            hub.broadcast(payload)
        except Exception as e:
            # warn 及以上事件的广播失败意味着前端时间线看不到该条（如急停），
            # 静默 debug 会让"事件缺条"无从排查 —— 升级为可见日志。
            if _LEVEL_RANK.get(lv, 1) >= 2:
                log.warning("事件广播失败(%s/%s): %s", cat, action, e)
            else:
                log.debug("事件广播失败: %s", e)

    # 同步写 Python 日志，方便不看页面时排查
    try:
        line = f"[{cat}/{lv}] {action}: {message}"
        if detail_txt:
            line += f" | {detail_txt[:300]}"
        if _LEVEL_RANK.get(lv, 1) >= 3:      # error / critical
            log.error(line)
        elif lv == "warn":
            log.warning(line)
        else:
            log.info(line)
    except Exception:
        pass

    # ★★ C2 报警合理化：把这条事件交给**报警层**观察（分级 / 折叠 / 延时 / 搁置的原料）。
    #   · 放在最后、整段包在 try 里：报警层只是**显示与处置层**，它出错绝不能影响
    #     事件落库、广播，更不能把调用 emit 的主流程（运动控制 / 采集 / 鉴权）带崩；
    #   · 只读 payload，不改任何字段 —— 各报警源怎么判定（围栏 danger/hit、就绪闸门、
    #     5005 清报警顺序）一个字节都没动，见 services/alarms.py 模块注释；
    #   · 报警层自己的审计事件（action 以 alarms. 开头）会被它自己忽略，不会递归。
    try:
        from app.services import alarms
        alarms.ingest(payload)
    except Exception as e:                   # noqa: BLE001 —— 观测层不许带崩主控
        log.debug("报警层摄入失败(已忽略): %s", e)

    return payload


def since_dt(hours: Optional[float]) -> Optional[datetime]:
    """把"最近 N 小时"换算成 UTC 时间；None/<=0 表示不限。"""
    if not hours or hours <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=float(hours))
