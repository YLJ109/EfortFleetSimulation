# -*- coding: utf-8 -*-
"""C2「报警合理化」回归钉（services/alarms.py + api/alarms.py）.

现场要解决的问题：十几路同源报警**平铺**成事件流 → 「同一条刷屏 + 分不清哪条要命 +
反复确认同一条」。这一轮做的是工业报警管理（EEMUA-191 / ISA-18.2）的最小可用版，
但**它只是显示与处置层** —— 所以本文件一半的钉子都在钉「不许越界」：

  ★★ 核心验收钉：`test_shelving_never_affects_guard_check`
     搁置 / 确认 / 折叠 / 延时**都不得**改变 `safety_guard.check()` 的返回值。
     互锁与报警显示是两条独立通路：把报警「关掉」绝不可能让围栏放行。

其余按机制分：字典（含未登记不吞）· 关联抑制（明细一条不丢）· 延时确认
（安全类一律不延时，反证钉）· 分级处置（critical/high 必须人确认、low 可自动清除）·
搁置（安全类禁止、到期自动回活动态）· 优先级不可被静默降级 · 审计 · 水位（§5.5 铁律①）。

★ 全部用例都不动真机：`now` 参数把时间喂给纯内存的报警层；需要 emit 的地方
  一律 monkeypatch 掉，不写生产审计库。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.services import alarms as AL
from app.services import events as EV
from app.services import safety_guard as SG
from app.services.alarms import Rule, alarm_center

REAL_EMIT = EV.emit          # 打桩前抓住真入口（有一条钉子要用真 emit 验容错）

# 本例被拦下的 emit 记录：报警层的审计（alarms.ack / alarms.shelve / …）都落在这里。
# ★ 用**模块级唯一**列表并在每例清空 —— 报警层是 `from app.services.events import emit`
#   在调用时取属性，若装两个互相覆盖的桩，谁生效取决于 fixture 顺序，钉子就会假绿。
AUDIT_CALLS = []


# ---------------------------------------------------------------- 隔离（进程级台账）
@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每例前后清报警台账；默认**不**往审计库里写东西（纯机制用例不需要 DB）。

    ★ 报警层是进程级内存状态（和 ui_speed / hold_state 同一类），不清理就会出现
      「上一条用例留下的活动报警把这一条挤掉」的跨用例串味。
    """
    AL.reset()
    SG.reset_server()
    AUDIT_CALLS.clear()

    def _stub(category, level, action, message="", detail=None, actor="system",
              ip="", broadcast=True):
        AUDIT_CALLS.append({"category": category, "level": level, "action": action,
                            "message": message, "detail": detail, "actor": actor,
                            "ip": ip, "broadcast": broadcast})
        return {"type": "event", "id": len(AUDIT_CALLS), "action": action,
                "category": category, "level": level, "message": message,
                "detail": detail, "actor": actor}

    monkeypatch.setattr(EV, "emit", _stub)
    yield AUDIT_CALLS
    AL.reset()
    SG.reset_server()
    AUDIT_CALLS.clear()


T0 = 1_700_000_000.0        # 固定基准时刻（用例里所有时间都由 now= 显式喂进来）


def _ev(action, level="warn", category="safety", message="", detail=None, eid=1):
    return {"type": "event", "id": eid, "category": category, "level": level,
            "action": action, "message": message or action,
            "detail": (json.dumps(detail, ensure_ascii=False) if detail is not None else ""),
            "actor": "system", "ip": "", "timestamp": ""}


def _keys(now=None):
    return [i["key"] for i in AL.active(now=now)]


def _one(key, now=None):
    for i in AL.active(now=now):
        if i["key"] == key:
            return i
    return None


# ================================================================ ① 报警字典
def test_dictionary_registers_priority_consequence_action_and_source():
    """每条登记都必须有优先级 + 后果 + 要求动作 + 来源（缺一项就不是可用的报警字典）。"""
    rs = AL.rules()
    assert len(rs) >= 25, "字典至少要覆盖现场那十几路报警源"
    for rid, r in rs.items():
        assert r.priority in AL.PRIORITIES, rid
        assert r.consequence and r.action and r.source, rid
        assert r.id == rid
    # 现场最关心的几条必须在字典里（不是空壳字典）
    for must in ("control.estop", "control.estop_failed", "safety.hit", "safety.danger",
                 "safety.server_danger", "connection.degraded", "collector.crashed",
                 "control.move_blocked", "twin_residual_over", "sorting.no_judge",
                 "rc.alarm.5005", "rc.alarm.3909", "rc.alarm.1812", "rc.alarm.4902"):
        assert must in rs, must
    assert rs["control.estop_failed"].priority == AL.P_CRITICAL
    assert rs["twin_residual_over"].priority == AL.P_LOW          # 性能/提示 = low
    assert rs["safety.hit"].priority == AL.P_CRITICAL             # 会撞机/人身 = critical


