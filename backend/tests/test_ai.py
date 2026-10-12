# -*- coding: utf-8 -*-
"""AI 执行页（后端）回归：把"大模型会编"的防线与执行闸门钉死。

为什么必须有这些用例：
  · 大模型输出的动作/点位**必须先过白名单**再谈执行 —— 它编一个 "action": "run_program"
    或者 "slot": "A99"，绝不能走到运动通道；
  · 「解析」与「执行」必须是同一份计划（plan_id 绑定），否则界面显示的和真正跑的可能不是一回事；
  · 批量搬运超过上限、把多件放到同一个具体格（会叠件）都必须**当场拒绝**；
  · AI 不是权限后门：所有接口都要控制令牌。

运行： cd backend && .venv/Scripts/python.exe -m pytest tests/test_ai.py -q
"""
from __future__ import annotations

import pytest

from app.services import llm


# ---------------------------------------------------------------- A. 白名单
def test_inventory_answer_from_record(monkeypatch):
    """★ 2026-10-10 用户口径："A区几个红/几个蓝/几个无效颜色" —— 只读回答。

    铁律：**只答"记录过什么"**；没盘过就返回 None（调用方引导去「盘点」），绝不编数量。
    """
    from app.api import ai as ai_api

    monkeypatch.setitem(ai_api._inv, "at", 0.0)      # 从没盘过
    monkeypatch.setitem(ai_api._inv, "A", {})
    assert ai_api.inv_answer("A区几个红") is None

    ai_api._inv.update({"at": 1.0, "A": {"A1": "红", "A2": "红", "A3": "蓝", "A4": ""}})
    s = ai_api._inv_summary(ai_api._inv["A"])
    assert s == {"total": 4, "counts": {"红": 2, "绿": 0, "蓝": 1}, "unknown": 1}
    assert "红色 2 件" in ai_api.inv_answer("A区几个红")
    assert "蓝色 1 件" in ai_api.inv_answer("A区几个蓝")
    assert "无效（判不出色）1 件" in ai_api.inv_answer("A区几个无效颜色")
    assert "红、蓝" in ai_api.inv_answer("A区有哪些颜色")
    assert "共 **4 件**" in ai_api.inv_answer("A区有几个盒子")


def test_sanitize_accepts_goto_start():
    """★ 2026-10-10：大模型也可以给出「回起点」这一动作（白名单内，无 from/to）。"""
    r = llm.sanitize({"action": "goto_start"})
    assert r["ok"] is True and r["plan"] == {"action": "goto_start"}, r


def test_llm_sanitize_rejects_unknown_action():
    """编出来的动作必须被丢弃（不是"忽略字段继续执行"）。"""
    r = llm.sanitize({"action": "run_program", "code": "MJOINT(90)"})
    assert r["ok"] is False and "白名单" in r["error"]


def test_llm_sanitize_rejects_invented_slot():
    """编出来的格号（A99 / Z1 / 空）必须被拒 —— 否则会去动一个不存在的点。"""
    for bad in ("A99", "Z1", "A", "11", "a"):
        r = llm.sanitize({"action": "move_one", "from": {"zone": "A", "slot": bad},
                          "to": {"zone": "B", "slot": "B1"}})
        assert r["ok"] is False, "非法格号 %s 居然通过了" % bad


def test_llm_sanitize_accepts_valid_move():
    r = llm.sanitize({"action": "move_one",
                      "from": {"zone": "A", "slot": "A1"},
                      "to": {"zone": "B", "color": "红"}})
    assert r["ok"] is True
    assert r["plan"]["from"]["kind"] == "slot" and r["plan"]["to"]["kind"] == "color_col"
    assert r["source"] == "llm"


def test_llm_sanitize_sorting_op_whitelist():
    """流程操作只认 start/stop/pause/resume；编一个 "reboot" 必须被拒。"""
    assert llm.sanitize({"action": "sorting", "op": "stop"})["ok"] is True
    bad = llm.sanitize({"action": "sorting", "op": "reboot"})
    assert bad["ok"] is False and "白名单" in bad["error"]


def test_llm_sanitize_needs_both_ends_for_move():
    """缺起点或目标 → 拒绝（不猜"大概是 A1 吧"）。"""
    assert llm.sanitize({"action": "move_one", "to": {"zone": "B", "color": "红"}})["ok"] is False
    assert llm.sanitize({"action": "move_one", "from": {"zone": "A", "slot": "A1"}})["ok"] is False


def test_llm_disabled_without_key(monkeypatch):
    """没配 key 时必须**明确不可用**（而不是抛异常或假装成功）。"""
    monkeypatch.delenv("EFORT_LLM_API_KEY", raising=False)
    assert llm.enabled() is False
    r = llm.parse("把A1放到B1")
    assert r["ok"] is False and "未配置大模型" in r["error"]


