# -*- coding: utf-8 -*-
"""引擎/会话/建库。CPU 绑定 SQLite, 关闭同进程内多线程检查。"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app.core.config import db_path
from app.db.models import Base

_engine = None
_SessionLocal = None


def _engine_url() -> str:
    """数据库连接串。优先级（★ 审计修复 P1-C4）：

      1. 环境变量 ``EFORT_DB_URL`` —— 测试/部署硬覆盖，永远最高；
      2. 配置文件 ``database.url`` —— 设置页/robot.yaml 里改的**真生效**；
         （原实现只读环境变量，配置项标着 apply="restart" 却是死配置，
          改完重启仍在写旧库，现场会误判"数据丢了"）
      3. 默认 ``data/robot.db`` 绝对路径。

    配置里的相对路径按项目根目录解析，与 robot.yaml 的写法一致。
    """
    override = os.environ.get("EFORT_DB_URL")
    if override:
        return override
    cfg_url = ""
    try:
        from app.core.config import get_config, project_root
        cfg_url = str(get_config().get("database", "url", default="") or "").strip()
    except Exception:
        cfg_url = ""
    if cfg_url:
        if cfg_url.startswith("sqlite:///"):
            path = cfg_url[len("sqlite:///"):]
            if not os.path.isabs(path):
                path = os.path.join(project_root(), path)
            return "sqlite:///" + path.replace("\\", "/")
        return cfg_url          # 其它方言（postgresql:// 等）原样交给 SQLAlchemy
    return f"sqlite:///{db_path()}"


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            _engine_url(),
            connect_args={"check_same_thread": False},
            future=True,
        )

        # P1-3: 启用 WAL + busy_timeout, 避免采集写入与 API 读取并发时 "database is locked"
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=5000")
            finally:
                cur.close()

        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, future=True)
    return _engine


def SessionLocal():
    if _SessionLocal is None:
        raise RuntimeError("数据库未初始化, 请先调用 init_db()")
    return _SessionLocal()


def engine_url() -> str:
    """**真正在用**的数据库连接串（初始化之后才有意义）。

    ★ 启动日志原来打的是 `db_path()`（默认路径），而 `EFORT_DB_URL` / `database.url`
      都能把它顶掉 —— 于是测试日志里出现"数据库就绪: …data/robot.db"，
      看着像在写生产库（§5.5 铁律⑤：日志不许编没发生的事）。
    """
    if _engine is None:
        return _engine_url()
    return str(_engine.url)


def _migrate(eng) -> None:
    """轻量迁移: create_all 不会给已存在的表加列, 这里按需补列(幂等)。"""
    insp = inspect(eng)
    if "recordings" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("recordings")}
    if "source" not in cols:
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE recordings ADD COLUMN source VARCHAR(16) DEFAULT 'sim'"))


def init_db() -> None:
    eng = get_engine()
    Base.metadata.create_all(eng)
    _migrate(eng)
