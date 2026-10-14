# -*- coding: utf-8 -*-
"""★★ E7「一格三证」回归钉（`services/cell_evidence.py` + `api/sorting.py` 接线 + 只读端点）。

这条功能要防的事故（方案 §E7 的原话）：**数据早就有，缺的是"装订"** ——
判色结论、到位自证残差、照片档各存各的，事后没法一眼回答「B3 这一格当时到底看到了什么、
机器人真到了没有」。所以钉子也分四类：

  ① **格号只有一份规则**：`B3` 与 `B3上`（正上位）是**同一个格子**（进出该格必经正上位），
     不是料格的点（起点 / `J6正面`）→ 空串，**不许硬造格子**。
  ② **缺就写缺**：三证里任何一条没到手，记录里必须有 `missing` + **为什么缺**；
     `ok` 只认调用方明确写下的结论（§5.5 铁律③：不知道就说不不知道）。
  ③ **不另立第二秩序权威**：期望姿态取 `_move` **返回的实发目标**、到位结论取 **E2 同源残差**、
     偏差公式用 `sorting.joint_deviation`（J6 环形差）—— 本模块一律不自己再算一遍。
  ④ **观测件绝不拖累主线**：不新增拍摄 / 不新增移动 / 不新增读库，异常吞掉，
     落盘交给后台线程（AGENTS.md §5.1「绝不在真机分拣运行期间做任何可能打断它的操作」）。
"""
from __future__ import annotations

import inspect
import re
import time

import pytest

from app.api import sorting as A
from app.services import cell_evidence as CE
from app.services import sorting as S


@pytest.fixture(autouse=True)
def _clean_cells():
    """内存历史是进程级的 —— 用例之间必须清（不清就会"上一格的记录出现在这一例里"）。"""
    CE.reset()
    yield
    CE.reset()


# ---------------------------------------------------------------- ① 格号：唯一派生
def test_slot_and_its_above_point_share_one_cell():
    """`B3`（原位）与 `B3上`（正上位）**必须**是同一个格号 —— 否则三证永远凑不齐。"""
    assert CE.cell_id("B3") == "B3"
    assert CE.cell_id("B3上") == "B3"
    assert CE.cell_id("A1上") == "A1"
    assert CE.cell_id("b3上") == "B3"          # 大小写不敏感（现场示教表存在小写写法）


def test_check_point_is_a_reviewable_cell():
    """C 检测位是"判色发生的那一格"，也算格子（照片/判色证天然属于它）。"""
    for n in ("C检测", "C位", "C区", "C"):
        assert CE.cell_id(n) == "C"


def test_non_cell_points_never_invent_a_cell():
    """起点 / 虚拟切面点 / 越界编号 → 空串（调用方据此**如实跳过**装订，不硬造）。"""
    for n in ("开始/结束点", "J6正面", "J6反面", "A10", "D1", "", None, "   "):
        assert CE.cell_id(n) == ""
    assert CE.is_cell("B3上") is True
    assert CE.is_cell("J6正面") is False


def test_alias_shares_the_same_single_rule():
    """`cell_of_point` 只是别名，**里面不能再有一套规则**（否则两处会漂）。"""
    assert "return cell_id(" in inspect.getsource(CE.cell_of_point)


# ---------------------------------------------------------------- ② 装订：缺就写缺
def test_bind_missing_evidence_says_so_with_reason():
    """三份都没给 → `missing` 三条齐全、`complete=False`、每条都带原因（不静默省略）。"""
    rec = CE.bind("B3")
    assert rec["complete"] is False
    assert rec["missing"] == list(CE.EVIDENCE_KEYS)
    for k in CE.EVIDENCE_KEYS:
        assert rec["evidence"][k]["ok"] is False
        assert rec["evidence"][k]["reason"]            # 必须"说得出为什么缺"


def test_bind_never_infers_ok_from_content():
    """★ §5.5 铁律③：调用方**没写 `ok`** → 算"没有这一证"，不许因为"看着有内容"盖成到手。"""
    rec = CE.bind("B3", colour={"color": "红", "conf": 0.9})
    assert rec["evidence"]["colour"]["ok"] is False
    assert "colour" in rec["missing"]


