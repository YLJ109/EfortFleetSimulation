# -*- coding: utf-8 -*-
"""★ E6「机器人自述 = 每天一页现场日报」（**只读汇总，零指令权，零 LLM**）。

立意（方案 §E6，本轮先出纯模板版）：这台机器每天把自己干了什么写成**一页中文日报**，
每一个数字都必须能从结构化数据源里追溯到同一值 ——
「不做能连上的远程，做每一步都能自证的远程」。

★★ 本功能的命门（改动前先读）：
  · **绝不允许界面或后端编造一个数字**：每个数字旁必须带 `source`（哪个 service / 表 / 端点）；
    取不到就写「无此项数据」，**不许塌成 0**（§5.5 铁律③，与 `log.length` 同类事故）；
  · **零 LLM**：本轮不接 `services/llm.py`（将来接也只当讲解员，零否决权零指令权）；
    数字一致性自检 `consistency_check()` 就是为「将来接 LLM 时文本不许造数」预留的钉子；
  · **零下发**：不 import motion / modbus / jog / rc_ready 的任何写路径，不 emit 事件
    （生成日报本身不是控制动作）；
  · **内存态如实标注**：到位自证（E2）/ 报警台账（C2）/ 一格三证（E7）是**进程内**数据，
    日报里必须写明「本机本次会话」，不许当持久历史；
  · **日期口径**：按**本地日期**切「今天」，窗口换算成 UTC 查事件表，时区标注在报告头上。

数据源（全部已存在，只读它们的读侧 API）：
  · db:system_events（services/events.py::emit 落库）—— 谁 / 何时 / 跑了什么 / 被拦次数
  · services/twin_residual.py —— 今天最险的段（peak_deg）与非 arrived 判据
  · services/safety_evidence.py —— 取证包数量与最后一次触发
  · services/alarms.py —— 分级数量 / 折叠去重 / 刷屏项 / 未登记报警
  · services/point_audit.py —— 可疑点位（bad/warn/unknown）
  · services/cell_evidence.py —— 一格三证齐全度与缺证分布
  · 判色样本量（E4）：**本轮不接 vision_samples / color_advice**（接口尚未定），
    留可注入参数 `color_samples`，默认 None → 如实写「判色样本环尚未接入」。
"""
from __future__ import annotations

import re
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.logger import get_logger

log = get_logger("daily_report")

SCHEMA = 1
TITLE = "现场日报（机器人自述）"
#: 「没数据」的统一文案 —— 不许在任何地方塌成 0（test_empty_day_says_no_data_not_zero）
NO_DATA_TEXT = "无此项数据"
#: 「今日最该关注的事」最多列几条（模板里出现的数必须能在结构化里找到）
ATTENTION_LIMIT = 3
#: 清单类字段最多列几行（同样进结构化，保证一致性自检放行）
LIST_LIMIT = 10
#: 事件表一次读取的封顶（超过就如实标注"基于最近 N 条"）
EVENT_FETCH_CAP = 2000
#: 报警 count 达到它就算"刷屏项"（与 GROUP_WINDOW_MS 同量级的现场口径）
SPAM_MIN_COUNT = 10

#: 会话级（进程内）数据源的统一声明 —— 出现在这些节的 source/note 里，不许含糊
SESSION_NOTE = "本机本次会话（进程内内存态，重启即归零，不是持久历史）"

# 事件动作名 → 日报里的中文流程名（"跑了什么"；没列出的动作只进总数，不编名字）
FLOW_LABELS: Dict[str, str] = {
    "sorting.start": "分拣启动",
    "sorting.piece": "分拣完成件",
    "sorting.no_judge": "判色失败原路放回",
    "sorting.goto": "到点（goto）",
    "tictactoe.start": "井字棋开局",
    "tictactoe.restore_start": "井字棋复原",
    "control.ready": "一键就绪完成",
    "control.vacuum": "气路动作",
    "control.estop": "急停",
    "control.move_blocked": "下发被拦截",
    "twin_residual_over": "到位自证超差",
    "safety.evidence_written": "取证包落盘",
    "system.start": "后端启动",
    "system.stop": "后端停止",
}
_ALARM_LEVELS = ("warn", "error", "critical")
#: 到位自证里"没成"的判据（arrived/slow 之外；pending/moving 在飞的段不算进来也没关系 ——
#:   它们是会话尾部最多一条的在飞态，note 里如实说明）
_BAD_VERDICTS = ("stuck", "not_reached", "overshoot", "aborted", "no_data")


