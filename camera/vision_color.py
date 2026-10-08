# -*- coding: utf-8 -*-
"""
视觉颜色分拣 · 算法流水线（纯 OpenCV + numpy，零新增依赖）。

设计要点（详见 docs/方案-视觉颜色分拣（OpenCV）.md）：
  - 分割：背景差分（显式给背景）或 MOG2 自适应背景（VisionEngine 持有状态）；
  - 筛选：只认"长方形物体" —— 面积/短边/长宽比/矩形度/实心度/四顶点 多重过滤；
  - 取色：掩膜腐蚀 + 剔高光 + 剔暗部 + 中心加权 → kmeans 主簇中位 Lab；
  - 判色：CIE Lab + CIEDE2000（感知均匀、与设备无关）；
          ★ 白/银/米白/灰/黑 **单独走低饱和判据**，不参与彩色 ΔE 竞争
            （否则"深蓝"常被最近的"黑"抢走 —— 这是现场最常见的误判）；
  - 稳定：连续 N 帧"质心不动 + 框稳定"才触发一次，保证 1 秒停留只记录/播报一次。

★ 本模块**不 import 相机 SDK，也不碰 HTTP**：全部是纯函数 + 一个带状态的小引擎，
  所以可以脱离相机、用静态图直接跑回归（tools/vision_color_test.py）。
"""
from __future__ import annotations

import json
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

# =====================================================================
# 1. 色卡（18 类常见色）。★ 初值仅作参考，现场务必用实物采样覆盖：
#    真实物料/光照下，标准色卡的 ΔE 往往 >15，实物标定后可降到 <5。
# =====================================================================
COLOR_CARD: Dict[str, str] = {
    "红": "#E02020", "橙": "#FF7A18", "黄": "#FFD400", "草绿": "#4CAF50",
    "深绿": "#1E6B34", "青": "#00BFC8", "天蓝": "#4FA8FF", "蓝": "#1F4FD8",
    "深蓝": "#0E2A6B", "紫": "#8A3FC0", "粉": "#FF9EC4", "棕": "#7A4A22",
    "金": "#D4A017", "米白": "#F0E4CC", "白": "#FFFFFF",
    "银灰": "#C8CDD2", "灰": "#8A8F95", "黑": "#14181C",
}

# =====================================================================
# 三色目标：分拣只认红 / 绿 / 蓝。
#   ★ 判色只在 RGB_NAMES 三色里做最近邻，其余颜色一律判为"未知"，
#     由置信度阈值过滤，不参与画框 / 记录 / 分拣。
# =====================================================================
RGB_NAMES = ("红", "绿", "蓝")

# 三色参考卡（固定，与 18 类卡独立）：分拣只认这三色，判色只用它做参考。
#   三色 Lab 互相 ΔE 足够大，非三色(橙/黄/青/草绿…）归到最近的三色时 conf 明显偏低，
#   由置信度阈值过滤。
RGB_CARD: Dict[str, str] = {"红": "#E02020", "绿": "#1FA74A", "蓝": "#1F4FD8"}


# 预计算三色卡的 Lab（判色参考）；srgb_to_lab 定义在下方，故此处后移到底部统一算

# 非高饱和/低亮度不属三色目标（阈值参照参考实现 dominant_color_name）
HUE_S_MIN = 70.0
HUE_V_MIN = 60.0

# =====================================================================
# ★ 审计修复 P0-cam-3：检测阈值集中成"一张可覆盖的默认表"。
#   以前这些数字散落在 hsv_color_mask / find_rectangles / _hue_vote 的函数签名里，
#   现场想按光照、物料大小微调只能改源码重部署；现在：
#     - 默认值只在这里定义一次，下面各函数的默认参数一律取自本表（不改算法结构）；
#     - 相机服务 `POST /vision/config {"thr": {...}}` 可覆盖任意子集（见 camera_service）；
#     - norm_thr() 丢弃未知键 + 逐键限幅，配置写错也拆不掉这道门。
#   ★ 本次唯一放宽的一项：min_area_ratio 0.005 → 0.003。
#     理由（纯代码事实，非猜测）：检测统一跑在缩放后的小图 STREAM_MAX_W=960 上
#     （camera_service.py:100/728-730），960x540 下 0.005 ≈ 2592px² ≈ 51x51；
#     而同一张表里的 min_side=30 只要求 30x30=900px² —— 两个下限互相打架，
#     真正生效的是随分辨率缩放的面积比：小件/远一点的物料必须长到 ~51px 宽才可能
#     被检出，表现为"偶尔才检测到"。改成 0.003 ≈ 1555px² ≈ 39x39 后，
#     与 min_side=30 基本对齐（短边判据重新成为主约束），且仍然：
#       远高于 200px 的绝对噪声底，fill/solidity/长宽比/锐角/色相一致性一条没松。
#     面积**上限**与其它阈值本次只做成可配置、默认值一律不动【证据不足，未放宽默认值】。
DEFAULT_THR: Dict[str, float] = {
    "min_area_ratio": 0.003,   # 面积下限(占整帧比例)；0.005 → 0.003，理由见上
    "max_area_ratio": 0.60,    # 面积上限(整屏大块挡掉)。默认不变，可配置
    "min_side": 30,            # 短边像素下限。默认不变
    "mask_sat_min": 60,        # HSV 彩色前景掩膜 S 下限。80 → 60（现场光照偏暗时，
                               #   红/绿/蓝实物饱和度常落到 60~80，旧值会让目标整体漏检）
    "mask_val_min": 35,        # HSV 彩色前景掩膜 V 下限。40 → 35（略放宽暗部，勿过低以免引噪）
    "hue_s_min": 60,           # 色相投票最低饱和度。70 → 60，与掩膜门槛对齐
    "hue_v_min": 50,           # 色相投票最低亮度。60 → 50，兼顾偏暗光照
    # ★ 参考实现（visual_object_detector.py 的 ColorPatchDetector.MIN_AREA=3000）：
    #   在"只报一个最大目标"的口径下，加一道**绝对面积下限**比只按比例更稳 ——
    #   远处的小噪点/反光碎块不会被当成"最大目标"顶上来。
    #   默认 2500px：介于参考的 3000（640×480 帧）与本项目 960 宽帧的 0.003 比例之间，
    #   两者取 max，小物料仍能被检出，纯噪点被挡掉。可按现场用 /vision/config 覆盖。
    "min_area_abs": 2500,
    # ★★ 2026-09-25 二次优化（用户现场反馈"物体移动时检测不到 / 只出现矩形的一部分"）：
    #   把"必须是标准矩形"的三道门槛**放宽**，因为现场物料常常被遮挡、只露出一部分，
    #   或被机械手/夹具压住边角 —— 过严的填充率/实心度会把整块拒掉（"只出现矩形的一部分"）。
    #   放宽后仍保留"排除细长条 + 排除圆形"两个必要判据（圆形靠"是否存在锐角"剔除）。
    "min_fill": 0.45,          # 最小外接矩形填充率下限（原写死 0.55）
    "solidity_min": 0.75,      # 实心度下限（面积/凸包面积；原写死 0.90）
    "aspect_max": 6.0,         # 长宽比上限（原写死 4.0；部分遮挡的长条仍可能是同一个物料）
}

