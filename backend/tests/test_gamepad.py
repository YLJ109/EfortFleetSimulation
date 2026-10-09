# -*- coding: utf-8 -*-
"""手柄后端读取（XInput）回归 —— 不碰真手柄。

★ 为什么一律 stub：本机是 Windows 且**真的插着手柄**，直接调 `read_raw()`
  会拿到真实硬件状态（随人按键变化），测试会不稳。所以把 `read_raw` 换成脚本化读数，
  只钉"映射 / 短按不漏 / 状态结构 / 接口免令牌只读"这几件事。
"""
from __future__ import annotations

from app.services import gamepad as G


def _raw(bits: int = 0, lt: float = 0.0, rt: float = 0.0,
         connected: bool = True, index: int = 0) -> dict:
    return {"available": True, "connected": connected, "index": index, "bits": bits,
            "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": lt, "rt": rt}


# ---------------------------------------------------------------- 映射
def test_bits_to_indices_standard_layout():
    """XInput 按钮位 → **标准布局下标**（必须与前端 games/gamepad.js 的 BTN 一致）。"""
    assert G.bits_to_indices(0x1000) == [0]      # A
    assert G.bits_to_indices(0x2000) == [1]      # B
    assert G.bits_to_indices(0x4000) == [2]      # X
    assert G.bits_to_indices(0x8000) == [3]      # Y
    assert G.bits_to_indices(0x0010) == [9]      # Start → Menu
    assert G.bits_to_indices(0x0020) == [8]      # Back  → View
    assert G.bits_to_indices(0x0001) == [12]     # 十字键 ↑
    assert G.bits_to_indices(0x0002) == [13]     # ↓
    assert G.bits_to_indices(0x0004) == [14]     # ←
    assert G.bits_to_indices(0x0008) == [15]     # →
    assert G.bits_to_indices(0x0001 | 0x1000) == [0, 12]   # 多键同按
    assert G.bits_to_indices(0) == []


def test_triggers_map_to_6_and_7_over_threshold():
    assert G.bits_to_indices(0, lt=1.0) == [6]           # LT
    assert G.bits_to_indices(0, rt=1.0) == [7]           # RT
    assert G.bits_to_indices(0, lt=0.4, rt=0.49) == []   # 没过阈值不算按下


def test_read_raw_shape():
    """真读一次（不管有没有手柄）：字段必须齐、类型正确 —— 不依赖硬件状态。"""
    r = G.read_raw()
    for k in ("available", "connected", "index", "bits",
              "lx", "ly", "rx", "ry", "lt", "rt"):
        assert k in r, k
    assert isinstance(r["available"], bool) and isinstance(r["connected"], bool)


# ---------------------------------------------------------------- 短按不漏
def test_tapped_survives_quick_release(monkeypatch):
    """★ 核心口径：**短按**（两次读取之间按下又松开）也必须被读到一次。

    场景：tick1 按住 A → tick2 已松开（人手 ~100ms，前端可能还没来读）。
    期望：第一次 drain 仍能看到 A（buttons 含 0），但 current 为空（此刻没按住）；
          消费过之后再 drain 就干净了（不重复触发）。
    """
    monkeypatch.setattr(G, "ensure_started", lambda: None)      # 不起真线程，手工驱动
    monkeypatch.setattr(G, "read_raw", lambda: _raw(bits=0x1000))
    G._tick()
    monkeypatch.setattr(G, "read_raw", lambda: _raw(bits=0))
    G._tick()

    d1 = G.drain()
    assert d1["buttons"] == [0], d1
    assert d1["current"] == [], d1
    d2 = G.drain()
    assert d2["buttons"] == [], d2                          # 已消费，不再重复


def test_drain_reports_disconnected(monkeypatch):
    monkeypatch.setattr(G, "ensure_started", lambda: None)
    monkeypatch.setattr(G, "read_raw", lambda: _raw(connected=False))
    G._tick()
    d = G.drain()
    assert d["connected"] is False and d["buttons"] == [] and d["current"] == []


def test_drain_reports_idle_sec(monkeypatch):
    """★ 诊断"连了却不动"：idle_sec=None = 开机以来**一次输入都没收到**（设备在、数据不动）。"""
    monkeypatch.setattr(G, "ensure_started", lambda: None)
    G._st["last_input_at"] = 0.0                       # 模拟"从未有输入"
    monkeypatch.setattr(G, "read_raw", lambda: _raw(bits=0))
    G._tick()
    assert G.drain()["idle_sec"] is None
    monkeypatch.setattr(G, "read_raw", lambda: _raw(bits=0x1000))   # 有按键 → 记下时刻
    G._tick()
    d = G.drain()
    assert isinstance(d["idle_sec"], float) and d["idle_sec"] < 5.0


def test_drain_shape(monkeypatch):
    monkeypatch.setattr(G, "ensure_started", lambda: None)
    monkeypatch.setattr(G, "read_raw", lambda: _raw(bits=0x1000))
    G._tick()
    d = G.drain()
    for k in ("available", "connected", "index", "buttons", "current", "axes",
              "name", "poll_hz"):
        assert k in d, k


# ---------------------------------------------------------------- 接口
def test_state_api_is_read_only_and_tokenless(client, monkeypatch):
    """只读免令牌（与 /control/preview 同类）：裸调就该 200，且结构完整。"""
    monkeypatch.setattr(G, "ensure_started", lambda: None)
    monkeypatch.setattr(G, "read_raw", lambda: _raw(connected=False))
    G._tick()
    r = client.get("/api/gamepad/state")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    for k in ("available", "connected", "buttons", "current", "axes"):
        assert k in d, k
