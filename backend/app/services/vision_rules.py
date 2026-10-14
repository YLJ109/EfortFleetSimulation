# -*- coding: utf-8 -*-
"""
视觉分拣规则：颜色 → 程序 → （可选）自动下发。

安全设计（这块写错就是现场事故，所以规则从三条线同时收口）：
  1. 全局开关 `config.vision.auto_execute`（robot.yaml，默认 false）
  2. 单条规则开关 `rule.auto`（默认 false）
  3. 每个步骤下发前逐条过 `safety_guard.check()` —— 围栏 danger/hit 直接中断

  三者必须同时满足才会真的动。任何一条不满足都只记录"命中规则但未执行"，
  并把原因写进 outcome/note —— 现场最怕的是"以为它在动/以为它没动"。

规则存 config/vision_rules.json（原子写；与 safety.json 同一套约定）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import numpy as np

from app.core.config import get_config, project_root
from app.core.logger import get_logger
# ★ 全维度审查：速度常量与点位解析的唯一实现
from app.core.safety_const import clamp_speed
from app.utils.joints import parse_joints
from app.db.crud import get_point, get_program, list_programs
from app.db.database import SessionLocal
from app.services.collector import collector          # ★ 取当前实时姿态（"到点不旋转"要读当前 J6）
from app.services.kinematics import fk_matrix, ikine, rpy_to_matrix, tcp_of
# ★ 审计修复 P1-E1：限位读取与判定的唯一实现在 services/limits.py。
#   原实现的兜底限位是 ±360（其它三处都是 ±180），且 `_in_limits` 按 range(6)
#   硬索引、不查长度也不查 NaN —— 自动执行这条路上的校验曾经是四份里最松的。
from app.services.limits import in_limits, load_limits
from app.services.motion import (motion, real_write_enabled,
                                 servo_not_ready_reason)
from app.services.runmode import runmode
from app.services.safety_guard import check as guard_check
# ★ J6 正/反面的"只用 J6"唯一判定口（`is_j6_face_point` / `compose_j6_only`）。
#   视觉规则自动执行同样是"按点名下发"的一条链路 —— 必须与 /control/move、/sorting/goto 同口径。
from app.services import sorting as S

log = get_logger("vision_rules")

RULES_PATH = os.path.join(project_root(), "config", "vision_rules.json")

_MAX_RULES = 64


def _rules_path() -> str:
    """允许测试用环境变量重定向（避免污染现场配置）。"""
    p = os.environ.get("EFORT_VISION_RULES")
    return p or RULES_PATH


DEFAULT_RULES: Dict[str, Any] = {"version": 1, "rules": []}


def normalize_rules(raw: Optional[dict]) -> Dict[str, Any]:
    """把任意输入归一化成合法规则表（坏数据丢字段，不抛异常）。"""
    raw = raw if isinstance(raw, dict) else {}
    out_rules: List[dict] = []
    for it in (raw.get("rules") or [])[:_MAX_RULES]:
        if not isinstance(it, dict):
            continue
        color = str(it.get("color") or "").strip()[:16]
        if not color:
            continue
        pid = it.get("program_id")
        try:
            pid = int(pid) if pid not in (None, "", "null") else None
        except Exception:
            pid = None
        out_rules.append({
            "enabled": bool(it.get("enabled", True)),
            "color": color,
            "program_id": pid,
            "program_name": str(it.get("program_name") or "")[:64],
            # ★★ 2026-10-11 修复（"参数写死 → 安全缺省失效"这一类）：
            #   原来是 `int(it.get("speed_pct") or 100)`，于是"没设速度"被**落盘成 100%**；
            #   而 `execute_rule` 里的 `clamp_speed(rule["speed_pct"])` 拿到 100 还是 100 ——
            #   那段"缺省改为安全下限"的注释**从未生效**（视觉规则是相机事件**无人值守**触发，
            #   缺省跑满速是最危险的默认值）。现在缺省/非法 → `clamp_speed(None)` = SPEED_MIN(5)，
            #   越界 → 夹到 [5,100]（与全项目唯一速度口径同源）。
            "speed_pct": clamp_speed(it.get("speed_pct")),
            "auto": bool(it.get("auto", False)),
            "note": str(it.get("note") or "")[:120],
        })
    return {"version": 1, "rules": out_rules}


def load_rules() -> Dict[str, Any]:
    try:
        with open(_rules_path(), "r", encoding="utf-8") as f:
            return normalize_rules(json.load(f))
    except Exception:
        return dict(DEFAULT_RULES)


def save_rules(cfg: Optional[dict]) -> Dict[str, Any]:
    """原子写（先写 .tmp 再 replace），避免半个文件把现场配置写坏。"""
    norm = normalize_rules(cfg)
    path = _rules_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(norm, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return norm


def rule_for(color: str, rules: Optional[dict] = None) -> Optional[dict]:
    """按颜色找第一条启用中的规则。"""
    rules = rules if rules is not None else load_rules()
    for r in rules.get("rules") or []:
        if r.get("enabled") and r.get("color") == color:
            return r
    return None


def auto_execute_enabled() -> bool:
    return bool(get_config().vision.get("auto_execute", False))


def _limits() -> List[dict]:
    # ★ 审计修复 P1-E1：统一由 limits.load_limits() 给出（长度恒 6、缺项补默认）。
    #   原实现的兜底是 ±360、并且用 `.get("min", -360)` 给"缺字段"填了个
    #   比另外三处宽一倍的值 —— 同一份配置在自动执行和手动控制下会得出不同限位。
    return load_limits()


def _in_limits(q: List[float], limits: List[dict]) -> bool:
    # ★ 审计修复 P1-E1：判据（长度 → 有限性 → 区间）统一在 limits.in_limits()。
    #   原实现是 `0 <= i < 6` 硬索引：q 不足 6 个直接 IndexError（500），
    #   而 NaN 会一路判成"在限内"，正好是自动执行最不该放行的那种输入。
    return in_limits(q, limits)


def _steps_from_items(items, db) -> List[dict]:
    """把程序条目归一化为可执行步骤（与 api/control._steps_from 同形）。"""
    out: List[dict] = []
    for i, it in enumerate(items or []):
        idx = i + 1
        if not isinstance(it, dict):
            out.append({"index": idx, "ok": False, "error": "步骤格式非法"})
            continue
        sp = int(it.get("speed_pct") or 0) or None
        dw = int(it.get("dwell_ms") or 0)
        pid = it.get("point_id", it.get("point"))
        if pid is not None and str(pid).strip() != "":
            row = get_point(db, int(pid)) if str(pid).isdigit() else None
            if row is None:
                out.append({"index": idx, "ok": False, "error": "点位 #%s 不存在" % pid})
                continue
            try:
                joints = parse_joints(row.joints, where=f"点位「{row.name}」")
            except ValueError as e:
                out.append({"index": idx, "ok": False, "error": str(e)})
                continue
            st = {"index": idx, "name": row.name, "point_id": row.id, "mode": "joint",
                  "joints": joints, "speed_pct": sp, "dwell_ms": dw}
            if row.kind == "cartesian":
                try:
                    t = json.loads(row.tcp or "null")
                except Exception:
                    t = None
                if isinstance(t, dict):
                    st["mode"] = "cartesian"
                    st["tcp"] = t
            out.append(st)
            continue
        if isinstance(it.get("tcp"), dict):
            out.append({"index": idx, "name": it.get("name") or "步骤%d" % idx,
                        "mode": "cartesian", "tcp": it["tcp"],
                        "speed_pct": sp, "dwell_ms": dw})
        elif isinstance(it.get("joints"), list) and len(it["joints"]) == 6:
            out.append({"index": idx, "name": it.get("name") or "步骤%d" % idx,
                        "mode": "joint", "joints": [float(x) for x in it["joints"]],
                        "speed_pct": sp, "dwell_ms": dw})
        else:
            out.append({"index": idx, "ok": False, "error": "步骤缺少 joints / tcp / point_id"})
    return out


def _live_joints_or_none() -> Optional[List[float]]:
    """当前实时 6 轴；拿不到 → None（**不抛**）—— 与 `api/sorting.py` 的同名函数**同口径**。

    ★ 为什么需要：2026-10-11 旋转管控下，"到点不旋转"要求**读得到当前 J6** 才能保证不转；
      读不到 → 该步**拒绝**（`resolve_target_joints` 返回原因），绝不"按点位自身值转一下"。
    """
    snap = collector.get_latest()
    if snap:
        try:
            return [float(snap["j%d" % i]) for i in range(1, 7)]
        except Exception:  # noqa: BLE001 —— 快照字段异常 → 当作拿不到
            pass
    lt = list(getattr(motion, "last_target", None) or [])
    return lt if len(lt) == 6 else None


def execute_rule(rule: dict, *, color: str = "", seq: int = 0,
                 actor: str = "vision") -> Dict[str, Any]:
    """执行一条规则指向的程序。返回 {ok, outcome, note, steps, program_id, program_name}。

    outcome ∈ executed | skipped | blocked | error
    """
    res: Dict[str, Any] = {
        "ok": False, "outcome": "skipped", "note": "", "steps": [],
        "program_id": rule.get("program_id"), "program_name": rule.get("program_name") or "",
    }
    if not rule.get("enabled", True):
        res["note"] = "规则已停用"
        return res
    if not auto_execute_enabled():
        res["note"] = "全局自动执行开关(config.vision.auto_execute)为关，仅记录"
        return res
    if not rule.get("auto"):
        res["note"] = "该规则未勾选自动执行，仅记录"
        return res

    pid = rule.get("program_id")
    if not pid:
        res["outcome"] = "error"
        res["note"] = "规则未绑定程序"
        return res

    db = SessionLocal()
    try:
        prog = get_program(db, int(pid))
        if prog is None:
            res["outcome"] = "error"
            res["note"] = "程序 #%s 不存在" % pid
            return res
        res["program_id"] = prog.id
        res["program_name"] = prog.name
        try:
            items = json.loads(prog.items or "[]")
        except Exception:
            items = []
        steps = _steps_from_items(items, db)
        if not steps:
            res["outcome"] = "error"
            res["note"] = "程序为空"
            return res

        limits = _limits()
        # ★ 全维度审查 B-05：原缺省 100% —— 视觉链路是"相机事件无人值守触发"，
        #   缺省跑满速是极其危险的默认值。改为安全下限。
        speed_pct = clamp_speed(rule.get("speed_pct"))
        # ★★ 2026-10-11：**视觉规则不读也不写"界面滑块"** —— 它是**无人值守**的自动执行，
        #   必须用**规则自己配的**速度；若它也去登记/跟随，就会把操作员拖的 50% 顶成规则里的
        #   20%（现场："快速分拣偶尔变成 20%" 的根因之一）。
        run: List[dict] = []
        ok_all = True
        # ★★ 2026-10-12（用户口径「**原位到起点和摄像头点也是禁止执行的动作，必须在正上位执行**」
        #   「**任何程序也是**」）：无人值守规则**不走 `api/sorting._move`**（自己这条下发链）
        #   → 「原位出发」规则必须在这里**也有一份**（点名步骤 + 显式 6 轴步骤都管）；
        #   点位姿态表**本次执行惰性只读一次**。
        _point_poses_box: Dict[str, object] = {"rows": None, "err": None}

        def _point_poses_once() -> Dict[str, object]:
            if _point_poses_box["rows"] is None and _point_poses_box["err"] is None:
                try:
                    _point_poses_box["rows"] = S.all_point_poses()
                except Exception as _re:               # noqa: BLE001 —— 判不出 → fail-safe
                    _point_poses_box["err"] = str(_re)[:120]
                    _point_poses_box["rows"] = {}
            return _point_poses_box

        for st in steps:
            if st.get("ok") is False:
                run.append(st)
                ok_all = False
                break
            # ★ 每步都过围栏互锁：视觉误判造成的"意外启动"在这里被兜住
            okg, reason, _snap = guard_check()
            if not okg:
                run.append({"index": st["index"], "ok": False, "blocked": True,
                            "reason": reason})
                res["outcome"] = "blocked"
                res["note"] = "围栏互锁拦截：%s" % reason
                ok_all = False
                break

            sp = clamp_speed(st.get("speed_pct") or speed_pct)
            dw = int(st.get("dwell_ms") or 0)
            target = st.get("joints")
            if target is None:
                tcp = st.get("tcp") or {}
                q0 = list(motion.last_target or [0.0] * 6)
                T = np.eye(4)
                T[:3, 3] = [float(tcp.get("x", 0.0)), float(tcp.get("y", 0.0)),
                            float(tcp.get("z", 0.0))]
                pose_given = all(tcp.get(k) is not None for k in ("rx", "ry", "rz"))
                if pose_given:
                    T[:3, :3] = rpy_to_matrix(tcp["rx"], tcp["ry"], tcp["rz"])
                    rw = 150.0
                else:
                    T[:3, :3] = fk_matrix(q0)[:3, :3]
                    rw = 0.0
                r = ikine(T, q0, limits, rot_weight=rw)
                if not r["ok"] or not _in_limits(r["joints"], limits):
                    run.append({"index": st["index"], "ok": False,
                                "error": "IK 解算失败或超限位", "solver": r})
                    ok_all = False
                    break
                target = r["joints"]

            # ★★ 2026-10-11 护栏：规则程序步骤里若点名 `J6正面`/`J6反面` → 同样"原地只改 J6"。
            #   这两个点"只有 J6 不同"、且是**在起点录的**，整姿态下发会把录点当时的 J1~J5
            #   一起带过去（= 跑回起点）；也与 /control/move 按 point_id、/sorting/goto 同口径。
            #   基姿态取 `motion.last_target`（本模块本来就用它作 IK 基姿态）；拿不到 → 拒绝该步。
            #   ★ 决策收敛到唯一入口 `S.resolve_target_joints`（与"残影预演"同源）。
            #   ★★ 统一口径（2026-10-11 用户原话「我在正上位反面，去原料区是变成了正面」）：
            #     规则步骤点名若为**原料位**（A1…A9/B1…B9）→ face 归一成 `keep`（保持当前面、绝不翻面）。
            #   ★★★ 2026-10-11 旋转管控：**无人值守的规则一律不旋转**（`auto` = 保持当前 J6）——
            #     用户口径「J6 如果用户在旋转、双面分拣、AI 快速分拣、游戏过程中可以旋转，
            #     **其他时间一律不允许旋转**」；后台自动规则没有人看着，更没有理由转。
            #   ★ 但**显式 6 轴步骤**（`joints` / IK 解算出来的目标，不来自点位）**不走这套**：
            #     那是程序里**明确写死的目标姿态**，与 `/control/move` 的显式 joints 同一口径 → 原样透传。
            #     ★ 判据 = **是否来自点位**（`point_id` 存在）且 `mode == "joint"`；
            #       笛卡尔点位步的 J6 是 IK 解出来的（覆盖它就到不了那个 TCP 姿态），也原样透传。
            # ★★ 2026-10-12「原位出发」规则见下方（紧接"未就绪"闸门之后）——
            #   **点名步骤 + 显式 6 轴步骤都管**，与 `/control/run-file`、`api/sorting._move` 同一口径。
            if st.get("point_id") is not None and st.get("mode") == "joint":
                target, _j6_only, _j6_reason = S.resolve_target_joints(
                    st.get("name"), target, _live_joints_or_none,
                    face=S.effective_face(st.get("name"), S.FACE_AUTO))
            else:
                target, _j6_only, _j6_reason = S.resolve_target_joints(
                    None, target, None, face=S.FACE_FRONT)
            if _j6_reason:
                run.append({"index": st["index"], "ok": False, "error": _j6_reason})
                ok_all = False
                break

            # ★ 全维度审查 B-05：补档位检查。原链路完全不查档位，
            #   T1/T2 下照样写寄存器（"写成功但不动"），运维难以察觉。
            if real_write_enabled():
                mok, mwhy, _mst = runmode.check_jog()
                if not mok:
                    run.append({"index": st["index"], "ok": False, "blocked": True,
                                "reason": mwhy})
                    res["outcome"] = "blocked"
                    res["note"] = "档位不允许下发：" + mwhy
                    ok_all = False
                    break
            # ★ 统一"未就绪"闸门：这是**无人值守**的视觉规则自动执行，伺服未上电时
            #   更不能下发（否则悄悄写寄存器、机器人不动）。与用户点击入口同一句文案。
            _nready = servo_not_ready_reason()
            if _nready:
                run.append({"index": st["index"], "ok": False, "blocked": True,
                            "reason": _nready})
                res["outcome"] = "blocked"
                res["note"] = _nready
                ok_all = False
                break
            # ★★ 2026-10-12「原位安全路径」**自动补垂直段**（用户口径：「**禁止原位到原位执行**」
            #   「**禁止原位去起点和摄像头点**」「**禁止摄像头点位或者起点直接去原点**」
            #   「**如果原位旋转，先回到正上位，旋转，然后来到原位**」「**任何程序也是**」）：
            #   无人值守规则也**自动补段**（`pre` 在动作前、`post` 在动作后 = 切完回原位）。
            #   ★ 只在"真机会下发"时判（模拟/仿真链路下实时姿态无意义，不误伤）；
            #     拿不到实时姿态 → 不补；读库失败 / 正上位未示教 → **fail-safe 拒绝该步**。
            #   ★ **笛卡尔（IK）步骤不判**（见 AGENTS §7 已知限制）。
            _pre_v, _post_v, _vbad = [], [], None
            if real_write_enabled() and st.get("mode") == "joint":
                _live0 = _live_joints_or_none()
                if _live0:
                    _box0 = _point_poses_once()
                    if _box0.get("err"):
                        _vbad = ("无法确认机器人当前位置与目标（读点位库失败：%s）"
                                 "—— 为安全已拒绝该步" % _box0["err"])
                    else:
                        _frompt = st.get("point_id") is not None
                        _pre_v, _post_v, _vbad = S.raw_slot_step_targets(
                            _live0, _box0.get("rows") or {},
                            target_name=(str(st.get("name") or "") if _frompt else None),
                            target_joints=(None if _frompt else target))
                    if not _vbad:
                        for _nm, _jt in _pre_v:
                            _prv = motion.command(
                                [float(v) for v in _jt], sp, dw,
                                abort_check=lambda: not auto_execute_enabled())
                            if not _prv.get("ok"):
                                _vbad = "原位安全路径「%s」失败：%s" % (_nm, _prv.get("error"))
                                break
            if _vbad:
                run.append({"index": st["index"], "ok": False, "blocked": True, "reason": _vbad})
                res["outcome"] = "blocked"
                res["note"] = _vbad
                ok_all = False
                break
            # ★ 主段若是"抬到目标正上位"之后的**垂直下降** → 主段 J6 也冻结（升降不转）
            _vnm = str(st.get("name") or "") if st.get("point_id") is not None else None
            if _pre_v and _vnm and S.is_vertical_move(_pre_v[-1][0], _vnm):
                try:
                    target = S.compose_j6_only(target, float(_pre_v[-1][1][5]))
                except Exception:                      # noqa: BLE001
                    pass
            # ★ 运行中关闭 auto_execute 开关也能立即停，不必等这一发走完
            rr = motion.command([float(x) for x in target], sp, dw,
                                abort_check=lambda: not auto_execute_enabled(),
                                source=str(st.get("name") or "视觉规则"))
            run.append({"index": st["index"], "name": st.get("name"),
                        "ok": bool(rr.get("ok")), "mode": rr.get("mode"),
                        "target": [round(float(v), 2) for v in target],
                        "error": rr.get("error")})
            if not rr.get("ok"):
                ok_all = False
                break
            # ★ 原位安全路径：`post` 段（只有"原位切面"才有 —— 切完回原位）
            for _nm, _jt in _post_v:
                motion.command([float(v) for v in _jt], sp, dw,
                               abort_check=lambda: not auto_execute_enabled())
            time.sleep(max(0.15, dw / 1000.0 + 0.2))

        res["steps"] = run
        res["ok"] = ok_all
        if res["outcome"] != "blocked":
            res["outcome"] = "executed" if ok_all else "error"
            if not ok_all:
                res["note"] = "执行中断（见 steps 中失败步）"
        return res
    except Exception as e:                     # noqa: BLE001 —— 规则执行绝不能让摄入线程挂掉
        log.warning("规则执行异常: %s", e)
        res["outcome"] = "error"
        res["note"] = "执行异常: %s" % str(e)[:120]
        return res
    finally:
        db.close()


def programs_brief() -> List[dict]:
    """给前端规则面板用：可选程序清单（只读）。"""
    db = SessionLocal()
    try:
        out = []
        for p in list_programs(db):
            try:
                n = len(json.loads(p.items or "[]"))
            except Exception:
                n = 0
            out.append({"id": p.id, "name": p.name, "steps": n})
        return out
    finally:
        db.close()


def tcp_of_last() -> Optional[List[float]]:
    """最近一次目标位姿的 TCP（诊断用）。"""
    try:
        return list(tcp_of(list(motion.last_target or [0.0] * 6)))
    except Exception:
        return None
