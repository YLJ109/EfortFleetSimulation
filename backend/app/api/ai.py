# -*- coding: utf-8 -*-
"""AI 执行页后端：自然语言（语音/文字）→ 结构化意图 → **确认后**执行。

方案：docs/方案-点位推导与AI执行页（语音文字控机器人）.md

★ 三条不可动摇的口径：
  1. **大模型不碰控制通道**：它只把话翻成受限 JSON（services/llm.py），
     真正执行的是既有的运动/吸放通道（复用 api/sorting.py 的同一套原语，
     不复制第二份"写目标 + 触发位"实现）。
  2. **不是权限后门**：全部接口挂 `require_control`；围栏互锁、轴锁、速度夹取、
     运行中互锁一个都不少 —— 与手点页面完全同一套。急停同样要令牌。
  3. **回显 + 二次确认**：`/parse` 只解析不执行；`/exec` 必须带同一个 `plan_id`。
     这样"界面显示的"与"真正执行的"不可能不是同一份计划。
     解析不出的指令**直接拒绝并说"没听懂"**，绝不猜。

执行语义（**如实说明的白话口径**）：
  · 只认单件/批量搬运、看颜色、分拣启停、急停（白名单五种，不做更多）。
  · "放到红列" = 该列**第一个空位**；空位来自**本页自己的**占位表（见 `_occ`），
    它**不共享**分拣页那一轮运行的占位表，也**不知道 A/B 区实际里面有什么**。
  · 批量上限默认 3 件（`EFORT_AI_BATCH_MAX` 可调），超过必须在计划里显式确认。
"""
from __future__ import annotations

import os
import random
import threading
import time
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_control
from app.api.control import (new_run, register_scene_source, request_cancel,
                            reject_if_not_ready)
from app.api.sorting import (_Stop, _air, _capture, _joints_of, _move,  # 复用同一套原语
                            _set as _set_state)
from app.core.logger import get_logger
from app.db.crud import list_points
from app.db.database import SessionLocal
from app.services import llm, nl_intent as NL
from app.services import placement as PL
from app.services import sorting as S
from app.services.collector import collector
from app.services.events import emit as emit_event
from app.services.motion import motion, real_write_enabled

log = get_logger("ai")
router = APIRouter(prefix="/api/ai", tags=["ai"])

# 计划缓存：plan_id → 计划（短 TTL，防"确认的和解析的不是同一条"）
_PLAN_TTL = 180.0
# ★ 接管队列的有效期（秒）。超时就不接力 —— 避免"人早就走了/改主意了，
#   几分钟后机器突然自己动起来"（那比不执行更危险）。
_PENDING_TTL = 120.0
_plans: Dict[str, Dict] = {}
_gate = threading.Lock()
_job: Dict[str, object] = {"thread": None, "ctx": None, "state": None}


# ★★ 2026-10-10：把**AI 执行状态**登记为 3D 的"场景状态源"（见 control.py 的
#   `/control/scene-state`）。这就是"AI 执行没有变颜色"的直接修复 ——
#   AI 与分拣的 state 由同一套 `_set_state()` 生成，字段同构，3D 可直接吃。
def _scene_state() -> Optional[Dict]:
    with _gate:
        st = _job.get("state")
    return dict(st) if isinstance(st, dict) else None


register_scene_source("ai", _scene_state)

# ★ 吸取后停留（ms）：吸上后先停一下再抬，等真空吸牢 —— 与分拣同口径。
#   ★★ 2026-10-11：数值**不再各写一份**，直接取工艺时序的**唯一来源** `services/sorting.py`
#     （原来这里与 `api/sorting.py`、`api/tictactoe.py` 三处各写 500，改一处会漏两处）。
#   ★ 2026-10-10 用户口径："吸住要一直吸住，到放料时才停止吸住" ——
#     本链路以前抄的是**分拣的旧写法**（抬起来就「停止吸」+ 没有吸牢停留），
#     件只靠杯内残余负压吊着飞一路；这里与分拣引擎对齐。
SUCK_HOLD_MS = S.SUCK_HOLD_MS
# ★ C 检测位停留（秒）—— 与分拣的 `dwell_ms` 同口径（同为 DWELL_MS，500ms）；
#   其间抓 3 帧（帧间隔见 api/sorting.py 的 FRAME_GAP_S）。
CAM_DWELL_S = S.DWELL_MS / 1000.0

# ★ 本页自己的占位表（A 区是否有料 / B 区各格颜色）—— 见模块头"如实说明"。
#   只在本页会话内有效，重启后端即清空（不假装知道真实格内容）。
_occ: Dict[str, Dict] = {"A": {}, "B": {}}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def batch_max() -> int:
    """批量搬运上限（超过必须显式确认）。"""
    return max(1, min(9, _env_int("EFORT_AI_BATCH_MAX", 3)))


# ★★ 2026-10-10 现场 bug（"「蓝」的摆法里已没有可用格"）：执行阶段要把"整区/颜色列"
#   落成**具体格**，于是重建了一份执行计划 —— 但重建时**只写了动作相关字段**，
#   把解析阶段的附加键全丢了。丢的每一件都会出事故：
#     · placement        → 解析阶段解出来的摆法没了 → 现场报"摆法里已没有可用格"
#                          （其实摆法就在计划里，只是没带下来）
#     · ban              → 禁放格失效（"B5、B9 禁止摆放"被忽略）
#     · on_invalid       → "无效色放回原位**继续**"退回成"停"
#     · pattern          → 日志/回显里看不出按什么约束摆的
#   所以：**重建必须以原计划为底**，只覆盖动作相关字段（唯一入口 exec_plan）。
_EXEC_KEEP = ("ban", "pattern", "on_invalid", "placement", "placement_note",
              "placement_alts", "quotas", "prefer_color")


def exec_plan(plan: Dict, over: Dict) -> Dict:
    """重建**执行计划**：以原计划为底，只覆盖动作相关字段（见 _EXEC_KEEP 的注释）。"""
    out = {k: plan.get(k) for k in _EXEC_KEEP if k in plan}
    out.update(over)
    return out


def _ban_of(plan: Dict) -> Dict:
    """取计划里的**逐格约束**（2026-10-10 用户口径："某格不能拿 / 某格不能放"）。

    缺省 = 没有任何约束（返回空表）→ 分拣/井字棋/普通搬运行为完全不变。
    ★ `have` = 用户说"这些格**已有料**"的那些格（与"不能放"同样不可落料，
      但回显要分开写 —— 现场处置方式不同）。
    """
    b = plan.get("ban") or {}
    up = lambda xs: [str(x).strip().upper() for x in (xs or [])]  # noqa: E731
    return {"from": up(b.get("from")), "to": up(b.get("to")), "have": up(b.get("have"))}


# ---------------------------------------------------------------- 盘点记录（只读查询的数据源）
# ★ 2026-10-10 用户口径："A区有几个盒子、什么颜色、有效的几个、几个红、几个蓝、几个无效颜色"。
#   ★ 硬事实：**相机只对着 C 检测位，照不到 A 区** —— 系统没有"A 区有几个盒子"的直接观测。
#     所以这里只存"**真盘过**"的结果：查询一律标注来源与时间；没盘过就老实说不知道。
_inv: Dict[str, object] = {"at": 0.0, "A": {}}


def _inv_summary(res: Dict[str, str]) -> Dict:
    """统计 {槽位: 颜色} → 总数 / 各色件数 / 无效（判不出）件数。"""
    counts = {"红": 0, "绿": 0, "蓝": 0}
    unknown = 0
    for c in (res or {}).values():
        if c in counts:
            counts[c] += 1
        else:
            unknown += 1
    return {"total": len(res or {}), "counts": counts, "unknown": unknown}


def inv_answer(text: str) -> Optional[str]:
    """按**已盘点的记录**回答；没记录返回 None（调用方给"请先盘点"的引导）。

    ★ 绝不编数量：A 区颜色只能靠逐件拍照得到，没有记录就只能说不知道。
    """
    at = float(_inv.get("at") or 0)
    res = _inv.get("A") or {}
    if not at or not res:
        return None
    s = _inv_summary(res)
    age = int(max(0, time.time() - at))
    head = "据 %d 秒前的盘点记录" % age
    t = text or ""
    ask_n = ("几" in t or "多少" in t)
    if "红" in t and ask_n:
        return "%s：A 区红色 %d 件。" % (head, s["counts"]["红"])
    if "蓝" in t and ask_n:
        return "%s：A 区蓝色 %d 件。" % (head, s["counts"]["蓝"])
    if "绿" in t and ask_n:
        return "%s：A 区绿色 %d 件。" % (head, s["counts"]["绿"])
    if "无效" in t or "判不出" in t or "未知" in t:
        return "%s：A 区无效（判不出色）%d 件。" % (head, s["unknown"])
    if "哪些颜色" in t or "什么颜色" in t or "有什么颜色" in t:
        got = [k for k in ("红", "绿", "蓝") if s["counts"][k]]
        return "%s：A 区有 %s。" % (head, "、".join(got) if got else "（没判出任何有效颜色）")
    return ("%s：A 区共 **%d 件** —— 红 %d、绿 %d、蓝 %d，无效（判不出）%d。"
            % (head, s["total"], s["counts"]["红"], s["counts"]["绿"],
               s["counts"]["蓝"], s["unknown"]))


