# -*- coding: utf-8 -*-
"""
系统运维 API（阶段 3 运维增强 + 阶段 4 加固）：健康总览 / 配置备份恢复。

GET  /api/system/health    健康总览（服务、数据库、机器人链路、执行引擎、WS 在线数）
POST /api/system/reconnect 重连控制器（★ 需控制权限，比 /api/reconnect 多一层鉴权与留痕）
GET  /api/system/export    导出系统配置包（安全围栏 + 点位 + 程序）★ 需控制权限
POST /api/system/import    导入配置包恢复（★ 管理员权限，导入前自动归档围栏版本）

备份包格式 "efort-system/v1"：
{
  "format": "efort-system/v1", "exported_at": "...",
  "service": {"name": "...", "version": "..."},
  "safety": { ...完整围栏配置... },
  "points":  [{ id, name, group, kind, joints, tcp, note }, ...],
  "programs":[{ id, name, note, items:[{point_id, point_name, speed_pct, dwell_ms}] }, ...]
}
程序条目额外带 point_name，是为了让恢复端把点位重新对上号 —— 点位 id 在新库里可能不同。
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.auth import require_admin, require_control, token_role
from app.core.config import db_path, get_config
from app.core.deps import get_db
from app.core.safety_config import load_safety, save_safety
from app.db import crud
from app.db.database import SessionLocal
from app.services.collector import collector
from app.services import daily_report
from app.services import metrics
from app.services.events import emit as emit_event
from app.services.hold_state import hold_state
from app.services.hub import hub
from app.services.motion import motion
from app.services.runmode import runmode
from app.services.safety_guard import snapshot as safety_snapshot
from app.core.safety_const import joint_lock_state
from app.core.brand import (
    EXPORT_FORMAT as _EXPORT_FORMAT,
    SERVICE_NAME,
    SERVICE_NAME_EN,
    SERVICE_VERSION,
)

router = APIRouter(prefix="/api/system", tags=["system"])

_START = time.time()

# 相机服务探测缓存。★ 必须有缓存：底栏每 5 秒拉一次自检，
# 而相机服务没启动是最常见的情形 —— 没缓存的话每次都要等满超时。
_LINK_CAM: Dict[str, Any] = {"ts": 0.0, "data": None}
_LINK_CAM_TTL = 4.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _human_size(n: int) -> str:
    """把字节数格式化成人类可读字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


# ---------------------------------------------------------------- 健康
@router.get("/health")
def api_health():
    """健康总览：一眼看出"服务活着、库能读写、机器人在不在线、有没有人连着看"。"""
    db_info: Dict[str, Any] = {"ok": False}
    try:
        s = SessionLocal()
        try:
            s.execute(text("SELECT 1"))
            p = db_path()
            size = os.path.getsize(p) if os.path.isfile(p) else 0
            db_info = {"ok": True, "path": p, "size": _human_size(size)}
        finally:
            s.close()
    except Exception as e:
        db_info = {"ok": False, "error": str(e)[:200]}

    cfg = get_config()
    # ★ Modbus 连接参数在 robot.yaml 的 connection 段（不是 modbus 段），
    #   两个都兜一遍，避免健康检查里 host 显示成空字符串。
    mb = cfg.modbus or {}
    conn_cfg = cfg.connection or {}
    conn_ok = bool(collector.connected)
    simulated = bool(collector.simulated)

    if not db_info["ok"]:
        status = "down"
    elif conn_ok and not simulated:
        status = "ok"
    else:
        status = "degraded"   # 服务与库正常，但机器人链路处于模拟/掉线

    return {
        "status": status,
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "time": _now_iso(),
        "uptime_sec": int(time.time() - _START),
        "python": platform.python_version(),
        "db": db_info,
        "robot": {
            "model": cfg.robot_model,
            "connected": conn_ok,
            "simulated": simulated,
            "mode": "simulate" if simulated else "real",
        },
        "modbus": {
            "host": mb.get("host") or conn_cfg.get("host", ""),
            "port": mb.get("port") or conn_cfg.get("port", 502),
            "unit_id": conn_cfg.get("unit_id", 1),
        },
        "ws_clients": hub.count(),
        "motion": motion.state(),
        "safety_interlock": safety_snapshot(),
        # ★ 全维度审查 F-03：把轴锁模式暴露给前端，让执行页/顶栏徽标能显示当前
        #   是「J1–J6 全轴可动」还是「轴锁模式：仅 J6」。默认 enabled=false（全轴可动）。
        "joint_lock": joint_lock_state(),
    }


