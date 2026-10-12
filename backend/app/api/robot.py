# -*- coding: utf-8 -*-
"""REST 接口: 健康/当前姿态/历史/CSV 导出/元数据。"""
from __future__ import annotations

import csv
import io
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_control
from app.core.config import get_config
from app.core.deps import get_db
from app.db.crud import get_recent_poses
from app.schemas.pose import HealthOut, HistoryOut, MetaOut
from app.services.collector import collector, feed_run_mode
from app.services.hub import hub
from app.services.motion import motion, real_write_enabled, snapshot_ready
from app.services.rc_ready import ReadinessService

router = APIRouter(prefix="/api", tags=["robot"])
_ready = ReadinessService()


@router.get("/health", response_model=HealthOut)
def health():
    return HealthOut(
        robot=get_config().robot_model,
        connected=collector.connected,
        simulated=collector.simulated,
        clients=hub.count(),
    )


@router.post("/reconnect")
def reconnect(tok: str = Depends(require_control)):
    """手动重连机器人（网线在摄像头/机器人间切换后触发，无需重启服务）。

    ★ 审计修复 P1-B2：本接口原来**无鉴权** —— 匿名可反复触发重连，
      每次都会做 TCP 探测并强制切换真实/模拟模式（把演示环境拽回真机链路）。
      属于会影响产线链路状态的写操作，必须持控制令牌。
    """
    return collector.reconnect()


@router.get("/rc-status")
def rc_status():
    """控制器寄存器快照（只读，不写任何寄存器）。

    返回状态位解码（模式/伺服/报警/急停/程序）、报警码、当前程序号、
    点动触发/完成位、以及"真实下发是否已开启"与点动服务程序号。
    供前端「真机链路就绪」卡展示与排障。
    """
    snap, err = motion.modbus.rc_snapshot()
    cfg = get_config()
    prog_no = 0
    try:
        prog_no = int(cfg.get("motion", "jog", "service_program", default=0) or 0)
    except Exception:
        prog_no = 0
    if snap is None:
        return {"ok": False, "error": err or "读不到控制器",
                "real_enabled": real_write_enabled(), "service_program": prog_no}
    snap["ok"] = True
    snap["real_enabled"] = real_write_enabled()
    snap["service_program"] = prog_no
    # 就绪态一句话判定（前端直接显示）
    b = snap["bits"]
    # ★★ 2026-10-12 修正（真 bug）：判据收敛到 `motion.snapshot_ready`（唯一实现）——
    #   旧式 `servo and prog_loaded and run` 在**正常运行态**（prog=210/run=1/prog_loaded=0）
    #   恒为 False（"已加载"位在程序 RUN 起来后会落下），导致前端 `rc.ready` 永远为假、
    #   所有就绪门控的按钮点不动。详见 `motion.readiness_reason` 的现场注释。
    snap["ready"] = snapshot_ready(snap)
    # ★★ 2026-10-09 新增：**伺服是否上电**（只看伺服位）。
    #   为什么单列一个字段：用户口径 ——
    #     "如果伺服上电的状态，应该显示取消就绪；如果没有上电伺服，那就需要一键就绪"
    #   而 `ready` 是**完整就绪**（伺服 + 程序已加载 + 运行位 + AUTO + 无报警）。
    #   于是出现现场那个怪现象：伺服明明上着电，但只要程序没加载/没运行，
    #   `ready=false` → 按钮还显示「一键就绪」→ 操作员不知道该点哪个。
    #   现在按钮用 `servo_on` 判（该显示什么），`ready` 继续表示"整条链路是否可用"。
    snap["servo_on"] = bool(b["servo"] and not b["alarm"] and not b["estop"])
    # ★ 实测档位喂给 runmode（与采集循环同一条路）：用户手动刷新真机链路时，
    #   底栏「示教器」灯也能立刻反映旋钮实际位置，不必等下一个 4s 采集周期。
    feed_run_mode(snap.get("mode"), snap.get("status_word"))
    return snap


class ReadyIn(BaseModel):
    # ★ 全维度审查 B-01：范围约束 + 服务端白名单（见 rc_ready.allowed_programs）
    prog: Optional[int] = Field(default=None, ge=1, le=9999)


class ReadyCancelIn(BaseModel):
    """取消就绪入参。★ servo_off 默认 False：停程序 ≠ 断伺服，
    默认只做"最轻的那一步"，要下电必须由操作者显式勾选。"""
    servo_off: bool = False


