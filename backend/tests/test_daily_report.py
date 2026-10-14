# -*- coding: utf-8 -*-
"""★★ E6「机器人自述 = 每天一页现场日报」回归钉（`services/daily_report.py` + 只读端点）。

本功能的命门（方案 §E6 + 任务口径）：**每一个数字都必须能从结构化数据源里追溯到同一值**。
所以钉子分五类：

  ① **可追溯**：每节带 `source`（service / 表 / 端点级），attention 每条带"依据数字"；
  ② **零 LLM**：本轮纯模板版 —— 源码里不许出现 `services/llm` 的 import/调用；
     数字一致性自检 `consistency_check()` 是给"将来接讲解员 LLM"预留的同一颗钉子；
  ③ **不编数字**：往 markdown 注入一个假数值 → 自检必须抓出来；
  ④ **无数据不塌成 0**：空的一天写「无此项数据」，界面拿到的 markdown 里不许出现
     把"没有"说成"0 次"的行；
  ⑤ **只读、零下发、不写事件**：端点不要控制令牌；生成日报不 emit 任何事件、
     不 import motion/modbus 写路径（AGENTS.md §5.1 真机纪律）。

测试隔离：走 conftest 的临时 DB / `EFORT_SIMULATE=always` / 临时 `EFORT_LOGS_DIR`；
内存态数据源（twin_residual / alarms / cell_evidence）用例前后 reset()，防跨用例串味。
"""
from __future__ import annotations

import os
import re
import tempfile
import time

import pytest

from app.services import alarms as AL
from app.services import cell_evidence as CE
from app.services import daily_report as DR
from app.services.twin_residual import twin_residual

SRC_PATH = "app/services/daily_report.py"


