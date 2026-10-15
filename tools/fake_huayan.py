# -*- coding: utf-8 -*-
"""华沿 ELFIN 控制器的**只读**仿真服务端（无硬件时验证 DataSheet 解析用）。

它扮演的是机器人那一侧：TCP 服务端，客户端连上后每 50ms 主动推一帧 LTBR+JSON。
本脚本不接收任何命令 —— 只单向推流，所以不存在"误发指令让机器人动"的可能。

用法：
    python tools/fake_huayan.py --port 20004 --joints 10,20,30,40,50,60
    python tools/fake_huayan.py --port 20004 --motion sine --period 8
    python tools/fake_huayan.py --port 20004 --byte-order big     # 验证猜错字节序时会怎样

帧格式取自《HuayanRobotV6控制通信协议接口》§5.1：
    Magicnumber 4B = "LTBR" | 总长 4B | 净数据长度 4B | 净数据(JSON)
★ 文档没写这两个 4 字节整数的字节序，所以 --byte-order 两个方向都能出，
  首台真机联调时用实测确定 config/fleet.yaml 里的 header_byte_order。
JSON 字段名与 §5.2 的示例逐字对齐（含 Actual_Position 前 6 个是关节角、
后 6 个是笛卡尔这条容易踩的约定）。
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import threading
import time

BASE_POS = [0.0, 0.0, 81.099, 0.0, 81.099, 0.0]
BASE_CART = [367.945, 0.0, 359.628, 180.000, 17.803, 180.000]


def joint_series(t: float, base, mode: str, amp: float, period: float):
    if mode == "none":
        return list(base)
    return [base[i] + amp * math.sin(2 * math.pi * (t / period) + i * 0.7)
            for i in range(6)]


def build_packet(joints, cart, fmt: str, moving: bool) -> bytes:
    pos = [round(v, 3) for v in joints] + [round(v, 3) for v in cart]
    payload = {
        "PosAndVel": {
            "Actual_Position": ["%.3f" % v for v in pos],
            "Actual_Joint_Current": ["-0.000"] * 6,
            "Actual_Joint_Velocity": ["%.3f" % (0.5 if moving else 0.0)] * 6,
            "Actual_Joint_Acceleration": ["0.000"] * 6,
            "Actual_PCS_TCP": ["%.3f" % v for v in cart],
            "Actual_PCS_Base": ["%.3f" % v for v in cart],
            "Actual_Override": "1.000",
        },
        "StateAndError": {
            "robotState": 34,
            "robotEnabled": 1,
            "robotPaused": 0,
            "robotMoving": 1 if moving else 0,
            "InPos": 0 if moving else 1,
            "Error_AxisID": 0,
            "Error_Code": 0,
            "AutoMode": "1",
            "BrakeState": [0] * 6,
            "nAxisStatus": [6] * 6,
        },
        "MsgTitle": {"Stamp": "%d" % int(time.time() * 1000),
                     "UpdateTime": time.strftime("%H:%M:%S")},
    }
    net = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    total = 12 + len(net)
    return b"LTBR" + struct.pack(fmt + "II", total, len(net)) + net


class Server:
    def __init__(self, port: int, joints, mode: str, amp: float, period: float,
                 interval: float, byte_order: str) -> None:
        self.port = port
        self.joints = joints
        self.mode = mode
        self.amp = amp
        self.period = period
        self.interval = interval
        self.fmt = ">" if byte_order == "big" else "<"
        self.byte_order = byte_order
        self.stop = threading.Event()
        self.packets = 0
        self.clients = 0

    def _handle(self, conn: socket.socket, addr) -> None:
        self.clients += 1
        print("[fake-huayan] 客户端接入 %s（本脚本只推流，不处理任何命令）" % (addr,))
        t0 = time.time()
        next_at = t0
        try:
            while not self.stop.is_set():
                now = time.time()
                if now >= next_at:
                    js = joint_series(now - t0, self.joints, self.mode, self.amp, self.period)
                    cart = [js[0], js[1], js[2], 367.945, 0.0, 359.628]
                    conn.sendall(build_packet(js, cart, self.fmt,
                                              moving=(self.mode != "none")))
                    self.packets += 1
                    next_at = now + self.interval
                time.sleep(0.005)
        except Exception as e:
            print("[fake-huayan] 连接结束:", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(4)
        srv.settimeout(0.5)
        print("[fake-huayan] 监听 127.0.0.1:%d，推送周期 %.0fms，字节序 %s，运动 %s"
              % (self.port, self.interval * 1000, self.byte_order, self.mode))
        threads = []
        while not self.stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            th = threading.Thread(target=self._handle, args=(conn, addr), daemon=True)
            th.start()
            threads.append(th)
        srv.close()
        print("[fake-huayan] 退出，共推 %d 帧 / %d 个客户端" % (self.packets, self.clients))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=20004)
    ap.add_argument("--joints", default="0,0,81.099,0,81.099,0")
    ap.add_argument("--motion", choices=["none", "sine"], default="sine")
    ap.add_argument("--amp", type=float, default=15.0)
    ap.add_argument("--period", type=float, default=8.0)
    ap.add_argument("--interval-ms", type=float, default=50.0)
    ap.add_argument("--byte-order", choices=["little", "big"], default="little")
    a = ap.parse_args()
    Server(a.port, [float(x) for x in a.joints.split(",")], a.motion, a.amp, a.period,
           a.interval_ms / 1000.0, a.byte_order).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
