# -*- coding: utf-8 -*-
"""服务端独立安全评估（方案 A1 / A2）。

为什么要有这个模块
------------------
围栏余量原本**只在浏览器里算**（`three/safety.js`），后端 `safety_guard` 只是把前端
上报的 state 字符串记下来。于是：脚本直连 `/api/control/move`、第三方客户端、
浏览器崩溃/页面停帧这三种情况下，互锁形同虚设。本模块让**后端自己**每拍算一遍。

口径来源（不许各处另立一套）
--------------------------
- 围栏几何与阈值：`core/safety_config.py` + `config/safety.json`（与前端出厂同源）
- 正运动学：`services/kinematics.py`（基座系 mm、Z 向上、DH 标准）
- 坐标换算：与 `frontend/src/three/sceneCoords.js` **同一公式** (x,y,z)→(x, z+lift, −y)，
  钉 `test_scene_coords_mapping_matches_frontend`
- 分级公式：与 `three/safety.js` 的 `classify()/classifyGround()` 逐行对齐
  （`basisOf` 取区域中心到最近边的距离，`ratio<=0→hit / <danger / <warn`）

保守性与诚实边界
--------------
- 连杆包络用**胶囊体（线段 + 半径）**，半径来自 `config/robot.yaml: safety.links`。
  这组数由**官方数模逐顶点真值反解**（`dump_mesh_lowpoints.mjs` + `fit_safety_links.py`），
  并由 `tests/test_safety_envelope.py` 钉住"任何采样姿态下服务端包络都 ≤ 数模真值"
  —— **只许比真实本体更早报警，不许更晚**。
  ★ 前提：数模 = 真机、且**没装末端工具**。装了夹爪/吸盘、或换机型 ⇒ 必须重跑反解，
    在此之前把 `links_calibrated` 改回 false（enforce 会被自动降级为 shadow 并如实说明）。
- 只算"围栏 + 立足面净空"，**不做碰撞检测**（机器人 vs 工件/夹具不在本模块范围）。
- 本模块**只读**：不 import motion、不调 modbus 写。钉 `test_safety_eval_never_writes`。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import get_config
from app.core.safety_config import load_safety
from app.services.kinematics import forward_kinematics

# 与前端 manager.js STATE_RANK 同序
STATE_RANK = {"safe": 0, "warn": 1, "danger": 2, "hit": 3}
UNSAFE = ("danger", "hit")

# 兜底包络（与 config/robot.yaml 的 safety.links **同一套数**，只在配置缺失时用）。
# ★★ 来源不是"估"：用官方 GLB 逐顶点量 503 个姿态的真值包围盒，再反解"每段都能把刚体包住"
#   的最小半径 —— `frontend/tools/dump_mesh_lowpoints.mjs` + `backend/tools/fit_safety_links.py`，
#   并由 `tests/test_safety_envelope.py` 钉住"服务端包络 ≤ 数模真值"。
# link = DH 帧原点连线（frames[0]=base … frames[6]=法兰）；from_frame≥1 的段才参与地面净空
# （与前端 getGroundRoot() 排除贴地底座同语义），所以转座的地面覆盖挂在段 1 上。
DEFAULT_LINKS: List[Dict[str, float]] = [
    {"from_frame": 0, "to_frame": 1, "r_mm": 225.0},   # 底座 + 转座水平外廓
    {"from_frame": 1, "to_frame": 2, "r_mm": 185.0},   # 大臂 + 转座地面覆盖
    {"from_frame": 2, "to_frame": 3, "r_mm": 130.0},   # 肘
    {"from_frame": 3, "to_frame": 4, "r_mm": 105.0},   # 小臂
    {"from_frame": 4, "to_frame": 5, "r_mm": 95.0},    # 腕
    {"from_frame": 5, "to_frame": 6, "r_mm": 55.0},    # 法兰前
]

_SEGMENTS = 32          # 圆形区域的离散化边数（与前端 safety.js SEGMENTS 同值）


def _num(v: Any, d: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    return f if math.isfinite(f) else d


# ---------------------------------------------------------------- 配置

def links() -> List[Dict[str, float]]:
    """连杆包络（唯一来源 = config/robot.yaml 的 safety.links）。"""
    cfg = get_config()
    raw = cfg.get("safety", "links", default=None)
    if not isinstance(raw, list) or not raw:
        return [dict(x) for x in DEFAULT_LINKS]
    out: List[Dict[str, float]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        a = int(_num(item.get("from_frame"), i))
        b = int(_num(item.get("to_frame"), i + 1))
        r = _num(item.get("r_mm"), 60.0)
        if b <= a or r <= 0:
            continue
        out.append({"from_frame": a, "to_frame": b, "r_mm": r})
    return out or [dict(x) for x in DEFAULT_LINKS]


def links_calibrated() -> bool:
    """包络半径是否已经现场卡尺复核（决定允许不允许 enforce）。"""
    try:
        return bool(get_config().get("safety", "links_calibrated", default=False))
    except Exception:
        return False


def guard_mode() -> str:
    """off | shadow | enforce（默认 shadow：只记录不拦截）。

    ★ enforce 的前提是包络已标定；否则**自动降级为 shadow**（宁可少拦，
      也不拿猜出来的半径误杀现场动作）—— 降级原因由 `mode_reason()` 给出。
    """
    mode = str(_cfg_get("safety", "server_guard", "shadow") or "shadow").lower()
    if mode not in ("off", "shadow", "enforce"):
        mode = "shadow"
    return mode


def _cfg_get(section: str, key: str, default: Any) -> Any:
    try:
        return get_config().get(section, key, default=default)
    except Exception:
        return default


def effective_mode() -> Tuple[str, str]:
    """@return (实际生效模式, 说明)。"""
    mode = guard_mode()
    if mode == "enforce" and not links_calibrated():
        return "shadow", ("已请求 enforce，但连杆包络尚未现场标定"
                          "（safety.links_calibrated=false）→ 自动降级为影子模式")
    if mode == "enforce":
        return "enforce", "服务端评估参与互锁"
    if mode == "off":
        return "off", "服务端评估已关闭"
    return "shadow", "服务端评估只记录，不拦截"


def platform_lift_m() -> float:
    """机器人立足面高度(米)。与前端 `three/cell.js PLATFORM_H` 同一个数。"""
    return _num(_cfg_get("safety", "platform_height_mm", 180.0), 180.0) / 1000.0


# ---------------------------------------------------------------- 几何

def scene_from_base_mm(x: float, y: float, z: float, lift: float = 0.0) -> Tuple[float, float, float]:
    """机器人基座系(mm,Z向上) → 场景(米,Y向上)。与 sceneCoords.js 逐字同式。"""
    return float(x) / 1000.0, float(z) / 1000.0 + lift, -float(y) / 1000.0


def capsules(joints_deg: Sequence[float], lift: Optional[float] = None,
              dz: float = 0.0) -> List[Dict[str, Any]]:
    """当前姿态下的连杆胶囊（世界系，米）。dz = 整段路径额外抬高量(米)，供 lift_suggest 用。"""
    lift = platform_lift_m() if lift is None else lift
    fk = forward_kinematics(list(joints_deg))
    frames = fk["frames"]
    out: List[Dict[str, Any]] = []
    for i, link in enumerate(links()):
        a, b = link["from_frame"], link["to_frame"]
        if b >= len(frames):
            continue
        r = _num(link["r_mm"], 60.0) / 1000.0
        p0 = scene_from_base_mm(*frames[a][:3, 3], lift)
        p1 = scene_from_base_mm(*frames[b][:3, 3], lift)
        p0 = (p0[0], p0[1] + dz, p0[2])
        p1 = (p1[0], p1[1] + dz, p1[2])
        out.append({"i": i, "from": a, "to": b, "p0": p0, "p1": p1, "r": r})
    return out


def aabb_of(caps: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """胶囊集合的外接 AABB（米，保守外扩）。"""
    if not caps:
        return {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 0, "y": 0, "z": 0}}
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    for c in caps:
        for p in (c["p0"], c["p1"]):
            for k in range(3):
                mn[k] = min(mn[k], p[k] - c["r"])
                mx[k] = max(mx[k], p[k] + c["r"])
    return {"min": {"x": mn[0], "y": mn[1], "z": mn[2]},
            "max": {"x": mx[0], "y": mx[1], "z": mx[2]}}


def zone_polygon(z: Dict[str, Any]) -> List[List[float]]:
    """区域水平多边形（世界 XZ，米）—— 与前端 zonePolygon() 同式。"""
    cx = _num((z.get("center") or {}).get("x"), 0.0)
    cz = _num((z.get("center") or {}).get("z"), 0.0)
    if z.get("shape") == "circle":
        R = _num(z.get("radius"), 0.9)
        return [[cx + R * math.cos(i / SEGMENTS_C * math.tau),
                 cz + R * math.sin(i / SEGMENTS_C * math.tau)] for i in range(SEGMENTS_C)]
    if z.get("shape") == "quad":
        cs = [[_num(p[0], 0.0), _num(p[1], 0.0)] for p in (z.get("corners") or []) if p]
        if len(cs) == 4:
            return cs
    hx = _num((z.get("half") or {}).get("x"), 0.82)
    hz = _num((z.get("half") or {}).get("z"), 0.82)
    return [[cx - hx, cz - hz], [cx + hx, cz - hz], [cx + hx, cz + hz], [cx - hx, cz + hz]]


SEGMENTS_C = _SEGMENTS


def polygon_halfspaces(pts: Sequence[Sequence[float]]) -> List[Dict[str, float]]:
    """多边形 → 内法线半空间（不依赖顶点绕向，与前端同式）。"""
    pts = [p for p in (pts or [])]
    if len(pts) < 3:
        return []
    cx = sum(p[0] for p in pts) / len(pts)
    cz = sum(p[1] for p in pts) / len(pts)
    out: List[Dict[str, float]] = []
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        dx, dz = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dz)
        if L < 1e-9:
            continue
        dx, dz = dx / L, dz / L
        nx, nz = -dz, dx
        if nx * (cx - a[0]) + nz * (cz - a[1]) < 0:
            nx, nz = -nx, -nz
        out.append({"nx": nx, "nz": nz, "ax": a[0], "az": a[1]})
    return out


def clearance_to_polygon(box: Dict[str, Dict[str, float]], hs: Sequence[Dict[str, float]]) -> float:
    xs = [box["min"]["x"], box["max"]["x"]]
    zs = [box["min"]["z"], box["max"]["z"]]
    best = math.inf
    for e in hs:
        d = min(e["nx"] * (x - e["ax"]) + e["nz"] * (z - e["az"]) for x in xs for z in zs)
        best = min(best, d)
    return 0.0 if best == math.inf else best


def clearance_to_circle(box: Dict[str, Dict[str, float]], cx: float, cz: float, R: float) -> float:
    px = box["min"]["x"] if abs(box["min"]["x"] - cx) > abs(box["max"]["x"] - cx) else box["max"]["x"]
    pz = box["min"]["z"] if abs(box["min"]["z"] - cz) > abs(box["max"]["z"] - cz) else box["max"]["z"]
    return R - math.hypot(px - cx, pz - cz)


def basis_of(z: Dict[str, Any]) -> float:
    t = z.get("thresholds") or {}
    if t.get("basis") == "fixed":
        return max(1e-6, _num(t.get("fixed_mm"), 300.0) / 1000.0)
    if z.get("shape") == "circle":
        return max(1e-6, _num(z.get("radius"), 0.9))
    cx = _num((z.get("center") or {}).get("x"), 0.0)
    cz = _num((z.get("center") or {}).get("z"), 0.0)
    m = min((e["nx"] * (cx - e["ax"]) + e["nz"] * (cz - e["az"])
             for e in polygon_halfspaces(zone_polygon(z))), default=math.inf)
    if not math.isfinite(m) or m <= 1e-6:
        m = min(_num((z.get("half") or {}).get("x"), 0.82),
                _num((z.get("half") or {}).get("z"), 0.82))
    return max(1e-6, m)


def classify(clearance: float, basis: float, th: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """间隙 + 基准 → 四级。与前端 classify() 同式（ratio<=0→hit）。"""
    warn = _num((th or {}).get("warn"), 0.3)
    danger = _num((th or {}).get("danger"), 0.1)
    b = basis if basis > 1e-6 else 1.0
    ratio = clearance / b
    state = "safe"
    if ratio <= 0:
        state = "hit"
    elif ratio < danger:
        state = "danger"
    elif ratio < warn:
        state = "warn"
    return {"state": state, "ratio": ratio, "clearance": clearance}


def classify_ground(min_y: float, g: Optional[Dict[str, Any]]) -> str:
    """立足面净空(米) → 四级。与前端 classifyGround() 同式。"""
    warn = _num((g or {}).get("warn_mm"), 150.0) / 1000.0
    danger = _num((g or {}).get("danger_mm"), 80.0) / 1000.0
    hit = _num((g or {}).get("hit_mm"), 30.0) / 1000.0
    if min_y <= hit:
        return "hit"
    if min_y <= danger:
        return "danger"
    if min_y <= warn:
        return "warn"
    return "safe"


# ---------------------------------------------------------------- 评估

def evaluate(joints_deg: Sequence[float], cfg: Optional[Dict[str, Any]] = None,
             lift: Optional[float] = None, dz: float = 0.0) -> Dict[str, Any]:
    """当前姿态的服务端独立结论（单位与前端上报一致：**米**）。

    @return {state, ratio, clearance, zone_id, zone_name, zones[], ground, per_link[],
             source:"server", calibrated}
    """
    cfg = cfg or load_safety()
    lift = platform_lift_m() if lift is None else lift
    caps = capsules(joints_deg, lift, dz)
    box = aabb_of(caps)
    out_zones: List[Dict[str, Any]] = []

    enabled = cfg.get("enabled") is not False

    if enabled:
        for z in (cfg.get("zones") or []):
            if not z or z.get("enabled") is False:
                continue
            if z.get("shape") == "circle":
                clr = clearance_to_circle(box, _num((z.get("center") or {}).get("x"), 0.0),
                                         _num((z.get("center") or {}).get("z"), 0.0),
                                         _num(z.get("radius"), 0.9))
            else:
                clr = clearance_to_polygon(box, polygon_halfspaces(zone_polygon(z)))
            r = classify(clr, basis_of(z), z.get("thresholds"))
            out_zones.append({"id": str(z.get("id") or ""), "name": str(z.get("name") or ""),
                              **r})

    # 地面：各胶囊"最低点"= min(端点 y) − 半径，再减去立足面高度
    # ★★ 只取 **J1 以上子树**（from_frame >= 1），与前端 getGroundRoot() 同语义：
    #    底座本来就贴在立足面上（min.y≈立足面），把它算进来会**永远**判成"碰撞地面"，
    #    这正是前端当初排除底座的原因（three/manager.js getGroundRoot 注释）。
    g = cfg.get("ground") or {}
    g_on = enabled and g.get("enabled") is not False
    gclear, gst = 0.0, "safe"
    per_link: List[Dict[str, Any]] = []
    measured = False
    above_base = [c for c in caps if c["from"] >= 1]
    if above_base:
        measured = True
        lows = [min(c["p0"][1], c["p1"][1]) - c["r"] for c in above_base]
        gclear = float(min(lows) - lift)      # ★ 必须转 python float，numpy 标量进不了 JSON
        gst = classify_ground(gclear, g) if g_on else "safe"
        for c, low in zip(above_base, lows):
            per_link.append({"link": c["i"], "low_m": round(low - lift, 4),
                             "state": classify_ground(low - lift, g) if g_on else "safe"})

    # 总状态 = 各区域 + 地面 里**最严重**的那个（与前端 update() 的 consider 同语义）
    candidates = list(out_zones) if caps else []
    if g_on and measured:
        candidates.append({"id": "ground", "name": "地面", "state": gst,
                           "clearance": gclear, "ratio": 0.0})
    top: Optional[Dict[str, Any]] = None
    for c in candidates:
        top = c if top is None else worse(top, c)
    if top is None:
        # ★ 拿不到包络 / 只剩底座 → **判不了**，如实写 unknown（铁律③：未知不得塌成"安全"）
        state = "unknown"
    else:
        state = top.get("state", "safe")
    result = {
        "state": state,
        "ratio": _num((top or {}).get("ratio"), 1.0),
        "clearance": _num((top or {}).get("clearance"), 0.0),
        "zone_id": (top or {}).get("id", ""),
        "zone_name": (top or {}).get("name", ""),
        "zones": out_zones,
        "measured": bool(caps) and measured,
        "ground": {"enabled": g_on, "measured": measured, "clearance": gclear,
                   "clearance_mm": gclear * 1000.0, "state": gst if measured else "unknown"},
        "per_link": per_link,
        "source": "server",
        "calibrated": links_calibrated(),
    }
    return result


def worse(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """两路结论取更严重（fail-safe）。"""
    if not a:
        return b
    if not b:
        return a
    ra, rb = STATE_RANK.get(a.get("state", "safe"), 0), STATE_RANK.get(b.get("state", "safe"), 0)
    if rb > ra:
        return b
    if ra > rb:
        return a
    return b if _num(b.get("clearance"), 0.0) < _num(a.get("clearance"), 0.0) else a


def combine(client: Optional[Dict[str, Any]], server: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """双源合并：谁严重听谁；一侧缺失不影响另一侧。"""
    out = worse(client, server) or {"state": "unknown", "clearance": 0.0, "ratio": 1.0}
    return {"state": out.get("state", "safe"), "clearance": _num(out.get("clearance"), 0.0),
            "ratio": _num(out.get("ratio"), 1.0),
            "zone_id": out.get("zone_id", "") or out.get("zoneId", ""),
            "zone_name": out.get("zone_name", "") or out.get("zoneName", ""),
            "sources": {"client": client or None, "server": server or None}}


# ---------------------------------------------------------------- A2 扫掠

def sweep(q0: Sequence[float], q1: Sequence[float], n: int = 48,
          cfg: Optional[Dict[str, Any]] = None, dz: float = 0.0) -> Dict[str, Any]:
    """关节空间等分插值的**几何**扫掠（保守包络，不是控制器真实轨迹）。

    ★ 只做几何，不做时间：PC 写目标+触发位、控制器 210 自己执行且采样 10Hz，
      拿不到"什么时候到"，所以**绝不输出 ETA**（方案 §10.3 档三第 2 条）。
    """
    n = max(2, int(n))
    cfg = cfg or load_safety()
    lift = platform_lift_m()
    curve: List[Dict[str, Any]] = []
    worst: Optional[Dict[str, Any]] = None
    worst_idx = 0
    for i in range(n):
        f = i / (n - 1)
        q = [float(a) + (float(b) - float(a)) * f for a, b in zip(q0, q1)]
        r = evaluate(q, cfg=cfg, lift=lift, dz=dz)
        curve.append({"t": round(f, 4), "state": r["state"],
                      "clearance": r["clearance"], "zone": r["zone_id"]})
        if worst is None or STATE_RANK[r["state"]] > STATE_RANK[worst["state"]] or (
                r["state"] == worst["state"] and r["clearance"] < worst["clearance"]):
            worst, worst_idx = r, i
    return {
        "ok": bool(worst) and worst["state"] == "safe",
        "worst_idx": worst_idx, "worst_state": worst["state"] if worst else "unknown",
        "worst_mm": round(_num(worst["clearance"], 0.0) * 1000.0, 1) if worst else 0.0,
        "worst_zone": (worst or {}).get("zone_id", ""),
        "curve": curve, "samples": n,
        "note": "保守包络估计（胶囊近似），不是控制器真实轨迹；无 ETA",
    }


def lift_suggest(q0: Sequence[float], q1: Sequence[float], target_mm: Optional[float] = None,
                 n: int = 16, step_mm: float = 10.0, max_mm: float = 400.0) -> Dict[str, Any]:
    """要把这段路径**整体抬高**多少毫米才能全程安全（只算台面净空）。

    ★ 抬高只对"地面/立足面净空"有效 —— 围栏余量是**水平方向**的（前端也不看区域高度），
      所以这里不声称能解决撞墙。给不出解就如实返回 None。
    """
    cfg = load_safety()
    target = _num(target_mm, _num((cfg.get("ground") or {}).get("warn_mm"), 150.0)) / 1000.0
    base = sweep(q0, q1, n=n, cfg=cfg)
    if base["ok"]:
        return {"needed_mm": 0.0, "ok": True, "sweep": base, "axis": "z"}
    mm = 0.0
    while mm < max_mm:
        mm += step_mm
        s = sweep(q0, q1, n=n, cfg=cfg, dz=mm / 1000.0)
        if s["ok"] and s["worst_mm"] / 1000.0 >= target:
            return {"needed_mm": mm, "ok": True, "sweep": s, "axis": "z"}
    return {"needed_mm": None, "ok": False, "sweep": base, "axis": "z",
            "reason": f"整体抬高 {max_mm:.0f}mm 内仍不满足"
                      f"（可能需要先经正上位再平移，或重录点位）"}


# ---------------------------------------------------------------- A3 SSM 建议

def ssm_advice(distance_mm: Optional[float] = None,
               speed_pct: Optional[float] = None,
               cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按 ISO/TS 15062 的 `S = K·T + C` 给**建议**（只提示，绝不闭环）。

    ★★ 三个量里 **T（总停机时间）和 K100（100% 速度对应的接近线速度）都必须现场实测**。
      本项目 PC 端只能"写目标 + 触发位"，真正的加减速由控制器常驻程序 210 执行，
      **拿不到加减速曲线** —— 所以网上抄的"典型停机时间 0.3s"在这里就是编数，
      绝不能拿它给现场划安全距离（§5.5 铁律③：不知道就说不知道）。
      缺哪个就原样列进 `unknown`，`available=False`，界面必须显示"无法给出"。

    ★ 本函数**只读**：不写 40103、不调 `ui_speed.publish`、不动滑块
      （速度唯一口径 = 右上角滑块）。钉 `test_ssm_never_writes_speed`。
    """
    cfg = cfg or {}
    t_stop = _num(cfg.get("stop_time_s", _cfg_get("safety", "stop_time_s", None)), float("nan"))
    k100 = _num(cfg.get("ssm_k100_mm_s", _cfg_get("safety", "ssm_k100_mm_s", None)), float("nan"))
    c_mm = cfg.get("ssm_c_mm", _cfg_get("safety", "ssm_c_mm", None))

    unknown: List[str] = []
    if not math.isfinite(t_stop) or t_stop <= 0:
        unknown.append("T：总停机时间（检测+响应+制动）未现场实测"
                       "（config/robot.yaml: safety.stop_time_s=null）")
    if not math.isfinite(k100) or k100 <= 0:
        unknown.append("K100：100% 速度对应的接近线速度未实测"
                       "（config/robot.yaml: safety.ssm_k100_mm_s=null）")
    c = _num(c_mm, float("nan"))
    if not math.isfinite(c) or c < 0:
        unknown.append("C：附加距离（停机过冲+工具/工件形状裕量）未配置"
                       "（config/robot.yaml: safety.ssm_c_mm=null）")

    if distance_mm is None:
        try:  # 缺省用后端自己刚算出的最小余量（不是前端上报的那一路）
            from app.services.safety_guard import snapshot
            server = (snapshot() or {}).get("server") or {}
            # ★ 只有**真的算过**才把这个数当"分隔距离"——快照里没数据时 clearance 是 0.0，
            #   直接拿来会显示"分隔距离 0mm"这种看着像事实的假读数（铁律①/③）。
            if server.get("evaluated"):
                distance_mm = _num(server.get("clearance"), float("nan")) * 1000.0
            if distance_mm is None or not math.isfinite(distance_mm):
                distance_mm = float("nan")
                unknown.append("分隔距离：后端还没有可用的实时评估结果")
        except Exception:
            distance_mm = float("nan")
            unknown.append("分隔距离：读不到实时评估")
    if speed_pct is None:
        try:
            from app.services import ui_speed
            speed_pct = _num(ui_speed.peek().get("speed_pct"), float("nan"))
            if not math.isfinite(speed_pct):
                unknown.append("速度：界面滑块尚未推送过速度")
        except Exception:
            speed_pct = float("nan")

    out: Dict[str, Any] = {
        "available": not unknown,
        "formula": "S = K·T + C（ISO/TS 15062 SSM 分隔距离）",
        "inputs": {
            "T_stop_time_s": t_stop if math.isfinite(t_stop) and t_stop > 0 else None,
            "K100_mm_s": k100 if math.isfinite(k100) and k100 > 0 else None,
            "C_mm": c if math.isfinite(c) else None,
            "speed_pct": speed_pct if math.isfinite(speed_pct) else None,
            "distance_mm": distance_mm if math.isfinite(distance_mm) else None,
        },
        "unknown": unknown,
        "required_speed_cap_pct": None,
        "separation_mm": None,
        "note": "软件级建议，不替代控制器安全板，也未取得 ISO 10218 SSM 认证；"
                "**不回写速度**（速度唯一口径 = 界面右上角滑块）",
    }
    if unknown:
        out["reason"] = "参数未实测，不给建议：" + "；".join(unknown)
        return out

    k = k100 * speed_pct / 100.0
    s_req = k * t_stop + c
    out["separation_mm"] = round(s_req, 1)
    out["ok"] = bool(math.isfinite(distance_mm)) and distance_mm >= s_req
    if math.isfinite(distance_mm) and t_stop > 0:
        cap = max(0.0, (distance_mm - c) / t_stop / k100 * 100.0)
        # 上限就是滑块的 100%（算出 375% 没有意义，界面也只有 0~100）
        out["required_speed_cap_pct"] = int(min(100, math.floor(cap)))
    return out
