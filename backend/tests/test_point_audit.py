# -*- coding: utf-8 -*-
"""D2 点位数值体检的**口径**钉（离线纯函数 + 只读接口）。

体检这种东西最容易变成"看着像事实的仪表"，所以钉的全是边界：
- **±327 可表示**必须是 bad（2026-10-11 现场事故：J6 录成 −360 → 下发被拒 → 流程终止）；
- 数据坏 / 笛卡尔点必须**如实说"体检不了"**，不许塌成 green（§5.5 铁律③）；
- 奇异度只是**相对指标**，算不出来时是 `measured=false`，不是"不奇异"；
- 整条链路**只读**：不写点位库、不下发运动。
"""
from __future__ import annotations

import inspect
import io
import json
import tokenize

from app.services import point_audit as PA
from app.services.limits import ENCODE_LIMIT_DEG, load_limits


def _code_tokens(fn_or_src) -> str:
    """把源码压成**只剩可执行 token** 的串（丢掉 docstring / 注释 / 字符串字面量）。

    ★ 注释里写"与 modbus.encode_angle 同口径但不调用它"不该把静态钉自己判红
      （同 `test_safety_sweep_ssm.py` 的处理）。
    """
    src = fn_or_src if isinstance(fn_or_src, str) else inspect.getsource(fn_or_src)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return "".join(out)


def _audit(joints, kind="joint", limits=None):
    return PA.audit_point("测试点", joints, limits=limits, kind=kind)


# ---------------------------------------------------------------- ① ±327 可表示

def test_j6_360_point_is_flagged_unencodable():
    """★ 方案点名的那条钉：把 −360 事故变成**体检项**（而不是等下发时才炸）。"""
    r = _audit([0, 0, 0, 0, 0, -360.0])
    assert r["grade"] == PA.BAD, r
    assert [d["axis"] for d in r["unencodable"]] == ["J6"]
    assert r["unencodable"][0]["encodable_limit_deg"] == ENCODE_LIMIT_DEG
    assert "下发必被拒绝" in r["reasons"][0]
    # 只**建议**重录，不改值、不"顺手折成 0"
    assert r["joints"][5] == -360.0
    assert "重录" in r["advice"]


def test_just_inside_encode_range_is_not_flagged():
    """327.0 以内是**合法**目标（反证钉：不许把体检做成"越紧越好"）。"""
    r = _audit([0, 0, 0, 0, 0, 300.0])
    assert r["unencodable"] == []
    assert r["grade"] in (PA.GOOD, PA.WARN), r


# ---------------------------------------------------------------- ② 限位裕度

def test_tight_margin_is_warn_not_ok():
    """贴着限位录的点 = warn（现场一点漂移就顶到限位）。"""
    limits = load_limits()
    hi = limits[0]["max"]
    r = _audit([hi - 1.0, 0, 0, 0, 0, 0], limits=limits)
    assert r["grade"] == PA.WARN, r
    assert r["tight_axes"] == ["J1"]
    assert r["worst_margin_deg"] is not None and r["worst_margin_deg"] < PA.MARGIN_WARN_DEG
    assert any("离限位不足" in s for s in r["reasons"])


def test_beyond_soft_limit_is_bad_with_numbers_not_vibes():
    limits = [{"name": f"J{i}", "min": -100.0, "max": 100.0} for i in range(1, 7)]
    r = _audit([140.0, 0, 0, 0, 0, 0], limits=limits)
    assert r["grade"] == PA.BAD
    v = r["violations"][0]
    assert v["axis"] == "J1" and v["over_by_deg"] == 40.0 and v["max"] == 100.0


def test_limits_come_from_the_single_source_not_a_local_table():
    """★ 限位必须取自 `limits.load_limits()`（唯一事实来源，已收窄到 ±327）。

    体检自己再写一份表 = 又制造一次"预演说通过、下发被拒"的分叉（P1-E1 那个坑）。
    """
    src = inspect.getsource(PA)
    assert "load_limits()" in src and "bounds_of(" in src
    for banned in ("joint_limits", "get_config("):     # 不自己读 yaml
        assert banned not in src, f"体检里出现了自己的限位来源：{banned}"


# ---------------------------------------------------------------- ③ 奇异（相对指标）

def test_wrist_singular_reads_as_near_but_stays_a_relative_signal():
    """J4 与 J6 共线（J5≈0）是经典腕部奇异 → near=True，且 note 讲清只是相对指标。"""
    r = _audit([0, -30, 30, 0, 0, 0])
    s = r["singularity"]
    assert s["measured"] is True
    assert s["cond"] is not None and s["cond"] > PA.COND_WARN
    assert s["near"] is True
    assert "横向比较" in s["note"] and "不是严格奇异判据" in s["note"]


def test_singularity_never_fakes_not_singular_when_math_unavailable():
    """★ 算不出来 → measured=false + near=None（不许回 "不奇异"）。"""
    import app.services.kinematics as K
    orig = K.jacobian_space
    try:
        K.jacobian_space = lambda q: (_ for _ in ()).throw(RuntimeError("运动学不可用"))
        s = PA._singularity([0, 0, 0, 0, 0, 0])
    finally:
        K.jacobian_space = orig
    assert s["measured"] is False and s["cond"] is None and s["near"] is None
    assert "算不出来" in s["note"]


# ---------------------------------------------------------------- 坏数据不许冒充好数据

