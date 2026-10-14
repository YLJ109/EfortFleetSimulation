# -*- coding: utf-8 -*-
"""
WebSocket 广播中枢 (线程安全)。

采集线程 (Collector) 在任意线程调用 broadcast(), 经事件循环安全推送到所有客户端。
P1-2 背压: 每客户端只保留"最新一帧"; 客户端发送未完成时, 新帧覆盖旧帧(丢弃积压),
          避免慢客户端导致 asyncio task 无限堆积(延迟/内存上涨)。

★ 审计修复 P1-B6：**姿态流与事件流分流**。
  原实现所有客户端共用一个集合，`type="event"` 的审计帧（含 actor/ip/登录结果）
  会原样推给匿名连上 `/ws/pose` 的任何浏览器 —— 浏览器 WS 不受 CORS 保护，
  服务端不校验就等于把审计日志广播出去。现在按客户端登记的 kind 过滤：
  pose 客户端只收姿态/rc_status，events 客户端只收事件帧。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Dict, FrozenSet, Optional

from app.core.logger import get_logger
from app.services import metrics

log = get_logger("hub")

# 每条帧归属哪条流：type 缺省按"非事件即姿态"归类，兜住旧调用点。
_EVENT_TYPES: FrozenSet[str] = frozenset({"event"})
_KIND_POSE = "pose"
_KIND_EVENTS = "events"


def _kind_of(payload: dict) -> str:
    return _KIND_EVENTS if payload.get("type") in _EVENT_TYPES else _KIND_POSE


class _Client:
    __slots__ = ("ws", "loop", "latest", "sending", "kinds")

    def __init__(self, ws, loop, kinds: FrozenSet[str]) -> None:
        self.ws = ws
        self.loop = loop
        self.latest = None       # 待发的最新帧(字符串), None 表示无积压
        self.sending = False     # 是否已有 drain 任务在跑
        self.kinds = kinds       # ★ P1-B6：本客户端订阅的流


class WsHub:
    def __init__(self) -> None:
        self._clients: Dict[object, _Client] = {}      # ws -> _Client

    def register(self, ws, loop, kind: str = _KIND_POSE) -> None:
        self._clients[ws] = _Client(ws, loop, frozenset({kind}))

    def unregister(self, ws) -> None:
        self._clients.pop(ws, None)

    def count(self, kind: Optional[str] = None) -> int:
        """在线数。默认统计**全部**（/api/health 的 ws_clients 用的是这个口径），
        传 kind 则只统计订阅了该流的连接（连接上限判定用）。"""
        if kind is None:
            return len(self._clients)
        return sum(1 for c in self._clients.values() if kind in c.kinds)

    def pending(self) -> int:
        """★ B1 只读指标：当前「有未发出积压帧」的客户端数。

        背压设计下每客户端最多积压 **1 帧**（新帧覆盖旧帧），所以这个数
        就是积压上界的真值 —— 大于 0 = 至少一个慢客户端还没发完上一帧。
        """
        return sum(1 for c in self._clients.values() if c.latest is not None)

    def broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        # ★ B1 只读指标：量「序列化 + 入队」耗时（实际 send 在事件循环里，量不到，
        #   如实命名 broadcast_enqueue_ms，不冒充整条推送延迟）。
        t0 = time.perf_counter()
        kind = _kind_of(payload)
        data = json.dumps(payload, ensure_ascii=False)
        for c in list(self._clients.values()):
            if kind not in c.kinds:      # ★ P1-B6：不订阅这条流的客户端直接跳过
                continue
            c.latest = data              # 覆盖旧帧 —— 只保留最新
            if c.sending:
                continue
            c.sending = True
            try:
                c.loop.call_soon_threadsafe(self._drain, c)
            except Exception as e:
                log.debug("broadcast skip: %s", e)
                self.unregister(c.ws)
        metrics.stat("ws.broadcast_enqueue_ms", (time.perf_counter() - t0) * 1000.0)
        metrics.incr("ws.broadcast_frames")

    def _drain(self, c: "_Client") -> None:
        """在事件循环线程内执行: 连续发送直到无积压。"""
        async def _go():
            try:
                while c.latest is not None:
                    data, c.latest = c.latest, None
                    await c.ws.send_text(data)
            except Exception:
                self.unregister(c.ws)
            finally:
                c.sending = False

        asyncio.ensure_future(_go())


hub = WsHub()