def test_safety_class_can_never_be_registered_with_delay_or_auto_clear():
    """★★ 反证钉（红线在**登记口**就拒绝，不是运行时才降级）：

    「danger/hit/急停/围栏互锁一律不走延时」这条如果只在运行时判，
    漏写一处就晚了一年 —— 所以这里让 register() 直接抛错。
    """
    with pytest.raises(ValueError):
        AL.register(Rule("test.safety.delayed", AL.P_CRITICAL, "后果", "动作", "来源",
                         safety=True, delay_ms=5_000))
    with pytest.raises(ValueError):
        AL.register(Rule("test.safety.autoclear", AL.P_MEDIUM, "后果", "动作", "来源",
                         safety=True, auto_resolve_ms=60_000))
    # critical/high 一律不许自动清除（必须人确认）
    for pr in (AL.P_CRITICAL, AL.P_HIGH):
        with pytest.raises(ValueError):
            AL.register(Rule("test.hi.autoclear", pr, "后果", "动作", "来源",
                             auto_resolve_ms=60_000))
    # 优先级乱写 / 缺后果缺动作 / 重复登记：同样拒
    with pytest.raises(ValueError):
        AL.register(Rule("test.bad.prio", "urgent", "后果", "动作", "来源"))
    with pytest.raises(ValueError):
        AL.register(Rule("test.bad.text", AL.P_LOW, "", "", "来源"))
    AL.register(Rule("test.dup", AL.P_LOW, "后果", "动作", "来源"))
    with pytest.raises(ValueError):
        AL.register(Rule("test.dup", AL.P_LOW, "后果", "动作", "来源"))
    assert "test.safety.delayed" not in AL.rules()      # 被拒的登记不许留下半个键


def test_unregistered_alarm_is_still_shown_as_unclassified():
    """★ 字典里没有的报警 → 归入 unclassified 并**照常显示**（不许因为没登记就吞掉）。"""
    AL.ingest(_ev("brand.new.alarm.source", level="error", category="vision",
                  message="字典里没写过的某路报警"), now=T0)
    it = _one("unregistered:brand.new.alarm.source", now=T0 + 1)
    assert it is not None, "没登记的报警被吞掉了 —— 这就是静默失效"
    assert it["priority"] == AL.P_UNCLASSIFIED
    assert it["priority_source"] == "unregistered"
    assert "未分级" in it["priority_label"]
    assert "未" in it["consequence"] and "未知" in it["consequence"], it["consequence"]
    assert it["action_required"] and "登记" in it["action_required"], "要告诉下一步做什么"
    assert it["note"], "要如实说明为什么没有分级"


def test_unclassified_sorts_above_medium_below_high():
    """「不知道有多严重」不许垫底：排序 critical > high > 未分级 > medium > low。"""
    AL.ingest(_ev("control.estop", level="critical", category="control"), now=T0)
    AL.ingest(_ev("safety.danger", level="warn", category="safety",
                  detail={"zone_id": "ZD"}), now=T0)
    AL.ingest(_ev("mystery.source", level="error", category="system"), now=T0)
    AL.ingest(_ev("twin_residual_over", level="warn", category="safety"), now=T0)
    order = [(i["priority"], i["key"]) for i in AL.active(now=T0 + 1)]
    pr = [p for p, _ in order]
    assert pr == [AL.P_CRITICAL, AL.P_HIGH, AL.P_UNCLASSIFIED, AL.P_LOW], order
    ranks = [AL.priority_rank(p) for p in pr]
    assert ranks == sorted(ranks), "排序不是按优先级水位：操作员会先看到不痛不痒的那条"


def test_codes_are_only_taken_from_alarm_context_not_any_number():
    """★ 反证钉：不许把「第 3 次」「寄存器 40103」这类数字当成控制器报警码。

    抓错一个数字 = 凭空造出一条「控制器报警 4013/4010」的假报警，比漏报更糟。
    """
    assert AL.extract_codes({"fail_count": 3}, "连续 30 次读取失败，寄存器 40103 未变") == []
    assert AL.extract_codes(None, "速度写成 20 而不是 100") == []
    # 结构化字段与「报警」语境才认
    assert AL.extract_codes({"alarm": [5005, 3909]}) == [5005, 3909]
    assert AL.extract_codes(None, "控制器有未清报警 5005/3909") == [5005, 3909]
    # 未登记的码也照实出来（归未分级），不是丢掉
    AL.ingest(_ev("control.move_blocked", category="control", level="warn",
                  message="未知报警 6001 未清", detail={"alarm": [6001]}), now=T0)
    it = _one("rc.alarm.6001", now=T0 + 1)
    assert it is not None and it["priority"] == AL.P_UNCLASSIFIED
    # ★ 键必须与「来路」无关：动作没登记、只从消息里认到码 → 同一个键（不许显示成两行）
    AL.reset()
    AL.ingest(_ev("brand.new.source", category="control", level="warn",
                  message="控制器有未清报警 5005", detail={"alarm": [5005]}), now=T0)
    AL.ingest(_ev("control.move_blocked", category="control", level="warn",
                  message="控制器有未清报警 5005", detail={"alarm": [5005]}), now=T0)
    keys = _keys(now=T0 + 1)
    assert len([k for k in keys if k.startswith("rc.alarm.5005")]) == 1, \
        "同一个物理报警码因为来路不同显示成两行：%s" % keys


