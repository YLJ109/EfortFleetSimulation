# -*- coding: utf-8 -*-
"""安全围栏配置与报警事件 API。

- 配置持久化在 config/safety.json（与 robot.yaml 同级，便于手工编辑/备份）
- 报警事件写入 SQLite，供前端时间线展示
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_admin, require_control, token_role
from app.core.deps import get_db
from app.core.exceptions import AppError
from app.core.safety_config import (
    SafetyConfigError,
    default_safety,
    load_safety,
    reset_safety,
    save_safety,
    validate_safety,
)
from app.db.crud import (
    clear_safety_events,
    get_safety_version,
    insert_safety_event,
    insert_safety_version,
    list_safety_events,
    list_safety_versions,
    prune_safety_versions,
)
from app.services.events import emit as emit_event
from app.services import safety_evidence
from app.services.safety_eval import ssm_advice
from app.services.safety_eval import sweep as safety_sweep
from app.services.safety_guard import snapshot as guard_snapshot
from app.services.safety_guard import update as guard_update

router = APIRouter(prefix="/api", tags=["safety"])


class SafetyIn(BaseModel):
    """整体配置（直接透传，后端做结构校验）。"""

    config: Dict[str, Any]


class EventIn(BaseModel):
    zone_id: str = ""
    zone_name: str = ""
    state: str = Field(..., description="danger | hit | clear")
    clearance: float = 0.0
    ratio: float = 0.0


class EventOut(BaseModel):
    id: int
    timestamp: str
    zone_id: str = ""
    zone_name: str = ""
    state: str = ""
    clearance: float = 0.0
    ratio: float = 0.0


# ★ 审计修复 P2-E：原文件在 :67 与 :227 定义了**两个同名 _iso**，
#   Python 后者覆盖前者 → :145 的 `_iso(ts) + "Z"` 实际拿到的已经是带 Z 的
#   字符串，拼出 `…ZZ` 这种非法时间戳（前端 new Date() 得到 Invalid Date）。
#   这里只保留一份，并让它统一负责"naive datetime → UTC Z 后缀"。
def _iso(dt) -> str:
    try:
        if not dt:
            return ""
        return dt.isoformat() + "Z" if dt.tzinfo is None else dt.isoformat()
    except Exception:
        return ""


@router.get("/safety")
def api_get_safety():
    """读取当前安全围栏配置。"""
    return load_safety()


def _archive(db: Session, prev_cfg: dict, actor: str, source: str, note: str) -> None:
    """把改动前的配置归档为历史版本（失败不影响主流程 —— 版本历史是锦上添花）。"""
    try:
        if json.dumps(prev_cfg, sort_keys=True) == json.dumps(load_safety(), sort_keys=True):
            return                       # 与原配置完全相同，没必要往历史里塞垃圾
        insert_safety_version(db, json.dumps(prev_cfg, ensure_ascii=False),
                              actor=actor, source=source, note=note)
        prune_safety_versions(db, keep=30)
    except Exception:
        pass


@router.put("/safety")
def api_put_safety(body: SafetyIn, tok: str = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """保存配置（★ 管理员权限；后端强校验，非法返回 400 并带具体字段）。"""
    actor = token_role(tok) or "unknown"
    prev = load_safety()
    try:
        cfg = save_safety(body.config)
    except SafetyConfigError as e:
        emit_event("config", "error", "safety.save_failed",
                   f"围栏配置保存被拒绝：{str(e)[:120]}", {"reason": str(e)}, actor=actor)
        raise AppError(f"配置校验失败: {e}", 400, "SAFETY_CONFIG_INVALID", str(e))
    _archive(db, prev, actor, "save", "保存前自动归档")
    emit_event("config", "info", "safety.save", "安全围栏配置已保存",
               {"zones": len(cfg.get("zones", [])), "enabled": cfg.get("enabled", True)},
               actor=actor)
    return {"ok": True, "config": cfg}


@router.post("/safety/reset")
def api_reset_safety(tok: str = Depends(require_admin), db: Session = Depends(get_db)):
    """恢复出厂默认配置（★ 管理员权限，重置前自动归档当前配置）。"""
    actor = token_role(tok) or "unknown"
    prev = load_safety()
    cfg = reset_safety()
    _archive(db, prev, actor, "reset", "恢复默认前自动归档")
    emit_event("config", "warn", "safety.reset", "围栏配置已恢复出厂默认", actor=actor)
    return {"ok": True, "config": cfg}


@router.get("/safety/default")
def api_default_safety():
    """返回出厂默认（供前端"重置"预览/撤销用，不落盘）。"""
    return default_safety()


@router.post("/safety/validate")
def api_validate_safety(body: SafetyIn):
    """只校验不落盘（前端可先试算）。"""
    try:
        cfg = validate_safety(body.config)
    except SafetyConfigError as e:
        raise AppError(f"配置校验失败: {e}", 400, "SAFETY_CONFIG_INVALID", str(e))
    return {"ok": True, "config": cfg}


@router.get("/safety/events")
def api_list_events(limit: int = Query(100, ge=1, le=1000),
                    db: Session = Depends(get_db)):
    rows = list_safety_events(db, limit)
    return [
        EventOut(
            id=r.id,
            timestamp=_iso(r.timestamp),   # ★ P2-E：_iso 已负责 Z 后缀，不再二次拼接
            zone_id=r.zone_id,
            zone_name=r.zone_name,
            state=r.state,
            clearance=r.clearance,
            ratio=r.ratio,
        )
        for r in rows
    ]


@router.post("/safety/events")
def api_add_event(body: EventIn, tok: str = Depends(require_control),
                  db: Session = Depends(get_db)):
    """记录一条报警事件。只在状态变严重/恢复时调用，避免每帧刷库。

    ★ 同时镜像一份到统一事件总线，让「运维时间线」里能看到报警，不必两个数据源来回翻。
    ★ 审计修复 P2：本接口原来**无鉴权**（同文件 DELETE 却要 admin），
      匿名可无限伪造"碰撞报警"刷库 + 污染审计时间线。与下发侧同一把钥匙。
    """
    if body.state not in ("danger", "hit", "clear"):
        raise AppError("state 必须是 danger / hit / clear", 400, "BAD_SAFETY_STATE")
    row = insert_safety_event(db, body.zone_id, body.zone_name, body.state,
                             body.clearance, body.ratio)
    level = {"danger": "warn", "hit": "critical", "clear": "info"}.get(body.state, "info")
    zone = body.zone_name or body.zone_id or "未命名区域"
    msg = {
        "danger": f"接近/危险：{zone}",
        "hit": f"碰撞报警：{zone}",
        "clear": f"已恢复安全：{zone}",
    }.get(body.state, zone)
    emit_event("safety", level, f"safety.{body.state}", msg, {
        "zone_id": body.zone_id, "zone_name": body.zone_name,
        "clearance": body.clearance, "ratio": body.ratio,
    })
    return {"ok": True, "id": row.id}


@router.delete("/safety/events")
def api_clear_events(tok: str = Depends(require_admin), db: Session = Depends(get_db)):
    """清空报警事件（★ 管理员权限）。"""
    n = clear_safety_events(db)
    emit_event("config", "info", "safety.events_clear",
               f"报警事件已清空（{n} 条）", {"deleted": n},
               actor=token_role(tok) or "unknown")
    return {"ok": True, "deleted": n}


# =====================================================================
# 围栏实时状态上报（阶段 4：把"前端算出来的余量"喂给后端做下发互锁）
# =====================================================================
class LiveIn(BaseModel):
    state: str = "safe"                  # safe | warn | danger | hit
    zone_id: str = ""
    zone_name: str = ""
    clearance: float = 0.0
    ratio: float = 1.0


@router.post("/safety/live")
def api_report_live(body: LiveIn, tok: str = Depends(require_control)):
    """前端每帧评估围栏后节流上报（1s 一次），后端据此拦截危险状态下的下发。

    ★ 审计修复 P0-3：本接口**原来无鉴权** —— 任何能连到 8000 的客户端都可以
      持续上报 state="safe" 来**架空围栏互锁**，或伪造 danger 让产线停摆。
      由于下发侧 /api/control/* 本来就要求控制令牌，上报侧挂同一把钥匙是自洽的：
      能下发的人才有资格告诉后端"围栏安全"。
    """
    if body.state not in ("safe", "warn", "danger", "hit"):
        raise AppError("state 必须是 safe / warn / danger / hit",
                       400, "BAD_SAFETY_STATE")
    import math
    if not math.isfinite(float(body.clearance)) or not math.isfinite(float(body.ratio)):
        raise AppError("clearance/ratio 必须是有限数值", 400, "BAD_SAFETY_NUMBER")
    return guard_update(body.state, body.zone_id, body.zone_name,
                        body.clearance, body.ratio)


@router.get("/safety/live")
def api_get_live():
    """查看互锁状态是否有效。

    ★ 2026-10-12 双源化：返回体除前端那一路的扁平键外，新增
      `client/server/merged/mode/mode_reason`，且每个源都带**三态** freshness
      （never / fresh / stale）。★ 界面**必须**按三态显示：never/stale 时不许写"安全"，
      要写"没有实时数据"（§5.5 铁律③：不知道就说不知道）。
    """
    return guard_snapshot()


# =====================================================================
# A2 路径几何扫掠（"这条路会不会擦到围栏/台面"）+ A3 SSM 建议
# =====================================================================
class SweepIn(BaseModel):
    from_joints: List[float] = Field(..., description="起点 6 关节角(度)")
    to_joints: List[float] = Field(..., description="终点 6 关节角(度)")
    samples: int = Field(24, ge=2, le=96)


@router.post("/safety/sweep")
def api_sweep(body: SweepIn, tok: str = Depends(require_control)):
    """两段关节角之间的**几何**扫掠结论（残影/预演的"提前预警"用）。

    ★★ 诚实边界（务必带到界面上，别只画个绿勾）：
      · 插值是**关节空间等分**，控制器 210 走的是它自己的加减速轨迹 —— 这是**保守包络估计**，
        不是真实轨迹；
      · PC 只能"写目标 + 触发位"、采样 10Hz 且没有期望轨迹的时间基准 ⇒ **没有 ETA**，
        绝不输出"几秒后到达/几点几分越界"这种推断；
      · 只判围栏 + 立足面净空，不判工件/夹具碰撞。
    """
    if len(body.from_joints) != 6 or len(body.to_joints) != 6:
        raise AppError("from_joints / to_joints 必须是 6 个关节角", 400, "BAD_JOINTS")
    import math
    for v in list(body.from_joints) + list(body.to_joints):
        if not math.isfinite(float(v)):
            raise AppError("关节角必须是有限数值", 400, "BAD_JOINTS")
    r = safety_sweep(list(body.from_joints), list(body.to_joints), n=body.samples)
    r["eta"] = None            # ★ 明确写 null：有这个键但没有值，界面就不会自己编
    return r


@router.get("/safety/ssm")
def api_ssm(distance_mm: Optional[float] = Query(None),
            speed_pct: Optional[float] = Query(None)):
    """A3 SSM 式分隔距离/速度上限**建议**（ISO/TS 15062 `S = K·T + C`）。

    ★★ 只提示，**绝不闭环**：本接口不写 40103、不动右上角滑块、不改 `ui_speed`
      （速度唯一口径 = 界面滑块，AGENTS.md §5.3）。
    ★ **T / K100 / C 必须现场实测**（`config/robot.yaml: safety.stop_time_s /
      ssm_k100_mm_s / ssm_c_mm`）。没实测就 `available=false` 并把缺的量**逐条列进
      `unknown`** —— 界面向操作员显示"未实测 → 无法给出"，**不许**显示"安全"，
      更不许用网上抄的"典型停机时间 0.3s"给现场划安全距离。
    """
    return ssm_advice(distance_mm=distance_mm, speed_pct=speed_pct)


# =====================================================================
# A5 事件取证包（danger/hit 上升沿自动落盘，兑现"每一步都能自证"）
# =====================================================================
@router.get("/safety/evidence")
def api_evidence_list(limit: int = Query(30, ge=1, le=200),
                      offset: int = Query(0, ge=0),
                      tok: str = Depends(require_control)):
    """取证包列表（按 **seq 倒序**，最新在前）。

    ★★ 前端刷新水位**必须**用返回的 `latest_seq`，不许用 `packs.length` ——
      列表会被 `evidence.keep_days` / `max_packs` 裁短，长度不是单调的（§5.5 铁律①，
      和当年 `log.length` 满 240 行后水位失效是同一个坑）。
    ★ 每包的 `honest_gaps` 逐条写明"这次没采到什么、为什么"（相机没开 / 寄存器没读到 /
      3D 截图没有上传通道）—— **缺就说缺**，不静默省略。
    """
    return safety_evidence.list_packs(limit=limit, offset=offset)


@router.get("/safety/evidence/{pack_id}")
def api_evidence_detail(pack_id: str, tok: str = Depends(require_control)):
    """单包详情：manifest + 文件清单 + 样本/事件预览（读不了就如实报错，不返回半截空壳）。"""
    d = safety_evidence.get_pack(pack_id)
    if not d.get("ok"):
        raise AppError(d.get("error") or "取证包不存在", 404, "EVIDENCE_NOT_FOUND")
    return d


@router.get("/safety/evidence/{pack_id}/zip")
def api_evidence_zip(pack_id: str, background: BackgroundTasks,
                     tok: str = Depends(require_control)):
    """整包下载（manifest + samples.jsonl + events.jsonl + frame.jpg）。"""
    path, err = safety_evidence.zip_pack(pack_id)
    if not path:
        raise AppError(err or "取证包打包失败", 404, "EVIDENCE_ZIP_FAILED")
    background.add_task(_unlink, path)
    return FileResponse(path, filename=f"{pack_id}.zip", media_type="application/zip")


def _unlink(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


# =====================================================================
# 配置版本化（阶段 4）：历史版本列表 / 查看 / 回滚
# =====================================================================
class VersionOut(BaseModel):
    id: int
    created_at: str
    actor: str = ""
    source: str = ""
    note: str = ""
    zones: int = 0
    enabled: bool = True


@router.get("/safety/versions", response_model=List[VersionOut])
def api_list_versions(db: Session = Depends(get_db),
                      tok: str = Depends(require_control)):
    """历史版本列表（摘要，不含完整配置）。"""
    out = []
    for v in list_safety_versions(db, limit=50):
        try:
            cfg = json.loads(v.config or "{}")
        except Exception:
            cfg = {}
        out.append(VersionOut(
            id=v.id, created_at=_iso(v.created_at), actor=v.actor,
            source=v.source, note=v.note,
            zones=len(cfg.get("zones", []) or []),
            enabled=bool(cfg.get("enabled", True)),
        ))
    return out


@router.get("/safety/versions/{vid}")
def api_get_version(vid: int, db: Session = Depends(get_db),
                    tok: str = Depends(require_control)):
    """查看某个历史版本的完整配置（回滚前先看看内容）。"""
    v = get_safety_version(db, vid)
    if not v:
        raise AppError("版本不存在", 404, "VERSION_NOT_FOUND")
    try:
        cfg = json.loads(v.config or "{}")
    except Exception:
        cfg = {}
    return {"id": v.id, "created_at": _iso(v.created_at), "actor": v.actor,
            "source": v.source, "note": v.note, "config": cfg}


@router.post("/safety/versions/{vid}/rollback")
def api_rollback_version(vid: int, db: Session = Depends(get_db),
                         tok: str = Depends(require_admin)):
    """回滚到某个历史版本（★ 管理员权限；回滚前会把当前配置再归档一次）。"""
    actor = token_role(tok) or "unknown"
    v = get_safety_version(db, vid)
    if not v:
        raise AppError("版本不存在", 404, "VERSION_NOT_FOUND")
    try:
        target = json.loads(v.config or "{}")
    except Exception as e:
        raise AppError(f"历史版本数据损坏: {e}", 400, "VERSION_CORRUPTED")

    prev = load_safety()
    try:
        cfg = save_safety(target)      # 走完整校验，坏数据不会污染线上
    except SafetyConfigError as e:
        raise AppError(f"历史版本校验失败: {e}", 400, "SAFETY_CONFIG_INVALID", str(e))

    _archive(db, prev, actor, "rollback", f"回滚至版本 #{vid} 前自动归档")
    emit_event("config", "warn", "safety.rollback",
               f"围栏配置已回滚到历史版本 #{vid}", {"version": vid}, actor=actor)
    return {"ok": True, "config": cfg, "from_version": vid}
