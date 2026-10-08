# -*- coding: utf-8 -*-
"""
指令预演 (L1, 零风险)。

输入目标(关节角 或 直角坐标) → 逆运动学求解 → 限位/可达校验 → 生成插值轨迹。
★ 本模块只做"计算 + 校验 + 预演轨迹", 不向机器人写入任何数据。
"""
from __future__ import annotations

import math
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.api.auth import require_control, token_role
from app.core import frames_config as fc
from app.core.config import get_config, project_root
from app.db.crud import get_point, list_points, list_programs
from app.db.database import SessionLocal
from app.services import jog_frames as jf
from app.services.collector import collector
from app.services.events import emit as emit_event
from app.services.jog import jog
from app.services.kinematics import (
    fk_matrix, ikine, invert, matrix_to_rpy, pose_of, rpy_to_matrix, tcp_of,
)
# ★ 审计修复 P1-E1：限位的**读取与判定**只有一个实现在 services/limits.py。
#   本文件原先那份 `zip` 版 _in_limits 有两个问题：q 比限位短时后面的轴会被静默
#   漏查；NaN 的比较恒为 False，配合不同的写法会被判成"在限内"。
#   这里只留同名薄封装，调用点一个都不用改 —— 批量改名反而容易漏。
from app.services.limits import in_limits as _check_in_limits
from app.services.limits import load_limits as _load_limits
from app.services.limits import violations as _violations_of
from app.core.safety_const import clamp_speed
from app.services.motion import motion, real_write_enabled
from app.services.runmode import MODES as RUN_MODES
from app.services.runmode import runmode
from app.services.safety_guard import check as guard_check
# ★ 全维度审查 B-12：点位关节角解析的唯一 fail-safe 实现
from app.utils.joints import parse_joints
# ★ 全维度审查：速度/轴锁常量唯一来源
from app.core.safety_const import SPEED_MIN, SPEED_MAX, clamp_speed
from app.core.logger import get_logger

log = get_logger("control")

# ★ 所有控制类接口需持管理员控制令牌（阶段 1 鉴权门）
router = APIRouter(prefix="/api/control", tags=["control"], dependencies=[Depends(require_control)])

# ★ 只读预演路由（**不挂** require_control）：/preview 与 /ik 只做逆解与限位校验，
#   不向机器人写任何数据（响应里 readonly=True）—— 它们属于"看"，不属于"动"。
#
#   为什么必须单开一个 router：FastAPI 的 router 级 dependencies 无法逐路由豁免，
#   而「模拟仿真」页在 frontend/src/tabs.js 里是**公开页**（sim 无 needAuth）：
#   让"只算不发"的预演去要控制令牌，只会让未登录访客的控制台刷满 401，
#   预演按钮本身也永远点不动（历史缺陷 P1-9）。
#   真正的"动"（/move、/run-file、/jog/*、/ready、/estop）仍在上面那个 router 里，
#   一律需要控制令牌 —— 这里放开的是计算，不是下发。
router_ro = APIRouter(prefix="/api/control", tags=["control-readonly"])


def _actor(tok: str) -> str:
    """把令牌翻译成"谁干的"，用于审计留痕。"""
    return token_role(tok) or "unknown"


# ---------- 工具 ----------
def _limits() -> List[dict]:
    # ★ 审计修复 P1-E1：统一由 limits.load_limits() 给出 —— 长度恒为 6、缺项补默认、
    #   单条写坏只影响该条。原实现直接把 config 的原始 list 丢出去，
    #   配置少写一条时 `zip` 会漏查后面的轴。
    return _load_limits()


def _in_limits(q: List[float], limits: List[dict], tol: float = 1e-6) -> bool:
    # ★ 审计修复 P1-E1：判据（长度 → 有限性 → 区间）统一在 limits.in_limits()。
    return _check_in_limits(q, limits, tol)


def _start_joints(current: Optional[List[float]]) -> List[float]:
    if current and len(current) == 6:
        return [float(v) for v in current]
    p = collector.get_latest()
    if p:
        return [float(p[f"j{i}"]) for i in range(1, 7)]
    return [0.0] * 6


def _ik_best(target, q0: List[float], limits: List[dict], rot_weight: float) -> dict:
    """多初值 IK：优先取"收敛 + 在限位内 + 误差最小"的解。"""
    best = None
    for c0 in (q0, [0.0] * 6, [180.0, 0, 0, 0, 0, 0], [0, 0, 0, 180.0, 0, 0]):
        r = ikine(target, c0, limits, rot_weight=rot_weight)
        ok_lim = _in_limits(r["joints"], limits)
        score = (0 if (r["ok"] and ok_lim) else 1, r["pos_err"])
        if best is None or score < best[0]:
            best = (score, r)
    return best[1]


def _solve_tcp(tcp: dict, current: Optional[List[float]] = None,
               keep_orientation: bool = True):
    """直角坐标 -> 关节角（含限位判定）。返回 (joints, solver, violations)。"""
    limits = _limits()
    q0 = _start_joints(current)
    T_cur = fk_matrix(q0)

    target = np.eye(4)
    target[:3, 3] = [float(tcp.get("x", 0)), float(tcp.get("y", 0)), float(tcp.get("z", 0))]
    pose_given = all(tcp.get(k) is not None for k in ("rx", "ry", "rz"))
    if pose_given:
        target[:3, :3] = rpy_to_matrix(tcp["rx"], tcp["ry"], tcp["rz"])
        rw = 150.0
    elif keep_orientation:
        target[:3, :3] = T_cur[:3, :3]
        rw = 150.0
    else:
        rw = 0.0          # 仅约束位置，姿态自由（收敛更稳）

    r = _ik_best(target, q0, limits, rw)
    q = r["joints"]
    # ★ 审计修复 P1-E1：这里原本是第二份手写循环，与 _in_limits 各查各的 ——
    #   两边判据不同（这份没有 NaN 检查、也没有容差），于是同一个 IK 结果可以
    #   同时给出 `solver.in_limits=True` 和一条非空 violations。
    #   现在与 _in_limits 共用同一份代码，两者恒一致。
    violations = _violations_of(q, limits)
    solver = {
        "type": "ik", "ok": r["ok"], "iters": r["iters"],
        "pos_err_mm": r["pos_err"], "rot_err_deg": r["rot_err_deg"],
        "in_limits": not violations,
    }
    return q, solver, violations


# ---------- Schemas ----------
class TcpTarget(BaseModel):
    x: float
    y: float
    z: float
    rx: Optional[float] = None   # 末端姿态(度, ZYX); 三者都缺省则沿用当前姿态
    ry: Optional[float] = None
    rz: Optional[float] = None


class PreviewIn(BaseModel):
    mode: str = "joint"                    # joint | cartesian
    joints: Optional[List[float]] = None   # 关节模式目标(度)
    tcp: Optional[TcpTarget] = None        # 直角模式目标(mm / deg)
    current: Optional[List[float]] = None  # 起点(缺省用当前实时姿态)
    duration_ms: int = Field(default=2500, ge=100, le=60000)
    steps: int = Field(default=60, ge=2, le=600)


# ---------- 端点 ----------
@router.get("/limits")
def api_limits():
    cfg = get_config()
    return {
        "joints": _limits(),
        "reach_mm": cfg.get("robot", "reach_mm", default=712),
        "readonly": True,   # 明确本接口只用于预演, 不下发
    }


def _compute_preview(mode: str = "joint", joints: Optional[List[float]] = None,
                     tcp: Optional[dict] = None, current: Optional[List[float]] = None,
                     duration_ms: int = 2500, steps: int = 60) -> dict:
    """指令预演核心：目标求解 → 限位/可达校验 → 插值轨迹。★ 只算不发。

    被 /preview 与 /run-file（试运行）复用，保证"界面预演"与"空跑测试"用同一套判据。
    """
    limits = _limits()
    q0 = _start_joints(current)

    result: dict = {
        "ok": False, "mode": mode,
        "start": [round(v, 3) for v in q0],
        "warnings": [], "violations": [], "solver": None,
        "readonly": True,
    }

    # ---- 求目标关节角 ----
    if mode == "joint":
        if not joints or len(joints) != 6:
            return {**result, "error": "关节模式需提供 6 个目标关节角"}
        q_target = [float(v) for v in joints]
        result["solver"] = {"type": "direct", "ok": True}
    elif mode == "cartesian":
        if tcp is None:
            return {**result, "error": "直角模式需提供 TCP 目标"}
        T_cur = fk_matrix(q0)
        target = np.eye(4)
        target[:3, 3] = [tcp.get("x", 0.0), tcp.get("y", 0.0), tcp.get("z", 0.0)]
        pose_given = all(tcp.get(k) is not None for k in ("rx", "ry", "rz"))
        if pose_given:
            target[:3, :3] = rpy_to_matrix(tcp["rx"], tcp["ry"], tcp["rz"])
            rw = 150.0
        else:
            target[:3, :3] = T_cur[:3, :3]
            rw = 0.0     # 未指定姿态: 仅约束位置
            result["warnings"].append("未指定姿态, 仅约束末端位置")
        r = _ik_best(target, q0, limits, rw)
        q_target = r["joints"]
        result["solver"] = {
            "type": "ik", "ok": r["ok"], "iters": r["iters"],
            "pos_err_mm": r["pos_err"], "rot_err_deg": r["rot_err_deg"],
            "in_limits": _in_limits(q_target, limits),
        }
        if not r["ok"]:
            result["warnings"].append("IK 未完全收敛(位置误差 %.2f mm)" % r["pos_err"])
        if not _in_limits(q_target, limits):
            result["warnings"].append("IK 解超出关节限位(可尝试换目标点)")
    else:
        return {**result, "error": "未知模式: " + mode}

    # ---- 限位校验 ----
    # ★ 审计修复 P1-E1：与 _in_limits 共用同一份判据 —— 结果里的 `ok`（用 violations
    #   算）和 warnings（用 _in_limits 算）不再可能互相矛盾。
    violations = _violations_of(q_target, limits)
    result["violations"] = violations
    result["target"] = [round(v, 3) for v in q_target]

    # ---- 可达性 / 位移 ----
    tcp_start = tcp_of(q0)
    tcp_end = tcp_of(q_target)
    dist = math.dist(tcp_start, tcp_end)
    reach = float(get_config().get("robot", "reach_mm", default=712))
    result["tcp_start"] = [round(v, 1) for v in tcp_start]
    result["tcp_end"] = [round(v, 1) for v in tcp_end]
    result["distance_mm"] = round(dist, 1)
    if dist > reach * 2:
        result["warnings"].append("末端位移较大(%.0f mm), 请确认轨迹中间无干涉" % dist)

    # ---- 插值轨迹 (ease-in-out) ----
    traj = []
    for k in range(steps + 1):
        f = k / steps
        s = 0.5 - 0.5 * math.cos(math.pi * f)
        frame = {"t": int(duration_ms * f)}
        for i in range(6):
            frame[f"j{i + 1}"] = round(q0[i] + (q_target[i] - q0[i]) * s, 3)
        traj.append(frame)
    result["trajectory"] = traj
    result["duration_ms"] = duration_ms
    result["ok"] = len(violations) == 0 and bool(result.get("solver", {}).get("ok"))
    return result


@router_ro.post("/preview")
def api_preview(body: PreviewIn):
    """指令预演：只算不发（无控制令牌亦可调用）。"""
    return _compute_preview(body.mode, body.joints,
                            body.tcp.model_dump() if body.tcp else None,
                            body.current, body.duration_ms, body.steps)