@router.post("/ready")
def ready(body: Optional[ReadyIn] = None, request: Request = None,
          tok: str = Depends(require_control)):
    """一键就绪：清报警 → 伺服上电 → 加载点动服务程序 → 运行挂起于 WAIT。

    ★ 本身就是真机寄存器写操作：需控制令牌 + 双确认总闸（EFORT_REAL_MOTION=1
      且 motion.real_write=true）。总闸没开时明确拒绝并解释怎么开。
    """
    # ★ 全维度审查 B-24：审计事件带真实操作人与真实 IP（原 actor 硬编码 "web"）
    from app.api.auth import _client_ip, token_role
    ip = _client_ip(request)
    actor = token_role(tok) or "web"
    from app.services.events import emit as emit_event
    r = _ready.ready(prog=(body.prog if body else None),
                     on_step=lambda s, m: None)
    emit_event("control", "info" if r.get("ok") else "warn", "control.ready_request",
               "一键就绪：%s" % ("成功" if r.get("ok") else (r.get("error") or "失败")),
               {"steps": r.get("steps", []), "prog": (body.prog if body else None)},
               actor=actor, ip=ip)
    return r


@router.post("/ready/cancel")
def ready_cancel(body: Optional[ReadyCancelIn] = None, request: Request = None,
                 tok: str = Depends(require_control)):
    """取消就绪：停止点动服务程序，可选关闭伺服。

    用于"一键就绪"后想恢复到非就绪状态（如切换档位、调整参数）。
    不做急停，只是让控制器程序停止运行，伺服可选是否下电。
    ★ 与 /ready 同级的真机写操作：同样要控制令牌 + 双确认总闸。
    """
    # ★ 全维度审查 B-24：审计事件带真实操作人与真实 IP
    from app.api.auth import _client_ip, token_role
    ip = _client_ip(request)
    actor = token_role(tok) or "web"
    from app.services.events import emit as emit_event
    # ★ 走 _ready.cancel()（与 /ready 同一个实例）：它内部拿 _busy_lock + motion._exec_lock，
    #   保证取消就绪不会插进飞行中的点动/下发序列 —— 裸函数版没有这层互斥。
    r = _ready.cancel(servo_off=bool(body.servo_off) if body else False)
    emit_event("control", "info" if r.get("ok") else "warn", "control.ready_cancel",
               "取消就绪：%s" % ("成功" if r.get("ok") else (r.get("error") or "失败")),
               {"steps": r.get("steps", [])}, actor=actor, ip=ip)
    return r


@router.get("/pose")
def pose():
    latest = collector.get_latest()
    if latest is None:
        return {"type": "pose", "connected": False, "simulated": True,
                "j1": 0, "j2": 0, "j3": 0, "j4": 0, "j5": 0, "j6": 0,
                "tcp": {"x": 0, "y": 0, "z": 0},
                "t": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    return latest


@router.get("/history", response_model=List[HistoryOut])
def history(limit: int = Query(500, ge=1, le=5000), db: Session = Depends(get_db)):
    rows = get_recent_poses(db, limit)
    return [
        {
            "id": r.id,
            "timestamp": r.timestamp.isoformat() + "Z" if r.timestamp else None,
            "j1": r.j1, "j2": r.j2, "j3": r.j3,
            "j4": r.j4, "j5": r.j5, "j6": r.j6,
            "tcp_x": r.tcp_x, "tcp_y": r.tcp_y, "tcp_z": r.tcp_z,
            "source": r.source,
        }
        for r in rows
    ]


@router.get("/export/csv")
def export_csv(limit: int = Query(2000, ge=1, le=20000), db: Session = Depends(get_db)):
    rows = get_recent_poses(db, limit)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "timestamp", "j1", "j2", "j3", "j4", "j5", "j6",
                "tcp_x", "tcp_y", "tcp_z", "source"])
    for r in rows:
        w.writerow([r.id, r.timestamp.isoformat() if r.timestamp else "",
                    r.j1, r.j2, r.j3, r.j4, r.j5, r.j6,
                    r.tcp_x, r.tcp_y, r.tcp_z, r.source])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pose_history.csv"},
    )


@router.get("/meta", response_model=MetaOut)
def meta():
    cfg = get_config()
    return {
        "robot": cfg.robot_model,
        "serial": cfg.get("robot", "serial", default=""),
        "reach_mm": cfg.get("robot", "reach_mm", default=0),
        "axes": ["J1", "J2", "J3", "J4", "J5", "J6"],
        "dh_calibration_pending": cfg.dh.get("calibration_pending", True),
        "mounting": cfg.get("mounting", "type", default="floor"),
        "simulate_mode": cfg.get("connection", "simulate", default="auto"),
        "server_ws_path": cfg.get("server", "ws_path", default="/ws/pose"),
        "dh": cfg.dh.get("joints", []),
        "joint_limits": cfg.get("joint_limits", default=[]),
    }
