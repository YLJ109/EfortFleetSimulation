# -*- coding: utf-8 -*-
"""★★ E7「一格三证」：同一个料格的 **照片 / 判色结论 / 机器人位姿** 用同一个 `cellId` 装订。

方案 §E7 的落点：`ArrivalCheck` 那份"到位自证"数据**早就有**（`services/twin_residual.py`），
判色结论也早就有（`services/sorting.py::vote_color` 的 `color/conf/votes/frame_detail`），
照片路径也早就有（相机服务落档的 `vision_records.images`）—— **缺的从来不是数据，是"装订"**：
三样各存各的，事后没法一眼回答「B3 这一格当时看到的到底是什么、机器人真到了没有」。

★ 这个模块只做两件事（刻意很小）
  ① **唯一派生 `cell_id()`**：`B3` / `B3上`（正上位）→ 同一个 `B3`；`C检测` → `C`；
     其它点位（起点 / `J6正面` …）不是料格 → 返回空串并由调用方**如实跳过装订**。
     ★ 全项目只允许这一处派生格号（过去 `t + "上"` 这类字符串拼法散在 4 个地方，
       拼出来的"格子"彼此对不上 —— 那正是"三证凑不齐"的根因）。
  ② **纯装订 `bind()`**：把三样证据装成一条记录，**缺哪一样就写缺哪一样 + 为什么缺**。

★ 硬边界（改动前先读）
  · **绝不在分拣热路径里新增拍摄/新增移动/新增读库**：照片这一证**只引用已经存在的档**
    （相机服务落过的 `images`），拿不到就 `ok=False` + 原因。
    —— 现场相机默认是关的（AGENTS.md §1），"为了凑三证去开相机/补拍一张"会造成新的节拍抖动。
  · **不落 DB、不进分拣线程**：`record()` 只做一次内存 append + 异步 jsonl 落盘
    （`{EFORT_LOGS_DIR}/cells/`，测试隔离三样之一）。
  · **水位必须单调**：`latest_seq` 只增不减（§5.5 铁律①），前端刷新用它，不用 `records.length`。
  · **不裁决、不推断**：本模块不重新判色、不重新判到位、不算毫米/不换算坐标，
    只搬运各权威源的结论并**注明来源**（同一屏同一结论必须同源，§5.5 铁律④）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import logs_dir
from app.core.logger import get_logger

log = get_logger("cell_evidence")

SCHEMA = 1
DIR_TAG = "cells"
RECENT_CAP = 200                     # 内存里留多少条最近的三证（界面复盘够用即可）
_PHOTO_WINDOW_S = 20.0               # 照片档与这条记录的时间容差（超出就不算同一件事）

# 格号的唯一形态：A/B 区 9×2 = 18 个原料位；C 检测点是"判色位"，也算一个可复盘的格子。
_SLOT_RE = re.compile(r"^([AB])([1-9])$")
_ABOVE_RE = re.compile(r"^([AB])([1-9])上$")
_CHECK_RE = re.compile(r"^C(检测|位|区)?$")
# ★ 三证的名字固定成一个元组 —— 界面/测试都按它逐条核对，缺一条就少一条（不许改名）。
EVIDENCE_KEYS: Tuple[str, str, str] = ("photo", "colour", "pose")


# ---------------------------------------------------------------- ① 唯一派生
def cell_id(name: Any) -> str:
    """点位名 → **格号**（`A1`…`A9` / `B1`…`B9` / `C`）；不是料格 → `""`（空串，不编）。

    · `B3` → `B3`（原位）
    · `B3上` → `B3`（**正上位属于同一个格子** —— 进出该格必经它，见
      `services/sorting.py::raw_slot_safe_path`）
    · `C检测` / `C位` / `C区` → `C`
    · `开始/结束点` / `J6正面` / `J6反面` / 任何未知名 → `""`
    """
    s = str(name or "").strip().upper()
    if not s:
        return ""
    m = _SLOT_RE.match(s)
    if m:
        return "%s%s" % (m.group(1), m.group(2))
    m = _ABOVE_RE.match(s)
    if m:
        return "%s%s" % (m.group(1), m.group(2))
    if _CHECK_RE.match(s):
        return "C"
    return ""


def is_cell(name: Any) -> bool:
    return bool(cell_id(name))


def cell_of_point(name: Any) -> str:
    """别名（读代码时更直白）：与 `cell_id` 同一个实现，**没有第二套规则**。"""
    return cell_id(name)


# ---------------------------------------------------------------- ② 装订（纯函数）
def _why_absent(given: bool, empty: bool) -> Tuple[bool, str]:
    """一份证据"到手了吗"：调用方**根本没给**（`None`）与"给了但是空的"是两种缺法，
    原因要分开写 —— 前者通常是"这条流程没有这一步"，后者是"采集到了但没有内容"。"""
    if not given:
        return False, "调用方没给这一证（未采集 / 该流程没有这一步）"
    if empty:
        return False, "给了但是空的（采集到了却没有内容）"
    return True, ""


def bind(cell: str,
         photo: Optional[Dict[str, Any]] = None,
         colour: Optional[Dict[str, Any]] = None,
         pose: Optional[Dict[str, Any]] = None,
         at: Optional[float] = None,
         extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把三证装成一条记录。**缺就说缺**，并把"为什么缺"原样带上（不静默省略）。

    每份证据的约定形状（调用方负责填权威源的值，本函数不改写数字）：
      · `photo`  `{"ok", "images": {"full": "rel", ...}, "source", "reason"}`
      · `colour` `{"ok", "color", "conf", "votes", "frames", "source", "reason"}`
      · `pose`   `{"ok", "live": [6], "target": [6], "residual": {...}, "source", "reason"}`
    ★ `ok` **只认调用方明确写下的结论**：没写 `ok` 一律算"没有这一证"（§5.5 铁律③：
      不知道就说不不知道，**不许**因为"看起来有内容"就盖成到手）。
    """
    out: Dict[str, Any] = {}
    missing: List[str] = []
    for key, given in (("photo", photo), ("colour", colour), ("pose", pose)):
        row = dict(given) if given is not None else {}
        has, why = _why_absent(given is not None, not row)
        # ★ `ok` 缺失时**不猜**：只有调用方明确写了 ok=True 才算到手（§5.5 铁律③）
        ok = bool(row.get("ok")) and has
        row["ok"] = ok
        row.setdefault("source", "")
        if not ok:
            row.setdefault("reason", row.get("reason") or why or "这一证没有拿到")
            missing.append(key)
        else:
            row.setdefault("reason", "")
        out[key] = row
    return {
        "schema": SCHEMA,
        "cell_id": str(cell or ""),
        "at": float(at if at is not None else time.time()),
        "evidence": out,
        "complete": not missing,
        "missing": missing,
        "extra": dict(extra or {}),
    }