class IkIn(BaseModel):
    """轻量逆运动学：给定目标 TCP 位置，返回关节角（供"机器人模式"实时操控）。"""
    tcp: TcpTarget
    current: Optional[List[float]] = None  # 起点关节角(缺省用当前实时姿态)
    keep_orientation: bool = True          # True 保持当前姿态; False 仅约束位置


class FkIn(BaseModel):
    """正运动学：给定 6 关节角，返回末端位姿(x,y,z mm + rx,ry,rz deg)。

    ★ 与 /ik 是**同一套约定**（kinematics.pose_of ↔ _solve_tcp/rpy_to_matrix），
      前端"机器人坐标系"六个滑动条(位置 X/Y/Z + 姿态 A/B/C)的当前值就取自这里 ——
      避免前端自己算一套欧拉角，两边约定不一致会导致"拖动方向莫名其妙"。
    """
    joints: List[float] = Field(..., min_length=6, max_length=6)


@router_ro.post("/fk")
def api_fk(body: FkIn):
    """只算不发：关节角 → 末端位姿。供"机器人坐标系"六维滑块的当前值。"""
    p = pose_of([float(v) for v in body.joints])
    return {"ok": True, **p, "readonly": True}


@router_ro.post("/ik")
def api_ik(body: IkIn):
    """机器人模式操控：末端沿 X/Y/Z 平移（或给定姿态绕 X/Y/Z 转），反解 6 关节角。
    只算不发，零风险（无控制令牌亦可调用）。"""
    q, solver, violations = _solve_tcp(body.tcp.model_dump(), body.current,
                                       body.keep_orientation)
    return {
        "ok": bool(solver["ok"]) and not violations,
        "joints": [round(v, 4) for v in q],
        "pos_err_mm": solver["pos_err_mm"],
        "rot_err_deg": solver["rot_err_deg"],
        "in_limits": not violations,
        "violations": violations,
        "readonly": True,
    }


@router_ro.get("/programs")
def api_programs_ro():
    """只读：programs 目录下的可执行程序文件名（模拟仿真页用来选 XPL）。

    ★ 只回**文件名**，不回内容也不回绝对路径以外的信息；公开只读是刻意的 ——
      模拟仿真页是公开页，读一份程序清单本身不产生任何运动。
    """
    base = _program_dir()
    try:
        names = sorted(
            n for n in os.listdir(base)
            if n.lower().endswith((".xpl", ".json")) and os.path.isfile(os.path.join(base, n))
        )
    except Exception:
        names = []
    return {"dir": base, "items": names, "readonly": True}


@router_ro.get("/file-source")
def api_file_source(name: str = Query(..., min_length=1, max_length=200)):
    """只读：读 programs 目录下某个程序文件的**文本内容**（供仿真页预览/解析）。

    ★ 路径穿越防护复用 `_safe_file`（只认文件名、commonpath 必须落在 programs 内），
      不另写一份 —— 两份判据必然漂移，那是目录穿越漏洞的标准成因。
    ★ 只读：本接口不解析、不校验、更不下发；真正执行仍走 /run-file（需控制令牌）。
    """
    p = _safe_file(name)
    if not p:
        raise HTTPException(404, "文件不存在或不在程序目录内")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(512 * 1024)   # 512KB 上限，防止把超大文件整个拖进浏览器
    except OSError as e:
        raise HTTPException(500, "读取失败：%s" % e)
    return {"ok": True, "name": os.path.basename(p), "text": text, "readonly": True}


# =====================================================================
# 执行引擎 (阶段 2): 真实下发受 require_control 鉴权门保护
# =====================================================================
class MoveIn(BaseModel):
    """执行一次目标到位: 直接传 joints, 或传 point_id 用预设点位的关节角。"""
    joints: Optional[List[float]] = None
    point_id: Optional[int] = None
    # ★ 速度硬下限 5%（SPEED_MIN）：无论前端/调用方传什么，都拒绝低于 5% 的请求，
    #   杜绝"很快"的误发。默认也取 5，没传 speed 时就按最慢执行。
    speed_pct: int = Field(default=SPEED_MIN, ge=SPEED_MIN, le=SPEED_MAX)
    dwell_ms: int = Field(default=0, ge=0, le=60000)

    @model_validator(mode="after")
    def _must_have_target(self):
        """★ 全维度审查 B-11：空 body（既不传 joints 也不传 point_id）原本会走到
        `[float(x) for x in target]` 对 None 迭代 → 500 INTERNAL_ERROR，
        且审计事件也没写。这里在入参层就明确报 422。"""
        if not self.joints and self.point_id is None:
            raise ValueError("必须提供 joints 或 point_id 之一")
        if self.joints is not None and len(self.joints) != 6:
            raise ValueError("joints 必须为 6 个关节角")
        return self


@router.post("/move")
def api_move(body: MoveIn, tok: str = Depends(require_control)):
    target = body.joints
    if target is None and body.point_id is not None:
        s = SessionLocal()
        try:
            p = get_point(s, body.point_id)
        finally:
            s.close()
        if not p:
            raise HTTPException(404, "点位不存在")
        # ★ 全维度审查 B-12：点位 joints 解析失败一律**拒绝下发**。
        #   原实现 `except: target = [0,0,0,0,0,0]` 会把机器人送向全零位。
        try:
            target = parse_joints(p.joints, where=f"点位「{p.name}」")
        except ValueError as e:
            raise HTTPException(400, str(e) + "，已拒绝下发")
    # ---------- 围栏互锁（阶段 4）----------
    # 前端会在 SafetyPanel 里实时算余量并上报；这里做"服务端兜底"，
    # 就算有人绕过界面直接调 API，危险状态下也发不下去。
    ok, reason, snap = guard_check()
    if not ok:
        emit_event("control", "warn", "control.move_blocked",
                   f"下发被拦截：{reason}",
                   {"reason": reason, "state": snap.get("state"),
                    "target": target}, actor=_actor(tok))
        raise HTTPException(409, detail=f"安全围栏互锁：{reason}")
    if reason.startswith("未收到") or reason.startswith("围栏状态已过期"):
        # 互锁未生效（典型情况：没开界面 / 只跑后台脚本）—— 记一条提示，便于事后追责
        emit_event("control", "warn", "control.interlock_absent",
                   f"围栏互锁未生效仍下发：{reason}",
                   {"target": target}, actor=_actor(tok))

    # ★ 全维度审查 B-13：真机下发前补档位检查。
    #   原实现只在点动（jog._block_reason）里有档位判据，/move 与 /run-file 没有
    #   → T1/T2 档下寄存器写得进去但机器人不动，接口仍返回 ok:true（假成功）。
    if real_write_enabled():
        mok, mwhy, _mst = runmode.check_jog()
        if not mok:
            emit_event("control", "warn", "control.move_blocked",
                       f"档位不允许下发：{mwhy}",
                       {"target": target}, actor=_actor(tok))
            raise HTTPException(409, detail=f"档位不允许下发：{mwhy}")

    res = motion.command([float(x) for x in target], clamp_speed(body.speed_pct),
                         body.dwell_ms)
    emit_event("control", "info" if res.get("ok") else "warn",
               "control.move",
               f"下发目标到位（{res.get('mode', '?')}）" if res.get("ok")
               else f"下发失败：{res.get('error', '未知原因')}",
               {"target": target, "speed_pct": body.speed_pct,
                "dwell_ms": body.dwell_ms, "mode": res.get("mode"),
                "point_id": body.point_id}, actor=_actor(tok))
    return res


# =====================================================================
# 单文件执行 (阶段 5): 输入文件名 → 解析 → 试运行测试 / 真实下发
#
# 解析顺序：数据库执行程序(按名) → 数据库预设点位(按名) → programs 目录下 JSON 文件。
# dry_run=True 即"空跑测试"：逐步预演并校验限位/可达/IK，绝不下发；
# dry_run=False 才真正下发，且每一步都过一次围栏互锁。
# =====================================================================
PROGRAM_DIR_ENV = "EFORT_PROGRAM_DIR"