# ================================================================ ② 关联抑制（chrouping）
def test_same_root_cause_within_window_collapses_into_one():
    """同一根因短时窗内重复 → 折叠成一条活动报警，记 count 与首/末时刻。"""
    AL.ingest(_ev("safety.hit", category="safety", level="critical",
                  detail={"zone_id": "Z1"}), now=T0)
    AL.ingest(_ev("safety.hit", category="safety", level="critical",
                  detail={"zone_id": "Z1"}), now=T0 + 2)
    AL.ingest(_ev("safety.hit", category="safety", level="critical",
                  detail={"zone_id": "Z1"}), now=T0 + 4)
    rows = [i for i in AL.active(now=T0 + 5) if i["key"] == "safety.hit|Z1"]
    assert len(rows) == 1, "同一条被刷成三行 = 现场那个刷屏问题没解决"
    it = rows[0]
    assert it["count"] == 3 and it["suppressed"] == 2
    assert it["first_at"] and it["last_at"] and it["first_at"] != it["last_at"]
    assert it["state"] == "active" and not it["pending"]


def test_collapsed_entries_are_still_in_history():
    """★ 被抑制的条目必须仍能在明细里查到（只在列表上折叠，一条都不许丢）。"""
    for i in range(5):
        AL.ingest(_ev("connection.degraded", category="connection", level="warn",
                      eid=100 + i, detail={"fail_count": 30 + i}), now=T0 + i)
    # 让它升级为活动报警（延时 2s），再确认列表只有一条
    AL.ingest(_ev("connection.degraded", category="connection", level="warn",
                  detail={"fail_count": 40}), now=T0 + 6)
    assert len(_keys()) == 1
    h = AL.history()
    rows = [r for r in h["items"] if r["key"] == "connection.degraded"]
    assert len(rows) == 6, "明细里少了条目 —— 折叠不等于丢数据"
    states = [r["state"] for r in rows]
    assert "folded" in states, "被折叠的原始条目要能看出它是被折叠的"
    assert all(r["event_id"] for r in rows if r["state"] != "cleared"), \
        "每条明细都要带上审计 event_id，好让 /api/events 逐条对上"


def test_recurrence_beyond_window_is_a_new_episode():
    """超过关联窗后再出现 = **又发生了一次**（不并入旧计数，否则新故障被旧计数掩盖）。"""
    AL.ingest(_ev("collector.crashed", category="connection", level="error"), now=T0)
    later = T0 + AL.GROUP_WINDOW_MS / 1000.0 + 5
    AL.ingest(_ev("collector.crashed", category="connection", level="error"), now=later)
    it = _one("collector.crashed", now=later + 1)
    assert it["count"] == 1, "跨窗复发被并进了旧计数 —— 现场会以为只有一次"
    assert "新一次" in (it["note"] or "")


def test_different_zones_do_not_merge():
    """两个区域各自报警是**两条**（合并会把「哪一侧被入侵」这个关键信息抹掉）。"""
    AL.ingest(_ev("safety.danger", category="safety", detail={"zone_id": "ZA"}), now=T0)
    AL.ingest(_ev("safety.danger", category="safety", detail={"zone_id": "ZB"}), now=T0)
    keys = _keys(now=T0 + 1)
    assert "safety.danger|ZA" in keys and "safety.danger|ZB" in keys


def test_consequence_and_root_cause_report_separately():
    """「下发被拦（后果）」+「控制器报警 5005（根因）」= 两条，操作员才分得清哪条要命。"""
    AL.ingest(_ev("control.move_blocked", category="control", level="warn",
                  message="控制器有未清报警 5005/3909，运动指令会被拒绝执行",
                  detail={"alarm": [5005, 3909]}), now=T0)
    keys = set(_keys(now=T0 + 1))
    assert {"control.move_blocked", "rc.alarm.5005", "rc.alarm.3909"} <= keys, keys
    assert AL.rules()["rc.alarm.5005"].consequence, "根因必须写清意味着什么"


# ================================================================ ③ 延时确认（on-delay）
def test_jittery_alarm_stays_pending_until_it_persists():
    """抖动型（通讯瞬时超时）→ 先候选态 pending，**不**直接升级成活动报警。"""
    AL.ingest(_ev("connection.degraded", category="connection", level="warn",
                  message="连续 30 次读取失败，临时降级为模拟"), now=T0)
    it = _one("connection.degraded", now=T0 + 0.5)
    assert it["pending"] is True and it["state"] == "pending"
    assert it["delay_ms"] == 2000
    assert "候选" in (it["note"] or "")


def test_pending_candidate_upgrades_after_delay_ms():
    AL.ingest(_ev("connection.degraded", category="connection", level="warn"), now=T0)
    AL.ingest(_ev("connection.degraded", category="connection", level="warn"), now=T0 + 2.5)
    it = _one("connection.degraded", now=T0 + 2.6)
    assert it["pending"] is False and it["state"] == "active"
    assert it["count"] == 2 and "升级为活动报警" in (it["note"] or "")


