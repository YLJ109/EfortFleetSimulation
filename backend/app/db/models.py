# -*- coding: utf-8 -*-
"""ORM 表定义 (Base + 三张表)。引擎/会话见 database.py。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PoseHistory(Base):
    __tablename__ = "pose_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # 机房 12 台后，历史曲线必须按机器人分开存；单机时期（含本列加入前的旧行）
    # 一律归到 MAIN —— 旧数据不是"不知道是谁"，而是"就是那台 ER8-700H"。
    robot_id: Mapped[str] = mapped_column(String(16), default="MAIN", index=True)
    j1: Mapped[float] = mapped_column(Float, default=0.0)
    j2: Mapped[float] = mapped_column(Float, default=0.0)
    j3: Mapped[float] = mapped_column(Float, default=0.0)
    j4: Mapped[float] = mapped_column(Float, default=0.0)
    j5: Mapped[float] = mapped_column(Float, default=0.0)
    j6: Mapped[float] = mapped_column(Float, default=0.0)
    tcp_x: Mapped[float] = mapped_column(Float, default=0.0)  # 末端 TCP (mm)
    tcp_y: Mapped[float] = mapped_column(Float, default=0.0)
    tcp_z: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default="real")  # real | replay


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")  # INFO|WARN|ERROR
    message: Mapped[str] = mapped_column(String(512), default="")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    label: Mapped[str] = mapped_column(String(64), default="")


class SafetyEvent(Base):
    """安全围栏报警事件（时间线用）。

    只在状态"变严重"时写入（进入 danger / hit），以及从 danger/hit 恢复时写一条
    state=clear，避免每帧刷库。
    """

    __tablename__ = "safety_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    zone_id: Mapped[str] = mapped_column(String(32), default="")
    zone_name: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(16), default="")  # danger | hit | clear
    clearance: Mapped[float] = mapped_column(Float, default=0.0)  # 余量(m)
    ratio: Mapped[float] = mapped_column(Float, default=0.0)  # 余量百分比 0~1


class Recording(Base):
    """录制的动作序列: 帧存 JSON 文本 [{t, j1..j6}], t 为相对录制起点毫秒。

    source: real = 录制真机动作; sim = 模拟视图键盘摆出的动作。
    """

    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="未命名录制")
    description: Mapped[str] = mapped_column(String(512), default="")
    source: Mapped[str] = mapped_column(String(16), default="sim")  # real | sim
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    frames: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Point(Base):
    """预设点位: 关节角或直角坐标目标, 供执行引擎调用。

    joints: JSON 文本 [j1..j6] (度); tcp: JSON 文本 {x,y,z,rx,ry,rz} 或 null (直角坐标模式下解析用)。
    kind: joint(关节模式) | cartesian(直角坐标模式, 执行时经 IK 转关节角)。
    """

    __tablename__ = "points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="未命名点位")
    group: Mapped[str] = mapped_column(String(64), default="默认")
    kind: Mapped[str] = mapped_column(String(16), default="joint")  # joint | cartesian
    joints: Mapped[str] = mapped_column(Text, default="[0,0,0,0,0,0]")
    tcp: Mapped[str] = mapped_column(Text, default="null")         # {x,y,z,rx,ry,rz} | null
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Program(Base):
    """执行程序(有序点位序列): items = JSON 文本 [{point_id, speed_pct, dwell_ms}]。"""

    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="未命名程序")
    note: Mapped[str] = mapped_column(String(512), default="")
    items: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SystemEvent(Base):
    """统一事件总线 / 审计日志（阶段 3 时间线 + 阶段 4 审计）。

    ★★ 为什么要另起一张表，而不是给既有的 events 表加字段？
        events 表在旧库里已经存在（只有 id/timestamp/level/message 四列），
        而 SQLAlchemy 的 create_all() **不会**给已存在的表补列 —— 直接改 Event
        模型会导致线上库查询时报 "no such column"。这里另起名 system_events，
        既有数据一行不动，新库也能正常建。

    category: system | connection | auth | control | config | safety
    level:    debug | info | warn | error | critical
    actor:    system | admin | operator | anonymous
    detail:   JSON 文本（扩展信息，可为空）
    """

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(16), default="system", index=True)
    level: Mapped[str] = mapped_column(String(8), default="info", index=True)
    action: Mapped[str] = mapped_column(String(64), default="", index=True)
    actor: Mapped[str] = mapped_column(String(16), default="system")
    ip: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(String(512), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class SafetyConfigVersion(Base):
    """安全围栏配置历史版本（阶段 4 配置版本化）。

    每次保存/重置/导入前，把"改动前"的那份配置归档进来，支持事后审计与一键回滚。
    config 为 JSON 文本（已是校验归一化后的结果）。
    """

    __tablename__ = "safety_config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(16), default="system")
    source: Mapped[str] = mapped_column(String(16), default="save")  # save|reset|import|rollback
    note: Mapped[str] = mapped_column(String(255), default="")
    config: Mapped[str] = mapped_column(Text, default="{}")


class VisionRecord(Base):
    """视觉颜色分拣记录（后端侧，独立于相机服务的内存记录）。

    ★ 同样是**新建表**而不是给旧表加列：create_all() 不给已存在的表补字段，
      直接扩已有 ORM 类会让线上库查询报 "no such column"。

    seq 是相机服务侧的触发序号，唯一索引用于幂等摄入（后端重启后重放不会重复入库）。
    source 区分"相机服务上报"与"人工复核修正"，便于追溯谁改了判定。
    """

    __tablename__ = "vision_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True, unique=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    color: Mapped[str] = mapped_column(String(16), default="", index=True)
    hex: Mapped[str] = mapped_column(String(9), default="")
    conf: Mapped[float] = mapped_column(Float, default=0.0)
    de: Mapped[float] = mapped_column(Float, default=0.0)
    lab: Mapped[str] = mapped_column(String(64), default="")       # "L,a,b"
    ratio: Mapped[float] = mapped_column(Float, default=0.0)
    alt: Mapped[str] = mapped_column(String(16), default="")
    center: Mapped[str] = mapped_column(String(32), default="")    # "cx,cy"
    area: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(64), default="")
    images: Mapped[str] = mapped_column(Text, default="{}")        # {"full":rel,...}
    program_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    program_name: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(16), default="recorded")
    # recorded | executed | skipped | blocked | error | reviewed
    note: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(16), default="camera")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class VisionSample(Base):
    """★ E4「判色样本环」：每一次判色留下的 **ROI 特征 + 判据值 + 结论 + 事后人工标签**。

    ★★ 为什么是**新建表**（项目约定，见 `VisionRecord` / `SystemEvent` 的同款注释）：
        `create_all()` 不会给已存在的表补列，给旧表加字段会让线上库直接
        "no such column"。样本环与视觉记录（`vision_records`）问的是两件不同的事：
        那张表记"相机服务报了什么"，这张表记"**后端这一次判色凭什么这么判**"
        （逐帧特征 + 投票明细 + 当时的阈值快照 + 人工复核回流），是**建议派生的唯一数据源**。

    ★ 只存**数字特征**，不存像素：`frames` 里每帧只有 Lab/hue/fill/ΔE/chroma/conf/reason。
      ★ `mask`（numpy 数组的 JSON 形态）**绝不入表** —— 一帧就是几十万字节的噪声，
        体积爆炸且对"阈值该怎么挪"没有任何信息量（要图去相机侧归档链路
        `GET /vision/image?p=`，那里已经有）。白名单抽取，见 `services/vision_samples.py`。

    outcome（复核回流 = 方案说的"免费标注"）：
      unreviewed 未复核 → confirmed 人工确认判对 → corrected 人工改判（true_color 有值）
      put_back   无效色/判不出 → 原路放回（流程事实，由调用方在 meta 里如实带上）
    placeholder=1（空跑占位色）**不是判色结论**：`vote_ok` 会写成 0，
    建议派生一律排除占位样本（拿编出来的色当已标定证据 = §5.5 铁律③禁止的假显示）。

    thr_snapshot: 当时生效阈值的快照 JSON（{"thr","min_conf","state",...}）。
      ★ state="unknown" 是**有效值**（相机服务没回显 = 不知道现场用的是什么），
        它必须和"快照里确实没有这个键"分得开 —— 建议面据此拒绝给数值（三态不塌）。
    """

    __tablename__ = "vision_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    cell: Mapped[str] = mapped_column(String(16), default="", index=True)   # 格号（可空）
    color: Mapped[str] = mapped_column(String(16), default="", index=True)  # 投票结论色
    hex: Mapped[str] = mapped_column(String(9), default="")
    conf: Mapped[float] = mapped_column(Float, default=0.0)
    valid_colors: Mapped[str] = mapped_column(String(128), default="")      # "红,绿,蓝"
    vote_ok: Mapped[int] = mapped_column(Integer, default=0)
    placeholder: Mapped[int] = mapped_column(Integer, default=0)            # 空跑占位 → 不算结论
    votes: Mapped[str] = mapped_column(Text, default="{}")                  # 投票明细 JSON
    reason: Mapped[str] = mapped_column(String(128), default="")            # 判不出的真原因
    frames: Mapped[str] = mapped_column(Text, default="[]")                 # 逐帧特征 JSON
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_frames: Mapped[int] = mapped_column(Integer, default=0)          # {}＝抓取失败帧数
    outcome: Mapped[str] = mapped_column(String(16), default="unreviewed", index=True)
    true_color: Mapped[str] = mapped_column(String(16), default="")
    source: Mapped[str] = mapped_column(String(16), default="sorting")      # sorting|tictactoe|vision_rule
    stage: Mapped[str] = mapped_column(String(32), default="")
    thr_snapshot: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class ColorAdviceProposal(Base):
    """★ E4：判色阈值**建议提案**（落库=账面动作，绝不等于下发）。

    ★★ 为什么单独一张表：提案必须"保留上一版可回滚"，而 `vision_samples` 是逐帧事实流水，
        把提案塞进样本环会让两种生命周期完全不同的东西互相污染。
    ★ 本表**没有任何**"已生效"状态：生效只发生在人显式走既有下发通道
      （`POST /api/vision/config` → 相机服务 `/vision/config`），那条路不写这张表。
      所以"表里有一条 active 提案"永远不等于"现场阈值被改了" —— 界面也不许这么写。

    status: active（当前提案）| superseded（被新提案顶掉，可回滚到这里）| rolled_back（已回滚）
    diff:         建议项 JSON 数组 [{key, from, to, color, n, misjudged, ...}]
    thr_current:  提案时读到的现场阈值快照（state=unknown 时如实标 unknown）
    """

    __tablename__ = "color_advice_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(16), default="system")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    summary: Mapped[str] = mapped_column(String(512), default="")
    diff: Mapped[str] = mapped_column(Text, default="[]")
    thr_current: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(String(255), default="")
