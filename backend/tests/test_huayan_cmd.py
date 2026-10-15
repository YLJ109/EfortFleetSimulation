# -*- coding: utf-8 -*-
"""华沿 10003 命令通道（app/services/huayan_cmd.py）回归。

★ 判据全部对着《HuayanRobotV6控制通信协议接口_V1.0.19.1》原文：
  §2.1 消息格式 / §2.2 应答 / §4.2.1~4.2.4 / §4.5.1 SetOverride / §4.10.5 WayPoint。
  这些测试钉的是"发出去的字节就是文档那一条"，不是实现细节 ——
  语法漂移在这里不会报错，只会让真机做一个没人想要的动作。

★ 同时钉住**默认关**：enabled=false 时一条命令都不许发出去（连 socket 都不建）。
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from app.services import huayan_cmd as hc
from app.services.huayan_cmd import (COMMANDS, HuayanCommandChannel, ProtocolError,
                                     TransportError, decode, encode)


class FakeController(threading.Thread):
    """假 10003：收一条完整消息，按脚本回应答，并把收到的原文记下来。

    reply=None 时**照收到的命令名回 OK** —— 控制器的应答是带命令名的（§2.2），
    写死一条应答会让"两条命令共用一个假服务器"的用例被"命令名不一致"的守卫拒掉。
    """

    def __init__(self, reply: str = None, delay: float = 0.0):
        super().__init__(daemon=True)
        self.reply = reply
        self.delay = delay
        self.received: list = []
        self.connections = 0
        self.stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]

    def _answer_for(self, text: str) -> str:
        if self.reply is not None:
            return self.reply
        name = (text.split(",")[0] or "").strip()
        return ("%s,OK,;" % name) if name else ""

    def run(self):
        self._srv.settimeout(0.2)
        while not self.stop.is_set():
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            c.settimeout(0.5)
            buf = b""
            try:
                while b",;" not in buf and len(buf) < 8192:
                    chunk = c.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            text = buf.decode("ascii", errors="replace")
            self.received.append(text)
            if self.delay:
                time.sleep(self.delay)
            answer = self._answer_for(text).encode("ascii")
            try:
                if answer:
                    c.sendall(answer)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        self._srv.close()

    def start_ok(self):
        self.start()
        return self


def _chan(fake, **over):
    cfg = dict(port=fake.port, timeout_s=1.0, enabled=True)
    cfg.update(over)
    return HuayanCommandChannel("127.0.0.1", cfg["port"], cfg["timeout_s"],
                                cfg.get("rbt_id", 0), cfg["enabled"])


# ======================================================================
# 1) 编码：字节级对着文档
# ======================================================================
def test_encode_follows_section_2_1():
    assert encode("GrpEnable", [0]) == b"GrpEnable,0,;"                 # §4.2.1 示例
    assert encode("GrpStop", [0]) == b"GrpStop,0,;"                     # §4.2.4 示例
    assert encode("SetOverride", [0, 0.5]) == b"SetOverride,0,0.5,;"    # §4.5.1 示例
    assert encode("ReadActCoord", [0, "UCS_1", "TCP_1"]) == b"ReadActCoord,0,UCS_1,TCP_1,;"


def test_encode_rejects_unknown_or_miscounted():
    assert "EnableRobot" not in COMMANDS and "StartRun" not in COMMANDS   # UR 那套名字
    with pytest.raises(ProtocolError):
        encode("EnableRobot", [0])           # 华沿文档里没有这个命令
    with pytest.raises(ProtocolError):
        encode("GrpEnable", [])              # 参数少一个也不发
    with pytest.raises(ProtocolError):
        encode("WayPoint", [0] * 23)         # §4.10.5 是 24 个参数


def test_encode_blocks_injection_into_the_ascii_stream():
    # 命令串没有引号，一个逗号就能改写整条消息的结构
    with pytest.raises(ProtocolError):
        encode("ReadActCoord", [0, "TCP,GrpStop,0", "X"])
    with pytest.raises(ProtocolError):
        encode("ReadActCoord", [0, "TCP;ok", "X"])
    with pytest.raises(ProtocolError):
        encode("ReadActCoord", [0, "工具", "X"])          # §2.1：ASCII only
    with pytest.raises(ProtocolError):
        encode("WayPoint", [0] * 13 + ["TCP", "Base", 50, 360, 0, 0, 1, 0, 0, 0, "a,b"])


def test_float_formatting_matches_doc_examples():
    assert encode("SetOverride", [0, 0.05]) == b"SetOverride,0,0.05,;"
    # 50.0 必须写成 "50"（文档示例里没有尾随 .0）
    assert b",50,360," in encode("WayPoint", [0] * 15 + [50.0, 360.0] + [0] * 7)


# ======================================================================
# 2) WayPoint 参数位次（§4.10.5）
# ======================================================================
def test_joint_message_layout():
    ch = HuayanCommandChannel("127.0.0.1", 1, 1.0, 0, True)
    msg = hc.encode("WayPoint", _wp_params(ch, [1, 2, 3, 4, 5, 6]))
    f = msg.decode().rstrip(";").rstrip(",").split(",")
    assert f[0] == "WayPoint" and len(f) == 25                 # 名称 + 24 参数
    assert f[hc.WP["nRbtID"]] == "0"
    for i in range(6):
        assert f[hc.WP["dJ%d" % (i + 1)]] == str(i + 1)
    assert f[hc.WP["sTcpName"]] == "TCP" and f[hc.WP["sUcsName"]] == "Base"
    assert f[hc.WP["dVelocity"]] == "50" and f[hc.WP["dAcc"]] == "360"
    assert f[hc.WP["nMoveType"]] == "0" and f[hc.WP["nIsUseJoint"]] == "1"
    assert f[hc.WP["nIsSeek"]] == "0" and f[hc.WP["strCmdID"]] == "F1"
    # 关节模式下空间坐标"无效"，按 §2.1 的"没有用到的参数赋值为 0"
    assert [f[hc.WP[k]] for k in ("dX", "dY", "dZ", "dRx", "dRy", "dRz")] == ["0"] * 6


def _wp_params(ch, joints):
    p = [0.0] * 24
    p[0] = ch.robot_id
    for i in range(6):
        p[hc.WP["dJ%d" % (i + 1)] - 1] = float(joints[i])
    p[hc.WP["sTcpName"] - 1] = "TCP"
    p[hc.WP["sUcsName"] - 1] = "Base"
    p[hc.WP["dVelocity"] - 1] = 50.0
    p[hc.WP["dAcc"] - 1] = 360.0
    p[hc.WP["nMoveType"] - 1] = 0
    p[hc.WP["nIsUseJoint"] - 1] = 1
    p[hc.WP["strCmdID"] - 1] = "F1"
    return p


def test_override_range_is_the_doc_range():
    """§4.5.1：dOverride 有效范围 0.01~1（比例，不是百分数）。"""
    with pytest.raises(ProtocolError):
        HuayanCommandChannel("127.0.0.1", 1, 1.0, 0, True).set_override(5)
    with pytest.raises(ProtocolError):
        HuayanCommandChannel("127.0.0.1", 1, 1.0, 0, True).set_override(0.001)


# ======================================================================
# 3) 应答解析
# ======================================================================
def test_decode_ok_and_fail():
    r = decode("WayPoint,OK,;")
    assert r["ok"] is True and r["name"] == "WayPoint"
    f = decode("GrpEnable,Fail,20018,commandforbiddeninthisstate,;")
    assert f["ok"] is False and f["code"] == 20018
    assert "commandforbidden" in f["explanation"]
    # §2.2.2 的模板少了结尾逗号，示例里有 —— 两种都要吃
    assert decode("GrpEnable,Fail,20018,commandforbiddeninthisstate;")["code"] == 20018


def test_decode_does_not_call_unknown_a_success():
    r = decode("SomeNoise")
    assert r["ok"] is False and "不认识" in r["explanation"]
    assert decode("")["ok"] is False


# ======================================================================
# 4) 传输：一条命令一次连接、不重试、错名不认
# ======================================================================
def test_send_roundtrip_one_connection_per_command():
    fake = FakeController().start_ok()          # 应答带命令名（照 §2.2）
    try:
        ch = _chan(fake)
        r = ch.send("GrpEnable", 0)
        assert r["ok"] is True
        assert r["sent"] == "GrpEnable,0,;"
        assert fake.received == ["GrpEnable,0,;"]
        assert fake.connections == 1
        ch.send("GrpStop", 0)
        assert fake.connections == 2          # 每条各一次连接，不假设会话行为
        assert fake.received[1] == "GrpStop,0,;"
    finally:
        fake.stop.set()


def test_fail_reply_is_passed_through_verbatim():
    fake = FakeController("GrpEnable,Fail,20018,commandforbiddeninthisstate,;").start_ok()
    try:
        r = _chan(fake).enable()
        assert r["ok"] is False and r["code"] == 20018
        assert "commandforbiddeninthisstate" in r["explanation"]     # 不查码表、不猜含义
    finally:
        fake.stop.set()


def test_mismatched_reply_name_is_not_trusted():
    fake = FakeController("GrpStop,OK,;").start_ok()
    try:
        r = _chan(fake).send("GrpEnable", 0)
        assert r["ok"] is False and "不一致" in r["explanation"]
    finally:
        fake.stop.set()


def test_timeout_says_unknown_and_does_not_retry():
    fake = FakeController("", delay=0.0).start_ok()      # 连上但不回应答
    try:
        ch = _chan(fake)
        ch.timeout_s = 0.4
        with pytest.raises(TransportError) as e:
            ch.send("GrpEnable", 0)
        assert "超时" in str(e.value) or "没拿到完整应答" in str(e.value)
        assert fake.connections == 1                     # ★ 只发了一次
    finally:
        fake.stop.set()


def test_connection_refused_is_reported_not_swallowed():
    ch = HuayanCommandChannel("127.0.0.1", 1, 0.5, 0, True)   # 端口 1 没人监听
    with pytest.raises(TransportError) as e:
        ch.send("GrpEnable", 0)
    assert "连接" in str(e.value)


# ======================================================================
# 5) ★ 默认关：开着界面也不会碰到真机
# ======================================================================
def test_disabled_channel_opens_nothing():
    fake = FakeController("GrpEnable,OK,;").start_ok()
    try:
        ch = HuayanCommandChannel("127.0.0.1", fake.port, 1.0, 0, False)
        with pytest.raises(TransportError) as e:
            ch.enable()
        assert "未启用" in str(e.value)
        assert fake.connections == 0                      # 连 TCP 都没建
    finally:
        fake.stop.set()


def test_build_from_config_defaults_to_disabled():
    ch = hc.build_from_config("192.0.2.7", None)
    assert ch.enabled is False
    assert hc.build_from_config("h", {"enabled": True}).enabled is True
    assert hc.build_from_config("h", {"enabled": True, "port": 19999}).port == 19999


def test_driver_joint_move_disabled_by_default():
    from app.services.drivers import build_driver

    d = build_driver("B1-1", "huayan_datasheet", {"host": "192.0.2.9"})
    assert d.joint_move_enabled() is False
    r = d.joint_move([0] * 6, 5)
    assert r["ok"] is False and "未启用" in r["reason"]


def test_driver_refuses_when_state_is_not_safe():
    """使能/报警/运动/无帧 —— 四种状态下发都被拒，且绝不自动 GrpEnable/GrpReset。"""
    from app.services.drivers import RobotFrame, build_driver

    d = build_driver("B1-1", "huayan_datasheet",
                     {"host": "127.0.0.1", "command_write": {"enabled": True}})

    r = d.joint_move([0] * 6, 5)
    assert r["ok"] is False and "实时帧" in r["reason"]

    d._latest = RobotFrame(robot_id="B1-1", joints=[0] * 6, online=True,
                           status={"enabled": False, "moving": False, "error_code": 0})
    assert "不会代为" in d.joint_move([0] * 6, 5)["reason"]

    d._latest = RobotFrame(robot_id="B1-1", joints=[0] * 6, online=True,
                           status={"enabled": True, "moving": False, "error_code": 20018})
    assert "报警" in d.joint_move([0] * 6, 5)["reason"]

    d._latest = RobotFrame(robot_id="B1-1", joints=[0] * 6, online=True,
                           status={"enabled": True, "moving": True, "error_code": 0})
    assert "运动中" in d.joint_move([0] * 6, 5)["reason"]


def test_driver_sends_override_then_waypoint():
    from app.services.drivers import RobotFrame, build_driver

    # 应答必须带**当条命令名**（§2.2），所以这里用 echo 模式而不是写死一条
    fake = FakeController().start_ok()
    d = build_driver("B1-1", "huayan_datasheet",
                     {"host": "127.0.0.1",
                      "command_write": {"enabled": True, "port": fake.port,
                                        "timeout_s": 1.0}})
    d._latest = RobotFrame(robot_id="B1-1", joints=[0] * 6, online=True,
                           status={"enabled": True, "moving": False, "error_code": 0})
    try:
        # 两条命令共用同一个应答脚本（SetOverride 也回 OK），顺序才是重点
        r = d.joint_move([10, 20, 30, 0, 10, 20], 5)
        assert r["ok"] is True, r.get("reason")
        assert r["override"] == 0.05                      # 红线：最慢档
        assert fake.received[0] == "SetOverride,0,0.05,;"       # §4.5.1 先给速度比
        assert fake.received[1].startswith("WayPoint,0,0,0,0,0,0,0,")
        assert len(fake.received) == 2                          # 顺序：override → waypoint
    finally:
        fake.stop.set()


def test_fleet_move_falls_back_to_501_for_real_driver():
    """没开 command_write 的真机分支必须 501 —— 这条是"界面按了也不会动"的钉子。"""
    from app.services.drivers import build_driver
    from app.services.fleet import FleetManager, Station

    entry = {"id": "B1-1", "order": 1, "name": "B区-下组-1号", "zone": "B",
             "group": "lower", "slot": 1, "brand": "HUAYAN", "model": "huayan_elfin",
             "host": "192.0.2.9", "port": 10004, "driver": "huayan_datasheet",
             "pos": {"x": 1.0, "y": 1.0, "yaw": 0}}

    class _Cfg:
        enabled = True
        simulate = False
        robots = [entry]
        layout = {}
        streaming = {}
        def driver_params(self, name): return {}
        def model_profile(self, key): return {}

    mgr = FleetManager(_Cfg())
    drv = build_driver("B1-1", "huayan_datasheet", {"host": "192.0.2.9"})
    mgr.stations = [Station(entry, drv)]
    mgr._running = True
    out = mgr.move("B1-1", [0] * 6, 5)
    assert out["ok"] is False and out["code"] == 501
    assert "command_write" in out["reason"] or "P4c" in out["reason"]