def test_nan_short_and_non_numeric_are_unknown_not_green():
    for bad in ([0, float("nan"), 0, 0, 0, 0], [0, 0, 0, 0, 0], None, ["a"] * 6):
        r = _audit(bad)
        assert r["grade"] == PA.UNKNOWN, bad
        assert r["evaluable"] is False, bad
        assert any("无法体检" in s for s in r["reasons"]), bad


def test_cartesian_point_says_not_applicable_instead_of_green():
    """笛卡尔点的执行目标是 TCP，库里那串关节值不是下发姿态 → 体检**不适用**。"""
    r = _audit([0, 0, 0, 0, 0, 0], kind="cartesian")
    assert r["applicable"] is False and r["grade"] == PA.UNKNOWN
    assert r["evaluable"] is True                       # 数据本身是好的
    assert any("不适用" in s for s in r["reasons"])


def test_tcp_is_null_when_forward_kinematics_cannot_answer():
    import app.services.kinematics as K
    orig = K.tcp_of
    try:
        K.tcp_of = lambda q: (_ for _ in ()).throw(RuntimeError("DH 配置坏了"))
        r = _audit([0, 0, 0, 0, 0, 0])
    finally:
        K.tcp_of = orig
    assert r["tcp"] is None                             # null，不是 [0,0,0]


# ---------------------------------------------------------------- 批量与排序

def test_audit_rows_sorts_most_incorrect_first_and_counts_grades():
    rows = [
        {"name": "好点", "joints": json.dumps([0, 0, 0, 0, 30, 0]), "kind": "joint"},
        {"name": "负360", "joints": json.dumps([0, 0, 0, 0, 0, -360]), "kind": "joint"},
        {"name": "坏数据", "joints": "[1,2,3]", "kind": "joint"},
        {"name": "贴限位", "joints": json.dumps([179.9, 0, 0, 0, 30, 0]), "kind": "joint"},
    ]
    d = PA.audit_rows(rows)
    assert d["count"] == 4
    assert [p["name"] for p in d["points"]][0] == "负360"          # bad 最先
    assert d["by_grade"][PA.BAD] >= 1 and d["by_grade"][PA.UNKNOWN] == 1
    assert d["by_grade"][PA.GOOD] + d["by_grade"][PA.WARN] \
        + d["by_grade"][PA.BAD] + d["by_grade"][PA.UNKNOWN] == 4


def test_audit_declares_what_it_does_not_check():
    """★ 诚实边界：绿灯不等于"这活能干"——三样不在判定范围，必须自己说出来。"""
    d = PA.audit_rows([])
    assert len(d["not_checked"]) >= 3
    joined = "".join(d["not_checked"])
    assert "摆得对不对" in joined and "能不能到" in joined and "撞围栏" in joined
    assert "不写库、不下发、不改点" in d["note"]


# ---------------------------------------------------------------- 只读红线

def test_audit_is_read_only_no_motion_no_db_writes():
    """源码静态钉：整条体检链路不碰运动下发、不写点位库（只看可执行 token）。"""
    from app.api import points as api_points

    for name, src in (("point_audit", _code_tokens(PA)),
                      ("api/points 的体检入口",
                       _code_tokens(inspect.getsource(api_points.api_points_audit)))):
        for banned in ("motion.command", "modbus", "40103", "40101", "40135",
                       "update_point", "create_point", "delete_point", "commit"):
            assert banned not in src, f"{name} 里出现了写/下发相关的调用：{banned}"


# ---------------------------------------------------------------- 接口层

def test_audit_endpoint_is_read_only_and_leaves_the_library_unchanged(client):
    """接口层：只读（不需要控制令牌，与 `/api/points` 列表同口径）+ 调用前后库一致。"""
    from app.db.crud import list_points
    from app.db.database import SessionLocal

    s = SessionLocal()
    try:
        before = [(r.id, r.name, r.joints, r.kind) for r in list_points(s)]
    finally:
        s.close()

    r = client.get("/api/points/audit")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == len(before)
    assert set(d["by_grade"]) == {PA.GOOD, PA.WARN, PA.BAD, PA.UNKNOWN}
    assert all(p["name"] for p in d["points"])

    s = SessionLocal()
    try:
        after = [(x.id, x.name, x.joints, x.kind) for x in list_points(s)]
    finally:
        s.close()
    assert after == before, "体检接口改动了点位库（它只许读）"


def test_audit_endpoint_lists_the_real_library_with_encode_limit(client):
    r = client.get("/api/points/audit")
    d = r.json()
    assert d["encode_limit_deg"] == ENCODE_LIMIT_DEG
    assert d["margin_warn_deg"] == PA.MARGIN_WARN_DEG
    # 每个点都带 grade，且"未知"必须带原因（不许静默）
    for p in d["points"]:
        assert p["grade"] in (PA.GOOD, PA.WARN, PA.BAD, PA.UNKNOWN)
        if p["grade"] == PA.UNKNOWN:
            assert p["reasons"], "未知却不写原因 = 又做成静默失效"


def test_audit_route_is_registered_before_the_id_route():
    """★ 路由顺序钉：`/points/audit` 必须排在 `/points/{pid}` **之前**。

    顺序写反了 FastAPI 会拿 pid="audit" 去匹配 `{pid}` → 422，
    于是这个体检接口**永远不会被命中**（而前端只会看到一个转圈的加载）。
    """
    from app.api.points import router

    paths = [r.path for r in router.routes]
    assert "/api/points/audit" in paths, paths
    assert paths.index("/api/points/audit") < paths.index("/api/points/{pid}"), paths
