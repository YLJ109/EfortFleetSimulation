# -*- coding: utf-8 -*-
"""★★ E4「现场自学习判色闭环」的**样本环**（唯一写入方）。

把每一次判色留下的 **ROI 特征 + 当时的判据值 + 结论 + 事后人工标签**存成可复核的样本，
`services/color_advice.py` 只能**从这张表**里长出建议 —— 没有样本就没有建议（方案 §E4）。

调用契约（★ 签名固定，接线方 = `api/sorting.py` 的送检处；另一名同事按此签名接）：

    vision_samples.ingest(frames, vote, meta) -> {"ok", "inserted", "failed_frames", "reason"}

  frames = `GET /vision/detect` 的**原始 payload 列表**（某些项可能是 `{}`＝抓取失败）
  vote   = `app/services/sorting.py::vote_color(...)` 的返回值（空跑时可能带 `placeholder=True`）
  meta   = {"stage", "source", "valid_colors", "at", "cell_id"}（全部可选）

★★ 四条硬边界（改代码前先读，每条都对应方案 §E4 或 AGENTS.md §5 的口径）：

  1. **任何异常都不许抛出**（照抄 `api/sorting.py::_hold_record` 的纪律）：
     样本环是**观测件**，它坏了不许带走检主线 —— 现场真机正在分拣时，
     一条写库失败绝不能变成"判色失败/流程中止"。所有失败只写 debug 日志并回 `ok=False`。
  2. **只存数字特征，绝不存像素 / `mask`**：`/vision/detect` 的返回里可能带 `mask`
     （numpy 数组的 JSON 形态），一帧就是几十万字节的噪声，对"阈值该怎么挪"零信息量。
     这里用**白名单抽取**（不是黑名单剔除）—— 新出现的字段自动进不来，
     也就永远不会因为相机侧多回一个数组而把库撑爆。要图去相机归档链路（`/vision/image?p=`）。
  3. **空跑占位色不是判色结论**（`vote.placeholder`）：那个色是编给流程走的，
     入库当"已标定证据"就是 §5.5 铁律③禁止的假显示 → 如实写 `placeholder=1` +
     `outcome="not_judged"`，建议派生一律排除。
  4. **不新增拍摄、不新增移动、不碰任何下发通道**：本模块只写样本表；
     连"读现场阈值"都不在热路径上做 HTTP（见 `snapshot()` 的进程缓存口径），
     免得到 C 位那 0.5s 窗口里被一次相机往返拖慢（§5.1 绝不打断真机运行）。
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import get_logger

log = get_logger("vision_samples")

# ---- 复核结论（outcome）的取值；界面/统计只用这几个词，别在外面另造说法 ----
OUTCOME_UNREVIEWED = "unreviewed"    # 未复核（默认）
OUTCOME_CONFIRMED = "confirmed"      # 人工确认判对
OUTCOME_CORRECTED = "corrected"      # 人工改判（true_color 才是真色）
OUTCOME_PUT_BACK = "put_back"        # 无效色/判不出 → 原路放回（流程事实）
OUTCOME_NOT_JUDGED = "not_judged"    # ★ 空跑占位色：本轮**没有真的判色**
OUTCOMES = (OUTCOME_UNREVIEWED, OUTCOME_CONFIRMED, OUTCOME_CORRECTED,
            OUTCOME_PUT_BACK, OUTCOME_NOT_JUDGED)

SOURCES = ("sorting", "tictactoe", "vision_rule")

# ★★ 白名单：`results[i].color` 里**允许入表**的字段（camera/vision_color.py::analyze_color 的产物）。
#    刻意不含 mask / box / rect —— 见模块 docstring 硬边界 2。
COLOR_KEYS: Tuple[str, ...] = ("name", "hex", "conf", "reason", "ratio", "fill",
                               "lab", "de", "alt_de", "chroma", "hue", "alt")
# 数值型字段（lab 单独按三元组处理，name/hex/reason/alt 按字符串截断）
NUM_KEYS: Tuple[str, ...] = ("conf", "ratio", "fill", "de", "alt_de", "chroma", "hue")
# `results[i]` 这一层允许入表的几何量（只是数字，不是像素）
RESULT_KEYS: Tuple[str, ...] = ("area",)
CENTER_MAX = 2          # center 只取前两个数

NOTE_NOT_JUDGED = "未判色（空跑占位色：本轮没有送检，那个色是编给流程走的）"

# ---------------------------------------------------------------- 阈值快照（进程缓存）
# ★ 为什么是缓存而不是每次读相机：ingest 发生在**真机流程的送检那一刻**，
#   在这里做一次相机 HTTP 往返会拖慢 C 位停留窗口（§5.1），而且相机服务可能根本没开。
#   ⇒ 快照只由**界面侧**（`api/vision.py` 的读端点）刷新，ingest 只读缓存。
# ★ `state` 是三态：unknown（没刷到过，**不知道**现场值）| camera（相机服务回显的生效值）
#   | manual（人显式告知）。三态不许塌成"有/没有"（§5.5 铁律③）。
_lock = threading.RLock()
_thr_state: Dict[str, Any] = {"state": "unknown", "thr": {}, "min_conf": None,
                              "at": None, "source": ""}


def snapshot() -> Dict[str, Any]:
    """当前阈值快照的**副本**（纯读，不发 HTTP、不抛异常）。"""
    with _lock:
        return {
            "state": str(_thr_state.get("state") or "unknown"),
            "thr": dict(_thr_state.get("thr") or {}),
            "min_conf": _thr_state.get("min_conf"),
            "at": _thr_state.get("at"),
            "source": str(_thr_state.get("source") or ""),
        }


def remember_thr(payload: Optional[Dict[str, Any]], source: str = "camera") -> Dict[str, Any]:
    """把相机服务 `/vision/status` 里回显的生效阈值记进进程缓存。

    `payload` = 相机状态字典（含 `thr` / `min_conf`）。缺字段就**只更新拿到的那部分**，
    拿不到的保持原状 —— 宁可继续标 unknown，也不要把"没回显"写成"就是默认值"。
    """
    if not isinstance(payload, dict):
        return snapshot()
    thr = payload.get("thr")
    min_conf = payload.get("min_conf")
    clean_thr: Dict[str, float] = {}
    if isinstance(thr, dict):
        for k, v in thr.items():
            f = _num(v)
            if f is not None and isinstance(k, str):
                clean_thr[k[:32]] = f
    mc = _num(min_conf)
    with _lock:
        if clean_thr or mc is not None:
            _thr_state["thr"] = clean_thr or _thr_state.get("thr") or {}
            if mc is not None:
                _thr_state["min_conf"] = mc
            _thr_state["state"] = source if source in ("camera", "manual") else "camera"
            _thr_state["at"] = time.time()
            _thr_state["source"] = str(source or "camera")
    return snapshot()


def refresh_from_camera(timeout: float = 0.8) -> Dict[str, Any]:
    """**只在界面侧调用**：读一次相机服务状态把生效阈值缓存起来。

    ★ 失败（相机服务没开 / 超时 / 结构异常）→ 原样返回当前缓存并如实保持 unknown，
      绝不抛异常（这是观测件，不许把接口带崩）。
    """
    try:
        from app.services import camera_client
        st, js, err = camera_client.get_json("/vision/status", timeout=timeout)
        if st == 200 and not err and isinstance(js, dict) and js:
            remember_thr(js, source="camera")
    except Exception as e:                       # noqa: BLE001
        log.debug("阈值快照刷新失败（已忽略，继续用上一份/标 unknown）: %s", e)
    return snapshot()


def reset() -> None:
    """测试夹具用：清掉进程内的阈值缓存，避免用例之间串味。"""
    with _lock:
        _thr_state.update({"state": "unknown", "thr": {}, "min_conf": None,
                           "at": None, "source": ""})


# ---------------------------------------------------------------- 数字收敛（脏值一律夹掉）
def _num(v: Any) -> Optional[float]:
    """任何脏值 → None（**不是 0**：0 是一个真实的测量值，会进统计）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 4)