# ---------------------------------------------------------------- 落盘（异步，不进分拣线程）
_lock = threading.Lock()
_recent: List[Dict[str, Any]] = []
_seq = 0
_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _next_seq() -> int:
    """★ 只增不减的水位（进程内计数；重启后从 0 重新开始是**有意的** ——
       前端把它当"本次会话内的水位"用，跨会话复盘按 `at` 排序，不混用）。"""
    global _seq
    _seq += 1
    return _seq


def base_dir() -> str:
    d = os.path.join(logs_dir(), DIR_TAG)
    os.makedirs(d, exist_ok=True)
    return d


def _day_file() -> str:
    return os.path.join(base_dir(),
                        "cells-%s.jsonl" % datetime.now(timezone.utc).strftime("%Y%m%d"))


def record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """登记一条三证（**不阻塞**：写盘交给后台线程，分拣线程只做一次 append）。"""
    if not isinstance(rec, dict):
        return {"ok": False, "queued": False, "reason": "记录形状不对，没登记"}
    with _lock:
        rec["seq"] = _next_seq()
        _recent.append(rec)
        if len(_recent) > RECENT_CAP:
            del _recent[:len(_recent) - RECENT_CAP]
    _ensure_worker()
    _queue.put(rec)
    return {"ok": True, "queued": True, "seq": rec["seq"]}


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_drain, name="cell-evidence-writer", daemon=True)
        _worker.start()


