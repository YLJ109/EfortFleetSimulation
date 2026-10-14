# -*- coding: utf-8 -*-
"""★★ E3 反事实试跑（What-if 沙盒）—— **不碰真机**，离线回答「这样改值不值得上真机」。

立意（方案 §E3）：预演只算**单一终点**，操作员换型前想知道的是「托盘右移 10mm /
改成反面取 / 先放再取 / 用 40% 档」这一组改动**整体上稳不稳**。本模块把**既有纯函数**
组成一台离线实验台：

    点位库（只读） + `sorting.raw_slot_safe_path`（原位安全路径 · 唯一实现）
      + `sorting.face_for_target` / `face_j6_of`（面语义 · 唯一实现）
      + `kinematics`（FK/IK —— 把「托盘偏移 mm」落到关节角）
      + `safety_eval.sweep`（A2 保守包络扫掠 —— 几何/间隙这一档是真能算的）
      + `twin_residual`（**已发生的实测段** —— 节拍这一档唯一合法的数据源）
    ⇒ 参数扫描 + 蒙特卡洛抖动 ⇒ 输出**分布**（P5/P50/P95），不是单点。

★★★ 三条硬口径（比方案更严格；这是本项目的现场纪律，不许绕）

1) **绝不输出「未来的 ETA / 预计耗时」**。机理（AGENTS.md §4 关键机理 1）：上位机只写
   「目标数据 + 触发位」，真正的运动由控制器里的常驻程序执行，上位机**拿不到速度曲线**
   ⇒ 任何「预估节拍」都是编故事。所以「节拍」这一档**只允许**由**已经发生的实测样本**代入
   （段历史里的 `settle_ms` / `peak_deg` / `verdict`）。
   同名实测不足 `MIN_CYCLE_SAMPLES` 段 ⇒ 该档如实写「无实测样本·不给节拍估计」，
   不外推、不给 P50/P90；返回体带 `n_samples` 与 `basis`（写清「来自实测段，不是预测」）。
   ★ 输出里**没有任何键**叫 eta / estimate / 预计 —— 钉 `tests/test_whatif.py`。

2) **几何/间隙这一档是真能算的**：对**偏移后的点位姿态**做保守包络扫掠，输出**最小间隙分布**。
   抖动来源**逐项列明**（`jitter_sources`，含 `assumed` 标记：哪些是现场实测口径、
   哪些只是假设值），并写明「离线统计估计 · 非认证」。

3) **样本数 = 实际跑出来的那个**（§5.5 铁律②）。`SAMPLE_N_DEFAULT` 不是拍脑袋：
   ★★ 2026-10-13 用 `backend/tools/time_whatif.py`（纯离线：不连机器人、不读生产库、
      不写寄存器）在这台开发机上跑 3 次实测，评估路径 = **5 段 × 每段 12 拍保守包络扫掠**：
     · **单样本 sample_ms = 4.97 / 5.38 / 6.13 ms**（取均值 ≈ **5.5 ms**，warm-up 后）；
     · 整轮实测：n=50 → 271.5ms；n=100 → 544.7ms；n=140 → 726.5ms；
       **n=200 → 1017.8 / 1019.6 ms**；n=300 → 1501.5 / 1501.9 ms 且**被预算截断**
       （只跑完 291 / 299 个）—— 这就是保留 `RUN_BUDGET_MS` 的原因。
     · 据此定 **`SAMPLE_N_DEFAULT = 200`**：方案 §E3 写的那个未经计时的 n，
       实测 1.02s 跑得完（≈ 32% 余量留在 1.5s 预算内），所以可以采用；
       换到更慢的机器上不会"界面一个数、实际另一个数"，因为超预算会**少跑并如实改写 n**。
   `SAMPLE_N_MAX = 300`：实测这个档位一定撞预算 → 上限只做参数防御，界面默认不推到这里。
   并保留 `RUN_BUDGET_MS` 硬上限：**超预算就少跑几轮**，返回体的 `n` 写**真正跑完的轮数**
   （宁少不假），且 `failures` 里如实说明被截断。
   另：每次 `run()` 的固定开销（点位/名义扫掠/统计）实测 ≈ **13.4 ms/次（n=1）**，
   与 n 无关的那部分很便宜，所以耗时基本是 `n × 5.5ms` 线性 —— 想调 n 就按这条斜率算。

★★ 本模块**不做什么**（改动前先读）
  · **不 import 运动/下发那一层**：运动引擎、寄存器写路径、控制接口路由统统不在依赖里
    （具体模块名不写在这里 —— 写了会让"源码不含这些词"的那条静态钉检查误判）；
    **不产生任何动作**：不写寄存器、不碰触发位、不碰气路位表、不调长流程的那条咽喉。
    钉 `tests/test_whatif.py::test_whatif_never_touches_motion_or_modbus_writes`。
  · **不写速度口径**：`speed_gear_pct` 只是「评估用的档位标签」（并只用来筛实测样本），
    不写速度寄存器、不调速度发布口、不改右上角滑块（速度唯一口径不变）。
  · **不改点位、不改真机次序**：`order` / `face` 只重排**评估用的路径**，不落库、不执行。
  · **不声称仿真精度**：所有输出带 `note`=「离线统计估计 · 非认证」+ `not_modelled` 清单
    （没建模的东西如实列出：真实加减速、振动、工件滑移、判色置信度…）。
  · **不引入新依赖 / 仿真引擎**：只用标准库 `random` + numpy（`kinematics` 本来就用它）。

三态（§5.5 铁律③：未知不得塌成布尔）：`status` = `ok` / `partial` / `failed` / `no_data` ——
「没数据」和「跑失败」是两个结论，都不许被写成「安全」。
"""
from __future__ import annotations

