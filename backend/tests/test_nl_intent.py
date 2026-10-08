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
