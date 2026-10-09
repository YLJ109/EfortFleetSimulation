# -*- coding: utf-8 -*-
"""「取消就绪 · 伺服下电」的**时序**回归（P0）。

现场故障（2026-10-09）：
    取消就绪 → `已下发 0x2000→0x0000，但伺服状态位 2.1s 内仍未落下（现值 1）`
    且**同一序列有时成功有时失败**（实测：14:37:48 失败 / 14:38:54 成功 0.03s / 14:46:46 失败）。

根因（对照本仓的上电序列得出，不是猜的）：
    · 上电是 `0x0000 → **等 0.6s** → 0x1001`，注释写明"吸合延迟实测 ≈0.55s"；
    · 而原下电是 `0x2000 → **立即** 0x0000`，中间**没有等待** ⇒
      0x0000（全清）会把**还没执行完的"取消使能"意图一起清掉** → 下电不生效；
      赶上控制器先执行了就成功、没赶上就失败 —— 这就是"间歇性"的来源。

本文件用一个**有状态的假控制器**复刻这套时序语义，把修法钉住：
    ① 0x2000 与 0x0000 之间必须**留出等待**（不能立即全清）；
    ② 失败要**自动重试**（偶发不响应，且下电是安全方向）；
    ③ 诊断要可见（0x2000/0x0000 的返回都要记进步骤消息，能区分"写失败"与"写了没生效"）。
"""
from __future__ import annotations

import app.services.rc_ready as R


class _TimedMB:
    """复刻真实控制器的时序语义的假 client。

    · `0x2000`（取消使能）→ 启动一个"待生效"动作：需要 `latency_polls` 次
      `rc_snapshot()` 轮询后才把 servo 位落下（模拟 ≈0.55s 的机械/扫描延迟，测试里压成几次轮询）。
    · `0x0000`（全清）在动作**落下之前**写入 → **取消该未完成动作**（这正是现场失败机制）。
    · 落下之后再写 `0x0000` 无害。

    另记录命令调用序列，供断言"有没有留等待"。
    """

    def __init__(self, servo: int = 1, latency_polls: int = 2, zero_err=None):
        self.servo = servo
        self.latency_polls = latency_polls
        self.zero_err = zero_err
        self._pending = 0          # >0 = 有待生效的取消使能
        self.calls: list = []      # 命令序列（含 0x0000）

    # --- 命令字 ---
    def rc_command(self, word: int):
        self.calls.append(word)
        if word == R.CMD_DISABLE:                 # 0x2000
            self._pending = self.latency_polls
        elif word == R.CMD_ZERO:                  # 0x0000
            if self._pending > 0:
                self._pending = 0                 # ★ 打断未完成的取消使能
            if self.zero_err:
                return None, self.zero_err
        return 0, None

    # --- 快照（每次轮询推进"延迟"）---
    def rc_snapshot(self):
        if self._pending > 0:
            self._pending -= 1
            if self._pending == 0:
                self.servo = 0                    # 延迟结束 → 状态位真的落下
        return {"bits": {"run": 0, "servo": self.servo}, "jog_trig": False}, None

    def write_reg(self, *_a, **_k):
        return 0, None


def _patch(monkeypatch):
    monkeypatch.setattr(R, "real_write_enabled", lambda: True)
    # 把轮询间隔压到 0，测试不必真等 1.5s（时序语义靠"轮询次数"而不是墙钟推进）
    monkeypatch.setattr(R.time, "sleep", lambda *_a, **_k: None)


def test_servo_off_waits_before_full_clear(monkeypatch):
    """★★ 核心：0x2000 之后必须**先等状态位落下**，再写 0x0000。

    在"0x0000 会打断未完成动作"的控制器上，旧实现（立即全清）必然失败 —— 现场故障复现。
    """
    _patch(monkeypatch)
    mb = _TimedMB(servo=1, latency_polls=2)
    r = R.ReadinessService(modbus=mb).cancel(servo_off=True)
    assert r["ok"] is True, "留出等待后应当能下电成功：%s" % r.get("error")
    assert any(s["step"] == "servo_off" and s["ok"] is True for s in r["steps"])

    # ★ 命令顺序：0x2000 必须早于 0x0000（语义不能反）
    i_dis = mb.calls.index(R.CMD_DISABLE)
    i_zero = mb.calls.index(R.CMD_ZERO)
    assert i_dis < i_zero, "先取消使能、再全清"
    # ★ 且在两者之间确实发生了轮询（= 留了等待）——用"快照被读到了"间接断言
    assert mb.servo == 0, "状态位最终应当落下"


