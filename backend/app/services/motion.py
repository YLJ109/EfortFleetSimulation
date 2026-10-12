# -*- coding: utf-8 -*-
"""
执行引擎: 把目标关节角下发给机器人(真实模式写 Modbus)或安全模拟(默认)。

安全设计:
  - 所有写接口都必须经 require_control 鉴权(见 api/control.py)。
  - 急停优先: 触发后任何 command 都被拒绝, 直到 reset_estop。
  - 限位校验: 目标超出关节限位直接拒绝。
  - 真实下发默认关闭: 需同时设置环境变量 EFORT_REAL_MOTION=1 且
    config motion.real_write=true, 否则只做校验+记录(前端据此动画)。
"""
from __future__ import annotations

import os
import time
from threading import Lock
from typing import Callable, List, Optional

from app.core.config import get_config
from app.core.logger import get_logger
# ★ 全维度审查 2026-09-25：安全常量与轴锁策略的唯一来源
from app.core.safety_const import (
    ESTOP_RETRY, joint_lock_state,
    joint_lock_enabled, locked_joints, only_joint, joint_lock_tol,
    joint_lock_apply_in_sim, clamp_speed,
)
# ★ 审计修复 P1-E1：限位读取与判定的唯一实现在 services/limits.py。
#   原先本文件自己写了一份（tuple 形式 + 自己做 NaN 检查），与 control/vision 各查各的。
from app.services.limits import in_limits, load_ranges
from app.services.modbus import ADDR_SET_SPEED, ModbusRobot
from app.services.sim_robot import sim_robot

log = get_logger("motion")

# ★ 2026-09-24：这里**不能**顶层 import collector —— collector 顶层要 import
#   motion（读指令通道 + real_write_enabled），顶层互引会让 uvicorn 起不来
#   （ImportError: cannot import name 'collector' from partially initialized module）。
#   collector 只在 command() 的 J6 安全锁分支里用到，且那一支是真机下发路径，
#   改为函数内延迟导入，导入时机上 collector 早已初始化完毕。


def real_motion_env_active() -> bool:
    """★ P1-2：环境变量真值表唯一实现（设置页 runtime_flags 与执行引擎共用）。

    改造前两处各写一份且不一致：runtime_flags 认 true/yes/on，执行引擎只认 "1"
    → "真实下发已生效"指示灯会撒谎。现在只有这一份。
    """
    return os.environ.get("EFORT_REAL_MOTION", "0").strip().lower() in ("1", "true", "yes", "on")


def real_write_enabled() -> bool:
    """双确认总闸：环境变量 + config motion.real_write。任意一个没开都是安全模拟。"""
    return bool(real_motion_env_active()) and bool(
        get_config().get("motion", "real_write", default=False)
    )


# ======================================================================
# ★★ 2026-10-11 统一"未就绪"闸门（用户口径）：
#   「如果没有上电，执行点到点、移到什么的，全部提醒机器人没有就绪，所有的。」
#
#   覆盖 = **所有会让机器人动的真实下发入口**（见各 api 的调用点）：
#     /control/move · /control/run-file(非空跑) · /control/jog/step · /control/jog/start ·
#     /control/vacuum · /sorting/start(非空跑) · /sorting/goto · /ai/exec(非空跑) ·
#     /tictactoe/start(非空跑) · 以及后台视觉规则自动执行 vision_rules.execute_rule。
#
#   判据（唯一权威）= 控制器"伺服上电位" bits.servo —— 与 /api/rc-status 的
#     snap.bits.servo、前端 stores/rcReady.js 的 servoOn 同源。读不到状态 → 按
#     "未就绪"拒绝（fail-safe，宁可拒绝不蒙）。
#
#   ★ 为什么**不**把闸门直接塞进 motion.command()：
#     1) 点动（jog._loop）每 ~100ms 调一次 command —— 在此逐拍 rc_snapshot（3 次 FC3）
#        会把点动链路拖慢近一倍；本闸门放在**接口层**，每请求只查一次。
#     2) motion.command() 是大量单测的"纯逻辑出口"（test_audit_p0 的假 modbus 没有
#        rc_snapshot）—— 塞进去会整片打断，也违背"不改下发时序/互锁"的硬约束。
#     3) 就绪流程**不走** motion.command（rc_ready 直接用 mb.rc_command），本就不存在
#        "把自己拦死"的死锁；接口层收口更清晰、也便于逐条回归。
#
#   豁免（必须放行）：空跑 dry=True、模拟模式（双闸未开）、强制模拟链路；以及**就绪流程
#     本身**（/ready、/ready/cancel、/control/clear-error）与急停/停止类接口（它们不调用本函数）。
# ======================================================================
SERVO_NOT_READY_MSG = "机器人没有就绪：伺服未上电，请先点「一键就绪」"


