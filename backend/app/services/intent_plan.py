# -*- coding: utf-8 -*-
"""★★ E1 意图编译器 · 第 2 层：意图 AST → **逐段可行性预检**（展示卡的数据）。

方案 §E1 + §10.2 **收缩版**（逐字）：
  「**只做「意图 → 段列表 + 每段闸门结论」的展示卡（先不接执行，仍走现有入口）**」

这份卡回答的**只有三问**（操作员按下执行之前就能知道）：
  1. 这句话会**去到哪些点**（含 `raw_slot_safe_path` 现算出来的**自动补段**）；
  2. 每段走位时**闸门会怎么说**（未就绪 / 围栏互锁 / 点位未示教 / 面解算被拒 / 包络扫掠不安全）；
  3. 每段的**保守间隙包络**（`safety_eval.sweep`，A2 的既有实现）。

★ **刻意不做**（写清楚，免得后来者以为还能加）：
  · **不接执行**：本模块**不 import** motion / modbus，也不返回任何可下发的东西 ——
    执行仍走 `api/ai.py::/exec`（那里已有统一未就绪闸门 + 围栏互锁 + 轴锁 + 速度夹取）；
    钉 `tests/test_intent_plan.py::test_modules_never_import_write_path`。
  · **不给耗时 / ETA**：PC 只"写目标 + 触发位"，运动由控制器常驻程序 210 执行，
    上位机拿不到时间轴 —— 任何 ETA 都是编的（AGENTS.md §4 关键机理 1）。
  · **不决定速度 / 气路位号 / 面**：三样都有唯一口径（`ui_speed` / `VAC_ACTIONS` /
    `effective_face`），本卡只**只读派生**并在文案里注明（AST 层根本没有这些字段）。
  · **不重排业务次序**：吸/放/判色/送检的先后由流程编排决定；本卡只按"这句话会去哪些点"
    做**逐点预检**，次序是**访问序列**不是**工艺时序**。

★ **三态不塌成布尔**（§5.5 铁律③）：`ready` / `guard` 传 `None` = **没校验过**，
  与"校验过且通过"是两个结论，卡面必须分开显示（后者才是绿）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.logger import get_logger
from app.services import intent_ast as IA
from app.services import safety_eval as SE
from app.services import sorting as S

log = get_logger("intent_plan")

SWEEP_SAMPLES = 16          # 段间扫掠采样数（预检用的保守包络，够快即可，不做轨迹跟踪）

VERDICT_OK, VERDICT_WARN, VERDICT_BLOCKED = "ok", "warn", "blocked"
_VERDICT_RANK = {VERDICT_OK: 0, VERDICT_WARN: 1, VERDICT_BLOCKED: 2}
VERDICT_LABEL = {VERDICT_OK: "可走", VERDICT_WARN: "可走但有隐患",
                 VERDICT_BLOCKED: "执行时会被拒"}

FACE_LABEL = {S.FACE_KEEP: "不翻面（保持当前面）", S.FACE_AUTO: "不翻面（默认 auto）",
              S.FACE_FRONT: "正面", S.FACE_BACK: "反面"}

NOTE_CARD = ("意图编译器（E1）：本卡只回答「这句话会去哪几段、每段闸门怎么说」，"
             "**不下发任何动作**；执行仍走现有入口（AI 页「执行」→ 统一未就绪闸门 + 围栏互锁）。")
NOTE_ORDER = ("自动补段由唯一实现 `services/sorting.py::raw_slot_safe_path` **现算**"
              "（原位→正上位→目标正上位→目标）；本卡不重排工艺时序、不代替流程编排。")
NOTE_AIR = ("气路与速度**不在本卡**：位号唯一来源 `services/modbus.py::VAC_ACTIONS`、"
            "速度唯一口径 = 界面右上角滑块（`services/ui_speed.py`）；"
            "AST 里没有这些字段（`intent_ast.FORBIDDEN_KEYS`）。")
NOTE_TIME = "不给耗时/ETA：PC 只写目标 + 触发位，运动由控制器程序 210 执行，拿不到时间轴。"
NOTE_DRY = ("闸门结论按**真实下发**口径判（与 `_move` 真机分支一致）；"
            "**空跑**（界面那颗开关）不下发任何指令，因此不会被这些段拒绝 —— "
            "但段序、面与间隙预检**照样成立**。")
NOTE_SWEEP = "间隙 = 保守包络（胶囊近似）扫掠的最小余量，不是控制器真实轨迹。"


def _worst(a: str, b: str) -> str:
    """两个结论取**更严重**的那个（fail-safe；未收录的值按最严重处理）。"""
    return (b if _VERDICT_RANK.get(b, 9) > _VERDICT_RANK.get(a, 9) else a)


def _gate_verdict(reasons: List[Tuple[str, str]]) -> Dict[str, Any]:
    """[(结论, 原因)] → 单一结论（取最严重）+ 全部原因（按严重度排序，别只报第一条）。"""
    if not reasons:
        return {"verdict": VERDICT_OK, "reason": "", "reasons": []}
    reasons = sorted(reasons, key=lambda x: -_VERDICT_RANK.get(x[0], 9))
    return {"verdict": reasons[0][0],
            "reason": "；".join(r for _v, r in reasons if r),
            "reasons": [{"verdict": v, "reason": r} for v, r in reasons]}


def _clearance_of(q0: Optional[Sequence[float]], q1: Optional[Sequence[float]],
                  samples: int) -> Dict[str, Any]:
    """两段姿态之间的保守包络扫掠；拿不到姿态 → `state="unknown"`（不塌成 safe）。"""
    if not q0 or not q1 or len(list(q0)) != 6 or len(list(q1)) != 6:
        return {"state": "unknown", "mm": None, "zone": "", "samples": 0,
                "reason": "拿不到起止姿态（未采集/未示教），**未做几何预检**"}
    try:
        sw = SE.sweep([float(v) for v in q0], [float(v) for v in q1], n=samples)
    except Exception as e:      # noqa: BLE001 —— 预检失败不得打挂接口，但要如实写出来
        log.debug("意图预检扫掠失败（已如实降级）: %s", e)
        return {"state": "unknown", "mm": None, "zone": "", "samples": 0,
                "reason": "几何预检未能完成（%s）" % str(e)[:80]}
    return {"state": sw.get("worst_state") or "unknown",
            "mm": sw.get("worst_mm"), "zone": sw.get("worst_zone") or "",
            "samples": int(sw.get("samples") or 0), "ok": bool(sw.get("ok")),
            "reason": "" if sw.get("ok") else
            ("包络扫掠最差点「%s」（余量 %smm）" % (sw.get("worst_state"), sw.get("worst_mm")))}


def compile_plan(ast: Dict[str, Any], live: Optional[Sequence[float]] = None,
                 poses: Optional[Dict[str, Sequence[float]]] = None,
                 ready: Optional[Dict[str, Any]] = None,
                 guard: Optional[Dict[str, Any]] = None,
                 samples: int = SWEEP_SAMPLES,
                 poses_error: str = "") -> Dict[str, Any]:
    """AST → **段列表 + 每段闸门结论**（唯一实现；只读、零下发）。

    参数
      · `live`  = 当前实时 6 关节（度）；None → 无法判"在哪一格"→ 不补段、也如实标注未预检；
      · `poses` = `{点名: 6 值}`（`sorting.all_point_poses()`）；None → 无法校验点名；
      · `poses_error` 非空 = **读库失败**（与"库里没这个点"**分开写**，不冒充结论）；
      · `ready` = **None**（没校验）或 `{"ok": bool, "reason": str}`（未就绪闸门结论）；
      · `guard` = **None**（没校验）或 `{"ok": bool, "reason": str}`（围栏互锁结论）。

    返回 `{"ok","verdict","verdict_label","segments","blocked_at","notes","warnings",
          "checks","samples","action"}`；任一段 `blocked` → 整体标红并指出**第几段**
    （方案原话：「任一段被闸门判为不可行 → 计划整体标红并指出是哪一段，
      而不是跑到那儿再失败」）。
    """
    pf = IA.points_of(ast or {}, S.CAMERA_POINT, S.START_POINT)
    steps = list((ast or {}).get("steps") or pf["steps"])
    notes: List[str] = [NOTE_CARD, NOTE_ORDER, NOTE_AIR, NOTE_TIME, NOTE_DRY]
    warnings: List[str] = list((ast or {}).get("warnings") or [])
    for u in ((ast or {}).get("unknown") or []):
        warnings.append("预检未覆盖：%s" % u)
    for n in ((ast or {}).get("path_notes") or pf["notes"]):
        notes.append(n)
    if (ast or {}).get("ok") is False and (ast or {}).get("error"):
        notes.append("意图不完整：%s —— 本卡只列**已能确定**的段，缺口不补默认值"
                     % ast["error"])

    segs: List[Dict[str, Any]] = []
    cur: Optional[List[float]] = ([float(v) for v in live][:6]
                                  if live and len(list(live)) == 6 else None)
    # ★ **拿不到实时姿态就一路都不预检**：不许拿"点位名义姿态"当起点冒充"机器人在这儿"
    #   （那会把一个未知的起点算成安全的间隙 —— 正是铁律③要防的"未知塌成结论"）。
    has_live = cur is not None
    poses = poses or {}
    # ★ 后端独立评估**实际是否参与拦截**：影子模式只记录 → 预检不得谎称"执行时会被拒"
    mode, mode_reason = SE.effective_mode()
    enforces = mode == "enforce"
    if mode != "enforce" and mode_reason:
        notes.append("几何预检与真实拦截的关系：%s" % mode_reason)

    for name, purpose in steps:
        path: Optional[List[str]] = None
        if cur and poses:
            try:
                path, _reason = S.raw_slot_safe_path(cur, poses, name)
            except Exception as e:   # noqa: BLE001 —— 读库/判定异常：退回单段，**不假装安全**
                log.debug("意图预检补段失败（退回单段）: %s", e)
                path = None
        if path is None:
            path = [name]
        if not path:
            # 已经停在目标原料位（raw_slot_safe_path 的「无需移动」）→ 如实记一条，不算段
            notes.append("「%s」%s" % (name, purpose) + "：机器人**已经在这一格**（幂等，不动）")
            continue
        for k, p in enumerate(path):
            is_last = (k == len(path) - 1)
            reasons: List[Tuple[str, str]] = []
            taught = poses.get(p)
            target: Optional[List[float]] = None
            face = ""
            if taught is None:
                if poses_error:
                    # ★ 读不到点位库 ≠ "点位没示教" —— 不能拿它冒充结论（铁律③）
                    reasons.append((VERDICT_WARN,
                                    "读不到点位库（%s）→ 本卡无法校验点名" % poses_error))
                else:
                    reasons.append((VERDICT_BLOCKED,
                                    "点位「%s」尚未示教（到「点位」页补齐）" % p))
            else:
                # ★ 面：本卡**不给旋转授权**（rotate=None → 上下文未授权 → 一律 keep），
                #   与 AI 单件链路的实际执行**同一口径**（那条链路也没有 allow_rotate）。
                face = S.effective_face(p, S.FACE_AUTO)
                try:
                    target, _j6_only, err = S.resolve_target_joints(
                        p, list(taught), base_provider=lambda: cur, face=face)
                except Exception as e:   # noqa: BLE001
                    target, err = None, "面解算异常：%s" % str(e)[:80]
                else:
                    err = err or ""
                if err:
                    reasons.append((VERDICT_BLOCKED, err))
            clr = _clearance_of(cur, target, samples)
            if clr["state"] == "unknown":
                reasons.append((VERDICT_WARN, clr["reason"] or "未做几何预检"))
            elif clr["state"] in SE.UNSAFE:
                # ★ 结论必须对上**真实闸门**：影子模式（server_guard≠enforce）只是"记录"，
                #   并不会拦下这次下发 —— 这时把这段标成"执行时会被拒"就是**假报警**。
                if enforces:
                    reasons.append((VERDICT_BLOCKED,
                                    "包络扫掠最差点 %s（余量 %smm，区域 %s）→ 后端独立评估会拦"
                                    % (clr["state"], clr["mm"], clr["zone"] or "未命名")))
                else:
                    reasons.append((VERDICT_WARN,
                                    "包络扫掠最差点 %s（余量 %smm，区域 %s）；当前后端评估为"
                                    "**影子模式**（只记录、不拦），拦不拦由围栏互锁结论决定"
                                    % (clr["state"], clr["mm"], clr["zone"] or "未命名")))
            elif clr["state"] == "warn":
                reasons.append((VERDICT_WARN,
                                "包络扫掠进入警戒（余量 %smm，区域 %s）"
                                % (clr["mm"], clr["zone"] or "未命名")))
            if ready is None:
                reasons.append((VERDICT_WARN, "就绪状态未校验（本卡没有读控制器）"))
            elif not ready.get("ok"):
                reasons.append((VERDICT_BLOCKED, ready.get("reason") or "机器人没有就绪"))
            if guard is None:
                pass     # 围栏互锁没校验过 —— **不编成绿**，但也不谎报被拒：由 checks 如实标注
            elif not guard.get("ok"):
                reasons.append((VERDICT_BLOCKED,
                                "围栏互锁：%s" % (guard.get("reason") or "当前不允许下发")))
            segs.append({"i": len(segs) + 1, "point": p,
                         "purpose": purpose if is_last else "自动补段（原位安全路径）",
                         "auto_preface": not is_last,
                         "face": face, "face_label": FACE_LABEL.get(face, face or "—"),
                         "taught": taught is not None,
                         "target": ([round(float(v), 2) for v in target] if target else None),
                         "clearance": clr,
                         "gate": _gate_verdict(reasons)})
            nxt = target or ([float(v) for v in taught][:6] if taught else None)
            if nxt and has_live:
                cur = nxt

    verdict = VERDICT_OK
    for s in segs:
        verdict = _worst(verdict, s["gate"]["verdict"])
    if not steps:
        verdict = VERDICT_OK
    blocked = next((s for s in segs if s["gate"]["verdict"] == VERDICT_BLOCKED), None)
    if ready is None or guard is None:
        # "有一道闸门根本没校验过" ⇒ 最多只能给到"有隐患"，**绝不给整卡盖绿章**
        if verdict == VERDICT_OK:
            verdict = VERDICT_WARN
        missing = [n for n, v in (("就绪闸门", ready), ("围栏互锁", guard)) if v is None]
        warnings.append("这些闸门本卡**没有校验**（%s）→ 整卡不给「可以执行」的绿章"
                        % "、".join(missing))
    return {"ok": verdict != VERDICT_BLOCKED,
            "verdict": verdict, "verdict_label": VERDICT_LABEL.get(verdict, verdict),
            "action": str((ast or {}).get("action") or ""),
            "pieces": (ast or {}).get("pieces"),
            "segments": segs, "blocked_at": blocked["i"] if blocked else None,
            "notes": notes, "warnings": warnings,
            "samples": samples,
            "checks": {"ready": ("未校验" if ready is None else bool(ready.get("ok"))),
                       "guard": ("未校验" if guard is None else bool(guard.get("ok"))),
                       "live": bool(live), "poses": len(poses),
                       "sweep_note": NOTE_SWEEP}}