def test_llm_lenient_json_parsing():
    """模型偶尔会包 markdown 代码块 / 加前后缀 —— 必须能剥出来。"""
    assert llm._loads_lenient('```json\n{"action":"estop"}\n```') == {"action": "estop"}
    assert llm._loads_lenient('好的：{"action":"sorting","op":"stop"} 完毕') == {
        "action": "sorting", "op": "stop"}
    assert llm._loads_lenient("完全不是 JSON") is None


def test_llm_thinking_off_for_deepseek(monkeypatch):
    """★ 2026-10-10 接 DeepSeek 实测踩到的坑，钉死它。

    DeepSeek-V4 **默认开思考模式**：思维链走 `reasoning_content`，正文 `content`
    可能是**空串**（实测 max_tokens 小时直接是 ""），而且思考模式下 `temperature` 不生效。
    本服务 parse/chat 读的都是 `content` → 不关思考就等于"模型没回话"。
    """
    monkeypatch.setenv("EFORT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("EFORT_LLM_THINKING", raising=False)
    assert llm.thinking_param() == {"type": "disabled"}      # 默认关
    monkeypatch.setenv("EFORT_LLM_THINKING", "1")
    assert llm.thinking_param() == {"type": "enabled"}       # 想开也留了口子
    # 非 deepseek 端点：**不带**这个参数（别的端点不认它，可能直接 400）
    monkeypatch.setenv("EFORT_LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.delenv("EFORT_LLM_THINKING", raising=False)
    assert llm.thinking_param() is None


def test_llm_only_reasoning_content_must_be_reported():
    """只回思维链（content 空）→ 必须**明确报出来**，不能静默变成"空回复"。"""
    got = llm._pick_content({"choices": [{"message": {"content": "",
                                                     "reasoning_content": "思考…"}}]})
    assert got["ok"] is False and "思维链" in got["error"]
    assert llm._pick_content({"choices": [{"message": {"content": " 你好 "}}]}) == {
        "ok": True, "content": "你好"}


def test_llm_prompt_lists_every_allowed_action():
    """★ 白名单里的动作必须在 SYSTEM_PROMPT 里说明 —— 否则模型根本不知道能输出它。

    2026-10-10 实测：提示词里漏了 `goto_start`，DeepSeek 对「回到起点」直接回
    {"action":"none"} → 功能等于不存在（规则解析认得，但大模型兜底那条路断了）。
    """
    for act in llm.ALLOWED_ACTIONS:
        if act == "estop":
            continue                     # 急停由系统关键词处理，提示词里写明了"不必输出"
        assert act in llm.SYSTEM_PROMPT, "SYSTEM_PROMPT 漏了动作：" + act


# ---------------------------------------------------------------- B. 解析闸门
from app.api import ai as AI  # noqa: E402


def test_batch_max_from_env(monkeypatch):
    monkeypatch.setenv("EFORT_AI_BATCH_MAX", "5")
    assert AI.batch_max() == 5
    monkeypatch.setenv("EFORT_AI_BATCH_MAX", "999")     # 越界夹到 9
    assert AI.batch_max() == 9
    monkeypatch.setenv("EFORT_AI_BATCH_MAX", "垃圾")     # 坏值回默认 3
    assert AI.batch_max() == 3


def test_occ_default_all_a_slots_have_parts():
    """占位表默认口径：A 区视为满（现场默认就是满的），B 区为空。"""
    AI._occ["A"].clear()
    AI._occ["B"].clear()
    assert AI._occupied(["A1", "A2", "A3"]) == ["A1", "A2", "A3"]
    assert AI._free_a_slots() == []
    assert AI._free_b_slots("红") == ["B1", "B4", "B7"]
    # 记录一件后：该 A 格变空、该 B 格变占
    AI._occ["A"]["A1"] = False
    AI._occ["B"]["B1"] = "红"
    assert "A1" not in AI._occupied(["A1", "A2"])
    assert AI._free_b_slots("红") == ["B4", "B7"]
    AI._occ["A"].clear()
    AI._occ["B"].clear()


def test_occ_color_column_full_is_reported():
    """某颜色列占满 → 明确报"已满"，不静默换列、不越界。"""
    AI._occ["B"].update({"B1": "红", "B4": "红", "B7": "红"})
    r = AI._resolve_loc({"kind": "color_col", "color": "红", "zone": "B"})
    assert r["ok"] is False and "已满" in r["error"]
    AI._occ["B"].clear()


def test_resolve_src_rejects_color_column():
    """取料点不能按颜色列取（颜色是判出来才知道的）—— 必须拒绝并说明。"""
    r = AI._resolve_src({"kind": "color_col", "color": "红", "zone": "B"})
    assert r["ok"] is False and "颜色" in r["error"]


def test_resolve_loc_rejects_unknown_point_name():
    """点位名必须落在 38 点白名单内（编出来的名字直接拒）。"""
    r = AI._resolve_loc({"kind": "slot", "slot": "A99", "zone": "A"})
    assert r["ok"] is False and "不在分拣点位表" in r["error"]
    assert AI._resolve_loc({"kind": "slot", "slot": "A1", "zone": "A"})["ok"] is True


def test_echo_explains_color_column_target():
    """回显必须说清"放到哪"（颜色列 → 第一个空位），不能显示 None —— 那是确认对象。"""
    e = AI._echo("move_one", {"action": "move_one",
                              "from": {"kind": "slot", "slot": "A1"},
                              "to": {"kind": "color_col", "color": "红", "zone": "B"}})
    assert "A区第1行第1列" in e["detail"] and "红列" in e["detail"]
    assert "None" not in e["detail"]


def test_echo_move_quota_is_described():
    """★ 2026-10-10 现场事故：按颜色配额搬运时 `_echo` 漏了这条分支 → 计划卡只显示一个 "?"，
    操作员看不到"AI 要做什么"、也像"没回复"。回显必须有真实标题 + 说清配额与去向。"""
    e = AI._echo("move_quota", {"action": "move_quota",
                                "from": {"kind": "all", "zone": "A"},
                                "to": {"kind": "zone", "zone": "B"},
                                "quotas": {"蓝": 3, "绿": 2, "红": 1}})
    assert e["title"] not in ("", "?")
    assert "蓝×3" in e["detail"] and "绿×2" in e["detail"] and "红×1" in e["detail"]
    assert "B区" in e["detail"]


def test_echo_never_placeholder_for_supported_actions():
    """★ 兜底守卫：每个**白名单动作**都必须有真实回显，不许掉进末尾的 "?" 兜底。
    （这正是这次现场事故的根因 —— 加了新动作却忘了加回显分支。）"""
    for act in ("estop", "goto_start", "inventory", "move_one", "move_all",
                "move_quota", "look_color", "sorting", "query_state"):
        plan = {"action": act, "op": "resume"} if act == "sorting" else {"action": act}
        title = AI._echo(act, plan)["title"]
        assert title not in ("", "?"), "动作 %s 没有回显文案" % act


def test_ai_pick_keeps_suction_until_place():
    """★ 2026-10-10 现场事故：AI 这条链路"抬起来就立刻停止吸"，件只靠杯内残余负压吊着
    飞一路（现场表现："一吸住直接停止掉了"）。正确工艺（与分拣引擎同口径）：

        取料·吸 → 等 SUCK_HOLD_MS 吸牢 → 抬起 → 送检（**全程保持吸气**）
        → 放料·正上方 → ① 停止吸 ② 放气 ③ 回起点后停止放气

    引擎要真机才能整跑，这里用源码位置把**顺序**钉死。
    """
    import inspect

    src = inspect.getsource(AI._piece)
    # ★ 锚"真实调用/真实语句"，不锚普通词 —— 上面那段注释里也有这些词，
    #   按词 index 会指到注释上，得出假位置（分拣那边的回归钉踩过同一个坑）。
    i_suck = src.index('_air(ctx, "suck"')
    i_hold = src.index("if SUCK_HOLD_MS > 0")
    i_lift = src.index('"抬起"')
    i_at = src.index("放料·正上方")
    i_rel = src.index('_air(ctx, "release"')
    i_blow = src.index('_air(ctx, "blow"')
    assert i_suck < i_hold < i_lift, "吸牢后再抬的停留丢了"
    assert i_lift < i_at < i_rel < i_blow, (
        "吸气必须保持到放料位才断（顺序：抬起 < 放料 < 停止吸 < 放气）"
        "，实际 %s" % [i_suck, i_hold, i_lift, i_at, i_rel, i_blow])


def test_ai_run_releases_air_on_error():
    """★ 异常/取消时必须收掉两路气（不许带着负压停在半路）—— 与分拣同口径。"""
    import inspect

    src = inspect.getsource(AI._run)
    assert 'if _job.get("suck_on")' in src and "异常·停止吸" in src
    assert 'if _job.get("blow_on")' in src and "异常·停止放气" in src


def test_ai_suck_hold_matches_sorting_default():
    """吸取后停留与**分拣+C 位停留**同口径（2026-10-10 用户口径：各 0.5 秒）。"""
    from app.api.sorting import StartIn

    assert AI.SUCK_HOLD_MS == StartIn().suck_hold_ms == 500
    assert AI.CAM_DWELL_S == StartIn().dwell_ms / 1000.0 == 0.5


def test_echo_move_quota_shows_placement():
    """★ 回显必须写出**摆法**（同色成行/成列，约束求解出来的）——
    用户要能一眼看见"摆哪几格"，而不是只给一个执行按钮。"""
    plan = {"action": "move_quota", "to": {"kind": "zone", "zone": "B"},
            "quotas": {"蓝": 3, "红": 1}, "pattern": "row_or_col", "on_invalid": "continue",
            "placement": {"蓝": ["B1", "B4", "B7"], "红": ["B2"]},
            "placement_note": "蓝→B1·B4·B7（第1列）｜红→B2（第1行）",
            "ban": {"from": [], "to": ["B5"], "have": ["B5"]}}
    e = AI._echo("move_quota", plan)
    assert "摆法" in e["detail"] and "B1" in e["detail"]
    assert "已有料" in e["note"] and "B5" in e["note"]
    assert "继续" in e["note"]          # on_invalid=continue 必须如实写出来


def test_parse_solves_placement_and_rejects_unsat():
    """★ 摆法在**解析阶段**解出来（与执行同一份）；解不出就**当场拒绝**。"""
    AI._occ["A"].clear()
    AI._occ["B"].clear()
    r = AI._parse_to_plan("B5,B9已有摆放物体，从A区拿三个蓝，两个绿，一个红放到B区，"
                          "按同颜色一行或者一列摆放")
    assert r["ok"] is True, r
    slots = sum((r["plan"]["placement"] or {}).values(), [])
    assert "B5" not in slots and "B9" not in slots, slots
    assert r["plan"]["placement"]["蓝"] == ["B1", "B4", "B7"], r["plan"]["placement"]
    # 摆不下（4 件同色 > 一条线 3 格）→ 解析阶段就拒绝，并说清原因
    bad = AI._parse_to_plan("从A区拿四个蓝放到B区，按同颜色一行或者一列摆放")
    assert bad["ok"] is False and "摆不下" in bad["error"], bad


def test_ai_quota_uses_solved_placement_and_continues():
    """★ 源码钉：配额按**解出的摆法**落位（不再写死颜色列）；
    无效色（黑/判不出）→ **放回原格 + 继续**（不消耗配额、不整轮停）。"""
    import inspect

    run_src = inspect.getsource(AI._run)
    assert "plan_slots=slots_by_color" in run_src
    assert "continue_on_invalid=continue_inv" in run_src
    assert '"placement"' in run_src
    piece_src = inspect.getsource(AI._piece)
    assert "plan_slots" in piece_src and "无效色·已放回，继续" in piece_src
    assert "已放回原位" in piece_src          # 旧口径也要先放回再停


def test_llm_sanitize_accepts_placement_constraints():
    """★ 大模型可以翻"摆放方式 / 无效色之后怎么办"这两条**约束**（不含数量）；
    乱值只丢弃该字段，不因此拒绝整条指令。"""
    r = llm.sanitize({"action": "move_all", "from": {"zone": "A"}, "to": {"zone": "B"},
                      "pattern": "col", "on_invalid": "continue"})
    assert r["ok"] is True
    assert r["plan"]["pattern"] == "col" and r["plan"]["on_invalid"] == "continue"
    # 乱值只丢弃该字段，不因此拒绝整条指令
    r2 = llm.sanitize({"action": "move_all", "from": {"zone": "A"}, "to": {"zone": "B"},
                       "pattern": "乱说", "on_invalid": "乱说"})
    assert r2["ok"] is True and "pattern" not in r2["plan"] and "on_invalid" not in r2["plan"]


# ---------------------------------------------------------------- 大模型优先（2026-10-10）
def test_llm_can_parse_quota_and_adjacent():
    """★ 用户口径（Q2/Q3）：白名单覆盖**全部 9 个意图**、允许给格号；配额/相邻都认。"""
    r = llm.sanitize({"action": "move_quota", "to": {"zone": "B"},
                      "quotas": {"蓝": 3, "绿": 2, "红": 1},
                      "pattern": "adjacent", "on_invalid": "continue",
                      "ban": {"to": ["B5"], "have": ["B5"]}})
    assert r["ok"] is True, r
    assert r["plan"]["quotas"] == {"蓝": 3, "绿": 2, "红": 1}
    assert r["plan"]["pattern"] == "adjacent" and r["plan"]["on_invalid"] == "continue"
    assert r["plan"]["ban"]["have"] == ["B5"]
    # 非法配额（颜色三色之外 / 0 件）被逐个丢弃
    bad = llm.sanitize({"action": "move_quota", "to": {"zone": "B"},
                        "quotas": {"黑": 2, "红": 0, "蓝": 1}})
    assert bad["ok"] is True and bad["plan"]["quotas"] == {"蓝": 1}, bad
    for a in ("move_quota", "inventory", "query_state"):
        assert a in llm.ALLOWED_ACTIONS
    assert {"adjacent", "adjacent_row", "adjacent_col"} <= set(llm.ALLOWED_PATTERNS)


def test_parse_llm_first_uses_llm_when_enabled(monkeypatch):
    """★ 用户口径（Q2）：**每句话先走大模型**（规则不再优先）。"""
    from app.api import ai as ai_api
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "parse", lambda text, timeout=None: {
        "ok": True, "plan": {"action": "goto_start"}, "error": ""})
    ai_api._occ["A"].clear()
    ai_api._occ["B"].clear()
    r = ai_api._parse_to_plan("把东西收一收")
    assert r["ok"] is True and r["source"] == "llm"
    assert r["plan"]["action"] == "goto_start"


def test_parse_falls_back_to_rules_with_visible_label(monkeypatch):
    """★ 用户口径（Q7）：大模型不可用 → 降级到规则，但**必须标注**"这是系统规则"。"""
    from app.api import ai as ai_api
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "parse", lambda text, timeout=None: {
        "ok": False, "error": "大模型返回 HTTP 422"})
    ai_api._occ["A"].clear()
    ai_api._occ["B"].clear()
    r = ai_api._parse_to_plan("让机器人回到起点")
    assert r["ok"] is True and r["source"] == "rule-degraded", r
    assert any("系统解析规则" in w and "不是" in w for w in r["warnings"]), r["warnings"]


