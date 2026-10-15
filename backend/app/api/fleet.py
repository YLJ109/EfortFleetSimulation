# -*- coding: utf-8 -*-
"""机房 REST 接口（只读）。

用户口径：界面上要看到 12 台的坐标、IP、状态。本模块提供 HTTP 侧的一帧快照，
与 `/ws/pose` 的 `fleet_pose` 帧**同构** —— 前端可以 WS 正常走、掉线时退回轮询，
两套口径不会漂移（字段都来自 Station.snapshot()）。

★ 没有写接口：阶段一只读。这里连"重连某一台"都不提供，因为 driver.open()
   只建 TCP 连接，重连由采集线程自己做（见 services/fleet.py 的 fails%20 分支）。
"""
from __future__ import annotations

from typing import Any, Dict

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.fleet_config import FleetConfig, FleetConfigError, get_fleet
from app.api.auth import require_admin, require_control, token_role
from app.core import fleet_settings as fset
from app.core.fleet_session import IDLE_S, build_scope_ids, fleet_sessions
from app.services.events import emit
from app.services.fleet import fleet

router = APIRouter(prefix="/api", tags=["fleet"])


def _write_caps(cfg: FleetConfig) -> Dict[str, Any]:
    """前端据此决定"执行"按钮能不能按，以及按下去会发生什么。

    ★ 为什么把能力位发到前端而不是让前端猜：按钮能不能按必须和后端真实闸门同源。
      前端自己判"有令牌就允许执行"，就会出现"按下去才收到 501"的界面 ——
      操作员会以为设备坏了，而真实原因（写通道没开）只有后端知道。
    """
    if cfg.simulate:
        return {"ready": True, "mode": "sim",
                "reason": "仿真模式：执行只改本机合成帧，不动任何物理设备"}
    # 真机能不能下发，取决于各型号驱动上的写闸（不是前端能猜的）
    try:
        huayan_on = any(
            bool((cfg.driver_params("huayan_datasheet").get("command_write") or {}).get("enabled"))
            for e in cfg.robots if e.get("driver") == "huayan_datasheet")
    except Exception:
        huayan_on = False
    if huayan_on:
        return {"ready": True, "mode": "real",
                "reason": "真机写通道已开（华沿 10003）；埃夫特仍需现场验证（P4c）"}
    return {"ready": False, "mode": "off",
            "reason": "真机写通道未启用：华沿要 fleet.yaml 的 "
                      "drivers.huayan_datasheet.command_write.enabled=true（需现场验证后再开），"
                      "埃夫特下发在 P4c；要看可执行的效果请开 fleet.simulate"}


def _kinematics(cfg: FleetConfig) -> Dict[str, Any]:
    """每台**型号**一份运动学档案（不是每台一份，12 台只有 2 种身体）。

    为什么要把几何参数发到前端：3D 里画的是真机的姿态，而前端只有 ER8 的 DH
    （`/api/meta`）。华沿那 6 台既没有 GLB 也不是 DH 口径 —— 它用的是官方 xacro
    抄下来的关节链（见 config/models/huayan_elfin.yaml 与 vendor/elfin_model）。
    前端自己再抄一份数值表，就会出现"前后端两套关节链、画面和读数各说各话"。

    ★ 只发几何显示所需的最小字段。这里不含任何写路径参数，也绝不含寄存器表。
    """
    out: Dict[str, Any] = {}
    for entry in cfg.robots:
        key = entry["model"]
        if key in out:
            continue
        prof = cfg.model_profile(key)
        md = prof.get("model_data") or {}
        dh = md.get("dh") or {}
        joints = dh.get("joints") or []
        body: Dict[str, Any] = {
            "brand": prof.get("brand") or "",
            "display_name": prof.get("display_name") or key,
            "axes": int(prof.get("axes") or 6),
            "mounting": ((md.get("mounting") or {}).get("type")) or "floor",
            "joint_limits": md.get("joint_limits_display") or [],
            "mesh": md.get("mesh") or {},
        }
        if len(joints) == 6:
            # 埃夫特：标准 DH（mm / deg），与前端 robotModel.buildRobot 同一口径
            body["chain"] = "dh"
            body["dh"] = {"joints": joints,
                          "calibration_pending": bool(dh.get("calibration_pending"))}
        else:
            # 华沿：URDF 关节链（m / rad），刻意不转 DH —— 转换本身就是误差源
            var_key = md.get("variant") or ""
            var = (md.get("variants") or {}).get(var_key) or {}
            body["chain"] = "urdf"
            body["urdf"] = {
                "variant": var_key,
                "display_name": var.get("display_name") or var_key,
                "joints": var.get("joints") or [],
                "end_fixed": var.get("end_fixed") or {},
                "standing_height_mm": var.get("standing_height_mm"),
                "reach_mm": var.get("reach_mm"),
                "payload_kg": var.get("payload_kg"),
            }
            # 机型没核对过 → 前端要在卡片上标出来，别让它装作已确认
            body["variant_unverified"] = True
        out[key] = body
    return out


