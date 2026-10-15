# -*- coding: utf-8 -*-
"""华沿 ELFIN 控制器 **10003 命令端口**（IF 命令服务端口）的 ASCII 写通道。

★ 语法出处：《HuayanRobotV6控制通信协议接口_V1.0.19.1》（本机原文在
  C:/Users/Administrator/.qoder-cn/tmp/huayan_v6_protocol.txt，从桌面
  huayan-ros2-docs 里那份 PDF 抽取而来）。本项目**不反推、不类比 UR/ABB**，
  每个命令名与参数顺序都对着文档章节抄：

  · §2.1 消息格式      `消息名称,Param1,...,Paramn,;`
                        —— 字段间英文逗号，整条以 `",;"` 结束，命令与应答都是 ASCII 字符串，
                        —— "没有用到的参数赋值为 0"
  · §2.2.1 成功应答    `消息名称,OK,Param...,;`
  · §2.2.2 失败应答    `消息名称,Fail,ErrorCode,ErrorExplanation,;`（错误码 int）
  · §4.2.1 GrpEnable  `GrpEnable,nRbtID,;`            例 `GrpEnable,0,;`
  · §4.2.2 GrpDisable `GrpDisable,nRbtID,;`
  · §4.2.3 GrpReset   `GrpReset,nRbtID,;`
  · §4.2.4 GrpStop    `GrpStop,nRbtID,;`
  · §4.5.1 SetOverride`SetOverride,nRbtID,dOverride,;` 例 `SetOverride,0,0.5,;`
                        dOverride 有效范围 **0.01~1**（速度比，不是百分数）
                        且"运动状态不能修改速度比"
  · §4.9.14 ReadActCoord `ReadActCoord,nRbtID,UcsName,TcpName,;`
  · §4.10.5 WayPoint  24 个参数：
                        nRbtID, dX,dY,dZ,dRx,dRy,dRz, dJ1..dJ6, sTcpName, sUcsName,
                        dVelocity, dAcc, dRadius, nMoveType, nIsUseJoint, nIsSeek,
                        nIOBit, nIOState, strCmdID
                        §4.10.5.2 给的三条示例逐字对应：
                          nMoveType=0,nIsUseJoint=0 → 关节运动但用**空间坐标逆解**
                          nMoveType=0,nIsUseJoint=1 → **用 dJ1..dJ6 作为目标**（本项目走这条）
                          nMoveType=1               → 直线运动
                        单位：关节运动 dVelocity[°/s]、dAcc[°/s²]；
                        nIsUseJoint=1 时 sTcpName/sUcsName 无效，可用默认 "TCP"/"Base"。

★ 文档里**没有**的东西（所以不写进代码，也别据此下结论）：
  · `HuayanRobot_ErrorCode.docx` 不在这台机器上 → 只知道示例里的 20018
    (commandforbiddeninthisstate) 与 20007 (commandformaterror)。
    本模块因此**原样回传 code+explanation**，不做任何码表翻译。
  · 一条连接能不能连续发多条命令、应答是否按行/按帧分界、每命令超时多久 —— 都没写。
    所以这里用最保守的做法：**一条命令一次 TCP 连接**（connect→send→read 到 `",;"`
    →close），这是 TCP 本身就有保证的语义，不需要猜控制器的会话行为。
  · **不重试**。运动类命令重发等于可能执行两遍，宁可对操作员报"超时，状态未知"。

★ 这是真机写通道。出厂默认关闭（fleet.yaml: drivers.huayan_datasheet.command_write.enabled=false），
  关掉时 `HuayanDataSheetDriver.joint_move_enabled()` 返回 False，
  services/fleet.py 的 move() 走 501 分支，**一个字节都不会发出去**。
  真机红线：未在现场验证过急停/限位/使能行为之前，不要打开它。
"""
from __future__ import annotations

import math
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import get_logger

log = get_logger("huayan_cmd")

TERMINATOR = ",;"
MAX_REPLY = 64 * 1024

# 文档里逐条抄下来的命令名（§编号见模块头）。白名单之外一律拒发：
# 命令名会原样进 ASCII 流，放开就等于允许调用方往协议里塞任意内容。
COMMANDS = {
    "GrpEnable": 1,        # §4.2.1 nRbtID
    "GrpDisable": 1,       # §4.2.2
    "GrpReset": 1,         # §4.2.3
    "GrpStop": 1,          # §4.2.4
    "SetOverride": 2,      # §4.5.1 nRbtID, dOverride(0.01~1)
    "ReadActCoord": 3,     # §4.9.14 nRbtID, UcsName, TcpName
    "WayPoint": 24,        # §4.10.5
}

# strCmdID 是自由字符串，但它前面没有引号、后面直接是逗号 ——
# 只允许安全字符，否则一条畸形 ID 就能改写整条消息的结构。
_CMD_ID = re.compile(r"^[A-Za-z0-9_\-]{1,24}$")