# ---------------------------------------------------------------- 连接自检 / 操作引导
# ★ 审计修复 P1-B8/P1-C7：机器人网口探测的结果缓存。
#   原实现无缓存，而 `/api/system/guide` 被前端每 5s 轮询（每个标签页各一次）、
#   `/api/settings/live` 也在拉 —— 断网时每次都要等满 0.6s 超时，
#   多开几个标签页就能把 40 线程的线程池占满，整站 DoS（还会持续骚扰控制器）。
_PROBE_TTL = 3.0
_PROBE_CACHE: Dict[str, Tuple[float, bool, str]] = {}   # (host,port) -> (ts, ok, err)
_PROBE_LOCK = threading.Lock()


def _tcp_probe(host: str, port: int, timeout: float = 0.6,
               use_cache: bool = True):
    """探控制器网口是否可达（只握手不读数据，超时很短，不卡接口）。

    ``use_cache=False`` 供「测试连接」这类**用户主动触发**的探测绕过缓存 ——
    否则改完地址点测试，返回的还是 3 秒前旧地址的结论（"改了没生效"）。
    """
    import socket
    if not host:
        return False, "未配置控制器地址"
    key = "%s:%s" % (host, port)
    now = time.time()
    if use_cache:
        with _PROBE_LOCK:
            hit = _PROBE_CACHE.get(key)
        if hit and (now - hit[0]) < _PROBE_TTL:
            return hit[1], hit[2]
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            ok, err = True, ""
    except Exception as e:
        ok, err = False, str(e)[:80]
    with _PROBE_LOCK:
        # 缓存条目数封顶：配置里改来改去的 host 不至于无限堆积
        if len(_PROBE_CACHE) > 64:
            _PROBE_CACHE.clear()
        _PROBE_CACHE[key] = (now, ok, err)
    return ok, err


# ---------------------------------------------------------------- 链路状态（底栏常驻）
def invalidate_link_cache() -> None:
    """丢弃链路探测缓存（相机 + 控制器网口）。

    ★ 改了 `camera.host` / `camera.port` / `connection.host` 之后必须调用：
      `_camera_link()` 有 4 秒缓存、`_tcp_probe()` 有 3 秒缓存，
      不清的话设置页保存成功后底栏可能还挂着旧地址的探测结论（"保存了没生效"）。
    """
    _LINK_CAM["data"] = None
    _LINK_CAM["ts"] = 0.0
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


def _camera_link() -> Dict[str, Any]:
    """相机链路状态（探相机服务的 /status）。

    ★ 一律不抛异常：相机服务没启动是最常见的现场状态，不是错误。
    ★ 结果带 4 秒缓存：底栏每 5 秒轮询自检，没缓存时服务未启动要等满超时。
    """
    from app.services import camera_client

    now = time.time()
    if _LINK_CAM["data"] is not None and (now - _LINK_CAM["ts"]) < _LINK_CAM_TTL:
        return _LINK_CAM["data"]

    base = camera_client.base_url()
    port = base.rsplit(":", 1)[-1] if ":" in base else ""
    st, data, err = camera_client.get_json("/status", timeout=1.0)

    if st != 200 or not isinstance(data, dict):
        out: Dict[str, Any] = {
            "ok": False, "state": "down", "level": "warn",
            "label": "相机服务未启动",
            "detail": "启动 camera/camera_service.py（或 run.bat camera）后自动恢复",
            "base": base, "port": port,
            "opened": False, "device": "", "fps": 0, "resolution": "-",
            "error": (err or ("HTTP " + str(st))) if st else (err or "无法连接"),
        }
    else:
        dev = data.get("device") or {}
        model = str(dev.get("model") or "")
        serial = str(dev.get("serial") or "")
        ip = str(dev.get("ip") or "")
        opened = bool(data.get("opened"))
        res = str(data.get("resolution") or "-")
        fps = data.get("fps") or 0
        if opened:
            state, level, label = "ready", "ok", "相机已连接"
            detail = " ".join([x for x in (model, ("SN " + serial) if serial else "",
                                           ip, res, (str(fps) + " fps") if fps else "") if x])
        elif data.get("opening"):
            state, level, label = "opening", "warn", "相机正在打开"
            detail = "手动重连后需要 2~3 秒"
        elif data.get("error"):
            state, level, label = "error", "err", "相机未连接"
            detail = str(data["error"])[:160]
        else:
            state, level, label = "idle", "warn", "相机未打开"
            detail = "服务正常，点「打开相机」开始取流"
        out = {
            "ok": opened, "state": state, "level": level, "label": label,
            "detail": detail or "—",
            "base": base, "port": port, "opened": opened,
            "device": (model + (" · " + serial if serial else "")).strip(" ·"),
            "ip": ip, "fps": fps, "resolution": res,
            "error": str(data.get("error") or "")[:160],
        }

    _LINK_CAM["data"] = out
    _LINK_CAM["ts"] = now
    return out


