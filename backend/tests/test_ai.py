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


def test_llm_does_not_own_quota_action():
    """★ 大模型白名单**故意不含** move_quota（不让模型编数量）。"""
    assert "move_quota" not in llm.ALLOWED_ACTIONS
    assert "move_quota" in __import__("app.services.nl_intent", fromlist=["x"]).INTENTS


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


def test_llm_does_not_own_quota_action():
    """★ 大模型白名单**故意不含** move_quota（不让模型编数量）。"""
    assert "move_quota" not in llm.ALLOWED_ACTIONS
    assert "move_quota" in __import__("app.services.nl_intent", fromlist=["x"]).INTENTS


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
