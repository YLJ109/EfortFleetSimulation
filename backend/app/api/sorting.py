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
from app.api.control import (new_run, register_scene_source, request_cancel,
                            reject_if_not_ready)
from app.core.config import get_config
from app.core.logger import get_logger
# ★ 2026-10-11：到点（测试点位）速度改用统一夹取（与右上角滑块同一口径）。
from app.core.safety_const import clamp_speed
from app.db.crud import (create_point, delete_point, list_points,
                        update_point)
from app.db.database import SessionLocal
from app.services import camera_client
from app.services import sorting as S
from app.services.events import emit as emit_event
from app.services.collector import collector
from app.services.kinematics import forward_kinematics as FK
from app.services.motion import motion, real_write_enabled
# ★★ 2026-10-11：气路动作 → 40135 位号的**唯一映射口**（用户 210 位表，见 modbus 顶部注释）。
#   本文件不写死位号；`_air` 只把 (动作, 面) 解析成 `VAC_ACTIONS` 的键名再交给 rc_vacuum。
from app.services.modbus import VAC_ACTIONS
from app.services.safety_guard import check as guard_check
from app.services import ui_speed                     # ★ 上位机当前速度（长流程跟随滑块）
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
#   ★★ 2026-10-11 修复：该到位校验必须**与实发同源** —— 期望目标取 `_move` 本次真正下发的
#      target（按到达面解算），**绝不再拿 C 点自带的正面姿态当期望**（那会把"反面到达"误报成
#      偏差 180°）。容差 `CAM_ARRIVE_TOL_DEG` 未动；真没到位仍如实报"未到达"。详见 `_camera_deviation`。
# ★★ 2026-10-11：占位色直接复用 `S.COLORS`（唯一来源）—— 原来在这里又抄了一份三元组。
_DRY_PLACEHOLDER_COLORS = S.COLORS
CAM_ARRIVE_TOL_DEG = 3.0        # 判色前的到位容差（度）