def _triplet(v: Any, n: int = 3) -> Optional[List[float]]:
    """lab / center 这类短数值数组：长度不对或含脏值 → None。"""
    if not isinstance(v, (list, tuple)):
        return None
    out: List[float] = []
    for x in list(v)[:n]:
        f = _num(x)
        if f is None:
            return None
        out.append(f)
    return out or None


def _text(v: Any, limit: int = 64) -> str:
    s = str(v if v is not None else "")
    return s[:limit]


# ---------------------------------------------------------------- 逐帧特征（白名单抽取）
def frame_features(payload: Any) -> Tuple[Optional[Dict[str, Any]], bool]:
    """**一帧** `/vision/detect` 原始返回 → (特征 dict | None, 是否失败帧)。

    返回 (None, True)  = 这一帧没抓到（`{}` / 非 dict / `ok=False`）→ 计入 failed_frames；
    返回 (None, False) 不该出现（都归到上面两类了）；
    返回 ({...}, False) = 有效帧。没有候选时 `has_target=False`，**仍然是一条观测**
      （"相机明确回了空结果"本身是漏检证据，与"这一帧没读到"是两件事 —— 方案 §5.4
       要求把"物体超出画面"与"真漏检"分开统计，这里就是那条分界）。
    """
    if not isinstance(payload, dict) or not payload:
        return None, True
    if payload.get("ok") is False:
        return None, True

    base: Dict[str, Any] = {"has_target": False, "failed": False}
    results = payload.get("results")
    if isinstance(results, list) and results:
        best: Optional[Dict[str, Any]] = None
        best_area = -1.0
        for r in results:
            if not isinstance(r, dict):
                continue
            a = _num(r.get("area")) or 0.0
            if a >= best_area:
                best, best_area = r, a
        if best is not None:
            col = best.get("color") if isinstance(best.get("color"), dict) else {}
            one: Dict[str, Any] = {"has_target": True}
            for k in COLOR_KEYS:
                if k not in col:
                    continue
                if k == "lab":
                    continue          # ★ 结构化字段：只走下面的 _triplet（脏值→缺席），
                                      #   走 _text 会把 ['x',2,3] 变成字符串冒充观测值
                if k in NUM_KEYS:
                    f = _num(col.get(k))
                    if f is not None:
                        one[k] = f
                else:
                    one[k] = _text(col.get(k), 24 if k != "hex" else 9)
            lab = _triplet(col.get("lab"), 3)
            if lab is not None:
                one["lab"] = lab
            for k in RESULT_KEYS:
                f = _num(best.get(k))
                if f is not None:
                    one[k] = f
            ctr = _triplet(best.get("center"), CENTER_MAX)
            if ctr is not None:
                one["center"] = ctr
            # `fill` 缺失时退回同源的 `ratio`（camera 侧这两个字段在 analyze_color 里同值，
            # 见 vision_color.py 的 "ratio": round(fill,3), "fill": round(fill,3)）；
            # ★ 只在**真有数字**时补，补出 null 会污染统计（_num 的脏值→None 约定）。
            if one.get("fill") is None and "fill" not in col and one.get("ratio") is not None:
                one["fill"] = one["ratio"]
                one["fill_from_ratio"] = True      # ★ 派生要留痕：界面/统计知道这是同源补的
            base.update(one)
    elif isinstance(results, list):
        base["reason"] = "画面里没有候选"        # 空结果 ≠ 读不到（见上面注释）
    # tracker 只取"相机自己怎么说这一帧"的两个小字段（数字/布尔，无体积风险）
    trk = payload.get("tracker")
    if isinstance(trk, dict):
        stt = _text(trk.get("state"), 16)
        if stt:
            base["tracker_state"] = stt
    return base, False


