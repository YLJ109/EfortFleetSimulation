# -*- coding: utf-8 -*-
"""自然语言意图解析回归测试（规则层）。

★ 这一层是"大模型必须遵守的 JSON 契约" + 断网/无 key 时的兜底，所以必须钉死。
覆盖：行列表达、颜色列、正上方、批量、模糊速度拒绝、看不懂不猜、急停优先级。
"""
from __future__ import annotations

import pytest

from app.services import nl_intent as N


# ---------------------------------------------------------------- 槽位换算
def test_slot_math_row_major():
    """3×3 行优先：第1行第1列=A1、第2行第3列=A6、第3行第3列=A9。"""
    assert N.slot_of(1, 1) == "A1"
    assert N.slot_of(1, 3) == "A3"
    assert N.slot_of(2, 1) == "A4"
    assert N.slot_of(2, 3) == "A6"
    assert N.slot_of(3, 3) == "A9"
    assert N.slot_of(1, 1, "B") == "B1"
    assert N.slot_of(4, 1) is None and N.slot_of(1, 4) is None


def test_slot_pos_roundtrip():
    for i in range(1, 10):
        s = "A%d" % i
        r, c = N.slot_pos(s)
        assert N.slot_of(r, c) == s


def test_color_columns():
    """红列=B1/B4/B7，绿列=B2/B5/B8，蓝列=B3/B6/B9（与分拣口径一致）。"""
    assert N.color_column_slots("红") == ["B1", "B4", "B7"]
    assert N.color_column_slots("绿") == ["B2", "B5", "B8"]
    assert N.color_column_slots("蓝") == ["B3", "B6", "B9"]
    assert N.color_column_slots("紫") == []


# ---------------------------------------------------------------- 常见说法
@pytest.mark.parametrize("text,slot", [
    ("A区第一行第一列", "A1"),
    ("A区第1行第1列", "A1"),
    ("a区 第 2 行 第 3 列", "A6"),
    ("B区第三行第三列", "B9"),
    ("A6", "A6"),
    ("拿 A 4", "A4"),
])
def test_parse_rowcol_and_slot(text, slot):
    r = N.parse("把 %s 放到 B1" % text)
    assert r["ok"] is True, r
    assert r["intent"] == "move_one"
    assert r["plan"]["from"]["slot"] == slot


def test_parse_color_column_nth():
    """B区红列第2个 = B4（红列是 B1/B4/B7 的行序）。"""
    r = N.parse("把 A1 放到 B区红列第2个")
    assert r["ok"] is True and r["plan"]["to"]["slot"] == "B4"
    assert r["plan"]["to"]["color"] == "红"


def test_parse_whole_color_column_target():
    """只说"放到红列"→ kind=color_col（执行时取该列第一个空位）。"""
    r = N.parse("把 A区第1行第1列放到B区红列")
    assert r["ok"] is True
    assert r["plan"]["to"]["kind"] == "color_col" and r["plan"]["to"]["color"] == "红"


def test_plain_column_maps_to_color():
    """第1列/第2列/第3列 → 红/绿/蓝列。"""
    assert N.parse("把 A1 放到 B区第1列")["plan"]["to"]["color"] == "红"
    assert N.parse("把 A1 放到 B区第2列")["plan"]["to"]["color"] == "绿"
    assert N.parse("把 A1 放到 B区第3列")["plan"]["to"]["color"] == "蓝"


def test_above_flag():
    r = N.parse("把 A1正上方 放到 B2")
    assert r["ok"] is True and r["plan"]["from"]["above"] is True


# ---------------------------------------------------------------- 看颜色
def test_look_color_needs_motion_warning():
    """★ 相机只对 C 区 → 看颜色必须提示"这是运动"。"""
    r = N.parse("A区第一行第一列的矩形什么颜色")
    assert r["ok"] is True and r["intent"] == "look_color"
    assert r["plan"]["from"]["slot"] == "A1"
    assert any("运动" in w for w in r["warnings"])


def test_look_color_rejects_all():
    """一次只能看一格（要逐件送相机）→ 说"全部"应拒绝。"""
    r = N.parse("看看全部都是什么颜色")
    assert r["ok"] is False and "一格" in r["error"]


# ---------------------------------------------------------------- 批量
def test_move_all_with_shuffle():
    r = N.parse("B区的全部矩形随机摆放回A区")
    assert r["ok"] is True and r["intent"] == "move_all"
    assert r["plan"]["shuffle"] is True
    assert r["plan"]["from"]["kind"] == "all"
    assert any("上限" in w for w in r["warnings"])


def test_move_all_no_shuffle_when_not_said():
    r = N.parse("把B区全部放到A区第1行第1列")
    assert r["ok"] is True and r["plan"]["shuffle"] is False


# ---------------------------------------------------------------- 流程控制
@pytest.mark.parametrize("text,action", [
    ("开始分拣", "start"),
    ("跑分拣", "start"),
    ("停止", "stop"),
    ("停下", "stop"),
    ("暂停", "pause"),
    ("继续", "resume"),
])
def test_sorting_control(text, action):
    r = N.parse(text)
    assert r["ok"] is True and r["intent"] == "sorting"
    assert r["plan"]["action"] == action


def test_estop_has_priority():
    """★ 急停优先级最高：句子里还有别的词也必须判急停。"""
    r = N.parse("A1 放到 B2 急停")
    assert r["ok"] is True and r["intent"] == "estop"


