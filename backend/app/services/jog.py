# -*- coding: utf-8 -*-
"""
点动引擎 (Jog)：示教器式的 J1~J6 手动点动。

参考工业机器人示教器的通行做法（ISO 10218 / T1 教学模式的思路）：
  - 增量点动 (step)：按一次走固定角度(如 0.1/1/5/10°)，便于精密对位；
  - 连续点动 (hold)：按住期间以设定角速度(°/s)持续走，松开立即停；
  - 死人开关 / 看门狗：连续点动期间前端必须周期性发 keepalive，
    超过 watchdog_ms 未收到即自动停止 —— 对应示教器"三段使能开关"松开即停；
  - 任何时刻：急停锁定 / 安全围栏 danger|hit / 关节限位到达 → 立即停止。

★ 本模块本身不下发，统一经 motion.command()，因此真实下发仍受
  EFORT_REAL_MOTION + motion.real_write 双确认约束。
"""
from __future__ import annotations

import time
from threading import Lock, Thread
from typing import List, Optional

from app.core.config import get_config
from app.core.logger import get_logger
from app.services import jog_frames as jf
from app.services.collector import collector
# ★ 审计修复 P1-E1：限位读取的唯一实现在 services/limits.py。
#   本文件原先有 _limits() / _limit_dicts() 两份（tuple 与 dict 各一份，
#   还各写各的兜底），现在都只做转发 —— 判据只可能有一个。
from app.services.limits import load_limits, load_ranges
from app.services.motion import motion
from app.services.runmode import runmode
from app.services.safety_guard import check as guard_check

log = get_logger("jog")


def _cfg(key: str, default):
    return get_config().get("motion", "jog", key, default=default)


