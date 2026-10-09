# -*- coding: utf-8 -*-
"""用**真机存档图**实测：被判出来的方块长宽比/填充率分布（给"只认方形"定阈值用）。

★ 为什么要有这个：改 aspect/fill 阈值前，先用现场真实抓图量一遍，
  否则"凭感觉调阈值"会把现场本来就检出的方块也滤掉。
★ Windows 中文路径必须用 np.fromfile + imdecode —— cv2.imread 遇非 ASCII 路径直接返回 None。

跑法： <相机 venv>\\Scripts\\python.exe tools\\measure_vision_aspect.py
"""
from __future__ import annotations

import glob
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CAM = os.path.join(os.path.dirname(HERE), "camera")
sys.path.insert(0, CAM)

import vision_color as vc  # noqa: E402

BASE = os.path.join(CAM, "captures", "vision")


def imread_u(path):
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    dirs = sorted(glob.glob(os.path.join(BASE, "*")), key=os.path.getmtime, reverse=True)[:12]
    if not dirs:
        print("没有存档图（%s 为空）—— 先在页面上抓几张再来" % BASE)
        return 0
    print("阈值现状：aspect_max=%.1f  min_fill=%.2f  solidity=%.2f  fill_min=%.2f"
          % (vc.DEFAULT_THR["aspect_max"], vc.DEFAULT_THR["min_fill"],
             vc.DEFAULT_THR["solidity_min"], vc.DEFAULT_THR.get("fill_min", 0)))
    print("")
    rows = []
    for d in dirs:
        f = os.path.join(d, "full.jpg")
        img = imread_u(f) if os.path.isfile(f) else None
        if img is None:
            print("  %-32s 读不到 full.jpg" % os.path.basename(d))
            continue
        res = vc.VisionEngine().detect(img)
        got = res.get("results") or []
        if not got:
            print("  %-32s （无候选）" % os.path.basename(d))
            continue
        for r in got:
            (_cx, _cy), (w, h), _ang = r["rect"]
            ar = max(w, h) / max(1e-6, min(w, h))
            col = r.get("color") or {}
            rows.append(ar)
            print("  %-32s color=%-3s conf=%.3f  wxh=%.0fx%.0f  长宽比=%.2f  fill=%.2f  sol=%.2f"
                  % (os.path.basename(d), col.get("name"), col.get("conf") or 0,
                     w, h, ar, r.get("fill") or 0, r.get("solidity") or 0))
    if rows:
        rows.sort()
        print("")
        print("长宽比实测：n=%d  最小=%.2f  中位=%.2f  最大=%.2f"
              % (len(rows), rows[0], rows[len(rows) // 2], rows[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