# 每个键的合法区间：越界一律夹回，不做"拒绝整包"，避免现场配置一个错值就全不检测。
_THR_RANGE: Dict[str, Tuple[float, float]] = {
    "min_area_ratio": (0.0001, 1.0),
    "max_area_ratio": (0.01, 1.0),
    "min_side": (4.0, 2000.0),
    "mask_sat_min": (0.0, 255.0),
    "mask_val_min": (0.0, 255.0),
    "hue_s_min": (0.0, 255.0),
    "hue_v_min": (0.0, 255.0),
    "min_area_abs": (0.0, 5_000_000.0),
    "min_fill": (0.05, 1.0),
    "solidity_min": (0.05, 1.0),
    "aspect_max": (1.0, 50.0),
}


def norm_thr(thr) -> Dict[str, float]:
    """把外部阈值覆盖合并到 DEFAULT_THR（未知键丢弃、坏值忽略、越界夹紧）。

    ★ 只接受"部分覆盖"：`norm_thr({"min_side": 40})` 返回完整表、只改 min_side，
      这样调用方无需先拿到默认值，也不会因为漏传一个键把其它阈值清零。
    """
    out = dict(DEFAULT_THR)
    if not isinstance(thr, dict):
        return out
    for k, v in thr.items():
        if k not in _THR_RANGE or v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:                      # NaN 会让所有比较变 False → 整条产线静默停摆
            continue
        lo, hi = _THR_RANGE[k]
        out[k] = float(min(hi, max(lo, f)))
    # 兜底：下限不能大于上限（否则一条候选都进不来，画面看着正常却永远不触发）
    if out["min_area_ratio"] > out["max_area_ratio"]:
        out["min_area_ratio"] = out["max_area_ratio"]
    return out


def _hue_vote(bgr_roi, s_min=None, v_min=None):
    """逐像素色相分箱投票判红/绿/蓝。返回 (name, ratio)。

    ratio = 多数色带像素数 / 彩色像素数（该候选色相的一致性）。
    ★ 关键：H 是环形标尺(0°≡180°)，直接 `mean()` 会把"红带的 0° 端 + 176° 端"
      平均拉到约 40°（青/绿带）→ 误判绿。逐像素分箱投票从根本上避开环形均值陷阱，
      又与参考实现"只看三色带"的判定方式一致。
    """
    if bgr_roi is None or bgr_roi.size == 0:
        return ("", 0.0)
    # ★ 审计修复 P0-cam-3: 饱和度/亮度门槛可由 DEFAULT_THR 覆盖（缺省=原值）
    s_lim = HUE_S_MIN if s_min is None else float(s_min)
    v_lim = HUE_V_MIN if v_min is None else float(v_min)
    hsv = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2HSV)
    sel = (hsv[..., 1] >= s_lim) & (hsv[..., 2] >= v_lim)
    n = int(sel.sum())
    if n < 40:
        return ("", 0.0)
    H = hsv[..., 0][sel]
    nr = int(((H < 12) | (H >= 160)).sum())   # 红带（扩到 160 容纳高饱和品红/玫红）
    ng = int(((H >= 35) & (H < 90)).sum())    # 绿带
    nb = int(((H >= 95) & (H < 135)).sum())   # 蓝带（收窄上限 145→135，不再吞紫/青）
    m = max(nr, ng, nb)
    if m == 0:
        return ("", 0.0)                       # 全落在三色带之外 → 非三色
    name = "红" if nr == m else ("绿" if ng == m else "蓝")
    return (name, float(m) / float(n))


def hue_color_name(bgr_roi) -> str:
    """唯一红/绿/蓝（或空串）。见 _hue_vote。"""
    return _hue_vote(bgr_roi)[0]


# 附注：RGB_NAMES / RGB_CARD / classify_rgb（CIEDE2000 最近邻）仍保留使用，
#       用于低带宽/其它判定路径；颜色分拣主走 hue_color_name（参考算法）。


def classify_rgb(lab: Sequence[float], card: Optional[Dict[str, np.ndarray]] = None
                 ) -> Dict[str, object]:
    """三色判定：只在 红/绿/蓝 三色参考里做 ΔE 最近邻。低色度判为"未知"。

    返回 {name, hex, de, conf, alt, alt_de, chroma}。conf 由 ΔE 决定：
    越接近目标色相 conf 越高；离三色都远（如橙色 红↔绿 之间）conf 明显偏低，
    便于用"置信度阈值"把中间色 / 非目标色滤掉。
    """
    L, A, B = float(lab[0]), float(lab[1]), float(lab[2])
    chroma = float(np.hypot(A, B))
    # 三色判色只用固定三色卡参考，不受 18 类卡/标定覆盖影响
    tbl = RGB_CARD_LAB
    # 低色度（灰/黑/白）：不属于三色目标
    if chroma < CHROMA_CUT:
        return {"name": "未知", "hex": None, "de": None,
                "conf": 0.0, "alt": None, "alt_de": None, "chroma": round(chroma, 1)}
    names = list(RGB_NAMES)
    refs = np.stack([tbl[n] for n in names], axis=0)
    des = delta_e_2000(np.array([L, A, B], dtype=np.float64), refs)
    order = np.argsort(des)
    i1 = int(order[0])
    de1 = float(des[i1])
    name = names[i1]
    alt, alt_de = None, None
    if len(order) > 1:
        i2 = int(order[1])
        de2 = float(des[i2])
        if de2 - de1 < 10.0:                 # 三色本身远，10.0 已足够宽松地给"疑似"
            alt, alt_de = names[i2], round(de2, 2)
    return {"name": name, "hex": RGB_CARD[name], "de": round(de1, 2),
            "conf": round(max(0.0, min(1.0, 1.0 - de1 / 25.0)), 3),
            "alt": alt, "alt_de": alt_de, "chroma": round(chroma, 1)}


