# -*- coding: utf-8 -*-
"""华沿 ELFIN 正运动学（后端版）—— 按官方 URDF 关节链直接算，**不转 DH**。

链式口径来自 config/models/huayan_elfin.yaml（抄自 vendor/elfin_model 的 xacro 原文）：

    T = Π_i [ Translate(xyz_i) · Rz(yaw)Ry(pitch)Rx(roll_i) · Rz(θ_i) ] · End_fixed

rpy 的三个数是 **[roll, pitch, yaw]**（URDF 规范第一个数是 roll），固定角矩阵是
R = Rz(yaw)·Ry(pitch)·Rx(roll)。这条顺序在前后端各被判据钉住：零位末端高度
== 官方 standing_height_mm（前端 tools/verify_armchain.mjs，后端 tests/test_kinematics_elfin.py）。
按 (yaw,pitch,roll) 误读的话 E05 末端会躺到 220mm 高，两边都会立刻红。

为什么后端也要有一份（前端 armChain.js 不是已经在算了）：
  · 机房仿真帧要有 TCP —— 否则卡片那一格恒为"—"，历史曲线也没数据；
  · 机型档案末尾的 self_check 要求"本地 FK vs 控制器 Actual_PCS_Base"比对，
    这是唯一能证明 variant 选对、某轴方向没反的手段（控制器只报角度时 TCP 得自己算）；
  · 两处都读同一个 yaml 的同一个 `variant` 键，所以不会出现"画面一条链、数字另一条链"。

★ 坐标系/单位：URDF 基座系，+Z 竖直向上，xyz 为米，输出毫米（与埃夫特
  services/kinematics.py 的 tcp_of 同一个"Z 朝上 mm"口径，卡片才能混排两牌子）。
★ 只读运动学：没有逆解，不进任何写路径。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

M2MM = 1000.0
N_AXES = 6


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


def _vec(v: Any, n: int = 3) -> List[float]:
    """yaml 里的 xyz/rpy 可能是 None/短列表，一律补 0 而不是抛异常。

    宁可末端差一截（画面看得出来），也不要采集线程因为一个空字段整台掉线。
    """
    out = [0.0] * n
    if isinstance(v, (list, tuple)):
        for i in range(min(n, len(v))):
            try:
                out[i] = float(v[i])
            except (TypeError, ValueError):
                out[i] = 0.0
    return out


def rpy_matrix(rpy: Any) -> np.ndarray:
    """URDF rpy=[roll,pitch,yaw]（rad）→ 3x3 旋转矩阵 Rz(yaw)·Ry(pitch)·Rx(roll)。"""
    r, p, y = _vec(rpy)
    return _rot_z(y) @ _rot_y(p) @ _rot_x(r)


def _mat(xyz: Any, rpy: Any) -> np.ndarray:
    T = np.eye(4)
    x, y, z = _vec(xyz)
    T[:3, 3] = (x, y, z)
    T[:3, :3] = rpy_matrix(rpy)
    return T


def _rot4_z(a: float) -> np.ndarray:
    """绕 +Z 转 a（rad）的 4x4 —— 关节转角那一层，必须是齐次矩阵才能连乘。"""
    T = np.eye(4)
    T[:3, :3] = _rot_z(a)
    return T


def chain_for(model_data: Optional[Dict[str, Any]],
              variant: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """从机型档案里取出算正解要的那份关节链。取不到返回 None（调用方据此不出 TCP）。

    variant 键与 api/fleet.py::_kinematics 发给前端的是同一个 —— 两边都读
    `model_data["variant"]`，改机型只改 yaml 那一行，前后端跟着一起换。
    """
    md = model_data or {}
    var_key = str(variant or md.get("variant") or "")
    var = (md.get("variants") or {}).get(var_key) or {}
    joints = var.get("joints")
    if not isinstance(joints, list) or len(joints) != N_AXES:
        return None
    return {
        "variant": var_key,
        "joints": joints,
        "end_fixed": var.get("end_fixed") or {},
        "standing_height_mm": var.get("standing_height_mm"),
    }


def fk_matrix(chain: Dict[str, Any], joints_deg: Sequence[float]) -> np.ndarray:
    """末端（未装工具的 TCP）在基座系下的 4x4 齐次变换，单位米。"""
    T = np.eye(4)
    q = list(joints_deg) + [0.0] * max(0, N_AXES - len(joints_deg))
    for i, j in enumerate(chain["joints"][:N_AXES]):
        th = math.radians(float(q[i]))
        T = T @ _mat(j.get("xyz"), j.get("rpy")) @ _rot4_z(th)
    end = chain.get("end_fixed") or {}
    if end:
        T = T @ _mat(end.get("xyz"), end.get("rpy"))
    return T


def tcp_of(chain: Dict[str, Any], joints_deg: Sequence[float]) -> Tuple[float, float, float]:
    """关节角(度) → TCP (x, y, z) mm，基座系、+Z 向上。"""
    T = fk_matrix(chain, joints_deg)
    return (float(T[0, 3]) * M2MM, float(T[1, 3]) * M2MM, float(T[2, 3]) * M2MM)
