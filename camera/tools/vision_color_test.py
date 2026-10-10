# -*- coding: utf-8 -*-
"""
视觉颜色分拣 · 离线回归（不需要相机在线）。

覆盖：
  A. ΔE2000 数值正确性 —— 用 Sharma 等（CIEDE2000 原始论文）的经典测试对钉死公式；
  B. sRGB→Lab 正确性 —— 红/白/黑的已知值；
  C. 判色可达性 —— 18 类色卡**每一类都能被判回自己**（防止阈值把某类"吞掉"，
     例如无彩色分档曾让"米白"永远选不到）；
  D. 分割与筛选 —— 只认长方形；圆形必须被剔除；
  E. 稳定判定 —— 静止目标连续 N 帧只触发一次；目标离开后能复位；
  F. 光照鲁棒性 —— 轻度明暗变化下判色保持率；
  G. 真实抓拍冒烟 —— captures/ 里有图就跑一遍，确保管线不炸。

运行（相机 venv）：
  D:\\EFORT_Projects\\EFORT_Camera_Python_OpenCv\\venv\\Scripts\\python.exe camera\\tools\\vision_color_test.py
"""
from __future__ import annotations

import glob
import inspect
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vision_color as vc  # noqa: E402

pass_n = 0
fail_n = 0


def ok(name, cond, extra=""):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print("  PASS  " + name)
    else:
        fail_n += 1
        print("  FAIL  " + name + (("   -> " + extra) if extra else ""))


def hex_bgr(h: str):
    r, g, b = vc.hex_to_rgb(h)
    return (b, g, r)


BG = 118          # 18% 中灰背景
W, H = 640, 480