def test_estop_is_safety_fast_path_never_waits_for_llm(monkeypatch):
    """★ 唯一例外的"安全直达"：急停**绝不**绕大模型（网络慢/断不能拖住急停）。"""
    from app.api import ai as ai_api
    calls = {"n": 0}

    def _boom(text, timeout=None):
        calls["n"] += 1
        return {"ok": True, "plan": {"action": "goto_start"}, "error": ""}

    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "parse", _boom)
    r = ai_api._parse_to_plan("急停")
    assert r["ok"] is True and r["plan"]["action"] == "estop"
    assert r["source"] == "safety" and calls["n"] == 0, (r["source"], calls)


def test_feasibility_gate_refuses_stacking_and_suggests(monkeypatch):
    """★ Q1①：目标格**已有料/禁放** → 拒绝（不许叠放），并给建议。"""
    from app.api import ai as ai_api
    monkeypatch.delenv("EFORT_LLM_API_KEY", raising=False)      # 走规则
    ai_api._occ["A"].clear()
    ai_api._occ["B"].clear()
    ai_api._occ["B"]["B1"] = "红"                               # B1 已有料
    r = ai_api._parse_to_plan("把A区第1行第1列放到B1")
    assert r["ok"] is False and "不能往上放" in r["error"], r
    assert r["suggestions"], r
    ai_api._occ["B"].clear()


