# -*- coding: utf-8 -*-
"""摆放**约束求解**回归（纯逻辑，不需要真机/相机）。

背景（2026-10-10 用户口径）：
  「从A区拿三个蓝，两个绿，一个红放到B区，**按同颜色一行或者一列摆放**」
  以前"颜色→列"是写死的（红→B1/B4/B7…），用户说了"同色成行/成列"没有落点；
  且同一约束下**有很多种摆法** —— 所以必须有：合法性（同色同线）、
  不占"已有料/禁放"格、**不重叠**、多解可枚举、无解**明确拒绝**。
"""
from app.services import placement as PL


def _legal(plan, counts, occupied=(), banned=()):
    """解必须：件数对、同色在同一行或同一列、不碰占用/禁放、格不重叠。"""
    cells = []
    for color, slots in plan.items():
        assert len(slots) == counts[color], (color, slots)
        same_row = len({PL._row(s) for s in slots}) == 1
        same_col = len({PL._col(s) for s in slots}) == 1
        assert same_row or same_col, ("同色不在同一条线", color, slots)
        assert not (set(slots) & set(occupied)), ("占了已有料格", slots)
        assert not (set(slots) & set(banned)), ("占了禁放格", slots)
        cells += slots
    assert len(cells) == len(set(cells)), "两色抢同一格"


def test_row_or_col_with_occupied_avoids_them():
    """★ 现场那条：蓝3 绿2 红1，B5/B9 **已有料** → 解必须避开 B5/B9 且同色同线。"""
    counts = {"蓝": 3, "绿": 2, "红": 1}
    r = PL.solve(counts, "row_or_col", occupied=["B5", "B9"])
    assert r["ok"] is True, r
    _legal(r["plan"], counts, occupied=["B5", "B9"])
    # 3 件同色必须占满一条整线：含 B9 的第3行被占用 → 只剩第1列可整列
    assert r["plan"]["蓝"] == ["B1", "B4", "B7"]


def test_three_colors_one_column_each():
    """三色各 3 件 → 各占一整列（不重叠、覆盖 9 格）。"""
    counts = {"红": 3, "绿": 3, "蓝": 3}
    r = PL.solve(counts, "row_or_col")
    assert r["ok"] is True, r
    _legal(r["plan"], counts)
    assert sorted(sum(r["plan"].values(), [])) == ["B%d" % i for i in range(1, 10)]


def test_pattern_row_and_col_are_strict():
    a = PL.solve({"红": 3}, "col")
    assert a["ok"] and len({PL._col(s) for s in a["plan"]["红"]}) == 1
    b = PL.solve({"红": 3}, "row")
    assert b["ok"] and len({PL._row(s) for s in b["plan"]["红"]}) == 1


def test_alternatives_are_offered():
    """★ "还有好多种摆放方式" —— 多解必须能列出来（回显/界面要展示）。"""
    r = PL.solve({"红": 3}, "col")
    assert r["ok"] and len(r["alts"]) >= 2, r


def test_unsat_is_rejected_with_reason():
    """★ 摆不下要**明确拒绝并说清卡在哪**，绝不硬来。"""
    r = PL.solve({"蓝": 4}, "row_or_col")
    assert r["ok"] is False and "摆不下" in r["reason"], r
    # 3 件同色要一整条线，但所有含 3 格的线都被占/禁 → 无解
    r2 = PL.solve({"蓝": 3}, "row_or_col", banned=["B1", "B4", "B7", "B8", "B9"])
    assert r2["ok"] is False and r2["reason"], r2


def test_any_keeps_legacy_column_mapping():
    """`pattern="any"` = **不干预**：仍按既定颜色列（分拣页口径不变）。"""
    r = PL.solve({"红": 2, "蓝": 1}, "any")
    assert r["ok"] is True, r
    assert r["plan"]["红"] == ["B1", "B4"]
    assert r["plan"]["蓝"] == ["B3"]
    # 该色列被占掉 → 明确失败（不偷偷换到别的颜色列）
    bad = PL.solve({"红": 3}, "any", occupied=["B1"])
    assert bad["ok"] is False and "不够用" in bad["reason"], bad


def test_describe_is_human_readable():
    r = PL.solve({"蓝": 3, "红": 1}, "row_or_col")
    txt = r["note"]
    assert "蓝→" in txt and "（第1列）" in txt


# ---------------------------------------------------------------- 相邻（2026-10-10）
def _contiguous(slots):
    """同一列 → 行号连续；同一行 → 列号连续。"""
    rs = sorted(PL._row(s) for s in slots)
    cs = sorted(PL._col(s) for s in slots)
    by_row = rs == list(range(rs[0], rs[0] + len(rs)))
    by_col = cs == list(range(cs[0], cs[0] + len(cs)))
    return (len({PL._col(s) for s in slots}) == 1 and by_row) or \
           (len({PL._row(s) for s in slots}) == 1 and by_col)


def test_adjacent_requires_contiguous():
    """★ 用户口径"相邻"：同色必须落在**连续相邻**格上（整列 B1·B4·B7）。"""
    r = PL.solve({"蓝": 3}, "adjacent_col")
    assert r["ok"] is True, r
    assert r["plan"]["蓝"] == ["B1", "B4", "B7"]
    assert _contiguous(r["plan"]["蓝"])


def test_adjacent_vs_plain_when_box_in_between():
    """★「中间有盒子」= 逻辑不通：非相邻允许"隔空"(B1+B7)，相邻不允许。"""
    occ = ["B4", "B5", "B6"]                     # 每列中间那格都被占
    plain = PL.solve({"蓝": 2}, "col", occupied=occ)
    assert plain["ok"] is True and plain["plan"]["蓝"] == ["B1", "B7"]   # 隔空也算"同列"
    adj = PL.solve({"蓝": 2}, "adjacent_col", occupied=occ)
    assert adj["ok"] is False, adj
    assert "相邻" in adj["reason"] and "B4" in adj["reason"]             # 明确点出拦路的盒子


def test_adjacent_jumps_to_a_free_line():
    """相邻放不下本行/本列时，应**换一条线**；不要说"放不下"就完事。"""
    r = PL.solve({"蓝": 2}, "adjacent_row", occupied=["B2"])             # 第1行只剩 B1/B3（不连续）
    assert r["ok"] is True, r
    assert _contiguous(r["plan"]["蓝"]) and r["plan"]["蓝"] == ["B4", "B5"]


def test_suggest_offers_other_pattern():
    """★ 做不到要**给建议**：换个摆放方式能不能成（确定性穷举，不是随口说）。"""
    tips = PL.suggest({"蓝": 2}, "adjacent_col", occupied=["B4", "B5", "B6"])
    assert tips and any("可以" in t for t in tips), tips


def test_suggest_reduce_count_when_no_pattern_fits():
    """所有摆放方式都放不下 → 建议"减到几件"。"""
    tips = PL.suggest({"蓝": 4}, "row_or_col")
    assert tips and any("最多只能放 3 件" in t for t in tips), tips
