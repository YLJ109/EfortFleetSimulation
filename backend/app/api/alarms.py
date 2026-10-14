# -*- coding: utf-8 -*-
"""
报警合理化 API（C2）。

GET  /api/alarms            活动报警（含候选 / 已搁置）+ 各自分级、后果、要求动作、
                            出现次数、确认/搁置状态，以及 **latest_seq（只增不减的水位）**
GET  /api/alarms/history     明细（含被折叠 / 被抑制 / 候选过期 / 自动清除的原始条目）
GET  /api/alarms/dictionary  报警字典（哪些登记过、优先级/后果/要求动作是什么）
POST /api/alarms/ack         人确认（★ 只清显示，**不解除任何互锁 / 就绪闸门**）
POST /api/alarms/shelve      临时搁置（★ 安全类一律 409 拒绝）
POST /api/alarms/unshelve    取消搁置
POST /api/alarms/priority    显式改优先级（★ 必须写理由；这是优先级唯一的可写入口）

★ 鉴权口径（照项目既有约定，两条分开）：
  · **读**（alarms / history / dictionary）= 监控类，**免控制令牌** ——
    与 `/api/rc-status`、`/api/safety`、`/api/gamepad/state` 同一口径。
    理由很硬：没登录的观察者也必须看得见「围栏 hit」，否则报警面板自己就成了
    §5.5 铁律⑤ 里那种「看着正常、其实什么都没说」的假安静。
  · **写**（ack / shelve / unshelve / priority）= 人操作，需要控制令牌；
    优先级变更再收一档，只有管理员能改（把严重度降下去本身就是风险动作）。

★ 报警是**显示与处置层**：这些接口改不了互锁、改不了就绪闸门、改不了扫掠结论。
  `services/safety_guard.py::check()` 的返回值与这里发生过什么**无关**
  （反证钉：tests/test_alarms.py::test_shelving_never_affects_guard_check）。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.auth import require_admin, require_control, token_role
from app.services import alarms

router = APIRouter(prefix="/api/alarms", tags=["alarms"])


class AckIn(BaseModel):
    keys: List[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=200)


class ShelveIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    minutes: Optional[float] = Field(default=None, ge=0.1, le=1440,
                                     description="搁置多久；不传用默认（30 分钟），到期自动回到活动态")
    reason: str = Field(default="", max_length=200)


class UnshelveIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


class PriorityIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    priority: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=200)


def _actor(tok: Optional[str]) -> str:
    """审计里的操作者 = 令牌角色；拿不到就如实写 unknown，不冒充 system。"""
    return token_role(tok) or "unknown"


def _reject(key: str) -> HTTPException:
    return HTTPException(status_code=404,
                         detail="报警「%s」已不在活动列表里（可能已确认 / 已恢复 / 已到期）。"
                                "明细可在 GET /api/alarms/history 查到" % key)


@router.get("")
def api_alarms(
    priority: Optional[str] = Query(None, description="只看某一优先级"),
    include_shelved: bool = Query(True, description="是否把已搁置的一并列出来（默认列，方便取消）"),
    include_pending: bool = Query(True, description="是否把抖动候选态一并列出来（默认列，标 pending）"),
):
    s = alarms.summary()
    items = s["items"]
    if priority:
        items = [i for i in items if i.get("priority") == priority]
    if not include_shelved:
        items = [i for i in items if not i.get("shelved")]
    if not include_pending:
        items = [i for i in items if not i.get("pending")]
    # ★ 水位用 latest_seq（只增不减），**不许**用 items 长度：列表会被折叠 / 清除 / 过滤
    return {
        "items": items,
        "total": len(items),
        "total_all": s["total"],
        "by_priority": s["by_priority"],
        "pending": s["pending"],
        "shelved": s["shelved"],
        "safety_class": s["safety_class"],
        "latest_seq": s["latest_seq"],
        "history_seq": s["history_seq"],
        "unregistered": sum(1 for i in items if i.get("priority") == alarms.P_UNCLASSIFIED),
        "window_ms": alarms.GROUP_WINDOW_MS,
        "shelve_default_min": alarms.SHELVE_DEFAULT_MIN,
        "note": "报警显示与处置层：确认 / 搁置 / 抑制都只影响列表，不解除任何互锁与就绪闸门",
    }


@router.get("/history")
def api_alarms_history(
    key: Optional[str] = Query(None, description="只看某条报警的原始明细"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """明细：被折叠 / 被抑制的原始条目**一条不丢**（只在列表上折叠）。"""
    return alarms.history(limit=limit, offset=offset, key=key)


@router.get("/dictionary")
def api_alarms_dictionary():
    """报警字典快照（只读）：界面用它说明「这条有没有登记」。"""
    return {
        "items": [{"id": r.id, "priority": r.priority, "safety_class": r.safety,
                   "delay_ms": r.delay_ms, "title": r.title or r.id,
                   "consequence": r.consequence, "action": r.action, "source": r.source,
                   "auto_resolve_ms": r.auto_resolve_ms}
                  for r in sorted(alarms.rules().values(), key=lambda x: x.id)],
        "unclassified_policy": "字典里没有的报警一律标「未分级」并照常显示，绝不因为没登记就吞掉",
    }


@router.post("/ack")
def api_ack(body: AckIn, tok: str = Depends(require_control)):
    if not body.keys:
        raise HTTPException(status_code=400, detail="ack 需要至少一个报警 key")
    out = alarms.ack(body.keys, actor=_actor(tok), note=body.note)
    out["latest_seq"] = alarms.latest_seq()
    return out


@router.post("/shelve")
def api_shelve(body: ShelveIn, tok: str = Depends(require_control)):
    try:
        out = alarms.shelve(body.key, minutes=body.minutes, actor=_actor(tok),
                            reason=body.reason)
    except KeyError:
        raise _reject(body.key)
    except alarms.AlarmOpError as e:
        # ★ 安全类禁止搁置：如实 409 + 原因，不装作「成功但没生效」
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "alarm": out, "latest_seq": alarms.latest_seq()}


@router.post("/unshelve")
def api_unshelve(body: UnshelveIn, tok: str = Depends(require_control)):
    try:
        out = alarms.unshelve(body.key, actor=_actor(tok))
    except KeyError:
        raise _reject(body.key)
    except alarms.AlarmOpError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "alarm": out, "latest_seq": alarms.latest_seq()}


@router.post("/priority")
def api_priority(body: PriorityIn, tok: str = Depends(require_admin)):
    """★ 优先级唯一的可写入口：必须是管理员的显式动作 + 理由 + 审计事件。

    除这里以外，任何"出现太多次就降级"的自动降级都不存在（反证钉在测试里）。
    """
    try:
        out = alarms.set_priority(body.key, body.priority, actor=_actor(tok), reason=body.reason)
    except KeyError:
        raise _reject(body.key)
    except alarms.AlarmOpError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "alarm": out, "latest_seq": alarms.latest_seq()}