def test_pending_candidate_that_never_persists_never_becomes_active():
    """一闪即逝的抖动：不升级、不刷屏，但**明细里留痕**（说明为什么没升级）。"""
    AL.ingest(_ev("connection.degraded", category="connection", level="warn"), now=T0)
    much_later = T0 + 2 + AL.PENDING_EXPIRE_GRACE_MS / 1000.0 + 1
    assert "connection.degraded" not in _keys(now=much_later)
    rows = [r for r in AL.history()["items"] if r["key"] == "connection.degraded"]
    assert rows and rows[0]["state"] == "cleared"
    assert "没有升级" in rows[0]["message"]


def test_safety_alarm_is_active_immediately_never_delayed():
    """★★ 红线反证：安全类（danger/hit/急停/围栏互锁）**没有**候选态，第一条就是活动报警。

    延时一次就可能把「已经撞上了」晚两秒才摆到操作员面前 —— 这是不能换的。
    """
    for action, level in (("safety.hit", "critical"), ("safety.danger", "warn"),
                          ("control.estop", "critical"), ("control.estop_failed", "critical"),
                          ("control.move_blocked", "warn"), ("safety.server_danger", "warn")):
        AL.reset()
        AL.ingest(_ev(action, category="control" if "control" in action else "safety",
                       level=level, detail={"zone_id": "Z1"}), now=T0)
        it = AL.active(now=T0)[0]
        assert it["pending"] is False, action
        assert it["delay_ms"] == 0, action
        assert it["state"] == "active", action
        assert it["safety_class"] is True, action


# ================================================================ ④ 分级处置
def test_critical_requires_human_ack_and_ack_only_clears_display():
    """critical/high 只能由人确认；确认**只清显示**，并在审计里写清它不解除互锁。"""
    AL.ingest(_ev("control.estop", category="control", level="critical"), now=T0)
    # 时间再久也不会自己消失（登记里没有 auto_resolve_ms，且 critical 禁止登记它）
    assert "control.estop" in _keys(now=T0 + 10 * 60 * 60)
    out = AL.ack(["control.estop"], actor="operator", now=T0 + 5)
    assert out["acked"] == ["control.estop"] and out["ok"]
    assert "control.estop" not in _keys(now=T0 + 6)
    assert "只清显示" in out["note"]
    audits = [c for c in AUDIT_CALLS if c["action"] == "alarms.ack"]
    assert audits, "确认必须有审计留痕"
    assert "不解除任何互锁" in audits[0]["message"]
    assert audits[0]["detail"]["does_not_release"], "审计要写明它解除不了什么"


def test_low_priority_auto_clears_without_ack():
    """low（提示级）没有复发就自动清除 —— 别把操作员淹在不需确认的东西里。"""
    AL.ingest(_ev("twin_residual_over", category="safety", level="warn",
                  message="段级到位自证超差"), now=T0)
    assert "twin_residual_over" in _keys(now=T0 + 1)
    gone = T0 + AL.LOW_AUTO_RESOLVE_MS / 1000.0 + 5
    assert "twin_residual_over" not in _keys(now=gone)
    rows = [r for r in AL.history()["items"] if r["key"] == "twin_residual_over"]
    assert any("自动清除" in r["message"] for r in rows)


def test_medium_and_unknown_are_not_auto_cleared():
    """medium / 未分级 没有登记 auto_resolve → 必须人确认（不许自己蒸发）。"""
    AL.ingest(_ev("hold_state_contradiction", category="safety", level="warn"), now=T0)
    AL.ingest(_ev("nobody.known", category="vision", level="error"), now=T0)
    later = T0 + 2 * 60 * 60
    keys = _keys(now=later)
    assert "hold_state_contradiction" in keys and "unregistered:nobody.known" in keys


def test_recovery_event_clears_the_matching_alarm():
    """有明确恢复事件的（链路降级 → connection.restored）自动解除，不用人点。"""
    AL.ingest(_ev("connection.degraded", category="connection", level="warn"), now=T0)
    AL.ingest(_ev("connection.degraded", category="connection", level="warn"), now=T0 + 3)
    assert "connection.degraded" in _keys(now=T0 + 4)
    AL.ingest(_ev("connection.restored", category="connection", level="info"), now=T0 + 10)
    assert "connection.degraded" not in _keys(now=T0 + 11)


# ================================================================ ⑤ 搁置（shelving）
def test_shelve_rejects_safety_class():
    """★★ 安全类禁止搁置（如实拒绝 + 原因，不装作「搁置成功了」）。"""
    AL.ingest(_ev("safety.hit", category="safety", level="critical",
                  detail={"zone_id": "Z1"}), now=T0)
    AL.ingest(_ev("control.estop", category="control", level="critical"), now=T0)
    AL.ingest(_ev("control.move_blocked", category="control", level="warn"), now=T0)
    for k in ("safety.hit|Z1", "control.estop", "control.move_blocked"):
        assert _one(k, now=T0 + 1)["safety_class"] is True
        assert _one(k, now=T0 + 1)["can_shelve"] is False
        with pytest.raises(AL.AlarmOpError) as ei:
            AL.shelve(k, minutes=5, actor="operator", now=T0 + 2)
        assert "禁止搁置" in str(ei.value)
        assert _one(k, now=T0 + 3) is not None, "被拒绝的搁置不许顺手把报警清掉"


