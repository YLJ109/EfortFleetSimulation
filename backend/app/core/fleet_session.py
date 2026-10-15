# -*- coding: utf-8 -*-
"""机房控制作用域会话（同一时刻只允许一个作用域持权）。

为什么要这一层：控制令牌回答的是"**这个人**能不能操作机器人"，
但机房有 12 台，还需要回答"**现在操作的是哪几台**"。没有它会出现两种事故：

  1. 两个人/两个页签各自以为在开不同的机器，实际同时往 12 台上发指令；
  2. 界面上选中了 A2，手滑点执行却把隔壁 A3 也动了 —— 因为"目标集合"从来没被显式记过。

所以作用域是**服务端状态**，不是前端约定：下发时逐台比对，不在作用域里直接 403。

★ 本模块只管"谁被授权碰哪几台"，不含任何真实运动逻辑；
  真机写通道仍然由 services/fleet.py 的三闸把关。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable, List, Optional

# 空闲多久自动交还（秒）。比前端心跳宽一档：网络抖一下不该把人踢出控制权，
# 但人走开不点"释放"时也不能把控制权一直占着 —— 下一个人会莫名其妙拿不到。
IDLE_S = 60.0

# 作用域类型（前端 fleetScope.js 用同一套字面量，改这里要同步改）
KINDS = ("single", "zone", "row", "all")


class ScopeSession:
    def __init__(self, scope: Dict[str, Any], actor: str) -> None:
        self.scope = scope
        self.ids: List[str] = sorted(scope.get("ids") or [])
        self.actor = actor
        self.acquired_at = time.time()
        self.last_seen = self.acquired_at

    def touches(self, robot_id: str) -> bool:
        return robot_id in self.ids

    @property
    def idle_s(self) -> float:
        return time.time() - self.last_seen

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.scope.get("kind") or "",
            "label": self.scope.get("label") or "",
            "ids": list(self.ids),
            "count": len(self.ids),
            "actor": self.actor,
            "idle_s": round(self.idle_s, 1),
            "ttl_s": max(0.0, round(IDLE_S - self.idle_s, 1)),
        }


class FleetSessionStore:
    """单例：当前持权作用域。线程安全（采集线程与 HTTP 线程都会读）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cur: Optional[ScopeSession] = None

    def _reap_locked(self) -> None:
        if self._cur is not None and self._cur.idle_s > IDLE_S:
            self._cur = None

    def acquire(self, scope: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """申请作用域。已持有者可以换作用域；别人持有时返回 409 语义。"""
        ids = [str(x) for x in (scope.get("ids") or []) if x]
        kind = str(scope.get("kind") or "")
        if kind not in KINDS:
            raise ValueError("未知作用域类型 %r（可用：%s）" % (kind, ",".join(KINDS)))
        if not ids:
            raise ValueError("作用域里没有一台机器人")
        with self._lock:
            self._reap_locked()
            if self._cur is not None and self._cur.actor != actor:
                return {"ok": False, "taken_by": self._cur.actor,
                        "idle_s": round(self._cur.idle_s, 1), "session": self._cur.as_dict()}
            self._cur = ScopeSession(dict(scope, ids=ids), actor)
            return {"ok": True, "session": self._cur.as_dict()}

    def release(self, actor: str = "") -> Dict[str, Any]:
        with self._lock:
            gone = self._cur.as_dict() if self._cur else None
            # 空 actor = 服务端内部调用（如采集停机），无条件收回
            if self._cur is not None and (not actor or self._cur.actor == actor):
                self._cur = None
                return {"released": True, "session": gone}
            return {"released": False, "session": gone}

    def current(self) -> Optional[ScopeSession]:
        with self._lock:
            self._reap_locked()
            return self._cur

    def touch(self, robot_id: Optional[str] = None) -> bool:
        """一次下发/查询：刷新空闲计时，并校验这台在不在作用域里。"""
        with self._lock:
            self._reap_locked()
            if self._cur is None:
                return False
            if robot_id is not None and not self._cur.touches(robot_id):
                return False
            self._cur.last_seen = time.time()
            return True

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._reap_locked()
            return {"active": self._cur is not None,
                    "session": self._cur.as_dict() if self._cur else None,
                    "idle_ttl_s": IDLE_S}


def build_scope_ids(kind: str, robot_id: str = "", zone: str = "",
                    group: str = "", robots: Iterable[Dict[str, Any]] = ()) -> List[str]:
    """把"作用域意图"翻成机器人 id 列表（服务端算，不信前端传来的列表）。

    ★ 前端只报 kind + 锚点，ids 由这里按当前机房名单现算：
      名单刚被改过（增删台、换区）时，前端缓存的 id 列表可能已经过期，
      信它就等于把控制权发给一台已经不存在的机器。
    """
    rows = list(robots or [])
    if kind == "single":
        return [r["id"] for r in rows if r.get("id") == robot_id]
    if kind == "zone":
        return [r["id"] for r in rows if r.get("zone") == zone]
    if kind == "row":
        return [r["id"] for r in rows if r.get("group") == group]
    if kind == "all":
        return [r["id"] for r in rows if r.get("id")]
    return []


fleet_sessions = FleetSessionStore()