def test_unsat_quota_returns_suggestions(monkeypatch):
    """★ Q1②：做不到 → 不执行 + **给建议**（换摆放方式/减件）。"""
    from app.api import ai as ai_api
    monkeypatch.delenv("EFORT_LLM_API_KEY", raising=False)
    ai_api._occ["A"].clear()
    ai_api._occ["B"].clear()
    r = ai_api._parse_to_plan("从A区拿四个蓝放到B区，按同颜色一行或者一列摆放")
    assert r["ok"] is False and "摆不下" in r["error"], r
    assert r["suggestions"], r


def test_plan_cache_ttl_binding():
    """plan_id → 计划 的绑定存在（防"确认的和解析的不是同一条"）。"""
    import time as _t
    pid = "t" * 16
    AI._plans[pid] = {"plan": {"action": "estop"}, "at": _t.time(), "raw": "急停"}
    assert AI._plans[pid]["plan"]["action"] == "estop"
    # 过期清理逻辑（>180s 的计划会在下一次 parse/exec 时被清掉）
    AI._plans["old" * 6] = {"plan": {}, "at": _t.time() - 9999, "raw": ""}
    with AI._gate:
        for k in [k for k, v in AI._plans.items() if _t.time() - v["at"] > AI._PLAN_TTL]:
            AI._plans.pop(k, None)
    assert "old" * 6 not in AI._plans
    AI._plans.pop(pid, None)


