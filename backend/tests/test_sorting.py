# -*- coding: utf-8 -*-
"""三色方块分拣 · 纯逻辑回归测试。

覆盖（全部不碰硬件、不联网）：
  1. 38 点位清单与体检（缺点必须判不通过）
  2. 红/绿/蓝 → B 区列映射、列内按行依次取槽
  3. "对满了停"：某列满 3 后再来同色必须拒绝并给出原因
  4. 多帧判色投票：多数一致采信 / 不一致判未知 / 低 conf 判未知 / 无有效帧判未知
  5. 未知颜色一律拒绝（不硬猜）

现场口径来源见 docs/方案-三色方块分拣页（38点位·经210执行）.md：
  吸不到 / 空跑 / 检测不到 → 直接停（不重试）；开始点与结束点是同一个点。
"""
from __future__ import annotations

import pytest

from app.services import sorting as S


# ---------------------------------------------------------------- 1. 点位
def test_expected_points_is_38():
    """38 = A 18 + B 18 + C 1 + 开始/结束点 1（开始与结束**共用**一个点）。"""
    pts = S.expected_points()
    assert len(pts) == 38
    assert S.total_expected() == 38
    names = [p["name"] for p in pts]
    assert len(set(names)) == 38, "点位名不能重复"
    assert S.START_POINT == "开始/结束点"
    assert "开始点" not in names and "结束点" not in names, "不能再有独立的开始/结束点"


def test_check_points_all_present():
    names = [p["name"] for p in S.expected_points()]
    chk = S.check_points(names)
    assert chk["ok"] is True and chk["missing"] == [] and chk["found"] == 38


def test_check_points_reports_missing_and_blocks():
    """缺点位必须判不通过并列出缺哪些 —— 缺目标位会变成"下发到旧目标"的危险动作。"""
    chk = S.check_points(["A1", "B1", "C检测"])
    assert chk["ok"] is False
    assert "A2" in chk["missing"] and "开始/结束点" in chk["missing"]
    assert chk["found"] == 3 and chk["total"] == 38


def test_check_points_tolerates_blank_and_whitespace():
    names = ["  A1  ", "", None, "A1上"] + [p["name"] for p in S.expected_points()]
    chk = S.check_points(names)
    assert chk["ok"] is True


# ---------------------------------------------------------------- 2/3. 列映射与占位
def test_column_mapping_matches_field_layout():
    """第 1 列红（B1/B4/B7）、第 2 列绿（B2/B5/B8）、第 3 列蓝（B3/B6/B9）。"""
    assert S.DEFAULT_COLUMN_SLOTS["红"] == ("B1", "B4", "B7")
    assert S.DEFAULT_COLUMN_SLOTS["绿"] == ("B2", "B5", "B8")
    assert S.DEFAULT_COLUMN_SLOTS["蓝"] == ("B3", "B6", "B9")


def test_pick_slot_fills_rows_in_order():
    pl = S.new_placement()
    got = []
    for _ in range(3):
        slot, why = S.pick_target_slot(pl, "绿")
        assert slot and why == ""
        S.place(pl, "绿", slot)
        got.append(slot)
    assert got == ["B2", "B5", "B8"], got


def test_column_full_stops_with_reason():
    """★ "对满了停"：同一列放满 3 件再来同色 → 拒绝，原因必须是人话。"""
    pl = S.new_placement()
    for s in S.DEFAULT_COLUMN_SLOTS["红"]:
        S.place(pl, "红", s)
    plan = S.build_piece_plan(pl, "红")
    assert plan["ok"] is False
    assert plan["reason"] == "列已满"
    assert "红" in plan["detail"] and "3" in plan["detail"]


def test_unknown_color_refused():
    """未知颜色一律拒绝（不做任何猜测性落料）。"""
    pl = S.new_placement()
    for c in ("未知", "黄", "", "red", None):
        plan = S.build_piece_plan(pl, c)
        assert plan["ok"] is False and plan["slot"] is None


def test_placement_summary_and_all_full():
    pl = S.new_placement()
    sm = S.placement_summary(pl)
    assert [c["color"] for c in sm] == ["红", "绿", "蓝"]
    assert all(c["filled"] == 0 and c["capacity"] == 3 and not c["full"] for c in sm)
    assert S.all_full(pl) is False
    for c in ("红", "绿", "蓝"):
        for s in S.DEFAULT_COLUMN_SLOTS[c]:
            S.place(pl, c, s)
    assert S.all_full(pl) is True
    assert all(c["full"] for c in S.placement_summary(pl))


