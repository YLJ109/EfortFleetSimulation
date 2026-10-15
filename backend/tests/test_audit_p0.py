# -*- coding: utf-8 -*-
"""审计修复 P0/P1 回归钉。

★ 本文件全部在 conftest 强制的 EFORT_SIMULATE=always + EFORT_REAL_MOTION=0
  下运行：不连 Modbus、不写任何寄存器、不动机器人。
  凡是会走到 socket 的路径，一律用 monkeypatch 打桩。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select


# =====================================================================
# P0-1 角度编码 int16 溢出
# =====================================================================
def test_encode_angle_normal_range():
    from app.services.modbus import ModbusRobot
    assert ModbusRobot.encode_angle(0.0) == 0
    assert ModbusRobot.encode_angle(180.0) == 18000
    assert ModbusRobot.encode_angle(-180.0) == (-18000) & 0xFFFF
    assert ModbusRobot.encode_angle(327.67) == 32767
    assert ModbusRobot.encode_angle(-327.68) == -32768 & 0xFFFF


def test_encode_angle_rejects_overflow():
    """★ P0-1：超出 ±327.67° 必须抛错，绝不允许 `& 0xFFFF` 静默回绕反号。"""
    from app.services.modbus import ModbusRobot
    for bad in (350.0, -350.0, 327.68, -327.69, 1000.0):
        with pytest.raises(ValueError):
            ModbusRobot.encode_angle(bad)
    # 回归：原实现 350° → 35000 → 回绕成 -305.36°，方向完全相反
    with pytest.raises(ValueError):
        ModbusRobot.encode_angle(350.0)


def test_echo_match_is_signed():
    """★ P0-1：回读比对必须按有符号值解码（比无符号字会把回绕值判成一致）。"""
    from app.services.modbus import ModbusRobot
    r = ModbusRobot()
    want = ModbusRobot.encode_angle(-180.0)          # 47536（补码）
    assert r._echo_matches(want, want) is True       # 控制器回读同一字 → 一致
    assert r._echo_matches(100, want) is False       # 明显不一致
    assert r._echo_matches(None, want) is False      # 读不到 ≠ 一致
    assert ModbusRobot._to_signed(47536) == -18000


def test_write_reg_echo_reads_value_not_address(monkeypatch):
    """★★ 真实事故：FC6 回显取错字节 → 速度设定回读不一致（写 17% 读回 26112%）。

    帧布局：MBAP 7 字节（[6]=单元号）→ [7]功能码 → [8:10]**寄存器地址** → [10:12]**寄存器值**。
    原实现取 `data[9:11]`：写 40103=17%（地址 102=0x0066、值 17=0x0011）会被解成
    0x6600 = 26112 → motion 的 B-03 校验误判"写 17% 读回 26112%"。值必须在 [10:12]。
    """
    import struct as _s
    from app.services.modbus import ModbusRobot, ADDR_SET_SPEED
    assert ADDR_SET_SPEED == 102                     # 复现用例里的地址

    r = ModbusRobot()

    class _Sock:
        def __init__(self):
            self.sent = b""

        def sendall(self, b):
            self.sent = b

    sock = _Sock()
    frame = _s.pack(">HHHB", 1, 0, 6, r.uid) + _s.pack(">BHH", 6, ADDR_SET_SPEED, 17)
    monkeypatch.setattr(r, "_ensure_sock", lambda: sock, raising=False)
    monkeypatch.setattr(r, "_recv_frame", lambda s: frame, raising=False)

    echo, err = r.write_reg(ADDR_SET_SPEED, 17)
    assert err is None
    assert echo == 17, "回显应为写入值 17，实得 %r（说明又取到地址字节了）" % (echo,)

    # 反向钉：旧实现会得到 0x6600 = 26112
    assert _s.unpack(">H", frame[9:11])[0] == 26112


def test_jog_target_rejects_overflow_before_any_write(monkeypatch):
    """★ P0-1：必须在**写第一个寄存器之前**就拒绝，不能写一半才失败。"""
    from app.services.modbus import ModbusRobot
    r = ModbusRobot()
    calls = []

    def _boom(*a, **k):                     # 一旦被调用即证明"先写后校验"
        calls.append(a)
        return None, "should not be called"

    monkeypatch.setattr(r, "write_reg", _boom)
    ok, err = r.rc_write_jog_target([350.0, 0, 0, 0, 0, 0])
    assert ok is False
    assert "超出" in (err or "")
    assert calls == []


# =====================================================================
# P0-2 执行序列互斥 + P1-A7/A8 + NaN 防线
# =====================================================================
def test_command_rejected_while_exec_locked():
    """★ P0-2 回归钉：执行锁被占用时，新下发必须直接拒绝而不是排队交错执行。"""
    from app.services.motion import motion
    assert motion._exec_lock.acquire(blocking=False)
    try:
        r = motion.command([0, 0, 0, 0, 0, 0], 10)
        assert r["ok"] is False
        assert r.get("busy") is True
    finally:
        motion._exec_lock.release()
    # 锁释放后应恢复正常
    r2 = motion.command([0, 0, 0, 0, 0, 0], 10)
    assert r2["ok"] is True


def test_in_limits_rejects_nan_and_bad_shape():
    """★ P2：NaN 原来会绕过 `v < lo` / `v > hi`（比较恒 False）被判"限位内"。"""
    from app.services.motion import motion
    assert motion._in_limits([0.0] * 6) is True
    nan_q = [float("nan")] + [0.0] * 5
    assert motion._in_limits(nan_q) is False
    assert motion._in_limits([float("inf")] + [0.0] * 5) is False
    r = motion.command([float("nan")] * 6, 10)
    assert r["ok"] is False
    assert motion.command([0, 0, 0], 10)["ok"] is False       # 长度必须是 6


def test_max_displacement_prefers_cur_pose():
    """★ P1-A2：超时应按**位移**估算；没有当前位姿时退回绝对角（只更宽松）。"""
    from app.services.modbus import ModbusRobot
    tgt = [0.0, -90.0, 0, 0, 0, 45.0]
    # 有当前位姿：J2 本来就在 -90，位移只由 J6 决定
    assert ModbusRobot._max_displacement(tgt, [0, -90, 0, 0, 0, 0]) == 45.0
    # 无当前位姿：退回绝对角口径（=90）
    assert ModbusRobot._max_displacement(tgt, None) == 90.0


# =====================================================================
# P0-4/P0-5 分批清理与聚合统计
# =====================================================================
def _count(session, model) -> int:
    return int(session.execute(select(func.count(model.id))).scalar() or 0)


def test_prune_poses_batched_crosses_batches(client):
    """★ P0-4：删除必须跨批次完整执行，且单批事务不锁死整表。"""
    from app.db.crud import prune_old_poses
    from app.db.database import SessionLocal
    from app.db.models import PoseHistory

    s = SessionLocal()
    try:
        # ★ 审计修复 P1-E13：这里**故意不取**删除前的总数。下面已说明原因 ——
        #   后台采集线程在测试窗口里会持续入库，"总数严格相等"是错的判据；
        #   上一版留着一个从没用上的 `before` 变量，会让人误以为断言过它。
        old = datetime.now(timezone.utc) - timedelta(days=400)
        rows = [PoseHistory(timestamp=old, j1=0, j2=0, j3=0, j4=0, j5=0, j6=0,
                            tcp_x=0, tcp_y=0, tcp_z=0, source="real")
                for _ in range(2500)]
        s.add_all(rows)
        s.commit()

        # batch=1000 → 必须走 3 才能删完（证明不是"只删一批"）
        n = prune_old_poses(s, 30, batch=1000)
        assert n == 2500
        # ★ 不能断言"总数严格等于 before"：session 级 client 的采集线程在模拟模式下
        #   以 db_write_hz 持续入库，本测试的窗口里可能正好插进新姿态。
        #   正确的判据是"过期行一条不剩"，而不是"总数没变"。
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        leftover = int(s.execute(
            select(func.count(PoseHistory.id)).where(PoseHistory.timestamp < cutoff)
        ).scalar() or 0)
        assert leftover == 0

        # days<=0 不误删：先取"近期行"总数，跑一次 days=0 的清理，行数必须不减
        recent_sel = select(func.count(PoseHistory.id)).where(PoseHistory.timestamp >= cutoff)
        recent_before = int(s.execute(recent_sel).scalar() or 0)
        assert prune_old_poses(s, 0, batch=1000) == 0
        recent_after = int(s.execute(recent_sel).scalar() or 0)
        assert recent_after >= recent_before
    finally:
        s.close()


def test_system_event_stats_uses_sql_aggregation(client):
    """★ P1-C1：统计不得整表 ORM 加载（原实现 select(SystemEvent) 全量拉取）。"""
    from app.db.crud import system_event_stats
    from app.db.database import SessionLocal

    s = SessionLocal()
    try:
        st = system_event_stats(s)
        assert set(st) == {"total", "by_category", "by_level"}
        assert st["total"] == sum(st["by_category"].values())
        assert st["total"] == sum(st["by_level"].values())
        # 不带 since 与带 since-未来的差异必须自洽
        future = datetime.now(timezone.utc) + timedelta(days=1)
        st2 = system_event_stats(s, since=future)
        assert st2["total"] == 0
    finally:
        s.close()


def test_prune_system_events_respects_days(client):
    """★ P1-C5：审计表必须有保留策略（原来只增不减）。"""
    from app.db.crud import prune_system_events
    from app.db.database import SessionLocal
    from app.db.models import SystemEvent

    s = SessionLocal()
    try:
        before = _count(s, SystemEvent)
        # days>0：不动新数据
        assert prune_system_events(s, 36500) == 0
        assert _count(s, SystemEvent) == before
    finally:
        s.close()


# =====================================================================
# P0-3 / 互锁 fail-safe 方向
# =====================================================================
def test_guard_stale_danger_fails_safe(client):
    """★ P2：过期时必须保留最后状态方向性 —— danger 之后掉线不能自动放行。"""
    from app.services import safety_guard as g
    g.update("danger", "z1", "主工作区")
    with g._lock:
        # P6 起状态按台存：单机链路的键是 g.SINGLE_ID（"MAIN"）
        g._latest[g.SINGLE_ID]["ts"] = time.time() - 9999      # 人为制造过期
    allowed, reason, _snap = g.check()
    assert allowed is False
    assert "过期" in reason and "danger" in reason
    # 清理：恢复新鲜安全态，避免影响后续测试
    g.update("safe")


def test_guard_accepts_safe_when_fresh(client):
    from app.services import safety_guard as g
    g.update("safe")
    allowed, _reason, snap = g.check()
    assert allowed is True and snap["fresh"] is True


def test_safety_live_endpoint_requires_token(client):
    """★ P0-3：匿名上报必须 401（原实现可架空围栏互锁）。"""
    r = client.post("/api/safety/live", json={"state": "safe"})
    assert r.status_code == 401


# =====================================================================
# P1-B2 / P1-B3 / P1-B4 —— 无鉴权写接口与登录限流
# =====================================================================
def test_reconnect_requires_token(client):
    """★ P1-B2：重连会做 TCP 探测并切换真实/模拟链路，匿名不可触发。"""
    assert client.post("/api/reconnect").status_code == 401
    assert client.post("/api/reconnect", headers={"X-Control-Token": "bogus"}).status_code == 401


def test_recording_writes_require_token(client):
    """★ P1-B3：录制的增/改/删/导入原来完全无鉴权。"""
    hdr = {"X-Control-Token": "bogus"}
    assert client.post("/api/recordings", json={"name": "x"}).status_code == 401
    assert client.put("/api/recordings/1", json={"name": "x"}).status_code == 401
    assert client.delete("/api/recordings/1").status_code == 401
    assert client.post("/api/recordings/import", json={"name": "x"}).status_code == 401
    # 带一个错令牌也必须被拒（不能只判断"头存在"）
    assert client.post("/api/recordings", json={"name": "x"}, headers=hdr).status_code == 401


def test_recording_frames_capped(client):
    """★ P1-B3：帧数组必须有上限，否则一次导入可撑爆内存。"""
    from app.api.recordings import RecordingIn
    with pytest.raises(Exception):
        RecordingIn(name="huge", frames=[{"t": i} for i in range(200_001)])


def test_login_rate_limited_and_bounded(client):
    """★ P1-B4：连续失败按 IP 限流 429；超长口令在 422 就被挡下（不进 PBKDF2）。"""
    from app.api import auth as auth_mod

    # 超长口令 → 422（Pydantic 层），绝不进入 10 万次哈希计算
    r = client.post("/api/auth/login", json={"password": "x" * 1000})
    assert r.status_code == 422
    r = client.post("/api/auth/login", json={"password": ""})
    assert r.status_code == 422

    # 连续失败：前 20 次是 401，第 21 次被限流 429
    codes = []
    for _ in range(21):
        codes.append(
            client.post("/api/auth/login", json={"password": "definitely-wrong"}).status_code
        )
    assert codes[:20] == [401] * 20
    assert codes[20] == 429

    # 限流期间，**正确**口令同样被拒（限流发生在比对之前，才挡得住爆破）
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 429

    # 清理：本测试故意把 testclient 这个 IP 打进黑名单，
    # 不清掉会连累后续用例（整个测试套件共享同一个来源 IP）。
    auth_mod._LOGIN_FAIL.clear()


# =====================================================================
# P1-A3 / A5 / A6 / A10 / A11 —— 采集与点动链路的 fail-safe
# =====================================================================
def test_ready_guard_fails_safe_when_trigger_unknown(monkeypatch):
    """★ P1-A3：触发位读不到（jog_trig=None）必须拒绝就绪，不能当 0 放行。"""
    import app.services.rc_ready as R

    monkeypatch.setattr(R, "real_write_enabled", lambda: True)

    class _MB:
        def rc_snapshot(self):
            return {
                "jog_trig": None,          # ← 读取失败（原实现: falsy → 放行）
                "bits": {"manual": 0, "auto": 1, "remote": 0, "servo": 1,
                         "alarm": 0, "estop": 0, "prog_loaded": 1, "run": 1},
                "mode": "auto", "prog": 0, "status_word": 0,
                "alarm1": 0, "alarm2": 0, "joints": [0.0] * 6,
            }, None

        def rc_command(self, w):     # 若守卫失效，流程会走到清报警 —— 不该走到
            raise AssertionError("触发位未知时不该继续往下写控制器")

    r = R.ReadinessService(modbus=_MB()).ready()
    assert r["ok"] is False
    assert "触发位" in r["error"] and "fail-safe" in r["error"]


def test_ready_rejected_while_dispatch_in_flight():
    """★ P1-A11：就绪流程（全清/加载/运行）不得插进飞行中的点动序列。"""
    from app.services.motion import motion
    from app.services.rc_ready import ReadinessService

    assert motion._exec_lock.acquire(blocking=False)
    try:
        r = ReadinessService().ready()
        assert r["ok"] is False
        assert "下发" in r["error"]
    finally:
        motion._exec_lock.release()
    # 释放后不再拦（真正的就绪流程仍会因总闸未开而拒绝，那是另一道闸）
    assert "正在执行" not in (ReadinessService().ready().get("error") or "")


# ---------------- /ready/cancel（取消就绪）：与 ready 同一套闸 ----------------
def test_cancel_rejected_while_dispatch_in_flight(monkeypatch):
    """★ 取消就绪同样要拿 _exec_lock —— 它写 CMD_STOP/CMD_ZERO，
    没锁就能插进飞行中的点动序列，复现 P1-A11 修掉的"半截状态"。"""
    import app.services.rc_ready as R
    from app.services.motion import motion

    monkeypatch.setattr(R, "real_write_enabled", lambda: True)
    writes = []

    class _MB:
        def rc_snapshot(self):
            return {"bits": {"run": 0}, "jog_trig": False}, None

        def rc_command(self, w):
            writes.append(w)
            return 0, None

    svc = R.ReadinessService(modbus=_MB())
    assert motion._exec_lock.acquire(blocking=False)
    try:
        r = svc.cancel()
        assert r["ok"] is False and "下发" in r["error"]
        assert writes == [], "锁没拦住时它已经写寄存器了"
    finally:
        motion._exec_lock.release()

    r = svc.cancel()
    assert r["ok"] is True, "锁释放后应能正常走完（总闸已由 monkeypatch 打开）"
    assert writes, "确实发出了停止命令"


def test_cancel_rejected_when_total_gate_closed():
    """★ 双确认总闸没开时必须拒绝 —— 取消就绪是真机写操作，与 /ready 同级。"""
    from app.services.rc_ready import ReadinessService
    r = ReadinessService().cancel(servo_off=True)
    assert r["ok"] is False and "总闸" in r["error"]


def test_cancel_does_not_report_success_when_stop_ineffective(monkeypatch):
    """★ 回读显示运行位仍为 1 → 必须 ok=False。
    原实现步骤 ok=False 但整体 ok=True，前端会拿到自相矛盾的结果。"""
    import app.services.rc_ready as R

    monkeypatch.setattr(R, "real_write_enabled", lambda: True)

    class _MB:
        def rc_snapshot(self):
            # 第一次读（前置）、第二次读（停止后回读）都返回"仍在运行"
            return {"bits": {"run": 1}, "jog_trig": False}, None

        def rc_command(self, w):
            return 0, None

    r = R.ReadinessService(modbus=_MB()).cancel()
    assert r["ok"] is False
    assert "运行位仍为 1" in r["error"]
    assert any(s["step"] == "stop" and s["ok"] is False for s in r["steps"])


def test_cancel_servo_off_failure_is_not_silent(monkeypatch):
    """★ 伺服下电必须**真下电**且不静默 —— 否则界面显示"已下电"而伺服还在。

    ★★ 2026-10-08 重写（原用例钉的是错的实现）：
      原用例断言"CMD_ZERO(0x0000) 写失败 → ok=False"。但按官方手册 2.2 与参考实现
      efort_client.servo_disable()：**下电要靠 0x2000(Bit13 取消使能)造下降沿**，
      0x0000 只是全清、且**控制器常不回正常应答但已生效** —— 把它当失败判据是错的
      （会把一次成功下电记成失败，也可能反过来掩盖"0x2000 根本没发"）。
      现在分三条真实判据：
        (a) 0x2000 写失败 → ok=False（真失败）；
        (b) 0x2000 已发但伺服状态位不落 → ok=False（不静默谎报成功）；
        (c) 0x2000 已发且状态位落下 → ok=True（此时 0x0000 失败也不影响）。
    """
    import app.services.rc_ready as R

    monkeypatch.setattr(R, "real_write_enabled", lambda: True)

    def _mb(servo_after, dis_err=None, zero_err=None):
        """servo_after: 回读时的伺服状态位值；dis_err/zero_err: 对应命令的注入错误。"""
        class _MB:
            def rc_snapshot(self):
                return {"bits": {"run": 0, "servo": servo_after}, "jog_trig": False}, None

            def rc_command(self, w):
                if w == R.CMD_DISABLE and dis_err:
                    return None, dis_err
                if w == R.CMD_ZERO and zero_err:
                    return None, zero_err
                return 0, None
        return _MB()

    # (a1) 0x2000 被拒 **且伺服仍在位** → 必须失败（真失败，不许静默）
    r = R.ReadinessService(modbus=_mb(1, dis_err="注入：写失败")).cancel(servo_off=True)
    assert r["ok"] is False, "命令被拒且状态位仍在 → 必须报失败"
    assert any(s["step"] == "servo_off" and s["ok"] is False for s in r["steps"])
    assert "0x2000" in r["error"], "诊断要写清是哪条命令被拒"

    # (a2) ★★ 2026-10-09 口径修正：0x2000 被拒**但伺服状态位本来就是 0** → 判**成功**。
    #   理由：取消就绪的目标（伺服下电）**已经达成**，此时报失败是**误报**。
    #   （旧用例断言这种情况必须失败，那是把"命令没发出去"与"目标没达成"混为一谈了。）
    #   命令被拒这件事仍要如实写进消息里，不许悄悄吞掉。
    r = R.ReadinessService(modbus=_mb(0, dis_err="注入：写失败")).cancel(servo_off=True)
    assert r["ok"] is True, "伺服已在位下时不该误报失败：%s" % r.get("error")
    _msg = " ".join(str(s.get("msg") or "") for s in r["steps"])
    assert "0x2000" in _msg and "被拒" in _msg, "命令被拒的事实必须留在诊断里"

    # (b) 0x2000 发出去了但伺服位仍是 1（下电没生效）→ 必须失败，不能谎报成功
    r = R.ReadinessService(modbus=_mb(1)).cancel(servo_off=True)
    assert r["ok"] is False and "伺服" in r["error"]
    assert any(s["step"] == "servo_off" and s["ok"] is False for s in r["steps"])

    # (c) 0x2000 发出、伺服位落下 → 成功；且 **0x0000 超时不算失败**
    #     （控制器对 0x0000 常不回正常应答，但命令已生效）
    r = R.ReadinessService(modbus=_mb(0, zero_err="注入：0x0000 超时")).cancel(servo_off=True)
    assert r["ok"] is True, "0x0000 无应答不应把成功下电记成失败"
    assert any(s["step"] == "servo_off" and s["ok"] is True for s in r["steps"])

    # 不带 servo_off 时不碰伺服命令（默认只做"最轻的那一步"）
    sent = []

    class _MB2:
        def rc_snapshot(self):
            return {"bits": {"run": 0, "servo": 1}, "jog_trig": False}, None

        def rc_command(self, w):
            sent.append(w)
            return 0, None

    r2 = R.ReadinessService(modbus=_MB2()).cancel()
    assert r2["ok"] is True
    assert R.CMD_ZERO not in sent and R.CMD_DISABLE not in sent, \
        "默认不该发伺服下电命令"


def test_collector_loop_survives_exception(monkeypatch):
    """★ P1-A5：采集循环异常只降级重启，绝不让唯一采集线程静默死亡（假活）。"""
    from app.services import collector as cmod

    monkeypatch.setattr(cmod, "_backoff", lambda n: 0.0)   # 不让测试真睡
    c = cmod.Collector()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] >= 3:
            c._running = False            # 第 3 次让它"正常"退出
        raise RuntimeError("注入的循环异常")

    monkeypatch.setattr(c, "_loop_impl", boom)
    c._running = True
    c._loop()                              # 全程不抛
    assert calls["n"] >= 3                 # 崩了两次都重启了


def test_read_failure_marks_stale_instead_of_fabricating():
    """★ P1-A6：真实读失败必须标 stale 并冻结上一帧，绝不用扫掠补帧冒充真机。"""
    import threading

    from app.services import collector as cmod

    class _FailMB:
        host = "127.0.0.1"

        def read_pose(self):
            return None, "注入的读失败"

        def reachable(self):
            return False

        def rc_snapshot(self):
            return None, "no"

    c = cmod.Collector()
    c.modbus = _FailMB()
    c.simulated = False          # 假装处于真实链路（simulate=always 只锁 decide_mode）
    c._running = True
    th = threading.Thread(target=c._loop, daemon=True)
    th.start()
    try:
        latest = None
        for _ in range(80):
            latest = c.get_latest()
            if latest and latest.get("stale"):
                break
            threading.Event().wait(0.05)
        assert latest is not None, "采集循环没有产出帧"
        assert latest["stale"] is True
        assert latest["simulated"] is False
    finally:
        c._running = False
        th.join(timeout=3.0)


def test_abort_check_threaded_into_dispatch(monkeypatch):
    """★ P1-A10：点动的停止判据要真的传进下发序列（能打断飞行中的那一发）。"""
    from app.services import collector as cmod
    from app.services import motion as M

    cap = {}
    # command() 会把实时真值写回 motion.real（P1-A7），测试里我把
    # real_write_enabled 换成恒 True，跑完必须把 real 还原，
    # 否则 "real=True" 会漏给后续用例（test_jog 会因此看到错误的模式）。
    real_before = M.motion.real
    monkeypatch.setattr(M.motion, "real", real_before, raising=False)

    class _MB:
        def write_reg(self, addr, value):
            return value, None

        def rc_jog_execute(self, joints, speed_pct=100, on_event=None,
                            should_abort=None, cur_joints=None):
            cap["should_abort"] = should_abort
            return False, "注入：被中止", {}

    monkeypatch.setattr(M, "real_write_enabled", lambda: True)
    monkeypatch.setattr(M.motion, "modbus", _MB())
    monkeypatch.setattr(cmod.collector, "get_latest",
                        lambda: {f"j{i}": 0.0 for i in range(1, 7)})
    # ★ 链路真实性守卫(B-02)：simulate=always / collector.simulated 时会拒绝写真机。
    #   本用例要验证"中止判据确实传进 rc_jog_execute"，必须把链路呈现为非模拟：
    #   1) 把 simulate 从 always 改成 auto（guard 只看 connection.simulate 是否
    #      == "always"），2) 强制 collector.simulated=False。
    #   采集循环在真实读取连续失败 30 次(~数秒)后才会把 simulated 翻回 True，
    #   同步执行的 command() 远早于那个窗口，故本用例窗口内链路恒为非模拟。
    monkeypatch.setenv("EFORT_SIMULATE", "auto")
    monkeypatch.setattr(cmod.collector, "simulated", False)

    r = M.motion.command([0, 0, 0, 0, 0, 0], 5, abort_check=lambda: True)
    assert r["ok"] is False and "中止" in r["error"]
    assert cap["should_abort"]() is True      # 调用方判据确实进了回调

    r = M.motion.command([0, 0, 0, 0, 0, 0], 5, abort_check=lambda: False)
    assert cap["should_abort"]() is False
    assert "已有下发" not in (r.get("error") or "")   # 锁已正常释放

    # 不传 abort_check 时退化为"只看急停"，不能因为 None 就误判中止
    M.motion.command([0, 0, 0, 0, 0, 0], 5)
    assert cap["should_abort"]() is False


def test_failed_dispatch_rolls_back_last_target(monkeypatch):
    """★★ 2026-09-29：下发失败必须回滚 motion.last_target ★★

    缺陷实景（真机复现）：一发失败的点动（完成位超时 / 被急停打断）机器人**没动**，
    但原实现在**下发前**就把 last_target 推前，且失败时不回滚。而 jog._current()
    优先取 last_target 作为下一次增量点动的基准 —— 于是"没发生过的位移"被累加：
        实测 J6 -5.98 → 目标 -4.98（失败，未动）→ 再按，目标变成 -3.98
    误差逐次放大，且与真实位姿越漂越远（下次真走起来就是一次跳动）。

    本用例钉死两条：① 失败后 last_target 回到下发前的值；② 不能把成功值也回滚掉。
    """
    from app.services import collector as cmod
    from app.services import motion as M

    state = {"fail": True}

    class _MB:
        def write_reg(self, addr, value):
            return value, None

        def rc_jog_execute(self, joints, speed_pct=100, on_event=None,
                           should_abort=None, cur_joints=None):
            if state["fail"]:
                return False, "注入：点动完成位超时", {}
            return True, None, {}

    # command() 会把实时真值写回 motion.real（P1-A7），跑完还原，别漏给后续用例
    real_before = M.motion.real
    monkeypatch.setattr(M.motion, "real", real_before, raising=False)
    monkeypatch.setattr(M, "real_write_enabled", lambda: True)
    monkeypatch.setattr(M.motion, "modbus", _MB())
    monkeypatch.setattr(cmod.collector, "get_latest",
                        lambda: {f"j{i}": 0.0 for i in range(1, 7)})
    # 链路真实性守卫(B-02)：必须让链路看起来不是模拟，否则 command() 直接拒绝
    monkeypatch.setenv("EFORT_SIMULATE", "auto")
    monkeypatch.setattr(cmod.collector, "simulated", False)

    M.motion.reset_estop()
    try:
        # ① 首次下发就失败 → last_target 必须仍是 None（不能留下一个"幽灵目标"）
        M.motion.last_target = None
        r = M.motion.command([10.0, 0, 0, 0, 0, 0], 5)
        assert r["ok"] is False, r
        assert M.motion.last_target is None, (
            "下发失败后 last_target 必须回滚到下发前的 None，实测=%r" % (M.motion.last_target,))

        # ② 成功一次落到目标，之后失败必须退回**成功值**而不是清空
        state["fail"] = False
        assert M.motion.command([10.0, 0, 0, 0, 0, 0], 5)["ok"] is True
        assert M.motion.last_target and M.motion.last_target[0] == 10.0

        state["fail"] = True
        assert M.motion.command([20.0, 0, 0, 0, 0, 0], 5)["ok"] is False
        assert M.motion.last_target[0] == 10.0, (
            "失败必须退回上一次**成功**的逻辑目标，实测=%r" % (M.motion.last_target,))
    finally:
        M.motion.last_target = None
        M.motion.reset_estop()


# =====================================================================
# P1-B6 —— WebSocket：Origin 校验 + 事件流/姿态流分流与鉴权
# =====================================================================
def test_hub_filters_streams():
    """★ P1-B6 回归钉：事件帧绝不进姿态客户端，姿态帧绝不进事件客户端。"""
    from app.services.hub import WsHub

    class _Loop:
        def __init__(self):
            self.calls = []

        def call_soon_threadsafe(self, fn, *args):
            self.calls.append((fn, args))

    class _WS:
        pass

    h = WsHub()
    loop = _Loop()
    pose_ws, ev_ws = _WS(), _WS()
    h.register(pose_ws, loop, "pose")
    h.register(ev_ws, loop, "events")
    try:
        h.broadcast({"type": "pose", "j1": 1})
        # 被 call_soon_threadsafe 点名的客户端 = 收到了这一帧
        assert len(loop.calls) == 1
        assert loop.calls[0][1][0] is h._clients[pose_ws]
        assert h._clients[pose_ws].latest is not None
        assert h._clients[ev_ws].latest is None

        # 清掉积压槽（模拟 _drain 已发出），再广播事件帧
        h._clients[pose_ws].latest = None
        loop.calls.clear()
        h.broadcast({"type": "event", "message": "登录失败"})
        assert len(loop.calls) == 1
        assert loop.calls[0][1][0] is h._clients[ev_ws]
        assert h._clients[ev_ws].latest is not None
        assert h._clients[pose_ws].latest is None   # 姿态客户端没被点名 → 仍是 None
    finally:
        h.unregister(pose_ws)
        h.unregister(ev_ws)


def test_ws_rejects_foreign_origin(client):
    """★ P1-B6：浏览器 WS 不受 CORS 保护，恶意站点的 Origin 必须被拒。"""
    import pytest
    with pytest.raises(Exception):
        with client.websocket_connect(
                "/ws/pose", headers={"origin": "http://evil.example"}):
            pass


def test_ws_events_requires_control_token(client):
    """★ P1-B6：审计事件帧（actor/ip/登录结果）只发给持令牌的客户端。"""
    from starlette.websockets import WebSocketDisconnect

    # 没令牌 → 连上也会被以 4401 关闭
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/events") as ws:
            ws.send_text('{"type":"auth","token":"bogus"}')
            ws.receive_text()
    assert ei.value.code == 4401


def test_ws_pose_still_open_without_token(client):
    """★ 姿态流保持公开：底栏/3D/执行页都靠它，且帧里不含审计信息。"""
    with client.websocket_connect("/ws/pose") as ws:
        msg = ws.receive_json()          # 握手即推一帧快照
        assert msg.get("type") in ("pose", "rc_status", "event") or "j1" in msg
