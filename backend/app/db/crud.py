# -*- coding: utf-8 -*-
"""数据库读写辅助。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, true

from app.db.models import Event, PoseHistory, SafetyEvent, Session

# ★ 审计修复 P0-4：清理必须**分批 + 小事务**。
#   原实现把整表 ORM 对象全部读进内存再逐行 session.delete() + 单次 commit：
#   - 内存：180 天欠账 ≈ 1550 万行 × ORM 实例 ≈ 数 GB → 启动即 OOM；
#   - 锁：单个巨型事务在整个清理期间独占写锁 → 采集线程 insert_pose 静默失败丢数据。
PRUNE_BATCH = 5000          # 每批删除行数
PRUNE_MAX_BATCHES = 20000   # 单次调用最多批次（1 亿行上限，防死循环）


def prune_batched(session, model, where_cond, batch: int = PRUNE_BATCH,
                  max_batches: int = PRUNE_MAX_BATCHES) -> int:
    """按条件分批删除，返回总删除行数。

    用 `DELETE ... WHERE id IN (SELECT id ... LIMIT n)` 子查询，行 ID 只在
    子查询里物化（不加载 ORM 实例），每批一个短事务，内存与锁窗口都受控。
    """
    total = 0
    for _ in range(max_batches):
        try:
            subq = select(model.id).where(where_cond).limit(batch)
            res = session.execute(delete(model).where(model.id.in_(subq)))
            session.commit()
        except Exception:
            session.rollback()          # 半批失败必须回滚，避免悬空事务
            raise
        n = int(getattr(res, "rowcount", 0) or 0)
        total += n
        if n < batch:
            break
    return total


def insert_pose(session, j1, j2, j3, j4, j5, j6, tcp, source="real") -> PoseHistory:
    row = PoseHistory(
        timestamp=datetime.now(timezone.utc),
        j1=j1, j2=j2, j3=j3, j4=j4, j5=j5, j6=j6,
        tcp_x=tcp[0], tcp_y=tcp[1], tcp_z=tcp[2],
        source=source,
    )
    session.add(row)
    session.commit()
    return row


def insert_event(session, level: str, message: str) -> Event:
    row = Event(timestamp=datetime.now(timezone.utc), level=level, message=message)
    session.add(row)
    session.commit()
    return row


def get_recent_poses(session, limit: int = 500):
    stmt = (
        select(PoseHistory)
        .order_by(PoseHistory.id.desc())
        .limit(limit)
    )
    rows = session.execute(stmt).scalars().all()
    return list(reversed(rows))


def get_latest_pose(session) -> PoseHistory | None:
    stmt = select(PoseHistory).order_by(PoseHistory.id.desc()).limit(1)
    return session.execute(stmt).scalars().first()


def start_session(session, label: str = "") -> Session:
    row = Session(started_at=datetime.now(timezone.utc), label=label)
    session.add(row)
    session.commit()
    return row


def end_session(session, sid: int) -> None:
    row = session.get(Session, sid)
    if row:
        row.ended_at = datetime.now(timezone.utc)
        session.commit()


def prune_old_poses(session, retention_days: int, batch: int = PRUNE_BATCH) -> int:
    """删除超过保留天数的历史行, 返回删除条数。

    ★ 审计修复 P0-4：改为分批 DELETE（原实现全表 ORM 加载 → 启动 OOM +
      单事务长期持写锁 → 采集写入被静默顶掉）。
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return prune_batched(session, PoseHistory, PoseHistory.timestamp < cutoff,
                         batch=batch)


# ---------------- 录制 CRUD ----------------
from app.db.models import Recording  # noqa: E402


def list_recordings(session):
    """录制列表(按 updated_at 倒序)。"""
    stmt = select(Recording).order_by(Recording.updated_at.desc())
    return list(session.execute(stmt).scalars().all())


def get_recording(session, rid: int) -> Recording | None:
    return session.get(Recording, rid)


def create_recording(session, name: str, frames_json: str,
                     duration_ms: int, description: str = "",
                     source: str = "sim") -> Recording:
    now = datetime.now(timezone.utc)
    row = Recording(name=name, description=description, source=source,
                    duration_ms=duration_ms,
                    frames=frames_json, created_at=now, updated_at=now)
    session.add(row)
    session.commit()
    return row


