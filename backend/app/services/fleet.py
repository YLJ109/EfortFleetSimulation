# -*- coding: utf-8 -*-
"""机房采集编排：12 条只读链路 + 一帧合批推送。

为什么一台一条线程，而不是一条线程轮询 12 台：
  六台埃夫特是**轮询型**（每台 20Hz 一发 FC3，谁超时都不该拖累别人），
  六台华沿是**推送型**（连上就自己来包，recv 必须阻塞等）。
  两种节奏塞进一条线程，后果是一台卡网的机器人把 12 台的画面一起冻住 ——
  这正是本项目审计里 P1-A6 反复强调"画面看着正常、数据不动"最危险的形态。

★ 本模块只读：只调 driver.read()，不碰任何写通道。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from app.core.fleet_config import FleetConfig, get_fleet
from app.core.logger import get_logger
from app.db.crud import insert_pose
from app.db.database import SessionLocal
from app.services.drivers import RobotFrame, build_driver
from app.core.safety_const import SPEED_MIN
from app.services.safety_guard import check as guard_check
from app.services.events import emit
from app.services.hub import hub

log = get_logger("fleet")

# 机房下发的速度上限：真机红线要求"最慢档"，这里就是 5%。
# 想放开必须显式改这一行，不在前端给一个能拖到 100 的滑块。
SPEED_MIN_FLEET = SPEED_MIN


class Station:
    """一台机器人的采集状态。一条线程独占写入，读侧只取快照。"""

    def __init__(self, entry: Dict[str, Any], driver) -> None:
        self.entry = entry
        self.driver = driver
        self.frame: Optional[RobotFrame] = None
        self.lock = threading.Lock()
        self.fails = 0
        self.last_db_at = 0.0
        self.poll_interval = 0.05
        self.running = threading.Event()

    # ---- 读侧 ----
    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            f = self.frame
        e = self.entry
        # ★ zone/group/slot 三个都得带上：前端的作用域分页（哪一页看哪几台）
        #   与卡片短代号（A1…A6）全靠它们。少一个的后果不是报错，而是
        #   "12 台全掉进一个未知分组"——采集一启动就复现，不启动反而正常。
        if f is None:
            return {"id": e["id"], "name": e["name"], "zone": e["zone"],
                    "group": e.get("group") or "", "slot": e.get("slot"),
                    "order": e["order"], "model": e["model"], "brand": e["brand"],
                    "host": e["host"], "port": e["port"], "pos": e["pos"],
                    "online": False, "stale": False, "simulated": False,
                    "joints": [0.0] * 6, "status": {}, "error": "还没读到第一帧"}
        return {"id": e["id"], "name": e["name"], "zone": e["zone"],
                "group": e.get("group") or "", "slot": e.get("slot"),
                "order": e["order"], "model": e["model"], "brand": e["brand"],
                "host": e["host"], "port": e["port"], "pos": e["pos"],
                **f.as_dict()}

    @property
    def id(self) -> str:
        return self.entry["id"]


class FleetManager:
    def __init__(self, cfg: Optional[FleetConfig] = None) -> None:
        self.cfg = cfg or get_fleet()
        self.stations: List[Station] = []
        self._threads: List[threading.Thread] = []
        self._running = False
        self._batch_thread: Optional[threading.Thread] = None
        self.started_at = 0.0
        self._db_interval = 2.0

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.robots)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._running:
            return True
        self._running = True
        self.started_at = time.time()
        poll_ms = {}
        for name in ("efort_modbus", "huayan_datasheet"):
            poll_ms[name] = float(self.cfg.driver_params(name).get("poll_ms", 50))
        db_hz = float(self.cfg.streaming.get("db_write_hz", 0.5))
        self._db_interval = 1.0 / max(db_hz, 0.05)

        for entry in self.cfg.robots:
            merged = dict(self.cfg.driver_params(entry["driver"]))
            # host/port 取**登记表算出来的生效值**（单台覆盖 > 驱动段默认，
            # 见 core/fleet_config._validate）。这里若继续从驱动段拿端口，
            # 机房设置页改的端口就只是"存下来了但没人用"。
            merged.update({"host": entry["host"], "port": int(entry["port"])})
            drv_name = entry["driver"]
            if self.cfg.simulate:
                # 仿真：不建任何 TCP 连接，host/port 一律忽略。
                # brand 传下去，状态位才按各牌子的真实字段名生成（卡片文案不会串台）；
                # model 传下去，合成帧的 TCP 才拿得到**同一型号**的正解档案
                # （华沿是 URDF 关节链，埃夫特是 DH —— 两套算法不能串用）。
                merged = dict(merged)
                merged["brand"] = entry.get("brand") or ""
                merged["model"] = entry.get("model") or ""
                merged["order"] = entry.get("order")
                drv_name = "sim_swing"
            drv = build_driver(entry["id"], drv_name, merged)
            st = Station(entry, drv)
            st.poll_interval = poll_ms.get(entry["driver"], 50) / 1000.0
            self.stations.append(st)
            th = threading.Thread(target=self._run_station, args=(st,),
                                  name="fleet-%s" % entry["id"], daemon=True)
            th.start()
            self._threads.append(th)

        self._batch_thread = threading.Thread(target=self._batch_loop,
                                              name="fleet-batch", daemon=True)
        self._batch_thread.start()
        if self.cfg.simulate:
            log.warning("机房采集运行在**仿真模式**（fleet.simulate=true）："
                        "%d 台全部为合成帧，未连接任何设备", len(self.stations))
        log.info("机房采集启动：%d 台（埃夫特 %d / 华沿 %d），合批推送 %dms",
                 len(self.stations),
                 sum(1 for s in self.stations if s.entry["brand"] == "EFORT"),
                 sum(1 for s in self.stations if s.entry["brand"] == "HUAYAN"),
                 int(self.cfg.streaming.get("batch_interval_ms", 50)))
        return True

    # ------------------------------------------------------------------
    def reload(self) -> Dict[str, Any]:
        """机房设置保存后重建采集：停掉当前全部链路，按**新登记表**再起一遍。

        ★ 为什么整片重建，而不是逐台热改：型号/区/排/序号一变，采集线程名、
          驱动种类、地面坐标、作用域成员名单全跟着变；逐台改会留下"半新半旧"的
          链路 —— 那种状态出问题时既不是新配置也不是旧配置，最难排查。
        ★ 起不来就照实说（started=False + error），界面要显示"配置已保存但采集
          没起来"，而不是显示"已生效"。
        """
        was_running = self._running
        self.stop()
        self.cfg = get_fleet(reload=True)
        out: Dict[str, Any] = {"was_running": was_running,
                               "stations": len(self.cfg.robots),
                               "simulate": bool(self.cfg.simulate),
                               "started": False}
        try:
            out["started"] = bool(self.start())
        except Exception as e:
            log.error("机房采集重建失败：%s", e)
            out["error"] = str(e)[:200]
        return out

    # ------------------------------------------------------------------
    def move(self, robot_id: str, joints: List[float], speed_pct: float,
             actor: str = "", scope: str = "") -> Dict[str, Any]:
        """机房"执行"：把一帧目标关节角交给某一台。

        ★ 现在只接**仿真**那一条路。真机分支一律明确拒绝，不做"看起来执行了"：
          没接设备、也没做过任何寄存器级验证的情况下悄悄打开写通道，
          是这个项目最不该发生的一类事故（见 docs 交接文档的真机红线）。
        """
        st = next((x for x in self.stations if x.id == robot_id), None)
        if st is None:
            return {"ok": False, "code": 404, "reason": "机房名单里没有这台：%s" % robot_id}
        if not self._running:
            return {"ok": False, "code": 503, "reason": "机房采集未启动，没有可下发的通道"}
        q = [float(j) for j in joints]
        if len(q) != 6 or any(v != v for v in q):      # NaN 自检
            return {"ok": False, "code": 400, "reason": "目标姿态必须是 6 个有限角度"}
        # 软限位：越界直接拒，不夹紧 —— 夹紧等于替操作员改了他的目标
        md = self.cfg.model_profile(st.entry.get("model") or {}).get("model_data") or {}
        # 两个牌子的限位键名不同（埃夫特 robot.yaml 用 joint_limits，
        # 华沿 huayan_elfin.yaml 用 joint_limits_display）—— 两个都认，
        # 但一个都没有时必须走下面的"无限位档案"分支，不能当作"随便动"。
        limits = md.get("joint_limits_display") or md.get("joint_limits") or []
        for i, lim in enumerate(limits[:6]):
            lo, hi = float(lim.get("min", -1e9)), float(lim.get("max", 1e9))
            if q[i] < lo or q[i] > hi:
                return {"ok": False, "code": 400,
                        "reason": "%s 超出软限位：J%d=%.2f° 允许 %.2f°~%.2f°"
                                  % (st.entry.get("name"), i + 1, q[i], lo, hi)}
        # ★ P6 按台化围栏互锁：只看**这一台**的实时围栏状态。
        #   机房层原本完全没有这道闸 —— 单机那条路（/api/control/*）会过
        #   safety_guard.check()，而大屏的 /fleet/{id}/move 直接落到了驱动上。
        #   12 台挤在一间屋里，"隔壁撞围栏"不该拦这台，但"这台要撞围栏"必须拦。
        #   上报侧按 robot_id 存（见 api/safety.py 的 items），所以这里能拿到分台的状态。
        allowed, why, _snap = guard_check(robot_id)
        if not allowed:
            log.warning("拒绝下发 %s：围栏互锁（%s）", robot_id, why)
            emit("safety", "error", "fleet.move.blocked",
                 "%s 下发被围栏互锁拦下：%s" % (st.entry.get("name") or robot_id, why),
                 detail={"robot_id": robot_id, "reason": why}, actor=actor or "system")
            return {"ok": False, "code": 409, "reason": why}
        # ★ 机房下发一律锁最慢档，不接受前端传上来的速度：
        #   交接文档的真机红线写的是"锁定最慢档"，而单机那套 SPEED_MIN 只是下限。
        #   12 台同时可动的场景里，"操作员自己判断跑多快"这个前提还不成立
        #   （连一台都没验证过），所以这里收紧，放开要显式改这一行。
        sp = SPEED_MIN_FLEET
        setter = getattr(st.driver, "set_target", None)
        speed_out = float(sp)
        if setter is not None and getattr(st.driver, "simulated_ok", False):
            setter(q)
            channel = "sim"
        elif getattr(st.driver, "joint_move_enabled", lambda: False)():
            # P5 华沿真机写通道（10003 ASCII）。**默认关**：开着它才走这一支。
            # 真机埃夫特那条路（P4c）还没有寄存器级验证，仍然落下面的 501。
            res = st.driver.joint_move(q, sp)
            if not res.get("ok"):
                emit("control", "error", "fleet.move.rejected",
                     "%s 下发被控制器拒绝：%s"
                     % (st.entry.get("name") or robot_id, res.get("reason") or ""),
                     detail={"robot_id": robot_id, "scope": scope,
                             "reason": res.get("reason")}, actor=actor or "system")
                return {"ok": False, "code": 502, "reason": res.get("reason") or "控制器拒绝"}
            channel = "huayan_10003"
            speed_out = float(res.get("speed_pct") or sp)
        else:
            log.warning("拒绝真机下发 %s：写通道未接入（三闸未开/驱动不支持）", robot_id)
            return {"ok": False, "code": 501,
                    "reason": "这台是真机链路，写通道尚未启用"
                              "（华沿需 fleet.yaml: command_write.enabled=true；"
                              "埃夫特需现场验证，见 P4c）"}
        emit("control", "warn", "fleet.move",
             "%s 执行关节目标（%s，%d%%，经 %s）"
             % (st.entry.get("name") or robot_id, scope or "single", int(sp), channel),
             detail={"robot_id": robot_id, "scope": scope, "joints": [round(v, 3) for v in q],
                     "speed_pct": sp, "simulated": channel == "sim", "channel": channel},
             actor=actor or "system")
        return {"ok": True, "robot_id": robot_id, "speed_pct": speed_out,
                "simulated": channel == "sim", "channel": channel,
                "message": ("已下发（仿真）：%s" % (st.entry.get("name") or robot_id)
                            if channel == "sim"
                            else "已下发（真机 10003）：%s" % (st.entry.get("name") or robot_id))}

    def stop(self) -> None:
        self._running = False
        for st in self.stations:
            st.running.clear()
            try:
                st.driver.close()
            except Exception:
                pass
        self.stations = []
        self._threads = []

    # ------------------------------------------------------------------
    def _run_station(self, st: Station) -> None:
        st.running.set()
        try:
            st.driver.open()
        except Exception as e:
            st.fails += 1
            log.warning("%s 连接失败：%s", st.id, e)
        while self._running and st.running.is_set():
            t0 = time.time()
            try:
                f = st.driver.read(0.2)
            except Exception as e:                    # 驱动自己不该抛，抛了也不能死线程
                f = RobotFrame(robot_id=st.id, online=False, stale=st.frame is not None,
                               ts=time.time(), error="采集异常: %s" % str(e)[:160])
                log.warning("%s 采集异常: %s", st.id, e)
            with st.lock:
                st.frame = f
            if f.online:
                st.fails = 0
                self._maybe_db(st, f)
            else:
                st.fails += 1
                # 连续失败说明链路本身坏了（华沿推送断开后不会自己长回来），
                # 每 20 次关一次重开；其余时间只做低频重试，别把控制器问烦。
                if st.fails % 20 == 0:
                    try:
                        st.driver.close()
                        st.driver.open()
                    except Exception as e:
                        log.debug("%s 重开连接失败: %s", st.id, e)
            if not st.driver.push_mode:
                sleep_t = st.poll_interval - (time.time() - t0)
                if sleep_t > 0:
                    time.sleep(sleep_t)
        try:
            st.driver.close()
        except Exception:
            pass

    def _maybe_db(self, st: Station, f: RobotFrame) -> None:
        if f.tcp is None:
            return          # TCP 未知的行会污染历史曲线（看起来像机器人缩回原点了）
        now = time.time()
        if now - st.last_db_at < self._db_interval:
            return
        st.last_db_at = now
        try:
            s = SessionLocal()
        except Exception as e:
            # 库没就绪/打不开时只丢历史，采集线程必须活着 ——
            # 画面是操作员的实况，历史曲线晚点补得回来，界面不能因此变黑。
            log.warning("%s 数据库不可用，暂停入库: %s", st.id, e)
            return
        try:
            insert_pose(s, *f.joints, tuple(f.tcp),
                        source="sim" if f.simulated else "real",
                        robot_id=st.id)
        except Exception as e:
            log.error("%s 入库失败: %s", st.id, e)
        finally:
            s.close()

    # ------------------------------------------------------------------
    def _batch_loop(self) -> None:
        interval = float(self.cfg.streaming.get("batch_interval_ms", 50)) / 1000.0
        status_interval = float(self.cfg.streaming.get("status_interval_ms", 1000)) / 1000.0
        next_status_at = 0.0
        while self._running:
            t0 = time.time()
            robots = [st.snapshot() for st in self.stations]
            hub.broadcast({"type": "fleet_pose", "robots": robots, "at": t0})
            if t0 >= next_status_at:
                next_status_at = t0 + status_interval
                hub.broadcast({"type": "fleet_status", "at": t0,
                               "summary": self.summary(robots)})
            sleep_t = interval - (time.time() - t0)
            if sleep_t > 0:
                time.sleep(sleep_t)

    def summary(self, robots: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        robots = robots if robots is not None else [st.snapshot() for st in self.stations]
        online = [r for r in robots if r.get("online")]
        alarm = [r for r in robots if r.get("status", {}).get("alarm")
                 or r.get("status", {}).get("error_code")]
        moving = [r for r in robots if r.get("status", {}).get("moving")
                  or r.get("status", {}).get("running")]
        return {
            "total": len(robots),
            "online": len(online),
            "offline": len(robots) - len(online),
            "alarm": len(alarm),
            "moving": len(moving),
            "by_zone": {z: {
                "total": sum(1 for r in robots if r["zone"] == z),
                "online": sum(1 for r in robots if r["zone"] == z and r.get("online")),
            } for z in ("A", "B")},
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
        }

    def latest(self) -> Dict[str, Any]:
        """给 HTTP 兜底（WS 掉线时界面仍能拉到一帧）。"""
        robots = [st.snapshot() for st in self.stations]
        return {"enabled": True, "summary": self.summary(robots), "robots": robots}

    def one(self, robot_id: str) -> Optional[Dict[str, Any]]:
        for st in self.stations:
            if st.id == robot_id:
                return st.snapshot()
        return None


fleet = FleetManager()