def test_bind_complete_only_when_all_three_declared_ok():
    rec = CE.bind("B3",
                  photo={"ok": True, "images": {"full": "a/full.jpg"}},
                  colour={"ok": True, "color": "红", "conf": 0.9},
                  pose={"ok": True, "live": [0] * 6})
    assert rec["complete"] is True and rec["missing"] == []
    for k in CE.EVIDENCE_KEYS:
        assert rec["evidence"][k]["reason"] == ""


def test_bind_does_not_rewrite_authoritative_numbers():
    """装订**不改写数字**：判色 conf、姿态 live/target 原样搬运（改写就是第二权威）。"""
    rec = CE.bind("B3", colour={"ok": True, "color": "红", "conf": 0.42,
                                "votes": {"红": 3, "绿": 1}},
                  pose={"ok": True, "live": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5]})
    assert rec["evidence"]["colour"]["conf"] == 0.42
    assert rec["evidence"]["colour"]["votes"] == {"红": 3, "绿": 1}
    assert rec["evidence"]["pose"]["live"] == [1.5, 2.5, 3.5, 4.5, 5.5, 6.5]
    assert rec["cell_id"] == "B3"


def test_bind_honours_given_timestamp():
    """`at` 由调用方给 → 可确定性重放（与 E2 同一条纪律）。"""
    assert CE.bind("B3", at=1234.5)["at"] == 1234.5


# ---------------------------------------------------------------- ③ 三份证据的形状
def test_photo_absent_is_honest_and_never_recaptures():
    """没有档 → `ok=False` + 调用方给的**真实原因**（不许兜底成"相机没开"糊弄现场）。"""
    row = CE.photo_evidence(None, reason="本流程不归档图片")
    assert row["ok"] is False and row["images"] == {}
    assert row["reason"] == "本流程不归档图片"


def test_photo_refuses_to_stale_match_an_old_archive():
    """档与这条记录差得太远 → **不算同一件事**，不硬凑（宁可缺，不错装）。"""
    now = 2000.0
    row = CE.photo_evidence({"full": "a/full.jpg"}, rec_at=now - 60.0, at=now)
    assert row["ok"] is False and "60.0s" in row["reason"]
    assert CE.photo_evidence({"full": "a/full.jpg"}, rec_at=now - 1.0, at=now)["ok"] is True


def test_colour_evidence_passes_vote_conclusion_through():
    v = {"ok": True, "color": "红", "conf": 0.86, "votes": {"红": 2},
         "frames": 2, "valid": ["红", "绿", "蓝"]}
    row = CE.colour_evidence(v)
    assert row["ok"] is True and row["color"] == "红" and row["conf"] == 0.86
    assert "vote_color" in row["source"]                 # 来源必须写清
    empty = CE.colour_evidence(None)
    assert empty["ok"] is False and "没有判色" in empty["reason"]


def test_colour_evidence_refuses_dry_run_placeholder_color():
    """★ 空跑占位色**不是**判色结论：`placeholder=True` → `ok=False` + 如实说明（假显示红线）。"""
    row = CE.colour_evidence({"ok": True, "color": "红", "conf": 0.0,
                              "votes": {}, "placeholder": True})
    assert row["ok"] is False and row["placeholder"] is True
    assert "占位色" in row["reason"]


def test_pose_without_live_joints_is_not_an_evidence():
    """读不到实时姿态 → 位姿这一证**没有**（绝不拿名义姿态冒充"机器人在这儿"）。"""
    row = CE.pose_evidence(live=None, target=[0] * 6, residual={"verdict": "arrived"})
    assert row["ok"] is False and "实时姿态" in row["reason"]


def test_pose_without_residual_cannot_claim_arrived():
    """只有姿态、没有 E2 残差 → 如实写"不能说到到位"（到位结论的唯一权威是 E2）。"""
    row = CE.pose_evidence(live=[0] * 6, target=[0] * 6, residual=None)
    assert row["ok"] is False and "不能说到到位" in row["reason"]


@pytest.mark.parametrize("verdict,want_ok",
                         [("arrived", True), ("slow", True),
                          ("stuck", False), ("not_reached", False),
                          ("moving", False), ("", False)])
def test_pose_ok_follows_residual_verdict(verdict, want_ok):
    row = CE.pose_evidence(live=[0] * 6, target=[0] * 6,
                           residual={"verdict": verdict, "peak_deg": 0.4})
    assert row["ok"] is want_ok
    assert row["verdict"] == verdict