def update_recording(session, rid: int, name=None, description=None,
                     frames_json=None, duration_ms=None) -> Recording | None:
    row = session.get(Recording, rid)
    if not row:
        return None
    if name is not None:
        row.name = name
    if description is not None:
        row.description = description
    if frames_json is not None:
        row.frames = frames_json
    if duration_ms is not None:
        row.duration_ms = duration_ms
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return row


def delete_recording(session, rid: int) -> bool:
    row = session.get(Recording, rid)
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


# =====================================================================
# 预设点位 (Point) CRUD
# =====================================================================
from app.db.models import Point, Program  # noqa: E402


def list_points(session, group: str | None = None):
    stmt = select(Point)
    if group:
        stmt = stmt.where(Point.group == group)
    stmt = stmt.order_by(Point.updated_at.desc())
    return list(session.execute(stmt).scalars().all())


def get_point(session, pid: int) -> Point | None:
    return session.get(Point, pid)


def create_point(session, name: str, group: str = "默认", kind: str = "joint",
                 joints_json: str = "[0,0,0,0,0,0]", tcp_json: str = "null",
                 note: str = "") -> Point:
    now = datetime.now(timezone.utc)
    row = Point(name=name, group=group, kind=kind, joints=joints_json,
                tcp=tcp_json, note=note, created_at=now, updated_at=now)
    session.add(row)
    session.commit()
    return row


def update_point(session, pid: int, name=None, group=None, kind=None,
                 joints_json=None, tcp_json=None, note=None) -> Point | None:
    row = session.get(Point, pid)
    if not row:
        return None
    if name is not None:
        row.name = name
    if group is not None:
        row.group = group
    if kind is not None:
        row.kind = kind
    if joints_json is not None:
        row.joints = joints_json
    if tcp_json is not None:
        row.tcp = tcp_json
    if note is not None:
        row.note = note
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return row


def delete_point(session, pid: int) -> bool:
    row = session.get(Point, pid)
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


# =====================================================================
# 执行程序 (Program) CRUD
# =====================================================================
def list_programs(session):
    stmt = select(Program).order_by(Program.updated_at.desc())
    return list(session.execute(stmt).scalars().all())


def get_program(session, prid: int) -> Program | None:
    return session.get(Program, prid)


def create_program(session, name: str, items_json: str = "[]", note: str = "") -> Program:
    now = datetime.now(timezone.utc)
    row = Program(name=name, note=note, items=items_json, created_at=now, updated_at=now)
    session.add(row)
    session.commit()
    return row


def update_program(session, prid: int, name=None, note=None, items_json=None) -> Program | None:
    row = session.get(Program, prid)
    if not row:
        return None
    if name is not None:
        row.name = name
    if note is not None:
        row.note = note
    if items_json is not None:
        row.items = items_json
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return row


def delete_program(session, prid: int) -> bool:
    row = session.get(Program, prid)
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


# =====================================================================
# 安全围栏报警事件 (时间线)
# =====================================================================
def insert_safety_event(session, zone_id: str, zone_name: str, state: str,
                        clearance: float, ratio: float) -> SafetyEvent:
    row = SafetyEvent(
        timestamp=datetime.now(timezone.utc),
        zone_id=str(zone_id or ""),
        zone_name=str(zone_name or ""),
        state=str(state or ""),
        clearance=float(clearance or 0.0),
        ratio=float(ratio or 0.0),
    )
    session.add(row)
    session.commit()
    return row


def list_safety_events(session, limit: int = 100):
    stmt = select(SafetyEvent).order_by(SafetyEvent.id.desc()).limit(limit)
    return list(session.execute(stmt).scalars())


def clear_safety_events(session) -> int:
    n = session.query(SafetyEvent).delete()
    session.commit()
    return int(n)


# =====================================================================
# 统一事件总线 / 审计日志 (system_events)
# =====================================================================
from app.db.models import SystemEvent, SafetyConfigVersion  # noqa: E402


def insert_system_event(session, category: str, level: str, action: str,
                        message: str = "", detail: str = "",
                        actor: str = "system", ip: str = "") -> SystemEvent:
    row = SystemEvent(
        timestamp=datetime.now(timezone.utc),
        category=str(category or "system")[:16],
        level=str(level or "info")[:8],
        action=str(action or "")[:64],
        actor=str(actor or "system")[:16],
        ip=str(ip or "")[:64],
        message=str(message or "")[:512],
        detail=detail or "",
    )
    session.add(row)
    session.commit()
    return row