def _registry_only() -> Dict[str, Any]:
    """机房没启动时，仍然把地图/名单发出去 —— 前端要据此画 12 个工位并显示"离线"。"""
    cfg = get_fleet()
    robots = [{
        "id": r["id"], "name": r["name"], "zone": r["zone"], "group": r["group"],
        "slot": r["slot"], "order": r["order"], "model": r["model"],
        "brand": r["brand"], "host": r["host"], "port": r["port"],
        "pos": r["pos"], "driver": r["driver"],
        "online": False, "simulated": False, "stale": False,
        "joints": [0.0] * 6, "tcp": None, "cart": None, "status": {},
        "error": "机房采集未启动（config/fleet.yaml: fleet.enabled 或 EFORT_FLEET）",
    } for r in cfg.robots]
    return {"enabled": False, "running": False, "write": _write_caps(cfg), "layout": cfg.layout,
            "streaming": cfg.streaming, "kinematics": _kinematics(cfg),
            "summary": {"total": len(robots), "online": 0, "offline": len(robots),
                        "alarm": 0, "moving": 0},
            "robots": robots}


@router.get("/fleet")
def fleet_state() -> Dict[str, Any]:
    try:
        if fleet.stations:
            return {"enabled": True, "running": True,
                    "write": _write_caps(fleet.cfg),
                    "layout": fleet.cfg.layout, "streaming": fleet.cfg.streaming,
                    "kinematics": _kinematics(fleet.cfg),
                    **fleet.latest()}
        return _registry_only()
    except FleetConfigError as e:
        # 配置不自洽是"机房地图不可信"，必须让界面看到明确报错，而不是静默回空名单。
        raise HTTPException(status_code=500, detail="fleet.yaml 校验失败: %s" % e)


# ---------------------------------------------------------------------
# 控制作用域 + 下发（阶段二：只接仿真，真机分支明确拒绝）
# ---------------------------------------------------------------------

class ScopeIn(BaseModel):
    kind: str = Field(..., description="single | zone | row | all")
    robot_id: Optional[str] = None
    zone: Optional[str] = None
    group: Optional[str] = None


class MoveIn(BaseModel):
    joints: List[float] = Field(..., description="6 个目标关节角（度）")
    speed_pct: float = Field(5.0, ge=5, le=100)


def _actor(tok: str) -> str:
    return token_role(tok) or "unknown"


@router.get("/fleet/scope")
def scope_get() -> dict:
    """当前持权作用域（前端据此决定"执行"能不能按）。"""
    return fleet_sessions.status()


@router.post("/fleet/scope")
def scope_acquire(body: ScopeIn, tok: str = Depends(require_control)) -> dict:
    ids = build_scope_ids(body.kind, body.robot_id or "", body.zone or "",
                          body.group or "", fleet.cfg.robots)
    if not ids:
        raise HTTPException(status_code=400,
                            detail="作用域里没有匹配的机器人（名单可能已改，请刷新）")
    r = fleet_sessions.acquire({"kind": body.kind, "ids": ids,
                                "label": _scope_label(body)}, _actor(tok))
    if not r.get("ok"):
        raise HTTPException(status_code=409,
                            detail="控制权已被 %s 持有（空闲 %.0fs，最长等 %ds）"
                                   % (r.get("taken_by"), r.get("idle_s", 0), int(IDLE_S)))
    return r["session"]