def _robot_link(host: str, port: int, sim_mode: str) -> Dict[str, Any]:
    """机器人链路状态：真实链路 / 强制模拟 / 掉线降级模拟 / 完全读不到。

    ★ 「强制模拟」和「掉线降级模拟」必须分开：
      simulate=always 是**用户自己选的配置**，给红色等于天天误报警；
      simulate=auto 下读不到数据而自动降级才是真故障，必须红。
      这两者一旦合并，真故障就会被"反正一直在模拟"掩盖过去 —— 这是最危险的一类告警失效。
    """
    conn_ok = bool(collector.connected)
    sim = bool(collector.simulated)
    forced = str(sim_mode or "").strip().lower() == "always"
    if sim and forced:
        state, level, label = "sim_forced", "warn", "强制模拟"
        detail = "config/robot.yaml 的 connection.simulate=always：姿态由模拟器生成，不是真机姿态"
    elif sim:
        state, level, label = "sim", "err", "模拟数据（真机掉线降级）"
        detail = "已自动降级为模拟姿态；检查网线 / 示教器档位 / 伺服使能后点「重新连接」"
    elif conn_ok:
        state, level, label = "real", "ok", "真实链路"
        detail = f"{host}:{port} 实时读数"
    else:
        state, level, label = "offline", "err", "未连接"
        detail = f"读不到 {host}:{port} 的数据：检查网线 / 示教器模式 / 伺服使能"
    return {
        "ok": bool(conn_ok and not sim), "state": state, "level": level,
        "label": label, "detail": detail,
        "host": host, "port": port,
        "connected": conn_ok, "simulated": sim, "simulate": sim_mode,
    }


def _links(host: str, port: int, sim_mode: str) -> Dict[str, Any]:
    """底栏三个常驻状态灯的数据源（机器人 / 摄像头 / 示教器档位）。

    ★ 与 checks 分开：checks 是"该做什么"（会随状态增减），
      links 是"现在是什么"（**任何情况下都三个都在**），前端底栏常驻显示。
    """
    rm = runmode.summary()
    return {
        "robot": _robot_link(host, port, sim_mode),
        "camera": _camera_link(),
        "run_mode": rm,
    }


