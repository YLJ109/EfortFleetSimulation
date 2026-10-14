# -*- coding: utf-8 -*-
"""A2 路径扫掠 + A3 SSM 限速**建议** 的口径钉。

这两条都是"前瞻"，最容易犯的错是**把推断说成事实**，所以钉的全是边界：
- 扫掠**不许**输出 ETA（PC 只写目标+触发位、控制器 210 自己执行，没有时间基准）；
- SSM 在 T/K/C 未现场实测前**必须**返回 `available=false`，且**不许**回写速度。
"""
from __future__ import annotations

import inspect

from app.services import safety_eval as E


# ---------------------------------------------------------------- A2 扫掠

def test_sweep_is_geometry_only_no_eta():
    r = E.sweep([0, 0, 0, 0, 0, 0], [30, 0, 0, 0, 0, 0], n=8)
    assert r["samples"] == 8 and len(r["curve"]) == 8
    assert "eta" not in r and "time_s" not in r and "arrive" not in r
    # 曲线首尾就是传入的两端点姿态（不偷偷插值到别的点）
    assert r["curve"][0]["t"] == 0.0 and r["curve"][-1]["t"] == 1.0
    assert "保守包络" in r["note"] and "无 ETA" in r["note"]


def test_sweep_catches_a_path_that_the_endpoints_alone_miss():
    """★ 钉住 A2 存在的意义：**两端都安全、路上擦到围栏**时扫掠必须变红。

    现场真实形态就是这一类：绕 J1 横过去，途中某一刻整臂正对围栏 z1。
    只看起点/终点的实现（现状的 `/safety/evaluate` 口径）**永远看不见它**。
    """
    cfg = E.load_safety()
    a, b = [-60, -35, 0, 0, 0, 0], [60, -35, 0, 0, 0, 0]
    assert E.evaluate(a, cfg=cfg)["state"] == "safe"
    assert E.evaluate(b, cfg=cfg)["state"] == "safe"
    r = E.sweep(a, b, n=25, cfg=cfg)
    assert r["ok"] is False
    assert r["worst_state"] in ("warn", "danger", "hit")
    assert 0 < r["worst_idx"] < 24                # 最坏点在**路上**，不是端点
    assert r["worst_zone"] == "z1"
    assert r["curve"][r["worst_idx"]]["state"] == r["worst_state"]


def test_sweep_is_no_less_conservative_with_finer_samples():
    """采样加密（13 的点是 25 的子集）只会**更早/更严**报警，不会反过来。

    反过来就说明扫掠结论是采样噪声 —— 那这种东西不能拿去拦动作。
    """
    cfg = E.load_safety()
    a, b = [-60, -35, 0, 0, 0, 0], [60, -35, 0, 0, 0, 0]
    coarse, fine = E.sweep(a, b, n=13, cfg=cfg), E.sweep(a, b, n=25, cfg=cfg)
    assert E.STATE_RANK[fine["worst_state"]] >= E.STATE_RANK[coarse["worst_state"]]
    if fine["worst_state"] == coarse["worst_state"]:
        assert fine["worst_mm"] <= coarse["worst_mm"] + 1e-6


def test_lift_suggest_reports_honest_failure_not_zero():
    """抬不够就 `needed_mm=None` + 原因，绝不回 0 表示"不用抬"（那是假安全）。"""
    r = E.lift_suggest([0, 0, 0, 0, 0, 0], [0, -18, 0, 0, 0, 0], max_mm=10.0)
    if not r["ok"]:
        assert r["needed_mm"] is None
        assert "仍不满足" in r["reason"]


# ---------------------------------------------------------------- A3 SSM

def test_ssm_advice_returns_none_when_brake_time_unmeasured():
    """★ 出厂配置必须"无法给出"：T/K/C 都是 null → available=false + 逐条 unknown。

    这条钉的是**不编数**：网上抄的"典型停机时间 0.3s"一旦进配置，界面就会拿它
    给现场划安全距离 —— 那正是本项目反对的"推断冒充事实"。
    """
    a = E.ssm_advice()
    assert a["available"] is False
    assert a["separation_mm"] is None and a["required_speed_cap_pct"] is None
    assert any("stop_time_s" in u for u in a["unknown"])
    assert any("ssm_k100_mm_s" in u for u in a["unknown"])
    assert any("ssm_c_mm" in u for u in a["unknown"])
    assert "未实测" in a["reason"]
    assert "不替代控制器安全板" in a["note"]