@pytest.mark.parametrize("path,need", [
    ("/parse", "POST"), ("/exec", "POST"), ("/state", "GET"),
    ("/cancel", "POST"), ("/config", "GET"), ("/occ/reset", "POST"),
])
def test_all_ai_endpoints_require_control_token(path, need):
    """**AI 不是权限后门**：每个接口都必须挂 require_control（缺一个就是绕过令牌的口子）。"""
    found = None
    for r in AI.router.routes:
        if getattr(r, "path", "") == "/api/ai" + path:
            found = r
            break
    assert found is not None, "路由 %s 不存在" % path
    deps = [d.call.__name__ for d in found.dependant.dependencies] if found.dependant else []
    # 依赖可能嵌套一层（Depends(require_control) 直接挂载的情况）
    names = []
    stack = [found.dependant]
    while stack:
        d = stack.pop()
        for sub in (getattr(d, "dependencies", None) or []):
            names.append(getattr(sub.call, "__name__", ""))
            stack.append(sub)
    assert "require_control" in names, "%s 没有挂 require_control（依赖=%s）" % (path, names)


# ---------------------------------------------------------------- 按颜色配额搬运
def test_parse_quota_basic():
    """★ 用户口径（2026-10-09）："全部要学会解析" —— "3个红色，2个蓝色，1个绿色"。
    A 区颜色事先未知，所以解析只出**配额**，执行时逐件判色再决定放还是放回。
    """
    from app.services import nl_intent as NL

    r = NL.parse("A区的3个红色，2个蓝色，1个绿色搬到B区")
    assert r["ok"] is True, r.get("error")
    assert r["intent"] == "move_quota"
    assert r["plan"]["quotas"] == {"红": 3, "蓝": 2, "绿": 1}
    assert r["plan"]["action"] == "move_quota"