@router.get("/guide")
def api_guide(request: Request):
    """连接自检 + 操作引导：把"网线没插 / 示教器没打到 AUTO / 没拿控制权"这类
    现场高频问题，直接翻译成人能执行的下一步动作。

    返回按严重程度排序的 checks，前端取第一个未通过项做主提示条；
    另附 links（机器人/摄像头/示教器档位）供底栏常驻状态灯使用。
    """
    cfg = get_config()
    conn = cfg.connection or {}
    mb = cfg.modbus or {}
    host = mb.get("host") or conn.get("host", "")
    port = int(mb.get("port") or conn.get("port", 502) or 502)
    sim_mode = (conn.get("simulate") or "auto")

    checks: List[Dict[str, Any]] = []

    # 1) 网线 / 网口可达性
    net_ok, net_err = _tcp_probe(host, port)
    checks.append({
        "key": "net", "ok": net_ok, "level": "info" if net_ok else "error",
        "title": "机器人网线" if net_ok else "机器人网线未连接",
        "hint": "" if net_ok else f"无法连接控制器 {host}:{port}（{net_err}）",
        "steps": [] if net_ok else [
            f"确认控制器已上电，网口指示灯亮（{host}）",
            "用网线连接控制器网口与工控机网口，观察网口灯是否闪烁",
            f"把工控机网口 IP 设成与 {host} 同网段（如 192.168.1.x，掩码 255.255.255.0）",
            f"命令行执行 ping {host} 验证链路",
        ],
        "action": {"kind": "none", "label": ""},
    })

    # 2) Modbus 通信（网通 ≠ 通信正常；示教器模式不对时会握手失败/无数据）
    linked = bool(collector.connected) and not bool(collector.simulated)
    if net_ok and not linked:
        checks.append({
            "key": "link", "ok": False, "level": "error",
            "title": "网口已连通，但控制器无响应",
            "hint": "网线已通，但读不到关节数据 —— 通常是示教器模式或使能状态不对",
            "steps": [
                "把示教器上的模式开关拨到 **AUTO（自动/远程）**，不要停在 T1/T2 手动模式",
                "确认示教器上的急停已旋起复位，且伺服已上电（SERVO ON）",
                "确认控制器未处于报警/暂停状态（示教器报警需先复位）",
                "若使用外部急停/安全门，确认安全回路已闭合",
                "回到本页面点「重新连接」",
            ],
            "action": {"kind": "reconnect", "label": "重新连接"},
        })
    else:
        checks.append({
            "key": "link", "ok": linked, "level": "info" if linked else "warn",
            "title": "控制器通信正常" if linked else "控制器未连接",
            "hint": "" if linked else "当前未读到真实关节数据（模拟/离线）",
            "steps": [] if linked else ["先按上一步接好网线并确认示教器处于 AUTO 模式"],
            "action": {"kind": "reconnect", "label": "重新连接"},
        })

    # 3) 真实链路 / 强制模拟
    if collector.simulated:
        checks.append({
            "key": "mode", "ok": sim_mode == "always", "level": "info" if sim_mode == "always" else "warn",
            "title": "当前显示的是模拟数据" if sim_mode != "always" else "已强制模拟模式",
            "hint": "" if sim_mode == "always"
                    else "页面上的姿态由模拟器生成，不是真机姿态（不影响查看，但下发前必须恢复真实链路）",
            "steps": [] if sim_mode == "always" else [
                f"检查 config/robot.yaml 的 connection.simulate（当前 {sim_mode}）",
                "设为 auto 或 never 后重启后端，或点「重新连接」切回真实",
            ],
            "action": {"kind": "reconnect", "label": "尝试切回真实"},
        })

    # 4) 控制权限（拿不到令牌 = 不能下发；示教器也必须在 AUTO）
    tok = request.headers.get("x-control-token") or ""
    if not tok:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    has_token = bool(token_role(tok))
    checks.append({
        "key": "token", "ok": has_token, "level": "info" if has_token else "warn",
        "title": "已获得控制权限" if has_token else "未获得控制权限",
        "hint": "" if has_token else "执行/点动/改点位都需要限时控制令牌（管理员密码）",
        "steps": [] if has_token else [
            "确认示教器已处于 **AUTO（自动/远程）** 模式 —— 手动模式下位机控制会被控制器拒绝",
            "点页面上的「请求控制」，输入管理员密码获取限时令牌",
        ],
        "action": {} if has_token else {"kind": "request_control", "label": "请求控制权限"},
    })

    # 5) 急停
    st = motion.state()
    if st.get("stopped"):
        checks.append({
            "key": "estop", "ok": False, "level": "error",
            "title": "急停已触发，执行引擎锁定",
            "hint": "所有运动下发都会被拒绝，直到复位急停",
            "steps": ["旋起示教器/控制柜上的急停按钮", "示教器上复位报警", "点「复位急停」"],
            "action": {"kind": "reset_estop", "label": "复位急停"},
        })

    # 6) 安全围栏
    snap = safety_snapshot()
    fstate = (snap or {}).get("state") or "safe"
    if fstate in ("danger", "hit"):
        checks.append({
            "key": "fence", "ok": False, "level": "error" if fstate == "hit" else "warn",
            "title": f"安全围栏：{fstate}",
            "hint": "危险/碰撞状态下禁止下发运动（服务端互锁会拦截）",
            "steps": ["把机器人点动/运行到安全区域", "或调整围栏警戒区配置"],
            "action": {"kind": "none", "label": ""},
        })

    # 7) 真实下发开关（提示性，不算故障）
    real_ok = bool(getattr(motion, "real", False))
    checks.append({
        "key": "real_write", "ok": real_ok, "level": "info",
        "title": "真实下发（点动通道）" if real_ok else "当前为安全模拟执行",
        "hint": "" if real_ok else "下发只做校验与仿真动画，不写控制器寄存器",
        "steps": [] if real_ok else [
            "确认点动链路就绪：控制器 AUTO/远程档、伺服上电、点动服务程序挂起于 WAIT（界面「一键就绪」可自动完成）",
            "设置 motion.real_write=true 且环境变量 EFORT_REAL_MOTION=1，重启后端",
        ],
        "action": {"kind": "none", "label": ""},
    })

    # 8) 示教器档位（L1）
    # ★ level 恒为 info：这是"常态提示"而不是"故障"。
    #   若给 warn，后端一重启（声明不持久化）主提示条就永远是它，反而把真正的
    #   故障挤下去 —— 档位常驻显示在底栏状态灯里，不抢主提示条。
    # ★ 档位优先取**控制器实测**（状态字里的 auto/remote/manual 位）：实测到 AUTO/远程
    #   就算已确认，不必人工声明；只有控制器读不到（未上电/掉线/强制模拟）才需要声明。
    rm = runmode.summary()
    src = rm["source"]
    src_label = {"controller": "控制器实测", "manual": "手动声明"}.get(src, "")
    if rm["confirmed"]:
        title = f"示教器档位：{rm['label']}" + (f"（{src_label}）" if src_label else "")
    else:
        title = "示教器档位未确认（控制器读不到）"
    checks.append({
        "key": "run_mode", "ok": bool(rm["confirmed"] and rm["joggable"]), "level": "info",
        "title": title,
        "hint": (rm["desc"] if rm["confirmed"] else
                 "控制器未上电/未连接，读不到档位；此时按旋钮实际位置手动声明一次即可"),
        "steps": [] if (rm["confirmed"] and rm["joggable"]) else [
            "看一眼示教器上的模式开关实际在哪一档（T1 / T2 / AUTO / REMOTE）",
            "点底栏「示教器」状态灯，按实际档位声明",
            "★ 手动声明不持久化：后端重启后需要重新确认，避免拿旧值误放行"
            "（控制器连上后会自动实测，不用再声明）",
        ],
        "action": {"kind": "run_mode", "label": "声明档位"},
    })

    blocked = [c for c in checks if not c["ok"] and c["level"] in ("error", "warn")]
    order = {"error": 0, "warn": 1, "info": 2}
    blocked.sort(key=lambda c: order.get(c["level"], 3))
    status = "ready" if not blocked else ("blocked" if blocked[0]["level"] == "error" else "degraded")
    return {
        "status": status,
        "host": host, "port": port,
        "connected": bool(collector.connected),
        "simulated": bool(collector.simulated),
        "has_token": has_token,
        "checks": checks,
        "primary": blocked[0] if blocked else None,
        # 底栏常驻状态灯（机器人 / 摄像头 / 示教器档位）——任何情况下都齐全
        "links": _links(host, port, sim_mode),
    }


