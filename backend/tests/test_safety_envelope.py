# -*- coding: utf-8 -*-
"""连杆包络对官方数模真值的**保守性**回归钉（方案 A1 的地基）。

为什么单独钉这一条（第六轮实测）：
    手写的一组"看着挺保守"的半径（130/115/95/75/62/50mm）在**零位**算出的 J1 子树
    最低点是 261mm，而官方 GLB 逐顶点量到的真实值是 191.5mm —— 包络比真实本体**更高**，
    于是"转座底缘快贴台面"被服务端说成"还早着呢"。这类错**不会报错、不会变慢**，
    只会让报警比真机晚响，属于本项目最反对的"假安全"。
    所以半径必须由数模真值反解（`tools/fit_safety_links.py`），并由下面这些钉子钉住方向：
    **服务端永远比真值更低/更大（更早报警），而且不能胖到离谱。**

真值来源：`frontend/tools/fixtures/mesh_lowpoints.json`
    生成命令 `cd frontend && node tools/dump_mesh_lowpoints.mjs`（离线、headless、不连真机）
"""
from __future__ import annotations

import json
import math
import os

import pytest

from app.services import safety_eval as E

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "..",
                       "frontend", "tools", "fixtures", "mesh_lowpoints.json")

REGEN = "cd frontend && node tools/dump_mesh_lowpoints.mjs"


def _rows():
    if not os.path.exists(FIXTURE):
        pytest.fail(f"缺少官方数模真值文件：{FIXTURE}\n先跑：{REGEN}")
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)["rows"]


def _env(q, shrink_mm: float = 0.0):
    """服务端包络（lift=0，即"相对基座平面"，与真值同口径）。"""
    caps = E.capsules(list(q), lift=0.0)
    caps = [{**c, "r": c["r"] - shrink_mm / 1000.0} for c in caps]
    above = [c for c in caps if c["from"] >= 1]          # 地面只算 J1 以上（同 evaluate）
    low = min((min(c["p0"][1], c["p1"][1]) - c["r"] for c in above), default=math.inf)
    box = E.aabb_of(caps)
    return low * 1000.0, box


def test_envelope_never_sits_above_the_real_mesh():
    """★ 方向钉：每个采样姿态，服务端包络最低点 ≤ 数模真实最低点（只许更早报警）。"""
    worst = -math.inf
    for row in _rows():
        low, _ = _env(row["q"])
        worst = max(worst, low - row["low_mm"])          # >0 = 包络比真值高 = 假安全
    assert worst <= 1e-6, (
        f"有姿态下服务端包络最低点**高于**数模真值 {worst:.1f}mm —— 报警会比真机晚响。"
        f"重跑反解：{REGEN} 然后 cd backend && python tools/fit_safety_links.py")


def test_envelope_contains_horizontal_footprint():
    """围栏余量吃的是**水平外接**（aabb_of）：包络必须 ≥ 数模外接，否则撞区域会漏报。"""
    worst = -math.inf
    for row in _rows():
        _, box = _env(row["q"])
        truth = list((row["bodies"] or {}).values())
        if not truth:
            continue
        over = max(
            max(b[1] for b in truth) - box["max"]["x"] * 1000.0,      # +X 漏包
            box["min"]["x"] * 1000.0 - min(b[0] for b in truth),      # −X
            max(b[5] for b in truth) - box["max"]["z"] * 1000.0,      # +Z
            box["min"]["z"] * 1000.0 - min(b[4] for b in truth))      # −Z
        worst = max(worst, over)
    assert worst <= 1e-6, f"水平方向漏包 {worst:.1f}mm：围栏余量会比真实本体算得乐观"


def test_envelope_is_tight_not_absurd():
    """保守 ≠ 荒唐：真值与包络的差必须**有界**，否则围栏天天误报、现场只能关掉。"""
    worst = 0.0
    for row in _rows():
        low, _ = _env(row["q"])
        worst = max(worst, row["low_mm"] - low)
    assert worst <= 60.0, f"包络比真值低 {worst:.1f}mm，过于保守会让围栏误报成常态"


def test_shrinking_radii_breaks_the_pin():
    """钉子必须真的会咬人：把所有半径统一改小 10mm，保守性就必须被破坏。

    （没有这条，前两条可能只是"传上去刚好没越界"的巧合，改了配置也不会红。）
    """
    broke = False
    for row in _rows():
        low, _ = _env(row["q"], shrink_mm=10.0)
        if low - row["low_mm"] > 1e-6:
            broke = True
            break
    assert broke, "半径缩 10mm 仍处处保守 ⇒ 包络被估得过肥，标定流程需要重看"


def test_zero_pose_ground_clearance_matches_mesh():
    """零位（机械零点）台面净空：服务端读数应贴着数模真值 191.5mm（转座底缘）。"""
    lift = E.platform_lift_m()
    r = E.evaluate([0, 0, 0, 0, 0, 0], cfg={"enabled": False, "ground": {"enabled": True}},
                   lift=lift)
    assert r["measured"] is True
    mm = r["ground"]["clearance_mm"]
    assert 180.0 <= mm <= 191.6, f"零位净空读数 {mm:.1f}mm 与数模 191.5mm 对不上"


def test_links_config_and_default_are_the_same_numbers():
    """配置与代码兜底必须是**同一套数**（否则"改配置没生效"这种坑会重现）。"""
    from app.core.config import get_config
    raw = get_config().get("safety", "links", default=None) or []
    cfg_links = [{"from_frame": int(item["from_frame"]), "to_frame": int(item["to_frame"]),
                  "r_mm": float(item["r_mm"])} for item in raw]
    assert cfg_links == E.DEFAULT_LINKS, f"robot.yaml 的 links 与 DEFAULT_LINKS 不一致：{cfg_links}"