@router.delete("/fleet/scope")
def scope_release(tok: str = Depends(require_control)) -> dict:
    return fleet_sessions.release(_actor(tok))


def _scope_label(body: "ScopeIn") -> str:
    return {"single": "单台 " + (body.robot_id or ""),
            "zone": (body.zone or "") + " 区",
            "row": {"lower": "下排", "upper": "上排"}.get(body.group or "", body.group or ""),
            "all": "全部 12 台"}.get(body.kind, body.kind)


@router.post("/fleet/{robot_id}/move")
def fleet_move(robot_id: str, body: MoveIn, tok: str = Depends(require_control)) -> dict:
    sess = fleet_sessions.current()
    if sess is None:
        raise HTTPException(status_code=403, detail="尚未申请控制作用域：请先选中要操作的范围")
    if not sess.touches(robot_id):
        raise HTTPException(status_code=403,
                            detail="%s 不在当前控制作用域（%s）内" % (robot_id, sess.scope.get("label") or ""))
    fleet_sessions.touch(robot_id)
    r = fleet.move(robot_id, body.joints, body.speed_pct,
                   actor=_actor(tok), scope=str(sess.scope.get("kind") or ""))
    if not r.get("ok"):
        raise HTTPException(status_code=int(r.get("code") or 400), detail=r.get("reason") or "下发被拒")
    return r


@router.get("/fleet/config")
def fleet_config_get() -> Dict[str, Any]:
    """机房登记表的**生效值**与被改过的项（公开读，与 /api/settings 同口径）。

    读接口不锁是有理由的：现场排查时"这台到底写的哪个 IP、是不是被界面改过"
    必须随时能看到，把读也锁上只会让人去翻 fleet.yaml 原文，反而更容易改错。
    """
    rows, meta = fset.describe()
    base = fset.base_registry()
    return {
        "ok": True,
        "robots": rows,
        "models": meta["models"],
        "layout": meta["layout"],
        "layout_overridden": meta["layout_overridden"],
        "overlay_path": meta["overlay_path"],
        "overlay_keys": meta["overlay_keys"],
        "zones": meta["zones"],
        "groups": meta["groups"],
        "name_max": fset.NAME_MAX,
        # 阵列拓扑不可在界面改（几排几台由 yaml 定），界面要显示成只读并说明原因
        "group_size": int(((base.get("layout") or {}).get("group_size")) or 0) or None,
        "readonly_note": "id / 每排台数 / 居中方式 不可在界面修改；改 IP、端口、名称、型号、区排号可以。",
        "collector": {"running": bool(fleet.stations), "simulate": bool(fleet.cfg.simulate)},
    }