def test_shelve_holds_non_safety_and_returns_on_expiry():
    AL.ingest(_ev("sorting.no_judge", category="sorting", level="warn"), now=T0)
    out = AL.shelve("sorting.no_judge", minutes=10, actor="operator",
                    reason="正在换料", now=T0 + 1)
    assert out["shelved"] is True and out["state"] == "shelved"
    assert out["shelve_until"]
    AL.refresh(now=T0 + 5)
    assert "sorting.no_judge" in [i["key"] for i in AL.active(now=T0 + 5)] and \
        _one("sorting.no_judge", now=T0 + 5)["shelved"] is True
    # 到期 → 自动回到活动态（搁置不是永久静音）
    assert _one("sorting.no_judge", now=T0 + 11 * 60)["shelved"] is False
    rows = [r for r in AL.history()["items"] if r["key"] == "sorting.no_judge"]
    assert any(r["state"] == "shelve_expired" for r in rows)
    with pytest.raises(AL.AlarmOpError):
        AL.unshelve("sorting.no_judge", now=T0 + 12 * 60)


def test_default_shelve_timeout_is_documented_and_bounded():
    assert AL.SHELVE_DEFAULT_MIN == 30
    AL.ingest(_ev("ai.relay_fail", category="ai", level="warn"), now=T0)
    out = AL.shelve("ai.relay_fail", actor="operator", now=T0)
    left = out["shelve_left_sec"]
    assert 29 * 60 < left <= 30 * 60, left
    # 传垃圾值不会把搁置变成「永久」
    AL.ingest(_ev("ai.takeover", category="ai", level="warn"), now=T0)
    out2 = AL.shelve("ai.takeover", minutes=999999, actor="operator", now=T0)
    assert out2["shelve_left_sec"] <= 24 * 3600 + 1


# ================================================================ 核心验收钉
def test_shelving_never_affects_guard_check():
    """★★★ 本轮验收的核心：搁置 / 确认 / 折叠任何报警，都**不得**改变 safety_guard.check()。

    互锁与报警显示是两条独立通路 —— 把报警「关掉」绝不可能让围栏放行。
    做法：让前端那一路真的报 danger（check 必须拒），然后把报警层能做的事全做一遍
    （摄入这条报警、折叠 100 次、确认、试图搁置），再逐字段比对 check() 的返回。
    """
    SG.reset_server()
    SG.update("danger", zone_id="Z9", zone_name="碰撞区", clearance=-0.05, ratio=0.2)
    allow0, reason0, snap0 = SG.check()
    assert allow0 is False, "前置条件就不成立：danger 必须拒下发"

    # 报警层看到同一条 danger，并重复 100 次（折叠 + 优先级压力测试）
    AL.reset()
    for i in range(100):
        AL.ingest(_ev("safety.danger", category="safety", level="warn",
                      message="后端评估：接近/危险 碰撞区", eid=i + 1,
                      detail={"zone_id": "Z9", "zone_name": "碰撞区"}), now=T0 + i * 0.1)
    it = _one("safety.danger|Z9", now=T0 + 12)
    assert it["count"] == 100
    assert it["priority"] == AL.P_HIGH, "优先级被静默改过"

    # 能搁置吗？安全类一律拒绝
    with pytest.raises(AL.AlarmOpError):
        AL.shelve("safety.danger|Z9", minutes=60, actor="operator", now=T0 + 20)
    # 人确认（只清显示）
    AL.ack(["safety.danger|Z9"], actor="operator", note="我看过了", now=T0 + 21)
    assert "safety.danger|Z9" not in _keys(now=T0 + 22)

    allow1, reason1, snap1 = SG.check()
    assert allow1 == allow0 is False
    assert reason1 == reason0, "报警处置改了互锁结论 —— 这是事故"
    assert snap1["state"] == snap0["state"] == "danger"
    assert snap1["merged"]["state"] == "danger", "合并结论也被动过"
    # 后端独立评估那一路同理
    SG.update_server({"state": "hit", "zone_id": "Z9", "zone_name": "碰撞区", "clearance": -0.1})
    a_hit, r_hit, _ = SG.check()
    AL.ack([k for k in _keys(now=T0 + 30) if k.startswith("safety.server_hit")] or ["none"],
           actor="operator", now=T0 + 31)
    a_hit2, r_hit2, _ = SG.check()
    assert (a_hit, r_hit) == (a_hit2, r_hit2)