def sample_from(frames: Optional[List[Any]], vote: Optional[Dict[str, Any]],
                meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """纯函数：frames/vote/meta → 一行样本（**不碰数据库、不发事件、不抛异常**）。

    ★ 单独拆出来是为了"入库与否"与"怎么组装特征"能各自被钉住（test_color_advice.py）。
    """
    v: Dict[str, Any] = dict(vote or {}) if isinstance(vote, dict) else {}
    m: Dict[str, Any] = dict(meta or {}) if isinstance(meta, dict) else {}

    feats: List[Dict[str, Any]] = []
    failed = 0
    for f in (frames or []):
        one, bad = frame_features(f)
        if bad:
            failed += 1
            continue
        if one is not None:
            feats.append(one)

    placeholder = bool(v.get("placeholder"))
    vote_ok = bool(v.get("ok")) and not placeholder
    valid = m.get("valid_colors")
    valid_s = ",".join([_text(x, 8) for x in valid if _text(x, 8)]) if isinstance(valid, (list, tuple)) else ""
    src = _text(m.get("source") or "sorting", 16)
    if src not in SOURCES:
        src = "sorting" if src == "" else _text(src, 16)
    outcome = _text(m.get("outcome") or OUTCOME_UNREVIEWED, 16)
    if placeholder:
        # ★ 硬边界 3：占位色一律"未判色"，不接受调用方把它说成已判
        outcome = OUTCOME_NOT_JUDGED
    if outcome not in OUTCOMES:
        outcome = OUTCOME_UNREVIEWED
    at = _num(m.get("at"))
    dt = (datetime.fromtimestamp(at, tz=timezone.utc) if at else datetime.now(timezone.utc))

    reason = _text(v.get("reason") or "", 128)
    note = _text(m.get("note") or "", 255)
    if placeholder:
        note = (NOTE_NOT_JUDGED + ("；" + note if note else ""))[:255]
        if not reason:
            reason = "未判色（空跑）"

    return {
        "dt": dt,
        "cell": _text(m.get("cell_id") or "", 16),
        "color": _text(v.get("color") or "", 16),
        "hex": _text(m.get("hex") or "", 9),
        "conf": _num(v.get("conf")) or 0.0,
        "valid_colors": valid_s[:128],
        "vote_ok": 1 if vote_ok else 0,
        "placeholder": 1 if placeholder else 0,
        "votes": json.dumps(v.get("votes") or {}, ensure_ascii=False)[:4000],
        "reason": reason,
        "frames": json.dumps(feats, ensure_ascii=False),
        "frame_count": len(feats),
        "failed_frames": failed,
        "outcome": outcome,
        "true_color": _text(m.get("true_color") or "", 16),
        "source": src,
        "stage": _text(m.get("stage") or "判色", 32),
        "thr_snapshot": json.dumps(snapshot(), ensure_ascii=False),
        "note": note,
    }


def ingest(frames: List, vote: dict, meta: Optional[dict] = None) -> dict:
    """★ 固定签名（接线方按这个调，**不许改**）：登记一次判色的样本环。

    返回 `{"ok", "inserted", "failed_frames", "reason"}`；**任何异常都不抛**
    （`_hold_record` 同一条纪律：观测件坏了不许带走检主线）。
    """
    try:
        data = sample_from(frames, vote, meta)
        from app.db.crud import insert_vision_sample
        from app.db.database import SessionLocal
        db = SessionLocal()
        try:
            row = insert_vision_sample(db, data)
        finally:
            db.close()
        if row is None:
            return {"ok": False, "inserted": 0,
                    "failed_frames": int(data["failed_frames"]), "reason": "写库未返回记录"}
        return {"ok": True, "inserted": 1,
                "failed_frames": int(data["failed_frames"]), "reason": ""}
    except Exception as e:                       # noqa: BLE001 —— 观测件绝不带崩主控
        log.debug("判色样本登记失败（已忽略）: %s", e)
        failed = 0
        try:
            for f in (frames or []):
                if not isinstance(f, dict) or not f:
                    failed += 1
        except Exception:                        # noqa: BLE001
            failed = 0
        return {"ok": False, "inserted": 0, "failed_frames": failed,
                "reason": "样本环登记失败（已忽略，不影响判色主线）：%s" % _text(e, 96)}