def list_system_events(session, category: str | None = None, level: str | None = None,
                       actor: str | None = None, action: str | None = None,
                       q: str | None = None, since=None, limit: int = 200,
                       offset: int = 0):
    """事件列表（默认最新在前）。所有过滤条件都是可选的与关系。"""
    stmt = select(SystemEvent)
    if category:
        stmt = stmt.where(SystemEvent.category == category)
    if level:
        # level 按"最低级别"过滤（如 level=warn 表示 warn 及以上）
        lvls = _at_or_above(level)
        stmt = stmt.where(SystemEvent.level.in_(lvls))
    if actor:
        stmt = stmt.where(SystemEvent.actor == actor)
    if action:
        stmt = stmt.where(SystemEvent.action == action)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (SystemEvent.message.like(like)) | (SystemEvent.action.like(like))
        )
    if since is not None:
        stmt = stmt.where(SystemEvent.timestamp >= since)
    stmt = stmt.order_by(SystemEvent.id.desc()).limit(limit).offset(offset)
    return list(session.execute(stmt).scalars())


_LEVELS = ("debug", "info", "warn", "error", "critical")


def _at_or_above(level: str):
    """返回 >= level 的所有级别（用于"最低级别"过滤）。"""
    lv = str(level or "").lower()
    if lv not in _LEVELS:
        return [lv] if lv else []
    return list(_LEVELS[_LEVELS.index(lv):])


def count_system_events(session, since=None) -> int:
    # ★ 审计修复 P1-C1：计数用 SQL COUNT，不再全表 ORM 加载
    #   （原实现 50 万行会把 50 万行 ORM 对象拉进内存只为数个数）。
    from sqlalchemy import func
    stmt = select(func.count(SystemEvent.id))
    if since is not None:
        stmt = stmt.where(SystemEvent.timestamp >= since)
    return int(session.execute(stmt).scalar() or 0)


def system_event_stats(session, since=None) -> dict:
    """按类别/级别统计条数（时间线页头部概览）。

    ★ 审计修复 P1-C1：改用 group_by 聚合，不再 select(SystemEvent) 整表加载。
    """
    from sqlalchemy import func
    base = select(SystemEvent.category, func.count(SystemEvent.id)).group_by(SystemEvent.category)
    by_lv_q = select(SystemEvent.level, func.count(SystemEvent.id)).group_by(SystemEvent.level)
    if since is not None:
        base = base.where(SystemEvent.timestamp >= since)
        by_lv_q = by_lv_q.where(SystemEvent.timestamp >= since)
    by_cat = {str(k): int(v) for k, v in session.execute(base).all()}
    by_lv = {str(k): int(v) for k, v in session.execute(by_lv_q).all()}
    return {"total": sum(by_cat.values()), "by_category": by_cat, "by_level": by_lv}


def prune_system_events(session, days: int) -> int:
    """删除 N 天前的事件，返回删除条数。days<=0 表示清空。

    ★ 审计修复 P0-4/P1-C6：分批删除；清空分支用恒真条件同样走分批。
    """
    if days and days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cond = SystemEvent.timestamp < cutoff
    else:
        cond = true()
    return prune_batched(session, SystemEvent, cond)


# =====================================================================
# 安全围栏配置版本 (safety_config_versions)
# =====================================================================
def insert_safety_version(session, config_json: str, actor: str = "system",
                           source: str = "save", note: str = "") -> SafetyConfigVersion:
    row = SafetyConfigVersion(
        created_at=datetime.now(timezone.utc),
        actor=str(actor or "system")[:16],
        source=str(source or "save")[:16],
        note=str(note or "")[:255],
        config=config_json,
    )
    session.add(row)
    session.commit()
    return row


def list_safety_versions(session, limit: int = 50):
    stmt = (select(SafetyConfigVersion)
            .order_by(SafetyConfigVersion.id.desc())
            .limit(limit))
    return list(session.execute(stmt).scalars())


def get_safety_version(session, vid: int) -> SafetyConfigVersion | None:
    return session.get(SafetyConfigVersion, vid)