class RobotPatch(BaseModel):
    """一台机器人的登记表改动。**没有** id 以外的身份字段可换：id 是全后端主键。

    ★ `extra="forbid"`：传上来一个不认识的键（比如想写 `driving`）必须**当场报错**，
      而不是被 pydantic 悄悄丢掉 —— "点了保存、提示成功、其实什么都没改"是这里
      最坏的一种界面，它比一次 400 难发现得多。
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., max_length=16)
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    model: Optional[str] = None
    zone: Optional[str] = None
    group: Optional[str] = None
    slot: Optional[int] = None
    order: Optional[int] = None


class LayoutPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_pitch_m: Optional[float] = None
    group_gap_m: Optional[float] = None
    zone_gap_m: Optional[float] = None
    yaw_deg: Optional[float] = None


class FleetConfigIn(BaseModel):
    robots: List[RobotPatch] = Field(default_factory=list)
    layout: Optional[LayoutPatch] = None


@router.put("/fleet/config")
def fleet_config_put(body: FleetConfigIn, tok: str = Depends(require_admin)) -> Dict[str, Any]:
    """保存机房登记表改动 → 落覆盖层 → **整片重建采集**。

    ★ 为什么要管理员：改 IP 等于改"我在读谁的数据"。操作员手滑把 A1-1 指向隔壁
      产线的控制器，屏幕上照样出一串合理的关节角 —— 这种错不会报错，只会让人
      拿着别人的姿态做判断。与单机设置里 connection.host 的档位保持一致。

    ★ 顺序是"先验后写"：跨条目冲突（两台踩同一位置、编号重复、slot 越界）由
      FleetConfig 在内存里判，不通就 400 返回，覆盖层一个字都不写。
      存了半张错地图再让采集线程去连，比保存失败糟糕得多。
    """
    base = fset.base_registry()
    updates = [r.model_dump(exclude_unset=True) for r in body.robots]
    layout = body.layout.model_dump(exclude_unset=True) if body.layout else None
    errs = fset.check_updates(base, updates, layout)
    if errs:
        raise HTTPException(status_code=400, detail="；".join(errs[:8]))
    overlay = fset.build_overlay(base, updates, layout, keep=fset.load_overlay())
    try:
        FleetConfig(fset.merge_raw(base, overlay))
    except FleetConfigError as e:
        raise HTTPException(status_code=400, detail="配置冲突：%s" % e)

    path = fset.save_overlay(overlay)
    # 登记表一变，作用域成员名单可能就是另一批机器了。让持有人重新声明，
    # 而不是让他继续握着"按旧名单算出来的那几台"的控制权。
    held = fleet_sessions.current()
    released = ""
    if held is not None:
        # 空 actor = 服务端内部无条件收回。这里必须传空串：传"变更原因"会被当成
        # 另一个持有者，release 直接判不匹配返回 released=False，控制权没收回。
        fleet_sessions.release("")
        released = held.actor or "unknown"
    restarted = fleet.reload()
    changed = sorted({str(u.get("id")) for u in updates if u.get("id")})
    emit("config", "warn", "fleet.config",
         "机房登记表已保存，采集重建：%d 台%s"
         % (restarted.get("stations") or 0, "" if restarted.get("started") else "（未启动）"),
         detail={"changed": changed, "layout": layout or {}, "overlay": path,
                 "collector": restarted, "scope_released": released},
         actor=_actor(tok))
    rows, meta = fset.describe()
    return {"ok": True, "robots": rows, "layout": meta["layout"],
            "overlay_keys": meta["overlay_keys"], "collector": restarted,
            "scope_released": bool(released),
            "message": ("已保存并重建采集（%d 台）" % restarted["stations"]
                        if restarted.get("started")
                        else "已保存，但机房采集没有在跑（enabled/总闸未开）")}


@router.post("/fleet/config/reset")
def fleet_config_reset(tok: str = Depends(require_admin)) -> Dict[str, Any]:
    """恢复登记默认：删掉覆盖层，一切回到 config/fleet.yaml。

    写成"删文件"而不是"逐字段写回默认值" —— 覆盖层只存差异，删掉才等于清零；
    逐字段写回会让覆盖层变稠，以后再也看不出"现场到底动过哪几处"。
    """
    deleted = fset.reset_overlay()
    restarted = fleet.reload()
    rows, meta = fset.describe()
    emit("config", "warn", "fleet.config.reset",
         "机房登记表已恢复默认（覆盖层%s）" % ("已删除" if deleted else "本来就是空的"),
         detail={"collector": restarted}, actor=_actor(tok))
    return {"ok": True, "deleted": deleted, "robots": rows, "layout": meta["layout"],
            "overlay_keys": meta["overlay_keys"], "collector": restarted,
            "message": "已恢复 config/fleet.yaml 的登记值" if deleted else "没有需要恢复的改动"}


@router.get("/fleet/{robot_id}")
def fleet_robot(robot_id: str) -> Dict[str, Any]:
    snap = fleet.one(robot_id)
    if snap is None:
        for r in _registry_only()["robots"]:
            if r["id"] == robot_id:
                return r
        raise HTTPException(status_code=404, detail="没有这台机器人: %s" % robot_id)
    return snap