def _camera_deviation(expect: Optional[Sequence[float]]) -> Optional[float]:
    """当前实际姿态与**本次到达 C 点实际下发的目标**的**最大关节偏差（度）**；读不到返回 None。

    ★★ 2026-10-11 修复（现场误报警「未到达 C 检测点（实际偏差 180.0° > 3.0°），不判色」）：
      过去这里把 **C 检测点自带的正面 6 值**（旧实现用 `_joints_of` 取该点自带姿态）当成期望 ——
      而"送检到达面 = 首个采集面（= 反面）"这条**省翻面优化**（上一轮上线）之后，
      双面流程会**在反面**到达 C 位（J6 = 该点自身 J6 ∓180）→ 与"C 点自带的正面"**恰好差 180°**
      → 校验把**正常到达**误判成"没到" → 停 + "异常·回开始/结束点"。
      ⇒ 根因 = **期望目标与实发不同源**。改为**结构性同源**：期望 = `_move` **本次真正下发的
        target**（由 `_move` 返回 —— 它已按与实发**同一个 face** 经 `S.resolve_target_joints`
        解算过）。正面到达时它就是 C 点自带值（**与旧行为逐字一致、零回归**），
        反面到达时它就是 ∓180 那一侧（偏差≈0，不再误报）。

    ★ **校验绝不放宽**（本次最关键的安全属性）：
      · 到达面=反面 → 期望 J6 = ∓180 → 偏差≈0（**不再误报 180°**）；
      · 机器人**真没到位**（例如 J2 差 10°、整姿态偏）→ 偏差照报 > 容差 → **仍然**"未到达"；
      · 只换"期望值"这一个来源，**容差 `CAM_ARRIVE_TOL_DEG` 一个字节未动**
        （**不**忽略 J6、**不**放大容差、**不**跳过校验）。

    ★ 读不到实时姿态（`collector` 无快照）或 expect 不可用 → 返回 None → 调用方**放行**
      （与旧行为一致：没有实时数据时不下"没到"的结论，避免误停）。
    """
    snap = collector.get_latest()
    if not snap or expect is None:
        return None
    try:
        live = [float(snap["j%d" % i]) for i in range(1, 7)]
        tgt = [float(v) for v in expect]
    except Exception:  # noqa: BLE001 —— 快照/目标形状异常 → 当作拿不到，放行
        return None
    if len(tgt) != 6:
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
    dwell_ms: int = Field(default=S.DWELL_MS, ge=0, le=10000,
                          description="C 区停顿时长（ms，默认 500；其间抓 N 帧判色）")
    # ★ 2026-10-09 新增（用户流程口径）："吸取 → 停一下 → 抬起"。
    #   吸上后**先停一下再抬**，让真空建立/料被完全吸住 —— 立刻抬起容易在半空掉件，
    #   而现场没有真空反馈，掉了也只能等判色失败才发现。
    # ★ 2026-10-10 用户口径：停顿时长 **1s → 0.5 秒**。
    suck_hold_ms: int = Field(default=S.SUCK_HOLD_MS, ge=0, le=5000,
                              description="吸取后停留（ms，默认 500）：等吸牢再抬起")
    frames: int = Field(default=3, ge=1, le=9, description="停顿后抓几帧判色")
    min_conf: Optional[float] = Field(default=None, ge=0.0, le=1.0,
                                      description="留空则用视觉服务当前阈值")
    # ★ 2026-10-08 第二轮语义变更（用户："到了B区停止吸气然后再放气0.5秒，停止放气"）：
    #   放气**不再保持到回起点**，而是"在 B 正上方吹够这么久就地停"。
    #   所以这个值就是**放气持续时间**，默认 500ms。
    blow_ms: int = Field(default=S.BLOW_MS, ge=0, le=3000,
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
        # ★ 统一"未就绪"闸门：真实分拣（非空跑）前先校验伺服是否上电。
        #   空跑 dry_run=true 放行（不下发真实运动）。
        reject_if_not_ready(dry=bool(body.dry_run))

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


def _live_joints_or_none() -> Optional[List[float]]:
    """安全地取**当前实时关节**（6 值）；拿不到 → None（**不抛**）。

    ★ 与 `_live_joints()`（示教用，拿不到就 503）刻意区分：这里只是"选面时参考当前 J6"
      的可选输入，**读不到就走确定性规则、不影响下发**（见 `S.resolve_target_joints`）。
    """
    snap = collector.get_latest()
    if snap:
        try:
            return [float(snap["j%d" % i]) for i in range(1, 7)]
        except Exception:  # noqa: BLE001 —— 快照字段异常 → 当作拿不到
            pass
    try:
        st = motion.state()
        if st.get("last_target"):
            return [float(v) for v in st["last_target"]]
    except Exception:  # noqa: BLE001
        pass
    return None


def _move(ctx, pmap, name, speed, dry, stage, st=None, face=S.FACE_AUTO,
          *, rotate=None, _no_insert=False, _freeze=False) -> List[float]:
    """走一个示教点位（写目标 + 触发位 → 常驻 210 执行 MJOINT）。

    ★★★ 2026-10-11 **旋转管控（本轮 · 修致命 bug）**：用户原话
      「**其他时间一律不允许旋转**……**如果测试点到原位回到正上位突然旋转卡住了，不能旋转，
       回家，去摄像头点位也 J6 旋转一下这个绝对不允许**」。
      · **默认 `face=S.FACE_AUTO`（= keep）→ J6 不翻面**；
      · ★★ 2026-10-12 **终版口径**（用户原话「**所有点的 J6 跟之前的恢复过来，正面就是之前录的
        J6，反面就是当前点 J6 的值减掉 180 度**」）：keep/auto 的 J6 = 按当前在哪一面取
        **该点自身的对应变体** —— 正面变体 = 该点自身录制 J6、反面变体 = 自身 − 180；
        **不再有任何示教面基准**（`bases` 参数已删，判面用固定约定 `face_of_j6`）。
      · 真翻转只有**拿到旋转授权**时才可能：`rotate=True` 或处于 `with S.allow_rotate():` 内
        （唯一闸门 = `S.effective_face`）；**未授权时连 `face="front"/"back"` 也被降级成 keep**。
      · **fail-safe 方向：漏改/忘传 = 不旋转**。
    ★★ 2026-10-11 **face-aware（上一轮修根因）**：不再无条件把该点自带的 6 轴整姿态下发 ——
      改由**唯一解算入口** `S.resolve_target_joints(name, joints, base, face)` 决定目标。
    ★ `J6正面`/`J6反面`（虚拟切面命令）拿不到实时基姿态 → 解算返回拒绝原因，这里**回退为
      该点自带整姿态**（与 `_move_flip_j6` 的既有回退语义一致；普通点位不会命中这条）。
    ★ `state.face` 如实记录**实际保持的面**（`S.resolve_face`），绝不把 keep 谎报成正面。
    ★★ 2026-10-11 **返回本次真正下发的 target**（`-> List[float]`）：供调用方做**同源校验**
      （如"送检是否到达 C 点"）—— 校验要用**实发值**当期望，不能再自己拿点位自带姿态推一遍
      （那正是"反面到达被误报 180°"的根因，见 `_camera_deviation`）。空跑（dry）也返回该值。
    ★★ 2026-10-11（用户口径「**应该电脑的速度去执行**」）：**每一步都取"上位机当前速度"** ——
      长流程（分拣 / 井字棋 / 文件执行 / AI）跑到一半拖滑块，**下一步就生效**，
      与"点位执行 / 点动"的手感一致；**没推送过时原样用传入的 `speed`（零回归）**。
      ★ 换速只发生在**两次 `_move` 之间**（步/趟边界），**绝不打断飞行中的那句运动**。
    """
    # ★ 唯一咽喉：`_move` 是所有长流程下发点位的地方 → 速度跟随只做在这一处。
    #   ★★ 2026-10-11 第二版（修"快速分拣偶尔变成 20%"）：**只认界面滑块**，
    #   且按**时间戳**判断"流程跑起来之后人是否动过滑块" —— 别的流程 / 后台自报
    #   不再能顶掉操作员设的值。详见 `services/ui_speed.py` 顶部说明。
    speed = ui_speed.apply(speed, st)
    if ctx.wait_pause():
        raise _Stop("已被停止", stage)
    j = _joints_of(pmap, name)
    live = _live_joints_or_none()                      # 只采一次，解算与"面记录"共用
    # ★★ 2026-10-12「原位安全路径」**自动补垂直段**（用户口径：禁止原位直接横移/斜插，
    #   「必须 原位 → 正上位 →（目标的正上位）→ 目标」）——`_move` 是长流程的**唯一咽喉**，
    #   在这里补段 ⇒ 任何漏改的流程路径也自动走安全路径。
    #   ★ 判据取自本次流程的 `pmap`（内存点位表），**不额外读库**；`pmap` 里没有原料位 → 判不出 → 不补段。
    #   ★ 空跑（dry）不下发 → 不补段（且空跑时实时姿态不更新，拿它判定会误伤预演流程）。
    #   ★ `_no_insert=True` 防重入（补段本身就是一次 `_move`）。
    if live and not dry and not _no_insert:
        _poses = {}
        for _n in (pmap or {}):
            try:
                _poses[_n] = _joints_of(pmap, _n)
            except Exception:                          # noqa: BLE001 —— 坏点跳过
                pass
        _apath, _areason = S.raw_slot_safe_path(live, _poses, name)
        if _areason:
            raise _Stop(_areason, stage)
        _aprev = S.point_at_pose(live, _poses)
        # ★ 只补"本次点位表里真的有"的段（`pmap` 缺该点时无法补 —— 不硬造，照常走原目标）
        _ains = [n for n in (_apath[:-1] if _apath else []) if n in (pmap or {})]
        if _ains:
            for _seg in _ains:
                _move(ctx, pmap, _seg, speed, dry, "%s·安全路径(%s)" % (stage, _seg), st,
                      face=face, rotate=rotate, _no_insert=True,
                      _freeze=S.is_vertical_move(_aprev, _seg))
                _aprev = _seg
            live = _live_joints_or_none()               # 补段后重取姿态（供解算/面记录）
        # ★ 主段若也是垂直段（原位 ↔ 其正上位）→ J6 冻结（「原位到正上位更不能旋转」）
        if S.is_vertical_move(_aprev, name):
            _freeze = True
    eff = S.effective_face(name, face, rotate=rotate)  # ★★ 旋转闸门：未授权 → 一律 keep
    target, _j6_only, reason = S.resolve_target_joints(name, j, (lambda: live), face=eff)
    if reason:
        if _j6_only or dry:
            # · `J6正/反面` 拿不到基姿态 → 旧回退（该点自带整姿态）；
            # · **空跑/离线本来就不下发任何指令**（用户口径）→ 用点位自带值把流程/仿真走通。
            target = j
        else:
            # ★★ 真机 + 拿不到当前 J6（keep 无法"保证不转"）→ **拒绝下发**，
            #   绝不"按该点自身值转一下"（那正是用户报的乱转）。
            raise _Stop(reason, stage)
    if _freeze and live:
        # ★★ 垂直升降段 J6 冻结（保持当前值）——「原位到正上位更不能旋转」
        target = _freeze_j6_at(target, live)
    # ★ 如实记录"本次实际生效的面"：keep/auto 时按当前实时 J6 判出实际保持的是哪一面
    face_rec = S.resolve_face(eff, j[5], (live[5] if live else None))
    if dry:
        _set(st, stage=stage, last_point=name, face=face_rec)
        return [float(v) for v in target]
    ok, reason, _snap = guard_check()
    if not ok:
        raise _Stop("安全围栏互锁：%s" % reason, stage)
    r = motion.command([float(v) for v in target], int(speed), 0,
                       abort_check=lambda: ctx.cancelled())
    _set(st, stage=stage, last_point=name, face=face_rec)
    if not r.get("ok"):
        raise _Stop("运动到「%s」失败：%s" % (name, r.get("error")), stage)
    return [float(v) for v in target]


# ★★ 2026-10-11：动作 × 面 → 210 触发位动作名（**唯一映射口**）。
#   用户权威位表：下板 = 1~4 位（suck/release/blow/unblow）；
#                 反面/上板 = 5~8 位（suck_back/release_back/blow_back/unblow_back）。
#   位号本身**只在** `modbus.VAC_ACTIONS` 里维护一处；这里只做"面 → 动作名"的解析。
#   ★ 两面是**独立气路**（此前"两面共用一路真空、放一件会连累另一件"的前提已被现场纠正）。
_AIR_FACE_ACTION = {
    (S.FACE_FRONT, "suck"): "suck",
    (S.FACE_FRONT, "release"): "release",
    (S.FACE_FRONT, "blow"): "blow",
    (S.FACE_FRONT, "unblow"): "unblow",
    (S.FACE_BACK, "suck"): "suck_back",
    (S.FACE_BACK, "release"): "release_back",
    (S.FACE_BACK, "blow"): "blow_back",
    (S.FACE_BACK, "unblow"): "unblow_back",
}


def _air_action(action: str, face: str = S.FACE_FRONT) -> str:
    """(动作, 面) → `VAC_ACTIONS` 键名。

    · 以 `_back` 结尾（已是"反面"完整动作名）→ 原样返回；
    · 否则按 `face` 选面：front → 下板那路（suck/release/blow/unblow）；
      back → 反面那路（suck_back/…）；
    · 未知动作原样透传（交由 rc_vacuum 报"action 必须是…"）。
    """
    a = str(action or "").strip().lower()
    if a.endswith("_back"):          # 已是"反面"完整动作名 → 原样返回（不再二次加后缀）
        return a
    return _AIR_FACE_ACTION.get((S.norm_face(face), a), a)


def _air(ctx, action: str, dry: bool, stage: str, st=None,
         face: str = S.FACE_FRONT) -> None:
    """吸/停止吸/放/停止放（40135.Bit1~Bit8 → 常驻 210 执行）—— **face-aware**。

    ★★ 2026-10-11（用户口径 · 权威位表）：气路动作集扩到 **8 个**（索引严格按用户表）：
        suck=1 / release=2 / blow=3 / unblow=4（**下板/正面**）；
        suck_back=5 / release_back=6 / blow_back=7 / unblow_back=8（**反面/上板**）。
      `face="front"|"back"` 决定走哪一路；位号见 `modbus.VAC_ACTIONS`（单一映射口）。
      **移动位仍是 0**（走点动通道），与本函数无关、不许改。
    ★ 两面板**各自独立气路** —— 此前的"只发一路、两面共用"实现是错的（反面那一路从未被驱动）。
    ★ 与运动共用 40135 寄存器，必须取执行互斥（否则与在飞的点动互相清触发位）。
    """
    act = _air_action(action, face)
    label = (VAC_ACTIONS.get(act) or (0, act))[1]
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
        ok, verr, _d = motion.modbus.rc_vacuum(act, timeout=10.0)
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
# ★★ 2026-10-12 **终版**（用户原话「反面就是当前点 J6 的值减掉 180 度……不用录制正反面」）：
#   `J6反面` 不再是示教点位，而是**虚拟翻转命令** —— 在正上位原地把 J6 转到
#   「当前 J6 − 180」（折算等价角，见 `_move_flip_j6` / `S.resolve_target_joints`）。
# ★ 名字必须与界面按钮用的名字**完全一致**：`frontend/src/components/SortingView.vue`
#   的 extraSlots 里写的就是 "J6反面"；它同时也在 `S.OPTIONAL_EXTRA_POINTS` 里
#   （→ 在点位名白名单内，界面能"到点/删除"）。三处任一改名都必须同步（回归钉）。
DUAL_FACE_FLIP_POINT = "J6反面"
# ★ 每面帧数：正面 2 + 反面 2 = **4 帧 / 2 个独立结论**（方案 §2 步 4）。
#   与单面口径一样，帧跨度 = (N-1)×FRAME_GAP_S 必须 ≤ 0.5s（C 位停留窗口，回归钉）。
DUAL_FRAMES_PER_FACE = 2


def _rotation_guard(name: str) -> None:
    """★ 方案 §6-1 回归钉（**2026-10-11 更新口径**）：**带 J6 翻转的下发只禁止落在原料位**。

    口径（用户原话）：「J6 反正不能在**原位**进行旋转！其他的时候可以旋转」；
                    「18 个正上位，摄像头点位，起点可以旋转……18 个原位不能旋转」。
      —— 「原位」= **原料位（A 区 9 格 + B 区 9 格，共 18 个）**。
    判据是**黑名单**（见 `S.is_raw_material_slot`）：**只有原料位禁止，其余一切允许**
    （正上位 / J6正面 / J6反面 / C检测 / 摄像头位 / 开始/结束点 / 起点 / home … 都放行）。

    在**原料位（A/B 区格位本身，共 18 个）**请求旋转 → `_Stop` 拒绝并写明原因（这条改不掉）。
    """
    if not S.rotation_point_allowed(name):
        raise _Stop("拒绝 J6 翻转：点位「%s」为原料位（A/B 区格位）—— **原料位严禁旋转**（其余位置均可）" % name)


# ---------------------------------------------------------------- ★★ 原位禁转 → 自动补段（2026-10-12）
# 用户口径（逐字）：「**原位也不能进行 J6 旋转**」「原位到正上位更不能旋转……
#                 **必须正上位旋转完成后才可以来到原位**」
#                 「**如果原位旋转，先回到正上位，旋转，然后来到原位**」。
# ⇒ 判据 = **机器人当前是否停在原料位**（实时关节 vs 18 个原料位示教姿态，见
#   `S.current_raw_slot`）；是 → **不拒绝、而是自动补段**：`X → X上`（抬起）→ 切面 → `X`（回原位）。
#   实现见 `_move_flip_j6` 与 `/sorting/goto`（切面分支）。
def _move_flip(ctx, pmap, name, speed, dry, stage, st=None) -> None:
    """**带 J6 翻转的下发**：先过旋转**黑名单**（`_rotation_guard`）再走普通移动。

    与 `_move` 的唯一区别就是那道护栏 —— 原料位翻转会被**拒绝在下发之前**，
    不会出现"已经写下去才后悔"。
    """
    _rotation_guard(name)
    _move(ctx, pmap, name, speed, dry, stage, st)


def _move_flip_j6(ctx, pmap, name, speed, dry, stage, st=None) -> None:
    """★ J6 双面翻转（**虚拟翻转命令**，2026-10-12 终版口径）：原地只覆盖第 6 轴。

    ★★ 终版（用户原话「反面就是**当前点 J6 的值减掉 180 度**」「**不用录制正反面**」）：
      **不再读点位库** —— 目标面由**点名**决定（"J6反面"→反面），翻转目标 =
      **当前实时 J6 − 180**（折算等价角），J1~J5 保持**当前实时姿态**原样不动
      （唯一实现 = `S.resolve_target_joints` 的虚拟切面分支，与 `/sorting/goto` 同源）。
      · **已在目标面 → 原地不动**（幂等、如实零动作；绝不来回翻）；
      · 要翻 → `normalize_j6(当前 J6 − 180)`（折进软限位 ∩ 可表示范围）。
    ★ 仍传 **6 个值** 给 `motion.command`（`motion.py` 强制 len==6，本函数不改那条铁律）。
    ★ 旋转**黑名单**（`_rotation_guard`）**先过**：顺序、语义与 `_move_flip` 一致（**原料位禁止**）。
    ★ 基姿态（当前实时关节）取不到时：**空跑**只记 stage（不下发本来就不动，流程照走）；
      **真机** → `_Stop` **拒绝** —— 绝不"按别的姿态翻一下"（那正是现场踩过的坑）。
    """
    _rotation_guard(name)
    if ctx.wait_pause():
        raise _Stop("已被停止", stage)
    try:
        base = _live_joints()
    except Exception as e:  # noqa: BLE001 —— 含 HTTPException(503)：读不到实时姿态
        if dry:
            # ★ 空跑/离线本来就不下发任何指令（用户口径）→ 只记 stage，把流程走通。
            _set(st, stage=stage, last_point=name)
            return
        raise _Stop("拒绝翻转「%s」：拿不到当前实时姿态（%s）—— 原地切面必须以当前姿态为基"
                    % (name, e), stage)
    # ★★ 2026-10-12（用户口径「**如果原位旋转，先回到正上位，旋转，然后来到原位**」）：
    #   若机器人**正停在原料位 X** → **自动补段**：`X → X上`（抬起）→ 翻转 → `X`（回原位）。
    #   （双面流程本就在**正上位**调用本函数 → `_oslot` 判不出 → 零回归。）
    _oslot = None
    try:
        _oposes = S.all_point_poses()
    except Exception:                                  # noqa: BLE001 —— 判不出 → 不补段（下面照常翻转）
        _oposes = {}
    if _oposes:
        _oslot = S.current_raw_slot([float(v) for v in base], _oposes)
    if _oslot:
        if (_oslot + "上") not in (pmap or {}):
            raise _Stop("无法在原位切面：正上位「%s上」不在本次点位表里（请先补齐示教）" % _oslot, stage)
        _move(ctx, pmap, _oslot + "上", speed, dry, "%s·先到正上位" % stage, st)
        try:
            base = _live_joints()                      # 抬起后重取基姿态
        except Exception:                              # noqa: BLE001 —— 取不到 → 用旧基（下方 resolve 会拒绝）
            pass
    target, _j6_only, reason = S.resolve_target_joints(
        name, [float(v) for v in base], (lambda: base), face=S.FACE_FRONT)
    if reason or target is None:
        raise _Stop(reason or ("无法解算「%s」的切面目标" % name), stage)
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
    if _oslot and _oslot in (pmap or {}):
        # 回原位：J6 冻结为**刚切好的面**（keep），下降途中不再转。
        _move(ctx, pmap, _oslot, speed, dry, "%s·回原位" % stage, st, face=S.FACE_KEEP)


# ---------------------------------------------------------------- 持件槽位 → 用面（B 块核心）
# ★★ 2026-10-11（方案 §B）：**持件槽位 → 用面**的唯一映射口。
#   用户口径（本轮已确认）：一趟两件**固定"第 1 件 = 正面、第 2 件 = 反面"**，由**持件槽位**判定。
#     · lower（下板，先吸的第 1 件）→ **正面 front**（= 该点自身 J6）；
#     · upper（上板，翻面后吸的第 2 件）→ **反面 back**（= 该点自身 J6 ∓180）。
#   ⇒ 所有"带着这个件去移动 / 落料"的下发都用它取面 —— 这样第 2 件后续每步都**保持反面**，
#     **不再被点位默认正面顶掉**（这正是 B 块要修的核心：过去 `_move` 恒按该点自带 J6 下发）。
_SLOT_FACE = {S.FACE_SLOTS[0]: S.FACE_FRONT, S.FACE_SLOTS[1]: S.FACE_BACK}


def _face_of_slot(slot) -> str:
    """持件槽位（"lower"/"upper"，或下标 0/1）→ 该件对应的**面**；未知 → 正面（保守，同旧行为）。"""
    if isinstance(slot, int) and not isinstance(slot, bool):
        slot = S.FACE_SLOTS[slot] if 0 <= slot < len(S.FACE_SLOTS) else None
    return _SLOT_FACE.get(str(slot or ""), S.FACE_FRONT)


def _dual_drop(ctx, dry: bool, face: str = S.FACE_FRONT, st=None) -> None:
    """★ 一趟两件的**原地放件三步**（顺序即工艺）：`停止吸 → 放 → 停止放`。

    ★★ 2026-10-11 **前提更正（用户权威位表）**：两面板**各自独立气路** ——
      下板 = 40135.Bit1~4（索引 1~4），反面/上板 = Bit5~8（索引 5~8）。
      **原设计基于"全项目只有一路真空（io.DOut[8]）、两面共用"这一前提，该前提已被现场纠正**：
      放掉一件**不会**影响另一件 → 故原为"共用一路"而加的
      「**瞬时复吸**四步（停止吸→放→停止放→立刻吸）」**已取消**（第 1 件瞬时失真空的风险随之消失，
      这是安全上的净改善）。
    ★ `face` 决定这一趟只动哪一路（`_air` 内按面取位，位号见 `modbus.VAC_ACTIONS`）：
        front → 1~4 位；back → 5~8 位。**只碰该面那一路、不越界**。
    """
    _air(ctx, "release", dry, "落料·停止吸", st, face=face)
    _air(ctx, "blow", dry, "落料·放气", st, face=face)
    _air(ctx, "unblow", dry, "落料·停止放气", st, face=face)


def _capture_per_face(ctx, faces: Sequence[str], frames_per_face: int,
                      min_conf: float, stage: str, set_face=None) -> List[Dict]:
    """**按面**抓帧投票：每个面各抓 `frames_per_face` 帧 → `vote_color_per_face` 分组结论。

    ★ 判据**完全复用现有 `vote_color`**（经 `vote_color_per_face` 只做分组）——
      绝不新造第二套判色逻辑（那正是本项目最忌讳的分叉）。
    ★ 帧间隔沿用 `FRAME_GAP_S`：单面跨度 = (N-1)×0.15 ≤ 0.5s（与单面 `_capture` 同一约束）。
    ★★ 2026-10-11（B 块 · 送检真翻面）：新增 `set_face(slot)` 回调 —— **每面拍照前**先把 J6
      摆到该面对应的姿态（`_face_of_slot`：lower→正面 / upper→反面）。
      为什么必须：机器人到 C 后若**不再翻 J6**，两组帧会取在**同一姿态** → 两块板拍成同一件，
      与会话注释"正面 2 帧 → 翻面 → 反面 2 帧"不符（见方案 §B-5）。由调用方注入"怎么摆面"
      （复用 `_move(..., face=...)`），本函数只负责"按面分组 + 组间摆面"，不自己造下发通道。
      ★ `set_face=None` → 与旧行为逐字一致（不夹带任何运动）。
    """
    groups: List[List[Dict]] = []
    for _slot in list(faces):
        if callable(set_face):
            set_face(_slot)
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
              blow_ms: int = S.BLOW_MS, prefix: str = "放回原位", st=None,
              go_home: bool = True,
              face: str = S.FACE_FRONT) -> None:
    """★ 2026-10-10 用户口径：**判不出色（或自证不符）→ 把件原路放回取料格**。

    调用前提：件正被吸着、机器人停在半路（分拣停在 C 检测位）。
    工艺与"取料"完全对称、只是反过来：
        当前位置 → A{n}上方 → 下落 → 停止吸 → 放气 → 等 blow_ms → 停止放气 → 抬起 → 回起点

    ★ 为什么必须"回到原格上空再放"而不是就地松手：就地松件会掉在 C 区/半路，
      位置随机 —— 现场得满地找件，而且下一次取料还会撞上它。
    ★ 两路气在**这里正常关断**（不靠异常收尾兜底），所以调用方随后要把
      suck_on/blow_on 置 False，免得收尾再关一次。
    ★ `go_home=False` → **不回起点**（双面"两件都放完才回起点"，件与件之间不能回起点）。
    ★★ 2026-10-11（前提更正 · 用户权威位表）：两面板**各自独立气路** →
      **取消**原"共用一路真空"专用的 `keep`（复吸）参数：放这一面**只动这一面那一路**，
      不影响另一面。`face` 决定用哪一路（front → 1~4 位 / back → 5~8 位），
      "用哪面取的就用哪面放回"。默认 `front` → 单件路径 / 井字棋**逐字不变**（零回归）。
    """
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·到原格上方" % prefix, st, face=face)
    # ★★ 2026-10-11 用户口径：「原位禁止旋转」+「如果正面保持正面，如果反面保持反面，
    #   如果都不是调整接近哪个点就那个」——**去原料位（回原位那一步）保持当前面、不旋转**：
    #   用 `face="keep"` 而不是该件的面（用件的面会把 J6 按"该点自身∓180"重算 = 在原料位翻面；
    #   用 front 又会把反面件**翻回正面** → 正是用户报的"让它去反面位却总弄成正面"）。
    #   `keep` = J6 沿用当前面（正面/反面/判不出取更近），**全程不主动翻面**，与"原料位严禁旋转"相容。
    _move(ctx, pmap, src_slot, speed, dry, "%s·下落" % prefix, st, face=S.FACE_KEEP)
    _air(ctx, "release", dry, "%s·停止吸" % prefix, st, face=face)
    _air(ctx, "blow", dry, "%s·放气" % prefix, st, face=face)
    if int(blow_ms) > 0:
        ctx.sleep(int(blow_ms) / 1000.0)
    _air(ctx, "unblow", dry, "%s·停止放气" % prefix, st, face=face)
    # 这一面的件已放下 → 该面空槽。`st` 是双面 state 时，由调用方随后的 face_slot_take 刷新 holding；
    # 单件路径 st 为 None → 不设 holding（异常分支会置 False）。
    _set(st, stage="%s·完成" % prefix)
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·抬起" % prefix, st, face=face)
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
    # ★★★ 2026-10-11 旋转白名单②：**双面分拣允许按面翻 J6**（用户口径「双面分拣……可以旋转」）。
    #   `with S.allow_rotate()` 是本流程唯一的旋转授权声明 —— 授权只覆盖本流程的线程，
    #   出了这个 with 立刻恢复"默认不旋转"（fail-safe）。AI「快速分拣」也走这条分支，
    #   所以它同样被覆盖（无需在 ai.py 里再声明一次）。
    if bool(getattr(body, "dual_face", False)):
        with S.allow_rotate():
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
            # ★★ 2026-10-11 统一口径：**去原料位（A{a_slot}）那一步一律 `keep`**（保持当前面、不旋转）——
            #   用户原话「我在正上位反面，去原料区是变成了正面」。
            #   单件路径此刻当前=正面（全程无翻面）→ 与旧的"正面"**行为等价**（零回归，见回归钉）；
            #   但用 keep 后即使上游给了反面也能"保持着去原料位"、不被该点默认正面顶掉。
            #   ★ 目标点必为原料位（A_SLOTS）→ `face_for_target` 恒返回 keep。
            _move(ctx, pmap, a_slot, speed, dry, "取料·下落",
                  face=S.face_for_target(a_slot, S.FACE_FRONT))
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
            # ★ 送检返回**本次真正下发的目标**：下面"是否到达 C 点"的校验要用它当期望
            #   （与实发同源，见 `_camera_deviation` 的 ★★ 说明）。单件路径到达面=正面 →
            #   与"C 点自带姿态"一致 → 与旧行为逐字一致（零回归）。
            cam_target = _move(ctx, pmap, cam, speed, dry, "送检")
            # 3) 停顿 + 判色
            _set(stage="停顿")
            if ctx.sleep(int(body.dwell_ms) / 1000.0):
                stop_reason = "已被停止"
                phase = "cancelled"
                break
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
                #   ★★ 期望值取**实发那次的目标**（`cam_target`）→ 与实发**结构性同源**：
                #     不再拿 C 点自带正面姿态硬比（那会把"反面到达"误报成偏差 180°）。
                dev = _camera_deviation(cam_target)
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
      ③ → 取 A{j}（**上板**吸住）：★ **发「吸反面的气」(Bit5) 吸住上板那一路** ——
         项目此前按"两面共用一路真空"实现，故把「吸气」(Bit1，下板那路) 当成通用吸气，
         **反面的那一路从未被驱动** → 现场"反面吸不到"。现已按用户权威位表改用 Bit5。
         （上一轮误判为"没有 off→on 边沿"、据此加的"停止吸→立刻吸"**已删除**：两面独立。）
      ④ 到 C 检测位：**先正面 N 帧 → 投票**，切到反面 → **反面 N 帧 → 投票**（两个独立结论）；
         ★ 到达 C 时已停在**首个采集面（= 正面）**（省掉一次多余翻面）；拍完停在**反面**；
      ⑤ **先送反面那件、再送正面那件**（固定次序，唯一口径 `S.order_for_drop`）——
         ★ 2026-10-11 更新：先送反面是因为"拍完就停在反面"，该件姿态天然匹配、**不用翻面**；
         有效件按色落 B 槽，无效件 → **原路放回它自己的 A 原位**（不消耗配额）——
         有效/无效只决定"放哪"，**不改变先后**；
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
    # ★★ 2026-10-11：两面板**各自独立气路** → **分面记账**（下板前阀 = suck_on/blow_on；
    #   反面后阀 = suck_back_on/blow_back_on），异常收尾时"哪面有件就关哪面"。
    blow_on = False
    suck_on = False
    blow_back_on = False
    suck_back_on = False

    def _close_air(why: str) -> None:
        """异常/取消/收尾按**分面记账**收气：哪面那一路开着就关哪面（best-effort）。

        ★★ 2026-10-11：两面**独立气路** —— 下板用 1~4 位（release/unblow），
          反面/上板用 5~8 位（release_back/unblow_back）。**不许有件悬空失真空**：
          手臂上还挂着哪一面的件，就把那一面的吸气/吹气阀关掉。
        不吹气（在半空吹会把件吹飞、落到哪儿不知道），只"停止吸 + 停止放"。
        """
        nonlocal blow_on, suck_on, blow_back_on, suck_back_on
        if dry:
            return
        faces = ((S.FACE_FRONT, blow_on or suck_on, ""),
                 (S.FACE_BACK, blow_back_on or suck_back_on, "反面"))
        for face, opened, tag in faces:
            if not opened:
                continue
            for act, lab in (("unblow", "停止放"), ("release", "停止吸")):
                try:
                    _air(ctx, act, False, "%s·%s" % (why, lab), face=face)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s关气（%s%s）失败：%s", why, tag, lab, exc)
        blow_on = False
        suck_on = False
        blow_back_on = False
        suck_back_on = False

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
                stop_reason = "已被停止"
                phase = "cancelled"
                break
            first = trip[0]
            _set(piece=done + 1, stage="取料", color="", conf=0.0, target="",
                 hold_lower=None, hold_upper=None, holding=False, suspect_pick=False,
                 frame_detail=[], best_frame=None, votes={})

            # ① 取第 1 件（下板）：上方 → 下落 → 吸 → 等吸牢 → 抬起
            _move(ctx, pmap, first + "上", speed, dry, "取料·上方")
            # ★★ 2026-10-11 用户口径：「原位禁止旋转」—— **去原料位那一步用 `keep`
            #   （保持当前面、不旋转）**：J1~J5 用该点自身，J6 沿用当前面（此处当前=正面，
            #   故等价于正面），**绝不主动翻面**。详见 services/sorting.py::keep_j6。
            _move(ctx, pmap, first, speed, dry, "取料·下落", face=S.FACE_KEEP)
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

            # ③ 取第 2 件（上板/反面）：上方 → 下落 → **吸反面的气(Bit5)** → 等吸牢 → 抬起
            #   ★★ 2026-10-11 用户口径（原话 · 权威位表）：「5 是吸反面的气，6 停止吸反面，
            #      7 是放气反面的，8 是停止放气了」—— 即第 2 件必须发**反面那一路的吸气**。
            #   根因（本次已证实）：项目此前按"两面共用一路真空（io.DOut[8]）"实现 —— 我们发
            #      「吸气」驱动的是**下板那一路**，**反面的那一路从未被驱动**，这才是"反面吸不到"的
            #      真正原因。（上一轮误判为"没有 off→on 边沿"，据此加的"停止吸→立刻吸"是**不准确**的，
            #      现已删除：两面独立、放一件不影响另一件，不需要造边沿、也不会让第 1 件失真空。）
            #   ★ 第 2 件=上板=**反面**：**正上位**那几步走 `face=FACE_BACK`（保持反面），
            #     **去原料位那一步走 `face=keep`**（保持当前面、在原料位不旋转）；
            #     两者都不是"该点默认正面" —— 否则 `_move` 会把 J6 拉回正面、把 ② 的翻转顶掉
            #     （用户报的"从反面切回正面 / 去原料位总弄成正面"就是这条根因）。
            if len(trip) >= 2:
                second = trip[1]
                _move(ctx, pmap, second + "上", speed, dry, "取料·上方", face=S.FACE_BACK)
                # ★★ 去**原料位**（A{second}）那一步：`keep`（保持当前面、不旋转）——
                #   此刻已在②翻到反面 → J6 沿用反面，**不会把面翻回该点默认正面**
                #   （用户报的"让它去原料位却总给我弄成正面"就是这里）。J1~J5 仍用该点自身。
                _move(ctx, pmap, second, speed, dry, "取料·下落", face=S.FACE_KEEP)
                # ★ 只发**反面那一路**吸气（Bit5）：两面独立 → 不碰下板那一路、第 1 件不受影响。
                _air(ctx, "suck", dry, "取第2件·吸反面", face=S.FACE_BACK)
                suck_back_on = True
                if int(body.suck_hold_ms) > 0:
                    ctx.sleep(int(body.suck_hold_ms) / 1000.0)
                S.face_slot_put(state, "upper", piece=second, ok=True)
                _move(ctx, pmap, second + "上", speed, dry, "抬起", face=S.FACE_BACK)

            # ④ 送检 → **先反面 N 帧 → 切到正面 → 正面 N 帧**、分组投票（两个独立结论）
            #   ★ 2026-10-11 用户需求 2（「取完两个件之后、去 C 检测之前先经过"统一的正上位"」）：
            #     **查证结果 = 点位库里没有名为"正上位"的统一点位**（`S.expected_points()` 的
            #     正上位是**每格各自**的 `A{i}上`/`B{i}上`，共 36 个；`/api/points` 里也无"正上位"）。
            #     ⇒ 按用户口径**不猜、不臆造点名** → **保持现状**：只保留"各料自己的正上方"
            #       （①/③ 的 `A{i}上`），**不新增**一个"统一正上位"点。待用户给出点名后再接。
            #   ★★ 2026-10-11（本轮优化）：到 C 时的**到达面 = 首个采集面**
            #      （`faces_for_capture` 首槽对应的面，经唯一映射口 `_face_of_slot`）——
            #      即"到达即开拍"：到达面与首组帧的面一致 → 首组帧**不需要额外翻面**。
            #      · 两件趟：首组帧 = **正面(lower)**（用户更新口径："第一次检测正面"）→ 到达即在正面；
            #        拍完正面后切反面拍 → 拍完**停在反面** → 接着"先送反面那件"**不用翻面**。
            #      · 单件趟（len(trip)==1）：首组帧 = 正面(lower) → 到达仍在**正面**（零回归）。
            #   ★ 拍照面序仍**唯一口径** `S.faces_for_capture`（先正面、后反面，用户"顺序不能乱"）。
            #   ★ `_set_cam_face`（见下）依旧"面变了才摆 J6"：到达面 == 首组帧面 → 首组帧不重复下发。
            faces = S.faces_for_capture(len(trip))
            arrival_face = _face_of_slot(faces[0]) if faces else S.FACE_FRONT
            # ★★ 送检返回**本次真正下发的目标**（已按 `arrival_face` 解算）：
            #   下面"是否到达 C 点"的校验要用它当期望 —— **校验与实发结构性同源**。
            #   两件趟 arrival_face=反面 → 期望 J6 = ∓180 那一侧 → 偏差≈0（不再误报 180°）；
            #   单件趟 arrival_face=正面 → 与 C 点自带姿态一致 → 零回归。
            cam_target = _move(ctx, pmap, cam, speed, dry, "送检", face=arrival_face)
            _set(stage="停顿")
            if ctx.sleep(int(body.dwell_ms) / 1000.0):
                stop_reason = "已被停止"
                phase = "cancelled"
                break
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
                # ★★ 期望值取**实发那次的目标**（`cam_target`，按 `arrival_face` 解算）
                #   → 与实发**结构性同源**：到达面=反面时期望 J6=∓180、偏差≈0（不再误报 180°）；
                #   真没到位（如 J2 偏 10°）仍 > 容差 → 照报"未到达 C 检测点"（校验未放宽）。
                dev = _camera_deviation(cam_target)
                if dev is not None and dev > CAM_ARRIVE_TOL_DEG:
                    raise _Stop("未到达 C 检测点（实际偏差 %.1f° > %.1f°），不判色"
                                % (dev, CAM_ARRIVE_TOL_DEG), "判色", done + 1)
                _set(stage="判色")
                _cur = {"face": arrival_face}   # 到 C 时 J6 已停在**到达面**（= 首个采集面，上面那次 _move 已摆好）

                def _set_cam_face(slot, _cur=_cur):
                    """每面拍照前把 J6 摆到该面对应姿态（lower→正面 / upper→反面）。

                    ★ 只在"面变了"时才下发（同姿态不重复写，省一次下发）；
                      仍是**同一点位、只改 J6**（`_move` + `face`），不是整姿态平移。
                    ★ `_cur=_cur` 是**显式绑定**（B023）：`_cur` 在外层 `for` 里赋值，
                      闭包引用它会被判"晚绑定"；绑定成默认参数后语义不变且更明确。
                    """
                    f = _face_of_slot(slot)
                    if S.norm_face(f) == _cur["face"]:
                        return
                    _move(ctx, pmap, cam, speed, dry, "送检·%s面" % S.face_zh(f), face=f)
                    _cur["face"] = S.norm_face(f)

                verdicts = _capture_per_face(ctx, faces, DUAL_FRAMES_PER_FACE,
                                             min_conf, "判色", set_face=_set_cam_face)
                if verdicts:
                    _set(frame_detail=verdicts[0].get("frame_detail") or [],
                         best_frame=verdicts[0].get("best_frame"),
                         votes=verdicts[0].get("votes") or {})
            _set(color=",".join(str(v.get("color") or "") for v in verdicts))

            # ⑤ 落料：**先反面那件、再正面那件**
            #   （★ 2026-10-11 更新口径：用户「先放回第二次，然后放回第一次」——
            #     拍完停在反面 → 先送反面那件**不用翻面**；再翻到正面送正面那件）。
            #   ★ 顺序**只由面决定**（`S.order_for_drop`：upper=反面 在前）——
            #     有效/无效只决定"放到哪"（B 槽 / 原路回自己 A 原位），**不改先后**。
            #   ★ 用哪面取的就用哪面放（`_face_of_slot(slot)`），移动时 J6 保持该面、不被目标点默认正面顶掉。
            ordered = S.order_for_drop(verdicts)
            for k, v in enumerate(ordered):
                slot = v.get("slot")
                idx = S.FACE_SLOTS.index(slot) if slot in S.FACE_SLOTS else 0
                src = trip[min(idx, len(trip) - 1)]
                face = _face_of_slot(slot)
                back = (S.norm_face(face) == S.FACE_BACK)
                if v.get("ok"):
                    plan = S.build_piece_plan(placement, v.get("color"))
                    if not plan["ok"]:
                        raise _Stop("%s" % plan["detail"], "选槽", done + 1)
                    tgt = plan["slot"]
                    _set(target=tgt)
                    _move(ctx, pmap, tgt + "上", speed, dry, "放料·正上方", face=face)
                    # ★★ 只放**这一面那一路**（front→1~4 位 / back→5~8 位）——
                    #   两面独立：放下板不碰反面、放反面不碰下板（独立钉，test_double_face.py）。
                    _dual_drop(ctx, dry, face=face, st=state)
                    if back:
                        suck_back_on = False
                        blow_back_on = False
                    else:
                        suck_on = False
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
                              go_home=False, face=face)
                    if back:
                        suck_back_on = False
                        blow_back_on = False
                    else:
                        suck_on = False
                        blow_on = False
                S.face_slot_take(state, slot)
                done += 1

            # ⑥ 两件都放完才回起点（件与件之间不回起点）
            _move(ctx, pmap, start, speed, dry, "回起点")

        if not stop_reason:
            _set(stage="收尾·回开始/结束点")
            _move(ctx, pmap, start, speed, dry, "收尾回原点")
    except _Stop as e:
        # 停止/异常 → 按面收气（下板 1~4 位 / 反面 5~8 位，哪面有件关哪面）
        _close_air("异常")
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
        _close_air("内部异常")
    finally:
        ctx.finish(phase)
        try:
            _close_air("收尾")
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
    # ★ 2026-10-11 用户口径：「测试点时不要限速，所有的速度必须右上角的滑轮同步」——
    #   「到点/复核」是**测试点位**的运动，速度必须来自右上角滑块（请求体 speed_pct），
    #   不再回落到 config 里那个固定的 default_speed_pct（它恒为 5%，等于把测试点限速到 5%）。
    #   旧前端/脚本没带该字段时仍兼容（None → 回落 config 默认）。
    speed_pct: Optional[int] = None
    # ★★ 2026-10-11 旋转管控（本轮）：到点**默认不旋转**（`auto` = 保持当前 J6）。
    #   要翻面必须**显式**两件事都到位：`face="front"|"back"` **且** `allow_rotate=true`。
    #   见 `S.effective_face` —— 未授权时任何 face 都会被降级成 keep（fail-safe）。
    face: Optional[str] = Field(default="auto", max_length=8,
                                description="auto=不旋转(默认) / front / back（后者需 allow_rotate=true）")
    allow_rotate: bool = Field(default=False,
                               description="★ 是否允许本次翻 J6（默认 false = 一律不旋转）")


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
    # ★★ 2026-10-12 **终版**（用户原话「去掉 J6正面和反面录的 0 和 180 的值……**不用录制正反面**」）：
    #   这两个名字是**虚拟切面命令**（正面 = 该点自身录制的 J6 / 反面 = 当前点 J6 − 180，
    #   全部由代码按终版口径折算）—— **不再需要也不允许示教**，直接 400 拒绝。
    #   点位库里历史遗留的 `J6正面`/`J6反面` 两行已**无任何消费方**（goto 走虚拟命令、
    #   翻转不读库），可删可留。
    if S.is_j6_face_point(name):
        raise HTTPException(400, detail="「%s」无需示教：正面/反面已改为自动折算"
                            "（正面 = 该点自身录制的 J6，反面 = 当前点 J6 的值 − 180），"
                            "界面上的正面/反面按钮就是原地旋转，不用录制" % name)
    j = [round(float(v), 3) for v in (body.joints or _live_joints())]
    if len(j) != 6:
        raise HTTPException(400, detail="关节角必须是 6 个数值")
    payload = json.dumps(j, ensure_ascii=False)
    db = SessionLocal()
    try:
        row = next((p for p in list_points(db) if p.name == name), None)
        if row is None:
            row = create_point(db, name=name, group="分拣", kind="joint",
                               joints_json=payload, note=(body.note or "分拣示教"))
            action = "created"
        else:
            update_point(db, row.id, group="分拣", kind="joint",
                         joints_json=payload, note=(body.note or row.note))
            action = "updated"
    finally:
        db.close()
    emit_event("sorting", "info", "sorting.teach",
               "示教点位 %s（%s）" % (name, "新建" if action == "created" else "覆盖"),
               {"name": name, "joints": j, "action": action}, actor="control")
    return {"ok": True, "name": name, "joints": j, "action": action}


