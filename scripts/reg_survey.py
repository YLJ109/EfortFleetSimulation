# -*- coding: utf-8 -*-
"""控制器 Modbus 寄存器**只读**勘察工具。

★ 本工具只发 FC3（读保持寄存器），**绝不写入任何寄存器**。
  ★ 审计修复 P1-E5：原文写「robot.yaml 里 motion.write_base_addr / write_speed_addr /
  estop_addr 至今标着 TODO（未按手册核对）」—— 已过期。2026-09-23 的实测勘察
  （docs/控制器Modbus寄存器勘察报告.md）已把这三个假设**证伪**，robot.yaml 里现在
  标的是「已证伪」，写路径改走 40101 指令字 / 40103 速度 / 40135 触发位 / 40139~44 目标角。
  本工具的价值不变：现场仍要用只读方式确认"控制器暴露了哪些地址、哪些地址在动"——
  没有这些事实，开启 motion.real_write 就是在盲写真机。

用法（默认连 config/robot.yaml 里那台埃夫特 192.168.1.12）：
    python scripts/reg_survey.py windows          # 探测允许读取的地址窗口
    python scripts/reg_survey.py dump             # 全量 dump 可读寄存器（0..400）
    python scripts/reg_survey.py joints           # 复核两路关节角读数
    python scripts/reg_survey.py watch [秒]       # 旁路监听寄存器变化（操作示教器时用）

★ 换一台**没见过**的控制器（例如华沿）—— 三条命令按顺序跑：
    python scripts/reg_survey.py --host 192.168.2.21 scan
        → 找出这台控制器允许读哪些地址，结果存 logs/survey_192_168_2_21.json
    python scripts/reg_survey.py --host 192.168.2.21 watch 300 --from-file auto
        → 只盯上一步探到的可读地址；此时**只动 J1 一个轴**，看哪个地址在跳
    python scripts/reg_survey.py --host 192.168.2.21 dump
        → 对照非零寄存器，把「地址 ↔ 关节」表手抄进勘察报告

  watch 的变化行会自动附带 float32 反解（大端/字序交换两种），
  出现 `≈ -12.345°` 这种量级的，基本就是关节角。

安全边界（不要越过）：
    · 只读。任何 FC6/FC16 都不在本工具范围内。
    · ★ 特别注意：**不要**试图用"写 0 试探"来验证目标寄存器区。
      如果某个地址区真的是"目标关节角"，写 0 = 命令机器人回到全零位姿，
      这比写当前位姿危险得多。写路径必须等手册核对后再开。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import sys
import time

HOST = "192.168.1.12"
PORT = 502
UID = 1
MAXADDR = 400


def _w2f(low: int, high: int) -> float:
    """两寄存器 → float32（大端、字序交换），与 services/modbus.py 一致。"""
    return struct.unpack(">f", struct.pack(">HH", high, low))[0]


def _i16(v: int) -> int:
    return v - 65536 if v > 32767 else v


class Client:
    """复用单条 TCP 长连接。控制器对短连接连打比较敏感（会回异常）。"""

    def __init__(self, host: str = HOST, port: int = PORT, uid: int = UID) -> None:
        self.host, self.port, self.uid = host, port, uid
        self.tid = 0
        self.s: socket.socket | None = None
        self._connect()

    def _connect(self) -> None:
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.settimeout(2.5)
        self.s.connect((self.host, self.port))

    def _recv(self) -> bytes:
        buf = b""
        while len(buf) < 6:
            c = self.s.recv(6 - len(buf))
            if not c:
                raise ConnectionError("连接被关闭")
            buf += c
        total = 6 + struct.unpack(">H", buf[4:6])[0]
        while len(buf) < total:
            c = self.s.recv(total - len(buf))
            if not c:
                raise ConnectionError("响应不完整")
            buf += c
        return buf

    def read(self, addr: int, qty: int, retry: int = 3, pause: float = 0.2):
        """返回 (寄存器列表, None) 或 (None, 错误串)。

        pause: 失败后重连前的等待秒数。scan 逐地址探测时把它调小，
               否则 150 个不可读地址 × 3 次重试 × 0.2s 会白等三分钟。
        """
        last = "unknown"
        for _ in range(retry):
            try:
                self.tid = (self.tid + 1) & 0xFFFF
                pdu = struct.pack(">BHH", 3, addr, qty)
                frame = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.uid) + pdu
                self.s.sendall(frame)
                data = self._recv()
                if len(data) < 9:
                    last = "响应过短"
                    raise ConnectionError(last)
                fc = data[7]
                if fc & 0x80:
                    return None, "Modbus 异常 0x%02x" % data[8]
                bc = data[8]
                return list(struct.unpack(">%dH" % (bc // 2), data[9:9 + bc])), None
            except Exception as e:  # noqa: BLE001
                last = str(e)
                try:
                    self.s.close()
                except Exception:  # noqa: BLE001
                    pass
                if pause:
                    time.sleep(pause)
                try:
                    self._connect()
                except Exception as e2:  # noqa: BLE001
                    last = str(e2)
        return None, last

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:  # noqa: BLE001
            pass


def cmd_windows(c: Client) -> None:
    """哪种 (起始地址, 数量) 组合会被控制器接受 —— 决定读窗口。"""
    print("=" * 78)
    print("地址窗口探测：start 0..63 各读 12 个寄存器")
    print("=" * 78)
    ok = []
    for st in range(0, 64):
        regs, err = c.read(st, 12)
        print("  start=%-3d %s" % (st, regs if regs else err))
        if err is None:
            ok.append(st)
        time.sleep(0.1)
    print()
    print("允许 qty=12 的起始地址:", ok)
    print("★ 经验：单个寄存器读(qty=1)与整块读(qty=12)的**允许集合不同** ——")
    print("  例如 addr=10 qty=1 被拒，但 addr=10 qty=12 正常返回关节角。")
    print("  判断可读性必须用与业务一致的 qty，不能只按单寄存器探测。")


def cmd_dump(c: Client) -> None:
    print("=" * 78)
    print("全量只读 dump  addr 0..%d" % MAXADDR)
    print("=" * 78)
    vals, bad = {}, []
    for a in range(0, MAXADDR + 1):
        regs, err = c.read(a, 1)
        if err is None:
            vals[a] = regs[0]
        else:
            bad.append(a)
    print("可读 %d 个 / 不可读 %d 个" % (len(vals), len(bad)))
    print()
    print("--- 非零寄存器 ---")
    for a, v in sorted(vals.items()):
        if v:
            print("  addr=%-4d %-8d (0x%04X)" % (a, v, v))
    print()
    print("--- 每 12 个一行（*值 = 非零，X = 不可读，. = 零） ---")
    for base in range(0, MAXADDR + 1, 12):
        cells = []
        any_r = False
        for i in range(12):
            a = base + i
            if a in vals:
                any_r = True
                cells.append(("*%d" % vals[a]) if vals[a] else ".")
            else:
                cells.append("X")
        if any_r:
            print("  %-4d  %s" % (base, "  ".join(cells)))


def cmd_joints(c: Client) -> None:
    """控制器暴露了**两路**关节角读数，交叉核对。"""
    print("=" * 78)
    print("两路关节角读数交叉核对")
    print("=" * 78)
    fl, e1 = c.read(10, 12)
    if e1:
        print("  float 区 (addr 10..21) 读取失败:", e1)
    else:
        deg = [_w2f(fl[i * 2], fl[i * 2 + 1]) for i in range(6)]
        print("  float32 @10..21 : %s" % ", ".join("%.3f" % v for v in deg))
    it, e2 = c.read(138, 6)
    if e2:
        print("  int16  区 (addr 138..143) 读取失败:", e2)
    else:
        deg2 = [_i16(v) / 100.0 for v in it]
        print("  int16×100 @138..143 : %s" % ", ".join("%.3f" % v for v in deg2))
        print("  裸寄存器: %s" % it)
    if not e1 and not e2:
        d = max(abs(a - b) for a, b in zip(deg, deg2))
        print("  两路最大偏差: %.4f 度  → %s" % (d, "一致" if d < 0.02 else "不一致，需排查"))
    print()
    print("★ 这两路是**不同表示**的同一组关节角：")
    print("  · 采集器(services/collector.py)用的是 float32 那路 —— 不要动它。")
    print("  · int16×100 那路单位是 0.01°，可作为「是否读到真机」的第二证据。")


def _host_tag(host: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", host)


def _survey_path(host: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "logs", "survey_%s.json" % _host_tag(host))


def _runs(addrs, max_qty=120):
    """把地址列表压成连续段 [(start, qty), ...]，每段不超过 max_qty（Modbus 上限 125）。"""
    out = []
    for a in sorted(set(addrs)):
        if out and a == out[-1][0] + out[-1][1] and out[-1][1] < max_qty:
            out[-1][1] += 1
        else:
            out.append([a, 1])
    return [(s, q) for s, q in out]


def cmd_scan(c: Client, maxaddr: int = MAXADDR) -> None:
    """陌生控制器第一步：它到底允许读哪些地址。结果落盘给 watch 复用。"""
    print("=" * 78)
    print("可读性扫描  %s:%d uid=%d  addr 0..%d" % (c.host, c.port, c.uid, maxaddr))
    print("=" * 78)
    single, block = [], []
    for a in range(maxaddr + 1):
        # 逐地址探测：一次失败就判不可读，不重试、几乎不等待 —— 否则陌生控制器
        # 上 150 个不可读地址会把勘察变成三分钟干等。
        if c.read(a, 1, retry=1, pause=0.02)[0]:
            single.append(a)
    # 整块可读性单独探：只按 4 的步进抽样，够判断"能不能一次读 12 个"
    for a in range(0, maxaddr + 1, 4):
        if c.read(a, 12, retry=1, pause=0.02)[0]:
            block.append(a)
    print("单寄存器可读 %d 个 / 整块(qty=12)可读起始地址 %d 个" % (len(single), len(block)))
    if not single:
        print()
        print("★ 一个都读不到 —— 这台**很可能不说 Modbus TCP**。")
        print("  先跑 scripts/port_scan.py 看它还开了什么口（4840=OPC UA / 80=网页 / 厂商私有）。")
        print("  不要凭猜继续，猜寄存器地址等于盲连别人的控制器。")
        return
    print()
    print("--- 连续段（可直接当读窗口用）---")
    for s, q in _runs(single):
        print("  start=%-4d qty=%-4d  (手册 4xxxx = %d)" % (s, q, s + 1))
    out = _survey_path(c.host)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"host": c.host, "port": c.port, "uid": c.uid,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "readable_single": single, "readable_block12": block}, f)
    print()
    print("已存:", os.path.abspath(out))
    print("下一步: python scripts/reg_survey.py --host %s watch 300 --from-file auto" % c.host)


def _angle_guess(snap: dict, addr: int) -> str:
    """把变化的地址往"关节角"上靠：相邻两寄存器拼 float32，量级像角度才报。"""
    hits = []
    for pair in ((addr, addr + 1), (addr - 1, addr)):
        lo_hi = [snap.get(pair[0]), snap.get(pair[1])]
        if None in lo_hi:
            continue
        for low, high in ((lo_hi[0], lo_hi[1]), (lo_hi[1], lo_hi[0])):
            v = _w2f(low, high)
            if v == v and abs(v) > 1e-6 and abs(v) <= 360.0:
                hits.append("@%d..%d=%+.3f" % (pair[0], pair[1], v))
    i = snap.get(addr)
    if i is not None and 0 < abs(_i16(i)) <= 36000:
        hits.append("int16x100=%+.3f" % (_i16(i) / 100.0))
    return "  |  " + "  ".join(dict.fromkeys(hits)) if hits else ""


def cmd_watch(c: Client, dur: float = 120.0, addrs=None) -> None:
    """旁路监听：只报"哪个寄存器从什么变成了什么"。"""
    if addrs is None:
        windows = [(0, 12), (34, 12), (100, 12), (128, 12), (132, 12),
                   (300, 12), (312, 12), (324, 12), (336, 12), (348, 12), (360, 12)]
    else:
        windows = _runs(addrs)
    print("=" * 78)
    print("只读变化监听 %.0f 秒（%d 个读窗口）—— 请在示教器上操作，本工具只记录变化"
          % (dur, len(windows)))
    print("=" * 78)

    def snap():
        m = {}
        for st, q in windows:
            r, _ = c.read(st, q)
            if r:
                m.update({st + i: v for i, v in enumerate(r)})
        return m

    t0 = time.time()
    prev = snap()
    if not prev:
        print("★ 一个寄存器都读不到，监听没有意义。先跑 scan 确认这台说 Modbus。")
        return
    print("基线非零:", [(a, prev[a]) for a in sorted(prev) if prev[a]])
    print("提示: 接下来**一次只动一个轴**，记下每段变化对应的轴号。")
    while time.time() - t0 < dur:
        time.sleep(0.6)
        cur = snap()
        diff = [a for a in sorted(set(list(cur) + list(prev)))
                if prev.get(a) != cur.get(a)]
        if diff:
            print("[t=%7.2fs] %s" % (time.time() - t0, "  ".join(
                "addr%d: %s->%s%s" % (a, prev.get(a), cur.get(a), _angle_guess(cur, a))
                for a in diff)))
        prev = cur
    print("监听结束。")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="控制器 Modbus 寄存器只读勘察（绝不写入）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", nargs="?", default="dump",
                    choices=["windows", "dump", "joints", "watch", "scan"])
    ap.add_argument("dur", nargs="?", default="120", help="watch 秒数")
    ap.add_argument("--host", default=HOST, help="控制器 IP（默认 %(default)s）")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--unit", type=int, default=UID, help="Modbus unit id")
    ap.add_argument("--max-addr", type=int, default=MAXADDR, help="scan/dump 扫描上限")
    ap.add_argument("--from-file", default=None,
                    help="watch 只盯这些地址；auto = 用 scan 落盘的 survey_<host>.json")
    a = ap.parse_args()

    addrs = None
    if a.from_file:
        p = _survey_path(a.host) if a.from_file == "auto" else a.from_file
        if not os.path.exists(p):
            print("找不到 %s —— 先跑 scan 生成可读地址表。" % os.path.abspath(p))
            return
        addrs = json.load(open(p, encoding="utf-8"))["readable_single"]
        print("watch 地址表来自 %s（%d 个可读地址）" % (p, len(addrs)))

    c = Client(a.host, a.port, a.unit)
    try:
        if a.what == "windows":
            cmd_windows(c)
        elif a.what == "dump":
            cmd_dump(c)
        elif a.what == "joints":
            cmd_joints(c)
        elif a.what == "scan":
            cmd_scan(c, a.max_addr)
        elif a.what == "watch":
            cmd_watch(c, float(a.dur), addrs)
    finally:
        c.close()


if __name__ == "__main__":
    main()
