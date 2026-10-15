# -*- coding: utf-8 -*-
"""机房登记表的**稀疏覆盖层**：`config/fleet_settings.json`。

## 为什么不从界面回写 config/fleet.yaml

fleet.yaml 的每一段都带着成段手写注释（间距是从用户哪句话来的、端口表抄自协议
哪一节、哪个 host 是占位待核）。`yaml.safe_dump` 回写会把这些注释全抹掉，而这个
项目恰恰靠那些注释判断"这个数字是实测还是猜的"。所以和单机设置
（core/app_settings.py）用同一套办法：

    config/fleet.yaml            （人工维护、带注释、只读）
        ↑ 深合并
    config/fleet_settings.json   （界面写、稀疏、只存与 yaml 不同的项）

- 只存差异：某项改回 yaml 原值时自动从覆盖层消失；删掉整个文件 = 全部恢复登记默认。
- 合并粒度：layout 逐键合并；robots **按 id** 逐字段合并（id 本身不换，见下）。
- 写盘：先写 `.tmp` 再 `os.replace`，原子替换，断电不会留下半个文件。

## ★ 覆盖层改不了的东西（硬剥掉，不是"忘了支持"）

  · `id`     —— 全后端拿它当主键（采集线程名、作用域成员、历史表 robot_id、3D 取姿）。
                 换号等于换一台机器，历史曲线会接到另一台上。
  · `driving` / `write_allowed` —— 阶段一只读闸门。允许界面写这两个字段，
                 等于把"只有改代码才能开写"这条红线降级成"点一下就能开写"。
  · `group_size` / `center_array` —— 改的是阵列拓扑（几排几台），前端布局、
                 作用域分页、编号规则都按 4 组 × 3 台写死；要改拓扑请改 yaml。

## 校验在哪一层

本模块只做**单字段**的格式检查（IP 写法、端口范围、区/排/序号取值、型号是否存在）。
"两台踩在同一个位置""order 重复""slot 越界"这类**跨条目**冲突由
`fleet_config.FleetConfig` 在合并后的整张登记表上判 —— 保存前 API 会先在内存里
建一次 FleetConfig，不通就不落盘（宁可保存失败，也不要让一张错地图上线）。
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import project_root

OVERLAY_PATH = os.path.join(project_root(), "config", "fleet_settings.json")
BASE_PATH = os.path.join(project_root(), "config", "fleet.yaml")

SCHEMA_VERSION = 1

# 每台可改的字段（其余键一律剥掉，见模块头）
ROBOT_FIELDS = ("name", "host", "port", "model", "zone", "group", "slot", "order")
# 阵列可改的字段：只有间距与朝向；拓扑（group_size）不在这里
LAYOUT_FIELDS = ("cell_pitch_m", "group_gap_m", "zone_gap_m", "yaw_deg")

ZONES = ("A", "B")
GROUPS = ("lower", "upper")
NAME_MAX = 40

_IPv4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def base_registry() -> Dict[str, Any]:
    """config/fleet.yaml 原文（**不合并**覆盖层）—— 稀疏化时要拿它当基准。"""
    import yaml

    if not os.path.isfile(BASE_PATH):
        return {}
    with open(BASE_PATH, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f) or {}


def load_overlay() -> Dict[str, Any]:
    """读覆盖层。文件坏了解析不了时返回空表并保留原文件 ——

    这里不能"顺手重写一份干净的"：半死的覆盖层里可能正好记着现场改过的 IP，
    静默覆盖等于把操作员改的东西冲掉。宁可界面退回 yaml 默认值 + 界面报警。
    """
    if not os.path.isfile(OVERLAY_PATH):
        return {}
    try:
        with open(OVERLAY_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def merge_raw(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """把覆盖层并进登记表，返回**新**的 raw（不修改入参）。"""
    raw = copy.deepcopy(base or {})
    ov = overlay or {}
    lay = raw.get("layout")
    if not isinstance(lay, dict):
        lay = {}
        raw["layout"] = lay
    for k in LAYOUT_FIELDS:
        if k in (ov.get("layout") or {}):
            lay[k] = ov["layout"][k]

    robots = raw.get("robots")
    if not isinstance(robots, list):
        robots = []
        raw["robots"] = robots
    ov_r = ov.get("robots") or {}
    known = {str(m) for m in (raw.get("models") or {})}
    for entry in robots:
        if not isinstance(entry, dict):
            continue
        patch = ov_r.get(str(entry.get("id"))) or {}
        for k in ROBOT_FIELDS:
            if k not in patch:
                continue
            v = patch[k]
            # 型号被改没了（yaml 换过键名）时忽略这条覆盖，而不是让整张表起不来
            if k == "model" and known and str(v) not in known:
                continue
            entry[k] = v
    # 覆盖层里指向已注销 id 的残留条目：合并时自然忽略（不影响生效），保存时清掉
    return raw


def effective_registry() -> Dict[str, Any]:
    return merge_raw(base_registry(), load_overlay())


# ---------------------------------------------------------------------------
# 单字段校验
# ---------------------------------------------------------------------------
def _is_ipv4(s: str) -> bool:
    m = _IPv4.match(str(s).strip())
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())


def check_updates(base: Dict[str, Any], updates: List[Dict[str, Any]],
                  layout: Optional[Dict[str, Any]] = None) -> List[str]:
    """逐字段格式检查，返回人类可读的错误列表（空表 = 可保存）。

    错误一次报全，不要修一个报一个：现场是在一张表格里改十几行，
    逐次弹窗会让人怀疑"到底哪个才是真拦我的那条"。
    """
    errs: List[str] = []
    ids = {str(r.get("id")) for r in (base.get("robots") or []) if isinstance(r, dict)}
    models = {str(k) for k in (base.get("models") or {})}
    size = int(((base.get("layout") or {}) or {}).get("group_size") or 0)

    seen_order: Dict[Any, str] = {}
    for u in updates or []:
        rid = str(u.get("id") or "")
        if not rid:
            errs.append("有一条改动没写 id，无法定位是哪台")
            continue
        if rid not in ids:
            errs.append("%s：机房登记表里没有这台（id 不可新增/修改，只能改已登记的）" % rid)
            continue
        for k, v in u.items():
            if k == "id" or v is None:
                continue
            if k not in ROBOT_FIELDS:
                errs.append("%s：%s 不是可改字段" % (rid, k))
            elif k == "name":
                s = str(v).strip()
                if not s:
                    errs.append("%s：名称不能为空" % rid)
                elif len(s) > NAME_MAX:
                    errs.append("%s：名称最长 %d 字（当前 %d）" % (rid, NAME_MAX, len(s)))
            elif k == "host":
                if not _is_ipv4(v):
                    errs.append("%s：IP 写法不对（要点分四段 IPv4，例如 192.168.1.12）" % rid)
            elif k == "port":
                try:
                    p = int(v)
                except (TypeError, ValueError):
                    p = -1
                if not 1 <= p <= 65535:
                    errs.append("%s：端口要在 1~65535" % rid)
            elif k == "model":
                if str(v) not in models:
                    errs.append("%s：型号 %s 不在 models 段里（可用：%s）"
                                % (rid, v, ",".join(sorted(models)) or "无"))
            elif k == "zone":
                if str(v).upper() not in ZONES:
                    errs.append("%s：zone 只能是 A 或 B" % rid)
            elif k == "group":
                if str(v).lower() not in GROUPS:
                    errs.append("%s：group 只能是 lower 或 upper" % rid)
            elif k == "slot":
                try:
                    s = int(v)
                except (TypeError, ValueError):
                    s = -1
                if not 1 <= s <= (size or 99):
                    errs.append("%s：slot 要在 1~%d（每排台数由 layout.group_size 定）"
                                % (rid, size or 0))
            elif k == "order":
                try:
                    o = int(v)
                except (TypeError, ValueError):
                    o = 0
                if o < 1:
                    errs.append("%s：order（全场编号）必须是正整数" % rid)
                elif o in seen_order:
                    errs.append("%s：编号 %d 与 %s 重复" % (rid, o, seen_order[o]))
                else:
                    seen_order[o] = rid

    for k, v in (layout or {}).items():
        if k not in LAYOUT_FIELDS:
            errs.append("layout.%s 不可在界面修改（拓扑/居中请改 fleet.yaml）" % k)
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            errs.append("layout.%s 必须是数字" % k)
            continue
        if k == "yaw_deg":
            if not -360 <= f <= 360:
                errs.append("layout.yaw_deg 要在 -360~360")
        elif not 0 <= f <= 20:
            errs.append("layout.%s 要在 0~20 米之间（当前 %s）" % (k, v))
    return errs


# ---------------------------------------------------------------------------
# 稀疏化 + 落盘
# ---------------------------------------------------------------------------
def build_overlay(base: Dict[str, Any], updates: List[Dict[str, Any]],
                  layout: Optional[Dict[str, Any]] = None,
                  keep: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """在既有覆盖层上打补丁，并只保留**与 yaml 不同**的字段。

    等于原值的项从覆盖层里消失 —— 这样"改回默认"不需要额外按钮，
    而 `overlay_keys` 也永远如实反映"现场到底动过哪几处"。
    """
    ov: Dict[str, Any] = copy.deepcopy(keep or {})
    rob = ov.get("robots")
    if not isinstance(rob, dict):
        rob = {}
        ov["robots"] = rob
    lay = ov.get("layout")
    if not isinstance(lay, dict):
        lay = {}
        ov["layout"] = lay

    by_id = {str(r.get("id")): r for r in (base.get("robots") or []) if isinstance(r, dict)}
    for u in updates or []:
        rid = str(u.get("id") or "")
        cur = by_id.get(rid)
        if not cur:
            continue
        cell = rob.setdefault(rid, {})
        for k in ROBOT_FIELDS:
            if k not in u or u[k] is None:
                continue
            v = u[k]
            if k == "name":
                v = str(v).strip()
            elif k == "host":
                v = str(v).strip()
            elif k in ("port", "slot", "order"):
                v = int(v)
            elif k in ("zone", "model"):
                v = str(v)
            elif k == "group":
                v = str(v).lower()
            if k == "port" and v == _default_port(base, u.get("model") or cur.get("model")):
                # 端口默认来自驱动段：填回默认值 = 没有覆盖
                cell.pop(k, None)
                continue
            if cur.get(k) == v:
                cell.pop(k, None)
            else:
                cell[k] = v
        if not cell:
            rob.pop(rid, None)

    for k, v in (layout or {}).items():
        if k not in LAYOUT_FIELDS or v is None:
            continue
        f = float(v)
        if ((base.get("layout") or {}).get(k)) == f:
            lay.pop(k, None)
        else:
            lay[k] = f

    if not rob:
        ov.pop("robots", None)
    if not lay:
        ov.pop("layout", None)
    ov["schema_version"] = SCHEMA_VERSION
    return ov


def _default_port(base: Dict[str, Any], model_key: Any) -> int:
    """型号 → 驱动段里的默认端口（fleet.yaml 的口径：端口跟驱动走）。"""
    m = (base.get("models") or {}).get(str(model_key or "")) or {}
    drv = str(m.get("driver") or "")
    dp = (base.get("drivers") or {}).get(drv) or {}
    try:
        return int(dp.get("port") or 0)
    except (TypeError, ValueError):
        return 0


def save_overlay(overlay: Dict[str, Any]) -> str:
    """原子落盘；覆盖层为空时直接删文件（"全部恢复登记默认"就是这个意思）。"""
    if not (overlay or {}).get("robots") and not (overlay or {}).get("layout"):
        if os.path.isfile(OVERLAY_PATH):
            os.remove(OVERLAY_PATH)
        return ""
    d = os.path.dirname(OVERLAY_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = OVERLAY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(overlay, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, OVERLAY_PATH)
    return OVERLAY_PATH


def reset_overlay() -> bool:
    """删除覆盖层，返回是否真的删掉了。"""
    if os.path.isfile(OVERLAY_PATH):
        os.remove(OVERLAY_PATH)
        return True
    return False


# ---------------------------------------------------------------------------
# 给界面用的描述（前端不硬编码字段清单）
# ---------------------------------------------------------------------------
def describe(base: Optional[Dict[str, Any]] = None,
             overlay: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """返回 (每台当前生效值 + 哪几项是改过的, 阵列与可选型号)。"""
    base = base or base_registry()
    overlay = overlay if overlay is not None else load_overlay()
    merged = merge_raw(base, overlay)
    models = []
    for key, m in (merged.get("models") or {}).items():
        models.append({"key": str(key), "display_name": str((m or {}).get("display_name") or key),
                       "brand": str((m or {}).get("brand") or ""),
                       "driver": str((m or {}).get("driver") or "")})
    ov_r = overlay.get("robots") or {}
    models_map = merged.get("models") or {}
    rows = []
    for r in merged.get("robots") or []:
        rid = str(r.get("id"))
        patched = ov_r.get(rid) or {}
        prof = models_map.get(r.get("model")) or {}
        rows.append({
            "id": rid,
            "name": r.get("name"),
            "host": r.get("host"),
            # 端口默认来自驱动段（fleet.yaml 的口径），yaml 里通常不逐台写；
            # 界面必须显示**生效值**，否则"看着是空、其实走驱动默认"会被当成没填。
            "port": int(r.get("port") or _default_port(base, r.get("model")) or 0) or None,
            "model": r.get("model"),
            # brand 也是 FleetConfig 派生出来的（型号档案里才有），覆盖层基准里没有它，
            # 所以这里从 models 段取 —— 界面要按牌子给型号下拉框做默认值。
            "brand": prof.get("brand") or "",
            "zone": r.get("zone"),
            "group": r.get("group"),
            "slot": r.get("slot"),
            "order": r.get("order"),
            "pos": r.get("pos"),
            "overridden": sorted(k for k in ROBOT_FIELDS if k in patched),
        })
    lay = merged.get("layout") or {}
    meta = {
        "models": models,
        "layout": {k: lay.get(k) for k in LAYOUT_FIELDS},
        "layout_overridden": sorted(k for k in LAYOUT_FIELDS if k in (overlay.get("layout") or {})),
        "zones": list(ZONES),
        "groups": list(GROUPS),
        "overlay_path": OVERLAY_PATH,
        "overlay_keys": sorted((overlay.get("robots") or {}).keys()),
        "base_path": BASE_PATH,
    }
    return rows, meta
