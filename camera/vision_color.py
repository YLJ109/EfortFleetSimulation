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
  - 稳定：连续 N 帧"质心不动 + 框稳定"才触发一次，保证 1 秒停留只记录/播报一次；
  - 七色调色板（2026-10-10）：可识别色 = 红/橙/黄/绿/青/蓝/紫（`PALETTE_NAMES`）。
          检测器**只如实报"是什么颜色"**；"有效 / 无效"由上层（用户指令）决定 ——
          用户没提到的颜色 + 无彩色(黑) → 原路放回原位（见 backend/services/sorting.py）。
  - 暗色兜底（2026-10-10）：黑/深色物体在彩色掩膜里往往是"零像素"（或只有少量带色偏的
          像素），会被报成"检测不到"或被误判成蓝/青；用户口径是「黑色 = 无效颜色，
          要能识别到并原路放回原位」。做法（只影响"暗/彩如何共存"，**彩色判据不动**）：
            ① 彩色候选**一个都不丢** —— 其中"暗 **且** 不艳（低色度/低饱和）"的改判为「黑」；
            ② **没有**任何彩色候选时，才用低亮度掩膜找一遍并报「黑」。
          ★「暗 **且** 不艳」是关键：只按"暗"改判会把**暗绿/暗蓝**误判成黑
            （dark_object_mask 的阈值在亮背景下会顶到 130，暗绿 V≈87 也落进去）。
          ⚠ 不做"按面积用暗色候选顶掉彩色候选"：C 位背景有大块暗矩形（显示器等），会误吞彩料。

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
# 颜色名与"有效/无效"的边界（2026-10-10 七色重构后见下方）
# =====================================================================
# ★★ 2026-10-10 用户口径："把 7 种颜色都能识别" + "只要用户说的颜色是有效颜色，
#     用户没有提到的颜色的是无效颜色"。
#   · **调色板（可识别色）** = 7 色：红/橙/黄/绿/青/蓝/紫 —— 检测器能给出的颜色名；
#   · "有效 / 无效"**不在这里定**：由上层按用户指令动态划（见 backend/services/sorting.py
#     的 `vote_color(..., valid_colors=...)`）；检测器**只如实报"是什么颜色"**。
#   · RGB_NAMES 保留：既是调色板子集，也是旧调用方 / classify_rgb / ColorSampler 的兼容名。
PALETTE_NAMES = ("红", "橙", "黄", "绿", "青", "蓝", "紫")

RGB_NAMES = ("红", "绿", "蓝")
DARK_NAME = "黑"                          # 无彩色（"确信不是 7 色之一"）；永远不是有效色
TARGET_NAMES = PALETTE_NAMES + (DARK_NAME,)   # 界面展示用：七色 + 无彩色

# 调色板各色展示 hex（判色结果回给界面画框用；黑无颜色 → None）
PALETTE_HEX: Dict[str, Optional[str]] = {
    "红": "#E02020", "橙": "#FF7A18", "黄": "#FFD400", "绿": "#1FA74A",
    "青": "#00BFC8", "蓝": "#1F4FD8", "紫": "#8A3FC0",
}