class JogEngine:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active = False
        self._thread: Optional[Thread] = None
        self._joint = 0            # 1..6
        self._dir = 1              # +1 / -1
        self._speed = 15.0         # 关节系 °/s；直角系 mm/s（旋转轴 °/s）
        self._target: List[float] = [0.0] * 6
        self._deadline = 0.0       # 看门狗到期时间(monotonic)
        self._started_at = 0.0
        self._last_reason = ""
        self._last_tick_at = 0.0
        # 坐标系（阶段 6）：缺省 joint ⇒ 与旧行为逐字节一致，老脚本/旧前端不受影响
        self._frame = "joint"
        self._user_frame = ""
        self._slow = False
        self._speed_scale = 1.0    # 最近一次 tick 的实际限速比例（1 = 未限速）

    # ---------- 配置 / 工具 ----------
    @property
    def max_speed(self) -> float:
        try:
            return float(_cfg("max_speed_dps", 30) or 30)
        except Exception:
            return 30.0

    @property
    def watchdog_ms(self) -> int:
        try:
            return int(_cfg("watchdog_ms", 1500) or 1500)
        except Exception:
            return 1500

    @property
    def tick_ms(self) -> int:
        try:
            return max(40, int(_cfg("tick_ms", 100) or 100))
        except Exception:
            return 100

    def _limits(self) -> List[tuple]:
        # ★ 审计修复 P1-E1：见 services/limits.py。原实现在"配置条数 < 6"时
        #   不补默认值，`limits[j - 1]` 会直接 IndexError（点动按钮 500）。
        #   现在长度恒为 6，且与 control / vision / motion 读的是同一份配置解析。
        return load_ranges()

    def _limit_dicts(self) -> List[dict]:
        """关节限位（dict 形式）—— 直角求解器（jog_frames）要带名字，便于报"哪个轴到限位了"。"""
        # ★ 审计修复 P1-E1：与 _limits() 同源，只是形态不同（多一个 name）。
        return load_limits()

    # ---------- 直角系速度上限（单位按轴）----------
    @property
    def max_axis_speed(self) -> float:
        """直角系平移轴速度上限（mm/s）。默认 50 = 官方 T1 档的上限。"""
        try:
            return max(1.0, float(_cfg("max_speed_mmps", jf.DEFAULT_MAX_MMPS) or jf.DEFAULT_MAX_MMPS))
        except Exception:
            return jf.DEFAULT_MAX_MMPS

    @property
    def max_rot_speed(self) -> float:
        """直角系旋转轴（A/B/C）速度上限（°/s）。"""
        try:
            return max(1.0, float(_cfg("max_speed_rot_dps", jf.DEFAULT_MAX_ROT_DPS) or jf.DEFAULT_MAX_ROT_DPS))
        except Exception:
            return jf.DEFAULT_MAX_ROT_DPS

    @property
    def slow_ratio(self) -> float:
        """慢速模式倍率（官方示教器：慢速 = 速度 ÷10）。"""
        try:
            return max(1.0, float(_cfg("slow_ratio", 10) or 10))
        except Exception:
            return 10.0

    def axis_speed_limit(self, frame: str, axis: int) -> float:
        if frame == "joint":
            return self.max_speed
        return self.max_axis_speed if int(axis) <= 3 else self.max_rot_speed

    def axis_speed(self, frame: str, axis: int, speed: float, slow: bool = False) -> float:
        """把请求速度夹到该轴上限；慢速模式再 ÷slow_ratio。"""
        v = max(1e-3, abs(float(speed)))
        v = min(v, self.axis_speed_limit(frame, axis))
        if slow:
            v = max(1e-3, v / self.slow_ratio)
        return v

    def _current(self) -> List[float]:
        """当前基准姿态：优先执行引擎最近目标，其次实时采集姿态。"""
        st = motion.state()
        if st.get("last_target"):
            return [float(v) for v in st["last_target"]]
        p = collector.get_latest()
        if p:
            return [float(p[f"j{i}"]) for i in range(1, 7)]
        return [0.0] * 6

    def _speed_pct(self, speed_dps: float) -> int:
        """°/s → %（**引擎内部用**：把每拍的角速度换算成下发给 40103 的百分比）。

        ★ 与 `_dps_est` 互为逆函数；因 max_speed 作为 100% 参考，两者往返是恒等的
          （pct → 0.3×pct °/s → pct），所以"用户设的 % == 下发 40103 的 %"。
        """
        pct = int(round(speed_dps / max(self.max_speed, 1e-6) * 100))
        return max(1, min(100, pct))

    def _dps_est(self, speed_pct: int) -> float:
        """% → 估算 °/s（**仅用于耗时估算/显示，不参与下发**）。

        ★ 速度的唯一权威口径是**百分比**（与右上角全局速度一致）：
          有效速度 = 程序内 v…perc × 40103。控制器不反馈"每秒多少度"，
          故按 config motion.jog.max_speed_dps（默认 30 = 现场 100% 的参考角速度）
          线性折算，纯估算。2026-09-29 起点动入参改为 speed_pct ——
          旧实现让"角速度 5°/s"经 5/30→17% 再被 v50perc 反算成 40103=34%，
          造成"示教器显示 34%、右上角 5%"的口径分裂。
        """
        pct = max(1, min(100, int(speed_pct)))
        return max(0.1, self.max_speed * pct / 100.0)

    def _block_reason(self) -> str:
        """返回非空字符串表示禁止点动。"""
        if motion.state().get("stopped"):
            return "急停已触发，请先复位急停"
        ok, reason, _snap = guard_check()
        if not ok:
            return f"安全围栏互锁：{reason}"
        # 示教器档位：声明为 AUTO/REMOTE 时控制器会拒绝手动点动，软件层先说清楚。
        # ★ 未声明时放行（否则真机联调前完全没法用），只在事件里留痕 ——
        #   与围栏互锁"未上报则放行但记 warn"保持同一策略。
        mok, mwhy, _mst = runmode.check_jog()
        if not mok:
            return mwhy
        return ""

    def mode_warning(self) -> str:
        """非致命的模式提示（未声明/已过期档位时给前端一句提醒）。"""
        st = runmode.state()
        if st["confirmed"]:
            return ""
        if st["stale"]:
            return f"示教器模式声明已过期（上次声明 {st['claimed_mode']}），请重新声明"
        return "尚未声明示教器档位（T1/T2/AUTO/REMOTE），点动会被登记为『模式未声明』"

    # ---------- 增量点动 ----------
    def step(self, joint: int, direction: int, angle_deg: float,
             speed_pct: int, frame: str = "joint",
             user_frame: Optional[str] = None, slow: bool = False) -> dict:
        """按一次走固定距离/角度，返回执行结果(含目标姿态与预计耗时)。

        ★ `frame` 缺省 "joint" ⇒ 走原有关节路径，**行为与升级前逐字节一致**；
          直角系（base/tool/user）才进求解器。向后兼容是硬要求：
          旧脚本/旧前端不传 frame 时语义不能漂移。
        """
        f, ferr = jf.norm_frame(frame)
        if ferr:
            return {"ok": False, "error": ferr}
        j = int(joint)
        if j < 1 or j > 6:
            return {"ok": False, "error": "轴号须在 1~6（关节系为 J1~J6，直角系为 X/Y/Z/A/B/C）"}
        why = self._block_reason()
        if why:
            return {"ok": False, "error": why, "blocked": True}

        d = 1 if direction >= 0 else -1
        amt = abs(float(angle_deg))
        self.stop("increment")            # 连续点动与增量点动互斥
        with self._lock:
            self._frame, self._user_frame, self._slow = f, str(user_frame or ""), bool(slow)

        # ★ 速度口径统一：入参是**百分比**（右上角全局速度）。引擎内部按 °/s 计算每拍位移，
        #   故在此**单点换算一次**（_dps_est 只做参考折算，权威值仍是 %）。
        dps = self._dps_est(speed_pct)
        if f == "joint":
            return self._step_joint(j, d, amt, dps)
        return self._step_cartesian(f, j, d, amt, dps, user_frame, slow)

    def _step_joint(self, j: int, d: int, ang: float, speed_dps: float) -> dict:
        sp = max(0.1, min(float(speed_dps), self.max_speed))
        limits = self._limits()
        q = self._current()
        lo, hi = limits[j - 1]
        target = q[:]
        target[j - 1] = q[j - 1] + d * ang
        clamped = False
        if target[j - 1] < lo:
            target[j - 1] = lo
            clamped = True
        elif target[j - 1] > hi:
            target[j - 1] = hi
            clamped = True

        duration_ms = int(round(ang / sp * 1000))
        res = motion.command([float(v) for v in target], self._speed_pct(sp), 0,
                             source="点动J%d" % j)
        out = {
            "ok": bool(res.get("ok")),
            "mode": res.get("mode"),
            "frame": "joint",
            "unit": "deg",
            "joint": j,
            "dir": d,
            "angle_deg": round(ang, 3),
            "speed_pct": self._speed_pct(sp),   # ★ 权威口径（== 右上角速度）
            "speed_dps_est": round(sp, 2),      # 仅估算，供参考/耗时
            "duration_ms": duration_ms,
            "target": [round(float(v), 3) for v in target],
            "limit_clamped": clamped,
            "error": res.get("error"),
        }
        if clamped:
            out["warnings"] = [f"J{j} 已到限位 [{lo}, {hi}]，实际停在 {target[j - 1]:.2f}°"]
        return out

    def _step_cartesian(self, frame: str, axis: int, d: int, amount: float,
                        speed: float, user_frame: Optional[str], slow: bool) -> dict:
        """直角系增量点动：坐标系方向 + 单轴增量 → 反解关节角 → 下发。

        ★ 增量**不做步长缩减**：用户点名要走这么多，解不出来就如实失败并给奇异提示，
          悄悄少走一点会让人以为走对了（对位场景下这很危险）。
        """
        sp = self.axis_speed(frame, axis, speed, slow)
        unit = jf.unit_of(frame, axis)
        r = jf.plan_step(self._current(), frame, axis, d, amount, sp,
                         user_frame, self._limit_dicts(), self.max_speed)
        if not r.get("ok"):
            return {
                "ok": False, "frame": frame, "unit": unit, "axis": axis, "dir": d,
                "amount": round(amount, 4), "requested": round(amount, 4),
                "singular": r.get("singular") or "",
                "error": r.get("singular") or r.get("reason") or "直角求解失败",
                "blocked": False,
            }
        target = [float(v) for v in r["joints"]]
        res = motion.command(target, self._speed_pct(r["need_dps"]), 0,
                             source="点动(直角)")
        # ★ 不能用 `or 1.0`：speed_scale=0.0（完全限速）会被 or 吞掉变 1.0
        raw_scale = r.get("speed_scale")
        scale = float(raw_scale) if raw_scale is not None else 1.0
        out = {
            "ok": bool(res.get("ok")),
            "mode": res.get("mode"),
            "frame": frame, "unit": unit, "axis": axis, "dir": d,
            "amount": round(amount, 4),
            "speed": round(sp, 3), "speed_unit": unit + "/s",
            "slow": bool(slow),
            "duration_ms": int(r.get("duration_ms") or 0),
            "target": [round(v, 3) for v in target],
            "limit_clamped": bool(r.get("clamped")),
            "speed_scale": round(scale, 4),
            "limited": scale < 0.999,
            "need_dps": r.get("need_dps"),
            "tcp_before": r.get("tcp_before"),
            "tcp_after": r.get("tcp_after"),
            "pos_err_mm": r.get("pos_err_mm"),
            "rot_err_deg": r.get("rot_err_deg"),
            "error": res.get("error"),
        }
        warn: List[str] = []
        if r.get("clamped"):
            hs = r.get("hit_limit") or []
            names = "、".join(h["joint"] for h in hs)
            warn.append("直角目标超出关节限位，已夹紧到限位：" + (names or "已夹紧到关节限位"))
        if scale < 0.999:
            warn.append(f"关节速度受限，实际只走到请求速度的 {scale * 100:.0f}%")
        if warn:
            out["warnings"] = warn
        return out

    # ---------- 连续点动 ----------
    def start(self, joint: int, direction: int, speed_pct: int,
              frame: str = "joint", user_frame: Optional[str] = None,
              slow: bool = False) -> dict:
        f, ferr = jf.norm_frame(frame)
        if ferr:
            return {"ok": False, "error": ferr}
        j = int(joint)
        if j < 1 or j > 6:
            return {"ok": False, "error": "轴号须在 1~6（关节系为 J1~J6，直角系为 X/Y/Z/A/B/C）"}
        why = self._block_reason()
        if why:
            return {"ok": False, "error": why, "blocked": True}

        with self._lock:
            d = 1 if direction >= 0 else -1
            # ★ 同 step：入口是 %，此处单点换算成引擎内部用的 °/s
            sp = self.axis_speed(f, j, self._dps_est(speed_pct), slow)
            switching = self._active and (self._joint != j or self._dir != d
                                          or self._frame != f
                                          or self._user_frame != str(user_frame or ""))
            if switching:
                self._stop_locked("switch")       # 换轴/换向/换坐标系必须先停
            self._joint = j
            self._dir = d
            self._speed = sp
            self._frame = f
            self._user_frame = str(user_frame or "")
            self._slow = bool(slow)
            self._speed_scale = 1.0
            self._target = self._current()
            self._deadline = time.monotonic() + self.watchdog_ms / 1000.0
            self._started_at = time.monotonic()
            self._last_reason = ""
            self._active = True
            if self._thread is None or not self._thread.is_alive():
                self._thread = Thread(target=self._loop, name="efort-jog", daemon=True)
                self._thread.start()
        return {"ok": True, "action": "start", **self.state()}

    def keepalive(self) -> dict:
        """死人开关保持信号：刷新看门狗；未点动时调用返回当前状态。"""
        with self._lock:
            if self._active:
                self._deadline = time.monotonic() + self.watchdog_ms / 1000.0
                # ★ 必须用 _state_locked()：self.state() 会再次抢 self._lock，
                #   而 threading.Lock 不可重入 → 同线程二次获取直接死锁。
                return {"ok": True, "action": "keepalive", **self._state_locked()}
        return {"ok": False, "action": "keepalive", "error": "当前没有进行中的点动",
                **self.state()}

    def stop(self, reason: str = "user") -> dict:
        with self._lock:
            was = self._active
            self._stop_locked(reason)
        return {"ok": True, "action": "stop", "stopped_motion": was, **self.state()}

    # ★ 审计修复 P1-A10：交给 motion.command 的中止判据（死人开关/看门狗/松手停止）。
    #   只做裸读 bool —— 回调发生在**已持有 motion._exec_lock 的下发线程**里，
    #   若这里去抢 jog._lock，一旦别的线程同时"持 jog._lock 调 motion.command"
    #   就会锁序打架；bool 赋值在 CPython 下是原子的，裸读足够。
    def _stopped_for_abort(self) -> bool:
        return not self._active

    def _stop_locked(self, reason: str) -> None:
        self._active = False
        self._deadline = 0.0
        self._last_reason = reason
        if reason in ("user", "watchdog", "estop", "fence", "limit"):
            # 真正停下来了：坐标系与限速比例一并复位，避免界面上还挂着
            # 上一次的"工具坐标系 / 已限速 40%"残留（下一次 start 会重新设）。
            self._joint = 0
            self._speed_scale = 1.0

    # ---------- 主循环 ----------
    def _loop(self) -> None:
        tick = self.tick_ms / 1000.0
        try:
            while True:
                with self._lock:
                    if not self._active:
                        break
                    if time.monotonic() > self._deadline:
                        self._stop_locked("watchdog")
                        log.warning("点动看门狗超时，已自动停止（未收到 keepalive）")
                        break
                    j, d, sp = self._joint, self._dir, self._speed
                    frame, uf = self._frame, self._user_frame
                    q = self._target[:]
                    limits = self._limits()
                    # ★ 急停判断收进锁内快照：原来在锁外读 motion.state() 再回头
                    #   加锁，中间窗口里急停已置位而本拍仍会下发目标角。
                    #   motion.state() 只抢 motion._lock；motion 从不反向抢
                    #   jog._lock（见 motion.py，无 jog 引用），无死锁风险。
                    if motion.state().get("stopped"):
                        self._stop_locked("estop")
                        log.warning("点动期间急停触发，已停止")
                        break

                ok, reason, _snap = guard_check()
                if not ok:
                    with self._lock:
                        self._stop_locked("fence")
                        self._last_reason = reason
                    log.warning("点动期间围栏进入危险状态，已停止：%s", reason)
                    break

                if frame == "joint":
                    # ================= 关节系（与升级前逻辑一致）=================
                    lo, hi = limits[j - 1]
                    nxt = q[j - 1] + d * sp * tick
                    hit_limit = False
                    if nxt < lo:
                        nxt, hit_limit = lo, True
                    elif nxt > hi:
                        nxt, hit_limit = hi, True
                    q[j - 1] = nxt
                    # ★ 审计修复 P1-A10：点动下发必须可被"停止"打断。
                    #   rc_jog_execute 同步阻塞最长 30s，只靠急停中断的话，
                    #   看门狗超时/松手停止要等这一发走完才停（远超 watchdog_ms=1500）。
                    #   判据用裸读 bool：**不能在回调里抢 jog._lock**，
                    #   否则与"持有 _exec_lock 的下发线程"形成锁序耦合。
                    res = motion.command([float(v) for v in q], self._speed_pct(sp), 0,
                                         abort_check=self._stopped_for_abort,
                                         source="点动J%d" % j)
                    scale = 1.0
                    limit_names = [f"J{j}"]
                else:
                    # ================= 直角系 =================
                    # plan_tick 负责"缩步长 + 限速"，保证每拍目标都在一个周期内可完成
                    # （否则目标会跑在伺服前面，松手后机器人还在走）。
                    r = jf.plan_tick(q, frame, j, d, sp, tick, uf,
                                     self._limit_dicts(), self.max_speed)
                    if not r.get("ok"):
                        with self._lock:
                            self._stop_locked("singular" if r.get("singular") else "error")
                            self._last_reason = (r.get("singular") or r.get("reason")
                                                 or "直角求解失败")
                        log.warning("直角点动求解失败，已停止：%s", self._last_reason)
                        break
                    q = [float(v) for v in r["joints"]]
                    res = motion.command(q, self._speed_pct(r["need_dps"]), 0,
                                         abort_check=self._stopped_for_abort,
                                         source="点动(直角)")
                    scale = float(r.get("scale") or 1.0)
                    hit_limit = bool(r.get("hit_limit"))
                    limit_names = [h["joint"] for h in (r.get("hit_limit") or [])]

                with self._lock:
                    self._target = q[:]
                    self._last_tick_at = time.monotonic()
                    self._speed_scale = scale
                    if not res.get("ok"):
                        self._stop_locked("error")
                        self._last_reason = res.get("error") or "下发失败"
                    elif hit_limit:
                        self._stop_locked("limit")
                        axes = "、".join(limit_names) if limit_names else "关节"
                        self._last_reason = f"{axes} 已到限位"
                if hit_limit or not res.get("ok"):
                    break
                time.sleep(tick)
        except Exception as e:      # 兜底：线程内异常也必须退出并停机
            log.error("点动线程异常: %s", e)
            with self._lock:
                self._stop_locked("error")
                self._last_reason = str(e)

    # ---------- 状态 ----------
    def _state_locked(self) -> dict:
        """构造状态快照。★ 调用方必须已持有 self._lock（本函数不再加锁，避免不可重入死锁）。"""
        elapsed = int((time.monotonic() - self._started_at) * 1000) if self._active else 0
        left = max(0, int((self._deadline - time.monotonic()) * 1000)) if self._active else 0
        return {
            "active": self._active,
            "joint": self._joint,
            "dir": self._dir,
            "speed_dps": round(self._speed, 2),
            "max_speed_dps": self.max_speed,
            "elapsed_ms": elapsed,
            "watchdog_left_ms": left,
            "target": [round(float(v), 3) for v in self._target] if self._active else None,
            "reason": self._last_reason,
            "mode": motion.state().get("mode", "sim"),
            # ★ 键名用 run_mode 而不是 mode：上面的 mode 是"模拟/真实"执行模式，
            #   这里说的是示教器档位（T1/T2/AUTO/REMOTE），两者不是一个东西。
            "run_mode": runmode.summary(),
            "mode_warning": "" if runmode.state()["confirmed"] else "尚未声明示教器档位",
            # ---------- 坐标系（阶段 6）----------
            "frame": self._frame,
            "user_frame": self._user_frame,
            "slow": self._slow,
            "speed_unit": jf.unit_of(self._frame, self._joint or 1) + "/s",
            "axis_speed_limit": round(self.axis_speed_limit(self._frame, self._joint or 1), 2),
            "speed_scale": round(self._speed_scale, 4),
            "frame_axes": jf.axes_of(self._frame),
        }

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()


jog = JogEngine()