def _occupied(slots: List[str]) -> List[str]:
    """A 区"认为有料"的格：没记录过的一律当作有料（现场默认就是满的）→ 保序返回。"""
    return [s for s in slots if _occ["A"].get(s, True)]


def _free_b_slots(color: str) -> List[str]:
    """某颜色列里"认为空"的格（按列内顺序）。"""
    return [s for s in NL.color_column_slots(color, "B") if not _occ["B"].get(s)]


def _free_a_slots() -> List[str]:
    return [s for s in ["A%d" % i for i in range(1, 10)] if not _occ["A"].get(s, True)]


def _resolve_loc(loc: Dict) -> Dict:
    """把解析出的地点规范化成**具体格**（槽位/颜色列/整区 → 单格）。

    ★ 所有"选哪一格"的判断都在这里，且**显式失败**（不猜、不越界）。
    """
    kind = loc.get("kind")
    if kind == "slot":
        s = loc.get("slot") or ""
        if s not in {p["name"] for p in S.expected_points()}:
            return {"ok": False, "error": "点位「%s」不在分拣点位表里（拒绝执行）" % s}
        return {"ok": True, "slot": s, "zh": NL.slot_to_zh(s)}
    if kind == "color_col":
        color = loc.get("color")
        col = NL.color_column_slots(color, "B")
        free = _free_b_slots(color)
        if not free:
            return {"ok": False,
                    "error": "B 区「%s」列 3 个槽位已满（按本页占位表判断）" % color}
        return {"ok": True, "slot": free[0], "zh": "%s列第%d个（%s）"
                % (color, col.index(free[0]) + 1, free[0]),
                "slot_zh": NL.slot_to_zh(free[0])}
    if kind == "zone":
        zone = loc.get("zone")
        if zone == "B":
            # 整区放到 B：不知道颜色 → 必须有颜色列（否则无法选槽）
            return {"ok": False,
                    "error": "放到 B 区要指定颜色列（例如「放到B区红列」）—— 颜色要判色后才知道"}
        free = _free_a_slots()
        if not free:
            return {"ok": False, "error": "A 区 9 个槽位都已被占用（按本页占位表判断）"}
        return {"ok": True, "slot": free[0], "zh": "A区第1个空位（%s）" % free[0],
                "slot_zh": NL.slot_to_zh(free[0])}
    return {"ok": False, "error": "地点类型不认识（%s）" % kind}


def _resolve_src(loc: Dict) -> Dict:
    """源点：颜色列/整区 → 该范围内**第一个有料**的格。"""
    kind = loc.get("kind")
    if kind == "slot":
        return _resolve_loc(loc)
    if kind == "color_col":
        return {"ok": False, "error": "取料点要指定具体格或整区，不能按颜色列取（颜色是判出来的）"}
    if kind == "zone":
        zone = loc.get("zone")
        if zone not in ("A", "B"):
            return {"ok": False, "error": "区域只能是 A 或 B"}
        cands = _occupied(["%s%d" % (zone, i) for i in range(1, 10)]) if zone == "A" \
            else [s for s in ["B%d" % i for i in range(1, 10)] if _occ["B"].get(s)]
        if not cands:
            return {"ok": False, "error": "%s 区没有可取的件（按本页占位表判断）" % zone}
        return {"ok": True, "slot": cands[0], "zh": "%s区第1个有料的格（%s）"
                % (zone, cands[0])}
    return {"ok": False, "error": "地点类型不认识（%s）" % kind}


def _solve_quota_placement(plan: Dict) -> Dict:
    """把"各色几件 + 摆放方式 + 已有料/禁放"解成**具体格位**（services/placement.solve）。

    ★ 占用的三个来源合一：
      ① 本条指令里明说"已有料"的格（`ban.have`）；
      ② 本条指令里明说"不能放"的格（`ban.to`）；
      ③ **本页这一轮已经放过的格**（`_occ["B"]`，我方确知的事实）。
    """
    ban = _ban_of(plan)
    placed = [s for s, v in (_occ["B"] or {}).items() if v]
    return PL.solve(plan.get("quotas") or {}, plan.get("pattern") or "any",
                    occupied=list(ban["have"]) + placed, banned=ban["to"])


# ---------------------------------------------------------------- 解析
class ParseIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)


def _echo(intent: str, plan: Dict) -> Dict:
    """把结构化意图翻成"人话回显"（操作员确认的就是这段文字）。"""

    def _loc_zh(loc: Optional[Dict], role: str) -> str:
        loc = loc or {}
        k = loc.get("kind")
        if k == "slot":
            s = loc.get("slot") or ""
            # 解析阶段给的是行/列，执行阶段给的是"红列第1个（B1）"—— 有 zh 就别再叠一层
            if loc.get("zh"):
                return str(loc["zh"])
            return NL.slot_to_zh(s) if s else "?"
        if k == "color_col":
            c = loc.get("color") or "?"
            return "%s区%s列的第一个空位" % (loc.get("zone") or "B", c)
        if k == "zone":
            return "%s区（整区）" % (loc.get("zone") or "?")
        if k == "all":
            return "%s区全部" % (loc.get("zone") or "?")
        return "?"

    a = plan.get("action")
    if a == "estop":
        return {"title": "急停", "detail": "立即急停（最高优先级，不需确认）", "danger": True}
    if a == "sorting":
        zh = {"start": "开始分拣", "stop": "停止执行（软停，不是急停）",
              "pause": "暂停", "resume": "继续"}.get(plan.get("op"), plan.get("op"))
        note = "「停止」= 当前动作结束后停并回起点；急停请明说「急停」"
        if plan.get("op") == "start":
            # ★★ 2026-10-11（方案 §C）：**如实标注本次模式** ——
            #   「正常（一面）」/「快速（双面·一趟两件）」。用户口径："计划卡上如实标本次=正常/快速"。
            if plan.get("dual_face"):
                return {"title": "分拣流程",
                        "detail": "开始分拣（本次 = 快速 · 双面 · 一趟两件：第 1 件正面、第 2 件反面）",
                        "note": note}
            return {"title": "分拣流程",
                    "detail": "开始分拣（本次 = 正常 · 一面，一趟一件）", "note": note}
        return {"title": "分拣流程", "detail": zh, "note": note}
    if a == "inventory":
        return {"title": "盘点 A 区",
                "detail": "逐件 取料 → 送 C 位判色 → **原格放回**（不搬运、不改变 A 区）",
                "note": "属运动：每件走一趟；跑完会说清「共几件 / 红多少 / 蓝多少 / 无效多少」"}
    if a == "goto_start":
        # ★ 2026-10-10 用户口径：「让它回到起点」—— 这是个**真实运动**，必须出计划卡 + 确认
        return {"title": "回开始/结束点",
                "detail": "机器人运动到「开始/结束点」（即回起点）",
                "note": "这是运动（会真的走）；「停止」也能让它在结束动作后回起点"}
    if a == "look_color":
        return {"title": "看颜色",
                "detail": "取 %s 的件送到 C 区判色，判完放回原格" % _loc_zh(plan.get("from"), "from"),
                "note": "这是运动：要真取真放"}
    if a == "query_state":
        # ★ 2026-10-10：只读查询本来走 /chat 直接作答（不弹计划卡）；这里补一条回显，
        #   防它走 /parse 时又掉进 "?" 兜底。
        return {"title": "查询（只读）",
                "detail": "查我记录过的 A 区颜色 / 数量 —— 不动机器人",
                "note": "A 区颜色只能靠逐件拍照；没盘过就只会说「不知道」，不编数量"}
    if a == "move_quota":
        # ★ 2026-10-10 补上：以前 _echo 没有这个分支 → 回显直接掉进末尾兜底，
        #   计划卡只显示一个 "?"（现场截图那条）。这里按"配额搬运"如实描述，
        #   并把**摆法**（同色成行/成列，约束求解出来的）写进回显。
        q = plan.get("quotas") or {}
        zh = "、".join("%s×%d" % (c, int(n)) for c, n in q.items()) or "?"
        place = str(plan.get("placement_note") or "")
        detail = ("从 A 区按颜色取：%s → 逐件判色 → 放到 %s"
                  % (zh, _loc_zh(plan.get("to"), "to")))
        if place:
            detail += "；摆法：%s" % place
        notes = ["A 区颜色事先未知；不需要的件放回原位；合计 %d 件" % sum(q.values())]
        ptn = plan.get("pattern") or "any"
        if ptn != "any":
            notes.append("摆放方式：%s（同色落在同一条线上）" % PL.PATTERN_ZH.get(ptn, ptn))
        ban = _ban_of(plan)
        if ban["have"]:
            notes.append("已有料（不会往上放）：" + "、".join(ban["have"]))
        if ban["to"]:
            notes.append("不能放：" + "、".join(ban["to"]))
        notes.append("判不出色/黑色 → 放回原格后%s"
                     % ("继续" if plan.get("on_invalid") == "continue" else "停"))
        alts = plan.get("placement_alts") or []
        if alts:
            notes.append("其它可行摆法：" + " ／ ".join(alts))
        return {"title": "按颜色配额搬运", "detail": detail, "note": "；".join(notes)}
    if a in ("move_one", "move_all"):
        n = "全部（受件数上限约束）" if a == "move_all" else "1 件"
        return {"title": "搬运" if a == "move_one" else "批量搬运",
                "detail": "取 %s → 判色 → 放 %s（%s）"
                          % (_loc_zh(plan.get("from"), "from"),
                             _loc_zh(plan.get("to"), "to"), n),
                "note": "取件顺序随机" if plan.get("shuffle") else ""}
    return {"title": "?", "detail": ""}


