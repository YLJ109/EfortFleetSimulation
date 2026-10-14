# -*- coding: utf-8 -*-
"""REST 接口: 预设点位 (Point) 的增删改查 + 批量导入/导出。"""
from __future__ import annotations

import json
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_control, token_role
from app.core.deps import get_db
from app.db.crud import (
    create_point,
    delete_point,
    get_point,
    list_points,
    update_point,
)
from app.services.events import emit as emit_event

router = APIRouter(prefix="/api/points", tags=["points"])

EXPORT_FORMAT = "efort-point/v1"


def _actor(tok: str) -> str:
    return token_role(tok) or "unknown"


def _norm_kind(s: Optional[str]) -> str:
    return s if s in ("joint", "cartesian") else "joint"


def _norm_joints(v) -> List[float]:
    """接受 [6] 或 {j1..j6}, 统一成 [j1..j6] 且限位裁剪交由运动学/执行层。"""
    if isinstance(v, dict):
        return [float(v.get(f"j{i}", 0.0)) for i in range(1, 7)]
    arr = [float(x) for x in (v or [0, 0, 0, 0, 0, 0])]
    if len(arr) != 6:
        raise ValueError("joints 必须为 6 个数值")
    return arr


class PointIn(BaseModel):
    name: str = Field(default="未命名点位", max_length=128)
    group: str = Field(default="默认", max_length=64)
    kind: str = Field(default="joint")
    joints: List[float] = Field(default_factory=lambda: [0, 0, 0, 0, 0, 0])
    tcp: Optional[dict] = None          # 直角坐标模式目标 {x,y,z,rx,ry,rz}
    note: str = Field(default="", max_length=512)


class PointUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    group: Optional[str] = Field(default=None, max_length=64)
    kind: Optional[str] = None
    joints: Optional[List[float]] = None
    tcp: Optional[dict] = None
    note: Optional[str] = Field(default=None, max_length=512)


class ImportIn(BaseModel):
    format: Optional[str] = None
    name: str = Field(default="导入点位", max_length=128)
    group: str = Field(default="默认", max_length=64)
    kind: str = Field(default="joint")
    joints: List[float] = Field(default_factory=lambda: [0, 0, 0, 0, 0, 0])
    tcp: Optional[dict] = None
    note: str = Field(default="", max_length=512)


def _summary(row) -> dict:
    try:
        joints = json.loads(row.joints or "[0,0,0,0,0,0]")
    except Exception:
        joints = [0, 0, 0, 0, 0, 0]
    return {
        "id": row.id,
        "name": row.name,
        "group": row.group,
        "kind": row.kind,
        "joints": [round(float(x), 3) for x in joints],
        "tcp": _safe_tcp(row.tcp),
        "note": row.note,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
    }


def _detail(row) -> dict:
    d = _summary(row)
    return d


def _safe_tcp(raw: str):
    try:
        v = json.loads(raw or "null")
        return v
    except Exception:
        return None


def _joints_json(body) -> str:
    return json.dumps(_norm_joints(body.joints), ensure_ascii=False)


def _tcp_json(body) -> str:
    if getattr(body, "tcp", None) is None:
        return "null"
    return json.dumps(body.tcp, ensure_ascii=False)


# ---------- 导入 (放 /{pid} 之前) ----------
# ★ 写操作统一要求控制令牌（阶段 4）：点位决定机器人往哪走，不能让人随手改。
@router.post("/import")
def api_import_point(body: ImportIn, db: Session = Depends(get_db),
                     tok: str = Depends(require_control)):
    row = create_point(db, body.name, body.group, _norm_kind(body.kind),
                       _joints_json(body), _tcp_json(body), body.note)
    emit_event("config", "info", "point.import", f"已导入点位「{row.name}」",
               {"id": row.id, "name": row.name, "group": row.group},
               actor=_actor(tok))
    return _detail(row)


# ---------- 列表 / 创建 ----------
@router.get("")
def api_list_points(group: Optional[str] = None, db: Session = Depends(get_db)):
    return [_summary(r) for r in list_points(db, group)]


@router.post("")
def api_create_point(body: PointIn, db: Session = Depends(get_db),
                     tok: str = Depends(require_control)):
    row = create_point(db, body.name, body.group, _norm_kind(body.kind),
                       _joints_json(body), _tcp_json(body), body.note)
    emit_event("config", "info", "point.create", f"已新建点位「{row.name}」",
               {"id": row.id, "name": row.name, "group": row.group},
               actor=_actor(tok))
    return _detail(row)


# ---------- D2 点位数值体检（★ 必须排在 /{pid} 之前，否则 "audit" 会被当成 pid） ----------
@router.get("/audit")
def api_points_audit(group: Optional[str] = None, db: Session = Depends(get_db)):
    """点位**数值**体检：±327 可表示性 / 软限位裕度 / 粗略奇异指示。

    ★ 与 `GET /api/points` 同口径：只读、不要令牌（体检不新暴露任何数据）。
    ★★ **绝不写库、绝不"顺手把 −360 折成 0"** —— 那是替操作员改数据。
       这里只输出 `advice`（建议重录/调整），改不改由人决定。
    ★ 排序 = 最该重录的在前（bad → warn → unknown → ok，同级按余量小者优先）。
    """
    from app.services.point_audit import audit_rows      # 延迟导入：体检是可选诊断件
    return audit_rows(list_points(db, group))


