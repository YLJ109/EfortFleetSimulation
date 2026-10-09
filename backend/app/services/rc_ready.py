# -*- coding: utf-8 -*-
"""
一键就绪（Stage D）：把控制器从"任意状态"推到"可接受点动"的就绪态。

参考私有参考实现 tools/ready_up.py 的实机验证流程：
  清报警 → （伺服掉了就重新吸合）→ 加载点动服务程序 → 运行（挂起在 WAIT）。

★ 铁律（全部来自实测教训）：
  - 触发位（40135.Bit0）为 1 时**绝不执行** —— 上一发点动还没收尾，任何写入
    都可能被正在 WAIT 的程序读到半截数据；
  - 必须在【自动/远程】档 —— T1/T2 下控制器忽略一切上位机指令（寄存器写得进
    去但机器人不动），先检查模式位再动手，省得用户对着"写成功但没动"排查；
  - 清报警后伺服可能掉电：掉电就重新吸合（0x0000 → 等 0.6s → 0x1001，
    吸合延迟实测 ≈0.55s，等待必须 ≥0.6s）；
  - 先加载成功才准运行（否则 5005 加载的程序不存在）；
  - 全程**不写**目标角区（40139~44）与触发位 —— 本流程只让程序挂起到 WAIT，
    不产生任何运动。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from app.core.config import get_config
from app.core.logger import get_logger
from app.services.modbus import (
    ADDR_RO_TRIG,
    ADDR_SET_PROG,
    CMD_CLEAR,
    CMD_DISABLE,
    CMD_LOAD,
    CMD_RUN,
    CMD_SERVO,
    CMD_STOP,
    CMD_ZERO,
    SERVO_REENGAGE_DELAY,
    ModbusRobot,
)
from app.services.motion import real_write_enabled

log = get_logger("rc_ready")

# 报警码 → 现场处置提示（只覆盖已现场勘察确认的码，来源 config/robot.yaml:140 与
# docs/控制器Modbus寄存器勘察报告.md）。未列出的码给通用提示，避免"猜错方向"。
ALARM_HINTS: Dict[int, str] = {
    1812: "安全门/安全回路不满足：检查安全门、光栅、外部急停是否复位",
    3909: ("示教器（bcc 客户端）通讯断开/未连接：远程加载与运行都依赖示教器在线，"
           "请检查示教器线缆、电源与急停，恢复连接后重试「一键就绪」"),
    5005: ("远程加载/运行程序错误。★ 2026-09-29 实机更正：最常见的原因不是示教器，"
           "而是**程序停在报警态且仍在运行** —— 此时清报警会被它立刻重新顶上"
           "（清完 0.5s 内复现）。正确处置：先 CMD_STOP 停掉当前程序，再清报警，"
           "然后重新加载（本流程已按此顺序执行）。若停机后仍清不掉，再查："
           "① 控制器上确实没有目标程序号；② 示教器停在文件管理/编辑界面；"
           "③ 示教器未连接(3909)；④ 该程序文件损坏(4902)"),
    4902: "XPL 文件损坏：重新导出/保存该程序",
}


def jog_service_program() -> int:
    """点动服务程序号（config motion.jog.service_program；0 = 未指定，不瞎猜）。"""
    try:
        return int(get_config().get("motion", "jog", "service_program", default=0) or 0)
    except Exception:
        return 0


def allowed_programs() -> List[int]:
    """★ 全维度审查 B-01：就绪允许「加载并运行」的程序号白名单。

    原实现 `ReadyIn.prog` 无任何约束 → 持 operator 令牌即可让控制器加载并
    **运行任意程序号**（仓库里就有 411.XPL 这类产线/测试程序）。这不是点动
    服务程序，误触发就是真实运动。

    缺省只含点动服务程序；确需运行其它程序，由运维在
    config/robot.yaml 的 motion.ready.allowed_programs 里显式追加。
    """
    s = set()
    try:
        p = int(jog_service_program())
        if p > 0:
            s.add(p)
    except Exception:
        pass
    try:
        extra = get_config().get("motion", "ready", "allowed_programs",
                                 default=[]) or []
        for x in extra:
            try:
                v = int(x)
                if v > 0:
                    s.add(v)
            except Exception:
                continue
    except Exception:
        pass
    s.discard(0)
    return sorted(s) or ([200] if jog_service_program() <= 0 else [])


class ReadinessService:
    """一键就绪执行器。无状态（每次完整跑一遍），线程安全由"单次串行"保证。"""

    def __init__(self, modbus: Optional[ModbusRobot] = None) -> None:
        self._mb = modbus  # None = 用 motion 的连接（共享 socket + IO 锁）
        self._busy_lock = __import__("threading").Lock()

    def _client(self) -> ModbusRobot:
        if self._mb is not None:
            return self._mb
        from app.services.motion import motion   # 延迟导入避免环
        return motion.modbus

    def ready(self, prog: Optional[int] = None,
              on_step: Optional[Callable[[str, str], None]] = None) -> Dict[str, Any]:
        """执行就绪流程，返回 {ok, steps:[{step,ok,msg}], error, summary}。

        ★ 审计修复 P1-A11：就绪流程与下发序列必须共用**同一把**执行互斥。
          原来只有自己的 _busy_lock（只在"就绪流程之间"互斥），于是
          全清(0x0000)/加载/运行可以插进飞行中的点动序列 ——
          触发位置着 1 的同时程序被重载，产生谁也说不清的半截状态。
        """
        if not self._busy_lock.acquire(blocking=False):
            return {"ok": False, "error": "就绪流程正在执行中，请稍候", "steps": []}
        try:
            from app.services.motion import motion   # 延迟导入避免环
            if not motion._exec_lock.acquire(blocking=False):
                # ★★ 2026-10-09（用户口径）："如果再触发这个条件，**直接下电伺服**，等待一键就绪吧"。
                #   原来只回一句"已推迟"，操作员干瞪眼不知道该干什么。
                #   现在改为**主动补救**：等点动/下发收尾（通常 <1s）→ 立即执行
                #   "取消就绪"（停程序 + 撤触发 + **伺服下电**），把系统放回一个明确的静止态，
                #   然后提示"请再点一次一键就绪"。
                #
                #   ⚠ 为什么是"等它收尾"而不是"插队直接写 0x2000"：
                #     写指令字（40101）**必须持执行锁** —— 强行插队会和飞行中的点动抢
                #     指令字，产生的正是这句提示想避免的"半截状态"。
                #     所以"直接下电"的正确含义是"不再让用户手动去处理，由系统完成下电"，
                #     而不是"不等点动结束就抢着写"。
                return self._defer_then_servo_off()
            try:
                return self._ready(prog, on_step)
            finally:
                motion._exec_lock.release()
        finally:
            self._busy_lock.release()

    def _defer_then_servo_off(self, wait_s: float = 3.0) -> Dict[str, Any]:
        """被点动/下发占用时的补救：等它收尾 → 取消就绪（**含伺服下电**）。

        返回 ok=False（本次**没有**完成一键就绪）+ 明确指引，让界面照原样显示。
        """
        from app.services.motion import motion
        t0 = time.time()
        if not motion._exec_lock.acquire(timeout=float(wait_s)):
            return {"ok": False, "steps": [], "deferred": True,
                    "error": ("有点动/下发正在执行，已推迟一键就绪（等待 %.0f 秒仍未释放）。"
                              "请松开点动按钮或等它跑完再点。" % wait_s)}
        try:
            r = self._cancel(servo_off=True)          # 停程序 + 撤触发 + 下电伺服
        finally:
            motion._exec_lock.release()
        steps = list(r.get("steps") or [])
        off = any(s.get("step") == "servo_off" and s.get("ok") for s in steps)
        steps.append({"step": "defer", "ok": True,
                      "msg": "原请求被点动/下发占用 → 已自动改为「取消就绪」"
                             "（等待 %.1fs）" % (time.time() - t0)})
        return {"ok": False, "steps": steps, "deferred": True,
                "error": ("刚才有点动/下发在执行：已**主动停止程序%s**"
                          "（避免半截状态）。现在请再点一次「一键就绪」。"
                          % ("并下电伺服" if off else "（伺服未确认下电）"))}

    # ------------------------------------------------------------------
    def _step(self, steps: List[dict], name: str, on_step, ok: bool, msg: str) -> None:
        rec = {"step": name, "ok": ok, "msg": msg}
        steps.append(rec)
        log.info("[就绪] %s: %s", name, msg)
        if on_step:
            try:
                on_step(name, msg)
            except Exception:
                pass

    def _ready(self, prog: Optional[int], on_step) -> Dict[str, Any]:
        steps: List[dict] = []
        mb = self._client()

        # 0) 总闸：一键就绪本身就是要写真机寄存器 —— 双确认必须已开
        if not real_write_enabled():
            self._step(steps, "gate", on_step, False,
                       "真实下发未开启（需要 EFORT_REAL_MOTION=1 且 motion.real_write=true）")
            return {"ok": False, "error": "真实下发未开启：总闸未打开，拒绝写控制器",
                    "steps": steps}

        # 0.5) 程序号白名单：★ 必须在**任何 Modbus 操作之前**（含读快照）
        #   理由（安全 + 副作用）：白名单外的程序号意味着这次调用本就不该发生，
        #   连读都不必读；更关键的是后面的"清报警/上伺服/加载/运行"都是**写**操作，
        #   把它们排在校验之前，等于"先动控制器、后判断该不该动"。
        #   （回归 test_safety_redline::test_ready_rejects_unlisted_program 钉死此顺序。）
        prog_no = int(prog if prog is not None else jog_service_program())
        allowed = allowed_programs()
        if prog_no not in allowed:
            return {"ok": False,
                    "error": ("程序号 %d 不在就绪白名单 %s 内，拒绝加载运行。"
                              "如需放行，请在 config/robot.yaml 的 "
                              "motion.ready.allowed_programs 中显式追加后重启"
                              % (prog_no, allowed)),
                    "allowed_programs": allowed, "steps": steps}
        if prog_no <= 0:
            self._step(steps, "prog", on_step, False, "未配置点动服务程序号")
            return {"ok": False,
                    "error": "未配置点动服务程序号（config motion.jog.service_program，现场=200/JOGSVC）",
                    "steps": steps}

        # 0.6) ★★ 撤触发位（写 40135.Bit0 = 0）—— 现场铁律："加载前只需清报警 + 撤触发"。
        #   为什么必须放在这里：触发位为 1 时下面的守卫会拒绝就绪。若某次点动异常退出
        #   把触发位留在 1，用户就**再也点不了「一键就绪」**（界面按钮被禁用 → 死锁，
        #   现场报的"现在一键就绪点不了"就是这个）。撤触发只是"撤销执行意图"，
        #   **不产生任何运动**，所以正确做法是先撤销、再刷新快照，而不是把按钮永久禁用。
        _trig_before = None
        try:
            _snap0, _ = mb.rc_snapshot()
            _trig_before = bool(_snap0.get("jog_trig")) if _snap0 else None
        except Exception:
            _trig_before = None
        if _trig_before:
            _ok_t, e_trig = mb.write_reg(ADDR_RO_TRIG, 0x0000)
            self._step(steps, "trig_clear", on_step, e_trig is None,
                       ("已撤销点动触发位（原为 1）" if e_trig is None
                        else "撤销点动触发位失败: %s" % e_trig))
            if e_trig is not None:
                return {"ok": False,
                        "error": "撤销点动触发位失败（40135 写 0）：%s。请检查控制器通讯后重试" % e_trig,
                        "steps": steps}

        # 1) 快照：先看清楚控制器现在什么状态
        snap, err = mb.rc_snapshot()
        if snap is None:
            self._step(steps, "snapshot", on_step, False, "读快照失败: %s" % err)
            return {"ok": False, "error": "无法读取控制器状态: %s" % err, "steps": steps}
        b = snap["bits"]
        self._step(steps, "snapshot", on_step, True,
                   "模式=%s 伺服=%d 报警=%d 程序=%d 加载=%d 运行=%d"
                   % (snap["mode"], b["servo"], b["alarm"], snap["prog"],
                      b["prog_loaded"], b["run"]))

        # 2) 前置守卫：触发位
        #   ★ 审计修复 P1-A3：读不到触发位（jog_trig=None）必须**按"仍在触发"处理**。
        #   原实现 `if snap.get("jog_trig"):` 把 None 当成 0，fail-open ——
        #   "触发位为 1 绝不执行"这条铁律在通讯抖动的瞬间会自动失效，
        #   而抖动恰恰是最容易出现半截点动状态的时候。
        trig = snap.get("jog_trig")
        if trig is not False:
            self._step(steps, "guard", on_step, False,
                       "点动触发位无法确认（读取失败）" if trig is None else "点动触发位仍为 1")
            return {"ok": False,
                    "error": ("无法读取点动触发位，拒绝就绪（fail-safe）：请检查控制器通讯"
                              if trig is None else
                              "点动触发位仍为 1（上一发未收尾），请先撤销触发再就绪"),
                    "steps": steps}

        # 3) 档位守卫：T1/T2 下控制器忽略上位机指令（实机确认）
        if b["manual"] and not (b["auto"] or b["remote"]):
            self._step(steps, "mode", on_step, False, "控制器在手动档（T1/T2）")
            return {"ok": False,
                    "error": "控制器在手动档（T1/T2）：上位机指令无效，请把模式开关拨到 AUTO 或 远程",
                    "steps": steps}

        # 4) 报警
        #   ★★ 2026-09-29 实机修复：**清报警必须先停掉正在运行的程序** ★★
        #   实测（真机复现）：程序还在跑的时候发 CMD_CLEAR(0x1009)，报警看起来清掉了，
        #   但 0.5s 内就被那个（坏/半残的）运行中程序重新顶上来：
        #       初始   alarm=1(5005) run=1
        #       CMD_STOP  → run=0（报警仍在）
        #       CMD_CLEAR → alarm=0/0 **且不再复现**
        #   于是原顺序必然报"清报警后仍处于报警状态（5005）"，而那条提示语把矛头
        #   指向示教器（"退出文件管理器/恢复连接"）—— **真因是顺序**。
        #   CMD_STOP 是"程序停止"，不是急停：不断伺服、不产生任何位移。
        #
        #   ★★ 注意：**只在真有报警时才停机** ★★
        #   第一版把停机写成了无条件执行 —— 结果"程序本来正常在跑、无报警"这条
        #   最常见的路径也被停掉，而 STOP 之后 `_prog_ok` 认为已完成加载会跳过
        #   CMD_LOAD，紧接着 CMD_RUN 起不来（实测 2.5s 运行位不置位）。
        #   所以停机与"强制重新加载"必须配对出现，且只在清报警这条支路上。
        stopped_for_alarm = False
        if b["alarm"]:
            if b.get("run"):
                _, sterr = mb.rc_command(CMD_STOP)
                if sterr:
                    self._step(steps, "stop", on_step, False, "停止程序失败: %s" % sterr)
                    return {"ok": False,
                            "error": "停掉当前程序失败: %s（清报警前需先停机）" % sterr,
                            "steps": steps}
                t0 = time.time()
                stopped = False
                while time.time() - t0 < 2.0:
                    s2, _ = mb.rc_snapshot()
                    if s2 and not s2["bits"].get("run"):
                        stopped = True
                        break
                    time.sleep(0.1)
                self._step(steps, "stop", on_step, stopped,
                           "已停机（%.2fs）——运行中的程序会把报警重新顶上来，"
                           "必须先停再清" % (time.time() - t0) if stopped
                           else "停机后运行位 2s 内未落下")
                if not stopped:
                    return {"ok": False,
                            "error": "程序仍在运行（运行位未落下），无法可靠清报警。"
                                     "请确认控制器急停/安全回路正常后重试",
                            "steps": steps}
                b = dict(b)
                b["run"] = 0
                stopped_for_alarm = True

        if b["alarm"]:
            # ★ 现场实测：0x1009 清报警后控制器需要一点时间回写报警位，单次 0.4s
            #   轮询偶尔会"抓到还没落定"的报警位 → 误报"清报警后仍处于报警状态"。
            #   改为最多 3 次重试、每次 0.6s 后回读，给控制器足够的落定时间；
            #   若仍清不掉（如 3909 示教器未连接 / 5005 程序确实加载不上），那是
            #   真因未除，下面给出精准处置提示，而不是假失败把人带偏。
            alarm_cleared = False
            snap2 = None
            for _ in range(3):
                _, cerr = mb.rc_command(CMD_CLEAR)
                if cerr:
                    break
                time.sleep(0.6)
                snap2, _ = mb.rc_snapshot()
                if snap2 and not snap2["bits"]["alarm"]:
                    alarm_cleared = True
                    break
            alarm_now = not alarm_cleared
            self._step(steps, "alarm", on_step, not alarm_now,
                       ("清报警失败（码 %s/%s 仍在）" % (snap["alarm1"], snap["alarm2"]))
                       if alarm_now else
                       ("报警已清（原码 %s/%s）" % (snap["alarm1"], snap["alarm2"])))
            if alarm_now:
                a1 = (snap2 or snap or {}).get("alarm1", "?")
                a2 = (snap2 or snap or {}).get("alarm2", "?")
                # ★ 同时给 alarm1 / alarm2 两条码出提示（如 5005+3909 并存）：
                #   只给 alarm1 的提示会漏掉"示教器未连接"这条根因。
                hints = []
                for raw in (a1, a2):
                    try:
                        ci = int(raw)
                    except (TypeError, ValueError):
                        ci = None
                    if ci in ALARM_HINTS:
                        hints.append(ALARM_HINTS[ci])
                    elif ci not in (None, 0):
                        hints.append("报警 %s：请按示教器报警明细排查" % raw)
                if not hints:
                    hints.append("请按示教器报警信息排查，处理后重试")
                hint = "；".join(dict.fromkeys(hints))
                return {"ok": False,
                        "error": ("清报警后仍处于报警状态（当前码 %s/%s）。%s"
                                  % (a1, a2, hint)),
                        "steps": steps}
            b = snap2["bits"] if snap2 else b

        # 5) 伺服：掉了就重新吸合（0x0000 → 0.6s → 0x1001）
        if not b["servo"]:
            try:
                mb.rc_command(CMD_ZERO)          # 全清（控制器可能不回正常应答，容忍）
            except Exception:
                pass
            time.sleep(SERVO_REENGAGE_DELAY)
            _, serr = mb.rc_command(CMD_SERVO)
            if serr:
                self._step(steps, "servo", on_step, False, "上电命令失败: %s" % serr)
                return {"ok": False, "error": "伺服上电失败: %s" % serr, "steps": steps}
            # 吸合延迟 ≈0.55s，以 0.1s 步进等待伺服位（采样太快会误判"没上电"）
            t0 = time.time()
            servo_on = False
            while time.time() - t0 < 1.5:
                s2, _ = mb.rc_snapshot()
                if s2 and s2["bits"]["servo"]:
                    servo_on = True
                    break
                time.sleep(0.1)
            self._step(steps, "servo", on_step, servo_on,
                       "伺服已吸合（%.2fs）" % (time.time() - t0) if servo_on
                       else "伺服位 1.5s 内未吸合")
            if not servo_on:
                return {"ok": False, "error": "伺服上电后 1.5s 内未吸合，请检查使能回路/急停", "steps": steps}
        else:
            self._step(steps, "servo", on_step, True, "伺服已在上电状态")

        # 6) 加载点动服务程序
        #   ★ prog_no / 白名单已在上面的"0.5) 程序号白名单"里校验并绑定，这里直接用。
        #
        #   ★★ 判据修正（2026-09-29 实机确认）★★
        #   实测：控制器处于**正常运行态**时读到的是  prog=210 / run(bit6)=1 /
        #   prog_loaded(bit11)=0 —— 即"已加载"位在程序 RUN 起来之后会**落下**。
        #   原实现只认 prog_loaded：
        #     · 短路条件 `prog_loaded and prog==目标` 永不成立 → 每次都去发 CMD_LOAD；
        #     · 加载成功判据又是 `prog_loaded` → 2.5s 等不到 → 每次都假报
        #       "程序 210 加载失败（5005=加载的程序不存在）"。
        #   而程序其实**早就加载并在跑了** —— 这条假报警把现场一路引向
        #   "去示教器找程序 / 退出文件管理器"的错误方向，真实原因在判据本身。
        #   正确判据：目标程序号已就位，且处于「已加载」或「正在运行」任一态。
        def _prog_ok(snapx) -> bool:
            if not snapx:
                return False
            try:
                if int(snapx.get("prog") or 0) != prog_no:
                    return False
            except (TypeError, ValueError):
                return False
            bb = snapx.get("bits") or {}
            return bool(bb.get("prog_loaded") or bb.get("run"))

        if (not stopped_for_alarm) and _prog_ok(snap):
            self._step(steps, "prog", on_step, True,
                       "程序 %d 已在加载/运行态（跳过加载）" % prog_no)
        else:
            # ★ 刚为清报警 STOP 过 → 加载状态已被打断，**必须重新 LOAD**，
            #   否则紧接着的 CMD_RUN 起不来（实测：跳过加载 → 运行位 2.5s 不置位）。
            #   （不额外上报中间步骤，避免出现两条同名的 prog 步骤；原因写进最终文案。）
            why_reload = "（因清报警停过程序，强制重新加载）" if stopped_for_alarm else ""
            _, e1 = mb.write_reg(ADDR_SET_PROG, prog_no)    # 40104 目标程序号
            if e1:
                return {"ok": False, "error": "写目标程序号失败: %s" % e1, "steps": steps}
            _, e2 = mb.rc_command(CMD_LOAD)       # 0x1011 加载
            if e2:
                return {"ok": False, "error": "加载命令失败: %s" % e2, "steps": steps}
            t0 = time.time()
            loaded = False
            s2 = None
            while time.time() - t0 < 3.0:
                s2, _ = mb.rc_snapshot()
                if _prog_ok(s2):
                    loaded = True
                    break
                time.sleep(0.1)
            got = (s2 or {}).get("prog")
            self._step(steps, "prog", on_step, loaded,
                       ("程序 %d 已就位（%.2fs）%s"
                        % (prog_no, time.time() - t0, why_reload)) if loaded
                       else "程序 %d 未就位（目标程序号仍为 %s）" % (prog_no, got))
            if not loaded:
                return {"ok": False,
                        "error": ("程序 %d 未加载成功：控制器目标程序号仍为 %s（未切到 %d）。"
                                  "可能原因：① 控制器上确实没有该程序号；"
                                  "② 控制器有报警未清（如 5005 加载错误）；"
                                  "③ 示教器停在文件管理/编辑界面时拒绝远程加载。"
                                  "请在示教器确认程序 %d 存在、退出文件界面后重试"
                                  % (prog_no, got, prog_no, prog_no)),
                        "steps": steps}

        # 7) 运行（程序会执行到 WAIT 挂起等触发 —— 不产生任何运动）
        _, rerr = mb.rc_command(CMD_RUN)          # 0x1013
        if rerr:
            return {"ok": False, "error": "运行命令失败: %s" % rerr, "steps": steps}
        t0 = time.time()
        running = False
        while time.time() - t0 < 2.5:
            s2, _ = mb.rc_snapshot()
            if s2 and s2["bits"]["run"]:
                running = True
                break
            time.sleep(0.1)
        self._step(steps, "run", on_step, running,
                   "程序 %d 运行中（挂起于 WAIT 等触发）" % prog_no if running
                   else "运行位 2.5s 内未置位")
        if not running:
            return {"ok": False, "error": "程序运行位未置位，请检查程序状态", "steps": steps}

        # 8) 终态汇总
        fin, _ = mb.rc_snapshot()
        summary = {
            "prog": prog_no,
            "mode": fin["mode"] if fin else "",
            "servo": bool(fin and fin["bits"]["servo"]),
            "loaded": bool(fin and fin["bits"]["prog_loaded"]),
            "running": bool(fin and fin["bits"]["run"]),
            "jog_done": fin.get("jog_done") if fin else None,
        }
        from app.services.events import emit as emit_event
        emit_event("control", "info", "control.ready",
                   "一键就绪完成：程序 %d 已挂起于 WAIT" % prog_no,
                   {"steps": steps, "summary": summary})
        return {"ok": True, "steps": steps, "summary": summary}


    # ------------------------------------------------------------------
    def cancel(self, servo_off: bool = False) -> Dict[str, Any]:
        """取消就绪：停止运行中的程序，可选关闭伺服。

        ★ 与 ready() **共用同一对互斥**（P1-A11 同款理由）：本函数要写
          CMD_STOP / CMD_ZERO，若不拿 _exec_lock 就能插进飞行中的点动/下发序列，
          会复现"触发位置 1 的同时指令字被改写"那类谁也说不清的半截状态。
          _busy_lock 则保证两个"取消就绪"不会并发写同一指令字。
        :param servo_off: 是否顺带伺服下电（默认 False —— 停程序 ≠ 断伺服）
        """
        if not self._busy_lock.acquire(blocking=False):
            return {"ok": False, "error": "就绪/取消流程正在执行中，请稍候", "steps": []}
        try:
            from app.services.motion import motion
            if not motion._exec_lock.acquire(blocking=False):
                return {"ok": False,
                        "error": "有点动/下发正在执行，已推迟取消就绪（避免半截状态）",
                        "steps": []}
            try:
                return self._cancel(servo_off)
            finally:
                motion._exec_lock.release()
        finally:
            self._busy_lock.release()

    # ------------------------------------------------------------------
    def _cancel(self, servo_off: bool) -> Dict[str, Any]:
        if not real_write_enabled():
            return {"ok": False, "error": "真实下发未开启：总闸未打开，拒绝写控制器",
                    "steps": []}

        steps: List[dict] = []
        mb = self._client()

        def _step(name: str, ok: bool, msg: str) -> None:
            self._step(steps, name, None, ok, msg)

        # 1) 读快照
        snap, err = mb.rc_snapshot()
        if snap is None:
            _step("snapshot", False, "读快照失败: %s" % err)
            return {"ok": False, "error": "无法读取控制器状态: %s" % err, "steps": steps}

        # 1.5) ★ 撤触发位：取消就绪应把控制器留在"干净"状态（不残留执行意图）。
        #   与 _ready 的同类步骤一致：写 0 只是撤销意图，不产生运动。
        if snap.get("jog_trig"):
            _, e_trig = mb.write_reg(ADDR_RO_TRIG, 0x0000)
            _step("trig_clear", e_trig is None,
                  "已撤销点动触发位" if e_trig is None else "撤销触发位失败: %s" % e_trig)

        # 2) 停止程序 (CMD_STOP = 0x1005，与 /control/estop 停止沿同一条通道)
        _, serr = mb.rc_command(CMD_STOP)
        if serr:
            _step("stop", False, "停止命令失败: %s" % serr)
            return {"ok": False, "error": "停止程序失败: %s" % serr, "steps": steps}
        time.sleep(0.3)
        snap2, err2 = mb.rc_snapshot()
        if snap2 is None:
            # 命令已写进去了，只是回读不到 —— 不能谎报成功，也不武断判失败：
            #   交给下一次 rc_status 轮询确认（前端就绪卡 4s 后自己会变）。
            _step("stop", True, "停止命令已下发（回读失败: %s）" % err2)
        else:
            running = bool(snap2["bits"]["run"])
            _step("stop", not running,
                  "程序已停止" if not running else "运行位仍为 1（停止未生效）")
            if running:
                # ★ 步骤已经 ok=False，整体就不能再回 ok=True ——
                #   否则前端拿到 ok:true 却显示步骤红叉，用户无从判断该不该重试。
                return {"ok": False,
                        "error": "停止命令已下发但运行位仍为 1，请检查控制器档位/程序状态",
                        "steps": steps}

        # 3) 可选：伺服下电。
        #    ★★ 2026-10-08 修复（用户反馈"取消就绪伺服还是上电的状态"）：
        #    原实现只写 CMD_ZERO=0x0000 —— 单写全清**不足以取消伺服使能**：
        #      官方手册 2.2 明确定义 Bit13=取消使能，而命令字约定"始终保留 Bit0+Bit12"，
        #      0x0000 只是把指令字清空，并不表达"取消使能"这个动作。
        #    必须按参考实现 efort_client.servo_disable()：**先 0x2000 造下降沿 → 再 0x0000 全清**。
        #    ★ 0x0000 控制器常不回正常应答（超时/异常）但已生效 → 这步容忍失败。
        #    ★ 另补**回读校验**：原实现写完就报"伺服已下电"，从不核对状态位，
        #      于是"实际没下电"也显示成功 —— 用户看到的那条假成功就是这么来的。
        if servo_off:
            # =====================================================================
            # ★★ 2026-10-09 修复"取消就绪偶发失败"（现场报：已下发 0x2000→0x0000，
            #    但伺服状态位 2.1s 内仍未落下；同一序列**有时成功有时失败**）。
            #
            # 根因（对照本仓上电序列得出，不是猜的）：
            #   上电是 `0x0000 → **等 0.6s** → 0x1001`，注释写明"吸合延迟实测 ≈0.55s"；
            #   而原下电是 `0x2000 → **立即** 0x0000`，中间**没有等待**。
            #   ⇒ 0x0000（全清）很可能把**还没执行完的"取消使能"意图一起清掉**，
            #     于是下电不生效。赶上控制器先执行了就成功（实测有 0.03s 就落的），
            #     没赶上就失败 —— 这正是"间歇性"的来源。
            #
            # 修法（两条）：
            #   ① **0x2000 与 0x0000 之间加等待**：先轮询等状态位落下（最多 1.5s），
            #      落下后再写 0x0000 把命令字清干净（保留原设计意图）；
            #   ② **失败自动重试一轮**：偶发不响应时重发即可，且**下电是安全方向**动作，
            #      重发无风险。
            # 另补：0x0000 的返回值以前**直接丢掉** → 无法区分"写失败"还是"写了没生效"，
            #      现在两个命令的返回都记进步骤消息，现场一眼能看出是哪一种。
            # =====================================================================
            trace: List[str] = []
            servo_down = False
            last_servo = None
            t_all = time.time()
            for attempt in (1, 2):
                _, e_dis = mb.rc_command(CMD_DISABLE)      # 0x2000 Bit13 取消使能
                if e_dis:
                    trace.append("第%d次 0x2000 被拒(%s)" % (attempt, e_dis))
                else:
                    trace.append("第%d次 0x2000 已写" % attempt)
                # ① 先等"取消使能"真正生效（照上电 0.6s 吸合延迟的口径，给到 1.5s）
                t0 = time.time()
                while time.time() - t0 < 1.5:
                    s3, _ = mb.rc_snapshot()
                    if s3 is not None:
                        last_servo = s3["bits"].get("servo")
                        if not last_servo:
                            servo_down = True
                            break
                    time.sleep(0.1)
                # ② 状态位已落 → 再把命令字清干净（这一步容忍无应答，与参考实现一致）
                _, e_zero = mb.rc_command(CMD_ZERO)
                trace.append("第%d次 0x0000 %s" % (attempt, e_zero or "ok"))
                if servo_down:
                    break
                # 没落 → 再确认一次（给控制器一点余量），仍没落就进下一轮重试
                time.sleep(0.2)
                s3, _ = mb.rc_snapshot()
                if s3 is not None:
                    last_servo = s3["bits"].get("servo")
                    if not last_servo:
                        servo_down = True
                        break
            if not servo_down:
                _step("servo_off", False,
                      "已下发 0x2000→0x0000（重试 2 轮，%.1fs），伺服状态位仍未落下"
                      "（现值 %s）｜%s" % (time.time() - t_all, last_servo, "；".join(trace)))
                return {"ok": False,
                        "error": ("伺服下电命令已下发（0x2000→0x0000，重试 2 轮），但控制器"
                                  "伺服状态位仍为 %s，下电未生效。请检查是否有运行中程序/"
                                  "外部使能，或现场确认后重试。｜%s"
                                  % (last_servo, "；".join(trace))),
                        "steps": steps}
            _step("servo_off", True,
                  "伺服已下电并回读确认（状态位已落，%.2fs）｜%s"
                  % (time.time() - t_all, "；".join(trace)))

        return {"ok": True, "steps": steps}


    # ------------------------------------------------------------------
    def clear_alarm(self, on_step=None) -> Dict[str, Any]:
        """仅清报警（不重新上电/加载/运行）。

        ★ 区别于 ready()：ready() 把清报警当成"推到就绪态"的一环，清完会继续
          上伺服→加载→运行。本函数**只清报警**，清完即返回，把"重新就绪"
          留给用户点「一键就绪」去做。
        ★ 铁律（2026-09-29 实机）：清报警前若程序在跑，必须 CMD_STOP 先停，
          否则报警清掉 0.5s 内被运行中程序顶回（5005 复现）。
        ★ 清报警后伺服可能掉电 —— 那是「一键就绪」的职责，本函数不替它做。
        """
        steps: List[Dict] = []
        if not real_write_enabled():
            self._step(steps, "gate", on_step, False,
                       "真实下发未开启，拒绝写控制器")
            return {"ok": False,
                    "error": "真实下发未开启：总闸未打开，拒绝写控制器",
                    "steps": steps}
        if not self._busy_lock.acquire(blocking=False):
            return {"ok": False, "error": "就绪/清除流程正在执行中，请稍候", "steps": steps}
        try:
            from app.services.motion import motion
            if not motion._exec_lock.acquire(blocking=False):
                return {"ok": False,
                        "error": "有点动/下发正在执行，已推迟清除（避免半截状态）",
                        "steps": steps}
            try:
                return self._clear_alarm(steps, on_step)
            finally:
                motion._exec_lock.release()
        finally:
            self._busy_lock.release()

    def _clear_alarm(self, steps, on_step) -> Dict[str, Any]:
        mb = self._client()
        snap, err = mb.rc_snapshot()
        if snap is None:
            self._step(steps, "snapshot", on_step, False, "读快照失败: %s" % err)
            return {"ok": False, "error": "无法读取控制器状态: %s" % err, "steps": steps}
        b = snap["bits"]
        a1, a2 = snap.get("alarm1"), snap.get("alarm2")
        self._step(steps, "snapshot", on_step, True,
                   "模式=%s 伺服=%d 报警=%d 程序=%d 运行=%d"
                   % (snap["mode"], b["servo"], b["alarm"], snap["prog"], b["run"]))
        if not b["alarm"]:
            return {"ok": True, "cleared": False, "was_alarm": False,
                    "message": "当前无报警（码 %s/%s），无需清除" % (a1, a2),
                    "steps": steps}
        # ★ 有报警：若在跑先停（铁律：否则 0.5s 内被顶回）
        if b.get("run"):
            _, sterr = mb.rc_command(CMD_STOP)
            if sterr:
                self._step(steps, "stop", on_step, False, "停止程序失败: %s" % sterr)
                return {"ok": False,
                        "error": "停掉当前程序失败: %s（清报警前需先停机）" % sterr,
                        "steps": steps}
            t0 = time.time(); stopped = False
            while time.time() - t0 < 2.0:
                s2, _ = mb.rc_snapshot()
                if s2 and not s2["bits"].get("run"):
                    stopped = True; break
                time.sleep(0.1)
            self._step(steps, "stop", on_step, stopped,
                       "已停机（%.2fs），运行中程序会把报警顶回，必须先停再清"
                       % (time.time() - t0) if stopped
                       else "停机后运行位 2s 内未落下")
            if not stopped:
                return {"ok": False,
                        "error": "程序仍在运行（运行位未落下），无法可靠清报警",
                        "steps": steps}
        # ★ 重试清报警（0x1009 后控制器需时间落定报警位）
        cleared = False; snap2 = None
        for _ in range(3):
            _, cerr = mb.rc_command(CMD_CLEAR)
            if cerr:
                self._step(steps, "alarm", on_step, False, "清报警命令失败: %s" % cerr)
                break
            time.sleep(0.6)
            snap2, _ = mb.rc_snapshot()
            if snap2 and not snap2["bits"]["alarm"]:
                cleared = True; break
        if not cleared:
            na1 = (snap2 or snap).get("alarm1", "?"); na2 = (snap2 or snap).get("alarm2", "?")
            hints = []
            for raw in (na1, na2):
                try:
                    ci = int(raw)
                except (TypeError, ValueError):
                    ci = None
                if ci in ALARM_HINTS:
                    hints.append(ALARM_HINTS[ci])
                elif ci not in (None, 0):
                    hints.append("报警 %s：请按示教器报警明细排查" % raw)
            hint = "；".join(dict.fromkeys(hints)) or "请按示教器报警信息排查，处理后重试"
            self._step(steps, "alarm", on_step, False,
                       "清报警失败（码 %s/%s 仍在）" % (na1, na2))
            return {"ok": False,
                    "error": "清报警后仍处于报警状态（当前码 %s/%s）。%s"
                             % (na1, na2, hint),
                    "steps": steps}
        self._step(steps, "alarm", on_step, True, "报警已清（原码 %s/%s）" % (a1, a2))
        return {"ok": True, "cleared": True, "was_alarm": True,
                "message": "报警已清（原码 %s/%s）；伺服可能已掉电，需重新「一键就绪」再动作"
                           % (a1, a2),
                "steps": steps}