@router.post("/parse")
def api_parse(body: ParseIn, tok: str = Depends(require_control)):
    """原文 → 结构化意图（**只解析、不执行**）。

    解析顺序：**规则优先 → 规则失败才调大模型**（大模型没配就直接报"没听懂"）。
    返回 `plan_id`：`/exec` 必须带它，保证"确认的就是解析出来的那一份"。
    """
    return _parse_to_plan(body.text)


def _free_b_tips(bad_slot: str) -> List[str]:
    """目标格被占/禁放时给的**可操作建议**（B 区当前认为空的格）。"""
    free = [s for s in ["B%d" % i for i in range(1, 10)] if not _occ["B"].get(s)]
    return ["B 区当前认为空的格：%s" % "、".join(free)] if free else []


def _feasibility(plan: Dict) -> Dict:
    """解析阶段的**可行性闸门**（2026-10-10 用户口径：先查已有/禁放，绝不叠放）。

    ★ 只管"目标格是被占/被禁"的单件搬运；配额搬运的可行性由 `_solve_quota_placement` 解。
    不可行 → {ok:False, reason, suggestions}（调用方**不执行**并把建议给用户）。
    """
    ban = _ban_of(plan)
    if plan.get("action") == "move_one":
        src = plan.get("from") or {}
        dst = plan.get("to") or {}
        if (src.get("kind") or "") == "slot" and (src.get("slot") or "") in ban["from"]:
            return {"ok": False, "suggestions": [],
                    "reason": "「%s」是禁取格，不能从它拿件" % src["slot"]}
        if (dst.get("kind") or "") == "slot":
            s = dst.get("slot") or ""
            if s in ban["to"] or s in ban["have"]:
                return {"ok": False, "suggestions": _free_b_tips(s),
                        "reason": "「%s」是禁放/已有料格，不能往上放" % s}
            if _occ["B"].get(s):
                return {"ok": False, "suggestions": _free_b_tips(s),
                        "reason": "「%s」本页记录已有料，不能往上放" % s}
    return {"ok": True}


def _finalize(text: str, res: Dict, source: str) -> Dict:
    """解析结果**收口**：可行性闸门 → 配额求解 → 缓存 plan_id → 回显。

    ★ 单一出口：不管 plan 来自大模型还是规则，都走同一套校验/回显 ——
      "确认的就是执行的那一份"（plan_id 绑定）。
    """
    out = {"ok": bool(res.get("ok")), "raw": text, "source": source,
           "intent": res.get("intent") or "", "plan": res.get("plan") or {},
           "warnings": list(res.get("warnings") or []),
           "error": res.get("error") or "",
           "llm_enabled": llm.enabled(),
           "llm_error": res.get("llm_error", ""),
           "batch_max": batch_max(),
           # ★ "理解了"（有明确动作意图）—— 即使做不到也**不要**掉进闲聊，要摊开原因+建议。
           "understood": bool(res.get("intent"))}
    if not out["ok"]:
        return out
    # ---- ① 可行性闸门（占用/禁放）----
    gate = _feasibility(out["plan"])
    if not gate.get("ok"):
        out.update(ok=False, plan={}, error=gate.get("reason") or "这个目标格不能放",
                   suggestions=gate.get("suggestions") or [])
        return out
    # ---- ② 配额搬运：解析阶段就把"摆法"解出来（解不出 → 给建议，不执行）----
    if (out["plan"] or {}).get("action") == "move_quota":
        sp = _solve_quota_placement(out["plan"])
        if not sp.get("ok"):
            ban = _ban_of(out["plan"])
            placed = [s for s, v in (_occ["B"] or {}).items() if v]
            tips = PL.suggest(out["plan"].get("quotas") or {},
                              out["plan"].get("pattern") or "any",
                              occupied=list(ban["have"]) + placed, banned=ban["to"])
            out.update(ok=False, plan={},
                       error=sp.get("reason") or "在给定约束下摆不下", suggestions=tips)
            return out
        out["plan"] = {**out["plan"], "placement": sp["plan"],
                       "placement_note": sp["note"],
                       "placement_alts": sp.get("alts") or []}
    # ---- ③ 缓存 + 回显 ----
    pid = uuid.uuid4().hex
    with _gate:
        _plans[pid] = {"plan": out["plan"], "at": time.time(), "raw": text}
    out["plan_id"] = pid
    out["echo"] = _echo(out["intent"], out["plan"])
    if out["plan"].get("action") == "move_all":
        cands = _occupied(["A%d" % i for i in range(1, 10)]) \
            if (out["plan"].get("from") or {}).get("zone", "A") == "A" else []
        out["batch_count"] = min(len(cands), batch_max())
        out["over_limit"] = len(cands) > batch_max()
    return out


def _parse_to_plan(text: str) -> Dict:
    """**大模型优先**的纯解析（2026-10-10 用户口径："每句话必须大模型推理"）。

    顺序：
      0) **安全直达**（唯一例外）：明确说「急停」→ 立刻走规则 —— **急停不能等网络**；
      1) **大模型解析**（`llm.parse` → 白名单 `sanitize`，产出与规则同构的 plan）；
      2) 大模型不可用/解析不出 → **降级到规则**（`source="rule-degraded"`，
         界面必须标注"系统规则，**不是**云端大模型"）；
      3) 收口过 **可行性闸门**（占用/禁放/摆放约束）—— 不可行 → 不执行 + 给建议。

    ★ 只解析、不执行；执行侧另走 /exec 的安全闸。
    """
    text = (text or "").strip()
    # ---- 0) 安全直达：急停不绕大模型 ----
    if NL.is_estop(text):
        r = NL.parse(text)
        if r.get("ok"):
            return _finalize(text, r, "safety")
    res: Optional[Dict] = None
    source = ""
    llm_err = ""
    if llm.enabled():
        lr = llm.parse(text)
        if lr.get("ok"):
            p = dict(lr["plan"])
            res = {"ok": True, "intent": p.get("action") or "", "plan": p,
                   "warnings": [], "raw": text, "error": ""}
            source = "llm"
        else:
            llm_err = lr.get("error") or ""
    if res is None:
        # 降级：规则解析（**必须**让用户知道这不是大模型）
        res = dict(NL.parse(text))
        if llm.enabled():
            source = "rule-degraded"
            if res.get("ok"):
                res.setdefault("warnings", []).append(
                    "⚠ 这是**系统解析规则**的结果，**不是**云端大模型（大模型没解析成功）。")
        else:
            source = "rule"
        if llm_err:
            res["llm_error"] = llm_err
    return _finalize(text, res, source)


# ---------------------------------------------------------------- 对话（2026-10-10）
class ChatIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    # 最近对话历史（前端裁剪好，后端再清洗一遍：只收 user/assistant 文本，最多 10 条）
    history: List[Dict[str, str]] = Field(default_factory=list)


_CHAT_FALLBACK = (
    "我在 —— 大模型暂时没连上，指令我还是听得懂。"
    "试试：「把A区第1行第1列放到B区红列」「A区第1行第1列是什么颜色」「开始分拣」。"
)

_NO_LLM_HINT = (
    "你好，我是机器人助手。大模型还没配置（.env 里需要 EFORT_LLM_API_KEY），"
    "现在只能听指令：「把A区第1行第1列放到B区红列」「A区第1行第1列是什么颜色」"
    "「开始分拣」「急停」。"
)


