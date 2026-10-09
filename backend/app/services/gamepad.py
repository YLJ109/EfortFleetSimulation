# -*- coding: utf-8 -*-
"""手柄读取（**后端直读 Windows XInput**）—— 浏览器读不到手柄时的替代方案。

★ 为什么要有它（现场实况 2026-10-10）：
  Windows 已把「山湾 Q34」认成 XInput 手柄（joy.cpl / XnaComposite / VID_045E&PID_028E 都在），
  但 **Edge/Chrome 的 `navigator.getGamepads()` 始终返回 0 个**（浏览器侧读不到）。
  与其在浏览器里折腾，不如**后端直接读 XInput**，再把状态喂给前端 —— 前端照旧走
  `games/gamepad.js` 那一套逻辑动作（边沿/死区/连发），**不新增第二套语义**。

★ 采样与"不漏按"：XInput 是**轮询式**的，短按（<100ms）如果只靠"前端每次 HTTP 取一次当前值"
  会被漏掉。所以这里开一个 ~120Hz 的后台采样线程：
    · `current` = 此刻按住的键；
    · `tapped`  = **自上次被读取以来出现过**的键（按位 OR，读一次就清）。
  前端把 `current ∪ tapped` 当作"按下集合"，短按也一定有一次边沿。

★ 只在 Windows 上可用；拿不到 DLL / 非 Windows → `available=False`，**如实上报，绝不假装**。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Dict, List, Optional

from app.core.logger import get_logger

log = get_logger("gamepad")

# ---- XInput 按钮位 → (名字, **标准布局下标**) ----
# 标准下标与前端 games/gamepad.js 的 BTN 完全一致：0=A 1=B 2=X 3=Y 4=LB 5=RB 6=LT 7=RT
#   8=View(Back) 9=Menu(Start) 10=LS 11=RS 12=↑ 13=↓ 14=← 15=→
XINPUT_BUTTONS = (
    (0x0001, "UP", 12), (0x0002, "DOWN", 13), (0x0004, "LEFT", 14), (0x0008, "RIGHT", 15),
    (0x0010, "START", 9), (0x0020, "BACK", 8), (0x0040, "LS", 10), (0x0080, "RS", 11),
    (0x0100, "LB", 4), (0x0200, "RB", 5),
    (0x1000, "A", 0), (0x2000, "B", 1), (0x4000, "X", 2), (0x8000, "Y", 3),
)
TRIGGER_T = 0.5          # LT/RT 超过这个比例算"按下"（标准下标 6/7）
POLL_HZ = 120            # 采样率：短按（人手 ~100ms）不会被漏


def bits_to_indices(bits: int, lt: float = 0.0, rt: float = 0.0) -> List[int]:
    """XInput 按钮位 + 扳机量 → **标准布局下标**列表（升序，去重）。"""
    out = [idx for mask, _name, idx in XINPUT_BUTTONS if int(bits) & mask]
    if float(lt) >= TRIGGER_T:
        out.append(6)
    if float(rt) >= TRIGGER_T:
        out.append(7)
    return sorted(set(out))


# ---------------------------------------------------------------- ctypes 结构
class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XINPUT_GAMEPAD)]


_dll = None
_dll_tried = False


def _load_dll():
    """按 1_4 → 1_3 → 9_1_0 顺序加载 XInput（Win8+ 用 1_4；旧系统回退）。

    ★ 只加载一次；失败就记 `_dll_tried`，不再反复尝试。
    """
    global _dll, _dll_tried
    if _dll_tried:
        return _dll
    _dll_tried = True
    if sys.platform != "win32":
        log.info("非 Windows 平台：XInput 不可用")
        return None
    for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            d = ctypes.WinDLL(name)          # type: ignore[attr-defined]
            d.XInputGetState.argtypes = [ctypes.c_ulong, ctypes.POINTER(_XINPUT_STATE)]
            d.XInputGetState.restype = ctypes.c_ulong
            _dll = d
            log.info("XInput 已加载：%s", name)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("加载 %s 失败：%s", name, e)
    return _dll


def read_raw() -> Dict:
    """读一次 XInput。

    返回恒为 dict：
      {"available": bool, "connected": bool, "index": int,
       "bits": int, "lx","ly","rx","ry": float(-1~1), "lt","rt": float(0~1)}
    `available=False` = 这台机器上读不了（非 Windows / 无 XInput DLL）。
    """
    dll = _load_dll()
    if dll is None:
        return {"available": False, "connected": False, "index": -1, "bits": 0,
                "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0}
    st = _XINPUT_STATE()
    for idx in range(4):                     # XInput 最多 4 个槽位
        try:
            if dll.XInputGetState(idx, ctypes.byref(st)) == 0:   # 0 = ERROR_SUCCESS
                g = st.Gamepad
                return {
                    "available": True, "connected": True, "index": idx,
                    "bits": int(g.wButtons),
                    # ★ XInput 的 LY 向上为正，而标准布局 Y **向下为正** → 取反
                    "lx": g.sThumbLX / 32767.0, "ly": -g.sThumbLY / 32767.0,
                    "rx": g.sThumbRX / 32767.0, "ry": -g.sThumbRY / 32767.0,
                    "lt": g.bLeftTrigger / 255.0, "rt": g.bRightTrigger / 255.0,
                }
        except Exception as e:  # noqa: BLE001
            log.warning("XInputGetState(%d) 异常：%s", idx, e)
            break
    return {"available": True, "connected": False, "index": -1, "bits": 0,
            "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0}


# ---------------------------------------------------------------- 采样线程
_lock = threading.Lock()
_st = {"available": False, "connected": False, "index": -1, "packet": 0,
       "buttons": set(), "tapped": set(),
       "axes": [0.0, 0.0, 0.0, 0.0], "name": "",
       "last_input_at": 0.0}          # 最后一次"真有输入"的时刻（诊断"连了却不动"）
_thread: Optional[threading.Thread] = None


def _tick() -> None:
    """一次采样：更新 current，并把按下的键 OR 进 tapped（供"不被漏按"）。"""
    r = read_raw()
    with _lock:
        _st["available"] = bool(r.get("available"))
        _st["connected"] = bool(r.get("connected"))
        _st["index"] = int(r.get("index", -1))
        if r.get("connected"):
            idxs = set(bits_to_indices(r.get("bits", 0), r.get("lt", 0.0), r.get("rt", 0.0)))
            axes = [round(float(r.get(k, 0.0)), 3) for k in ("lx", "ly", "rx", "ry")]
            _st["buttons"] = idxs
            _st["tapped"] |= idxs            # ★ 累计"这段时间出现过"的键
            _st["axes"] = axes
            _st["packet"] += 1
            _st["name"] = "XInput #%d" % int(r.get("index", 0))
            # ★ 只要"有按键 或 摇杆明显偏离中位"就算一次真输入 —— 用来诊断
            #   "设备在却一直不动"（现场常见：接收器插着但手柄没配对/没开机）
            if idxs or any(abs(v) > 0.15 for v in axes):
                _st["last_input_at"] = time.time()
        else:
            _st["buttons"] = set()
            _st["axes"] = [0.0, 0.0, 0.0, 0.0]


def _loop() -> None:
    period = 1.0 / float(POLL_HZ)
    while True:
        try:
            _tick()
        except Exception as e:  # noqa: BLE001
            log.warning("手柄采样异常：%s", e)
        time.sleep(period)


def ensure_started() -> None:
    """幂等启动采样线程（首次被请求时启动，daemon，不阻塞退出）。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="gamepad-sampler", daemon=True)
    _thread.start()
    log.info("手柄采样线程已启动（%dHz，XInput）", POLL_HZ)