# ---------------------------------------------------------------- 重连
@router.post("/reconnect")
def api_reconnect(tok: str = Depends(require_control)):
    """手动重连控制器（带鉴权 + 审计留痕）。

    ★ 保留旧的 /api/reconnect 不动（老脚本依赖），这里是带权限的运维入口。
    """
    res = collector.reconnect()
    emit_event("connection", "info" if res.get("connected") else "warn",
               "connection.reconnect",
               ("重连成功，已切回真实链路" if res.get("connected")
                else "重连未成功，仍处于模拟/离线"),
               res, actor=token_role(tok) or "unknown")
    return res


# ---------------------------------------------------------------- 备份导出
@router.get("/export")
def api_export(tok: str = Depends(require_control), db: Session = Depends(get_db)):
    """导出系统配置包（围栏 + 点位 + 程序）。"""
    points = []
    name_of = {}
    for p in crud.list_points(db):
        try:
            joints = json.loads(p.joints or "[0,0,0,0,0,0]")
        except Exception:
            joints = [0, 0, 0, 0, 0, 0]
        try:
            tcp = json.loads(p.tcp or "null")
        except Exception:
            tcp = None
        name_of[p.id] = p.name
        points.append({
            "id": p.id, "name": p.name, "group": p.group, "kind": p.kind,
            "joints": joints, "tcp": tcp, "note": p.note,
        })

    programs = []
    for pr in crud.list_programs(db):
        try:
            items = json.loads(pr.items or "[]")
        except Exception:
            items = []
        enriched = []
        for it in items:
            pid = it.get("point_id")
            enriched.append({
                "point_id": pid,
                "point_name": name_of.get(pid, ""),
                "speed_pct": it.get("speed_pct", 100),
                "dwell_ms": it.get("dwell_ms", 0),
            })
        programs.append({"id": pr.id, "name": pr.name, "note": pr.note, "items": enriched})

    bundle = {
        "format": _EXPORT_FORMAT,
        "exported_at": _now_iso(),
        "service": {"name": SERVICE_NAME, "name_en": SERVICE_NAME_EN, "version": SERVICE_VERSION},
        "safety": load_safety(),
        "points": points,
        "programs": programs,
    }
    emit_event("config", "info", "system.export",
               f"导出系统配置包：{len(points)} 点位 / {len(programs)} 程序",
               {"points": len(points), "programs": len(programs)},
               actor=token_role(tok) or "unknown")
    return bundle


