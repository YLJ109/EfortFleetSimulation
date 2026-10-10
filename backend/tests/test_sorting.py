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


# ---- ★★ P0 回归：真实 /vision/detect 结构（颜色嵌在 results[i].color 里）----
# 为什么单列一组：旧用例喂的都是**自己造的扁平结构** _f()，与真实接口对不上，
# 于是 22 条全绿、真机却**永远**判不出色（现场那句"检测不到（无有效判定（全部未知…））"）。
# 这里用**真实抓取**的帧当夹具（camera/tools 的 VisionEngine 跑中灰底红方块导出），
# 读法一旦改回顶层就会立刻红。
import json  # noqa: E402
import os  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "vision_detect_frame.json")


def _real_frame():
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_fixture_is_real_detect_shape():
    """夹具本身必须是真实结构 —— 否则这组回归又变成"自己骗自己"。"""
    d = _real_frame()
    assert set(d.keys()) >= {"ok", "results"}
    assert isinstance(d["results"], list) and d["results"], "夹具必须含至少一个候选"
    assert "color" in d["results"][0] and "name" in d["results"][0]["color"]
    assert "name" not in d, "真实返回的 name 不在顶层（这正是 P0 的成因）"


def test_vote_color_reads_real_detect_payload():
    """★ 真实结构 → 必须判出颜色（三帧一致 → 采信）。"""
    fr = _real_frame()
    v = S.vote_color([fr, fr, fr], 0.28)
    assert v["ok"] is True, "真实结构判不出色 = P0 复发：%s" % v["reason"]
    assert v["color"] == fr["results"][0]["color"]["name"]
    assert v["conf"] > 0.28


def test_frame_color_extracts_nested_color():
    got = S.frame_color(_real_frame(), 0.28)
    assert got["name"] == "红" and got["conf"] > 0.9 and got["area"] > 0


def test_vote_color_says_no_candidate_when_results_empty():
    """画面里没有候选 → 原因必须写"没有候选"，不能与"读法写错"共用一句话。"""
    empty = {"ok": True, "results": [], "tracker": {}}
    v = S.vote_color([empty] * 3, 0.28)
    assert v["ok"] is False and "没有候选" in v["reason"]


def test_vote_color_surfaces_server_error():
    """相机没开/服务报错 → 原因里必须带上服务端原话（无画面），方便现场判断。"""
    v = S.vote_color([{"ok": False, "error": "无画面"}] * 3, 0.28)
    assert v["ok"] is False and "无画面" in v["reason"]


def test_frame_color_picks_valid_over_larger_invalid():
    """一帧里有多个候选：**名次有效且过阈**的优先，不按面积盲取最大。"""
    fr = _real_frame()
    big_unknown = {"area": 999999, "box": [[0, 0]], "center": [0, 0],
                   "color": {"name": "未知", "conf": 0.0, "reason": "非矩形(填充率 0.30 < 0.72)"}}
    fr2 = {"ok": True, "results": [big_unknown] + fr["results"], "tracker": {}}
    got = S.frame_color(fr2, 0.28)
    assert got["name"] == "红", "面积最大的'未知'不该压过有效的红"


def test_frame_color_reports_nonrect_reason():
    """只有非矩形候选时：原因要能带出"非矩形"（现场才调得对旋钮）。"""
    fr = {"ok": True, "results": [
        {"area": 5000, "box": [[0, 0]], "center": [0, 0],
         "color": {"name": "未知", "conf": 0.0, "reason": "非矩形(填充率 0.55 < 0.72)"}}],
        "tracker": {}}
    v = S.vote_color([fr] * 3, 0.28)
    assert v["ok"] is False and "非矩形" in v["reason"]


def test_frame_color_low_conf_reports_threshold():
    """顶层结构（旧调用方）低置信 → 原因里带出阈值，便于现场调。"""
    v = S.vote_color([{"name": "红", "conf": 0.10}] * 3, 0.28)
    assert v["ok"] is False and "置信度" in v["reason"]


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