# ---------- E3 反事实试跑（★ 必须排在 /{pid} 之前，否则 "whatif" 会被当成 pid） ----------
class WhatIfIn(BaseModel):
    """试跑入参：只有**场景描述**，没有任何"动作"字段（本接口不产生动作）。"""
    scenario: Optional[dict] = None          # {tray:{dx,dy,dz}mm, face, order, points, speed_gear_pct}
    n: Optional[int] = Field(default=None, ge=1, le=300)
    seed: int = 1
    live: Optional[List[float]] = None       # 当前实时 6 关节（度），仅用于"从哪一格起步"


@router.post("/whatif")
def api_points_whatif(body: WhatIfIn, db: Session = Depends(get_db),
                      tok: str = Depends(require_control)):
    """**离线**回答「这样改值不值得上真机」：参数扫描 + 蒙特卡洛 → 输出**分布**。

    ★★ 这个接口**不产生任何动作**：不写寄存器、不碰触发位与气路、不下发、不改速度口径，
       连"就绪才放行"那一套门都不需要（它不动机器人，谈何"现在能不能动"）。
       所以要控制令牌**只是**因为它会把点位库和围栏几何读给调用方看（与写操作同一道门）。
    ★ 节拍那一档**只代已发生的实测段**，样本不足就如实回「无实测样本·不给节拍估计」，
      绝不外推 —— 上位机拿不到速度曲线（AGENTS.md §4 关键机理 1），任何 ETA 都是编故事。
    ★ 几何/间隙用既有纯函数（保守包络扫掠 + FK/IK），抖动来源逐项列明并标 `assumed`；
      结论一律是「离线统计估计 · 非认证」。**不引入新依赖、不引入仿真引擎**。
    ★ `n`（样本数）由 `services/whatif.py` 顶部那条离线计时定；超墙钟预算会**少跑并如实改写 n**
      —— 界面显示的样本数 = 真跑出来的那个（§5.5 铁律②）。
    ★ 不写事件流：试跑不改变任何东西，改变真机的动作才配留痕（见 `services/whatif.py`）。
    """
    from app.services import whatif                    # 延迟导入：实验台是可选诊断件
    from app.utils.joints import parse_joints
    poses: dict = {}
    skipped: List[str] = []
    for row in list_points(db):                        # 只读点位库
        name = str(getattr(row, "name", "") or "")
        if not name:
            continue
        try:
            poses[name] = parse_joints(getattr(row, "joints", None), where="点位「%s」" % name)
        except Exception:                              # noqa: BLE001 —— 坏行跳过，别打挂整片评估
            skipped.append(name)
    live = None
    if body.live is not None:
        try:
            live = [float(x) for x in body.live]
            if len(live) != 6:
                live = None
        except (TypeError, ValueError):
            live = None
    res = whatif.run(body.scenario, n=body.n, seed=int(body.seed or 1), poses=poses, live=live)
    if skipped:
        res["failures"] = list(res.get("failures") or []) + [
            "点位库里 %d 个点位关节角读不出（%s%s）→ 未纳入本次评估"
            % (len(skipped), "、".join(skipped[:5]), "…" if len(skipped) > 5 else "")]
    res["actor"] = _actor(tok)
    return res


# ---------- 导出 ----------
@router.get("/{pid}/export")
def api_export_point(pid: int, db: Session = Depends(get_db)):
    row = get_point(db, pid)
    if not row:
        raise HTTPException(404, "点位不存在")
    payload = {
        "format": EXPORT_FORMAT,
        "name": row.name,
        "group": row.group,
        "kind": row.kind,
        "joints": json.loads(row.joints or "[0,0,0,0,0,0]"),
        "tcp": _safe_tcp(row.tcp),
        "note": row.note,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"{(row.name or 'point').strip()}.json"
    return Response(
        content=body.encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@router.get("/{pid}")
def api_get_point(pid: int, db: Session = Depends(get_db)):
    row = get_point(db, pid)
    if not row:
        raise HTTPException(404, "点位不存在")
    return _detail(row)


@router.put("/{pid}")
def api_update_point(pid: int, body: PointUpdate, db: Session = Depends(get_db),
                     tok: str = Depends(require_control)):
    js = None
    if body.joints is not None:
        js = json.dumps(_norm_joints(body.joints), ensure_ascii=False)
    tc = None if body.tcp is None else json.dumps(body.tcp, ensure_ascii=False)
    row = update_point(db, pid, name=body.name, group=body.group, kind=body.kind,
                       joints_json=js, tcp_json=tc, note=body.note)
    if not row:
        raise HTTPException(404, "点位不存在")
    emit_event("config", "info", "point.update", f"已更新点位「{row.name}」",
               {"id": row.id, "name": row.name}, actor=_actor(tok))
    return _detail(row)


@router.delete("/{pid}")
def api_delete_point(pid: int, db: Session = Depends(get_db),
                     tok: str = Depends(require_control)):
    row = get_point(db, pid)
    if not delete_point(db, pid):
        raise HTTPException(404, "点位不存在")
    emit_event("config", "warn", "point.delete",
               f"已删除点位「{(row.name if row else pid)}」",
               {"id": pid, "name": (row.name if row else "")}, actor=_actor(tok))
    return {"ok": True, "id": pid}
