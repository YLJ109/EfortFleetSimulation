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
from app.api.control import new_run, request_cancel
from app.api.sorting import (_Stop, _air, _capture, _joints_of, _move,  # 复用同一套原语
                            _set as _set_state)
from app.core.logger import get_logger
from app.db.crud import list_points
from app.db.database import SessionLocal
from app.services import llm, nl_intent as NL
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
        return {"title": "分拣流程", "detail": zh,
                "note": "「停止」= 当前动作结束后停并回起点；急停请明说「急停」"}
    if a == "look_color":
        return {"title": "看颜色",
                "detail": "取 %s 的件送到 C 区判色，判完放回原格" % _loc_zh(plan.get("from"), "from"),
                "note": "这是运动：要真取真放"}
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


def _parse_to_plan(text: str) -> Dict:
    """规则优先 → 大模型兜底的**纯解析**（不执行）。

    ★ 2026-10-10 从 api_parse 抽出：/parse 与 /chat（对话模式）共用同一套
      解析 + 计划缓存逻辑 —— "确认的就是解析出来的那一份"口径不变。
    """
    text = (text or "").strip()
    res = NL.parse(text)
    source = "rule"
    # ★ 调用方明确要求"要依赖大模型"：规则失败时尝试大模型，仍失败则原样返回规则报错。
    if not res.get("ok") and llm.enabled():
        lr = llm.parse(text)
        if lr.get("ok"):
            # 大模型只给"动作 + 地点"，槽位/空位仍由本模块解析（不让它碰具体格）
            p = dict(lr["plan"])
            if p.get("action") in ("move_one", "move_all"):
                src = _resolve_src(p.get("from") or {})
                dst = _resolve_loc(p.get("to") or {})
                if not src.get("ok"):
                    res = {"ok": False, "raw": text, "error": "大模型解析的起点不可用：" + src["error"]}
                elif not dst.get("ok"):
                    res = {"ok": False, "raw": text, "error": "大模型解析的目标不可用：" + dst["error"]}
                else:
                    p["from"] = {"kind": "slot", "zone": src["slot"][:1], "slot": src["slot"],
                                 "zh": src.get("zh")}
                    p["to"] = {"kind": "slot", "zone": dst["slot"][:1], "slot": dst["slot"],
                               "zh": dst.get("zh")}
                    res = {"ok": True, "intent": p["action"], "plan": p, "warnings": [],
                           "raw": text, "error": ""}
                    source = "llm"
            elif p.get("action") == "look_color":
                src = _resolve_src(p.get("from") or {})
                if src.get("ok"):
                    p["from"] = {"kind": "slot", "zone": src["slot"][:1], "slot": src["slot"],
                                 "zh": src.get("zh")}
                    res = {"ok": True, "intent": "look_color", "plan": p, "warnings": [],
                           "raw": text, "error": ""}
                    source = "llm"
            elif p.get("action") in ("sorting", "estop"):
                res = {"ok": True, "intent": p["action"], "plan": p, "warnings": [],
                       "raw": text, "error": ""}
                source = "llm"
        else:
            res = dict(res)
            res["llm_error"] = lr.get("error") or ""

    out = {"ok": bool(res.get("ok")), "raw": text, "source": source,
           "intent": res.get("intent") or "", "plan": res.get("plan") or {},
           "warnings": list(res.get("warnings") or []),
           "error": res.get("error") or "",
           "llm_enabled": llm.enabled(),
           "llm_error": res.get("llm_error", ""),
           "batch_max": batch_max()}
    if out["ok"]:
        pid = uuid.uuid4().hex
        with _gate:
            _plans[pid] = {"plan": out["plan"], "at": time.time(), "raw": text}
        out["plan_id"] = pid
        out["echo"] = _echo(out["intent"], out["plan"])
        # 批量件数：先算清"这一轮会动多少件"，让人看到再确认
        if out["plan"].get("action") == "move_all":
            cands = _occupied(["A%d" % i for i in range(1, 10)]) \
                if (out["plan"].get("from") or {}).get("zone", "A") == "A" else []
            out["batch_count"] = min(len(cands), batch_max())
            out["over_limit"] = len(cands) > batch_max()
    return out


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
        return {"ok": True, "mode": "plan", "parsed": parsed,
                "source": parsed.get("source") or "rule"}
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
            r = api_start(StartIn(dry_run=True), tok)
            emit_event("ai", "info", "ai.sorting", "AI 指令：开始分拣（空跑起步）",
                       {"raw": rec["raw"]}, actor="control")
            return {"ok": bool(r.get("ok")), "action": "sorting", "op": "start",
                    "message": "已开始分拣（空跑）；确认无误后到分拣页切真机",
                    "state": r.get("state")}
        raise HTTPException(400, detail="未知的流程操作")

    # ---- 运动类：单件/批量/看颜色 ----
    if not body.confirm and act != "move_one":
        raise HTTPException(400, detail="这类指令需要先确认（点「确认执行」）")
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
        rp = {"action": "look_color",
              "from": {"kind": "slot", "scope": s["slot"],
                       "slot": s["slot"], "zh": s.get("zh")}}
    elif act == "move_one":
        s = _resolve_src(plan.get("from") or {})
        if not s.get("ok"):
            raise HTTPException(400, detail="起点不可用：" + s["error"])
        d = _resolve_loc(plan.get("to") or {})
        if not d.get("ok"):
            raise HTTPException(400, detail="目标不可用：" + d["error"])
        rp = {"action": "move_one",
              "from": {"kind": "slot", "slot": s["slot"], "zh": s.get("zh")},
              "to": {"kind": "slot", "slot": d["slot"], "zh": d.get("zh")}}
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
        rp = {"action": "move_quota",
              "from": {"kind": "all", "zone": "A"},
              "to": {"kind": "color_col", "zone": dstq.get("zone") or "B",
                     "color": dstq.get("color")},
              "quotas": quotas}
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
        rp = {"action": "move_all",
              "from": {"kind": "zone", "zone": zsrc},
              "to": {"kind": dk or "color_col", "zone": dst.get("zone") or "B",
                     "color": dst.get("color"), "slot": fixed},
              "shuffle": bool(plan.get("shuffle"))}

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
        th = threading.Thread(target=_run, args=(ctx, rp, pmap, st, dry, speed),
                              name="ai", daemon=True)
        _job["thread"] = th
    th.start()
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
    blow_on = False
    start = S.START_POINT
    try:
        if act == "look_color":
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
                       prefer_color=plan["to"].get("color") if color_col else None)
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
            srcs = _occupied(["A%d" % i for i in range(1, 10)])
            if not srcs:
                raise _Stop("A 区没有可取的件（按本页占位表判断）", "取料")
            _set_state(st, total=want_total)
            done = 0
            # ★ 上限 = A 区当前可取的件数：某颜色不够时**最多试完全部**，
            #   不会无限试下去（避免"永远等下去"）。
            for s in srcs:
                if sum(quotas.values()) <= 0:
                    break
                if ctx.cancelled():
                    stop_reason, phase = "已被停止", "cancelled"
                    break
                _set_state(st, piece=done + 1, src=s)
                before = dict(quotas)
                _piece(ctx, pmap, st, dry, s, None, False, speed, min_conf, quotas=quotas)
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
        if blow_on and not dry:
            try:
                _air(ctx, "unblow", False, "异常·停止放气", st)
            except Exception:  # noqa: BLE001
                pass
        _set_state(st, stage="结束", error=stop_reason, finished=True,
                   ok=(phase == "done"), phase=phase)
        # ★★ 2026-10-09 接管接力：本任务已收尾（含"取消 → 回起点"），
        #   把排队的新指令接上。放在 finally 的最后一行：
        #   此刻"回起点"已经走完，是启动新任务唯一可靠的时刻。
        _relay_pending()
        ctx.finish(phase)
        emit_event("ai", "info" if phase == "done" else "warn", "ai.stop",
                   "AI 执行%s%s" % ("完成" if phase == "done" else "结束",
                                   "" if phase == "done" else "：" + stop_reason),
                   {"phase": phase, "reason": stop_reason}, actor="control")