def test_parse_quota_variants():
    """中文数字、省略量词、多个"和"连接都要认得。"""
    from app.services import nl_intent as NL

    assert NL.parse("把A区3个红色搬到B区")["plan"]["quotas"] == {"红": 3}
    assert NL.parse("把A区两个绿色和1个蓝色搬到B区")["plan"]["quotas"] == {"绿": 2, "蓝": 1}
    # 同色说两次 → 累加
    assert NL.parse("把A区2个红色和1个红色搬到B区")["plan"]["quotas"] == {"红": 3}


def test_parse_quota_does_not_break_existing_intents():
    """★ 反证：新增规则**不能**把原有的整区搬运/单件搬运抢走。"""
    from app.services import nl_intent as NL

    assert NL.parse("把A区全部搬到B区")["intent"] == "move_all"
    assert NL.parse("把A区所有的件搬到B区")["intent"] == "move_all"
    assert NL.parse("把A区第1行第1列放到B区红列")["intent"] == "move_one"
    assert NL.parse("A区第1行第1列是什么颜色")["intent"] == "look_color"


def test_parse_quota_rejects_specific_slot():
    """配额搬运不能指定"某一个具体格" —— 颜色要判色后才知道，指格是说不通的。"""
    from app.services import nl_intent as NL

    r = NL.parse("把A区2个红色搬到B区第1行第1列")
    assert r["ok"] is False and "颜色列" in r["error"]


def test_quota_carries_honest_warning():
    """★ 必须提前说清代价：A 区颜色未知 → 会逐件判色，不要的件放回原位。"""
    from app.services import nl_intent as NL

    w = " ".join(NL.parse("A区的3个红色，2个蓝色，1个绿色搬到B区")["warnings"])
    assert "放回原位" in w and "未知" in w


def test_llm_owns_quota_action_now():
    """★ 口径变更（2026-10-10 用户拍板 Q2/Q3）：大模型**可以**给配额/格号（大模型优先）。

    防线从"根本不让模型碰数量"改成"**白名单 + 可行性闸门 + 执行前件数确认**"：
      · 配额颜色只能 红/绿/蓝、1..9 整数（越界/非法**逐个丢弃**）；
      · 解析阶段就把摆法解出来，解不出 → **不执行 + 给建议**；
      · 界面回显件数并要求确认后才执行（见前端 AiControlView）。
    """
    assert "move_quota" in llm.ALLOWED_ACTIONS
    assert "move_quota" in __import__("app.services.nl_intent", fromlist=["x"]).INTENTS
    # 数量越界（99）/ 颜色非法（黑）→ 丢弃，绝不静默变成别的件数
    r = llm.sanitize({"action": "move_quota", "to": {"zone": "B"},
                      "quotas": {"红": 99, "黑": 2, "蓝": 2}})
    assert r["ok"] is True and r["plan"]["quotas"] == {"蓝": 2}, r


def test_piece_quota_puts_back_when_not_needed():
    """★★ 执行侧的语义：**不需要的颜色 / 没判出颜色 → 放回原位**（用户口径）。

    用源码位置断言（真机跑不了）：配额分支里必须同时存在两条出口 ——
    `quotas[color] = want - 1`（放走）与 `tgt_slot, mode = src, "back"`（放回）。
    """
    import inspect

    from app.api import ai as A

    src = inspect.getsource(A._piece)
    assert "quotas is not None" in src
    assert "quotas[color] = want - 1" in src, "配额足够时要放走并扣减"
    assert 'tgt_slot, mode = src, "back"' in src, "不需要时要放回原位"
    # 放不进去要把配额还回去，否则会凭空少一件
    assert "quotas[color] = want" in src


def test_run_quota_reports_shortfall_honestly():
    """★ 凑不齐必须如实报（不许假装完成）——否则人会以为"3个红色都搬完了"。"""
    import inspect

    from app.api import ai as A

    src = inspect.getsource(A._run)
    assert 'act == "move_quota"' in src
    assert "未凑齐" in src
    # 上限 = A 区可取的件数（某颜色不够时不会无限试下去）
    assert "for s in srcs:" in src


# ================================================================================
# ★★ 2026-10-11（方案 §C）「快速完成」→ 双面：回显如实标注 + 透传 dual_face
#   全部离线/接口级（conftest 已关真实下发），不连真机、不真下发。
# ================================================================================
def test_echo_marks_normal_and_fast_sorting():
    """★ 计划卡回显**如实标注** 本次=正常（一面）/ 快速（双面·一趟两件）。"""
    e1 = AI._echo("sorting", {"action": "sorting", "op": "start"})
    assert "本次 = 正常" in e1["detail"] and "一面" in e1["detail"]
    e2 = AI._echo("sorting", {"action": "sorting", "op": "start", "dual_face": True})
    assert "本次 = 快速" in e2["detail"] and "双面" in e2["detail"]
    # 停止/暂停不受影响（仍逐字回显软停文案）
    assert "停止" in AI._echo("sorting", {"action": "sorting", "op": "stop"})["detail"]


