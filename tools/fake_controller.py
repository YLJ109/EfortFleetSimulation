# -*- coding: utf-8 -*-
"""假机器人控制器（Modbus TCP 从站）—— 纯测试用，不碰真机。

用途：
  1. 验证只读勘察工具（scripts/reg_survey.py / port_scan.py）在没有真机时也能跑通；
  2. 后端 fleet 化之后，本机一次起 12 个假控制器做并发/断线/串扰测试；
  3. 演示"关节在动"时 watch 的变化行长什么样。

★ 本从站**不实现任何写功能码**：收到 FC6/FC16 一律回 Modbus 异常 0x02
  （illegal data address）。所以拿它测客户端时，任何"写成功"都是客户端的 bug。

寄存器布局刻意模仿埃夫特 Robox（0 基址）：
  addr 0        状态字（1=AUTO 运行）
  addr 10..21   J1..J6，float32，大端、字序交换(CDAB)，单位 deg
  addr 34       机器人→PC 用户位区（Bit0 = 点动完成）
  addr 138..143 J1..J6，int16 × 100
  addr 100/102/103/134  写区，这里只读返回 0

用法：
    python tools/fake_controller.py --port 5021
    python tools/fake_controller.py --port 5021 --motion none --joints 10,20,30,40,50,60
    python tools/fake_controller.py --port 5021 --motion sine --amp 15 --period 8
"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import threading
import time

STATUS_ADDR = 0
FLOAT_BASE = 10          # J1..J6 float32 → 12 个寄存器
RO_TRIG = 34
INT_BASE = 138           # J1..J6 int16×100
FLOAT_TOP = FLOAT_BASE + 12
INT_TOP = INT_BASE + 6


def _f2w(f: float):
    """float32 → (low, high)。

    与 services/modbus.py 的 `_w2f(low, high) = unpack('>f', pack('>HH', high, low))`
    对齐：先发的字是**低字**（CDAB 字序），后发的才是高字。
    这里曾经把两个名字写反，导致假控制器发的是 ABCD —— 客户端按 CDAB 解出来
    全是 1e-41 这种废值，勘察工具会误判成"这台不说 Modbus"。
    """
    first, second = struct.unpack(">HH", struct.pack(">f", f))
    return second, first


class FakeController:
    def __init__(self, joints, motion, amp, period) -> None:
        self.base = list(joints)
        self.motion = motion
        self.amp = amp
        self.period = period
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.conns = 0
        self.reads = 0
        self.writes_refused = 0

    def joints(self) -> list:
        """当前关节角（deg）。motion=sine 时按正弦缓慢摆动，模拟真机在动。"""
        if self.motion == "none":
            return list(self.base)
        el = time.time() - self.t0
        out = []
        for i, b in enumerate(self.base):
            phase = 2 * math.pi * (el / self.period + i * 0.15)
            out.append(b + self.amp * math.sin(phase))
        return out

    def regs(self) -> dict:
        q = self.joints()
        m = {STATUS_ADDR: 1, RO_TRIG: 1}          # Bit0 常置 1 = 点动"已完成"
        for i, deg in enumerate(q):
            low, high = _f2w(deg)
            m[FLOAT_BASE + i * 2] = low
            m[FLOAT_BASE + i * 2 + 1] = high
            m[INT_BASE + i] = int(round(deg * 100)) & 0xFFFF
        return m

    def handle(self, conn: socket.socket) -> None:
        conn.settimeout(30.0)
        with self.lock:
            self.conns += 1
        buf = b""
        try:
            while True:
                c = conn.recv(1)
                if not c:
                    break
                buf += c
                if len(buf) < 8:
                    continue
                total = struct.unpack(">H", buf[4:6])[0]
                if len(buf) < 6 + total:
                    continue
                frame, buf = buf[:6 + total], buf[6 + total:]
                tid = struct.unpack(">H", frame[0:2])[0]
                uid, fc = frame[6], frame[7]
                body = self._reply(fc, frame[8:])
                # MBAP 的 length 字段 = unit_id(1) + PDU 字节数
                if body is None:                       # 异常响应：PDU = 功能码 + 异常码
                    payload = struct.pack(">HHHB", tid, 0, 3, uid) + bytes([fc | 0x80, 0x02])
                else:
                    payload = struct.pack(">HHHB", tid, 0, len(body) + 1, uid) + body
                conn.sendall(payload)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            with self.lock:
                self.conns -= 1

    def _reply(self, fc: int, pdu: bytes):
        """返回响应 PDU；None = 回异常码。"""
        if fc == 3:                                    # 读保持寄存器（只读勘察走这条）
            addr, qty = struct.unpack(">HH", pdu[:4])
            with self.lock:
                m = self.regs()
                self.reads += 1
            if qty < 1 or qty > 125:
                return None
            out = b""
            for i in range(qty):
                out += struct.pack(">H", m.get(addr + i, 0))
            return struct.pack(">BB", 3, qty * 2) + out
        if fc in (5, 6, 15, 16, 22, 23):               # 一切写功能码
            with self.lock:
                self.writes_refused += 1
            return None
        return None                                    # 其它功能码也不支持


def serve(port: int, ctl: FakeController) -> None:
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", port))
    ls.listen(16)
    print("假控制器已启动  http://127.0.0.1:%d  (Modbus TCP, 只读)" % port)
    print("  关节角 %s  motion=%s" % (ctl.base, ctl.motion))
    print("  Ctrl+C 停止。勘察示例：")
    print("    python scripts/reg_survey.py --host 127.0.0.1 --port %d scan --max-addr 150" % port)
    print("    python scripts/reg_survey.py --host 127.0.0.1 --port %d watch 30" % port)
    try:
        while True:
            conn, _ = ls.accept()
            threading.Thread(target=ctl.handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        print("\n停止。累计读 %d 次 / 拒绝写 %d 次" % (ctl.reads, ctl.writes_refused))
    finally:
        ls.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="假机器人控制器（Modbus TCP 从站，只读）")
    ap.add_argument("--port", type=int, default=5021)
    ap.add_argument("--joints", default="0,0,0,0,0,0", help="六个初始关节角 deg")
    ap.add_argument("--motion", choices=["none", "sine"], default="sine")
    ap.add_argument("--amp", type=float, default=15.0, help="正弦振幅 deg")
    ap.add_argument("--period", type=float, default=8.0, help="正弦周期 s")
    a = ap.parse_args()
    js = [float(x) for x in a.joints.split(",")]
    if len(js) != 6:
        print("--joints 需要正好 6 个值")
        return 2
    serve(a.port, FakeController(js, a.motion, a.amp, a.period))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