@router.post("/chat")
def api_chat(body: ChatIn, tok: str = Depends(require_control)):
    """对话入口（2026-10-10 用户口径："要一个能正常聊天的大模型"）。

    顺序：**指令解析先行**（规则 → 白名单 LLM）→ 是指令就出计划卡（与 /parse 同一份
    plan_id 口径，前端照常"回显 + 确认"）；不是指令才进大模型自由对话 ——
    聊天只许说话，绝不碰控制（对话人格在 llm.CHAT_SYSTEM_PROMPT）。
    ★ 一句闲聊会打两次大模型（翻译人格判"不是指令" + 对话人格回话）：
      免费 glm-4-flash 够快，可接受；指令则只打一次。
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="先说点什么")
    parsed = _parse_to_plan(text)
    if parsed.get("ok"):
        # ★ 状态查询（2026-10-10）：**只读**，有记录就直接答，
        #   绝不弹计划卡让人点"执行"（那会让人以为要动机器人）。
        if parsed.get("intent") == "query_state":
            ans = inv_answer(text)
            if ans:
                return {"ok": True, "mode": "chat", "reply": ans, "source": "inv"}
            return {"ok": True, "mode": "chat", "source": "inv",
                    "reply": "A 区的颜色我**没有实测过** —— 相机只对着 C 检测位，照不到 A 区。"
                             "要说准就先说「盘点A区」：逐件拍照判色后**放回原处**（属运动，需要确认）。"}
        return {"ok": True, "mode": "plan", "parsed": parsed,
                "source": parsed.get("source") or "rule"}
    # ★★ 2026-10-10 用户口径（Q1②）：**理解了但做不到**（占用/禁放/摆不下）→
    #   绝不掉进闲聊 —— 要把"无法实现 + 建议"摊给用户看（且不给执行按钮）。
    if parsed.get("understood"):
        return {"ok": True, "mode": "plan", "parsed": parsed,
                "source": parsed.get("source") or ""}
    # 不是指令 → 闲聊/问答（history 只留 user/assistant 文本，最多 10 条）
    if llm.enabled():
        hist = [{"role": str(h.get("role") or ""), "content": str(h.get("content") or "")}
                for h in (body.history or [])]
        hist = [h for h in hist if h["role"] in ("user", "assistant") and h["content"]][-10:]
        cr = llm.chat(text, hist)
        if cr.get("ok"):
            return {"ok": True, "mode": "chat", "reply": cr["reply"], "source": "llm"}
        # 如实告知大模型不可用，再给指令引导 —— 不装死、不编话
        return {"ok": True, "mode": "chat",
                "reply": "（大模型暂时不可用：%s）%s"
                         % (cr.get("error") or "未知原因", _CHAT_FALLBACK),
                "source": "fallback"}
    return {"ok": True, "mode": "chat", "reply": _NO_LLM_HINT, "source": "rule"}


class ExecIn(BaseModel):
    plan_id: str = Field(..., min_length=8, max_length=64)
    confirm: bool = Field(default=False, description="批量/看颜色必须显式确认")
    dry_run: bool = Field(default=True, description="空跑：只校验点位与解析，不下发运动、不判色")
    speed_pct: int = Field(default=5, ge=5, le=100,
                           description="沿用右上角那份全局速度（全项目唯一速度口径）")
    # ★★ 2026-10-09 接管开关（用户口径"丝滑"）：执行中再来指令时，
    #   默认**排队接替**（当前任务到步边界停止 → 回起点 → 自动执行新的），
    #   而不是像以前那样直接 409 拒绝。
    #   置 False 可恢复旧行为（明确要求"等它跑完"）。
    takeover: bool = Field(default=True, description="执行中是否允许排队接替（默认允许）")


@router.post("/exec")
def api_exec(body: ExecIn, tok: str = Depends(require_control)):
    """按解析出的计划执行（**必须带同一个 plan_id**）。"""
    with _gate:
        rec = _plans.get(body.plan_id)
        for k in [k for k, v in _plans.items() if time.time() - v["at"] > _PLAN_TTL]:
            _plans.pop(k, None)
    if not rec:
        raise HTTPException(400, detail="计划已过期或不存在，请重新解析")
    plan = rec["plan"]
    act = plan.get("action")

    # ---- 急停：最高优先级，不排队、不需确认（仍要令牌）----
    if act == "estop":
        # ★ 复用 /control/estop 的同一实现（它还会停点动线程 + 中止在跑流程，
        #   并在"急停未送达控制器"时抛 503 —— 不能显示"已停"）。绝不自己重写一份。
        from app.api.control import api_estop
        emit_event("ai", "warn", "ai.estop", "AI 指令：急停", {"raw": rec["raw"]},
                   actor="control")
        api_estop(tok)          # 未送达会抛 503（如实失败）
        return {"ok": True, "action": "estop", "message": "急停已下发"}

    # ---- 流程控制：复用既有通道 ----
    if act == "sorting":
        op = plan.get("op")
        if op == "stop":
            hit = request_cancel()
            emit_event("ai", "warn", "ai.sorting", "AI 指令：停止执行",
                       {"raw": rec["raw"], "cancelled": hit}, actor="control")
            return {"ok": True, "action": "sorting", "op": "stop",
                    "message": "已请求停止（步边界生效，随后回起点）"}
        if op in ("pause", "resume"):
            from app.api.control import RunCancelIn, api_run_pause, api_run_resume
            if op == "pause":
                api_run_pause(RunCancelIn(), tok)
                msg = "已暂停（当前动作结束后生效）"
            else:
                api_run_resume(RunCancelIn(), tok)
                msg = "已继续"
            emit_event("ai", "info", "ai.sorting", "AI 指令：%s" % msg,
                       {"raw": rec["raw"]}, actor="control")
            return {"ok": True, "action": "sorting", "op": op, "message": msg}
        if op == "start":
            from app.api.sorting import StartIn, api_start
            # ★★ 2026-10-11（方案 §C）：把 `dual_face` **真正传下去**（过去恒 False）。
            #   plan 里 `dual_face=True`（语法「快速完成/双面/一趟两件」）→ 本次走双面（一趟两件）；
            #   其余一律一面（默认）—— 大模型降级/没解析出也回落一面。
            dual = bool(plan.get("dual_face"))
            # ★★ 2026-10-11 修复（"参数写死 → 开关失效 + 界面编故事"这一类）：
            #   原实现写死 `StartIn(dry_run=True, ...)`，于是
            #     ① AI 页那颗「空跑」开关对"开始分拣"**完全不生效**（机器人永远不动）；
            #     ② 前端运行日志却按本地开关打印"真实下发" —— 界面与实际相反。
            #   现在：**尊重调用方的 `dry_run`**（= AI 页的空跑开关），
            #   并把**右上角全局速度**一起传下去（原来没传 → 落到 StartIn 兜底 5%，"速度不跟滑块"）。
            #   硬安全网不变：伺服未上电时 `api_start` 内部统一 409 拒绝。
            dry = bool(body.dry_run)
            r = api_start(StartIn(dry_run=dry, dual_face=dual,
                                  speed_pct=int(body.speed_pct)), tok)
            mode_cn = "空跑" if dry else "真实下发"
            msg = "已开始分拣（%s，%s）" % (
                mode_cn, "快速·双面·一趟两件" if dual else "正常·一面")
            if dry:
                msg += "；确认无误后可取消勾选「空跑」再执行"
            emit_event("ai", "info", "ai.sorting",
                       "AI 指令：开始分拣（%s，%s）" % (
                           mode_cn, "快速·双面" if dual else "正常·一面"),
                       {"raw": rec["raw"], "dual_face": dual, "dry_run": dry},
                       actor="control")
            return {"ok": bool(r.get("ok")), "action": "sorting", "op": "start",
                    "dual_face": dual, "dry_run": dry, "message": msg,
                    "state": r.get("state")}
        raise HTTPException(400, detail="未知的流程操作")

    # ---- 运动类：单件/批量/看颜色 ----
    if not body.confirm and act != "move_one":
        raise HTTPException(400, detail="这类指令需要先确认（点「确认执行」）")

    # ★ 统一"未就绪"闸门：真实下发（非空跑）前先校验伺服是否上电 —— 未就绪直接拒绝，
    #   不排队、不接替（排队接替的是"已就绪"场景）。空跑 dry_run=true 放行。
    #   放在目标解析之前：伺服没上电时，先给出"机器人没有就绪"，而不是先纠结点位。
    reject_if_not_ready(dry=bool(body.dry_run))

    # ★★ 2026-10-09：忙碌检查**从"解析之前"挪到了"解析+点位校验之后"**。
    #   为什么：要支持"接管"（执行中来新指令 → 旧任务回起点后自动接上新的），
    #   排队的计划必须是**已经校验通过**的 —— 否则会把一个坏计划排进队列，
    #   等旧任务跑完回起点才报错，现场体验很差（而且那时人已经走开了）。

    # ★★ 目标解析放在**启动线程之前**：解析不出来就当场 400，
    #    而不是让线程跑起来再以"内部异常"收场（那既难查也不诚实）。
    #    —— 这里把"颜色列/整区"落成**具体格**，runner 只需按格执行。
    rp = dict(plan)
    if act == "look_color":
        s = _resolve_src(plan.get("from") or {})
        if not s.get("ok"):
            raise HTTPException(400, detail=s["error"])
        rp = exec_plan(plan, {"action": "look_color",
                              "from": {"kind": "slot", "scope": s["slot"],
                                       "slot": s["slot"], "zh": s.get("zh")}})
    elif act == "move_one":
        s = _resolve_src(plan.get("from") or {})
        if not s.get("ok"):
            raise HTTPException(400, detail="起点不可用：" + s["error"])
        d = _resolve_loc(plan.get("to") or {})
        if not d.get("ok"):
            raise HTTPException(400, detail="目标不可用：" + d["error"])
        # ★ 2026-10-10 审计补漏：**禁取/禁放格对单件搬运同样有效**（以前只在批量里筛）
        _ban = _ban_of(plan)
        if s["slot"] in _ban["from"]:
            raise HTTPException(400, detail="「%s」是禁取格，不能从它拿件" % s["slot"])
        if d["slot"] in _ban["to"]:
            raise HTTPException(400, detail="「%s」是禁放格，不能放到它" % d["slot"])
        rp = exec_plan(plan, {"action": "move_one",
                              "from": {"kind": "slot", "slot": s["slot"], "zh": s.get("zh")},
                              "to": {"kind": "slot", "slot": d["slot"], "zh": d.get("zh")}})
    elif act == "move_quota":
        # ★★ 2026-10-09 新增：按颜色配额搬运（"3个红色，2个蓝色，1个绿色"）。
        #   校验放在**启动线程之前**（与其它动作同口径）：配额为空/目标写成具体格
        #   都要当场 400，而不是跑起来再以"内部异常"收场。
        quotas = {k: int(v) for k, v in (plan.get("quotas") or {}).items() if int(v) > 0}
        if not quotas:
            raise HTTPException(400, detail="配额为空：要说清每种颜色几件，例如「3个红色2个蓝色」")
        dstq = plan.get("to") or {}
        if (dstq.get("kind") or "color_col") == "slot":
            raise HTTPException(400, detail="按颜色配额搬运要放到**颜色列**，不能指定某一个具体格")
        # ★★ 2026-10-10：**必须用 exec_plan 重建**（否则 placement/ban/on_invalid 会被丢掉，
        #   现场报"「蓝」的摆法里已没有可用格"）。
        rp = exec_plan(plan, {"action": "move_quota",
                              "from": {"kind": "all", "zone": "A"},
                              "to": {"kind": "color_col", "zone": dstq.get("zone") or "B",
                                     "color": dstq.get("color")},
                              "quotas": quotas})
        # 配额模式的候选是"运行时按占位表筛"的，这里只做点位齐备性校验
        cands = ["A%d" % i for i in range(1, 10)]
    elif act == "move_all":
        zsrc = (plan.get("from") or {}).get("zone", "A")
        if zsrc == "A":
            cands = _occupied(["A%d" % i for i in range(1, 10)])
        else:
            # B 区有料的格 = 本页占位表里记了颜色的格（顺序 B1..B9）
            cands = [s for s in ["B%d" % i for i in range(1, 10)] if _occ["B"].get(s)]
        if not cands:
            raise HTTPException(400, detail="%s 区没有可取的件（按本页占位表判断）" % zsrc)
        dst = plan.get("to") or {}
        dk = dst.get("kind")
        if dk == "zone" and dst.get("zone") == "B":
            raise HTTPException(400, detail="放到 B 区要指定颜色列（例如「放到B区红列」）"
                                           "—— 颜色要判色后才知道")
        if dk == "slot":
            # 固定格只对第 1 件有意义，后面的件会叠在一起 → 明确拒绝，不含糊
            if len(cands) > 1:
                raise HTTPException(
                    400, detail="批量搬运到「同一个具体格」会把件叠在一起，已拒绝。"
                                "请改用颜色列（例如「放到B区红列」）或整区（例如「放回A区」）")
            fixed = dst.get("slot")
        else:
            fixed = None
        rp = exec_plan(plan, {"action": "move_all",
                              "from": {"kind": "zone", "zone": zsrc},
                              "to": {"kind": dk or "color_col", "zone": dst.get("zone") or "B",
                                     "color": dst.get("color"), "slot": fixed},
                              "shuffle": bool(plan.get("shuffle"))})

    pmap = _load_pmap()
    need = [x for x in (rp.get("from", {}).get("slot"),
                        rp.get("to", {}).get("slot")) if x]
    # 批量/配额：逐个候选也要在点位表里（缺示教就当场说清缺哪个）
    if act in ("move_all", "move_quota"):
        for s in cands:
            need += [s, s + "上"]
    miss = sorted({p for p in need if p not in pmap})
    if miss:
        raise HTTPException(400, detail="点位缺失：%s（先补齐示教）" % "、".join(miss))

    # ★★ 2026-10-09 接管（用户口径："如果触发过程当中用户让它停，或者下达了别的指令，
    #    必须回到起点再开始执行用户的命令，保证丝滑完成任务"）。
    #
    #   行为：执行中再来指令**不再 409 拒绝**，而是——
    #     ① 把**已校验通过**的新计划记进 pending（只留最后一条，避免排队一堆旧命令）
    #     ② 请求取消当前任务 → 步边界生效 → **回起点**（既有语义，不是急停）
    #     ③ 立即返回 {queued:true}，不阻塞 HTTP
    #   旧任务在 `_run` 的 finally 里接力启动 pending —— 那是**唯一可靠**的
    #   "已经回完起点"时刻（前端轮询做不到，刷新/断网就丢）。
    #   ⚠ 急停不走这条路：它在上面单独分支返回，永远立即执行、不排队。
    with _gate:
        # ★★ 2026-10-10：与井字棋对局互斥（共用 40135 触发位，交错下发会互相清触发位）
        from app.api.tictactoe import is_game_running as _tt_running
        if _tt_running():
            raise HTTPException(409, detail="井字棋对局进行中，先「停止」对局再下达运动指令")
        busy = bool(_job.get("thread") and _job["thread"].is_alive())
        if busy:
            if not body.takeover:
                raise HTTPException(409, detail="AI 执行正在运行，等它结束或先「停止」")
            _job["pending"] = {"rp": rp, "dry": bool(body.dry_run),
                               "speed": int(body.speed_pct),
                               "raw": rec.get("raw") or "", "at": time.time()}
    if busy:
        request_cancel()
        emit_event("ai", "warn", "ai.takeover",
                   "AI 指令：接管（当前任务到步边界停止并回起点后，自动执行新指令）",
                   {"raw": rec.get("raw"), "plan": rp}, actor="control")
        return {"ok": True, "queued": True, "plan": rp,
                "message": "已排队：当前动作结束 → 回起点 → 自动执行「%s」"
                           % (rec.get("raw") or "")}

    st = _new_state(bool(body.dry_run))
    snap = _snap(st)                      # ★ 快照必须在线程启动**之前**取
    ctx = _start_job(rp, pmap, st, bool(body.dry_run), int(body.speed_pct))
    emit_event("ai", "info", "ai.exec",
               "AI 执行：%s（%s）" % (rec.get("raw"), "空跑" if body.dry_run else "真实下发"),
               {"plan": rp, "raw": rec["raw"]}, actor="control")
    return {"ok": True, "run_id": ctx.run_id, "state": snap, "plan": rp}


@router.get("/state")
def api_state(tok: str = Depends(require_control)):
    """AI 执行进度快照（阶段/判色/停止原因 + 本页占位表）。"""
    with _gate:
        st = dict(_job.get("state") or {}) or None
        running = bool(_job.get("thread") and _job["thread"].is_alive())
        # ★ 排队的接管计划（界面要显示"已排队，等当前任务回起点后执行"）
        pend = _job.get("pending")
        queued = ({"raw": pend.get("raw") or "",
                   "age": round(time.time() - float(pend.get("at") or 0), 1)}
                  if pend else None)
    return {"ok": True, "running": running, "state": st, "queued": queued,
            "occ": {"A": dict(_occ["A"]), "B": dict(_occ["B"])},
            "batch_max": batch_max()}


@router.post("/cancel")
def api_cancel(tok: str = Depends(require_control)):
    """停止 AI 执行（步边界打断，随后回起点）。

    ★ 同时**清掉排队中的接管计划** —— 用户说"停"就是要全停，
      不能停完当前任务又自动把排队的下一条跑起来（那会让人以为"停不掉"）。
    """
    with _gate:
        dropped = _job.get("pending")
        _job["pending"] = None
    hit = request_cancel()
    # dropped：被一并丢掉的排队指令（界面可提示"已同时取消排队的「…」"）
    return {"ok": True, "cancelled": hit,
            "dropped": (dropped or {}).get("raw") or ""}


@router.post("/occ/reset")
def api_occ_reset(tok: str = Depends(require_control)):
    """清空本页占位表（"格子里的东西和记录不一致"时用 —— 如实说明：本页不知道真实格内容）。"""
    _occ["A"].clear()
    _occ["B"].clear()
    emit_event("ai", "info", "ai.occ_reset", "AI 页占位表已清空（回到'A 区默认有料'）",
               {}, actor="control")
    return {"ok": True, "occ": {"A": {}, "B": {}}}


@router.get("/config")
def api_config(tok: str = Depends(require_control)):
    """AI 页配置（**不回传 key**）。"""
    return {"ok": True, "llm_enabled": llm.enabled(), "llm_model": llm.model_name(),
            "confirm_required_for": ["move_all", "look_color"],
            "batch_max": batch_max(), "real_write": real_write_enabled()}


# ---------------------------------------------------------------- 执行
def _load_pmap() -> Dict[str, object]:
    db = SessionLocal()
    try:
        return {p.name: p for p in list_points(db)}
    finally:
        db.close()


def _live_joints() -> Optional[List[float]]:
    snap = collector.get_latest()
    if not snap:
        return None
    try:
        return [float(snap["j%d" % i]) for i in range(1, 7)]
    except Exception:  # noqa: BLE001
        return None


def _new_state(dry: bool) -> Dict:
    """一次 AI 执行的初始 state（**首次启动与接管接力共用**，避免两处口径漂移）。"""
    return {"dry_run": bool(dry), "stage": "准备", "piece": 0, "total": 1,
            "color": "", "conf": 0.0, "target": "", "src": "", "holding": False,
            "suspect_pick": False, "error": "", "finished": False, "ok": None,
            "judge": (not bool(dry)) and real_write_enabled(), "sim_color": False}


def _snap(st: Dict) -> Dict:
    """启动响应回**线程启动前的快照**，别把活动字典交出去
    （否则前端拿到的"启动瞬间"可能已经是跑完的状态 —— 复位的依据就不对了）。"""
    import copy as _copy
    return _copy.deepcopy(st)


def _start_job(rp: Dict, pmap: Dict[str, object], st: Dict, dry: bool, speed: int):
    """启动执行线程。★ **首次启动与"接管接力"共用这一处**，
    保证两条路径的线程名/状态挂载/互斥口径完全一致。"""
    ctx = new_run("AI 执行", kind="ai")
    with _gate:
        _job["state"] = st
        _job["ctx"] = ctx
        # ★ 两路气的"还在开"标记（异常/取消时兜底收掉，见 _run 的 finally）
        _job["suck_on"] = False
        _job["blow_on"] = False
        th = threading.Thread(target=_run, args=(ctx, rp, pmap, st, dry, speed),
                              name="ai", daemon=True)
        _job["thread"] = th
    th.start()
    ctx.set_thread(th)      # ★ 绑定执行线程：让"卡死登记"能被 is_alive() 精确识别
    return ctx


def _relay_pending() -> None:
    """接管接力：被接管的任务收尾后，自动启动排队的新指令。

    ★ 只在 `_run` 的 finally 里调用 —— 那是**唯一可靠**的"已经回完起点"时刻。
      （前端轮询做不到：刷新页面/断网就丢，而且多一次往返不"丝滑"。）
    ★ pending 为空/已过期就直接返回；启动失败要**如实记事件**，不能静默吞掉。
    ★ 急停不进 pending（它在上面的分支里立即执行），所以这里不存在"接力急停"的风险。
    """
    with _gate:
        p = _job.get("pending")
        _job["pending"] = None
    if not p:
        return
    age = time.time() - float(p.get("at") or 0)
    if age > _PENDING_TTL:
        log.info("接管接力跳过：排队已过期（%.0fs > %ds）", age, _PENDING_TTL)
        emit_event("ai", "warn", "ai.relay_skip",
                   "接管取消：排队超过 %d 秒未轮到，已丢弃（请重新下达）" % _PENDING_TTL,
                   {"age": round(age, 1)}, actor="control")
        return
    try:
        pmap = _load_pmap()
        _start_job(p["rp"], pmap, _new_state(bool(p.get("dry"))),
                   bool(p.get("dry")), int(p.get("speed") or 5))
        emit_event("ai", "info", "ai.relay",
                   "接管：已开始执行新指令「%s」" % (p.get("raw") or ""),
                   {"plan": p["rp"]}, actor="control")
    except Exception as e:  # noqa: BLE001
        log.exception("接管接力启动失败")
        emit_event("ai", "warn", "ai.relay_fail",
                   "接管失败：新指令未能启动（%s）" % str(e)[:120], {}, actor="control")


def _run(ctx, plan: Dict, pmap: Dict[str, object], st: Dict, dry: bool,
         speed: int = 5) -> None:
    """执行**已解析好的**计划（目标已落成具体格；见 api_exec）。

    异常一律回起点（与分拣同口径），并把原因写进 state。
    """
    act = plan.get("action")
    # 速度只认调用方传来的那一份（UI 右上角），并夹到 5~100
    speed = max(5, min(100, int(speed or 5)))
    min_conf = 0.3
    stop_reason = ""
    phase = "done"
    start = S.START_POINT
    # ★★ 逐格约束（2026-10-10 用户口径："某格不能拿 / 某格不能放"）—— 说到做到：
    #   ① 单件：源/目标命中禁格 → **直接拒绝**（不静默改成别的格）；
    #   ② 批量/配额：取件候选里**剔除禁取格**；
    #   ③ 禁放：列内**自动落位会跳过**被禁的格（用户口径还含"这格已经有物体了"——
    #      那一半由占位表覆盖）；跳满时原因会写明"被禁用格占掉了"。
    ban = _ban_of(plan)
    if act == "move_one":
        if (plan.get("from") or {}).get("slot") in ban["from"]:
            raise _Stop("你指定了 %s 不能拿" % plan["from"]["slot"], "约束")
        if (plan.get("to") or {}).get("slot") in ban["to"]:
            raise _Stop("你指定了 %s 不能放" % plan["to"]["slot"], "约束")
    try:
        if act == "inventory":
            # ★★ 盘点（2026-10-10 用户口径）：逐件 取料 → 送 C 位判色 → **原格放回**
            #    （复用 look_only 的那条路：取真件、判真色、放回原格，**不搬走、不改变 A 区**）。
            #    跑完把结果写进 `_inv`，供"几个红/几个蓝/几个无效"这类只读查询回答。
            slots = [s for s in _occupied(["A%d" % i for i in range(1, 10)])
                     if s not in ban["from"]]
            res: Dict[str, str] = {}
            _set_state(st, total=len(slots), stage="盘点")
            for i, s in enumerate(slots, start=1):
                if ctx.cancelled():
                    stop_reason, phase = "已被停止", "cancelled"
                    break
                _set_state(st, piece=i, stage="盘点 %s" % s, color="", conf=0.0)
                _piece(ctx, pmap, st, dry, s, None, True, speed, min_conf)
                res[s] = str(st.get("color") or "")
            _inv.update({"at": time.time(), "A": res})
            _set_state(st, stage="盘点完成", inventory=res,
                       inv_summary=_inv_summary(res), piece=None)
        elif act == "goto_start":
            # ★ 2026-10-10 用户口径：AI 要能"回起点"。
            #   此前**没有这个动作** → 用户说「回到起点」时落进闲聊 → 大模型只能编
            #   "已回到起点位置"（它没有执行权，纯属谎报）。现在是一条真运动。
            _set_state(st, stage="回开始/结束点")
            _move(ctx, pmap, start, speed, dry, "回开始/结束点", st)
        elif act == "look_color":
            _piece(ctx, pmap, st, dry, plan["from"]["slot"], None, True,
                   speed, min_conf)
        elif act == "move_one":
            _piece(ctx, pmap, st, dry, plan["from"]["slot"], plan["to"]["slot"], False,
                   speed, min_conf)
        elif act == "move_all":
            zsrc = (plan.get("from") or {}).get("zone", "A")
            srcs = _occupied(["A%d" % i for i in range(1, 10)]) if zsrc == "A" \
                else [s for s in ["B%d" % i for i in range(1, 10)] if _occ["B"].get(s)]
            if plan.get("shuffle"):
                random.shuffle(srcs)
            srcs = srcs[:batch_max()]
            srcs = [s for s in srcs if s not in ban["from"]]      # ★ 禁取格剔除（2026-10-10）
            color_col = plan["to"].get("kind") == "color_col"
            _set_state(st, total=len(srcs))
            for i, s in enumerate(srcs, start=1):
                if ctx.cancelled():
                    stop_reason, phase = "已被停止", "cancelled"
                    break
                _set_state(st, piece=i)
                # 目标：颜色列 → 每件判色后选该列空位；固定格 → 就是那一格（已在 exec 校验过）
                dst = None if color_col else plan["to"].get("slot")
                _piece(ctx, pmap, st, dry, s, dst, False, speed, min_conf,
                       prefer_color=plan["to"].get("color") if color_col else None,
                       skip_to=ban["to"])      # ★ 自动落位跳过禁放格（2026-10-10）
            _set_state(st, piece=None)
        elif act == "move_quota":
            # ★★ 2026-10-09 新增（用户口径："全部要学会解析，如果没有检测到放回原位"）：
            #   "A区的3个红色，2个蓝色，1个绿色搬到B区"
            #   → 逐件取到 C 区判色：该颜色还缺就放走，不缺/没判出就**放回原位**。
            #   ⚠ 结果**不保证**：A 区颜色事先未知，凑不齐就如实报"未凑齐"
            #     （不假装完成 —— 那会让人以为 3 个红色都搬完了）。
            quotas = {k: int(v) for k, v in (plan.get("quotas") or {}).items()
                      if int(v) > 0}
            if not quotas:
                raise _Stop("配额为空：要说清每种颜色几件，例如「3个红色2个蓝色」", "规划")
            want_total = sum(quotas.values())
            # ★ 2026-10-10 审计补漏：**禁取格在配额模式也要剔除**（以前只有批量筛了）
            srcs = [s for s in _occupied(["A%d" % i for i in range(1, 10)])
                    if s not in ban["from"]]
            if not srcs:
                raise _Stop("A 区没有可取的件（按本页占位表判断）", "取料")
            _set_state(st, total=want_total)
            # ★★ 2026-10-10：**解析阶段**解出来的摆法（同色成行/成列，跳过已有料与禁放格）——
            #   执行就用这一份，"回显的摆法"与"真正执行的摆法"是同一份（plan_id 绑定）。
            slots_by_color = {c: list(v) for c, v in (plan.get("placement") or {}).items()}
            continue_inv = (plan.get("on_invalid") == "continue")
            done = 0
            # ★ 上限 = A 区当前可取的件数：某颜色不够时**最多试完全部**，
            #   不会无限试下去（避免"永远等下去"）；也不是"固定的 counts 之和"次数
            #   —— 遇到无效色（黑）放回后会继续试下一件。
            for s in srcs:
                if sum(quotas.values()) <= 0:
                    break
                if ctx.cancelled():
                    stop_reason, phase = "已被停止", "cancelled"
                    break
                _set_state(st, piece=done + 1, src=s)
                before = dict(quotas)
                _piece(ctx, pmap, st, dry, s, None, False, speed, min_conf,
                       quotas=quotas, skip_to=ban["to"],
                       plan_slots=slots_by_color, continue_on_invalid=continue_inv)
                if sum(quotas.values()) < sum(before.values()):
                    done += 1
            _set_state(st, piece=done)
            left = {k: v for k, v in quotas.items() if v > 0}
            if left and phase == "done":
                raise _Stop("未凑齐：%s（A 区可能没有这个颜色，或已被取空）——"
                            "已放走 %d 件" % ("、".join("%s×%d" % (k, v) for k, v in left.items()),
                                            done), "取料")
        else:
            raise _Stop("不支持的动作：%s" % act, "规划")
    except _Stop as e:
        stop_reason = e.reason
        if "停止" in e.reason:
            phase = "cancelled"
        else:
            phase = "error"
            _set_state(st, holding=False, suspect_pick=True)
            try:
                _set_state(st, stage="异常·回开始/结束点", error=e.reason)
                _move(ctx, pmap, start, speed, dry, "异常回原点", st)
            except _Stop as e2:
                log.warning("AI 异常后回原点也失败：%s", e2.reason)
                stop_reason = "%s；回原点失败：%s" % (stop_reason, e2.reason)
    except Exception as e:  # noqa: BLE001
        log.exception("AI 执行异常")
        stop_reason, phase = "内部异常：%s" % str(e)[:150], "error"
    finally:
        # ★★ 2026-10-10 现场死机修复（用户："程序正在执行中、但没跑代码；就绪和取消都成功，
        #   却无法下达指令"）—— **注销运行登记必须最先做，且不可被跳过**。
        #   根因：原来把「接管接力」（会 new_run + 起线程 + 校验 pending）排在 `ctx.finish()`
        #   **前面**。它一旦抛异常，`ctx.finish()` 永远执行不到 →
        #   `_RUN_ACTIVE` 与本 run 的 `running=True` 双双残留 →
        #   `_reject_if_run_active()` **永久**拒绝所有手动下发；而 `/ready`、`/ready/cancel`
        #   根本不看 `_RUN_ACTIVE`，所以"就绪和取消都成功"。三条现象**全部对上**。
        #   现在：先 finish（释放登记），其余收尾统一兜在 try 里 —— 任一步失败都不影响
        #   "锁已释放"这个事实（否则就是把人锁死）。
        ctx.finish(phase)
        try:
            # ★★ 2026-10-10：异常/取消时**收掉两路气**（与分拣同口径："不许带着负压停在半路"）。
            if not dry:
                if _job.get("blow_on"):
                    try:
                        _air(ctx, "unblow", False, "异常·停止放气", st)
                    except Exception:  # noqa: BLE001
                        pass
                    _job["blow_on"] = False
                if _job.get("suck_on"):
                    try:
                        _air(ctx, "release", False, "异常·停止吸", st)
                    except Exception:  # noqa: BLE001
                        pass
                    _job["suck_on"] = False
            _set_state(st, stage="结束", error=stop_reason, finished=True,
                       ok=(phase == "done"), phase=phase)
            # ★★ 2026-10-09 接管接力：本任务已收尾（含"取消 → 回起点"），
            #   把排队的新指令接上。此刻"回起点"已经走完，是启动新任务唯一可靠的时刻。
            _relay_pending()
            emit_event("ai", "info" if phase == "done" else "warn", "ai.stop",
                       "AI 执行%s%s" % ("完成" if phase == "done" else "结束",
                                       "" if phase == "done" else "：" + stop_reason),
                       {"phase": phase, "reason": stop_reason}, actor="control")
        except Exception:  # noqa: BLE001
            log.exception("AI 执行收尾异常（运行登记已注销，不影响后续指令）")


def _piece(ctx, pmap, st, dry, src: str, dst: Optional[str], look_only: bool,
           speed: int, min_conf: float,
           prefer_color: Optional[str] = None, zone: str = "B",
           quotas: Optional[Dict[str, int]] = None,
           skip_to: Optional[List[str]] = None,
           plan_slots: Optional[Dict[str, List[str]]] = None,
           continue_on_invalid: bool = False) -> None:
    """取一件 → 判色 → 放到目标（或放回原格 / 按颜色列选位 / **按配额决定放还是放回**）。

    ★ 与分拣引擎**同一套原语**（`_move/_air/_capture` 来自 api/sorting.py），
      所以"写目标 + 触发位""吸/停止吸/放/停止放""抓帧投票"只有一处实现。
    ★ `quotas`（用户口径 2026-10-09："全部要学会解析，**如果没有检测到放回原位**"）：
      传了它就是**按颜色配额**模式 —— 判色后看该颜色还缺不缺：
        · 还缺 → 放该颜色对应的格，配额减一
        · 不缺 / **没判出颜色** → **放回原位**（不占 B 区、不消耗配额）
      配额是**原地修改**的（调用方持有同一份 dict，据此判断是否收工）。
    ★ `plan_slots`（2026-10-10 用户口径："按同颜色一行或者一列摆放"）：
      **解析阶段**由 `services/placement.solve()` 解出来的"颜色→格位队列"（同色成行/成列、
      跳过已有料与禁放格）。传了它就**按解落位**，不再走写死的颜色列 ——
      每放一件从该色的队首取一格（原地 pop，调用方持有的同一份）。
    ★ `continue_on_invalid`（用户口径："**如果出现无效颜色放回原位继续**"）：
      True = 判不出色/黑色时**放回原格后继续**（不消耗配额、不整轮停）；False = 旧口径（放回后停）。
    """
    cam = S.CAMERA_POINT
    judge = (not dry) and real_write_enabled()
    # ★ 状态字段用 src/dst（不能用 from —— 它是 Python 关键字，不能当关键字参数）
    _set_state(st, src=src, stage="取料", color="", conf=0.0, target="", holding=False,
               suspect_pick=False, judge=judge)
    _move(ctx, pmap, src + "上", speed, dry, "取料·上方", st)
    # ★★ 统一口径（2026-10-11 用户原话「我在正上位反面，去原料区是变成了正面」）：
    #   `src` 是**原料位**（A 区取料格）→ 一律 `keep`（保持当前面、绝不翻面）；
    #   此刻当前=正面（上面那步）→ 与旧的"正面"行为等价（零回归）。
    _move(ctx, pmap, src, speed, dry, "取料·下落", st,
          face=S.face_for_target(src, S.FACE_FRONT))
    _air(ctx, "suck", dry, "取料·吸", st)
    _job["suck_on"] = True
    _set_state(st, holding=True)
    # ★★ 2026-10-10 用户口径："吸住要一直吸住，到放料时才停止吸住"。
    #   两处修正（与分拣引擎同口径 —— 分拣早已这样，本链路漏了）：
    #     ① 吸上后**先停 SUCK_HOLD_MS** 等真空吸牢再抬（以前没有这一步）；
    #     ② **抬起 / 送检 / 停顿判色 全程保持吸气** —— 以前"抬起后立刻停止吸"，
    #        件只靠杯内残余负压吊着飞一路，现场表现就是"一吸住就松了"。
    if SUCK_HOLD_MS > 0:
        ctx.sleep(SUCK_HOLD_MS / 1000.0)
    _move(ctx, pmap, src + "上", speed, dry, "抬起", st)
    _move(ctx, pmap, cam, speed, dry, "送检", st)
    _set_state(st, stage="停顿")
    if ctx.sleep(CAM_DWELL_S):              # C 区停留（与分拣 dwell_ms 默认同口径）
        raise _Stop("已被停止", "判色")
    invalid = False
    invalid_reason = ""
    if not judge:
        # 空跑 / 双闸未开：不判色（方块不会到 C 区）—— 与分拣同一条修正口径
        _set_state(st, stage="空跑·未判色", color="", conf=0.0, sim_color=True)
    else:
        _set_state(st, stage="判色")
        # ★★ 2026-10-10 用户口径："只要用户说的颜色是有效颜色，用户没提到的颜色是无效颜色"。
        #   这里传**全部 7 个可识别色**给投票器 → 检测器**如实报出真实颜色**（含橙/黄/青/紫）；
        #   "放走 / 放回"交给下面的配额与摆法决定（用户没提到的颜色 → 配额为 0 → **放回原位**），
        #   且报的是**真实色名**（不是"无法识别"）—— 现场才分得清"是没识别到"还是"没要这个色"。
        v = _capture(ctx, 3, min_conf, "判色", st, valid_colors=S.RECOGNIZED_COLORS)
        _set_state(st, color=v["color"], conf=v["conf"])
        if not v["ok"]:
            # ★★ 2026-10-10 用户口径："如果出现无效颜色放回原位继续"。
            #   不再**就地抛停**（那样件会掉在 C 区/半路，现场满地找件）——
            #   统一走"**先放回原格**"，再由 continue_on_invalid 决定接着干还是收工。
            invalid = True
            invalid_reason = str(v.get("reason") or "")
            _set_state(st, stage="无法识别·放回原位", suspect_pick=False)
    color = st.get("color") or ""

    # ---- 目标解析 ----
    # ★ 禁放格（2026-10-10 用户口径："B5、B9 禁止摆放" / "B6 不能放"）：
    #   列内**自动选槽**时一律跳过；用户口径里"禁止摆放"也含"这格已经有物体了"的意思 ——
    #   那一半由 `_occ["B"]` 占位表覆盖（已放过的格本就不会再被选中）。
    _skip = set(skip_to or ())
    if invalid:
        # ★ 无效色（黑/判不出）：**放回原格**（下面统一走落料流程），**不消耗配额**。
        tgt_slot, mode = src, "back"
    elif quotas is not None:
        # ★★ 按颜色配额（"3个红色，2个蓝色，1个绿色"）：
        #   判色结果说了算 —— 该颜色还缺就放走，不缺或**没判出颜色**就放回原位。
        #   ⚠ 放到 B 区还是放回 A 区，走的是**同一条**已有路径（place / back），
        #     没有新增任何运动原语。
        want = int(quotas.get(color) or 0) if color else 0
        if want > 0 and plan_slots is not None:
            # ★★ 2026-10-10：按**解析阶段解出来的摆法**落位（同色成行/成列），
            #   不再走写死的"颜色→列"；并且**按队列原地消耗**（放一件少一格）。
            #   ⚠ 以前写的是 `list(plan_slots.get(color))` —— 复制一份再 pop，
            #     pop 不留痕 ⇒ 同一色多件会**全落在同一格**（现场就是"叠在一起"）。
            cands = plan_slots.get(color)
            if cands is None:
                cands = []
            while cands and _occ["B"].get(cands[0]):   # 该格已被占（已有料等）→ 顺延
                cands.pop(0)
            if not cands:
                raise _Stop("「%s」的摆法里已没有可用格（可能被已有料/禁放占掉）" % color, "选槽")
            quotas[color] = want - 1          # 真放才扣配额
            tgt_slot, mode = cands.pop(0), "place"
        elif want > 0:
            quotas[color] = want - 1
            cands = [s for s in NL.color_column_slots(color, zone)
                     if not _occ["B"].get(s) and s not in _skip]
            if not cands:
                quotas[color] = want          # 放不进去就把配额还回去（否则会凭空少一件）
                raise _Stop("B 区「%s」列已满" % color, "选槽")
            tgt_slot, mode = cands[0], "place"
        else:
            tgt_slot, mode = src, "back"      # → 放回原位
    elif look_only:
        tgt_slot, mode = src, "back"          # 看颜色：放回原格
    elif dst:
        tgt_slot, mode = dst, "place"         # 指定格
    else:
        want = prefer_color or color
        if not want:
            raise _Stop("判色未得到颜色，无法选目标槽位", "选槽")
        cands = [s for s in NL.color_column_slots(want, zone)
                 if not _occ["B"].get(s) and s not in _skip]
        if not cands:
            raise _Stop("B 区「%s」列已满%s" % (
                want, "（被禁用格占掉了 %s）" % "、".join(sorted(_skip)) if _skip else ""), "选槽")
        tgt_slot, mode = cands[0], "place"
    S_ok = tgt_slot in pmap
    if not S_ok:
        raise _Stop("目标点位 %s 未示教" % tgt_slot, "放料")

    _set_state(st, target=tgt_slot, stage="放料·正上方")
    _move(ctx, pmap, tgt_slot + "上", speed, dry, "放料·正上方", st)
    # ★ 工艺顺序（与分拣同）：到放料位 → ① 停止吸 ② 放气 ③ 回起点后停止放气。
    #   吸气一路保持到**这里**才断（用户口径："到放料时才停止吸住"）。
    _air(ctx, "release", dry, "放料·停止吸", st)
    _job["suck_on"] = False
    _air(ctx, "blow", dry, "放料·放气", st)
    _job["blow_on"] = True
    _set_state(st, holding=False)
    # 回起点后再停吹（与分拣用户口径一致）
    _move(ctx, pmap, S.START_POINT, speed, dry, "回起点", st)
    _air(ctx, "unblow", dry, "停止放气", st)
    _job["blow_on"] = False

    # ---- 占位表更新（**按本页口径**，见模块头）----
    if mode == "place" and not dry:
        _occ["A"][src] = False
        _occ["B"][tgt_slot] = color or "?"
    emit_event("ai", "info", "ai.piece",
               "%s → %s%s（%s）" % (src, tgt_slot,
                                    "（放回原格）" if mode == "back" else "",
                                    ("颜色 %s" % color) if color else "未判色"),
               {"src": src, "dst": tgt_slot, "color": color, "mode": mode,
                "dry_run": dry}, actor="control")

    # ---- 无效色（黑/判不出）：已放回原格，这里决定"继续"还是"收工" ----
    if invalid:
        if continue_on_invalid:
            # ★ 用户口径："如果出现无效颜色放回原位继续" —— 不消耗配额，去试下一件。
            #   循环上限是"A 区当前可取的件数"（不是固定次数），所以 1/2/3 个黑都只是多试几次。
            emit_event("ai", "warn", "ai.piece_invalid",
                       "无效颜色（%s）→ 已放回原格 %s，继续下一件" % (invalid_reason, src),
                       {"src": src, "reason": invalid_reason}, actor="control")
            _set_state(st, stage="无效色·已放回，继续", color="", conf=0.0,
                       holding=False, target="")
            return
        # 旧口径：放回原格后**整轮停**（不再像以前那样就地抛 —— 件会掉在 C 区）
        raise _Stop("颜色无法识别（%s），已放回原位 %s" % (invalid_reason, src), "判色")
