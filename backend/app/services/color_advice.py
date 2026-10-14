# -*- coding: utf-8 -*-
"""★ E4「现场自学习判色闭环」的**建议派生**（纯函数层，唯一实现）。

数据只来自 `vision_samples` 样本环：逐帧特征 + 投票明细 + 阈值快照 + 人工标签。
输出是**提案**（proposals），不是配置：

    证据 → 建议 → **人确认** → **现场真图回放** → 人走既有下发通道 → 生效

★★ 四条硬边界（方案 §E4「不做什么」+ AGENTS.md §5.4，逐条都有对应回归钉）：

  1. **n 不足（默认 <20）不给建议**，并如实说明缺多少条（`missing`）。
     小样本给出的"分布"全是噪声，拿它调阈值比不调更糟。
  2. **硬安全相关阈值绝不进建议面**：黑暗判据 `dark_chroma_max` / `dark_s_max`
     只能人显式改并留档（它们是"黑 = 无效色 → 放回"的判据，动它等于动安全口径）。
     ⇒ 这里用**白名单**（`ADVISABLE_KEYS`）而不是黑名单：结构上就发不出别的键。
  3. **不自动下发**：本模块只读样本 + 算数字；`apply` 那一步在 `api/vision.py` 里
     **不存在** —— 生效只能人显式走既有通道 `POST /api/vision/config`，
     而改阈值前**必须先跑** `camera/tools/vision_e2e_replay.py`（现场真图回放）。
     理由（§5.4 原话）：判色阈值只能按现场实测值定，不能按合成图定 ——
     合成图 fill≈0.95+，现场真矩形只有 0.67~0.75。
  4. **橙/黄/青/紫的"未标定"不许被学习自动填上**：`calib_state` 只有两个来源 ——
     现场实测过色卡的色（红/绿/蓝）与"未标定"；**样本数再多也不会翻成已标定**，
     真要标定走既有的实物标定（`POST /api/vision/calibrate/sample`，人显式做、留档）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 口径常量
MIN_SAMPLES = 20          # ★ 方案 §E4：n 不足（<20）就不给建议
MIN_REVIEWED = 5          # 需要人工标签的规则至少要有几条复核样本
BLOCK_MARGIN = 0.02       # 放宽形状门槛时留的余量（贴着观测最大值 = 下次照样掉线）
FILL_GAP_MIN = 0.05       # 判对/判错两组的 fill 中位差至少这么多才值得提软提示线

# ★★ 建议面白名单：**只有这三个键**可以被提出来（结构上挡掉一切其它键，见 docstring 边界 2）。
#   · fill_min  = 能否给出颜色结论的矩形硬门槛（现场实测最低 0.67，出厂 0.55）
#   · fill_warn = 软提示线（不拦检、只写 reason 提醒复核）—— 风险最低，主要建议项
#   · min_conf  = 三色置信度门（在 /vision/config 顶层，不在 thr 表里）
ADVISABLE_KEYS: Tuple[str, ...] = ("fill_min", "fill_warn", "min_conf")

# ★★ 硬安全相关：绝不进建议面（方案 §E4「不做什么」点名的一条）。
#   这条清单同时用于回归钉 —— 建议输出里出现任何一条都算失败。
HARD_EXCLUDED_KEYS: Tuple[str, ...] = ("dark_chroma_max", "dark_s_max")
#   黑暗判据一族（含这三条）整体排除：它们共同决定"黑 = 无效色 → 放回"，不可学习放宽。
HARD_KEY_PREFIXES: Tuple[str, ...] = ("dark_",)

# 出厂默认值的**只读镜像**（唯一事实来源仍是 camera/vision_color.py::DEFAULT_THR）。
# ★ 为什么镜像而不是 import：后端进程不能去 import 相机侧模块（那个 venv 才有 cv2/numpy，
#   且相机服务是**独立进程**）。镜像漂移由 tests/test_color_advice.py 的
#   `test_advice_mirror_matches_camera_defaults` 逐字钉住 —— 改 DEFAULT_THR 不镜像会红。
DEFAULTS_MIRROR: Dict[str, float] = {"fill_min": 0.55, "fill_warn": 0.72}

CALIBRATED_BY_FIELDWORK: Tuple[str, ...] = ("红", "绿", "蓝")   # 现场实测过色带的三色
UNCALIBRATED_COLORS: Tuple[str, ...] = ("橙", "黄", "青", "紫")  # ★ 永久"未标定"（学习不填）
SEVEN_COLORS: Tuple[str, ...] = ("红", "橙", "黄", "绿", "青", "蓝", "紫")

CALIB_LABEL = {
    "calibrated": "已标定（现场实测过）",
    "uncalibrated": "未标定（不许被学习自动填上）",
    "unknown": "未知（不在七色集内）",
}

E2E_REPLAY = "camera/tools/vision_e2e_replay.py"
E2E_CMD = "cd camera && %CAM_PY% tools\\vision_e2e_replay.py 40"
APPLY_CHANNEL = "POST /api/vision/config（相机阈值面板 —— 人工显式点，本卡不代做）"

NOTE_SURFACE = ("建议只是**提案**：落库不等于生效。生效必须人先跑现场真图回放"
                "（camera/tools/vision_e2e_replay.py），确认三色检出率不掉，"
                "再自己到相机阈值面板走既有下发通道。"
                "硬安全相关阈值（dark_chroma_max / dark_s_max 一族黑暗判据）不进本建议面；"
                "n<20 的色一律不给建议；橙/黄/青/紫样本不足时永久显示「未标定」，"
                "不会被学习自动填上。")

REQUIRES = {
    "e2e_replay": E2E_REPLAY,
    "e2e_cmd": E2E_CMD,
    "apply_channel": APPLY_CHANNEL,
    "manual_confirm": True,
}


# ---------------------------------------------------------------- 小工具（纯数字）
def _num(v: Any) -> Optional[float]:
    """脏值/None → None（**不是 0** —— 0 是真实测量值，会进统计）。"""
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def percentile(vals: Sequence[float], p: float) -> Optional[float]:
    """线性插值分位（纯 python，无 numpy 依赖）。空序列 → None（不是 0）。"""
    xs = sorted(float(x) for x in (vals or []))
    if not xs:
        return None
    if len(xs) == 1:
        return round(xs[0], 4)
    k = max(0.0, min(1.0, float(p))) * (len(xs) - 1)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    frac = k - lo
    return round(xs[lo] * (1 - frac) + xs[hi] * frac, 4)


def _stats(vals: Sequence[float]) -> Optional[Dict[str, Any]]:
    xs = [float(x) for x in (vals or []) if _num(x) is not None]
    if not xs:
        return None
    return {"n": len(xs), "min": round(min(xs), 4), "p10": percentile(xs, 0.10),
            "p50": percentile(xs, 0.50), "p90": percentile(xs, 0.90),
            "max": round(max(xs), 4),
            "mean": round(sum(xs) / len(xs), 4)}


def current_value(key: str, snap: Optional[Dict[str, Any]] = None) -> Tuple[Optional[float], str]:
    """现场**生效值**（三态）：值 + 来源状态 camera|manual|unknown。

    ★ `min_conf` 在相机状态顶层、其余在 `thr` 子表里 —— 这一层差异只在这里处理一次。
    ★ 拿不到就是 (None, "unknown")，**绝不**用出厂默认冒充现场生效值（§5.5 铁律③）。
    """
    s = snap or {}
    src = str(s.get("state") or "unknown")
    if src not in ("camera", "manual"):
        return None, "unknown"
    if key == "min_conf":
        return _num(s.get("min_conf")), src
    return _num((s.get("thr") or {}).get(key)), src


def _thr_of(sample: Dict[str, Any]) -> Dict[str, Any]:
    """样本自带的阈值快照（ingest 当时缓存的那一份；可能是 unknown）。"""
    raw = sample.get("thr_snapshot")
    if isinstance(raw, dict):
        return raw
    try:
        j = json.loads(str(raw or "{}"))
        return j if isinstance(j, dict) else {}
    except Exception:                            # noqa: BLE001
        return {}


def _frames_of(sample: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = sample.get("frames")
    if isinstance(raw, list):
        return [f for f in raw if isinstance(f, dict)]
    try:
        j = json.loads(str(raw or "[]"))
        return [f for f in j if isinstance(f, dict)] if isinstance(j, list) else []
    except Exception:                            # noqa: BLE001
        return []


def label_of(sample: Dict[str, Any]) -> str:
    """这条样本的"真色"标签（人工复核回流才是唯一权威）。

    返回 "" = **还没有标签**（未复核 / 放回 / 占位）→ 只进样本数，绝不进误判统计。
    ★ 未复核不等于判对：把"没人说过"当成"通过"就是 §5.5 铁律③禁止的塌布尔。
    """
    if int(sample.get("placeholder") or 0):
        return ""
    outcome = str(sample.get("outcome") or "")
    if outcome == "corrected":
        return str(sample.get("true_color") or "")
    if outcome == "confirmed":
        return str(sample.get("color") or "")
    return ""


# ---------------------------------------------------------------- 逐色统计
def color_stats(color: str, rows: List[Dict[str, Any]], min_samples: int = MIN_SAMPLES) -> Dict[str, Any]:
    """一个色的样本统计（纯函数）。`rows` 只喂**与该色相关**的样本（见 `advice()`）。"""
    n = len(rows)
    reviewed = 0
    misjudged = 0
    confs: List[float] = []
    fills_ok: List[float] = []
    fills_bad: List[float] = []
    blocked_fills: List[float] = []      # 被 fill_min 拒掉、事后确认"那格确实有这个色"
    no_candidate = 0                     # 画面里没有候选（物体超出画面那类）
    failed_frames = 0                    # 这一帧没读到（传输失败那类）
    for r in rows:
        lab = label_of(r)
        if lab:
            reviewed += 1
            if lab != color:
                misjudged += 1
            else:
                c = _num(r.get("conf"))
                if c is not None:
                    confs.append(c)
                for f in _frames_of(r):
                    fv = _num(f.get("fill"))
                    if fv is not None:
                        fills_ok.append(fv)
        for f in _frames_of(r):
            if lab == color and "非矩形" in str(f.get("reason") or ""):
                fv = _num(f.get("fill"))
                if fv is not None:
                    blocked_fills.append(fv)
        if lab and lab != color:
            for f in _frames_of(r):
                fv = _num(f.get("fill"))
                if fv is not None:
                    fills_bad.append(fv)
                c = _num(f.get("conf"))
                if c is not None:
                    confs.append(c)
        no_candidate += sum(1 for f in _frames_of(r)
                            if not f.get("has_target") and str(f.get("reason") or ""))
        failed_frames += int(r.get("failed_frames") or 0)

    enough = n >= int(min_samples)
    return {
        "color": color,
        "n": n,
        "reviewed": reviewed,
        "misjudged": misjudged,
        "enough": bool(enough),
        "missing": max(0, int(min_samples) - n),        # ★ 缺多少条，如实说
        "conf_stats": _stats(confs),
        "fill_stats": _stats(fills_ok),
        "fill_ok_p50": percentile(fills_ok, 0.50),
        "fill_bad_p50": percentile(fills_bad, 0.50),
        "blocked_fills": [round(x, 4) for x in sorted(set(blocked_fills))][:8],
        # ★ 两类"没检测到"必须分开（方案 §5.4：不许把它们混成一个数）
        "no_candidate_frames": no_candidate,
        "failed_frames": failed_frames,
        "calib_state": calib_state(color),
        "calib_label": CALIB_LABEL.get(calib_state(color), CALIB_LABEL["unknown"]),
        "_fills_ok": fills_ok, "_fills_bad": fills_bad, "_blocked": blocked_fills,
    }


def calib_state(color: str) -> str:
    """标定三态（唯一实现）。

    ★★ **样本数不参与**：learning 永远不能把"未标定"翻成"已标定"（方案 §E4 第 4 条）。
      真要标定 = 人显式做实物标定（`POST /api/vision/calibrate/sample`）并留档。
    """
    if color in CALIBRATED_BY_FIELDWORK:
        return "calibrated"
    if color in UNCALIBRATED_COLORS:
        return "uncalibrated"
    return "unknown"


# ---------------------------------------------------------------- 建议项
def _item(key: str, color: str, to: float, st: Dict[str, Any],
          snap: Optional[Dict[str, Any]], why: str, risk: str,
          digits: int = 3) -> Dict[str, Any]:
    cur, state = current_value(key, snap)
    default = DEFAULTS_MIRROR.get(key)
    to_r = round(float(to), digits)
    actionable = cur is not None
    if actionable:
        text = "%s：`%s` %s→%s，样本 n=%s，误判 %s 次" % (
            color, key, _fmt(cur), _fmt(to_r), st["n"], st["misjudged"])
    else:
        text = ("%s：`%s` →%s（现场生效值未知：相机服务没回显阈值 → 只给方向、不给 diff，"
                "核对后再落数值）" % (color, key, _fmt(to_r)))
    return {
        "key": key,
        "color": color,
        "from": cur,                        # None = 现场生效值未知（三态，不塌成默认值）
        "from_state": state,
        "factory_default": default,
        "to": to_r,
        "n": st["n"],
        "reviewed": st["reviewed"],
        "misjudged": st["misjudged"],
        "actionable": bool(actionable),
        "applied": False,                   # ★ 永远是 False：本模块没有任何下发能力
        "text": text,
        "why": why,
        "risk": risk,
    }


def _fmt(v: Any) -> str:
    n = _num(v)
    if n is None:
        return "未知"
    return ("%.3f" % n).rstrip("0").rstrip(".") if n < 1 else "%.3f" % n


def _suggest_for_color(st: Dict[str, Any], snap: Optional[Dict[str, Any]],
                       min_samples: int, min_reviewed: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """一个色的建议（纯规则）。返回 (建议项, **未给建议的如实原因**)。

    ★ 顺序即优先级：先门槛（n / 复核数），后规则 —— 任何一条不满足就**只出原因不出建议**。
    """
    reasons: List[str] = []
    out: List[Dict[str, Any]] = []
    if not st["enough"]:
        # ★ 方案 §E4 点名的一条：n 不足就不给建议，并说清缺多少
        reasons.append("%s：样本 n=%s 不足 %s 条 → 不给建议（还差 %s 条）"
                       % (st["color"], st["n"], min_samples, st["missing"]))
        return out, reasons
    if st["reviewed"] < int(min_reviewed):
        reasons.append("%s：样本够（n=%s）但**人工复核**只有 %s 条（需 ≥%s）→ 误判率无从算起，不给建议"
                       % (st["color"], st["n"], st["reviewed"], min_reviewed))
        return out, reasons

    color = st["color"]
    cur_fill_min, _ = current_value("fill_min", snap)
    ref_fill_min = cur_fill_min if cur_fill_min is not None else DEFAULTS_MIRROR.get("fill_min")

    # R1 —— fill_min：被形状硬门槛拒掉、事后确认那格真有这个色 → 建议放宽
    blocked = st.get("_blocked") or []
    if blocked and ref_fill_min is not None:
        hi = max(blocked)
        if hi < float(ref_fill_min):
            to = round(min(1.0, hi + BLOCK_MARGIN), 2)
            if 0.0 < to < float(ref_fill_min):
                out.append(_item("fill_min", color, to, st, snap,
                                 why="该色有 %d 帧填充率低于门槛被拒（非矩形），但人工确认那一格里确实是"
                                     "%s（实测最高 %.2f）→ 门槛至少要比它低 %s"
                                     % (len(blocked), color, hi, _fmt(BLOCK_MARGIN)),
                                 risk="放宽形状门槛 = 让更多「遮挡/非矩形」帧给出颜色结论，"
                                      "必须用现场真图回放核对三色检出率与误判率都不掉",
                                 digits=2))

    # R2 —— fill_warn：判错那组明显比判对那组"不像矩形" → 提软提示线（不拦检，风险最低）
    ok_p50, bad_p50 = st.get("fill_ok_p50"), st.get("fill_bad_p50")
    if (ok_p50 is not None and bad_p50 is not None and st["misjudged"] >= 1
            and float(ok_p50) - float(bad_p50) >= FILL_GAP_MIN):
        to = round((float(ok_p50) + float(bad_p50)) / 2.0, 2)
        if 0.0 < to < 1.0:
            out.append(_item("fill_warn", color, to, st, snap,
                             why="判对样本 fill 中位 %s、判错样本 %s（差 ≥%s）→ 把软提示线放到两值之间，"
                                 "这批低质量帧会被标成「杂色（建议复核）」"
                                 % (_fmt(ok_p50), _fmt(bad_p50), _fmt(FILL_GAP_MIN)),
                             risk="软提示线不拦检、只写 reason → 不会改变判色结论，风险最低；"
                                  "但它也不会自己修好误判，真收紧还得人改 fill_min/min_conf"))

    # R3 —— min_conf：判错那组置信度明显低于判对那组 → 建议把置信门提上去
    confs_ok = [c for c in (st.get("_confs_ok") or [])]
    confs_bad = [c for c in (st.get("_confs_bad") or [])]
    if len(confs_ok) >= 2 and len(confs_bad) >= 2:
        ok_p10 = percentile(confs_ok, 0.10)
        bad_p90 = percentile(confs_bad, 0.90)
        if ok_p10 is not None and bad_p90 is not None and float(bad_p90) < float(ok_p10):
            to = round((float(bad_p90) + float(ok_p10)) / 2.0, 2)
            if 0.0 < to < 1.0:
                out.append(_item("min_conf", color, to, st, snap,
                                 why="判错样本的置信度整段低于判对样本（错 p90=%s < 对 p10=%s）"
                                     "→ 门放到两值之间能挡住这一族误判"
                                     % (_fmt(bad_p90), _fmt(ok_p10)),
                                 risk="上调置信门会让**判不出**变多（→ 更多「原路放回」），"
                                      "节拍变慢；这是有意的保守方向，仍需现场真图回放确认"))
    if not out:
        reasons.append("%s：样本与复核都够（n=%s / 复核 %s），但当前证据没有指向任何一个可建议的阈值"
                       % (color, st["n"], st["reviewed"]))
    return out, reasons


def advice(samples: List[Dict[str, Any]], snap: Optional[Dict[str, Any]] = None,
           min_samples: int = MIN_SAMPLES,
           min_reviewed: int = MIN_REVIEWED) -> Dict[str, Any]:
    """★ 唯一派生入口（**纯函数**）：样本 dict 列表 → 建议面。

    @param samples  每条形如 `vision_samples.sample_from()` 的产物（或 DB 行的 dict 视图）
    @param snap     阈值快照；不传则取**最新一条样本自带的快照**（现场到底用的什么值，
                    以"判色那一刻记下的那份"为准 —— 拿不到就是 unknown，见 `current_value`）
    """
    rows = [dict(s or {}) for s in (samples or []) if isinstance(s, dict)]
    # 占位样本（空跑）不进任何统计 —— 那个色不是判出来的
    rows = [r for r in rows if not int(r.get("placeholder") or 0)]
    if snap is None:
        for r in reversed(rows):
            t = _thr_of(r)
            if t:
                snap = t
                break

    # 按"这条样本属于哪个色"归堆：结论色与人工真色**都算**（误判样本对两个色都是证据）
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        for c in {str(r.get("color") or ""), str(r.get("true_color") or "")}:
            if not c:
                continue
            buckets.setdefault(c, []).append(r)
    for c in SEVEN_COLORS:
        buckets.setdefault(c, [])

    colors_out: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for c in list(SEVEN_COLORS) + [k for k in sorted(buckets) if k not in SEVEN_COLORS]:
        rs = buckets.get(c) or []
        # 置信分布按"结论是该色"的样本算，误判样本另带一组（R3 要用两组的分离度）
        st = color_stats(c, rs, min_samples)
        st["_confs_ok"] = [ _num(x.get("conf")) for x in rs
                            if label_of(x) == c and _num(x.get("conf")) is not None]
        st["_confs_bad"] = [_num(x.get("conf")) for x in rs
                            if label_of(x) and label_of(x) != c]
        got, why_not = _suggest_for_color(st, snap, min_samples, min_reviewed)
        st["advice"] = got
        st["no_advice_reason"] = why_not
        # 内部中间量不上界面（避免"看着有字段其实没人消费"的编故事）
        for k in ("_fills_ok", "_fills_bad", "_blocked", "_confs_ok", "_confs_bad"):
            st.pop(k, None)
        colors_out.append(st)
        items.extend(got)
        skipped.extend(why_not)

    return {
        "ok": True,
        "min_samples": int(min_samples),
        "min_reviewed": int(min_reviewed),
        "samples_total": len(rows),
        "threshold_snapshot": snap or {"state": "unknown", "thr": {}, "min_conf": None},
        "advisable_keys": list(ADVISABLE_KEYS),
        "hard_excluded_keys": list(HARD_EXCLUDED_KEYS),
        "hard_excluded_prefixes": list(HARD_KEY_PREFIXES),
        "uncalibrated_colors": list(UNCALIBRATED_COLORS),
        "calibrated_by_learning": False,      # ★ 永远是 False：学习不填标定
        "colors": colors_out,
        "items": items,
        "no_advice_reasons": skipped,
        "requires": dict(REQUIRES),
        "applied": False,                     # ★ 建议面本身从不生效
        "note": NOTE_SURFACE,
    }


def summarize(out: Dict[str, Any]) -> str:
    """提案摘要（人看的中文一行，含"必须先跑 e2e 回放"）。"""
    items = out.get("items") or []
    if not items:
        return "没有可提案的建议（样本不足或证据不指向任何阈值）"
    parts = ["%s %s→%s" % (it["key"], _fmt(it["from"]), _fmt(it["to"])) for it in items[:6]]
    return ("建议 %d 项：%s ｜ 生效前必须先跑现场真图回放 %s，再由人走既有下发通道 %s"
            % (len(items), "；".join(parts), E2E_REPLAY, APPLY_CHANNEL))


def diff_rows(out: Dict[str, Any]) -> List[Dict[str, Any]]:
    """落库的 diff（只收建议项；`applied` 一律 False，结构上不给"已生效"留位置）。"""
    rows: List[Dict[str, Any]] = []
    for it in (out.get("items") or []):
        if it.get("key") not in ADVISABLE_KEYS:        # ★ 兜底闸：白名单外的键一律不落库
            continue
        r = dict(it)
        r["applied"] = False
        rows.append(r)
    return rows