# ---------------------------------------------------------------- 数值与格式
def _fmt_num(v: Any) -> str:
    """统一数字渲染（结构化里存的就是它，一致性自检按同一个口径对）。"""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return str(v)
    try:
        f = round(float(v), 3)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return str(int(f))
    return ("%g" % f)


def _num_tokens(s: str) -> List[str]:
    return re.findall(r"\d+(?:\.\d+)?", str(s or ""))


def _walk_numbers(obj: Any, allowed: set) -> None:
    """递归收集结构化源里**所有可追溯的数值 token**（含字符串里出现的数字）。

    ★ 跳过纯展示字段（markdown / lines / basis）—— 它们就是"被渲染出来的文本"，
      若允许它们当依据，自检就恒真了；markdown 里的数字必须能落到 fields 级别的源。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("markdown", "lines", "basis"):
                continue                      # ★ 被检对象本身不算依据（否则自检恒真）
            _walk_numbers(v, allowed)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_numbers(v, allowed)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        t = _fmt_num(obj)
        allowed.add(t)
        allowed.add(t.lstrip("-"))             # markdown 抽 token 不带负号，这里两个口径都放行
    elif obj is not None:
        for t in _num_tokens(obj):
            allowed.add(t)


def consistency_check(structured: Dict[str, Any], markdown: str) -> List[str]:
    """★ 数字一致性自检（本功能的钉子，将来接 LLM 时同一口径复用）。

    扫 markdown 里出现的每个数值 token，凡是在结构化源（不含 markdown 字段本身）里
    **找不到同值**的 → 作为不一致项返回。不为空就必须显示出来，不许静默通过。
    """
    allowed: set = set()
    _walk_numbers(structured, allowed)
    out: List[str] = []
    seen: set = set()
    for i, line in enumerate(str(markdown or "").splitlines(), start=1):
        for tok in _num_tokens(line):
            if tok in allowed:
                continue
            key = "%s@%d" % (tok, i)
            if key in seen:
                continue
            seen.add(key)
            out.append("第 %d 行出现数值 %s，结构化源里找不到同值 —— 疑似编造，不许发布" % (i, tok))
    return out


# ---------------------------------------------------------------- 日期窗口（本地日 → UTC）
def _local_tz():
    return datetime.now().astimezone().tzinfo


def _resolve_date(date_str: Optional[str]) -> _date:
    if not date_str:
        return datetime.now().astimezone().date()
    try:
        return datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("date 需为 YYYY-MM-DD（本地日期），收到 %r" % date_str)


def _day_window_utc(d: _date) -> Tuple[datetime, datetime, str]:
    tz = _local_tz()
    start_local = datetime.combine(d, _time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    off = start_local.strftime("%z")           # 例 +0800
    label = "本地日期 %s（UTC%s）；事件表查询窗口为换算后的 UTC [%s, %s)" % (
        d.isoformat(), off[:3] + ":" + off[3:],
        start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        end_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    naive = lambda x: x.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return naive(start_local), naive(end_local), label


def _as_naive_utc(dt: Any) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso_utc(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.isoformat() + "Z"


# ---------------------------------------------------------------- 各节构建（全部 try/except）
def _section(key: str, title: str, source: str) -> Dict[str, Any]:
    return {"key": key, "title": title, "source": source,
            "status": "ok", "note": "", "fields": {}, "lines": []}


def _unavailable(sec: Dict[str, Any], err: Exception) -> Dict[str, Any]:
    sec["status"] = "unavailable"
    sec["note"] = "这一项数据源读不出来（%s）—— 按「%s」处理，不猜、不补 0" % (
        str(err)[:120], NO_DATA_TEXT)
    sec["lines"] = ["%s：%s" % (sec["title"], NO_DATA_TEXT)]
    return sec


def _build_flows(d: _date, w_from: datetime, w_to: datetime) -> Dict[str, Any]:
    sec = _section("flows", "事件与流程（谁 / 何时 / 跑了什么）",
                   "db:system_events（services/events.py::emit 落库，"
                   "经 app/db/crud.py::list_system_events 只读）")
    try:
        from app.db.crud import list_system_events
        from app.db.database import SessionLocal
        s = SessionLocal()
        try:
            rows = list_system_events(s, since=w_from, limit=EVENT_FETCH_CAP)
        finally:
            s.close()
        rows = [r for r in rows
                if (_as_naive_utc(r.timestamp) is not None
                    and _as_naive_utc(r.timestamp) < w_to)]
    except Exception as e:                    # noqa: BLE001
        return _unavailable(sec, e)

    f = sec["fields"]
    f["fetch_cap"] = EVENT_FETCH_CAP
    f["count"] = len(rows)
    if not rows:
        sec["status"] = "no_data"
        f["first_at"] = None
        f["last_at"] = None
        f["actors"] = []
        f["actions"] = {}
        f["move_blocked"] = 0
        f["no_judge"] = 0
        sec["lines"] = ["%s：今天没有任何事件记录" % NO_DATA_TEXT]
        return sec

    ts = sorted(_as_naive_utc(r.timestamp) for r in rows)
    actors: Dict[str, int] = {}
    by_level: Dict[str, int] = {}
    actions: Dict[str, Dict[str, int]] = {}
    move_blocked = 0
    for r in rows:
        actors[str(r.actor or "?")] = actors.get(str(r.actor or "?"), 0) + 1
        lv = str(r.level or "info")
        by_level[lv] = by_level.get(lv, 0) + 1
        a = str(r.action or "")
        if a in FLOW_LABELS:
            cur = actions.setdefault(a, {"label": FLOW_LABELS[a], "normal": 0, "alarm": 0})
            cur["alarm" if lv in _ALARM_LEVELS else "normal"] += 1
        if a == "control.move_blocked":
            move_blocked += 1
    f["first_at"] = _iso_utc(ts[0])
    f["last_at"] = _iso_utc(ts[-1])
    f["actors"] = [{"actor": k, "count": v}
                   for k, v in sorted(actors.items(), key=lambda x: -x[1])]
    f["by_level"] = by_level
    f["actions"] = actions
    f["move_blocked"] = move_blocked
    nj = actions.get("sorting.no_judge") or {}
    f["no_judge"] = int(nj.get("normal", 0)) + int(nj.get("alarm", 0))

    L = ["共 %s 条事件（%s ～ %s）" % (_fmt_num(f["count"]), f["first_at"], f["last_at"])]
    if len(rows) >= EVENT_FETCH_CAP:
        L.append("已达单次读取上限 %s 条，统计基于最近 %s 条（如实标注）"
                 % (_fmt_num(EVENT_FETCH_CAP), _fmt_num(EVENT_FETCH_CAP)))
    L.append("按级别：" + " / ".join("%s %s" % (k, _fmt_num(v))
                                    for k, v in sorted(by_level.items())))
    L.append("参与者：" + " / ".join("%s×%s" % (x["actor"], _fmt_num(x["count"]))
                                    for x in f["actors"]))
    for a, cur in actions.items():
        L.append("%s（%s）：正常 %s · 警告及以上 %s"
                 % (cur["label"], a, _fmt_num(cur["normal"]), _fmt_num(cur["alarm"])))
    L.append("下发被拦截（control.move_blocked）：%s 次" % _fmt_num(move_blocked))
    sec["lines"] = L
    return sec


def _build_twin() -> Dict[str, Any]:
    sec = _section("twin", "到位自证（E2 段级残差）",
                   "services/twin_residual.py::twin_residual.recent(40) —— %s" % SESSION_NOTE)
    try:
        from app.services.twin_residual import twin_residual
        rows = twin_residual.recent(40)
    except Exception as e:                    # noqa: BLE001
        return _unavailable(sec, e)

    f = sec["fields"]
    f["segments"] = len(rows)
    if not rows:
        sec["status"] = "no_data"
        f["verdicts"] = {}
        f["worst_peak_deg"] = None
        f["worst"] = []
        f["non_arrived"] = 0
        sec["lines"] = ["%s：本机本次会话没有到位自证记录（≠ 今天没跑过 —— "
                        "重启前的段不在这里）" % NO_DATA_TEXT]
        return sec

    verdicts: Dict[str, int] = {}
    for r in rows:
        v = str(r.get("verdict") or "?")
        verdicts[v] = verdicts.get(v, 0) + 1
    worst = sorted(rows, key=lambda r: -(float(r.get("peak_deg") or 0.0)))[:3]
    non_arrived = sum(1 for r in rows if str(r.get("verdict") or "") in _BAD_VERDICTS)
    f["verdicts"] = verdicts
    f["worst"] = [{"rank": i, "id": r.get("id"),
                   "source": str(r.get("source") or "未标注来源"),
                   "peak_deg": r.get("peak_deg"), "verdict": r.get("verdict")}
                  for i, r in enumerate(worst, start=1)]
    f["worst_peak_deg"] = worst[0].get("peak_deg")
    f["non_arrived"] = non_arrived
    sec["note"] = ("残差是关节角差（°）不是 mm；本节只覆盖%s —— 在飞的段最多一条。"
                   % SESSION_NOTE)
    L = ["本次会话共观测 %s 段；判据分布：%s"
         % (_fmt_num(f["segments"]),
            " / ".join("%s×%s" % (k, _fmt_num(v)) for k, v in sorted(verdicts.items())))]
    L.append("未到位/超差段数（%s）：%s"
             % ("、".join(_BAD_VERDICTS), _fmt_num(non_arrived)))
    for r in f["worst"]:
        L.append("第 %s 险：段 %s「%s」peak %s°，判据 %s"
                 % (_fmt_num(r["rank"]), _fmt_num(r["id"]), r["source"],
                    _fmt_num(r["peak_deg"]), r["verdict"]))
    sec["lines"] = L
    return sec


def _pack_local_date(pack: Dict[str, Any]) -> Optional[str]:
    s = str(pack.get("captured_at_utc") or "")
    # ★ manifest 里的字面量形如 "...+00:00Z"（isoformat 自带偏移 + 项目习惯再补 Z），
    #   先把尾部的 Z 摘掉再按带偏移的 ISO 解析，否则 fromisoformat 会抛。
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s).astimezone()
        return dt.date().isoformat()
    except Exception:
        return None


def _build_evidence(d: _date) -> Dict[str, Any]:
    sec = _section("evidence", "安全取证包（A5）",
                   "services/safety_evidence.py::list_packs（logs/evidence/*/manifest.json 摘要）")
    try:
        from app.services import safety_evidence
        data = safety_evidence.list_packs(limit=500)
        packs = data.get("packs") or []
    except Exception as e:                    # noqa: BLE001
        return _unavailable(sec, e)

    f = sec["fields"]
    today = [p for p in packs if _pack_local_date(p) == d.isoformat()]
    f["total_packs"] = data.get("total", len(packs))
    f["today_packs"] = len(today)
    if not today:
        sec["status"] = "no_data"
        f["latest"] = None
        sec["lines"] = ["%s：今天没有触发过 danger/hit，没有落取证包（不会凭空造包）；"
                        "目录里累计 %s 个包" % (NO_DATA_TEXT, _fmt_num(f["total_packs"]))]
        return sec

    latest = max(today, key=lambda p: int(p.get("seq") or 0))
    f["latest"] = {"seq": latest.get("seq"), "id": latest.get("id"),
                   "captured_at_utc": latest.get("captured_at_utc"),
                   "to_state": latest.get("to_state"),
                   "zone_name": str(latest.get("zone_name") or "未命名区域"),
                   "clearance_mm": latest.get("clearance_mm")}
    L = ["今天落了 %s 个取证包（目录累计 %s 个）"
         % (_fmt_num(f["today_packs"]), _fmt_num(f["total_packs"]))]
    lv = f["latest"]
    L.append("最后一次：#%s %s · %s（%s）· 间隙 %smm"
             % (_fmt_num(lv["seq"]), lv["captured_at_utc"], lv["zone_name"],
                lv["to_state"], _fmt_num(lv["clearance_mm"])))
    sec["lines"] = L
    return sec


def _build_alarms() -> Dict[str, Any]:
    sec = _section("alarms", "报警合理化（C2）",
                   "services/alarms.py::summary()/stats()/history() —— %s" % SESSION_NOTE)
    try:
        from app.services import alarms
        summ = alarms.summary()
        st = alarms.stats()
        hist = alarms.history(limit=1000)
    except Exception as e:                    # noqa: BLE001
        return _unavailable(sec, e)

    f = sec["fields"]
    items = summ.get("items") or []
    f["events_seen"] = st.get("events_seen", 0)
    f["spam_min_count"] = SPAM_MIN_COUNT
    f["active_total"] = summ.get("total", len(items))
    f["by_priority"] = summ.get("by_priority") or {}
    f["pending"] = summ.get("pending", 0)
    f["shelved"] = summ.get("shelved", 0)
    f["suppressed_active"] = sum(int(x.get("suppressed") or 0) for x in items)
    f["folded_rows"] = sum(1 for r in (hist.get("items") or [])
                           if str(r.get("state")) == "folded")
    f["unregistered"] = sum(1 for x in items if x.get("priority_source") == "unregistered")
    f["spam"] = [{"key": x.get("key"), "title": x.get("title"), "count": x.get("count")}
                 for x in items if int(x.get("count") or 0) >= SPAM_MIN_COUNT]
    if f["events_seen"] == 0 and f["active_total"] == 0:
        sec["status"] = "no_data"
        sec["lines"] = ["%s：本次会话报警层还没摄入过任何事件（≠ 没有报警 —— "
                        "重启前的台账不在这里，原始流水在事件表）" % NO_DATA_TEXT]
        return sec

    sec["note"] = ("报警台账是%s，**未按日切分**（内存台账没有日期索引）；"
                   "折叠掉的逐条明细在 history（state=folded）里一条不丢。" % SESSION_NOTE)
    L = ["活动报警 %s 条：分级 %s；候选中 %s · 搁置中 %s · 未分级 %s"
         % (_fmt_num(f["active_total"]),
            " / ".join("%s×%s" % (k, _fmt_num(v)) for k, v in
                       sorted((f["by_priority"] or {}).items())),
            _fmt_num(f["pending"]), _fmt_num(f["shelved"]), _fmt_num(f["unregistered"]))]
    L.append("关联抑制折叠掉（活动条内合计）：%s 次；明细环里标 folded：%s 条"
             % (_fmt_num(f["suppressed_active"]), _fmt_num(f["folded_rows"])))
    if f["spam"]:
        L.append("刷屏项（count ≥ %s）：%s"
                 % (_fmt_num(SPAM_MIN_COUNT),
                    " / ".join("%s×%s" % (x["title"], _fmt_num(x["count"])) for x in f["spam"])))
    else:
        L.append("刷屏项（count ≥ %s）：无" % _fmt_num(SPAM_MIN_COUNT))
    sec["lines"] = L
    return sec


def _build_points() -> Dict[str, Any]:
    sec = _section("points", "点位数值体检（D2）",
                   "services/point_audit.py::audit_rows（db:points 当前点位库，只读体检）")
    try:
        from app.db.crud import list_points
        from app.db.database import SessionLocal
        from app.services import point_audit
        s = SessionLocal()
        try:
            rows = list_points(s)
        finally:
            s.close()
        audit = point_audit.audit_rows(rows)
    except Exception as e:                    # noqa: BLE001
        return _unavailable(sec, e)

    f = sec["fields"]
    bg = audit.get("by_grade") or {}
    f["count"] = audit.get("count", 0)
    f["by_grade"] = bg
    f["encode_limit_deg"] = audit.get("encode_limit_deg")
    suspicious = [p for p in (audit.get("points") or [])
                  if p.get("grade") in ("bad", "warn", "unknown")]
    f["suspicious_count"] = len(suspicious)
    f["suspicious"] = [{"name": p.get("name"), "grade": p.get("grade"),
                        "worst_margin_deg": p.get("worst_margin_deg"),
                        "advice": str(p.get("advice") or "")[:80]}
                       for p in suspicious[:LIST_LIMIT]]
    sec["note"] = "针对**当前点位库**的静态体检（不是当日增量）；只出建议，不回写、不下发。"
    if f["count"] == 0:
        sec["status"] = "no_data"
        sec["lines"] = ["%s：点位库里没有任何点位" % NO_DATA_TEXT]
        return sec

    L = ["体检 %s 个点位：ok %s / warn %s / bad %s / unknown %s；可疑（warn+bad+unknown）%s 个；"
         "可表示上限 ±%s°"
         % (_fmt_num(f["count"]), _fmt_num(bg.get("ok", 0)), _fmt_num(bg.get("warn", 0)),
            _fmt_num(bg.get("bad", 0)), _fmt_num(bg.get("unknown", 0)),
            _fmt_num(f["suspicious_count"]), _fmt_num(f["encode_limit_deg"]))]
    for p in f["suspicious"]:
        L.append("· %s [%s] 最紧余量 %s° —— %s"
                 % (p["name"], p["grade"], _fmt_num(p["worst_margin_deg"]), p["advice"]))
    if not suspicious:
        L.append("无可疑点位（全部通过静态体检；「能不能到/会不会撞」不在本工具口径内）")
    sec["lines"] = L
    return sec


def _build_cells() -> Dict[str, Any]:
    sec = _section("cells", "一格三证齐全度（E7）",
                   "services/cell_evidence.py::stats() —— %s" % SESSION_NOTE)
    try:
        from app.services import cell_evidence
        st = cell_evidence.stats()
    except Exception as e:                    # noqa: BLE001
        return _unavailable(sec, e)

    f = sec["fields"]
    f["total"] = st.get("total", 0)
    cells = st.get("cells") or []
    f["cells"] = [{"cell_id": c.get("cell_id"), "n": c.get("n"),
                   "complete": c.get("complete"),
                   "missing": {k: (c.get("missing") or {}).get(k, 0)
                               for k in ("photo", "colour", "pose")}}
                  for c in cells[:LIST_LIMIT]]
    f["complete"] = sum(int(c.get("complete") or 0) for c in cells)
    f["missing_records"] = f["total"] - f["complete"]
    if f["total"] == 0:
        sec["status"] = "no_data"
        sec["lines"] = ["%s：本次会话还没有装订过任何三证记录（没有记录 ≠ 没有发生，"
                        "历史看 logs/cells/cells-YYYYMMDD.jsonl）" % NO_DATA_TEXT]
        return sec

    L = ["三证记录 %s 条，齐全 %s 条，有缺证 %s 条"
         % (_fmt_num(f["total"]), _fmt_num(f["complete"]), _fmt_num(f["missing_records"]))]
    for c in f["cells"]:
        L.append("· %s：%s/%s 齐全（缺 照片%s 判色%s 位姿%s）"
                 % (c["cell_id"], _fmt_num(c["complete"]), _fmt_num(c["n"]),
                    _fmt_num(c["missing"]["photo"]), _fmt_num(c["missing"]["colour"]),
                    _fmt_num(c["missing"]["pose"])))
    sec["lines"] = L
    return sec


def _build_color(color_samples: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sec = _section("color", "判色样本量（E4）",
                   "可注入参数 build_report(color_samples=...)（E4 判色样本模块接口未定，"
                   "本轮**不 import 它的 services**，默认 None）")
    if color_samples is None:
        sec["status"] = "no_data"
        sec["lines"] = ["%s：判色样本环尚未接入 —— 不编一个样本量" % NO_DATA_TEXT]
        return sec
    f = sec["fields"]
    f.update(color_samples if isinstance(color_samples, dict)
             else {"raw": str(color_samples)[:120]})
    L = []
    for k, v in f.items():
        L.append("%s：%s" % (k, _fmt_num(v) if isinstance(v, (int, float))
                             and not isinstance(v, bool) else str(v)[:120]))
    sec["lines"] = L or ["%s" % NO_DATA_TEXT]
    return sec


# ---------------------------------------------------------------- 今日最该关注的事（纯规则排序）
def _attention(sections: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """由结构化数字算出来的排序 —— 每条都给"依据数字"，不写玄学文案。"""
    def _g(key: str, *path: str) -> Optional[int]:
        sec = sections.get(key)
        if not sec or sec.get("status") == "unavailable":
            return None
        cur: Any = sec.get("fields") or {}
        for p in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur if isinstance(cur, int) and not isinstance(cur, bool) else None

    cands: List[Tuple[str, str, Optional[int], str, str]] = [
        ("flows", "下发被拦截次数（围栏/档位/闸门都算，见事件表 control.move_blocked）",
         _g("flows", "move_blocked"), "次", "db:system_events"),
        ("twin", "到位自证未到位/超差段数", _g("twin", "non_arrived"), "段",
         "services/twin_residual.py（%s）" % SESSION_NOTE),
        ("flows", "判色失败原路放回次数", _g("flows", "no_judge"),
         "次", "db:system_events"),
        ("alarms", "未分级（未在字典登记）的活动报警数", _g("alarms", "unregistered"),
         "条", "services/alarms.py（%s）" % SESSION_NOTE),
        ("points", "可疑点位数（warn/bad/unknown，最该重录的在前）", _g("points", "suspicious_count"),
         "个", "services/point_audit.py + db:points"),
        ("evidence", "今日安全取证包数（每次都是 danger/hit 上升沿）", _g("evidence", "today_packs"),
         "个", "services/safety_evidence.py"),
        ("cells", "一格三证有缺证的记录数", _g("cells", "missing_records"), "条",
         "services/cell_evidence.py（%s）" % SESSION_NOTE),
    ]
    pos = [(i, k, label, v, unit, src)
           for i, (k, label, v, unit, src) in enumerate(cands) if v is not None and v > 0]
    pos.sort(key=lambda x: (-x[3], x[0]))
    items = []
    for rank, (_i, k, label, v, unit, src) in enumerate(pos[:ATTENTION_LIMIT], start=1):
        items.append({"rank": rank, "label": label, "value": v, "unit": unit,
                      "section": k, "source": src,
                      "basis": "依据数字：%s = %s（出自「%s」一节）"
                               % (k, _fmt_num(v), (sections.get(k) or {}).get("title", k))})
    out = {"display_limit": ATTENTION_LIMIT, "candidates": len(cands), "items": items}
    if not items:
        out["note"] = ("候选 %s 项今日**没有一条有非零数字依据** —— 不凑数，"
                       "这不代表一切正常，只是本日报看不到异常" % _fmt_num(len(cands)))
    elif len(items) < ATTENTION_LIMIT:
        out["note"] = ("今日只有 %s 项有数字依据（不足 %s 条，不凑数）"
                       % (_fmt_num(len(items)), _fmt_num(ATTENTION_LIMIT)))
    else:
        out["note"] = "按数值从大到小排（同值按登记顺序），上限 %s 条" % _fmt_num(ATTENTION_LIMIT)
    return out


# ---------------------------------------------------------------- 渲染（只从结构化代入数字）
def render_markdown(report: Dict[str, Any]) -> str:
    r = report
    L: List[str] = []
    L.append("# %s · %s" % (r["title"], r["date"]))
    L.append("")
    L.append("- 日期/时区口径：%s" % r["timezone"])
    L.append("- 生成时刻：%s" % r["generated_at"])
    L.append("- 硬口径：每个数字都来自结构化源并带来源；取不到写「%s」，不塌成 0；"
             "本日报不生成任何下发。" % NO_DATA_TEXT)
    L.append("- 内存态声明：到位自证 / 报警台账 / 一格三证 为**本机本次会话**，不是持久历史。")
    L.append("")
    a = r["attention"]
    L.append("## 今日最该关注的事（规则排序 · 上限 %s 条）" % _fmt_num(a["display_limit"]))
    L.append("")
    if not a["items"]:
        L.append("- %s" % a["note"])
    else:
        for it in a["items"]:
            L.append("- 第 %s 件：%s —— %s%s（%s）" % (
                _fmt_num(it["rank"]), it["label"], _fmt_num(it["value"]), it["unit"],
                it["basis"]))
        L.append("- %s" % a["note"])
    for sec in r["sections"].values():
        L.append("")
        L.append("## %s" % sec["title"])
        L.append("")
        L.append("- 来源：%s" % sec["source"])
        if sec.get("note"):
            L.append("- 说明：%s" % sec["note"])
        if sec.get("status") in ("no_data", "unavailable"):
            L.append("- 状态：%s" % sec["status"])
        for line in sec["lines"]:
            L.append("- %s" % line)
    L.append("")
    L.append("## 数据源清单（人工核对入口）")
    L.append("")
    for sec in r["sections"].values():
        L.append("- %s ← %s" % (sec["title"], sec["source"]))
    L.append("")
    L.append("> 由后端 services/daily_report.py 固定模板生成（零 LLM）；"
             "打印贴墙前先核对「数据源清单」一节。")
    return "\n".join(L)


# ---------------------------------------------------------------- 总入口
_SECTION_BUILDERS: List[Tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [
    ("flows", lambda d, w: _build_flows(d, w[0], w[1])),
    ("twin", lambda d, w: _build_twin()),
    ("evidence", lambda d, w: _build_evidence(d)),
    ("alarms", lambda d, w: _build_alarms()),
    ("points", lambda d, w: _build_points()),
    ("cells", lambda d, w: _build_cells()),
    ("color", lambda d, w: _build_color(w[2])),
]


def build_report(date: Optional[str] = None,
                 color_samples: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """生成当天日报：结构化 + markdown + 一致性自检结果。

    @param date          "YYYY-MM-DD"（**本地日期**）；缺省 = 今天
    @param color_samples 判色样本量（E4）——**可注入参数**，本轮默认 None：
        将来 vision_samples 接口定了由调用方把结构化样本数传进来，本模块不 import 它。
    """
    d = _resolve_date(date)
    w_from, w_to, tz_label = _day_window_utc(d)
    sections: Dict[str, Dict[str, Any]] = {}
    for key, builder in _SECTION_BUILDERS:
        try:
            sec = builder(d, (w_from, w_to, color_samples))
        except Exception as e:                # noqa: BLE001 —— 单节崩了不许带崩整份日报
            sec = _unavailable(_section(key, key, "生成这一节时抛了异常"), e)
        sections[key] = sec

    report: Dict[str, Any] = {
        "ok": True,
        "schema": SCHEMA,
        "title": TITLE,
        "date": d.isoformat(),
        "timezone": tz_label,
        "generated_at": _iso_utc(datetime.now(timezone.utc).replace(tzinfo=None)),
        "window": {"utc_from": _iso_utc(w_from), "utc_to": _iso_utc(w_to)},
        "hard_rules": ("数字全部来自结构化源；无数据写「%s」；零 LLM；零下发；"
                       "内存态标注本机本次会话" % NO_DATA_TEXT),
        "sections": sections,
        "attention": _attention(sections),
    }
    report["markdown"] = render_markdown(report)
    # ★ 出稿前自检（对正文）；不一致项必须显示出来，不许静默通过
    body_incons = consistency_check(report, report["markdown"])
    report["consistency"] = {
        "rule": "markdown 里出现的每个数值都必须能在结构化字段里找到同值（markdown 自身除外）",
        "inconsistent": body_incons,
        "count": len(body_incons),
    }
    if body_incons:
        extra = ["", "## ★ 一致性自检未通过（%s 处，不许发布）" % _fmt_num(len(body_incons))]
        extra += ["- %s" % s for s in body_incons]
        report["markdown"] = report["markdown"] + "\n".join(extra)
    else:
        report["markdown"] = report["markdown"] + "\n一致性自检：通过（正文数值均可追溯到结构化源）\n"
    return report


def stats_note() -> str:
    """给端点/界面的一句话口径说明（同源，不在前端再写一遍判据）。"""
    return ("日报只汇总既有结构化源（事件表 / 到位自证 / 取证包 / 报警台账 / 点位体检 / 一格三证），"
            "零 LLM、零下发；取不到写「%s」，内存态注明本机本次会话。" % NO_DATA_TEXT)
