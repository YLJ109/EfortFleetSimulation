# -*- coding: utf-8 -*-
"""机房 12 台机器人的**只读**采集驱动。

两个品牌说的是两种话（都来自厂商文档，不是反推）：
  · 埃夫特 ER8-700H —— Robox 控制器 Modbus TCP，客户端**轮询** FC3 读 40011~22；
  · 华沿 ELFIN     —— 控制器**主动推送** DataSheet（10004 端口 50ms 一帧，LTBR 帧头），
                     客户端只管连上去收包，连"问"都不用问。

★ 本模块只有读通道。这里没有任何写方法（不写寄存器、不发 Move/Enable 类命令）。
  真机写路径继续走 services/motion.py 的双闸，与本文件无关。
  —— 用户口径：「只读为主，预留控制位」。预留的位置在 RobotDriver.write_allowed
  这个恒为 False 的字段上，等阶段二再谈。
"""
from __future__ import annotations

import json
import hashlib
import socket
import struct
import threading
import time
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import get_logger

log = get_logger("drivers")

N_AXES = 6
OFFLINE_HOLD_S = 2.0        # 连续这么久没拿到有效帧 → 判离线
PHASE_SLOTS = 12            # 仿真相位均分的格数（机房台数，order 唯一 → 每台差 30°）


@dataclass
class RobotFrame:
    """一帧采集结果。字段口径与前端 fleet 卡片一致。"""
    robot_id: str
    joints: List[float] = field(default_factory=lambda: [0.0] * N_AXES)
    tcp: Optional[List[float]] = None      # mm；埃夫特=本地正解，华沿=控制器上报
    cart: Optional[List[float]] = None      # 控制器自己算的笛卡尔（mm / deg）
    velocity: Optional[List[float]] = None  # °/s
    status: Dict[str, Any] = field(default_factory=dict)
    online: bool = False
    simulated: bool = False
    stale: bool = False
    ts: float = 0.0
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.robot_id,
            "joints": [round(float(j), 4) for j in self.joints],
            "tcp": [round(float(v), 3) for v in self.tcp] if self.tcp else None,
            "cart": self.cart,
            "velocity": self.velocity,
            "status": self.status,
            "online": self.online,
            "simulated": self.simulated,
            "stale": self.stale,
            "at": self.ts,
            "error": self.error,
        }