def test_soft_stop_is_not_estop():
    """★ 安全口径："停下/停止/别动" 是**软停**（停止执行），不是急停。

    急停会断伺服、手臂可能因重力下沉；把一句客气的"停下"当急停是危险的。
    要急停必须**明确说"急停"**。
    """
    for t in ("快停下别放了", "停下", "别动了", "停止"):
        r = N.parse(t)
        assert r["ok"] is True, (t, r)
        assert r["intent"] == "sorting" and r["plan"]["action"] == "stop", (t, r)
    assert N.parse("急停")["intent"] == "estop"
    assert N.parse("紧急停止")["intent"] == "estop"


# ---------------------------------------------------------------- 拒绝与不猜
def test_vague_speed_rejected():
    """模糊速度词明确拒绝（速度只认右上角那一份），且要说清怎么调。"""
    for t in ("快点把A1放到B1", "慢一点放到B2"):
        r = N.parse(t)
        assert r["ok"] is False and "速度" in r["error"]


def test_goto_start_is_a_real_action():
    """★ 2026-10-10 用户口径：「让它回到起点」—— 这是**可执行动作**。

    此前没有这个动作 → 规则层听不懂 → 落进大模型闲聊 → 模型只能编
    "已回到起点位置"（它没有任何执行权，纯属谎报设备状态）。
    """
    for t in ("回到起点", "回起点", "回原点", "回零", "返回起点", "去起点", "回到开始/结束点"):
        r = N.parse(t)
        assert r["ok"] and r["intent"] == "goto_start", (t, r)
        assert r["plan"] == {"action": "goto_start"}, (t, r)
    # 它是**运动**，不是停：别被"回"字带进停止/急停
    assert N.parse("回到起点")["intent"] != "estop"
    assert N.parse("回到起点")["intent"] != "sorting"


def test_ban_slots_parsed():
    """★ 2026-10-10 用户口径：任意「某格不能拿 / 某格不能放」都要能解析（无规则也要认）。"""
    cases = [
        ("A4不能拿", {"from": ["A4"], "to": []}),
        ("A5不能放", {"from": [], "to": ["A5"]}),
        ("不要拿A4", {"from": ["A4"], "to": []}),
        ("禁止取A1", {"from": ["A1"], "to": []}),
        ("B5、B9禁止摆放物体", {"from": [], "to": ["B5", "B9"]}),   # 先枚举后动词
        ("B5和B9不能放", {"from": [], "to": ["B5", "B9"]}),
        ("A4不能拿，A5不能放", {"from": ["A4"], "to": ["A5"]}),      # 一句里两个方向
        ("B3不要放和A2不要拿", {"from": ["A2"], "to": ["B3"]}),
    ]
    for text, exp in cases:
        r = N.parse(text + "，把A1放到B1")
        assert r["ok"], (text, r)
        assert r["plan"].get("ban") == exp, (text, r["plan"])
    # ★ 约束不得污染目标解析：说了"B6不能放"≠ 要放到 B6
    r = N.parse("把A1放到B1，B6不能放")
    assert r["plan"]["to"]["slot"] == "B1", r["plan"]
    assert r["plan"]["ban"] == {"from": [], "to": ["B6"]}
    assert any("不能放料" in w for w in r["warnings"])
    # 非法格号 → 明说，不静默忽略
    bad = N.parse("把A1放到B1，B0不能放")
    assert bad["ok"] is False and "格号" in bad["error"]


def test_query_and_inventory_intents():
    """★ 2026-10-10 用户口径：状态查询（**只读**）与盘点（**运动**）都要能解析。"""
    for t in ("A区有几个盒子", "A区有几个物体", "A区几个红几个蓝", "A区有哪些颜色",
              "A区还有几个空位", "A区有几个无效颜色", "B区放了什么"):
        r = N.parse(t)
        assert r["ok"] and r["intent"] == "query_state", (t, r)
    for t in ("盘点A区", "清点一下A区", "把A区数一遍"):
        r = N.parse(t)
        assert r["ok"] and r["intent"] == "inventory", (t, r)
    # ★ 不能把搬运指令误判成查询（含"搬到"一律走搬运）
    r = N.parse("把A区的3个红色，2个蓝色，1个绿色搬到B区")
    assert r["intent"] == "move_quota", r
    # ★ 单格看颜色仍是 look_color（不是整区查询）
    r = N.parse("A区第1行第1列是什么颜色")
    assert r["intent"] == "look_color", r


def test_unknown_does_not_guess():
    for t in ("你好", "今天天气怎么样", "把那个东西弄一下"):
        r = N.parse(t)
        assert r["ok"] is False and r["error"]
        assert r["intent"] == ""


def test_take_without_target_rejected():
    r = N.parse("把A1取起来")
    assert r["ok"] is False and "放到哪里" in r["error"]


def test_out_of_range_rowcol_rejected():
    r = N.parse("把 A区第4行第1列 放到 B1")
    assert r["ok"] is False and "超出" in r["error"]


def test_color_col_nth_out_of_range():
    r = N.parse("把 A1 放到 B区红列第5个")
    assert r["ok"] is False and "第 5" in r["error"]


def test_empty_text():
    assert N.parse("")["ok"] is False
    assert N.parse("   ")["ok"] is False


def test_echo_helper():
    """回显给人看：A6 → 'A区第2行第3列'。"""
    assert N.slot_to_zh("A6") == "A区第2行第3列"
    assert N.slot_to_zh("B9") == "B区第3行第3列"
