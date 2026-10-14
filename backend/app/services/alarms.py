# -*- coding: utf-8 -*-
"""
报警合理化（C2）：工业报警管理（EEMUA-191 / ISA-18.2 思路）的最小可用版。

★★ 这个模块**只是显示与处置层**，不是第二套安全判据 ★★
  - 它**只观察**统一事件总线（`services/events.py::emit`）已经发出的报警，
    **不改任何报警源的判定**（围栏怎么算、就绪怎么判、气路位表，一个字节都不动）；
  - 它**不 import 任何写路径**（motion / modbus / jog / rc_ready / sim_robot / camera_client），
    也**不被**互锁读取 —— 反证钉在 `tests/test_alarms.py`：
    `safety_guard.check()` 的返回值与「搁置 / 抑制 / 确认了什么」**完全无关**。
    把报警「关掉」绝不可能让围栏放行。
  - 全仓消费者只有四个（名单被那条钉子钉死，多一个就要重新论证它不是在把它当判据用）：
    `services/events.py` 的观察钩子（只调 ingest）、`api/alarms.py`（唯一人操作入口，要令牌）、
    `main.py` 只挂路由、`services/daily_report.py` 只读日报（不许确认 / 搁置 / 改优先级）。

为什么要做：现场把十几路同源报警（控制器报警字 5005/3909、后端独立评估、
围栏 danger/hit、残影超差、通讯降级、视觉/AI 错误）**平铺**成事件流，结果是
「同一条刷屏 + 分不清哪条要命 + 反复确认同一条」。本模块给六件事：

  ① **报警字典**（唯一登记口 `register()`）：每条报警有 priority / consequence / action /
     source。字典里没有的 → 如实归入 `unclassified`（优先级写「未分级」，排在 high 之后、
     medium 之前 —— 「不知道有多严重」不许垫底），**仍然显示**，绝不因为没登记就吞掉。
  ② **关联抑制**（chrouping）：同一根因在 `GROUP_WINDOW_MS` 内重复出现 → 折叠成一条活动报警，
     记 `count` 与 `first_at/last_at`；**被折叠的条目逐条进明细**（`history()`，一条不丢）。
  ③ **延时确认**（on-delay）：抖动型报警（通讯瞬时超时等）要**连续存在超过 `delay_ms`**
     才升级为活动报警；升级前是 `pending` 候选态（列表与明细都看得到，标「候选中」）。
     ★★ 红线：`safety=True` 的报警（danger/hit/急停/围栏互锁/未清报警）**一律不许带延时** ——
     `register()` 直接拒绝这种登记（ ValueError ），而不是等到运行时才降级。
  ④ **分级处置**：critical/high 必须**人确认**（ack，需控制令牌）才算确认；
     **确认只清显示、不解除任何互锁 / 就绪闸门**；low 可按登记的 `auto_resolve_ms` 自动清除。
  ⑤ **搁置**（shelving）：非安全类报警可临时搁置，到期**自动回到活动态**
     （默认 `SHELVE_DEFAULT_MIN` 分钟）；安全类禁止搁置（如实 409 + 原因）。
  ⑥ **优先级不可被静默降级**：`priority` 只来自字典或操作员显式动作
     （`set_priority()`，必须带理由并出审计事件）；出现次数再多也只加 `count`。

时间窗取值的理由（现场口径，不是拍脑袋）：
  · `GROUP_WINDOW_MS = 15s`：真机上一条 5005 在就绪流程里会**秒级连报**（清→顶→再清），
    采集降级/恢复抖动也是秒级；15s 足够把「一次故障」并成一条，又短到「15 秒后再出现」
    被视作**又发生了一次**（中间操作员已经动过手，不能把新的故障并进旧的计数里）。
  · `LOW_AUTO_RESOLVE_MS = 60s`：low 是提示级（残影超差、单次登录失败），
    一分钟没有复发就说明过去了，不该一直占着列表逼人点确认。
  · `SHELVE_DEFAULT_MIN = 30`：与 §5.3 令牌默认时长同一量级（一个班次内的处置窗口）；
    搁置必须**到期自己回来**，否则就是「永久静音」，那是假安全。

水位（§5.5 铁律①）：`latest_seq` 是**只增不减**的计数器，任何状态变化都推进它；
前端刷新必须比较它，**不许**用列表长度（列表会被折叠 / 清除 / 裁环，长度不是单调的）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 优先级（四档 + 未分级）
P_CRITICAL = "critical"
P_HIGH = "high"
P_MEDIUM = "medium"
P_LOW = "low"
P_UNCLASSIFIED = "unclassified"        # ★ 未知不得塌成布尔、不得当成 low（§5.5 铁律③）
PRIORITIES = (P_CRITICAL, P_HIGH, P_MEDIUM, P_LOW)
PRIORITY_LABELS = {P_CRITICAL: "紧急", P_HIGH: "高", P_MEDIUM: "中",
                   P_LOW: "低", P_UNCLASSIFIED: "未分级"}

# 排序用：critical 最前，未分级紧跟 high（不知道有多严重不能垫底），low 最后
_RANK = {P_CRITICAL: 0, P_HIGH: 1, P_UNCLASSIFIED: 2, P_MEDIUM: 3, P_LOW: 4}

# ---------------------------------------------------------------- 时间窗（理由见模块 docstring）
GROUP_WINDOW_MS = 15_000
LOW_AUTO_RESOLVE_MS = 60_000
SHELVE_DEFAULT_MIN = 30
PENDING_EXPIRE_GRACE_MS = 5_000        # 候选态这么久没续上 → 过期（只落明细，不进活动列表）

HISTORY_MAX = 1000                     # 明细环形上限（更久的原始条目在审计表里，见 history()）
MAX_ACTIVE = 200                       # 活动台账上限（防 7×24 进程被历史键撑爆）

ALARM_LEVELS = ("warn", "error", "critical")   # 只有这些级别的事件算报警（info/debug 是流水）

# 自己出的审计事件**绝不能再被自己吃进去**（否则 emit→ingest→emit 无限递归）
AUDIT_ACTIONS = ("alarms.ack", "alarms.shelve", "alarms.unshelve",
                 "alarms.priority", "alarms.auto_clear", "alarms.stale_prune")

_STATE_ACTIVE = "active"
_STATE_PENDING = "pending"
_STATE_SHELVED = "shelved"


@dataclass(frozen=True)
class Rule:
    """报警字典的一条登记。"""
    id: str                                    # 登记号（= 事件动作名，或 rc.alarm.<码>）
    priority: str                              # critical / high / medium / low
    consequence: str                           # 这条意味着什么后果（一句话）
    action: str                                # 要求操作员做什么（一句话）
    source: str                                # 谁产生的（控制器报警字 / 后端独立评估 / 围栏…）
    title: str = ""                            # 界面短标题（缺省用 id）
    safety: bool = False                       # ★ 安全类：禁延时、禁搁置、禁自动清除
    delay_ms: int = 0                          # on-delay（抖动型）
    scope_fields: Tuple[str, ...] = ()         # 分区键（如区域）→ 不同分区**不合并**
    clear_actions: Tuple[str, ...] = ()        # 这些动作的事件一到就视为已恢复
    auto_resolve_ms: int = 0                   # 0 = 不自动清除（critical/high 必须人确认）


@dataclass
class Rec:
    """台账里的一条报警（可能是候选态 / 已搁置）。"""
    key: str
    rule_id: str
    action: str
    priority: str
    priority_source: str          # dictionary | operator | unregistered
    consequence: str
    action_required: str
    source: str
    title: str
    safety: bool
    delay_ms: int
    scope: str
    first_at: float
    last_at: float
    count: int = 1
    pending: bool = False
    acked: bool = False
    acked_at: Optional[float] = None
    acked_by: str = ""
    shelved: bool = False
    shelve_until: Optional[float] = None
    shelved_at: Optional[float] = None
    shelved_by: str = ""
    shelve_reason: str = ""
    note: str = ""
    last_event_id: Optional[int] = None
    last_message: str = ""
    priority_set_by: str = ""
    priority_reason: str = ""


_RULES: Dict[str, Rule] = {}
_BY_ACTION: Dict[str, str] = {}            # 事件动作名 → 登记号
_CLEAR_INDEX: Dict[str, List[str]] = {}    # 恢复动作名 → 要解除的登记号列表


def register(rule: Rule) -> Rule:
    """★ 报警字典的唯一登记口。

    两条红线在**登记时就地拒绝**（不是运行时才降级 —— 漏一条就晚了）：
      · 安全类报警不许带延时（danger/hit/急停/围栏互锁必须立刻可见）；
      · 安全类与 critical/high 不许自动清除（必须人确认）。
    """
    if not rule.id:
        raise ValueError("报警登记必须有 id")
    if rule.priority not in PRIORITIES:
        raise ValueError("priority 必须是 %s 之一，收到 %r" % ("、".join(PRIORITIES), rule.priority))
    if not (rule.consequence and rule.action):
        raise ValueError("报警登记必须写清 consequence 与 action：%s" % rule.id)
    if rule.safety and rule.delay_ms > 0:
        raise ValueError("红线：安全类报警禁止延时确认（on-delay）：%s" % rule.id)
    if rule.safety and rule.auto_resolve_ms > 0:
        raise ValueError("红线：安全类报警禁止自动清除：%s" % rule.id)
    if rule.priority in (P_CRITICAL, P_HIGH) and rule.auto_resolve_ms > 0:
        raise ValueError("critical/high 必须人确认，禁止自动清除：%s" % rule.id)
    if rule.id in _RULES:
        raise ValueError("报警登记号重复：%s" % rule.id)
    _RULES[rule.id] = rule
    _BY_ACTION[rule.id] = rule.id
    for a in rule.clear_actions:
        _CLEAR_INDEX.setdefault(a, []).append(rule.id)
    return rule


def rules() -> Dict[str, Rule]:
    """字典快照（只读）。"""
    return dict(_RULES)


def rule_of(action: str) -> Optional[Rule]:
    rid = _BY_ACTION.get(str(action or ""))
    return _RULES.get(rid) if rid else None


def priority_rank(priority: str) -> int:
    """优先级水位（0 最要命）。★ 未分级 = 2，排在 high 之后、medium 之前：
    「不知道有多严重」不许垫底 —— 垫底就等于让操作员先看不痛不痒的那条。"""
    return _RANK.get(str(priority or ""), 9)


# ---------------------------------------------------------------- 控制器报警字（alarm1/alarm2）
# ★ 真因长文案的唯一来源仍是 `rc_ready.ALARM_HINTS`（就绪流程里那份）；
#   这里只给「一句话后果 + 一句话动作」用于显示，**不改那边的判定与文案**。
CODE_RULES: Dict[int, Rule] = {}


def _register_code(code: int, priority: str, consequence: str, action: str) -> None:
    # ★ 登记号本身已经含报警码（rc.alarm.5005）→ **不再**设 scope_fields：
    #   设了会在「事件动作已登记（带码派生）」与「动作未登记、只从消息里认码」
    #   两条路径下产生两种键（rc.alarm.5005 与 rc.alarm.5005|5005），
    #   同一个物理报警就会在列表里显示两行 —— 键必须与来路无关。
    r = Rule(id="rc.alarm.%d" % code, priority=priority, consequence=consequence,
             action=action, source="控制器报警字（alarm1/alarm2）",
             title="控制器报警 %d" % code, safety=True)
    CODE_RULES[code] = r
    _RULES[r.id] = r          # 不进 _BY_ACTION：它由报警码派生，不是事件动作名


_register_code(1812, P_HIGH,
               "安全回路 / 安全门不满足：控制器拒绝执行任何运动指令",
               "检查安全门、光栅、外部急停是否已复位，再停机清报警")
_register_code(3909, P_HIGH,
               "示教器（bcc 客户端）通讯断开或未连接：远程加载与运行都依赖示教器在线",
               "查示教器线缆、电源与急停；恢复在线后重新「一键就绪」")
_register_code(5005, P_HIGH,
               "远程加载 / 运行程序错误：最常见是程序停在报警态且仍在运行，清报警会被它立刻顶上",
               "先停当前程序再清报警，然后重新加载（「一键就绪」已按此顺序执行）")
_register_code(4902, P_HIGH,
               "XPL 程序文件损坏：该程序号无法加载",
               "重新导出 / 保存该程序文件后重试")

_CODE_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def extract_codes(detail: Any = None, message: str = "") -> List[int]:
    """从事件里取控制器报警码（**只认结构化字段与报警语境**，不许到处抓数字）。

    ① `detail.alarm = [a1, a2]`（`modbus` 点动报警守卫写的结构化字段）→ 直接用；
    ② 消息里出现「报警」/「alarm」字样时，取其中成对的 4 位码（5005/3909 这类）。
    其它数字（第 3 次、15s、寄存器号 40103）**一律不当报警码** —— 抓错就是凭空造报警。
    """
    out: List[int] = []
    d = detail
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            d = None
    if isinstance(d, dict):
        v = d.get("alarm")
        if isinstance(v, (list, tuple)):
            for x in v:
                try:
                    n = int(x)
                except (TypeError, ValueError):
                    continue
                if n and n not in out:
                    out.append(n)
    txt = str(message or "")
    if ("报警" in txt) or ("alarm" in txt.lower()):
        for m in _CODE_RE.finditer(txt):
            n = int(m.group(1))
            if n not in out:
                out.append(n)
    return out


def unclassified_rule(action: str, category: str = "",
                      code: Optional[int] = None) -> Rule:
    """字典里没有的报警：如实标「未分级」，**仍然显示**，绝不吞掉。"""
    rid = ("rc.alarm.%d" % code) if code else ("unregistered:%s" % (action or "?"))
    return Rule(
        id=rid, priority=P_UNCLASSIFIED,
        consequence="未在报警字典登记：后端不知道这条的确切后果，界面按未知如实显示（不猜、不降级）",
        action="请核对真因；确认后在 services/alarms.py 的字典里登记这条（含优先级/后果/要求动作）",
        source="事件总线（category=%s）" % (category or "?"),
        title=("控制器报警 %d（未登记）" % code) if code else ("未登记报警：%s" % (action or "?")),
        safety=False, delay_ms=0)


# ---------------------------------------------------------------- 事件 → 报警
def detail_of(event: Dict[str, Any]) -> Dict[str, Any]:
    d = event.get("detail")
    if isinstance(d, dict):
        return d
    if isinstance(d, str) and d.strip():
        try:
            o = json.loads(d)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def _scope_of(rule: Rule, event: Dict[str, Any], detail: Dict[str, Any]) -> str:
    """分区键：区域 / 报警码…同规则不同分区**不合并**（两个区域被入侵是两条报警）。"""
    if not rule.scope_fields:
        return ""
    for f in rule.scope_fields:
        v = detail.get(f) or event.get(f)
        if v:
            return str(v)[:80]
    return ""


def resolve_rule(action: str, category: str = "", detail: Any = None,
                 message: str = "") -> Tuple[Rule, bool]:
    """@return (规则, 是否来自字典)。字典没有 → 未分级规则（仍然显示，不吞）。"""
    r = rule_of(action)
    if r is not None:
        return r, True
    codes = extract_codes(detail, message)
    if codes:
        code = codes[0]
        known = CODE_RULES.get(code)
        return (known or unclassified_rule(action, category, code)), (known is not None)
    return unclassified_rule(action, category), False


def rules_for_event(event: Dict[str, Any]) -> List[Tuple[Rule, Dict[str, Any]]]:
    """一条事件可以出**多条**报警：例如「下发被拦（后果）」+「控制器报警 5005（根因）」。

    根因与后果分开登记，操作员才分得清哪条要命（这正是本轮要解决的问题）。
    ★ 报警码**逐条**成报警，登记过的用字典优先级、没登记过的归「未分级」也照实显示；
      这样动作名在不在字典里、码是第几个，都不会让某一路根因悄悄消失。
    """
    action = str(event.get("action") or "")
    category = str(event.get("category") or "")
    message = str(event.get("message") or "")
    detail = detail_of(event)
    main, _from_dict = resolve_rule(action, category, detail, message)
    out: List[Tuple[Rule, Dict[str, Any]]] = [(main, detail)]
    for code in extract_codes(detail, message):
        r = CODE_RULES.get(code) or unclassified_rule(action, category, code)
        if r.id != main.id:
            out.append((r, detail))
    return out


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


class AlarmOpError(Exception):
    """人操作被如实拒绝（不是内部错误）。API 层翻成 409 + 原因。"""


# ---------------------------------------------------------------- 报警中心
class AlarmCenter:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._recs: Dict[str, Rec] = {}
        self._hist: deque = deque(maxlen=HISTORY_MAX)
        self._seq = 0                 # ★ 只增不减的水位
        self._hseq = 0                # 明细序号（同样单调）
        self._events_seen = 0
        self._alarms_raised = 0
        self._unregistered = 0
        self._audit_q: List[Tuple[str, str, str, Any, str]] = []

    # ---------------- 水位 ----------------
    def _bump(self) -> int:
        self._seq += 1
        return self._seq

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def history_seq(self) -> int:
        with self._lock:
            return self._hseq

    # ---------------- 摄入（唯一入口：events.emit 的观察者） ----------------
    def ingest(self, event: Dict[str, Any], now: Optional[float] = None) -> List[str]:
        """吃一条事件总线载荷 → 更新报警台账。@return 触碰到的报警键列表（可能为空）。

        ★ 纯内存计算：不碰 DB、不碰任何写路径模块；调用方（events.emit）负责吞掉异常，
          报警层坏了绝不能把运动控制 / 采集 / 鉴权带崩。
        """
        if not isinstance(event, dict):
            return []
        action = str(event.get("action") or "")
        if not action or action in AUDIT_ACTIONS or action.startswith("alarms."):
            return []                              # 自己的审计不回吃（防递归）
        ts = float(now) if now is not None else time.time()
        touched: List[str] = []
        with self._lock:
            self._events_seen += 1
            self._expire_shelves(ts)
            # ① 恢复事件（level 通常是 info）：先按登记的 clear_actions 解除
            if action in _CLEAR_INDEX:
                touched += self._resolve_by_clear(action, event, ts)
            if str(event.get("level") or "") not in ALARM_LEVELS:
                changed = bool(touched)
            else:
                changed = True
                for rule, detail in rules_for_event(event):
                    k = self._touch(rule, event, detail, ts)
                    if k:
                        touched.append(k)
                self._prune_locked()
            if changed:
                self._bump()
        self._flush_audits()
        return touched

    def _touch(self, rule: Rule, event: Dict[str, Any], detail: Dict[str, Any],
               ts: float) -> str:
        scope = _scope_of(rule, event, detail)
        key = rule.id + (("|" + scope) if scope else "")
        level = str(event.get("level") or "warn")
        msg = str(event.get("message") or "")[:400]
        eid = event.get("id")
        eid = int(eid) if isinstance(eid, (int, float)) else None
        old = self._recs.get(key)
        if old is None:
            rec = Rec(
                key=key, rule_id=rule.id, action=str(event.get("action") or ""),
                priority=rule.priority,
                priority_source=("dictionary" if rule.priority in PRIORITIES else "unregistered"),
                consequence=rule.consequence, action_required=rule.action, source=rule.source,
                title=rule.title or rule.id, safety=rule.safety, delay_ms=rule.delay_ms,
                scope=scope, first_at=ts, last_at=ts, count=1,
                pending=(rule.delay_ms > 0), note=self._origin_note(rule),
                last_event_id=eid, last_message=msg)
            self._recs[key] = rec
            self._alarms_raised += 1
            if rec.priority_source == "unregistered":
                self._unregistered += 1
            self._log(rec, ts, event, "pending" if rec.pending else "active", level)
            return key
        # 已有同一条：窗内 = 同一次故障（折叠计数）；超窗 = 又发生了一次（新起一轮）
        if (ts - old.last_at) * 1000.0 > GROUP_WINDOW_MS:
            prev_acked = old.acked
            old.first_at = ts
            old.count = 1
            old.pending = (old.delay_ms > 0)
            old.acked = False
            old.acked_at = None
            old.acked_by = ""
            old.note = ("上一轮已确认后再次出现 = 又发生了一次（不并入旧计数，确认不是永久静音）"
                        if prev_acked else
                        "距上一轮超过 %ds 再次出现：按新一次故障登记" % (GROUP_WINDOW_MS // 1000))
        else:
            old.count += 1
        old.last_at = ts
        old.last_message = msg
        old.last_event_id = eid if eid is not None else old.last_event_id
        old.action = str(event.get("action") or old.action)
        # ★ 优先级**只**来自字典或操作员显式动作（set_priority）：重复出现绝不改动它（铁律⑥）
        if old.pending and old.delay_ms > 0 and (ts - old.first_at) * 1000.0 >= old.delay_ms:
            old.pending = False
            old.note = "候选持续 %.1fs（≥ %dms）→ 升级为活动报警" % (
                (ts - old.first_at), old.delay_ms)
        self._log(old, ts, event,
                  "pending" if old.pending else ("shelved" if old.shelved else "folded"), level)
        return old.key

    @staticmethod
    def _origin_note(rule: Rule) -> str:
        if rule.delay_ms > 0:
            return "抖动型：需连续存在 %dms 才升级为活动报警（当前是候选）" % rule.delay_ms
        if rule.priority == P_UNCLASSIFIED:
            return "未在字典登记 → 优先级未知，仍照常显示"
        if rule.safety:
            return "安全类：不延时、不可搁置、只能人确认"
        return ""

    def _log(self, rec: Rec, ts: float, event: Dict[str, Any], state: str, level: str) -> None:
        """明细：每一次**真实发生**都落一条（被折叠的也落，只是在列表上不重复显示）。"""
        self._hseq += 1
        eid = event.get("id")
        self._hist.appendleft({
            "seq": self._hseq,
            "at": ts,
            "at_iso": _iso(ts),
            "key": rec.key,
            "rule_id": rec.rule_id,
            "action": str(event.get("action") or ""),
            "level": level,
            "priority": rec.priority,
            "state": state,                  # active | pending | folded | shelved | cleared
            "event_id": (int(eid) if isinstance(eid, (int, float)) else None),
            "message": str(event.get("message") or "")[:400],
            "category": str(event.get("category") or ""),
            "count_at_this_point": rec.count,
        })

    def _detail_row(self, rec: Rec, ts: float, state: str, message: str) -> None:
        self._hseq += 1
        self._hist.appendleft({
            "seq": self._hseq, "at": ts, "at_iso": _iso(ts), "key": rec.key,
            "rule_id": rec.rule_id, "action": rec.action, "level": "info",
            "priority": rec.priority, "state": state, "event_id": None,
            "message": message[:400], "category": "control",
            "count_at_this_point": rec.count})

    # ---------------- 到期 / 恢复 / 自动清除 ----------------
    def _expire_shelves(self, ts: float) -> None:
        for rec in list(self._recs.values()):
            if rec.shelved and rec.shelve_until is not None and ts >= rec.shelve_until:
                rec.shelved = False
                rec.shelve_until = None
                rec.shelved_at = None
                rec.shelved_by = ""
                rec.shelve_reason = ""
                rec.note = "搁置到期 → 自动回到活动态（搁置不是永久静音）"
                self._detail_row(rec, ts, "shelve_expired", rec.note)
                self._bump()

    def _expire_pending(self, ts: float) -> None:
        """候选态一直没续上 → 不进活动列表，只在明细里留痕（说明为什么没升级）。"""
        for rec in list(self._recs.values()):
            if not rec.pending:
                continue
            if (ts - rec.last_at) * 1000.0 > max(rec.delay_ms, 0) + PENDING_EXPIRE_GRACE_MS:
                self._drop(rec, ts, "pending_expired",
                           "抖动候选未达持续时长（%dms）→ 没有升级为活动报警；"
                           "明细里仍查得到这一次" % rec.delay_ms)

    def _auto_resolve(self, ts: float) -> None:
        """只有字典里登记了 auto_resolve_ms 的（low 级）才自动清除。

        ★ 判据在 `register()` 里就钉死了：安全类与 critical/high 不许登记 auto_resolve_ms
          —— 所以这里**不可能**自动清掉一条要命的报警（反证测试另有一条）。
        """
        for rec in list(self._recs.values()):
            if rec.shelved or rec.pending or rec.safety:
                continue
            rule = _RULES.get(rec.rule_id)
            ttl = rule.auto_resolve_ms if rule else 0
            if ttl and (ts - rec.last_at) * 1000.0 > ttl:
                self._drop(rec, ts, "auto",
                           "超过 %.0fs 没有复发，提示级报警自动清除（没有复发的提示不必逼人确认）"
                           % (ttl / 1000.0))

    def _resolve_by_clear(self, action: str, event: Dict[str, Any], ts: float) -> List[str]:
        targets = _CLEAR_INDEX.get(action) or []
        d = detail_of(event)
        scope = ""
        for f in ("zone_id", "zone_name"):
            if d.get(f):
                scope = str(d[f])[:80]
                break
        hit: List[str] = []
        for key, rec in list(self._recs.items()):
            if rec.rule_id not in targets:
                continue
            if scope and rec.scope and rec.scope != scope:
                continue                       # 分区不同：A 区恢复不清掉 B 区
            hit.append(key)
            self._drop(rec, ts, "restore_event", "收到恢复事件：%s" % action)
        return hit

    def _drop(self, rec: Rec, ts: float, cleared_by: str, why: str) -> None:
        self._recs.pop(rec.key, None)
        rec.note = why
        self._detail_row(rec, ts, "cleared",
                         "%s（清除方式：%s，本轮共出现 %d 次）" % (why, cleared_by, rec.count))
        if cleared_by == "auto":
            self._queue_audit(
                "alarms.auto_clear", "info",
                "报警自动清除：%s（%s）" % (rec.title, why),
                {"key": rec.key, "priority": rec.priority, "count": rec.count}, "system")
        self._bump()

    def _prune_locked(self) -> None:
        if len(self._recs) <= MAX_ACTIVE:
            return
        order = sorted(self._recs.values(), key=lambda r: r.last_at)
        victims = order[: max(0, len(self._recs) - MAX_ACTIVE)]
        for rec in victims:
            self._recs.pop(rec.key, None)
            self._detail_row(rec, time.time(), "cleared",
                             "报警台账超过 %d 条，折叠了最久没有再出现的条目（明细仍保留这一条记录）"
                             % MAX_ACTIVE)
        self._bump()

    # ---------------- 人操作（唯一能改显示状态与优先级的入口） ----------------
    def ack(self, keys: List[str], actor: str = "operator", note: str = "",
            now: Optional[float] = None) -> Dict[str, Any]:
        """确认 = **只清显示**。不解除任何互锁 / 就绪闸门 / 扫掠结论。"""
        ts = float(now) if now is not None else time.time()
        done: List[str] = []
        missing: List[str] = []
        with self._lock:
            self._expire_shelves(ts)
            for raw in keys or []:
                k = str(raw)
                rec = self._recs.get(k)
                if rec is None:
                    missing.append(k)
                    continue
                who = actor or "operator"
                rec.acked = True
                rec.acked_at = ts
                rec.acked_by = who
                self._drop(rec, ts, "ack",
                           "已由%s确认（只清显示，不解除任何互锁 / 就绪闸门 / 扫掠结论）%s"
                           % (who, ("：" + note)[:120] if note else ""))
                done.append(rec.key)
            if done:
                self._queue_audit(
                    "alarms.ack", "info",
                    "报警确认：%s（只清显示，不解除任何互锁）" % "、".join(done),
                    {"keys": done, "note": (note or "")[:200],
                     "does_not_release": ["safety_guard.check", "motion.readiness_reason",
                                          "safety sweep 结论"]},
                    who_actor(actor))
        self._flush_audits()
        return {"ok": bool(done), "acked": done, "missing": missing,
                "note": "确认只清显示，不解除任何互锁；同一报警再次出现会重新成为活动报警"}

    def shelve(self, key: str, minutes: Optional[float] = None, actor: str = "operator",
               reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now) if now is not None else time.time()
        with self._lock:
            rec = self._recs.get(str(key))
            if rec is None:
                raise KeyError(str(key))
            if rec.safety:
                raise AlarmOpError(
                    "安全类报警禁止搁置：%s。danger / hit / 急停 / 围栏互锁 / 未清报警这一类"
                    "一律不许静音；要处理请先解决现场条件，再点确认（确认也只清显示，"
                    "不影响互锁本身）" % rec.title)
            try:
                mins = float(minutes) if minutes else SHELVE_DEFAULT_MIN
            except (TypeError, ValueError):
                mins = SHELVE_DEFAULT_MIN
            mins = max(0.1, min(mins, 24 * 60))
            rec.shelved = True
            rec.shelved_at = ts
            rec.shelve_until = ts + mins * 60.0
            rec.shelved_by = actor or "operator"
            rec.shelve_reason = (reason or "")[:200]
            rec.note = "已搁置 %.1f 分钟（到期自动回到活动态；互锁与就绪闸门不受影响）" % mins
            out = self.public(rec, ts)
            self._queue_audit(
                "alarms.shelve", "warn",
                "报警搁置：%s（%.1f 分钟，到期自动回到活动态）" % (rec.title, mins),
                {"key": rec.key, "minutes": mins, "reason": rec.shelve_reason,
                 "safety_class": False,
                 "does_not_release": ["safety_guard.check", "motion.readiness_reason"]},
                who_actor(actor))
            self._bump()
        self._flush_audits()
        return out

    def unshelve(self, key: str, actor: str = "operator",
                 now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now) if now is not None else time.time()
        with self._lock:
            rec = self._recs.get(str(key))
            if rec is None:
                raise KeyError(str(key))
            if not rec.shelved:
                raise AlarmOpError("这条报警本来就没有被搁置：%s" % rec.title)
            rec.shelved = False
            rec.shelve_until = None
            rec.shelved_at = None
            rec.shelved_by = ""
            rec.shelve_reason = ""
            rec.note = "搁置已由%s解除 → 回到活动态" % (actor or "operator")
            out = self.public(rec, ts)
            self._queue_audit("alarms.unshelve", "info",
                              "报警取消搁置：%s" % rec.title, {"key": rec.key},
                              who_actor(actor))
            self._bump()
        self._flush_audits()
        return out

    def set_priority(self, key: str, priority: str, actor: str = "operator",
                     reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
        """★ 优先级**唯一**的可写入口：必须是操作员显式动作 + 理由 + 审计。

        除这里以外，代码里没有任何地方能改 priority（重复出现只加 count）。
        """
        if priority not in PRIORITIES:
            raise AlarmOpError("优先级只能是 %s 之一，收到「%s」"
                               % ("、".join(PRIORITIES), priority))
        if not (reason or "").strip():
            raise AlarmOpError("改优先级必须写理由（审计要能看出是谁、为什么升/降级）")
        ts = float(now) if now is not None else time.time()
        with self._lock:
            rec = self._recs.get(str(key))
            if rec is None:
                raise KeyError(str(key))
            old = rec.priority
            rec.priority = priority
            rec.priority_source = "operator"
            rec.priority_set_by = actor or "operator"
            rec.priority_reason = (reason or "")[:200]
            rec.note = "优先级由%s显式改为 %s（原 %s）：%s" % (
                rec.priority_set_by, priority, old, reason[:120])
            out = self.public(rec, ts)
            self._queue_audit(
                "alarms.priority", "warn",
                "报警优先级变更：%s %s → %s" % (rec.title, old, priority),
                {"key": rec.key, "from": old, "to": priority, "reason": (reason or "")[:200]},
                who_actor(actor))
            self._bump()
        self._flush_audits()
        return out

    # ---------------- 读 ----------------
    def refresh(self, now: Optional[float] = None) -> int:
        """推进所有跟时间有关的状态（搁置到期 / 候选过期 / low 自动清除）。@return 水位。"""
        ts = float(now) if now is not None else time.time()
        with self._lock:
            self._expire_shelves(ts)
            self._expire_pending(ts)
            self._auto_resolve(ts)
            seq = self._seq
        self._flush_audits()
        return seq

    def active(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = float(now) if now is not None else time.time()
        self.refresh(ts)
        with self._lock:
            recs = list(self._recs.values())
        recs.sort(key=lambda r: (priority_rank(r.priority), -r.last_at))
        return [self.public(r, ts) for r in recs]

    def public(self, rec: Rec, ts: Optional[float] = None) -> Dict[str, Any]:
        now = float(ts) if ts is not None else time.time()
        state = _STATE_PENDING if rec.pending else (_STATE_SHELVED if rec.shelved else _STATE_ACTIVE)
        return {
            "key": rec.key,
            "rule_id": rec.rule_id,
            "title": rec.title,
            "priority": rec.priority,
            "priority_label": PRIORITY_LABELS.get(rec.priority, rec.priority),
            "priority_source": rec.priority_source,          # dictionary|operator|unregistered
            "priority_set_by": rec.priority_set_by or None,
            "priority_reason": rec.priority_reason or None,
            "safety_class": bool(rec.safety),
            "consequence": rec.consequence,
            "action_required": rec.action_required,
            "source": rec.source,
            "action": rec.action,
            "scope": rec.scope or None,
            "count": rec.count,
            "suppressed": max(0, rec.count - 1),              # 被折叠的条数（明细里都在）
            "first_at": _iso(rec.first_at),
            "last_at": _iso(rec.last_at),
            "duration_sec": round(now - rec.first_at, 1),
            "since_last_sec": round(now - rec.last_at, 1),
            "acked": bool(rec.acked),
            "acked_at": _iso(rec.acked_at) if rec.acked_at else None,
            "acked_by": rec.acked_by or None,
            "shelved": bool(rec.shelved),
            "shelve_until": _iso(rec.shelve_until) if rec.shelve_until else None,
            "shelve_left_sec": (round(rec.shelve_until - now, 1) if rec.shelve_until else None),
            "shelved_by": rec.shelved_by or None,
            "shelve_reason": rec.shelve_reason or None,
            "pending": bool(rec.pending),
            "delay_ms": int(rec.delay_ms),
            "state": state,
            "can_shelve": (not rec.safety),                   # ★ 安全类禁止搁置（后端判据）
            "note": rec.note or None,
            "last_event_id": rec.last_event_id,
            "last_message": rec.last_message or None,
        }

    def history(self, limit: int = 100, offset: int = 0,
                key: Optional[str] = None) -> Dict[str, Any]:
        """明细：含**被折叠 / 被抑制 / 候选过期 / 自动清除**的原始条目，一条不丢。

        ★ 内存环只保近期 HISTORY_MAX 条；更早的原始条目在审计表里 —— 每条明细都带
          `event_id`，可用 `GET /api/events` 逐条对上。这里如实说明，不假装内存环是全量历史。
        """
        with self._lock:
            rows = [dict(r) for r in self._hist]
        if key:
            rows = [r for r in rows if r.get("key") == key]
        total = len(rows)
        return {"items": rows[max(0, offset): max(0, offset) + max(1, limit)],
                "total": total, "limit": limit, "offset": offset,
                "ring_max": HISTORY_MAX,
                "durable_trail": "system_events（每条带 event_id，可用 /api/events 回溯）"}

    def summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        items = self.active(now)
        by = {p: 0 for p in (P_CRITICAL, P_HIGH, P_MEDIUM, P_LOW, P_UNCLASSIFIED)}
        for it in items:
            by[it["priority"]] = by.get(it["priority"], 0) + 1
        return {"items": items, "total": len(items), "by_priority": by,
                "pending": sum(1 for i in items if i["pending"]),
                "shelved": sum(1 for i in items if i["shelved"]),
                "safety_class": sum(1 for i in items if i["safety_class"]),
                "latest_seq": self.latest_seq(),
                "history_seq": self.history_seq()}

    # ---------------- 审计（唯一入口 events.emit；category 用已存在的合法值） ----------------
    def _queue_audit(self, action: str, level: str, message: str, detail: Any,
                     actor: str) -> None:
        """审计**先排队**，出锁之后再写 —— 绝不拿着报警锁去开 DB 会话。"""
        self._audit_q.append((action, level, message, detail, actor))

    def _flush_audits(self) -> None:
        with self._lock:
            q, self._audit_q = self._audit_q, []
        for action, level, message, detail, actor in q:
            try:
                from app.services.events import emit as emit_event
                emit_event("control", level, action, message, detail, actor=actor or "operator")
            except Exception:
                pass                    # 审计写不出去也不能让报警层把主流程带崩

    # ---------------- 测试 / 运维 ----------------
    def reset(self) -> None:
        with self._lock:
            self._recs.clear()
            self._hist.clear()
            self._seq = 0
            self._hseq = 0
            self._events_seen = 0
            self._alarms_raised = 0
            self._unregistered = 0
            self._audit_q = []

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"active": len(self._recs), "history": len(self._hist),
                    "events_seen": self._events_seen, "alarms_raised": self._alarms_raised,
                    "unregistered_alarms": self._unregistered,
                    "latest_seq": self._seq, "group_window_ms": GROUP_WINDOW_MS,
                    "shelve_default_min": SHELVE_DEFAULT_MIN,
                    "low_auto_resolve_ms": LOW_AUTO_RESOLVE_MS,
                    "dictionary_size": len(_RULES)}


def who_actor(actor: Optional[str]) -> str:
    """审计里的操作者：令牌角色（admin/operator），未知就如实写 unknown（不冒充 system）。"""
    a = str(actor or "").strip().lower()
    return a if a in ("admin", "operator") else "unknown"


alarm_center = AlarmCenter()


# ---------------------------------------------------------------- 模块级入口（API 只调这些）
def ingest(event: Dict[str, Any], now: Optional[float] = None) -> List[str]:
    return alarm_center.ingest(event, now)


def active(now: Optional[float] = None) -> List[Dict[str, Any]]:
    return alarm_center.active(now)


def summary(now: Optional[float] = None) -> Dict[str, Any]:
    return alarm_center.summary(now)


def history(limit: int = 100, offset: int = 0, key: Optional[str] = None) -> Dict[str, Any]:
    return alarm_center.history(limit, offset, key)


def latest_seq() -> int:
    return alarm_center.latest_seq()


def history_seq() -> int:
    return alarm_center.history_seq()


def refresh(now: Optional[float] = None) -> int:
    """推进所有跟时间有关的状态（搁置到期 / 候选过期 / low 自动清除）；@return 水位。"""
    return alarm_center.refresh(now)


def ack(keys: List[str], actor: str = "operator", note: str = "",
        now: Optional[float] = None) -> Dict[str, Any]:
    return alarm_center.ack(keys, actor, note, now)


def shelve(key: str, minutes: Optional[float] = None, actor: str = "operator",
           reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    return alarm_center.shelve(key, minutes, actor, reason, now)


def unshelve(key: str, actor: str = "operator", now: Optional[float] = None) -> Dict[str, Any]:
    return alarm_center.unshelve(key, actor, now)


def set_priority(key: str, priority: str, actor: str = "operator", reason: str = "",
                 now: Optional[float] = None) -> Dict[str, Any]:
    return alarm_center.set_priority(key, priority, actor, reason, now)


def stats() -> Dict[str, Any]:
    return alarm_center.stats()


def reset() -> None:
    alarm_center.reset()


# =============================================================== 报警字典（唯一登记口）
# 键 = 事件动作名（`services/events.py::emit` 的 action）；值 = 优先级 / 后果 / 要求动作。
# ★ 这些**都是显示与处置口径**，不改变任何源头判定：谁算 danger、谁拒下发，
#   照旧在 safety_guard / safety_eval / motion / modbus / rc_ready 里。
_R = register

# —— 急停 / 围栏互锁（★ 安全类：禁延时、禁搁置、禁自动清除）——
_R(Rule("control.estop", P_CRITICAL,
        "急停已执行：机器人立即停止，后续任何下发都不会生效，直到复位",
        "确认现场无人无物危险后按物理复位，再走「一键就绪」；先查是什么触发的急停",
        "控制链路（/control/estop）", "急停已按下", safety=True))
_R(Rule("control.estop_failed", P_CRITICAL,
        "急停指令没能写进控制器：界面显示已急停，但机器人可能仍在动 —— 最要命的一种不符",
        "立刻按物理急停按钮，停止一切远程操作，检查 Modbus 链路与常驻程序 210",
        "控制链路（/control/estop）", "急停下发失败", safety=True))
_R(Rule("ai.estop", P_CRITICAL,
        "自然语言指令触发了急停：机器人已停，自动流程被打断",
        "核对这是不是你预期的指令；复位后重新下发，必要时收掉 AI 的控制权",
        "AI 执行", "AI 急停", safety=True))

for _st, _pr in (("hit", P_CRITICAL), ("danger", P_HIGH), ("warn", P_MEDIUM)):
    _R(Rule("safety.%s" % _st, _pr,
            "前端上报围栏状态 %s：真实下发会被围栏互锁拒绝（余量不足或已经接触）" % _st,
            "停下手上的下发，确认区域内障碍 / 人员，把机器人移开等状态回到 safe",
            "围栏（前端上报）", "围栏 %s" % _st, safety=True,
            scope_fields=("zone_id", "zone_name"), clear_actions=("safety.clear",)))
    _R(Rule("safety.server_%s" % _st, _pr,
            "后端独立评估围栏状态 %s：按后端口径此刻会被拦（影子模式只提示不拦）" % _st,
            "对照画面核实是真靠近还是包络过于保守；需要时由管理员调整 server_guard 模式",
            "围栏（后端独立评估）", "后端评估 %s" % _st, safety=True,
            scope_fields=("zone_id", "zone_name"),
            clear_actions=("safety.server_safe", "safety.server_clear", "safety.clear")))

_R(Rule("control.interlock_absent", P_HIGH,
        "围栏互锁已启用但拿不到实时状态：这次下发是在没有互锁依据的情况下被拒的",
        "确认前端页面在跑且采集在线（/api/safety 新鲜度为 fresh），再决定是否继续下发",
        "围栏互锁", "互锁状态缺失", safety=True))
_R(Rule("control.move_blocked", P_HIGH,
        "一次真实下发被拒绝（互锁 / 未就绪 / 越界）：指令没有落到控制器",
        "按消息里的原因处置（清报警 / 一键就绪 / 移开障碍），不要反复重试同一条",
        "控制链路（下发前置校验）", "下发被拦截", safety=True))

# —— 链路 / 采集（抖动型走 on-delay；有明确恢复事件）——
_R(Rule("connection.degraded", P_HIGH,
        "连续读失败已降级为模拟：画面里的姿态不再是真机位置，任何「看着没碰到」都不作数",
        "检查控制器电源与网线；确认现场没人靠近机器人，恢复真实链路后再继续",
        "采集器（Modbus 链路）", "链路降级为模拟", delay_ms=2_000,
        clear_actions=("connection.restored",)))
_R(Rule("collector.crashed", P_CRITICAL,
        "采集线程异常重启过：这段时间位姿是冻结的旧值，画面「看着正常但不动」",
        "先看运维页是否已恢复真实读数；没恢复就不要再远程下发，先查故障原因",
        "采集器（线程守护）", "采集线程异常"))
_R(Rule("safety.save_failed", P_HIGH,
        "安全围栏配置没能落盘：界面以为改过了，实际生效的还是旧配置",
        "重新保存并核对当前围栏配置；改不出来就先靠物理保护，别用远程下发",
        "围栏配置", "围栏配置保存失败"))

# —— 就绪 / 程序执行 ——
_R(Rule("control.ready", P_HIGH,
        "一键就绪没有走通：机器人仍在未就绪态，真实下发会被统一闸门拒掉",
        "按步骤条上失败的那一步处置（停机 / 清报警 / 伺服 / 加载 / 运行），别绕开就绪硬发",
        "就绪流程", "就绪失败"))
_R(Rule("control.run_file_missing", P_MEDIUM,
        "要执行的程序号在控制器上不存在：这一步根本没跑",
        "核对程序号与白名单（motion.ready.allowed_programs），必要时重新下发程序",
        "程序执行", "程序号不存在"))
_R(Rule("control.run_reaped", P_MEDIUM,
        "执行线程被回收（长时间没有心跳）：这一步之后的流程状态不可信",
        "到示教器 / 画面上确认机器人实际停在哪一步，再决定要不要重跑",
        "程序执行", "执行被回收"))
_R(Rule("control.run_cancelled", P_LOW,
        "执行被取消：剩余步骤没有跑",
        "确认是你自己要停的；不是就查是谁点了取消",
        "程序执行", "执行已取消", auto_resolve_ms=LOW_AUTO_RESOLVE_MS))
_R(Rule("control.run_file", P_MEDIUM,
        "程序执行有步骤失败：整体没有按预期走完",
        "看逐步结果里失败那一步的原因（回读不一致 / 完成位超时 / 被拦截），修好再重跑",
        "程序执行", "程序执行有失败步骤"))
_R(Rule("control.dry_run", P_MEDIUM,
        "空跑预演有步骤失败：真实下发会更早、更严重地卡在同一处",
        "把空跑里失败的那一步修顺了再切真实下发",
        "程序执行（空跑）", "空跑有失败步骤"))

# —— 数字孪生 / 观测（提示级，不参与控制）——
_R(Rule("twin_residual_over", P_LOW,
        "残影超差：下发目标与实际观测没收敛到带内（只观测，不会自动暂停 / 重试 / 急停）",
        "先分清是通讯抖动还是机械问题；持续超差就降速、复查示教点与负载",
        "孪生残影观测", "到位自证超差", auto_resolve_ms=LOW_AUTO_RESOLVE_MS))
_R(Rule("hold_state_contradiction", P_MEDIUM,
        "持件证据与流程矛盾：界面按流程认为手上有件，但视觉观测不支持（现场没有真空传感器）",
        "别继续往深走：先看机器人手上到底有没有件，必要时手动放件并复位流程",
        "持件证据（固定相机反用）", "持件证据矛盾"))
_R(Rule("safety.evidence_failed", P_MEDIUM,
        "取证包没能落盘：这次 danger/hit 的现场数据缺一份，事后追溯会缺证据",
        "查磁盘 / 相机是否可用；重要试验前先确认取证能写出来",
        "安全取证", "取证包写入失败"))

# —— 视觉 / 分拣 / AI（判不了就放回，但操作员必须知道）——
_R(Rule("sorting.no_judge", P_MEDIUM,
        "判色失败（判不出颜色或不在有效色内）：这一件按原路放回，没有分拣",
        "看颜色是否本就不在指令里；要判得更准就按现场实物调阈值（先跑 e2e 回放）",
        "三色视觉", "判色失败"))
_R(Rule("ai.piece_invalid", P_MEDIUM,
        "AI 判定这一件无效（颜色 / 位置不符）：按原路放回",
        "核对指令里的有效色与实物摆放；不确定就切回人工确认",
        "AI 执行", "AI 判定无效"))
_R(Rule("ai.relay_fail", P_MEDIUM,
        "AI 接力失败（把结果转交分拣流程没成）：这一趟没有按计划继续",
        "看运行日志里的失败原因（未就绪 / 点位缺失 / 互锁），修好后重发指令",
        "AI 执行", "AI 接力失败"))
_R(Rule("ai.relay_skip", P_LOW,
        "AI 接力被跳过：条件不满足（例如没有有效色或缺点位）",
        "确认指令本身是否完整；需要就补点位 / 颜色再发",
        "AI 执行", "AI 接力跳过", auto_resolve_ms=LOW_AUTO_RESOLVE_MS))
_R(Rule("ai.takeover", P_MEDIUM,
        "AI 已交还控制权（人工接管）：自动流程停了",
        "确认这是你预期的接管；要继续就重新把控制权交给 AI",
        "AI 执行", "AI 控制权交接"))

# —— 配置类改动（动了现场保护或标定数据就是报警，不是流水）——
_R(Rule("safety.reset", P_HIGH,
        "围栏配置被恢复成出厂默认：现场标定过的保护区没了",
        "立刻重新导入 / 标定现场围栏；在此之前不要把远程下发当作有保护区在用",
        "围栏配置", "围栏配置已复位"))
_R(Rule("safety.rollback", P_HIGH,
        "围栏配置被回滚到历史版本：当前保护区可能与操作员以为的不一样",
        "核对回滚到的版本内容与时间，必要时再回滚回来",
        "围栏配置", "围栏配置回滚"))
_R(Rule("point.delete", P_MEDIUM,
        "预设点位被删除：引用它的程序 / 分拣路径会突然找不到点",
        "确认是有意删除；若程序还在引用，请重录该点或改程序",
        "点位管理", "点位已删除"))
_R(Rule("program.delete", P_MEDIUM,
        "执行程序被删除：引用它的下发会失败",
        "确认是有意删除，必要时从备份导入",
        "程序管理", "程序已删除"))
_R(Rule("config.frames_reset", P_MEDIUM,
        "坐标系标定被重置：所有依赖它的位置换算都不再可信",
        "重新做坐标系标定；在此之前不要用远程定位",
        "坐标系标定", "坐标系已重置"))

# —— 鉴权 / 语音（提示级）——
_R(Rule("auth.login_throttled", P_MEDIUM,
        "短时间内登录失败次数过多（可能有人在试控制口令）",
        "确认是不是自己人手滑；不是就改管理员口令并检查网络暴露面",
        "鉴权", "登录被限流"))
_R(Rule("auth.login_failed", P_LOW,
        "一次登录失败：口令不对",
        "少量属正常手滑；成片出现就按登录限流那条处置",
        "鉴权", "登录失败", delay_ms=1_000, auto_resolve_ms=LOW_AUTO_RESOLVE_MS))
_R(Rule("asr.cloud_error", P_LOW,
        "语音识别云服务报错：字幕功能不可用（不影响机器人控制）",
        "看网络与密钥配置；要用语音就重开会话",
        "语音（ASR）", "语音云服务错误", auto_resolve_ms=LOW_AUTO_RESOLVE_MS))
_R(Rule("asr.session_error", P_LOW,
        "语音识别会话连接失败：这一路字幕断了",
        "重开会话或改用文字输入",
        "语音（ASR）", "语音会话失败", auto_resolve_ms=LOW_AUTO_RESOLVE_MS))
