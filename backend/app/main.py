# -*- coding: utf-8 -*-
"""
EFORT Web Monitoring 后端入口 (FastAPI)。
启动: 建库 + 启动采集器; 关闭: 停止采集器。
生产: 托管 frontend/dist 静态文件 (SPA)。开发: 用 vite 单独跑前端。
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    ai,
    asr,
    auth,
    control,
    events,
    frames,
    gamepad,
    points,
    programs,
    recordings,
    robot,
    safety,
    settings,
    sorting,
    system,
    tictactoe,
    vision,
    ws,
)
from app.core.config import get_config, public_dir, db_path, dotenv_loaded
from app.core.brand import (
    DEVICE_MODEL,
    SERVICE_NAME,
    SERVICE_NAME_EN,
    SERVICE_VERSION,
)
from app.core.exceptions import register_exception_handlers
from app.core.logger import get_logger
from app.core.middleware import RequestLogMiddleware
from app.db.crud import prune_old_poses, prune_system_events
from app.db.database import SessionLocal, init_db
from starlette.middleware.base import BaseHTTPMiddleware
from app.services.collector import collector
from app.services.events import emit as emit_event
from app.services.vision_ingest import ingest

log = get_logger("main")
if dotenv_loaded:
    log.info("已从 .env 载入 %d 个环境变量", dotenv_loaded)


class NoCacheStaticFiles(StaticFiles):
    """静态资源禁用浏览器缓存 —— 每次重新构建后刷新页面即可看到新版本。"""

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp


def _prune_history() -> None:
    """按配置保留天数清理历史（分批，见 crud.prune_batched）。"""
    try:
        days = int(get_config().sampling.get("history_retention_days", 0) or 0)
        if days <= 0:
            return
        s = SessionLocal()
        try:
            n = prune_old_poses(s, days)
            if n:
                log.info("历史清理: 删除 %d 条超过 %d 天的记录", n, days)
        finally:
            s.close()
    except Exception as e:
        log.warning("历史清理失败: %s", e)


def _prune_vision() -> None:
    """按配置清理超期的视觉分拣记录（分批）。"""
    try:
        days = int(get_config().vision.get("retention_days", 0) or 0)
        if days <= 0:
            return
        n = ingest.prune(days)
        if n:
            log.info("视觉记录清理: 删除 %d 条超过 %d 天的记录", n, days)
    except Exception as e:
        log.warning("视觉记录清理失败: %s", e)


def _prune_events() -> None:
    """★ 审计修复 P1-C5：审计表（system_events）原来**没有任何保留策略**，
    只增不减 → 点动/下发/登录每条一 commit，7×24 下无限膨胀。
    保留天数取 retention.audit_days（缺省 90 天，<=0 表示不清理）。
    """
    try:
        days = int(get_config().get("retention", "audit_days", default=90) or 0)
        if days <= 0:
            return
        s = SessionLocal()
        try:
            n = prune_system_events(s, days)
            if n:
                log.info("审计事件清理: 删除 %d 条超过 %d 天的记录", n, days)
        finally:
            s.close()
    except Exception as e:
        log.warning("审计事件清理失败: %s", e)


def _prune_all() -> None:
    """一次完整清理（启动时跑一遍，之后由周期任务复用）。"""
    _prune_history()
    _prune_vision()
    _prune_events()


def _prune_interval_sec() -> int:
    """清理周期（秒）。缺省 1 小时；retention.prune_interval_min 可调。"""
    try:
        mins = int(get_config().get("retention", "prune_interval_min", default=60) or 0)
        return max(300, mins * 60)
    except Exception:
        return 3600


async def _prune_loop():
    """★ 审计修复 P0-5：清理原来**只在启动跑一次**。
    7×24 运行的监控服务永远不再清理 → pose_history 按 1550 万行/年、
    日志按 15~30 MB/天 无限增长，最终磁盘打满、查询变慢、启动更慢。
    现在改成后台周期任务；单轮失败不影响下一轮（有兜底 try）。
    """
    while True:
        try:
            await asyncio.to_thread(_prune_all)
        except Exception:
            log.exception("周期清理失败，下一轮继续")
        await asyncio.sleep(_prune_interval_sec())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    log.info("数据库就绪: %s", db_path())
    # ★ P0-4：prune 已改为分批 + 短事务，启动不再可能因欠账 OOM/长锁。
    #   为避免"启动即做大量 IO 拖慢就绪"，启动只清理一次，后续交给周期任务。
    await asyncio.to_thread(_prune_all)
    collector.start()
    ingest.start()          # 视觉事件摄入（相机服务没起时会自动重试, 不影响后端启动）
    emit_event("system", "info", "system.start",
               f"服务已启动（{'模拟' if collector.simulated else '真实'}链路）",
               {"simulated": collector.simulated, "connected": collector.connected})
    # ★ P0-5：周期清理任务（必须在 finally 里 cancel，否则关闭时留下悬挂任务）
    prune_task = asyncio.create_task(_prune_loop())
    try:
        yield
    finally:
        prune_task.cancel()
        try:
            await prune_task
        except asyncio.CancelledError:
            pass
        emit_event("system", "info", "system.stop", "服务正在关闭")
        ingest.stop()
        collector.stop()
        log.info("采集器已停止")


app = FastAPI(
    title=SERVICE_NAME,
    version=SERVICE_VERSION,
    description=f"{SERVICE_NAME_EN} — {DEVICE_MODEL} 工业机器人全栈 Web 监控/仿真/控制系统",
    lifespan=lifespan,
)

# CORS：同源部署（前端 dist 由本服务托管）+ 开发用 vite proxy，浏览器视角均为同源，
# 故无需携带凭据；通配符 origin 与 allow_credentials=True 是浏览器禁止的非法组合。
# ★ 审计修复 P1-B5：原 allow_origins=["*"] + allow_methods/headers=["*"] 会把
#   无鉴权写接口暴露给**任意第三方网页**（只要运维在同机开着浏览器访问过本服务）。
#   生产是同源，根本不需要 CORS；只有开发时的 vite(5173) 才需要。
_CORS_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",      # vite preview
    "http://127.0.0.1:4173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_DEV_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Control-Token", "Authorization"],
)

# ★ 审计修复 P1-B7：请求体大小上限。
#   原来没有任何限制 —— 匿名可 POST 一个几百 MB 的 JSON（/recordings、/system/import
#   等）直接把进程内存打爆。8 MB 覆盖现有最大合法载荷（导入的程序/点位包）。
MAX_BODY_BYTES = int(os.environ.get("EFORT_MAX_BODY", str(8 * 1024 * 1024)))


class BodyLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cl = request.headers.get("content-length", "")
        if cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return JSONResponse(
                {"code": "PAYLOAD_TOO_LARGE", "message": "请求体过大",
                 "detail": None},
                status_code=413,
            )
        return await call_next(request)


app.add_middleware(BodyLimitMiddleware)

# ★★ 2026-10-10 性能加固（静态评审 B1）：
#   本项目大量端点是 `def`（同步）—— Starlette 会把它们丢进 anyio 的线程池（**默认 40**）。
#   而 `/control/run-file`（一次程序执行可能跑几分钟）、`/api/ready`（等锁+轮询 ~6s）、
#   `/ai/chat`（LLM 超时 12s）都是**占着一个 worker 直到返回**；
#   几个长任务 + 若干长轮询就能把池占满 → 表现是"整个网页转圈、点哪都不响应"。
#   把这些任务的瓶颈是**设备 I/O 与全局执行锁**（它们自己会排队），不是 CPU ——
#   多给几个线程只是避免"池被长任务占满，连短请求也跟着排队"。
#   ⚠ 这是**止血**，不是根治：根治要把长任务改成"起线程 + 立刻返回 run_id"
#     （`/ai/exec`、`/sorting/start` 已是这个模式；`/run-file` 因为前端要读它的
#     逐步结果，异步化需要连 UI 一起改，另行排期）。
try:
    import anyio
    anyio.to_thread.current_default_thread_limiter().total_tokens = int(
        os.environ.get("EFORT_THREAD_POOL", "96"))
except Exception:  # noqa: BLE001  （anyio 版本差异时静默跳过，不因此起不来）
    pass

# 请求日志 + 全局异常处理
app.add_middleware(RequestLogMiddleware)
register_exception_handlers(app)

app.include_router(robot.router)
app.include_router(ws.router)
app.include_router(recordings.router)
app.include_router(control.router)
# 只读预演（/control/preview、/control/ik）：不挂 require_control —— 只算不发，见 control.py
app.include_router(control.router_ro)
app.include_router(safety.router)
app.include_router(auth.router)
app.include_router(points.router)
app.include_router(programs.router)
app.include_router(events.router)
app.include_router(system.router)
app.include_router(vision.router)
app.include_router(frames.router)
app.include_router(settings.router)
# ★ 实时语音字幕（腾讯云 ASR）：HTTP 配置查询 + WebSocket 音频/字幕双向代理
app.include_router(asr.router)
app.include_router(asr.ws_router)
# ★ 三色方块分拣（独立页）：38 点位体检 + 分拣引擎（复用同一套停止/暂停/进度设施）
app.include_router(sorting.router)
app.include_router(ai.router)
app.include_router(tictactoe.router)
# ★ 手柄状态（后端直读 XInput，只读免令牌）：浏览器读不到手柄时的替代输入源
app.include_router(gamepad.router)


# 存活探针/根信息：必须在静态挂载之前注册，否则被 mount("/") 拦截。
@app.get("/healthz")
def healthz():
    """存活探针：不依赖 DB/机器人，仅确认进程存活。"""
    return {"status": "alive"}


@app.get("/api/version")
def api_version():
    """服务版本信息（运维/诊断用）。"""
    return {
        "service": SERVICE_NAME,
        "service_en": SERVICE_NAME_EN,
        "device": DEVICE_MODEL,
        "version": SERVICE_VERSION,
        "docs": "/docs",
        "ws": "/ws/pose",
    }


# 生产: 托管前端构建产物 (SPA)。必须在 API 路由之后挂载。
# NoCacheStaticFiles: 强制不缓存, 避免"重启后界面无变化"的浏览器缓存问题。
_dist = public_dir()
if os.path.isdir(_dist):
    app.mount("/", NoCacheStaticFiles(directory=_dist, html=True), name="static")
    log.info("已托管前端静态目录(no-cache): %s", _dist)
else:
    log.warning("前端构建目录不存在(%s), 仅提供 API/WS。运行 frontend npm run build 后重试。", _dist)