class RobotDriver:
    """所有驱动的公共外形。FleetManager 只认这四个方法。"""

    kind = "base"
    # 事件推送型（华沿）不需要我们掐表轮询；轮询型（埃夫特）由管理器按 poll_ms 驱动。
    push_mode = False
    write_allowed = False          # 阶段一恒 False；不是"待办"，是本阶段的边界声明

    def __init__(self, robot_id: str, params: Dict[str, Any]) -> None:
        self.robot_id = robot_id
        self.params = params or {}
        self.host = str(self.params.get("host") or "")
        self.last_error: Optional[str] = None

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def read(self, wait_s: float = 0.2) -> RobotFrame:
        """取最新一帧。push_mode 驱动在 wait_s 内等包；轮询驱动立即返回。"""
        raise NotImplementedError

    def reachable(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            port = int(self.params.get("port", 502))
            ok = s.connect_ex((self.host, port)) == 0
            s.close()
            return ok
        except Exception:
            return False


# ======================================================================
# 埃夫特 ER8-700H —— Modbus TCP
# ======================================================================
class EfortModbusDriver(RobotDriver):
    """复用 services/modbus.ModbusRobot（长连接 + 全链路寄存器表），每台一个实例。

    为什么不重写一遍 Modbus 客户端：ModbusRobot 已经解决了这个控制器实测出来的
    三件麻烦事 —— 长连接复用（短连接连打会被回异常）、整帧 IO 锁、
    CDAB 字序。再写一份只会让两套实现漂移。
    """

    kind = "efort_modbus"

    def __init__(self, robot_id: str, params: Dict[str, Any]) -> None:
        super().__init__(robot_id, params)
        from app.services.modbus import ModbusRobot
        self.modbus = ModbusRobot(conn_override={
            "host": self.host,
            "port": int(params.get("port", 502)),
            "unit_id": int(params.get("unit_id", 1)),
            "timeout_s": float(params.get("timeout_s", 2.0)),
        })
        self._last: Optional[RobotFrame] = None
        self._last_ok_at = 0.0
        self._status_every = float(params.get("status_interval_s", 2.0))
        self._status_cache: Dict[str, Any] = {}
        self._next_status_at = 0.0
        from app.services.kinematics import tcp_of
        self._tcp_of = tcp_of

    def open(self) -> None:
        pass     # 长连接由 ModbusRobot 在第一次读时惰性建立

    def close(self) -> None:
        try:
            self.modbus._close()
        except Exception:
            pass

    def read(self, wait_s: float = 0.2) -> RobotFrame:
        from app.core.config import get_config
        now = time.time()
        joints, err = self.modbus.read_pose()
        if joints is None:
            self.last_error = err
            online = (now - self._last_ok_at) < OFFLINE_HOLD_S and self._last is not None
            f = RobotFrame(robot_id=self.robot_id,
                           joints=list(self._last.joints) if self._last else [0.0] * N_AXES,
                           tcp=list(self._last.tcp) if (self._last and self._last.tcp) else None,
                           status=dict(self._status_cache),
                           online=online,
                           stale=self._last is not None,
                           ts=now,
                           error=err)
            return f
        self._last_ok_at = now
        self.last_error = None
        # 轴符号校准与单机采集链路（collector）同一份配置、同一处生效 ——
        # 现场"某轴转向反了"改一次，单机与机房都得跟着变，不能两处各拧各的。
        signs = [float(s) for s in get_config().get("axis_sign", default=[1] * 6)]
        if len(signs) < N_AXES:
            signs = signs + [1.0] * (N_AXES - len(signs))
        joints = [j * s for j, s in zip(joints, signs)]
        try:
            tcp = [float(v) for v in self._tcp_of(joints)]
        except Exception as e:
            log.warning("%s 正解失败: %s", self.robot_id, e)
            tcp = None
        status = self._status(now)
        f = RobotFrame(robot_id=self.robot_id, joints=list(joints), tcp=tcp,
                       status=status, online=True, ts=now)
        self._last = f
        return f

    def _status(self, now: float) -> Dict[str, Any]:
        """低频读一帧寄存器快照（3 个 FC3 事务），中间复用缓存。

        20Hz 逐帧读快照会把 6 台控制器各打满 60 事务/秒，而状态位本来就不是
        高频量 —— 采集节奏跟着 robot.yaml 的 rc-status 口径走（原来就是 4s 一帧）。
        """
        if now >= self._next_status_at:
            self._next_status_at = now + self._status_every
            snap, err = self.modbus.rc_snapshot()
            if snap is not None:
                b = snap.get("bits") or {}
                self._status_cache = {
                    "servo_on": bool(b.get("servo")),
                    "auto": bool(b.get("auto")),
                    "remote": bool(b.get("remote")),
                    "alarm": bool(b.get("alarm")),
                    "estop": bool(b.get("estop")),
                    "running": bool(b.get("run")),
                    "program": snap.get("program"),
                    "speed_pct": snap.get("speed"),
                    "mode": snap.get("mode"),
                }
            else:
                self._status_cache = {"read_error": str(err or "")[:120]}
        return dict(self._status_cache)


# ======================================================================
# 华沿 ELFIN —— DataSheet 被动推送（《HuayanRobotV6控制通信协议接口》§3.2/§5.1/§5.2）
# ======================================================================
LTBR_MAGIC = b"LTBR"          # §5.1：0x4C 0x54 0x42 0x52
LTBR_HEADER = 12              # magic 4 + 总长 4 + 净长度 4


def _get_path(data: Any, path: str) -> Any:
    """按 "PosAndVel.Actual_Position[0:6]" 这种点路径取值，取不到返回 None。

    写成配置驱动而不是硬编码：厂商小版本会挪字段（文档里已有"新版本不再提供
    Actual_PCS_Tool"这种话），改 yaml 比改代码安全。
    """
    cur = data
    for seg in str(path).split("."):
        key = seg
        idx = None
        if "[" in seg:
            key, _, rest = seg.partition("[")
            idx = rest.rstrip("]").split(":")
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if idx is not None:
            if not isinstance(cur, (list, tuple)):
                return None
            try:
                if len(idx) == 2 and idx[0] and idx[1]:
                    cur = cur[int(idx[0]):int(idx[1])]
                else:
                    cur = cur[int(idx[0])]
            except (ValueError, IndexError):
                return None
    return cur


def _as_floats(v: Any, n: int = N_AXES) -> Optional[List[float]]:
    if not isinstance(v, (list, tuple)):
        return None
    out: List[float] = []
    for x in v[:n]:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            return None
    return out if len(out) == n else None


def split_ltbr(buf: bytes, byte_order: str = "little") -> Tuple[Optional[bytes], int, str]:
    """从缓冲区里切出一帧 LTBR。返回 (净数据 或 None, 消耗字节数, 说明)。

    说明字段不是装饰 —— 现场最容易遇到的两件事必须能区分开：
      · "还没收全"（正常，等下一次 recv）；
      · "长度/内容不对劲"（九成是 header_byte_order 猜错，见下）。

    三种脏情况都要处理：
      · 粘包：一次 recv 拿到 1.5 帧 → 按总长切片，剩下的留在缓冲区；
      · 帧间垃圾：从 magic 重新对齐；
      · 字节序猜错：长度解出来"看着合法"但净数据不是 JSON 对象。
        这种包最阴 —— 不报错、只是解不出关节角，界面会一路显示离线。
        所以除了校验长度，还要校验净数据首字节是 '{'。
    """
    if len(buf) < LTBR_HEADER:
        return None, 0, ""
    start = buf.find(LTBR_MAGIC)
    if start < 0:
        return None, len(buf), "没有 LTBR 帧头，丢弃 %d 字节重同步" % len(buf)
    if start > 0:
        return None, start, ""
    fmt = ">" if byte_order == "big" else "<"
    total, plen = struct.unpack(fmt + "II", buf[4:12])
    if not (0 < plen <= 4 * 1024 * 1024):
        return None, len(buf), (
            "净长度字段不合理（payload=%d）—— 若一直如此，"
            "把 fleet.yaml 的 header_byte_order 改成另一种字节序" % plen)
    # 文档写「总长 = 本表中所有字段的长度之和」，但没界定它含不含帧头。
    # 这里两种口径都吃：按 max(total, 12+plen) 收，收够再切 ——
    # 猜错口径的后果是"永远判为半帧、界面一路离线"，不值得赌。
    frame_end = max(total, LTBR_HEADER + plen)
    if frame_end > 4 * 1024 * 1024:
        return None, len(buf), "帧长 %d 超限，丢弃重同步" % frame_end
    if len(buf) < LTBR_HEADER + plen:
        return None, 0, ""                          # 还没收全
    payload = buf[LTBR_HEADER:LTBR_HEADER + plen]
    used = min(frame_end, len(buf))
    if not payload.startswith(b"{"):
        return None, used, (
            "净数据不是 JSON 对象（首字节 %r）—— 多半是 header_byte_order 猜错"
            % (payload[:1],))
    return payload, used, ""


class HuayanDataSheetDriver(RobotDriver):
    """连 10004 收 JSON DataSheet；断流时可退到 10003 命令端口问一次 ReadActCoord。

    关键设计：**不轮询关节角**。控制器 50ms 自己推一帧，12 台并发时
    每台只有"收包"这一笔开销，比轮询省一个量级，也不会把控制器问烦。
    """

    kind = "huayan_datasheet"
    push_mode = True

    def __init__(self, robot_id: str, params: Dict[str, Any]) -> None:
        super().__init__(robot_id, params)
        self.port = int(params.get("port", 10004))
        self.timeout = float(params.get("timeout_s", 3.0))
        self.byte_order = str(params.get("header_byte_order", "little"))
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._lock = threading.Lock()
        self._latest: Optional[RobotFrame] = None
        self._last_rx_at = 0.0
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._fallback_cfg = params.get("fallback_command") or {}
        self._fallback_next_at = 0.0
        # ★ 写通道（10003）：出厂默认关 —— command_write.enabled 不写死成 True 是红线。
        #   关着的时候 joint_move_enabled() 返回 False，services/fleet.py 走 501，
        #   一个字节都不会往控制器发。
        from app.services.huayan_cmd import build_from_config
        self._cmd = build_from_config(self.host, params.get("command_write"))
        self._cmd_cfg = params.get("command_write") or {}

    # ---- 写通道（默认关闭）----
    def joint_move_enabled(self) -> bool:
        return bool(self._cmd and self._cmd.enabled)

    def joint_move(self, joints_deg: List[float], speed_pct: float) -> Dict[str, Any]:
        """把大屏"执行"的那一帧关节角交给控制器。

        顺序与前置检查都是**读着文档定的**，不是想当然：
          1) 现在这一帧必须拿得到（没数据就说明 10004 断了，状态未知不发运动）；
          2) 报警中（Error_Code≠0）不发 —— 协议 §4.2.3 的 GrpReset 是复位手段，
             但"替操作员按下复位"是使能级动作，本函数不干，只把原因报回去；
          3) 未使能不发 —— 同理不自动 GrpEnable；
          4) §4.5.1 明确"运动状态不能修改速度比"，所以运动中标的也不发；
          5) SetOverride 先于 WayPoint（override 是小数比例 0.01~1，红线锁最慢档 = 0.05）。
        """
        if not self.joint_move_enabled():
            return {"ok": False, "reason": "写通道未启用（command_write.enabled=false）"}
        with self._lock:
            f = self._latest
        if f is None:
            return {"ok": False, "reason": "10004 没有实时帧，状态未知，不下发"}
        st = f.status or {}
        if float(st.get("error_code") or 0) != 0:
            return {"ok": False, "reason": "控制器在报警（Error_Code=%s），"
                                           "需先复位（GrpReset）再下发"
                                           % st.get("error_code")}
        if not bool(st.get("enabled")):
            return {"ok": False, "reason": "机器人未使能，本项目不会代为 GrpEnable（§4.2.1）"}
        if bool(st.get("moving")):
            return {"ok": False, "reason": "正在运动中：§4.5.1 运动状态不能改速度比"}
        ratio = max(0.01, min(1.0, float(speed_pct) / 100.0))
        try:
            ov = self._cmd.set_override(ratio)
            if not ov["ok"]:
                return {"ok": False, "reason": "SetOverride 被拒：%s" % (ov["explanation"] or "")}
            wp = self._cmd.joint_move(
                joints_deg,
                velocity_dps=float(self._cmd_cfg.get("velocity_dps", 50.0)),
                acc_dps2=float(self._cmd_cfg.get("acc_dps2", 360.0)),
                cmd_id=str(self._cmd_cfg.get("cmd_id_prefix", "F")) +
                       str(int(time.time()) % 100000))
        except Exception as e:            # ProtocolError / TransportError 都只报原因，不重试
            return {"ok": False, "reason": str(e)[:200]}
        if not wp["ok"]:
            return {"ok": False, "reason": "WayPoint 被拒：%s" % (wp["explanation"] or "")}
        return {"ok": True, "speed_pct": int(round(ratio * 100)),
                "override": ratio, "sent": wp.get("sent", ""),
                "message": "已按关节目标下发（nMoveType=0 / nIsUseJoint=1）"}

    # ---- 连接生命周期 ----
    def open(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            self._sock = s
            self._buf = b""
        self._stop.clear()
        self._reader = threading.Thread(target=self._recv_loop,
                                        name="huayan-%s" % self.robot_id, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    # ---- 收包 ----
    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    if self._sock is None:
                        break
                    chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except Exception as e:
                self.last_error = "收包中断: %s" % e
                with self._lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except Exception:
                            pass
                        self._sock = None
                return
            if not chunk:
                self.last_error = "控制器关闭了 DataSheet 连接"
                return
            self._consume(chunk)

    def _consume(self, chunk: bytes) -> None:
        self._buf += chunk
        while self._buf:
            payload, used, note = split_ltbr(self._buf, self.byte_order)
            if used:
                self._buf = self._buf[used:]
            if note:
                self.last_error = note
            if payload is not None:
                self._ingest(payload)
                continue
            if not used:
                break                 # 半帧，等下一次 recv

    def _ingest(self, payload: bytes) -> None:
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except Exception as e:
            self.last_error = "DataSheet JSON 解析失败: %s" % e
            return
        p = self.params
        joints = _as_floats(_get_path(data, p.get("joints_path",
                                                  "PosAndVel.Actual_Position[0:6]")))
        if joints is None:
            self.last_error = "DataSheet 里没有 %s" % p.get("joints_path")
            return
        cart = _as_floats(_get_path(data, p.get("cartesian_path",
                                                "PosAndVel.Actual_PCS_Base")), 6)
        vel = _as_floats(_get_path(data, p.get("joint_velocity_path",
                                                "PosAndVel.Actual_Joint_Velocity")))
        status = {
            "enabled": bool(_num(_get_path(data, p.get("enabled_path",
                                                       "StateAndError.robotEnabled")))),
            "moving": bool(_num(_get_path(data, "StateAndError.robotMoving"))),
            "inpos": bool(_num(_get_path(data, "StateAndError.InPos"))),
            "error_code": _num(_get_path(data, "StateAndError.Error_Code")),
            "error_axis": _num(_get_path(data, "StateAndError.Error_AxisID")),
            "robot_state": _num(_get_path(data, "StateAndError.robotState")),
            "auto_mode": str(_get_path(data, "StateAndError.AutoMode") or ""),
            "override": _num(_get_path(data, p.get("override_path",
                                                   "PosAndVel.Actual_Override"))),
        }
        now = time.time()
        f = RobotFrame(robot_id=self.robot_id, joints=joints,
                       tcp=list(cart[:3]) if cart else None,
                       cart=cart,
                       velocity=vel, status=status, online=True, ts=now)
        with self._lock:
            self._latest = f
        self._last_rx_at = now
        self.last_error = None

    # ---- 对外 ----
    def read(self, wait_s: float = 0.2) -> RobotFrame:
        deadline = time.time() + max(0.0, wait_s)
        while True:
            with self._lock:
                f = self._latest
            if f is not None and (time.time() - self._last_rx_at) < OFFLINE_HOLD_S:
                return f
            if time.time() >= deadline:
                break
            time.sleep(0.01)
        # 断流：先试兜底命令口，再报离线
        fb = self._fallback_once()
        if fb is not None:
            return fb
        with self._lock:
            f = self._latest
        return RobotFrame(
            robot_id=self.robot_id,
            joints=list(f.joints) if f else [0.0] * N_AXES,
            tcp=list(f.tcp) if (f and f.tcp) else None,
            cart=f.cart if f else None,
            status=dict(f.status) if f else {},
            online=False,
            stale=f is not None,
            ts=time.time(),
            error=self.last_error or "10004 端口没有 DataSheet 推送")

    def _fallback_once(self) -> Optional[RobotFrame]:
        """§4.7.14 ReadActCoord —— 只读命令，用来在 DataSheet 没开时确认"这台是活的"。

        频率限制：每次成功/失败都至少隔 5s 才再问，避免命令口被连打。
        """
        fb = self._fallback_cfg
        now = time.time()
        if not fb or now < self._fallback_next_at:
            return None
        self._fallback_next_at = now + 5.0
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.host, int(fb.get("port", 10003))))
            s.sendall(str(fb.get("request", "ReadActCoord,0,,,;")).encode("ascii"))
            reply = s.recv(4096)
            s.close()
        except Exception as e:
            self.last_error = "兜底命令失败: %s" % e
            return None
        text = reply.decode("utf-8", errors="replace").strip()
        prefix = str(fb.get("reply_prefix", "ReadActCoord,OK,"))
        if not text.startswith(prefix):
            self.last_error = "兜底命令返回异常: %s" % text[:120]
            return None
        parts = [x for x in text.split(",")]
        lo, hi = int(fb.get("joints_from", 2)), int(fb.get("joints_to", 8))
        joints = _as_floats(parts[lo:hi])
        if joints is None:
            self.last_error = "兜底返回里关节角数不对: %s" % text[:120]
            return None
        self._buf = b""
        f = RobotFrame(robot_id=self.robot_id, joints=joints,
                       status={"via": "ReadActCoord"}, online=True, ts=time.time())
        with self._lock:
            self._latest = f
        self._last_rx_at = f.ts
        return f


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class SimSwingDriver(RobotDriver):
    """机房仿真帧：没有真机时，让每台按自己的相位缓慢摆动。

    为什么要有它：这套界面（20Hz 合批、卡片读数、围栏逐台评估、残影预判）必须在
    **"有数据"的状态下**被验证过。12 台全离线的大屏只能证明布局没塌，
    证明不了链路、证明不了颜色会跟着状态变。

    ★ 三条硬边界：
      1) 不碰任何网络 —— open/close 是空的，不会去连谁；
      2) 每帧 simulated=True，前端卡片挂「仿真」标，没人能把它当真机读数；
      3) 摆动幅度远小于关节软限位，且永远不产生任何写操作（write_allowed 继承基类 False）。
    """

    kind = "sim_swing"
    # 能力位：这台的数据源是本机合成的，下发它不会动到任何物理设备。
    # 真机驱动**没有**这个属性 → services/fleet.py 的 move() 会直接拒绝下发。
    simulated_ok = True

    def __init__(self, robot_id: str, params: Dict[str, Any]) -> None:
        super().__init__(robot_id, params)
        self.brand = str(self.params.get("brand") or "").upper()
        self.period = max(2.0, float(self.params.get("period_s", 12.0)))
        amp = self.params.get("amplitude") or [25, 20, 18, 12, 10, 8]
        self.amp = [float(amp[i]) if i < len(amp) else 10.0 for i in range(N_AXES)]
        # 相位：让同一片 12 台**看起来不像复制粘贴**。
        # ★ 原来只用 robot_id 的 h = h*131 + ord(c) 取模 —— 相邻编号（A1-1 / A1-2）
        #   哈希只差 1，相位差 0.1°，于是同排三台读数一模一样。大屏上十二个格子
        #   填同一组数字，操作员的第一反应是"数据根本没进来"，比离线还难排查。
        #   现在按登记的 order 均分一整圈（12 台 → 每台差 30°）；没有 order 时
        #   退回 md5 前 8 位（雪崩效应，相邻 id 也会拉开），而不是原那种线性哈希。
        #   仍然**不用 random**：每次重启画面都变的话，两张截图就没法比对，
        #   现场排查会直接废掉这条路。
        self.phase = self._phase_of(order=self.params.get("order"))
        self._prev: Optional[List[float]] = None
        self._prev_at = 0.0
        self._t0 = time.time()
        # 被"执行"接管后就不再摆动：目标姿态由上位机给定（见 set_target）。
        self._hold: Optional[List[float]] = None
        self._tcp = self._build_tcp()

    # ---- 相位：让 12 台不像复制粘贴 ----
    def _phase_of(self, order: Any) -> float:
        """按登记的顺序号均分一整圈（12 台 → 每台差 30 度）。

        ★ 原来只用 robot_id 的线性哈希（h = h*131 + ord(c) 取模）：相邻编号
          （A1-1 / A1-2）哈希只差 1，相位差 0.1 度，同排三台的六轴读数一模一样。
          大屏十二个格子填同一组数字，操作员的第一反应是"数据根本没进来"，
          这比直接显示离线更难排查。
        ★ 仍然不用 random：每次重启画面都变的话，两张截图就没法比对，
          现场排查会直接废掉这条路。没有 order 时退回 md5（雪崩效应，
          相邻 id 也拉得开），而不是原来那种逐字符线性累加。
        """
        try:
            n = int(order)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            # 一整圈均分成 12 格（机房就 12 台，order 由 fleet.yaml 保证不重复），
            # 相邻编号相位差 30° —— 除数是"格数"，不是 360。写成 /360 的话
            # 每台只差 1°，等于没拉开（这行本身栽过一次，tests/test_fleet.py 钉着）。
            return ((n - 1) % PHASE_SLOTS) / float(PHASE_SLOTS) * math.pi * 2
        digest = hashlib.md5(str(self.robot_id).encode("utf-8")).hexdigest()
        return (int(digest[:8], 16) % 3600) / 3600.0 * math.pi * 2

    # ---- TCP 正解 ----
    def _build_tcp(self):
        """按牌子挑一条正解通道；挑不到就返回 None（TCP 那格留空，不编数）。

        仿真帧以前不带 TCP，于是卡片那一格恒为"—"，历史曲线也没有合成数据。
        补上之后必须保证它和**真机同一条**正解：埃夫特复用 services/kinematics
        （真机驱动拿寄存器角度算 TCP 用的就是它），华沿复用官方 URDF 关节链。
        """
        if self.brand == "HUAYAN":
            try:
                from app.core.fleet_config import get_fleet
                from app.services import kinematics_elfin as elfin
                md = get_fleet().model_profile(
                    str(self.params.get("model") or "")).get("model_data")
                chain = elfin.chain_for(md)
                if chain is None:
                    log.warning("%s 华沿关节链档案不可用，仿真帧不带 TCP", self.robot_id)
                    return None
                return lambda q: elfin.tcp_of(chain, q)
            except Exception as e:
                log.warning("%s 仿真 TCP 正解初始化失败: %s", self.robot_id, e)
                return None
        if self.brand == "EFORT":
            try:
                from app.services.kinematics import tcp_of
                return tcp_of
            except Exception as e:
                log.warning("%s 仿真 TCP 正解初始化失败: %s", self.robot_id, e)
                return None
        # 第三个牌子：没有它的运动学档案就**不猜** —— 拿 ER8 的 DH 去算别的机型，
        # 得到的是一组看着合理、实际错位的毫米数，比空格危险得多。
        if self.brand:
            log.warning("%s 品牌 %s 没有正解档案，仿真帧不带 TCP", self.robot_id, self.brand)
        return None

    def _tcp_of(self, q: List[float]) -> Optional[List[float]]:
        if self._tcp is None:
            return None
        try:
            return [float(v) for v in self._tcp(q)]
        except Exception as e:
            log.debug("%s 仿真 TCP 正解失败: %s", self.robot_id, e)
            return None

    def open(self) -> None:
        self.last_error = None

    def close(self) -> None:
        pass

    def reachable(self) -> bool:
        return True          # 仿真没有"连不上"这回事

    def _read_hold(self) -> RobotFrame:
        """保持模式：停在被下发的目标姿态上，速度归零、inpos=True。"""
        now = time.time()
        q = list(self._hold or [0.0] * N_AXES)
        vel = [0.0] * N_AXES
        self._prev = list(q)
        self._prev_at = now
        if self.brand == "HUAYAN":
            status = {"enabled": True, "moving": False, "inpos": True,
                      "error_code": 0, "error_axis": 0, "auto_mode": True,
                      "override": 5, "robot_state": 33}
        else:
            status = {"servo_on": True, "auto": True, "remote": True, "alarm": False,
                      "estop": False, "running": False, "speed_pct": 5, "mode": "SIM"}
        return RobotFrame(robot_id=self.robot_id, joints=q, tcp=self._tcp_of(q),
                          cart=None, velocity=vel, status=status, online=True,
                          simulated=True, stale=False, ts=now, error=None)

    def set_target(self, joints: List[float]) -> None:
        """接住一帧目标姿态并停在那里 —— 仿真的"执行"就是走到这个位置。"""
        self._hold = [float(joints[i]) for i in range(N_AXES)]

    def read(self, wait_s: float = 0.2) -> RobotFrame:
        if self._hold is not None:
            return self._read_hold()
        t = time.time() - self._t0
        w = 2.0 * math.pi / self.period
        q = [self.amp[i] * math.sin(w * t + self.phase + i * 0.7) for i in range(N_AXES)]
        vel: Optional[List[float]] = None
        moving = False
        now = time.time()
        if self._prev is not None and now > self._prev_at:
            dt = now - self._prev_at
            vel = [(q[i] - self._prev[i]) / dt for i in range(N_AXES)]
            moving = max(abs(v) for v in vel) > 0.6      # °/s：比读数抖动大才算在动
        self._prev = list(q)
        self._prev_at = now
        if self.brand == "HUAYAN":
            status = {"enabled": True, "moving": moving, "inpos": not moving,
                      "error_code": 0, "error_axis": 0, "auto_mode": True,
                      "override": 5, "robot_state": 33}
        else:
            status = {"servo_on": True, "auto": True, "remote": True, "alarm": False,
                      "estop": False, "running": moving, "speed_pct": 5, "mode": "SIM"}
        return RobotFrame(robot_id=self.robot_id, joints=q, tcp=self._tcp_of(q),
                          cart=None, velocity=vel, status=status, online=True,
                          simulated=True, stale=False, ts=now, error=None)


DRIVERS = {
    "efort_modbus": EfortModbusDriver,
    "huayan_datasheet": HuayanDataSheetDriver,
    "sim_swing": SimSwingDriver,
}


def build_driver(robot_id: str, kind: str, merged_params: Dict[str, Any]) -> RobotDriver:
    cls = DRIVERS.get(kind)
    if cls is None:
        raise ValueError("未知驱动类型: %s（可用: %s）" % (kind, ",".join(sorted(DRIVERS))))
    return cls(robot_id, merged_params)
