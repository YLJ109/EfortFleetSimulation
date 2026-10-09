# -*- coding: utf-8 -*-
"""局域网存活 IP 扫描（现场设备清点用）。

用法：
    python tools/scan_lan.py                 # 自动扫本机所在的所有网段
    python tools/scan_lan.py 192.168.1.0/24  # 只扫指定网段（可写多个）

做法（两步，比"只 ping"可靠）：
    ① 并发 ping 一遍 → 让操作系统填充 **ARP 表**（有些设备不回 ping，但会在 ARP 里）；
    ② 读 `arp -a` 拿 **MAC 地址** —— 这是"二层真的存在"的硬证据，
       能区分"对方不回 ping"与"根本没这台机器"。

★ 纯只读：只发 ICMP / 读 ARP，不写任何寄存器、不碰机器人控制器。
"""
from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import re
import subprocess
import sys
import time

PING_TIMEOUT_MS = 400          # 单包等待；现场交换机快，400ms 足够
WORKERS = 128                  # 并发度


def local_networks() -> list:
    """从 ipconfig 解析出本机所在网段（跳过 127.0.0.1）。"""
    out = subprocess.run(["ipconfig"], capture_output=True).stdout.decode("gbk", "replace")
    ips = re.findall(r"IPv4 地址[^\d]*([\d.]+)", out)
    masks = re.findall(r"子网掩码[^\d]*([\d.]+)", out)
    nets = []
    for ip, mask in zip(ips, masks):
        try:
            net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        except Exception:
            continue
        if not net.is_loopback:
            nets.append(net)
    return nets


def ping(ip: str) -> bool:
    r = subprocess.run(["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip],
                       capture_output=True)
    return r.returncode == 0


def arp_table() -> dict:
    """读 ARP 缓存 → {ip: mac}。无效条目（如 00-00-00-00-00-00）跳过。"""
    out = subprocess.run(["arp", "-a"], capture_output=True).stdout.decode("gbk", "replace")
    tbl = {}
    for ip, mac in re.findall(r"([\d.]+)\s+([0-9a-fA-F-]{17})", out):
        if mac.lower().replace("-", "") == "0" * 12:
            continue
        tbl[ip] = mac.lower()
    return tbl


def scan(net: ipaddress.IPv4Network) -> dict:
    hosts = [str(h) for h in net.hosts()]
    alive = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # ping 返回 bool → 用 zip 配上 IP（ex.map 与 hosts 顺序一致）
        for ip, ok in zip(hosts, ex.map(ping, hosts)):
            if ok:
                alive[ip] = True
    return {"net": str(net), "hosts": len(hosts), "alive": sorted(
        alive, key=lambda s: tuple(int(x) for x in s.split("."))), "sec": time.time() - t0}


def main() -> int:
    nets = []
    for a in sys.argv[1:]:
        nets.append(ipaddress.IPv4Network(a, strict=False))
    if not nets:
        nets = local_networks()
    if not nets:
        print("没解析到本机网段")
        return 2

    print("== 局域网存活 IP 扫描 ==")
    print("网段：%s ｜ 并发 %d ｜ 单包超时 %dms\n"
          % ("、".join(str(n) for n in nets), WORKERS, PING_TIMEOUT_MS))

    total_alive = 0
    for net in nets:
        r = scan(net)
        total_alive += len(r["alive"])
        print("【%s】%d 个地址，存活 **%d** 个（耗时 %.1fs）"
              % (r["net"], r["hosts"], len(r["alive"]), r["sec"]))
        for ip in r["alive"]:
            print("   ", ip)
        if not r["alive"]:
            print("    （无响应 —— 对方可能禁 ping；请看下方 ARP 表）")
        print()

    # ARP 表：包含"不回 ping 但二层存在"的设备
    arp = arp_table()
    print("== ARP 缓存（二层可达的设备，含不回 ping 的） ==")
    if not arp:
        print("  （空）")
    for ip, mac in sorted(arp.items(), key=lambda kv: tuple(int(x) for x in kv[0].split("."))):
        print("    %-16s %s" % (ip, mac))

    # 汇总：只统计扫描网段内的
    in_scope = set()
    for net in nets:
        in_scope |= {ip for ip in arp if ipaddress.IPv4Address(ip) in net}
    print()
    print("== 汇总 ==")
    print("  ping 通      : %d 个" % total_alive)
    print("  ARP 表在网段内: %d 个" % len(in_scope))
    both = in_scope | set()
    print("  合起来见过  : %d 个（ping 通 ∪ ARP 有记录）" % len(both))
    return 0


if __name__ == "__main__":
    sys.exit(main())