def test_old_sequence_would_fail_on_this_controller(monkeypatch):
    """★★ 反证：证明上面那个假控制器**真的能复现现场故障**，用例不是恒真的。

    手工重放"旧实现"的序列（0x2000 → 立即 0x0000，不等待），必须下电失败。
    """
    mb = _TimedMB(servo=1, latency_polls=2)
    mb.rc_command(R.CMD_DISABLE)      # 0x2000
    mb.rc_command(R.CMD_ZERO)         # 立即全清（旧实现）→ 打断
    assert mb.servo == 1, "立即全清必须导致下电不生效（这正是现场 2.1s 未落下的机制）"


def test_servo_off_retries_when_controller_ignores_first_attempt(monkeypatch):
    """★ 偶发不响应 → 自动重试一轮必须能救回来（下电是安全方向，重发无风险）。"""

    class _StubbornMB(_TimedMB):
        """第一次 0x2000 完全被忽略（不启动待生效动作），第二次才正常。"""

        def __init__(self):
            super().__init__(servo=1, latency_polls=2)
            self.dis_seen = 0

        def rc_command(self, word):
            if word == R.CMD_DISABLE:
                self.dis_seen += 1
                self.calls.append(word)
                if self.dis_seen == 1:
                    return 0, None          # 第一次：写了但控制器**不响应**
                self._pending = self.latency_polls
                return 0, None
            return super().rc_command(word)

    _patch(monkeypatch)
    mb = _StubbornMB()
    r = R.ReadinessService(modbus=mb).cancel(servo_off=True)
    assert mb.dis_seen >= 2, "必须重发 0x2000（重试）"
    assert r["ok"] is True, "重试后应当成功：%s" % r.get("error")


def test_failure_message_carries_command_trace(monkeypatch):
    """★ 失败时要能看出"是哪一种失败"：两个命令的返回都必须记进消息。

    以前 0x0000 的返回值**直接丢掉** → 现场无法区分"写失败"还是"写了没生效"。
    """
    _patch(monkeypatch)

    class _DeadMB(_TimedMB):
        """完全不响应取消使能（状态位永远是 1）。"""

        def rc_command(self, word):
            self.calls.append(word)
            if word == R.CMD_ZERO and self.zero_err:
                return None, self.zero_err
            return 0, None      # 0x2000 也不生效

    mb = _DeadMB(servo=1, latency_polls=9999, zero_err="注入：0x0000 超时")
    r = R.ReadinessService(modbus=mb).cancel(servo_off=True)
    assert r["ok"] is False
    msg = r["error"] + " ".join(str(s.get("msg") or "") for s in r["steps"])
    assert "0x2000" in msg, "诊断里要写清发了 0x2000"
    assert "0x0000" in msg, "诊断里要写清发了 0x0000"
    assert "超时" in msg, "0x0000 的返回（错误）必须被记录，否则分不清是哪种失败"
    assert "重试" in msg or "轮" in msg, "要说明重试了几轮"


def test_success_message_also_carries_trace(monkeypatch):
    """成功时也带上命令轨迹（便于现场对比"成功那次"和"失败那次"差在哪）。"""
    _patch(monkeypatch)
    mb = _TimedMB(servo=1, latency_polls=1)
    r = R.ReadinessService(modbus=mb).cancel(servo_off=True)
    assert r["ok"] is True
    msg = " ".join(str(s.get("msg") or "") for s in r["steps"])
    assert "0x2000" in msg and "0x0000" in msg


def test_servo_off_not_touched_when_flag_off(monkeypatch):
    """不带 servo_off 时**不许碰**伺服命令（默认只做"最轻的那一步"）。"""
    _patch(monkeypatch)
    mb = _TimedMB(servo=1)
    r = R.ReadinessService(modbus=mb).cancel(servo_off=False)
    assert r["ok"] is True
    assert R.CMD_DISABLE not in mb.calls, "不该发取消使能"
    assert R.CMD_ZERO not in mb.calls, "不该发全清"


def test_no_zero_write_between_dis_and_servo_drop(monkeypatch):
    """★★ 顺序钉死：`0x2000` 之后、状态位落下之前**不得**出现 `0x0000`。

    这是本次修复的本质 —— 用"落下前不许全清"这一条把实现锁死，
    而不是只断言"最终成功"（那样换个写法也能过）。
    """
    _patch(monkeypatch)

    class _WatchedMB(_TimedMB):
        def __init__(self):
            super().__init__(servo=1, latency_polls=3)
            self.violation = None

        def rc_command(self, word):
            if word == R.CMD_ZERO and self._pending > 0:
                self.violation = "状态位落下前就写了 0x0000（会打断取消使能）"
            return super().rc_command(word)

    mb = _WatchedMB()
    r = R.ReadinessService(modbus=mb).cancel(servo_off=True)
    assert mb.violation is None, mb.violation
    assert r["ok"] is True