def render(color_bgr, angle=12.0, gain=1.0, noise=0.0, shape="rect"):
    """在中灰背景上画一个物体。gain 只作用于物体（模拟物料受光变化）。"""
    img = np.full((H, W, 3), BG, np.uint8)
    c = tuple(int(max(0, min(255, v * gain))) for v in color_bgr)
    if shape == "rect":
        box = cv2.boxPoints(((W / 2, H / 2), (260, 150), angle)).astype(np.int32)
        cv2.fillPoly(img, [box], c)
    else:  # circle：用于"不该被当成长方形"的反例
        cv2.circle(img, (W // 2, H // 2), 110, c, -1)
    if noise > 0:
        n = np.random.normal(0.0, noise, img.shape)
        img = np.clip(img.astype(np.float32) + n, 0, 255).astype(np.uint8)
    return img


BG_IMG = np.full((H, W, 3), BG, np.uint8)


def main():
    np.random.seed(7)
    cv2.setRNGSeed(7)

    print("== A. CIEDE2000 对照 Sharma 论文测试对 ==")
    # (lab1, lab2, 期望 ΔE00)
    pairs = [
        ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
        ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
        ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
        ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
        ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ]
    for lab1, lab2, expect in pairs:
        got = float(vc.delta_e_2000(np.array(lab1), np.array(lab2)))
        ok("ΔE00(%s, %s) = %.4f" % (lab1, lab2, got),
           abs(got - expect) < 5e-3, "期望 %.4f 实得 %.4f" % (expect, got))

    print("== B. sRGB -> Lab 已知值 ==")
    white = vc.srgb_to_lab(np.array([255.0, 255.0, 255.0]))
    black = vc.srgb_to_lab(np.array([0.0, 0.0, 0.0]))
    red = vc.srgb_to_lab(np.array([255.0, 0.0, 0.0]))
    ok("白 L*≈100", abs(white[0] - 100.0) < 0.05, "L=%.3f" % white[0])
    ok("黑 L*≈0", abs(black[0]) < 0.05, "L=%.3f" % black[0])
    ok("纯红 ≈ (53.24, 80.09, 67.20)",
       abs(red[0] - 53.24) < 0.1 and abs(red[1] - 80.09) < 0.1 and abs(red[2] - 67.20) < 0.1,
       "实得 (%.2f, %.2f, %.2f)" % (red[0], red[1], red[2]))

    print("== C. 七色调色板可达：7 色各自判回自己；结果只出调色板名 ==")
    eng = vc.VisionEngine(background=BG_IMG, use_mog2=False)
    # ★★ 2026-10-10 七色重构：可识别色 = 红/橙/黄/绿/青/蓝/紫（vc.PALETTE_NAMES）。
    #   每张调色板色卡都必须能判回自己（防止某条色相带被阈值吞掉）。
    _pal7 = {"红": "#E02020", "橙": "#FF7A18", "黄": "#FFD400", "绿": "#1FA74A",
             "青": "#00BFC8", "蓝": "#1F4FD8", "紫": "#8A3FC0"}
    reach_bad = []
    for name, hx in _pal7.items():
        r = eng.detect(render(hex_bgr(hx)))
        got = r["results"][0]["color"]["name"] if r["results"] else "<无目标>"
        if got != name:
            reach_bad.append("%s->%s" % (name, got))
    ok("红/橙/黄/绿/青/蓝/紫 七色各自可判回自己", not reach_bad, "错判: " + ", ".join(reach_bad))
    # 全 18 类色卡：低饱和（白/灰/米白…）"<无目标>" 是**预期**；其余必须落进**调色板**
    #   （或 未知/黑），绝不许出现调色板之外的**新类别名**。
    leak_bad = []
    for name, hx in vc.COLOR_CARD.items():
        if name in _pal7:
            continue                       # 七色已在上面测过可达
        img = render(hex_bgr(hx))
        r = eng.detect(img)
        got = r["results"][0]["color"]["name"] if r["results"] else "<无目标>"
        # ★ 白/银灰/灰/米白这类**低饱和**物体在前景掩膜里根本不存在 → "<无目标>" 是预期。
        # ★「黑」= **无彩色**（暗色兜底识别出来的），是**预期**类别名，不算泄漏。
        if got == "<无目标>":
            continue
        if got not in vc.PALETTE_NAMES and got not in ("未知", vc.DARK_NAME):
            leak_bad.append("%s->%s" % (name, got))
    ok("非七色输入不新造类别（只允许落进调色板 / 未知 / 黑）", not leak_bad,
       "泄漏: " + ", ".join(leak_bad[:8]))

    print("== D. 只认长方形（圆必须剔除） ==")
    eng2 = vc.VisionEngine(background=BG_IMG, use_mog2=False)
    rr = eng2.detect(render(hex_bgr("#1F4FD8")))
    ok("矩形被找到", len(rr["results"]) == 1, "找到 %d 个" % len(rr["results"]))
    eng3 = vc.VisionEngine(background=BG_IMG, use_mog2=False)
    rc = eng3.detect(render(hex_bgr("#1F4FD8"), shape="circle"))
    ok("圆形被剔除", len(rc["results"]) == 0, "找到 %d 个" % len(rc["results"]))

    # ★★ 2026-10-10 用户口径："黑色是无效颜色，要能识别到并放回原位"（第 4 色「黑」）。
    #   黑/深色物体在彩色掩膜里是**零像素**（旧行为直接报"检测不到"）——
    #   现在有"暗色兜底"（只在没检到彩色时才启用）：必须给出候选，
    #   且判色为**第 4 色「黑」**、原因写明"无效颜色"（上层据此原路放回原位）。
    eng_black = vc.VisionEngine(use_mog2=False)           # 与相机服务同一条链路(color_only)
    rb = eng_black.detect(render(hex_bgr("#14181C")))     # 近黑
    ok("黑块能被检出（不再报「检测不到」）", len(rb["results"]) == 1,
       "找到 %d 个" % len(rb["results"]))
    if rb["results"]:
        cb = rb["results"][0]["color"]
        ok("黑块判为「无效颜色」（黑 + 原因写明）",
           cb["name"] == vc.DARK_NAME and "无效颜色" in (cb.get("reason") or ""),
           "name=%s reason=%s" % (cb["name"], cb.get("reason")))
        # ★ 关键：conf 必须过上游门槛（camera_service.pick_best / 后端 vote_color），
        #   否则刚检出的候选又被滤掉，现场又变回"检测不到"，本支路等于白加。
        ok("暗色候选 conf 能过上游门槛（>=0.28）",
           float(cb["conf"]) >= 0.28, "conf=%.2f" % float(cb["conf"]))
        ok("pick_best 仍保留暗色候选（只会被当成「黑/无效」，不会是有效颜色）",
           len(vc.pick_best(rb["results"], 0.28)) == 1)
    # 反证：纯背景（无物体）不能靠暗色兜底凭空造出一个候选
    eng_bg = vc.VisionEngine(use_mog2=False)
    ok("纯背景不产生候选（暗色兜底不是无中生有）",
       len(eng_bg.detect(BG_IMG.copy())["results"]) == 0)
    # ★★ 2026-10-10 实测**黑块实物照**后的修正：哑光黑在光下 V≈58~105（不是 20 那种纯黑），
    #   固定阈值 55 会把它切碎成碎块 → 检不出。所以判据改成**相对**："明显暗于画面背景"。
    eng_matte = vc.VisionEngine(use_mog2=False)
    r_matte = eng_matte.detect(render((72, 72, 72)))      # V≈72：实测的黑块面亮度
    ok("哑光黑（V≈72，实测亮度）能被暗色兜底整块检出",
       len(r_matte["results"]) == 1, "找到 %d 个" % len(r_matte["results"]))
    if r_matte["results"]:
        c = r_matte["results"][0]["color"]
        ok("哑光黑也判为「无效颜色」", c["name"] == vc.DARK_NAME and "无效颜色" in (c.get("reason") or ""),
           "%s / %s" % (c["name"], c.get("reason")))
    # ★★ 2026-10-10 修正（用户："现在红色也没有检测到…把黑色识别成蓝色了"）：
    #   ① **不再按面积用暗色候选顶掉彩色候选** —— 画面里有更大暗区（黑料/夹具/暗显示器）时，
    #      彩色候选必须**还在**（C 位背景本就有大块暗矩形，按面积竞争会把真彩料吞掉 = 本次回归根因）；
    #   ② 黑块**带蓝/青色偏**（会混进彩色掩膜、被 hue 投票判成"蓝"）时，因其"暗"（框内多数像素
    #      落在暗色掩膜里）→ 必须**改判为「黑」**，不能报蓝。
    mix = np.full((H, W, 3), BG, np.uint8)
    cv2.rectangle(mix, (60, 120), (300, 340), (32, 32, 224), -1)      # 小红块（料）
    cv2.rectangle(mix, (340, 60), (620, 420), (60, 60, 60), -1)       # 更大暗区块（背景/夹具）
    names_c = [it["color"]["name"] for it in vc.VisionEngine(use_mog2=False).detect(mix)["results"]]
    ok("★ 画面有更大暗区块时，彩色候选仍被保留（不被暗色吞掉）",
       "红" in names_c, "实得: " + str(names_c))
    # 反证：黑块**带蓝偏**（S/V 都够 → 会进彩色掩膜）→ 仍必须判「黑」，不能判「蓝」
    bluish = np.full((H, W, 3), BG, np.uint8)
    # ★★ 2026-10-10 七色重构后口径："黑" = **暗 且 不艳**。此合成块要代表
    #   "哑光黑 + 轻微蓝偏"（真实现象），**不是**一块高饱和深蓝（那块本来就该判蓝）。
    #   (50,42,38) BGR ≈ RGB(38,42,50)：S≈61（够进彩色掩膜）、Lab 色度≈5（消色）→ 必须判黑。
    cv2.rectangle(bluish, (180, 120), (460, 380), (50, 42, 38), -1)
    res_b = vc.VisionEngine(use_mog2=False).detect(bluish)["results"]
    ok("★ 哑光黑带轻微蓝偏 → 仍判「黑」（不被 hue 投票判成蓝）",
       len(res_b) == 1 and res_b[0]["color"]["name"] == vc.DARK_NAME,
       str(res_b[0]["color"]["name"] if res_b else "<无目标>"))
    # ★★ 2026-10-10 新增钉子（本次核心 bug：暗绿被判黑 → 现场"绿色检测不到"）：
    #   亮背景（C 位那种，V=200）+ **暗绿**物体（V=120 低于 dark_val_max=130，但仍高饱和/高色度）
    #   → 必须仍判「绿」。旧规则只看"暗"（_dark_self_frac≥0.5）会把它误判成黑。
    bright_g = np.full((H, W, 3), 200, np.uint8)
    cv2.rectangle(bright_g, (180, 120), (460, 380), (40, 120, 55), -1)   # 暗绿：V 低但"艳"
    res_g = vc.VisionEngine(use_mog2=False).detect(bright_g)["results"]
    ok("★ 亮背景下的暗绿块仍判「绿」（旧规则会误判成黑）",
       len(res_g) == 1 and res_g[0]["color"]["name"] == "绿",
       str(res_g[0]["color"]["name"] if res_g else "<无目标>"))
    # 同场景对照：亮背景 + **哑光黑**（暗 且 不艳）→ 必须判「黑（无效色）」
    bright_k = np.full((H, W, 3), 200, np.uint8)
    cv2.rectangle(bright_k, (180, 120), (460, 380), (48, 44, 40), -1)    # 哑光黑 + 轻微蓝偏
    res_k = vc.VisionEngine(use_mog2=False).detect(bright_k)["results"]
    ok("★ 亮背景下的黑块仍判「黑（无效色）」（暗 且 不艳）",
       len(res_k) == 1 and res_k[0]["color"]["name"] == vc.DARK_NAME,
       str(res_k[0]["color"]["name"] if res_k else "<无目标>"))
    # 反证：黑块**比彩料小** → 仍按彩料报（不能反过来把彩料判成无效颜色）
    sd = np.full((H, W, 3), BG, np.uint8)
    cv2.rectangle(sd, (60, 120), (400, 460), (32, 32, 224), -1)       # 大红块（料）
    cv2.rectangle(sd, (440, 150), (540, 250), (60, 60, 60), -1)       # 小黑块（杂物）
    res_s = vc.VisionEngine(use_mog2=False).detect(sd)["results"]
    ok("反证：黑块比彩料小 → 仍按彩料报（不会把彩料误判成无效颜色）",
       len(res_s) == 1 and res_s[0]["color"]["name"] == "红",
       str(res_s[0]["color"]["name"] if res_s else "<无目标>"))
    # 反证：**整幅都暗**（背景也暗）→ 不启用暗色兜底（宁可不猜，也不把整屏当成物体）
    all_dark = np.full((H, W, 3), 40, np.uint8)
    ok("整幅偏暗时不启用暗色兜底（不会把整屏当成物体）",
       len(vc.VisionEngine(use_mog2=False).detect(all_dark)["results"]) == 0)
    # 反证：三色一个都不能被暗色兜底改动（红块仍须判红）
    eng_r = vc.VisionEngine(use_mog2=False)
    rr2 = eng_r.detect(render(hex_bgr("#E02020")))
    ok("红块仍判红（暗色兜底不介入三色路径）",
       len(rr2["results"]) == 1 and rr2["results"][0]["color"]["name"] == "红",
       str(rr2["results"][0]["color"]["name"] if rr2["results"] else "<无目标>"))

    print("== E. 稳定判定：静止持续触发；离开后复位 ==")
    eng4 = vc.VisionEngine(background=BG_IMG, use_mog2=False)
    frame = render(hex_bgr("#4CAF50"))
    fires = [bool(eng4.detect(frame.copy()).get("fire")) for _ in range(6)]
    # ★ 语义已按实机改为"实时模式"：稳定后**只要目标还在场就持续触发**（帧率节流交给上层
    #   color_min_interval）。旧断言"6 帧恰好 1 次"与实现矛盾，属过期断言，这里更正。
    first = fires.index(True) if any(fires) else -1
    ok("第 1 帧不触发、第 2 帧起持续触发（实时模式，stable_frames=2）",
       first == 1 and all(fires[1:]), "序列=%s" % fires)
    ok("第 3 帧仍在触发", fires[2] is True, "序列=%s" % fires)
    for _ in range(12):                       # 目标离开
        eng4.detect(BG_IMG.copy())
    ok("目标离开后复位为 EMPTY", eng4.tracker.state == "EMPTY",
       "state=%s" % eng4.tracker.state)
    again = eng4.detect(frame.copy())
    ok("新物体可再次触发计数", again["tracker"]["hits"] >= 1, str(again["tracker"]))

    print("== F. 光照鲁棒性：红/绿/蓝在明暗 ±12% 下保持率 ==")
    keep, total, changed = 0, 0, []
    # 只评估三色目标：橙/黄/青等非目标在光变下改判"未知"属预期，不参与保持率
    for name, hx in vc.RGB_CARD.items():
        bgr = hex_bgr(hx)
        for gain in (0.88, 0.94, 1.0, 1.06):
            e = vc.VisionEngine(background=BG_IMG, use_mog2=False)
            r = e.detect(render(bgr, gain=gain, noise=3.0))
            got = r["results"][0]["color"]["name"] if r["results"] else "<无目标>"
            total += 1
            if got == name:
                keep += 1
            elif gain != 1.0:
                changed.append("%s@%.2f->%s" % (name, gain, got))
    rate = keep / float(total)
    ok("三色保持率 >= 78%%", rate >= 0.78, "保持率=%.1f%% (%d/%d)" % (rate * 100, keep, total))
    if changed:
        print("        （受光变化后改名，属预期内的相邻色混淆）: " + ", ".join(changed[:12]))

    print("== G. 真实抓拍冒烟 ==")
    caps = sorted(glob.glob(os.path.join(os.path.dirname(HERE), "captures", "*.jpg")))
    if not caps:
        print("  SKIP  无 captures 图片")
    for p in caps[:3]:
        img = cv2.imread(p)
        if img is None:
            ok("读取 %s" % os.path.basename(p), False, "cv2.imread 返回 None")
            continue
        try:
            e = vc.VisionEngine(use_mog2=True)     # 无背景 -> 走 MOG2
            for _ in range(4):
                r = e.detect(img)
            ok("跑通 %s (MOG2)" % os.path.basename(p), "results" in r)
            print("        检出 %d 个候选; 画面 %dx%d" % (len(r["results"]), img.shape[1], img.shape[0]))
        except Exception as ex:                     # noqa
            ok("跑通 %s" % os.path.basename(p), False, repr(ex))

    # ------------------------------------------------------------------
    # H. 定向钉子：钉死两个"看着没问题但一定会炸"的历史缺陷，防止被改回去。
    #    ★ 这类断言必须落在最底层函数上（而不是只靠端到端），这样才能在
    #      有人把绝对阈值写回来时立刻红。
    # ------------------------------------------------------------------
    print("== H. 定向钉子（历史缺陷防回退） ==")

    # H1. 饱和物料（某通道 = 255）不得被判成"未知"
    #     旧版 dominant_lab 用绝对阈值 V < 0.96*255 剔高光 → 整块清空。
    sat_bad = []
    for nm in ("橙", "黄", "天蓝", "粉", "白"):
        bgr = hex_bgr(vc.COLOR_CARD[nm])
        assert max(bgr) == 255, nm            # 前提：这几种色卡确实有通道饱和
        m = vc.diff_mask(render(bgr), BG_IMG)
        d = vc.dominant_lab(render(bgr), m)
        if not d["ok"]:
            sat_bad.append(nm)
    ok("饱和物料不被高光判据清空（dominant_lab.ok）", not sat_bad,
       "被清空: " + ", ".join(sat_bad))

    # H2. 纯黑物料不得被判成"未知"
    #     旧版用绝对阈值 L > 10 剔暗部 → 黑(L*≈8) 被整块清空。
    blk = render(hex_bgr(vc.COLOR_CARD["黑"]))
    dblk = vc.dominant_lab(blk, vc.diff_mask(blk, BG_IMG))
    ok("纯黑物料不被暗部判据清空（dominant_lab.ok）", bool(dblk["ok"]),
       "valid=%s" % dblk.get("valid"))
    ok("纯黑物料 L* 落在无彩色黑档(<34)", float(dblk["lab"][0]) < vc.L_GRAY,
       "L*=%.2f" % float(dblk["lab"][0]))

    # H3. 背景差分的通道灵敏度：灰物体压在中灰背景上，灰度差很小，
    #     必须靠"逐通道最大差"保留下来（旧版先转灰度会把它平均掉）。
    g_obj = tuple(int(v * 0.94) for v in hex_bgr(vc.COLOR_CARD["灰"]))
    g_img = np.full((H, W, 3), BG, np.uint8)
    box = cv2.boxPoints(((W / 2, H / 2), (260, 150), 0)).astype(np.int32)
    cv2.fillPoly(g_img, [box], g_obj)
    diff = cv2.absdiff(g_img, BG_IMG)
    max_ch = int(diff.max(axis=2).max())
    gray_avg = int(cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY).max())
    ok("背景差分用逐通道最大差（灵敏度高于灰度平均）",
       max_ch > 12 and vc.diff_mask(g_img, BG_IMG).any(),
       "通道最大差=%d 灰度差=%d" % (max_ch, gray_avg))

    # H4. 无彩色档位边界必须是对称的中点，且米白的色度留有余量
    #     （旧版白/银灰边界写成 95.5 而非中点 91.1；CHROMA_CUT=12 离米白只差 1.18）。
    mid_ws = (vc.CARD_LAB["白"][0] + vc.CARD_LAB["银灰"][0]) / 2.0
    ok("白/银灰亮度边界 = 两卡中点", abs(vc.L_WHITE - mid_ws) < 1.5,
       "L_WHITE=%.1f 中点=%.2f" % (vc.L_WHITE, mid_ws))
    min_margin = min(
        min(float(np.hypot(*vc.CARD_LAB[n][1:])) for n in vc.ACHROMATIC) - 0.0,
        vc.CHROMA_CUT - max(float(np.hypot(*vc.CARD_LAB[n][1:])) for n in vc.ACHROMATIC),
    )
    mb_c = float(np.hypot(*vc.CARD_LAB["米白"][1:]))
    ok("CHROMA_CUT 对无彩色/米白两侧均留 >=2.5 余量",
       vc.CHROMA_CUT - max(float(np.hypot(*vc.CARD_LAB[n][1:])) for n in vc.ACHROMATIC) >= 2.5
       and mb_c - vc.CHROMA_CUT >= 2.5,
       "CUT=%.1f 无彩色最大C*=%.2f 米白C*=%.2f" % (
           vc.CHROMA_CUT,
           max(float(np.hypot(*vc.CARD_LAB[n][1:])) for n in vc.ACHROMATIC), mb_c))

    # ------------------------------------------------------------------
    # I. 只报一个目标（对齐参考实现 visual_object_detector.py）
    # ------------------------------------------------------------------
    print("== I. 只报一个：先过置信度阈值 → 取面积最大（并列取置信度高） ==")
    rs = [
        {"area": 100, "color": {"conf": 0.90, "name": "红"}},
        {"area": 900, "color": {"conf": 0.30, "name": "绿"}},   # 面积最大但不达标
        {"area": 500, "color": {"conf": 0.80, "name": "蓝"}},
    ]
    ok("过滤后取面积最大者（蓝）",
       [r["color"]["name"] for r in vc.pick_best(rs, 0.5)] == ["蓝"],
       str([r["color"]["name"] for r in vc.pick_best(rs, 0.5)]))
    ok("只返回 0 或 1 个", len(vc.pick_best(rs, 0.0)) == 1)
    ok("全不达标 → 空", vc.pick_best(rs, 0.95) == [])
    ok("空/None 输入 → 空", vc.pick_best([], 0.0) == [] and vc.pick_best(None, 0.0) == [])
    ok("面积并列取置信度高者（绿）",
       [r["color"]["name"] for r in vc.pick_best(
           [{"area": 100, "color": {"conf": 0.3, "name": "红"}},
            {"area": 100, "color": {"conf": 0.9, "name": "绿"}}], 0.0)] == ["绿"])
    # ★ 用**正**阈值（相机服务实际就用 color_conf>0）：conf=0 的"未知色"必须被过滤掉。
    #   （阈值=0 时 conf=0 也会通过，这是"不过滤"的正常语义，不是缺陷。）
    try:
        a = vc.pick_best([{"area": None, "color": None},
                          {"area": 10, "color": {"conf": 0.5, "name": "红"}}], 0.1)
        b = vc.pick_best([{"area": None, "color": None}], 0.1)
        ok("坏数据不炸，且正阈值下 conf=0/None 被过滤",
           len(a) == 1 and a[0]["color"]["name"] == "红" and b == [],
           "a=%s b=%s" % (a, b))
    except Exception as ex:                      # noqa: BLE001
        ok("坏数据不炸，且正阈值下 conf=0/None 被过滤", False, repr(ex))

    # I2. color_only（实时链路口径）：只用颜色阈值，彩色矩形可检出、灰色物体不产生候选
    e_col = vc.VisionEngine(color_only=True)
    ok("color_only：彩色矩形可检出",
       len(e_col.detect(render(hex_bgr("#1F4FD8")))["results"]) == 1)
    e_col2 = vc.VisionEngine(color_only=True)
    ok("color_only：低饱和灰物体不产生候选（只认彩色，是预期取舍）",
       len(e_col2.detect(render(hex_bgr(vc.COLOR_CARD["灰"])))["results"]) == 0)

    # I3. 绝对面积下限：小于 min_area_abs 的目标被挡掉（参考实现 MIN_AREA 口径）
    e_abs = vc.VisionEngine(color_only=True)
    small = np.full((H, W, 3), BG, np.uint8)
    cv2.rectangle(small, (300, 220), (340, 260), hex_bgr("#1F4FD8"), -1)   # 40x40 = 1600 < 2500
    ok("小于绝对面积下限的小块被挡掉",
       len(e_abs.detect(small)["results"]) == 0,
       "thr.min_area_abs=%s" % vc.DEFAULT_THR.get("min_area_abs"))

    # ------------------------------------------------------------------
    # J. 现场场景：物体被遮挡 / 移动 —— 实时链路必须照样检出
    #    （用户现场反馈："物体移动时检测不到""只出现矩形的一部分"）
    # ------------------------------------------------------------------
    print("== J. 遮挡 / 移动场景：实时链路（纯颜色阈值）必须照样检出 ==")

    # J1. 部分遮挡：矩形右下角被灰色夹具压住 ~1/4 → 仍应检出
    #     （旧的实心度 0.90 会把这个带缺口的形状整块拒掉）
    occ = render(hex_bgr("#1F4FD8"))
    cv2.rectangle(occ, (int(W / 2) + 10, int(H / 2) + 10),
                  (int(W / 2) + 140, int(H / 2) + 110), (BG, BG, BG), -1)
    r_occ = vc.VisionEngine().detect(occ)
    ok("被遮挡一部分的矩形仍被检出", len(r_occ["results"]) == 1,
       "检出 %d 个（阈值 min_fill=%.2f solidity=%.2f）"
       % (len(r_occ["results"]), vc.DEFAULT_THR["min_fill"], vc.DEFAULT_THR["solidity_min"]))

    # J2. 移动：同一个物体分别在左/右两个位置各测一帧（各自新引擎，模拟逐帧）
    #     ★ 纯颜色阈值与"物体动没动"无关；若退回背景差分/MOG2，静止物体会消失、
    #       移动物体只会留下残影 → 这条会红。
    hits = 0
    for dx in (-140, 140):
        mv = np.full((H, W, 3), BG, np.uint8)
        bx = cv2.boxPoints(((W / 2 + dx, H / 2), (240, 150), 8)).astype(np.int32)
        cv2.fillPoly(mv, [bx], hex_bgr("#4CAF50"))
        if vc.VisionEngine().detect(mv)["results"]:
            hits += 1
    ok("物体在不同位置都能检出（不依赖背景差分）", hits == 2, "命中 %d/2" % hits)

    # J3. 只报一个：同帧放一大一小两块物料 → 经 pick_best 只报面积最大的那块
    #     ★ 注意分层：`VisionEngine.detect()` 按设计返回**全部候选**（离线/调试要看全量），
    #       "只报一个"是 `pick_best()` 的职责（相机服务实时链路用的就是它）。
    two = render(hex_bgr("#1F4FD8"))                     # 主体 260x150
    cv2.rectangle(two, (40, 40), (40 + 120, 40 + 90), hex_bgr("#4CAF50"), -1)  # 次体更小
    r_two = vc.VisionEngine().detect(two)
    best_two = vc.pick_best(r_two["results"], 0.0)
    names = [x["color"]["name"] for x in best_two]
    ok("同帧多目标时只报一个（面积最大的蓝）", len(best_two) == 1 and names == ["蓝"],
       "候选 %s → 选中 %s" % ([x["color"]["name"] for x in r_two["results"]], names))

    # ------------------------------------------------------------------
    print("")
    print("== K. 矩形硬门槛 fill_min（用户口径：只识别矩形；2026-10-08 新增） ==")
    # ------------------------------------------------------------------
    # K1. 旋转不变性：★ 这一条是"硬门槛会不会误杀"的关键 ——
    #     fill 用 minAreaRect 的长×宽，与旋转无关；斜着举的方块必须照样判得出颜色。
    rot_ok = True
    for ang in (0.0, 12.0, 30.0, 45.0, 78.0):
        img = np.full((H, W, 3), BG, np.uint8)
        box = cv2.boxPoints(((W / 2, H / 2), (200, 200), ang)).astype(np.int32)
        cv2.fillPoly(img, [box], hex_bgr("#E02020"))
        r = vc.VisionEngine().detect(img)
        got = [x["color"]["name"] for x in r["results"]]
        if got != ["红"]:
            rot_ok = False
            ok("旋转 %.0f° 的方块仍判红" % ang, False, "实际 %s" % got)
    ok("★ 旋转不变：0/12/30/45/78° 的正方块都判得出「红」（硬门槛不误杀斜着举的件）",
       rot_ok)

    # K2. 硬门槛精确单测：把 fill 注入到 0.40（低于新默认 0.55）→
    #     必须**直接判非矩形、不给颜色**。
    #     ★ 为什么要注入而不是造形状：候选阶段本身就有 min_fill=0.45 + solidity=0.75 两道门，
    #       现实里"能过候选、却卡在硬门槛"的形状很少（三角形/细长条在候选阶段就被挡了）。
    #       这条断言保证：门槛真的接在判色链路上，而不是写了没人用。
    sq = np.full((H, W, 3), BG, np.uint8)
    cv2.rectangle(sq, (W // 2 - 110, H // 2 - 110), (W // 2 + 110, H // 2 + 110),
                  hex_bgr("#E02020"), -1)
    eng = vc.VisionEngine()
    r0 = eng.detect(sq)
    ok("正方形正常给出颜色（新门槛不误伤）", len(r0["results"]) == 1
       and r0["results"][0]["color"]["name"] == "红",
       "实际 %s" % [x["color"]["name"] for x in r0["results"]])
    cand = r0["results"][0]
    FMIN = float(vc.DEFAULT_THR["fill_min"])
    fake = {"box": np.array(cand["box"], np.int32), "fill": 0.40, "rect": cand["rect"]}
    col = eng._color(sq, r0["mask"], fake)
    ok("★ fill=0.40（< 默认 %.2f）→ 判「非矩形」且不输出颜色" % FMIN,
       col["name"] == "未知" and "非矩形" in (col.get("reason") or ""),
       "实际 name=%s reason=%s" % (col["name"], col.get("reason")))

    # K3. fill_min 可关闭：填 0 时同一注入值必须重新给出颜色结论（现场可退回旧行为）
    t_off = dict(vc.DEFAULT_THR)
    t_off["fill_min"] = 0.0
    eng_off = vc.VisionEngine(thr=t_off)
    col_off = eng_off._color(sq, r0["mask"], fake)
    ok("fill_min=0 时门槛关闭（同一输入重新给出颜色）", col_off["name"] == "红",
       "实际 %s" % col_off["name"])

    # K3b. 边界：fill 略高于门槛 → 必须放过（不可把刚好达标的也拦了）
    col_hi = eng._color(sq, r0["mask"],
                        {"box": np.array(cand["box"], np.int32),
                         "fill": FMIN + 0.03, "rect": cand["rect"]})
    ok("fill=%.2f（略高于门槛 %.2f）→ 正常给颜色（门槛不过度拦）" % (FMIN + 0.03, FMIN),
       col_hi["name"] == "红", "实际 %s" % col_hi["name"])

    # ------------------------------------------------------------------
    # K5. ★★ 关键回归（2026-10-08 现场事故）：**门槛必须低于现场实测最低值**
    #
    #   首次把 fill_min 定成 0.72（只看合成图定的，合成图 fill≈0.95+），
    #   而现场相机图里**真矩形**的 fill 只有 0.67~0.75（透视斜视+边缘模糊）→
    #   一律被判"非矩形"→ 不给颜色 → 三色全都"检测不到"（用户报的绿色检不出就是这个）。
    #
    #   这里把**现场实测数据**钉死：真实方块的 fill 最低 0.67（蓝色 210746 那张），
    #   门槛必须比它低、且留 ≥0.08 的余量，否则稍微换个角度/光照就会掉出去。
    #   ⚠ 改 fill_min 之前必须跑 tools/vision_replay_test.py（真图回放）确认三色检出率。
    FIELD_MIN_FILL = 0.67      # 现场实测：真矩形的最低 fill（见 tools/vision_replay_test.py）
    ok("★★ 默认 fill_min 低于现场实测最低 fill（%.2f）且留 ≥0.08 余量（当前 %.2f）"
       % (FIELD_MIN_FILL, FMIN),
       FMIN < FIELD_MIN_FILL - 0.08,
       "fill_min=%.2f，现场实测最低=%.2f" % (FMIN, FIELD_MIN_FILL))
    ok("★ 反证：旧的 0.72 会被这条判据否掉（说明判据不是恒真的）",
       0.72 >= FIELD_MIN_FILL - 0.08)
    ok("fill_min 可由 /vision/config 覆盖（在合法区间表里）",
       "fill_min" in vc._THR_RANGE)
    # 软提示线与硬门槛**必须解耦**（以前共用一个数，调一个另一个跟着动，极易改错）
    ok("fill_warn（软提示线）与 fill_min（硬门槛）是两个独立可调项",
       "fill_warn" in vc.DEFAULT_THR and "fill_warn" in vc._THR_RANGE
       and float(vc.DEFAULT_THR["fill_warn"]) > FMIN)

    # ------------------------------------------------------------------
    # K6. ★★ 置信度不得被 Lab 一票否决（同日第二处现场事故）
    #   旧实现 conf = min(hsv_conf, lab_conf)：Lab 用**固定标准色卡**比色，
    #   现场光照偏暗时实测红 a*=12~14 vs 色卡 a*=69 → ΔE 26~34 → lab_conf=0
    #   → **形状完美（fill=0.98）的红块 conf=0.00** → 被 min_conf 卡掉 → "检测不到"。
    #   现在口径：HSV 一致性是主判据；Lab 只做复核/提示。
    #   ⚠ 只在**去掉注释**的源码里找：注释里会写"旧实现是 min(hsv_conf, lab_conf)"
    #     来解释历史，直接扫全文会把解释文字当成"还在调用"（第一版就踩了）。
    _src = inspect.getsource(vc.VisionEngine._color)
    _code = "\n".join(l for l in _src.split("\n") if not l.strip().startswith("#"))
    ok("★★ 置信度不再被 Lab 的 ΔE 一票否决（代码里没有 min(hsv_conf, lab_conf)）",
       "min(hsv_conf, lab_conf)" not in _code)
    ok("★ HSV/Lab 一致时置信度略升（互相印证）",
       "hsv_conf * 1.15" in inspect.getsource(vc.VisionEngine._color))
    ok("★ HSV/Lab 分歧时降但不归零（仍交上层按 min_conf 决定）",
       "hsv_conf * 0.55" in inspect.getsource(vc.VisionEngine._color))
    ok("★ Lab 偏差大只写进 reason 提示标定，不影响能否给出颜色",
       "建议现场标定" in inspect.getsource(vc.VisionEngine._color))
    # 实测反证：现场那张"形状完美但 ΔE=34"的红图，现在必须给出颜色且 conf 高
    #   （用合成图复刻同一情形：纯色块 + 拉低亮度模拟现场暗光 → ΔE 变大但 HSV 仍对）
    dark = np.full((H, W, 3), BG, np.uint8)
    cv2.rectangle(dark, (W // 2 - 110, H // 2 - 110), (W // 2 + 110, H // 2 + 110),
                  hex_bgr("#7A1010"), -1)          # 暗红：Lab 远离标准红，HSV 色相仍属红
    rd = vc.VisionEngine().detect(dark)
    if rd["results"]:
        c0 = rd["results"][0]["color"]
        ok("★★ 暗光下的红块仍给出「红」且 conf 不被 Lab 拖到 0",
           c0["name"] == "红" and float(c0["conf"]) > 0.3,
           "name=%s conf=%s de=%s" % (c0["name"], c0["conf"], c0.get("de")))
    else:
        ok("★★ 暗光下的红块仍给出「红」（不被形状门槛误杀）", False, "无候选")

    # ------------------------------------------------------------------
    # K7. 七色色相带必须"纯划分"（无缝隙、无重叠）
    #   ★★ 2026-10-10 七色重构：可识别色扩到 7 色后，色相环被 7 条带**完整切分**
    #     （红<12或≥160 / 橙12~22 / 黄22~35 / 绿35~88 / 青88~100 / 蓝100~135 / 紫135~160）。
    #     旧口径"绿带上限 95"作废；绿带上限由 75 再退到 **88**（现场"绿被判青"后修正）。
    #     要守的不变的是：**任何色相都不得落在带外**（旧代码曾有 90~95 的 5° 空隙 → 判不出）。
    src_vote = inspect.getsource(vc._palette_vote)
    ok("★ 七色带全部来自 HUE_BANDS（纯划分，无手写空隙）", "HUE_BANDS" in src_vote)
    # ★★ 2026-10-10 现场反馈"绿被判成青"后的修正：绿带上限 75 → **88**。
    #   现场绿件 H≈69~70，离 75 只有 5 格 → 光照/白平衡一漂就翻到"青"。现绿留 ≥18 格余量，
    #   青带收到 [88,100)（只留给真青），蓝仍从 100 起（护住现场蓝 H104~106）。
    ok("★ 绿带上限 88（给现场绿 H69~70 留 ≥18 格余量）、青/蓝分界 100",
       vc.HUE_BANDS[4] == ("绿", 35.0, 88.0) and vc.HUE_BANDS[5] == ("青", 88.0, 100.0))
    cov = [False] * 180
    for _nm, lo, hi in vc.HUE_BANDS:
        for hh in range(int(lo), int(hi)):
            cov[hh] = True
    band_gap = [hh for hh in range(180) if not cov[hh]]
    ok("★ 0~179 每个色相都被某条带覆盖（不存在判不出的色相）", not band_gap,
       "空档: " + str(band_gap[:12]))
    # 偏青的绿（BGR(140,190,40) → H≈80）：**必须归「绿」**（现场绿件就落这一带，不许再判青）
    teal = np.full((H, W, 3), BG, np.uint8)
    cv2.rectangle(teal, (W // 2 - 110, H // 2 - 110), (W // 2 + 110, H // 2 + 110),
                  (140, 190, 40), -1)
    r_teal = vc.VisionEngine().detect(teal)
    got_teal = r_teal["results"][0]["color"]["name"] if r_teal["results"] else ""
    ok("★ 偏青的绿（H≈80）现归「绿」（现场绿件就落这一带）",
       got_teal == "绿", "实际 %r" % got_teal)
    # 强青（BGR(200,190,0) → H≈91）：七色里的「青」要留得住
    cyan = np.full((H, W, 3), BG, np.uint8)
    cv2.rectangle(cyan, (W // 2 - 110, H // 2 - 110), (W // 2 + 110, H // 2 + 110),
                  (200, 190, 0), -1)
    r_cyan = vc.VisionEngine().detect(cyan)
    got_cyan = r_cyan["results"][0]["color"]["name"] if r_cyan["results"] else ""
    ok("★ 强青（H≈91）仍判「青」", got_cyan == "青", "实际 %r" % got_cyan)

    print("")
    print("结果: %d 通过 / %d 失败" % (pass_n, fail_n))
    return 1 if fail_n else 0
if __name__ == "__main__":
    sys.exit(main())
