# -*- coding: utf-8 -*-
"""端到端判色回归：**用现场真图** 走完整生产链路（抓 3 帧 → 服务端阈值筛 → 后端投票）。

与 vision_color_test.py 的分工：
  · vision_color_test.py = 合成图，测**算法逻辑**（旋转不变、门槛开关、边界…）；
  · 本脚本            = 现场图，测**整条链路能不能真的把三色判出来**（阈值/置信度/投票）。

★ 为什么必须分开（2026-10-08 现场事故的教训）：
  `fill_min=0.72` 与 `conf=min(hsv_conf,lab_conf)` 这两处**都能通过合成图测试**
  （合成图 fill≈0.95+、Lab 与色卡几乎一样），却在现场全军覆没：
    · 现场真矩形 fill 只有 0.67~0.75 → 被判"非矩形"；
    · 现场光照下 Lab ΔE 26~34 → lab_conf=0 → conf=0 → 被阈值挡掉。
  真机日志实证：`检测不到（0.17 低于阈值 0.28；0.25 低于阈值 0.28；0.19 …）`。
  → **判色阈值只能拿现场图定**，所以这个脚本必须常跑。

跑法：
  <相机 venv>\\Scripts\\python.exe camera\\tools\\vision_e2e_replay.py [每色张数]
  # 后端那半段（vote_color）需要 backend 的依赖；装不上就只跑视觉服务那半段（会自动降级）
"""
from __future__ import annotations

import os
import sys
from collections import Counter

import cv2
import numpy as np

CAM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(CAM)
sys.path.insert(0, CAM)
from vision_color import VisionEngine, pick_best, explain_no_candidate  # noqa: E402

try:
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    from app.services import sorting as S
    _BACKEND_OK = True
except Exception as _e:                                          # noqa: BLE001
    S = None
    _BACKEND_OK = False
    _BACKEND_ERR = str(_e)

CAP_DIR = os.path.join(CAM, "captures", "vision")
MIN_CONF = 0.28        # 服务端阈值（现场 .env EFORT_CAM_COLOR_CONF）
VOTE_CONF = 0.30       # 后端投票阈值（/sorting/start 默认）


def imread_u(path: str):
    """★ Windows 中文路径：cv2.imread 返回 None，必须 imdecode。"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def newest(per_color: int):
    out = {}
    if not os.path.isdir(CAP_DIR):
        return out
    for n in sorted(os.listdir(CAP_DIR), reverse=True):
        p = os.path.join(CAP_DIR, n, "full.jpg")
        if not os.path.isfile(p):
            continue
        for c in ("红", "绿", "蓝"):
            if n.endswith("_" + c) and len(out.setdefault(c, [])) < per_color:
                out[c].append((n, p))
    return out


def main() -> int:
    per_color = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    groups = newest(per_color)
    if not groups:
        print("没有现场图像：%s" % CAP_DIR)
        return 2
    print("== 端到端判色回放（现场真图 → 抓3帧 → 阈值筛 → 投票） ==")
    print("服务端阈值 %.2f ｜ 投票阈值 %.2f ｜ 后端链路 %s"
          % (MIN_CONF, VOTE_CONF, "可用" if _BACKEND_OK else "不可用（跳过投票段）"))
    print("")
    bad = []
    for want in ("红", "绿", "蓝"):
        items = groups.get(want) or []
        if not items:
            continue
        eng = VisionEngine(color_only=True)
        hit = 0
        reject = 0          # 合理拒绝（物体超出画面：本来就不该判色）
        miss = Counter()
        for name, path in items:
            img = imread_u(path)
            if img is None:
                miss["图像解不开"] += 1
                continue
            frames = []
            last_mask = None
            for _ in range(3):
                d = eng.detect(img)
                last_mask = d.get("mask")
                best = pick_best(d["results"], MIN_CONF)
                frames.append({"ok": True, "results": best})
            if _BACKEND_OK:
                v = S.vote_color(frames, VOTE_CONF)
                got, ok = v.get("color"), v.get("ok")
                why = v.get("reason") or ""
            else:
                # 后端不可用：退化成"3 帧里最大候选的颜色是否 == 期望"
                got = frames[0]["results"][0]["color"]["name"] if frames[0]["results"] else ""
                ok = got == want
                why = ""
            if ok and got == want:
                hit += 1
                continue
            # ★★ 分清"合理拒绝"与"真漏检"（2026-10-08）：
            #   现场有一批图是"物体怼到镜头前、上下都超出画面"（面积 16~21 万 px，
            #   是正常工作距离 5 千 px 的 40 倍）—— 那时**根本看不到完整方块**，
            #   拒绝判色是正确的，不该算进"检测率不达标"里（否则会逼着人把阈值调烂）。
            exp = ""
            if last_mask is not None:
                try:
                    exp = explain_no_candidate(last_mask)
                except Exception:            # noqa: BLE001
                    exp = ""
            if exp.startswith("物体超出画面"):
                reject += 1
                continue
            miss[(exp or why or "判成 %r" % got)[:70]] += 1
        real_total = max(1, len(items) - reject)
        rate = 100.0 * hit / real_total
        mark = "✓" if rate >= 80 else "✗"
        extra = "，另 %d 张物体超出画面（合理拒绝）" % reject if reject else ""
        print("%s [%s] 判定正确 %d/%d（%.0f%%）%s"
              % (mark, want, hit, real_total, rate, extra))
        for r, n in miss.most_common(4):
            print("      失败 ×%d: %s" % (n, r))
        if rate < 80:
            bad.append(want)
    print("")
    if bad:
        print("== 结论：%s 未达标（<80%%）—— 先看上面的失败原因，再核 DEFAULT_THR ==" % "/".join(bad))
        print("   常见真因：物体超出画面被裁切（贴上/下边）→ 这不是阈值问题，是摆放问题。")
        return 1
    print("== 结论：红/绿/蓝三色全部达标 ✓ ==")
    print("   （个别失败若显示「超出画面」，属正确的拒绝：物体没完整进画面，判色无意义）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
