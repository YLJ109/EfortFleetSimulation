# -*- coding: utf-8 -*-
"""单台控制器的**只读**端口侦察。

用途：面对一台没有文档的机器人控制器（例如华沿），先确认它到底开了什么口子，
再决定往下走 Modbus 寄存器勘察（scripts/reg_survey.py）还是找厂商 SDK。

★ 只读：只做 TCP connect、一次 FC3 读、一次 HTTP HEAD。**绝不写入、绝不下发指令**。
★ 只打**一个 IP**。本工具不做网段扫描 —— 现场 12 台机器人的 IP 是逐台填进
  config/fleet.yaml 的，一次只勘察一台，避免误连到不认识的第三方设备。

用法：
    python scripts/port_scan.py 192.168.2.21
    python scripts/port_scan.py 192.168.2.21 --ports 502,4840,8000
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

# 常见工业/服务端口。标注是"这个端口一般是什么"，不是厂商承诺。
COMMON = {
    21: "FTP", 22: "SSH", 23: "Telnet", 80: "HTTP", 443: "HTTPS",
    102: "SAP/ISO", 502: "Modbus TCP（工业标准口）",
    4840: "OPC UA", 1025: "常见厂商私有服务",
    2101: "常见厂商私有服务", 3000: "Web/调试",
    5000: "常见厂商私有服务", 5020: "Modbus TCP 备用口",
    8000: "Web/API", 8080: "Web/API", 9000: "常见厂商私有服务",
}


def _tcp(host: str, port: int, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def _modbus_alive(host: str, port: int, uid: int, timeout: float):
    """发一条 FC3 读（addr=0 qty=1）。能回包 = 这台确实说 Modbus TCP。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        pdu = struct.pack(">BHH", 3, 0, 1)
        s.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, uid) + pdu)
        data = s.recv(9)
        if len(data) < 9:
            return "连上了但回包不完整 —— 可能不是 Modbus，或被防火墙掐了"
        fc = data[7]
        if fc & 0x80:
            return "是 Modbus TCP（回异常码 0x%02X —— 说明协议在，只是该地址不可读）" % data[8]
        if fc == 3:
            val = struct.unpack(">H", data[9:11])[0] if len(data) >= 11 else None
            return "是 Modbus TCP，addr0=%s" % val
        return "回包功能码 0x%02X，不像标准 Modbus" % fc
    except Exception as e:  # noqa: BLE001
        return "读失败: %s" % e
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def _http_hint(host: str, port: int, timeout: float) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(("HEAD / HTTP/1.0\r\n\r\n").encode("ascii"))
        head = s.recv(512).decode("latin1", "replace")
        for line in head.splitlines()[1:]:
            if line.lower().startswith(("server:", "title:")):
                return line.strip()
        return head.splitlines()[0] if head else ""
    except Exception as e:  # noqa: BLE001
        return "连接失败: %s" % e
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="控制器只读端口侦察（单台，不写任何东西）")
    ap.add_argument("host", help="控制器 IP（一次只填一台）")
    ap.add_argument("--ports", default=None, help="逗号分隔，覆盖默认端口表")
    ap.add_argument("--uid", type=int, default=1, help="Modbus unit id")
    ap.add_argument("--timeout", type=float, default=1.2)
    a = ap.parse_args()

    ports = ([int(p) for p in a.ports.split(",")] if a.ports else sorted(COMMON))
    print("=" * 74)
    print("只读端口侦察  %s   %d 个端口   %s" % (a.host, len(ports), time.strftime("%H:%M:%S")))
    print("=" * 74)
    open_ports = []
    for p in ports:
        hit = _tcp(a.host, p, a.timeout)
        print("  %-6s %-3s %s" % (p, "OPEN" if hit else "-", COMMON.get(p, "")))
        if hit:
            open_ports.append(p)
        time.sleep(0.05)

    print()
    if not open_ports:
        print("★ 一个端口都不通。先排查网络本身，不要怀疑协议：")
        print("   · 本机网卡是否在控制器那个网段（192.168.1.x 与 192.168.2.x 是两个网段）")
        print("   · ping %s 通不通" % a.host)
        print("   · 控制器是否上电、网线是否插好")
        return 2

    print("--- 对开放端口做只读指纹识别 ---")
    modbus_ok = False
    for p in open_ports:
        if p in (502, 5020):
            hint = _modbus_alive(a.host, p, a.uid, a.timeout)
            modbus_ok = not hint.startswith("连上了但") and "异常" not in hint[:4]
            print("  %-6s %s" % (p, hint))
        elif p in (80, 443, 8000, 8080):
            print("  %-6s HTTP: %s" % (p, _http_hint(a.host, p, a.timeout)))
        elif p == 4840:
            print("  %-6s 大概率是 OPC UA 服务器 —— 需要 OPC UA 客户端，不走 Modbus" % p)
        else:
            print("  %-6s 未知服务，别乱发数据；去问厂商这个口是什么协议" % p)

    print()
    print("--- 下一步 ---")
    if modbus_ok:
        print("  python scripts/reg_survey.py --host %s --port 502 --unit %d scan"
              % (a.host, a.uid))
        print("  python scripts/reg_survey.py --host %s watch 300 --from-file auto" % a.host)
        print("  （watch 期间一次只动一个轴，看哪个地址在跳）")
    else:
        print("  这台**没有可用的 Modbus**。按顺序确认：")
        print("  1) 上面若有 4840 → 走 OPC UA，需要换采集实现（driver 抽象里加 HuayanOpcuaDriver）")
        print("  2) 有 80/443 → 打开网页看有没有『开放接口/二次开发』入口和手册下载")
        print("  3) 都不像 → 找厂商要 SDK 与协议文档，**不要靠猜寄存器地址继续**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