def test_pose_deviation_uses_the_single_joint_deviation_impl():
    """偏差公式**只有 `sorting.joint_deviation` 一份**（J6 按环形差），本模块不再写第二遍。"""
    live = [0.0, 0.0, 0.0, 0.0, 0.0, 179.0]
    tgt = [0.0, 0.0, 0.0, 0.0, 0.0, -179.0]
    row = CE.pose_evidence(live=live, target=tgt, residual={"verdict": "arrived"})
    assert row["dev_max_deg"] == 2.0                      # 环形差，不是 358
    assert row["dev_max_deg"] == S.joint_deviation([round(x, 3) for x in live], tgt)
    src = inspect.getsource(CE.pose_evidence)
    assert "S.joint_deviation" in src and "abs(" not in src


# ---------------------------------------------------------------- residual_for：同源取段
def test_residual_for_picks_named_segment_and_drops_curve():
    rows = [{"source": "B3上", "verdict": "arrived", "profile": [[0.1]] * 240},
            {"source": "C检测", "verdict": "moving"}]
    got = CE.residual_for("C检测", rows=rows)
    assert got["verdict"] == "moving" and "profile" not in got
    assert CE.residual_for("没走过的点", rows=rows) == {}


def test_residual_for_reads_newest_first_from_twin_residual(monkeypatch):
    """真实数据源是 **E2 到位自证**（同屏同源），不是自己攒的第二份历史。"""
    from app.services import twin_residual as TR
    monkeypatch.setattr(TR.twin_residual, "recent",
                        lambda n=10: [{"source": "B3上", "verdict": "stuck"}])
    assert CE.residual_for("B3上")["verdict"] == "stuck"


# ---------------------------------------------------------------- ④ 记录 · 水位 · 落盘
def test_seq_is_monotonic_and_watermark_visible():
    """★ §5.5 铁律①：水位只用**只增不减**的 `seq`（前端不许用 `records.length`）。"""
    r1 = CE.record(CE.bind("B3"))
    r2 = CE.record(CE.bind("A1"))
    assert (r1["seq"], r2["seq"]) == (1, 2)
    assert CE.latest_seq() == 2


def test_recent_is_newest_first_and_cell_filterable():
    CE.record(CE.bind("B3"))
    CE.record(CE.bind("A1"))
    rows = CE.recent(10)
    assert [r["cell_id"] for r in rows] == ["A1", "B3"]
    assert [r["cell_id"] for r in CE.recent(10, cell="B3")] == ["B3"]


def test_stats_reports_completeness_per_cell():
    CE.record(CE.bind("B3", colour={"ok": True, "color": "红"},
                      pose={"ok": True, "live": [0] * 6}))     # 缺照片
    CE.record(CE.bind("B3", photo={"ok": True}, colour={"ok": True},
                      pose={"ok": True}))                       # 齐
    st = CE.stats()
    assert st["total"] == 2
    one = next(c for c in st["cells"] if c["cell_id"] == "B3")
    assert one["n"] == 2 and one["complete"] == 1 and one["missing"]["photo"] == 1


def test_record_survives_a_broken_disk_and_never_blocks(monkeypatch):
    """落盘失败**不许**把分拣线程带崩（写盘在后台线程，主线程只 append）。"""
    def _boom():
        raise OSError("磁盘坏了")
    monkeypatch.setattr(CE, "_day_file", _boom)
    out = CE.record(CE.bind("B3"))
    assert out["ok"] is True and out["queued"] is True
    time.sleep(0.4)                       # 给后台线程一次"失败也只写 warning"的机会


def test_record_eventually_reaches_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(CE, "base_dir", lambda: str(tmp_path))
    CE.record(CE.bind("B3", colour={"ok": True, "color": "红"}))
    path = next(tmp_path.glob("cells-*.jsonl"), None)
    for _ in range(60):
        if path and path.exists():
            break
        time.sleep(0.05)
        path = next(tmp_path.glob("cells-*.jsonl"), None)
    assert path is not None and path.exists()
    assert '"B3"' in path.read_text(encoding="utf-8")


def test_reset_only_clears_memory(tmp_path, monkeypatch):
    """`reset()` 是测试隔离用的，**不删已经落盘的档**（复盘要看历史）。"""
    monkeypatch.setattr(CE, "base_dir", lambda: str(tmp_path))
    CE.record(CE.bind("B3"))
    CE.reset()
    assert CE.recent(10) == [] and CE.latest_seq() == 0


# ---------------------------------------------------------------- ⑤ 接线：三处都装订
def _src(fn) -> str:
    return inspect.getsource(fn)