def _goto_speed(body) -> int:
    """`/sorting/goto` 的速度 = 请求体 `speed_pct`（右上角滑块）→ 夹到 [5,100]；
    没带（旧前端/脚本）→ 回落 config 默认。★ 与"测试点被限速"的旧 bug 相对。"""
    if body.speed_pct is not None:
        return clamp_speed(int(body.speed_pct))
    try:
        return clamp_speed(int(get_config().get("control", "default_speed_pct", default=5) or 5))
    except Exception:                                  # noqa: BLE001
        return clamp_speed(5)


def _freeze_j6_at(target, live):
    """把目标 `target` 的第 6 轴**冻结为当前实时 J6**（其余轴不动）；拿不到实时 → 原样返回。"""
    if not live or not target:
        return target
    try:
        return S.compose_j6_only(target, float(live[5]))
    except Exception:                                  # noqa: BLE001 —— 拿不到就保持原目标
        return target


def _goto_segment(seg: str, sp: int, *, base=None, freeze_j6: bool = False):
    """**单段**到点下发（读库 → 解算（默认不旋转）→ 下发）→ 返回**实际下发的 6 值目标**。

    ★ 只给「原位安全路径」（自动补的垂直段 + 目标段）复用；闸门/围栏/速度已由调用方过。
    ★ `base` = **上一段的终点**（物理正确的基准；缺省用实时姿态）—— 链式传递才能让
      "切面 → 回原位"那一段沿用**刚切好的面**（用实时姿态会拿旧值、把面转回去）。
    ★ `freeze_j6=True` → 这一段是**垂直升降**（原位 ↔ 正上位）→ J6 冻结 `base[5]`，途中不转。
    """
    _b = [float(v) for v in base] if base else None
    if S.is_j6_face_point(seg):
        # ★ 虚拟切面命令：**不查点位库**（库里没有那一行）——基姿态 = 上一段终点 / 实时姿态
        _seed = _b if _b else _live_joints_or_none()
        if not _seed:
            raise HTTPException(503, detail="「%s」是原地切面命令，需要当前实时姿态" % seg)
        j = [float(v) for v in _seed]
    else:
        db = SessionLocal()
        try:
            row = next((p for p in list_points(db) if p.name == seg), None)
        finally:
            db.close()
        if row is None:
            raise HTTPException(404, detail="点位「%s」尚未示教（原位安全路径需要双方的正上位）" % seg)
        try:
            j = parse_joints(row.joints, where="点位「%s」" % seg)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
    target, _only, reason = S.resolve_target_joints(
        seg, j, (lambda: _b), face=S.effective_face(seg, S.FACE_AUTO, rotate=False))
    if reason:
        raise HTTPException(409, detail=reason)
    if freeze_j6 and _b:
        target = S.compose_j6_only(target, float(_b[5]))
    r = motion.command(target, int(sp), 0)
    if not r.get("ok"):
        raise HTTPException(502, detail="移动到「%s」失败：%s" % (seg, r.get("error")))
    return target