def _service_program_no() -> int:
    """常驻点动服务程序号（`motion.jog.service_program`，现场 = 210）；读不到 → 0（= 不校验）。"""
    try:
        return int(get_config().get("motion", "jog", "service_program", default=0) or 0)
    except Exception:                                  # noqa: BLE001
        return 0


def readiness_reason(snap) -> Optional[str]:
    """**控制器快照 → 「未就绪」原因**（None = 就绪）。★ **唯一判据实现**。

    ★★ 2026-10-12（用户口径：「**一键就绪的要求是程序加载、上电等等**」+ 现场实测修正）：
      **就绪 = 伺服上电 + 常驻服务程序（210）已就位且运行 + AUTO/远程档 + 无报警 + 无极停**。
      · ⚠ **不再要求 `prog_loaded`** —— 现场实测「prog=210 / run=1 / **prog_loaded=0** 就是
        正常运行态」（"已加载"位在程序 RUN 起来之后会**落下**，见
        `modbus.rc_vacuum_prepare` / `rc_ready._prog_ok` 的现场注释）。
        旧判据 `servo and prog_loaded and run` 在正常运行时**恒为 False** →
        「未就绪」被长期误报、所有就绪门控的按钮永远点不动（**本轮修的真 bug**）。
    ★ 判据与 `rc_ready._prog_ok`（就绪加载流程）**同源**：目标程序号已就位 且 处于运行态。
    ★ 为什么放在 motion 层：`api/robot.py` 与 `services/collector.py` 都直接 import motion，
      把唯一实现放这里 → "快照就绪"与"下发前闸门"共用**同一份代码**（§5.5 铁律④ 同屏同源）。
    ★ 返回的每条文案都以「机器人没有就绪：」开场（现场一眼识别），主语在冒号后**如实**说明
      到底是哪一条不满足（不笼统写"伺服未上电"—— 那是撒谎）。
    """
    if snap is None:
        return "机器人没有就绪：读不到控制器状态，请先点「一键就绪」"
    b = snap.get("bits") or {}
    if not bool(b.get("servo")):
        return SERVO_NOT_READY_MSG
    if bool(b.get("estop")):
        return "机器人没有就绪：急停已按下，请先复位急停"
    if bool(b.get("alarm")):
        return ("机器人没有就绪：控制器有报警（%s/%s），请先点「清除报错」"
                % (snap.get("alarm1"), snap.get("alarm2")))
    if not (bool(b.get("auto")) or bool(b.get("remote"))):
        return "机器人没有就绪：示教器不在 AUTO/远程档（T1/T2 下上位机指令无效），请先拨到 AUTO"
    svc = _service_program_no()
    if svc > 0:
        try:
            prog = int(snap.get("prog") or 0)
        except (TypeError, ValueError):
            prog = 0
        if prog != svc:
            return ("机器人没有就绪：常驻服务程序（%d）未加载，当前程序号 %s，"
                    "请先在「真机链路」点「一键就绪」" % (svc, prog or "?"))
        if not bool(b.get("run")):
            return ("机器人没有就绪：常驻服务程序（%d）未运行（未挂起在 WAIT），"
                    "请先在「真机链路」点「一键就绪」" % svc)
    return None


def snapshot_ready(snap) -> bool:
    """控制器快照 → 是否**完整就绪**（前端 `rc.ready` 与各就绪门控按钮的唯一来源）。"""
    return readiness_reason(snap) is None