# ---------------------------------------------------------------- 恢复导入
class BundleIn(BaseModel):
    format: str = ""
    safety: Optional[dict] = None
    points: Optional[list] = None
    programs: Optional[list] = None
    # ★ 保留字段：导入始终是"安全合并"——按名称去重更新，绝不删除既有数据。
    #   故意不做 replace(先清空)：运维场景下误选清空导致配置全毁的代价太高。
    mode: str = "merge"


def _norm_joints(v: Any) -> List[float]:
    """点位关节角归一化：接受 [..6] 数组 或 {j1..j6} 对象。"""
    out = [0.0] * 6
    if isinstance(v, list):
        for i in range(min(6, len(v))):
            try:
                out[i] = float(v[i])
            except Exception:
                out[i] = 0.0
    elif isinstance(v, dict):
        for i in range(6):
            try:
                out[i] = float(v.get(f"j{i + 1}", 0.0))
            except Exception:
                out[i] = 0.0
    return out


@router.post("/import")
def api_import(body: BundleIn, tok: str = Depends(require_admin),
               db: Session = Depends(get_db)):
    """导入配置包恢复系统（★ 管理员权限）。

    merge 模式：点位按 (name, group) 去重更新，程序按 name 去重更新，其余新建。
    导入围栏配置前会自动把"当前配置"归档为历史版本，可随时回滚。
    """
    actor = token_role(tok) or "unknown"
    warnings: List[str] = []
    result: Dict[str, Any] = {"safety": False, "warnings": warnings}

    # ---- 安全围栏：先归档现状，再覆盖 ----
    if body.safety:
        try:
            prev = load_safety()
            crud.insert_safety_version(db, json.dumps(prev, ensure_ascii=False),
                                       actor=actor, source="import",
                                       note="导入配置包前自动归档")
            crud.prune_safety_versions(db, keep=30)
            save_safety(body.safety)
            result["safety"] = True
        except Exception as e:
            warnings.append(f"围栏配置导入失败（已保留原配置）：{str(e)[:120]}")

    # ---- 点位 ----
    created = updated = 0
    id_map: Dict[int, int] = {}       # 源 id -> 本地 id
    for raw in (body.points or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "未命名点位")
        group = str(raw.get("group") or "默认")
        joints = json.dumps(_norm_joints(raw.get("joints")))
        tcp = raw.get("tcp")
        tcp_json = json.dumps(tcp) if tcp is not None else "null"
        note = str(raw.get("note") or "")
        kind = str(raw.get("kind") or "joint")

        exist = next((p for p in crud.list_points(db, group=group)
                      if p.name == name), None)
        if exist:
            crud.update_point(db, exist.id, kind=kind, joints_json=joints,
                              tcp_json=tcp_json, note=note)
            id_map[int(raw.get("id") or 0)] = exist.id
            updated += 1
        else:
            new_p = crud.create_point(db, name=name, group=group, kind=kind,
                                      joints_json=joints, tcp_json=tcp_json, note=note)
            id_map[int(raw.get("id") or 0)] = new_p.id
            created += 1
    result["points"] = {"created": created, "updated": updated}

    # ---- 程序（点位 id 重映射：优先按 point_name 找，其次按原 id） ----
    p_created = p_updated = 0
    all_points = crud.list_points(db)
    by_name = {p.name: p.id for p in all_points}
    for raw in (body.programs or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "未命名程序")
        note = str(raw.get("note") or "")
        items_out = []
        for it in (raw.get("items") or []):
            if not isinstance(it, dict):
                continue
            pid = it.get("point_id")
            pname = it.get("point_name")
            new_id = None
            if pname and pname in by_name:
                new_id = by_name[pname]
            elif pid is not None and id_map.get(int(pid)):
                new_id = id_map[int(pid)]
            elif pid is not None and any(p.id == int(pid) for p in all_points):
                new_id = int(pid)
            if new_id is None:
                warnings.append(f"程序「{name}」有条目引用的点位不存在，已跳过该步骤")
                continue
            items_out.append({
                "point_id": new_id,
                "speed_pct": int(it.get("speed_pct", 100) or 100),
                "dwell_ms": int(it.get("dwell_ms", 0) or 0),
            })
        items_json = json.dumps(items_out, ensure_ascii=False)
        exist = next((p for p in crud.list_programs(db) if p.name == name), None)
        if exist:
            crud.update_program(db, exist.id, note=note, items_json=items_json)
            p_updated += 1
        else:
            crud.create_program(db, name=name, note=note, items_json=items_json)
            p_created += 1
    result["programs"] = {"created": p_created, "updated": p_updated}

    emit_event("config", "info", "system.import",
               f"导入配置包：围栏{'✓' if result['safety'] else '✗'}、"
               f"点位 +{created}/~{updated}、程序 +{p_created}/~{p_updated}",
               result, actor=actor)
    return {"ok": True, "result": result}


