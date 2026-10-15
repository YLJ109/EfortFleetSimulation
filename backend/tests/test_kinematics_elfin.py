# -*- coding: utf-8 -*-
"""华沿 URDF 关节链正解（app/services/kinematics_elfin.py）单元测试。

这里钉的是**几何口径**，不是实现细节。判据与前端 tools/verify_armchain.mjs 同源：
零位末端高度必须等于官方 README 的站立高度 —— 这一条同时管住 rpy 顺序、
欧拉乘序、xyz 抄录三件事，任何一件写错都会让末端差出几十到几百毫米。
"""
from __future__ import annotations

import copy

import pytest

from app.core.fleet_config import load_model_config
from app.services.kinematics_elfin import chain_for, fk_matrix, tcp_of

MODEL = "config/models/huayan_elfin.yaml"
ZERO = [0.0] * 6


@pytest.fixture(scope="module")
def md() -> dict:
    return load_model_config(MODEL)


def _swap_rpy(chain: dict) -> dict:
    """把 rpy 当成 (yaw,pitch,roll) 读 —— 用来证明上面的判据真的有灵敏度。"""
    bad = copy.deepcopy(chain)
    for j in bad["joints"]:
        j["rpy"] = [j["rpy"][2], j["rpy"][1], j["rpy"][0]]
    return bad


# ---------------------------------------------------------------------
def test_chain_for_picks_declared_variant(md):
    chain = chain_for(md)
    assert chain is not None
    assert chain["variant"] == md["variant"] == "elfin5"
    assert len(chain["joints"]) == 6
    assert chain["end_fixed"]["xyz"][2] == pytest.approx(0.1565)


def test_chain_for_unknown_is_none(md):
    assert chain_for(None) is None
    assert chain_for({}) is None
    assert chain_for(md, variant="elfin99") is None
    # 轴数不对（少一节）也不能算 —— 静默按 5 轴链出个 TCP 比报错危险
    broken = {"variant": "x", "variants": {"x": {"joints": md["variants"]["elfin5"]["joints"][:5]}}}
    assert chain_for(broken) is None


# ---------------------------------------------------------------------
@pytest.mark.parametrize("key", ["elfin3", "elfin5", "elfin5_l",
                                 "elfin10", "elfin10_l", "elfin15"])
def test_zero_pose_height_matches_official_standing_height(md, key):
    var = md["variants"][key]
    chain = chain_for(md, variant=key)
    x, y, z = tcp_of(chain, ZERO)
    expect = float(var["standing_height_mm"])
    # 零位是"竖直叠臂"：末端必须在基座正上方，所以水平偏移也必须≈0
    assert z == pytest.approx(expect, abs=1.0), f"{key} 零位高度 {z:.1f}mm ≠ 官方 {expect}mm"
    assert (x ** 2 + y ** 2) ** 0.5 < 1.0, f"{key} 零位水平偏移不为 0（xyz/rpy 抄错）"


def test_wrong_rpy_order_breaks_height(md):
    chain = _swap_rpy(chain_for(md, variant="elfin5"))
    _, _, z = tcp_of(chain, ZERO)
    assert abs(z - 1176.5) > 100, "误读 rpy 顺序后仍然对得上站立高度 → 判据失效"


# ---------------------------------------------------------------------
def test_joint_actually_drives_chain(md):
    """只动 J2：末端必须大幅位移，且不跑出基座 X-Z 平面。

    J2 的 rpy=[π/2,0,0] 把转动轴转到基座 -Y（水平），所以摆 J2 = 手臂在
    X-Z 平面内前后倒。若 y 也跟着动了，说明转动层挂到了固定偏置层之前。
    阈值取 0.02mm 而非 0：xacro 原文把 π/2 写成 1.5708（四位小数），
    轴位差 1.8e-5 rad，摆 30° 会残留微米级侧向分量 —— 那是抄录精度，不是接错。
    """
    chain = chain_for(md, variant="elfin5")
    p0 = tcp_of(chain, ZERO)
    p1 = tcp_of(chain, [0.0, 30.0, 0.0, 0.0, 0.0, 0.0])
    moved = sum((a - b) ** 2 for a, b in zip(p0, p1)) ** 0.5
    assert moved > 100
    assert abs(p1[1] - p0[1]) < 0.02
    assert p1[2] < p0[2], "J2 正转应让末端放倒 → 高度必须下降"


def test_missing_or_short_vectors_do_not_raise(md):
    """现场 yaml 少写一个数时：出个偏矮的 TCP，而不是让采集线程整台掉线。"""
    chain = {"joints": [{"xyz": None, "rpy": None}] * 6, "end_fixed": {}}
    assert tcp_of(chain, ZERO) == (0.0, 0.0, 0.0)
    chain2 = {"joints": [{"xyz": [0, 0], "rpy": [1.5708]}] * 6}
    T = fk_matrix(chain2, [10, 10, 10, 10, 10, 10])
    assert T.shape == (4, 4)