# ---------------------------------------------------------------- 有效色集（2026-10-10 七色重构）
#   用户口径："只要用户说的颜色是有效颜色，用户没提到的颜色是无效颜色"。
#   vote_color(..., valid_colors=...) 把"有效色"从写死的红/绿/蓝换成**用户这句话提到的颜色**。
def test_vote_valid_colors_default_keeps_rgb_and_rejects_palette_extras():
    """默认（不传 valid_colors）仍是红/绿/蓝：调色板新增的「青」按**无效色**处理（零回归）。"""
    v = S.vote_color([_f("青", 0.95)] * 3, 0.3)
    assert v["ok"] is False and v["color"] == S.COLOR_UNKNOWN
    assert set(v["votes"].keys()) == {"红", "绿", "蓝"}
    # 三色本身不受影响
    assert S.vote_color([_f("红", 0.9)] * 3, 0.3)["ok"] is True


def test_vote_valid_colors_makes_unmentioned_color_invalid():
    """用户只说「红」→ 蓝是**无效色**（不是"未知"）：多帧全蓝也不采信。"""
    v = S.vote_color([_f("蓝", 0.95)] * 3, 0.3, valid_colors=("红",))
    assert v["ok"] is False and v["color"] == S.COLOR_UNKNOWN
    assert set(v["votes"].keys()) == {"红"}
    assert v["votes"] == {"红": 0}


def test_vote_valid_colors_accepts_extra_palette_color():
    """用户点名「青」→ 青变成有效色（同一批帧，默认会被拒、指名后被采信）。"""
    frames = [_f("青", 0.95)] * 3
    assert S.vote_color(frames, 0.3)["ok"] is False                    # 默认：青无效
    v = S.vote_color(frames, 0.3, valid_colors=("红", "绿", "蓝", "青"))
    assert v["ok"] is True and v["color"] == "青"


def test_vote_valid_colors_independent_of_order_and_dupes():
    """有效色集允许乱序/去重（内部按集合用）。"""
    v = S.vote_color([_f("橙", 0.9)] * 3, 0.3, valid_colors=("橙", "橙", "红"))
    assert v["ok"] is True and v["color"] == "橙"


def test_frame_color_respects_names_argument():
    """frame_color 的名字白名单也要跟着 valid 走（否则内层会把"局"提前判掉）。"""
    f = {"ok": True, "results": [{"area": 900.0, "color": {"name": "紫", "conf": 0.9}}]}
    assert S.frame_color(f, 0.3)["name"] == "紫"                       # 结构读法不变
    assert S.frame_color(f, 0.3, ("红",))["name"] == "紫"              # 名字白名单不影响"读出什么"



# ---------------------------------------------------------------- 接口层（不碰硬件）
def test_api_points_endpoint(client):
    """（无令牌）点位体检接口应要求控制令牌 —— 它是运维信息但会暴露点位清单。"""
    r = client.get("/api/sorting/points")
    assert r.status_code in (401, 403), r.text


def test_put_back_sequence(monkeypatch):
    """★ 2026-10-10 用户口径：判不出色 → **原路放回取料格**（不是就地松手）。

    钉住工艺顺序（顺序即工艺，不许调换）：
        A{n}上 → A{n}下落 → 停止吸 → 放气 → 等 → 停止放气 → A{n}上抬起 → 回起点
    为什么必须回原格上空再放：就地松件会掉在 C 区/半路，位置随机 ——
    现场要满地找件，下一件取料还可能撞上它。
    """
    from app.api import sorting as SA

    seq = []
    monkeypatch.setattr(SA, "_move",
                        lambda ctx, pmap, name, sp, dry, stage, st=None: seq.append(("move", name)))
    monkeypatch.setattr(SA, "_air",
                        lambda ctx, act, dry, stage, st=None: seq.append(("air", act)))
    monkeypatch.setattr(SA, "_set", lambda st=None, **kw: None)

    class _Ctx:
        def sleep(self, s): return False

    SA._put_back(_Ctx(), {}, "A3", 40, False, blow_ms=0, prefix="放回原位 A3")
    assert [v for k, v in seq if k == "move"] == ["A3上", "A3", "A3上", SA.S.START_POINT]
    assert [v for k, v in seq if k == "air"] == ["release", "blow", "unblow"]