def _program_dir() -> str:
    """可执行程序文件目录（默认 <项目根>/programs），可用环境变量覆盖。"""
    d = os.environ.get(PROGRAM_DIR_ENV) or os.path.join(project_root(), "programs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _safe_file(name: str) -> Optional[str]:
    """文件名 → 目录内绝对路径；拒绝路径穿越，文件不存在返回 None。
    支持 .json 与 .xpl 两种可执行程序文件。"""
    base = os.path.abspath(_program_dir())
    n = (name or "").strip().replace("\\", "/")
    n = os.path.basename(n)                 # 只认文件名，丢弃任何目录片段
    if not n or n in (".", "..") or ".." in n:
        return None
    if n.lower().endswith(".json") or n.lower().endswith(".xpl"):
        p = os.path.abspath(os.path.join(base, n))
    else:
        # 无扩展名时先照抄原名(可不区分大小写命中 .XPL)，再试 .xpl / .json
        p = os.path.abspath(os.path.join(base, n))
        if not os.path.isfile(p):
            p = os.path.abspath(os.path.join(base, n + ".xpl"))
        if not os.path.isfile(p):
            p = os.path.abspath(os.path.join(base, n + ".json"))
    try:
        if os.path.commonpath([p, base]) != base:
            return None
    except ValueError:                      # 不同盘符时 commonpath 抛错
        return None
    return p if os.path.isfile(p) else None


def _norm_name(s: str) -> str:
    """归一化匹配名：去空白、转小写、去掉 .json/.xpl 后缀。"""
    n = (s or "").strip().lower()
    if n.endswith(".json"):
        n = n[:-5]
    elif n.endswith(".xpl"):
        n = n[:-4]
    return n


def _load_disk(path: str):
    """读取磁盘上的程序 / 点位文件 → (kind, name, items)。
    .json 为原始 JSON；.xpl 为标准文本脚本（_parse_xpl）。"""
    if path.lower().endswith(".xpl"):
        return _parse_xpl(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("文件内容必须是 JSON 对象")
    name = data.get("name") or os.path.splitext(os.path.basename(path))[0]
    if isinstance(data.get("items"), list):
        return "program", name, data["items"]
    if isinstance(data.get("steps"), list):
        return "program", name, data["steps"]
    if isinstance(data.get("joints"), list) or isinstance(data.get("tcp"), dict):
        return "point", name, [data]
    raise ValueError("文件既不是程序(items/steps) 也不是点位(joints/tcp)")


def _parse_xpl(path: str):
    """解析标准 XPL 文本脚本 → (kind, name, items)。

    支持指令（大小写不敏感，`#`/`;`/`//` 视为注释）：
      MOVE x y z | MOVEP x y z | MOVL x y z      → 直角坐标移动（mm）
      MOVEJ j1 j2 j3 j4 j5 j6                    → 关节角移动（度）
      SUCK ON / SUCK OFF | 吸气 / 放气           → 吸盘状态（io 步）
      GRIP ON / GRIP OFF                         → 同上（别名）
      WAIT ms | DELAY ms | 等待 ms               → 停留（io 步）
    未知指令被标记为 op=unknown，解析不崩溃、校验会报具体行号。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        raise ValueError(f"读取 XPL 失败：{e}")
    base = os.path.splitext(os.path.basename(path))[0]
    # 示教器导出的真程序是 XML 格式（<Xpl-source>/<mjoint>/<set> 等），走专用解析器
    if raw.lstrip().startswith("<") and "<?xml" in raw[:80].lower():
        return _parse_xpl_xml(raw, base)
    items: List[dict] = []
    line_no = 0
    for line in raw.splitlines():
        line_no += 1
        s = line.split("#")[0].split(";")[0].split("//")[0].strip()
        if not s:
            continue
        toks = s.split()
        cmd = toks[0].upper()

        # ★ 审计修复 P1-E13（ruff B023）：`_f` 是在 for 循环体内定义的函数，
        #   按 Python 的闭包语义它捕获的是**循环变量 `toks` 本身**，不是这一刻的值。
        #   目前 `_f` 都在同一次迭代里被同步调用，所以现在没有 bug —— 但只要有人
        #   将来把它存进 items 再延后调用，读到的就是下一行的 toks（串行）。
        #   用默认参数把"这一拍的 toks"钉死，彻底断掉这类隐患。
        def _f(i, _toks=toks):
            try:
                return float(_toks[i])
            except Exception:
                return None

        if cmd in ("MOVE", "MOVEP", "MOVL", "MOV"):
            x, y, z = _f(1), _f(2), _f(3)
            if None in (x, y, z):
                items.append({"op": "unknown", "raw": s, "line": line_no})
            else:
                items.append({"tcp": {"x": x, "y": y, "z": z}})
        elif cmd in ("MOVEJ", "MOVJ"):
            vals = [_f(i) for i in range(1, 7)]
            if None in vals or len(vals) != 6:
                items.append({"op": "unknown", "raw": s, "line": line_no})
            else:
                items.append({"joints": vals})
        elif cmd in ("SUCK", "GRIP") or cmd in ("吸气", "放气"):
            arg = (toks[1].upper() if len(toks) > 1 else "")
            on = arg in ("ON", "1", "OPEN", "开") or cmd == "吸气"
            items.append({"op": "suck", "on": on})
        elif cmd in ("WAIT", "DELAY", "等待"):
            ms = _f(1)
            if ms is None:
                items.append({"op": "unknown", "raw": s, "line": line_no})
            else:
                items.append({"op": "wait", "dwell_ms": max(0, int(ms))})
        else:
            items.append({"op": "unknown", "raw": s, "line": line_no})
    if not items:
        raise ValueError("XPL 文件没有任何可执行指令")
    return "program", base, items


def _parse_xpl_xml(raw: str, base: str):
    """解析示教器导出的 XML 程序（<Xpl-source>）→ (kind, name, items)。

    识别以下指令（转为与简化文本同构的 items，供 _steps_from 消费）：
      <mjoint><target>POINTJ(6 角)</target>        → {'joints': [...]}
      <mjoint><target>POINTC(x,y,z,a,b,c,...)</target> → {'tcp': {...}}（直角+姿态）
      <mjoint><speed>vNNperc</speed>                 → 附到上一条的 speed_pct
      <set><dest>io.DOut[K]</dest><expr>bool</expr>  → 吸气/放气（true=吸气 DOut 使能）
      <mline>/<mlinep>/<movel>/movej 等其它移动     → 尝试解析 POINTJ/POINTC，未知标 unknown
    无法识别的元素映射为 op=unknown，解析不崩溃。
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"XML 解析失败：{e}")

    items: List[dict] = []

    def _fnum(s, idx):
        try:
            return float(s[idx])
        except Exception:
            return None

    def _parse_target(el):
        """从 <mjoint>/<mline> 的 target 文本解析 POINTJ/POINTC → items 或 None。
        ★ 不写死 speed_pct：让后端 fallback 到 body.speed_pct（前端全局速度），
          否则 XML 程序自带的 v100perc 会压掉用户在前端调的档位。"""
        tgt = el.findtext("target") or ""
        tgt = tgt.strip()
        if tgt.upper().startswith("POINTJ("):
            inner = tgt[tgt.find("(") + 1: tgt.rfind(")")]
            bits = [x.strip() for x in inner.split(",")]
            vals = [_fnum(bits, i) for i in range(6)]
            if None not in vals:
                items.append({"joints": vals})
                return
        elif tgt.upper().startswith("POINTC("):
            inner = tgt[tgt.find("(") + 1: tgt.rfind(")")]
            bits = [x.strip() for x in inner.split(",")]
            xyz = [_fnum(bits, i) for i in range(3)]
            if None not in xyz:
                st = {"tcp": {"x": xyz[0], "y": xyz[1], "z": xyz[2]}}
                # 姿态角 A/B/C（粗-细角），若能解析则并入 tcp 供前端展示
                abc = [_fnum(bits, i) for i in range(3, 6)]
                if None not in abc:
                    st["tcp"]["a"], st["tcp"]["b"], st["tcp"]["c"] = abc
                items.append(st)
                return
        items.append({"op": "unknown", "raw": tgt or "<mjoint>", "line": 0})

    for el in root.iter():
        tag = (el.tag or "").lower()
        if tag in ("mjoint", "mjointp", "mline", "mlinep", "movel", "movej"):
            _parse_target(el)
        elif tag == "set":
            dest = el.findtext("dest") or ""
            expr = (el.findtext("expr") or "").strip().lower()
            # 吸真空：DOut[K]=true 吸气头 / false 放气头
            if dest.upper().startswith("IO.DOUT["):
                items.append({"op": "suck", "on": expr in ("true", "1", "on"),
                              "raw": f"{dest}={expr}", "line": 0})
            else:
                items.append({"op": "unknown", "raw": f"{dest}={expr}", "line": 0})
        elif tag in ("wait", "delay"):
            try:
                ms = int(float(el.text or 0))
                items.append({"op": "wait", "dwell_ms": max(0, ms)})
            except Exception:
                items.append({"op": "wait", "dwell_ms": 0})
        elif tag in ("rem", "info", "title", "text", "body", "pou", "name",
                     "hostenvironment", "xplenvironment", "config", "fileid",
                     "target", "speed", "zone", "tool", "dest", "expr", "description",
                     "version", "author", "mjoint", "xpl-source", "pous"):
            pass  # 结构 / 注释 / 属性标签：无动作
    if not items:
        raise ValueError("XML 程序不包含任何可执行指令")
    return "program", base, items


def _steps_from(items, db) -> List[dict]:
    """把步骤描述(point_id / joints / tcp)归一化为可执行步骤。"""
    out: List[dict] = []
    for i, it in enumerate(items or []):
        idx = i + 1
        if not isinstance(it, dict):
            out.append({"index": idx, "ok": False, "error": "步骤格式非法"})
            continue
        # ★ P1-B1：步骤未显式指定速度时用 None（= 由调用方/请求决定）。
        #   原实现缺省写死 100，导致 run-file 请求的 speed_pct=5 被完全覆盖，
        #   真实执行恒 100% —— 这与"速度硬下限 5%"的安全承诺直接冲突。
        sp = int(it["speed_pct"]) if it.get("speed_pct") else None
        dw = int(it.get("dwell_ms") or 0)
        pid = it.get("point_id", it.get("point"))
        if pid is not None and str(pid).strip() != "":
            row = None
            if str(pid).isdigit():
                row = get_point(db, int(pid))
            if not row:
                out.append({"index": idx, "ok": False, "error": f"点位 #{pid} 不存在"})
                continue
            try:
                joints = parse_joints(row.joints, where=f"点位「{row.name}」")
            except ValueError as e:
                out.append({"index": idx, "name": row.name, "point_id": row.id,
                            "ok": False, "error": str(e)})
                continue
            st = {"index": idx, "name": row.name, "point_id": row.id,
                  "mode": "joint", "joints": joints, "speed_pct": sp, "dwell_ms": dw}
            if row.kind == "cartesian":
                try:
                    t = json.loads(row.tcp or "null")
                except Exception:
                    t = None
                if isinstance(t, dict):
                    st["mode"] = "cartesian"
                    st["tcp"] = t
            out.append(st)
            continue
        if isinstance(it.get("tcp"), dict):
            out.append({"index": idx, "name": it.get("name") or f"步骤{idx}",
                        "mode": "cartesian", "tcp": it["tcp"],
                        "speed_pct": sp, "dwell_ms": dw})
        elif isinstance(it.get("joints"), list) and len(it["joints"]) == 6:
            out.append({"index": idx, "name": it.get("name") or f"步骤{idx}",
                        "mode": "joint", "joints": [float(x) for x in it["joints"]],
                        "speed_pct": sp, "dwell_ms": dw})
        elif it.get("op"):   # io 步骤：吸气 / 停止吸气 / 等待（可校验，无移动目标）
            # ★ 2026-09-29 序列编辑器：op 归一化为三值 suck / release / wait，
            #   与界面上的五类操作一一对应（wait 的秒数由 dwell_ms 承载）。
            #   · 显式 {"op":"suck"} / {"op":"release"}
            #   · 兼容 XPL 解析器产出的 {"op":"suck","on":false}（= 停止吸气）
            _raw = str(it.get("op") or "").strip().lower()
            if _raw == "suck":
                op = "suck" if it.get("on") is not False else "release"
            elif _raw in ("release", "unsuck", "stop_suck"):
                op = "release"
            elif _raw in ("blow", "blowoff", "purge"):
                op = "blow"
            elif _raw in ("unblow", "stop_blow"):
                op = "unblow"
            elif _raw in ("wait", "dwell"):
                op = "wait"
            else:
                op = "unknown"
            label = {"suck": "吸", "release": "停止吸",
                     "blow": "放", "unblow": "停止放", "wait": "等待"}.get(op, _raw)
            out.append({"index": idx, "name": it.get("name") or label,
                        "ok": op != "unknown", "op": op, "dwell_ms": dw,
                        "error": None if op != "unknown" else ("未知操作类型：%s" % _raw),
                        "raw": it.get("raw"), "line": it.get("line")})
        else:
            out.append({"index": idx, "ok": False,
                        "error": "步骤缺少 point_id / joints / tcp"})
    return out


def _point_steps(row) -> List[dict]:
    """数据库点位 → 单步列表。"""
    try:
        joints = parse_joints(row.joints, where=f"点位「{getattr(row, 'name', '')}」")
    except ValueError as e:
        # ★ B-12：不返回全零位姿；让上层看到明确失败
        return [{"index": 1, "name": getattr(row, "name", ""),
                 "point_id": getattr(row, "id", None),
                 "ok": False, "error": str(e)}]
    # ★ P1-B1：速度留空（None）→ 由 run-file 请求的 speed_pct 决定并夹取，
    #   不再硬写 100（原实现让"请求 5%"永远无效）。
    st = {"index": 1, "name": row.name, "point_id": row.id,
          "mode": "joint", "joints": joints, "speed_pct": None, "dwell_ms": 0}
    if row.kind == "cartesian":
        try:
            t = json.loads(row.tcp or "null")
        except Exception:
            t = None
        if isinstance(t, dict):
            st["mode"] = "cartesian"
            st["tcp"] = t
    return [st]


def _prog_no(name: str) -> Optional[int]:
    """从 .XPL 文件名提取程序号（200.XPL → 200、411.XPL → 411）。

    ★ 程序号是**文件名里明写的事实**，不是猜出来的 —— 示教器程序以数字编号。
      无数字前缀（如 foo.XPL）返回 None，界面按"无程序号"处理，不凭空编一个。
    """
    digits = ""
    for ch in os.path.basename(name or ""):
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def _candidates(db) -> List[dict]:
    """可执行候选（供界面分组展示 / 下拉提示 / 未命中时回显）。

    ★ 两类候选，前端据此分栏：
      group="teach"  → 示教器程序：programs 目录下的 .XPL 文件（200/JOGSVC、411 等，
                       示教器导出、在控制器上可执行的程序代码）；
      group="local"  → 本地程序：数据库点位序列程序 + 预设点位 + programs 目录下的 .json。
    """
    out: List[dict] = []
    for r in list_programs(db):
        try:
            n = len(json.loads(r.items or "[]"))
        except Exception:
            n = 0
        out.append({"kind": "program", "group": "local", "name": r.name,
                    "id": r.id, "steps": n})
    for r in list_points(db):
        out.append({"kind": "point", "group": "local", "name": r.name,
                    "id": r.id, "steps": 1})
    try:
        for fn in sorted(os.listdir(_program_dir())):
            low = fn.lower()
            # .XPL = 示教器（控制器）程序；.json = 本地 JSON 程序。两类分开喂给前端。
            if low.endswith(".xpl"):
                out.append({"kind": "file", "group": "teach", "name": fn,
                            "no": _prog_no(fn), "steps": None, "file_ext": "xpl"})
            elif low.endswith(".json"):
                out.append({"kind": "file", "group": "local", "name": fn,
                            "steps": None, "file_ext": "json"})
    except Exception:
        pass
    return out


def _resolve_file(filename: str, db):
    """按文件名解析可执行目标 → dict(kind/name/source/steps) 或 None。"""
    q = _norm_name(filename)
    if not q:
        return None

    # 1) 数据库执行程序：先精确后包含
    progs = list_programs(db)
    hit = next((r for r in progs if _norm_name(r.name) == q), None)
    if hit is None:
        hit = next((r for r in progs if q in _norm_name(r.name)), None)
    if hit is not None:
        try:
            items = json.loads(hit.items or "[]")
        except Exception:
            items = []
        return {"kind": "program", "name": hit.name, "id": hit.id,
                "source": "database", "steps": _steps_from(items, db)}

    # 2) 数据库预设点位：先精确后包含
    pts = list_points(db)
    hitp = next((r for r in pts if _norm_name(r.name) == q), None)
    if hitp is None:
        hitp = next((r for r in pts if q in _norm_name(r.name)), None)
    if hitp is not None:
        return {"kind": "point", "name": hitp.name, "id": hitp.id,
                "source": "database", "steps": _point_steps(hitp)}

    # 3) 磁盘文件（programs 目录）
    path = _safe_file(filename)
    if path:
        try:
            kind, name, items = _load_disk(path)
        except Exception as e:
            return {"kind": "file", "name": os.path.basename(path),
                    "source": "file", "path": path, "steps": [],
                    "error": f"文件解析失败：{e}"}
        return {"kind": kind, "name": name, "source": "file",
                "path": os.path.basename(path), "steps": _steps_from(items, db)}
    return None


class RunFileIn(BaseModel):
    # ★ 2026-09-29 序列编辑器：filename 与 items **二选一**。
    #   · 给了 filename → 按文件执行（以文件为准，忽略 items，避免"界面显示 A、实际跑 B"）
    #   · 只给 items   → 直接跑编辑器里正在编的序列（不落盘也能试跑）
    filename: str = Field(default="", max_length=200)
    items: Optional[List[dict]] = None
    name: Optional[str] = Field(default=None, max_length=60)   # items 模式下的显示名
    dry_run: bool = True                     # ★ 默认空跑测试，不下发
    # ★ 速度硬下限 5%：同 MoveIn，杜绝"很快"的误发。
    speed_pct: int = Field(default=5, ge=5, le=100)
    steps: int = Field(default=40, ge=2, le=600)
    # ★ 审计修复 P0-7：客户端可指定本次执行的 run_id，用于"中止"接口精确匹配。
    #   不传则由后端生成（此时前端仍可用 /control/run-cancel 按文件名中止，
    #   见 api_run_cancel 的 fallback）。
    run_id: Optional[str] = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _need_target(self):
        has_file = bool((self.filename or "").strip())
        has_items = isinstance(self.items, list) and len(self.items) > 0
        if not has_file and not has_items:
            raise ValueError("必须给 filename（按文件执行）或 items（直接跑编辑器序列）")
        if not has_file and len(self.items) > SEQ_MAX_ITEMS:
            raise ValueError("步骤数超过上限 %d" % SEQ_MAX_ITEMS)
        return self


# =====================================================================
# ★ 审计修复 P0-7：程序执行的"中止"机制。
#   原实现前端 abortRun() 只把 runAbort 标志置 true，**后端根本不知道** ——
#   界面显示"已中止"，机器人却把整份文件跑完。这在真机上是严重安全事故。
#   现在：前端调 POST /control/run-cancel → 后端在**每一步之前**检查取消表 →
#   命中就 break 并回传 cancelled=true。
#   说明：取消表是进程级内存表（重启即清）。单机部署足够；将来若做多实例，
#   应换成 Redis/pub-sub，接口语义保持不变。
# =====================================================================
_RUN_CANCEL: Dict[str, float] = {}      # run_id -> 请求取消的时间戳
_RUN_CANCEL_LOCK = threading.Lock()
_RUN_CANCEL_TTL = 3600.0                # 中止标记保留 1h，防止孤儿条目堆积
_RUN_ACTIVE: Dict[str, Dict] = {}       # run_id -> {"filename":..., "started_at":...}
# ★ 2026-09-29 序列编辑器：暂停开关与进度登记（与取消表共用同一把锁）
#   _RUN_PAUSE : run_id -> Event（set = 已暂停）。执行循环在**步边界**与**等待步**上读取；
#                点动/吸放步本身不中断（控制器正在执行 MJOINT / 写 IO），
#                暂停在该步结束后生效 —— 界面必须如实写明（要立刻停用急停）。
#   _RUN_PROGRESS : run_id -> 进度快照，供 GET /control/run-state 轮询。
_RUN_PAUSE: Dict[str, "threading.Event"] = {}
_RUN_PROGRESS: Dict[str, Dict] = {}
# ★ 最近一次执行的 run_id。执行结束后 _RUN_ACTIVE 会被清空，但**终态必须还能读到**
#   （前端要显示"完成/已停止"以及最终的 index/total）——否则 run-state 会在收尾瞬间
#   变成 null，界面只能靠乐观值猜。
_LAST_RUN_ID: Optional[str] = None


def _cancel_expired() -> None:
    now = time.time()
    with _RUN_CANCEL_LOCK:
        for k in [k for k, t0 in _RUN_CANCEL.items() if now - t0 > _RUN_CANCEL_TTL]:
            _RUN_CANCEL.pop(k, None)


def _is_cancelled(run_id: Optional[str]) -> bool:
    if not run_id:
        return False
    with _RUN_CANCEL_LOCK:
        return run_id in _RUN_CANCEL


def _set_progress(run_id: Optional[str], **kw) -> None:
    """更新某次执行的进度快照（线程安全；供 /control/run-state 读取）。"""
    if not run_id:
        return
    with _RUN_CANCEL_LOCK:
        st = _RUN_PROGRESS.get(run_id)
        if st is None:
            st = {"run_id": run_id, "running": True, "paused": False}
            _RUN_PROGRESS[run_id] = st
        st.update(kw)


def _pause_event(run_id: str) -> "threading.Event":
    """取（或建）该次执行的暂停开关。"""
    with _RUN_CANCEL_LOCK:
        ev = _RUN_PAUSE.get(run_id)
        if ev is None:
            ev = threading.Event()
            _RUN_PAUSE[run_id] = ev
        return ev


def _is_paused(run_id: Optional[str]) -> bool:
    if not run_id:
        return False
    with _RUN_CANCEL_LOCK:
        ev = _RUN_PAUSE.get(run_id)
    return bool(ev is not None and ev.is_set())


def _wait_if_paused(run_id: Optional[str]) -> bool:
    """已暂停则阻塞在这里。返回 True = 等待期间被"停止执行"打断。

    ★ 只在**步边界**调用：点动/吸放步一旦下发就不打断（那是急停的语义）。
    """
    while _is_paused(run_id):
        if _is_cancelled(run_id):
            return True
        time.sleep(0.1)
    return _is_cancelled(run_id)


def _sleep_pausable(seconds: float, run_id: Optional[str]) -> bool:
    """可暂停、可打断的等待。暂停时**冻结剩余时间**（不会"暂停完发现已经等过了"）。

    返回 True = 被"停止执行"打断。
    """
    remain = max(0.0, float(seconds))
    while remain > 0:
        if _is_cancelled(run_id):
            return True
        if _is_paused(run_id):
            time.sleep(0.1)
            continue                      # 冻结：不推进 remain
        d = 0.05 if remain > 0.05 else remain
        time.sleep(d)
        remain -= d
    return _is_cancelled(run_id)


def _reject_if_run_active(what: str) -> None:
    """★ 2026-09-29：序列/文件执行期间拒绝**手动**下发。

    理由（真机层面）：手动点动与序列共用**同一个 40135 触发位寄存器**，
    序列的 io 步骤还共用 40135.Bit1/Bit2；交错下发会互相覆盖/清掉触发位，
    产生谁也说不清的半截状态（机器人走一半、阀开着、完成位对不上）。
    要手动操作，请先点「停止执行」。
    """
    with _RUN_CANCEL_LOCK:
        active = list(_RUN_ACTIVE.keys())
    if active:
        raise HTTPException(
            409,
            detail=("有序列/文件正在执行（%s），已拒绝手动%s —— 请先点「停止执行」，"
                    "或等它跑完" % (", ".join(active[:2]), what)))


# =====================================================================
# ★ 2026-10-08 执行上下文（RunContext）
#   给"非文件类"的长流程（如三色分拣：9 件 × 十几次动作）复用**同一套**
#   取消 / 暂停 / 进度 设施。
#
#   ★ 为什么必须复用而不是另起一套：界面上只有一个「停止执行」和一个进度轮询。
#     若分拣自建状态，就会出现两套互不相干的"运行中"——停止按钮停不掉它、
#     run-state 也看不到它，这是真机能出事的缺口（机器人还在动，界面说空闲）。
# =====================================================================
class RunContext:
    """一次长流程的执行上下文。用法：

        ctx = new_run("sorting", "三色分拣")
        try:
            while ...:
                if ctx.cancelled(): break
                ctx.progress(stage="取料", piece=i, total=9)
                ...
                if ctx.sleep(1.0): break        # 可暂停、可打断的等待
        finally:
            ctx.finish("done" | "cancelled" | "error")
    """

    def __init__(self, run_id: str, name: str, kind: str = "sorting"):
        self.run_id = run_id
        self.name = name
        self.kind = kind
        _cancel_expired()
        with _RUN_CANCEL_LOCK:
            _RUN_CANCEL.pop(run_id, None)
            _RUN_PAUSE.pop(run_id, None)
            for k in [k for k, v in _RUN_PROGRESS.items() if not v.get("running")]:
                _RUN_PROGRESS.pop(k, None)
        _RUN_ACTIVE[run_id] = {"filename": name, "kind": kind, "started_at": time.time()}
        global _LAST_RUN_ID
        _LAST_RUN_ID = run_id
        _set_progress(run_id, name=name, kind=kind, running=True, paused=False,
                      index=0, total=0, stage="", phase="starting")

    def cancelled(self) -> bool:
        return _is_cancelled(self.run_id)

    def wait_pause(self) -> bool:
        """已暂停则阻塞在步边界；返回 True = 期间被停止。"""
        return _wait_if_paused(self.run_id)

    def sleep(self, sec: float) -> bool:
        """可暂停（冻结计时）、可打断的等待；返回 True = 被停止。"""
        return _sleep_pausable(sec, self.run_id)

    def progress(self, **kw) -> None:
        _set_progress(self.run_id, **kw)

    def finish(self, phase: str = "done") -> None:
        with _RUN_CANCEL_LOCK:
            _RUN_ACTIVE.pop(self.run_id, None)
            _RUN_CANCEL.pop(self.run_id, None)
            _RUN_PAUSE.pop(self.run_id, None)
        _set_progress(self.run_id, running=False, paused=False, phase=phase)


def new_run(name: str, kind: str = "sorting") -> RunContext:
    """开一次长流程（同类流程同时只允许一路 —— 见 api/sorting.py 的单例闸）。"""
    return RunContext("run-%d" % time.time_ns(), name, kind)


def request_cancel(run_id: Optional[str] = None, filename: Optional[str] = None) -> List[str]:
    """请求停止：给"停止执行"/"中止"共用的落地实现（公开给其它模块调用）。

    返回被标记的 run_id 列表。语义与原 /control/run-cancel 完全一致：
    只打标记，真正的停止发生在执行循环的**步边界**与 motion.command 的 abort_check 里。
    """
    _cancel_expired()
    with _RUN_CANCEL_LOCK:
        active = list(_RUN_ACTIVE.keys())
    hit: List[str] = []
    if run_id:
        with _RUN_CANCEL_LOCK:
            _RUN_CANCEL[run_id] = time.time()
        hit = [run_id]
    else:
        for r in active:
            info = _RUN_ACTIVE.get(r) or {}
            if filename and info.get("filename") != filename:
                continue
            with _RUN_CANCEL_LOCK:
                _RUN_CANCEL[r] = time.time()
            hit.append(r)
    return hit


class RunCancelIn(BaseModel):
    run_id: Optional[str] = Field(default=None, max_length=64)
    filename: Optional[str] = Field(default=None, max_length=200)


def _interruptible_sleep(seconds: float, run_id: Optional[str]) -> bool:
    """可打断的等待：每 100ms 查一次取消表。返回 True 表示被中止。"""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if _is_cancelled(run_id):
            return True
        time.sleep(min(0.1, max(0.0, end - time.time())))
    return _is_cancelled(run_id)


@router.post("/run-cancel")
def api_run_cancel(body: RunCancelIn, tok: str = Depends(require_control)):
    """中止正在执行的文件/程序。

    ★ P0-7：这是"中止"按钮唯一有效的后端实现。支持按 run_id 精确中止，
      未给 run_id 时按 filename 匹配当前在跑的目标（兼容旧前端）。
    """
    _cancel_expired()
    matched: List[str] = []
    with _RUN_CANCEL_LOCK:
        if body.run_id:
            _RUN_CANCEL[body.run_id] = time.time()
            matched.append(body.run_id)
        elif body.filename:
            for rid, info in list(_RUN_ACTIVE.items()):
                if info.get("filename") == body.filename:
                    _RUN_CANCEL[rid] = time.time()
                    matched.append(rid)
        else:
            # 既没 run_id 也没 filename → 中止**当前所有**在跑的目标（兜底，
            # 保证"中止"按钮按下一定有效，不会出现点了没反应）
            for rid in list(_RUN_ACTIVE.keys()):
                _RUN_CANCEL[rid] = time.time()
                matched.append(rid)
            for rid in list(_RUN_CANCEL.keys()):
                matched.append(rid)
    # ★ 全维度审查 B-04：打标记只作用于"步骤之间"。飞行中的那一发需要显式下发
    #   CMD_STOP(0x1005) 才能让常驻点动服务程序回到 WAIT。
    #   这里只发"停止程序"，**不发急停**（不切断伺服），是可逆的最小动作。
    stop_sent = False
    if matched:
        try:
            ok, err = motion.modbus.rc_command(0x1005)   # CMD_STOP
            stop_sent = bool(ok) and err is None
        except Exception as e:  # noqa
            log.warning("中止时下发停止命令失败: %s", e)
    emit_event("control", "warn", "control.run_cancel",
               f"用户请求中止执行（{body.filename or body.run_id or '全部'}）",
               {"run_id": body.run_id, "filename": body.filename,
                "matched": matched, "stop_sent": stop_sent}, actor=_actor(tok))
    return {"ok": True, "matched": sorted(set(matched)), "stop_sent": stop_sent}


# =====================================================================
# ★ 2026-09-29 序列编辑器（「程序执行」页）——进度 / 暂停 / 继续 / 序列文件读写
#   设计见 docs/方案-程序执行序列编辑器（五类操作·经210执行）.md
#   ★ 支持的操作（**只有这五类**，其余一律拒）：
#       标记点(point) / 吸(suck) / 停止吸(release) / 放(blow) / 停止放(unblow) / 等待(wait)
#     ★ 四路气路**全部电平保持**（与控制器 210 的 Bit1~Bit4 一一对应）。
#       只关真空阀挡不住残余负压、工件会吸住不掉，所以要能单独"放"（吹气）；
#       但也不能并入"停止吸"——有些场合只需松手不想吹气。
#     本文件是**唯一入口**，后端在这里做硬校验，前端不可能绕过。
# =====================================================================
SEQ_NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5\-. ]{1,40}$")
SEQ_MAX_ITEMS = 200          # 单条序列最多 200 步（防止界面失控 + 文件过大）
SEQ_MAX_BYTES = 512 * 1024   # 保存文件大小上限