# WayPoint 参数位次（1 起，与文档顺序一致）
WP = {
    "nRbtID": 1, "dX": 2, "dY": 3, "dZ": 4, "dRx": 5, "dRy": 6, "dRz": 7,
    "dJ1": 8, "dJ2": 9, "dJ3": 10, "dJ4": 11, "dJ5": 12, "dJ6": 13,
    "sTcpName": 14, "sUcsName": 15, "dVelocity": 16, "dAcc": 17, "dRadius": 18,
    "nMoveType": 19, "nIsUseJoint": 20, "nIsSeek": 21, "nIOBit": 22,
    "nIOState": 23, "strCmdID": 24,
}


class ProtocolError(ValueError):
    """消息不符合 §2.1 的 ASCII/逗号约束 —— 发出去会污染整条协议流。"""


def _param(v: Any) -> str:
    """单参数序列化：数字用十进制字符串，其余必须是纯 ASCII 安全串。"""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ProtocolError("参数不是有限数值: %r" % (v,))
        # 文档示例里整数速度写成 "50"、小数速度写成 "0.5" —— 去掉尾随 0 保持同一口径
        s = ("%.4f" % v).rstrip("0").rstrip(".")
        return s if s not in ("", "-", ".0") else "0"
    s = str(v).strip()
    if any(ch in s for ch in (",", ";", "\r", "\n", "\t")):
        raise ProtocolError("参数里不允许出现 , ; 或控制字符: %r" % (v,))
    try:
        s.encode("ascii")
    except UnicodeEncodeError:
        raise ProtocolError("命令与应答都是 ASCII（§2.1），非 ASCII 参数拒发: %r" % (v,)) from None
    return s


def encode(name: str, params: List[Any]) -> bytes:
    """按 §2.1 组一条消息。参数个数必须与该命令的文档定义一致。"""
    if name not in COMMANDS:
        raise ProtocolError("未文档化的命令名: %s（可用：%s）"
                            % (name, ",".join(sorted(COMMANDS))))
    want = COMMANDS[name]
    if len(params) != want:
        raise ProtocolError("%s 需要 %d 个参数（见协议 §4），当前 %d 个"
                            % (name, want, len(params)))
    if name == "WayPoint" and not _CMD_ID.match(str(params[WP["strCmdID"] - 1])):
        raise ProtocolError("strCmdID 只能是字母数字/_/-，长度≤24")
    body = ",".join([name] + [_param(p) for p in params])
    return (body + TERMINATOR).encode("ascii")


