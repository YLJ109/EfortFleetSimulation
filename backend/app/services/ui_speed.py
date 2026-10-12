# -*- coding: utf-8 -*-
"""「界面上那条速度滑块」的当前位置 —— 让**长流程在步边界跟随滑块**（2026-10-11 用户口径）。

## 为什么需要它（用户原话）

> 「点位执行的速度正常啊…**但是智能分拣不行啊，还有游戏**」
> 「**应该电脑的速度去执行**」
> 「快速分拣为什么又变成 20%，我调试的 50%？**偶尔出现这个问题啊**」

**第一个问题（已修）**：点位执行 / 点动是**每次下发都读当前滑块**；分拣 / 井字棋这类
**长流程**原来"**点开始那一刻**"就把速度定死（`st["speed_pct"]`）→ 跑起来后再拖滑块不生效。

**第二个问题（本版修的就是它）**：上一版让**每个流程开始时"自报"自己的速度**，
于是变成"**谁最后说谁说了算**" —— 分拣跑到一半，**别的流程 / 后台视觉规则**一自报，
就把操作员的 50% **顶成 20%**（用户："偶尔出现这个问题"）。

## 本版的唯一口径（一句话）

> **本模块只表示"界面上那条滑块现在的位置"，只由前端拖动来写。**

| 谁 | 能写吗 | 为什么 |
|---|---|---|
| **前端拖滑块**（`POST /control/ui-speed`） | ✅ **唯一写入者** | 这是"操作员此刻的意图" |
| 各流程"开始时自报" | ❌ **已全部删除** | 那正是"偶尔变 20%"的根因（互相顶） |
| 后台视觉规则自动执行 | ❌ 不读也不写 | 无人值守自动化用**它自己的**速度，不该被人上次拖的值影响 |

## 流程怎么"跟随"（`apply(fallback, st)`，替换旧的 `effective`）

**按时间戳比较**，不需要任何"自报"、也不需要额外配置：

1. 流程**第一次**取速度时，把"当时滑块的登记时刻"记进 `st["_ui_speed_base"]`（**基准**）；
2. 之后每次取速度：
   · 滑块登记时刻 **晚于基准**（说明**流程跑起来之后，人动过滑块**）→ **用滑块值**；
   · 否则（人没动过）→ **原样用传入的 `fallback`**（即该流程启动时那一份）。

这样：
- **界面发起的流程** + 运行中拖滑块 → **下一步生效** ✓（用户要的）
- **脚本 / 直连 API** 发起的流程 → 之后没人拖滑块 → 用**它自己请求里的值**，**不会被界面串味** ✓
- **后台视觉规则** → 根本不用本模块（它有自己的速度配置）✓
- **没推送过 / `st=None` / 非法值** → `apply` 原样返回 `fallback` → **零回归** ✓
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from app.core.logger import get_logger
from app.core.safety_const import clamp_speed

log = get_logger(__name__)

#: `st` 里存"本流程的基准时刻"的私有键（不会与流程自身字段冲突）
BASE_KEY = "_ui_speed_base"

_lock = threading.Lock()
_pct: Optional[int] = None          # None = 从未推送过 → apply() 原样返回 fallback
_at: float = 0.0                    # 最近一次推送的时刻（time.time()）
_meta: Dict[str, object] = {}       # {"note": 来源}


def publish(pct, note: str = "") -> int:
    """登记"界面上那条滑块"的位置（%），并盖时间戳。返回**实际存下的值**（已夹取）。

    ★ **只有前端拖滑块（`POST /control/ui-speed`）该调它** —— 流程自报已按用户口径删除，
      否则又会回到"谁最后说谁说了算"（正是"偶尔变 20%"的根因）。
    ★ `note` 只用于日志/追溯，不参与判据。
    """
    global _pct, _at
    try:
        v = clamp_speed(pct)
    except Exception:  # noqa: BLE001 —— 非法值一律退回安全下限，绝不抛给调用方
        v = clamp_speed(None)
    with _lock:
        _pct = v
        _at = time.time()
        _meta.clear()
        _meta.update({"note": str(note or "")})
    return v


def at() -> float:
    """最近一次推送的时刻（0 = 从未推送）。"""
    with _lock:
        return _at


def apply(fallback, st: Optional[Dict] = None):
    """**取"本次该用的速度"**（旧名 `effective`，语义升级为"按时间戳判断是否跟随"）。

    · 从未推送过 / 拿不到 `st` / `fallback` 非法 → **原样返回 `fallback`**（零回归）；
    · 流程**第一次**调用 → 建基准（记下当时滑块时刻）→ 本次**仍用 `fallback`**；
    · 之后：滑块时刻**晚于基准**（人动过滑块）→ 用滑块值；否则用 `fallback`。
    """
    with _lock:
        cur, cur_at = _pct, _at
    if cur is None or not isinstance(st, dict):
        return clamp_speed(fallback) if fallback is not None else fallback
    base = st.get(BASE_KEY)
    if base is None:
        # ★ 第一次取速度 = 本流程的"基准时刻"：此后**只有人再动滑块**才算"跟随"。
        st[BASE_KEY] = cur_at
        return clamp_speed(fallback) if fallback is not None else fallback
    if cur_at > float(base):
        return cur                      # 人动过滑块 → 跟随（下一步生效）
    return clamp_speed(fallback) if fallback is not None else fallback


#: 兼容旧名（行为 = `apply`）
effective = apply


def peek() -> Dict[str, object]:
    """当前登记值快照（`pct=None` 表示从未推送）—— 供 `/control/ui-speed` 回显与自检。"""
    with _lock:
        return {"speed_pct": _pct, "at": (_at or None), "note": _meta.get("note") or ""}


def reset() -> None:
    """清空登记（**只给测试用**：避免用例之间串味；生产路径不调它）。"""
    global _pct, _at
    with _lock:
        _pct = None
        _at = 0.0
        _meta.clear()