def hsv_color_mask(bgr: np.ndarray, sat_min=None, val_min=None,
                   morph: int = 5) -> np.ndarray:
    """HSV 彩色前景掩膜（参照实时"彩色矩形目标"检测思路）。

    ★ 背景差分对"物体色≈背景色"的情形会漏检；而鲜艳物体的 HSV 高饱和特征
      与背景无关，OR 到分割掩膜上是对背景差分 / MOG2 的可靠兜底增强。
      只取高饱和度 + 非过暗区域（排除近黑/近白/灰），形态学闭合联通同色碎块。
    ★ 审计修复 P0-cam-3: sat_min/val_min 缺省取自 DEFAULT_THR（可由 /vision/config 覆盖）。
    """
    if sat_min is None:
        sat_min = int(DEFAULT_THR["mask_sat_min"])
    if val_min is None:
        val_min = int(DEFAULT_THR["mask_val_min"])
    h, w = bgr.shape[:2]
    if h * w == 0:
        return np.zeros((h, w), np.uint8)
    blurred = cv2.GaussianBlur(bgr, (5, 5), 0)
    hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 1] > sat_min) & (hsv[..., 2] > val_min)).astype(np.uint8) * 255
    k = np.ones((morph, morph), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    return mask


# 无彩色（低饱和）单独判定，不参与彩色 ΔE 竞争。
# ★ 米白必须留在"彩色"一侧：它的色度 C*≈18（暖白），若按低饱和规则判会被
#   亮度分档吞掉，导致"米白"这个类永远选不到。放在彩色侧走 ΔE 最近邻反而很稳
#   （它离黄/粉都远，ΔE 差一大截）。
ACHROMATIC = ("白", "银灰", "灰", "黑")
CHROMATIC = tuple(n for n in COLOR_CARD if n not in ACHROMATIC)

CHROMA_CUT = 9.0      # 色度 C* = hypot(a*, b*) 低于此值视为"无彩色"
                      # ★ 取 9.0 是让两侧余量对称：无彩色侧最"有彩"的是灰(C*=3.86)，
                      #   彩色侧最"无彩"的是米白(C*=13.18)。旧值 12.0 离米白只剩 1.18，
                      #   物料稍暗就掉进无彩色分支被亮度分档吃掉（米白→银灰）。

# 无彩色的亮度分档：边界统一取相邻两档色卡 L* 的中点
#   （白100.00 / 银灰82.16 / 灰59.18 / 黑8.00 → 中点 91.1 / 70.7 / 33.6）
L_WHITE = 91.0
L_SILVER = 70.0
L_GRAY = 34.0


def hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# =====================================================================
# 2. 色彩空间与色差（自写，离线可部署）
# =====================================================================
_XYZ_M = np.array([[0.4124564, 0.3575761, 0.1804375],
                   [0.2126729, 0.7151522, 0.0721750],
                   [0.0193339, 0.1191920, 0.9503041]], dtype=np.float64)
_WP = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB(0~255) → CIE L*a*b* (D65)。支持 (...,3) 批量。"""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (lin @ _XYZ_M.T) / _WP
    f = np.where(xyz > 0.008856451679, np.cbrt(xyz),
                 (903.2962962 * xyz + 16.0) / 116.0)
    return np.stack([116.0 * f[..., 1] - 16.0,
                     500.0 * (f[..., 0] - f[..., 1]),
                     200.0 * (f[..., 1] - f[..., 2])], axis=-1)


def bgr_to_lab(bgr: np.ndarray) -> np.ndarray:
    """OpenCV BGR(0~255) → Lab。"""
    arr = np.asarray(bgr, dtype=np.uint8).reshape(-1, 1, 3)
    lab = cv2.cvtColor(arr, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float64)
    # OpenCV 的 Lab 是 8bit 编码: L*255/100, a+128, b+128 → 还原成真实标度
    lab[:, 0] *= 100.0 / 255.0
    lab[:, 1] -= 128.0
    lab[:, 2] -= 128.0
    return lab


def delta_e_2000(lab1: np.ndarray, lab2: np.ndarray,
                 kL: float = 1.0, kC: float = 1.0, kH: float = 1.0) -> np.ndarray:
    """CIEDE2000 色差（Sharma 实现，向量化）。支持 (...,3) 广播。"""
    a = np.asarray(lab1, dtype=np.float64)
    b = np.asarray(lab2, dtype=np.float64)
    L1, A1, B1 = a[..., 0], a[..., 1], a[..., 2]
    L2, A2, B2 = b[..., 0], b[..., 1], b[..., 2]

    C1 = np.hypot(A1, B1)
    C2 = np.hypot(A2, B2)
    Cbar = (C1 + C2) / 2.0
    Cbar7 = Cbar ** 7
    G = 0.5 * (1.0 - np.sqrt(Cbar7 / (Cbar7 + 25.0 ** 7)))
    A1p, A2p = (1.0 + G) * A1, (1.0 + G) * A2
    C1p, C2p = np.hypot(A1p, B1), np.hypot(A2p, B2)

    H1p = np.degrees(np.arctan2(B1, A1p)) % 360.0
    H2p = np.degrees(np.arctan2(B2, A2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p
    dhp = H2p - H1p
    dhp = np.where(dhp > 180.0, dhp - 360.0, np.where(dhp < -180.0, dhp + 360.0, dhp))
    zero_chroma = (C1p * C2p) == 0.0
    dhp = np.where(zero_chroma, 0.0, dhp)
    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp / 2.0))

    Lbp = (L1 + L2) / 2.0
    Cbp = (C1p + C2p) / 2.0
    hsum = H1p + H2p
    hdiff = np.abs(H1p - H2p)
    Hbp = np.where(zero_chroma, hsum,
                   np.where(hdiff <= 180.0, hsum / 2.0,
                            np.where(hsum < 360.0, (hsum + 360.0) / 2.0,
                                     (hsum - 360.0) / 2.0)))

    T = (1.0 - 0.17 * np.cos(np.radians(Hbp - 30.0))
         + 0.24 * np.cos(np.radians(2.0 * Hbp))
         + 0.32 * np.cos(np.radians(3.0 * Hbp + 6.0))
         - 0.20 * np.cos(np.radians(4.0 * Hbp - 63.0)))
    dtheta = 30.0 * np.exp(-(((Hbp - 275.0) / 25.0) ** 2))
    Cbp7 = Cbp ** 7
    Rc = 2.0 * np.sqrt(Cbp7 / (Cbp7 + 25.0 ** 7))
    SL = 1.0 + (0.015 * (Lbp - 50.0) ** 2) / np.sqrt(20.0 + (Lbp - 50.0) ** 2)
    SC = 1.0 + 0.045 * Cbp
    SH = 1.0 + 0.015 * Cbp * T
    RT = -np.sin(np.radians(2.0 * dtheta)) * Rc
    return np.sqrt((dLp / (kL * SL)) ** 2 + (dCp / (kC * SC)) ** 2
                   + (dHp / (kH * SH)) ** 2
                   + RT * (dCp / (kC * SC)) * (dHp / (kH * SH)))


# =====================================================================
# 3. 判色（无彩色单独判 + 彩色最近邻 ΔE）
# =====================================================================
def classify_lab(lab: Sequence[float], card: Optional[Dict[str, np.ndarray]] = None
                 ) -> Dict[str, object]:
    """返回 {name, hex, de, conf, alt, alt_de}。card 为 name→Lab 的覆盖表（标定后传入）。"""
    L, A, B = float(lab[0]), float(lab[1]), float(lab[2])
    chroma = float(np.hypot(A, B))
    tbl = card or CARD_LAB

    # ---- 无彩色：低饱和，按亮度分档（避免"深蓝→黑""浅蓝→白"的互抢） ----
    if chroma < CHROMA_CUT:
        if L >= L_WHITE:
            name = "白"
        elif L >= L_SILVER:
            name = "银灰"
        elif L >= L_GRAY:
            name = "灰"
        else:
            name = "黑"
        ref = tbl.get(name)
        de = float(delta_e_2000(np.array([L, A, B]), ref)) if ref is not None else 0.0
        return {"name": name, "hex": COLOR_CARD[name], "de": round(de, 2),
                "conf": round(max(0.0, min(1.0, 1.0 - de / 25.0)), 3),
                "alt": None, "alt_de": None, "chroma": round(chroma, 1)}

    # ---- 彩色：只在彩色子集里找最近邻 ----
    names = [n for n in CHROMATIC if n in tbl]
    refs = np.stack([tbl[n] for n in names], axis=0)
    des = delta_e_2000(np.array([L, A, B], dtype=np.float64), refs)
    order = np.argsort(des)
    i1 = int(order[0])
    de1 = float(des[i1])
    name = names[i1]
    alt, alt_de = None, None
    if len(order) > 1:
        i2 = int(order[1])
        de2 = float(des[i2])
        if de2 - de1 < 2.0:                    # 双候选接近 → 提示"疑似"
            alt, alt_de = names[i2], round(de2, 2)
    return {"name": name, "hex": COLOR_CARD[name], "de": round(de1, 2),
            "conf": round(max(0.0, min(1.0, 1.0 - de1 / 25.0)), 3),
            "alt": alt, "alt_de": alt_de, "chroma": round(chroma, 1)}


# 预计算色卡 Lab（含无彩色，供无彩色分支取参考）
CARD_LAB: Dict[str, np.ndarray] = {
    n: srgb_to_lab(np.array(hex_to_rgb(h), dtype=np.float64)) for n, h in COLOR_CARD.items()
}

# 预计算三色卡 Lab（供 classify_rgb 三色判色）
RGB_CARD_LAB: Dict[str, np.ndarray] = {
    n: srgb_to_lab(np.array(hex_to_rgb(h), dtype=np.float64)) for n, h in RGB_CARD.items()
}


# =====================================================================
# 4. 分割 / 候选筛选
# =====================================================================
def diff_mask(frame_bgr: np.ndarray, background_bgr: np.ndarray,
              thresh: int = 12, morph: int = 5) -> np.ndarray:
    """显式背景差分（静态相机 + 已知背景时最稳，也便于离线测试）。"""
    d = cv2.absdiff(frame_bgr, background_bgr)
    if d.ndim == 3:
        # ★ 逐通道取最大差，**不要**先 cvtColor 转灰度：转灰度会把三通道差做加权平均，
        #   像"灰物体落在灰背景"这种亮度接近但通道差仍在的情形会被平均掉 → 目标消失。
        gray = d.max(axis=2)
    else:
        gray = d
    _, m = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
    k = np.ones((morph, morph), np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    if morph >= 3:
        m = cv2.dilate(m, np.ones((3, 3), np.uint8))
    return m


def find_rectangles(mask: np.ndarray, min_area_ratio=None,
                    max_area_ratio=None, min_side=None,
                    aspect=None,
                    rect_fill=None, solidity=None,
                    max_items: int = 8,
                    min_area_abs=None) -> List[Dict[str, object]]:
    """从掩膜里挑"长方形物体"。返回 [{box, rect, area, fill, solidity}]。

    ★ 现场口径（2026-09-25 二次放宽）：**不要求"标准矩形"** —— 物料常被遮挡/只露出一部分，
      过严的填充率/实心度会把整块拒掉（表现为"只出现矩形的一部分"）。现在：
      - 长宽比上限 6.0、填充率下限 0.45、实心度下限 0.75（都可由 /vision/config 覆盖）；
      - 仍保留两道必要判据：① 排除细长条；② 排除圆形/正多边形（靠"是否存在锐角"）。
    ★ 审计修复 P0-cam-3: 尺寸门槛缺省取自 DEFAULT_THR（可由 /vision/config 覆盖），
      传 None = 用默认表，显式传值 = 完全沿用旧调用方式（离线脚本不受影响）。
    """
    min_area_ratio = (DEFAULT_THR["min_area_ratio"] if min_area_ratio is None
                      else float(min_area_ratio))
    max_area_ratio = (DEFAULT_THR["max_area_ratio"] if max_area_ratio is None
                      else float(max_area_ratio))
    min_side = int(DEFAULT_THR["min_side"] if min_side is None else min_side)
    min_area_abs = float(DEFAULT_THR["min_area_abs"] if min_area_abs is None else min_area_abs)
    # ★ 2026-09-25：三道"矩形度"门槛也走阈值表（原写死在签名里），
    #   现场可按物料被遮挡的程度用 /vision/config 调松紧，无需改代码重部署。
    rect_fill = float(DEFAULT_THR["min_fill"] if rect_fill is None else rect_fill)
    solidity = float(DEFAULT_THR["solidity_min"] if solidity is None else solidity)
    aspect = tuple(aspect) if aspect is not None else (1.0, float(DEFAULT_THR["aspect_max"]))
    h, w = mask.shape[:2]
    total = float(h * w)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: List[Dict[str, object]] = []
    for c in contours:
        area = float(cv2.contourArea(c))
        # ★ 面积下限 = max(绝对下限, 比例下限)：绝对下限挡远处小噪点（参考实现 MIN_AREA），
        #   比例下限负责随分辨率自适应。
        if area < max(min_area_abs, min_area_ratio * total) or area > max_area_ratio * total:
            continue
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
        if rw < 1 or rh < 1:
            continue
        short, long = min(rw, rh), max(rw, rh)
        if short < min_side:
            continue
        ar = long / short
        if ar < aspect[0] or ar > aspect[1]:
            continue
        fill = area / float(rw * rh)
        if fill < rect_fill:
            continue
        hull = cv2.convexHull(c)
        hull_area = float(cv2.contourArea(hull))
        sol = area / hull_area if hull_area > 1 else 0.0
        if sol < solidity:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        n = len(approx)
        if n < 4:
            continue
        # ★ 顶点数从"必须=4"放宽：矩形带耳片/圆角会被近似成 6~8 顶点，"只认4角"
        #   会把它们整块拒掉（现场蓝板 ≈6 顶点导致漏检）。改判"是否存在锐角"：
        #   矩形(含耳片)至少有一个内角<115°；纯圆/正多边形近似后各顶点内角≈135°，
        #   无锐角 → 剔除（保持"只认长方形、圆形剔除"）。
        has_corner = False
        for i in range(n):
            a = approx[i % n][0].astype(float)
            b = approx[(i + 1) % n][0].astype(float)
            d = approx[(i + 2) % n][0].astype(float)
            v1, v2 = a - b, d - b
            n1, n2 = float(np.linalg.norm(v1)), float(np.linalg.norm(v2))
            if n1 < 1e-6 or n2 < 1e-6:
                continue
            cosang = float(np.clip(float(np.dot(v1, v2)) / (n1 * n2), -1.0, 1.0))
            if math.degrees(math.acos(cosang)) < 115.0:
                has_corner = True
                break
        if not has_corner:
            continue
        box = cv2.boxPoints(((cx, cy), (rw, rh), ang)).astype(int)
        # ★ rect 必须存成纯 Python float: cv2.minAreaRect 返回的是 numpy 标量,
        #   直接塞进 JSON 会 TypeError（而且是在 json.dumps 阶段炸, 连响应都发不出去）。
        out.append({"box": box,
                    "rect": [[float(cx), float(cy)], [float(rw), float(rh)], float(ang)],
                    "area": area, "fill": round(fill, 3), "solidity": round(sol, 3),
                    "aspect": round(ar, 2)})
    out.sort(key=lambda d: -float(d["area"]))
    return _nms(out, max_items)


def pick_best(results, conf_thr: float = 0.0) -> List[Dict[str, object]]:
    """**只保留唯一目标**：先按置信度阈值过滤，再取面积最大者（面积并列取置信度高者）。

    ★ 需求（对齐参考实现 visual_object_detector.py 的"只报最大的一个"）：
      实时检测只报一个目标，判据是"最大的 + 置信度最高的"：
        1) 先丢掉置信度不达标的候选（含"未知"色，其 conf 恒为 0）；
        2) 在剩下的里取**面积最大**的；
        3) 面积相同（几乎不可能，但保证确定性）时取置信度高的。
      返回长度 0 或 1 的列表，调用方无需再判重。

    ⚠ 注意：面积是"画框时的实际像素面积"，不是填充率；这样"大物料"优先，
      而"小而实"的碎块（如反光斑）不会再顶掉真正的主体。
    """
    cand = []
    for r in (results or []):
        try:
            conf = float((r.get("color") or {}).get("conf") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= conf_thr:
            cand.append((float(r.get("area") or 0.0), conf, r))
    if not cand:
        return []
    cand.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [cand[0][2]]


def debug_mask_contours(work: np.ndarray, mask: np.ndarray, thr=None) -> str:
    """排查用：列出每个连通域的尺寸/填充率/长宽比/颜色/被滤原因。
    ★ 仅在引擎 debug=True 时调用（默认关），逐行打印到服务日志，不参与判色。
    ★ 审计修复 P0-cam-3: 判据与 find_rectangles 用同一张阈值表，否则诊断会"说谎"
      （阈值改了，日志还按旧值报"area超限"，排查会被带偏）。
    """
    t = norm_thr(thr)
    total = float(mask.shape[0] * mask.shape[1]) or 1.0
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    lines = ["[mask 诊断] 连通域数=%d, 有效像素=%d (%.1f%%)"
             % (len(cnts), int(cv2.countNonZero(mask)), cnt_nonzero_pct(mask, total))]
    n = 0
    for c in cnts:
        area = float(cv2.contourArea(c)) or 0.0
        box = cv2.boxPoints(cv2.minAreaRect(c))
        rw, rh = cv2.boundingRect(np.int32(box))[2], cv2.boundingRect(np.int32(box))[3]
        w, h = max(1, rw), max(1, rh)
        fill = area / float(w * h)
        ar = (w / float(h)) if h else 0.0
        if ar < 1.0:
            ar = 1.0 / ar
        hull = cv2.contourArea(cv2.convexHull(c))
        sol = (area / hull) if hull > 0 else 0.0
        reasons = []
        if (area < max(200.0, t["min_area_ratio"] * total)
                or area > t["max_area_ratio"] * total):
            reasons.append("area超限")
        if w < t["min_side"] or h < t["min_side"]:
            reasons.append("边<%d" % int(t["min_side"]))
        if ar < 1.0 or ar > t["aspect_max"]:
            reasons.append("长宽比%.2f超界" % ar)
        if fill < t["min_fill"]:
            reasons.append("填充率%.2f" % fill)
        if sol < t["solidity_min"]:
            reasons.append("实心度%.2f" % sol)
        # 颜色
        cs = ""
        if area > 50.0:
            sub = np.zeros(mask.shape[:2], np.uint8)
            cv2.fillPoly(sub, [np.int32(box)], 255)
            sub = cv2.bitwise_and(sub, mask)
            dom = dominant_lab(work, sub)
            if dom["ok"]:
                cr = classify_rgb(dom["lab"])
                cs = "%s(%.2f)" % (cr["name"], cr["conf"])
            else:
                cs = "(采样不足)"
        lines.append("  #%d area=%d wxh=%dx%d fill=%.2f ar=%.2f sol=%.2f 色=[%s] 滤因=%s"
                     % (n, int(area), w, h, fill, ar, sol, cs, ",".join(reasons) or "-"))
        n += 1
        if n >= 12:
            lines.append("  ... 仅显示前 12 个")
            break
    return "\n".join(lines)


def cnt_nonzero_pct(mask: np.ndarray, total: float) -> float:
    return 100.0 * cv2.countNonZero(mask) / total


def _iou_box(a: np.ndarray, b: np.ndarray) -> float:
    ax1, ay1 = a[:, 0].min(), a[:, 1].min()
    ax2, ay2 = a[:, 0].max(), a[:, 1].max()
    bx1, by1 = b[:, 0].min(), b[:, 1].min()
    bx2, by2 = b[:, 0].max(), b[:, 1].max()
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return float(inter) / float(ua) if ua > 0 else 0.0


def _nms(items: List[Dict[str, object]], limit: int, thr: float = 0.5) -> List[Dict[str, object]]:
    keep: List[Dict[str, object]] = []
    for it in items:
        if all(_iou_box(it["box"], k["box"]) <= thr for k in keep):
            keep.append(it)
        if len(keep) >= limit:
            break
    return keep


# =====================================================================
# 5. 主色提取（掩膜净化 + 中心加权 + kmeans）
# =====================================================================
def dominant_lab(frame_bgr: np.ndarray, mask: np.ndarray,
                 min_pixels: int = 200) -> Dict[str, object]:
    """掩膜内的主色（Lab）。返回 {lab, ratio, valid, ok}。"""
    m = cv2.erode(mask, np.ones((5, 5), np.uint8), iterations=1)
    if cv2.countNonZero(m) < 50:
        m = mask                                     # 物体太小就不腐蚀了
    lab_img = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

    ys, xs = np.nonzero(m)
    if ys.size == 0:
        return {"lab": np.array([0.0, 0.0, 0.0]), "ratio": 0.0, "valid": 0, "ok": False}

    L = lab_img[ys, xs, 0].astype(np.float64) * 100.0 / 255.0
    V = hsv[ys, xs, 2].astype(np.float64)

    # ★ 剔高光 / 剔暗部必须用"相对"判据（分位数），不能用绝对阈值：
    #   旧版 `V < 0.96*255` 会把任何有一个通道饱和的物料整块清空
    #   （橙 #FF7A18 / 黄 / 天蓝 / 粉 / 白 的 V 恒为 255）→ 采样为 0 → 判成"未知"；
    #   旧版 `L > 10` 又会把纯黑物料（L*≈8）清空。
    #   这里按分位数裁掉最亮 4%（镜面高光）与最暗 3%（阴影），
    #   且只在"裁完仍不少于 min_pixels"时才真的裁，避免把目标裁没。
    if ys.size > min_pixels:
        v_hi = float(np.percentile(V, 96.0))
        l_lo = float(np.percentile(L, 3.0))
        keep = (V <= v_hi) & (L >= l_lo)
        n_keep = int(np.count_nonzero(keep))
        if min_pixels <= n_keep < ys.size:
            ys, xs, L = ys[keep], xs[keep], L[keep]
    if ys.size < min_pixels:
        return {"lab": np.array([0.0, 0.0, 0.0]), "ratio": 0.0,
                "valid": int(ys.size), "ok": False}

    # 中心加权采样（抗边缘漏背景）
    cx, cy = float(xs.mean()), float(ys.mean())
    # ★ cv2.minAreaRect 需要"点集"而非二值图：直接传 mask 时 OpenCV 会把它当作
    #   点索引数组解析 → cv::convexHull 断言 (depth==CV_32F||CV_32S) 崩溃。
    #   用 findNonZero 转成 Nx1x2 点集；万一取不到点就退化为包围盒估算。
    pts = cv2.findNonZero(m)
    if pts is not None and len(pts) >= 3:
        (_, (rw, rh), _) = cv2.minAreaRect(pts)
    else:
        rw = float(xs.max() - xs.min() + 1)
        rh = float(ys.max() - ys.min() + 1)
    sigma = max(6.0, 0.35 * max(1.0, min(rw, rh)))
    w = np.exp(-(((xs - cx) ** 2 + (ys - cy) ** 2) / (2.0 * sigma * sigma)))
    w = w / w.sum()
    # ★ np.random.choice 要求 p 的浮点和严格≈1（容差 ~1e-8），
    #   归一化后仍有舍入残差 → 把残差补到最后一个元素上，避免随机抛错。
    if w.size > 1:
        w[-1] = max(0.0, 1.0 - float(w[:-1].sum()))
    n = min(5000, ys.size)
    idx = np.random.choice(ys.size, size=n, replace=False, p=w)

    samples = lab_img[ys[idx], xs[idx], :].astype(np.float32)
    samples[:, 0] *= 100.0 / 255.0
    samples[:, 1] -= 128.0
    samples[:, 2] -= 128.0

    k = 3 if samples.shape[0] >= 30 else 1
    if k == 1:
        lab = np.median(samples, axis=0)
        return {"lab": lab, "ratio": 1.0, "valid": int(ys.size), "ok": True}

    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
    _, labels, centers = cv2.kmeans(samples, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    labels = labels.ravel()
    counts = np.bincount(labels, minlength=k)
    big = int(np.argmax(counts))
    ratio = float(counts[big]) / float(labels.size)
    lab = np.median(samples[labels == big], axis=0)     # 中位比均值抗离群
    return {"lab": lab, "ratio": round(ratio, 3), "valid": int(ys.size), "ok": True}


# =====================================================================
# 6. 稳定判定状态机（1 秒停留只触发一次）
# =====================================================================
class StabilityTracker:
    """EMPTY → TRACKING(连续 N 帧稳定) → CAPTURED → LEAVE → EMPTY。"""

    def __init__(self, stable_frames: int = 3, move_px: float = 3.0,
                 area_tol: float = 0.08, iou_thr: float = 0.85,
                 leave_frames: int = 10):
        self.stable_frames = stable_frames
        self.move_px = move_px
        self.area_tol = area_tol
        self.iou_thr = iou_thr
        self.leave_frames = leave_frames
        self.reset()

    def reset(self):
        self.state = "EMPTY"
        self.box = None
        self.hits = 0
        self.miss = 0
        self.captured = False

    def update(self, rects: List[Dict[str, object]]) -> Dict[str, object]:
        """喂入当前帧的候选框；返回 {state, fire, box, hits}。fire=True 表示本帧触发一次记录。"""
        if not rects:
            self.miss += 1
            if self.state != "EMPTY" and self.miss >= self.leave_frames:
                self.reset()
            return {"state": self.state, "fire": False, "box": None, "hits": self.hits}

        cand = rects[0]                     # 已按面积降序，取最大
        box = cand["box"]
        if self.box is None:
            self._start(box)
            return {"state": self.state, "fire": False, "box": box, "hits": self.hits}

        iou = _iou_box(box, self.box)
        (cx, cy) = _centroid(box)
        (px, py) = _centroid(self.box)
        moved = float(np.hypot(cx - px, cy - py))
        a0, a1 = float(cand["area"]), float(self._area or 1.0)
        area_ok = abs(a1 - a0) / max(a0, 1.0) < self.area_tol

        if iou > self.iou_thr and moved < self.move_px and area_ok:
            self.hits += 1
            self.box = box
            self._area = a0
        else:
            # 目标明显动了 → 视为新物体，重新计数
            self._start(box)
            return {"state": self.state, "fire": False, "box": box, "hits": self.hits}

        fire = False
        if self.hits >= self.stable_frames:
            self.state = "CAPTURED"
            # ★ 实时模式: 只要达标目标持续在场就持续触发(不再"触发一次后必须离开才复位")，
            #   帧率节流交给上层 color_min_interval 控制, 避免每个对象只报一次造成"偶尔才检测"。
            fire = True
        return {"state": self.state, "fire": fire, "box": box, "hits": self.hits}

    def _start(self, box):
        self.box = box
        self.hits = 1
        self.miss = 0
        self.captured = False
        self.state = "TRACKING"
        self._area = float(cv2.contourArea(box.astype(np.float32)))

    _area = 1.0


def _centroid(box: np.ndarray) -> Tuple[float, float]:
    return float(box[:, 0].mean()), float(box[:, 1].mean())


# =====================================================================
# 7. 引擎（带 MOG2 背景 + 稳定器；脱离 HTTP 可独立使用）
# =====================================================================
class VisionEngine:
    def __init__(self, background: Optional[np.ndarray] = None, use_mog2: bool = True,
                 roi: Optional[Tuple[int, int, int, int]] = None,
                 calib: Optional[Dict[str, object]] = None,
                 use_hsv_mask: bool = True,
                 debug: bool = False,
                 thr: Optional[Dict[str, object]] = None,
                 color_only: bool = True):
        self.background = None if background is None else background.copy()
        self.use_mog2 = use_mog2 and background is None
        self.use_hsv = use_hsv_mask          # HSV 彩色前景增强(对鲜艳物体可靠兜底)
        # ★ 参考实现（visual_object_detector.py）的核心简化：**只用颜色阈值**做前景，
        #   不做背景差分/MOG2。原因：我们只认红/绿/蓝三色矩形，而"高饱和"这一条
        #   本身就与背景无关，直接用 HSV 掩膜既稳又快；背景差分反而会因
        #   "物体色≈背景色"或静止不出前景而漏检（现场"偶尔才检测到"的根因之一）。
        #   ★ 默认 True（2026-09-25 起）= **实测路径与生产路径一致**：以前默认走背景差分，
        #     而相机服务显式传 color_only=True 走颜色阈值 → "测的"和"跑的"不是同一条，
        #     现场问题在离线回归里复现不出来。现在两边同源。
        #     显式设了静态背景时相机服务会传 False（那才需要差分）。
        self.color_only = bool(color_only)
        self.debug = debug                   # 诊断模式: 逐帧打印 mask 连通域, 默认关
        # ★ 审计修复 P0-cam-3: 检测阈值随引擎携带（相机服务改配置时重建引擎即生效）。
        #   缺省 = DEFAULT_THR 原值；传入的部分覆盖由 norm_thr 合并+限幅。
        self.thr = norm_thr(thr)
        self.mog2 = (cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=16,
                                                        detectShadows=True) if self.use_mog2 else None)
        self.roi = roi
        self.calib = calib or {}
        self.tracker = StabilityTracker(stable_frames=2)
        self.card = dict(CARD_LAB)
        self._apply_calib_card()

    def _apply_calib_card(self):
        refs = self.calib.get("card") or {}
        for name, lab in refs.items():
            if name in self.card and isinstance(lab, (list, tuple)) and len(lab) == 3:
                self.card[name] = np.array([float(x) for x in lab], dtype=np.float64)

    def apply_gains(self, bgr: np.ndarray) -> np.ndarray:
        """白平衡通道增益（灰卡标定产物）。"""
        g = self.calib.get("gains")
        if not g:
            return bgr
        out = bgr.astype(np.float32)
        out[:, :, 0] *= float(g[0]); out[:, :, 1] *= float(g[1]); out[:, :, 2] *= float(g[2])
        return np.clip(out, 0, 255).astype(np.uint8)

    def detect(self, frame_bgr: np.ndarray, record_boxes: bool = True) -> Dict[str, object]:
        """单帧完整流水线。返回 {ok, results[...], mask, tracker}（不落盘、不画图）。

        ★ payload 里带着 mask 的引用（不拷贝）：调用方想存档"掩膜图"直接用，
          不想要就 pop 掉。避免为了存档再跑一遍分割。
        """
        frame = self.apply_gains(frame_bgr)
        if self.roi:
            x, y, w, h = self.roi
            work = frame[y:y + h, x:x + w]
        else:
            work = frame
        # ★ color_only（参考实现口径）：前景**只用颜色阈值**，不做背景差分/MOG2。
        #   只认红/绿/蓝三色矩形时，高饱和与背景无关，这条最稳、最快。
        if self.color_only:
            mask = hsv_color_mask(work, sat_min=int(self.thr["mask_sat_min"]),
                                  val_min=int(self.thr["mask_val_min"]))
        else:
            mask = self._mask(work)
            # ★ HSV 彩色前景增强：背景差分对"物体色≈背景色"会漏检，
            #   鲜艳物体的高饱和特征与背景无关，OR 上去做可靠兜底。
            if self.use_hsv:
                mask = cv2.bitwise_or(mask, hsv_color_mask(
                    work, sat_min=int(self.thr["mask_sat_min"]),
                    val_min=int(self.thr["mask_val_min"])))
        # ★ 审计修复 P0-cam-3: 面积/短边门槛走可配置阈值表（算法结构未变）
        rects = find_rectangles(mask,
                                min_area_ratio=self.thr["min_area_ratio"],
                                max_area_ratio=self.thr["max_area_ratio"],
                                min_side=int(self.thr["min_side"]),
                                min_area_abs=self.thr["min_area_abs"],
                                rect_fill=self.thr["min_fill"],
                                solidity=self.thr["solidity_min"],
                                aspect=(1.0, self.thr["aspect_max"]))
        results = []
        for r in rects:
            col = self._color(work, mask, r)
            results.append({
                "rect": r["rect"], "box": r["box"].tolist(), "area": r["area"],
                "color": col,
                "center": _centroid(r["box"]),
            })
        st = self.tracker.update(rects)
        if self.debug:
            import sys
            print(debug_mask_contours(work, mask, thr=self.thr),
                  flush=True, file=sys.stderr)
        payload = {"ok": True, "results": results, "tracker": st, "mask": mask}
        if record_boxes and st["fire"] and results:
            payload["fire"] = True
        return payload

    def _mask(self, work: np.ndarray) -> np.ndarray:
        if self.background is not None:
            if self.background.shape[:2] != work.shape[:2]:
                bg = cv2.resize(self.background, (work.shape[1], work.shape[0]))
            else:
                bg = self.background
            return diff_mask(self.apply_gains(work), self.apply_gains(bg))
        if self.mog2 is not None:
            fg = self.mog2.apply(work, learningRate=0.002)
            _, m = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)   # 去掉 shadow(127)
            k = np.ones((5, 5), np.uint8)
            m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
            return m
        # 无背景：用大津阈值兜底（背景与物体有反差时可用）
        gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
        _, m = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return m

    def _color(self, work: np.ndarray, mask: np.ndarray, r: Dict[str, object]) -> Dict[str, object]:
        box = r["box"].astype(np.int32)
        sub = np.zeros(mask.shape[:2], np.uint8)
        cv2.fillPoly(sub, [box], 255)
        sub = cv2.bitwise_and(sub, mask)            # 候选框内掩膜像素
        ys, xs = np.nonzero(sub)
        fill = float(r.get("fill") or 0.0)
        if ys.size == 0:
            return {"name": "未知", "hex": None, "conf": 0.0, "alt": None,
                    "reason": "采样不足", "ratio": 0.0, "fill": round(fill, 3),
                    "lab": [0.0, 0.0, 0.0]}
        # 取该候选 bounding 矩形内的 BGR，用 HSV 色相分箱直判红/绿/蓝
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        roi = work[y0:y1 + 1, x0:x1 + 1]
        # ★ 审计修复 P0-cam-3: 色相投票的 S/V 门槛走可配置阈值表
        name, ratio_color = _hue_vote(roi, s_min=self.thr["hue_s_min"],
                                      v_min=self.thr["hue_v_min"])
        dom = dominant_lab(work, sub)
        de = None
        alt = None
        alt_de = None
        chroma = 0.0
        name_l = "未知"
        lab_conf = 0.0
        lab_arr = [round(float(v), 2)
                   for v in cv2.cvtColor(roi, cv2.COLOR_BGR2LAB).reshape(-1, 3).mean(axis=0)]
        if dom["ok"]:
            cr = classify_rgb(dom["lab"])
            name_l = cr["name"]
            de = cr["de"]
            alt = cr["alt"]
            alt_de = cr["alt_de"]
            chroma = cr["chroma"]
            lab_conf = cr["conf"]
            lab_arr = [round(float(v), 2) for v in dom["lab"]]
        if not name:
            return {"name": "未知", "hex": None, "conf": 0.0, "alt": alt,
                    "reason": "非红绿蓝三色", "ratio": round(fill, 3),
                    "fill": round(fill, 3), "lab": lab_arr,
                    "de": de, "alt_de": alt_de, "chroma": chroma}
        # 置信度 = HSV 一致性 × 方正度，再与 Lab ΔE 置信取较严者（交叉门控更稳）
        hsv_conf = float(ratio_color * (0.5 + 0.5 * min(1.0, max(0.0, fill))))
        conf = round(float(min(hsv_conf, lab_conf)), 3) if dom["ok"] else round(hsv_conf, 3)
        reason = ""
        if name_l != name:
            reason = "HSV/Lab 分歧(%s)" % name_l
        elif fill < 0.72:
            reason = "杂色（建议复核）"
        return {"name": name, "hex": {"红": "#E02020", "绿": "#1FA74A", "蓝": "#1F4FD8"}[name],
                "conf": conf, "alt": alt,
                "reason": reason,
                "ratio": round(fill, 3), "fill": round(fill, 3), "lab": lab_arr,
                "de": de, "alt_de": alt_de, "chroma": chroma}


# =====================================================================
# 8. 标定文件读写
# =====================================================================
def load_calib(path: str) -> Dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_calib(path: str, calib: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(calib, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def gray_card_gains(patch_bgr: np.ndarray, target: float = 118.0) -> List[float]:
    """用 18% 灰卡的实测中位值算三通道增益（把灰卡拉到 18% 中灰）。"""
    med = np.median(np.asarray(patch_bgr, dtype=np.float64).reshape(-1, 3), axis=0)
    med = np.maximum(med, 1.0)
    return [float(np.clip(target / med[2], 0.3, 3.0)),   # B
            float(np.clip(target / med[1], 0.3, 3.0)),   # G
            float(np.clip(target / med[0], 0.3, 3.0))]   # R