def decode(text: str) -> Dict[str, Any]:
    """解析应答：{name, ok, code, explanation, params, raw}。

    失败应答的模板在 §2.2.2 写成 `...,ErrorExplanation;`（没有结尾那个逗号），
    但全书的示例都带 `,;` —— 两种都吃，别在这种地方赌文档。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"name": "", "ok": False, "code": None, "explanation": "空应答",
                "params": [], "raw": raw}
    fields = [f.strip() for f in raw.rstrip(";").rstrip(",").split(",")]
    name = fields[0] if fields else ""
    if len(fields) > 1 and fields[1].upper() == "OK":
        return {"name": name, "ok": True, "code": None, "explanation": "",
                "params": fields[2:], "raw": raw}
    if len(fields) > 1 and fields[1].upper() == "FAIL":
        code = None
        try:
            code = int(fields[2])
        except (IndexError, ValueError, TypeError):
            pass
        expl = fields[3] if len(fields) > 3 else ""
        return {"name": name, "ok": False, "code": code, "explanation": expl,
                "params": fields[4:], "raw": raw}
    # 既不是 OK 也不是 Fail：原样交出去。这里最危险的诱惑是"当作成功"——
    # 控制器在报警时可能回一截别的东西，把它认成成功就等于谎报"已执行"。
    return {"name": name, "ok": False, "code": None,
            "explanation": "应答格式不认识: %s" % raw[:120], "params": fields[1:], "raw": raw}


class TransportError(RuntimeError):
    pass


class HuayanCommandChannel:
    """一条命令一次 TCP 连接（保守口径，见模块头"文档里没有的"）。"""

    def __init__(self, host: str, port: int = 10003, timeout_s: float = 2.0,
                 robot_id: int = 0, enabled: bool = False) -> None:
        self.host = str(host or "")
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self.robot_id = int(robot_id)
        self.enabled = bool(enabled)
        self.last_error: Optional[str] = None

    # ---- 底层 ----
    def send(self, name: str, *params: Any) -> Dict[str, Any]:
        if not self.enabled:
            raise TransportError("写通道未启用（fleet.yaml command_write.enabled=false）")
        if not self.host:
            raise TransportError("没有目标地址")
        payload = encode(name, list(params))
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except OSError as e:
            self.last_error = "连接 10003 失败: %s" % e
            raise TransportError(self.last_error) from e
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(payload)
            reply, err = _read_until_terminator(s)
        finally:
            try:
                s.close()
            except OSError:
                pass
        if err:
            self.last_error = err
            raise TransportError(err)
        self.last_error = None
        r = decode(reply)
        if r["name"] and r["name"] != name:
            # 应答的命令名和自己发的对不上：多半是上一笔的残留或串了连接。
            # 宁可报"无法确认"，也不要拿别人的 OK 当自己这条的执行结果。
            r["ok"] = False
            r["explanation"] = "应答命令名 %s 与发出命令 %s 不一致，执行结果未知" % (
                r["name"], name)
            self.last_error = r["explanation"]
        else:
            self.last_error = None if r["ok"] else (
                "错误码 %s %s" % (r["code"], r["explanation"]))
        r["sent"] = payload.decode("ascii")
        return r

    # ---- 文档化命令的薄封装（不做任何"顺手多发一条"的事）----
    def enable(self) -> Dict[str, Any]:
        return self.send("GrpEnable", self.robot_id)

    def disable(self) -> Dict[str, Any]:
        return self.send("GrpDisable", self.robot_id)

    def reset(self) -> Dict[str, Any]:
        return self.send("GrpReset", self.robot_id)

    def stop(self) -> Dict[str, Any]:
        return self.send("GrpStop", self.robot_id)

    def set_override(self, ratio: float) -> Dict[str, Any]:
        """§4.5.1：dOverride 是 **0.01~1** 的速度比。运动状态下控制器会拒（20018）。"""
        if not 0.01 <= float(ratio) <= 1.0:
            raise ProtocolError("dOverride 必须在 0.01~1（§4.5.1），当前 %r" % (ratio,))
        return self.send("SetOverride", self.robot_id, float(ratio))

    def joint_move(self, joints_deg: List[float], velocity_dps: float = 50.0,
                   acc_dps2: float = 360.0, cmd_id: str = "1",
                   radius_mm: float = 0.0) -> Dict[str, Any]:
        """关节运动：nMoveType=0 + nIsUseJoint=1（§4.10.5.2 "关节运动-使用关节点位"）。

        dX..dRz 与 sTcpName/sUcsName 在该模式下无效 —— 按 §2.1"没有用到的参数赋值为 0"，
        坐标位填 0，坐标系名用文档示例里的默认名 "TCP"/"Base"。
        """
        q = [float(joints_deg[i]) for i in range(6)]
        if any(not math.isfinite(v) for v in q):
            raise ProtocolError("目标关节角必须是有限数值")
        if not 0 < float(velocity_dps) <= 400:
            raise ProtocolError("dVelocity 要给正的 °/s（文档示例 50）")
        p: List[Any] = [0.0] * 24
        p[WP["nRbtID"] - 1] = self.robot_id
        for i in range(6):
            p[WP["dJ%d" % (i + 1)] - 1] = q[i]
        p[WP["sTcpName"] - 1] = "TCP"
        p[WP["sUcsName"] - 1] = "Base"
        p[WP["dVelocity"] - 1] = float(velocity_dps)
        p[WP["dAcc"] - 1] = float(acc_dps2)
        p[WP["dRadius"] - 1] = float(radius_mm)
        p[WP["nMoveType"] - 1] = 0          # 关节运动
        p[WP["nIsUseJoint"] - 1] = 1        # 用 dJ1..dJ6 作为目标
        p[WP["nIsSeek"] - 1] = 0            # 不接 DI 停止检测
        p[WP["nIOBit"] - 1] = 0
        p[WP["nIOState"] - 1] = 0
        p[WP["strCmdID"] - 1] = str(cmd_id)
        return self.send("WayPoint", *p)


def _read_until_terminator(s: socket.socket) -> Tuple[str, str]:
    """读到第一个 `",;"` 为止。返回 (文本, 错误说明)。"""
    buf = b""
    deadline = time.time() + 5.0            # 总耗时上限，超时口径见模块头
    while True:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            return "", "等应答超时（执行结果未知，**不要**盲目重发）"
        except OSError as e:
            return "", "读应答失败: %s" % e
        if not chunk:
            return ("", "连接被控制器关闭，没拿到完整应答") if TERMINATOR not in buf.decode(
                "ascii", errors="replace") else (buf.decode("ascii", errors="replace"), "")
        buf += chunk
        if len(buf) > MAX_REPLY:
            return "", "应答超过 %d 字节，判为异常" % MAX_REPLY
        if TERMINATOR.encode() in buf:
            return buf.decode("ascii", errors="replace"), ""
        if time.time() > deadline:
            return "", "应答不完整（5s 内没等到 ,; 结束符）"


def build_from_config(host: str, cfg: Optional[Dict[str, Any]],
                      robot_id: int = 0) -> HuayanCommandChannel:
    """按 fleet.yaml 的 drivers.huayan_datasheet.command_write 建通道。

    ★ enabled 缺省 False：写通道的开关必须是"显式写出来才生效"，
      不能是"没配就等于默认允许"。
    """
    c = cfg or {}
    return HuayanCommandChannel(
        host=host,
        port=int(c.get("port", 10003)),
        timeout_s=float(c.get("timeout_s", 2.0)),
        robot_id=int(c.get("rbt_id", robot_id)),
        enabled=bool(c.get("enabled", False)),
    )