def test_ssm_advice_computes_when_all_measured():
    a = E.ssm_advice(distance_mm=800.0, speed_pct=30.0, cfg={
        "stop_time_s": 0.4, "ssm_k100_mm_s": 500.0, "ssm_c_mm": 50.0})
    assert a["available"] is True and a["unknown"] == []
    # S = K·T + C = (500×0.3)×0.4 + 50 = 110
    assert a["separation_mm"] == 110.0
    assert a["ok"] is True
    assert a["required_speed_cap_pct"] == 100        # 算得出 375%，但滑块上限就是 100
    b = E.ssm_advice(distance_mm=60.0, speed_pct=30.0, cfg={
        "stop_time_s": 0.4, "ssm_k100_mm_s": 500.0, "ssm_c_mm": 50.0})
    assert b["ok"] is False
    assert b["required_speed_cap_pct"] == 5           # (60-50)/0.4/500×100 = 5%


def test_ssm_never_fakes_clearance_when_no_live_evaluation():
    """★ 后端没算过时，分隔距离是"不知道"，不是 0.0mm 这种看着像事实的读数。"""
    from app.services import safety_guard as G
    G.reset_server()
    a = E.ssm_advice(speed_pct=30)
    assert a["inputs"]["distance_mm"] is None
    assert any("分隔距离" in u for u in a["unknown"])


def _code_tokens(fn_or_src) -> str:
    """把源码压成**只剩可执行 token** 的串（丢掉 docstring / 注释 / 字符串字面量）。

    ★ 注释里写"不许 publish"不该把静态钉自己判红（把它算进去 = 钉自己咬自己）。
    """
    import io
    import tokenize
    src = fn_or_src if isinstance(fn_or_src, str) else inspect.getsource(fn_or_src)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return "".join(out)


def test_ssm_never_writes_speed():
    """源码静态钉：A3 整条链路**只读** —— 不 publish 速度、不碰 40103、不发运动指令。"""
    from app.api import safety as api_safety

    for name, src in (("ssm_advice", _code_tokens(E.ssm_advice)),
                      ("api/safety", _code_tokens(inspect.getsource(api_safety)))):
        for banned in ("ui_speed.publish", "40103", "set_speed", "write_register",
                       "motion.command"):
            assert banned not in src, f"{name} 里出现了写速度/下发相关的调用：{banned}"
    # 且确实只用了那个**只读** getter
    assert "ui_speed.peek()" in inspect.getsource(E.ssm_advice)


def test_sweep_endpoint_requires_control_and_returns_null_eta(client):
    """接口层：无令牌 401（前瞻不是免费的状态泄露），有令牌时 `eta` 明确为 null。"""
    body = {"from_joints": [0, 0, 0, 0, 0, 0], "to_joints": [0, -18, 0, 0, 0, 0],
            "samples": 8}
    assert client.post("/api/safety/sweep", json=body).status_code == 401
    tok = client.post("/api/auth/login",
                      json={"password": "test1234"}).json()["token"]
    r = client.post("/api/safety/sweep", json=body,
                    headers={"X-Control-Token": tok})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["eta"] is None and d["samples"] == 8
    assert client.post("/api/safety/sweep",
                       json={**body, "from_joints": [0, 0]},
                       headers={"X-Control-Token": tok}).status_code == 400
    # NaN 走**裸 JSON**（httpx 的 json= 在客户端就抛 ValueError，到不了服务端）：
    # 服务端必须拦住 —— 否则 NaN 会进曲线、进 JSON 响应，前端 JSON.parse 直接崩。
    bad = client.post("/api/safety/sweep",
                      content='{"from_joints":[0,0,0,0,0,0],'
                              '"to_joints":[0,NaN,0,0,0,0],"samples":8}',
                      headers={"X-Control-Token": tok,
                               "Content-Type": "application/json"})
    assert bad.status_code in (400, 422), bad.text


def test_ssm_endpoint_is_read_only_and_says_unavailable(client):
    r = client.get("/api/safety/ssm")
    assert r.status_code == 200                       # 只读诊断：不需要令牌
    d = r.json()
    assert d["available"] is False
    assert d["required_speed_cap_pct"] is None