# ---------------------------------------------------------------- 轻量信息
@router.get("/info")
def api_info():
    cfg = get_config()
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "robot": cfg.robot_model,
        "uptime_sec": int(time.time() - _START),
        "docs": "/docs",
        "ws": "/ws/pose",
    }


# ---------------------------------------------------------------- 只读观测件（E2 / E5′）
@router.get("/metrics/hold-state")
def api_hold_state():
    """★ E5′「固定相机反用法」：最近一轮送检帧的**持件/吸空证据**（三档）+ 历史。

    只读、不要求控制令牌 —— 观测不属于控制。判据唯一实现见
    `app/services/hold_state.py`（同屏同源：这张卡与 3D 里"推断"用的是同一份 state）。
    """
    return hold_state.state()


@router.get("/metrics/platform")
def api_platform_metric():
    """立足面高度（D5′：前端 three/cell.js 与后端 A1 扫掠必须读**同一个数**）。"""
    cfg = get_config()
    safety = cfg.get("safety", default=None) or {}
    hold = cfg.get("hold_state", default=None) or {}
    return {
        "platform_height_mm": float(safety.get("platform_height_mm", 0.0) or 0.0),
        "hold_state_enabled": bool(hold.get("enabled", True)),
        "area_ratio_max": float(hold.get("area_ratio_max", 2.0) or 2.0),
    }


# ---------------------------------------------------------------- ★ B1：实时指标（只读）
def _live(value: Any, unit: str, source: str, desc: str = "",
          note: str = "") -> Dict[str, Any]:
    """请求时刻**直接从真实结构读**的四元组（不经过计数器）。"""
    return {"kind": "live", "value": value, "unit": unit, "ts": _now_iso(),
            "source": source, "measured": value is not None,
            "desc": desc or "读取时刻的真实结构值", "note": note}