def servo_not_ready_reason(dry: bool = False) -> Optional[str]:
    """真实下发前的统一"未就绪"闸门：返回 None = 放行；否则返回统一的拒绝文案。

    - dry=True（空跑/预演）→ 放行（不下发真实运动）。
    - 双闸未开（模拟模式）→ 放行（由 motion.command 播放仿真，不写控制器）。
    - 强制模拟链路（simulate=always / 采集器已降级）→ 放行（交给 motion.command 的
      "模拟链路"分支给更准确的原因，本闸门不抢答）。
    - 否则按 `readiness_reason`（**完整就绪**：伺服 + 210 已运行 + AUTO + 无报警/急停）判定。

    ★★ 2026-10-12（用户口径「**没有就绪，所有能执行/到点/姿势/控制按钮全部禁止**」）：
      判据由"只看伺服位"**升级为完整就绪** —— 伺服上电但 210 没加载/没运行、或不在
      AUTO 档、或有报警/急停，一律按"未就绪"拒绝。就绪流程本身**不调用本函数**（防死锁）。
    """
    if dry:
        return None
    if not real_write_enabled():
        return None
    # 强制模拟/离线链路：不做真实读（rc_snapshot 会开 TCP，失败会阻塞），
    # 也不抢答 —— 那是 motion.command 的"模拟链路"分支职责。
    try:
        from app.services.collector import collector   # 延迟导入防环
        if collector.simulated:
            return None
    except Exception:                                  # noqa: BLE001
        pass
    try:
        snap, err = motion.modbus.rc_snapshot()
    except Exception as e:                             # noqa: BLE001 —— 通讯异常按未就绪
        return ("机器人没有就绪：读不到控制器状态（%s），请先点「一键就绪」"
                % str(e)[:120])
    if snap is None:
        return ("机器人没有就绪：读不到控制器状态（%s），请先点「一键就绪」"
                % (err or "无响应"))
    return readiness_reason(snap)


