# -*- coding: utf-8 -*-
"""
视觉颜色分拣 API（后端侧：代理相机服务 + 记录落库 + 规则联动）。

分工：
  算法与实时画面在相机服务(:8100)；本模块只负责
    - 把相机服务的状态/配置/标定透出来（代理，前端不必直连两个端口）
    - 把分拣结果落库并提供查询/统计/导出/复核
    - 维护"颜色 → 程序"规则，并在开启时联动执行（多重开关见 services/vision_rules.py）

权限（沿用项目约定）：
  读接口（状态/记录/统计/导出/图片/规则查询）全公开；
  改配置/切背景/一键标定/规则变更 需管理员令牌。

端点一览（前端 apiUrl() 会自动补 /api 前缀）：
  GET    /vision/status                相机服务 + 摄入线程状态
  GET    /vision/last                  最近一次触发（透传相机）
  GET    /vision/records               记录列表（DB，支持颜色/时间过滤 + 分页）
  GET    /vision/stats                 按颜色统计
  GET    /vision/export                导出 json / csv
  GET    /vision/image?p=              取存档图（代理相机服务，带目录穿越防护）
  GET    /vision/rules                 规则表
  GET    /vision/programs              可选程序清单
  GET    /vision/card                  色卡 + 当前标定概览
  POST   /vision/config                更新相机侧配置           [admin]
  POST   /vision/background            固化/清除静态背景        [admin]
  POST   /vision/calibrate/gray        灰卡白平衡               [admin]
  POST   /vision/calibrate/sample      实物采样标定             [admin]
  PUT    /vision/rules                 保存规则表               [admin]
  POST   /vision/records/{rid}/review  人工复核（改判 / 并入色卡）[admin]
  DELETE /vision/records               清理记录（?days=N）      [admin]

  ★ E4 判色样本环（详见文件底部与 docs/方案-创新功能四批…§E4）：
  GET    /vision/samples               样本列表 + 逐色样本数（只读，无需令牌）
  POST   /vision/samples/{sid}/review  人工标签回流：对/错 + 真色       [control]
  GET    /vision/samples/advice        按色统计 → 建议 diff（只读）
  POST   /vision/samples/advice/propose        存为待确认提案（**不下发**）[control]
  POST   /vision/samples/advice/rollback       回到上一版提案（**只回账面**）[control]
  GET    /vision/samples/proposals     提案历史（只读）
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import _client_ip, require_admin, require_control, token_role
from app.core.deps import get_db
from app.db import crud
from app.services import camera_client
from app.services import color_advice as CA
from app.services import vision_samples as VS
from app.services.events import emit as emit_event
from app.services.events import since_dt
from app.services.vision_ingest import ingest
from app.services.vision_rules import load_rules, programs_brief, save_rules

router = APIRouter(prefix="/api/vision", tags=["vision"])

_EXPORT_FORMAT = "efort-vision/v1"
COLORS_HINT = ("红 橙 黄 草绿 深绿 青 天蓝 蓝 深蓝 紫 粉 棕 金 米白 白 银灰 灰 黑")


def _iso(dt) -> str:
    try:
        if dt is None:
            return ""
        return dt.isoformat() + "Z" if dt.tzinfo is None else dt.isoformat()
    except Exception:
        return ""


def _row(r) -> dict:
    try:
        images = json.loads(r.images or "{}")
    except Exception:
        images = {}
    return {
        "id": r.id, "seq": r.seq, "timestamp": _iso(r.timestamp),
        "color": r.color, "hex": r.hex, "conf": r.conf, "de": r.de,
        "lab": r.lab, "ratio": r.ratio, "alt": r.alt, "center": r.center,
        "area": r.area, "reason": r.reason, "images": images,
        "program_id": r.program_id, "program_name": r.program_name,
        "outcome": r.outcome, "note": r.note, "source": r.source,
    }


# ============================ 状态 / 代理 ============================
@router.get("/status")
def api_status():
    """相机服务状态 + 摄入状态（前端一个接口拿全）。"""
    st, cam, err = camera_client.get_json("/vision/status", timeout=5.0)
    return {
        "camera": cam if (st == 200 and not err) else None,
        "camera_ok": st == 200 and not err,
        "camera_error": err or "",
        "base_url": camera_client.base_url(),
        "ingest": ingest.status(),
    }


@router.get("/last")
def api_last(since: int = Query(0, ge=0)):
    st, j, err = camera_client.get_json("/vision/last?since=%d" % since, timeout=2.0)
    if st != 200 or err:
        return {"ok": False, "error": err or "相机服务不可用", "event": None, "seq": 0}
    return {"ok": True, "event": (j or {}).get("event"), "seq": (j or {}).get("seq", 0)}


@router.get("/image")
def api_image(p: str = Query(..., description="相对 VISION_DIR 的路径（来自记录的 images 字段）")):
    """代理相机服务的存档图。★ 目录穿越由相机服务侧统一拦截，这里只做透传。"""
    from urllib.parse import quote
    st, data, err = camera_client.get_binary("/vision/image?p=" + quote(p, safe="/"))
    if st != 200 or not isinstance(data, (bytes, bytearray)):
        raise HTTPException(404, detail=err or "图片不存在")
    ctype = "image/png" if p.lower().endswith(".png") else "image/jpeg"
    return StreamingResponse(iter([bytes(data)]), media_type=ctype)


@router.get("/card")
def api_card():
    """色卡 + 当前标定（前端标定面板用）。"""
    st, cam, err = camera_client.get_json("/vision/status", timeout=5.0)
    if st != 200 or err or not cam:
        return {"ok": False, "error": err or "相机服务不可用", "card": {}, "calib": {}}
    return {"ok": True, "card": cam.get("card") or {},
            "achromatic": cam.get("achromatic") or [],
            "calib": cam.get("calib") or {},
            "hint": COLORS_HINT}


# ============================ 记录 / 统计 / 导出 ============================
@router.get("/records")
def api_records(
    color: Optional[str] = Query(None),
    hours: Optional[float] = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    since = since_dt(hours)
    rows = crud.list_vision_records(db, color=color, since=since, limit=limit, offset=offset)
    return {
        "items": [_row(r) for r in rows],
        "total": crud.count_vision_records(db, color=color, since=since),
        "stats": crud.vision_stats(db, since),
        "limit": limit, "offset": offset,
    }


@router.get("/stats")
def api_stats(hours: Optional[float] = Query(None, ge=0), db: Session = Depends(get_db)):
    return crud.vision_stats(db, since_dt(hours))


@router.get("/export")
def api_export(
    format: str = Query("csv", description="json | csv"),
    color: Optional[str] = None,
    hours: Optional[float] = None,
    limit: int = Query(5000, ge=1, le=50000),
    db: Session = Depends(get_db),
):
    rows = crud.list_vision_records(db, color=color, since=since_dt(hours),
                                    limit=limit, offset=0)
    items = [_row(r) for r in rows]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "time", "color", "hex", "conf", "de", "lab", "ratio",
                    "alt", "center", "area", "reason", "outcome",
                    "program_name", "note", "full_image"])
        for it in items:
            w.writerow([it["id"], it["timestamp"], it["color"], it["hex"],
                        it["conf"], it["de"], it["lab"], it["ratio"], it["alt"],
                        it["center"], it["area"], it["reason"], it["outcome"],
                        it["program_name"], it["note"],
                        (it["images"] or {}).get("full", "")])
        payload = buf.getvalue().encode("utf-8-sig")     # Excel 直接打开不乱码
        return StreamingResponse(
            iter([payload]), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     "attachment; filename=efort_vision_%s.csv" % stamp})

    body = {
        "format": _EXPORT_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
        "filters": {"color": color, "hours": hours},
        "count": len(items),
        "stats": crud.vision_stats(db, since_dt(hours)),
        "items": items,
    }
    return StreamingResponse(
        iter([json.dumps(body, ensure_ascii=False, indent=2)]),
        media_type="application/json",
        headers={"Content-Disposition":
                 "attachment; filename=efort_vision_%s.json" % stamp})


@router.delete("/records")
def api_clear_records(
    days: Optional[float] = Query(None, ge=0, description="只清理 N 天前；不传为全清"),
    tok: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    n = crud.prune_vision_records(db, int(days or 0))
    emit_event("vision", "warn" if not days else "info", "vision.records_clear",
               "视觉记录清理：删除 %d 条（days=%s）" % (n, days or 0),
               {"deleted": n, "days": days or 0}, actor=token_role(tok) or "admin")
    return {"ok": True, "deleted": n}


class ReviewIn(BaseModel):
    color: Optional[str] = Field(None, description="人工改判后的颜色；不传则只标记已复核")
    teach: bool = Field(False, description="是否把该样本的实测 Lab 并入色卡（自学习）")
    note: Optional[str] = None


@router.post("/records/{rid}/review")
def api_review(rid: int, body: ReviewIn, tok: str = Depends(require_admin),
               db: Session = Depends(get_db)):
    """人工复核：低置信/未识别样本点一下就归好类，并可自动并入色卡。

    ★ 这是"越用越准"的关键：现场实物标定比任何标准色卡都贴合实际。
    """
    row = crud.get_vision_record(db, rid)
    if row is None:
        raise HTTPException(404, detail="记录不存在")
    fields = {"outcome": "reviewed", "source": "review"}
    if body.color:
        fields["color"] = str(body.color)[:16]
    if body.note is not None:
        fields["note"] = str(body.note)[:255]
    row = crud.update_vision_record(db, rid, **fields)

    taught = None
    if body.teach:
        lab = None
        try:
            lab = [float(x) for x in (row.lab or "").split(",")]
        except Exception:
            lab = None
        if lab and len(lab) == 3:
            st, j, err = camera_client.post_json(
                "/vision/calibrate/sample",
                {"name": body.color or row.color, "lab": lab}, timeout=3.0)
            taught = bool(st == 200 and not err and (j or {}).get("ok"))
        else:
            taught = False

    emit_event("vision", "info", "vision.review",
               "视觉记录复核：#%s → %s" % (rid, fields.get("color") or row.color),
               {"id": rid, "color": fields.get("color") or row.color,
                "teach": bool(body.teach), "taught": taught},
               actor=token_role(tok) or "admin")
    out = _row(row)
    out["taught"] = taught
    return out


# ============================ 配置 / 标定（代理）============================
class VisionConfigIn(BaseModel):
    enabled: Optional[bool] = None
    roi: Optional[list] = None
    use_mog2: Optional[bool] = None
    min_interval: Optional[float] = None
    save_on_detect: Optional[bool] = None
    reset_background: Optional[bool] = None
    # ★ 审计修复 P0-cam-3: 三色置信度阈值 + 检测阈值表(部分覆盖)，
    #   以前后端这条路是断的 —— 只能在相机服务上手改/手发，现场没法从界面调。
    #   透传给相机服务，未知键由相机侧 norm_thr() 丢弃并限幅，不会因为脏值停摆。
    min_conf: Optional[float] = Field(None, ge=0.0, le=1.0,
                                      description="三色置信度阈值(0~1)")
    thr: Optional[dict] = Field(None, description="检测阈值部分覆盖，如 {min_area_ratio:0.003}")


@router.post("/config")
def api_set_config(body: VisionConfigIn, tok: str = Depends(require_admin)):
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    st, j, err = camera_client.post_json("/vision/config", payload, timeout=3.0)
    if st != 200 or err:
        raise HTTPException(502, detail=err or "相机服务不可用")
    emit_event("config", "info", "vision.config",
               "视觉分拣配置更新：%s" % (", ".join("%s=%s" % kv for kv in payload.items()) or "无"),
               {"changed": payload}, actor=token_role(tok) or "admin")
    return {"ok": True, "camera": (j or {}).get("status")}


class BackgroundIn(BaseModel):
    reset: bool = False


@router.post("/background")
def api_background(body: BackgroundIn, tok: str = Depends(require_admin)):
    st, j, err = camera_client.post_json("/vision/background", {"reset": body.reset},
                                         timeout=3.0)
    if st != 200 or err:
        raise HTTPException(502, detail=err or "相机服务不可用")
    emit_event("config", "info", "vision.background",
               "静态背景%s" % ("已清除" if body.reset else "已固化"),
               actor=token_role(tok) or "admin")
    return {"ok": True, "message": (j or {}).get("message", ""),
            "camera": (j or {}).get("status")}


class GrayIn(BaseModel):
    target: float = Field(118.0, ge=20.0, le=240.0)


@router.post("/calibrate/gray")
def api_calib_gray(body: GrayIn, tok: str = Depends(require_admin)):
    st, j, err = camera_client.post_json("/vision/calibrate/gray",
                                         {"target": body.target}, timeout=4.0)
    if st != 200 or err or not (j or {}).get("ok"):
        raise HTTPException(400, detail=(j or {}).get("message") or err or "标定失败")
    emit_event("config", "info", "vision.calib_gray",
               "视觉灰卡白平衡标定完成", {"gains": (j or {}).get("info")},
               actor=token_role(tok) or "admin")
    return {"ok": True, "info": (j or {}).get("info")}


class SampleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=16)
    lab: Optional[list] = None


@router.post("/calibrate/sample")
def api_calib_sample(body: SampleIn, tok: str = Depends(require_admin)):
    st, j, err = camera_client.post_json("/vision/calibrate/sample",
                                         {"name": body.name, "lab": body.lab}, timeout=3.0)
    if st != 200 or err or not (j or {}).get("ok"):
        raise HTTPException(400, detail=(j or {}).get("message") or err or "标定失败")
    emit_event("config", "info", "vision.calib_sample",
               "视觉实物标定：%s" % body.name, {"info": (j or {}).get("info")},
               actor=token_role(tok) or "admin")
    return {"ok": True, "info": (j or {}).get("info")}


# ============================ 规则 ============================
@router.get("/rules")
def api_get_rules():
    return {"rules": load_rules().get("rules") or [],
            "auto_execute": ingest.status().get("auto_execute", False),
            "programs": programs_brief(),
            "hint": "颜色→程序；只有 全局开关(config.vision.auto_execute) 与 规则勾选 同时为真才会真正下发。"}


class RulesIn(BaseModel):
    rules: list = Field(default_factory=list)


@router.put("/rules")
def api_put_rules(body: RulesIn, tok: str = Depends(require_admin)):
    norm = save_rules({"rules": body.rules})
    emit_event("config", "warn" if len(norm["rules"]) != len(body.rules) else "info",
               "vision.rules_save",
               "视觉分拣规则已保存（%d 条）" % len(norm["rules"]),
               {"rules": norm["rules"]}, actor=token_role(tok) or "admin")
    return {"ok": True, "rules": norm["rules"]}


# =====================================================================
# ★ E4「现场自学习判色闭环」：样本环 + 建议 + **提案（绝不下发）**
#   方案 docs/方案-创新功能四批…§E4；唯一实现 = services/vision_samples.py（写）
#   + services/color_advice.py（算）。
#
# 权限口径（沿用本项目先例）：
#   · 读端点（样本 / 建议）= **观测**，不需要控制令牌（同 /api/twin/residual、
#     /api/sorting/cells）—— 现场要看"凭什么这么判"不该先要口令；
#   · 写端点（复核 / 提案 / 回滚）= `require_control`。复核是"免费标注"回流，
#     操作员就该能点（比 `api_review` 的 require_admin 低一级是有意的：
#     它只改样本的标签三列，不动判色结论、不动任何配置、不动机器人）。
#
# ★★★ 这三条边界的**存在理由**（回归钉 tests/test_color_advice.py 逐条对上）：
#   1. `propose` **不会**调相机服务的 `/vision/config` —— 一次都不会。建议落库只是
#      一张账面提案，生效必须人自己到相机阈值面板显式点（既有下发通道）。
#   2. 响应里**明确写出**"改阈值前必须先跑现场真图回放 `camera/tools/vision_e2e_replay.py`"
#      —— §5.4 的口径"阈值只能按现场实测值定，不能按合成图定"的产品化表达。
#   3. 硬安全相关阈值（`dark_chroma_max` / `dark_s_max` 一族黑暗判据）**不进建议面**：
#      它们决定"黑 = 无效色 → 放回"，属于安全口径，只能人显式改并留档。
# =====================================================================

_SAMPLE_ORDER = ("correct", "wrong")


def _sample_row(r) -> dict:
    """DB 行 → 界面/统计用的 dict（★ 与 `_row` 同风格：JSON 列解析坏了就给空值，不抛）。"""
    try:
        frames = json.loads(r.frames or "[]")
    except Exception:
        frames = []
    try:
        votes = json.loads(r.votes or "{}")
    except Exception:
        votes = {}
    try:
        thr = json.loads(r.thr_snapshot or "{}")
    except Exception:
        thr = {}
    return {
        "id": r.id, "timestamp": _iso(r.timestamp), "cell": r.cell,
        "color": r.color, "hex": r.hex, "conf": r.conf,
        "valid_colors": [c for c in (r.valid_colors or "").split(",") if c],
        "vote_ok": bool(r.vote_ok), "placeholder": bool(r.placeholder),
        "votes": votes, "reason": r.reason,
        "frames": frames if isinstance(frames, list) else [],
        "frame_count": r.frame_count, "failed_frames": r.failed_frames,
        "outcome": r.outcome, "true_color": r.true_color, "source": r.source,
        "stage": r.stage, "note": r.note, "reviewed_at": _iso(r.reviewed_at),
        "thr_snapshot": thr if isinstance(thr, dict) else {},
    }


@router.get("/samples")
def api_samples(
    color: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    hours: Optional[float] = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """样本环列表（**只读观测**，无需令牌）。

    ★ 顺带做一件只有读端点该做的事：把相机服务回显的**现场生效阈值**刷进进程快照缓存，
      这样后续 ingest 与 `advice` 都拿得到"当时到底用的什么值"。
      这一步**不在这条链路上等相机**：`refresh_from_camera` 失败就继续标 unknown（三态不塌），
      超时给到 0.6s —— 界面读样本不该被相机没开拖住。
    """
    since = since_dt(hours)
    rows = crud.list_vision_samples(db, color=color, outcome=outcome, source=source,
                                    since=since, limit=limit, offset=offset)
    try:
        snap = VS.refresh_from_camera(timeout=0.6)
    except Exception:                            # noqa: BLE001 —— 观测件不许把接口带崩
        snap = VS.snapshot()
    by_color = crud.sample_color_counts(db, since)
    return {
        "items": [_sample_row(r) for r in rows],
        "total": crud.count_vision_samples(db, since),
        "by_color": by_color,
        # ★ 七色全列出：没有样本的色补 0（"0 条"与"这个色不在统计范围内"是两件事，
        #   后者由 calib_state=unknown 表达，别在这一层就混掉）
        "colors": {c: int(by_color.get(c) or 0) for c in CA.SEVEN_COLORS},
        "outcomes": list(VS.OUTCOMES),
        "sources": list(VS.SOURCES),
        "snapshot": snap,
        "limit": limit, "offset": offset,
    }


def _proposal_row(p) -> Optional[dict]:
    """提案行 → dict（`None` = 确实没有这一版；★ 不许返回 {} 让界面读成"有但为空"）。"""
    if p is None:
        return None
    try:
        diff = json.loads(p.diff or "[]")
    except Exception:
        diff = []
    try:
        thr = json.loads(p.thr_current or "{}")
    except Exception:
        thr = {}
    return {
        "id": p.id, "created_at": _iso(p.created_at), "actor": p.actor,
        "status": p.status, "summary": p.summary, "note": p.note,
        "diff": diff if isinstance(diff, list) else [],
        "thr_current": thr if isinstance(thr, dict) else {},
        # ★ 提案永远标着"未下发"：这张表里根本没有"已生效"这个状态（见 models.py）
        "applied": False,
    }


class SampleReviewIn(BaseModel):
    judgement: str = Field(..., description="correct=判对 / wrong=改判（真色必填）")
    true_color: Optional[str] = Field(None, description="人工认定的真色")
    note: Optional[str] = None


@router.post("/samples/{sid}/review")
def api_sample_review(sid: int, body: SampleReviewIn,
                      tok: str = Depends(require_control),
                      db: Session = Depends(get_db)):
    """★ 人工标签回流（方案说的"免费标注"）：对 / 错 + 真色。

    只改标签三列（outcome / true_color / reviewed_at），**不动**逐帧特征与当时的结论 ——
    样本环的价值就是"原始观测 + 事后标签"两份都在；涂掉结论等于把证据删了。
    """
    j = str(body.judgement or "").lower()
    if j not in _SAMPLE_ORDER:
        raise HTTPException(400, detail="judgement 只能是 correct / wrong")
    tc = str(body.true_color or "").strip()[:16]
    if j == "wrong" and not tc:
        # 说"判错了"却不给真色 = 这条样本仍然无法参与误判统计 → 如实拒绝，不猜
        raise HTTPException(400, detail="改判必须给出真色（否则这条样本无从统计）")
    row = crud.review_vision_sample(db, sid, j, true_color=tc, note=body.note)
    if row is None:
        raise HTTPException(404, detail="样本不存在")
    emit_event("vision", "info", "vision.sample_review",
               "判色样本复核：#%s → %s（%s）" % (sid, row.outcome, row.true_color or row.color),
               {"id": sid, "judgement": j, "true_color": row.true_color,
                "outcome": row.outcome},
               actor=token_role(tok) or "operator")
    return _sample_row(row)


@router.get("/samples/advice")
def api_advice(
    min_samples: int = Query(CA.MIN_SAMPLES, ge=1, le=1000),
    min_reviewed: int = Query(CA.MIN_REVIEWED, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """按色统计样本 → **建议 diff**（纯读，无需令牌，不产生任何动作）。"""
    try:
        snap = VS.refresh_from_camera(timeout=0.6)
    except Exception:                            # noqa: BLE001
        snap = VS.snapshot()
    rows = [_sample_row(r) for r in crud.samples_for_advice(db)]
    out = CA.advice(rows, snap=snap, min_samples=min_samples, min_reviewed=min_reviewed)
    out["proposal"] = _proposal_row(crud.get_active_proposal(db))
    out["history"] = [_proposal_row(p) for p in crud.list_advice_proposals(db, limit=10)]
    return out


class AdviceProposeIn(BaseModel):
    min_samples: int = Field(CA.MIN_SAMPLES, ge=1, le=1000)
    min_reviewed: int = Field(CA.MIN_REVIEWED, ge=1, le=1000)
    note: Optional[str] = None


@router.post("/samples/advice/propose")
def api_advice_propose(body: AdviceProposeIn, request: Request,
                       tok: str = Depends(require_control),
                       db: Session = Depends(get_db)):
    """把当前建议存成**待确认提案**（+ 审计事件）。★★ 本端点不调任何下发通道。

    ★ 为什么"propose"还算写操作要令牌：它进的是审计链（人签了字的一张纸），
      不是浏览；但它**同样**不碰相机 —— 生效只在既有通道 `POST /vision/config`，
      由人核对现场真图回放结果后自己点。
    """
    rows = [_sample_row(r) for r in crud.samples_for_advice(db)]
    out = CA.advice(rows, min_samples=body.min_samples, min_reviewed=body.min_reviewed)
    diff = CA.diff_rows(out)
    if not diff:
        # 没有可提案的建议就**不落库**（免得攒一张"空提案"，现场以为改了什么）
        raise HTTPException(409, detail="没有可提案的建议：%s" % ("；".join(
            out.get("no_advice_reasons") or ["样本不足或证据不指向任何阈值"])))
    prev = crud.get_active_proposal(db)
    row = crud.insert_advice_proposal(
        db, diff_json=json.dumps(diff, ensure_ascii=False),
        summary=CA.summarize(out),
        thr_current_json=json.dumps(out.get("threshold_snapshot") or {}, ensure_ascii=False),
        actor=token_role(tok) or "operator", note=(body.note or "")[:255])
    emit_event("config", "info", "vision.advice_propose",
               "判色阈值建议提案（%d 项，**未下发**）：%s" % (len(diff), CA.summarize(out)),
               {"proposal_id": row.id, "diff": diff, "applied": False,
                "superseded_id": (prev.id if prev else None),
                "must_run_first": CA.E2E_REPLAY},
               actor=token_role(tok) or "operator", ip=_client_ip(request))
    return {
        "ok": True,
        "applied": False,                       # ★ 永远是 False：提案 ≠ 生效
        "proposal": _proposal_row(row),
        "superseded_id": (prev.id if prev else None),
        "requires": dict(CA.REQUIRES),
        "message": ("已存为待确认提案（未下发）。生效前必须：① 在**现场**跑 %s（现场真图回放，"
                    "确认三色检出率与误判率都不掉）→ ② 由人到相机阈值面板走既有通道 "
                    "%s 显式点一次。本接口没有、也不会替你做这两步。"
                    % (CA.E2E_REPLAY, CA.APPLY_CHANNEL)),
    }


@router.post("/samples/advice/rollback")
def api_advice_rollback(request: Request, tok: str = Depends(require_control),
                        db: Session = Depends(get_db)):
    """回到上一版提案（**只回账面**）。

    ★★ 必须写清这一点，否则就是假显示：本端点回滚的是"哪一份提案在案"，
      它**不会**把现场阈值改回去（现场生效值只有人自己到相机阈值面板改）。
      所以回滚后界面仍要显示"现场生效值未知/未变"，不许写成"已回滚到旧阈值"。
    """
    active = crud.get_active_proposal(db)
    if active is None:
        raise HTTPException(404, detail="当前没有待确认提案，无需回滚")
    prev = crud.get_latest_superseded(db)
    crud.mark_proposal(db, active.id, "rolled_back")
    restored = None
    if prev is not None:
        restored = crud.mark_proposal(db, prev.id, "active")
    emit_event("config", "warn" if restored else "info", "vision.advice_rollback",
               "判色阈值提案回滚：%s（**只回账面提案，现场生效阈值未变**）"
               % ("回到 #%s" % restored.id if restored else "没有上一版提案 → 建议面回到空"),
               {"rolled_back_id": active.id, "restored_id": (restored.id if restored else None),
                "applied": False},
               actor=token_role(tok) or "operator", ip=_client_ip(request))
    return {
        "ok": True,
        "applied": False,
        "rolled_back_id": active.id,
        "proposal": _proposal_row(restored),
        "restored": restored is not None,
        "message": ("已把提案账目退回上一版%s。★ 现场生效阈值**没有**被改动 —— "
                    "要真的改回去，仍需人自己到相机阈值面板按 e2e 回放结果操作。"
                    % ("（#%s）" % restored.id if restored else "（没有上一版 → 当前无提案）")),
    }


@router.get("/samples/proposals")
def api_proposals(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    """提案历史（只读）：当前 active + 可回滚到的 superseded 列表。"""
    return {"items": [_proposal_row(p) for p in crud.list_advice_proposals(db, limit=limit)],
            "active_id": (crud.get_active_proposal(db).id
                          if crud.get_active_proposal(db) else None)}