def test_pick_target_slot_skips_banned():
    """★ 2026-10-10 用户口径："某格不能放" → 落位要**真的跳过**它。

    跳满时原因必须能分清是"禁放占掉了"还是"天然满了"（现场处理方式完全不同）。
    """
    pl = S.new_placement()
    assert S.pick_target_slot(pl, "红", None, ["B1"])[0] == "B4"      # B1 被禁 → 顺延
    assert S.pick_target_slot(pl, "红")[0] == "B1"                   # 不传 → 行为与原来一致
    slot, why = S.pick_target_slot(pl, "红", None, ["B1", "B4", "B7"])
    assert slot is None and "禁放" in why, why


def test_api_start_requires_control(client):
    r = client.post("/api/sorting/start", json={"dry_run": True})
    assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------- 6. 每次运行必须从头开始
def test_fresh_run_state_is_clean():
    """★ 用户口径（2026-10-08）："每次运行必须从头开始"。

    每轮开始的状态必须是**全新的**：进度 0、三列占位全空、没有上一轮的颜色/目标/
    停止原因/夹持/疑似标记。否则会出现"第二轮接着第一轮的占位跑"或
    "刚点开始就显示上一轮的错误"。
    """
    st = S.fresh_run_state(dry_run=True, max_pieces=3, judge=False)
    assert st["piece"] == 0 and st["total"] == 3
    assert st["stage"] == "准备"
    assert st["color"] == "" and st["conf"] == 0.0 and st["target"] == ""
    assert [c["filled"] for c in st["placement"]] == [0, 0, 0]
    assert all(c["used"] == [] for c in st["placement"])
    assert st["error"] == "" and st["finished"] is False and st["ok"] is None
    assert st["holding"] is False and st["suspect_pick"] is False
    assert st["sim_color"] is False
    assert st["judge"] is False            # 空跑不判色


def test_fresh_run_state_judge_flag_follows_input():
    """真实下发且双闸开 → judge=True（本轮会真判色）。"""
    st = S.fresh_run_state(dry_run=False, max_pieces=9, judge=True)
    assert st["judge"] is True and st["total"] == 9


def test_two_fresh_states_are_independent():
    """两次构造必须互不影响（共享可变对象会让第二轮"继承"第一轮的占位）。"""
    a = S.fresh_run_state(True, 3, False)
    b = S.fresh_run_state(True, 3, False)
    a["placement"][0]["used"].append("B1")
    a["placement"][0]["filled"] = 1
    assert b["placement"][0]["used"] == [] and b["placement"][0]["filled"] == 0


def test_start_from_scratch_uses_A1_first():
    """引擎顺序必须**每轮从 A1 开始**（不是接着上一轮的第 n 个槽）。"""
    assert list(S.A_SLOTS[:3]) == ["A1", "A2", "A3"]
    assert list(S.A_SLOTS) == ["A%d" % i for i in range(1, 10)]


# ---------------------------------------------- 7. 3D 摆位接口（尺寸 / 相机高度）
def _ctrl(client):
    """控制类接口所需的鉴权头（与 test_api._ctrl_headers 同法）。"""
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


def test_api_blocks_requires_control(client):
    """摆位接口暴露全部点位坐标 → 同样必须带控制令牌。"""
    r = client.get("/api/sorting/blocks")
    assert r.status_code in (401, 403), r.text


def test_api_blocks_defaults_match_user_request(client):
    """★ 用户口径（2026-10-08 第 2 轮）：

    · "矩形大一点点…现在四倍大" → 30 → **70mm**（受实测列距限制，见下一条）
    · "摄像头在地上看着上面的" → 相机底座高 **0**（坐在地面/台面上，镜头朝上）
    """
    d = client.get("/api/sorting/blocks", headers=_ctrl(client)).json()
    assert d["ok"] is True
    assert d["block_mm"] == [70.0, 70.0, 14.0]
    assert d["camera_height_mm"] == 0.0