@router.post("/goto")
def api_goto(body: NameIn, tok: str = Depends(require_control)):
    """移动到该示教点位（复核示教用）。★ 这是**运动**：走围栏互锁 + 轴锁 + 速度夹取。

    ★ 速度（2026-10-11 用户口径：「测试点时不要限速，所有速度必须右上角滑块同步」）：
      取**请求体 speed_pct（= 右上角滑块）**，只夹到寄存器合法范围 [5,100]；不再固定用 config 的
      `default_speed_pct`（那会把测试点强行限到 5%）。

    ★★ 2026-10-12 **终版**（用户原话「**不用录制正反面**……正面就是之前录的 J6，
      反面就是当前点 J6 的值减掉 180 度」）：`J6正面`/`J6反面` 是**虚拟切面命令** ——
      **不再查点位库**：基姿态 = **当前实时关节**，目标面由**点名**决定
      （已在目标面 → 原地零动作；要翻 → **当前 J6 − 180** 折算），只覆盖第 6 轴；
      拿不到实时基姿态 → **拒绝**（绝不用库里旧行偷偷整姿态下发）。

    分拣运行期间禁止（与手动点动同一套互锁理由：抢同一个 40135 触发位）。
    """
    name = _require_sorted_name(body.name)
    with _gate:
        if _job.get("thread") and _job["thread"].is_alive():
            raise HTTPException(409, detail="分拣正在运行，先「停止」再手动到点")
    # ★ 统一"未就绪"闸门：到点是真实运动，伺服未上电 → 拒绝并明确提示
    #   （放在点位查询之前：伺服没上电时先给"机器人没有就绪"，而不是先纠结点位）。
    reject_if_not_ready()
    # ★ 速度先算（"原位安全路径"拆段时每段共用它）
    sp = _goto_speed(body)
    # ★★ 2026-10-12 终版：虚拟切面命令**跳过查库**（库里历史遗留行已无消费方）。
    face_pt = S.is_j6_face_point(name)
    row = None
    if not face_pt:
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
    # ★★★ 2026-10-12 终版：**「原位安全路径」自动补垂直段**（用户口径，三条 + 旋转一条）：
    #   「**禁止原位到原位执行，必须 原位 → 正上位 → 目标的正上位 → 目标点原位**」
    #   「**禁止原位去起点和摄像头点，必须先 原位 → 正上位 → 起点或者摄像头点**」
    #   「**禁止摄像头点位或者起点直接去原点，必须 摄像头点或者正上位 →（目标的正上位）→ 原点**」
    #   「**如果原位旋转，先回到正上位，旋转，然后来到原位**」
    #   ⇒ 安全模型：**垂直只在"某格正上位 ↔ 该格原位"；横向只在"正上位/起点/摄像头"之间**；
    #     且**升降两段 J6 冻结**（「原位到正上位更不能旋转」，见下 `_freeze_j6_at`）。
    #   ★ 拿不到实时姿态 → **不拒绝**（用户口径「不要禁止」）：退回原单段路径；
    #     真实下发由"未就绪闸门"兜底；读不到点位库 → 也无法算安全路径 → 同样退回单段。
    _live0 = None
    try:
        _live0 = _live_joints()
    except Exception:                                  # noqa: BLE001 —— 拿不到实时姿态
        _live0 = None                                  # ★ 不拒绝（用户口径「不要禁止」）；
                                                       #   真实下发本就被"未就绪闸门"兜底
    _path = None
    _prev0 = None
    if _live0:
        try:
            _poses0 = S.all_point_poses()
        except Exception:                              # noqa: BLE001 —— 读不到点位库
            _poses0 = None                             # ★ 不拒绝 → 退回单段（同上）
        if _poses0 is None:
            _prev0 = None
        else:
            _prev0 = S.point_at_pose(_live0, _poses0)
            if face_pt:
                # 切面：**抬起 → 切 → 回原位**（不在原位 → 单段，就是原来的"原地切面"）
                _oslot = S.current_raw_slot(_live0, _poses0)
                _path = [_oslot + "上", name, _oslot] if _oslot else [name]
            else:
                _path, _preason = S.raw_slot_safe_path(_live0, _poses0, name)
                if _preason:
                    raise HTTPException(409, detail=_preason)
    if _path is not None and len(_path) == 0:
        # `path == []` = 已经在目标点（交互入口如实报「无需移动」，不下发）
        raise HTTPException(409, detail=("机器人已经停在「%s」—— 无需移动（已拒绝）" % name))
    if _path and len(_path) > 1:
        _from0 = _prev0                                  # 起点（识别到的当前位置，供日志核对）
        # ★ 链式基准：每一段都以**上一段的终点**为基准（"切面 → 回原位"才能沿用刚切好的面）
        _base0 = [float(v) for v in _live0]
        for _seg in _path:
            # ★ 垂直段（原位 ↔ 正上位）冻结 J6 ——「原位到正上位更不能旋转」
            _t0 = _goto_segment(_seg, sp, base=_base0,
                                freeze_j6=S.is_vertical_move(_prev0, _seg))
            if _t0:
                _base0 = [float(v) for v in _t0]
            _prev0 = _seg
        emit_event("sorting", "info", "sorting.goto",
                   "原位安全路径（起点「%s」）：%s" % (_from0 or "?", " → ".join(_path)),
                   {"name": name, "path": _path, "from": _from0, "speed_pct": sp},
                   actor="control")
        return {"ok": True, "name": name, "speed_pct": sp,
                "j6_only": False, "path": _path, "from": _from0}
    if face_pt:
        # ★ 虚拟切面命令：基姿态 = 当前实时关节；
        #   拿不到 → 503 如实拒绝（"切面"必须以当前姿态为基，绝不猜）。
        if _live0 is None:
            raise HTTPException(503, detail="「%s」是原地切面命令，需要当前实时姿态（机器人未连接/无实时数据）"
                                % name)
        base = _live0
        j = [float(v) for v in base]
        target, _j6_only, _j6_reason = S.resolve_target_joints(
            name, j, (lambda: base), face=S.FACE_FRONT)
    else:
        try:
            j = parse_joints(row.joints, where="点位「%s」" % name)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        # ★★ 统一口径（2026-10-11 用户原话「我在正上位反面，去原料区是变成了正面」）：
        #   **目标是原料位（A1…A9/B1…B9）→ 一律 `keep`（保持当前面、绝不翻面）**。
        # ★★★ 2026-10-11 旋转管控（用户原话「其他时间一律不允许旋转」）：
        #   到点**默认不旋转** —— `S.effective_face` 在**未授权**时把任何 face 降级成 `keep`；
        #   只有显式 `allow_rotate=true`（脚本/联调）才可能按 `face` 翻面，且原料位仍强制 keep。
        target, _j6_only, _j6_reason = S.resolve_target_joints(
            name, j, _live_joints,
            face=S.effective_face(name, body.face, rotate=bool(body.allow_rotate)))
    if _j6_reason:
        raise HTTPException(409, detail=_j6_reason)
    # ★★ 「原位到正上位更不能旋转」：**垂直升降段（原位 ↔ 其正上位）J6 一律冻结**（保持当前值）。
    if _live0 and S.is_vertical_move(_prev0, name):
        target = _freeze_j6_at(target, _live0)
    # ★ 速度已在函数开头算好（`sp = _goto_speed(body)`）—— 请求体带 speed_pct（= 右上角滑块）
    #   就用它，只夹到寄存器合法范围 [5,100]；旧前端/脚本没带时才回落 config 默认（向后兼容）。
    r = motion.command(target, sp, 0)
    if not r.get("ok"):
        raise HTTPException(502, detail="移动到「%s」失败：%s" % (name, r.get("error")))
    emit_event("sorting", "info", "sorting.goto",
               ("原地切面：%s（仅 J6）" % name) if _j6_only else ("手动到点：%s" % name),
               {"name": name, "speed_pct": sp, "j6_only": _j6_only}, actor="control")
    return {"ok": True, "name": name, "speed_pct": sp, "j6_only": _j6_only}


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