@router.get("/metrics")
def api_metrics():
    """★ B1（B 批·可观测性）：进程级实时指标（**只读**，不要求控制令牌）。

    与 `/api/system/health`、`/api/rc-status`、`/metrics/hold-state` 同一条只读
    口径：观测不属于控制。每一项都是「值 + 单位 + 时间戳 + 来源」四元组；
    本进程启动以来没采到过样 ⇒ **value=null + note 如实写「没测」** ——
    「0」与「没测」必须能区分（§5.5 铁律③，绝不假显示）。

    唯一埋点清单见 `app/services/metrics.py` 顶部 docstring；
    ws 在线数/积压、run 登记数这类**没有计数器**的项，改为请求时刻直接读
    真实结构（`hub.count()/hub.pending()`、`control._RUN_ACTIVE`），kind="live"。
    """
    snap = metrics.snapshot()
    uptime = time.time() - _START

    # —— 线程池 / 执行登记：直接读真实结构（唯一事实来源 = api/control.py）——
    from app.api import control as control_api      # 延迟导入：避免模块级循环
    with control_api._RUN_CANCEL_LOCK:
        active_runs = list(control_api._RUN_ACTIVE.keys())
        progress_entries = len(control_api._RUN_PROGRESS)
        last_run_id = control_api._LAST_RUN_ID

    # —— 事件写入速率：计数 ÷ 运行秒数（**自启动以来的平均**，不是滑动窗口 —— 如实标注）
    ev_writes = snap["event.writes"]["value"]
    rate = _live(None, "条/秒", "派生：event.writes ÷ uptime_sec",
                 note="没测：尚无任何事件写入样本")
    if ev_writes:
        rate = _live(round(float(ev_writes) / max(1.0, uptime), 4), "条/秒",
                     "派生：event.writes ÷ uptime_sec",
                     note="自启动以来的平均值，非滑动窗口（没做窗口，就不装作有）")

    return {
        "time": _now_iso(),
        "uptime_sec": int(uptime),
        "groups": {
            "collector": {
                "loop_period_ms": snap["collector.loop_period_ms"],
                "loop_ms": snap["collector.loop_ms"],
                "read_fail_streak": snap["collector.read_fail_streak"],
                "configured_read_hz": _live(
                    float(get_config().sampling.get("read_hz", 10) or 10), "Hz",
                    "config/robot.yaml sampling.read_hz", desc="配置的目标读取频率（不是实测）"),
            },
            "modbus": {
                "read_pose_rt_ms": snap["modbus.read_pose_rt_ms"],
                "read_pose_fail": snap["modbus.read_pose_fail"],
                "read_regs_rt_ms": snap["modbus.read_regs_rt_ms"],
                "read_regs_fail": snap["modbus.read_regs_fail"],
            },
            "ws": {
                "clients_total": _live(hub.count(), "个", "hub.count()",
                                       desc="在线 WS 客户端总数（pose + events 两条流）"),
                "clients_pose": _live(hub.count("pose"), "个", "hub.count('pose')"),
                "clients_events": _live(hub.count("events"), "个", "hub.count('events')"),
                "backlog_clients": _live(hub.pending(), "个", "hub.pending()",
                                         desc="有未发出积压帧的客户端数",
                                         note="背压设计每客户端至多积压 1 帧（新帧覆盖旧帧）"),
                "broadcast_enqueue_ms": snap["ws.broadcast_enqueue_ms"],
                "broadcast_frames": snap["ws.broadcast_frames"],
            },
            "runs": {
                "active_runs": _live(len(active_runs), "个",
                                     "api/control._RUN_ACTIVE（请求时刻直接读）"),
                "active_run_ids": _live(active_runs, "-",
                                        "api/control._RUN_ACTIVE（请求时刻直接读）"),
                "progress_entries": _live(progress_entries, "条",
                                          "api/control._RUN_PROGRESS（含已结束的终态）"),
                "last_run_id": _live(last_run_id, "-", "api/control._LAST_RUN_ID",
                                     note="本进程还没有过执行登记（跑过一次执行后才有值）"
                                          if last_run_id is None else ""),
                "queue_len": _live(None, "个", "不适用",
                                   note="★ 没有队列：/control/run-file 每次执行**直接起守护线程**"
                                        "（见 api/control.py::api_run_file），不存在排队结构"),
                "thread_pool_tokens": _live(
                    int(os.environ.get("EFORT_THREAD_POOL", "96")), "个",
                    "main.py anyio 线程池上限（env EFORT_THREAD_POOL，与启动配置同源）",
                    note="启动时设定的值；anyio 版本差异导致设定失败时实际仍是默认 40"
                         "（以启动日志为准）"),
            },
            "vision": {
                "http_rt_ms": snap["vision.http_rt_ms"],
                "http_calls": snap["vision.http_calls"],
                "http_fail": snap["vision.http_fail"],
                "last_path": snap["vision.last_path"],
                "last_error": snap["vision.last_error"],
            },
            "events": {
                "writes": snap["event.writes"],
                "write_fail": snap["event.write_fail"],
                "write_ms": snap["event.write_ms"],
                "write_rate": rate,
                "backlog": _live(None, "条", "不适用",
                                 note="★ 没有队列：emit 是**同步落库**（见 services/events.py），"
                                      "写入耗时看 write_ms"),
            },
        },
    }


# ---------------------------------------------------------------- ★ E6：机器人自述 · 每天一页现场日报
@router.get("/daily-report")
def api_daily_report(date: Optional[str] = Query(
        None, description="本地日期 YYYY-MM-DD，缺省为今天；时区口径见返回的 timezone 字段")):
    """★ E6 现场日报（**只读**，不需要控制令牌 —— 与 `/api/twin/residual`、
    `/api/sorting/cells` 同口径：观测不属于控制）。

    每个数字都由结构化数据代入固定模板（服务/表/端点级的 `source` 逐节标注）；
    取不到写「无此项数据」，**不塌成 0**；到位自证 / 报警台账 / 一格三证 是
    **本机本次会话**的进程内数据，日报里如实注明。
    零 LLM（本轮先出纯模板版）；零下发（生成日报不是控制动作）。
    返回 `markdown` 为可直接打印的正文，`consistency.inconsistent` 为数字一致性
    自检结果 —— **不为空就必须显示出来**（将来接 LLM 讲解员时这条钉子继续生效）。
    """
    try:
        return daily_report.build_report(date=date)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/daily-report.md")
def api_daily_report_md(date: Optional[str] = Query(None)):
    """同一份日报的纯 markdown 出口（可打印贴墙 / 可归档）。

    Content-Type 明确为 `text/markdown; charset=utf-8`；inline 展示，
    浏览器另存为时文件名 `daily-report-<date>.md`。
    """
    try:
        rep = daily_report.build_report(date=date)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return PlainTextResponse(
        rep["markdown"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 'inline; filename="daily-report-%s.md"' % rep["date"]},
    )