def drain() -> Dict:
    """取当前快照，并**清掉 tapped**（下次读到时只包含这之后新出现的按键）。

    返回（JSON 友好）：
      {"available","connected","index","buttons","current","axes","name","poll_hz"}
      · buttons = current ∪ tapped（**前端用它判按下**，短按不会漏）
      · current = 此刻真正按住的键（**界面实时读数用**）
    """
    ensure_started()
    with _lock:
        cur = set(_st["buttons"])
        union = cur | set(_st["tapped"])
        _st["tapped"] = set()                 # 读一次就清
        last = float(_st.get("last_input_at") or 0.0)
        return {
            "available": bool(_st["available"]),
            "connected": bool(_st["connected"]),
            "index": int(_st["index"]),
            "buttons": sorted(union),
            "current": sorted(cur),
            "axes": list(_st["axes"]),
            "name": str(_st["name"] or ""),
            "poll_hz": POLL_HZ,
            "packet": int(_st["packet"]),     # 采样计数（连续增长=采样线程活着）
            # ★ 距上次"真输入"多少秒；None = 开机以来一次输入都没收到
            #   —— 界面据此给出"连了却不动"的准确提示（现场常见：接收器没配对）
            "idle_sec": None if last <= 0 else round(time.time() - last, 1),
        }


def snapshot() -> Dict:
    """只读快照（不清 tapped；调试用）。"""
    with _lock:
        return {
            "available": bool(_st["available"]),
            "connected": bool(_st["connected"]),
            "index": int(_st["index"]),
            "buttons": sorted(_st["buttons"]),
            "axes": list(_st["axes"]),
            "name": str(_st["name"] or ""),
        }