def test_api_blocks_block_size_is_under_measured_spacing(client):
    """★★ 70mm 是**实测槽距之下**的上限，不许再往上调：

    重叠判定用 max(长,宽)×1.05（70 → 73.5mm）。实测列距 85~88mm、行距 97~105mm，
    所以 73.5 < 85 仍然安全；一旦 ≥85，整片 A/B 槽位都会被判成"重叠风险"而**全部不画**
    —— 症状是"3D 里一个方块都没有"，而不是报错，最难查。
    """
    from app.api import sorting as API

    side = max(API.BLOCK_MM_DEFAULT[0], API.BLOCK_MM_DEFAULT[1])
    assert side * 1.05 < 85.0, "超过实测列距下限 85mm：整片槽位会被判重叠"
    assert side * 1.05 < 97.8, "超过实测行距下限 97.8mm：整片槽位会被判重叠"
    # 反证：判据不是恒真的（120mm 的料必须被判重叠，说明这个上限真的在起作用）
    assert 120.0 * 1.05 > 85.0


def test_api_blocks_cam_h_can_be_overridden(client):
    """相机底座高度仍可覆盖（现场实测底座 40mm 时用得上）。"""
    d = client.get("/api/sorting/blocks?cam_h=40", headers=_ctrl(client)).json()
    assert d["camera_height_mm"] == 40.0


def test_api_blocks_block_mm_override_and_bad_input_falls_back(client):
    """block= 覆盖可用；坏输入**回落默认**，不能把 0/负数当尺寸（会画出 0 厚方块）。"""
    h = _ctrl(client)
    got = client.get("/api/sorting/blocks?block=50x50x20", headers=h).json()["block_mm"]
    assert got == [50.0, 50.0, 20.0]
    for bad in ("0,0,0", "abc", "10,20"):
        got = client.get("/api/sorting/blocks?block=" + bad, headers=h).json()["block_mm"]
        assert got == [70.0, 70.0, 14.0], "坏输入 %r 必须回落默认尺寸" % bad


def test_api_blocks_units_and_plane_are_declared(client):
    """单位/坐标系必须显式声明（前端换算只认这三个字段，改口径要一起改）。"""
    d = client.get("/api/sorting/blocks", headers=_ctrl(client)).json()
    assert d["unit"] == "mm_base" and "Z-up" in d["frame"]
    assert "非视觉定位" in d["note"]
    assert len(d["slots"]) == 18
    assert d["camera"]["name"] == S.CAMERA_POINT


# ------------------------------------- 8. 摆放面高度（"不要悬浮"）与落料时序（"一直吸住"）
def test_api_blocks_plane_h_defaults_to_zero(client):
    """★★ 用户 2026-10-08 第三轮："19 个摆放的 3D 物体怎么悬浮起来了啊，放在地上啊"。

    根因：把示教点 Z 当成了料的高度 —— 示教点 FK 出的是**法兰中心**（工具安装面），
    料在工具下方低着"一个工具长度"。所以摆放高度必须来自 `plane_h_mm`（默认 0 =
    站台台面 = 机器人底座平面），**不是** `plane_z`（法兰高度）。
    """
    h = _ctrl(client)
    # 先示教一个点，让 plane_z（示教算出来的法兰高度）真的有值
    r = client.post("/api/sorting/teach",
                    json={"name": "A1", "joints": [10.0, 20.0, 30.0, 0.0, 40.0, 0.0]}, headers=h)
    assert r.status_code == 200, r.text

    d = client.get("/api/sorting/blocks", headers=h).json()
    assert d["plane_h_mm"] == 0.0            # ★ 摆放面：默认坐台面
    assert d["plane_z"] > 0                   # 法兰高度（体检用）
    assert d["plane_z"] != d["plane_h_mm"]    # 两者不是一回事
    assert "仅用于共面性体检" in d["height_note"]
    assert "站台台面" in d["height_note"]
    # 该槽位必须被画出来（taught=True 且给了 FK 位置）
    a1 = [s for s in d["slots"] if s["name"] == "A1"][0]
    assert a1["taught"] is True and a1["pos"] and a1["pos"][2] > 0


def test_api_blocks_plane_h_can_be_overridden(client):
    """现场若垫了底板/托盘有厚度，可用 plane_h 抬起来（单位 mm，基座系）。"""
    d = client.get("/api/sorting/blocks?plane_h=25", headers=_ctrl(client)).json()
    assert d["plane_h_mm"] == 25.0
    # 反证：不是恒等返回 0（否则上面那条会假过）
    assert d["plane_h_mm"] != 0.0


