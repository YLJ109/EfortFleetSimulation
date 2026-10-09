# -*- coding: utf-8 -*-
"""手柄状态查询（**后端直读 XInput** 给前端用）。

★ 免令牌（只读）：与 `/control/preview`、`/control/ik` 同类 —— 纯查询、不碰任何状态。
  真正"用手柄驱动机器人"的动作仍然走各自的控制接口（那些有令牌门槛），
  所以这里放开读取不会成为权限后门。

★ 前端调用方式与浏览器 `navigator.getGamepads()` 等价：轮询本接口拿"按下集合 + 摇杆轴"，
  再喂给 `games/gamepad.js` 的同一套逻辑动作（边沿/死区/连发）。
"""
from fastapi import APIRouter

from app.services import gamepad

router = APIRouter(prefix="/api/gamepad", tags=["gamepad"])


@router.get("/state")
def api_state():
    """读一次手柄状态（会消费"自上次读取以来出现过的按键"，保证短按不漏）。"""
    return {"ok": True, **gamepad.drain()}