def _seq_path(name: str) -> Optional[str]:
    """序列名 → programs 目录内 .json 绝对路径（拒绝路径穿越）。返回 None = 非法。"""
    base = os.path.abspath(_program_dir())
    n = os.path.basename((name or "").strip().replace("\\", "/"))
    if not n or n in (".", ".."):
        return None
    if not n.lower().endswith(".json"):
        n += ".json"
    p = os.path.abspath(os.path.join(base, n))
    try:
        if os.path.commonpath([p, base]) != base:
            return None
    except ValueError:          # 不同盘符
        return None
    return p


def _norm_seq_items(items) -> List[dict]:
    """把编辑器提交的步骤**归一化并硬校验为这五类**，落盘格式与既有
    `_steps_from` 完全兼容（point_id / op / dwell_ms）。

    五类之外的任何东西都在这里被拒 —— 这是"不乱加操作"的后端保证。
    """
    if not isinstance(items, list):
        raise HTTPException(400, detail="items 必须是数组")
    if len(items) > SEQ_MAX_ITEMS:
        raise HTTPException(400, detail="步骤数超过上限 %d" % SEQ_MAX_ITEMS)
    out: List[dict] = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise HTTPException(400, detail="第 %d 步格式非法（必须是对象）" % i)
        t = str(it.get("type") or it.get("op") or "").strip().lower()
        if t in ("point", "move"):
            pid = it.get("point_id")
            if pid is None or not str(pid).strip().isdigit():
                raise HTTPException(400, detail="第 %d 步（标记点）缺少有效的 point_id" % i)
            out.append({"point_id": int(pid)})
        elif t == "suck":
            out.append({"op": "suck"})
        elif t in ("release", "unsuck", "stop_suck"):
            out.append({"op": "release"})
        elif t in ("blow", "blowoff", "purge"):
            # ★ 放（开吹气阀，电平保持）：只关阀时工件会因残余负压/密封吸住不掉。
            out.append({"op": "blow"})
        elif t in ("unblow", "stop_blow", "blowoff_stop"):
            out.append({"op": "unblow"})
        elif t in ("wait", "dwell"):
            try:
                sec = float(it.get("seconds", 0))
            except (TypeError, ValueError):
                raise HTTPException(400, detail="第 %d 步（等待）时长不是数字" % i)
            if not (0.1 <= sec <= 3600.0):
                raise HTTPException(400, detail="第 %d 步（等待）时长须在 0.1~3600 秒" % i)
            out.append({"op": "wait", "dwell_ms": int(round(sec * 1000))})
        else:
            raise HTTPException(
                400, detail=("第 %d 步操作类型「%s」不支持 —— 只允许 "
                             "标记点/吸/停止吸/放/停止放/等待" % (i, t or "空")))
    return out


class SeqIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    speed_pct: int = Field(default=5, ge=5, le=100)
    items: List[dict] = Field(default_factory=list)
    overwrite: bool = True


@router.get("/run-state")
def api_run_state(run_id: Optional[str] = Query(default=None, max_length=64),
                  tok: str = Depends(require_control)):
    """当前执行进度 / 暂停态快照（前端轮询它画进度条与当前步高亮）。"""
    with _RUN_CANCEL_LOCK:
        active = list(_RUN_ACTIVE.keys())
        rid = run_id or (active[-1] if active else _LAST_RUN_ID)
        st = dict(_RUN_PROGRESS.get(rid)) if rid else None
        if st is not None:
            st["paused"] = bool(_RUN_PAUSE.get(rid) and _RUN_PAUSE[rid].is_set())
        return {"ok": True, "state": st, "active": active}


@router.post("/run-pause")
def api_run_pause(body: Optional[RunCancelIn] = None, tok: str = Depends(require_control)):
    """暂停当前序列。

    ★ 语义（如实，不夸大）：暂停在**当前步结束后**生效；「等待」步立即冻结计时。
      想让机器人**立刻**停住，请用「急停」，不是暂停。
    """
    rid = (body.run_id if body else None) or None
    with _RUN_CANCEL_LOCK:
        targets = [rid] if rid else list(_RUN_ACTIVE.keys())
    if not targets:
        return {"ok": False, "error": "当前没有正在执行的序列/文件"}
    for r in targets:
        _pause_event(r).set()
        _set_progress(r, paused=True)
    emit_event("control", "warn", "control.run_pause",
               "已请求暂停执行（当前步结束后生效）",
               {"run_id": targets[0], "targets": targets}, actor=_actor(tok))
    return {"ok": True, "paused": targets}


@router.post("/run-resume")
def api_run_resume(body: Optional[RunCancelIn] = None, tok: str = Depends(require_control)):
    """继续执行。"""
    rid = (body.run_id if body else None) or None
    with _RUN_CANCEL_LOCK:
        targets = [rid] if rid else list(_RUN_PAUSE.keys())
    hit = []
    for r in targets:
        ev = _RUN_PAUSE.get(r)
        if ev is not None and ev.is_set():
            ev.clear()
            _set_progress(r, paused=False)
            hit.append(r)
    if not hit:
        return {"ok": False, "error": "当前没有处于暂停的执行"}
    emit_event("control", "info", "control.run_resume", "已继续执行",
               {"run_id": hit[0], "targets": hit}, actor=_actor(tok))
    return {"ok": True, "resumed": hit}


@router.get("/seq")
def api_seq_load(name: str = Query(..., min_length=1, max_length=64),
                 tok: str = Depends(require_control)):
    """载入已保存的序列文件，供编辑器回填（返回原始 items）。"""
    p = _seq_path(name)
    if not p or not os.path.isfile(p):
        raise HTTPException(404, detail="序列文件不存在：%s" % name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, detail="序列文件读取失败：%s" % e)
    if not isinstance(data, dict):
        raise HTTPException(400, detail="序列文件内容必须是 JSON 对象")
    items = data.get("items")
    if not isinstance(items, list):
        items = data.get("steps") if isinstance(data.get("steps"), list) else []
    return {"ok": True, "name": data.get("name") or os.path.splitext(os.path.basename(p))[0],
            "file": os.path.basename(p), "speed_pct": data.get("speed_pct"),
            "saved_at": data.get("saved_at"), "items": items}