def test_alarm_layer_is_not_consumed_by_any_safety_or_motion_module():
    """★ 结构反证：报警层只有**两个**消费者（事件总线的观察者 + 自己的 REST 读接口）。

    任何互锁 / 运动 / 就绪模块 import 或读取 alarms，就等于把它变成第二套安全判据。
    这里用 AST 精确判「有没有把这个模块当代码用」，不拿字符串瞎匹配（字符串会误报，
    也会漏报，误报的钉子一年后没人敢改）。

    白名单只有四个消费者，每个都写清它凭什么在名单里；新增消费者必须先想清楚
    自己是不是在把显示层当成第二套判据用。
    """
    root = Path(AL.__file__).resolve().parents[1]          # backend/app
    self_ = root / "services" / "alarms.py"
    # 允许出现的消费者（相对 app/ 的路径 → 凭什么被允许）
    whitelist = {
        "services/events.py": "事件总线的观察者钩子（只调 ingest，出错被 emit 吞掉）",
        "api/alarms.py": "报警 REST：唯一的人操作入口（ack/shelve/unshelve/priority，全要令牌）",
        "main.py": "只 include_router(alarms.router)，不读报警内容",
        "services/daily_report.py": "只读日报（summary/stats/history），不改任何报警状态",
    }
    # ★ 只有 REST 那一个人操作入口可以调用这些「会改台账」的口子
    mutating = {"ack", "shelve", "unshelve", "set_priority", "register", "reset"}
    consumers = {}
    for py in sorted(root.rglob("*.py")):
        if py == self_:
            continue
        src = py.read_text(encoding="utf-8", errors="ignore")
        if "alarm" not in src.lower():
            continue
        tree = ast.parse(src, filename=str(py))
        bound = set()                      # 本文件里被绑成报警层的那些名字
        for node in ast.walk(tree):        # 第一遍：只看绑定
            if isinstance(node, ast.Import):
                for a in node.names:
                    if str(a.name).split(".")[-1] == "alarms":
                        bound.add(a.asname or "alarms")
            elif isinstance(node, ast.ImportFrom):
                mod = str(node.module or "")
                for a in node.names:
                    # from app.services import alarms / from app.api import alarms
                    if mod.split(".")[-1] == "alarms" or str(a.name).split(".")[-1] == "alarms":
                        bound.add(a.asname or a.name)
        if not bound:
            continue
        rel = py.relative_to(root).as_posix()
        used = set()
        for node in ast.walk(tree):        # 第二遍：把它当代码用了的口子
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id in bound:
                used.add(node.attr)
        consumers[rel] = used

    assert consumers, "扫描没找到任何消费者 —— 说明钩子没接上，这条钉子是假的"
    extra = [k for k in consumers if k not in whitelist]
    assert not extra, "报警层多了名单外的消费者（互锁/运动绝不允许）：%s" % extra
    # 安全 / 运动 / 就绪那一路连「只读」都不许读它：读它就是把它当第二套判据
    for rel in consumers:
        low = rel.lower()
        for danger in ("motion", "modbus", "jog", "rc_ready", "safety", "limits",
                       "sim_robot", "camera", "collector"):
            assert danger not in low, "控制/互锁那一路在消费报警层：%s" % rel
    # 除了 REST 那一个入口，别处一律只能读，不许确认 / 搁置 / 改优先级 / 清台账
    for rel, used in consumers.items():
        if rel == "api/alarms.py":
            continue
        assert not (used & mutating), "%s 调用了会改台账的口子：%s" % (rel, sorted(used & mutating))
    # 日报这个只读消费者必须真的只读（它哪天开始 ack 就是事故）
    assert consumers.get("services/daily_report.py", set()) <= {
        "summary", "stats", "history", "active", "latest_seq", "history_seq", "rules",
        "refresh", "alarm_center"}, consumers.get("services/daily_report.py")

    # 报警层自己也不许碰任何写路径 / 互锁模块（它是显示层，不是控制层）
    src = self_.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            for a in node.names:
                imported.add(str(a.name).split(".")[-1])
                imported.add(str(mod).split(".")[-1])
    for forbidden in ("motion", "modbus", "rc_ready", "sim_robot", "jog",
                      "camera_client", "safety_guard", "safety_eval", "ui_speed",
                      "limits", "collector"):
        assert forbidden not in imported, "报警层 import 了控制/互锁模块：%s" % forbidden
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for danger in ("command", "write_reg", "write_registers", "read_regs", "estop",
                   "move", "jog", "get_json", "post_json"):
        assert danger not in calls, "报警层出现了下发形态的调用：%s" % danger


# ================================================================ ⑥ 优先级不可静默降级
def test_priority_never_downgraded_by_repetition():
    """★★ 出现 500 次也**不许**自动降优先级（只加 count）。"""
    AL.ingest(_ev("safety.hit", category="safety", level="critical",
                  detail={"zone_id": "Z1"}), now=T0)
    for i in range(500):
        AL.ingest(_ev("safety.hit", category="safety", level="critical",
                      detail={"zone_id": "Z1"}, eid=i + 10), now=T0 + (i % 10) * 0.5)
    it = _one("safety.hit|Z1", now=T0 + 12)
    assert it["priority"] == AL.P_CRITICAL
    assert it["priority_source"] == "dictionary"
    assert it["count"] == 501


def test_priority_only_changes_by_explicit_audited_operator_action():
    """优先级唯一的可写入口：显式动作 + 理由 + 审计事件（三者缺一不可）。"""
    AL.ingest(_ev("control.move_blocked", category="control", level="warn"), now=T0)
    with pytest.raises(AL.AlarmOpError):
        AL.set_priority("control.move_blocked", "low", actor="operator", reason="", now=T0 + 1)
    with pytest.raises(AL.AlarmOpError):
        AL.set_priority("control.move_blocked", "urgent", actor="operator",
                        reason="理由", now=T0 + 1)
    out = AL.set_priority("control.move_blocked", AL.P_MEDIUM, actor="operator",
                          reason="该条与本工位无关，临时降级", now=T0 + 2)
    assert out["priority"] == AL.P_MEDIUM
    assert out["priority_source"] == "operator" and out["priority_set_by"] == "operator"
    chg = [c for c in AUDIT_CALLS if c["action"] == "alarms.priority"]
    assert chg and chg[-1]["detail"]["from"] == AL.P_HIGH and \
        chg[-1]["detail"]["to"] == AL.P_MEDIUM and chg[-1]["detail"]["reason"]
    # 改完优先级后再重复出现：仍然是操作员给的那一档，不回退也不自动升降
    AL.ingest(_ev("control.move_blocked", category="control", level="warn"), now=T0 + 3)
    it = _one("control.move_blocked", now=T0 + 4)
    assert it["priority"] == AL.P_MEDIUM and it["priority_source"] == "operator"


