# -*- coding: utf-8 -*-
"""大模型解析（**可选增强**）：把自然语言翻成受限 JSON 意图。

★ 设计口径（与 docs/方案-点位推导与AI执行页 一致）：
  - 规则解析（services/nl_intent.py）**永远先跑**；只有它解析不出时才调大模型。
  - 没有 key / 断网 / 超时 / 返回垃圾 → **静默退回"规则解析失败"**，绝不因此报错崩链路。
    （现场经常没外网，AI 页必须"无 key 也能用"。）
  - 大模型**只负责翻译**：输出必须落在白名单 schema 内；点位名不在 38 点内 → 直接丢弃。
    它永远拿不到寄存器值、拿不到运动通道，也绝不执行任何东西。
  - key 只存在后端 .env，不回传前端（`/api/ai/config` 只说"开没开"）。

配置（.env）：
    EFORT_LLM_BASE_URL=https://api.openai.com/v1     # 任何 OpenAI 兼容端点
    EFORT_LLM_API_KEY=sk-xxxx                        # 没有它 = 关闭大模型
    EFORT_LLM_MODEL=gpt-4o-mini
    EFORT_LLM_TIMEOUT=12
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from app.core.logger import get_logger

log = get_logger("llm")

# 允许的动作白名单（与 nl_intent.INTENTS 一致）—— 大模型不许发明新动作
ALLOWED_ACTIONS = ("look_color", "move_one", "move_all", "sorting", "estop")
# ★ 2026-10-09：**故意不含 `move_quota`**（按颜色配额搬运，如"3个红色2个蓝色"）。
#   理由："要几个什么颜色"是**结构化配额**，规则解析（nl_intent）已能稳定识别；
#   让大模型也去生成 quotas，等于给它多开一个"可能编数量"的口子 ——
#   数量编错（"3个"变"8个"）在真机上是实打实的多做 5 件。
#   所以：规则解析认得就行；规则没认出来的说法，宁可不做也不让模型猜数量。
ALLOWED_SORTING = ("start", "stop", "pause", "resume")
ALLOWED_ZONES = ("A", "B")

SYSTEM_PROMPT = """你是工业机器人分拣系统的指令翻译器。把用户的中文指令翻译成 JSON，不要解释。

可用动作（action）只有 4 种：
1. "look_color" 看某格颜色 —— {"action":"look_color","from":{"zone":"A","slot":"A1"}}
2. "move_one"  搬一件 —— {"action":"move_one","from":{...},"to":{...}}
3. "move_all"  批量搬运 —— {"action":"move_all","from":{...},"to":{...},"shuffle":true/false}
4. "sorting"   流程控制 —— {"action":"sorting","op":"start|stop|pause|resume"}
（急停由系统关键词直接处理，你不必输出。）

地点（from/to）三种写法，只选一种：
- 具体格：{"zone":"A|B","slot":"A1"}      格子编号：A区 = A1..A9，B区 = B1..B9，
  行优先：第1行第1列=A1、第1行第2列=A2、第1行第3列=A3、第2行第1列=A4 …
- 颜色列（只对 B 区有意义）：{"zone":"B","color":"红|绿|蓝"}
  ★ B 区第1列 = 红列、第2列 = 绿列、第3列 = 蓝列
- 整区：{"zone":"A"}  或  {"zone":"B"}

规则：
- 输出**只有 JSON**，不要 markdown 代码块，不要多余文字。
- 拿不准就输出 {"action":"none","reason":"简短原因"}，绝不猜。
- 不要输出速度、不要输出寄存器、不要输出代码。
"""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    """是否配置了大模型（只看 key 在不在 —— 没 key 就纯规则跑，不让用户看到报错）。"""
    return bool(_env("EFORT_LLM_API_KEY"))


def model_name() -> str:
    return _env("EFORT_LLM_MODEL", "gpt-4o-mini")


def base_url() -> str:
    return _env("EFORT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def parse(text: str, timeout: Optional[float] = None) -> Dict:
    """调大模型拿 JSON。返回 {ok, plan, error}；任何异常都转成 ok=False（不抛）。"""
    if not enabled():
        return {"ok": False, "error": "未配置大模型（EFORT_LLM_API_KEY）"}
    try:
        to = float(timeout if timeout is not None else _env("EFORT_LLM_TIMEOUT", "12"))
    except ValueError:
        to = 12.0
    body = {
        "model": model_name(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 200,
    }
    req = urllib.request.Request(
        base_url() + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _env("EFORT_LLM_API_KEY")},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=to) as r:   # noqa: S310（URL 来自 .env 配置）
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        log.warning("大模型 HTTP %s: %s", e.code, detail)
        return {"ok": False, "error": "大模型返回 HTTP %s（%s）" % (e.code, detail or "无详情")}
    except Exception as e:  # noqa: BLE001
        log.warning("大模型调用失败：%s", e)
        return {"ok": False, "error": "大模型调用失败：%s" % e}

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "error": "大模型响应结构异常"}
    plan = _loads_lenient(content)
    if plan is None:
        return {"ok": False, "error": "大模型没给出可解析的 JSON"}
    return sanitize(plan)


def chat(text: str, history: Optional[List[Dict]] = None,
         timeout: Optional[float] = None) -> Dict:
    """自由对话（闲聊/问答/追问）—— **只许说话，不许碰控制**。

    ★ 2026-10-10 口径（用户："要一个能正常聊天的大模型"）：
      - 指令走 parse()（白名单 JSON，翻译器人格）；聊不走这条 —— 两条人格分开。
      - 这里是"对话人格"：可以正常回答问题、寒暄、解释系统功能；
        用户想动机器时，引导他去说白名单指令（不代下、不猜指令）。
      - history 只收 user/assistant 文本（前端已清洗），超长截断。
      - 任何异常都转成 ok=False（不抛），由调用方降级成固定引导话术。
    """
    if not enabled():
        return {"ok": False, "error": "未配置大模型（EFORT_LLM_API_KEY）"}
    try:
        to = float(timeout if timeout is not None else _env("EFORT_LLM_TIMEOUT", "12"))
    except ValueError:
        to = 12.0
    msgs = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for h in (history or [])[-10:]:          # 最多带 10 条历史，防 token 膨胀
        role = str(h.get("role") or "")
        content = str(h.get("content") or "")[:600]
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": text[:600]})
    body = {
        "model": model_name(),
        "messages": msgs,
        "temperature": 0.7,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        base_url() + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _env("EFORT_LLM_API_KEY")},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=to) as r:   # noqa: S310（URL 来自 .env 配置）
            payload = json.loads(r.read().decode("utf-8", "replace"))
        reply = str(payload["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        log.warning("大模型对话 HTTP %s: %s", e.code, detail)
        return {"ok": False, "error": "大模型返回 HTTP %s" % e.code}
    except Exception as e:  # noqa: BLE001
        log.warning("大模型对话调用失败：%s", e)
        return {"ok": False, "error": "大模型调用失败：%s" % e}
    if not reply:
        return {"ok": False, "error": "大模型返回了空回复"}
    return {"ok": True, "reply": reply}


# 对话人格：能闲聊、能答问；涉及"动机器"的话题只引导、不代下指令。
# ★ 与 parse() 的"翻译器人格"（输出纯 JSON）刻意不同 —— 两个 SYSTEM_PROMPT 互不复用。
CHAT_SYSTEM_PROMPT = """你是「工业机器人远程智控与数字孪生系统」页面里的助手小埃，\
部署在埃夫特 ER8-700H 六轴机器人分拣工作站。用简短中文口语回复（一般不超过 3 句）。