def test_plane_h_and_plane_z_are_independent(client):
    """两个字段口径必须彻底分开：plane_z 是示教算出来的（不可控），plane_h 是摆位参数。

    若哪天有人把摆放高度又接回 plane_z，这条会立刻红。
    """
    h = _ctrl(client)
    client.post("/api/sorting/teach",
                json={"name": "B1", "joints": [10.0, 20.0, 30.0, 0.0, 40.0, 0.0]}, headers=h)
    d1 = client.get("/api/sorting/blocks", headers=h).json()
    d2 = client.get("/api/sorting/blocks?plane_h=25", headers=h).json()
    assert d1["plane_z"] == d2["plane_z"] > 0, "plane_z 只跟示教数据有关"
    assert d1["plane_h_mm"] != d2["plane_h_mm"]
    # 槽位表本身也不该被 plane_h 改动（改的是"摆多高"，不是"摆在哪"）
    assert [(s["name"], s["pos"]) for s in d1["slots"]] == \
           [(s["name"], s["pos"]) for s in d2["slots"]]


def test_start_blow_default_is_500ms(client):
    """★ 用户口径："到了 B 区停止吸气然后再放气 0.5 秒，停止放气" → 默认 500ms。

    语义也变了：以前 blow_ms 是"放气后留在原位多久再走"（放气本身保持到回起点），
    现在是**放气持续时间**，吹完就地停止放气。
    """
    from app.api.sorting import StartIn

    m = StartIn()
    assert m.blow_ms == 500
    assert "放气持续" in StartIn.model_fields["blow_ms"].description


def test_start_suck_hold_default_is_500(client):
    """★ 用户流程口径："吸取 → 停一下 → 抬起"。

    吸上后**先停一下再抬**，等真空建立/吸牢 —— 立刻抬起容易在半空掉件，
    而现场没有真空反馈，掉了只能等判色失败才发现。
    ★ 2026-10-10 用户口径：停顿时长 **0.5 秒**（默认 500ms）。
    """
    from app.api.sorting import StartIn

    m = StartIn()
    assert m.suck_hold_ms == 500
    assert "吸牢" in StartIn.model_fields["suck_hold_ms"].description


def test_camera_point_dwell_and_three_frames_within_half_second():
    """★ 2026-10-10 用户口径："摄像头点位 0.5 秒、0.5 秒内取三帧图片"。

    · C 区停留默认 **500ms**；
    · 帧间隔 FRAME_GAP_S 固定 → **3 帧跨度 = (3-1)×间隔 ≤ 0.5s**（在停留窗口内取完）。
    """
    from app.api import sorting as SA
    from app.api.sorting import StartIn
    import inspect

    assert StartIn().dwell_ms == 500
    assert StartIn().frames == 3
    span = (3 - 1) * SA.FRAME_GAP_S
    assert span <= 0.5, "3 帧跨度 %.2fs 超过 0.5s（帧间隔 FRAME_GAP_S=%.2f 太大）" % (
        span, SA.FRAME_GAP_S)
    # 反证：帧间隔真被用于取帧（不是写死别的数）
    assert "FRAME_GAP_S" in inspect.getsource(SA._capture)


def test_vote_color_returns_per_frame_detail_and_best():
    """★★ 用户口径（2026-10-09）："捕捉3张（只显示一张置信度最高的）"。

    逐帧明细必须**如实**返回（含未判出的帧），`best_frame` 指向命中帧里置信度最高的那一个。
    以前只有 `votes`（每色几票），现场看不到"这 3 帧各自看到什么" ——
    判不出时无法判断是"三帧都低"还是"两帧对一帧错"。
    """
    frames = [
        {"ok": True, "results": [{"color": {"name": "红", "conf": 0.70}, "area": 5000}]},
        {"ok": True, "results": [{"color": {"name": "红", "conf": 0.95}, "area": 5000}]},
        {"ok": True, "results": [{"color": {"name": "蓝", "conf": 0.40}, "area": 5000}]},
    ]
    v = S.vote_color(frames, 0.3)
    assert v["ok"] is True and v["color"] == "红"
    assert len(v["frame_detail"]) == 3, "3 帧都要有明细"
    assert [f["color"] for f in v["frame_detail"]] == ["红", "红", "蓝"]
    assert v["frame_detail"][1]["conf"] == 0.95
    assert v["best_frame"] == 1, "best 必须指向命中帧里置信度最高的那张"
    assert all(f["ok"] for f in v["frame_detail"])       # 三帧都过了阈值


