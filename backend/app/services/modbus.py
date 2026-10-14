# -*- coding: utf-8 -*-
"""
Robox 控制器 Modbus TCP 客户端。
协议与已验证的 robot_pose.py 完全一致:
  FC3, 起始地址 10, 读 12 个保持寄存器;
  每关节 2 寄存器组成 float32(大端, 字序交换: 高字在后)。
  关节角单位: 度。

★ 审计修复 P1-E4 —— 模块 docstring 原写"**只读客户端**"，与实现不符：
  本模块明确会写控制器（FC6 写指令字 40101 / 速度 40103 / 程序号 40104 /
  点动触发位 40135 与目标角 40139~44），是**真机会动**的写通道。
  "只读"的错觉会让人放松对写路径的审查，也会误导部署时的防火墙/白名单决策。
  写操作的双闸在 motion 层（EFORT_REAL_MOTION + robot.yaml real_write），
  与本模块是否"只读"无关 —— 详见 docs/审计-项目代码审计与优化改进方案.md P1-E4。
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config
from app.core.logger import get_logger
from app.services import metrics

log = get_logger("modbus")

# ======================================================================
# ★ 实测确认的寄存器表（2026-09-23，详见 docs/控制器Modbus寄存器勘察报告.md）
#   手册 4xxxx = 0 基址 + 1。
READ_BLOCK = 32          # 系统区一次读 32 最稳（qty=23 确定性失败；白名单 1..8,10,12,16,20,24,32,48,64）
RO_WINDOW = 10           # 点动用户区 40135~40144 单读窗口
ADDR_STATUS = 0          # 40001 状态位
ADDR_SPEED = 2           # 40003 运行速度 %
ADDR_ALARM1 = 3          # 40004 报警代码 1
ADDR_ALARM2 = 4          # 40005 报警代码 2
ADDR_PROG = 5            # 40006 当前程序号
ADDR_JOINT1 = 10         # 40011~22 J1~J6 FLOAT（CDAB 字序）
ADDR_CMD = 100           # 40101 指令字（FC6 单条写入）
ADDR_SET_SPEED = 102     # 40103 速度设定（%）
ADDR_SET_PROG = 103      # 40104 目标程序号
ADDR_RO_TRIG = 134       # 40135 PC→机器人位区（Bit0 = 点动触发，电平）
ADDR_JOG_ANG = 138       # 40139~44 J1~J6 目标绝对角 ×100（int16 补码）
ADDR_WO_STAT = 34        # 40035 机器人→PC 位区（Bit0 = 点动完成）
# ★★ 真空吸放/放气触发位 —— **唯一来源**（铁律：PC 写触发、控制器常驻服务程序 210 执行）。
#
#   用户 2026-10-11 在示教器上更新了 210，并给出**权威位表（原话）**：
#     「之前 210 程序的 0 是点位移动，1 是吸气，2 是停止吸气，3 是放气，4 是停止放气，
#       新添加了 5 是吸反面的气，6 停止吸反面，7 是放气反面的，8 是停止放气了」
#   ⇒ 下标 = 210 里 40135（`ro_b[n]`）的**位数**；完成位 = 40035 同位（每次大循环顶部清 0）。
#      0（点位移动）走点动通道（ADDR_JOG_ANG + 40035.Bit0），**不在此表、不许改**。
#
#   索引 → 动作 → 位值（本表就是"8 动作 → 40135 位号"的单一映射口，别在别处再写死位号）：
#     1 吸气(下板/正面)  → 40135.Bit1 (0x0002)
#     2 停止吸气(下板)   → 40135.Bit2 (0x0004)
#     3 放气(下板)       → 40135.Bit3 (0x0008)
#     4 停止放气(下板)   → 40135.Bit4 (0x0010)
#     5 吸反面的气(上板) → 40135.Bit5 (0x0020)
#     6 停止吸反面       → 40135.Bit6 (0x0040)
#     7 放气反面的       → 40135.Bit7 (0x0080)
#     8 停止放气(反面)   → 40135.Bit8 (0x0100)
#
#   ★★ 2026-10-11 前提更正：两面板**各自独立气路** —— 下板用 1~4 位、反面/上板用 5~8 位。
#      此前项目一直按"**两面共用一路真空**（io.DOut[8]）"实现，**该前提是错的**（现已证实）：
#      我们发「吸气」驱动的是**下板那一路**，**反面的那一路从未被驱动** → 这才是"反面吸不到"
#      的真正原因（上一轮误判为"没有 off→on 边沿"）。
#   ★ N = 真空阀输出号（参考项目 8）；M = 吹气阀输出号（参考项目 9）——
#     两者都以现场 IO 监控实测为准（程序不读回 DO，号写错时上位机照报"完成"而阀没动）。
#   ★ 为什么"放气"要独立于"停止吸"：只关阀时工件会因残余负压/密封吸住不掉，
#     必须给一下正压才能脱开；但有些场合只需轻轻松手、不想吹气，故两者不能合并。
VAC_BIT_SUCK = 0x0002         # 40135.Bit1 吸气（下板/正面）
VAC_BIT_RELEASE = 0x0004      # 40135.Bit2 停止吸气（下板）
VAC_BIT_BLOW = 0x0008         # 40135.Bit3 放气（下板，吹气，电平保持）
VAC_BIT_UNBLOW = 0x0010       # 40135.Bit4 停止放气（下板）
VAC_BIT_SUCK_BACK = 0x0020    # 40135.Bit5 吸反面的气（上板）
VAC_BIT_RELEASE_BACK = 0x0040  # 40135.Bit6 停止吸反面
VAC_BIT_BLOW_BACK = 0x0080    # 40135.Bit7 放气反面的
VAC_BIT_UNBLOW_BACK = 0x0100  # 40135.Bit8 停止放气（反面）

# ★★ 8 个气路动作的**唯一映射口**：动作名 → (40135 位值, 中文标签)。
#   位值与上面的常量同源；`rc_vacuum` 只按本表查位，**不再散落写死位号**。
#   键名即 `/control/vacuum` 的 action 取值、也是 `_air` 解析后的调用名。
VAC_ACTIONS: Dict[str, Tuple[int, str]] = {
    # 中文标签 = 真机 stage 文案（流经 `_air` → `_set(stage=label)`）；**改动会波及前端流程条映射**
    # （`frontend/src/sorting/flowSteps.js` 的 STAGE_MAP + `tools/verify_sortflow.mjs`），改动须同步。
    "suck":         (VAC_BIT_SUCK, "吸"),          # 索引 1
    "release":      (VAC_BIT_RELEASE, "停止吸"),    # 索引 2
    "blow":         (VAC_BIT_BLOW, "放"),           # 索引 3
    "unblow":       (VAC_BIT_UNBLOW, "停止放"),     # 索引 4
    "suck_back":    (VAC_BIT_SUCK_BACK, "反面吸"),   # 索引 5
    "release_back": (VAC_BIT_RELEASE_BACK, "反面停止吸"),  # 索引 6
    "blow_back":    (VAC_BIT_BLOW_BACK, "反面放"),   # 索引 7
    "unblow_back":  (VAC_BIT_UNBLOW_BACK, "反面停止放"),   # 索引 8
}
ANG_SCALE = 100

# 命令字：必须同沿单条写入，且始终保留 Bit0(上伺服)+Bit12(伺服使能)
CMD_SERVO = 0x1001
CMD_LOAD = 0x1011
CMD_RUN = 0x1013
CMD_STOP = 0x1005
CMD_CLEAR = 0x1009
CMD_DISABLE = 0x2000
CMD_ZERO = 0x0000
SERVO_REENGAGE_DELAY = 0.6   # 全清→重上电的等待（实测吸合延迟 ≈0.55s）

# 状态位定义（reg0）
STATUS_BITS = {
    "manual": 0, "auto": 1, "remote": 2, "servo": 3, "alarm": 4,
    "estop": 5, "run": 6, "safe1": 7, "safe2": 8, "safe3": 9, "safe4": 10,
    "prog_loaded": 11, "servo_ready": 12, "prog_reserve": 13, "prog_reset": 14,
}


def _build(func: int, addr: int, qty: int, uid: int = 1, tid: int = 1) -> bytes:
    pdu = struct.pack(">BHH", func, addr, qty)
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu


def _words_to_float(low: int, high: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", high, low))[0]


def _float_to_words(f: float) -> Tuple[int, int]:
    """float32 大端 ↔ 两寄存器, 与 read_pose 的字节序严格一致(低字在前)。"""
    b = struct.pack(">f", float(f))
    high = (b[0] << 8) | b[1]
    low = (b[2] << 8) | b[3]
    return low, high


class ModbusRobot:
    def __init__(self) -> None:
        cfg = get_config()
        conn = cfg.connection
        mb = cfg.modbus
        self.host = conn.get("host", "192.168.1.12")
        self.port = int(conn.get("port", 502))
        self.uid = int(conn.get("unit_id", 1))
        self.timeout = float(conn.get("timeout_s", 2.0))
        self.func = int(mb.get("func", 3))
        self.base_addr = int(mb.get("base_addr", 10))
        self.count = int(mb.get("count", 12))
        self._sock: Optional[socket.socket] = None    # P1-1: 复用长连接
        # ★ 采集线程（read_pose）与执行线程（点动链路多条 FC6/FC3）共用同一 socket，
        #   TCP 是纯字节流，并发 send/recv 会串帧 —— 所有 IO 必须持锁整帧完成。
        self._io_lock = threading.Lock()

    def reachable(self) -> bool:
        """快速 TCP 探测, 用于决定模拟/真实模式。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            rc = s.connect_ex((self.host, self.port))
            s.close()
            return rc == 0
        except Exception:
            return False

    def reload_config(self) -> Dict[str, Any]:
        """★ 审计修复 P1-C2：按当前配置刷新连接参数（不新建实例）。

        原实现 host/port/timeout 在 ``__init__`` 里读死，而设置页把
        ``connection.*`` 标成 apply="reconnect" —— 用户改完 IP 点「立即生效」，
        探测和采集仍在用旧地址，只能重启进程，属于"声明与实现不符"。

        就地改字段而不是 ``ModbusRobot()`` 新建实例：本对象被采集线程与
        执行线程**共享**，换实例会让一帧进行到一半的 IO 落在被丢弃的旧 socket 上。
        参数变化时在 IO 锁内关掉旧连接，确保下一发走新地址。
        """
        cfg = get_config()
        conn = cfg.connection
        mb = cfg.modbus
        new = {
            "host": conn.get("host", "192.168.1.12"),
            "port": int(conn.get("port", 502)),
            "uid": int(conn.get("unit_id", 1)),
            "timeout": float(conn.get("timeout_s", 2.0)),
            "func": int(mb.get("func", 3)),
            "base_addr": int(mb.get("base_addr", 10)),
            "count": int(mb.get("count", 12)),
        }
        with self._io_lock:
            changed = any(getattr(self, k) != v for k, v in new.items())
            if changed:
                for k, v in new.items():
                    setattr(self, k, v)
                self._close()      # 旧连接仍指向旧地址/旧超时，必须断开
        return {"changed": changed, "host": new["host"], "port": new["port"]}

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _ensure_sock(self) -> socket.socket:
        if self._sock is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            self._sock = s
        return self._sock

    def _recv_frame(self, s: socket.socket) -> bytes:
        """收全一个 Modbus TCP 帧: MBAP 头 6 字节 + length 指示的后续字节。"""
        buf = b""
        while len(buf) < 6:
            chunk = s.recv(6 - len(buf))
            if not chunk:
                raise ConnectionError("连接被关闭")
            buf += chunk
        length = struct.unpack(">H", buf[4:6])[0]
        total = 6 + length
        while len(buf) < total:
            chunk = s.recv(total - len(buf))
            if not chunk:
                raise ConnectionError("响应不完整")
            buf += chunk
        return buf

    def read_pose(self) -> Tuple[Optional[List[float]], Optional[str]]:
        """返回 (6 个关节角 deg, None) 或 (None, 错误信息)。复用长连接。"""
        # ★ B1 只读指标：只量「IO 往返段」（连接+发送+收帧），解析失败不计入往返。
        t0 = time.perf_counter()
        try:
            with self._io_lock:               # ★ 与点动写链路共用 socket，必须整帧持锁
                s = self._ensure_sock()
                s.sendall(_build(self.func, self.base_addr, self.count, self.uid))
                data = self._recv_frame(s)
        except Exception as e:
            metrics.incr("modbus.read_pose_fail")
            self._close()          # 出错断开, 下次重建
            return None, "timeout/error: %s" % e
        metrics.stat("modbus.read_pose_rt_ms", (time.perf_counter() - t0) * 1000.0)

        if len(data) < 9:
            return None, "响应过短 %d 字节" % len(data)
        fc = data[7]
        if fc & 0x80:
            code = data[8] if len(data) > 8 else -1
            return None, "Modbus 异常 0x%02x" % code
        if fc not in (3, 4):
            return None, "非预期功能码 0x%02x" % fc

        # ★ 审计修复 P1-A4：帧解析必须纳入保护。
        #   原实现在 try 之外裸用 struct.unpack / 下标访问，坏帧（byte count
        #   与实际长度不符、截断帧）会抛 struct.error 直接穿透到采集线程，
        #   若上层无兜底 → 采集线程死亡（"假活"）。
        try:
            bc = data[8]
            if bc < 24 or len(data) < 9 + bc:
                return None, "byte count %d 与帧长 %d 不符" % (bc, len(data))
            if bc > 300:
                return None, "byte count %d 异常（疑似帧失步）" % bc
            regs = struct.unpack(">%dH" % (bc // 2), data[9:9 + bc])
            if len(regs) < 12:
                return None, "寄存器不足 %d" % len(regs)
            pose = [_words_to_float(regs[i * 2], regs[i * 2 + 1]) for i in range(6)]
        except Exception as e:
            return None, "帧解析失败: %s" % e
        return pose, None

    # ==================================================================
    # 实机寄存器协议层（2026-09-23 实测确认；写路径 Stage D 重写）
    #
    # 旧 write_target（FC16 → addr 30）已**实机证伪**：地址 30..41 恒 0，
    # 根本不是目标关节角区。真实下发走"点动通道"：
    #   PC 写 40139~44 目标绝对角(×100 int16, 逐寄存器 FC6)
    #   → 回读校验 → 稳定 0.15s（防竞态：不等待就会触发上一发目标）
    #   → 置 40135.Bit0 触发 → 等 40035.Bit0 完成 → 撤触发。
    # 控制器侧必须有一个常驻点动服务程序（现场 = 200 / JOGSVC）在 WAIT 挂起。
    # ==================================================================
    @staticmethod
    def encode_angle(deg: float) -> int:
        """角度 → 有符号 16 位（×100 补码）。量化 0.01°。

        ★ 审计修复 P0-1：原实现 `int(round(deg*100)) & 0xFFFF` 对超出
          ±327.67° 的角度会**静默回绕**（350° → 35000 → 35000-65536 = -30536
          → 下发 -305.36°，方向完全相反），且回读比对拿到的也是回绕后的值，
          于是"回读一致"校验形同虚设。超出量程一律拒绝，绝不静默反号。
        """
        v = int(round(float(deg) * ANG_SCALE))
        if v < -0x8000 or v > 0x7FFF:
            raise ValueError(
                "角度 %s° 超出 int16×100 表示范围(±327.67°)，拒绝下发" % deg)
        return v & 0xFFFF

    @staticmethod
    def decode_angle(word: int) -> float:
        v = word - 0x10000 if word > 0x7FFF else word
        return v / ANG_SCALE

    @staticmethod
    def _to_signed(word: Optional[int]) -> Optional[int]:
        """无符号寄存器字 → 有符号值。用于回读比对（P0-1 配套）。"""
        if word is None:
            return None
        w = int(word) & 0xFFFF
        return w - 0x10000 if w > 0x7FFF else w

    def _echo_matches(self, got: Optional[int], want: int) -> bool:
        """回读比对必须按**有符号值**解码后再比，不能比原始无符号字。

        P0-1 配套：`want` 是 encode_angle 返回的 0..65535 补码，
        `got` 是控制器回读的字，二者都转成有符号后才等价。
        """
        if got is None:
            return False
        return self._to_signed(got) == self._to_signed(want)

    def read_regs(self, addr: int, qty: int) -> Tuple[Optional[List[int]], Optional[str]]:
        """FC3 读任意寄存器区。返回 (寄存器列表, None) 或 (None, 错误)。"""
        # ★ B1 只读指标：单次寄存器读的 IO 往返（点动/吸放链路的逐拍读都从这里过）。
        t0 = time.perf_counter()
        try:
            with self._io_lock:
                s = self._ensure_sock()
                s.sendall(_build(3, int(addr), int(qty), self.uid))
                data = self._recv_frame(s)
        except Exception as e:
            metrics.incr("modbus.read_regs_fail")
            self._close()
            return None, "timeout/error: %s" % e
        metrics.stat("modbus.read_regs_rt_ms", (time.perf_counter() - t0) * 1000.0)
        if len(data) < 9:
            return None, "响应过短 %d 字节" % len(data)
        fc = data[7]
        if fc & 0x80:
            return None, "Modbus 异常 0x%02x" % (data[8] if len(data) > 8 else 0)
        if fc != 3:
            return None, "非预期功能码 0x%02x" % fc
        # ★ 审计修复 P1-A4：解析纳入保护，坏帧返回 (None, err) 而非抛异常穿透
        try:
            bc = data[8]
            if len(data) < 9 + bc:
                return None, "byte count %d 与帧长 %d 不符" % (bc, len(data))
            regs = struct.unpack(">%dH" % (bc // 2), data[9:9 + bc])
        except Exception as e:
            return None, "帧解析失败: %s" % e
        return list(regs), None

    def write_reg(self, addr: int, value: int) -> Tuple[Optional[int], Optional[str]]:
        """FC6 写单寄存器。返回 (回显值, None) 或 (None, 错误)。

        ★★ 帧布局（务必按这个取，别再挪偏）：MBAP 7 字节
           [0:2]事务 [2:4]协议 [4:6]长度 [6]单元号，随后 PDU：
           [7]功能码 [8:10]**寄存器地址** [10:12]**寄存器值**。
        ★ 真实事故（速度设定回读不一致）：原实现取 `data[9:11]` —— 那是"地址低字节+值高字节"，
          写 40103=17% 时地址 102=0x0066、值 17=0x0011，于是回显被解成 0x6600 = **26112**，
          触发 B-03「速度设定回读不一致：写 17% 读回 26112%」，把正确的下发误判为失败。
        """
        try:
            with self._io_lock:
                s = self._ensure_sock()
                pdu = struct.pack(">BHH", 6, int(addr), int(value) & 0xFFFF)
                s.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, self.uid) + pdu)
                data = self._recv_frame(s)
        except Exception as e:
            self._close()
            return None, "写异常: %s" % e
        if len(data) < 12:
            return None, "写响应过短 %d 字节" % len(data)
        fc = data[7]
        if fc & 0x80:
            return None, "Modbus 异常 0x%02x" % (data[8] if len(data) > 8 else 0)
        if fc != 6:
            return None, "非预期功能码 0x%02x" % fc
        # ★ 值在 [10:12]：地址占 [8:10]，不要取到地址字节（见上方真实事故注释）
        return struct.unpack(">H", data[10:12])[0], None

    def rc_command(self, word: int) -> Tuple[Optional[int], Optional[str]]:
        """写 40101 指令字。★ 必须同沿单条写入（保留 Bit0+Bit12）。"""
        return self.write_reg(ADDR_CMD, int(word) & 0xFFFF)

    def rc_snapshot(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """读一帧完整快照：系统区 32 + 点动用户区 10 + 完成位 1。

        ★ 40135~44 是合法单读窗口（10 个）：[0]=触发位区、[4..9]=目标角×100；
          40035 完成位单独一发（qty=1 在白名单内）。
        """
        regs, err = self.read_regs(0, READ_BLOCK)
        if regs is None:
            return None, err
        # ★ 全维度审查 B-15：控制器返回短帧时，下面的 regs[ADDR_JOINT1+i*2] 会
        #   IndexError → 采集循环被整条打崩、/api/rc-status 直接 500。
        #   这里显式校验寄存器数量并回可读错误。
        if len(regs) < READ_BLOCK:
            return None, "寄存器数不足 %d（期望 %d）" % (len(regs), READ_BLOCK)
        ro, err2 = self.read_regs(ADDR_RO_TRIG, RO_WINDOW)
        wo, err3 = self.read_regs(ADDR_WO_STAT, 1)
        st = regs[ADDR_STATUS]
        bits = {k: (st >> b) & 1 for k, b in STATUS_BITS.items()}
        return {
            "status_word": st,
            "bits": bits,
            "mode": ("manual" if bits["manual"] else
                     "auto" if bits["auto"] else
                     "remote" if bits["remote"] else "unknown"),
            "speed_pct": regs[ADDR_SPEED],
            "alarm1": regs[ADDR_ALARM1],
            "alarm2": regs[ADDR_ALARM2],
            "prog": regs[ADDR_PROG],
            "joints": [_words_to_float(regs[ADDR_JOINT1 + i * 2],
                                       regs[ADDR_JOINT1 + i * 2 + 1]) for i in range(6)],
            "jog_trig": bool(ro[0] & 0x0001) if ro else None,
            "jog_target_raw": ro[4:10] if ro else None,
            "jog_done": (bool(wo[0] & 0x0001) if wo else None),
            "errors": [e for e in (err2, err3) if e],
        }, None

    def rc_write_jog_target(self, joints: List[float]) -> Tuple[bool, Optional[str]]:
        """写 6 个目标绝对角到 40139~44（6 发 FC6）+ 回读校验。不置触发位。"""
        # ★ 审计修复 P0-1：encode_angle 现在会对超量程抛 ValueError，
        #   必须在**写任何一个寄存器之前**先整体校验，避免写了一半才失败。
        try:
            encoded = [self.encode_angle(a) for a in joints[:6]]
        except ValueError as e:
            return False, str(e)
        if len(encoded) < 6:
            return False, "目标角不足 6 个（收到 %d）" % len(encoded)
        for i, word in enumerate(encoded):
            _, err = self.write_reg(ADDR_JOG_ANG + i, word)
            if err:
                return False, "写目标角 J%d 失败: %s" % (i + 1, err)
        ro, err = self.read_regs(ADDR_RO_TRIG, RO_WINDOW)
        if ro is None:
            return False, "目标角回读失败: %s" % err
        for i in range(6):
            want = encoded[i]
            got = ro[4 + i] if 4 + i < len(ro) else None
            # ★ P0-1：有符号比对（原实现比无符号字，回绕值会被误判一致）
            if not self._echo_matches(got, want):
                return False, ("目标角回读不一致 J%d：写 %s 读 %s"
                               % (i + 1, self.decode_angle(want),
                                  self.decode_angle(got) if got is not None else None))
        return True, None

    def rc_jog_execute(self, joints: List[float], speed_pct: int = 100,
                       on_event=None, should_abort=None,
                       cur_joints: Optional[List[float]] = None) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """完整点动链路（同步）：校验触发位 → 写目标+回读 → settle → 触发 → 等完成 → 撤触发。

        调用方需已通过 real_write + EFORT_REAL_MOTION 双确认（见 motion.py）。
        on_event(step, msg) 可选，用于逐拍上报进度。
        should_abort() 可选，急停回调：在触发前与等待完成轮询中检查，
        为 True 时立即撤触发并中止（不等待本次点动走完）—— 急停必须能
        打断飞行中的点动，而不是排队等它自然结束（最长可达 30s）。
        cur_joints: ★ 审计修复 P1-A2 —— 下发时的当前位姿，用于按**真实位移**
        估算完成位超时；不传则退回"绝对角"保守口径（只会更宽松）。
        """
        detail: Dict[str, Any] = {"steps": []}

        # ★ 审计修复 P1-E13：原为一行嵌套 lambda（ruff E731）。展开成 def 之后，
        #   副作用顺序一眼可见：先落一步明细，再把同一拍透传给调用方的上报回调。
        #   三个调用点都不用返回值，故显式标注 -> None。
        def ev(step: str, msg: str) -> None:
            detail["steps"].append({"step": step, "msg": msg})
            if on_event:
                on_event(step, msg)

        # 0) 前置守卫：触发位必须空闲（上一发还没收尾就再写 = 竞态，绝不允许）
        if should_abort and should_abort():
            return False, "急停已触发，本次点动未下发", detail
        ro, err = self.read_regs(ADDR_RO_TRIG, RO_WINDOW)
        if ro is None:
            return False, "读触发位失败: %s" % err, detail
        if ro[0] & 0x0001:
            return False, "点动触发位仍为 1（上一发未收尾），已拒绝本次下发", detail

        # 0.5) ★★ 报警守卫（2026-09-29 实机新增）★★
        #   有活动报警时控制器会**拒绝执行 MJOINT**，但寄存器照样写得进去 ——
        #   症状是：目标角写成功且回读一致、触发位置 1、完成位永不置位，最后
        #   报一句"点动完成位 X 秒内未置位（检查点动服务程序是否在 WAIT 挂起）"。
        #   实测教训：真凶是控制器上挂着一个未清的 5005 报警（程序运行中会把
        #   它重新顶上来），而上面那句提示把整轮排查带向"程序有没有在 WAIT 挂起"。
        #   这里在读触发位的同一批次里顺手把状态字+报警码读回来，提前拦住并报真因。
        #   （只读，不写；读失败时保持原行为放行，由后面的触发位守卫兜底。）
        regs, serr = self.read_regs(ADDR_STATUS, 5)   # 状态字 + 报警码1/2（qty=5 在白名单内）
        if regs is not None and len(regs) >= 5:
            if (regs[ADDR_STATUS] >> STATUS_BITS["alarm"]) & 1:
                a1, a2 = regs[ADDR_ALARM1], regs[ADDR_ALARM2]
                detail["alarm"] = [a1, a2]
                return False, ("控制器有**未清报警 %s/%s**，运动指令会被拒绝执行"
                               "（目标角能写进去但机器人不动、完成位不置位）。"
                               "请先停机并清报警：示教器「复位/清报警」，或点网页「一键就绪」"
                               "（它会按 停机→清报警→加载→运行 的正确顺序处理）。"
                               "★ 若清完立刻复现，说明当前运行的程序是坏的，必须先停掉它"
                               % (a1, a2)), detail

        # 1) 写目标角 + 回读校验（"角度先落位、后触发"的硬顺序）
        ok, err = self.rc_write_jog_target(joints)
        if not ok:
            return False, err, detail
        ev("target", "目标角已写入并回读一致")

        # 2) settle ≥0.15s：写完立刻触发，程序可能读到上一发目标
        #    （症状：零位移但完成位瞬间回 1）—— 实测教训，必须等。
        time.sleep(0.15)
        if should_abort and should_abort():
            # 目标角已写但未触发 —— 不撤不补，触发位本来就是 0，直接放弃
            return False, "急停已触发，已放弃置触发位（目标角未执行）", detail

        # 3) 触发
        _, err = self.write_reg(ADDR_RO_TRIG, 0x0001)
        if err:
            return False, "置触发位失败: %s" % err, detail
        ev("trigger", "触发位已置 1")

        # ★ 审计修复 P1-A1：触发位清理必须走 finally。
        #   原实现在第 4 步等待期间若抛异常（read_regs 内部虽吞异常，但
        #   结构变化/编码错误/KeyboardInterrupt 都会穿透），触发位恒为 1，
        #   下一发被"触发位仍为 1"守卫永久拒绝 → 点动链路整条瘫痪。
        t0 = time.time()
        done = False
        aborted = False
        cerr = None
        try:
            # 4) 等完成位（40035.Bit0）。超时按**位移**与速度估算：
            #    40103=5% ≈1.3°/s，100% ≈60~90°/s —— 保守 45°/s@100% 线性折算 + 2s。
            #    ★ 审计修复 P1-A2：原式用"绝对角"当位移（J2 在 -90° 静止也要等 6s），
            #      改为用真实位移 max|目标 - 当前|；无当前位姿时退回旧口径。
            disp = self._max_displacement(joints, cur_joints)
            timeout = max(4.0, disp / max(1.0, 45.0 * min(100, speed_pct) / 100.0) * 2.0 + 2.0)
            while time.time() - t0 < timeout:
                if should_abort and should_abort():
                    aborted = True
                    break
                wo, err = self.read_regs(ADDR_WO_STAT, 1)
                if wo is not None and (wo[0] & 0x0001):
                    done = True
                    break
                time.sleep(0.05)
        finally:
            # 5) 无论成败（含异常）都撤触发（程序回到 WAIT 挂起）
            _, cerr = self.write_reg(ADDR_RO_TRIG, 0x0000)
        detail["elapsed_s"] = round(time.time() - t0, 3)
        detail["timeout_s"] = round(timeout, 2)
        if aborted:
            return False, "点动执行中被急停中止，触发位已撤", detail
        if not done:
            return False, ("点动完成位 %0.1fs 内未置位（检查点动服务程序是否在 WAIT 挂起）"
                           % timeout), detail
        if cerr:
            return False, "完成但撤触发失败: %s" % cerr, detail
        ev("done", "完成位已置 1，用时 %0.2fs" % detail["elapsed_s"])
        return True, None, detail

    def rc_vacuum(self, action: str, timeout: float = 6.0):
        """触发吸/放/放气（不移动机器人关节）—— **8 个动作，索引严格按用户 210 位表**。

        用户原话（2026-10-11）：「0 是点位移动，1 是吸气，2 是停止吸气，3 是放气，
          4 是停止放气，新添加了 5 是吸反面的气，6 停止吸反面，7 是放气反面的，
          8 是停止放气了」。位值见模块顶部 `VAC_ACTIONS`（**唯一映射口**，本函数不再写死位号）。
        完成位 = 40035 同位（每次大循环顶部清 0）。

        八路气路**全部电平保持**（置位后一直保持，直到对位停止动作）：
          suck/release/blow/unblow           → 下板/正面（1~4 位）
          suck_back/…/unblow_back            → 反面/上板（5~8 位）
        ★★ 2026-10-11 前提更正：两面板**各自独立气路** —— 原来的"全项目只有一路真空
          （io.DOut[8]）、两面共用"**已被现场纠正**（反面那一路此前从未被驱动）。
        ★ 放气是**脉冲**（程序内 DWELL(0.40)），不是长吹 —— 破真空只需一下正压。
        ★ 与 rc_jog_execute 同构：置触发位 → 轮询 40035 对应完成位 → finally 撤触发。
        前置：控制器须在接受 PC 指令的模式（AUTO/远程）且常驻服务程序运行中，
              否则触发位无人响应 → 超时失败（安全：不写任何运动指令）。
        """
        spec = VAC_ACTIONS.get(str(action or "").strip().lower())
        if spec is None:
            return False, ("action 必须是 %s 之一" % "/".join(VAC_ACTIONS)), {}
        trig, label = spec
        done_bit = trig          # 触发位与完成位同值（40035 同位）
        detail: Dict[str, Any] = {"action": action}

        # 0) 前置守卫：触发位必须空闲（上一发没收尾就再写 = 竞态，拒绝）
        ro, err = self.read_regs(ADDR_RO_TRIG, RO_WINDOW)
        if ro is None:
            return False, "读触发位失败: %s" % err, detail
        if ro[0] & trig:
            return False, "%s触发位仍为 1（上一发未收尾），已拒绝" % label, detail

        # 1) 触发
        _, err = self.write_reg(ADDR_RO_TRIG, trig)
        if err:
            return False, "置%s触发位失败: %s" % (label, err), detail
        detail["triggered"] = True

        # 2) 轮询完成位（40035.Bit1/Bit2）；无论成败 finally 撤触发
        t0 = time.time()
        done = False
        cerr = None
        try:
            while time.time() - t0 < timeout:
                wo, err = self.read_regs(ADDR_WO_STAT, 1)
                if wo is not None and (wo[0] & done_bit):
                    done = True
                    break
                time.sleep(0.05)
        finally:
            _, cerr = self.write_reg(ADDR_RO_TRIG, 0x0000)
        detail["elapsed_s"] = round(time.time() - t0, 3)
        if not done:
            return False, ("%s完成位 %0.1fs 内未置位（检查控制器是否在 AUTO/远程模式"
                           "且常驻服务程序(200)运行中）"
                           % (label, timeout)), detail
        if cerr:
            return False, "完成但撤触发失败: %s" % cerr, detail
        detail["done"] = True
        return True, None, detail

    def rc_vacuum_prepare(self, prog_no: int) -> Tuple[bool, Optional[str]]:
        """确保真空服务程序常驻运行：伺服吸合 → 停止当前程序 → 加载 → 运行。

        ★ 官方手册：程序运行过程中不可加载 —— 必须先 CMD_STOP（程序停止，非急停，
          不产生运动），否则 CMD_LOAD 被控制器静默忽略（实测：prog 纹丝不动）。
        ★ 加载成功判据 = 40006(prog) 变为目标程序号；prog_loaded 位是**残留的**
          （上一发加载过就恒 1），只看它会误判。
        幂等：目标程序已在运行 → 直接成功。
        """
        s, err = self.rc_snapshot()
        if s is None:
            return False, "读控制器快照失败: %s" % err
        if s["prog"] == prog_no and s["bits"].get("run"):
            return True, None
        # 1) 伺服吸合（掉电时 CMD_LOAD/CMD_RUN 被忽略；上伺服不含运动指令）
        if not s["bits"].get("servo"):
            self.rc_command(CMD_ZERO)
            time.sleep(0.6)
            self.rc_command(CMD_SERVO)
            t0 = time.time()
            ok_servo = False
            while time.time() - t0 < 5.0:
                s, _ = self.rc_snapshot()
                if s and s["bits"].get("servo"):
                    ok_servo = True
                    break
                time.sleep(0.2)
            if not ok_servo:
                return False, "伺服 5s 内未吸合（检查急停/安全回路/示教器使能）"
        # 2) 停止当前在跑的程序（官方：运行中不可加载）
        _, err = self.rc_command(CMD_STOP)
        if err:
            return False, "CMD_STOP 失败: %s" % err
        t0 = time.time()
        while time.time() - t0 < 2.0:
            s, _ = self.rc_snapshot()
            if s and not s["bits"].get("run"):
                break
            time.sleep(0.1)
        # 3) 加载（成功判据 = 目标程序号已就位；★ 不能只看 prog_loaded —— 见下）
        _, err = self.write_reg(ADDR_SET_PROG, prog_no)
        if err:
            return False, "写 40104 失败: %s" % err
        _, err = self.rc_command(CMD_LOAD)
        if err:
            return False, "CMD_LOAD 失败: %s" % err
        t0 = time.time()
        s = None
        while time.time() - t0 < 3.0:
            s, _ = self.rc_snapshot()
            # ★★ 2026-09-29 实机修正：程序一 RUN 起来，"已加载"位(状态字 bit11) 会**落下**
            #    （实测 prog=210 / run=1 / prog_loaded=0 就是正常运行态）。
            #    原来只认 prog_loaded → 永远等不到 → 假报"程序加载失败(5005)"，
            #    并把这个错误方向一路传给现场。判据改为「号对 + (已加载 或 运行中)」。
            if s and s["prog"] == prog_no and (s["bits"].get("prog_loaded")
                                               or s["bits"].get("run")):
                break
            time.sleep(0.1)
        else:
            return False, ("程序 %d 加载失败（控制器目标程序号仍为 %s）——可能是该程序号在"
                           "控制器上不存在、有未清报警(5005)，或示教器停在文件/编辑界面"
                           % (prog_no, s.get("prog") if s else "?"))
        # 4) 运行（挂起等触发，不产生运动）
        _, err = self.rc_command(CMD_RUN)
        if err:
            return False, "CMD_RUN 失败: %s" % err
        t0 = time.time()
        while time.time() - t0 < 2.0:
            s, _ = self.rc_snapshot()
            if s and s["bits"].get("run"):
                return True, None
            time.sleep(0.1)
        return False, "程序 %d 运行位 2s 未置位" % prog_no

    @staticmethod
    def _max_displacement(joints: List[float],
                          cur_joints: Optional[List[float]]) -> float:
        """下发目标相对当前位姿的最大关节位移（deg）。

        无当前位姿（调用方未传）时退回"绝对角最大值"这一保守口径，
        保证超时只会更长、不会更短 —— 宁可多等，不可早判失败。
        """
        try:
            tgt = [float(j) for j in joints[:6]]
            if cur_joints is None or len(cur_joints) < 6:
                return max(abs(v) for v in tgt)
            cur = [float(c) for c in cur_joints[:6]]
            return max(abs(t - c) for t, c in zip(tgt, cur)) or 0.0
        except Exception:
            return 0.0

    def rc_estop(self, on: bool) -> Tuple[bool, Optional[str]]:
        """软件急停/停止：0x1005 停止命令字（旧 estop_addr 方案已证伪废弃）。"""
        if on:
            return self.rc_command(CMD_STOP)
        return True, None   # 复位不做物理动作（真机复位走控制器按钮/清报警流程）
