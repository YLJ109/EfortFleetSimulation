# -*- coding: utf-8 -*-
"""
采集器: 后台线程读取关节角(真实 Modbus 或合成模拟) -> 算 TCP -> 广播 + 入库。
单例 collector 由 main.py 在启动时 start(), 关闭时 stop()。
"""
from __future__ import annotations

import time
from threading import Lock, Thread
from typing import Any, Dict, Optional

from app.core.config import get_config
from app.core.logger import get_logger
from app.db.database import SessionLocal
from app.db.crud import insert_pose
from app.services.events import emit as emit_event
from app.services.hub import hub
from app.services.kinematics import tcp_of
from app.services import metrics
from app.services.modbus import ModbusRobot
from app.services.motion import motion, real_write_enabled, snapshot_ready
from app.services.runmode import runmode
from app.services.safety_eval import evaluate as safety_evaluate
from app.services.safety_evidence import note as safety_note
from app.services.safety_guard import update_server as safety_update_server
from app.services.sim_robot import sim_robot
# ★ E2 到位自证：只读观测件（不 import 写路径、不产生任何动作）
from app.services.twin_residual import twin_residual

log = get_logger("collector")


def twin_observe(joints, at) -> None:
    """E2：每拍把**刚从控制器读到的**关节角喂给到位自证观测器。

    ★ 与 safety_note 同一条纪律：观测件绝不带崩采集 —— 一切异常吞掉。
    ★ 独立成模块级函数（而不是内联调用），单测可 monkeypatch 它来冻结/伪造喂帧。
    """
    try:
        twin_residual.observe(joints, at=at)
    except Exception as e:                     # noqa: BLE001
        log.debug("到位自证喂帧失败（已忽略）: %s", e)

# 寄存器快照（rc-status）并入 WS 的广播周期：与旧前端 4s 轮询节奏一致，
# 点到 / 适度 —— rc_snapshot 是 3 个 FC3 事务，太高会拖采集循环，太低则点动体验迟钝。
RC_STATUS_INTERVAL = 4.0


def _backoff(restarts: int) -> float:
    """采集循环崩溃后的重启退避秒数：0.5 → 1 → 2 → 4 → 8 → 封顶 30s。

    ★ P1-A5：既要"立刻重试"（短暂故障秒级恢复），又不能退化成无限疯狂重启。
    """
    return min(30.0, 0.5 * (2 ** max(0, restarts - 1)))