def _drain() -> None:
    while True:
        try:
            rec = _queue.get(timeout=30.0)
        except queue.Empty:
            return                        # 空闲 30s 退出，需要时懒启动
        try:
            with open(_day_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:            # 落盘失败不能带崩流程，但必须留下可见的痕迹
            log.warning("一格三证落盘失败（seq=%s）：%s", rec.get("seq"), e)


# ---------------------------------------------------------------- 查询（只读）
def latest_seq() -> int:
    with _lock:
        return _seq


def reset() -> None:
    """测试夹具用：清掉进程内的三证历史与水位（**不动已经落盘的 jsonl**）。

    ★ 为什么必须有：`_recent` 有上限（`RECENT_CAP`）—— 用例之间不清就会串味，
      而"用 `len(_recent)` 判断有没有新记录"正是 §5.5 铁律① 钉死过的错法。
    """
    global _seq
    with _lock:
        _recent.clear()
        _seq = 0
    while not _queue.empty():
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            break


def recent(limit: int = 50, cell: str = "") -> List[Dict[str, Any]]:
    with _lock:
        rows = list(_recent)
    if cell:
        rows = [r for r in rows if r.get("cell_id") == cell]
    return list(reversed(rows[-max(1, int(limit)):]))


def stats() -> Dict[str, Any]:
    """三证齐全度（界面只渲染这些数字，**不自己再判一遍**）。"""
    with _lock:
        rows = list(_recent)
    by: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cid = str(r.get("cell_id") or "")
        if not cid:
            continue
        b = by.setdefault(cid, {"cell_id": cid, "n": 0, "complete": 0,
                                "missing": {k: 0 for k in EVIDENCE_KEYS}})
        b["n"] += 1
        if r.get("complete"):
            b["complete"] += 1
        for k in (r.get("missing") or []):
            if k in b["missing"]:
                b["missing"][k] += 1
    return {"total": len(rows), "cells": sorted(by.values(), key=lambda x: x["cell_id"])}


# ---------------------------------------------------------------- 照片档：只引用"已经存在的档"
# 到位自证里**要带进三证**的字段（曲线 `profile` 不带 —— 一条三证里塞 240 个点没必要，
# 复盘要看曲线直接查 `/api/twin/residual`，同源不复制）。
RESIDUAL_KEYS = ("id", "source", "target", "verdict", "closed", "end_reason",
                 "tol_deg", "band_deg", "uncertainty_deg", "peak_deg", "first_deg",
                 "last_deg", "moved", "settled", "settle_ms", "elapsed_ms", "samples")


def residual_for(source: str,
                 rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """从**到位自证（E2）**的历史里挑出"走到 `source` 这一段"的观测（按点名对上）。

    ★ 用**点名**而不是段号：三证是在"放完这一件"那一刻装订的，那一刻要回答的是
      "刚刚去 `B3上` 那一段到了没有" —— 段号是 E2 的内部计数，调用方不该记。
    ★ 只取**最近一条同名段**（`twin_residual.recent` 已经是新的在前）。
    · 没找到 / 观测关闭 / 空跑（根本没下发所以根本没有段）→ `{}`，
      由 `pose_evidence` 如实写成"这一证没有"（**不拿"没观测"冒充"到了"**）。
    """
    try:
        from app.services.twin_residual import twin_residual     # 延迟导入，避免环状依赖
        pool = rows if rows is not None else twin_residual.recent(12)
    except Exception as e:                                       # noqa: BLE001
        log.debug("到位自证读取失败（当作没有这一证）：%s", e)
        return {}
    want = str(source or "")
    for seg in pool or []:
        if isinstance(seg, dict) and str(seg.get("source") or "") == want:
            return {k: seg.get(k) for k in RESIDUAL_KEYS if k in seg}
    return {}


def photo_evidence(images: Optional[Dict[str, Any]] = None,
                   rec_at: Optional[float] = None,
                   at: Optional[float] = None,
                   source: str = "camera_service:vision_records.images",
                   reason: str = "") -> Dict[str, Any]:
    """把"已有的照片档"装成一份证据；拿不到就 `ok=False` + 原因（**不为了凑齐去补拍**）。

    ★ `reason`：调用方**知道真正原因**时如实传进来（例如"本流程的判色走 `/vision/detect`
      一次性返回、根本不归档"）—— 不传才用兜底文案，免得把"这条链路没归档"说成"相机没开"。
    """
    now = float(at if at is not None else time.time())
    imgs = {k: v for k, v in (images or {}).items() if v}
    if not imgs:
        return {"ok": False, "images": {}, "source": source,
                "reason": reason or
                          "这一判色没有留档图片（相机未开启 / 该帧未归档）—— "
                          "**没有补拍**，三证里照片这一条缺"}
    age = None if rec_at is None else now - float(rec_at)
    if age is not None and age > _PHOTO_WINDOW_S:
        return {"ok": False, "images": imgs, "source": source,
                "reason": "最近的档与这条记录相差 %.1fs（>%ss），不算同一件事，不硬凑" % (
                    age, int(_PHOTO_WINDOW_S))}
    return {"ok": True, "images": imgs, "source": source, "age_s": age, "reason": ""}


def colour_evidence(v: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """判色结论证据：**原样**搬运 `services/sorting.py::vote_color` 的结论（不改写）。

    ★★ `placeholder`（空跑占位色）必须单独处理：空跑根本没送检，那个色是**编给流程走的**，
      拿来当"这一格判成了红色"的证据就是假显示 —— 一律 `ok=False` 并如实写明是占位色。
    """
    d = dict(v or {})
    if not d:
        return {"ok": False, "source": "services/sorting.py::vote_color",
                "reason": "这一格没有判色（流程没走送检）"}
    if d.get("placeholder"):
        return {"ok": False, "color": d.get("color") or "", "placeholder": True,
                "conf": d.get("conf"), "votes": d.get("votes") or {},
                "source": "services/sorting.py::vote_color",
                "reason": "空跑占位色（本轮没有真的判色）→ 不拿占位色当这一格的判色证据"}
    ok = bool(d.get("ok"))
    return {
        "ok": ok,
        "color": d.get("color") or "",
        "conf": d.get("conf"),
        "votes": d.get("votes") or {},
        "frames": d.get("frames"),
        "valid": d.get("valid"),
        "reason": (d.get("reason") or "") if not ok else "",
        "source": "services/sorting.py::vote_color",
    }


def pose_evidence(live: Optional[List[float]] = None,
                  target: Optional[List[float]] = None,
                  residual: Optional[Dict[str, Any]] = None,
                  simulated: Optional[bool] = None) -> Dict[str, Any]:
    """位姿证据 = 实时关节 + 本次**实发**目标 + 到位自证残差（`services/twin_residual.py`）。

    ★ 期望值必须取 `_move` 返回的**实发目标**（与到位校验同源）；自己按点位表再推一遍
      就是"第二秩序权威"（当年「未到达 C 检测点 偏差 180°」误报的根因）。
    """
    from app.services import sorting as S       # 延迟导入：只在真要算时才用（避免环状依赖）
    row: Dict[str, Any] = {"source": "collector.get_latest + twin_residual（E2 到位自证）"}
    if not live:
        row.update({"ok": False, "reason": "读不到实时姿态 → 位姿这一证**没有**（不猜到位）",
                    "live": None, "target": target, "residual": residual,
                    "simulated": simulated})
        return row
    row["live"] = [round(float(x), 3) for x in live][:6]
    row["simulated"] = simulated
    if target:
        tgt = [float(x) for x in target][:6]
        row["target"] = [round(x, 3) for x in tgt]
        if len(tgt) == 6 and len(row["live"]) == 6:
            # ★ 偏差公式的**唯一实现**在 sorting（J6 按环形差）；这里不另写一遍
            row["dev_max_deg"] = S.joint_deviation(row["live"], tgt)
        else:
            row["dev_max_deg"] = None
    else:
        row["target"] = None
        row["dev_max_deg"] = None
    row["residual"] = residual or {}
    if not residual:
        row["ok"] = False
        row["reason"] = "到位自证没有这一段的观测（残差未采到）→ 位姿只能给姿态、**不能说到到位**"
        return row
    verdict = str((residual or {}).get("verdict") or "")
    row["verdict"] = verdict
    row["ok"] = verdict in ("arrived", "slow")
    if not row["ok"]:
        row["reason"] = ("到位自证结论是「%s」（不是「arrived/慢到」）→ 位姿这一证只能算"
                         "拿到了姿态，**不能说到到位**" % (verdict or "未知"))
    return row