# 色相带（OpenCV H ∈ [0,180)）：7 色**纯划分**（无缝隙、无重叠），红带跨 0° 环绕。
#   ★★ 边界不是几何中点，而是**照顾现场实测**（见 docs/方案-七色识别与有效色集）：
#     现场实测：**绿 H≈69~70（光照/白平衡漂移可达 80+）、蓝 H≈104~106**。
#     · 绿带上限取 **88**（不是理论中点 75）：★ 2026-10-10 现场反馈"绿被判成青"，
#       根因就是上一版把分界放在 75 —— 离实测绿(69~70)只有 5 格，一漂就翻边。
#       现退到 88 → 绿有 ≥18 格余量；蓝仍从 100 起（护住蓝 104~106）。
#     · 青带 = [88,100)：只留给"真青"（H≈91 的强青），不抢绿。
#     橙/黄/紫 占用原来"都不认"的空档（12~35 / 135~160）。
HUE_BANDS: Tuple[Tuple[str, float, float], ...] = (
    ("红", 0.0, 12.0), ("红", 160.0, 180.0),
    ("橙", 12.0, 22.0),
    ("黄", 22.0, 35.0),
    ("绿", 35.0, 88.0),
    ("青", 88.0, 100.0),
    ("蓝", 100.0, 135.0),
    ("紫", 135.0, 160.0),
)

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
    # ★★ 2026-10-08 新增（用户口径"只需要识别矩形"）：**矩形硬门槛**。
    #   与上面的 min_fill(0.45) 不是一回事：
    #     · min_fill = **能否成为候选**（放宽，容忍遮挡/只露一部分，否则会整块漏检）；
    #     · fill_min = **能否给出颜色结论**（更严：形状明显不像矩形就别报颜色）。
    #   ⚠ 该值算的是 area/(minAreaRect 长×宽)，**与旋转无关**（斜着举的方块不会因此被误杀）。
    #
    #   ★★ 2026-10-08 修正（首次定 0.72 **太高，现场误杀**）：
    #     0.72 是只看**合成图**定出来的（合成图 fill≈0.95+），而现场相机图受
    #     透视斜视 + 边缘模糊 + 轻微反光影响，**真矩形的 fill 只有 0.67~0.75**：
    #       · 现场 miss 记录：0.68、0.71（"非矩形"→ 不给颜色 → 分拣按"检测不到"停）
    #       · 现场成功记录：0.745（蓝色，刚刚过线）
    #     0.72 正好卡在真实分布**正中间** → 表现就是"有时能检测、有时检测不到"，
    #     绿色/蓝色最先中招（fill 偏低）。
    #     现在取 **0.55**：比现场实测最低值 0.67 再低 0.12 留足余量，
    #     同时仍高于三角形（minAreaRect fill≈0.50）—— 明显不是矩形的照样拒。
    #   ⚠ 该值的定法**只能是"低于现场实测最低值"**，不能按合成图定。改之前先跑
    #     tools/vision_replay_test.py（真图回放）确认三色检出率不掉。
    #   ⚠ 其它副作用：真被遮住一半的方块会被判非矩形 → 不输出颜色 → 分拣按"检测不到"停。
    #     现场觉得太严就用面板调低（走 /vision/config，不必改代码）。
    "fill_min": 0.55,
    # ★ 软提示线（不拦检、只写 reason 提醒复核）：fill 低于它就标注"杂色（建议复核）"。
    #   与 fill_min 解耦 —— 以前两者共用一个 0.72，调了一个另一个跟着动，很容易改错。
    "fill_warn": 0.72,
    # ★★ 2026-10-10 用户口径："黑色是无效颜色，要能识别到并放回原位"。
    #   纯黑/深色物体**饱和度和亮度都低** → hsv_color_mask（高饱和 + 不过暗）一个像素都不收
    #   → 现场表现是"检测不到（没有彩色块）"，而不是"无效颜色"。
    #   这里给一条**暗色兜底**：只在"彩色候选一个都没有"时才用它再找一遍（见 VisionEngine.detect）；
    #   找到就按普通候选上报，判色必然给"未知"（非红绿蓝）→ 走既有的"原路放回原位"。
    #
    #   ★★ 判据是**相对**的（2026-10-10 实测黑块实物照后改）：
    #     哑光黑实物在光下 V 实测 **58~105**（不是 20），固定阈值 55 会把料切碎 → 检不出；
    #     而背景（桌面 187）远亮于它 —— 所以按"**明显暗于画面背景亮度**"来判：
    #         thr = min(dark_val_max, 背景亮度中位 × dark_ratio)
    #     背景不够亮（对比 < dark_min_contrast）时**直接不启用** —— 宁可不猜，也不误报。
    #   ⚠ 默认值仍需**现场真图**复核（C 位取景）；可用 /vision/config 覆盖。
    "dark_val_max": 130.0,     # 暗色兜底的**绝对上限**（够亮背景下不把中灰也算进来）
    "dark_ratio": 0.70,        # 背景亮度中位的比例：低于它的像素算"暗色物体"
    "dark_min_contrast": 25.0,  # 背景亮度 − 该阈值 至少要差这么多，否则不启用
    # ★ 暗色候选的**置信度**。★ 语义要说清：这里表达的是"**确信它不是红/绿/蓝**"
    #   （无效颜色），不是"颜色判对了"。上游（camera_service.pick_best / 后端 vote_color）
    #   拿 conf 当门 —— 给 0 会被直接滤掉，本支路就白加了，现场又变回"检测不到"。
    #   注意：后端 vote_color 只认"name ∈ 红/绿/蓝"，所以给多高都**不会**被当成有效颜色，
    #   只会让"无效颜色"这个**原因**如实传到分拣流程（→ 原路放回原位）。
    "dark_conf": 0.5,
    # ★★ 2026-10-10 七色重构：**"黑"必须"暗 且 不艳"**（修现场"绿色检测不到"）。
    #   旧规则只看"暗"（`_dark_self_frac ≥ 0.5`）→ 亮背景下 dark_object_mask 的阈值顶到 130，
    #   暗绿(V≈87)也落进去 → 误判成黑。现在按候选 ROI 的中位统计：
    #       V 暗 **且**（Lab 色度低 **或** 饱和度低）→ 才判「黑」。
    #   真图实测余量（docs/方案-七色识别与有效色集 §7）：彩色件最低 chroma=18.0 / S=96；
    #   暗色件最高 chroma=5.0 / S=62 → 阈值取中缝，两侧各留 ≥6 余量。
    "dark_chroma_max": 12.0,   # 判"黑"的 Lab 色度上限（≤ 它 = 不艳）
    "dark_s_max": 80.0,        # 判"黑"的 HSV 饱和度上限（≤ 它 = 不艳）
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
    "fill_min": (0.0, 1.0),      # 0 = 关闭该硬门槛（现场可据此完全退回旧行为）
    "fill_warn": (0.0, 1.0),     # 软提示线（只写 reason，不拦检）
    "dark_val_max": (0.0, 255.0),  # 暗色兜底亮度上限（绝对）
    "dark_ratio": (0.05, 1.0),     # 背景亮度中位的比例
    "dark_min_contrast": (0.0, 255.0),  # 背景与阈值的最小亮度差
    "dark_conf": (0.0, 1.0),       # 暗色候选置信度（"确信不是三色"）
    "dark_chroma_max": (0.0, 128.0),  # 判"黑"的 Lab 色度上限
    "dark_s_max": (0.0, 255.0),    # 判"黑"的 HSV 饱和度上限
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


