# -*- coding: utf-8 -*-
"""机房 12 台机器人的登记配置（config/fleet.yaml）加载与校验。

为什么单独一个模块，而不是塞进 core/config.py：
  robot.yaml 是**单机**的机型/寄存器/运动学参数，被采集、点动、安全、运动学
  全链路长期持有引用；fleet.yaml 只回答"这个机房里有谁、在哪、走哪条协议"。
  两者生命周期不同，混在一起会让"换一台机器人的 IP"变成一次全局配置改动。

★ 启动闸门（与 §8.2 真机双闸同构，但这里管的是"要不要开 12 条采集线程"）：
    fleet.yaml: fleet.enabled = false  → 默认不启动（测试/CI/单机调试不受打扰）
    环境变量 EFORT_FLEET=1             → 现场部署打开总闸（优先级高于 yaml）
    环境变量 EFORT_FLEET=0             → 强制关（yaml 写了 true 也不启动）

★ 校验是硬的：坐标重叠 / 编号缺失 / id 重复 / 围栏间距小于安全距离时，
  本模块直接抛 FleetConfigError，服务不带着错误的机房地图上线 ——
  因为画面上一台机器人"和邻居重叠"会被误读成"它越界了"。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import yaml

from app.core import fleet_settings
from app.core.config import project_root

FLEET_PATH = os.path.join(project_root(), "config", "fleet.yaml")

# 型号档案里华沿那台的关节链（官方 URDF 抄录，见 vendor/elfin_model/VENDOR.md）
MODEL_DIR = os.path.join(project_root(), "config", "models")


class FleetConfigError(ValueError):
    """fleet.yaml 不自洽 —— 宁可启动失败，也不要画出一张错的机房地图。"""


def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f) or {}


def load_model_config(rel_path: str) -> Dict[str, Any]:
    """按 fleet.yaml 里的 config_file 读机型档案（相对项目根）。"""
    p = rel_path if os.path.isabs(rel_path) else os.path.join(project_root(), rel_path)
    if not os.path.isfile(p):
        raise FleetConfigError("机型档案不存在: %s" % p)
    return _load_yaml(p)


def derive_positions(layout: Dict[str, Any],
                     robots: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """把 (zone, group, slot) 摊成地面坐标（米）。

    排布口径（来自用户的平面图与两句话）：
      · 同组 3 台**贴着**放 → 中心距 = 单台安全围栏边长 cell_pitch_m；
      · 上/下两组之间留 group_gap_m 净空（「竖着的中间有 1 米间隔」）；
      · A 区在 -x、B 区在 +x，两区围栏之间留 zone_gap_m；
      · slot 1 靠下（y 小的一侧）——「从下面 1 开始」。
    """
    pitch = float(layout.get("cell_pitch_m", 1.64))
    group_gap = float(layout.get("group_gap_m", 1.0))
    zone_gap = float(layout.get("zone_gap_m", 1.0))
    size = int(layout.get("group_size", 3))
    if pitch <= 0 or size < 1:
        raise FleetConfigError("layout.cell_pitch_m / group_size 不合法")

    lower = [i * pitch for i in range(size)]
    upper_start = lower[-1] + pitch / 2.0 + group_gap + pitch / 2.0
    upper = [upper_start + i * pitch for i in range(size)]
    rows = {"lower": lower, "upper": upper}

    if bool(layout.get("center_array", True)):
        mid = (upper[-1] + lower[0]) / 2.0
    else:
        mid = 0.0
    zone_x = {"A": -(pitch / 2.0 + zone_gap / 2.0), "B": (pitch / 2.0 + zone_gap / 2.0)}

    out: Dict[str, Dict[str, float]] = {}
    for r in robots:
        z, g, s = str(r["zone"]), str(r["group"]), int(r["slot"])
        if z not in zone_x:
            raise FleetConfigError("%s: zone 必须是 A 或 B（当前 %r）" % (r["id"], z))
        if g not in rows:
            raise FleetConfigError("%s: group 必须是 lower/upper（当前 %r）" % (r["id"], g))
        if not 1 <= s <= size:
            raise FleetConfigError("%s: slot 必须在 1..%d" % (r["id"], size))
        out[r["id"]] = {"x": zone_x[z], "y": rows[g][s - 1] - mid,
                        "yaw": float(layout.get("yaw_deg", 0.0))}
    return out


class FleetConfig:
    def __init__(self, raw: Optional[Dict[str, Any]], path: str = FLEET_PATH):
        self.path = path
        self.raw = raw or {}
        self._robots: List[Dict[str, Any]] = []
        self._validate()

    # ---- 属性 ----
    @property
    def enabled(self) -> bool:
        env = os.environ.get("EFORT_FLEET")
        if env is not None:
            return env.strip() not in ("0", "false", "off", "")
        return bool(self.raw.get("fleet", {}).get("enabled", False))

    @property
    def simulate(self) -> bool:
        """机房仿真总闸：把 12 条采集链路全部换成合成帧驱动。

        环境变量 EFORT_FLEET_SIM 优先于 yaml（现场临时看一眼效果，不必改配置）。
        ★ 它只换"数据源"，不碰任何写通道：driving / write_allowed 那两道校验
          照样是 False，开仿真不等于可以下发。
        """
        env = os.environ.get("EFORT_FLEET_SIM")
        if env is not None:
            return env.strip() not in ("0", "false", "off", "")
        return bool(self.raw.get("fleet", {}).get("simulate", False))

    @property
    def robots(self) -> List[Dict[str, Any]]:
        return self._robots

    @property
    def streaming(self) -> Dict[str, Any]:
        return self.raw.get("streaming", {}) or {}

    @property
    def drivers(self) -> Dict[str, Any]:
        return self.raw.get("drivers", {}) or {}

    @property
    def models(self) -> Dict[str, Any]:
        return self.raw.get("models", {}) or {}

    @property
    def layout(self) -> Dict[str, Any]:
        return self.raw.get("layout", {}) or {}

    def driver_params(self, driver_name: str) -> Dict[str, Any]:
        return dict(self.drivers.get(driver_name, {}) or {})

    def model_profile(self, model_key: str) -> Dict[str, Any]:
        m = self.models.get(model_key) or {}
        rel = m.get("config_file")
        prof = dict(m)
        if rel:
            prof["model_data"] = load_model_config(rel)
        return prof

    # ---- 校验 ----
    def _validate(self) -> None:
        robots = self.raw.get("robots") or []
        if not isinstance(robots, list):
            raise FleetConfigError("robots 必须是列表")
        ids = [r.get("id") for r in robots]
        if len(set(ids)) != len(ids):
            raise FleetConfigError("robot id 重复: %s" % ids)
        orders = [r.get("order") for r in robots]
        if len(set(orders)) != len(orders):
            raise FleetConfigError("order 编号重复: %s" % orders)

        pos = derive_positions(self.layout, robots) if robots else {}
        seen: Dict[Any, str] = {}
        out: List[Dict[str, Any]] = []
        for raw_r in robots:
            # ★ 派生字段写在**副本**上，不回写 self.raw：
            #   曾经直接改写原 dict，于是 get_fleet().raw 里被烙进了第一次算出的
            #   port/pos/brand —— 拿这份 raw 再校验一遍（测试与设置页都这么干）就会
            #   把旧端口当成"单台显式值"，驱动段的改动再也不生效。
            r = dict(raw_r)
            for k in ("id", "name", "zone", "group", "slot", "model", "host"):
                if not r.get(k):
                    raise FleetConfigError("%s 缺字段 %s" % (r.get("id"), k))
            if r.get("driving") or r.get("write_allowed"):
                raise FleetConfigError(
                    "%s: 阶段一只读，driving/write_allowed 必须为 false" % r["id"])
            drv = (self.models.get(r["model"]) or {}).get("driver")
            if not drv:
                raise FleetConfigError("型号 %s 没有 driver 字段" % r["model"])
            dp = self.driver_params(drv)
            r["brand"] = (self.models.get(r["model"]) or {}).get("brand") or ""
            r["driver"] = drv
            # 端口默认跟着驱动走，不在 12 行里各写一遍（写错一个比改一次危险得多）。
            # 但机房设置页可以给**单台**填端口 —— 现场确实存在"同一型号换过端口映射"
            # 的情况，所以这里让单台的显式值优先，缺省才回落到驱动段。
            r["port"] = int(r.get("port") or dp.get("port")
                            or (502 if drv == "efort_modbus" else 10004))
            key = (round(pos[r["id"]]["x"], 4), round(pos[r["id"]]["y"], 4))
            if key in seen:
                raise FleetConfigError("%s 与 %s 踩在同一个位置 %s" % (r["id"], seen[key], key))
            seen[key] = r["id"]
            r["pos"] = pos[r["id"]]
            out.append(r)

        self._check_pitch()
        self._robots = out

    def _check_pitch(self) -> None:
        """围栏间距若小于"两臂半径之和"，画面里两台机器人会互相够得到 ——
        这是 interlock.enabled=false 的唯一前提，必须自己守住。"""
        il = self.raw.get("interlock") or {}
        if il.get("enabled"):
            return
        need = float(il.get("min_safe_pitch_m", 0.0))
        if need:
            pitch = float(self.layout.get("cell_pitch_m", 0.0))
            if pitch < need:
                raise FleetConfigError(
                    "cell_pitch_m=%.3f < 安全间距 %.3f，但 interlock.enabled=false —— "
                    "要么改大间距，要么开启跨机互锁" % (pitch, need))


_fleet: Optional[FleetConfig] = None


def get_fleet(reload: bool = False) -> FleetConfig:
    """单例读取机房登记表；文件不存在时返回一个 enabled=False 的空登记。

    ★ 生效值 = config/fleet.yaml（人工维护、带注释）⊕ config/fleet_settings.json
      （机房设置页写的稀疏覆盖层）。界面永远不改 yaml 原文，见 core/fleet_settings.py。
    """
    global _fleet
    if _fleet is None or reload:
        _fleet = FleetConfig(fleet_settings.effective_registry())
    return _fleet


def station_count() -> int:
    return len(get_fleet().robots)
