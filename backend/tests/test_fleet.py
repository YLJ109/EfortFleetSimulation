# -*- coding: utf-8 -*-
"""机房（fleet）层的回归测试：全部对着本地假控制器跑，不需要任何真机。

★ 这些假控制器只应答读请求（FC3 / DataSheet 推送）。测试里出现的任何写功能码
  都必须被拒绝 —— 与本项目"程序执行/测试一律空跑"的红线一致。
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from app.core.fleet_config import FleetConfig, FleetConfigError, get_fleet
from app.services.drivers import (_as_floats, _get_path, build_driver,
                                  split_ltbr)


# ======================================================================
# 配置层
# ======================================================================
def test_real_fleet_registry_is_12_readonly_and_disjoint():
    cfg = get_fleet(reload=True)
    assert len(cfg.robots) == 12
    assert sorted(r["order"] for r in cfg.robots) == list(range(1, 13))
    assert {r["brand"] for r in cfg.robots} == {"EFORT", "HUAYAN"}
    assert sum(1 for r in cfg.robots if r["brand"] == "EFORT") == 6
    assert sum(1 for r in cfg.robots if r["brand"] == "HUAYAN") == 6
    for r in cfg.robots:
        assert r["driving"] is False and r["write_allowed"] is False
        assert r["driver"] in ("efort_modbus", "huayan_datasheet")
    pos = {(round(r["pos"]["x"], 4), round(r["pos"]["y"], 4)) for r in cfg.robots}
    assert len(pos) == 12                      # 没有任何两台踩在同一格
    # 贴着放：同组相邻两台的中心距必须正好是一个围栏边长
    ys = sorted(r["pos"]["y"] for r in cfg.robots)
    pitch = cfg.layout["cell_pitch_m"]
    assert any(abs((ys[i + 1] - ys[i]) - pitch) < 1e-6 for i in range(len(ys) - 1))


def test_fleet_disabled_by_default_and_env_wins():
    import os
    cfg = get_fleet(reload=True)
    os.environ.pop("EFORT_FLEET", None)
    assert cfg.enabled is False          # 测试/CI 不该被 12 条线程打扰
    os.environ["EFORT_FLEET"] = "1"
    try:
        assert get_fleet(reload=True).enabled is True
    finally:
        os.environ.pop("EFORT_FLEET", None)


def test_dangerous_pitch_rejected():
    cfg = get_fleet(reload=True)
    raw = json.loads(json.dumps(cfg.raw))       # 深拷贝，别改到单例
    raw["layout"]["cell_pitch_m"] = 1.20        # 小于两臂半径之和 1.424
    with pytest.raises(FleetConfigError, match="cell_pitch_m"):
        FleetConfig(raw)


def test_write_gate_in_config_is_enforced():
    cfg = get_fleet(reload=True)
    raw = json.loads(json.dumps(cfg.raw))
    raw["robots"][0]["driving"] = True
    with pytest.raises(FleetConfigError, match="只读"):
        FleetConfig(raw)


def test_duplicate_positions_rejected():
    cfg = get_fleet(reload=True)
    raw = json.loads(json.dumps(cfg.raw))
    raw["robots"][1]["zone"] = raw["robots"][0]["zone"]
    raw["robots"][1]["group"] = raw["robots"][0]["group"]
    raw["robots"][1]["slot"] = raw["robots"][0]["slot"]
    with pytest.raises(FleetConfigError, match="同一个位置"):
        FleetConfig(raw)


def test_numbering_starts_from_bottom():
    """用户口径「从下面 1 开始」：编号小的必须在 y 小（靠前/靠下）的一侧。"""
    cfg = get_fleet(reload=True)
    a = [r for r in cfg.robots if r["zone"] == "A"]
    a.sort(key=lambda r: r["order"])
    ys = [r["pos"]["y"] for r in a]
    assert ys == sorted(ys)


# ======================================================================
# DataSheet 帧解析（华沿 §5.1）
# ======================================================================
def _pkt(payload: dict, byte_order="little", total_offset=0) -> bytes:
    net = json.dumps(payload).encode("utf-8")
    fmt = ">" if byte_order == "big" else "<"
    return b"LTBR" + struct.pack(fmt + "II", 12 + len(net) + total_offset, len(net)) + net


SAMPLE = {"PosAndVel": {"Actual_Position": ["1.0", "2.0", "3.0", "4.0", "5.0", "6.0",
                                            "367.9", "0.0", "359.6", "180", "17.8", "180"],
                        "Actual_PCS_Base": ["367.9", "0.0", "359.6", "180", "17.8", "180"],
                        # 故意与 Base 不同：§5.2 的示例里三路数值一样（默认工具/用户系是
                        # 单位变换），照抄示例就测不出"取错坐标系"。
                        "Actual_PCS_TCP": ["111.0", "222.0", "333.0", "0", "0", "0"],
                        "Actual_Joint_Velocity": ["0.0"] * 6,
                        "Actual_Override": "1.000"},
          "StateAndError": {"robotEnabled": 1, "robotMoving": 0, "InPos": 1,
                            "Error_Code": 0, "Error_AxisID": 0, "robotState": 34,
                            "AutoMode": "1"}}


def test_split_ltbr_whole_half_sticky():
    p = _pkt(SAMPLE)
    assert split_ltbr(p, "little")[0] == json.dumps(SAMPLE).encode()
    assert split_ltbr(p, "little")[1] == len(p)
    assert split_ltbr(p[:8], "little") == (None, 0, "")          # 半帧：等下一次 recv
    payload, used, _ = split_ltbr(p + p[:5], "little")           # 粘包：切一整帧
    assert payload is not None and used == len(p)


def test_split_ltbr_resyncs_over_garbage():
    p = _pkt(SAMPLE)
    payload, used, _ = split_ltbr(b"\x00\x01" + p, "little")
    assert used == 2 and payload is None
    payload, used, _ = split_ltbr(b"\x00\x01" + p, "little")
    buf = b"\x00\x01" + p
    buf = buf[2:]                                                # 模拟管理器丢掉前导垃圾后
    assert split_ltbr(buf, "little")[0] is not None


def test_split_ltbr_rejects_wrong_byte_order():
    """小端发的包，用大端解：必须报"字节序"而不是静默无数据或崩掉。"""
    p = _pkt(SAMPLE, "little")
    payload, used, note = split_ltbr(p, "big")
    assert payload is None
    assert "byte_order" in note or "JSON" in note


def test_split_ltbr_tolerates_total_excluding_header():
    """文档没界定"总长"含不含帧头，两种口径都要能收 —— 猜错口径的后果是永久离线。"""
    p = _pkt(SAMPLE, total_offset=-12)
    payload, used, note = split_ltbr(p, "little")
    assert payload is not None, note


def test_path_getter():
    assert _get_path(SAMPLE, "PosAndVel.Actual_Position[0:6]")[:2] == ["1.0", "2.0"]
    assert _get_path(SAMPLE, "PosAndVel.Missing") is None
    assert _get_path(SAMPLE, "PosAndVel.Actual_Position[99]") is None
    assert _as_floats(["1", "2", "3"], 6) is None                # 数量不足不猜
    assert _as_floats(["1", "2", "x", "4", "5", "6"]) is None     # 非数值不猜
    assert _as_floats(["1", "2", "3", "4", "5", "6", "7"]) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


# ======================================================================
# 假控制器（只应答读）
# ======================================================================
class FakeModbusSlave(threading.Thread):
    def __init__(self, joints):
        super().__init__(daemon=True)
        # 用生产编码器灌寄存器：字序（CDAB 低字在前）若哪天改了，
        # 这里会立刻跟着变，测试就不会出现"假控制器和驱动各按一套字序、
        # 双双自洽却与真机不符"的漂移。
        from app.services.modbus import _float_to_words
        self.reg = {}
        for i in range(6):
            low, high = _float_to_words(float(joints[i]))
            self.reg[10 + i * 2] = low
            self.reg[11 + i * 2] = high
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.stop = threading.Event()
        self.writes_seen = 0

    def run(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                break
            threading.Thread(target=self._svc, args=(c,), daemon=True).start()

    @staticmethod
    def _recv_exact(c, n):
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _svc(self, c):
        c.settimeout(2.0)
        try:
            while not self.stop.is_set():
                head = self._recv_exact(c, 7)          # MBAP: tid/proto/len/uid
                if head is None:
                    break
                tid, _, ln, uid = struct.unpack(">HHHB", head)
                data = self._recv_exact(c, max(0, ln - 1))   # 去掉 uid 后的剩余
                if data is None or not data:
                    break
                fc, body = data[0], data[1:]
                if fc == 3:
                    addr, qty = struct.unpack(">HH", body[0:4])
                    vals = [self.reg.get(addr + i, 0) for i in range(qty)]
                    rsp = bytes([fc, qty * 2]) + b"".join(struct.pack(">H", v) for v in vals)
                elif fc in (5, 6, 15, 16):
                    self.writes_seen += 1
                    rsp = bytes([fc | 0x80, 0x02])       # 拒绝写：非法数据地址
                else:
                    rsp = bytes([fc | 0x80, 0x01])
                c.sendall(struct.pack(">HHHB", tid, 0, len(rsp) + 1, uid) + rsp)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass


class FakeDataSheetServer(threading.Thread):
    def __init__(self, payload, interval=0.03, byte_order="little"):
        super().__init__(daemon=True)
        self.payload = payload
        self.interval = interval
        self.stop = threading.Event()
        net = json.dumps(payload).encode("utf-8")
        fmt = ">" if byte_order == "big" else "<"
        self.packet = b"LTBR" + struct.pack(fmt + "II", 12 + len(net), len(net)) + net
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]

    def run(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                break
            threading.Thread(target=self._svc, args=(c,), daemon=True).start()

    def _svc(self, c):
        try:
            while not self.stop.is_set():
                c.sendall(self.packet)
                time.sleep(self.interval)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass


EFORT_PARAMS = {"host": "127.0.0.1", "timeout_s": 1.0, "status_interval_s": 0.0}
HUAYAN_PARAMS = {"host": "127.0.0.1", "timeout_s": 1.0}


def test_efort_driver_reads_joints_and_tcp():
    slave = FakeModbusSlave([10.0, -20.0, 30.0, 0.0, 45.0, 90.0])
    slave.start()
    d = build_driver("A1-1", "efort_modbus", dict(EFORT_PARAMS, port=slave.port))
    d.open()
    f = d.read()
    assert f.online and f.error is None
    assert f.joints[0] == pytest.approx(10.0, abs=1e-3)
    assert f.joints[5] == pytest.approx(90.0, abs=1e-3)
    assert f.tcp is not None and len(f.tcp) == 3          # 本地正解给出的 TCP
    d.close()
    slave.stop.set()
    assert slave.writes_seen == 0


def test_efort_driver_offline_freeze_not_fabricated():
    """读不到时必须冻结+标 stale，绝不能编一条轨迹冒充真机（审计 P1-A6 的机房版）。"""
    d = build_driver("A1-9", "efort_modbus",
                     dict(EFORT_PARAMS, port=25099))      # 没人监听
    d.open()
    f = d.read()
    assert f.online is False
    assert f.joints == [0.0] * 6
    assert f.stale is False and f.error            # 一帧都没读过，不叫"陈旧"
    d.close()


def test_huayan_driver_parses_datasheet():
    srv = FakeDataSheetServer(SAMPLE)
    srv.start()
    d = build_driver("B1-1", "huayan_datasheet", dict(HUAYAN_PARAMS, port=srv.port))
    d.open()
    f = d.read(0.5)
    assert f.online and f.error is None
    assert f.joints == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert f.tcp == pytest.approx([367.9, 0.0, 359.6])       # §5.2：基座系 Actual_PCS_Base
    assert f.status["enabled"] is True and f.status["inpos"] is True
    srv.stop.set()
    d.close()


def test_huayan_wrong_byte_order_reports_the_cause():
    srv = FakeDataSheetServer(SAMPLE, byte_order="little")
    srv.start()
    d = build_driver("B1-2", "huayan_datasheet",
                     dict(HUAYAN_PARAMS, port=srv.port, header_byte_order="big"))
    d.open()
    f = d.read(0.2)
    assert f.online is False
    assert "byte_order" in (d.last_error or "")
    srv.stop.set()
    d.close()


# ======================================================================
# 仿真帧的 TCP：合成姿态也要走**真机同一条**正解
# ======================================================================
def test_sim_efort_frame_carries_the_same_fk_as_the_real_driver():
    from app.services.kinematics import tcp_of as ef_tcp

    d = build_driver("A1-1", "sim_swing", {"brand": "EFORT", "model": "efort_er8_700h"})
    f = d.read()
    assert f.online and f.simulated and f.error is None
    assert f.tcp is not None and len(f.tcp) == 3
    assert f.tcp == pytest.approx([float(v) for v in ef_tcp(f.joints)], rel=1e-6)


def test_sim_huayan_frame_carries_urdf_tcp():
    from app.services import kinematics_elfin as elfin

    d = build_driver("B1-1", "sim_swing", {"brand": "HUAYAN", "model": "huayan_elfin"})
    f = d.read()
    chain = elfin.chain_for(get_fleet().model_profile("huayan_elfin")["model_data"])
    assert chain is not None and chain["variant"] == "elfin5"
    assert f.tcp == pytest.approx([float(v) for v in elfin.tcp_of(chain, f.joints)], rel=1e-6)
    # E05 站立高度 1176.5mm，仿真摆幅远小于限位 → 末端必须在天花板以下的合理区间，
    # 这一条防的是"链挂反了，末端钻进地里/飞到 3 米高"这类整体错位。
    assert 400 < f.tcp[2] < 1400, f"仿真末端高度异常: {f.tcp}"


def test_sim_frame_without_kinematics_profile_leaves_tcp_empty():
    assert build_driver("B9-9", "sim_swing", {"brand": "HUAYAN"}).read().tcp is None
    assert build_driver("X1-1", "sim_swing", {"brand": "UNKNOWN"}).read().tcp is None


def test_sim_phases_are_spread_across_the_hall():
    """同一片 12 台的合成读数必须互相拉开 —— 判据用真实 fleet.yaml 的 id + order。

    早期版本按 robot_id 做线性哈希：A1-1 与 A1-2 的相位只差 0.1°，
    同排三台六轴数字一模一样，大屏十二个格子像复制粘贴，操作员只会认为
    "数据根本没进来"。这比显示离线更难排查，所以钉成测试。
    """
    robots = [(r["id"], r["order"], r["brand"], r["model"]) for r in get_fleet().robots]
    assert len(robots) == 12
    poses = {}
    for rid, order, brand, model in robots:
        d = build_driver(rid, "sim_swing",
                         {"brand": brand, "model": model, "order": order})
        poses[rid] = d.read().joints
    worst = None
    for i in range(len(robots)):
        for j in range(i + 1, len(robots)):
            a, b = poses[robots[i][0]], poses[robots[j][0]]
            gap = max(abs(x - y) for x, y in zip(a, b))
            if worst is None or gap < worst[2]:
                worst = (robots[i][0], robots[j][0], gap)
    assert worst[2] > 1.0, ("最差相位对 %s / %s 的六轴最大差只有 %.2f 度"
                            % (worst[0], worst[1], worst[2]))
    # 确定性：同一个 id 两次实例化必须给同一个相位（截图可比对）
    again = build_driver("A1-1", "sim_swing",
                         {"brand": "EFORT", "model": "efort_er8_700h", "order": 1})
    assert again.read().joints == pytest.approx(poses["A1-1"], abs=0.05)


def test_sim_phase_fallback_spreads_without_order():
    """没给 order 时退回 md5：相邻 id 也要拉开（线性哈希当年就是栽在这）。"""
    a = build_driver("Z1-1", "sim_swing", {"brand": "EFORT"}).read().joints
    b = build_driver("Z1-2", "sim_swing", {"brand": "EFORT"}).read().joints
    assert max(abs(x - y) for x, y in zip(a, b)) > 1.0


def test_sim_hold_frame_tcp_follows_the_executed_target():
    """被"执行"接管后：停在目标角度，TCP 必须是那个目标的正解（残影/回放对得上）。"""
    from app.services.kinematics import tcp_of as ef_tcp

    d = build_driver("A1-1", "sim_swing", {"brand": "EFORT", "model": "efort_er8_700h"})
    d.set_target([10.0, 20.0, 30.0, 0.0, 10.0, 20.0])
    f = d.read()
    assert f.joints == pytest.approx([10.0, 20.0, 30.0, 0.0, 10.0, 20.0])
    assert f.velocity == [0.0] * 6
    assert f.tcp == pytest.approx([float(v) for v in ef_tcp(f.joints)], rel=1e-6)


# ======================================================================
# 编排层：FleetManager 合批推送
# ======================================================================
def _mini_fleet_raw(efort_port: int, huayan_port: int) -> dict:
    """真实 fleet.yaml 的深拷贝，只留一台埃夫特 + 一台华沿，地址换成本机假控制器。

    只改 host/port、不改结构 —— 这样测的是"出厂配置能不能真跑起来"，
    而不是一份为测试特制的配置。
    """
    raw = json.loads(json.dumps(get_fleet().raw))
    keep = {"A1-1", "B1-1"}
    robots = [r for r in raw["robots"] if r["id"] in keep]
    assert len(robots) == 2
    for r in robots:
        r["host"] = "127.0.0.1"
    raw["robots"] = robots
    raw["fleet"]["enabled"] = True
    raw["drivers"]["efort_modbus"].update(port=efort_port, status_interval_s=0.0)
    # 关掉断流兜底：测试里没起 10003 命令口，让它安静地报离线而不是去连本机端口
    raw["drivers"]["huayan_datasheet"].update(port=huayan_port, fallback_command={})
    raw["streaming"].update(batch_interval_ms=50, db_write_hz=5)
    return raw


def test_fleet_manager_batches_both_brands(client, monkeypatch):
    """一台轮询 + 一台推送，必须被合成同一帧 fleet_pose 推给前端，并按 robot_id 入库。"""
    import os

    from sqlalchemy import text

    from app.db.database import SessionLocal
    from app.services.fleet import FleetManager
    from app.services.hub import hub

    os.environ.pop("EFORT_FLEET", None)      # 启用与否只由这份 raw 说了算
    slave = FakeModbusSlave([15.0, -25.0, 35.0, -5.0, 55.0, 85.0])
    slave.start()
    srv = FakeDataSheetServer(SAMPLE, interval=0.02)
    srv.start()

    frames: list = []
    monkeypatch.setattr(hub, "broadcast", lambda payload: frames.append(payload))

    mgr = FleetManager(FleetConfig(_mini_fleet_raw(slave.port, srv.port)))
    assert mgr.start() is True
    got = None
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and got is None:
            for f in frames:
                if f.get("type") != "fleet_pose":
                    continue
                by_id = {r["id"]: r for r in f["robots"]}
                if len(by_id) == 2 and all(r["online"] for r in by_id.values()):
                    got = by_id
                    break
            time.sleep(0.05)
        assert got is not None, "5s 内没等到两台都在线的合批帧：%s" % [
            (r["id"], r["online"], r.get("error")) for f in frames
            if f.get("type") == "fleet_pose" for r in f["robots"]]

        assert {"A1-1", "B1-1"} == set(got)
        a, b = got["A1-1"], got["B1-1"]
        # 元数据跟着帧走：前端不用再查一次 /api/fleet 才能画卡片
        assert a["zone"] == "A" and a["brand"] == "EFORT" and a["order"] == 1
        assert b["zone"] == "B" and b["brand"] == "HUAYAN" and b["order"] == 7
        assert a["host"] == "127.0.0.1" and a["port"] == slave.port
        assert a["joints"] == pytest.approx([15.0, -25.0, 35.0, -5.0, 55.0, 85.0], abs=1e-3)
        assert a["tcp"] and len(a["tcp"]) == 3        # 本地正解
        assert b["joints"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        assert b["tcp"] == pytest.approx([367.9, 0.0, 359.6])
        assert b["status"]["enabled"] is True

        s = SessionLocal()
        try:
            rows = dict(s.execute(text(
                "SELECT robot_id, COUNT(*) FROM pose_history "
                "WHERE robot_id IN ('A1-1','B1-1') GROUP BY robot_id")).all())
        finally:
            s.close()
        assert rows.get("A1-1") and rows.get("B1-1")   # 历史曲线按机分开
        assert slave.writes_seen == 0                  # ★ 全程只读
    finally:
        mgr.stop()
        srv.stop.set()
        slave.stop.set()
        assert mgr.summary()["total"] == 0


def test_all_offline_still_ships_every_station(monkeypatch):
    """连不上时前端仍要收到 12 张"离线卡"，而不是机器人在画面与摘要里凭空消失。

    这条看着像废话，却是机房界面最容易出的事故：链路一断就少一台，
    操作员会以为那台被移走了，而实际它正在旁边转。
    """
    import os

    from app.services.fleet import FleetManager
    from app.services.hub import hub

    os.environ.pop("EFORT_FLEET", None)
    frames: list = []
    monkeypatch.setattr(hub, "broadcast", lambda payload: frames.append(payload))
    mgr = FleetManager(FleetConfig(_mini_fleet_raw(25099, 25100)))   # 无人监听的端口
    assert mgr.start() is True
    try:
        deadline = time.time() + 4.0
        pose = status = None
        while time.time() < deadline and not (pose and status):
            pose = next((f for f in frames if f.get("type") == "fleet_pose"), pose)
            status = next((f for f in frames if f.get("type") == "fleet_status"), status)
            time.sleep(0.05)
        assert pose and status
        assert {r["id"] for r in pose["robots"]} == {"A1-1", "B1-1"}
        for r in pose["robots"]:
            assert r["online"] is False
            assert r["stale"] is False          # 一帧都没读过 → 不许标成"陈旧"
            assert r["error"]
        sm = status["summary"]
        assert sm["total"] == 2 and sm["offline"] == 2 and sm["alarm"] == 0
        assert sm["by_zone"] == {"A": {"total": 1, "online": 0},
                                 "B": {"total": 1, "online": 0}}
    finally:
        mgr.stop()


# ======================================================================
# REST 侧（前端 WS 掉线时的兜底）
# ======================================================================
def test_api_fleet_ships_registry_when_not_running(client):
    """采集没启动时 /api/fleet 仍要给出 12 个工位 + 布局，否则三维画面是空的。"""
    r = client.get("/api/fleet")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["running"] is False
    assert len(body["robots"]) == 12
    assert body["layout"]["cell_pitch_m"] == pytest.approx(1.64)
    assert body["summary"]["total"] == 12 and body["summary"]["online"] == 0
    xs = {round(v["pos"]["x"], 4) for v in body["robots"]}
    assert len(xs) == 2                       # A/B 两区各一条 x
    for v in body["robots"]:
        assert v["online"] is False and len(v["joints"]) == 6
        assert v["error"]

    one = client.get("/api/fleet/A1-1")
    assert one.status_code == 200 and one.json()["zone"] == "A"
    assert client.get("/api/fleet/Z9-9").status_code == 404


def test_api_fleet_ships_kinematics_per_model(client):
    """前端画 12 条手臂需要的几何档案：两种品牌、两套口径，都由后端给。

    埃夫特 = 标准 DH（mm/deg，与前端既有 robotModel 同口径）；
    华沿   = 官方 xacro 抄来的关节链（m/rad，刻意不转 DH）。
    这条用例钉住的是"别让前端自己抄一份数值表"这个约定。
    """
    kin = client.get("/api/fleet").json()["kinematics"]
    assert set(kin) == {"efort_er8_700h", "huayan_elfin"}

    ef = kin["efort_er8_700h"]
    assert ef["chain"] == "dh" and ef["brand"] == "EFORT"
    assert len(ef["dh"]["joints"]) == 6
    assert {k for k in ef["dh"]["joints"][0]} >= {"name", "d", "a", "alpha"}

    hy = kin["huayan_elfin"]
    assert hy["chain"] == "urdf" and hy["brand"] == "HUAYAN"
    joints = hy["urdf"]["joints"]
    assert len(joints) == 6
    assert joints[0]["xyz"] == [0, 0, 0.22] and joints[2]["xyz"][1] == 0.38   # E05
    assert all(len(j["rpy"]) == 3 for j in joints)
    assert hy["urdf"]["standing_height_mm"] == 1176.5
    assert hy["variant_unverified"] is True     # 铭牌没核对过，界面必须标出来
    assert hy["mesh"]["glb"] == "" and hy["mesh"]["fallback"] == "joint_chain"
    assert hy["mounting"] == "floor"
