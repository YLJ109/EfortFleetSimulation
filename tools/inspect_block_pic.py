# -*- coding: utf-8 -*-
"""量一张实物照片：列出主要物体（位置/尺寸/长宽比/颜色），并给出"方形还是长方形"结论。

跑法： <相机 venv>\\Scripts\\python.exe tools\\inspect_block_pic.py <图片>
"""
from __future__ import annotations
import os, sys
import cv2
import numpy as np


def imread_u(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def hue_name(hsv):
    h, s, v = int(hsv[0]), int(hsv[1]), int(hsv[2])
    if s < 45:
        return "灰/白/黑(低饱和)"
    if h < 10 or h >= 160:
        return "红"
    if h < 22:
        return "橙/棕"
    if h < 35:
        return "黄"
    if h < 78:
        return "绿"
    if h < 100:
        return "青"
    if h < 130:
        return "蓝"
    return "紫/粉"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else r"C:/Users/FIT/Desktop/方块.jpg"
    img = imread_u(path)
    if img is None:
        print("解不开图片：%s" % path)
        return 1
    H, W = img.shape[:2]
    print("图片 %s  %dx%d" % (os.path.basename(path), W, H))

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    best = []
    for name, m in (("亮前景", th), ("暗前景", 255 - th)):
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:3]:
            a = cv2.contourArea(c)
            if a < (H * W) * 0.01:
                continue
            (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
            ar = max(rw, rh) / max(1e-6, min(rw, rh))
            box = cv2.boxPoints(((cx, cy), (rw, rh), ang)).astype(int)
            mask = np.zeros((H, W), np.uint8)
            cv2.drawContours(mask, [box], 0, 255, -1)
            b, g, r = [float(v) for v in cv2.mean(img, mask=mask)[:3]]
            hsv = cv2.cvtColor(np.uint8([[[int(b), int(g), int(r)]]]), cv2.COLOR_BGR2HSV)[0][0]
            best.append({
                "src": name, "area": a, "ar": ar, "wh": (rw, rh), "ang": ang,
                "cxy": (cx, cy), "rgb": (r, g, b), "hsv": hsv,
                "cut": (rw > W * 0.97 or rh > H * 0.97),
            })
    if not best:
        print("  没找到占比 >1% 的物体（可能是特写/纯背景图）")
        return 0
    best.sort(key=lambda d: -d["area"])
    for i, d in enumerate(best[:4], 1):
        print("  #%d [%s] 面积占比=%.1f%%  外接矩形=%.0fx%.0f px  旋转=%.0f°  长宽比=%.2f  色=%s(RGB %.0f,%.0f,%.0f)%s"
              % (i, d["src"], 100 * d["area"] / (H * W), d["wh"][0], d["wh"][1], d["ang"],
                 d["ar"], hue_name(d["hsv"]), d["rgb"][0], d["rgb"][1], d["rgb"][2],
                 "  ★贴边(可能被裁)" if d["cut"] else ""))
    top = best[0]
    print("")
    print("  最大物体长宽比 = %.2f → %s"
          % (top["ar"], "接近方形" if top["ar"] <= 1.25 else
             ("轻度长方形" if top["ar"] <= 1.6 else "明显长方形")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