def prune_safety_versions(session, keep: int = 30) -> int:
    """只保留最近 keep 条历史版本，返回清理条数。

    ★ 审计修复 P2：原实现把**整表（含 config JSON 文本列）**读进内存只为切片。
      现在只取 keep 个主键（几十个 int），再按主键集合删除。
    """
    if keep <= 0:
        return prune_batched(session, SafetyConfigVersion, true())
    kept = [r[0] for r in session.execute(
        select(SafetyConfigVersion.id)
        .order_by(SafetyConfigVersion.id.desc())
        .limit(int(keep))
    ).all()]
    if not kept:
        return 0
    res = session.execute(delete(SafetyConfigVersion)
                          .where(SafetyConfigVersion.id.notin_(kept)))
    session.commit()
    return int(getattr(res, "rowcount", 0) or 0)


# ============================ 视觉分拣记录 ============================
from app.db.models import VisionRecord  # noqa: E402


def insert_vision_record(session, data: dict) -> VisionRecord | None:
    """幂等写入一条视觉记录。

    ★ 用 seq 做唯一键: 后端重启后会从 since=0 重放相机服务的最近事件,
      没有幂等保证就会出现重复记录。已存在直接返回 None。
    """
    seq = int(data.get("seq") or 0)
    if seq > 0:
        exist = session.execute(
            select(VisionRecord).where(VisionRecord.seq == seq)
        ).scalars().first()
        if exist is not None:
            return None
    lab = data.get("lab")
    lab_s = ""
    if isinstance(lab, (list, tuple)) and len(lab) >= 3:
        lab_s = "%.2f,%.2f,%.2f" % (float(lab[0]), float(lab[1]), float(lab[2]))
    center = data.get("center")
    center_s = ""
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        center_s = "%.1f,%.1f" % (float(center[0]), float(center[1]))
    images = data.get("images") or {}
    now = datetime.now(timezone.utc)
    row = VisionRecord(
        seq=seq,
        timestamp=now,
        color=str(data.get("color") or "")[:16],
        hex=str(data.get("hex") or "")[:9],
        conf=float(data.get("conf") or 0.0),
        de=float(data.get("de") or 0.0),
        lab=lab_s,
        ratio=float(data.get("ratio") or 0.0),
        alt=str(data.get("alt") or "")[:16],
        center=center_s,
        area=int(data.get("area") or 0),
        reason=str(data.get("reason") or "")[:64],
        images=json.dumps(images, ensure_ascii=False),
        program_id=data.get("program_id"),
        program_name=str(data.get("program_name") or "")[:64],
        outcome=str(data.get("outcome") or "recorded")[:16],
        note=str(data.get("note") or "")[:255],
        source=str(data.get("source") or "camera")[:16],
        created_at=now,
    )
    session.add(row)
    session.commit()
    return row


def list_vision_records(session, color: str | None = None, since=None,
                        limit: int = 100, offset: int = 0):
    stmt = select(VisionRecord)
    if color:
        stmt = stmt.where(VisionRecord.color == color)
    if since is not None:
        stmt = stmt.where(VisionRecord.timestamp >= since)
    stmt = stmt.order_by(VisionRecord.id.desc()).limit(limit).offset(offset)
    return list(session.execute(stmt).scalars())


def count_vision_records(session, color: str | None = None, since=None) -> int:
    from sqlalchemy import func
    stmt = select(func.count(VisionRecord.id))
    if color:
        stmt = stmt.where(VisionRecord.color == color)
    if since is not None:
        stmt = stmt.where(VisionRecord.timestamp >= since)
    return int(session.execute(stmt).scalar() or 0)


def vision_stats(session, since=None) -> dict:
    """按颜色统计 + 总数（现场最常问"今天红的有多少"）。"""
    from sqlalchemy import func
    stmt = select(VisionRecord.color, func.count(VisionRecord.id))
    if since is not None:
        stmt = stmt.where(VisionRecord.timestamp >= since)
    stmt = stmt.group_by(VisionRecord.color)
    by_color = {str(c or "?"): int(n) for c, n in session.execute(stmt).all()}
    return {"total": sum(by_color.values()), "by_color": by_color,
            "colors": len(by_color)}


