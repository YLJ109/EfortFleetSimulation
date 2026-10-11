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
    EFORT_LLM_BASE_URL=https://api.deepseek.com   # 任何 OpenAI 兼容端点
    EFORT_LLM_API_KEY=sk-xxxx                        # 没有它 = 关闭大模型
    EFORT_LLM_MODEL=deepseek-flash                   # DeepSeek V4 Flash（默认名字）
    EFORT_LLM_TIMEOUT=12
    EFORT_LLM_THINKING=0                             # DeepSeek 思考模式：0=关（默认）、1=开、空=不带参数
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from app.core.logger import get_logger

log = get_logger("llm")

# 允许的动作白名单（与 nl_intent.INTENTS 一致）—— 大模型不许发明新动作。
# ★★ 2026-10-10 用户口径（"每句话必须大模型推理"）：解析入口改为**大模型优先**，
#   所以白名单扩到**全部 9 个意图**（含配额/盘点/只读查询）—— 否则这些句子会掉回规则，
#   违背"不是有些解析规则"。规则只在**大模型不可用时显式降级**。
ALLOWED_ACTIONS = ("look_color", "move_one", "move_all", "move_quota", "sorting",
                   "estop", "goto_start", "inventory", "query_state")
ALLOWED_SORTING = ("start", "stop", "pause", "resume")
ALLOWED_ZONES = ("A", "B")
# ★ 摆放约束（与 services/placement.PATTERNS 同口径，含 2026-10-10 新增的"相邻"）。
ALLOWED_PATTERNS = ("row_or_col", "row", "col",
                    "adjacent", "adjacent_row", "adjacent_col", "any")
ALLOWED_ON_INVALID = ("stop", "continue")
ALLOWED_BAN_DIR = ("from", "to", "have")