class Collector:
    def __init__(self) -> None:
        self.modbus = ModbusRobot()
        self.latest: Optional[Dict[str, Any]] = None
        self.latest_rc_status: Optional[Dict[str, Any]] = None   # 最近一帧 rc_status（供 WS 握手即时推）
        self.simulated = False
        self.connected = False
        self._lock = Lock()
        self._running = False
        self._thread: Optional[Thread] = None
        self._last_db_write = 0.0
        self._force_reconnect = False   # 手动重连标志（网线切换后由 API 触发）

    def decide_mode(self) -> bool:
        """返回是否模拟: always / never / auto(不可达则模拟)。"""
        mode = str(get_config().connection.get("simulate", "auto")).lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        ok = self.modbus.reachable()
        self.connected = ok
        return not ok

    def start(self) -> None:
        self.simulated = self.decide_mode()
        self.connected = not self.simulated
        log.info("采集器启动, 模式=%s", "SIMULATE" if self.simulated else "REAL")
        self._running = True
        self._thread = Thread(target=self._loop, name="collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def get_latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.latest

    def reconnect(self) -> Dict[str, Any]:
        """手动重连（网线在摄像头/机器人间切换后触发）。

        立即探测 Modbus 可达性并切换真实/模拟模式，同时通知采集循环重置
        失败计数，无需重启服务。
        """
        # ★ 与自动恢复保持一致：显式 simulate=always 时，手动重连也不切真实，
        #   否则演示/离线调试环境点一下"重连"就被拽回真机链路。
        # ★ 审计修复 P1-C2：重连前先按**当前**配置刷新连接参数 ——
        #   设置页把 connection.host/port/timeout 标成 apply="reconnect"，
        #   原实现却一直用 import 时读死的旧地址探测，"改完没生效"。
        #   collector 与 motion 各持一个 ModbusRobot 实例，两个都要刷。
        try:
            self.modbus.reload_config()
            from app.services.motion import motion as _motion
            if _motion.modbus is not self.modbus:
                _motion.modbus.reload_config()
        except Exception as e:
            log.warning("刷新连接配置失败（保持原参数）: %s", e)
        mode = str(get_config().connection.get("simulate", "auto")).lower()
        ok = False if mode == "always" else self.modbus.reachable()
        with self._lock:
            if ok:
                self.simulated = False
                self.connected = True
            else:
                self.simulated = True
                self.connected = False
            self._force_reconnect = True   # 让 _loop 重置 fail_count/retry_at
        log.info("手动重连: 控制器%s%s",
                 "可达, 切回真实" if ok else "不可达, 保持模拟",
                 " (配置 simulate=always，已锁定模拟)" if mode == "always" else "")
        return {"connected": self.connected, "simulated": self.simulated}

    def _loop(self) -> None:
        """采集线程主入口：循环体异常兜底（★ 审计修复 P1-A5）。

        采集线程一旦静默死掉，前端姿态/安全灯会永远停在最后一帧 —— 操作员
        看到的是一台"画面正常、数据不动"的机器人，比直接报错危险得多。
        这里把整个循环体包一层：任何未预料的异常只降级重启，不结束线程；
        连续崩溃按指数退避，避免刷爆日志与事件总线。
        """
        restarts = 0
        while self._running:
            t_start = time.time()
            try:
                self._loop_impl()
                return                       # _running=False 的正常退出
            except Exception as e:
                restarts += 1
                log.exception("采集循环异常退出（第 %d 次），%.1f 秒后重启",
                              restarts, _backoff(restarts))
                try:
                    emit_event("connection", "error", "collector.crashed",
                               f"采集循环异常，已自动重启（第 {restarts} 次）",
                               {"error": str(e)[:200]})
                except Exception:
                    pass                     # 事件总线本身出错也不能挡住重启
                if not self._running:
                    return
                # 瞬时异常（进循环就崩）才退避；跑了一阵才崩说明系统仍在工作
                elapsed = time.time() - t_start
                if elapsed < 1.0:
                    deadline = time.time() + _backoff(restarts)
                    while self._running and time.time() < deadline:
                        time.sleep(0.2)

    @staticmethod
    def _sample_rates() -> Dict[str, float]:
        """★ 审计修复 P1-C3：采样频率必须**每轮**重读。

        原实现 `_loop` 只在线程启动时读一次，而设置页把
        ``sampling.read_hz/ws_push_hz/db_write_hz`` 标成 live/reload/reconnect
        （字段 help 还写着"防止数据库膨胀"）—— 调整入库频率完全无效。
        纯 dict 取值，成本可忽略。
        """
        samp = get_config().sampling
        read_hz = float(samp.get("read_hz", 10))
        push_hz = float(samp.get("ws_push_hz", 20))
        db_hz = float(samp.get("db_write_hz", 2))
        return {
            "interval": 1.0 / max(read_hz, push_hz),
            "read_interval": 1.0 / max(read_hz, 0.5),
            "db_interval": 1.0 / max(db_hz, 0.1),
        }

    def _loop_impl(self) -> None:
        cfg = get_config()
        # 轴符号校准 (config/robot.yaml: axis_sign), 界面转向与真机不符时可逐轴翻转
        signs = [float(s) for s in cfg.get("axis_sign", default=[1] * 6)]
        if len(signs) < 6:
            signs = signs + [1.0] * (6 - len(signs))

        # P0-1: 读取失败不永久降级 —— 仅累计; 连续失败到阈值才临时模拟, 并持续自动重试。
        # 阈值调大(read_hz=10 时约 3 秒): 避免控制器重启/网络抖动等短暂中断被误判为永久掉线,
        # 减少"时好时坏"的频繁降级/恢复抖动。
        FAIL_LIMIT = 30
        last_read = 0.0
        fail_count = 0
        retry_at = 0.0
        # ★ Stage C：模拟初值从零位开始（不再是无意义的正弦扫掠起点），
        #   有指令后由 sim_robot 推着走。
        last_joints = [0.0] * 6
        sim_tracking = False
        # ★ P1-A6：最近一次真实读取是否失败（失败帧必须标 stale，绝不冒充真机数据）
        read_stale = False
        last_rc_at = 0.0
        # ★ A1：后端独立围栏评估的节流钟（0.2s 一次；采集本身是 0.1s 一拍）
        last_guard_at = 0.0
        guard_error_at = 0.0
        # ★ P1-C3：循环变量先给默认值，随后每轮（按 rate_next 节流）重读配置。
        allow_real = str(get_config().connection.get("simulate", "auto")).lower() != "always"
        interval, read_interval, db_interval = 0.05, 0.1, 0.5
        rate_next = 0.0
        prev_loop = 0.0    # ★ B1 指标：上一拍开始时刻（算采集间隔；只读观测，不改变任何行为）

        while self._running:
            loop_start = time.time()
            # ★ B1（只读指标）：拍间隔与连续失败次数进 metrics —— 拿不到就是没测，
            #   不在这里做任何决策（采集逻辑本身一行没改）。
            if prev_loop:
                metrics.stat("collector.loop_period_ms", (loop_start - prev_loop) * 1000.0)
            prev_loop = loop_start
            joints = None

            # ★ P1-C3：采样频率每轮重读（设置页标的是 live/reload，原来只在线程
            #   启动时读一次 → 调整 db_write_hz"防止数据库膨胀"根本无效）。
            #   顺带每 2s 重读 simulate 开关，reload 后模式也能真的变。
            #   纯 dict 取值，2s 一次的成本可忽略。
            if loop_start >= rate_next:
                rate_next = loop_start + 2.0
                rates = self._sample_rates()
                interval = rates["interval"]
                read_interval = rates["read_interval"]
                db_interval = rates["db_interval"]
                sim_mode = str(get_config().connection.get("simulate", "auto")).lower()
                allow_real = sim_mode != "always"
                # 轴符号也一起重读：现场"转向与真机不符"时改 axis_sign 后 reload 即生效
                signs = [float(s) for s in get_config().get("axis_sign", default=[1] * 6)]
                if len(signs) < 6:
                    signs = signs + [1.0] * (6 - len(signs))

            # 0) 手动重连请求: 模式已由 reconnect() 同步更新, 这里仅重置失败计数/重试定时
            with self._lock:
                if self._force_reconnect:
                    self._force_reconnect = False
                    fail_count = 0
                    retry_at = 0.0

            # 1) 处于模拟(初始离线 或 降级中): 定期探测能否恢复真实
            #    ★ 只有 simulate=auto/never 才自动切回真实；显式 simulate=always
            #      （演示、离线调试、以及测试环境）必须"锁死"在模拟，否则采集线程
            #      一发现控制器可达就把用户强制的模拟模式顶掉。
            if self.simulated:
                if loop_start >= retry_at:
                    retry_at = loop_start + 5.0
                    if allow_real and self.modbus.reachable():
                        log.info("控制器恢复可达, 切回真实读取")
                        self.simulated = False
                        fail_count = 0
                        emit_event("connection", "info", "connection.restored",
                                   "Modbus 控制器恢复可达，已切回真实链路",
                                   {"host": self.modbus.host if hasattr(self.modbus, "host") else ""})
                if self.simulated:
                    # ★ Stage C：有状态仿真机——没有指令就停在原地，有指令真的走过去。
                    joints, sim_tracking = sim_robot.step(loop_start)
                    self.connected = False

            # 2) 真实读取 (按 read_hz 节流; 未到读取时刻复用上次值, 画面平滑)
            if joints is None:
                if loop_start - last_read >= read_interval:
                    last_read = loop_start
                    rj, err = self.modbus.read_pose()
                    if err is None:
                        fail_count = 0
                        metrics.gauge("collector.read_fail_streak", 0)
                        self.connected = True
                        last_joints = rj
                        joints = rj
                        read_stale = False
                    else:
                        fail_count += 1
                        metrics.gauge("collector.read_fail_streak", fail_count)
                        self.connected = False
                        if fail_count >= FAIL_LIMIT:
                            log.warning("连续 %d 次读取失败: %s -> 临时降级模拟(将持续自动重试)",
                                        fail_count, err)
                            self.simulated = True
                            retry_at = loop_start + 3.0
                            emit_event("connection", "warn", "connection.degraded",
                                       f"连续 {fail_count} 次读取失败，临时降级为模拟",
                                       {"fail_count": fail_count, "error": str(err)[:200]})
                        else:
                            log.warning("Modbus 读取失败(%d/%d): %s", fail_count, FAIL_LIMIT, err)
                        # ★ 审计修复 P1-A6：读失败**不再补帧冒充真机数据**。
                        #   原实现 joints = simulate_pose(t) —— 用一条与机器人无关的
                        #   正弦扫掠顶上，画面继续"动"，操作员看到的是假姿态却毫无察觉。
                        #   现在：冻结在最后一次有效读数上，并打上 stale 标记
                        #   （前端据 stale 点亮黄色"数据陈旧"灯，见 P1-D7/D8）。
                        joints = last_joints
                        read_stale = True
                        sim_tracking = False
                else:
                    joints = last_joints
                    sim_tracking = False

            if self.simulated:
                read_stale = False           # 模拟数据本来就是"合成"的，不叫陈旧

            joints = [j * s for j, s in zip(joints, signs)]

            try:
                tcp = tcp_of(joints)
            except Exception as e:
                log.error("运动学计算失败: %s", e)
                tcp = (0.0, 0.0, 0.0)

            # ★ Stage C：WS 帧携带指令通道 —— 前端"模拟未走位时显示指令目标"、
            #   "走位中显示仿真体位"的判据就吃这三个字段（Stage A 已接好）。
            mst = motion.state()
            cmd = mst.get("last_target")
            cmd_tcp = None
            if cmd:
                try:
                    ct = tcp_of(cmd)
                    cmd_tcp = {"x": ct[0], "y": ct[1], "z": ct[2]}
                except Exception:
                    cmd_tcp = None
            tracking = bool(sim_tracking) if self.simulated else False

            # ★ E2 到位自证：用**本帧**实测喂观测器（stale 帧不喂 —— 拿冻结的旧姿态
            #   连续喂会伪装成"停止收敛"，把通讯故障说成机器不准：§5.5 铁律③）。
            #   顺带取一份紧凑快照并入下面的位姿帧（同屏同源，前端不再自己算判据）。
            if not read_stale:
                twin_observe(joints, loop_start)
            residual_seg = twin_residual.ws_view()

            payload = {
                "type": "pose",
                "j1": joints[0], "j2": joints[1], "j3": joints[2],
                "j4": joints[3], "j5": joints[4], "j6": joints[5],
                "tcp": {"x": tcp[0], "y": tcp[1], "z": tcp[2]},
                "simulated": self.simulated,
                # ★ P1-A6/P1-D7/D8：stale = 这一帧不是刚从控制器读到的真机数据
                #   （读失败时冻结上一帧）。at = 本帧产生时刻(epoch 秒)，供前端算新鲜度。
                "stale": bool(read_stale),
                "at": loop_start,
                "cmd": cmd,
                "cmd_tcp": cmd_tcp,
                "tracking": tracking,
                "t": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            }
            if residual_seg:
                # 只在"有在飞的段"时携带（不含曲线 —— 曲线走 REST，WS 不变成下载通道）
                payload["residual"] = residual_seg
            with self._lock:
                self.latest = payload
            hub.broadcast(payload)

            # ★ A1（方案·服务端独立评估）：后端**自己**每 0.2s 用实时关节算一遍围栏余量。
            #   为什么不能只信前端上报：脚本直连 /api/control/move、第三方客户端、浏览器停帧
            #   这三种情况下前端那一路是哑的，互锁就形同虚设（这正是 §5.5 铁律④的反例）。
            #   · stale 帧不评估 —— 拿冻结的旧姿态算出来的"安全"是假的；
            #   · 异常绝不能打断采集（外层重试逻辑兜的是连接故障，不是我们的评估崩）；
            #   · 默认 server_guard=shadow：只记录+可观测，不参与拦截（见 safety_guard）。
            if not read_stale and loop_start - last_guard_at >= 0.2:
                last_guard_at = loop_start
                try:
                    res = safety_evaluate(joints)
                    # ★ A5 取证环形缓冲：与后端评估同一节拍（0.2s）推一条实时样本。
                    #   note() 内部只 append、吞掉一切异常 —— 取证坏了不能带崩采集。
                    safety_note({
                        "at": loop_start,
                        "joints": list(joints),
                        "tcp": {"x": tcp[0], "y": tcp[1], "z": tcp[2]},
                        "simulated": self.simulated,
                        "stale": bool(read_stale),
                        "state": res.get("state"),
                        "zone_id": res.get("zone_id"),
                        "zone_name": res.get("zone_name"),
                        "clearance": res.get("clearance"),
                        "ground_mm": (res.get("ground") or {}).get("clearance_mm")
                        if isinstance(res.get("ground"), dict) else None,
                    })
                    safety_update_server(res)
                except Exception as e:
                    # 同一个错误 60s 内只报一次，别让坏配置把日志刷爆
                    if loop_start - guard_error_at >= 60.0:
                        guard_error_at = loop_start
                        log.warning("服务端围栏评估失败（已跳过，互锁退回前端上报口径）: %s", e)

            # rc-status：低频并入 WS 推流（省去前端一路 HTTP 轮询）
            if loop_start - last_rc_at >= RC_STATUS_INTERVAL:
                last_rc_at = loop_start
                self._publish_rc_status()

            # ★ P1-A6：stale 帧不入库 —— 冻结的重复姿态写进 pose 表只会污染历史曲线，
            #   让"回放"看起来像机器人一直停在原地不动（而真实情况是通讯断了）。
            if not read_stale:
                now = time.time()
                if now - self._last_db_write >= db_interval:
                    self._last_db_write = now
                    self._db_write(joints, tcp)

            elapsed = time.time() - loop_start
            metrics.stat("collector.loop_ms", elapsed * 1000.0)   # ★ B1 只读指标（含广播/入库段）
            sleep_t = interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _db_write(self, joints, tcp) -> None:
        s = SessionLocal()
        try:
            insert_pose(
                s, joints[0], joints[1], joints[2],
                joints[3], joints[4], joints[5], tcp,
                source="sim" if self.simulated else "real",
            )
        except Exception as e:
            log.error("入库失败: %s", e)
        finally:
            s.close()

    @staticmethod
    def _service_program() -> int:
        """点动服务程序号（现场=200/JOGSVC）；与 robot.py::rc_status 口径一致。"""
        try:
            return int(get_config().get("motion", "jog", "service_program", default=0) or 0)
        except Exception:
            return 0

    def _publish_rc_status(self) -> None:
        """低频（RC_STATUS_INTERVAL）把控制器寄存器快照广播进 WS，并缓存供握手即时推。

        与 /api/rc-status 字段保持一致，让前端 WS 帧与 HTTP 种子可共用一套解析。
        ★ 模拟/离线时不做真实读 —— rc_snapshot 会 TCP 连接，失败会超时阻塞采集循环。
        """
        t_now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        if self.simulated:
            payload = {"type": "rc_status", "ok": False, "simulated": True,
                       "error": "控制器模拟/离线，读不到寄存器快照",
                       "real_enabled": real_write_enabled(),
                       "service_program": self._service_program(), "t": t_now}
            self.latest_rc_status = payload
            hub.broadcast(payload)
            return
        snap, err = self.modbus.rc_snapshot()
        if snap is None:
            payload = {"type": "rc_status", "ok": False, "simulated": False,
                       "error": err or "读不到控制器",
                       "real_enabled": real_write_enabled(),
                       "service_program": self._service_program(), "t": t_now}
        else:
            snap["ok"] = True
            snap["real_enabled"] = real_write_enabled()
            snap["service_program"] = self._service_program()
            snap["ready"] = snapshot_ready(snap)
            # ★ 顺手把实测档位喂给 runmode：底栏「示教器」灯从此自动确认，
            #   不必再让操作员按旋钮位置手动声明一遍（见 services/runmode.py）。
            feed_run_mode(snap.get("mode"), snap.get("status_word"))
            payload = {"type": "rc_status", "t": t_now, **snap}
        self.latest_rc_status = payload
        hub.broadcast(payload)


def feed_run_mode(mode: Optional[str], status_word: Optional[int] = None) -> None:
    """控制器实测档位 → runmode（变化时留一条事件，便于事后追"谁把旋钮拨走了"）。

    模块级函数而不是 Collector 的方法：`/api/rc-status`（robot.py）也会在
    "采集器锁死模拟、但用户手动点了一下真机链路刷新"时读到真快照，需要走同一条路。
    """
    r = runmode.observe(mode or "")
    if r.get("changed"):
        emit_event("control", "info", "control.run_mode_observed",
                   f"控制器实测档位：{r['mode']}"
                   + (f"（原 {r['previous']}）" if r.get("previous") else ""),
                   {"mode": r["mode"], "previous": r.get("previous") or "",
                    "raw": mode, "status_word": status_word})


collector = Collector()