def _palette_vote(bgr_roi, s_min=None, v_min=None):
    """逐像素色相分箱投票判**调色板七色**（红/橙/黄/绿/青/蓝/紫）。返回 (name, ratio)。

    ratio = 多数色带像素数 / 彩色像素数（该候选色相的一致性）。
    ★ 关键：H 是环形标尺(0°≡180°)，直接 `mean()` 会把"红带的 0° 端 + 176° 端"
      平均拉到约 40°（青/绿带）→ 误判绿。逐像素分箱投票从根本上避开环形均值陷阱。
    ★ 七色带由 `HUE_BANDS` 一处定义，**纯划分、无缝隙、无重叠**（红带跨 0° 累加两段）。
      现场实测 红(H2~4)/绿(H69~70)/蓝(H104~106) 全部落在各自带内 → 三色零回归。
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
    counts: Dict[str, int] = {}
    for nm, lo, hi in HUE_BANDS:
        counts[nm] = counts.get(nm, 0) + int(((H >= lo) & (H < hi)).sum())
    # 按带序取最大（红在 HUE_BANDS 里排最前 → 并列时稳定偏向红，与旧行为一致）
    name, m = "", 0
    for nm, _lo, _hi in HUE_BANDS:
        if counts[nm] > m:
            name, m = nm, counts[nm]
    if m == 0:
        return ("", 0.0)                       # 全落在七色带之外 → 非彩色
    return (name, float(m) / float(n))


def _hue_vote(bgr_roi, s_min=None, v_min=None):
    """旧接口：**只认红/绿/蓝**（非三色返回空串）。内部已走七色调色板投票。

    ★ 保留它是为了不惊动旧调用方（tools/hard_test.py 等）。新代码请直接用
      `_palette_vote`（七色）—— 注意：偏青的物体（H75~100）现在归**青**，
      不再是旧口径的"归绿"。
    """
    name, ratio = _palette_vote(bgr_roi, s_min=s_min, v_min=v_min)
    return (name if name in RGB_NAMES else "", ratio)


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

    ★★ 2026-10-09 修复（**现场"实物标定"以前是白做的**）：
      `card` 参数从有这个函数起就**完全没被使用**（函数体写死 `RGB_CARD_LAB`，
      注释还写着"不受标定覆盖影响"）。而现场标定正是往 `calib["card"]` 里写实测 Lab
      —— 于是**标了也不生效**：界面提示"已用实测样本标定「红」"，判色却仍按标准色卡走。
      现在：**传了现场卡就用现场卡**，没传才回落标准色卡（保持原有默认行为）。
      只认 红/绿/蓝 三个键，缺的颜色用标准卡补齐（现场只标了一色也不会 KeyError）。
    """
    L, A, B = float(lab[0]), float(lab[1]), float(lab[2])
    chroma = float(np.hypot(A, B))
    # 参考表：现场实测卡优先，缺项用标准卡补
    tbl = dict(RGB_CARD_LAB)
    if isinstance(card, dict):
        for k, v in card.items():
            if k in RGB_NAMES and v is not None:
                arr = np.asarray(v, dtype=np.float64).ravel()
                if arr.size >= 3 and np.all(np.isfinite(arr[:3])):
                    tbl[k] = arr[:3]
    # 低色度（灰/黑/白）：不属于三色目标
    if chroma < CHROMA_CUT:
        return {"name": "未知", "hex": None, "de": None,
                "conf": 0.0, "alt": None, "alt_de": None, "chroma": round(chroma, 1)}
    names = [n for n in RGB_NAMES if n in tbl]
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