def test_single_piece_drop_binds_three_evidences():
    assert "_cell_evidence(tgt + \"上\", v=v, target=drop_target" in _src(A._run_sorting)


def test_dual_face_drop_binds_per_face_and_is_labeled():
    body = _src(A._run_sorting_dual_face)
    assert "_cell_evidence(tgt + \"上\", v=v, target=drop_target" in body
    assert '"dual_face": True' in body                        # 同格两件也要分得开


def test_put_back_binds_the_failed_judgement_as_evidence():
    """放回原位**也要装订** —— "判不出色的那一格"恰恰是最需要证据的那一格。"""
    body = _src(A._put_back)
    assert "_cell_evidence(src_slot, v=v, target=down_target" in body
    assert "down_target = _move(" in body


def test_expected_pose_comes_from_the_move_return_value():
    """★ 期望值 = `_move` **返回的实发目标**（与到位校验同源），不再按点位表推第二遍。"""
    src = open(A.__file__, encoding="utf-8").read()
    assert src.count("drop_target = _move(") == 2          # 单件 + 双面
    assert src.count("down_target = _move(") == 1          # 放回
    assert src.count("_cell_evidence(") >= 4               # 定义 + 三处调用


def test_cell_helper_is_observation_only():
    """装订函数里**不许**出现：下发 / 拍照 / 读库 / 扫掠（观测件不得拖累主线、不得新增动作）。"""
    body = _src(A._cell_evidence)
    for forbidden in ("motion.command", "camera_client", "request_capture",
                      "SessionLocal", "list_points", "_move(", "emit_event"):
        assert forbidden not in body, forbidden
    assert "except Exception" in body and "return None" in body


def test_cell_helper_declares_its_hard_boundary():
    """文档即钉子：模块/函数要写明"不补拍、不猜到位、非料格如实跳过"。"""
    body = _src(A._cell_evidence)
    assert "不硬造格子" in body and "不为凑三证补拍" in body
    mod = open(CE.__file__, encoding="utf-8").read()
    assert "绝不在分拣热路径里新增拍摄" in mod and "不裁决、不推断" in mod


def test_piece_event_carries_cell_id_and_completeness():
    """事件里带**格号 + 齐全度**（只带结论，不把整条记录塞进事件表）。"""
    src = open(A.__file__, encoding="utf-8").read()
    assert src.count('"cell_id": CE.cell_id(tgt)') == 2
    assert src.count('"evidence": (None if ev is None else') == 2


def test_no_eta_no_speed_in_evidence():
    """三证**不产 ETA、不写速度、不写气路位号**（AGENTS.md：PC 只写目标 + 触发位）。

    ★ 只看**这一条链路**的两份源码（`cell_evidence` 模块 + 装订函数）——
      分拣里 `"speed_pct"` 是既有状态字段，跟三证无关，扫全文件会误报。
    """
    src = open(CE.__file__, encoding="utf-8").read() + _src(A._cell_evidence)
    assert not re.search(r'["\'](eta|eta_s|duration|seconds|elapsed)["\']\s*:', src)
    assert not re.search(r'["\'](speed_pct|vperc|vac_bit|bit)["\']\s*:', src)


# ---------------------------------------------------------------- ⑥ 只读端点
def test_cells_endpoint_is_readonly_without_control_token(client):
    """观测不属于控制 —— **不需要控制令牌**（与 `/api/twin/residual` 同口径）。"""
    r = client.get("/api/sorting/cells")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and set(d["evidence_keys"]) == {"photo", "colour", "pose"}
    assert "latest_seq" in d and "stats" in d and "note" in d


def test_single_cell_review_endpoint_returns_records_or_says_none(client):
    CE.record(CE.bind("B3", colour={"ok": True, "color": "红"}))
    d = client.get("/api/sorting/cells/b3").json()
    assert d["found"] is True and d["cell_id"] == "B3"
    assert d["records"][0]["missing"] == ["photo", "pose"]
    none = client.get("/api/sorting/cells/A9").json()
    assert none["found"] is False and "没有记录 ≠ 没有发生" in none["note"]


def test_cells_endpoint_filters_by_cell(client):
    CE.record(CE.bind("B3"))
    CE.record(CE.bind("A1"))
    assert [x["cell_id"] for x in client.get("/api/sorting/cells?cell=A1").json()["records"]] == ["A1"]