def get_vision_record(session, rid: int) -> VisionRecord | None:
    return session.get(VisionRecord, rid)


def update_vision_record(session, rid: int, **fields) -> VisionRecord | None:
    row = session.get(VisionRecord, rid)
    if row is None:
        return None
    for k, v in fields.items():
        if v is not None and hasattr(row, k):
            setattr(row, k, v)
    session.commit()
    return row


def prune_vision_records(session, days: int, batch: int = PRUNE_BATCH) -> int:
    """删除 N 天前的视觉记录，返回条数。

    ★ 审计修复 P0-4：days<=0 的"清空"分支原用 count()+query.delete()
      （单条语句无事务分批，大量行时锁窗口长），统一走分批。
    """
    if days <= 0:
        return prune_batched(session, VisionRecord, true(), batch=batch)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return prune_batched(session, VisionRecord, VisionRecord.timestamp < cutoff,
                         batch=batch)


# =====================================================================
# ★ E4 判色样本环 (vision_samples) + 阈值建议提案 (color_advice_proposals)
#   方案：docs/方案-创新功能四批…§E4。写入方唯一 = services/vision_samples.ingest()，
#   读取方 = api/vision.py 的 /samples* 三条 + services/color_advice.py。
#   ★ 同样是**新建表**，不给 vision_records 加列（create_all() 不补列，见 models.py）。
# =====================================================================
from app.db.models import ColorAdviceProposal, VisionSample  # noqa: E402

SAMPLE_MAX_CHARS = 24000   # frames/votes/thr 三段 JSON 的单列上限（防脏值撑爆库）


