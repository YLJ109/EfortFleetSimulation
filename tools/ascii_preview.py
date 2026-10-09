# -*- coding: utf-8 -*-
"""把一张图打成 ASCII 预览（64 列），用于"没有视觉能力时看形状"。

跑法： <相机 venv>\\Scripts\\python.exe tools\\ascii_preview.py <图片> [cols]
"""
from __future__ import annotations
import os, sys
import cv2
import numpy as np


def main() -> int:
    path = sys.argv[1]
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 64
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        print("解不开：%s" % path)
        return 1
    h, w = img.shape
    rows = max(6, int(cols * h / w * 0.5))
    small = cv2.resize(img, (cols, rows), interpolation=cv2.INTER_AREA)
    ramp = " .:-=+*#%@"
    print("== %s  %dx%d → %dx%d ==" % (os.path.basename(path), w, h, cols, rows))
    for r in small:
        print("".join(ramp[min(9, int(v) * 10 // 256)] for v in r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
