# -*- coding: utf-8 -*-
"""三色方块分拣（独立分拣页后端）—— 流程引擎 + 接口。

链路（全部经控制器常驻程序 210，不加载新程序）：
  取料：移到 A{i}上 → 移到 A{i} → 吸（40135.Bit1）
  送检：抬起 → 移到 C检测 → 停顿 dwell_ms → 抓 N 帧判色（/vision/detect）
        ★ 送检全程**保持吸气**（用户 2026-10-08 第二轮："吸气一直吸住"）
  放料：按颜色选 B 槽 → 移到 B{j}上（正上方）
        → 停止吸(Bit2) → 放(Bit3) → 等 blow_ms（默认 500）→ 停止放(Bit4) → 回开始/结束点
        ★ 放气**就地停**（不再保持到回起点）：吹够时长立刻关，避免带着吹气走一路
  收尾：9 件完成 → 回开始/结束点

现场口径（用户 2026-10-08）：
  · 不做真空反馈检测；**吸不到/空跑/检测不到 → 直接停**，然后**回到开始/结束点**；
  · 某列放满 3 件又来同色 → 停（"对满了停"）；
  · 不做任何自动恢复/重试绕行。

安全：每步都过围栏互锁；速度沿用全局那一份；吸放与运动共用执行互斥；
      执行期间 /jog、/vacuum 会被 control 的互锁拒绝；急停随时可用。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_control
from app.api.control import new_run, register_scene_source, request_cancel
from app.core.config import get_config
from app.core.logger import get_logger
from app.db.crud import (create_point, delete_point, list_points,
                        update_point)
from app.db.database import SessionLocal
from app.services import camera_client
from app.services import sorting as S
from app.services.events import emit as emit_event
from app.services.collector import collector
from app.services.kinematics import forward_kinematics as FK
from app.services.motion import motion, real_write_enabled
from app.services.safety_guard import check as guard_check
from app.utils.joints import parse_joints

log = get_logger("sorting")
router = APIRouter(prefix="/api/sorting", tags=["sorting"])

# 同时只允许一路分拣（与序列/文件执行共用一个"在跑"的世界观）
_gate = threading.Lock()
_job: Dict[str, object] = {"thread": None, "ctx": None, "state": None, "started_at": 0.0}


# ★★ 2026-10-10：把**分拣状态**登记为 3D 的"场景状态源"之一（见 control.py 的
#   `/control/scene-state`）。前端 3D 只认那一个口 —— 于是"哪种执行在跑，3D 都跟着动"。
def _scene_state() -> Optional[Dict]:
    with _gate:
        st = _job.get("state")
    return dict(st) if isinstance(st, dict) else None


register_scene_source("sorting", _scene_state)

# ★★ 2026-10-08 用户现场反馈："已停止：检测不到（无有效判定…）——物体都没取、
#    也没来到摄像头那边啊"。根因：**判色在"机器人根本没动"的情况下也照做** ——
#    空跑（dry_run）会跳过全部运动，双闸未开时运动也只是走仿真，两种情况下
#    相机前面根本没有方块，必然"检测不到"→ 于是第一件就停，看起来像程序乱了。
#    修正口径（用户："来到摄像头再检测，不然就不要这个"）：
#      1) 空跑 / 双闸未开 → **不调相机、不判色、不因检测不到而停**，
#         用轮换占位色把"点位 + 选槽 + 落料"整套逻辑走通（状态里标 dry）；
#      2) 真实下发 → 先校验"确实到了 C 检测点"（关节偏差容差），
#         没到就如实报"未到达 C 检测点"，而不是含糊地报"检测不到"。
_DRY_PLACEHOLDER_COLORS = ("红", "绿", "蓝")
CAM_ARRIVE_TOL_DEG = 3.0        # 判色前的到位容差（度）


def _camera_deviation(pmap: Dict[str, object], cam: str) -> Optional[float]:
    """当前实际姿态与 C 检测点的**最大关节偏差（度）**；读不到实时姿态返回 None。"""
    snap = collector.get_latest()
    if not snap:
        return None
    try:
        live = [float(snap["j%d" % i]) for i in range(1, 7)]
        tgt = _joints_of(pmap, cam)
    except Exception:
        return None
    return max(abs(a - b) for a, b in zip(live, tgt))


class _Stop(Exception):
    """需要停止并回原点的异常（吸不到/检测不到/列满/运动失败…）。"""

    def __init__(self, reason: str, stage: str = "", piece: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.piece = piece


class StartIn(BaseModel):
    dry_run: bool = Field(default=True, description="空跑：只校验点位与读帧判色，绝不下发运动")
    speed_pct: int = Field(default=5, ge=5, le=100, description="沿用右上角那份全局速度")
    # ★ 2026-10-10 用户口径：C 位停留 **0.5 秒**，并且**这 0.5 秒内抓完 N 帧**（默认 3 帧）。
    #   帧间隔见 FRAME_GAP_S：3 帧跨度 = 2 × 0.15 = 0.3s ≤ 0.5s。
    dwell_ms: int = Field(default=500, ge=0, le=10000,
                          description="C 区停顿时长（ms，默认 500；其间抓 N 帧判色）")
    # ★ 2026-10-09 新增（用户流程口径）："吸取 → 停一下 → 抬起"。
    #   吸上后**先停一下再抬**，让真空建立/料被完全吸住 —— 立刻抬起容易在半空掉件，
    #   而现场没有真空反馈，掉了也只能等判色失败才发现。
    # ★ 2026-10-10 用户口径：停顿时长 **1s → 0.5 秒**。
    suck_hold_ms: int = Field(default=500, ge=0, le=5000,
                              description="吸取后停留（ms，默认 500）：等吸牢再抬起")
    frames: int = Field(default=3, ge=1, le=9, description="停顿后抓几帧判色")
    min_conf: Optional[float] = Field(default=None, ge=0.0, le=1.0,
                                      description="留空则用视觉服务当前阈值")
    # ★ 2026-10-08 第二轮语义变更（用户："到了B区停止吸气然后再放气0.5秒，停止放气"）：
    #   放气**不再保持到回起点**，而是"在 B 正上方吹够这么久就地停"。
    #   所以这个值就是**放气持续时间**，默认 500ms。
    blow_ms: int = Field(default=500, ge=0, le=3000,
                         description="放气持续时间（ms，默认 500；吹完就地「停止放气」）")
    max_pieces: int = Field(default=9, ge=1, le=9, description="本轮做几件（调试可设 1）")
    # ★★ 2026-10-10 双面（一趟两件）总开关：**默认 False → 走下面那条原单件编排（一行未改）**。
    #   只有显式打开时才走 `_run_sorting_dual_face`（J6 双面吸盘：一趟两面各吸一件，
    #   靠 J6 翻转切换面；放件用"停止吸→放→停止放→立刻吸"的原地四步）。
    #   见 docs/方案-J6双面吸盘（一趟两件·靠J6翻转用另一面）.md。
    dual_face: bool = Field(default=False,
                            description="J6 双面吸盘一趟两件（默认 False = 原单件路径）")


# ---------------------------------------------------------------- 只读
@router.get("/points")
def api_points(tok: str = Depends(require_control)):
    """38 点位体检：缺哪些、列映射是什么。缺任何一项都不允许开跑。"""
    db = SessionLocal()
    try:
        names = [p.name for p in list_points(db)]
    finally:
        db.close()
    return {"ok": True, **S.check_points(names)}


@router.get("/state")
def api_state(tok: str = Depends(require_control)):
    """分拣进度快照（阶段/第几件/判色/三列占用/停止原因）。"""
    with _gate:
        st = dict(_job.get("state") or {}) or None
        running = bool(_job.get("thread") and _job["thread"].is_alive())
    return {"ok": True, "running": running, "state": st}


# ★ 3D 可视化默认尺寸（现场未提供实测值时的默认；可由查询参数覆盖）
#   ★ 2026-10-08 用户反馈"方块太小看不清"：30 → **70mm**（槽位实测间距 85~105mm，
#     70 留出 15~35mm 间隙，既能一眼看清又不会互相压住；不重叠判定仍用 70×1.05=73.5 < 85 ✓）
BLOCK_MM_DEFAULT = (70.0, 70.0, 14.0)     # 方块 长×宽×厚
CAMERA_H_MM_DEFAULT = 0.0                 # 相机底座离**摆放面**的高度（0 = 直接坐在台面/地面上）
# ★★ 摆放面高度（基座系 mm，0 = 站台台面 = 机器人底座平面）—— 用户 2026-10-08 第二轮：
#    "19 个摆放的 3D 物体怎么悬浮起来了啊，放在地上啊"
#
#    根因是我**把示教点的 Z 当成了料的高度**。示教点存的是关节角，FK 算出来的是
#    **法兰中心**（见 kinematics 的 REF 注释："法兰位置"），那是**工具安装面**，
#    不是料的表面 —— 料在工具下方，低着一个"末端工具长度"。
#    佐证：A1 法兰 Z=145.94、A1上 Z=180.28，两点**只差 34.3mm 且是纯竖直** = 抬升避让，
#    说明料厚 ≤34mm（真实料≈30mm），而不是"料在 146mm 高处"。
#    A/B/C 三区实测 X 285~578 / Y ±446mm **全在站台台面范围内（±655mm）**，
#    所以正确做法是：**统一坐在站台台面上**（= 机器人底座同一平面 = 基座系 Z=0），
#    示教点的 Z 只用来做共面性体检，**不参与摆放高度**。
PLANE_H_MM_DEFAULT = 0.0
PLANE_TOL_MM = 5.0                        # 槽位共面性告警阈值


@router.get("/blocks")
def api_blocks(block: Optional[str] = None, cam_h: Optional[float] = None,
               plane_h: Optional[float] = None,
               tok: str = Depends(require_control)):
    """18 个槽位 + C 检测工位的**场景摆位数据**（供分拣页 3D 画方块/相机）。

    ★ 位置从哪来：示教点存的是**关节角**，用前向运动学（FK）求出**法兰中心**位置 ——
      不是相机识别的实际位置（界面必须标注"按示教点位示意，非视觉定位"）。
    ★ 单位/坐标系：返回**机器人基座系、Z 向上、mm**；映射到 three.js 场景
      （Y-up、米、抬到台面）由前端 `sceneCoords.js` 统一做，那里有唯一的换算函数。
    ★★ 高度口径（2026-10-08 第二轮修正）：所有摆放物的**底面坐在 `plane_h_mm` 平面上**，
      默认 0 = 站台台面 = 机器人底座平面。**不再用示教点的 Z 当摆放高度** ——
      那是法兰高度（含工具长度），拿它当料高 → 料会悬在台面上方 15cm。
    ★ `plane_z` / `plane_spread`：**只作为体检数据**（示教法兰高度的中位数与共面偏差），
      用来发现"某几个点没示教平"，不参与摆放。
    ★ `overlaps`：任两槽位中心距 < 方块边长 × 1.05 → 报"重叠风险"，前端**不画那两个**。
    """
    block_mm = BLOCK_MM_DEFAULT
    if block:
        try:
            parts = [float(v) for v in str(block).replace("x", ",").replace("*", ",").split(",")]
            if len(parts) == 3 and all(v > 0 for v in parts):
                block_mm = (parts[0], parts[1], parts[2])
        except (TypeError, ValueError):
            pass
    cam_h_mm = float(cam_h) if cam_h else CAMERA_H_MM_DEFAULT
    plane_h_mm = float(plane_h) if plane_h else PLANE_H_MM_DEFAULT

    db = SessionLocal()
    try:
        rows = {p.name: p for p in list_points(db)}
    finally:
        db.close()

    def _fk_pose(name: str):
        """取该示教点的末端位置（基座系 mm）；缺失/坏数据 → (None, False)。"""
        row = rows.get(name)
        if row is None:
            return None, False
        try:
            j = parse_joints(getattr(row, "joints", "[]"), where="点位「%s」" % name)
            tcp = FK(j)["tcp"]
            return [round(float(tcp[0]), 2), round(float(tcp[1]), 2), round(float(tcp[2]), 2)], True
        except Exception:  # noqa: BLE001
            return None, False

    slots: List[Dict] = []
    missing: List[str] = []
    for role, names in (("A", S.A_SLOTS), ("B", S.B_SLOTS)):
        for i, nm in enumerate(names, start=1):
            pos, taught = _fk_pose(nm)
            if not taught:
                missing.append(nm)
            slots.append({"name": nm, "role": role,
                          "row": (i - 1) // 3 + 1, "col": (i - 1) % 3 + 1,
                          "pos": pos, "taught": taught})
    cam_pos, cam_taught = _fk_pose(S.CAMERA_POINT)
    if not cam_taught:
        missing.append(S.CAMERA_POINT)

    zs = [s["pos"][2] for s in slots if s["taught"]]
    plane_z = round(sorted(zs)[len(zs) // 2], 2) if zs else 0.0
    spread = round(max(abs(z - plane_z) for z in zs), 2) if zs else 0.0

    # 重叠检查（只在 XY 平面看中心距：方块是扁的，同平面摆放）
    need = max(block_mm[0], block_mm[1]) * 1.05
    overlaps: List[Dict] = []
    ok_slots = [s for s in slots if s["taught"]]
    for i in range(len(ok_slots)):
        for k in range(i + 1, len(ok_slots)):
            a, b = ok_slots[i], ok_slots[k]
            dx = a["pos"][0] - b["pos"][0]
            dy = a["pos"][1] - b["pos"][1]
            d = (dx * dx + dy * dy) ** 0.5
            if d < need:
                overlaps.append({"a": a["name"], "b": b["name"],
                                 "dist": round(d, 2), "need": round(need, 2)})

    return {"ok": True, "unit": "mm_base", "frame": "robot-base Z-up mm",
            "block_mm": list(block_mm), "camera_height_mm": cam_h_mm,
            "plane_h_mm": plane_h_mm,
            "plane_z": plane_z, "plane_spread": spread, "plane_tol": PLANE_TOL_MM,
            "slots": slots,
            "camera": {"name": S.CAMERA_POINT, "role": "C",
                       "pos": cam_pos, "taught": cam_taught},
            "overlaps": overlaps, "missing": missing,
            "note": "位置由示教点位经前向运动学算出（按示教点位示意，非视觉定位）",
            "height_note": ("摆放面 = 站台台面（机器人底座平面）+ plane_h_mm；"
                            "plane_z 是示教法兰高度，仅用于共面性体检，不作为摆放高度")}


# ---------------------------------------------------------------- 启停


# ---------------------------------------------------------------- 启停
@router.post("/cancel")
def api_cancel(tok: str = Depends(require_control)):
    """停止分拣（与「停止执行」同语义：能在步边界与飞行中打断）。"""
    hit = request_cancel()
    return {"ok": True, "cancelled": hit}


@router.post("/start")
def api_start(body: StartIn, tok: str = Depends(require_control)):
    """开始分拣（默认 dry_run=true 空跑）。"""
    with _gate:
        if _job.get("thread") and _job["thread"].is_alive():
            raise HTTPException(409, detail="已有分拣在进行，请先「停止」")
        if not body.dry_run and not real_write_enabled():
            raise HTTPException(403, detail="真实下发未开启（EFORT_REAL_MOTION=1 且 motion.real_write=true）")

    # ★★ 2026-10-10：与**井字棋对局**互斥 —— 两者共用 40135 触发位，
    #   对局进行中开分拣会交错下发（机器人走一半、完成位对不上）。
    #   延迟导入避免与 tictactoe 模块形成 import 环（tictactoe 反过来 import 本模块原语）。
    from app.api.tictactoe import is_game_running as _tt_running
    if _tt_running():
        raise HTTPException(409, detail="井字棋对局进行中，先「停止」对局再开始分拣")

    # ---- 前置体检（任一不过就拒绝启动，不给"跑了但做不了"的假象）----
    db = SessionLocal()
    try:
        rows = list_points(db)
        pmap = {p.name: p for p in rows}
    finally:
        db.close()
    chk = S.check_points(pmap.keys())
    if not chk["ok"]:
        raise HTTPException(400, detail={
            "message": "点位不齐，缺 %d 个，先补齐再开跑" % len(chk["missing"]),
            "missing": chk["missing"], "total": chk["total"]})

    # ★ 视觉服务前置校验：**只在真的要判色时才要求相机就绪**。
    #   空跑 / 双闸未开时不判色（物体不会到 C 区），这时强制要求相机在线
    #   只会让用户"想空跑验证点位却被相机挡住"，属于把无关条件当门槛。
    need_vision = (not bool(body.dry_run)) and real_write_enabled()
    min_conf = 0.3
    if need_vision:
        min_conf = vision_preflight(body.min_conf)

    ctx = new_run("三色分拣", kind="sorting")
    # ★ 每轮**全新状态**（"每次运行必须从头开始"）—— 由 services/sorting.fresh_run_state()
    #   统一构造，进度/占位/判色/停止原因/夹持标记全部归零，且引擎从 A1 第一格重跑。
    fresh = S.fresh_run_state(body.dry_run, body.max_pieces, need_vision)
    # ★★ 启动响应必须回**线程启动前的快照**（不是活动字典）：
    #   `_job["state"]` 是执行线程正在写的活字典，直接返回等于把"还会被改的对象"
    #   交给调用方 —— 实测（空跑跑得极快）启动响应里 piece/占位已经是**跑完**的值，
    #   前端拿它做"复位 3D / 清上一轮痕迹"就会清错，看起来像"没有从头开始"。
    #   所以先深拷贝一份冻住的初始快照，再启动线程。
    import copy as _copy
    snap = _copy.deepcopy(fresh)
    with _gate:
        _job["state"] = fresh                      # 线程这份是活的
        _job["started_at"] = time.time()
        th = threading.Thread(target=_run_sorting, args=(ctx, body, pmap, min_conf),
                              name="sorting", daemon=True)
        _job["thread"] = th
        _job["ctx"] = ctx
    th.start()
    ctx.set_thread(th)      # ★ 绑定执行线程：让"卡死登记"能被 is_alive() 精确识别
    emit_event("sorting", "info", "sorting.start",
               "开始分拣（%s，速度 %d%%，%d 件，抓 %d 帧）" % (
                   "空跑" if body.dry_run else "真实下发", body.speed_pct,
                   body.max_pieces, body.frames),
               {"dry_run": body.dry_run, "speed_pct": body.speed_pct,
                "frames": body.frames, "dry_run_note": "空跑不下发运动"},
               actor="control")
    return {"ok": True, "run_id": ctx.run_id, "dry_run": body.dry_run, "state": snap}


# ---------------------------------------------------------------- 引擎
def _set(st=None, **kw) -> None:
    """写阶段状态。

    ★ `st=None` → 写分拣自己的 `_job["state"]`（原行为不变）；
      传 dict → 写**那一份**（AI 执行页复用同一套运动/吸放/判色原语，
      但进度要落在它自己的 state 上，不能互相覆盖）。
    ★ 同一套原语、两份 state —— 这是"只保留一处实现"与"各自上报进度"的折中。
    """
    target = st if st is not None else _job.get("state")
    if isinstance(target, dict):
        with _gate:
            target.update(kw)


def _joints_of(pmap: Dict[str, object], name: str) -> List[float]:
    row = pmap.get(name)
    if row is None:
        raise _Stop("点位缺失：%s" % name)
    try:
        return parse_joints(getattr(row, "joints", "[]"), where="点位「%s」" % name)
    except ValueError as e:
        raise _Stop(str(e))


def _move(ctx, pmap, name, speed, dry, stage, st=None) -> None:
    """走一个示教点位（写目标 + 触发位 → 常驻 210 执行 MJOINT）。"""
    if ctx.wait_pause():
        raise _Stop("已被停止", stage)
    j = _joints_of(pmap, name)
    if dry:
        _set(st, stage=stage, last_point=name)
        return
    ok, reason, _snap = guard_check()
    if not ok:
        raise _Stop("安全围栏互锁：%s" % reason, stage)
    r = motion.command([float(v) for v in j], int(speed), 0,
                       abort_check=lambda: ctx.cancelled())
    _set(st, stage=stage, last_point=name)
    if not r.get("ok"):
        raise _Stop("运动到「%s」失败：%s" % (name, r.get("error")), stage)


def _air(ctx, action: str, dry: bool, stage: str, st=None) -> None:
    """吸/停止吸/放/停止放（40135.Bit1~Bit4 → 常驻 210 执行）。

    ★ 与运动共用 40135 寄存器，必须取执行互斥（否则与在飞的点动互相清触发位）。
    """
    label = {"suck": "吸", "release": "停止吸", "blow": "放", "unblow": "停止放"}.get(action, action)
    if dry:
        # ★★ 2026-10-09 修：空跑时原来写的是 `"%s(空跑)" % label` —— 把调用方传进来的
        #   **语义化阶段名丢了**，只剩「吸(空跑) / 放(空跑)」，而"吸"和"放"在流程里
        #   是两个完全不同的步骤（取料·吸 / 放料·放气）→ 界面的**执行流程高亮会跳错步**
        #   （空跑时看不出到底走到哪一步，而空跑正是最常用来排练流程的模式）。
        #   改为保留 stage 语义名 + 空跑标注。
        _set(st, stage=("%s(空跑)" % stage) if stage else ("%s(空跑)" % label))
        return
    try:
        svc = int(get_config().get("motion", "jog", "service_program", default=0) or 0)
    except Exception:
        svc = 0
    if svc <= 0:
        raise _Stop("未配置常驻服务程序号（motion.jog.service_program）", stage)
    if not motion._exec_lock.acquire(blocking=False):
        raise _Stop("有点动/下发正在执行，%s 已拒绝" % label, stage)
    try:
        okp, perr = motion.modbus.rc_vacuum_prepare(svc)
        if not okp:
            raise _Stop("常驻服务程序未就绪：%s" % perr, stage)
        ok, verr, _d = motion.modbus.rc_vacuum(action, timeout=10.0)
    finally:
        motion._exec_lock.release()
    _set(st, stage=label)
    if not ok:
        raise _Stop("%s 失败：%s" % (label, verr), stage)


# ★★ 判色失败时补一句"最可能的原因"。2026-10-09 现场实例：
#   用户执行 AI 指令「A区第1行第1列是什么颜色」，报「检测不到（有效帧不足 1/3，需 ≥2）」，
#   而**真因是 A 区已经空了** —— 前一轮分拣把 9 件全取走、B 区三列全满，
#   A1 里根本没有料可拿，镜头前自然什么都判不出（那 1 帧"红"很可能是拍到了
#   工具的**红色吸盘**，吸盘本身是红的）。
#   ★ 现场**没有料位传感器**，程序无法自己知道 A 区有没有料 —— 所以只能在提示语里
#     把这条排查方向摆出来，让人一眼能对上"哦，我该补料了"。
_HINT_NO_PIECE = "（若 A 区已无料/未放料请先补料；也可能是件没吸住、或红色吸盘挡住了视野）"


# ★ 2026-10-10 用户口径："摄像头点位 0.5 秒内取三帧图片"。
#   帧间隔固定 0.15s → 3 帧跨度 = 2×0.15 = **0.3s ≤ 0.5s**（在 C 位停留窗口内取完）。
#   ⚠ 别把它调大到 >0.25s，否则 3 帧跨度会超过 0.5s（这条在 test_sorting 里有钉子）。
FRAME_GAP_S = 0.15


def _capture(ctx, frames: int, min_conf: float, stage: str, st=None,
             valid_colors: Optional[Sequence[str]] = None) -> Dict:
    """停顿后连抓 N 帧 → 投票。任何一帧失败都记下来，最终交给投票判"检测不到"。

    ★ `valid_colors`（2026-10-10，用户口径"用户提到的颜色才是有效色"）：
      · 不传（默认）= 红/绿/蓝（**三色分拣 / 井字棋口径，零回归**）；
      · AI 链路传 `S.RECOGNIZED_COLORS`（7 色）→ 检测器**如实报出真实颜色**（含橙/黄/青/紫），
        再由"配额 / 摆法"决定放走还是放回 —— 于是"用户没提到的颜色"表现为**诚实的放回**，
        而不是被误报成"无法识别"（那样现场会以为是识别坏了）。
    """
    got: List[Dict] = []
    for i in range(int(frames)):
        if ctx.cancelled():
            raise _Stop("已被停止", stage)
        st_, js, err = camera_client.get_json("/vision/detect", timeout=6)
        if st_ == 200 and not err and isinstance(js, dict):
            got.append(js)
        else:
            got.append({})
        if i + 1 < int(frames):
            ctx.sleep(FRAME_GAP_S)
    v = S.vote_color(got, min_conf, valid_colors=valid_colors)
    log.info("判色 %d 帧 → %s (conf=%s) %s", len(got), v.get("color"),
             v.get("conf"), v.get("votes"))
    return v


# ---------------------------------------------------------------- 双面（一趟两件）原语
# ★★ 用户口径（方案 §7-e）：J6 正/反面的姿态**由用户示教**，代码**不自算 +180°**。
#   `J6反面` 就是一个**普通示教点位**（像 A1/A2，用户可增删改），代码只按名字取用。
# ★ 名字必须与界面录入时用的名字**完全一致**：`frontend/src/components/SortingView.vue`
#   的 extraSlots 里写的就是 "J6反面"；它同时也在 `S.OPTIONAL_EXTRA_POINTS` 里
#   （→ 在示教白名单内，界面能存点/到点/删除）。三处任一改名都必须同步（回归钉）。
DUAL_FACE_FLIP_POINT = "J6反面"
# ★ 每面帧数：正面 2 + 反面 2 = **4 帧 / 2 个独立结论**（方案 §2 步 4）。
#   与单面口径一样，帧跨度 = (N-1)×FRAME_GAP_S 必须 ≤ 0.5s（C 位停留窗口，回归钉）。
DUAL_FRAMES_PER_FACE = 2


def _rotation_guard(name: str) -> None:
    """★ 方案 §6-1 回归钉（**2026-10-11 更新口径**）：**带 J6 翻转的下发只禁止落在原料位**。

    口径（用户原话）：「J6 反正不能在**原位**进行旋转！其他的时候可以旋转」
      —— 「原位」= **原料位（A 区格位本身，如 A1…A9）**。
    判据是**黑名单**（见 `S.is_raw_material_slot`）：**只有原料位禁止，其余一切允许**
    （正上位 / J6正面 / J6反面 / C检测 / 摄像头位 / 开始/结束点 / 起点 / home … 都放行）。

    在**原料位（A 区格位本身）**请求旋转 → `_Stop` 拒绝并写明原因（这条改不掉）。
    """
    if not S.rotation_point_allowed(name):
        raise _Stop("拒绝 J6 翻转：点位「%s」为原料位（A 区格位）—— **原料位严禁旋转**（其余位置均可）" % name)


def _move_flip(ctx, pmap, name, speed, dry, stage, st=None) -> None:
    """**带 J6 翻转的下发**：先过旋转**黑名单**（`_rotation_guard`）再走普通移动。

    与 `_move` 的唯一区别就是那道护栏 —— 原料位翻转会被**拒绝在下发之前**，
    不会出现"已经写下去才后悔"。
    """
    _rotation_guard(name)
    _move(ctx, pmap, name, speed, dry, stage, st)


def _move_flip_j6(ctx, pmap, name, speed, dry, stage, st=None) -> None:
    """★ J6 双面翻转：**只覆盖第 6 轴**，J1~J5 保持**当前实时姿态**原样不动。

    为什么不能直接下发该点的完整 6 关节（原来 `_move_flip`→`_move` 就是那样）：
      · 用户口径（方案 §7-e / §3）：`J6正面`/`J6反面` 是"**同一姿态、只有 J6 不同**"；
      · 但 `J6正面`/`J6反面` 是通过 `/sorting/teach` 存的普通点位 —— 示教时
        **未指定轴就把"当时实时 6 轴整组"存下**（见 `api_teach`）。
      · 于是直接下发该点 → 会把**录点当时**的 J1~J5 **一起**带到翻转动作里 →
        现场表现为"只是翻个 J6，J1~J5 却也跑了/停的位置不对"（用户反馈："J6 的坐标不对"）。
    ⇒ 所以这里改成：**基姿态 = 当前实时关节**（`_live_joints()`），**只把第 6 轴**替换成
      该点存的 `joints[5]`，J1~J5 原样不动 —— 与 `jog._step_joint`「以当前姿态为基 + 只改某轴」
      是同一套口径（可对照 `app/services/jog.py`）。

    ★ 仍传 **6 个值** 给 `motion.command`（`motion.py` 强制 len==6，本函数不改那条铁律）。
    ★ 旋转**黑名单**（`_rotation_guard`）**先过**：顺序、语义与 `_move_flip` 一致（**原料位禁止**）。
    ★ 基姿态取不到时（空跑/离线/读不到实时关节）：**回退**为"该点自身的 6 轴姿态"，
      并在日志里**如实标注**「基姿态不可得，改用点位自带姿态」—— 不许悄悄用。
    """
    _rotation_guard(name)
    if ctx.wait_pause():
        raise _Stop("已被停止", stage)
    j6 = _joints_of(pmap, name)[5]      # 该点第 6 轴（用户示教的反面角）
    base: Optional[List[float]] = None
    try:
        base = _live_joints()
    except Exception as e:  # noqa: BLE001 —— 含 HTTPException(503)：读不到实时姿态
        log.warning("翻转「%s」：基姿态不可得（%s），改用点位自带姿态（J1~J5 不再保持当前值）",
                    name, e)
    if base is None:
        # ★ 回退路径：日志已如实标注原因（上面那条 warning），再做整点下发。
        _move(ctx, pmap, name, speed, dry, stage, st)
        return
    target = [float(v) for v in base]
    target[5] = float(j6)
    if dry:
        _set(st, stage=stage, last_point=name)
        return
    ok, reason, _snap = guard_check()
    if not ok:
        raise _Stop("安全围栏互锁：%s" % reason, stage)
    r = motion.command(target, int(speed), 0,
                       abort_check=lambda: ctx.cancelled())
    _set(st, stage=stage, last_point=name)
    if not r.get("ok"):
        raise _Stop("运动到「%s」失败：%s" % (name, r.get("error")), stage)


def _dual_drop(ctx, dry: bool, st=None, keep: bool = False) -> None:
    """★ 一趟两件的**原地放件四步**（方案 §5.2，顺序即工艺）：`停止吸 → 放 → 停止放 → 立刻吸`。

    ★ 为什么必须"立刻复吸"：全项目**只有一路真空**（io.DOut[8]），两面共用它 ——
      `停止吸` + `放` 会让**两件一起失真空**；要放的那件被吹落在台面/料上
      （用户："方块在物体上面不会掉落"），另一件只能靠残余负压 + 惯性吊着 →
      所以**立刻 `吸`** 把它重新吸住。
    ★ 窗口越短，悬挂那件越安全：这四步**原地连续**做完，**中间不加任何 dwell、
      不插入任何移动**（回归钉；方案 §5.3 已如实写明"悬挂件可能掉"的风险）。
    ★ `keep=False`（放最后一件）→ 不复吸（放完手上就没有悬挂件了）。
    """
    _air(ctx, "release", dry, "落料·停止吸", st)
    _air(ctx, "blow", dry, "落料·放气", st)
    _air(ctx, "unblow", dry, "落料·停止放气", st)
    if keep:
        _air(ctx, "suck", dry, "落料·复吸", st)


def _capture_per_face(ctx, faces: Sequence[str], frames_per_face: int,
                      min_conf: float, stage: str) -> List[Dict]:
    """**按面**抓帧投票：每个面各抓 `frames_per_face` 帧 → `vote_color_per_face` 分组结论。

    ★ 判据**完全复用现有 `vote_color`**（经 `vote_color_per_face` 只做分组）——
      绝不新造第二套判色逻辑（那正是本项目最忌讳的分叉）。
    ★ 帧间隔沿用 `FRAME_GAP_S`：单面跨度 = (N-1)×0.15 ≤ 0.5s（与单面 `_capture` 同一约束）。
    """
    groups: List[List[Dict]] = []
    for _slot in list(faces):
        got: List[Dict] = []
        for i in range(int(frames_per_face)):
            if ctx.cancelled():
                raise _Stop("已被停止", stage)
            st_, js, err = camera_client.get_json("/vision/detect", timeout=6)
            if st_ == 200 and not err and isinstance(js, dict):
                got.append(js)
            else:
                got.append({})
            if i + 1 < int(frames_per_face):
                ctx.sleep(FRAME_GAP_S)
        groups.append(got)
    vs = S.vote_color_per_face(groups, min_conf)
    log.info("双面判色 每面 %d 帧 → %s", int(frames_per_face),
             [(v.get("slot"), v.get("color")) for v in vs])
    return vs


def _put_back(ctx, pmap, src_slot: str, speed: int, dry: bool,
              blow_ms: int = 500, prefix: str = "放回原位", st=None,
              go_home: bool = True, keep: bool = False) -> None:
    """★ 2026-10-10 用户口径：**判不出色（或自证不符）→ 把件原路放回取料格**。

    调用前提：件正被吸着、机器人停在半路（分拣停在 C 检测位）。
    工艺与"取料"完全对称、只是反过来：
        当前位置 → A{n}上方 → 下落 → 停止吸 → 放气 → 等 blow_ms → 停止放气 → 抬起 → 回起点

    ★ 为什么必须"回到原格上空再放"而不是就地松手：就地松件会掉在 C 区/半路，
      位置随机 —— 现场得满地找件，而且下一次取料还会撞上它。
    ★ 两路气在**这里正常关断**（不靠异常收尾兜底），所以调用方随后要把
      suck_on/blow_on 置 False，免得收尾再关一次。

    ★ 两个**双面（一趟两件）**专用开关（默认值 = 既有单件行为，**零回归**）：
      · `keep=True`  → 手上还吊着另一件：放完**不再等待**，改走"停止放 → **立刻复吸**"
        （四步窗口越短，悬挂那件越安全）；收尾 `holding` 也按 keep 记（仍为流程推断）。
      · `go_home=False` → **不回起点**（双面"两件都放完才回起点"，件与件之间不能回起点）。
    """
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·到原格上方" % prefix, st)
    _move(ctx, pmap, src_slot, speed, dry, "%s·下落" % prefix, st)
    _air(ctx, "release", dry, "%s·停止吸" % prefix, st)
    _air(ctx, "blow", dry, "%s·放气" % prefix, st)
    if keep:
        # 还有件挂着 → 原地四步收尾：立刻停止放 + 复吸（不加 dwell）
        _air(ctx, "unblow", dry, "%s·停止放气" % prefix, st)
        _air(ctx, "suck", dry, "%s·复吸" % prefix, st)
    else:
        if int(blow_ms) > 0:
            ctx.sleep(int(blow_ms) / 1000.0)
        _air(ctx, "unblow", dry, "%s·停止放气" % prefix, st)
    _set(st, holding=bool(keep), stage="%s·完成" % prefix)
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·抬起" % prefix, st)
    if go_home:
        _move(ctx, pmap, S.START_POINT, speed, dry, "%s·回起点" % prefix, st)


def vision_preflight(body_min_conf: Optional[float] = None) -> float:
    """视觉服务前置校验 + 取本次判色门槛（**分拣 / 井字棋拍照自证 共用**）。

    ★ 只在"真的要判色"时才调用：空跑 / 双闸未开时物体到不了 C 区，
      判色必然"检测不到"，拿相机当门槛只会挡住空跑验证（原分拣口径）。
    ★ 校验项与文案与原分拣实现逐字一致 —— 抽出来只为共用，不改变行为。
    返回：本次判色所用的 min_conf。
    """
    st, js, err = camera_client.get_json("/vision/status", timeout=6)
    if st != 200 or err:
        raise HTTPException(502, detail="视觉服务不可达：%s（先启动相机服务）" % (err or st))
    v = js or {}
    if not v.get("color_capable"):
        raise HTTPException(400, detail="当前相机不是彩色相机，无法判色（换彩色相机/USB 摄像头）")
    if not v.get("enabled"):
        raise HTTPException(400, detail="颜色识别未开启（摄像头面板里打开三色识别）")
    if not v.get("thread_alive"):
        raise HTTPException(400, detail="颜色识别线程未运行（视觉服务异常，重启相机服务）")
    return float(body_min_conf if body_min_conf is not None else (v.get("min_conf") or 0.3))


def _run_sorting(ctx, body: StartIn, pmap: Dict[str, object], min_conf: float) -> None:
    # ★★ 2026-10-10 双面（一趟两件）**只加这一条分支**：开关打开才走独立编排；
    #   默认 False → **下面这段原单件编排一行未改**（零回归，见 tests/test_double_face.py）。
    if bool(getattr(body, "dual_face", False)):
        return _run_sorting_dual_face(ctx, body, pmap, min_conf)
    dry = bool(body.dry_run)
    speed = int(body.speed_pct)
    # ★ 判色的前提是"机器人真的把方块送到相机前"：
    #   空跑（dry）会跳过所有运动；双闸未开（real=False）时运动只走仿真不动机器人。
    #   这两种情况都不许判色（详见 CAM_ARRIVE_TOL_DEG 上方注释）。
    real = real_write_enabled()
    judge = (not dry) and real
    if not judge:
        emit_event("sorting", "warn", "sorting.no_judge",
                   "本次不判色：%s（物体不会到 C 区，判色必然检测不到）"
                   % ("空跑模式" if dry else "双闸未开，运动仅仿真"),
                   {"dry_run": dry, "real_write": real}, actor="control")
    # ★★ 气路是**电平保持型**（写一次就一直保持），所以异常/取消时**必须主动关掉**：
    #   · 放气保持 → 不关就一直吹（费气，现场还以为是卡死）；
    #   · 吸气保持 → 现在送检全程吸着（用户第二轮口径），不关就是**带着负压停在半路**，
    #     件吊在吸盘上过夜、阀长期带电 —— 这是真隐患，必须收口。
    #   关阀≠一定掉件（现场实测：关了阀靠杯内残余负压照样挂着），所以 3D 的"还在手上"
    #   与此不矛盾；关阀只是不让阀门一直带电。
    blow_on = False
    suck_on = False
    start = S.START_POINT
    cam = S.CAMERA_POINT
    placement = S.new_placement()
    stop_reason = ""
    stop_piece = 0
    phase = "done"
    try:
        # 0) 回开始/结束点
        _set(stage="回开始/结束点")
        _move(ctx, pmap, start, speed, dry, "回开始/结束点")

        for idx, a_slot in enumerate(S.A_SLOTS[: int(body.max_pieces)], start=1):
            if ctx.cancelled():
                stop_reason = "已被停止"
                phase = "cancelled"
                break
            _set(piece=idx, stage="取料", color="", conf=0.0, target="",
                 holding=False, suspect_pick=False,
                 # ★ 每件开头清掉上一件的抓帧明细，否则界面会一直显示上一件的颜色
                 frame_detail=[], best_frame=None, votes={})
            # 1) 取料：上方 → 槽位 → 吸
            _move(ctx, pmap, a_slot + "上", speed, dry, "取料·上方")
            _move(ctx, pmap, a_slot, speed, dry, "取料·下落")
            _air(ctx, "suck", dry, "取料·吸")
            suck_on = True
            # ★ 2026-10-09（用户流程口径）："吸取 → **等待0.5秒** → 抬起"。
            #   等真空吸牢再抬，避免刚吸上就走导致半空掉件（现场无真空反馈，掉了只能靠判色失败发现）。
            if int(body.suck_hold_ms) > 0:
                ctx.sleep(int(body.suck_hold_ms) / 1000.0)
            # ★ 3D 用：吸合步骤跑完 = 手上"有件"（**按流程推断**，现场无真空反馈）。
            _set(holding=True)
            # 2) ★★ 抬起 → 送检：**全程保持吸气**（用户 2026-10-08 第二轮："吸气一直吸住吧"）。
            #    旧实现是抬起来就「停止吸」，靠杯内残余负压吊着件飞一路 ——
            #    路上要是掉了，判色/落料都会莫名其妙；现在负压阀一直开着，件才真被吸住。
            _move(ctx, pmap, a_slot + "上", speed, dry, "抬起")
            _move(ctx, pmap, cam, speed, dry, "送检")
            # 3) 停顿 + 判色
            _set(stage="停顿")
            if ctx.sleep(int(body.dwell_ms) / 1000.0):
                stop_reason = "已被停止"; phase = "cancelled"; break
            if not judge:
                # ★ 空跑 / 双闸未开：机器人没动，方块不可能在相机前 →
                #   绝不调相机、绝不判色、绝不因"检测不到"停（用户口径）。
                #   用轮换占位色把"选槽 + 落料"逻辑走通，状态里标 dry 以免误读成真实判色。
                ph = _DRY_PLACEHOLDER_COLORS[(idx - 1) % len(_DRY_PLACEHOLDER_COLORS)]
                _set(stage="空跑·未判色", color=ph, conf=0.0, sim_color=True,
                     target="")
                v = {"ok": True, "color": ph, "conf": 0.0, "votes": {}, "placeholder": True}
                log.info("空跑/未开双闸：跳过判色（物体未到 C 区），占位色=%s", ph)
            else:
                # ★ 真实下发：先确认真的到了 C 检测点 —— 没到就判色等于瞎判，
                #   而且失败原因会被说成"检测不到"，把人往相机上带偏。
                dev = _camera_deviation(pmap, cam)
                if dev is not None and dev > CAM_ARRIVE_TOL_DEG:
                    raise _Stop("未到达 C 检测点（实际偏差 %.1f° > %.1f°），不判色"
                                % (dev, CAM_ARRIVE_TOL_DEG), "判色", idx)
                _set(stage="判色")
                v = _capture(ctx, int(body.frames), min_conf, "判色")
                # ★ 逐帧明细一起落进 state：界面要显示"捕捉 N 张、只突出置信度最高那张"
                _set(color=v["color"], conf=v["conf"], votes=v["votes"],
                     frame_detail=v.get("frame_detail") or [],
                     best_frame=v.get("best_frame"))
                if not v["ok"]:
                    # ★★ 2026-10-10 用户口径：**判不出色 → 原路放回取料格，然后停止**。
                    #    绝不再"就地放掉"（件会掉在 C 区/半路，位置随机、现场要满地找件，
                    #    而且下一件取料还可能撞上它）。分拣 / AI / 井字棋自证三条链路统一。
                    #    也不做"跳过继续"（用户明确否掉）—— 放回完这一轮就结束。
                    _set(stage="无法识别·放回原位", target="")
                    _put_back(ctx, pmap, a_slot, speed, dry,
                              blow_ms=int(body.blow_ms), prefix="放回原位 %s" % a_slot)
                    suck_on = False          # 两路气已在 _put_back 内正常关断
                    blow_on = False
                    raise _Stop("颜色无法识别（%s）%s，已放回原位 %s"
                                % (v["reason"], _HINT_NO_PIECE, a_slot), "判色", idx)
            _set(color=v["color"], conf=v["conf"])
            # 4) 按色选槽
            plan = S.build_piece_plan(placement, v["color"])
            if not plan["ok"]:
                raise _Stop("%s" % plan["detail"], "选槽", idx)
            tgt = plan["slot"]
            _set(target=tgt)
            # 5) 放料（★ 用户 2026-10-08 口径：**只到 B 槽正上方，不下落到槽位**，
            #    件靠停止吸 + 吹气掉下去 —— 少一次下落/抬起，节拍更短）
            _move(ctx, pmap, tgt + "上", speed, dry, "放料·正上方")
            # ★★ 落料三步（用户 2026-10-08 第二轮口径，顺序即工艺，别调换）：
            #    ① 停止吸（Bit2）—— 先断负压阀
            #    ② 放气（Bit3）—— 吹断杯内残余真空，件才真掉下来
            #    ③ 等 blow_ms（默认 500ms）后**就地**停止放（Bit4）
            #    旧实现把①放在"抬起"之后、③放在"回起点"之后：一路上阀是断的（件靠残余负压吊着），
            #    而且吹着气走一路。现在改成"吸住飞、到点再断、断完就吹、吹够就停"。
            _air(ctx, "release", dry, "放料·停止吸")
            suck_on = False
            _air(ctx, "blow", dry, "放料·放气")
            blow_on = True
            if int(body.blow_ms) > 0:
                ctx.sleep(int(body.blow_ms) / 1000.0)
            _air(ctx, "unblow", dry, "放料·停止放气")
            blow_on = False
            S.place(placement, v["color"], tgt)
            _set(placement=S.placement_summary(placement), holding=False)
            # 6) 回起点（此时吸/吹都已关断，可以干净地走）
            _move(ctx, pmap, start, speed, dry, "回起点")
            emit_event("sorting", "info", "sorting.piece",
                       "第 %d 件：%s → %s（conf %.2f）" % (idx, v["color"], tgt, v["conf"]),
                       {"piece": idx, "color": v["color"], "slot": tgt,
                        "conf": v["conf"], "votes": v["votes"], "dry_run": dry},
                       actor="control")

        # 收尾：循环内每件已回起点，这里只在"没跑完就正常结束"时兜一次（幂等）
        if not stop_reason:
            _set(stage="收尾·回开始/结束点")
            _move(ctx, pmap, start, speed, dry, "收尾回原点")
    except _Stop as e:
        # ★ 异常/取消先收掉还开着的两路气（best-effort，失败不影响主线报告）：
        #   停止吸 + 停止放 —— 不吹气（在半空吹会把件吹飞，落到哪儿不知道）。
        if not dry and (blow_on or suck_on):
            try:
                _air(ctx, "unblow", False, "异常·停止放气")
            except Exception as exc:  # noqa: BLE001
                log.warning("异常收气（停止放）失败：%s", exc)
            try:
                _air(ctx, "release", False, "异常·停止吸")
            except Exception as exc:  # noqa: BLE001
                log.warning("异常收气（停止吸）失败：%s", exc)
            blow_on = False
            suck_on = False
        stop_reason = e.reason
        stop_piece = e.piece or int((_job.get("state") or {}).get("piece") or 0)
        if "停止" in e.reason:
            # ★ 人工停止（点「停止」）：件可能还在吸盘上 → 3D **如实显示"还夹着"**，
            #   不自动回位（回位就是编故事，见方案问题 8）。
            phase = "cancelled"
        else:
            phase = "error"
            # ★★ 失败停止（吸不到/空跑异常/检测不到/列满…）：现场**没有真空反馈**，
            #   无法知道件到底在不在吸盘上；用户已定口径：**3D 也不显示吸住** ——
            #   把方块画回 A 格并标红（"疑似没取到"），而不是留在手上。
            #   这里落两个字段给 3D 用：holding=False + suspect_pick=True。
            _set(holding=False, suspect_pick=True)
            # ★ 用户明确要求：做不到就直接停，**然后回到起点**
            try:
                _set(stage="异常·回开始/结束点", error=e.reason)
                _move(ctx, pmap, start, speed, dry, "异常回原点")
            except _Stop as e2:
                log.warning("异常后回原点也失败：%s", e2.reason)
                stop_reason = "%s；回原点失败：%s" % (stop_reason, e2.reason)
    except Exception as e:  # noqa: BLE001
        log.exception("分拣异常")
        stop_reason = "内部异常：%s" % str(e)[:150]
        phase = "error"
        # ★ 内部异常（非 _Stop）同样不能带着负压/吹气停在半路
        if not dry and (blow_on or suck_on):
            for act, lab in (("unblow", "停止放"), ("release", "停止吸")):
                try:
                    _air(ctx, act, False, "内部异常·%s" % lab)
                except Exception:  # noqa: BLE001
                    pass
            blow_on = False
            suck_on = False
    finally:
        # ★★ 2026-10-10：**注销运行登记最先做且不可跳过**（与 ai.py 同名修复同因）。
        #   收尾里任何一步（关气 / 写结束态 / 写事件）抛异常，都不许把 `_RUN_ACTIVE` 留在表里 ——
        #   那会让 `_reject_if_run_active()` 永久拒绝手动下发（现场"执行中但没跑代码、
        #   就绪和取消都成功、指令下不去"的死机）。
        ctx.finish(phase)
        try:
            # ★★ 兜底：**无论走哪条路径**都不许把气路留在"开着"的状态。
            #   电平保持型气路最容易出的现场事故就是"程序结束了阀还开着"。
            if not dry and (blow_on or suck_on):
                for act, lab in (("unblow", "停止放"), ("release", "停止吸")):
                    try:
                        _air(ctx, act, False, "收尾·%s" % lab)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("收尾关气（%s）失败：%s", lab, exc)
            _set(stage="结束", error=stop_reason, finished=True,
                 ok=(phase == "done"), phase=phase, piece=stop_piece or None)
            emit_event("sorting", "info" if phase == "done" else "warn", "sorting.stop",
                       ("分拣完成" if phase == "done" else "分拣结束：%s" % stop_reason),
                       {"phase": phase, "reason": stop_reason, "dry_run": dry,
                        "placement": S.placement_summary(placement)},
                       actor="control")
        except Exception:  # noqa: BLE001
            log.exception("分拣收尾异常（运行登记已注销，不影响后续指令）")


def _run_sorting_dual_face(ctx, body: StartIn, pmap: Dict[str, object],
                           min_conf: float) -> None:
    """一趟两件（J6 双面吸盘）—— **仅** `dual_face=True` 时执行。

    ★★ 默认（dual_face=False）**完全不走这里**：`_run_sorting` 顶部直接绕过本函数，
       原单件编排一行未改（零回归）。
    完整口径见 docs/方案-J6双面吸盘（一趟两件·靠J6翻转用另一面）.md（§2 流程 / §5.2 气路 / §6 护栏）。

    一趟（= 最多两件）：
      ① 正上位 → 取 A{i}（**下板**吸住）→ 抬起；
      ② 在**正上位**把 J6 转到用户示教的「J6反面」点（`_move_flip_j6` 过旋转白名单，
         且**只覆盖第 6 轴**、J1~J5 保持当前实时姿态 —— 见 `_move_flip_j6` 的说明）；
      ③ → 取 A{j}（**上板**吸住）：★ 这一步**绝不断真空**（一断第 1 件就掉，方案 §5.2）；
      ④ 到 C 检测位：**正面 N 帧 → 投票**，J6 转到反面 → **反面 N 帧 → 投票**（两个独立结论）；
      ⑤ **先放有效的那件**（按色落 B 槽）；无效件 → **原路放回它自己的 A 原位**（不消耗配额）；
      ⑥ **两件都放完才回起点**（件与件之间不回起点）。
    节拍：9 件 = 4 趟×2 + 最后 1 趟×1（见 `S.plan_trips`）。
    """
    dry = bool(body.dry_run)
    speed = int(body.speed_pct)
    real = real_write_enabled()
    # 判色前提同默认路径：空跑 / 双闸未开时物体到不了 C 区 → 不调相机、不判色、不因"检测不到"停。
    judge = (not dry) and real
    if not judge:
        emit_event("sorting", "warn", "sorting.no_judge",
                   "本次不判色（双面）：%s（物体不会到 C 区，判色必然检测不到）"
                   % ("空跑模式" if dry else "双闸未开，运动仅仿真"),
                   {"dry_run": dry, "real_write": real, "dual_face": True}, actor="control")
    # ★ 气路是电平保持型（写一次就保持）→ 异常/取消/收尾都必须主动关掉（同默认路径口径）。
    blow_on = False
    suck_on = False
    start = S.START_POINT
    cam = S.CAMERA_POINT
    placement = S.new_placement()
    stop_reason = ""
    stop_piece = 0
    phase = "done"
    state = _job.get("state")
    if not isinstance(state, dict):
        state = {}
    faces_n = len(S.FACE_SLOTS)
    trips = S.plan_trips(list(S.A_SLOTS[: int(body.max_pieces)]), per_trip=faces_n)
    done = 0
    try:
        _set(stage="回开始/结束点", dual_face=True)
        _move(ctx, pmap, start, speed, dry, "回开始/结束点")
        for trip in trips:
            if ctx.cancelled():
                stop_reason = "已被停止"; phase = "cancelled"; break
            first = trip[0]
            _set(piece=done + 1, stage="取料", color="", conf=0.0, target="",
                 hold_lower=None, hold_upper=None, holding=False, suspect_pick=False,
                 frame_detail=[], best_frame=None, votes={})

            # ① 取第 1 件（下板）：上方 → 下落 → 吸 → 等吸牢 → 抬起
            _move(ctx, pmap, first + "上", speed, dry, "取料·上方")
            _move(ctx, pmap, first, speed, dry, "取料·下落")
            _air(ctx, "suck", dry, "取料·吸")
            suck_on = True
            if int(body.suck_hold_ms) > 0:
                ctx.sleep(int(body.suck_hold_ms) / 1000.0)
            S.face_slot_put(state, "lower", piece=first, ok=True)
            _move(ctx, pmap, first + "上", speed, dry, "抬起")

            # ② 在正上位翻转 J6 → 用另一面（旋转白名单；只有两件趟才需要）
            #   ★ 用 `_move_flip_j6`：**只覆盖 J6**、J1~J5 保持当前实时姿态
            #     （用户反馈："J6 的坐标不对，只该存 J6" —— 见该函数说明）。
            if len(trip) >= 2:
                _move_flip_j6(ctx, pmap, DUAL_FACE_FLIP_POINT, speed, dry,
                              "翻转·J6反面（仅J6）")

            # ③ 取第 2 件（上板）：★ 保持吸气，不碰气路（一断真空第 1 件就掉）
            if len(trip) >= 2:
                second = trip[1]
                _move(ctx, pmap, second + "上", speed, dry, "取料·上方")
                _move(ctx, pmap, second, speed, dry, "取料·下落")
                _air(ctx, "suck", dry, "取料·吸")
                suck_on = True
                if int(body.suck_hold_ms) > 0:
                    ctx.sleep(int(body.suck_hold_ms) / 1000.0)
                S.face_slot_put(state, "upper", piece=second, ok=True)
                _move(ctx, pmap, second + "上", speed, dry, "抬起")

            # ④ 送检 → 正/反各 N 帧、分组投票（两个独立结论）
            _move(ctx, pmap, cam, speed, dry, "送检")
            _set(stage="停顿")
            if ctx.sleep(int(body.dwell_ms) / 1000.0):
                stop_reason = "已被停止"; phase = "cancelled"; break
            faces = list(S.FACE_SLOTS[: len(trip)])
            if not judge:
                # 空跑 / 双闸未开：不调相机、不判色，用轮换占位色把选槽+落料逻辑走通
                ph = _DRY_PLACEHOLDER_COLORS
                verdicts = [{"ok": True, "color": ph[(done + i) % len(ph)],
                             "conf": 0.0, "slot": faces[i], "votes": {},
                             "placeholder": True} for i in range(len(faces))]
                _set(stage="空跑·未判色", sim_color=True,
                     color=",".join(v["color"] for v in verdicts))
                log.info("空跑/未开双闸（双面）：跳过判色，占位色=%s",
                         [v["color"] for v in verdicts])
            else:
                dev = _camera_deviation(pmap, cam)
                if dev is not None and dev > CAM_ARRIVE_TOL_DEG:
                    raise _Stop("未到达 C 检测点（实际偏差 %.1f° > %.1f°），不判色"
                                % (dev, CAM_ARRIVE_TOL_DEG), "判色", done + 1)
                _set(stage="判色")
                verdicts = _capture_per_face(ctx, faces, DUAL_FRAMES_PER_FACE,
                                             min_conf, "判色")
                if verdicts:
                    _set(frame_detail=verdicts[0].get("frame_detail") or [],
                         best_frame=verdicts[0].get("best_frame"),
                         votes=verdicts[0].get("votes") or {})
            _set(color=",".join(str(v.get("color") or "") for v in verdicts))

            # ⑤ 落料：先放有效件（B 槽），再放无效件（原路回自己 A 原位）
            ordered = ([v for v in verdicts if v.get("ok")]
                       + [v for v in verdicts if not v.get("ok")])
            for k, v in enumerate(ordered):
                keep = (k < len(ordered) - 1)      # 后面还有件挂着 → 立即复吸
                slot = v.get("slot")
                idx = S.FACE_SLOTS.index(slot) if slot in S.FACE_SLOTS else 0
                src = trip[min(idx, len(trip) - 1)]
                if v.get("ok"):
                    plan = S.build_piece_plan(placement, v.get("color"))
                    if not plan["ok"]:
                        raise _Stop("%s" % plan["detail"], "选槽", done + 1)
                    tgt = plan["slot"]
                    _set(target=tgt)
                    _move(ctx, pmap, tgt + "上", speed, dry, "放料·正上方")
                    _dual_drop(ctx, dry, state, keep)
                    suck_on = keep
                    blow_on = False
                    S.place(placement, v.get("color"), tgt)
                    _set(placement=S.placement_summary(placement))
                    emit_event("sorting", "info", "sorting.piece",
                               "第 %d 件：%s → %s（双面 %s，conf %.2f）" % (
                                   done + 1, v.get("color"), tgt, slot,
                                   v.get("conf") or 0.0),
                               {"piece": done + 1, "color": v.get("color"), "slot": tgt,
                                "face": slot, "conf": v.get("conf") or 0.0,
                                "dual_face": True, "dry_run": dry}, actor="control")
                else:
                    # 无效件 → 原路放回它自己的 A 原位（复用 _put_back 的移动+落料工艺）
                    _set(stage="无法识别·放回原位", target="")
                    _put_back(ctx, pmap, src, speed, dry,
                              blow_ms=int(body.blow_ms),
                              prefix="放回原位 %s" % src, st=state,
                              go_home=False, keep=keep)
                    suck_on = keep
                    blow_on = False
                S.face_slot_take(state, slot)
                done += 1

            # ⑥ 两件都放完才回起点（件与件之间不回起点）
            _move(ctx, pmap, start, speed, dry, "回起点")

        if not stop_reason:
            _set(stage="收尾·回开始/结束点")
            _move(ctx, pmap, start, speed, dry, "收尾回原点")
    except _Stop as e:
        if not dry and (blow_on or suck_on):
            try:
                _air(ctx, "unblow", False, "异常·停止放气")
            except Exception as exc:  # noqa: BLE001
                log.warning("异常收气（停止放）失败：%s", exc)
            try:
                _air(ctx, "release", False, "异常·停止吸")
            except Exception as exc:  # noqa: BLE001
                log.warning("异常收气（停止吸）失败：%s", exc)
            blow_on = False
            suck_on = False
        stop_reason = e.reason
        stop_piece = e.piece or int((_job.get("state") or {}).get("piece") or 0)
        if "停止" in e.reason:
            phase = "cancelled"
        else:
            phase = "error"
            # 现场没有真空反馈 → 不知道件到底在不在手上：3D 不显示吸住（同默认路径口径）
            _set(holding=False, suspect_pick=True)
            try:
                _set(stage="异常·回开始/结束点", error=e.reason)
                _move(ctx, pmap, start, speed, dry, "异常回原点")
            except _Stop as e2:
                log.warning("异常后回原点也失败：%s", e2.reason)
                stop_reason = "%s；回原点失败：%s" % (stop_reason, e2.reason)
    except Exception as e:  # noqa: BLE001
        log.exception("双面分拣异常")
        stop_reason = "内部异常：%s" % str(e)[:150]
        phase = "error"
        if not dry and (blow_on or suck_on):
            for act, lab in (("unblow", "停止放"), ("release", "停止吸")):
                try:
                    _air(ctx, act, False, "内部异常·%s" % lab)
                except Exception:  # noqa: BLE001
                    pass
            blow_on = False
            suck_on = False
    finally:
        ctx.finish(phase)
        try:
            if not dry and (blow_on or suck_on):
                for act, lab in (("unblow", "停止放"), ("release", "停止吸")):
                    try:
                        _air(ctx, act, False, "收尾·%s" % lab)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("收尾关气（%s）失败：%s", lab, exc)
            _set(stage="结束", error=stop_reason, finished=True,
                 ok=(phase == "done"), phase=phase, piece=stop_piece or None)
            emit_event("sorting", "info" if phase == "done" else "warn", "sorting.stop",
                       ("双面分拣完成" if phase == "done" else "双面分拣结束：%s" % stop_reason),
                       {"phase": phase, "reason": stop_reason, "dry_run": dry,
                        "dual_face": True,
                        "placement": S.placement_summary(placement)},
                       actor="control")
        except Exception:  # noqa: BLE001
            log.exception("双面分拣收尾异常（运行登记已注销，不影响后续指令）")


# ---------------------------------------------------------------- 点位示教
# ★ 为什么这几个接口要**白名单**：本页只该管分拣的点位。若允许写任意名字，
#   一次手滑就能在点位库里造出没人认识的条目，而分拣引擎仍然"照名字找点"，
#   出问题时极难排查。把"能写什么"限死，配合 /points 的体检形成闭环。
# ★★ 2026-10-10：白名单 = **38 个必备点 ∪ 可选附加点**（J6 正面/反面，见 S.OPTIONAL_EXTRA_POINTS）
#   —— J6 双面分拣要能录入这两个姿态；但它们**不是开跑必备**（体检允许缺失），
#   所以**不并入** expected_points()，38 这个数字与清单一个字未动。
_WHITELIST = S.allowed_teach_points()


class TeachIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    joints: Optional[List[float]] = Field(default=None, min_length=6, max_length=6,
                                          description="留空则取**当前实时关节角**（示教用）")
    note: str = Field(default="", max_length=200)


class NameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


def _require_sorted_name(name: str) -> str:
    n = (name or "").strip()
    if n not in _WHITELIST:
        raise HTTPException(400, detail="不允许的点位名：%s（只能操作分拣的 38 个点位 + J6 正/反面）"
                            % (n or "空"))
    return n


def _live_joints() -> List[float]:
    """取**当前实际姿态**（实时采集）用于示教；采集不可用时回退到执行引擎最近目标。"""
    snap = collector.get_latest()
    if snap:
        try:
            return [round(float(snap["j%d" % i]), 3) for i in range(1, 7)]
        except Exception:
            pass
    st = motion.state()
    if st.get("last_target"):
        return [round(float(v), 3) for v in st["last_target"]]
    raise HTTPException(503, detail="拿不到当前姿态（机器人未连接/无实时数据），无法示教")


@router.post("/teach")
def api_teach(body: TeachIn, tok: str = Depends(require_control)):
    """把当前位姿（或显式给的关节角）保存为该点位；已存在则**覆盖**。

    示教 = 配置写，不产生运动。运行中禁止（避免跑到一半把目标位改了）。
    """
    name = _require_sorted_name(body.name)
    with _gate:
        if _job.get("thread") and _job["thread"].is_alive():
            raise HTTPException(409, detail="分拣正在运行，先「停止」再改点位")
    j = [round(float(v), 3) for v in (body.joints or _live_joints())]
    if len(j) != 6:
        raise HTTPException(400, detail="关节角必须是 6 个数值")
    payload = json.dumps(j, ensure_ascii=False)
    db = SessionLocal()
    try:
        row = next((p for p in list_points(db) if p.name == name), None)
        if row is None:
            row = create_point(db, name=name, group="分拣", kind="joint",
                               joints_json=payload, note=body.note or "分拣示教")
            action = "created"
        else:
            update_point(db, row.id, group="分拣", kind="joint",
                         joints_json=payload, note=body.note or row.note)
            action = "updated"
    finally:
        db.close()
    emit_event("sorting", "info", "sorting.teach",
               "示教点位 %s（%s）" % (name, "新建" if action == "created" else "覆盖"),
               {"name": name, "joints": j, "action": action}, actor="control")
    return {"ok": True, "name": name, "joints": j, "action": action}


@router.post("/goto")
def api_goto(body: NameIn, tok: str = Depends(require_control)):
    """移动到该示教点位（复核示教用）。★ 这是**运动**：走围栏互锁 + 轴锁 + 速度夹取。

    分拣运行期间禁止（与手动点动同一套互锁理由：抢同一个 40135 触发位）。
    """
    name = _require_sorted_name(body.name)
    with _gate:
        if _job.get("thread") and _job["thread"].is_alive():
            raise HTTPException(409, detail="分拣正在运行，先「停止」再手动到点")
    db = SessionLocal()
    try:
        row = next((p for p in list_points(db) if p.name == name), None)
    finally:
        db.close()
    if row is None:
        raise HTTPException(404, detail="点位「%s」尚未示教" % name)
    ok, reason, _snap = guard_check()
    if not ok:
        raise HTTPException(409, detail="安全围栏互锁：%s" % reason)
    try:
        j = parse_joints(row.joints, where="点位「%s」" % name)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    try:
        sp = int(get_config().get("control", "default_speed_pct", default=5) or 5)
    except Exception:
        sp = 5
    sp = max(5, min(100, sp))
    r = motion.command([float(v) for v in j], sp, 0)
    if not r.get("ok"):
        raise HTTPException(502, detail="移动到「%s」失败：%s" % (name, r.get("error")))
    emit_event("sorting", "info", "sorting.goto", "手动到点：%s" % name,
               {"name": name, "speed_pct": sp}, actor="control")
    return {"ok": True, "name": name, "speed_pct": sp}


@router.delete("/point")
def api_del_point(name: str, tok: str = Depends(require_control)):
    """删除某个分拣点位（38 个必备点 + J6 正/反面 附加点）。"""
    n = _require_sorted_name(name)
    with _gate:
        if _job.get("thread") and _job["thread"].is_alive():
            raise HTTPException(409, detail="分拣正在运行，先「停止」再改点位")
    db = SessionLocal()
    try:
        row = next((p for p in list_points(db) if p.name == n), None)
        if row is None:
            raise HTTPException(404, detail="点位「%s」不存在" % n)
        delete_point(db, row.id)
    finally:
        db.close()
    emit_event("sorting", "warn", "sorting.point_deleted", "删除分拣点位：%s" % n,
               {"name": n}, actor="control")
    return {"ok": True, "name": n}