def test_llm_sanitize_passes_dual_face_boolean_only():
    """★★ 大模型给的 dual_face：**只认布尔 True**；缺失 / 字符串 / 非 start → 一律忽略（回落一面）。"""
    p = llm.sanitize({"action": "sorting", "op": "start", "dual_face": True})["plan"]
    assert p.get("dual_face") is True
    assert "dual_face" not in llm.sanitize({"action": "sorting", "op": "start"})["plan"]
    assert "dual_face" not in llm.sanitize(
        {"action": "sorting", "op": "start", "dual_face": "true"})["plan"]
    assert "dual_face" not in llm.sanitize(
        {"action": "sorting", "op": "stop", "dual_face": True})["plan"]


def _login(client):
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


def _exec_sorting(client, headers, pid, plan, dry_run=True, speed_pct=5):
    import time as _t
    AI._plans[pid] = {"plan": plan, "at": _t.time(), "raw": "分拣"}
    try:
        return client.post("/api/ai/exec",
                           json={"plan_id": pid, "confirm": True,
                                 "dry_run": dry_run, "speed_pct": speed_pct},
                           headers=headers)
    finally:
        AI._plans.pop(pid, None)


def test_ai_exec_start_honors_dry_run_and_speed(client, monkeypatch):
    """★★ 2026-10-11 修复（"参数写死 → 开关失效 + 界面编故事"这一类）：
      （a）`/ai/exec` 的「开始分拣」必须**尊重调用方的 `dry_run`** ——
          改前写死 `StartIn(dry_run=True)`，于是 AI 页那颗「空跑」开关**完全不生效**
          （机器人永远不动），而前端日志却按本地开关打印"真实下发"（界面与实际相反）；
      （b）必须把**右上角全局速度**（`speed_pct`）一起传下去 —— 改前根本没传，
          落到 `StartIn` 的兜底 5%（"速度不跟滑块"）。
    """
    from app.api import sorting as SORT
    cap = {}
    monkeypatch.setattr(SORT, "api_start",
                        lambda body, tok=None: (cap.update(dry=body.dry_run,
                                                          sp=body.speed_pct,
                                                          dual=body.dual_face)
                                                or {"ok": True, "state": {}}))
    h = _login(client)
    # ① 不勾「空跑」= 真实下发 + 速度跟滑块（37%）
    r = _exec_sorting(client, h, "unit-real-" + "c" * 8,
                      {"action": "sorting", "op": "start"},
                      dry_run=False, speed_pct=37)
    assert r.status_code == 200, r.text
    assert cap.get("dry") is False, "dry_run 没传下去（AI 的「空跑」开关失效）"
    assert cap.get("sp") == 37, "speed_pct 没跟右上角滑块传下去"
    assert r.json().get("dry_run") is False, "回包要如实带回**实际**口径（供前端日志不再编故事）"
    assert "真实下发" in (r.json().get("message") or ""), r.json()
    # ② 勾上「空跑」→ api_start 收到 dry_run=True，回包文案也如实
    r2 = _exec_sorting(client, h, "unit-dry-" + "d" * 8,
                       {"action": "sorting", "op": "start"},
                       dry_run=True, speed_pct=5)
    assert cap.get("dry") is True
    assert "空跑" in (r2.json().get("message") or ""), r2.json()


def test_ai_exec_start_forwards_dual_face(client, monkeypatch):
    """★★ `/ai/exec` 把 plan 里的 `dual_face` **真正传下去**（改前恒 False）。"""
    from app.api import sorting as SORT
    cap = {}
    monkeypatch.setattr(SORT, "api_start",
                        lambda body, tok=None: (cap.update(dual_face=body.dual_face)
                                                or {"ok": True, "state": {}}))
    h = _login(client)
    r = _exec_sorting(client, h, "unit-dual-" + "a" * 8,
                      {"action": "sorting", "op": "start", "dual_face": True})
    assert r.status_code == 200, r.text
    assert cap.get("dual_face") is True, "dual_face 没传下去"
    assert r.json().get("dual_face") is True


def test_ai_exec_start_defaults_one_face(client, monkeypatch):
    """★ 没提双面 → `api_start` 收到 dual_face=False（默认一面，零回归）。"""
    from app.api import sorting as SORT
    cap = {}
    monkeypatch.setattr(SORT, "api_start",
                        lambda body, tok=None: (cap.update(dual_face=body.dual_face)
                                                or {"ok": True, "state": {}}))
    h = _login(client)
    r = _exec_sorting(client, h, "unit-one-" + "b" * 8,
                      {"action": "sorting", "op": "start"})
    assert r.status_code == 200, r.text
    assert cap.get("dual_face") is False