def insert_vision_sample(session, data: dict) -> VisionSample:
    """落一条判色样本。★ 不做幂等（每一次判色都是一次观测，重复本身是信息）。

    `data` 由 `services/vision_samples.sample_from()` 产出（纯函数，已过白名单抽取），
    这里只负责类型收敛与截断 —— 任何脏值都夹成能写的形态，不抛异常。
    """
    now = data.get("dt") or datetime.now(timezone.utc)
    row = VisionSample(
        timestamp=now,
        cell=str(data.get("cell") or "")[:16],
        color=str(data.get("color") or "")[:16],
        hex=str(data.get("hex") or "")[:9],
        conf=float(data.get("conf") or 0.0),
        valid_colors=str(data.get("valid_colors") or "")[:128],
        vote_ok=1 if data.get("vote_ok") else 0,
        placeholder=1 if data.get("placeholder") else 0,
        votes=str(data.get("votes") or "{}")[:SAMPLE_MAX_CHARS],
        reason=str(data.get("reason") or "")[:128],
        frames=str(data.get("frames") or "[]")[:SAMPLE_MAX_CHARS],
        frame_count=int(data.get("frame_count") or 0),
        failed_frames=int(data.get("failed_frames") or 0),
        outcome=str(data.get("outcome") or "unreviewed")[:16],
        true_color=str(data.get("true_color") or "")[:16],
        source=str(data.get("source") or "sorting")[:16],
        stage=str(data.get("stage") or "")[:32],
        thr_snapshot=str(data.get("thr_snapshot") or "")[:SAMPLE_MAX_CHARS],
        note=str(data.get("note") or "")[:255],
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row


def list_vision_samples(session, color: str | None = None, outcome: str | None = None,
                        source: str | None = None, since=None,
                        limit: int = 100, offset: int = 0):
    stmt = select(VisionSample)
    if color:
        stmt = stmt.where(VisionSample.color == color)
    if outcome:
        stmt = stmt.where(VisionSample.outcome == outcome)
    if source:
        stmt = stmt.where(VisionSample.source == source)
    if since is not None:
        stmt = stmt.where(VisionSample.timestamp >= since)
    stmt = stmt.order_by(VisionSample.id.desc()).limit(limit).offset(offset)
    return list(session.execute(stmt).scalars())


def sample_color_counts(session, since=None) -> dict:
    """逐色样本数（界面"青：n=6"这种；★ 没有的色**不出现**在字典里，
    由上层显式补 0 —— 「读不到」与「读到 0」不能在这一层就混掉）。"""
    from sqlalchemy import func
    stmt = select(VisionSample.color, func.count(VisionSample.id))
    if since is not None:
        stmt = stmt.where(VisionSample.timestamp >= since)
    stmt = stmt.group_by(VisionSample.color)
    return {str(c or "?"): int(n) for c, n in session.execute(stmt).all()}


def count_vision_samples(session, since=None) -> int:
    from sqlalchemy import func
    stmt = select(func.count(VisionSample.id))
    if since is not None:
        stmt = stmt.where(VisionSample.timestamp >= since)
    return int(session.execute(stmt).scalar() or 0)


def get_vision_sample(session, sid: int) -> VisionSample | None:
    return session.get(VisionSample, sid)


def review_vision_sample(session, sid: int, judgement: str,
                         true_color: str | None = None,
                         note: str | None = None) -> VisionSample | None:
    """人工复核回流（方案 §E4 的"免费标注"）。

    judgement="correct" → outcome=confirmed；"wrong" → outcome=corrected（真色必填才有意义）。
    ★ 只改标签三列（outcome/true_color/reviewed_at），**不动**逐帧特征与当时的结论 ——
      样本环的价值就在于"原始观测 + 事后标签"两份都在，改结论就等于把证据涂掉了。
    """
    row = session.get(VisionSample, sid)
    if row is None:
        return None
    j = str(judgement or "").lower()
    if j == "correct":
        row.outcome = "confirmed"
    elif j == "wrong":
        row.outcome = "corrected"
        if true_color:
            row.true_color = str(true_color)[:16]
    else:
        row.outcome = "reviewed"
    if true_color and j != "wrong":
        row.true_color = str(true_color)[:16]
    if note is not None:
        row.note = str(note)[:255]
    row.reviewed_at = datetime.now(timezone.utc)
    session.commit()
    return row


def samples_for_advice(session, limit: int = 3000):
    """建议派生的取数窗口（最新 N 条，含未复核 —— 未复核的只进"样本数"，不进误判统计）。"""
    return list_vision_samples(session, limit=limit)


def prune_vision_samples(session, days: int, batch: int = PRUNE_BATCH) -> int:
    if days <= 0:
        return prune_batched(session, VisionSample, true(), batch=batch)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return prune_batched(session, VisionSample, VisionSample.timestamp < cutoff,
                         batch=batch)


# ---------------- 阈值建议提案（账面动作，永不下发）----------------
def insert_advice_proposal(session, diff_json: str, summary: str = "",
                           thr_current_json: str = "", actor: str = "system",
                           note: str = "") -> ColorAdviceProposal:
    """新增一条提案，并把上一条 active 顶成 superseded（保留下来 = 可回滚的那一版）。"""
    prev = session.execute(
        select(ColorAdviceProposal).where(ColorAdviceProposal.status == "active")
    ).scalars().all()
    for p in prev:
        p.status = "superseded"
    row = ColorAdviceProposal(
        created_at=datetime.now(timezone.utc),
        actor=str(actor or "system")[:16],
        status="active",
        summary=str(summary or "")[:512],
        diff=str(diff_json or "[]")[:SAMPLE_MAX_CHARS],
        thr_current=str(thr_current_json or "")[:SAMPLE_MAX_CHARS],
        note=str(note or "")[:255],
    )
    session.add(row)
    session.commit()
    return row


def list_advice_proposals(session, limit: int = 20):
    stmt = (select(ColorAdviceProposal).order_by(ColorAdviceProposal.id.desc())
            .limit(max(1, int(limit))))
    return list(session.execute(stmt).scalars())


def get_active_proposal(session) -> ColorAdviceProposal | None:
    stmt = (select(ColorAdviceProposal).where(ColorAdviceProposal.status == "active")
            .order_by(ColorAdviceProposal.id.desc()).limit(1))
    return session.execute(stmt).scalars().first()


def get_latest_superseded(session) -> ColorAdviceProposal | None:
    """回滚的目标 = 最近一条被顶掉的提案（"上一版"）。"""
    stmt = (select(ColorAdviceProposal).where(ColorAdviceProposal.status == "superseded")
            .order_by(ColorAdviceProposal.id.desc()).limit(1))
    return session.execute(stmt).scalars().first()


def mark_proposal(session, pid: int, status: str) -> ColorAdviceProposal | None:
    row = session.get(ColorAdviceProposal, pid)
    if row is None:
        return None
    row.status = str(status or "")[:16]
    session.commit()
    return row
