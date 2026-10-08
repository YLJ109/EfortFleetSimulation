# -*- coding: utf-8 -*-
"""三色方块分拣（独立分拣页后端）—— 流程引擎 + 接口。

链路（全部经控制器常驻程序 210，不加载新程序）：
  取料：移到 A{i}上 → 移到 A{i} → 吸（40135.Bit1）
  送检：抬起 → 移到 C检测 → 停顿 dwell_ms → 抓 N 帧判色（/vision/detect）
  放料：按颜色选 B 槽 → 移到 B{j}上 → 移到 B{j}
        → 停止吸(Bit2) → 放(Bit3) → 等 blow_ms → 停止放(Bit4) → 抬起
  收尾：9 件完成 → 回开始/结束点

现场口径（用户 2026-10-08）：
  · 不做真空反馈检测；**吸不到/空跑/检测不到 → 直接停**，然后**回到开始/结束点**；
  · 某列放满 3 件又来同色 → 停（"对满了停"）；
  · 不做任何自动恢复/重试绕行。

安全：每步都过围栏互锁；速度沿用全局那一份；吸放与运动共用执行互斥；
      执行期间 /jog、/vacuum 会被 control 的互锁拒绝；急停随时可用。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_control
from app.api.control import new_run, request_cancel
from app.core.config import get_config
from app.core.logger import get_logger
from app.db.crud import list_points
from app.db.database import SessionLocal
from app.services import camera_client
from app.services import sorting as S
from app.services.events import emit as emit_event
from app.services.motion import motion, real_write_enabled
from app.services.safety_guard import check as guard_check
from app.utils.joints import parse_joints

log = get_logger("sorting")
router = APIRouter(prefix="/api/sorting", tags=["sorting"])

# 同时只允许一路分拣（与序列/文件执行共用一个"在跑"的世界观）
_gate = threading.Lock()
_job: Dict[str, object] = {"thread": None, "ctx": None, "state": None, "started_at": 0.0}


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
    dwell_ms: int = Field(default=1000, ge=0, le=10000, description="C 区停顿时长")
    frames: int = Field(default=3, ge=1, le=9, description="停顿后抓几帧判色")
    min_conf: Optional[float] = Field(default=None, ge=0.0, le=1.0,
                                      description="留空则用视觉服务当前阈值")
    blow_ms: int = Field(default=400, ge=0, le=3000, description="放料吹气时长")
    max_pieces: int = Field(default=9, ge=1, le=9, description="本轮做几件（调试可设 1）")


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
    min_conf = float(body.min_conf if body.min_conf is not None else (v.get("min_conf") or 0.3))

    ctx = new_run("三色分拣", kind="sorting")
    with _gate:
        _job["state"] = {
            "dry_run": bool(body.dry_run), "piece": 0, "total": int(body.max_pieces),
            "stage": "准备", "color": "", "conf": 0.0, "target": "",
            "placement": S.placement_summary(S.new_placement()),
            "error": "", "finished": False, "ok": None,
        }
        _job["started_at"] = time.time()
        th = threading.Thread(target=_run_sorting, args=(ctx, body, pmap, min_conf),
                              name="sorting", daemon=True)
        _job["thread"] = th
        _job["ctx"] = ctx
    th.start()
    emit_event("sorting", "info", "sorting.start",
               "开始分拣（%s，速度 %d%%，%d 件，抓 %d 帧）" % (
                   "空跑" if body.dry_run else "真实下发", body.speed_pct,
                   body.max_pieces, body.frames),
               {"dry_run": body.dry_run, "speed_pct": body.speed_pct,
                "frames": body.frames, "dry_run_note": "空跑不下发运动"},
               actor="control")
    return {"ok": True, "run_id": ctx.run_id, "dry_run": body.dry_run, "state": _job["state"]}


# ---------------------------------------------------------------- 引擎
def _set(**kw) -> None:
    with _gate:
        st = _job.get("state")
        if isinstance(st, dict):
            st.update(kw)


def _joints_of(pmap: Dict[str, object], name: str) -> List[float]:
    row = pmap.get(name)
    if row is None:
        raise _Stop("点位缺失：%s" % name)
    try:
        return parse_joints(getattr(row, "joints", "[]"), where="点位「%s」" % name)
    except ValueError as e:
        raise _Stop(str(e))


def _move(ctx, pmap, name, speed, dry, stage) -> None:
    """走一个示教点位（写目标 + 触发位 → 常驻 210 执行 MJOINT）。"""
    if ctx.wait_pause():
        raise _Stop("已被停止", stage)
    j = _joints_of(pmap, name)
    if dry:
        _set(stage=stage, last_point=name)
        return
    ok, reason, _snap = guard_check()
    if not ok:
        raise _Stop("安全围栏互锁：%s" % reason, stage)
    r = motion.command([float(v) for v in j], int(speed), 0,
                       abort_check=lambda: ctx.cancelled())
    _set(stage=stage, last_point=name)
    if not r.get("ok"):
        raise _Stop("运动到「%s」失败：%s" % (name, r.get("error")), stage)


def _air(ctx, action: str, dry: bool, stage: str) -> None:
    """吸/停止吸/放/停止放（40135.Bit1~Bit4 → 常驻 210 执行）。

    ★ 与运动共用 40135 寄存器，必须取执行互斥（否则与在飞的点动互相清触发位）。
    """
    label = {"suck": "吸", "release": "停止吸", "blow": "放", "unblow": "停止放"}.get(action, action)
    if dry:
        _set(stage="%s(空跑)" % label)
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
    _set(stage=label)
    if not ok:
        raise _Stop("%s 失败：%s" % (label, verr), stage)


def _capture(ctx, frames: int, min_conf: float, stage: str) -> Dict:
    """停顿后连抓 N 帧 → 投票。任何一帧失败都记下来，最终交给投票判"检测不到"。"""
    got: List[Dict] = []
    for i in range(int(frames)):
        if ctx.cancelled():
            raise _Stop("已被停止", stage)
        st, js, err = camera_client.get_json("/vision/detect", timeout=6)
        if st == 200 and not err and isinstance(js, dict):
            got.append(js)
        else:
            got.append({})
        if i + 1 < int(frames):
            ctx.sleep(0.15)
    v = S.vote_color(got, min_conf)
    log.info("判色 %d 帧 → %s (conf=%s) %s", len(got), v.get("color"),
             v.get("conf"), v.get("votes"))
    return v


def _release_part(ctx, dry: bool, blow_ms: int, stage: str) -> None:
    """放料时的脱件序列：停止吸 → 放(吹) → 等 → 停止放。

    ★ 为什么要吹一下（现场实测）：只关真空阀，工件会因残余负压/密封**吸住不掉**。
    """
    _air(ctx, "release", dry, stage)
    if blow_ms > 0:
        _air(ctx, "blow", dry, stage)
        ctx.sleep(blow_ms / 1000.0)
        _air(ctx, "unblow", dry, stage)


def _run_sorting(ctx, body: StartIn, pmap: Dict[str, object], min_conf: float) -> None:
    dry = bool(body.dry_run)
    speed = int(body.speed_pct)
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
            _set(piece=idx, stage="取料", color="", conf=0.0, target="")
            # 1) 取料：上方 → 槽位 → 吸
            _move(ctx, pmap, a_slot + "上", speed, dry, "取料·上方")
            _move(ctx, pmap, a_slot, speed, dry, "取料·下落")
            _air(ctx, "suck", dry, "取料·吸")
            # 2) 抬起 → 送检
            _move(ctx, pmap, a_slot + "上", speed, dry, "抬起")
            _move(ctx, pmap, cam, speed, dry, "送检")
            # 3) 停顿 + 抓帧判色
            _set(stage="停顿")
            if ctx.sleep(int(body.dwell_ms) / 1000.0):
                stop_reason = "已被停止"; phase = "cancelled"; break
            _set(stage="判色")
            v = _capture(ctx, int(body.frames), min_conf, "判色")
            _set(color=v["color"], conf=v["conf"], votes=v["votes"])
            if not v["ok"]:
                raise _Stop("检测不到（%s）" % v["reason"], "判色", idx)
            # 4) 按色选槽
            plan = S.build_piece_plan(placement, v["color"])
            if not plan["ok"]:
                raise _Stop("%s" % plan["detail"], "选槽", idx)
            tgt = plan["slot"]
            _set(target=tgt)
            # 5) 放料
            _move(ctx, pmap, tgt + "上", speed, dry, "放料·上方")
            _move(ctx, pmap, tgt, speed, dry, "放料·下落")
            _release_part(ctx, dry, int(body.blow_ms), "放料·脱件")
            S.place(placement, v["color"], tgt)
            _set(placement=S.placement_summary(placement))
            _move(ctx, pmap, tgt + "上", speed, dry, "放料·抬起")
            emit_event("sorting", "info", "sorting.piece",
                       "第 %d 件：%s → %s（conf %.2f）" % (idx, v["color"], tgt, v["conf"]),
                       {"piece": idx, "color": v["color"], "slot": tgt,
                        "conf": v["conf"], "votes": v["votes"], "dry_run": dry},
                       actor="control")

        # 收尾：回开始/结束点（正常也要回）
        if not stop_reason:
            _set(stage="收尾·回开始/结束点")
            _move(ctx, pmap, start, speed, dry, "收尾回原点")
    except _Stop as e:
        stop_reason = e.reason
        stop_piece = e.piece or int((_job.get("state") or {}).get("piece") or 0)
        if "停止" in e.reason:
            phase = "cancelled"
        else:
            phase = "error"
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
    finally:
        _set(stage="结束", error=stop_reason, finished=True,
             ok=(phase == "done"), phase=phase, piece=stop_piece or None)
        ctx.finish(phase)
        emit_event("sorting", "info" if phase == "done" else "warn", "sorting.stop",
                   ("分拣完成" if phase == "done" else "分拣结束：%s" % stop_reason),
                   {"phase": phase, "reason": stop_reason, "dry_run": dry,
                    "placement": S.placement_summary(placement)},
                   actor="control")