SYSTEM_PROMPT = """你是工业机器人分拣系统的**指令解析器**。把用户的中文指令翻译成 JSON，只输出 JSON，不要解释、不要 markdown。

# 动作（action）—— 只能是下面 9 种之一
1. "look_color"  看某格颜色（会把件取到相机位判色） —— {"action":"look_color","from":{"zone":"A","slot":"A1"}}
2. "move_one"    搬一件 —— {"action":"move_one","from":{loc},"to":{loc},"shuffle":false}
3. "move_all"    把某区**全部**搬走 —— {"action":"move_all","from":{"zone":"A"},"to":{loc},"on_invalid":"continue|stop"}
4. "move_quota"  按颜色各取几件 —— {"action":"move_quota","to":{"zone":"B"},"quotas":{"蓝":3,"绿":2,"红":1},"pattern":"...","on_invalid":"continue|stop"}
5. "sorting"     流程控制 —— {"action":"sorting","op":"start|stop|pause|resume"}
   ★ 要「快速完成 / 双面 / 一趟两件」→ 开始分拣时**加** "dual_face":true
     （如 {"action":"sorting","op":"start","dual_face":true}）；**没提到 / 只说"快速"二字 → 不加**（默认一面）。
6. "estop"       只有用户**明确说「急停」**才用 —— {"action":"estop"}
   ★「停下/停止」是软停 → 用 sorting+stop，不要用 estop。
7. "goto_start"  回开始/结束点 —— {"action":"goto_start"}
   ★「回到起点 / 回原点 / 回零位 / 回原位 / 归位」都是这个。
8. "inventory"   盘点 A 区（逐件拍照判色后**放回原处**） —— {"action":"inventory"}
9. "query_state" **只读**查询 —— {"action":"query_state"}
   ★「A区有几个红 / 有几个盒子 / 都是什么颜色 / 有没有空位」这类**问句**用它（不要输出动作）。
（实在拿不准就 {"action":"none","reason":"简短原因"}，绝不猜。）

# 地点 {loc} —— 三种写法，只选一种
- 具体格：{"zone":"A|B","slot":"A1"}   格号：A区 A1..A9、B区 B1..B9（**行优先**：
  第1行第1列=A1、第1行第2列=A2、第1行第3列=A3、第2行第1列=A4 …）
- 颜色列（只对 B 区有意义）：{"zone":"B","color":"红|绿|蓝"}   ★ B 区第1列=红、第2列=绿、第3列=蓝
- 整区：{"zone":"A"} 或 {"zone":"B"}

# 可选字段（只翻约束；数量必须照抄用户说的数字）
- "pattern": "row_or_col"（一行或一列都行）｜"row"（必须同一行）｜"col"（必须同一列）
  ｜"adjacent"（同一行或一列且**相邻**）｜"adjacent_row"｜"adjacent_col"
  ★ 用户说「相邻 / 挨着 / 紧挨 / 连在一起 / 不能隔空 / 中间不能有盒子」→ adjacent；
    说「同色一行或者一列」→ row_or_col。
- "on_invalid": "continue"（判不出色/黑色 → 放回原格**继续**）｜"stop"（放回后停）
  ★ 用户说「如果出现无效颜色放回原位继续」→ continue。
- "quotas": {"颜色": 件数} —— 只有用户**明确说了**"几个某色"才给（例："3个蓝、2个绿、1个红"）。
  ★ 数字照抄用户的说法，**绝不自己编**；颜色只能是 红/绿/蓝。
- "ban": {"from":["A4"],"to":["B6"],"have":["B5","B9"]}
  ★「A4不能拿」→ from；「B6不能放 / B6禁止摆放」→ to；「B5已有物体 / 已经有料」→ **to 和 have 都写**。

# 铁律
- 输出**只有 JSON**；格号只能 A1..A9 / B1..B9；颜色只能 红/绿/蓝。
- 不要输出速度、寄存器、代码、多余解释。
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


def thinking_param() -> Optional[Dict]:
    """思考模式开关（**DeepSeek V4 专用**）。

    ★★ 2026-10-10 实测（接 DeepSeek 时踩到的）：DeepSeek-V4-Flash **默认开思考模式** ——
      思维链走 `reasoning_content`，正文 `content` 可能是**空串**
      （实测 max_tokens 小时 content 直接是 ""，本服务会误判成"大模型没给 JSON/空回复"）；
      而且思考模式下 `temperature` **不生效**。
      我们只干两件事：把话翻成受限 JSON（要确定性）+ 闲聊问答（要快）——
      都不需要思维链 → 默认**关掉**。
    ★ 只对 **deepseek** 端点自动下发这个参数：别的 OpenAI 兼容端点不认它（可能 400）。
      强制开关：`EFORT_LLM_THINKING=1` 开 / `0` 关 / 设成**空串** = 完全不带该参数。
    """
    want = os.environ.get("EFORT_LLM_THINKING")
    if want is None:
        if "deepseek" not in base_url().lower():
            return None
        want = "0"
    want = str(want).strip().lower()
    if want in ("1", "true", "yes", "on"):
        return {"type": "enabled"}
    if want in ("0", "false", "no", "off"):
        return {"type": "disabled"}
    return None


def _pick_content(payload: Dict) -> Dict:
    """从响应里取正文；空正文但只有思维链时，**明确报出来**（别让人猜）。"""
    try:
        msg = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "error": "大模型响应结构异常", "content": ""}
    content = str(msg.get("content") or "").strip()
    if not content and str(msg.get("reasoning_content") or "").strip():
        return {"ok": False, "content": "",
                "error": "大模型只回了思维链（content 为空）—— 把 EFORT_LLM_THINKING 设为 0，"
                         "或换非思考模型"}
    return {"ok": True, "content": content}


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
    th = thinking_param()
    if th:
        body["thinking"] = th
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
        content = _pick_content(payload)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "大模型响应结构异常"}
    if not content["ok"]:
        return {"ok": False, "error": content["error"]}
    plan = _loads_lenient(content["content"])
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
    th = thinking_param()
    if th:
        body["thinking"] = th
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
        got = _pick_content(payload)
        if not got["ok"]:
            return {"ok": False, "error": got["error"]}
        reply = got["content"]
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
- 回位：如「回到起点」（运动到开始/结束点）
- 流程：「开始分拣」「暂停」「继续」「停止执行」「急停」

★ 铁律（违反会误导现场操作员，必须遵守）：
- **你没有任何执行权**：你不能让机器人动、不能启停流程。凡是"做动作"的事，都得用户
  说指令 → 出计划卡 → 说「执行」才发生。
- **禁止**说「已执行 / 正在执行 / 已经回到 / 已停下 / 已启动 / 已完成」这类话 ——
  你有没有做，操作员看画面就知道；编造等于谎报设备状态。
- 用户要你做机器人动作时，把他**该说的指令原话**给他（例：「你可以说：回到起点」），
  并说明"会先生成执行计划，说「执行」才动"。
- 「停下」要说清是**软停**（当前动作跑完才停）；真急停必须明说「急停」。

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


def _slots(xs) -> List[str]:
    """过滤出一组合法格号（A1..A9 / B1..B9）；非法项**逐个丢弃**（不因此拒绝整条指令）。"""
    out: List[str] = []
    for x in (xs or []):
        s = str(x or "").strip().upper()
        if len(s) == 2 and s[0] in ALLOWED_ZONES and s[1] in "123456789":
            out.append(s)
    return out


def _quotas(x) -> Dict[str, int]:
    """校验配额：只收 红/绿/蓝，1..9 的整数（数量编错在真机上是实打实多做件）。"""
    if not isinstance(x, dict):
        return {}
    out: Dict[str, int] = {}
    for k, v in x.items():
        c = str(k or "").strip()
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if c in ("红", "绿", "蓝") and 1 <= n <= 9:
            out[c] = n
    return out


def _opts(plan: Dict) -> Dict:
    """可选字段：摆放方式 / 无效色处置 / 逐格约束（乱值丢弃，不拒绝整条指令）。"""
    out: Dict[str, object] = {}
    pat = str(plan.get("pattern") or "").strip().lower()
    if pat in ALLOWED_PATTERNS:
        out["pattern"] = pat
    oi = str(plan.get("on_invalid") or "").strip().lower()
    if oi in ALLOWED_ON_INVALID:
        out["on_invalid"] = oi
    b = plan.get("ban")
    if isinstance(b, dict):
        ban = {k: _slots(b.get(k)) for k in ALLOWED_BAN_DIR if k in b}
        ban = {k: v for k, v in ban.items() if v}
        if ban:
            out["ban"] = ban
    return out


def sanitize(plan: Dict) -> Dict:
    """白名单校验：动作/操作/地点/约束全部过滤；不合格 → ok=False 并说明。

    ★ 这是"大模型会编"的唯一防线：编出来的动作、不存在的格号、越界数量，在这里被丢掉。
    ★★ 2026-10-10：白名单覆盖**全部 9 个意图**（大模型优先口径），产出与 `nl_intent`
      完全同构的 plan —— 这样下游（_echo/_run/可行性闸门）不用区分"谁解析的"。
    """
    action = str(plan.get("action") or "").strip()
    if action not in ALLOWED_ACTIONS:
        return {"ok": False,
                "error": "大模型给的动作不在白名单内（%s）" % (action or "空")}
    src = "llm"
    if action in ("estop", "goto_start", "inventory", "query_state"):
        return {"ok": True, "plan": {"action": action}, "source": src}
    if action == "sorting":
        op = str(plan.get("op") or plan.get("action2") or "").strip().lower()
        if op not in ALLOWED_SORTING:
            return {"ok": False, "error": "大模型给的流程操作不在白名单内（%s）" % (op or "空")}
        out = {"action": "sorting", "op": op}
        # ★★ 2026-10-11（方案 §C）双面（快速完成）透传：**只认真正的布尔 True** ——
        #   非 bool（含 "true" 字符串）一律**忽略**（回落一面）。大模型降级/没解析出 → 一面。
        if op == "start" and plan.get("dual_face") is True:
            out["dual_face"] = True
        return {"ok": True, "plan": out, "source": src}

    opts = _opts(plan)
    if action == "look_color":
        loc = _loc(plan.get("from"), "A")
        if not loc or loc["kind"] != "slot":
            return {"ok": False, "error": "大模型没给出有效的格号（看颜色要指定具体某一格）"}
        return {"ok": True, "plan": {"action": "look_color", "from": loc, **opts}, "source": src}

    dst = _loc(plan.get("to"), "B")
    if action == "move_quota":
        quotas = _quotas(plan.get("quotas"))
        if not quotas:
            return {"ok": False, "error": "按颜色搬运必须给出每种颜色几件（如「3个蓝、2个绿」）"}
        if not dst or dst["kind"] not in ("color_col", "zone"):
            return {"ok": False,
                    "error": "按颜色搬运要放到**颜色列**或整区（例如「搬到B区」），不能指定具体某一格"}
        return {"ok": True, "plan": {"action": "move_quota",
                                     "from": {"kind": "all", "zone": "A"},
                                     "to": dst, "quotas": quotas, **opts}, "source": src}
    # move_one / move_all
    src_loc = _loc(plan.get("from"), "A")
    if not src_loc:
        return {"ok": False, "error": "大模型没给出有效的起点"}
    if not dst:
        return {"ok": False, "error": "大模型没给出有效的目标"}
    out = {"action": action, "from": src_loc, "to": dst,
           "shuffle": bool(plan.get("shuffle")), **opts}
    return {"ok": True, "plan": out, "source": src}