# ---------------------------------------------------------------- 按颜色配额搬运
def test_parse_quota_basic():
    """★ 用户口径（2026-10-09）："全部要学会解析" —— "3个红色，2个蓝色，1个绿色"。
    A 区颜色事先未知，所以解析只出**配额**，执行时逐件判色再决定放还是放回。
    """
    from app.services import nl_intent as NL

    r = NL.parse("A区的3个红色，2个蓝色，1个绿色搬到B区")
    assert r["ok"] is True, r.get("error")
    assert r["intent"] == "move_quota"
    assert r["plan"]["quotas"] == {"红": 3, "蓝": 2, "绿": 1}
    assert r["plan"]["action"] == "move_quota"


def test_parse_quota_variants():
    """中文数字、省略量词、多个"和"连接都要认得。"""
    from app.services import nl_intent as NL

    assert NL.parse("把A区3个红色搬到B区")["plan"]["quotas"] == {"红": 3}
    assert NL.parse("把A区两个绿色和1个蓝色搬到B区")["plan"]["quotas"] == {"绿": 2, "蓝": 1}
    # 同色说两次 → 累加
    assert NL.parse("把A区2个红色和1个红色搬到B区")["plan"]["quotas"] == {"红": 3}


def test_parse_quota_does_not_break_existing_intents():
    """★ 反证：新增规则**不能**把原有的整区搬运/单件搬运抢走。"""
    from app.services import nl_intent as NL

    assert NL.parse("把A区全部搬到B区")["intent"] == "move_all"
    assert NL.parse("把A区所有的件搬到B区")["intent"] == "move_all"
    assert NL.parse("把A区第1行第1列放到B区红列")["intent"] == "move_one"
    assert NL.parse("A区第1行第1列是什么颜色")["intent"] == "look_color"


def test_parse_quota_rejects_specific_slot():
    """配额搬运不能指定"某一个具体格" —— 颜色要判色后才知道，指格是说不通的。"""
    from app.services import nl_intent as NL

    r = NL.parse("把A区2个红色搬到B区第1行第1列")
    assert r["ok"] is False and "颜色列" in r["error"]


def test_quota_carries_honest_warning():
    """★ 必须提前说清代价：A 区颜色未知 → 会逐件判色，不要的件放回原位。"""
    from app.services import nl_intent as NL

    w = " ".join(NL.parse("A区的3个红色，2个蓝色，1个绿色搬到B区")["warnings"])
    assert "放回原位" in w and "未知" in w


def test_llm_owns_quota_action_now():
    """★ 口径变更（2026-10-10 用户拍板 Q2/Q3）：大模型**可以**给配额/格号（大模型优先）。

    防线从"根本不让模型碰数量"改成"**白名单 + 可行性闸门 + 执行前件数确认**"：
      · 配额颜色只能 红/绿/蓝、1..9 整数（越界/非法**逐个丢弃**）；
      · 解析阶段就把摆法解出来，解不出 → **不执行 + 给建议**；
      · 界面回显件数并要求确认后才执行（见前端 AiControlView）。
    """
    assert "move_quota" in llm.ALLOWED_ACTIONS
    assert "move_quota" in __import__("app.services.nl_intent", fromlist=["x"]).INTENTS
    # 数量越界（99）/ 颜色非法（黑）→ 丢弃，绝不静默变成别的件数
    r = llm.sanitize({"action": "move_quota", "to": {"zone": "B"},
                      "quotas": {"红": 99, "黑": 2, "蓝": 2}})
    assert r["ok"] is True and r["plan"]["quotas"] == {"蓝": 2}, r


def test_piece_quota_puts_back_when_not_needed():
    """★★ 执行侧的语义：**不需要的颜色 / 没判出颜色 → 放回原位**（用户口径）。

    用源码位置断言（真机跑不了）：配额分支里必须同时存在两条出口 ——
    `quotas[color] = want - 1`（放走）与 `tgt_slot, mode = src, "back"`（放回）。
    """
    import inspect

    from app.api import ai as A

    src = inspect.getsource(A._piece)
    assert "quotas is not None" in src
    assert "quotas[color] = want - 1" in src, "配额足够时要放走并扣减"
    assert 'tgt_slot, mode = src, "back"' in src, "不需要时要放回原位"
    # 放不进去要把配额还回去，否则会凭空少一件
    assert "quotas[color] = want" in src


def test_run_quota_reports_shortfall_honestly():
    """★ 凑不齐必须如实报（不许假装完成）——否则人会以为"3个红色都搬完了"。"""
    import inspect

    from app.api import ai as A

    src = inspect.getsource(A._run)
    assert 'act == "move_quota"' in src
    assert "未凑齐" in src
    # 上限 = A 区可取的件数（某颜色不够时不会无限试下去）
    assert "for s in srcs:" in src