def classify_palette(lab: Sequence[float], card: Optional[Dict[str, np.ndarray]] = None
                     ) -> Dict[str, object]:
    """**七色调色板**判定：只在 红/橙/黄/绿/青/蓝/紫 里做 ΔE 最近邻（**复核/提示**用）。

    与 classify_rgb 的关系：classify_rgb 只认三色（旧路径保留）；本函数覆盖 7 色，
    供 `VisionEngine._color` 与 HSV 投票**互相印证**（一致→置信度略升，分歧→降）。
    低色度（灰/黑/白）→ "未知"。现场实测卡优先，缺项用标准卡补（不会 KeyError）。
    """
    L, A, B = float(lab[0]), float(lab[1]), float(lab[2])
    chroma = float(np.hypot(A, B))
    tbl = dict(PALETTE_CARD_LAB)
    if isinstance(card, dict):
        for k, v in card.items():
            if k in PALETTE_NAMES and v is not None:
                arr = np.asarray(v, dtype=np.float64).ravel()
                if arr.size >= 3 and np.all(np.isfinite(arr[:3])):
                    tbl[k] = arr[:3]
    if chroma < CHROMA_CUT:
        return {"name": "未知", "hex": None, "de": None,
                "conf": 0.0, "alt": None, "alt_de": None, "chroma": round(chroma, 1)}
    names = [n for n in PALETTE_NAMES if n in tbl]
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
        if de2 - de1 < 10.0:
            alt, alt_de = names[i2], round(de2, 2)
    return {"name": name, "hex": PALETTE_HEX.get(name), "de": round(de1, 2),
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


def dark_object_mask(bgr: np.ndarray, val_max=None, ratio=None, min_contrast=None,
                     morph: int = 5) -> np.ndarray:
    """**暗色物体**掩膜（黑/深灰等低亮度物体）—— 彩色掩膜之外的**兜底支路**。

    ★ 为什么要有它（2026-10-10 用户口径："黑色是无效颜色，要能识别到"）：
      `hsv_color_mask` 只收"高饱和 + 不过暗"的像素，纯黑物体**两头都不满足**
      → 整块一个像素都进不了掩膜 → 现场表现成"检测不到（没有彩色块）"，
      而不是"无效颜色"。
    ★★ 判据是**相对**的（实测黑块实物照后的修正）：
      哑光黑实物在光下 V 实测 58~105（不是 20 那种"纯黑"），**固定阈值必然切碎**；
      而背景通常远亮于它。所以：
          thr = min(val_max, 背景亮度中位 × ratio)
      且要求"背景 − thr ≥ min_contrast"（背景不够亮就**不启用** —— 宁可不猜，不误报）。
    ★ 只在"彩色候选一个都没有"时才启用（见 VisionEngine.detect）：
      红/绿/蓝这条路径**零改动**，不存在把三色判丢的风险。
    """
    if val_max is None:
        val_max = float(DEFAULT_THR["dark_val_max"])
    if ratio is None:
        ratio = float(DEFAULT_THR["dark_ratio"])
    if min_contrast is None:
        min_contrast = float(DEFAULT_THR["dark_min_contrast"])
    h, w = bgr.shape[:2]
    if h * w == 0:
        return np.zeros((h, w), np.uint8)
    blurred = cv2.GaussianBlur(bgr, (5, 5), 0)
    hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
    v = hsv[..., 2].astype(np.float64)
    bg = float(np.median(v))                       # 画面主体是背景（暗物体占比小）
    thr = min(float(val_max), bg * float(ratio))
    if bg - thr < float(min_contrast):
        # 背景本身就不亮 → 分不出"暗物体"，直接不启用（避免整屏一片黑全被当成物体）
        return np.zeros((h, w), np.uint8)
    mask = (v <= thr).astype(np.uint8) * 255
    k = np.ones((morph, morph), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    return mask


# "它其实是暗色物体吗" 的判据 = **暗 且 不艳**（见 _looks_dark）。
# ★★ 为什么这么改（2026-10-10 用户口径两连击）：
#   ① "把黑色物体识别成蓝色了，应该是黑色" —— 黑块在灯下常有蓝/青色偏，会混进彩色掩膜被投成"蓝"；
#   ② 但旧的"只看暗"（`_dark_self_frac ≥ 0.5`）**太宽** —— 亮背景下 dark_object_mask 阈值顶到 130，
#      **暗绿/暗蓝也落进去** → 现场"绿色检测不到"。故必须叠加"不艳"（低色度/低饱和）。


def _force_dark_col(col: Optional[Dict], thr: Optional[Dict] = None) -> Dict:
    """把候选的判色结果**改写**成第 4 色「黑（无效颜色）」。

    ★ 语义：`name="黑"` 表达的是"**确信它不是红/绿/蓝**"，不是"判出了有用颜色"。
      后端 `services/sorting.vote_color` 只认红/绿/蓝 → 它仍走"原路放回原位"。
    ★ conf 必须能过上游门槛（`camera_service.pick_best`）—— 否则刚检出的候选又被滤掉，
      现场又变回"检测不到"。给 `dark_conf`（默认 0.5），只代表"确信不是三色"。
    """
    out = dict(col or {})
    out["name"] = DARK_NAME
    out["reason"] = "无效颜色（黑/暗色，非红绿蓝）"
    out["dark"] = True
    out["hex"] = None
    out["conf"] = round(float((thr or {}).get("dark_conf") or 0.0), 3)
    if out.get("lab") is None:
        out["lab"] = [0.0, 0.0, 0.0]
    return out


def _roi_medians(bgr: np.ndarray, box) -> Tuple[float, float, float]:
    """候选**框内**像素的中位 (V, S, Lab 色度)。

    ★ 用**框内**（含少量背景）而不是"仅彩色掩膜像素"：黑块的彩色掩膜像素本身 S≥门槛
      （>=mask_sat_min），拿它算 S 必然偏高、分不出黑；框内大多数像素是"没进掩膜的暗面/暗边"，
      才代表这块物体真实的"艳不艳"。真图实测：彩色件框内 S=96~145、暗件 S=9~62 → 分得开。
    """
    m = np.zeros(bgr.shape[:2], np.uint8)
    cv2.fillPoly(m, [np.int32(box)], 255)
    ys, xs = np.nonzero(m)
    if ys.size == 0:
        return (0.0, 0.0, 0.0)
    px = bgr[ys, xs]
    hsv = cv2.cvtColor(px.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    lab = cv2.cvtColor(px.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float64)
    a = lab[:, 1] - 128.0
    b = lab[:, 2] - 128.0
    chroma = np.hypot(a, b)
    return (float(np.median(hsv[:, 2])), float(np.median(hsv[:, 1])), float(np.median(chroma)))


def _roi_hue(bgr_roi, s_min=None, v_min=None) -> float:
    """候选 ROI 里"彩色像素"（S/V 过门）的**色相中位**（OpenCV H，0~179）。诊断用。

    ★ 为什么要它（2026-10-10 现场"绿被判青"的教训）：判色在"绿↔青"之间摇摆时，
      光看色名分不清是"色相真的到青了"还是"取景里混进了别的东西"。
      把 H 如实报出来（进 `/vision/detect` 与落盘 JSON），一眼就能定界、不用猜。
    无彩色像素（<40）→ -1.0（表示"给不出色相"）。
    """
    if bgr_roi is None or bgr_roi.size == 0:
        return -1.0
    s_lim = HUE_S_MIN if s_min is None else float(s_min)
    v_lim = HUE_V_MIN if v_min is None else float(v_min)
    hsv = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2HSV)
    sel = (hsv[..., 1] >= s_lim) & (hsv[..., 2] >= v_lim)
    if int(sel.sum()) < 40:
        return -1.0
    return float(np.median(hsv[..., 0][sel]))


def _looks_dark(bgr: np.ndarray, box, thr: Optional[Dict] = None) -> bool:
    """候选是不是"暗色物体（黑）" —— **暗 且 不艳**两条件同时成立。

    ★ 为什么不能只看"暗"（2026-10-10 现场 bug）：暗绿/暗蓝件 V 低（真图 V≈87），
      亮背景下 dark_object_mask 的阈值顶到 130 → 也被判"暗" → 误判成黑（"绿色检测不到"）。
      补上"不艳"（低 Lab 色度 **或** 低 HSV 饱和）后：彩色件 chroma≥18、S≥96 → 保留本色；
      暗件 chroma≤5、S≤62 → 判黑（真图实测，见 docs/方案-七色识别与有效色集 §4）。
    """
    t = thr or DEFAULT_THR
    v_med, s_med, c_med = _roi_medians(bgr, box)
    if v_med > float(t.get("dark_val_max", DEFAULT_THR["dark_val_max"])):
        return False                                   # 不够暗 → 不是黑
    if c_med <= float(t.get("dark_chroma_max", DEFAULT_THR["dark_chroma_max"])):
        return True                                    # 暗且低色度（消色）→ 黑
    return s_med <= float(t.get("dark_s_max", DEFAULT_THR["dark_s_max"]))


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

# 预计算七色调色板 Lab（供 classify_palette 复核）
PALETTE_CARD_LAB: Dict[str, np.ndarray] = {
    n: srgb_to_lab(np.array(hex_to_rgb(h), dtype=np.float64))
    for n, h in PALETTE_HEX.items()
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


def explain_no_candidate(mask: np.ndarray, thr: Optional[Dict[str, object]] = None) -> str:
    """掩膜里有彩色块、却**一个候选都没通过**时，说清"最可能是哪一条拦掉的"。

    ★ 为什么必须有它（2026-10-08 现场实测得出结论）：
      "检测不到"这句话对现场毫无指导意义 —— 现场需要知道**下一步该动什么**：
        · "物体超出画面" → 把方块往里挪，别怼到镜头前；
        · "物体太小"     → 把方块放近/调焦距；
        · "形状不完整"   → 拿开遮挡物 / 换个角度；
        · "颜色不在三色" → 确认拿的是红/绿/蓝料。
      以前这些情况一律报"未发现候选（可能太小/太暗/颜色不在红绿蓝）"，
      现场只能瞎试。这里按**优先级**逐条给出具体量值与建议。

    ★ 判定顺序刻意与 `find_rectangles` 的门槛链一致（面积 → 短边 → 长宽比 → 填充率 → 实心度），
      这样"它说的话"与"实际拦它的那一条"是同一个判据，不会指错方向。
    ★ 画面裁切（贴边）放在**最前面**：实测里这是最常见的真因，而且和其它项会同时成立
      （被裁切的形状天然 fill/solidity 都低），先说它才不会把人往"形状不好"上带偏。
    """
    t = norm_thr(thr)
    h, w = mask.shape[:2]
    total = float(h * w)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return "画面里没有彩色块（目标太暗 / 颜色不在红绿蓝 / 镜头被挡）"
    c = max(cnts, key=cv2.contourArea)
    area = float(cv2.contourArea(c))
    if area < 1:
        return "画面里没有彩色块（目标太暗 / 颜色不在红绿蓝 / 镜头被挡）"
    (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
    short, long = min(rw, rh), max(rw, rh)
    fill = area / float(rw * rh) if rw * rh > 0 else 0.0
    hull = cv2.convexHull(c)
    ha = float(cv2.contourArea(hull))
    sol = area / ha if ha > 1 else 0.0
    x, y, bw, bh = cv2.boundingRect(c)
    edges = []
    if x <= 2:
        edges.append("左")
    if y <= 2:
        edges.append("上")
    if x + bw >= w - 2:
        edges.append("右")
    if y + bh >= h - 2:
        edges.append("下")

    if edges:
        return ("物体超出画面（贴%s边缘，被裁掉）：把方块完整放进画面再测"
                "（当前彩色块 %d px，短边 %.0f px）" % ("/".join(edges), int(area), short))
    if area < max(float(t["min_area_abs"]), float(t["min_area_ratio"]) * total):
        return ("彩色块太小：%d px（需要 ≥ %d px）：把方块放近些或调大焦距"
                % (int(area), int(max(float(t["min_area_abs"]),
                                      float(t["min_area_ratio"]) * total))))
    if short < float(t["min_side"]):
        return "彩色块太窄：短边 %.0f px（需要 ≥ %d px）：换个角度让方块正面朝向镜头" % (
            short, int(t["min_side"]))
    # ⚠ 别写成 `if long/short if short else 99 > x`：三元优先级会把它解析成
    #   `(long/short) if short else (99 > x)`，判据就废了（我第一版就写错了）。
    ar = (long / short) if short else 99.0
    if ar > float(t["aspect_max"]):
        return "形状太细长（长宽比 %.1f > %.1f）：看它是条状物，不是方形料" % (
            ar, float(t["aspect_max"]))
    if fill < float(t["min_fill"]):
        return "形状不像方形（填充率 %.2f < %.2f）：可能被遮挡或只露出一部分" % (
            fill, float(t["min_fill"]))
    if sol < float(t["solidity_min"]):
        return "形状不完整（实心度 %.2f < %.2f）：可能被遮挡 / 有缺口 / 只露出一部分" % (
            sol, float(t["solidity_min"]))
    return "彩色块存在但未通过矩形判定（填充率 %.2f / 实心度 %.2f）" % (fill, sol)


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
    # ★★ 2026-10-08 修 P0：`np.random.choice(size=n, replace=False, p=w)` 有一条硬约束 ——
    #   **p 中非零元素的个数必须 ≥ n**。中心加权 w=exp(-d²/2σ²) 在离质心远的地方会**下溢成 0**，
    #   物体一大（点集铺开）就满足 `非零数 < n` → 直接抛
    #   `ValueError: Fewer non-zero entries in p than size` → 整帧 detect() 崩掉。
    #   现场表现：某个位置的方块一出现，检测线程就抛异常（连"未知"都不返回）。
    #   修法：**只在非零权重里采样**（语义不变，本来就是"按权重取点"）；
    #   非零点不够 n 时退回"允许重复"（加权有放回采样），既不崩、也不改变颜色结论。
    nz = np.nonzero(w)[0]
    if nz.size >= n:
        wn = w[nz]
        wn = wn / wn.sum()
        idx = nz[np.random.choice(nz.size, size=n, replace=False, p=wn)]
    else:
        idx = np.random.choice(ys.size, size=n, replace=True, p=w)

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
        # ★★ 2026-10-09 新增：**三色现场卡**（红/绿/蓝的实测 Lab）。
        #   与上面的 18 类 self.card 分开存：色分拣走的是 classify_rgb，
        #   它以前只认写死的 RGB_CARD_LAB —— 现场标定写了也没人读（"标了不生效"）。
        #   现在把 calib["card"] 里的三色项挑出来给 classify_rgb 用。
        self.rgb_card: Dict[str, np.ndarray] = {}
        for name in RGB_NAMES:
            v = refs.get(name)
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                try:
                    arr = np.array([float(x) for x in v[:3]], dtype=np.float64)
                except (TypeError, ValueError):
                    continue
                if np.all(np.isfinite(arr)):
                    self.rgb_card[name] = arr

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
        def _find(m):
            return find_rectangles(m,
                                   min_area_ratio=self.thr["min_area_ratio"],
                                   max_area_ratio=self.thr["max_area_ratio"],
                                   min_side=int(self.thr["min_side"]),
                                   min_area_abs=self.thr["min_area_abs"],
                                   rect_fill=self.thr["min_fill"],
                                   solidity=self.thr["solidity_min"],
                                   aspect=(1.0, self.thr["aspect_max"]))

        rects = _find(mask)
        # ★★ 2026-10-10：**暗色兜底**（黑/深色物体）—— 见 dark_object_mask 的说明。
        #   ★★ 2026-10-10 修正（用户："现在红色也没有检测到…把黑色识别成蓝色了"）：
        #     旧写法在"暗色块 ≥ dark_area_gain 倍彩色候选"时执行 `rects = drects`，把彩色候选
        #     **整批丢弃** —— 画面里只要有一块较大的暗区（黑料 / 夹具 / 暗显示器 / 暗背景），
        #     红/绿就整批消失（现场"红色检测不到、部分绿色检测不到"）。
        #     现改为**彩色候选一个不丢**：
        #       · 有彩色候选 → 以它们为准；其中"框内以暗色像素为主"的（黑块被光照带偏混进
        #         彩色掩膜、被投成蓝/青）**改判为「黑」（无效色）** ← 修"黑→蓝"；
        #       · **一个彩色候选都没有** → 才用暗色候选（黑块在彩色掩膜里是零像素）。
        #     ⚠ 不再做"面积竞争/追加暗色候选"：C 位背景里有暗显示器等**大块暗矩形**，
        #       一旦让暗色候选按面积参与竞争，它们会顶掉真正的彩料（就是本次回归的根因）。
        dark_fallback = False
        if self.color_only and not rects:
            # ★★ 2026-10-10 性能（评审 B3）：暗色掩膜**只在"一个彩色候选都没有"时才算**。
            #   原来每帧都算（一次 5×5 高斯 + 色彩转换 + 中位数，约几毫秒/帧），
            #   而它**只服务于这条兜底支路** —— 有彩色候选时算完就扔，纯浪费。
            dmask = dark_object_mask(work,
                                     val_max=float(self.thr["dark_val_max"]),
                                     ratio=float(self.thr["dark_ratio"]),
                                     min_contrast=float(self.thr["dark_min_contrast"]))
            drects = _find(dmask)
            if drects:
                rects = drects
                dark_fallback = True
        results = []
        for r in rects:
            col = self._color(work, mask, r)
            if dark_fallback:
                # 暗色兜底找出来的候选（彩色掩膜里几乎没有它）→ 第 4 色「黑（无效）」
                col = _force_dark_col(col, self.thr)
            elif self.color_only and _looks_dark(work, r["box"], self.thr):
                # 彩色掩膜收进来的候选，但**暗 且 不艳** → 其实是暗色物体
                #   （黑块带蓝/青色偏被投成"蓝"）→ 改判「黑」。
                # ★ 只看"暗"会把暗绿/暗蓝误判成黑（现场"绿色检测不到"的根因）→ 必须叠加"不艳"。
                col = _force_dark_col(col, self.thr)
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
        # 取该候选 bounding 矩形内的 BGR，用 HSV 色相分箱直判**七色调色板**
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        roi = work[y0:y1 + 1, x0:x1 + 1]
        # ★ 审计修复 P0-cam-3: 色相投票的 S/V 门槛走可配置阈值表
        name, ratio_color = _palette_vote(roi, s_min=self.thr["hue_s_min"],
                                          v_min=self.thr["hue_v_min"])
        # ★ 诊断：把真实色相一并报出（现场"绿↔青"定界全靠它，见 _roi_hue）
        hue_val = _roi_hue(roi, s_min=self.thr["hue_s_min"], v_min=self.thr["hue_v_min"])
        dom = dominant_lab(work, sub)
        de = None
        alt = None
        alt_de = None
        chroma = 0.0
        name_l = "未知"
        # ★ lab_conf 仍算出来，但**不再参与置信度**（见下方"置信度重做"的说明）：
        #   它是"与固定标准色卡的贴合度"，现场光照下必然偏低，拿它当门会误杀。
        #   保留赋值仅供调试/落盘观察，命名加下划线前缀以免被误当成生效判据。
        _lab_conf = 0.0
        lab_arr = [round(float(v), 2)
                   for v in cv2.cvtColor(roi, cv2.COLOR_BGR2LAB).reshape(-1, 3).mean(axis=0)]
        if dom["ok"]:
            cr = classify_palette(dom["lab"])
            name_l = cr["name"]
            de = cr["de"]
            alt = cr["alt"]
            alt_de = cr["alt_de"]
            chroma = cr["chroma"]
            _lab_conf = cr["conf"]
            lab_arr = [round(float(v), 2) for v in dom["lab"]]
        # ★★ 矩形硬门槛（2026-10-08，用户口径"只需要识别矩形"）：
        #   形状明显不像矩形 → **直接不给颜色结论**，并说明是"非矩形"（而不是含糊的"未知"）。
        #   fill 由 minAreaRect 的长×宽算出，**与旋转无关**，不会误杀斜着举的方块。
        #   阈值 0 表示关闭该门槛（现场可退回旧行为）；默认 0.55（见 DEFAULT_THR 的说明：
        #   必须低于现场实测最低值 0.67，0.72 会误杀真矩形）。
        fill_min = float(self.thr.get("fill_min") or 0.0)
        fill_warn = float(self.thr.get("fill_warn") or 0.0)
        if fill_min > 0 and fill < fill_min:
            return {"name": "未知", "hex": None, "conf": 0.0, "alt": alt,
                    "reason": "非矩形(填充率 %.2f < %.2f)" % (fill, fill_min),
                    "ratio": round(fill, 3), "fill": round(fill, 3), "lab": lab_arr,
                    "de": de, "alt_de": alt_de, "chroma": chroma}
        if not name:
            return {"name": "未知", "hex": None, "conf": 0.0, "alt": alt,
                    "reason": "非七色之一（红/橙/黄/绿/青/蓝/紫）", "ratio": round(fill, 3),
                    "fill": round(fill, 3), "lab": lab_arr, "hue": round(hue_val, 1),
                    "de": de, "alt_de": alt_de, "chroma": chroma}
        # =====================================================================
        # ★★ 置信度（2026-10-08 重做，修"三色全都检不出"）
        #
        # 旧实现：`conf = min(hsv_conf, lab_conf)` —— 把 HSV 与 Lab 两个判据**取较严者**。
        #   听起来更稳，实际是**拿一个坏判据去否决一个好判据**：
        #     · HSV 色相投票：与光照无关，实测 **100% 正确**（现场图里红/绿/蓝的
        #       主色相分别稳定落在 H=2~4 / 69~70 / 104~106，全在各自色带内）；
        #     · Lab ΔE 最近邻：用**固定标准色卡**（红 #E02020 的 a*=69.2）比实测色，
        #       现场光照偏暗时实测只有 a*=12~14 → **ΔE 26~34** → lab_conf = 1-ΔE/25 **=0**
        #       → conf=0 → 被 min_conf 卡掉 → 报"检测不到"。
        #   现场证据：红 fill=0.98（形状完美）却 conf=0.00；蓝 ΔE=21.6 → conf=0.13。
        #   **颜色本来判对了，是被 Lab 拖死的。**
        #
        # 新口径：**HSV 为主判据**（它才是那个稳的），Lab 只做"复核/提示"：
        #   · 两者一致     → 置信度**略升**（互相印证，最高 1.0）
        #   · 两者分歧     → 置信度**降一半左右**（但不归零，交回上层按 min_conf 决定）
        #   · Lab 偏差过大 → 只写进 reason 提示"建议现场标定"，**不影响能否给出颜色**
        # 这样"红/绿/蓝都能给出颜色"，同时分歧/偏差仍然看得见（不掩盖问题）。
        # =====================================================================
        hsv_conf = float(ratio_color * (0.5 + 0.5 * min(1.0, max(0.0, fill))))
        conf = hsv_conf
        reason = ""
        if dom["ok"]:
            if name_l == name:
                conf = min(1.0, hsv_conf * 1.15)          # 一致 → 略升（封顶 1.0）
            else:
                conf = hsv_conf * 0.55                    # 分歧 → 降，但不归零
                reason = "HSV/Lab 分歧(%s)" % name_l
        # Lab 与固定色卡差得远 → 提示标定（**不参与置信度**，只提示）
        if de is not None and de > 20.0:
            tip = "与标准色卡偏差大(ΔE=%.0f)建议现场标定" % de
            reason = (reason + "；" + tip) if reason else tip
        # 形状偏"杂" → 软提示（不拦检）
        if not reason and fill_warn > 0 and fill < fill_warn:
            reason = "杂色（建议复核）"
        return {"name": name, "hex": PALETTE_HEX.get(name),
                "conf": round(float(min(1.0, max(0.0, conf))), 3), "alt": alt,
                "reason": reason, "hue": round(hue_val, 1),
                "ratio": round(fill, 3), "fill": round(fill, 3), "lab": lab_arr,
                "de": de, "alt_de": alt_de, "chroma": chroma}


# =====================================================================
# 7.5 三色自学习标定（2026-10-09 新增）
#
#   为什么要有它（三个真问题）：
#     ① **标了不生效**：`classify_rgb()` 的 `card` 参数此前从未被使用，
#        现场"实物标定"写进 calib["card"] 也影响不到判色 —— 白做。
#     ② **单帧采样太脆**：原 `calibrate_sample` 直接取"最近一次判定"的 Lab，
#        一帧的反光/抖动就进了色卡（而且没有任何质量门槛）。
#     ③ **标完不知道有没有变好**：没有任何手段检验标定效果，只能靠现场反复试。
#
#   本模块用**稳健统计**解决 ②：多帧入池 → 中位 + MAD 离群剔除 → 取中位做主参考。
#   中位数对离群天然不敏感（比均值稳得多），MAD 用来显式挑出"这一帧明显偏了"。
# =====================================================================
class ColorSampler:
    """某色的实测样本池（每色最多 keep 帧），返回稳健的代表值。

    用法：
        s = ColorSampler()
        s.add("红", [48.2, 69.1, 50.3], conf=0.93)
        rep = s.representative("红")     # -> {ok, lab, n, used, outliers, spread}
    """

    def __init__(self, keep: int = 40, mad_k: float = 3.5):
        self.keep = int(keep)          # 每色最多保留多少帧
        self.mad_k = float(mad_k)      # 离群阈值：|x-median| > k*MAD 视为离群
        self.pool: Dict[str, List[List[float]]] = {c: [] for c in RGB_NAMES}

    def add(self, name: str, lab: Sequence[float], conf: float = 1.0) -> bool:
        """入池。★ 只收三色名 + 有限值；按 keep 截断（保留最新的）。"""
        if name not in self.pool:
            return False
        try:
            arr = [float(x) for x in lab][:3]
        except (TypeError, ValueError):
            return False
        if len(arr) != 3 or not all(math.isfinite(v) for v in arr):
            return False
        if float(conf) <= 0:
            return False
        self.pool[name].append(arr)
        if len(self.pool[name]) > self.keep:
            del self.pool[name][:len(self.pool[name]) - self.keep]
        return True

    def reset(self, name: Optional[str] = None) -> None:
        if name in self.pool:
            self.pool[name] = []
        elif name is None:
            for c in self.pool:
                self.pool[c] = []

    def _mad(self, vals: np.ndarray) -> Tuple[np.ndarray, float, np.ndarray]:
        """返回 (中位, MAD, 离群掩膜)。MAD = median(|x - median|)。"""
        med = np.median(vals, axis=0)
        dev = np.abs(vals - med).max(axis=1)          # 每个样本到中位的最大分量偏差
        mad = float(np.median(dev))
        if mad <= 1e-9:
            # MAD=0（样本几乎完全一致）→ 用极小的兜底尺度，避免把所有样本都判成离群
            thr = 1e-6
        else:
            thr = self.mad_k * mad
        return med, mad, dev > thr

    def representative(self, name: str) -> Dict[str, object]:
        """稳健代表值：剔除离群后取中位。样本不足时如实说明"还没标定"。"""
        vals = self.pool.get(name) or []
        n = len(vals)
        if n == 0:
            return {"ok": False, "n": 0, "used": 0, "reason": "还没有样本"}
        arr = np.asarray(vals, dtype=np.float64)
        med, mad, out = self._mad(arr)
        kept = arr[~out]
        if kept.size == 0:                            # 极端情形：全被判离群 → 退回全量中位
            kept = arr
        lab = np.median(kept, axis=0)
        spread = float(np.max(np.abs(kept - lab))) if kept.size else 0.0
        return {"ok": True, "n": n, "used": int(kept.shape[0]),
                "outliers": int(out.sum()), "lab": [round(float(v), 2) for v in lab],
                "mad": round(mad, 3), "spread": round(spread, 2)}

    def state(self) -> Dict[str, object]:
        return {c: {"n": len(self.pool[c]), "rep": self.representative(c)}
                for c in RGB_NAMES}


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