def _piece(ctx, pmap, st, dry, src: str, dst: Optional[str], look_only: bool,
           speed: int, min_conf: float,
           prefer_color: Optional[str] = None, zone: str = "B",
           quotas: Optional[Dict[str, int]] = None) -> None:
    """取一件 → 判色 → 放到目标（或放回原格 / 按颜色列选位 / **按配额决定放还是放回**）。

    ★ 与分拣引擎**同一套原语**（`_move/_air/_capture` 来自 api/sorting.py），
      所以"写目标 + 触发位""吸/停止吸/放/停止放""抓帧投票"只有一处实现。
    ★ `quotas`（用户口径 2026-10-09："全部要学会解析，**如果没有检测到放回原位**"）：
      传了它就是**按颜色配额**模式 —— 判色后看该颜色还缺不缺：
        · 还缺 → 放该颜色列，配额减一
        · 不缺 / **没判出颜色** → **放回原位**（不占 B 区、不消耗配额）
      配额是**原地修改**的（调用方持有同一份 dict，据此判断是否收工）。
    """
    cam = S.CAMERA_POINT
    judge = (not dry) and real_write_enabled()
    # ★ 状态字段用 src/dst（不能用 from —— 它是 Python 关键字，不能当关键字参数）
    _set_state(st, src=src, stage="取料", color="", conf=0.0, target="", holding=False,
               suspect_pick=False, judge=judge)
    _move(ctx, pmap, src + "上", speed, dry, "取料·上方", st)
    _move(ctx, pmap, src, speed, dry, "取料·下落", st)
    _air(ctx, "suck", dry, "取料·吸", st)
    _set_state(st, holding=True)
    _move(ctx, pmap, src + "上", speed, dry, "抬起", st)
    _air(ctx, "release", dry, "停止吸", st)
    _move(ctx, pmap, cam, speed, dry, "送检", st)
    _set_state(st, stage="停顿")
    if ctx.sleep(1.0):                      # C 区停顿 1 秒（与分拣同口径）
        raise _Stop("已被停止", "判色")
    if not judge:
        # 空跑 / 双闸未开：不判色（方块不会到 C 区）—— 与分拣同一条修正口径
        _set_state(st, stage="空跑·未判色", color="", conf=0.0, sim_color=True)
    else:
        _set_state(st, stage="判色")
        v = _capture(ctx, 3, min_conf, "判色", st)
        _set_state(st, color=v["color"], conf=v["conf"])
        if not v["ok"]:
            raise _Stop("检测不到（%s）" % v["reason"], "判色")
    color = st.get("color") or ""

    # ---- 目标解析 ----
    if quotas is not None:
        # ★★ 按颜色配额（"3个红色，2个蓝色，1个绿色"）：
        #   判色结果说了算 —— 该颜色还缺就放走，不缺或**没判出颜色**就放回原位。
        #   ⚠ 放到 B 区还是放回 A 区，走的是**同一条**已有路径（place / back），
        #     没有新增任何运动原语。
        want = int(quotas.get(color) or 0) if color else 0
        if want > 0:
            quotas[color] = want - 1
            cands = [s for s in NL.color_column_slots(color, zone) if not _occ["B"].get(s)]
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
        cands = [s for s in NL.color_column_slots(want, zone) if not _occ["B"].get(s)]
        if not cands:
            raise _Stop("B 区「%s」列已满" % want, "选槽")
        tgt_slot, mode = cands[0], "place"
    S_ok = tgt_slot in pmap
    if not S_ok:
        raise _Stop("目标点位 %s 未示教" % tgt_slot, "放料")

    _set_state(st, target=tgt_slot, stage="放料·正上方")
    _move(ctx, pmap, tgt_slot + "上", speed, dry, "放料·正上方", st)
    _air(ctx, "blow", dry, "放料·放气", st)
    _set_state(st, holding=False)
    # 回起点后再停吹（与分拣用户口径一致）
    _move(ctx, pmap, S.START_POINT, speed, dry, "回起点", st)
    _air(ctx, "unblow", dry, "停止放气", st)

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