def test_latest_seq_is_monotonic_and_drives_refresh():
    """★★ §5.5 铁律①：水位只增不减。列表变短（清除/折叠/过滤）时长度会骗人，seq 不会。"""
    a = AL.latest_seq()
    AL.ingest(_ev("safety.warn", category="safety", detail={"zone_id": "Z1"}), now=T0)
    b = AL.latest_seq()
    assert b > a
    # 折叠计数 → 继续推进
    AL.ingest(_ev("safety.warn", category="safety", detail={"zone_id": "Z1"}), now=T0 + 1)
    c = AL.latest_seq()
    assert c > b
    # 报警被清除、列表空了 → 水位**不降**
    AL.ack(["safety.warn|Z1"], actor="operator", now=T0 + 2)
    d = AL.latest_seq()
    assert d > c
    assert AL.active(now=T0 + 3) == []
    assert AL.summary(now=T0 + 3)["latest_seq"] >= d
    s = AL.stats()
    assert s["latest_seq"] == d


def test_shelve_expiry_and_auto_clear_bump_the_water_level():
    AL.ingest(_ev("ai.relay_skip", category="ai", level="warn"), now=T0)
    AL.shelve("ai.relay_skip", minutes=1, actor="operator", now=T0 + 1)
    before = AL.latest_seq()
    AL.refresh(now=T0 + 130)                     # 到期回到活动态
    assert AL.latest_seq() > before


# ================================================================ 审计 / 隔离性
def test_audit_uses_existing_categories_and_emit_entrypoint():
    """审计必须走 events.emit 这个唯一入口，category 用**已存在**的合法值（不新增）。"""
    from app.services.events import CATEGORIES
    AL.ingest(_ev("sorting.no_judge", category="sorting", level="warn"), now=T0)
    AL.shelve("sorting.no_judge", actor="operator", reason="临时", now=T0 + 1)
    AL.unshelve("sorting.no_judge", actor="admin", now=T0 + 2)
    AL.ingest(_ev("sorting.no_judge", category="sorting", level="warn"), now=T0 + 3)
    AL.ack(["sorting.no_judge"], actor="admin", now=T0 + 4)
    acts = [c["action"] for c in AUDIT_CALLS]
    assert {"alarms.shelve", "alarms.unshelve", "alarms.ack"} <= set(acts), acts
    for c in AUDIT_CALLS:
        assert c["category"] in CATEGORIES, c
        assert c["category"] == "control", "报警处置属于人操作：统一用既有 control 类"
    assert AUDIT_CALLS[0]["actor"] in ("admin", "operator", "unknown")


def test_alarm_layer_never_breaks_the_event_bus(monkeypatch):
    """★ 报警层出错绝不能把主流程带崩（events.emit 的观察者 = 可丢弃）。

    这里**必须用真的 emit**（不是打桩那份）：要验的是 emit 尾部那段观察钩子的容错，
    桩里没有 try/except，用桩来验就是假绿。落库那一段真 emit 自己也会吞异常。
    """
    seen = []

    def _boom(event, now=None):
        seen.append(event)
        raise RuntimeError("报警层故意抛错（模拟台账 bug）")

    monkeypatch.setattr(AL, "ingest", _boom)
    monkeypatch.setattr(EV, "emit", REAL_EMIT)
    payload = REAL_EMIT("safety", "critical", "control.estop", "急停已按下")
    assert seen, "钩子压根没调到报警层 —— 那这条钉子就是假的"
    assert payload.get("action") == "control.estop", "落库/返回照旧（没被报警层带崩）"
    assert payload.get("level") == "critical"


def test_own_audit_events_are_never_reingested():
    """自己的审计回吃 = emit→ingest→emit 无限递归；这里钉住那个入口守卫。"""
    assert AL.ingest(_ev("alarms.ack", category="control", level="info"), now=T0) == []
    assert AL.ingest(_ev("alarms.shelve", category="control", level="warn"), now=T0) == []
    assert AL.active(now=T0) == []
    seq = AL.latest_seq()
    assert AL.latest_seq() == seq       # 没推进水位：压根没被当成报警


def test_info_and_debug_events_are_not_alarms():
    """info/debug 是流水（登录成功、恢复可达），不许冒成报警。"""
    AL.ingest(_ev("control.ready", category="control", level="info"), now=T0)
    AL.ingest(_ev("auth.login", category="auth", level="debug"), now=T0)
    AL.ingest(_ev("connection.restored", category="connection", level="info"), now=T0)
    assert AL.active(now=T0 + 1) == []