def test_nine_pieces_three_colors_fit_exactly():
    """3 红 3 绿 3 蓝 → 三列正好各 3 行，不应触发"列满"。"""
    pl = S.new_placement()
    seq = ["红", "绿", "蓝", "红", "绿", "蓝", "红", "绿", "蓝"]
    for c in seq:
        plan = S.build_piece_plan(pl, c)
        assert plan["ok"] is True, "第 %s 件被拒：%s" % (c, plan["detail"])
        S.place(pl, c, plan["slot"])
    assert S.all_full(pl) is True
    assert [len(pl[c]) for c in ("红", "绿", "蓝")] == [3, 3, 3]


def test_fourth_piece_of_same_color_is_rejected():
    """4 红 → 第 4 件必须被拒（现场最多 3 个/色，多出来说明摆料有问题）。"""
    pl = S.new_placement()
    for i in range(3):
        plan = S.build_piece_plan(pl, "红")
        S.place(pl, "红", plan["slot"])
    assert S.build_piece_plan(pl, "红")["ok"] is False


# ---------------------------------------------------------------- 4/5. 判色投票
def _f(name, conf=0.8):
    return {"name": name, "conf": conf}


def test_vote_majority_accepted():
    v = S.vote_color([_f("红", 0.9), _f("红", 0.7), _f("绿", 0.95)], 0.3)
    assert v["ok"] is True and v["color"] == "红"
    assert 0.7 <= v["conf"] <= 0.9
    assert v["votes"]["红"] == 2


def test_vote_all_three_different_is_unknown():
    """帧间不一致 → 判"未知"（由上层停止并回原点），绝不硬猜一个。"""
    v = S.vote_color([_f("红"), _f("绿"), _f("蓝")], 0.3)
    assert v["ok"] is False and v["color"] == S.COLOR_UNKNOWN
    assert "不一致" in v["reason"]


def test_vote_low_conf_filtered_out():
    """没过阈值的帧不算有效帧；有效帧不足 → 未知。"""
    v = S.vote_color([_f("红", 0.10), _f("红", 0.12), _f("红", 0.9)], 0.5)
    assert v["ok"] is False and v["valid"] == 1
    assert "有效帧不足" in v["reason"] or "无有效判定" in v["reason"]


def test_vote_all_unknown_is_unknown():
    v = S.vote_color([_f("未知", 0.0), {"name": None}, {}], 0.3)
    assert v["ok"] is False and v["valid"] == 0
    assert "无有效判定" in v["reason"]
    assert v["frames"] == 3, "帧数要如实统计（含无效帧）"


def test_vote_empty_frames_is_unknown():
    v = S.vote_color([], 0.3)
    assert v["ok"] is False and v["frames"] == 0


def test_vote_two_of_two_is_enough():
    """2 帧全红 → 采信（多数即全部）。"""
    v = S.vote_color([_f("红"), _f("红")], 0.3)
    assert v["ok"] is True and v["color"] == "红"


def test_vote_single_frame_valid_is_accepted():
    """1 帧也必须能用（现场可把 frames 调成 1 追节拍）。"""
    v = S.vote_color([_f("蓝", 0.9)], 0.3)
    assert v["ok"] is True and v["color"] == "蓝"


def test_vote_two_to_one_with_confidence_within_frames():
    """3 帧里 2 帧蓝（其中一帧 conf 略低但仍过阈）→ 蓝，且 conf 取命中帧均值。"""
    v = S.vote_color([_f("蓝", 0.6), _f("蓝", 0.4), _f("红", 0.99)], 0.35)
    assert v["ok"] is True and v["color"] == "蓝"
    assert abs(v["conf"] - 0.5) < 1e-6


def test_vote_reason_is_human_readable():
    """不一致时原因要能直接给人看（不能把 dict 塞进文案）。"""
    v = S.vote_color([_f("红"), _f("绿"), _f("蓝")], 0.3)
    assert "{" not in v["reason"] and "}" not in v["reason"]
    assert "票" in v["reason"]


# ---------------------------------------------------------------- 接口层（不碰硬件）
def test_api_points_endpoint(client):
    """（无令牌）点位体检接口应要求控制令牌 —— 它是运维信息但会暴露点位清单。"""
    r = client.get("/api/sorting/points")
    assert r.status_code in (401, 403), r.text


def test_api_start_requires_control(client):
    r = client.post("/api/sorting/start", json={"dry_run": True})
    assert r.status_code in (401, 403), r.text