@router.post("/seq")
def api_seq_save(body: SeqIn, tok: str = Depends(require_control)):
    """把编辑器里的序列保存成本地文件 programs/<name>.json。

    ★ 校验：名称字符集（中英文/数字/_/-/./空格）、操作类型硬校验（六类）、
      步骤数上限、路径穿越防护、文件大小上限。
    ★ 格式与既有 `_load_disk` / `_steps_from` 兼容 → 保存后立刻出现在
      「本地程序」列表里，也能被「手动执行」按文件名跑。
    """
    name = (body.name or "").strip()
    if not SEQ_NAME_RE.match(name):
        raise HTTPException(400, detail="名称只能含中英文、数字、下划线、短横线、点与空格（1~40 字）")
    items = _norm_seq_items(body.items)
    if not items:
        raise HTTPException(400, detail="至少需要一个步骤")
    p = _seq_path(name)
    if not p:
        raise HTTPException(400, detail="名称非法（不允许路径分隔符）")
    existed = os.path.isfile(p)
    if existed and not body.overwrite:
        raise HTTPException(409, detail="同名文件已存在：%s" % os.path.basename(p))
    payload = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "speed_pct": int(body.speed_pct),
        "items": items,
    }
    blob = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(blob.encode("utf-8")) > SEQ_MAX_BYTES:
        raise HTTPException(400, detail="序列过大（超过 %d KB）" % (SEQ_MAX_BYTES // 1024))
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(blob)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, detail="保存失败：%s" % e)
    emit_event("control", "info", "control.seq_save",
               "已保存序列：%s（%d 步）" % (name, len(items)),
               {"file": os.path.basename(p), "steps": len(items),
                "existed": existed}, actor=_actor(tok))
    return {"ok": True, "file": os.path.basename(p), "name": name,
            "steps": len(items), "existed": existed, "path": p}


@router.get("/files")
def api_files(tok: str = Depends(require_control)):
    """列出"可按文件名执行"的目标：数据库程序 / 点位 + programs 目录 JSON。"""
    db = SessionLocal()
    try:
        return {"dir": _program_dir(), "items": _candidates(db)}
    finally:
        db.close()


@router.post("/run-file")
def api_run_file(body: RunFileIn, tok: str = Depends(require_control)):
    """按文件名执行：dry_run=True 试运行测试（只校验不下发），False 真实下发。"""
    # ★ P0-7：为本次执行分配 run_id 并登记，供 /control/run-cancel 精确中止。
    run_id = (body.run_id or "").strip() or ("run-%d" % time.time_ns())
    _cancel_expired()
    disp_name = (body.name or "").strip() or ((body.filename or "").strip() or "序列")
    with _RUN_CANCEL_LOCK:
        _RUN_CANCEL.pop(run_id, None)     # 同一 run_id 复用时先清掉旧的中止标记
        _RUN_PAUSE.pop(run_id, None)
        # 只保留"最近一次"的进度快照，避免条目无限增长
        for k in [k for k, v in _RUN_PROGRESS.items() if not v.get("running")]:
            _RUN_PROGRESS.pop(k, None)
    _RUN_ACTIVE[run_id] = {"filename": disp_name, "started_at": time.time()}
    global _LAST_RUN_ID
    _LAST_RUN_ID = run_id          # 供收尾后读取终态（见 api_run_state）
    _set_progress(run_id, name=disp_name, running=True, paused=False,
                  index=0, total=0, step_name="", phase="starting")
    cancelled = False
    db = SessionLocal()
    try:
        # ★ 2026-09-29：两种来源 —— 给了 filename 按文件执行（以文件为准）；
        #   只给 items 则直接跑编辑器里正在编的序列（不落盘也能试跑）。
        if (body.filename or "").strip():
            res = _resolve_file(body.filename, db)
            if not res:
                emit_event("control", "warn", "control.run_file_missing",
                           f"按文件名执行未命中：{body.filename}",
                           {"filename": body.filename}, actor=_actor(tok))
                raise HTTPException(404, detail={
                    "message": f"未找到可执行目标：{body.filename}",
                    "candidates": _candidates(db),
                })
        else:
            _seq = _norm_seq_items(body.items)          # ★ 六类操作硬校验
            if not _seq:
                raise HTTPException(400, detail="至少需要一个步骤")
            res = {"kind": "program", "name": disp_name, "source": "editor",
                   "steps": _steps_from(_seq, db)}
        if res.get("error"):
            return {"ok": False, "dry_run": body.dry_run, "kind": res["kind"],
                    "name": res["name"], "source": res.get("source"),
                    "error": res["error"], "steps": [], "readonly": True}

        # 常驻服务程序号（点动/吸放同号；= 控制器上的 210）。io 步骤要靠它执行。
        try:
            svc_prog = int(get_config().get("motion", "jog", "service_program", default=0) or 0)
        except Exception:
            svc_prog = 0

        results: List[dict] = []
        ok_all = True
        total_ms = 0
        _set_progress(run_id, total=len(res["steps"]),
                      speed_pct=body.speed_pct, dry_run=bool(body.dry_run))

        for st in res["steps"]:
            # ★ P0-7：每一步开始前先查中止表 —— 这是"中止"真正生效的位置。
            if _is_cancelled(run_id):
                cancelled = True
                break
            # ★ 2026-09-29 暂停（序列编辑器）：**步边界**生效。
            #   点动/吸放步一旦下发就不打断（那是急停的语义），暂停在它走完后生效。
            if _wait_if_paused(run_id):
                cancelled = True
                break
            _set_progress(run_id, index=st.get("index"), step_name=st.get("name"),
                          current_op=st.get("op") or "point", phase="running")
            if st.get("ok") is False:
                results.append(st)
                ok_all = False
                continue
            # ★ P1-B1：速度必须夹到 [5,100]。
            #   原实现 `int(st.get("speed_pct") or body.speed_pct)`：步骤里只要
            #   带了 speed_pct（_steps_from 缺省写 100），请求里的 5% 就被完全
            #   覆盖 → 真实执行恒 100%。现在：步骤未显式指定时继承请求值，
            #   最后统一夹取，杜绝"请求 5% 却按 100% 跑"。
            sp = int(st.get("speed_pct") or body.speed_pct)
            sp = max(5, min(100, sp))
            dw = int(st.get("dwell_ms") or 0)

            # ---------- io 步：吸气 / 停止吸气 / 等待（无移动目标） ----------
            # ★★ 2026-09-29：**真的执行了**。原实现是空转 + 注释"当前无真机吸气通道"，
            #   在 210 三合一常驻程序就位后已过时 ——
            #     吸气     = 置 40135.Bit1 → 210 执行 io.DOut[N] := true（保持）
            #     停止吸气 = 置 40135.Bit2 → 210 执行 io.DOut[N] := false
            #     等待     = 纯软件计时（可暂停：计时冻结；可停止：立刻返回）
            if st.get("op"):
                op = st.get("op")
                if st.get("ok") is False:
                    results.append({"index": st["index"], "name": st.get("name") or op,
                                    "ok": False, "op": op, "readonly": False,
                                    "error": st.get("error") or "步骤非法"})
                    ok_all = False
                    break
                if op == "wait":
                    if body.dry_run:
                        total_ms += dw
                    elif _sleep_pausable(dw / 1000.0, run_id):
                        cancelled = True
                        break
                    else:
                        total_ms += dw
                    results.append({"index": st["index"], "name": st.get("name") or "等待",
                                    "ok": True, "op": "wait", "dwell_ms": dw,
                                    "readonly": bool(body.dry_run)})
                    continue
                if op in ("suck", "release", "blow", "unblow"):
                    if body.dry_run:
                        total_ms += 300
                        results.append({"index": st["index"], "name": st.get("name") or op,
                                        "ok": True, "op": op, "readonly": True})
                        continue
                    if svc_prog <= 0:
                        results.append({"index": st["index"], "name": st.get("name") or op,
                                        "ok": False, "op": op, "readonly": False,
                                        "error": ("未配置常驻服务程序号（config motion.jog."
                                                  "service_program）—— 吸放步骤无法执行")})
                        ok_all = False
                        break
                    # ★ 取执行互斥：吸放与点动**共用同一个 40135 寄存器**，
                    #   若不互斥，与在飞的点动交错会互相清掉触发位。
                    if not motion._exec_lock.acquire(blocking=False):
                        results.append({"index": st["index"], "name": st.get("name") or op,
                                        "ok": False, "op": op, "readonly": False,
                                        "error": "有点动/下发正在执行，本步已拒绝（避免触发位互相覆盖）"})
                        ok_all = False
                        break
                    try:
                        # ① 常驻程序预检（幂等）：不在跑就 伺服→停机→加载→运行 210
                        okp, perr = motion.modbus.rc_vacuum_prepare(svc_prog)
                        if not okp:
                            results.append({"index": st["index"],
                                            "name": st.get("name") or op,
                                            "ok": False, "op": op, "readonly": False,
                                            "error": "常驻服务程序未就绪：%s" % perr})
                            ok_all = False
                            break
                        # ② 触发并等完成位
                        okv, verr, vdetail = motion.modbus.rc_vacuum(op, timeout=10.0)
                    finally:
                        motion._exec_lock.release()
                    results.append({"index": st["index"], "name": st.get("name") or op,
                                    "ok": bool(okv), "op": op, "readonly": False,
                                    "error": verr, "detail": vdetail})
                    if not okv:
                        ok_all = False
                        break
                    emit_event("control", "info", "control.seq_io",
                               "序列第 %d 步 %s 完成" % (st["index"], st.get("name") or op),
                               {"run_id": run_id, "op": op, "step": st["index"],
                                "elapsed_s": (vdetail or {}).get("elapsed_s")},
                               actor=_actor(tok))
                    continue
                # 未知 op（_steps_from 已标 ok=False，正常到不了这里）
                results.append({"index": st["index"], "name": st.get("name") or op,
                                "ok": False, "op": op, "readonly": False,
                                "error": "未知操作类型"})
                ok_all = False
                break

            # ---------- 试运行（空跑测试）----------
            if body.dry_run:
                pv = _compute_preview(st["mode"], st.get("joints"), st.get("tcp"),
                                      duration_ms=2500, steps=body.steps)
                total_ms += int(pv["duration_ms"])
                results.append({
                    "index": st["index"], "name": st.get("name"),
                    "ok": bool(pv["ok"]), "mode": st["mode"],
                    "target": pv["target"], "distance_mm": pv["distance_mm"],
                    "violations": pv["violations"], "warnings": pv["warnings"],
                    "solver": pv["solver"], "error": pv.get("error"),
                    "readonly": True,
                })
                if not pv["ok"]:
                    ok_all = False
                continue

            # ---------- 真实下发：每步都过围栏互锁 ----------
            ok, reason, snap = guard_check()
            if not ok:
                results.append({"index": st["index"], "name": st.get("name"),
                                "ok": False, "blocked": True, "readonly": False,
                                "reason": reason})
                ok_all = False
                emit_event("control", "warn", "control.move_blocked",
                           f"单文件执行第 {st['index']} 步被拦截：{reason}",
                           {"filename": body.filename, "step": st["index"],
                            "reason": reason}, actor=_actor(tok))
                break

            target = st.get("joints")
            if target is None:
                if not st.get("tcp"):
                    results.append({"index": st["index"], "name": st.get("name"),
                                    "ok": False, "readonly": False,
                                    "error": "直角步骤缺少 TCP 目标"})
                    ok_all = False
                    break
                q, solver, viol = _solve_tcp(st["tcp"])
                if viol or not solver["ok"]:
                    results.append({"index": st["index"], "name": st.get("name"),
                                    "ok": False, "readonly": False,
                                    "error": "IK 解算失败或超限位",
                                    "violations": viol})
                    ok_all = False
                    break
                target = [float(v) for v in q]

            # ★ P0-7：真实下发前再查一次（guard_check 本身可能耗时）
            if _is_cancelled(run_id):
                cancelled = True
                break
            # ★ 全维度审查 B-04：把中止判据接进**飞行中的这一发**。
            #   原实现只打 _RUN_CANCEL 标记 → 下一步循环开头才生效，而当前这一步
            #   已经进 motion.command()，rc_jog_execute 同步阻塞最长 30s 且无
            #   abort_check → 界面显示"已中止"、机器人还走完这一发。
            r = motion.command([float(x) for x in target], sp, dw,
                               abort_check=lambda: _is_cancelled(run_id))
            total_ms += dw + 300
            results.append({
                "index": st["index"], "name": st.get("name"),
                "ok": bool(r.get("ok")), "mode": r.get("mode"),
                "target": [round(float(v), 2) for v in target],
                "speed_pct": sp,
                "error": r.get("error"), "readonly": False,
            })
            if not r.get("ok"):
                ok_all = False
                break
            _set_progress(run_id, phase="settling")
            # ★ 用可暂停等待：点动步的"收尾静置"是暂停最自然的落点
            if _sleep_pausable(max(0.15, dw / 1000.0 + 0.2), run_id):
                cancelled = True
                break

        payload = {
            "ok": ok_all and not cancelled, "dry_run": body.dry_run,
            "kind": res["kind"], "name": res["name"], "source": res.get("source"),
            "path": res.get("path"), "id": res.get("id"),
            "count": len(results),
            "passed": sum(1 for r in results if r.get("ok")),
            "duration_ms": total_ms,
            "readonly": bool(body.dry_run),
            # ★ P0-7：明确告诉前端"是被中止的"，不是跑失败
            "cancelled": cancelled,
            "run_id": run_id,
            "steps": results,
        }
        if cancelled:
            emit_event("control", "warn", "control.run_cancelled",
                       f"执行已被用户中止（第 {len(results) + 1} 步前）：{res['name']}",
                       {"filename": body.filename, "run_id": run_id,
                        "done": len(results), "speed_pct": body.speed_pct},
                       actor=_actor(tok))
            return payload
        if body.dry_run:
            emit_event("control", "info" if ok_all else "warn", "control.dry_run",
                       ("试运行通过：" if ok_all else "试运行发现问题：") + str(res["name"]),
                       {"filename": body.filename, "kind": res["kind"],
                        "count": len(results), "passed": payload["passed"]},
                       actor=_actor(tok))
        else:
            emit_event("control", "info" if ok_all else "warn", "control.run_file",
                       ("已按文件执行：" if ok_all else "按文件执行中断：") + str(res["name"]),
                       {"filename": body.filename, "kind": res["kind"],
                        "count": len(results), "passed": payload["passed"],
                        "speed_pct": body.speed_pct},
                       actor=_actor(tok))
        return payload
    finally:
        # ★ P0-7：无论正常结束/异常/中止，都要注销在跑登记并清掉中止标记
        with _RUN_CANCEL_LOCK:
            _RUN_ACTIVE.pop(run_id, None)
            _RUN_CANCEL.pop(run_id, None)
            _RUN_PAUSE.pop(run_id, None)
        # ★ 2026-09-29：进度快照**保留**（供前端读取最后状态），但标记为已结束；
        #   下一次执行开始时会清掉这些已结束的旧条目。
        _set_progress(run_id, running=False, paused=False,
                      phase=("cancelled" if cancelled else "done"))
        db.close()


@router.post("/estop")
def api_estop(tok: str = Depends(require_control)):
    """紧急停止: 立即锁定执行引擎(真实模式同时下发急停寄存器)。"""
    # ★ 顺序必须是 motion 先：先把 stopped 标志+40101 停止字下发到线上，
    #   再停点动线程 —— 反过来的话，点动线程在 jog.stop 返回到 motion.estop
    #   生效之间还可能下发一拍目标角。motion.estop 置位 stopped 后，
    #   command() 会立刻拒绝点动线程后续的任何下发，无追加运动。
    st = motion.estop()
    jog.stop("estop")
    # ★ P0-7：急停必须同时中止在跑的文件执行 —— 否则急停复位后程序会继续往下跑。
    with _RUN_CANCEL_LOCK:
        for rid in list(_RUN_ACTIVE.keys()):
            _RUN_CANCEL[rid] = time.time()
    # ★ 全维度审查 B-06：急停**未送达**必须显式报错，不能显示"已停"。
    if not st.get("estop_sent"):
        emit_event("safety", "critical", "control.estop_failed",
                   "急停命令未送达控制器：%s" % (st.get("estop_error") or "未知原因"),
                   {"mode": st.get("mode"), "error": st.get("estop_error")},
                   actor=_actor(tok))
        raise HTTPException(
            status_code=503,
            detail=("急停命令未送达控制器（%s）。请检查链路后重试，"
                    "必要时立即使用示教器硬件急停按钮。"
                    % (st.get("estop_error") or "未知原因")))
    emit_event("control", "critical", "control.estop",
               "紧急停止已触发，执行引擎已锁定",
               {"mode": st.get("mode"), "stopped": st.get("stopped"),
                "estop_sent": True},
               actor=_actor(tok))
    return st


@router.post("/estop/reset")
def api_estop_reset(tok: str = Depends(require_control)):
    """复位急停(需重新评估安全后才能继续运动)。"""
    st = motion.reset_estop()
    emit_event("control", "info", "control.estop_reset",
               "急停已复位，可继续下发运动",
               {"mode": st.get("mode"), "stopped": st.get("stopped")},
               actor=_actor(tok))
    return st


@router.get("/state")
def api_control_state():
    """执行引擎状态: 运动中/急停/模式/最近目标。"""
    return motion.state()


# =====================================================================
# 坐标系（阶段 6）：关节 / 机器人 / 工具 / 用户
#
# ★ 前端"点位执行"页的坐标系切换器只读这里 —— 轴名、单位、标定状态全部由后端给，
#   前端不自己维护一份（否则改一边漏一边，界面上的轴名和实际动的轴会对不上）。
# =====================================================================
def _pose_dict(T) -> dict:
    rx, ry, rz = matrix_to_rpy(T[:3, :3])
    return {"x": round(float(T[0, 3]), 3), "y": round(float(T[1, 3]), 3),
            "z": round(float(T[2, 3]), 3),
            "rx": round(rx, 4), "ry": round(ry, 4), "rz": round(rz, 4)}


@router.get("/frames")
def api_frames():
    """四坐标系描述：轴名 / 单位 / 标定状态 / 世界位姿 / 各轴单位。"""
    cfg = fc.load_frames()
    items = []
    for f in fc.describe()["frames"]:
        fid = f["id"]
        try:
            world = jf.world_pose(fid, cfg, f.get("user_id"))
        except Exception:
            world = None
        items.append({
            "id": fid,
            "label": f["label"],
            "kind": f["kind"],                     # joint | cartesian
            "axes": list(f["axes"]),
            "unit": f["unit"],
            "unit_of": {str(i): jf.unit_of(fid, i) for i in range(1, 7)},
            "user_id": f.get("user_id"),
            "builtin": bool(f.get("builtin")),
            "verified": bool(f.get("verified")),
            "hint": f.get("hint") or "",
            "world": world,                        # joint/tool 为 None（它们随末端走）
        })
    # 单位说明：★ 单位是**按轴**决定的，不是按坐标系，前端必须按轴取
    return {
        "ok": True,
        "frames": items,
        "config": cfg,
        "axis_units": {"joint": "deg", "cartesian": {"1": "mm", "2": "mm", "3": "mm",
                                                     "4": "deg", "5": "deg", "6": "deg"}},
        "path": fc.FRAMES_PATH,
    }


@router.get("/tcp")
def api_tcp(frame: str = "base", user_frame: Optional[str] = None):
    """当前 TCP 位姿（默认按机器人坐标系表达）。

    ★ 这是"法兰位姿 ∘ 工具变换"后再用**该坐标系姿态**表达的结果 ——
      点动面板拿它显示"现在在哪"；未标定时也是标定的核对依据
      （示教到某点，看读数是否与卷尺量到的一致）。
    """
    f, ferr = jf.norm_frame(frame)
    q = _start_joints(None)
    T_ft = jf.tool_matrix()
    T_tcp_w = fk_matrix(q) @ T_ft
    W = jf.world_matrix(f if f != "joint" else "base", None, user_frame)
    T_in = invert(W) @ T_tcp_w
    return {
        "ok": True,
        "frame": f,
        "user_frame": user_frame or ("wobj0" if f == "user" else ""),
        "unit": "mm",
        "pose": _pose_dict(T_in),          # 直角系下：mm + deg
        "joints": [round(float(v), 3) for v in q],
        "flange": _pose_dict(fk_matrix(q)),  # 法兰位姿（世界系）
        "tcp_world": _pose_dict(T_tcp_w),    # TCP 位姿（世界系）
    }


# ---------------------------------------------------------------- 示教器档位（L1）
# ★ 档位有两个来源，控制器实测优先（见 services/runmode.py 模块头）：
#   - 控制器状态字（40001 的 manual/auto/remote 位）由采集循环每 4s 喂进来 → 自动确认；
#   - 控制器读不到时（未上电/掉线/强制模拟）才需要操作员**声明**。声明值不持久化，
#     后端重启后回到"未确认" —— 重启和旋钮位置无关，拿旧声明放行等于凭空假设。
class RunModeIn(BaseModel):
    mode: str = Field(..., min_length=1, max_length=16)   # T1 | T2 | AUTO | REMOTE
    note: str = Field(default="", max_length=120)


@router.get("/run-mode")
def api_run_mode_state(tok: str = Depends(require_control)):
    """当前示教器档位（控制器实测优先，含来源标记与可选项说明）。"""
    return runmode.state()


@router.post("/run-mode")
def api_run_mode_claim(body: RunModeIn, tok: str = Depends(require_control)):
    """声明示教器档位（控制器读不到时的回落手段）。

    ★ 控制器连上时**以实测为准**：此时声明只作为备注留存，不改变实际判定 ——
      免得"旋钮在 T1、界面声明 AUTO"这种自相矛盾的状态被当成真的放行依据。
    """
    try:
        st = runmode.claim(body.mode, actor=_actor(tok), note=body.note)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    emit_event("control", "info", "control.run_mode",
               f"示教器档位声明为 {st['label']}",
               {"mode": st["mode"], "joggable": st["joggable"], "note": st["note"],
                "options": list(RUN_MODES)},
               actor=_actor(tok))
    return {"ok": True, **st}


@router.delete("/run-mode")
def api_run_mode_clear(tok: str = Depends(require_control)):
    """撤销档位声明（回到"未确认"）。"""
    st = runmode.clear()
    emit_event("control", "info", "control.run_mode_clear",
               "示教器档位声明已撤销（回到未确认）", {}, actor=_actor(tok))
    return {"ok": True, **st}


# ---------------------------------------------------------------- 点动（示教）
# 示教器式的 J1~J6 手动点动。三种安全关（急停 / 围栏 danger|hit / 关节限位）
# 由 JogEngine 内部逐 tick 判定；连续点动另加"死人开关"看门狗（keepalive 超时即停）。
class JogStepIn(BaseModel):
    joint: int = Field(..., ge=1, le=6)          # 关节系 = J1..J6；直角系 = X/Y/Z/A/B/C
    dir: int = 1                                  # +1 / -1
    # ★ 单位随坐标系变：关节系 °，直角系 1/2/3 = mm、4/5/6 = deg。
    #   两个字段都能传，amount 优先（新前端用 amount，旧前端继续用 angle_deg，语义不变）。
    angle_deg: float = Field(default=1.0, gt=0, le=1800)
    amount: Optional[float] = Field(default=None, gt=0, le=1800)
    # ★ 2026-09-29 速度口径统一：**只认右上角全局速度的百分比** speed_pct。
    #   speed_dps 保留仅为兼容旧前端/脚本（按 max_speed_dps 折算成 %），新代码别再用。
    speed_pct: Optional[int] = Field(default=None, ge=5, le=100)
    speed_pct: Optional[int] = Field(default=None, ge=5, le=100)
    speed_dps: float = Field(default=15.0, gt=0, le=1000)   # 已废弃，仅兼容   # 已废弃，仅兼容
    frame: str = Field(default="joint", max_length=16)      # joint | base | tool | user
    user_frame: Optional[str] = Field(default=None, max_length=16)
    slow: bool = False                            # 慢速模式（速度 ÷slow_ratio，官方是 ÷10）


class JogStartIn(BaseModel):
    joint: int = Field(..., ge=1, le=6)
    dir: int = 1
    speed_dps: float = Field(default=15.0, gt=0, le=1000)
    frame: str = Field(default="joint", max_length=16)
    user_frame: Optional[str] = Field(default=None, max_length=16)
    slow: bool = False


def _jog_speed_pct(b) -> int:
    """点动速度的**唯一口径 = 百分比**（与右上角全局速度一致）。

    为什么要有这个函数：2026-09-29 现场报"点动速度突然飘升到 34%"——根因是
    点动页有个独立"角速度(°/s)"旋钮，5°/s 经 5/30→17% 再被 v50perc 反算成 40103=34%，
    于是示教器显示 34%、右上角 5%、实际又是第三个值。现在统一：入参就是右上角那个 %，
    一路直通到 40103（配合程序内 v100perc，写多少就是多少）。
    旧入参 speed_dps 仍接受，按 max_speed_dps 折算成 %（纯兼容，日志留痕）。
    """
    if getattr(b, "speed_pct", None) is not None:
        return clamp_speed(int(b.speed_pct))
    try:
        ref = float(get_config().get("motion", "jog", "max_speed_dps", default=30) or 30)
    except Exception:
        ref = 30.0
    old = max(0.1, float(getattr(b, "speed_dps", 15.0) or 15.0))
    return clamp_speed(int(round(old / max(ref, 1e-6) * 100)))


def _jog_emit(tok: str, action: str, ok: bool, text: str, detail: dict) -> None:
    emit_event("control", "info" if ok else "warn", action, text, detail, actor=_actor(tok))


def _jog_mode_note(tok: str, res: dict) -> None:
    """点动时若档位未声明/已过期 → 放行但留一条 warn 留痕。

    ★ 与围栏互锁的策略完全一致（control.interlock_absent）：不确定就放行 + 记事件，
      而不是把真机联调前的正常操作全部堵死。
    """
    if not res.get("ok"):
        return
    w = jog.mode_warning()
    if w:
        res["mode_warning"] = w
        emit_event("control", "warn", "control.jog_mode_unclaimed", w,
                   {"run_mode": runmode.summary()}, actor=_actor(tok))


def _axis_name(frame: str, axis: int) -> str:
    """轴显示名：关节系 J1..J6；直角系 X/Y/Z/A/B/C。"""
    axes = jf.axes_of(frame)
    a = int(axis)
    return axes[a - 1] if 1 <= a <= len(axes) else ("#%d" % a)


def _frame_label(frame: str, user_frame: Optional[str]) -> str:
    if frame == "joint":
        return "关节"
    if frame == "base":
        return "机器人"
    if frame == "tool":
        return "工具"
    return "用户" + (("·" + str(user_frame)) if user_frame else "")


@router.get("/jog")
def api_jog_state():
    """点动状态：是否进行中 / 轴 / 方向 / 速度 / 看门狗剩余毫秒 / 停止原因 / 坐标系。"""
    return jog.state()


class VacuumIn(BaseModel):
    # ★ 2026-09-29：新增 blow（放气/吹气破真空）。三种动作对应常驻程序 210 的
    #   Bit1（吸气保持）/ Bit2（只关真空阀）/ Bit3（关真空 + 吹气脉冲）。
    action: str = Field(..., pattern="^(suck|release|blow|unblow)$")
    timeout: float = Field(default=6.0, gt=0.0, le=30.0)


@router.post("/vacuum")
def api_vacuum(body: VacuumIn, tok: str = Depends(require_control)):
    """吸气 / 停止吸气 / 放气（不移动机器人关节）：写 40135.Bit1/Bit2/Bit3 触发位 → 常驻服务程序执行。

    ★ 走与 jog 相同的真实下发双闸（real_write_enabled：EFORT_REAL_MOTION=1 + motion.real_write=true）。
      控制器须在 AUTO/远程模式且常驻服务程序在运行，否则触发位无人响应 → 502。
      此接口只驱动电磁阀输出（真空阀 / 吹气阀），不写任何运动指令 → 不会移动机器人。
      ★ 放气（blow）= 关真空 + 吹气脉冲 = **破真空脱件**：只关阀时工件会被残余负压吸住不掉。
    ★ 2026-09-29：吸放与点动**共用同一个常驻程序 200**（见 config motion.jog/vacuum.service_program）。
      吸气=电平保持（不自动停），停止吸气需显式按「停止吸气」。
    """
    if not real_write_enabled():
        raise HTTPException(
            403, detail="真实下发未开启（需 EFORT_REAL_MOTION=1 且 motion.real_write=true）")
    # ★ 2026-09-29：序列/文件执行期间拒绝手动吸放 —— 两者共用 40135.Bit1/Bit2，
    #   交错下发会互相清触发位（界面显示"吸气完成"而实际被序列的停止吸气关掉）。
    _reject_if_run_active("吸放")
    # ★ 取执行互斥：吸放与点动**共用同一个 40135 寄存器**，不能与在飞的下发交错。
    if not motion._exec_lock.acquire(blocking=False):
        raise HTTPException(
            409, detail="有点动/下发正在执行，已拒绝本次吸放（避免触发位互相覆盖）")
    try:
        # ★ 确保常驻服务程序在运行：伺服→停止当前→加载→运行（幂等）。
        #   现场 210 = 三合一常驻服务（点动 + 吸气保持 + 停止吸气），与点动**同号** →
        #   程序通常已在跑，prepare 立即返回；不会来回切程序（切程序正是 5005 的根因）。
        prog_no = int(get_config().get("motion", "vacuum", "service_program",
                                       default=0) or 0)
        if prog_no <= 0:
            raise HTTPException(
                400, detail="未配置常驻服务程序号（config motion.vacuum.service_program）")
        ok, perr = motion.modbus.rc_vacuum_prepare(prog_no)
        if not ok:
            raise HTTPException(502, detail="常驻服务程序未就绪：%s" % perr)
        ok, err, detail = motion.modbus.rc_vacuum(body.action, body.timeout)
    finally:
        motion._exec_lock.release()
    if not ok:
        raise HTTPException(502, detail=err or "吸放触发失败")
    emit_event("control", "info", "control.vacuum",
               "%s完成" % ("吸气" if body.action == "suck" else "放气"),
               {"action": body.action, **(detail or {})}, actor=_actor(tok))
    return {"ok": True, "action": body.action, "detail": detail}


@router.post("/jog/step")
def api_jog_step(body: JogStepIn, tok: str = Depends(require_control)):
    """增量点动：按一次走固定距离/角度（可选 0.1/1/5/10），到限位自动夹紧并提示。

    `frame` 缺省 joint ⇒ 与升级前完全一致；直角系下 `amount` 单位为 mm（轴 1~3）或 °（轴 4~6）。
    """
    if body.dir not in (1, -1):
        raise HTTPException(400, detail="dir 只能是 +1 或 -1")
    # ★ 2026-09-29：序列/文件执行期间拒绝手动点动（共用 40135.Bit0 触发位）。
    #   注意只拦"下发"，不拦 /jog/stop —— 停永远要能停。
    _reject_if_run_active("点动")
    frame, ferr = jf.norm_frame(body.frame)
    if ferr:
        raise HTTPException(400, detail=ferr)
    amt = float(body.amount if body.amount is not None else body.angle_deg)
    res = jog.step(body.joint, body.dir, amt, _jog_speed_pct(body),
                   frame=frame, user_frame=body.user_frame, slow=body.slow)
    unit = jf.unit_of(frame, body.joint)
    name = _axis_name(frame, body.joint)
    sign = "+" if body.dir > 0 else "-"
    _jog_emit(tok, "control.jog_step", bool(res.get("ok")),
              (f"增量点动[{_frame_label(frame, body.user_frame)}] "
               f"{name}{sign}{amt:g}{unit}"
               if res.get("ok") else f"增量点动被拒：{res.get('error')}"),
              {"joint": body.joint, "dir": body.dir, "amount": amt, "unit": unit,
               "frame": frame, "user_frame": body.user_frame, "slow": body.slow,
               "speed_pct": _jog_speed_pct(body), "blocked": bool(res.get("blocked")),
               "limited": bool(res.get("limited")), "clamped": bool(res.get("limit_clamped"))})
    _jog_mode_note(tok, res)
    if not res.get("ok") and res.get("blocked"):
        raise HTTPException(409, detail=str(res.get("error") or "点动被拒绝"))
    return res


@router.post("/jog/start")
def api_jog_start(body: JogStartIn, tok: str = Depends(require_control)):
    """连续点动：按住期间以设定速度持续走；前端需周期性 keepalive，否则看门狗自动停。"""
    if body.dir not in (1, -1):
        raise HTTPException(400, detail="dir 只能是 +1 或 -1")
    frame, ferr = jf.norm_frame(body.frame)
    if ferr:
        raise HTTPException(400, detail=ferr)
    _reject_if_run_active("连续点动")
    res = jog.start(body.joint, body.dir, _jog_speed_pct(body),
                    frame=frame, user_frame=body.user_frame, slow=body.slow)
    unit = jf.unit_of(frame, body.joint)
    name = _axis_name(frame, body.joint)
    _jog_emit(tok, "control.jog_start", bool(res.get("ok")),
              (f"连续点动[{_frame_label(frame, body.user_frame)}] "
               f"{name}{'+' if body.dir > 0 else '-'} 起，{_jog_speed_pct(body)}%（全局速度）"
               + ("（慢速）" if body.slow else "")
               if res.get("ok") else f"连续点动被拒：{res.get('error')}"),
              {"joint": body.joint, "dir": body.dir, "speed_pct": _jog_speed_pct(body),
               "frame": frame, "user_frame": body.user_frame, "slow": body.slow,
               "blocked": bool(res.get("blocked"))})
    _jog_mode_note(tok, res)
    if not res.get("ok") and res.get("blocked"):
        raise HTTPException(409, detail=str(res.get("error") or "点动被拒绝"))
    return res


@router.post("/jog/keepalive")
def api_jog_keepalive():
    """死人开关保持信号（前端 ~4Hz 调用）；超时未收到即自动停。"""
    return jog.keepalive()


@router.post("/jog/stop")
def api_jog_stop(tok: str = Depends(require_control)):
    """松开 / 切轴 / 切窗口 → 立即停止点动。"""
    res = jog.stop("user")
    if res.get("stopped_motion"):
        emit_event("control", "info", "control.jog_stop", "连续点动已停止",
                   {"reason": "user"}, actor=_actor(tok))
    return res

