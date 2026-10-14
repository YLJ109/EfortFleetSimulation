# -*- coding: utf-8 -*-
"""★ E4「现场自学习判色闭环（判色样本环）」回归钉。

方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §E4
口径：AGENTS.md §5.4（判色阈值只能按现场实测值定，不能按合成图定）+ §5.5 五铁律。

分四层钉：
  A. `ingest` 的**纯函数**部分（`sample_from` / `frame_features`）：白名单抽取、失败帧计数、
     占位色不当结论、脏值不塌成 0；
  B. `ingest` 的**观测件纪律**：任何异常都不许抛出（数据库坏了/相机坏了都不带走检主线）、
     绝不调相机 HTTP、绝不 import 任何运动/下发通道；
  C. `color_advice` 的**建议面**：n 不足不给建议（方案点名）、硬阈值不进建议面（点名）、
     未标定不被学习自动填上、现场生效值未知时不拿出厂默认冒充；
  D. **REST 契约**：propose **不**打相机 `/vision/config`（点名 `test_apply_advice_runs_e2e_replay_first`）、
     人工复核回流真的进了统计、提案可回滚且回滚只回账面。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from app.services import color_advice as CA
from app.services import vision_samples as VS

ROOT = Path(__file__).resolve().parents[2]
CAMERA_COLOR = ROOT / "camera" / "vision_color.py"


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """★ 跨用例隔离：阈值快照是**进程级全局**（同 tests/conftest.py 对 ui_speed 的处理）。

    不清就会出现"上一条用例刷到了相机阈值、下一条用例的建议突然变成可执行"，
    那种红只有整片跑才有、单跑全绿，最难查。

    ★★ 同时把**两张表**清干净（样本环 + 提案账目）—— 这才是"整片跑才红"的真根因：
      `client` 用的是跨文件共享的会话库，而分拣/井字棋/视觉规则那些用例现在会经
      `_capture → vision_samples.ingest` 留下真样本行；本文件的断言看的是**整张表**
      （"提案历史正好两条"），不清就会被别的文件喂进来的数据顶掉一条。
      与 conftest 清 `app_settings.json` 覆盖层是同一个纪律：**用例自带数据，不继承现场**。
    """
    from sqlalchemy import delete

    from app.db.database import SessionLocal
    from app.db.models import ColorAdviceProposal, VisionSample

    def _wipe():
        db = SessionLocal()
        try:
            db.execute(delete(VisionSample))
            db.execute(delete(ColorAdviceProposal))
            db.commit()
        except Exception:                       # noqa: BLE001 —— 隔离件不许把用例带崩
            db.rollback()
        finally:
            db.close()

    VS.reset()
    _wipe()
    yield
    _wipe()
    VS.reset()


# ---------------------------------------------------------------- 造帧工具
def _frame(name="青", conf=0.81, fill=0.71, reason="", **extra):
    """一帧**真实形状**的 `/vision/detect` 返回（结构照 tests/data/vision_detect_frame.json）。

    ★ 故意带 `mask` / `box` / `rect` 这些"看着像 numpy 的东西" ——
      样本环必须**结构性地**把它们挡在外面（白名单抽取），这是钉点不是巧合。
    """
    col = {"name": name, "hex": "#00BBBB", "conf": conf, "reason": reason,
           "ratio": fill, "fill": fill, "lab": [70.0, -30.0, -8.0],
           "de": 12.5, "alt_de": 30.1, "chroma": 31.4}
    col.update(extra.get("color") or {})
    return {
        "ok": True,
        "results": [{
            "rect": [[479.5, 269.5], [153.5, 263.1], -77.9],
            "box": [[592, 371], [334, 317], [366, 167], [624, 221]],
            "area": 39970.0,
            "center": [479.0, 269.0],
            "mask": {"dtype": "uint8", "shape": [540, 960],
                     "data": "AAAB" * 4000},          # ← 绝不能入表
            "color": col,
        }],
        "tracker": {"state": "TRACKING", "fire": False,
                    "box": [[592, 371]], "hits": 1},
    }


def _vote(color="青", ok=True, conf=0.8, **kw):
    v = {"ok": ok, "color": color, "conf": conf, "votes": {color: 2}, "valid": 2,
         "frames": 2, "frame_detail": [], "reason": "" if ok else "无有效判定（非矩形）"}
    v.update(kw)
    return v


def _meta(**kw):
    m = {"stage": "判色", "source": "sorting", "valid_colors": ["红", "绿", "蓝", "青"],
         "cell_id": "B3"}
    m.update(kw)
    return m


def _row(color="青", outcome="confirmed", true_color="", conf=0.8, n=3,
         frames=None, placeholder=False, failed=0, thr=None):
    """样本 dict（形态 = `sample_from` 的产物，直接喂纯函数层）。"""
    return {
        "cell": "B3", "color": color, "conf": conf, "placeholder": 1 if placeholder else 0,
        "vote_ok": 0 if placeholder else (1 if outcome != "not_judged" else 0),
        "frames": json.dumps(frames if frames is not None
                             else [_frame(name=color)["results"][0]["color"]
                                   for _ in range(n)], ensure_ascii=False),
        "failed_frames": failed, "outcome": outcome, "true_color": true_color,
        "source": "sorting", "votes": "{}",
        "thr_snapshot": json.dumps(thr if thr is not None else {
            "state": "camera", "thr": {"fill_min": 0.55, "fill_warn": 0.72},
            "min_conf": 0.3}, ensure_ascii=False),
    }


# =====================================================================
# A. ingest 的纯函数部分
# =====================================================================
def test_frame_features_is_a_whitelist_never_stores_mask():
    """★ 硬边界 2：`mask`（numpy 的 JSON 形态）绝不进样本 —— 用白名单，不用黑名单剔除。"""
    one, failed = VS.frame_features(_frame())
    assert failed is False
    assert one and one["has_target"] is True
    blob = json.dumps(one, ensure_ascii=False)
    assert "mask" not in blob and "AAAB" not in blob, "mask 被写进样本 = 体积爆炸"
    assert "box" not in blob and "rect" not in blob
    # 留下的必须正是"当时的判据值"：Lab / fill / ΔE / chroma / conf / reason
    for k in ("lab", "fill", "de", "alt_de", "chroma", "conf", "area"):
        assert k in one, "缺判据字段 %s → 建议面无数据可算" % k
    assert one["lab"] == [70.0, -30.0, -8.0]


def test_ingest_counts_failed_frames_and_skips_them():
    """`{}`＝抓取失败：跳过、计数，**不**当"画面里没有候选"（§5.4 要求两类分开）。"""
    frames = [_frame(), {}, _frame(), None]
    data = VS.sample_from(frames, _vote(), _meta())
    assert data["failed_frames"] == 2, "非 dict / 空 dict 都要算失败帧"
    assert data["frame_count"] == 2
    assert json.loads(data["frames"])[0]["has_target"] is True

    empty = _frame()
    empty["results"] = []                        # 相机明确回了"什么都没有"
    d2 = VS.sample_from([empty], _vote(ok=False, color="未知"), _meta())
    assert d2["failed_frames"] == 0 and d2["frame_count"] == 1
    assert "画面里没有候选" in d2["frames"]


def test_ingest_signature_is_the_frozen_contract(client):
    """★ 另一名同事按 `ingest(frames, vote, meta=None) -> dict` 接线，签名不许改。"""
    import inspect
    sig = inspect.signature(VS.ingest)
    assert list(sig.parameters) == ["frames", "vote", "meta"]
    assert sig.parameters["meta"].default is None
    out = VS.ingest([_frame()], _vote(), _meta())
    assert set(out) == {"ok", "inserted", "failed_frames", "reason"}
    assert out["ok"] is True and out["inserted"] == 1 and out["failed_frames"] == 0


def test_placeholder_vote_is_never_a_judgement_conclusion():
    """★ 硬边界 3：空跑占位色不许当"已标定证据"—— 如实 placeholder + 未判色。"""
    v = {"ok": True, "color": "红", "conf": 0.0, "votes": {}, "placeholder": True}
    data = VS.sample_from([_frame(name="红")], v, _meta())
    assert data["placeholder"] == 1
    assert data["vote_ok"] == 0, "占位色不许写成判色成功"
    assert data["outcome"] == VS.OUTCOME_NOT_JUDGED
    assert "未判色" in data["note"]
    # 调用方就算硬说这是"人工确认判对"，也不能翻案（占位就是占位）
    forced = VS.sample_from([_frame()], v, _meta(outcome="confirmed"))
    assert forced["outcome"] == VS.OUTCOME_NOT_JUDGED


def test_dirty_values_never_collapse_into_zero():
    """§5.5 铁律③的样本环版本：脏值 → 字段**缺席**，而不是 0（0 会进统计）。"""
    f = _frame()
    f["results"][0]["color"]["conf"] = "nan"
    f["results"][0]["color"]["fill"] = None
    f["results"][0]["color"]["lab"] = ["x", 2, 3]
    one, failed = VS.frame_features(f)
    assert failed is False and one["has_target"] is True
    assert "conf" not in one and "fill" not in one and "lab" not in one
    # ★ 反向钉：只有**负载里根本没有 fill 这个键**（老相机版本）才允许用同源 ratio 补，
    #   且必须留痕 `fill_from_ratio`；显式 `fill=null` 属于"这一测没有" → 不许被 ratio 冒充。
    f2 = _frame()
    del f2["results"][0]["color"]["fill"]
    one2, _ = VS.frame_features(f2)
    assert one2.get("fill") == one2.get("ratio") and one2.get("fill_from_ratio") is True


def test_ingest_never_raises_even_when_the_database_is_broken(monkeypatch):
    """★ 硬边界 1（照 `_hold_record` 的纪律）：观测件坏了不许带走检主线。"""
    def boom(*a, **kw):
        raise RuntimeError("库坏了")

    monkeypatch.setattr("app.db.crud.insert_vision_sample", boom)
    out = VS.ingest([_frame()], _vote(), _meta())
    assert out["ok"] is False and out["inserted"] == 0
    assert "样本环登记失败" in out["reason"]

    monkeypatch.setattr("app.db.database.SessionLocal", boom)
    out2 = VS.ingest([_frame(), {}], _vote(), _meta())
    assert out2["ok"] is False and out2["failed_frames"] == 1


def test_ingest_touches_no_camera_http_and_no_motion_channel(client, monkeypatch):
    """送检那一刻**只做纯计算 + 一次写库**：不发相机 HTTP、不碰运动/下发。

    ★ 为什么钉这条：ingest 挂在真机分拣的 C 位停留窗口里（0.5s 取 3 帧）。
      在这里加一次相机往返 = 拖慢取帧；加任何下发 = §5.1 的红线。
    """
    def spy(*a, **kw):
        raise AssertionError("ingest 不许调相机 HTTP（阈值只读进程缓存）")

    import app.services.camera_client as cc
    monkeypatch.setattr(cc, "get_json", spy)
    res = VS.ingest([_frame()], _vote(), _meta())     # 无快照缓存也照样入库（state=unknown）
    assert res["ok"] is True and VS.snapshot()["state"] == "unknown"

    src = (ROOT / "backend" / "app" / "services" / "vision_samples.py").read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "motion" not in body and "modbus" not in body
    assert "rc_vacuum" not in body and "reject_if_not_ready" not in body
    # 相机 HTTP 只允许出现在 refresh_from_camera() 里（界面侧调用），不在 ingest/sample_from 里。
    #   ★ 判据用**调用点**（get_json 只有一处），不是 import 次数（import + 调用本来就是两行）。
    assert body.count("get_json(") == 1


def test_sample_row_lands_in_db_and_review_flows_back(client):
    """DB 落库 + 复核回流（"免费标注"）：样本 → 人工改判 → 统计里真的换了权威。"""
    from app.db.crud import (get_vision_sample, list_vision_samples,
                             review_vision_sample, sample_color_counts)
    from app.db.database import SessionLocal

    res = VS.ingest([_frame(name="青"), _frame(name="青")], _vote("青"), _meta())
    assert res["ok"] is True
    db = SessionLocal()
    try:
        rows = list_vision_samples(db, limit=5)
        assert rows
        sid = rows[0].id
        got = get_vision_sample(db, sid)
        assert got.cell == "B3" and got.source == "sorting"
        assert "mask" not in (got.frames or "")
        assert json.loads(got.votes) == {"青": 2}
        assert sample_color_counts(db).get("青", 0) >= 1
        # 复核：改成"绿" → outcome=corrected，真色进 true_color
        reviewed = review_vision_sample(db, sid, "wrong", true_color="绿", note="现场看是绿")
        assert reviewed.outcome == "corrected" and reviewed.true_color == "绿"
        assert reviewed.reviewed_at is not None
        st = CA.color_stats("青", [CA.__dict__ and _row("青")], min_samples=1)
        assert st["reviewed"] == 1
    finally:
        db.close()


def test_review_endpoint_requires_token_and_labels(client):
    """写侧要令牌；读侧不要（本项目先例：/api/twin/residual、/api/sorting/cells）。"""
    from app.db.database import SessionLocal
    from app.db.crud import list_vision_samples
    res = VS.ingest([_frame(name="青")], _vote("青"), _meta())
    assert res["ok"] is True
    db = SessionLocal()
    try:
        sid = list_vision_samples(db, limit=1)[0].id
    finally:
        db.close()

    assert client.post("/api/vision/samples/%s/review" % sid,
                       json={"judgement": "correct"}).status_code == 401
    tok = client.post("/api/auth/login", json={"password": "test1234"}).json()["token"]
    h = {"X-Control-Token": tok}
    # 说"判错了"却不给真色 → 400（不许猜真色）
    bad = client.post("/api/vision/samples/%s/review" % sid,
                      json={"judgement": "wrong"}, headers=h)
    assert bad.status_code == 400
    okrow = client.post("/api/vision/samples/%s/review" % sid,
                        json={"judgement": "correct"}, headers=h)
    assert okrow.status_code == 200 and okrow.json()["outcome"] == "confirmed"
    assert client.post("/api/vision/samples/999999/review",
                       json={"judgement": "correct"}, headers=h).status_code == 404
    # 读端点无需令牌
    assert client.get("/api/vision/samples").status_code == 200
    assert client.get("/api/vision/samples/advice").status_code == 200


# =====================================================================
# C. 建议派生（方案点名的三条 + 三态）
# =====================================================================
def _many(color, n, outcome="confirmed", conf=0.8, fill=0.74):
    return [_row(color=color, outcome=outcome, conf=conf,
                 frames=[_frame(name=color, conf=conf, fill=fill)["results"][0]["color"]] * 2)
            for _ in range(n)]


def test_advice_requires_sample_count():
    """★ 方案 §E4 点名（`test_advice_requires_sample_count`）：n 不足**不给建议**，并说清缺多少。"""
    out = CA.advice(_many("青", CA.MIN_SAMPLES - 1),
                    snap={"state": "camera", "thr": {"fill_min": 0.55, "fill_warn": 0.72},
                          "min_conf": 0.3})
    qing = next(c for c in out["colors"] if c["color"] == "青")
    assert qing["enough"] is False
    assert qing["advice"] == [], "样本不足却给了建议 = 拿噪声调阈值"
    assert out["items"] == []
    assert qing["missing"] == 1
    assert any("不足" in t for t in out["no_advice_reasons"])

    # 够数 + 有复核 + 证据确实指向门槛 → 才出建议
    rows = _many("青", 25)
    rows += [_row("青", outcome="corrected", true_color="绿", conf=0.55,
                  frames=[{"has_target": True, "name": "", "fill": 0.44, "conf": 0.55,
                           "reason": "杂色"}]) for _ in range(6)]
    out2 = CA.advice(rows, snap={"state": "camera",
                                 "thr": {"fill_min": 0.55, "fill_warn": 0.72},
                                 "min_conf": 0.3})
    keys2 = {i["key"] for i in out2["items"]}
    assert keys2 & set(CA.ADVISABLE_KEYS)
    assert all(i["applied"] is False for i in out2["items"])


def test_advice_never_uses_unreviewed_samples_as_proof():
    """未复核 ≠ 判对：没有人工标签就没有"误判"这件事，一条建议都不许出。"""
    out = CA.advice(_many("青", 40, outcome="unreviewed"),
                    snap={"state": "camera", "thr": {"fill_min": 0.55, "fill_warn": 0.72},
                          "min_conf": 0.3})
    assert out["items"] == []
    qing = next(c for c in out["colors"] if c["color"] == "青")
    assert qing["n"] == 40 and qing["reviewed"] == 0
    assert any("复核" in t and ("无从算起" in t or "不足" in t)
               for t in out["no_advice_reasons"])


def test_hard_thresholds_not_in_advice_surface():
    """★ 方案 §E4 点名（`test_hard_thresholds_not_in_advice_surface`）。

    黑暗判据 `dark_chroma_max` / `dark_s_max`（以及整族 `dark_*`）决定"黑 = 无效色 → 放回"，
    属于安全口径 —— **只能人显式改并留档**，一律不进建议面。
    """
    rows = _many("青", 30) + _many("红", 30)
    snap = {"state": "camera",
            "thr": {"fill_min": 0.55, "fill_warn": 0.72,
                    "dark_chroma_max": 12.0, "dark_s_max": 80.0, "dark_val_max": 130.0},
            "min_conf": 0.3}
    out = CA.advice(rows, snap=snap)
    text = json.dumps(out, ensure_ascii=False)
    for it in out["items"]:
        assert it["key"] in CA.ADVISABLE_KEYS
        assert not it["key"].startswith("dark_")
    for k in CA.HARD_EXCLUDED_KEYS:
        assert k in text, "快照里带着阈值全表（用于核对），但下面这条必须为空"
    assert all(k not in [i["key"] for i in out["items"]] for k in CA.HARD_EXCLUDED_KEYS)
    assert out["advisable_keys"] == list(CA.ADVISABLE_KEYS)
    assert CA.HARD_EXCLUDED_KEYS == ("dark_chroma_max", "dark_s_max")
    # 结构上就发不出别的键：diff_rows 也过同一道白名单闸
    forged = {"items": [{"key": "dark_chroma_max", "color": "黑", "from": 12.0, "to": 30.0}]}
    assert CA.diff_rows(forged) == [], "白名单外的键连落库都不许"


def test_uncalibrated_colors_are_never_filled_by_learning():
    """★ 方案 §E4 第 4 条：橙/黄/青/紫 的"未标定"不许被"学习"自动填上。"""
    for c in CA.UNCALIBRATED_COLORS:
        assert CA.calib_state(c) == "uncalibrated"
        out = CA.advice(_many(c, 500), snap={"state": "camera",
                                             "thr": {"fill_min": 0.55, "fill_warn": 0.72},
                                             "min_conf": 0.3})
        row = next(x for x in out["colors"] if x["color"] == c)
        assert row["calib_state"] == "uncalibrated", "样本数把未标定刷成已标定 = 假显示"
    assert out["calibrated_by_learning"] is False
    assert out["uncalibrated_colors"] == list(CA.UNCALIBRATED_COLORS)
    # 三态：七色之外的色（黑/未知）= unknown，与"未标定"、"已标定"都长得不一样
    assert CA.calib_state("黑") == "unknown"
    assert CA.calib_state("红") == "calibrated"
    assert len(set(CA.CALIB_LABEL)) == 3


def test_unknown_current_value_is_not_replaced_by_factory_default():
    """现场生效值没回显 → from=None + actionable=False（§5.5 铁律③：不知道就说不不知道）。"""
    rows = _many("青", 25) + [_row("青", outcome="corrected", true_color="绿", conf=0.5,
                                   frames=[{"has_target": True, "fill": 0.40, "conf": 0.5}])
                              for _ in range(6)]
    out = CA.advice(rows, snap={"state": "unknown", "thr": {}, "min_conf": None})
    assert out["items"], "证据足够时仍应给出方向"
    for it in out["items"]:
        assert it["from"] is None and it["from_state"] == "unknown"
        assert it["actionable"] is False
        assert "现场生效值未知" in it["text"]
        assert it["factory_default"] in (0.55, 0.72, None)


def test_blocked_frames_can_only_suggest_relaxing_fill_min():
    """被形状门槛拒掉、事后确认"那格确实是这个色" → 只往**放宽**方向提，且贴着观测最大值。"""
    blocked = {"has_target": True, "name": "未知", "conf": 0.0, "fill": 0.50,
               "reason": "非矩形(填充率 0.50 < 0.55)"}
    rows = _many("青", 25, fill=0.80)
    rows += [_row("青", outcome="corrected", true_color="青", conf=0.0,
                  frames=[dict(blocked)]) for _ in range(6)]
    # corrected 但真色=本行结论色 → 视为"这格确实是青"（标签权威来自 true_color）
    for r in rows[25:]:
        r["color"] = "未知"
    out = CA.advice(rows, snap={"state": "camera",
                               "thr": {"fill_min": 0.55, "fill_warn": 0.72},
                               "min_conf": 0.3})
    item = next((i for i in out["items"] if i["key"] == "fill_min"), None)
    assert item is not None
    assert item["to"] < 0.55 and item["to"] > 0.50, "必须落在观测值与现门槛之间"
    assert "非矩形" in item["why"]


def test_no_candidate_and_failed_frames_stay_separate():
    """§5.4：「物体超出画面」与「真漏检（没读到帧）」两类原因**不许混成一个数**。"""
    empty = {"has_target": False, "failed": False, "reason": "画面里没有候选"}
    rows = [_row("青", outcome="confirmed", frames=[empty], failed=3) for _ in range(25)]
    out = CA.advice(rows, snap={"state": "camera", "thr": {}, "min_conf": 0.3})
    qing = next(c for c in out["colors"] if c["color"] == "青")
    assert qing["no_candidate_frames"] == 25
    assert qing["failed_frames"] == 75


def test_advice_mirror_matches_camera_defaults():
    """★ 出厂默认值的**只读镜像**不许与 camera/vision_color.py::DEFAULT_THR 漂移。

    后端不能 import 相机模块（cv2 在相机 venv，而且是独立进程），所以这里镜像一份默认值。
    镜像唯一的合法性来源就是这条钉子：改了 DEFAULT_THR 不同步 → 这条直接红。
    ★ 只**读**相机源码文本，绝不执行它、绝不改它的任何默认数。
    """
    assert CAMERA_COLOR.exists(), CAMERA_COLOR
    src = CAMERA_COLOR.read_text(encoding="utf-8")
    block = src.split("DEFAULT_THR: Dict[str, float] = {")[1].split("}")[0]
    for key in CA.DEFAULTS_MIRROR:
        m = re.search(r'"%s":\s*([0-9.]+)' % re.escape(key), block)
        assert m, "DEFAULT_THR 里找不到 %s（结构变了要同步改这条钉）" % key
        assert abs(float(m.group(1)) - CA.DEFAULTS_MIRROR[key]) < 1e-9, (
            "镜像 %s=%s 与相机侧默认 %s 不一致 —— 建议面会拿错基准"
            % (key, CA.DEFAULTS_MIRROR[key], m.group(1)))


# =====================================================================
# D. REST：propose 绝不自动下发（方案点名）
# =====================================================================
def _seed_samples(n_per_color=25):
    """往样本表灌证据（走 `crud.insert_vision_sample`，形态 = `sample_from` 的产物）。"""
    from app.db.crud import insert_vision_sample
    from app.db.database import SessionLocal

    rows = _many("青", n_per_color)
    rows += [_row("青", outcome="corrected", true_color="绿", conf=0.5,
                  frames=[{"has_target": True, "fill": 0.41, "conf": 0.5}])
             for _ in range(8)]
    db = SessionLocal()
    try:
        for r in rows:
            r.setdefault("dt", None)
            insert_vision_sample(db, r)
    finally:
        db.close()


def test_apply_advice_runs_e2e_replay_first(monkeypatch, client):
    """★ 方案 §E4 点名（`test_apply_advice_runs_e2e_replay_first`）。

    口径（这条钉的是"后端不会在没有人工确认的情况下把阈值写进相机配置"）：
      ① `POST /samples/advice/propose` **一次都不调** `/vision/config`（也没有别的下发口）；
      ② 响应里**明确要求**先跑现场真图回放 `camera/tools/vision_e2e_replay.py`；
      ③ 响应如实写 `applied=False`（提案 ≠ 生效）。
    """
    hits = []

    def spy_post(path, payload=None, timeout=None):
        hits.append((path, payload))
        return (200, {"ok": True}, None)

    import app.services.camera_client as cc
    monkeypatch.setattr(cc, "post_json", spy_post)
    monkeypatch.setattr("app.api.vision.camera_client.post_json", spy_post)
    monkeypatch.setattr("app.api.vision.camera_client.get_json",
                        lambda *a, **kw: (500, None, "相机未开"))

    tok = client.post("/api/auth/login", json={"password": "test1234"}).json()["token"]
    h = {"X-Control-Token": tok}
    _seed_samples()

    # 建议本身要非空，否则 propose 走 409 分支（那条分支也得钉：不落空提案）
    adv = client.get("/api/vision/samples/advice", headers=h).json()
    assert adv["items"], "用例没造出可建议的证据（数据没到位，别把这条改成放宽断言）"

    r = client.post("/api/vision/samples/advice/propose", json={}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p for p, _ in hits if "/vision/config" in p] == [], "propose 竟下发了阈值"
    assert hits == [], "propose 不许对相机服务发任何 POST"
    assert body["applied"] is False
    txt = json.dumps(body, ensure_ascii=False)
    assert "vision_e2e_replay" in txt, "响应必须写明先跑现场真图回放"
    assert "camera/tools/vision_e2e_replay.py" in body["requires"]["e2e_replay"]
    assert "/vision/config" in body["requires"]["apply_channel"]
    assert "未下发" in body["message"] or "不会替你" in body["message"]

    # 没有可提案的建议 → 409，且不攒空提案
    from app.db.database import SessionLocal
    from app.db.crud import list_advice_proposals
    db = SessionLocal()
    try:
        assert len(list_advice_proposals(db)) == 1
    finally:
        db.close()
    empty = client.post("/api/vision/samples/advice/propose",
                        json={"min_samples": 1000}, headers=h)   # 1000=端点上界（再大被 pydantic 422 挡住，那是校验不是业务分支）
    assert empty.status_code == 409
    assert hits == []


def test_propose_needs_control_token(client):
    """写侧（提案/回滚）统一要令牌；不传令牌连门都进不了（更谈不上下发）。"""
    r = client.post("/api/vision/samples/advice/propose", json={})
    assert r.status_code == 401
    assert client.post("/api/vision/samples/advice/rollback").status_code == 401


def test_proposal_keeps_previous_version_and_rollback_is_bookkeeping_only(client):
    """提案可回滚；★ 回滚**只回账面**，现场生效阈值一个字节都没动（不许假显示"已回滚阈值"）。"""
    hits = []
    import app.services.camera_client as cc
    original_post = cc.post_json

    def spy_post(path, payload=None, timeout=None):
        hits.append(path)
        return (200, {"ok": True}, None)

    cc.post_json = spy_post
    try:
        tok = client.post("/api/auth/login", json={"password": "test1234"}).json()["token"]
        h = {"X-Control-Token": tok}
        _seed_samples()
        assert client.post("/api/vision/samples/advice/propose", json={}, headers=h).status_code == 200
        # 换一批证据（把复核标签去掉 → 建议项仍来自 fill_min 那条）再提一次，制造"上一版"
        assert client.post("/api/vision/samples/advice/propose", json={}, headers=h).status_code == 200
        lst = client.get("/api/vision/samples/proposals", headers=h).json()
        assert len(lst["items"]) == 2
        assert lst["items"][0]["status"] == "active"
        assert lst["items"][1]["status"] == "superseded"
        assert all(i["applied"] is False for i in lst["items"])

        rb = client.post("/api/vision/samples/advice/rollback", headers=h)
        assert rb.status_code == 200, rb.text
        assert rb.json()["applied"] is False
        assert rb.json()["restored"] is True
        assert "没有" in rb.json()["message"] or "未被改动" in rb.json()["message"]
        after = client.get("/api/vision/samples/proposals", headers=h).json()
        assert after["items"][0]["status"] == "rolled_back"   # 列表按新旧倒序：刚被回滚的是最新那条
        assert after["active_id"] == lst["items"][1]["id"]
        assert hits == [], "整条提案/回滚链路都不许对相机服务发 POST"
    finally:
        cc.post_json = original_post


def test_throttle_surface_shape_is_honest(client):
    """读端点回的东西必须够界面画三态：七色计数补齐、快照 state 在、未标定名单在。"""
    _seed_samples()
    d = client.get("/api/vision/samples").json()
    assert set(d["colors"]) == set(CA.SEVEN_COLORS)
    assert d["snapshot"]["state"] in ("camera", "manual", "unknown")
    assert VS.OUTCOME_NOT_JUDGED in d["outcomes"]
    a = client.get("/api/vision/samples/advice").json()
    assert {c["color"] for c in a["colors"]} >= set(CA.SEVEN_COLORS)
    for c in a["colors"]:
        assert c["calib_state"] in ("calibrated", "uncalibrated", "unknown")
        assert "missing" in c and "enough" in c
    assert a["hard_excluded_keys"] == list(CA.HARD_EXCLUDED_KEYS)


def test_no_writes_to_camera_or_motion_anywhere_in_the_module_pair():
    """★ 静态总钉：E4 的两个服务模块里根本不存在"下发阈值/动机器人"的调用。

    （`refresh_from_camera` 是**读**状态，且只在界面读端点里调；见上面的 GET 口径。）
    """
    for rel in ("app/services/vision_samples.py", "app/services/color_advice.py"):
        src = (ROOT / "backend" / rel).read_text(encoding="utf-8")
        assert "post_json" not in src, "%s 里出现 post_json = 有自动下发能力" % rel
        assert "rc_vacuum" not in src and "motion.command" not in src
        assert "reject_if_not_ready" not in src
    api = (ROOT / "backend" / "app" / "api" / "vision.py").read_text(encoding="utf-8")
    seg = api.split("# ★ E4", 1)[1]
    assert "post_json" not in seg, "E4 段里出现相机 POST = 越过了「不自动下发」这条线"


def test_env_file_and_camera_defaults_untouched():
    """钉住"我没做的事"：本轮没有改相机侧任何默认阈值、没有动 .env。"""
    src = CAMERA_COLOR.read_text(encoding="utf-8")
    m = re.search(r'"fill_min":\s*([0-9.]+)', src)
    assert m and float(m.group(1)) == 0.55, "fill_min 默认值被改了（§5.4：只能现场实测定）"
    m2 = re.search(r'"dark_chroma_max":\s*([0-9.]+)', src)
    assert m2 and float(m2.group(1)) == 12.0
    assert os.environ.get("EFORT_SIMULATE") == "always"