你可以正常聊天：打招呼、回答关于机器人/分拣系统/工业自动化的问题。
这台机器人支持的操作（用户说这些话会生成执行计划）：
- 搬运：如「把A区第1行第1列放到B区红列」「把A区全部搬到B区」
- 查询：如「A区第1行第1列是什么颜色」
- 流程：「开始分拣」「暂停」「继续」「停止执行」「急停」

原则：
- 用户想操作机器人但说得不清楚时，引导他直接说上面这些指令，**不要**替他编造指令。
- 不讨论武器/伤人等危险话题，拒绝并提醒安全规范。
- 不知道的事就说不知道，不编造。"""


def _loads_lenient(content: str) -> Optional[Dict]:
    """容错解析：模型偶尔会包 ```` ```json ```` 或加前缀说明，剥掉再试。"""
    s = (content or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s.strip("`")
        s = s.lstrip("json").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _loc(x, default_zone: str = "") -> Optional[Dict]:
    """校验/规范化一个地点：只允许 slot / color / zone 三种，非法 → None。"""
    if not isinstance(x, dict):
        return None
    zone = str(x.get("zone") or default_zone or "").strip().upper()
    if zone not in ALLOWED_ZONES:
        return None
    slot = str(x.get("slot") or "").strip().upper()
    if slot:
        if len(slot) != 2 or slot[0] not in ALLOWED_ZONES or slot[1] not in "123456789":
            return None
        return {"kind": "slot", "zone": slot[0], "slot": slot}
    color = str(x.get("color") or "").strip()
    if color:
        if color not in ("红", "绿", "蓝"):
            return None
        return {"kind": "color_col", "zone": zone, "color": color}
    return {"kind": "zone", "zone": zone}


def sanitize(plan: Dict) -> Dict:
    """白名单校验：动作/操作/地点全部过滤；不合格 → ok=False 并说明。

    ★ 这是"大模型会编"的唯一防线：编出来的动作、不存在的格号，在这里被丢掉。
    """
    action = str(plan.get("action") or "").strip()
    if action not in ALLOWED_ACTIONS:
        return {"ok": False,
                "error": "大模型给的动作不在白名单内（%s）" % (action or "空")}
    if action == "estop":
        return {"ok": True, "plan": {"action": "estop"}, "source": "llm"}
    if action == "sorting":
        op = str(plan.get("op") or plan.get("action2") or "").strip().lower()
        if op not in ALLOWED_SORTING:
            return {"ok": False, "error": "大模型给的流程操作不在白名单内（%s）" % (op or "空")}
        return {"ok": True, "plan": {"action": "sorting", "op": op}, "source": "llm"}
    if action == "look_color":
        src = _loc(plan.get("from"), "A")
        if not src or src["kind"] != "slot":
            return {"ok": False, "error": "大模型没给出有效的格号（看颜色要指定具体某一格）"}
        return {"ok": True, "plan": {"action": "look_color", "from": src}, "source": "llm"}
    # move_one / move_all
    src = _loc(plan.get("from"), "A")
    dst = _loc(plan.get("to"), "B")
    if not src:
        return {"ok": False, "error": "大模型没给出有效的起点"}
    if not dst:
        return {"ok": False, "error": "大模型没给出有效的目标"}
    out = {"action": action, "from": src, "to": dst,
           "shuffle": bool(plan.get("shuffle"))}
    return {"ok": True, "plan": out, "source": "llm"}
