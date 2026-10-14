# -*- coding: utf-8 -*-
"""★★ E1 意图编译器 · 第 1 层：把「解析结果」压成**意图 AST**（中间表示，**不是指令**）。

方案：`docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md` §E1，
并按 §10.2 **收缩版**逐字落地：
  「**只做「意图 → 段列表 + 每段闸门结论」的展示卡（先不接执行，仍走现有入口）**」

为什么要有这一层（而不是直接拿 plan 去编译）：
  · 大模型（`services/llm.py`）与规则（`services/nl_intent.py`）产出的形状**并不完全一样**，
    而且"模型多给一个字段就被下游当真话用"是一类反复出现的隐患；
  · 本层用 **字段白名单 + 禁字段清单** 把三件事钉死在指令之外：
      ① **速度** —— 唯一口径仍是界面右上角滑块（`services/ui_speed.py`）；
      ② **气路位号** —— 唯一来源仍是 `services/modbus.py::VAC_ACTIONS`；
      ③ **面语义 / 旋转授权 / 绕闸门开关** —— 唯一实现仍是 `services/sorting.py` 的
         `effective_face` / `face_for_target` / `resolve_target_joints`；
    这些**都不在 AST 里**：这句话"想要什么"只能由符号层从既有唯一判据**派生**，
    **不能由这句话自己带进来**。出现即丢弃 + 在回显里如实说明（钉：
    `tests/test_intent_plan.py::test_llm_never_emits_speed_or_vacuum_bit`）。
  · ★★ **字段缺就是缺**（§5.5 铁律③：未知不得塌成布尔/默认值）—— 缺的记进 `missing`、
    判不出的记进 `unknown`，交给编译器**如实标红**；本层**绝不补默认值**。

★ 本模块**纯逻辑**：不 import motion / modbus / db，不调用任何写通道（钉：
  `tests/test_intent_plan.py::test_modules_never_import_write_path`）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# 动作白名单（与 `services/llm.ALLOWED_ACTIONS` / `nl_intent.INTENTS` 同集合）
ACTIONS = ("look_color", "move_one", "move_all", "move_quota", "sorting",
           "estop", "goto_start", "inventory", "query_state")

# ★ 字段**白名单**：plan 里只有这些键能被 AST 承接
ALLOWED_KEYS: Tuple[str, ...] = (
    "action", "op", "from", "to", "shuffle", "quotas", "pattern", "on_invalid",
    "ban", "dual_face", "placement", "placement_note", "placement_alts",
    "prefer_color")

# ★★ 字段**禁清单**：出现即丢弃并如实登记（这些字段的所有权不在指令手里）
FORBIDDEN_KEYS: Tuple[str, ...] = (
    # 速度（唯一口径 = 右上角滑块 ui_speed）
    "speed", "speed_pct", "speed_override", "vperc", "velocity",
    # 气路位号（唯一来源 = modbus.VAC_ACTIONS）
    "vacuum", "vac", "vac_bit", "bit", "bits", "addr", "address", "register",
    "reg", "trigger",
    # 程序号 / 下发通道
    "program", "program_no", "cmd", "command",
    # 姿态与面（唯一实现 = sorting.resolve_target_joints / effective_face）
    "joints", "joint", "joint_values", "target_joints", "tcp", "pose",
    "face", "j6",
    # 任何"绕过闸门"的开关（统一未就绪闸门 / 围栏互锁不可被指令关掉）
    "allow_rotate", "bypass", "bypass_guard", "ignore_guard", "skip_guard",
    "dry_run", "servo", "enable")

# 需要"落到具体格"才能预检的动作：源 / 目标
_NEEDS_SRC = ("move_one", "look_color", "move_all", "move_quota")
_NEEDS_DST = ("move_one", "move_all", "move_quota")


def _clean(d: Any) -> Dict[str, Any]:
    return dict(d) if isinstance(d, dict) else {}


def _ban_of(plan: Dict[str, Any]) -> Dict[str, List[str]]:
    b = _clean(plan.get("ban"))
    return {"from": [str(x) for x in (b.get("from") or [])],
            "to": [str(x) for x in (b.get("to") or [])],
            "have": [str(x) for x in (b.get("have") or [])]}


def _loc(loc: Any) -> Dict[str, Any]:
    """地点 → {"kind","slot","zh"}；**判不出具体格就返回 slot=""（不猜）**。"""
    x = _clean(loc)
    kind = str(x.get("kind") or "")
    slot = str(x.get("slot") or "").strip().upper()
    if kind == "slot" and slot:
        return {"kind": kind, "slot": slot, "zh": str(x.get("zh") or "")}
    if kind == "color_col":
        return {"kind": kind, "slot": "", "zh": "%s列" % (x.get("color") or "?")}
    if kind == "zone":
        return {"kind": kind, "slot": "", "zh": "%s区" % (x.get("zone") or "?")}
    if kind == "all":
        return {"kind": kind, "slot": "", "zh": "%s区全部" % (x.get("zone") or "A")}
    return {"kind": kind or "unknown", "slot": "", "zh": ""}


def first_placement_slot(placement: Dict[str, Any], color: str = "") -> str:
    """解析阶段解出来的**摆法**（颜色→格位队列）里取第一格（只做预检代表件用）。

    ★ 这里"取第一格"**不代表执行时就这么放** —— 执行时按判色结果逐格消耗队列
      （唯一实现在 `api/ai.py::_piece`）。取它只是为了能算出"这一段走位可不可行"。
    """
    for _c, lst in (placement or {}).items():
        if color and str(_c) != color:
            continue
        for s in (lst or []):
            name = str(s or "").strip().upper()
            if name:
                return name
    return ""


def ast_of(plan: Optional[Dict[str, Any]], source: str = "") -> Dict[str, Any]:
    """解析结果 → **意图 AST**（唯一入口）。

    返回：{"ok","action","op","src","dst","src_zh","dst_zh","pieces","dual_face",
           "pattern","on_invalid","ban","placement","missing","dropped","unknown",
           "error","notes","warnings","source"}
      · `missing`  = 该动作**必须有**却没给的字段（编译器据此拒绝）；
      · `dropped`  = 命中禁清单的字段（**已丢弃**，并在 warnings 里如实说明）；
      · `unknown`  = "这一格要执行时才定"的地点（不猜、不编）。
    """
    p = _clean(plan)
    dropped = sorted([k for k in FORBIDDEN_KEYS if k in p])
    action = str(p.get("action") or "").strip()
    warnings: List[str] = []
    if dropped:
        warnings.append("这句话里带了 %d 个**不由指令决定**的字段，已全部丢弃：%s"
                        "（速度只看右上角滑块；气路位号只认 VAC_ACTIONS；"
                        "面与旋转只由 sorting 派生）" % (len(dropped), "、".join(dropped)))

    ast: Dict[str, Any] = {
        "ok": False, "source": source or "", "action": action,
        "op": str(p.get("op") or ""), "dual_face": bool(p.get("dual_face")),
        "src": "", "dst": "", "src_zh": "", "dst_zh": "",
        "pieces": None, "pattern": str(p.get("pattern") or ""),
        "on_invalid": str(p.get("on_invalid") or ""),
        "ban": _ban_of(p), "placement": _clean(p.get("placement")),
        "missing": [], "dropped": dropped, "unknown": [],
        "error": "", "notes": [], "warnings": warnings,
    }
    if not action:
        ast["error"] = "解析结果里没有动作（无法编译）"
        return ast
    if action not in ACTIONS:
        ast["error"] = "动作「%s」不在白名单里（拒绝编译）" % action
        return ast

    src = _loc(p.get("from"))
    dst = _loc(p.get("to"))
    if action in _NEEDS_SRC:
        ast["src"], ast["src_zh"] = src["slot"], (src["zh"] or src["slot"])
        if not src["slot"]:
            ast["unknown"].append("取料格：%s（要执行时逐件判色/选槽才知道是哪一格）" % src["zh"])
    if action in _NEEDS_DST:
        ast["dst"], ast["dst_zh"] = dst["slot"], (dst["zh"] or dst["slot"])
        if not dst["slot"]:
            # ★ 解析阶段若已解出摆法，就用**第一格**做"代表件"预检（并如实说明不是终定）
            rep = first_placement_slot(ast["placement"])
            if rep:
                ast["dst"] = rep
                ast["unknown"].append("放料格：预检用摆法队列的第一格「%s」作代表件，"
                                      "**执行时按判色逐格消耗**（可能不是这一格）" % rep)
            else:
                ast["unknown"].append("放料格：%s（要执行时判色后才能定）" % dst["zh"])

    quotas = {str(k): int(v) for k, v in _clean(p.get("quotas")).items()
              if str(v).lstrip("-").isdigit()}
    if action == "move_quota":
        ast["pieces"] = sum(max(0, v) for v in quotas.values()) or None
        ast["quotas"] = quotas
    elif action in ("move_one", "look_color"):
        ast["pieces"] = 1
    elif action in ("move_all", "inventory"):
        ast["pieces"] = None
        ast["unknown"].append("件数：整区/逐件盘点，**执行时才数得出来**（本卡只预检一段代表路径）")

    # ---- 必须有却没给的字段 → missing（**不补默认值**）----
    if action == "move_one" and not src["slot"]:
        ast["missing"].append("from")
    if action in ("move_one", "move_all", "move_quota") and not p.get("to"):
        ast["missing"].append("to")
    if action == "move_quota" and not quotas:
        ast["missing"].append("quotas")
    if action == "sorting" and not ast["op"]:
        ast["missing"].append("op")
    if ast["missing"]:
        ast["error"] = "意图缺少必要字段：%s" % "、".join(ast["missing"])
        return ast
    ast["ok"] = True
    return ast


def points_of(ast: Dict[str, Any], camera_point: str,
              start_point: str) -> Dict[str, Any]:
    """AST → **这句话会去到的点名序列**（预检用；**不是**气路/判色的业务次序）。

    · 只列**点名**（取料位、C 检测位、放料位、开始/结束点）；
    · 原料位 ↔ 正上位 的**自动补段**不在这里排 —— 那是 `raw_slot_safe_path` 的职责，
      由编译器现算，避免"第二秩序权威"；
    · "执行时才定的格"留在 `ast.unknown` 里**如实**说明（本卡只预检已知的点）。
    """
    action = str(ast.get("action") or "")
    src, dst = str(ast.get("src") or ""), str(ast.get("dst") or "")
    steps: List[Tuple[str, str]] = []      # (点名, 用途)
    notes: List[str] = []

    def _add(name: str, purpose: str) -> None:
        if name and (not steps or steps[-1][0] != name):
            steps.append((name, purpose))

    if action in ("move_one", "move_all", "move_quota"):
        _add(src, "取料位")
        _add(camera_point, "送检（C 检测位判色）")
        _add(dst, "放料位")
        _add(start_point, "回开始/结束点（放完收气）")
    elif action == "look_color":
        _add(src, "取料位")
        _add(camera_point, "送检（C 检测位判色）")
        _add(src, "看颜色 = 放回原格")
        _add(start_point, "回开始/结束点")
    elif action == "goto_start":
        _add(start_point, "回开始/结束点")
    elif action == "inventory":
        _add(camera_point, "盘点：逐件送 C 检测位判色")
        notes.append("盘点 = 每件一趟「取→判色→原格放回」；本卡只预检 C 检测位那一段，"
                     "逐件路径与搬运同口径（进出原料位必经该格正上位）")
    elif action == "estop":
        notes.append("急停是**刹车**（断使能 + 中止在跑流程），不属于路径计划；"
                     "它永不受本卡影响，也**永不排队**")
    elif action == "sorting":
        notes.append("流程控制（开始/停止/暂停/继续）由分拣流程自己编排段序，"
                     "本卡不重排、不代替；要预检路径请说搬运类指令")
    elif action == "query_state":
        notes.append("只读查询，不动机器人 → 无路径段")
    else:
        notes.append("未知动作「%s」，无路径段" % action)

    return {"steps": steps, "notes": notes}