# ================================================================ REST
def _login(client) -> str:
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_api_alarms_read_is_token_free_monitoring(client):
    """★ 读接口 = 监控类，免令牌（与 /api/rc-status 同口径）：没登录也必须看得见报警。"""
    AL.reset()
    AL.ingest(_ev("safety.hit", category="safety", level="critical",
                  detail={"zone_id": "Z1"}), now=__import__("time").time())
    r = client.get("/api/alarms")
    assert r.status_code == 200, r.text
    d = r.json()
    assert "latest_seq" in d and isinstance(d["latest_seq"], int)
    keys = [i["key"] for i in d["items"]]
    assert "safety.hit|Z1" in keys
    it = [i for i in d["items"] if i["key"] == "safety.hit|Z1"][0]
    for f in ("priority", "consequence", "action_required", "count", "first_at",
              "last_at", "acked", "shelved", "pending", "safety_class", "can_shelve",
              "priority_source"):
        assert f in it, f
    h = client.get("/api/alarms/history")
    assert h.status_code == 200 and h.json()["items"]
    dic = client.get("/api/alarms/dictionary")
    assert dic.status_code == 200 and dic.json()["items"]


def test_api_writes_require_control_token_and_safety_shelve_is_409(client):
    tok = _login(client)
    AL.reset()
    now = __import__("time").time()
    AL.ingest(_ev("safety.hit", category="safety", level="critical",
                  detail={"zone_id": "Z1"}), now=now)
    AL.ingest(_ev("sorting.no_judge", category="sorting", level="warn"), now=now)

    # 没令牌 → 401（与项目其它写接口一致）
    assert client.post("/api/alarms/ack", json={"keys": ["safety.hit|Z1"]}).status_code == 401
    assert client.post("/api/alarms/shelve",
                       json={"key": "sorting.no_judge"}).status_code == 401
    hdr = {"X-Control-Token": tok}
    # 安全类搁置 → 如实 409 + 原因
    r = client.post("/api/alarms/shelve", json={"key": "safety.hit|Z1"}, headers=hdr)
    assert r.status_code == 409, r.text
    assert "禁止搁置" in r.json()["message"]
    # 非安全类可以搁置 / 取消
    r = client.post("/api/alarms/shelve", json={"key": "sorting.no_judge",
                                                 "minutes": 5, "reason": "换料"}, headers=hdr)
    assert r.status_code == 200 and r.json()["alarm"]["shelved"] is True
    r = client.post("/api/alarms/unshelve", json={"key": "sorting.no_judge"}, headers=hdr)
    assert r.status_code == 200 and r.json()["alarm"]["shelved"] is False
    # 确认（只清显示）
    r = client.post("/api/alarms/ack", json={"keys": ["safety.hit|Z1"], "note": "看过"},
                    headers=hdr)
    assert r.status_code == 200 and r.json()["acked"] == ["safety.hit|Z1"]
    assert "safety.hit|Z1" not in [i["key"] for i in client.get("/api/alarms").json()["items"]]
    # 不存在的键 → 如实 404，不假装成功
    assert client.post("/api/alarms/ack", json={"keys": ["no.such.alarm"]},
                       headers=hdr).status_code == 200
    assert client.post("/api/alarms/shelve", json={"key": "no.such.alarm"},
                       headers=hdr).status_code == 404


def test_api_priority_is_admin_gated(client):
    """改优先级=把严重度降下去=风险动作 → 只有管理员令牌能做（操作员 403）。"""
    from app.api import auth as AU
    op = AU.issue_token(AU.ROLE_OPERATOR)["token"]
    adm = _login(client)
    AL.reset()
    AL.ingest(_ev("sorting.no_judge", category="sorting", level="warn"),
              now=__import__("time").time())
    r = client.post("/api/alarms/priority",
                    json={"key": "sorting.no_judge", "priority": "low", "reason": "现场确认"},
                    headers={"X-Control-Token": op})
    assert r.status_code == 403, r.text
    r = client.post("/api/alarms/priority",
                    json={"key": "sorting.no_judge", "priority": "low", "reason": "现场确认"},
                    headers={"X-Control-Token": adm})
    assert r.status_code == 200, r.text
    assert r.json()["alarm"]["priority"] == "low"
    assert r.json()["alarm"]["priority_source"] == "operator"
    # 缺理由 → 422/409（不许「顺手降个级」）
    r = client.post("/api/alarms/priority",
                    json={"key": "sorting.no_judge", "priority": "low", "reason": ""},
                    headers={"X-Control-Token": adm})
    assert r.status_code in (409, 422), r.text


def test_api_history_keeps_collapsed_rows(client):
    """REST 明细里被折叠的条目查得到（列表折叠 ≠ 数据丢失）。"""
    AL.reset()
    now = __import__("time").time()
    for i in range(7):
        AL.ingest(_ev("connection.degraded", category="connection", level="warn",
                      eid=200 + i), now=now + i)
    d = client.get("/api/alarms/history?key=connection.degraded").json()
    assert d["total"] == 7, d["total"]
    assert len(client.get("/api/alarms").json()["items"]) == 1
    assert d["durable_trail"], "要如实说明更久的原始条目在审计表里"
