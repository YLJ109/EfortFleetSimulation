# -*- coding: utf-8 -*-
"""现场图像回放回归：拿 captures/vision 下的**真实相机图**，用生产路径跑检测，
统计红/绿/蓝三色的检出率与失败原因。

为什么必须有这个工具：
  tools/vision_color_test.py 用的是**自己合成**的图（纯色矩形），它永远能过；
  而现场的方块是**相机拍的**——透视斜视、边缘模糊、反光、光照不均，
  填充率天然低于理想值。上一轮把 fill_min 定成 0.72 就是只看合成图定出来的，
  现场实测 fill 只有 0.68~0.75 → 正好卡在门槛两侧 → "有时能检测、有时检测不到"。
  ★ 结论：判色阈值只能拿**现场图**定，合成图只能测逻辑。

跑法：
  <相机 venv>\\Scripts\\python.exe camera\\tools\\vision_replay_test.py [每色张数]
"""
from __future__ import annotations

import os
import sys
from collections import Counter

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vision_color import VisionEngine  # noqa: E402

CAP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "captures", "vision")


def imread_u(path: str):
    """★ Windows 中文路径：cv2.imread 会返回 None，必须走 imdecode。"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def newest(per_color: int):
    """按时间倒序取每色最新的 per_color 张（最新 = 最贴近现场当前光照）。"""
    if not os.path.isdir(CAP_DIR):
        return []
    names = sorted(os.listdir(CAP_DIR), reverse=True)
    out = {}
    for n in names:
        p = os.path.join(CAP_DIR, n, "full.jpg")
        if not os.path.isfile(p):
            continue
        for c in ("红", "绿", "蓝"):
            if n.endswith("_" + c) and len(out.setdefault(c, [])) < per_color:
                out[c].append((n, p))
    return out


def main() -> int:
    per_color = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    groups = newest(per_color)
    if not groups:
        print("没有现场图像：%s" % CAP_DIR)
        return 2

    print("== 现场图像回放（生产路径：color_only=True, fill_min 取默认） ==")
    print("目录：%s" % CAP_DIR)
    print("")
    print("★ 判据说明：一张现场图里**可能同时有多个颜色的物体**（比如红块和蓝块同框），")
    print("  生产链路 `pick_best` 只报**面积最大**的那个（'只报最主要目标'）—— 所以这里")
    print("  分两个指标看，别把'报了更大的另一个颜色'误读成'判错色'：")
    print("    · 能识别   = 期望颜色出现在**任一**候选里（说明该色能认出来）")
    print("    · 主目标符 = 面积最大的候选 == 期望颜色（生产实际会采用的那个）")
    print("")

    engines = {}       # 每色一个独立引擎（复刻"连续帧"语义：tracker 需要多帧）
    problems = []
    for want in ("红", "绿", "蓝"):
        items = groups.get(want) or []
        if not items:
            print("[%s] 无现场图，跳过" % want)
            continue
        eng = engines.setdefault(want, VisionEngine(color_only=True))
        hit = 0            # 期望颜色出现在任一候选里
        top_ok = 0         # 最大候选就是期望颜色
        miss_reasons = Counter()
        details = []
        for name, path in items:
            img = imread_u(path)
            if img is None:
                miss_reasons["图像解不开"] += 1
                continue
            # ★ 同一张图喂两遍：第 1 遍建 tracker 基准，第 2 遍才是"稳定触发"语义
            eng.detect(img, record_boxes=True)
            d = eng.detect(img, record_boxes=True)
            res = d.get("results") or []
            names_got = [r["color"]["name"] for r in res]
            top = names_got[0] if names_got else ""
            if want in names_got:
                hit += 1
                c = res[names_got.index(want)]["color"]
                mark = "主" if top == want else "副"
                if top == want:
                    top_ok += 1
                details.append("%s ✓[%s]%s fill=%.2f conf=%.2f %s"
                               % (name[-20:], mark, want, c.get("fill", 0), c.get("conf", 0),
                                  ("（%d 个候选，报了更大的 %s）" % (len(res), top))
                                  if top != want else ""))
            else:
                why = (res[0]["color"].get("reason") if res else "无候选（形状/面积门槛拦掉）")
                miss_reasons[why or "判成别的颜色"] += 1
                details.append("%s ✗ got=%s %s" % (name[-20:], names_got or "[]", why))
        ok_rate = 100.0 * hit / max(1, len(items))
        print("[%s] 能识别 %d/%d（%.0f%%）｜主目标符合 %d/%d"
              % (want, hit, len(items), ok_rate, top_ok, len(items)))
        for t in details[:12]:
            print("     " + t)
        if len(details) > 12:
            print("     …（其余 %d 张略）" % (len(details) - 12))
        if miss_reasons:
            for r, n in miss_reasons.most_common():
                print("     失败原因 ×%d: %s" % (n, r))
            problems.append((want, hit, len(items), miss_reasons))
        print("")

    print("== 结论 ==")
    if not problems:
        print("  红/绿/蓝三色全部检出 ✓")
        return 0
    for want, hit, total, reasons in problems:
        print("  ✗ %s：%d/%d 检出；原因 %s" % (want, hit, total, dict(reasons)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