def _src() -> str:
    import app.services.daily_report as m
    with open(m.__file__, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _clean_memory_sources():
    """三个进程内数据源必须清 + 独占 logs 目录（否则 attention 会测到历史堆积）。

    · twin_residual / cell_evidence / alarms 都是进程内单例，跨用例不清就会
      "上一例的段出现在这一例的日报里"（§5.5 铁律①"单调水位"的同类事故）；
    · `EFORT_LOGS_DIR` 里 safety_evidence 的取证包是**磁盘持久**的 —— 别的模块
      的取证用例每跑一次就往公共 tmp 里堆一次，我的 attention 排序就会被
      "今天=181 个取证包"这种历史污染顶掉刚注入的 3 次拦截。这里每条用例
      一个独占空目录，让日报只反映本例注入的数据。
    """
    twin_residual.reset()
    CE.reset()
    AL.reset()
    prev_logs = os.environ.get("EFORT_LOGS_DIR")
    tmp_logs = tempfile.mkdtemp(prefix="dr_logs_")
    os.environ["EFORT_LOGS_DIR"] = tmp_logs
    yield
    os.environ.pop("EFORT_LOGS_DIR", None)
    if prev_logs is not None:
        os.environ["EFORT_LOGS_DIR"] = prev_logs
    twin_residual.reset()
    CE.reset()
    AL.reset()


def _seed_residual() -> None:
    """两段观测：一段顺利到位（arrived），一段一帧就结束（not_reached）。

    ★ `_events_enabled=False`：测试里超差不往事件表写（防污染 flows 计数）；
      时间由调用方传入（at）→ 确定性重放，不靠真实时钟。
    """
    twin_residual._events_enabled = False
    at = time.time()
    twin_residual.begin([0.0] * 6, source="B3上", speed_pct=10, at=at)
    twin_residual.observe([3.0, 0, 0, 0, 0, 0], at=at + 0.1)
    twin_residual.observe([0.2, 0, 0, 0, 0, 0], at=at + 1.0)
    twin_residual.observe([0.0] * 6, at=at + 1.5)
    twin_residual.end("done", at=at + 1.6)
    twin_residual.begin([10.0] * 6, source="A1上", at=at + 2.0)
    twin_residual.observe([10.0] * 6, at=at + 2.1)
    twin_residual.end("done", at=at + 2.2)


def _emit(action: str, level: str = "warn", category: str = "control",
          message: str = "测试注入", actor: str = "operator") -> None:
    from app.services.events import emit as emit_event
    emit_event(category, level, action, message, {"from": "test_daily_report"},
               actor=actor, broadcast=False)


# ---------------------------------------------------------------- ① 可追溯
def test_report_numbers_all_traceable_to_structured_sources(client):
    """每个数字旁都有 source：每节必须带非空 `source`，attention 每条带依据与来源。"""
    rep = DR.build_report()
    assert rep["ok"] is True
    assert set(rep["sections"]) == {"flows", "twin", "evidence", "alarms",
                                    "points", "cells", "color"}
    for key, sec in rep["sections"].items():
        assert sec["source"], "节 %s 没有来源标识" % key
        assert "lines" in sec and "fields" in sec
        # 来源要能指到真实存在的东西（service 文件 / db 表 / 可注入参数），不许写"大概"
        assert re.search(r"(services/|db:|build_report\()", sec["source"]), \
            "节 %s 的来源不可核对：%r" % (key, sec["source"])
    for it in rep["attention"]["items"]:
        assert it["source"] and it["basis"]
        assert str(it["value"]) in it["basis"]      # "依据数字"就是那个数本身
    # 出稿前自检必须真跑过，且正常报告应为空
    assert rep["consistency"]["count"] == 0
    assert rep["consistency"]["inconsistent"] == []


def test_session_state_is_declared_not_pretended_history(client):
    """内存态（twin/alarms/cells）必须注明「本机本次会话」，不许当持久历史。"""
    rep = DR.build_report()
    for key in ("twin", "alarms", "cells"):
        sec = rep["sections"][key]
        assert "本机本次会话" in (sec["source"] + sec.get("note", "") + "".join(sec["lines"]))
    assert "本机本次会话" in rep["markdown"]


def test_timezone_note_is_explicit(client):
    rep = DR.build_report()
    assert "本地日期" in rep["timezone"] and "UTC" in rep["timezone"]
    assert rep["window"]["utc_from"] and rep["window"]["utc_to"]


# ---------------------------------------------------------------- ② 零 LLM / 零下发
def test_report_has_no_llm_dependency_and_survives_without_it(client):
    """不调 services/llm.py；静态钉：源码里不出现 llm 的 import/调用。"""
    src = _src()
    code = re.sub(r'"""[\s\S]*?"""', "", re.sub(r"^\s*#.*$", "", src, flags=re.M))
    for pat in (r"from\s+app\.services\s+import\s+.*\bllm\b",
                r"from\s+app\.services\.llm",
                r"import\s+app\.services\.llm",
                r"\bllm\s*\.",
                r"\bask_llm\b|\bllm_complete\b|\bchat_completion\b"):
        assert not re.search(pat, code), "日报里出现了 LLM 依赖: %s" % pat
    # LLM 模块即使坏/不可用也不影响出报（本模块根本不看它）
    rep = DR.build_report(date="2000-01-01")
    assert rep["ok"] is True and rep["markdown"]


def test_service_never_touches_write_paths_or_emits_events(client, monkeypatch):
    """静态：不 import motion/modbus 写路径、不调 emit；
    动态：**spy 挂到 `insert_system_event` 上**，日报生成同线程调用数必须为 0。

    ★ 为什么不用"事件表前后计数"：`client` 夹具启动 lifespan → 采集器后台线程持续
      往事件表写（vision.detect 每秒好几条）；前后计数变成在测竞态。
    ★ 为什么 spy 要**按主线程过滤**：后台线程也会走 insert —— 那不是我这条调用链的锅。
      用 `threading.get_ident()` 只数主线程（也就是 build_report 的调用线程）的落库。
    """
    import threading
    src = _src()
    code = re.sub(r'"""[\s\S]*?"""', "", re.sub(r"^\s*#.*$", "", src, flags=re.M))
    for pat in (r"from\s+app\.services\.motion", r"import\s+motion",
                r"modbus", r"emit_event|events\s+import\s+emit",
                r"vision_samples|color_advice"):
        assert not re.search(pat, code), "日报出现了禁止的依赖/动作: %s" % pat
    from app.db import crud as crud_mod
    from app.services import events as events_mod
    main_tid = threading.get_ident()
    calls = {"insert": 0, "emit": 0}
    real_insert = crud_mod.insert_system_event
    real_emit = events_mod.emit

    def _spy_insert(*a, **kw):
        if threading.get_ident() == main_tid:
            calls["insert"] += 1
        return real_insert(*a, **kw)

    def _spy_emit(*a, **kw):
        if threading.get_ident() == main_tid:
            calls["emit"] += 1
        return real_emit(*a, **kw)

    monkeypatch.setattr(crud_mod, "insert_system_event", _spy_insert)
    monkeypatch.setattr(events_mod, "emit", _spy_emit)
    DR.build_report()
    assert calls["insert"] == 0, "生成日报期间（同线程）往事件表写了 %d 条 —— 观测件不该是控制动作" \
        % calls["insert"]
    assert calls["emit"] == 0, "生成日报期间（同线程）调用了 events.emit %d 次" % calls["emit"]


# ---------------------------------------------------------------- ③ 一致性自检
def test_consistency_check_flags_fabricated_number(client):
    """注入一个结构化源里不存在的数值 → 必须作为不一致项返回（不许静默通过）。"""
    rep = DR.build_report(date="2000-01-01")
    tainted = rep["markdown"] + "\n今天机器人一共跑了 8760.5 次，最险峰值 412.9°\n"
    bad = DR.consistency_check(rep, tainted)
    assert bad, "假数字没被抓出来 —— 自检失效"
    joined = "\n".join(bad)
    assert "8760.5" in joined and "412.9" in joined
    # 干净正文自检必须为空（否则天天误报，钉子就废了）
    clean = DR.build_report(date="2000-01-01")
    assert clean["consistency"]["inconsistent"] == []


def test_consistency_check_survives_negative_and_source_numbers(client):
    """负角度与来源串里的数字（recent(40) 之类）不得被误判成编造。"""
    _seed_residual()
    rep = DR.build_report()
    assert rep["consistency"]["count"] == 0
    twin = rep["sections"]["twin"]
    assert twin["status"] == "ok" and twin["fields"]["segments"] == 2


def test_markdown_numbers_are_subset_of_structured(client):
    """markdown 正文里的数值 token 必须全部落在结构化 fields 可追溯集合内。"""
    _emit("control.move_blocked", message="测试：下发被拦截一次")
    _seed_residual()
    rep = DR.build_report()
    allowed = set()
    DR._walk_numbers(rep, allowed)
    for tok in re.findall(r"\d+(?:\.\d+)?", rep["markdown"]):
        assert tok in allowed, "markdown 里出现了不可追溯的数值 %s" % tok


# ---------------------------------------------------------------- ④ 无数据不塌成 0
def test_empty_day_says_no_data_not_zero(client):
    """没数据的一天：写「无此项数据」，不许把"没有"渲染成"0 次/0 段"。"""
    rep = DR.build_report(date="2000-01-01")
    assert "无此项数据" in rep["markdown"]
    flows = rep["sections"]["flows"]
    assert flows["status"] == "no_data"
    assert "今天没有任何事件记录" in "".join(flows["lines"])
    # 空日里不许出现"0 条事件 / 0 段"这类把没有说成零的行
    body = rep["markdown"]
    assert "0 条事件" not in body and "共 0 段" not in body
    assert rep["sections"]["twin"]["status"] == "no_data"        # reset 后本次会话无观测
    assert "判色样本环尚未接入" in body                            # E4 未接线 → 如实写


def test_color_samples_is_injectable_default_none(client):
    """E4 判色样本量 = 可注入参数；默认 None → 「判色样本环尚未接入」，不 import 未定接口。"""
    rep = DR.build_report(date="2000-01-01")
    assert rep["sections"]["color"]["status"] == "no_data"
    rep2 = DR.build_report(date="2000-01-01",
                           color_samples={"samples": 137, "days": 4})
    assert rep2["sections"]["color"]["status"] == "ok"
    assert "137" in "".join(rep2["sections"]["color"]["lines"])
    assert rep2["consistency"]["count"] == 0      # 注入的数也走同一颗自检


# ---------------------------------------------------------------- ⑤ 规则排序的关注事项
def test_attention_is_rule_sorted_with_basis_numbers(client):
    """「今日最该关注的事」= 结构化数字从大到小的规则排序，每条给依据；不凑数。"""
    for _ in range(3):
        _emit("control.move_blocked", message="测试：下发被拦截")
    _emit("sorting.no_judge", category="sorting", message="测试：判色失败放回")
    _seed_residual()
    rep = DR.build_report()
    items = rep["attention"]["items"]
    assert items, "注入了拦截/超差后不可能没有关注项"
    assert len(items) <= DR.ATTENTION_LIMIT
    vals = [it["value"] for it in items]
    assert vals == sorted(vals, reverse=True), "关注项必须按数值降序（规则排序，非玄学）"
    for it in items:
        assert it["value"] > 0, "零值项不许进关注列表（不凑数）"
        assert str(it["value"]) in it["basis"]
    top = items[0]
    assert top["value"] >= max(vals)
    assert "下发被拦截" in top["label"]      # 3 次 > 判色失败 1 次 > 超差 1 段


# ---------------------------------------------------------------- ⑥ 端点：只读、不要令牌
def test_report_endpoint_is_readonly_without_control_token(client):
    """观测不属于控制 —— 不需要控制令牌（与 /api/twin/residual、/api/sorting/cells 同口径）。"""
    r = client.get("/api/system/daily-report")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["markdown"] and d["sections"]
    assert d["consistency"]["count"] == 0

    m = client.get("/api/system/daily-report.md")
    assert m.status_code == 200
    assert m.headers["content-type"].startswith("text/markdown")
    assert "现场日报" in m.text
    assert "一致性自检" in m.text


def test_report_endpoint_bad_date_rejected_honestly(client):
    r = client.get("/api/system/daily-report?date=不是日期")
    assert r.status_code == 400
    m = client.get("/api/system/daily-report.md?date=2026-13-99")
    assert m.status_code == 400