class MotionService:
    def __init__(self) -> None:
        # 真实下发开关: 环境变量 + 配置双重确认, 默认 False(安全模拟)
        # ★ 真值表统一走 real_motion_env_active()（P1-2：两处不一致会让指示灯撒谎）
        self.real = real_write_enabled()
        self._lock = Lock()
        # ★ 审计修复 P0-2：执行级互斥锁。
        #   原实现只有 self._lock 这把"状态短锁"，而真正的下发序列
        #   （写速度 40103 → 写目标角 → settle → 触发 → 等完成 → 撤触发）
        #   全程在锁外执行 → 两个请求线程可以交错写同一批寄存器，
        #   后果是 A 的目标角配 B 的速度、甚至 A 的目标被 B 覆盖后触发。
        #   这把锁专用于串行化"整段下发"，与状态短锁分工明确。
        self._exec_lock = Lock()
        self.moving = False
        self.stopped = False
        self.last_target: Optional[List[float]] = None
        self.last_executed_at = 0.0
        self.last_error: Optional[str] = None
        self.modbus = ModbusRobot()
        log.info("执行引擎就绪, 模式=%s", "REAL" if self.real else "SIMULATE(安全模拟)")

    def _limits(self) -> List[tuple]:
        # ★ 审计修复 P1-E1：读取收敛到 limits.load_ranges() —— 长度恒为 6，
        #   配置缺项补默认、单条写坏只影响该条（原实现少一条就会在调用侧下标越界）。
        return load_ranges()

    def _in_limits(self, q: List[float]) -> bool:
        # ★ 审计修复 P1-E1：判据（长度 → 有限性 → 区间）统一在 limits.in_limits()。
        #   原实现用 zip：q 比限位短时**后面的轴根本没查**；NaN 检查是这一份独有的，
        #   control / vision 那两份没有 —— 同一个目标在预演和下发上可能结论相反。
        #   这里保留方法名，是因为它是"下发前最后一道闸"，测试也直接钉着它。
        return in_limits(q, self._limits())

    def command(self, joints: List[float], speed_pct: int = 100,
                dwell_ms: int = 0,
                abort_check: Optional[Callable[[], bool]] = None) -> dict:
        """执行一次目标到位。返回执行结果(含模式/是否真下发)。

        ★ 审计修复 P0-2：整段下发序列在 _exec_lock 内完成，
          并发调用只会有一个真正执行，另一个直接返回可读错误。

        ★ 审计修复 P1-A10：abort_check —— 调用方自带的中止判据。
          点动（死人开关/watchdog/松手停止）必须能**打断飞行中的下发**：
          rc_jog_execute 同步阻塞可达 30s，原来只有急停能中断，
          看门狗超时后要等这一发走完才停（远超 watchdog_ms=1500 的设计）。
        """
        if not isinstance(joints, (list, tuple)) or len(joints) != 6:
            return {"ok": False, "error": "目标必须是 6 个关节角"}

        # ★ P1-A7：真值实时求值，不再用启动时缓存的 self.real。
        #   原实现 self.real 只在 __init__ 算一次 → 运行中改了
        #   motion.real_write / EFORT_REAL_MOTION 不重启不生效，
        #   出现"关了闸还在写真机"。这里实时读，self.real 只用于展示。
        real = real_write_enabled()
        self.real = real

        if not self._exec_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有下发在执行中，请稍候重试",
                    "busy": True}
        try:
            return self._command_locked(joints, speed_pct, dwell_ms, real,
                                        abort_check)
        finally:
            self._exec_lock.release()

    def _command_locked(self, joints: List[float], speed_pct: int,
                        dwell_ms: int, real: bool,
                        abort_check: Optional[Callable[[], bool]] = None) -> dict:
        """已在 _exec_lock 内的下发主体。"""
        with self._lock:
            if self.stopped:
                return {"ok": False, "estop": True, "error": "急停已触发，请先复位急停"}
            if not self._in_limits(joints):
                return {"ok": False, "error": "目标超出关节限位或含非法值(NaN/Inf)",
                        "violations": True}

        cur_j: Optional[List[float]] = None
        # ★ 全维度审查 2026-09-25（F-03 v2.1）：轴锁改成「可配置模式」。
        #   原实现把"仅 J6"**硬编码**在真机分支里 —— 操作员要全轴操控却被后端否决，
        #   而前端六轴按钮全开、又没有任何"当前处于仅 J6 模式"的提示（体验断裂）。
        #   现在：默认关闭 → J1~J6 全轴可控；开启（AI 测试模式）→ 仅 J6 可动。
        lock_on = joint_lock_enabled()
        if lock_on and (real or joint_lock_apply_in_sim()):
            from app.services.collector import collector  # 延迟导入防循环
            cur_p = collector.get_latest()
            # ★ 审计修复 P1-A8：原实现拿不到当前位姿时
            #   `cur_j = joints` → 偏差恒 0 → **轴锁形同虚设**。取不到真值一律拒绝。
            if not cur_p:
                return {"ok": False, "lock_j6": True,
                        "error": "无法获取当前位姿（fail-safe），已拒绝下发"}
            cur_j = [float(cur_p.get(f"j{i}", 0.0)) for i in range(1, 7)]
            tol = joint_lock_tol()
            only = only_joint()
            for i in locked_joints():
                if i == only:
                    continue
                dev = abs(float(joints[i - 1]) - cur_j[i - 1])
                if dev > tol:
                    return {"ok": False,
                            "error": (f"轴锁模式已开启：J{i} 不可动（仅 J{only} 可动），"
                                      f"偏差 {dev:.2f}° 已拒绝。关闭方法：config/robot.yaml "
                                      f"motion.joint_lock.enabled=false 后重启后端"),
                            "lock_j6": True, "joint_lock": True,
                            "locked_joints": locked_joints(), "only_joint": only,
                            "current": cur_j}

        # ★ 全维度审查 2026-09-25（B-02）：真机下发前必须确认"链路真的连着真机"。
        #   1) connection.simulate=always 属强制模拟，禁止写真机；
        #   2) collector 已降级（掉线/重连中）时 get_latest() 返回的是仿真角，
        #      拿它当基准等于把仿真值当真机值，必须拒绝；
        #   3) 位姿过期（遥测冻结）时读数不可信，同样拒绝。
        if real:
            from app.services.collector import collector  # 延迟导入防循环
            sim_mode = str(get_config().connection.get("simulate", "auto")).lower()
            if sim_mode == "always" or collector.simulated:
                return {"ok": False, "mode": "sim", "lock_j6": lock_on,
                        "error": ("当前为模拟链路（connection.simulate=%s / "
                                  "collector.simulated=%s），拒绝写控制器"
                                  % (sim_mode, collector.simulated))}
            cur_p = collector.get_latest()
            if not cur_p:
                return {"ok": False, "lock_j6": lock_on,
                        "error": "无法获取当前位姿（fail-safe），已拒绝真机下发"}
            if cur_p.get("stale"):
                return {"ok": False, "lock_j6": lock_on,
                        "error": "当前位姿已过期（遥测冻结），读数不可信，已拒绝真机下发"}

        with self._lock:
            # ★ 2026-09-29：失败回滚基线。last_target 是"逻辑目标"，增量点动的下一次
            #   目标由它推算（jog._current() 优先取它）。原实现在**下发前**就无条件把
            #   last_target 推前，失败也不回滚 → 一发失败的 1° 点动虽然机器人没动，
            #   但逻辑目标已经跑了 1°，下一次按下会走 2°（实测：-5.98 → 目标 -4.98 失败
            #   → 再按目标变成 -3.98），误差逐次累积、且与真实位姿越漂越远。
            _prev_target = self.last_target
            self.moving = True
            self.last_target = [round(float(x), 3) for x in joints]
            self.last_error = None

        mode = "real" if real else "sim"
        if real:
            # ★ Stage D：真实下发走"点动通道"（实测确认的唯一有效路径）——
            #   速度设定(40103) → 目标角(40139~44, FC6 逐发)+回读校验 → settle 0.15s
            #   → 触发(40135.Bit0) → 等完成(40035.Bit0) → 撤触发。
            #   控制器必须有常驻点动服务程序（200/JOGSVC）在 WAIT 挂起。
            # ★ 速度寄存器校正（2026-09-24 实机探测复验）：必须用常量
            #   ADDR_SET_SPEED（102 = 40103），原写死 103 会冲掉 40104 程序号。
            # ★ 审计修复 P1-A9：速度写入与 rc_jog_execute 现在同处
            #   _exec_lock 内 → "校验→写速度→写目标→回读→触发" 原子化，
            #   不再可能被并发请求插进来改速/改目标。
            # ★ 全维度审查 B-03：速度用统一常量夹取（原来是散落的 max(1, min(100,...))）
            # ★ 速度补偿（铁律 6）：有效速度 = 程序内 v…perc × 40103。
            #   常驻服务程序 200 带 v50perc，若直接把 UI 速度写进 40103，
            #   机器人实际只走 一半（设 20% → 实际 10%）。为让「右上角速度 = 实际速度」，
            #   这里**反算** 40103 = clamp(UI速度 ÷ (v…perc/100))，再写 40103。
            #   v…perc 取 config motion.jog.service_program_vperc（现场 50 = v50perc）。
            vperc = float(get_config().get("motion", "jog",
                                            "service_program_vperc", default=50) or 50)
            if vperc <= 0:
                vperc = 50.0
            # 反算：UI 速度(speed_pct) 是"期望实际速度"，故 40103 = speed_pct / (vperc/100)
            speed_40103 = clamp_speed(int(round(speed_pct / (vperc / 100.0))))
            ok, err = self.modbus.write_reg(ADDR_SET_SPEED, speed_40103)
            if err is not None or ok is None:
                with self._lock:
                    self.moving = False
                    self.last_error = err
                    self.last_target = _prev_target     # ★ 失败回滚（见上）
                return {"ok": False, "mode": mode, "error": "写速度设定失败: %s" % err}
            # ★ 全维度审查 B-03：速度是安全红线，必须**回读比对数值**
            #   （40139~40144 有完整回读，唯独速度这条最关键的通道原来只查错误码、
            #    丢弃回显值 → 控制器钳位/映射后前后端仍以为"设定值已生效"）。
            try:
                echo_i = int(ok)
            except (TypeError, ValueError):
                echo_i = None
            if echo_i is not None and echo_i != speed_40103:
                with self._lock:
                    self.moving = False
                    self.last_error = ("速度设定回读不一致：写 %d%% 读回 %d%%"
                                       % (speed_40103, echo_i))
                    self.last_target = _prev_target     # ★ 失败回滚（见上）
                log.error("速度回读不一致，已中止下发: %s", self.last_error)
                return {"ok": False, "mode": mode, "error": self.last_error,
                        "speed_written": speed_40103, "speed_echo": echo_i}
            # ★ should_abort：急停置位后，飞行中的点动链路（写目标→触发→轮询
            #   完成位，最长 30s）能立刻感知并撤触发中止，而不是排队走完。
            #   _lock 不可重入且不能在 I/O 期间持有，用短锁快照读 stopped。
            # ★ 审计修复 P1-A10：中止判据扩为"急停 OR 调用方判据"——
            #   点动的死人开关/watchdog 停止也必须能打断飞行中的这一发。
            def _abort_now() -> bool:      # noqa: E306 —— 紧贴使用点，避免裸闭包外泄
                if self._stopped_now():
                    return True
                if abort_check is None:
                    return False
                try:
                    return bool(abort_check())
                except Exception:          # 判据本身出错 → 按"中止"处理（fail-safe）
                    log.exception("abort_check 抛异常，按中止处理")
                    return True
            try:
                ok, err, detail = self.modbus.rc_jog_execute(
                    self.last_target, speed_40103, should_abort=_abort_now,
                    cur_joints=cur_j)
            except Exception as e:  # noqa: BLE001 —— 兜底：任何异常都必须复位 moving
                log.exception("下发序列异常")
                with self._lock:
                    self.moving = False
                    self.last_error = str(e)
                    self.last_target = _prev_target     # ★ 失败回滚（见上）
                return {"ok": False, "mode": mode, "error": "下发异常: %s" % e}
            with self._lock:
                self.moving = False
                self.last_executed_at = time.time()
                if not ok:
                    self.last_error = err
                    # ★ 失败回滚：机器人没到位（完成位超时/被急停打断/触发失败），
                    #   逻辑目标必须退回到下发前 —— 否则下一次增量点动会基于一个
                    #   "没发生过的位移"累加，误差逐次放大。
                    self.last_target = _prev_target
                    return {"ok": False, "mode": mode, "error": err or "下发失败",
                            "detail": detail}
            return {"ok": True, "target": self.last_target,
                    "speed_pct": speed_pct, "speed_40103_written": speed_40103,
                    "mode": mode, "executed": True, "detail": detail}

        # 模拟模式: 仅校验 + 记录；★ Stage C：同时把目标交给有状态仿真机，
        #   采集循环会推着仿真机体位真的"走向"目标（tracking=true → 到位停住），
        #   三页的 3D/读数因此能反映指令的执行过程，而不再是无关的正弦扫掠。
        sim_robot.on_command(self.last_target, speed_pct)
        time.sleep(min(dwell_ms / 1000.0, 0.05))
        with self._lock:
            self.moving = False
            self.last_executed_at = time.time()
        return {"ok": True, "target": self.last_target, "speed_pct": speed_pct,
                "mode": mode, "executed": False}

    def _stopped_now(self) -> bool:
        """短锁快照读急停标志（rc_jog_execute 的 should_abort 回调用）。"""
        with self._lock:
            return self.stopped

    def estop(self) -> dict:
        with self._lock:
            self.stopped = True
            self.moving = False
        # ★ 全维度审查 B-06：急停结果必须**可见**。
        #   原实现：下发失败只 log.error，然后照常返回 state()（含 stopped=True）
        #   → 前端/运维看到"急停已触发"，控制器其实没收到停止字。
        #   现在返回 estop_sent / estop_error，失败由路由层转 503 + critical 事件。
        sent, send_err = False, None
        # ★ P1-A7：急停实时求值。急停方向宁可多发不可漏发 ——
        #   即使缓存的 self.real 是 False，只要当前双闸是开的就必须真发。
        if real_write_enabled() or self.real:
            for _ in range(ESTOP_RETRY):     # 急停不能只发一次
                try:
                    # ★ Stage D：急停 = 0x1005 停止命令字（旧 estop_addr 方案已证伪）
                    ok, err = self.modbus.rc_estop(True)
                    if ok:
                        sent = True
                        break
                    send_err = err
                except Exception as e:  # noqa
                    send_err = str(e)
                time.sleep(0.05)
            if not sent:
                log.error("急停下发失败（已重试 %d 次）: %s", ESTOP_RETRY, send_err)
        else:
            sent = True          # 模拟模式：无需下发，本地锁定即成立
            log.warning("急停(模拟): 已锁定执行引擎")
        st = self.state()
        st["estop_sent"] = bool(sent)
        st["estop_error"] = send_err or ""
        return st

    def reset_estop(self) -> dict:
        with self._lock:
            self.stopped = False
        if real_write_enabled() or self.real:
            try:
                self.modbus.rc_estop(False)
            except Exception as e:  # noqa
                log.error("急停复位下发异常: %s", e)
        return self.state()

    def state(self) -> dict:
        # ★ 全维度审查 B-20：mode 原用 __init__ 缓存的 self.real，
        #   运行中改了 real_write / EFORT_REAL_MOTION 后，指示灯与 health 仍显示旧模式。
        #   与 command() 一样改为实时求值（self.real 只保留给日志用）。
        real_now = real_write_enabled()
        with self._lock:
            return {
                "moving": self.moving,
                "stopped": self.stopped,
                "mode": "real" if real_now else "sim",
                "last_target": self.last_target,
                "last_executed_at": self.last_executed_at,
                "last_error": self.last_error,
                # ★ F-03 v2.1：轴锁模式对外可见，前端据此显示徽标/禁用按钮
                "joint_lock": joint_lock_state(),
            }


motion = MotionService()