import math
import random
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.logger import get_logger
from app.services import kinematics as K
from app.services import limits as LM
from app.services import safety_eval as SE
from app.services import sorting as S

log = get_logger("whatif")

# ---------------------------------------------------------------- 口径常量
#: 一轮蒙特卡洛的默认样本数（★ 依据见模块顶部那条离线计时实测：单样本 ≈ 5.5ms / 5 段，
#: n=200 整轮实测 ≈ 1.02s ≤ 1.5s 预算；更慢的机器上会被 RUN_BUDGET_MS 截断并如实改写 n）
SAMPLE_N_DEFAULT = 200
#: 上限：再多界面就要等人（超过一律夹到这里）
SAMPLE_N_MAX = 300
#: 整轮墙钟预算（毫秒）：超了就**少跑几轮**，`n` 如实写实际跑完的轮数
RUN_BUDGET_MS = 1500.0
#: 每段的保守包络扫掠拍数（A2 的 sweep 默认 48 太贵；实验台 12 拍足够看出趋势）
SWEEP_SAMPLES_PER_SEGMENT = 12
#: 段历史里**至少这么多条实测**才允许给节拍档（样本不足就不给，方案 §E3 同口径）
MIN_CYCLE_SAMPLES = 8
#: 缺省评估路径：一件「取 → 检测 → 放 → 回起点」的完整循环（缺的点会被跳过并写进 failures）
DEFAULT_PATH: Tuple[str, ...] = (S.A_SLOTS[0], S.A_SLOTS[0] + "上", S.CAMERA_POINT,
                                 S.B_SLOTS[0] + "上", S.B_SLOTS[0], S.START_POINT)
#: 摆放偏移的缺省不确定度（mm）：现场**没量过托盘重复度** → 当"假设值"如实标注
TRAY_REPEAT_MM_ASSUMED = 2.0
#: 示教点自身重复偏差（°）：同样是假设值，标 `assumed=True`（绝不冒充实测）
TEACH_REPEAT_DEG_ASSUMED = 0.5

FACES = ("keep", "front", "back")
ORDERS = ("as_listed", "reverse", "by_zone")

STATE_LABEL = {"safe": "全程在警戒线外", "warn": "警戒", "danger": "危险",
               "hit": "触界（包络进入围栏）", "unknown": "判不了"}
STATE_TAIL = {"safe": "可以考虑上真机（先低速试一件）",
              "warn": "建议先减小改动量或重录点位",
              "danger": "不建议上真机",
              "hit": "不要上真机（保守包络已进入围栏）",
              "unknown": "什么都算不出来 —— 这不代表安全"}

NOTE = ("离线统计估计 · **非认证**：保守包络（胶囊近似）+ 关节空间蒙特卡洛抖动，"
        "不是控制器真实轨迹，也**不产 ETA / 预计耗时**（上位机拿不到速度曲线）。"
        "本模块不产生任何动作：不写寄存器、不碰触发位与气路、不改速度口径。")

#: ★ 未建模清单（唯一一份）：`jitter_model` 与 `_early`（没有几何结论那一支）**共用它** ——
#:    算不出来的那一档同样必须说清"哪些东西根本没建模"，不许给界面一个空列表
#:    （§5.5 铁律⑤：死状态要么接线、要么删、要么如实注明）。
NOT_MODELLED: Tuple[str, ...] = (
    "真实加减速与制动距离（上位机拿不到速度曲线 → 也不据此推任何时间量）",
    "工件滑移 / 吸盘持力（现场没有真空传感器，持夹状态只是流程推断）",
    "判色置信度（视觉侧的事，本实验台不读也不改阈值）",
    "控制器常驻程序的实际段序（本台按原位安全路径模型评估，不等于控制器走法）",
    "托盘自身坐标系与机器人基座系的夹角（偏移一律加在基座系上）",
)


# ---------------------------------------------------------------- 小工具

