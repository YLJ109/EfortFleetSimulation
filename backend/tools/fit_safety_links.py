# -*- coding: utf-8 -*-
"""离线标定：用**官方数模真值**反解 `config/robot.yaml: safety.links` 的胶囊半径。

为什么不能拍脑袋给半径（第六轮的实测结论）
------------------------------------------
原先手写的 6 段半径（130/115/95/75/62/50mm）在**零位**算出的 J1 子树最低点是
**261mm**，而官方 GLB 逐顶点量出来的真实最低点是 **191.5mm** —— 猜的包络比真实本体
**更高**，等于把"转座底缘快贴台面"说成"还早着呢"，是**假安全**。
所以半径必须由数模真值反解，并由 `tests/test_safety_envelope.py` 每个姿态钉住
"服务端包络 ≤ 数模真值"（永远保守）。

输入  `frontend/tools/fixtures/mesh_lowpoints.json`（由 `frontend/tools/dump_mesh_lowpoints.mjs` 生成）
输出  打印一段可直接粘进 `config/robot.yaml` 的 `links:` 列表

运行： cd backend && .venv\\Scripts\\python.exe tools\\fit_safety_links.py
★ 只读：不连控制器、不写库、不改配置。
"""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))          # backend/tools
ROOT = os.path.dirname(HERE)                                # backend（`app` 的父目录）
sys.path.insert(0, ROOT)                                    # 让 `app` 可导入
FIXTURE = os.path.join(os.path.dirname(ROOT), "frontend", "tools",
                       "fixtures", "mesh_lowpoints.json")

from app.services.kinematics import forward_kinematics   # noqa: E402
from app.services.safety_eval import scene_from_base_mm   # noqa: E402

# 帧索引：frames[0]=基座, frames[i]=第 i 个 DH 帧（= 第 i+1 轴原点）
# class k 的刚体挂在第 k 轴的转动群上 ⇒ 它相对 frames[k-1..6] 都是固定的
# ⇒ 候选胶囊段的起点 a 必须 ≥ k-1，否则该段会随别的轴转走、包不住这个刚体。
N_FRAMES = 7

_FK_CACHE: Dict[Tuple[float, ...], list] = {}


def _fk(q) -> list:
    """姿态 → DH 帧（拟合要反复算同一批姿态，缓存一次即可）。"""
    key = tuple(round(float(v), 3) for v in q)
    fr = _FK_CACHE.get(key)
    if fr is None:
        fr = forward_kinematics(list(key))["frames"]
        _FK_CACHE[key] = fr
    return fr


def _scene_mm(frames, idx: int) -> Tuple[float, float, float]:
    """DH 帧原点 → 场景(mm, lift=0)。与后端 capsules 同式（只是不除 1000、不加 lift）。"""
    p = frames[idx][:3, 3]
    x, y, z = scene_from_base_mm(float(p[0]), float(p[1]), float(p[2]), 0.0)
    return x * 1000.0, y * 1000.0, z * 1000.0


def required_r(seg: Tuple[int, int], cls: int, rows) -> float:
    """把 class 刚体在**所有采样姿态**都包住所需的最小半径(mm)。

    包住 = 三个方向都包住：
      · 竖直（只算 from>=1，即参与地面净空的那批）：胶囊最低点 ≤ 刚体最低点
      · 水平 X / Z：胶囊外接 ≥ 刚体外接
    水平方向直接决定围栏余量（`aabb_of` 就是用端点 ±r 取外接），所以也一起钉。
    """
    a, b = seg
    need = 0.0
    for r in rows:
        box = (r["bodies"] or {}).get(str(cls))
        if not box:
            continue
        bminx, bmaxx, bminy, bmaxy, bminz, bmaxz = box
        fr = _fk(r["q"])
        p0 = _scene_mm(fr, a)
        p1 = _scene_mm(fr, b)
        cands = [
            # 胶囊外接必须 ≥ 刚体外接（水平两个方向各两侧；竖直只在参与地面净空的段上收紧）
            bmaxx - max(p0[0], p1[0]),
            min(p0[0], p1[0]) - bminx,
            bmaxz - max(p0[2], p1[2]),
            min(p0[2], p1[2]) - bminz,
        ]
        if a >= 1:      # 只有 from>=1 的段参与地面净空，竖直方向只对它收紧
            cands.append(min(p0[1], p1[1]) - bminy)
        need = max(need, max(cands))
    return need


def _vreq(seg: Tuple[int, int], cls: int, rows) -> float:
    """只算"竖直包住"所需半径：胶囊最低点 ≤ 刚体最低点（= 后端 ground 的口径）。"""
    a, b = seg
    need = 0.0
    for r in rows:
        box = (r["bodies"] or {}).get(str(cls))
        if not box:
            continue
        p0, p1 = _scene_mm(_fk(r["q"]), a), _scene_mm(_fk(r["q"]), b)
        need = max(need, min(p0[1], p1[1]) - box[2])
    return need