def test_vote_color_frame_detail_survives_failure():
    """判不出时**同样**要给逐帧明细 —— 现场要看到"三帧各自是什么"，
    而不是只收到一句"检测不到"（那只能瞎调旋钮）。
    """
    frames = [
        {"ok": True, "results": []},                      # 画面里没候选
        {"ok": False, "error": "相机未开"},                # 视觉服务报错
        {"ok": True, "results": [{"color": {"name": "红", "conf": 0.10}, "area": 5000}]},
    ]
    v = S.vote_color(frames, 0.3)
    assert v["ok"] is False
    assert len(v["frame_detail"]) == 3
    assert all(f["ok"] is False for f in v["frame_detail"])
    assert "画面里没有候选" in v["frame_detail"][0]["reason"]
    assert "相机未开" in v["frame_detail"][1]["reason"]
    assert v["frame_detail"][2]["color"] == "红" and v["frame_detail"][2]["conf"] == 0.10


def test_fresh_state_clears_frame_detail():
    """每轮开头必须清掉抓帧明细（"每次运行从头开始"）——
    否则新一轮界面会显示上一轮抓到的颜色。
    """
    st = S.fresh_run_state(True, 3, False)
    assert st["frame_detail"] == []
    assert st["best_frame"] is None
    assert st["votes"] == {}


def test_air_dry_keeps_semantic_stage():
    """★★ 空跑时阶段名必须**保留语义**：写 "取料·吸(空跑)"，不能退化成 "吸(空跑)"。

    因为"吸"与"放"在流程里是两个完全不同的步骤（取料·吸 / 放料·放气），
    丢掉语义后界面的执行流程高亮会跳错步 —— 而空跑恰恰是最常用来排练流程的模式。
    """
    import inspect

    from app.api import sorting as API

    src = inspect.getsource(API._air)
    assert 'stage=("%s(空跑)" % stage)' in src, "空跑分支必须带上调用方的语义化 stage"
    assert 'stage="%s(空跑)" % label' not in src.replace('stage=("%s(空跑)" % stage)', ""), \
        "不许再退回只剩气泡标签（吸/放）的写法"


def test_engine_air_sequence_source_pins():
    """★★ "顺序即工艺"：吸气全程保持 → 到 B 才「停止吸 → 放气 → 等 → 停止放气」。

    引擎整跑需要真机（运动 + 气路），在 CI 里跑不了；这里用**源码位置**把顺序钉死，
    并断言异常/收尾路径一定会收掉两路气（电平保持型气路最怕"程序结束了阀还开着"）。

    ★ 只在**主管线切片**里找位置：文件头的流程注释里也有这些词，
      全文 indexOf 会指到注释上（第一版就踩了：算出"回起点在第 367 字"）。
    """
    import inspect

    from app.api import sorting as API

    src = inspect.getsource(API)
    loop = src[src.index("for idx, a_slot in enumerate"):]
    i_suck = loop.index('_air(ctx, "suck"')
    i_rel = loop.index('_air(ctx, "release"')
    i_blow = loop.index('_air(ctx, "blow"')
    i_unblow = loop.index('_air(ctx, "unblow"')
    i_at_a = loop.index("取料·下落")
    i_at_b = loop.index("放料·正上方")
    # 锚"真实调用"，不锚词（注释里也会写"回起点"解释历史写法）
    i_back = loop.index('_move(ctx, pmap, start, speed, dry, "回起点")')

    assert i_suck > i_at_a, "吸气必须在落到 A 槽之后"
    assert i_rel > i_at_b, "★ 停止吸必须在到 B 正上方之后（送检全程吸住）"
    assert i_rel < i_blow, "先停止吸，再放气"
    assert i_blow < i_unblow < i_back, "★★ 放气就地停（不再保持到回起点）"
    # 异常/收尾兜底：三处判断 + 两路气的动作都必须存在
    assert src.count("if not dry and (blow_on or suck_on)") >= 3
    assert src.count("suck_on = False") >= 3 and src.count("blow_on = False") >= 3