def _num(v: Any, d: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    return f if math.isfinite(f) else d


def _finite6(q: Any) -> Optional[List[float]]:
    try:
        arr = [float(x) for x in q]
    except (TypeError, ValueError):
        return None
    if len(arr) != 6 or any(not math.isfinite(v) for v in arr):
        return None
    return arr


def _pct(vals: Sequence[float], q: float) -> Optional[float]:
    """分位数（线性插值）；空集合 → **None**（不塌成 0，铁律③）。"""
    s = sorted(float(v) for v in (vals or []) if math.isfinite(v))
    if not s:
        return None
    if len(s) == 1:
        return round(s[0], 2)
    i = (len(s) - 1) * max(0.0, min(1.0, float(q)))
    lo, hi = math.floor(i), math.ceil(i)
    return round(s[lo] + (s[hi] - s[lo]) * (i - lo), 2)


def _bins(vals: Sequence[float], k: int = 12) -> List[Dict[str, float]]:
    """直方图（前端只画、不重算）；空 → []。"""
    s = [float(v) for v in (vals or []) if math.isfinite(v)]
    if not s:
        return []
    lo, hi = min(s), max(s)
    if hi - lo < 1e-6:
        return [{"lo": round(lo, 2), "hi": round(hi, 2), "count": len(s)}]
    w = (hi - lo) / k
    counts = [0] * k
    for v in s:
        counts[min(k - 1, int((v - lo) / w))] += 1
    return [{"lo": round(lo + i * w, 2), "hi": round(lo + (i + 1) * w, 2), "count": c}
            for i, c in enumerate(counts) if c]


# ---------------------------------------------------------------- 场景参数

def scenario_of(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把界面/脚本传来的场景归一化（非法值回缺省，**不猜别的东西**）。"""
    raw = raw or {}
    tray = raw.get("tray") or {}
    face = str(raw.get("face") or "keep").strip().lower()
    if face not in FACES:
        face = "keep"
    order = str(raw.get("order") or "as_listed").strip().lower()
    if order not in ORDERS:
        order = "as_listed"
    pts = raw.get("points")
    names = [str(x).strip() for x in pts] if isinstance(pts, (list, tuple)) else []
    names = [x for x in names if x]
    speed = raw.get("speed_gear_pct")
    gear = None if speed is None else int(max(0, min(100, int(_num(speed, 0.0)))))
    return {
        "tray": {"dx_mm": _num(tray.get("dx")), "dy_mm": _num(tray.get("dy")),
                 "dz_mm": _num(tray.get("dz"))},
        "face": face,
        "order": order,
        "points": names,
        "speed_gear_pct": gear,
        "tray_repeat_mm": _num(raw.get("tray_repeat_mm"), TRAY_REPEAT_MM_ASSUMED),
        "teach_repeat_deg": _num(raw.get("teach_repeat_deg"), TEACH_REPEAT_DEG_ASSUMED),
    }


def _zone_key(name: str) -> str:
    """次序分组用的「区」：A / B / C（检测）/ 其它。纯评估用，不参与任何工艺判定。"""
    t = str(name or "").strip()
    for k in ("A", "B", "C"):
        if t.startswith(k):
            return k
    return "X"


def reorder(names: Sequence[str], mode: str) -> List[str]:
    """取放次序（**只重排评估用的路径**，不落库、不执行）。"""
    seq = [str(x) for x in (names or [])]
    if mode == "reverse":
        return list(reversed(seq))
    if mode == "by_zone":
        rank = {"A": 0, "B": 1, "C": 2, "X": 3}
        return sorted(seq, key=lambda n: (rank.get(_zone_key(n), 3), n))
    return seq


# ---------------------------------------------------------------- 点位姿态

def shift_pose(q: Sequence[float], dx: float, dy: float, dz: float,
               failures: List[str]) -> List[float]:
    """把「托盘偏移(mm，基座系)」落到**该点位的关节角**上。

    ★ 做法：FK 取示教 TCP → 平移 → IK 回关节角（初值 = 示教姿态 → 拿到的是"最近的那组解"）。
    ★ 诚实边界：偏移加在**基座系**上（现场没量过托盘坐标系与基座系的夹角）；
      IK 拿不到解时**退回示教姿态**并把这件事写进 `failures`（不装作偏移没影响）。
    """
    base = _finite6(q)
    if base is None:
        return [float(v) for v in (q or [])]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9 and abs(dz) < 1e-9:
        return base
    target = K.translate_matrix(dx, dy, dz) @ K.fk_matrix(base)
    r = K.ikine(target, base, joint_limits=LM.load_limits(), max_iter=120) or {}
    joints = _finite6(r.get("joints"))
    if joints is None:
        failures.append("偏移 %g/%g/%gmm 的 IK 没解出来 → 该点退回示教姿态"
                        "（这一点的间隙分布会**偏乐观**）" % (dx, dy, dz))
        return base
    if not r.get("ok"):
        failures.append("偏移 %g/%g/%gmm 的 IK 残差偏大（pos_err=%smm、rot_err=%s°）"
                        " → 用了残差最小的那组解，间隙分布会**偏乐观**"
                        % (dx, dy, dz, r.get("pos_err"), r.get("rot_err_deg")))
    return joints


def face_pose(name: str, joints: Sequence[float], face: str,
              current: Optional[Sequence[float]]) -> List[float]:
    """按**既有面语义**取该点的 J6（唯一判据 `sorting.face_for_target`：原料位一律 keep）。

    ★ 18 个原料位的**禁转黑名单一个字节没改**：`face_for_target` 会把它们压成 keep，
      实验台自然也"转不出来" —— 要评估翻面，只能在正上位 / C 检测 / 起点的姿态上比。
    """
    q = list(joints)
    if len(q) != 6:
        return q
    want = face if face in ("front", "back") else S.FACE_FRONT
    eff = S.face_for_target(name, want)
    cur = _finite6(current)
    cur6 = cur[5] if cur else None
    v6 = S.keep_j6(float(q[5]), cur6) if S.is_keep_face(eff) else S.face_j6_of(float(q[5]), eff, cur6)
    q[5] = S.normalize_j6(v6)
    return q


def build_segments(names: Sequence[str], poses: Dict[str, Sequence[float]],
                   sc: Dict[str, Any], live: Optional[Sequence[float]],
                   failures: List[str]) -> List[Dict[str, Any]]:
    """点名序列 → **逐段起止关节角**（含 `raw_slot_safe_path` 的自动补段，与现场同一模型）。

    ★ 补段用唯一实现（原位 → 正上位 → 目标正上位 → 目标），所以"改次序"改的是**整条段序列**，
      不是拿"违禁的低位横插"来比 —— 那条路现场根本走不通，比了也是假的。
    ★ 需要的正上位**未示教** → 跳过那一跳并写进 `failures`（不硬造点位）。
    """
    tray = sc["tray"]
    cache: Dict[str, List[float]] = {}

    def pose_of(nm: str) -> Optional[List[float]]:
        if nm in cache:
            return cache[nm]
        taught = poses.get(nm)
        if taught is None:
            return None
        shifted = shift_pose(taught, tray["dx_mm"], tray["dy_mm"], tray["dz_mm"], failures)
        out = face_pose(nm, shifted, sc["face"], live)
        cache[nm] = out
        return out

    expanded: List[str] = []
    cur_pose = _finite6(live) if live else None
    for nm in names:
        path: Optional[List[str]] = None
        if cur_pose is not None:
            try:
                path, _reason = S.raw_slot_safe_path(cur_pose, poses, nm)
            except Exception as e:                        # noqa: BLE001 —— 退回单段，不硬造
                failures.append("原位安全路径判定失败（%s）→ 本跳按单段评估" % str(e)[:60])
                path = None
        for hop in (list(path) if path else [nm]):
            if expanded and expanded[-1] == hop:
                continue                                  # 幂等：已经在这一格，不重复成段
            if hop not in poses:
                failures.append("点位「%s」不在点位库（未示教）→ **没有算它**，"
                                "不代表它安全" % hop)
                continue
            expanded.append(hop)
            got = pose_of(hop)
            if got is not None:
                cur_pose = got
    segs: List[Dict[str, Any]] = []
    for i in range(1, len(expanded)):
        a, b = expanded[i - 1], expanded[i]
        qa, qb = pose_of(a), pose_of(b)
        if qa is None or qb is None:
            continue
        segs.append({"i": len(segs) + 1, "from": a, "to": b, "point": b, "q0": qa, "q1": qb})
    return segs


# ---------------------------------------------------------------- 抖动来源

def _residual_calibre() -> Tuple[float, float]:
    """到位容差 / 采集不确定度（°）：**与到位自证同源**（读它的口径，不自己写一份）。"""
    try:
        from app.services.twin_residual import twin_residual      # 只读 state()，不喂数据
        st = twin_residual.state() or {}
        tol = _num(st.get("tol_deg"), 1.0) or 1.0
        unc = _num((st.get("current") or {}).get("uncertainty_deg"), 0.0)
        if unc <= 0:
            unc = max(0.2, min(5.0, tol) / 2.0)   # 没有在飞段 → 按"带封顶"的一半保守取，不冒充实测
        return tol, unc
    except Exception as e:                                          # noqa: BLE001
        log.debug("whatif 读到位自证口径失败（用保守缺省）: %s", e)
        return 1.0, 0.5


def jitter_model(sc: Dict[str, Any], segs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """逐项列明抖动来源（方案：「所有输出必须带实际使用的抖动来源清单」）。"""
    tol, unc = _residual_calibre()
    teach = sc["teach_repeat_deg"]
    mm = sc["tray_repeat_mm"]
    sigma_deg = math.sqrt(tol ** 2 + unc ** 2 + teach ** 2)
    # 摆放重复(mm) → 绕 J1 的等效角：1mm ≈ (180/π)/R 度（R = TCP 到基座竖轴的水平距离）
    radius = 0.0
    for s in segs:
        try:
            tcp = K.tcp_of(s["q1"])
            radius = max(radius, math.hypot(_num(tcp[0]), _num(tcp[1])))
        except Exception:                                            # noqa: BLE001 —— 量不到就不给系数
            radius = 0.0
    lever = (180.0 / math.pi / radius) if radius > 50.0 else 0.0
    return {
        "joint_sigma_deg": round(sigma_deg, 4),
        "tray_sigma_mm": round(mm, 3),
        "tray_lever_deg_per_mm": round(lever, 5),
        "tcp_radius_mm": round(radius, 1),
        "sources": [
            {"name": "到位容差", "key": "arrival_tol_deg", "sigma": round(tol, 3),
             "unit": "deg", "assumed": False,
             "basis": "config/robot.yaml 的 twin_residual.tol_deg（与后端/界面同一份口径）"},
            {"name": "采集不确定度", "key": "sampling_uncertainty_deg", "sigma": round(unc, 3),
             "unit": "deg", "assumed": False,
             "basis": "到位自证的「带 = 容差 + 一拍采样延迟的不确定度」（读它自己的读数；"
                      "没有在飞段时按容差的一半保守取，不冒充实测）"},
            {"name": "示教点自身偏差", "key": "teach_repeat_deg", "sigma": round(teach, 3),
             "unit": "deg", "assumed": True,
             "basis": "现场没有示教重复度的实测记录 → 假设值 %s°，**不是**量出来的" % teach},
            {"name": "托盘摆放重复度", "key": "tray_repeat_mm", "sigma": round(mm, 3),
             "unit": "mm", "assumed": True,
             "basis": "现场没有卡尺实测托盘重复度 → 假设值 %smm，按 FK 半径 %smm 折算成"
                      "绕 J1 的等效角（线性近似 · 非认证）" % (mm, round(radius, 1))},
        ],
        "not_modelled": list(NOT_MODELLED),
    }


def _jitter(q: Sequence[float], rng: random.Random, model: Dict[str, Any]) -> List[float]:
    """一个样本的姿态 = 名义姿态 + 关节域抖动（RSS 后一次抽样）+ 摆放重复的等效角 → 夹到软限位。"""
    sigma = float(model["joint_sigma_deg"])
    lever = float(model["tray_lever_deg_per_mm"])
    out = [float(v) for v in q]
    for i in range(6):
        out[i] = out[i] + (rng.gauss(0.0, sigma) if sigma > 0 else 0.0)
    if lever > 0:
        # 只加在 J1：它是唯一能完整表达"水平偏摆"的轴（线性近似，见 jitter_model 的 basis）
        out[0] = out[0] + lever * (rng.gauss(0.0, float(model["tray_sigma_mm"])))
    out[5] = S.normalize_j6(out[5])
    clamped, _hit = LM.clamp(out)
    return [float(v) for v in clamped]


# ---------------------------------------------------------------- 节拍档（只代实测样本）

def _settled_rows() -> List[Dict[str, Any]]:
    try:
        from app.services.twin_residual import twin_residual      # 只读段历史
        rows = twin_residual.recent(40) or []
    except Exception as e:                                        # noqa: BLE001
        log.debug("whatif 读段历史失败: %s", e)
        return []
    return [r for r in rows if str(r.get("verdict") or "") in ("arrived", "slow")
            and r.get("settle_ms") is not None]


def cycle_time_for(path: Sequence[str], gear: Optional[int]) -> Dict[str, Any]:
    """「节拍」这一档：**只代已发生的实测样本**；不足 `MIN_CYCLE_SAMPLES` 条 → 如实说没有。

    ★ 数据来源 = 到位自证的段历史（`settle_ms` = 实测收敛耗时，`peak_deg` = 实测峰值残差）。
    ★ 匹配口径（写进 `basis`，别让人误以为是预测）：
      ① 首选**同名**（段历史 `source` 里出现本次路径的点名）；
      ② 不足再退**同速度档**（±10%，且只在界面确实选了档时）—— 仍然**全部来自实测**。
    ★ 两条都不足 ⇒ `available=False` + `reason=无实测样本·不给节拍估计`，
      **不出现任何 P50/P90 数字**（钉 `test_no_cycle_time_without_measured_samples`）。
    """
    def _none(tier: str, k: int, why: str) -> Dict[str, Any]:
        return {"available": False, "reason": "无实测样本·不给节拍估计", "n_samples": k,
                "need_samples": MIN_CYCLE_SAMPLES,
                "basis": "本档只接受**本机本次会话的实测段**（不是预测）。匹配口径=「%s」，"
                         "现在只有 %d 段 < %d 段 ⇒ **不外推、不给 P50/P90**。%s"
                         % (tier, k, MIN_CYCLE_SAMPLES, why),
                "p50_ms": None, "p90_ms": None}

    rows = _settled_rows()
    if not rows:
        return _none("同名", 0, "段历史是空的（还没有任何已收尾且到位的下发段）。")
    names = [str(x) for x in (path or [])]
    same_name = [r for r in rows if any(n and n in str(r.get("source") or "") for n in names)]
    pool, tier = same_name, "点名匹配"
    if len(pool) < MIN_CYCLE_SAMPLES and gear is not None:
        near = [r for r in rows if abs(_num(r.get("speed_pct"), -999.0) - gear) <= 10.0]
        if len(near) > len(pool):
            pool, tier = near, "同速度档 ±10%"     # ★ 单个 %：这个串只会被 %s 插值，不再二次格式化
    if len(pool) < MIN_CYCLE_SAMPLES:
        return _none(tier, len(pool), "实测段不够 = 节拍没有依据，硬编一个数就是编故事。")
    vals = [_num(r.get("settle_ms")) for r in pool]
    peaks = [_num(r.get("peak_deg")) for r in pool]
    return {"available": True, "reason": "", "n_samples": len(pool),
            "need_samples": MIN_CYCLE_SAMPLES,
            "basis": "来自本机本次会话的**实测段**（settle_ms 收敛耗时 / peak_deg 峰值残差），"
                     "匹配口径=「%s」；**不是**对未来节拍的预测，也没建模加减速。" % tier,
            "p50_ms": _pct(vals, 0.50), "p90_ms": _pct(vals, 0.90),
            "peak_deg_p90": _pct(peaks, 0.90),
            "speed_pct": sorted({int(_num(r.get("speed_pct"), 0.0)) for r in pool})}


# ---------------------------------------------------------------- 主入口

def _early(status: str, msg: str, t0: float, n_req: int, seed: int, sc: Dict[str, Any],
           failures: List[str], path: Sequence[str], sources: Optional[List[Dict[str, Any]]],
           segs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """跑不出几何结果时的**如实**返回（三态里"没数据/失败"那一支，绝不盖绿章）。"""
    return {
        "ok": False, "status": status, "n": 0, "n_requested": n_req,
        "sample_ms": None, "run_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        "seed": int(seed), "path": [str(x) for x in (path or [])], "segments": [],
        "jitter_sources": sources or [],
        "min_clearance": {"p5": None, "p50": None, "p95": None, "unit": "mm", "bins": [],
                          "worst_state": "unknown"},
        "worst_segment": {"point": None, "segment": None, "p_not_arrived": None,
                          "min_mm_p5": None, "basis": msg},
        "cycle_time": cycle_time_for([s.get("point", "") for s in segs] or list(path or []),
                                     sc.get("speed_gear_pct")),
        "failures": failures + ([msg] if msg else []),
        "scenario": sc, "sweep_samples_per_segment": SWEEP_SAMPLES_PER_SEGMENT,
        "budget_ms": RUN_BUDGET_MS, "not_modelled": list(NOT_MODELLED), "note": NOTE,
        "conclusion": "没有可评估的段（点位库为空 / 未示教 / 扫掠没跑成）—— "
                      "这**不代表安全**，是什么都没算。",
    }


def run(scenario: Optional[Dict[str, Any]] = None, n: Optional[int] = None,
        seed: int = 1, poses: Optional[Dict[str, Sequence[float]]] = None,
        live: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """离线跑一轮参数扫描 + 蒙特卡洛，返回**分布**（不产生任何动作）。

    @param scenario {tray:{dx,dy,dz} mm, face: keep|front|back, order: as_listed|reverse|by_zone,
                    points: [点名…], speed_gear_pct: 仅评估标签, tray_repeat_mm, teach_repeat_deg}
    @param n        样本数；缺省 = `SAMPLE_N_DEFAULT`（依据 = 顶部那条离线计时实测）。
                    ★ 返回体的 `n` = **实际跑完**的轮数（受 `RUN_BUDGET_MS` 约束）。
    @param poses    点位姿态表；None → 只读点位库（`sorting.all_point_poses`）。
    @param live     当前实时 6 关节（度）；None → 不判"在哪一格"，按给定点序逐段评估。
    """
    sc = scenario_of(scenario)
    n_req = int(n) if n else SAMPLE_N_DEFAULT
    n_req = max(1, min(SAMPLE_N_MAX, n_req))
    t0 = time.perf_counter()
    failures: List[str] = []
    names = reorder(sc["points"] or list(DEFAULT_PATH), sc["order"])

    if poses is None:
        try:
            poses = S.all_point_poses()              # 只读点位库（db 延迟导入在 sorting 里）
        except Exception as e:                        # noqa: BLE001 —— 读不到就如实说
            poses = {}
            failures.append("读不到点位库（%s）→ 没有可评估的几何" % str(e)[:60])
    poses = poses or {}

    usable = [x for x in names if x in poses]
    for d in [x for x in names if x not in poses]:
        failures.append("点位「%s」不在点位库（未示教）→ 未纳入本次评估" % d)
    segs = build_segments(usable, poses, sc, live, failures)
    if not segs:
        return _early("no_data", "没有可评估的段（点位缺失或姿态不可用）", t0, n_req, seed,
                      sc, failures, usable, [], [])

    model = jitter_model(sc, segs)
    rng = random.Random(int(seed))

    # 名义（不抖）每段先扫一次：跑不成的段**如实剔除**，不当它安全
    keep: List[Dict[str, Any]] = []
    attempted = [s["to"] for s in segs]      # ★ 一段都没扫成时也要把"算过哪些点"如实带出去
    for s in segs:
        try:
            sw = SE.sweep(s["q0"], s["q1"], n=SWEEP_SAMPLES_PER_SEGMENT)
        except Exception as e:                         # noqa: BLE001
            failures.append("段「%s→%s」包络扫掠失败（%s）→ 该段不计入分布"
                            % (s["from"], s["to"], str(e)[:60]))
            sw = None
        if sw:
            s["nominal"] = sw
            keep.append(s)
    segs = keep
    if not segs:
        return _early("failed", "所有段的包络扫掠都没跑成 → 没有几何结论", t0, n_req, seed,
                      sc, failures, attempted, model["sources"], [])

    per_sample: List[float] = []
    seg_min: List[List[float]] = [[] for _ in segs]
    seg_bad = [0] * len(segs)
    ran = 0
    for _ in range(n_req):
        sample_min = math.inf
        for i, s in enumerate(segs):
            a = _jitter(s["q0"], rng, model)
            b = _jitter(s["q1"], rng, model)
            try:
                sw = SE.sweep(a, b, n=SWEEP_SAMPLES_PER_SEGMENT)
            except Exception as e:                      # noqa: BLE001
                failures.append("抖动样本里段「%s→%s」扫掠失败（%s）"
                                % (s["from"], s["to"], str(e)[:60]))
                continue
            mm = _num(sw.get("worst_mm"), 0.0)
            sample_min = min(sample_min, mm)
            seg_min[i].append(mm)
            if str(sw.get("worst_state") or "") in SE.UNSAFE:
                seg_bad[i] += 1
        if math.isfinite(sample_min):
            per_sample.append(sample_min)
            ran += 1
        if (time.perf_counter() - t0) * 1000.0 > RUN_BUDGET_MS:
            failures.append("整轮墙钟超过预算 %.0fms → 只跑了 %d 轮；界面显示的样本数就是 %d"
                            "（不多报）" % (RUN_BUDGET_MS, ran, ran))
            break

    elapsed = (time.perf_counter() - t0) * 1000.0
    if not per_sample:
        return _early("failed", "一个抖动样本都没跑成 → 没有几何结论", t0, n_req, seed,
                      sc, failures, [s["to"] for s in segs], model["sources"], segs)

    p5s = [_pct(v, 0.05) for v in seg_min]
    worst_i, worst_key = 0, (-1.0, -1e9)
    for i in range(len(segs)):
        key = (seg_bad[i] / float(ran), -(p5s[i] if p5s[i] is not None else 1e9))
        if key > worst_key:
            worst_i, worst_key = i, key
    p_na = round(seg_bad[worst_i] / float(ran), 4)
    worst = {
        "point": segs[worst_i]["to"],
        "segment": "%s → %s" % (segs[worst_i]["from"], segs[worst_i]["to"]),
        "p_not_arrived": p_na,
        "min_mm_p5": p5s[worst_i],
        "basis": "离线统计：这一段的保守包络在 %d 个抖动样本里有 %d 个进入 danger/hit "
                 "（**几何风险比例，不是控制器实测到位率**；时间量一律不在这里推断）"
                 % (ran, seg_bad[worst_i]),
    }
    mc = {"p5": _pct(per_sample, 0.05), "p50": _pct(per_sample, 0.50),
          "p95": _pct(per_sample, 0.95), "unit": "mm", "bins": _bins(per_sample),
          "worst_state": _grade_of(_pct(per_sample, 0.05))}
    out = {
        "ok": True,
        "status": "ok" if not failures else "partial",
        "n": ran,                                   # ★ 实际跑完的轮数（界面就显示这个数）
        "n_requested": n_req,
        "sample_ms": round(elapsed / ran, 3),
        "run_ms": round(elapsed, 1),
        "seed": int(seed),
        "path": [s["to"] for s in segs],
        "segments": [{"i": s["i"], "from": s["from"], "to": s["to"],
                      "nominal_mm": _num(s["nominal"].get("worst_mm")),
                      "nominal_state": s["nominal"].get("worst_state"),
                      "p5_mm": p5s[i],
                      "p50_mm": _pct(seg_min[i], 0.50),
                      "p_not_arrived": round(seg_bad[i] / float(ran), 4)}
                     for i, s in enumerate(segs)],
        "jitter_sources": model["sources"],
        "jitter_totals": {k: model[k] for k in
                          ("joint_sigma_deg", "tray_sigma_mm", "tray_lever_deg_per_mm",
                           "tcp_radius_mm")},
        "min_clearance": mc,
        "worst_segment": worst,
        "cycle_time": cycle_time_for([s["to"] for s in segs], sc["speed_gear_pct"]),
        "failures": failures,
        "scenario": sc,
        "samples_per_segment": SWEEP_SAMPLES_PER_SEGMENT,
        "sweep_samples_per_segment": SWEEP_SAMPLES_PER_SEGMENT,
        "budget_ms": RUN_BUDGET_MS,
        "not_modelled": model["not_modelled"],
        "note": NOTE,
    }
    out["conclusion"] = conclusion_of(out)
    return out


def _grade_of(p5_mm: Optional[float]) -> str:
    """按**围栏阈值**（config/safety.json，唯一来源）把 P5 间隙折成四级之一。

    ★ 只是为了给一句结论；逐段判据仍然由 `safety_eval.sweep` 给（不在这里另立一套分级）。
    """
    if p5_mm is None or not math.isfinite(p5_mm):
        return "unknown"
    th = zone_thresholds_mm()
    if p5_mm <= 0.0:
        return "hit"
    if p5_mm <= th["danger"]:
        return "danger"
    if p5_mm <= th["warn"]:
        return "warn"
    return "safe"


_TH_CACHE: Dict[str, Dict[str, float]] = {}


def zone_thresholds_mm() -> Dict[str, float]:
    """把围栏的**比例阈值**折成 mm（基准与阈值都取自 config/safety.json，不自己写一份）。"""
    if "mm" in _TH_CACHE:
        return _TH_CACHE["mm"]
    warn, danger, basis_m, th = 246.0, 82.0, 0.82, {"warn": 0.3, "danger": 0.1}
    try:
        cfg = SE.load_safety()
        zones = [z for z in (cfg.get("zones") or []) if z and z.get("enabled") is not False]
        if zones:
            basis_m = max(basis_m, max(SE.basis_of(z) for z in zones))
            th = zones[0].get("thresholds") or th
    except Exception as e:                                 # noqa: BLE001
        log.debug("whatif 读围栏阈值失败（用出厂兜底）: %s", e)
    out = {"warn": round(basis_m * 1000.0 * _num(th.get("warn"), 0.3), 1),
           "danger": round(basis_m * 1000.0 * _num(th.get("danger"), 0.1), 1),
           "basis_mm": round(basis_m * 1000.0, 1)}
    _TH_CACHE["mm"] = out
    return out


def conclusion_of(res: Dict[str, Any]) -> str:
    """一句结论（界面原文显示，前端不重算）。**只说几何与实测，不说时间预测**。"""
    if not res.get("ok"):
        return res.get("conclusion") or "本轮没跑出结果（见失败原因）—— 这不代表安全。"
    mc = res.get("min_clearance") or {}
    sc = res.get("scenario") or {}
    tray = sc.get("tray") or {}
    parts: List[str] = []
    dx, dy, dz = _num(tray.get("dx_mm")), _num(tray.get("dy_mm")), _num(tray.get("dz_mm"))
    if dx or dy or dz:
        parts.append("托盘偏移 %+g/%+g/%+gmm" % (dx, dy, dz))
    if sc.get("face") in ("front", "back"):
        parts.append("面策略=%s" % sc["face"])
    if sc.get("order") in ("reverse", "by_zone"):
        parts.append("次序=%s" % sc["order"])
    if sc.get("speed_gear_pct") is not None:
        parts.append("速度档=%s%%（仅评估标签，不写 40103）" % sc["speed_gear_pct"])
    head = " · ".join(parts) or "原样基线"
    st = str(mc.get("worst_state") or "unknown")
    p5 = mc.get("p5")
    p5s = "P5=%s" % ("—（没算出来）" if p5 is None else "%gmm" % p5)
    return "%s：最小间隙 %s（%s）—— %s。样本 n=%s。%s" % (
        head, p5s, STATE_LABEL.get(st, "判不了"), STATE_TAIL.get(st, "判不了 → 不假装结论"),
        res.get("n"), "离线统计估计 · 非认证 · 无 ETA")