def fit(classes: List[int], rows) -> List[Dict[str, float]]:
    """逐刚体挑"最贴合的胶囊段"，再按段合并半径。

    ★★ 第二轮（地面口径）：`evaluate()` 只把 **from_frame ≥ 1** 的胶囊算进地面净空
       （与前端 getGroundRoot() 排除贴地底座同语义）。所以"底座上方那段"（class 1 转座）
       若被分到 a=0 的段上，它的真实最低点就**没有任何参与地面评估的胶囊盖得住** ——
       实测差 14.5mm（数模 191.5 vs 包络 206），方向是"说高了"= 假安全。
       因此这里给每个 class≥1 的刚体**再补一条 from≥1 的竖直覆盖**（优先复用已有的段，
       不为它新增胶囊）。
    """
    best: Dict[Tuple[int, int], Dict] = {}
    for cls in classes:
        opts = sorted((required_r((a, b), cls, rows), a, b)
                      for a in range(max(0, cls - 1), N_FRAMES - 1)
                      for b in range(a + 1, N_FRAMES))
        r, a, b = opts[0]
        cell = best.setdefault((a, b), {"r": 0.0, "classes": []})
        cell["r"] = max(cell["r"], r)
        cell["classes"].append(cls)
        print(f"  class {cls} → frames[{a}]→frames[{b}]  r={r:.1f}mm"
              + (f"   次优={opts[1][0]:.1f}mm({opts[1][1]},{opts[1][2]})" if len(opts) > 1 else ""))

    for cls in [c for c in classes if c >= 1]:
        if any(a >= 1 and cls in v["classes"] for (a, b), v in best.items()):
            continue                                  # 已有 from≥1 的段覆盖它
        pool = [seg for seg in best if seg[0] >= 1 and seg[0] >= cls - 1]
        if not pool:
            pool = [(a, b) for a in range(max(1, cls - 1), N_FRAMES - 1)
                    for b in range(a + 1, N_FRAMES)]
        # 代价 = 给该段**多加**的半径（已有半径够大就不加）
        _, (a, b) = min((max(0.0, _vreq(seg, cls, rows) - best[seg]["r"])
                             if seg in best else _vreq(seg, cls, rows), seg) for seg in pool)
        need = _vreq((a, b), cls, rows)
        cell = best.setdefault((a, b), {"r": 0.0, "classes": []})
        cell["r"] = max(cell["r"], need)
        cell["classes"].append(cls)
        print(f"  class {cls} 的地面覆盖补到 frames[{a}]→frames[{b}]（原段 from=0 不参与地面净空）"
              f" → r={need:.1f}mm")

    return [{"from_frame": a, "to_frame": b, "r_mm": math.ceil(v["r"] / 5) * 5,
             "covers": sorted(v["classes"])} for (a, b), v in sorted(best.items())]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # Windows 中文控制台默认 GBK
    except Exception:
        pass
    if not os.path.exists(FIXTURE):
        print(f"缺少真值文件：{FIXTURE}\n先跑： cd frontend && node tools/dump_mesh_lowpoints.mjs")
        return 1
    data = json.load(open(FIXTURE, "r", encoding="utf-8"))
    rows = data["rows"]
    classes = sorted({int(k) for r in rows for k in (r["bodies"] or {}).keys()})
    print(f"真值姿态 {len(rows)} 个，刚体 class={classes}\n")
    links = fit(classes, rows)
    print("\n# 粘进 config/robot.yaml 的 safety.links（单位 mm，已上取整到 5mm）")
    for link in links:
        print(f'  {{from_frame: {link["from_frame"]}, to_frame: {link["to_frame"]}, '
              f'r_mm: {link["r_mm"]}}}   # 覆盖 class {link["covers"]}')

    # 自检：合并后的包络是否处处 ≤ 真值（竖直）且外接 ≥ 真值（水平）
    worst_low = worst_x = worst_z = 0.0
    for r in rows:
        fr = forward_kinematics(r["q"])["frames"]
        low = math.inf
        mx = -math.inf
        mn_x = math.inf
        mz = -math.inf
        mn_z = math.inf
        for link in links:
            a, b, rad = link["from_frame"], link["to_frame"], link["r_mm"]
            p0, p1 = _scene_mm(fr, a), _scene_mm(fr, b)
            if a >= 1:
                low = min(low, min(p0[1], p1[1]) - rad)
            mx = max(mx, p0[0] + rad, p1[0] + rad)
            mn_x = min(mn_x, p0[0] - rad, p1[0] - rad)
            mz = max(mz, p0[2] + rad, p1[2] + rad)
            mn_z = min(mn_z, p0[2] - rad, p1[2] - rad)
        truth = list((r["bodies"] or {}).values())
        t_minx = min(x[0] for x in truth)
        t_maxx = max(x[1] for x in truth)
        t_minz = min(x[4] for x in truth)
        t_maxz = max(x[5] for x in truth)
        t_low_above_base = min([x[2] for k, x in (r["bodies"] or {}).items() if int(k) >= 1],
                               default=math.inf)
        worst_low = max(worst_low, low - t_low_above_base)       # >0 = 不保守！
        worst_x = max(worst_x, t_maxx - mx, mn_x - t_minx)       # >0 = 包不住
        worst_z = max(worst_z, t_maxz - mz, mn_z - t_minz)
    print(f"\n保守性自检（≤0 才算通过）：竖直最坏超出真值 {worst_low:.1f}mm / "
          f"水平 X 最坏漏包 {worst_x:.1f}mm / Z 最坏漏包 {worst_z:.1f}mm")
    print("★ 竖直为负 = 服务端比真值更低（保守，会在真值之前报警）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
