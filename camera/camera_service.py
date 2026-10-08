# -*- coding: utf-8 -*-
"""
EFORT 视觉检测服务 — 海康机器人 GigE 工业相机 + YOLO 目标检测。

独立进程运行 (复用 EFORT_Camera_Python_OpenCv 的 venv, 内含 torch/ultralytics/MVS SDK):
    <参考项目>/venv/Scripts/python.exe camera/camera_service.py

仅用 Python 标准库 http.server 提供:
    GET  /stream          MJPEG 实时流(可含检测框/颜色框)
    GET  /snapshot        当前单帧 JPEG
    POST /snapshot-save   保存当前帧到 camera/captures/
    GET  /status          状态 JSON (含 vision 子对象)
    GET  /models          可选模型列表
    POST /open, /close    打开/关闭相机 (异步, 立即返回, 前端轮询 /status)
    POST /reconnect       手动重连相机 (网口切换后, 无需重启服务)
    POST /config          更新 AI 配置 (enabled/model/conf/imgsz)
    POST /unload          卸载 YOLO 模型释放内存 (torch 加载后常驻, 可达数 GB)

颜色分拣 (纯 OpenCV, 不依赖 torch; 算法在 camera/vision_color.py):
    GET  /vision/status         颜色线程状态 + 色卡 + 标定概览 + 生效阈值(thr)
    GET  /vision/last?since=N   最近一次触发(前端/后端轮询去重)
                                 ★ P0-cam-1: 返回的 seq 恒等于已发布事件的 event.seq，
                                   没有已发布事件时为 0 —— 消费方只能按 event 推进游标。
    GET  /vision/records?limit= 历史记录列表(含缩略图路径)
    POST /vision/records/clear  清空记录
    POST /vision/config         {enabled,roi,use_mog2,min_interval,save_on_detect,
                                 min_conf,debug,thr}   thr=检测阈值部分覆盖(P0-cam-3)
    POST /vision/background     把当前画面设为静态背景 (reset=true 清除)
    POST /vision/calibrate/gray {target} 中心灰卡一键白平衡
    POST /vision/calibrate/sample {name} 用最近一次判定结果标定该色(现场实物标定)
    GET  /vision/detect         对当前帧同步跑一次(调试用, 返回完整 JSON)
所有响应带 CORS 头, 供 :8000 上的网页直接调用。

性能设计:
    - 抓帧线程: 抓全分辨率 -> 立即缩放到 STREAM_MAX_W -> 只保存小图(省内存/CPU)
    - 编码线程(单例): 只在有 MJPEG 客户端时, 把 [小图+检测框] 编码成 JPEG 一次
    - 多个客户端共享同一份 latest_jpeg, 不重复编码
    - 检测线程: 用同一张小图推理, 检测框坐标与显示图一致
"""
from __future__ import annotations

import gc
import io
import json
import os
import sys
import threading
import time
from ctypes import *
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

# ★ 2026-09-24：OpenBLAS 线程数限制 —— 必须在 import numpy/torch 之前设置，
#   否则 OpenBLAS 会为每个 CPU 核心分配线程本地内存，核心数多时可能失败
#   （报错："Memory allocation still failed after 10 retries, giving up."）。
#   设为 2 足够（相机抓帧 + YOLO 推理各一个线程），避免内存分配失败。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")

# ---- MVS 运行时 DLL (海康机器人相机 SDK) ----
# 默认路径是 MVS 客户端标准安装位置; 装到别处时用 EFORT_MVS_RUNTIME_DIR 覆盖。
MVS_DLL_DIR = os.environ.get(
    "EFORT_MVS_RUNTIME_DIR",
    r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64",
)
if os.path.isdir(MVS_DLL_DIR):
    os.environ["PATH"] = MVS_DLL_DIR + os.pathsep + os.environ.get("PATH", "")

# ---- 复用参考项目的 MVS Python 封装 ----
# MvCameraControl_class.py 不在 PyPI 上, 随 MVS 客户端/厂商样例包提供。
# 默认指向本机已存在的参考项目; 换机器时用 EFORT_MVS_SDK_DIR 覆盖。
REF_SDK = os.environ.get(
    "EFORT_MVS_SDK_DIR",
    r"D:\EFORT_Projects\EFORT_Camera_Python_OpenCv\src\sdk\MvImport",
)
if os.path.isdir(REF_SDK):
    sys.path.append(REF_SDK)

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    from MvCameraControl_class import (
        MvCamera, MV_CC_DEVICE_INFO_LIST, MV_CC_DEVICE_INFO, MV_FRAME_OUT,
        MV_CC_PIXEL_CONVERT_PARAM, MVCC_ENUMENTRY,
        MV_GIGE_DEVICE, MV_USB_DEVICE, MV_GENTL_GIGE_DEVICE,
        MV_ACCESS_Exclusive, MV_TRIGGER_MODE_OFF,
        PixelType_Gvsp_RGB8_Packed,
    )
    _SDK_OK = True
    _SDK_ERR = ""
except Exception as e:
    _SDK_OK = False
    _SDK_ERR = str(e)

PORT = int(os.environ.get("CAMERA_PORT", "8100"))
MODELS_DIR = os.path.join(HERE, "models")
CAPTURE_DIR = os.path.join(HERE, "captures")
VISION_DIR = os.path.join(CAPTURE_DIR, "vision")     # 颜色分拣存档
CALIB_PATH = os.path.join(HERE, "calib.json")        # 白平衡增益 + 色卡 Lab
STREAM_MAX_W = 960           # 抓帧后统一缩放到的宽度(检测与显示共用)
JPEG_QUALITY = 78
STREAM_FPS = 20              # 编码目标帧率上限
COLOR_FPS = 12               # 颜色线程目标帧率上限(纯 OpenCV, <15ms/帧)
MAX_RECORDS = 300            # 内存中保留的最近记录条数
os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(VISION_DIR, exist_ok=True)

# ---- 颜色分拣算法模块(纯 OpenCV + numpy; 与本文件同目录) ----
# 导入失败只降级"颜色分拣", 不影响 YOLO 检测与取流。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import vision_color as vc
    _VC_OK = True
    _VC_ERR = ""
except Exception as _e:
    vc = None
    _VC_OK = False
    _VC_ERR = str(_e)

CN_NAMES = {
    "person": "人", "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "airplane": "飞机", "bus": "公交车", "train": "火车", "truck": "卡车",
    "boat": "船", "traffic light": "红绿灯", "fire hydrant": "消防栓",
    "stop sign": "停车标志", "parking meter": "停车计时器", "bench": "长椅",
    "bird": "鸟", "cat": "猫", "dog": "狗", "horse": "马", "sheep": "羊",
    "cow": "牛", "elephant": "大象", "bear": "熊", "zebra": "斑马",
    "giraffe": "长颈鹿", "backpack": "背包", "umbrella": "雨伞",
    "handbag": "手提包", "tie": "领带", "suitcase": "行李箱", "frisbee": "飞盘",
    "skis": "滑雪板", "snowboard": "滑雪单板", "sports ball": "运动球",
    "kite": "风筝", "baseball bat": "棒球棒", "baseball glove": "棒球手套",
    "skateboard": "滑板", "surfboard": "冲浪板", "tennis racket": "网球拍",
    "bottle": "瓶子", "wine glass": "酒杯", "cup": "杯子", "fork": "叉子",
    "knife": "刀", "spoon": "勺子", "bowl": "碗", "banana": "香蕉",
    "apple": "苹果", "sandwich": "三明治", "orange": "橙子", "broccoli": "西兰花",
    "carrot": "胡萝卜", "hot dog": "热狗", "pizza": "披萨", "donut": "甜甜圈",
    "cake": "蛋糕", "chair": "椅子", "couch": "沙发", "potted plant": "盆栽",
    "bed": "床", "dining table": "餐桌", "toilet": "马桶", "tv": "电视",
    "laptop": "笔记本", "mouse": "鼠标", "remote": "遥控器", "keyboard": "键盘",
    "cell phone": "手机", "microwave": "微波炉", "oven": "烤箱",
    "toaster": "烤面包机", "sink": "水槽", "refrigerator": "冰箱", "book": "书",
    "clock": "时钟", "vase": "花瓶", "scissors": "剪刀", "teddy bear": "泰迪熊",
    "hair drier": "吹风机", "toothbrush": "牙刷",
}

_FONT_CACHE: dict = {}


def _font(size: int):
    size = max(12, int(size))
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)
    except Exception:
        f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def _hex_rgb(h) -> tuple:
    """'#RRGGBB' → (r, g, b)。解析失败给一个显眼的橙黄兜底色。"""
    try:
        s = str(h).lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return (255, 176, 32)


def _safe_name(s: str) -> str:
    """把颜色名变成安全的目录/文件名片段(去掉 Windows 非法字符)。"""
    out = "".join(ch for ch in str(s) if ch not in '\\/:*?"<>|').strip()
    return out or "unknown"


def _json_default(o):
    """json 兜底: numpy 标量/数组 → 原生类型。

    ★ 少了这个兜底, 一个漏网的 numpy 标量会让 json.dumps 在**发响应之前**抛异常,
      客户端只会看到"连接被复位"而拿不到任何错误信息 —— 极难排查。
    """
    try:
        return o.tolist()
    except Exception:
        return str(o)


def _decoding_char(c_arr):
    b = memoryview(c_arr).tobytes()
    i = b.find(b"\x00")
    if i != -1:
        b = b[:i]
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("latin-1", errors="replace")


def _ip_to_str(n):
    return "%d.%d.%d.%d" % ((n >> 24) & 0xFF, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF)


def _trim_working_set() -> bool:
    """Windows: 让系统回收本进程工作集, 把物理内存归还 OS。

    纯 Python/C 扩展(torch)释放对象后, 内存分配器往只缓存不归还系统,
    任务管理器里仍显示高占用; EmptyWorkingSet 可把工作集裁剪到最小,
    显著降低任务管理器显示的内存(虚拟内存不变, 但物理内存归还)。
    """
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32.dll")
        psapi = ctypes.WinDLL("psapi.dll")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())
        return True
    except Exception:
        return False


def _enum_devices():
    device_list = MV_CC_DEVICE_INFO_LIST()
    tlayer = MV_GIGE_DEVICE | MV_USB_DEVICE | MV_GENTL_GIGE_DEVICE
    ret = MvCamera.MV_CC_EnumDevices(tlayer, device_list)
    if ret != 0:
        raise RuntimeError("EnumDevices failed 0x%x" % ret)
    print(f"[相机枚举] 发现 {device_list.nDeviceNum} 个设备")
    found = []
    for i in range(device_list.nDeviceNum):
        info = cast(device_list.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
        if info.nTLayerType in (MV_GIGE_DEVICE, MV_GENTL_GIGE_DEVICE):
            g = info.SpecialInfo.stGigEInfo
            model = _decoding_char(g.chModelName)
            serial = _decoding_char(g.chSerialNumber)
            ip = _ip_to_str(g.nCurrentIp)
            print(f"  [{i}] GigE: {model} (SN: {serial}, IP: {ip})")
            found.append((i, "GigE", model, serial, ip))
        elif info.nTLayerType == MV_USB_DEVICE:
            u = info.SpecialInfo.stUsb3VInfo
            model = _decoding_char(u.chModelName)
            serial = _decoding_char(u.chSerialNumber)
            print(f"  [{i}] USB3: {model} (SN: {serial})")
            found.append((i, "USB3", model, serial, "-"))
        else:
            print(f"  [{i}] 未知类型: nTLayerType={info.nTLayerType}")
    return device_list, found


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """读环境变量里的浮点配置，越界/垃圾值一律回退到默认（不让服务起不来）。

    ★ 为什么要有这个（2026-10-08 现场反馈"图像检测误报比较多"）：
      检测置信度 conf 原先是**源码里硬编码的 0.25**（YOLO 出厂值），
      没有任何外部入口 → ① 误报多时只能改代码；② 视觉服务一重启就回到 0.25
      （和之前 TTL 硬编码是同一类坑）。现在默认提到 0.5，并可用 .env 覆盖。
    """
    try:
        v = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))

def _open_camera(device_list, idx):
    cam = MvCamera()
    info = cast(device_list.pDeviceInfo[idx], POINTER(MV_CC_DEVICE_INFO)).contents
    ret = cam.MV_CC_CreateHandle(info)
    if ret != 0:
        raise RuntimeError("CreateHandle failed 0x%x" % ret)
    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    if ret != 0:
        cam.MV_CC_DestroyHandle()
        raise RuntimeError("OpenDevice failed 0x%x (相机可能被 MVS/其他程序占用)" % ret)
    if info.nTLayerType in (MV_GIGE_DEVICE, MV_GENTL_GIGE_DEVICE):
        try:
            pkt = cam.MV_CC_GetOptimalPacketSize()
            if pkt and pkt > 0:
                cam.MV_CC_SetIntValue("GevSCPSPacketSize", pkt)
        except Exception:
            pass
    cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
    # ★ 强制设置像素格式为 BayerRG8（彩色）：
    #   工业相机默认可能输出 Mono8（灰度），导致画面黑白。
    #   设为 BayerRG8 后，_frame_to_rgb 才能正确转换为彩色 RGB。
    #   若相机不支持此格式，会静默失败（保持原格式），不影响使用。
    try:
        cam.MV_CC_SetEnumValue("PixelFormat", 0x01080009)  # BayerRG8
    except Exception:
        pass
    return cam


def _frame_to_rgb(cam, frame):
    w = frame.stFrameInfo.nWidth
    h = frame.stFrameInfo.nHeight
    dst_size = w * h * 3 + 4096
    buf = (c_ubyte * dst_size)()
    cp = MV_CC_PIXEL_CONVERT_PARAM()
    cp.nWidth = w
    cp.nHeight = h
    cp.enSrcPixelType = frame.stFrameInfo.enPixelType
    cp.pSrcData = frame.pBufAddr
    cp.nSrcDataLen = frame.stFrameInfo.nFrameLen
    cp.enDstPixelType = PixelType_Gvsp_RGB8_Packed
    cp.pDstBuffer = cast(buf, POINTER(c_ubyte))
    cp.nDstBufferSize = dst_size
    if cam.MV_CC_ConvertPixelType(cp) != 0:
        return None
    n = cp.nDstLen if cp.nDstLen else w * h * 3
    arr = np.frombuffer(buf, dtype=np.uint8, count=min(n, dst_size))
    if arr.size < w * h * 3:
        return None
    return arr[:w * h * 3].reshape(h, w, 3)


# ============================ 服务主体 ============================
class CameraService:
    def __init__(self):
        self.lock = threading.RLock()
        self.cam = None
        self.cam_type = None            # "mvs" | "opencv" | None
        self.sdk_ready = False
        self.running = False
        self.opening = False
        self.closing = False
        self.latest_frame = None        # PIL RGB, 已缩放到 STREAM_MAX_W
        self.latest_jpeg = None         # 编码线程产出, 供所有客户端共享
        self.latest_dets = []           # [(xyxy, raw_name, conf)] 与 latest_frame 同尺寸
        self.frame_count = 0
        self.fps = 0.0
        self.info = {}
        self.error = "" if _SDK_OK else ("SDK 导入失败: " + _SDK_ERR)
        self.stream_clients = 0
        # AI 配置
        self.yolo_enabled = False
        self.yolo_name = "yolo26n.pt"
        self.yolo_model = None
        self.yolo_loading = False
        # ★ 目标检测置信度阈值：默认 0.5（原 0.25 误报偏多）。
        #   现场可改 .env 的 EFORT_CAM_CONF，或运行时 POST /config {"conf":…}，
        #   也可以在摄像头面板的「检测置信度」滑杆上直接调。
        self.conf = _env_float("EFORT_CAM_CONF", 0.5, 0.05, 0.95)
        self.imgsz = 640
        self.det_ms = 0.0
        self.det_count = 0
        self.opened_at = 0.0
        self._grab_thread = None
        self._det_thread = None
        self._enc_thread = None
        self.opencv_idx = None           # 当前持有/正在打开的 USB 摄像头索引
        # ★ 记住"上一次成功打开的设备"，供 /open、/reconnect 复用：
        #   USB 相机用完关闭后再次点开，若仍按默认走 MVS 枚举，就会枚举不到海康网口相机
        #   而弹出"未找到相机(检查网线/网卡 192.168.1.x)" —— 这是误报（USB 相机根本不走网线）。
        #   记住后，重开就是"原设备原路重开"，USB 走 USB、MVS 走 MVS，两条路彻底分开。
        self.last_open = None            # {"idx": int|None, "cam_type": "mvs"|"opencv"}
        self._enum_lock = threading.Lock()  # USB 探测串行化(探测=独占打开, 并发会互相打架)

        # ---------- 颜色分拣 (纯 OpenCV, 与 YOLO 互不影响) ----------
        # ★ 需求：**检测默认开启**（以前默认关，现场每次都要手动打开）。
        #   打开相机时再按"是否彩色相机"校正：灰度相机没有颜色信息，会被关掉并提示。
        self.color_enabled = True
        self.color_engine = None        # vision_color.VisionEngine(配置变更时重建)
        # ★ 实时链路只用颜色阈值做前景（对齐 visual_object_detector.py）：见 _ensure_engine
        self.color_only = True
        self.color_calib = vc.load_calib(CALIB_PATH) if _VC_OK else {}
        self.background = None          # 静态背景 BGR(设了就优先于 MOG2)
        self.use_mog2 = True
        self.roi = None                 # (x, y, w, h) 小图坐标
        self.color_min_interval = 0.8   # 同色重复触发节流(秒)
        self.save_on_detect = True      # 触发时是否落盘三件套
        # 三色识别置信度阈值（conf<此值不画框/不记录）；可用 EFORT_CAM_COLOR_CONF 覆盖
        self.color_conf = _env_float("EFORT_CAM_COLOR_CONF", 0.28, 0.05, 0.95)
        # ★ 审计修复 P0-cam-3: 检测阈值表(面积/短边/HSV/色相)，可由
        #   POST /vision/config {"thr": {...}} 覆盖子集；默认 = vision_color.DEFAULT_THR。
        self.vision_thr = (dict(vc.DEFAULT_THR) if _VC_OK else {})
        self.color_capable = False      # 当前相机是否彩色(灰度相机无法做颜色识别)
        self.vision_debug = False       # 诊断开关: 逐帧打印 mask 连通域(默认关)
        self.latest_color = []          # 当前帧颜色候选(与 latest_frame 同尺寸)
        self._black_streak = 0          # 连续疑似坏帧计数(整帧全黑/单色), 用于平滑偶发"黑屏闪一下"
        self.color_seq = 0              # 触发序号(前端用 since 去重)
        self.last_event = None          # 最近一次触发详情
        self.records = []               # 最近记录(倒序, 上限 MAX_RECORDS)
        self.color_count = 0
        self.color_ms = 0.0
        self._color_thread = None
        self._last_fire = (None, 0.0)   # (颜色名, 时间) 节流用

    # ---------- 状态 ----------
    def status(self):
        with self.lock:
            return {
                "sdk_ok": _SDK_OK,
                "error": self.error,
                "opened": self.cam is not None,
                "opening": self.opening,
                "closing": self.closing,
                "running": self.running,
                "device": self.info,
                "fps": round(self.fps, 1),
                "frames": self.frame_count,
                "resolution": ("%dx%d" % (self.latest_frame.width, self.latest_frame.height))
                if self.latest_frame else "-",
                "ai_enabled": self.yolo_enabled,
                "ai_model": self.yolo_name,
                "ai_loaded": self.yolo_model is not None,
                "ai_loading": self.yolo_loading,
                "conf": self.conf,
                "imgsz": self.imgsz,
                "det_count": self.det_count,
                "det_ms": round(self.det_ms * 1000, 1),
                "clients": self.stream_clients,
                # ★ 诊断用：三个工作线程是否存活。现场"检测不工作/画面不动"时，
                #   看一眼这里就能区分是"抓帧挂了 / 编码挂了 / 颜色线程挂了"。
                "threads": {
                    "grab": bool(self._grab_thread and self._grab_thread.is_alive()),
                    "encode": bool(self._enc_thread and self._enc_thread.is_alive()),
                    "color": bool(self._color_thread and self._color_thread.is_alive()),
                },
                # 审计修复 P0-ui-4：连续疑似全黑帧计数（超过 MAX_BAD_STREAK=12 表示
                # 画面持续全黑 —— 此时服务端保持最后一帧有效帧、不再透传真黑帧）
                "black_streak": self._black_streak,
                "models": self.list_models(),
                "port": PORT,
                "vision": self.vision_status_locked(),
            }

    def vision_status_locked(self) -> dict:
        """颜色分拣状态(调用方需已持有 self.lock)。"""
        if not _VC_OK:
            return {"supported": False, "error": _VC_ERR, "enabled": False}
        return {
            "supported": True,
            "error": "",
            "enabled": self.color_enabled,
            "engine": self.color_engine is not None,
            # ★ 诊断：颜色检测线程是否存活（开着却死了 = 检测"不工作"）
            "thread_alive": bool(self._color_thread and self._color_thread.is_alive()),
            "mode": "color_only" if self.color_only else "segmentation",
            # ★ 实时链路口径改为"只用颜色阈值"（参考实现），这里如实上报，
            #   避免界面还写着 MOG2/静态背景、与实际用的判据不符。
            "seg": "颜色阈值(仅HSV)" if self.color_only else (
                "静态背景" if self.background is not None
                else ("MOG2" if self.use_mog2 else "Otsu")),
            "roi": list(self.roi) if self.roi else None,
            "count": self.color_count,
            "seq": self.color_seq,
            "ms": round(self.color_ms * 1000, 1),
            "min_interval": self.color_min_interval,
            "min_conf": self.color_conf,
            # ★ 审计修复 P0-cam-3: 阈值回显，前端/运维可据此核对现场生效值
            "thr": dict(self.vision_thr),
            "color_capable": self.color_capable,
            "targets": list(vc.RGB_NAMES) if _VC_OK else [],
            "save": self.save_on_detect,
            "card": dict(vc.COLOR_CARD),
            "achromatic": list(vc.ACHROMATIC),
            "calib": {
                "gains": self.color_calib.get("gains"),
                "card_n": len(self.color_calib.get("card") or {}),
            },
            "last": self.last_event,
            "records": len(self.records),
        }

    def list_models(self):
        if not os.path.isdir(MODELS_DIR):
            return []
        return sorted([f for f in os.listdir(MODELS_DIR) if f.endswith(".pt")])

    # ---------- 设备列表与切换 ----------
    def list_devices(self):
        """列出所有可用相机设备（包括海康工业相机和普通 USB 摄像头）。"""
        devices = []
        # 1. 海康工业相机（MVS SDK）
        if _SDK_OK:
            try:
                if not self.sdk_ready:
                    MvCamera.MV_CC_Initialize()
                    self.sdk_ready = True
                _, found = _enum_devices()
                for f in found:
                    devices.append({"idx": f[0], "type": f[1], "model": f[2],
                                    "serial": f[3], "ip": f[4],
                                    "label": f"{f[2]} ({f[1]} {f[3]})",
                                    "cam_type": "mvs"})
            except Exception:
                pass
        # 2. 普通 USB 摄像头（OpenCV）
        # ★ DirectShow 相机是独占的: 探测=真打开再释放。
        #   - 正在持有/正在打开的索引绝不能再探测(否则和 _do_open 抢相机)
        #   - 并发 /devices 请求必须串行(多页面面板同时拉列表会互相打架)
        try:
            import cv2
            with self._enum_lock:
                with self.lock:
                    held_idx = self.opencv_idx
                    held_info = dict(self.info) if held_idx is not None else None
                for i in range(5):  # 检测前 5 个可能的摄像头索引
                    if held_idx is not None and i == held_idx:
                        # 自己正持有(或正在打开)这台: 直接上报信息, 不重新打开
                        model = (held_info or {}).get("model", f"Web Camera {i}")
                        devices.append({"idx": i, "type": "USB", "model": model,
                                        "serial": f"opencv-{i}", "ip": "-",
                                        "label": model + " (使用中)",
                                        "cam_type": "opencv"})
                        continue
                    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                    if cap.isOpened():
                        # 获取摄像头名称（如果支持）
                        name = f"USB Camera {i}"
                        try:
                            # 某些摄像头支持获取后端名称
                            backend = cap.getBackendName()
                            name = f"Web Camera {i} ({backend})"
                        except Exception:
                            pass
                        cap.release()
                        devices.append({"idx": i, "type": "USB", "model": name,
                                        "serial": f"opencv-{i}", "ip": "-",
                                        "label": name,
                                        "cam_type": "opencv"})
        except Exception:
            pass
        return devices

    def current_device(self):
        """返回当前已打开的设备信息。"""
        with self.lock:
            if self.info:
                return {"model": self.info.get("model", ""),
                        "serial": self.info.get("serial", ""),
                        "ip": self.info.get("ip", ""),
                        "type": self.info.get("layer", "")}
            return None

    def switch_device(self, idx, cam_type="mvs"):
        """切换到指定索引的相机设备。"""
        # 先关闭当前相机
        if self.cam is not None:
            ok, msg = self.close_async()
            # 等待关闭完成
            for _ in range(50):  # 最多等 5 秒
                with self.lock:
                    if self.cam is None:
                        break
                time.sleep(0.1)
        # 打开新相机
        return self.open_async(idx=idx, cam_type=cam_type)

    # ---------- 相机 (异步) ----------
    def _auto_target(self):
        """无历史记录时**自动选相机**：优先海康网口，没有则用 USB。

        ★ 为什么必须要有这个（2026-10-08 现场反馈"USB 相机连不上"）：
          `open_async(cam_type=None)` 原本在无 last_open 时**无脑回落 "mvs"**（海康网口），
          于是 USB 摄像头用户第一次点「打开」必然撞上
          "未找到海康网口相机（请检查网线/网卡 192.168.1.x）" ——
          相机明明在设备列表里、单独选它就能开，表现得却像"USB 相机坏了"。
          更糟的是 last_open 只在内存里：**视觉服务每重启一次，这个坑就复现一次**。
          所以这里改成先枚举一遍再决定走哪一路。
        返回 (idx, cam_type)；两路都没有时返回 (None, "mvs")，让原错误提示照旧给出排查指引。
        """
        try:
            devs = self.list_devices()
        except Exception:
            devs = []
        for want in ("mvs", "opencv"):          # 有网口优先用网口，没有再退 USB
            hit = [d for d in devs if d.get("cam_type") == want]
            if hit:
                return hit[0].get("idx"), want
        return None, "mvs"

    def open_async(self, idx=None, cam_type=None):
        """打开相机。cam_type=None 表示"自动"：复用上一次成功打开的设备类型/索引，
        没有历史时**枚举一次自动选**（见 _auto_target）——而不是无脑当海康网口。

        ★ 两路相机（海康 GigE / 普通 USB）打开逻辑完全分开：
          - 指定 opencv → 只走 cv2.VideoCapture，绝不会报"检查网线"；
          - 指定 mvs    → 只走 MVS 枚举，才可能报"检查网线/网卡"。
          - 不指定      → 沿用 last_open（USB 关了再开还是 USB）；无历史时自动选。
        """
        with self.lock:
            # 如果相机已打开且运行正常，直接返回成功
            if self.cam is not None and self.running and not self.opening and not self.closing:
                return True, "已打开"
            # 如果正在打开中，返回进行中状态（避免重复启动线程）
            if self.opening:
                return True, "正在打开..."
            # 如果正在关闭中，等待关闭完成后再打开
            if self.closing:
                return False, "正在关闭中，请稍候再试"
            # ---- 解析"自动"目标：优先复用上次设备（USB 关了再开还是 USB）----
            if cam_type is None:
                last = self.last_open or {}
                cam_type = last.get("cam_type")
                if idx is None:
                    idx = last.get("idx")
        # ★ 自动选择必须在**锁外**做：list_devices() 自己会取 self.lock（不可重入）
        if cam_type is None:
            idx, cam_type = self._auto_target()
        with self.lock:
            self.opening = True
            self.error = ""
            # ★ 同步占位: 响应返回前就登记, 让随后的 /devices 探测跳过这台相机
            if cam_type == "opencv" and idx is not None:
                self.opencv_idx = idx
        threading.Thread(target=self._do_open, args=(idx, cam_type), name="open", daemon=True).start()
        return True, "正在打开..."

    def _do_open(self, idx=None, cam_type=None):
        if cam_type is None:
            last = self.last_open or {}
            cam_type = last.get("cam_type") or "mvs"
            if idx is None:
                idx = last.get("idx")
        try:
            if cam_type == "opencv":
                # 普通 USB 摄像头（OpenCV）—— 与 MVS/网线无关，失败信息不提网线
                import cv2
                cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                if not cap.isOpened():
                    # 极少数设备首次打开会失败（驱动/DShow 未就绪）：释放后重试一次
                    cap.release()
                    time.sleep(0.6)
                    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                if not cap.isOpened():
                    raise RuntimeError("无法打开 USB 摄像头 %s（请确认未被其它程序占用，或换 USB 口）" % idx)
                # 设置分辨率（可选）
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                with self.lock:
                    self.cam = cap
                    self.cam_type = "opencv"
                    self.info = {"layer": "USB", "model": f"Web Camera {idx}",
                                 "serial": f"opencv-{idx}", "ip": "-"}
                    self.running = True
                    self.error = ""
                    self.color_capable = True       # USB/彩色网口相机: 支持颜色识别
                    self.opened_at = time.time()
                    self.last_open = {"idx": idx, "cam_type": "opencv"}
                self._start_threads()
            else:
                # 海康工业相机（MVS SDK）—— 只有这一路才与网线/网卡有关
                if not _SDK_OK:
                    raise RuntimeError("SDK 不可用: " + _SDK_ERR)
                if not self.sdk_ready:
                    MvCamera.MV_CC_Initialize()
                    self.sdk_ready = True
                # ★ 枚举偶发为空（网口刚上电/交换机未就绪）：重试 2 次再报"检查网线"
                device_list, found = _enum_devices()
                tries = 0
                while not found and tries < 2:
                    tries += 1
                    time.sleep(0.8)
                    device_list, found = _enum_devices()
                if not found:
                    raise RuntimeError("未找到海康网口相机（请检查网线/网卡 192.168.1.x；"
                                       "若用的是 USB 摄像头请在设备列表里选择 USB）")
                # 如果指定了 idx，使用指定的设备；否则使用第一个
                if idx is not None:
                    target = next((f for f in found if f[0] == idx), None)
                    if target is None:
                        target = found[0]     # 指定索引已消失（换了口）→ 退回第一个可用
                else:
                    target = found[0]
                cam = _open_camera(device_list, target[0])
                ret = cam.MV_CC_StartGrabbing()
                if ret != 0:
                    cam.MV_CC_CloseDevice()
                    cam.MV_CC_DestroyHandle()
                    raise RuntimeError("开始采集失败 0x%x" % ret)
                with self.lock:
                    self.cam = cam
                    self.cam_type = "mvs"
                    self.info = {"layer": target[1], "model": target[2],
                                 "serial": target[3], "ip": target[4]}
                    # 海康型号尾缀: -GM/UM/EM = 灰度(单色), -GC/UC/EC = 彩色
                    #   灰度工业相机输出天生黑白, 无颜色信息, 无法做三色识别
                    model_up = str(target[2]).upper()
                    self.color_capable = any(k in model_up for k in ("GC", "UC", "EC"))
                    self.running = True
                    self.error = ""
                    self.opened_at = time.time()
                    self.last_open = {"idx": target[0], "cam_type": "mvs"}
                self._start_threads()
        except Exception as e:
            with self.lock:
                self.error = str(e)
                # 打开失败要释放占位, 否则这台相机永远从设备列表里消失
                if cam_type == "opencv":
                    self.opencv_idx = None
        finally:
            with self.lock:
                self.opening = False

    def _start_threads(self):
        if self._grab_thread is None or not self._grab_thread.is_alive():
            self._grab_thread = threading.Thread(target=self._grab_loop, name="grab", daemon=True)
            self._grab_thread.start()
        if self._enc_thread is None or not self._enc_thread.is_alive():
            self._enc_thread = threading.Thread(target=self._encode_loop, name="encode", daemon=True)
            self._enc_thread.start()
        # ★ 需求：颜色检测**默认开启** —— 相机一打开就把颜色线程拉起来（灰度相机会被关掉）。
        #   灰度相机没有颜色信息，强开会每帧全判"未知"，所以这里按 color_capable 校正。
        with self.lock:
            if not self.color_capable:
                self.color_enabled = False
        if self.color_enabled and _VC_OK:
            self._start_color_thread()

    def close_async(self):
        with self.lock:
            if self.cam is None and not self.opening:
                # 相机本就没打开: 但检测模型可能仍常驻(占数 GB) -> 一并卸载释放内存
                if self.yolo_model is not None:
                    threading.Thread(target=self.unload_model, name="unload", daemon=True).start()
                    return True, "已关闭(并释放检测模型)"
                return True, "已关闭"
            self.closing = True
        threading.Thread(target=self._do_close, name="close", daemon=True).start()
        return True, "正在关闭..."

    def _do_close(self, unload=True):
        try:
            with self.lock:
                # 状态先复位: 相机立即视为已关闭, 后续 MVS SDK 清理(StopGrabbing/
                # CloseDevice 在相机异常/拔线时可能长时间阻塞)不再拖住前端 UI。
                self.running = False
                cam = self.cam
                cam_type = self.cam_type
                self.cam = None
                self.cam_type = None
                self.opencv_idx = None
                self.color_capable = False
                self.latest_frame = None
                self.latest_jpeg = None
                self.latest_dets = []
                self.latest_color = []
                self.det_count = 0
                self.fps = 0.0
                self.closing = False
            if cam is not None:
                try:
                    if cam_type == "opencv":
                        # OpenCV 摄像头
                        cam.release()
                    else:
                        # MVS 工业相机
                        cam.MV_CC_StopGrabbing()
                        cam.MV_CC_CloseDevice()
                        cam.MV_CC_DestroyHandle()
                except Exception:
                    pass
            # 等全部工作线程退出（grab 可能正卡在 1s 超时的 GetImageBuffer；
            # enc/det/color 跟随 running=False 自然退出，但 join 确保退出
            # 后才继续做模型卸载/GC —— 否则 torch 状态可能与清理竞态）
            t = self._grab_thread
            if t is not None and t.is_alive():
                t.join(timeout=2.0)
            for t in (self._enc_thread, self._det_thread, self._color_thread):
                if t is not None and t.is_alive():
                    t.join(timeout=2.0)
            # 主动关闭相机时一并卸载检测模型, 释放 torch 常驻内存(约数 GB)
            # 重连(reconnect)时传 unload=False, 保留模型以便切换回来直接检测
            if unload and self.yolo_model is not None:
                self.unload_model()
        except Exception as e:
            with self.lock:
                self.error = "关闭异常: %s" % e
        finally:
            with self.lock:
                self.closing = False

    def reconnect_async(self):
        """手动重连（网线在摄像头/机器人间切换后触发）：先关再开，无需重启服务。"""
        with self.lock:
            if self.opening or self.closing:
                return False, "正在切换中, 请稍候"
            self.closing = True
        threading.Thread(target=self._do_reconnect, name="reconnect", daemon=True).start()
        return True, "正在重连..."

    def _do_reconnect(self):
        self._do_close(unload=False)   # 重连不卸载模型(切换回来可直接继续检测)
        with self.lock:
            self.opening = True
            self.error = ""
        self._do_open()           # 重新枚举设备并打开（finally 会重置 opening）

    # ---------- 抓帧线程 ----------
    def _grab_loop(self):
        MAX_BAD_STREAK = 12          # 连续疑似坏帧上限(~0.4s @30fps): 超过判"持续全黑"
                                     # 审计修复 P0-ui-4: 魔数语义保留(仍是非偶发坏帧的判据),
                                     # 但超过后不再透传真黑帧, 改为保持最后一帧有效帧
        last = time.time()
        counter = 0
        n = 0
        while self.running:
            # 局部持有一份句柄: 避免与 _do_close 竞态(self.cam 可能被中途置 None)
            cam = self.cam
            cam_type = self.cam_type
            if cam is None:
                break
            try:
                if cam_type == "opencv":
                    # OpenCV 摄像头
                    ret, frame = cam.read()
                    if not ret or frame is None:
                        time.sleep(0.01)
                        continue
                    # OpenCV 返回 BGR 格式，转换为 RGB
                    rgb = frame[:, :, ::-1]
                else:
                    # MVS 工业相机
                    frame = MV_FRAME_OUT()
                    memset(byref(frame), 0, sizeof(frame))
                    ret = cam.MV_CC_GetImageBuffer(frame, 500)
                    if ret != 0 or not frame.pBufAddr:
                        continue
                    rgb = _frame_to_rgb(cam, frame)
                    cam.MV_CC_FreeImageBuffer(frame)
            except Exception:
                # 相机在抓帧过程中被关闭/拔出(句柄失效): 安全退出抓帧循环
                break
            if rgb is None:
                continue
            # ★ 坏帧平滑：工业相机/网络偶发会吐出一帧"整帧全黑或近乎单色"的坏帧，
            #   旧代码只过滤 frame is None/ret != 0，坏帧会被直接编码上屏 → "偶尔黑屏一下"。
            #   这里用下采样估算亮度均值/方差：<4 全黑 或 <3.5 近单色 → 判坏帧，
            #   直接跳过、保持上一有效帧（画面不闪）。
            # 审计修复 P0-ui-4：连续坏帧**超过** MAX_BAD_STREAK 时，原逻辑会掉下去把这帧
            #   真黑帧写进 latest_frame 透传上屏 —— 前端看到的就是"黑屏闪一下"。
            #   看门狗魔数语义原样保留（12 帧仍是"非偶发坏帧"的判据，计数继续往上走），
            #   只是处置从"透传真黑帧"改为"保持最后一帧有效帧"：画面停在最后一帧好帧上，
            #   /status 的 black_streak 照实上报持续全黑，由运维/前端判断真黑，
            #   而不是拿黑帧去闪用户的眼睛。
            rs = rgb[::4, ::4]
            fmean = float(rs.mean())
            if fmean < 4.0 or float(rs.std()) < 3.5:
                self._black_streak += 1
                if self._black_streak <= MAX_BAD_STREAK:
                    time.sleep(0.002)
                    continue
                if self._black_streak == MAX_BAD_STREAK + 1:
                    print(f"[抓帧] 连续坏帧超过 {MAX_BAD_STREAK} 帧，判定画面持续全黑 → "
                          f"保持最后一帧有效帧，不再透传真黑帧", flush=True)
                time.sleep(0.002)
                continue
            self._black_streak = 0
            try:
                img = Image.fromarray(rgb, "RGB")
                if img.width > STREAM_MAX_W:      # 立即降分辨率, 后续都用小图
                    h = int(img.height * STREAM_MAX_W / img.width)
                    img = img.resize((STREAM_MAX_W, h), Image.Resampling.BILINEAR)
                with self.lock:
                    self.latest_frame = img
                    self.frame_count += 1
            except MemoryError:
                # 内存不足时跳过本帧, 不崩溃
                time.sleep(0.1)
                continue
            counter += 1
            n += 1
            # ★ 每 200 帧裁剪一次工作集: 抓帧/缩放/编码的内存分配器只缓存不归还,
            #   长时间运行会让物理内存持续上涨, 定期归还系统(不阻塞抓帧)
            if n % 200 == 0:
                _trim_working_set()
            now = time.time()
            if now - last >= 1.0:
                with self.lock:
                    self.fps = counter / (now - last)
                counter = 0
                last = now

    # ---------- 编码线程 (单例, 多客户端共享) ----------
    def _encode_loop(self):
        target_dt = 1.0 / STREAM_FPS
        n = 0
        err_streak = 0
        while self.running:
            t0 = time.time()
            try:
                with self.lock:
                    has_clients = self.stream_clients > 0
                if has_clients:
                    jpg = self.render_jpeg()
                    if jpg is not None:
                        with self.lock:
                            self.latest_jpeg = jpg
                err_streak = 0
            except Exception as e:
                # ★★ 关键健壮性修复：原实现这里**没有 try/except** —— 只要 render_jpeg 里
                #    任何一次意外（画框坐标越界、PIL 版本差异、临时内存不足…）抛异常，
                #    整个编码线程就直接死掉：/stream 再也不出新帧，画面冻在最后一帧，
                #    现场表现就是"检测突然不工作了/画面不动了"。现在吞掉并限频打印，
                #    线程永不退出，最多丢一帧。
                err_streak += 1
                if err_streak == 1 or err_streak % 50 == 0:
                    self.error = "编码异常(已跳过该帧): %s" % str(e)[:120]
                    print("[编码] 异常，跳过该帧(连续 %d): %r" % (err_streak, e), flush=True)
                time.sleep(0.05)
            # ★ 自愈看门狗：颜色检测开着、相机在跑，但颜色线程死了 → 自动拉起来。
            #   （颜色线程一旦异常退出就不再恢复，表现同样是"检测不工作了"。）
            if self.color_enabled and _VC_OK and self.running:
                ct = self._color_thread
                if ct is not None and not ct.is_alive():
                    print("[颜色] 检测线程已退出，自动重启", flush=True)
                    self._color_thread = None
                    self._start_color_thread()
            dt = time.time() - t0
            time.sleep(max(0.005, target_dt - dt))
            n += 1
            # ★ 每 300 帧裁剪一次工作集: 编码线程的 BytesIO/jpeg 分配只缓存不归还,
            #   长时间运行会让物理内存持续上涨, 定期归还系统(不阻塞编码)
            if n % 300 == 0:
                _trim_working_set()

    def render_jpeg(self, quality=JPEG_QUALITY):
        """用最新小图 + 检测框/颜色框编码一帧 JPEG。内存不足时返回 None（跳帧不崩线程）。"""
        with self.lock:
            if self.latest_frame is None:
                return None
            img = self.latest_frame
            dets = list(self.latest_dets) if self.yolo_enabled else []
            cits = list(self.latest_color) if self.color_enabled else []
        try:
            if dets or cits:
                img = img.copy()
                if dets:
                    img = self._draw(img, dets)
                if cits:
                    img = self._draw_color(img, cits)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
        except MemoryError:
            # copy/draw/save 任一分配失败都跳过本帧，编码线程继续活着
            time.sleep(0.1)
            return None

    def get_jpeg(self):
        with self.lock:
            return self.latest_jpeg

    # ---------- AI ----------
    def set_config(self, enabled=None, model=None, conf=None, imgsz=None):
        if conf is not None:
            self.conf = max(0.05, min(0.95, float(conf)))
        if imgsz is not None:
            self.imgsz = max(320, min(1280, int(imgsz)))
        need_load = False
        if model is not None and model != self.yolo_name:
            self.yolo_name = model
            self.yolo_model = None
            need_load = True
        if enabled is not None:
            self.yolo_enabled = bool(enabled)
            # 取消勾选"启用检测" -> 卸载模型释放 torch 常驻内存(不用就不占)
            if not self.yolo_enabled and self.yolo_model is not None:
                self.unload_model()
                return self.status()
        # 启用时: 模型未加载 -> 加载; 已加载但检测线程已退出 -> 重启线程
        if self.yolo_enabled:
            if self.yolo_model is None and not self.yolo_loading:
                need_load = True
            elif self.yolo_model is not None and (self._det_thread is None or not self._det_thread.is_alive()):
                need_load = True
        if need_load and self.yolo_enabled:
            # ★ 必须 gate yolo_enabled：否则 enabled=False + 仅改模型名也会
            #   触发数 GB 的模型加载，检测线程又立即退出 —— 纯浪费内存。
            threading.Thread(target=self._load_yolo, args=(self.yolo_name,), daemon=True).start()
        return self.status()

    def _load_yolo(self, name):
        self.yolo_loading = True
        try:
            if self.yolo_model is None:
                from ultralytics import YOLO
                path = name if os.path.isabs(name) else os.path.join(MODELS_DIR, name)
                self.yolo_model = YOLO(path)
            self.error = ""
            if self._det_thread is None or not self._det_thread.is_alive():
                self._det_thread = threading.Thread(target=self._detect_loop, name="detect", daemon=True)
                self._det_thread.start()
        except Exception as e:
            self.error = "模型加载失败: %s" % e
        finally:
            self.yolo_loading = False

    def unload_model(self):
        """卸载 YOLO 检测模型并尽量回收内存。

        torch(PyTorch) 一旦加载即常驻, 占用可达数 GB; 本方法释放模型对象 +
        触发 GC + 清空 torch 缓存, 把不用检测时的内存尽可能还回系统。
        """
        if self.yolo_loading:
            return False, "模型正在加载中, 请稍候"
        with self.lock:
            self.yolo_enabled = False
            self.yolo_model = None
            self.latest_dets = []
            self.det_count = 0
            self.det_ms = 0.0
        # 等检测线程退出
        t = self._det_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._det_thread = None
        # 尽量回收: gc + torch 缓存 + Windows 工作集裁剪
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        gc.collect()
        _trim_working_set()
        return True, "已卸载检测模型并回收内存"

    def _detect_loop(self):
        n = 0
        while self.yolo_enabled and self.yolo_model is not None:
            # 局部持有一份模型引用: 避免与 unload_model 竞态(中途被置 None)
            model = self.yolo_model
            if model is None:
                break
            with self.lock:
                frame = self.latest_frame.copy() if self.latest_frame is not None else None
            if frame is None:
                time.sleep(0.1)
                continue
            try:
                t0 = time.time()
                arr = np.asarray(frame)
                results = model.predict(arr, conf=self.conf, imgsz=self.imgsz, verbose=False)
                self.det_ms = time.time() - t0
                dets = []
                r0 = results[0] if results else None
                if r0 is not None and r0.boxes is not None:
                    names = r0.names
                    for b in r0.boxes:
                        xyxy = [float(v) for v in b.xyxy[0].tolist()]
                        cls = int(b.cls[0])
                        raw = names[cls] if cls in names else str(cls)
                        dets.append((xyxy, raw, float(b.conf[0])))
                with self.lock:
                    self.latest_dets = dets
                    self.det_count = len(dets)
                # 每 100 帧裁剪一次工作集: torch 推理的内存分配器只缓存不归还,
                # 长时间运行会让物理内存持续上涨, 定期归还系统(不阻塞检测)
                n += 1
                if n % 100 == 0:
                    _trim_working_set()
            except Exception as e:
                with self.lock:
                    self.error = "检测异常: %s" % str(e)[:120]
                time.sleep(0.5)

    # ============ 颜色分拣 (P2/P3) ============
    @staticmethod
    def _pil_to_bgr(img):
        """PIL RGB → numpy BGR(连续内存)。不用 cv2, 少一层依赖。"""
        arr = np.asarray(img)
        if arr.ndim == 3 and arr.shape[2] == 3:
            return np.ascontiguousarray(arr[:, :, ::-1])
        return arr

    def _norm_roi(self, roi):
        try:
            x, y, w, h = [int(float(v)) for v in roi]
        except Exception:
            return None
        if x < 0 or y < 0 or w <= 8 or h <= 8:
            return None
        return (x, y, w, h)

    def _ensure_engine(self):
        if self.color_engine is None:
            # ★ 实时链路对齐参考实现（visual_object_detector.py）：**只用颜色阈值**做前景，
            #   不做 MOG2/背景差分（背景差分对"物体色≈背景色"和静止目标都会漏检，
            #   是现场"偶尔才检测到"的主因之一）。只认红/绿/蓝三色矩形时这条最稳。
            self.color_engine = vc.VisionEngine(
                background=self.background, use_mog2=False,
                roi=self.roi, calib=self.color_calib,
                debug=self.vision_debug,
                color_only=self.color_only,
                thr=self.vision_thr)   # ★ 审计修复 P0-cam-3: 阈值随引擎重建生效
        return self.color_engine

    def set_vision_config(self, enabled=None, roi=None, use_mog2=None,
                          min_interval=None, save_on_detect=None,
                          min_conf=None, debug=None, reset_background=False,
                          thr=None):
        """更新颜色分拣配置。任何会改变输入/判据的项都重建引擎(清空稳定器状态)。"""
        if not _VC_OK:
            return self.status()
        rebuild = False
        with self.lock:
            if debug is not None:
                self.vision_debug = bool(debug)
                if self.color_engine is not None:
                    self.color_engine.debug = bool(debug)
            # ★ 审计修复 P0-cam-3: 只接受部分覆盖(未知键/坏值由 norm_thr 丢弃限幅)，
            #   与旧阈值合并后整体比较 —— 值没变就不重建引擎，避免稳定器状态被白清。
            if thr:
                merged = vc.norm_thr({**self.vision_thr, **thr})
                if merged != self.vision_thr:
                    self.vision_thr = merged
                    rebuild = True
            if enabled is not None and bool(enabled) != self.color_enabled:
                if bool(enabled) and self.cam is not None and not self.color_capable:
                    # 灰度相机没有颜色信息, 强行开只会每帧全判"未知"; 直接拒并提示
                    self.error = "当前相机为灰度相机，无法彩色识别，请切换到彩色相机（USB 彩色 / 海康 GC 系列）"
                else:
                    self.color_enabled = bool(enabled)
                    rebuild = True
            if roi is not None:
                self.roi = self._norm_roi(roi) if roi not in ([], None) else None
                rebuild = True
            if use_mog2 is not None and bool(use_mog2) != self.use_mog2:
                self.use_mog2 = bool(use_mog2)
                rebuild = True
            if min_interval is not None:
                self.color_min_interval = max(0.0, float(min_interval))
            if save_on_detect is not None:
                self.save_on_detect = bool(save_on_detect)
            if min_conf is not None:
                # 阈值可微调: 无需重建引擎, 只影响每帧画框/记录的过滤
                self.color_conf = float(max(0.0, min(1.0, min_conf)))
            if reset_background:
                self.background = None
                rebuild = True
            if rebuild:
                self.color_engine = None
                self.latest_color = []
        if self.color_enabled and self.running:
            self._start_color_thread()
        return self.status()

    def _start_color_thread(self):
        if self._color_thread is None or not self._color_thread.is_alive():
            self._color_thread = threading.Thread(
                target=self._color_loop, name="color", daemon=True)
            self._color_thread.start()

    def _color_loop(self):
        """颜色检测线程: 复用 latest_frame 小图, 不额外抓帧、不跨进程传图。"""
        target_dt = 1.0 / max(1, COLOR_FPS)
        n = 0
        while self.color_enabled and self.running:
            t0 = time.time()
            with self.lock:
                frame = self.latest_frame.copy() if self.latest_frame is not None else None
            if frame is None:
                time.sleep(0.1)
                continue
            try:
                bgr = self._pil_to_bgr(frame)
                eng = self._ensure_engine()
                res = eng.detect(bgr)
                with self.lock:
                    self.color_ms = time.time() - t0
                    conf_thr = self.color_conf
                    # ★ 需求（对齐参考实现"只报最大的一个"）：实时只显示**唯一**目标 ——
                    #   先过置信度阈值，再取面积最大的（并列取置信度高者）。
                    #   ⚠ 之前是"每色各留一个（≤3）"，会同时画多个框；现按用户口径收敛成一个。
                    best = vc.pick_best(res["results"], conf_thr)
                    self.latest_color = [
                        {"box": r["box"], "color": r["color"], "center": r["center"]}
                        for r in best
                    ]
                if res.get("fire"):
                    self._on_detect(frame, bgr, res)
            except Exception as e:
                with self.lock:
                    self.error = "视觉异常: %s" % str(e)[:120]
                time.sleep(0.5)
            n += 1
            if n % 200 == 0:               # 与检测线程同理: 定期把工作集还给系统
                _trim_working_set()
            dt = time.time() - t0
            time.sleep(max(0.005, target_dt - dt))

    def _on_detect(self, frame_pil, bgr, res):
        """稳定触发一次: 节流 → 落盘三件套 → 生成记录/事件。"""
        r0 = None
        with self.lock:
            conf_thr = self.color_conf
        qualified = vc.pick_best(res["results"], conf_thr)   # ★ 与实时画框同一判据：唯一、最大
        r0 = qualified[0] if qualified else None
        if r0 is None:
            return None
        col = r0["color"]
        name = str(col.get("name") or "?")
        now = time.time()
        # 同一颜色在节流窗口内的抖动不重复记录(稳定器已保证单物体只触发一次,
        # 这里再兜一层: 防止物体边缘抖动导致 离开→复位→再触发 的抖动串)。
        prev_name, prev_t = self._last_fire
        if prev_name == name and (now - prev_t) < self.color_min_interval:
            return None
        self._last_fire = (name, now)

        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        ms = int((now % 1.0) * 1000)
        adir = os.path.join(VISION_DIR, "%s_%03d_%s" % (stamp, ms, _safe_name(name)))

        with self.lock:
            save = self.save_on_detect
        paths = {}
        if save:
            try:
                paths = self._archive(adir, frame_pil, res, r0)
            except Exception as e:
                with self.lock:
                    self.error = "视觉存档失败: %s" % str(e)[:120]

        ev_body = {
            "ts": round(now, 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "color": name,
            "hex": col.get("hex"),
            "conf": col.get("conf"),
            "de": col.get("de"),
            "alt": col.get("alt"),
            "alt_de": col.get("alt_de"),
            "chroma": col.get("chroma"),
            "ratio": col.get("ratio"),
            "lab": col.get("lab"),
            "reason": col.get("reason"),
            "box": [[int(v) for v in p] for p in r0["box"]],
            "center": [round(float(r0["center"][0]), 1), round(float(r0["center"][1]), 1)],
            "area": int(r0.get("area") or 0),
            "objects": len(res["results"]),
            "dir": adir if save else None,
            "images": paths,
        }
        with self.lock:
            # ★ 审计修复 P0-cam-1: 序号自增 + 事件发布 + 记录入列必须在**同一个临界区**
            #   内一次性完成，且全部放在落盘(_archive)之后。
            #   旧版在落盘前就 `self.color_seq += 1`，而 `last_event` 要等三张图写完才更新，
            #   于是中间存在一个可观测窗口：GET /vision/last 返回 {seq: N, event: 旧/None}。
            #   消费方(backend vision_ingest)只要拿到 seq 就会推进游标 → 游标越过 N 之后，
            #   事件 N 永远不再满足 `event.seq > since` → 这次检测被**永久丢弃**，
            #   表现就是"偶尔才检测到"(丢不丢取决于轮询是否恰好落进落盘窗口)。
            #   现在 seq 与事件同生共死：没有事件就没有序号，游标永远追不上"还没发布的东西"。
            self.color_seq += 1
            ev = {"seq": self.color_seq, **ev_body}
            self.color_count += 1
            self.last_event = ev
            self.records.insert(0, ev)
            if len(self.records) > MAX_RECORDS:
                del self.records[MAX_RECORDS:]
        return ev

    def _archive(self, adir, frame_pil, res, r0):
        """落盘三件套: 带框图 / 物体裁剪 / 分割掩膜。

        ★ 掩膜用 PIL 存 PNG 而不是 cv2.imwrite: 存档目录名含中文,
          cv2.imwrite 在 Windows 上遇到非 ASCII 路径会静默失败。
        ★ 返回的 images 是**相对 VISION_DIR 的正斜杠路径** —— 前端要直接拿它拼
          /vision/image?p=... 取图; 绝对路径另放在事件的 dir 字段里供运维查看。
        """
        os.makedirs(adir, exist_ok=True)
        paths = {}

        def _rel(p):
            return os.path.relpath(p, VISION_DIR).replace("\\", "/")

        item = [{"box": r0["box"], "color": r0["color"], "center": r0["center"]}]
        full = self._draw_color(frame_pil.copy(), item)
        p_full = os.path.join(adir, "full.jpg")
        full.save(p_full, quality=92)
        paths["full"] = _rel(p_full)

        xs = [float(p[0]) for p in r0["box"]]
        ys = [float(p[1]) for p in r0["box"]]
        x1, y1 = max(0, int(min(xs)) - 8), max(0, int(min(ys)) - 8)
        x2 = min(frame_pil.width, int(max(xs)) + 8)
        y2 = min(frame_pil.height, int(max(ys)) + 8)
        if x2 > x1 and y2 > y1:
            p_crop = os.path.join(adir, "crop.jpg")
            frame_pil.crop((x1, y1, x2, y2)).save(p_crop, quality=92)
            paths["crop"] = _rel(p_crop)

        mask = res.get("mask")
        if mask is not None:
            try:
                p_mask = os.path.join(adir, "mask.png")
                Image.fromarray(np.asarray(mask)).save(p_mask)
                paths["mask"] = _rel(p_mask)
            except Exception:
                pass
        return paths

    def capture_background(self):
        """把当前画面固化为静态背景(静态相机下比 MOG2 更稳)。"""
        if not _VC_OK:
            return False, "颜色模块不可用"
        with self.lock:
            if self.latest_frame is None:
                return False, "无画面(相机未开启)"
            self.background = self._pil_to_bgr(self.latest_frame)
            # ★ 显式设了静态背景 = 明确要"背景差分"语义 → 关掉"只用颜色阈值"，
            #   否则设了背景却不生效（界面写着静态背景、实际仍在跑 HSV 阈值）。
            self.color_only = False
            self.color_engine = None
            self.latest_color = []
        return True, "已把当前画面设为静态背景"

    def clear_background(self):
        with self.lock:
            self.background = None
            self.color_engine = None
            # 清掉背景后回到默认的"只用颜色阈值"（参考实现口径）
            self.color_only = True
        return True, "已清除静态背景（回到颜色阈值模式）"

    def calibrate_gray(self, target=118.0):
        """中心区域一键灰卡白平衡(现场把 18% 灰卡放画面中心, 点一下即可)。"""
        if not _VC_OK:
            return False, "颜色模块不可用", None
        with self.lock:
            if self.latest_frame is None:
                return False, "无画面(相机未开启)", None
            img = self.latest_frame
        bgr = self._pil_to_bgr(img)
        h, w = bgr.shape[:2]
        cw, ch = max(24, int(w * 0.12)), max(24, int(h * 0.12))
        cx, cy = w // 2, h // 2
        patch = bgr[max(0, cy - ch // 2):cy + ch // 2, max(0, cx - cw // 2):cx + cw // 2]
        if patch.size == 0:
            return False, "取样区域为空", None
        gains = vc.gray_card_gains(patch, target=float(target))
        with self.lock:
            self.color_calib["gains"] = gains
            self.color_engine = None
            calib = dict(self.color_calib)
        vc.save_calib(CALIB_PATH, calib)
        return True, "灰卡白平衡已标定", {"gains": gains}

    def calibrate_sample(self, name: str, lab=None):
        """现场实物标定: 用最近一次判定(或给定 Lab)覆盖该色的参考值。

        这比任何标准色卡都贴合实际 —— 现场物料的光谱响应往往和标准色卡差很多。
        """
        if not _VC_OK:
            return False, "颜色模块不可用", None
        if not name:
            return False, "缺少颜色名", None
        if lab is None:
            with self.lock:
                ev = self.last_event
            if not ev or not ev.get("lab"):
                return False, "还没有可用的判定结果", None
            if ev.get("color") != name:
                # 允许"把当前样本归到指定颜色" —— 正是低置信样本人工复核的用法
                pass
            lab = ev["lab"]
        try:
            lab3 = [float(x) for x in lab][:3]
        except Exception:
            return False, "Lab 格式不正确", None
        if len(lab3) != 3:
            return False, "Lab 需要 3 个分量", None
        with self.lock:
            card = dict(self.color_calib.get("card") or {})
            card[name] = lab3
            self.color_calib["card"] = card
            self.color_engine = None
            calib = dict(self.color_calib)
        vc.save_calib(CALIB_PATH, calib)
        return True, "已用实测样本标定「%s」" % name, {"name": name, "lab": lab3}

    def detect_once(self):
        """对当前帧同步跑一次(调试/标定用), 返回完整 JSON(含 Lab)。"""
        if not _VC_OK:
            return {"ok": False, "error": _VC_ERR}
        with self.lock:
            if self.latest_frame is None:
                return {"ok": False, "error": "无画面"}
            frame = self.latest_frame.copy()
        bgr = self._pil_to_bgr(frame)
        res = self._ensure_engine().detect(bgr)
        res.pop("mask", None)
        return res

    def get_records(self, limit=50, color=None):
        with self.lock:
            recs = list(self.records)
        if color:
            recs = [r for r in recs if r.get("color") == color]
        try:
            limit = max(1, min(MAX_RECORDS, int(limit)))
        except Exception:
            limit = 50
        return recs[:limit]

    def clear_records(self):
        with self.lock:
            self.records = []
        return True, "已清空颜色记录"

    def stats(self):
        """各颜色计数(现场最常问"今天红的有多少")。"""
        with self.lock:
            recs = list(self.records)
        cnt: dict = {}
        for r in recs:
            cnt[r.get("color") or "?"] = cnt.get(r.get("color") or "?", 0) + 1
        return {"total": len(recs), "by_color": cnt}

    def _draw_color(self, img, items):
        """画颜色框 + 中文标签(含置信度/疑似色)。

        ★ 画风（回退到 2026-09-25 现场确认"检测可以"的那版）：
          贴合目标的旋转多边形整圈外框 + 外扩加深描边 + 四角加强 + 半透明圆角标签条。
          （曾试过"只画四个角 + 标签上移"，现场反馈不如这一版，故回退。）
          ★ 标签文字按现场要求**再放大**：0.032 → 0.040（最小 26px）。
        """
        draw = ImageDraw.Draw(img, "RGBA")
        fsize = max(26, int(min(img.width, img.height) * 0.040))   # ★ 文字放大
        font = _font(fsize)
        lw = max(5, int(fsize / 5))
        for it in items:
            col = it.get("color") or {}
            rgb = _hex_rgb(col.get("hex"))
            pts = [(int(p[0]), int(p[1])) for p in it.get("box") or []]
            if len(pts) >= 3:
                # 1) 外扩加深描边（半透明）→ 视觉上更像"框"，远处也看得见
                draw.line(pts + [pts[0]], fill=rgb + (95,), width=lw * 2, joint="curve")
                # 2) 主框
                draw.line(pts + [pts[0]], fill=rgb, width=lw, joint="curve")
                # 3) 四角加强（在四个顶点画小方块）
                cl = max(9, fsize // 2)
                for (px, py) in pts:
                    draw.rectangle((px - cl, py - cl, px + cl, py + cl),
                                   outline=rgb, width=lw)
            name = str(col.get("name") or "?")
            conf = col.get("conf")
            label = name if conf is None else "%s %.0f%%" % (name, float(conf) * 100.0)
            if col.get("alt"):
                label += " (疑似%s)" % col["alt"]
            if col.get("reason"):
                label += " ·%s" % col["reason"]
            th = fsize + 10
            tw = int(draw.textlength(label, font=font))
            px, py = pts[0] if pts else (0, 0)
            # 标签条贴着框的上边；贴到顶就压在框内，绝不让它跑出画面
            ty = py - th if py - th >= 0 else py
            bx2 = min(img.width - 1, px + tw + 12)
            draw.rounded_rectangle([px, ty, bx2, ty + th], radius=8,
                                   fill=(0, 0, 0, 165), outline=rgb + (220,), width=2)
            draw.text((px + 6, ty + 5), label, fill=(255, 255, 255, 255), font=font)
        return img

    def _draw(self, img, dets):
        draw = ImageDraw.Draw(img)
        fsize = max(16, int(min(img.width, img.height) * 0.024))
        font = _font(fsize)
        lw = max(2, int(fsize / 9))
        for xyxy, raw, conf in dets:
            x1, y1, x2, y2 = [int(v) for v in xyxy]
            x1, y1 = max(0, x1), max(0, y1)
            cn = CN_NAMES.get(raw, raw)
            label = "%s(%s) %.2f" % (cn, raw, conf)
            draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 90), width=lw)
            tw = draw.textlength(label, font=font)
            th = fsize + 6
            draw.rectangle([x1, max(0, y1 - th), x1 + int(tw) + 6, y1], fill=(0, 255, 90))
            draw.text((x1 + 3, max(0, y1 - th) + 3), label, fill=(0, 0, 0), font=font)
        return img

    def save_snapshot(self):
        with self.lock:
            if self.latest_frame is None:
                return None
            img = self.latest_frame.copy()
            dets = list(self.latest_dets) if self.yolo_enabled else []
            cits = list(self.latest_color) if self.color_enabled else []
        name = "capture_%s.jpg" % time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(CAPTURE_DIR, name)
        out = img
        if dets:
            out = self._draw(out, dets)
        if cits:
            out = self._draw_color(out, cits)
        out.save(path, quality=92)
        return path


SERVICE = CameraService()


# ============================ HTTP ============================
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle(self):
        """客户端在请求完成前断开(刷新页面 / 中断 MJPEG 流)会抛
        ConnectionAbortedError(10053)/ConnectionResetError 等, 这里静默忽略,
        避免 socketserver.handle_error 打印满屏 traceback。"""
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/stream":
            return self._stream()
        if path == "/snapshot":
            jpg = SERVICE.render_jpeg(quality=90)   # 按需编码一张高质量单帧
            if jpg is None:
                return self._json({"ok": False, "error": "无画面(相机未开启)"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(jpg)
            return
        if path == "/status":
            return self._json(SERVICE.status())
        if path == "/models":
            return self._json({"models": SERVICE.list_models()})
        # ---------- 颜色分拣 ----------
        if path == "/vision/status":
            with SERVICE.lock:
                return self._json(SERVICE.vision_status_locked())
        if path == "/vision/last":
            q = urlparse(self.path).query
            since = 0
            for kv in q.split("&"):
                if kv.startswith("since="):
                    try:
                        since = int(kv[6:])
                    except Exception:
                        since = 0
            with SERVICE.lock:
                ev = SERVICE.last_event
                # ★ 审计修复 P0-cam-1: 对消费方可见的 seq 必须等于"已发布事件"的序号。
                #   seq 是**消费游标**，一旦大于 event.seq，消费方就会把"序号已自增、
                #   事件还没发布"的窗口读成"这条我已经收过了" → 永久漏检。
                #   保守取 min(当前序号, 事件序号)；事件为 None 时返回 0 —— 消费方
                #   用 max() 推进，返回 0 只会让它原地等待，不会回退，也不会跳号。
                ev_seq = int(ev.get("seq") or 0) if ev else 0
                seq = min(SERVICE.color_seq, ev_seq) if ev else 0
            if ev is not None and ev_seq > since:
                return self._json({"ok": True, "seq": seq, "event": ev})
            return self._json({"ok": True, "seq": seq, "event": None})
        if path == "/vision/records":
            q = urlparse(self.path).query
            limit, color = 50, None
            for kv in q.split("&"):
                if kv.startswith("limit="):
                    try:
                        limit = int(kv[6:])
                    except Exception:
                        pass
                elif kv.startswith("color="):
                    color = kv[6:]
            return self._json({"ok": True, "records": SERVICE.get_records(limit, color),
                               "stats": SERVICE.stats()})
        if path == "/vision/detect":
            return self._json(SERVICE.detect_once())
        if path == "/vision/image":
            return self._serve_vision_image(urlparse(self.path).query)
        # ---------- 设备列表 ----------
        if path == "/devices":
            return self._json({"ok": True, "devices": SERVICE.list_devices(),
                               "current": SERVICE.current_device()})
        return self._json({"ok": False, "error": "未知路径"}, 404)

    def _serve_vision_image(self, query: str):
        """按相对路径回传视觉存档图。★ 路径必须落在 VISION_DIR 内(防目录穿越)。"""
        rel = ""
        for kv in query.split("&"):
            if kv.startswith("p="):
                rel = kv[2:]
        try:
            from urllib.parse import unquote
            rel = unquote(rel).replace("\\", "/")
            base = os.path.abspath(VISION_DIR)
            full = os.path.abspath(os.path.join(base, rel))
            if not full.startswith(base + os.sep) or not os.path.isfile(full):
                return self._json({"ok": False, "error": "图片不存在"}, 404)
            with open(full, "rb") as f:
                data = f.read()
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        ctype = "image/png" if full.lower().endswith(".png") else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        if path == "/open":
            ok, msg = SERVICE.open_async()
            return self._json({"ok": ok, "message": msg, "status": SERVICE.status()})
        if path == "/close":
            ok, msg = SERVICE.close_async()
            return self._json({"ok": ok, "message": msg, "status": SERVICE.status()})
        if path == "/config":
            st = SERVICE.set_config(
                enabled=body.get("enabled"), model=body.get("model"),
                conf=body.get("conf"), imgsz=body.get("imgsz"))
            return self._json({"ok": True, "status": st})
        if path == "/snapshot-save":
            p = SERVICE.save_snapshot()
            return self._json({"ok": bool(p), "path": p})
        if path == "/reconnect":
            ok, msg = SERVICE.reconnect_async()
            return self._json({"ok": ok, "message": msg, "status": SERVICE.status()})
        if path == "/switch":
            idx = body.get("idx")
            cam_type = body.get("cam_type", "mvs")
            if idx is None:
                return self._json({"ok": False, "error": "缺少 idx 参数"}, 400)
            ok, msg = SERVICE.switch_device(idx, cam_type=cam_type)
            return self._json({"ok": ok, "message": msg, "status": SERVICE.status()})
        if path == "/unload":
            ok, msg = SERVICE.unload_model()
            return self._json({"ok": ok, "message": msg, "status": SERVICE.status()})
        # ---------- 颜色分拣 ----------
        if path == "/vision/config":
            st = SERVICE.set_vision_config(
                enabled=body.get("enabled"),
                roi=body.get("roi", None) if "roi" in body else None,
                use_mog2=body.get("use_mog2"),
                min_interval=body.get("min_interval"),
                save_on_detect=body.get("save_on_detect"),
                min_conf=body.get("min_conf"),
                debug=body.get("debug"),
                thr=body.get("thr") if "thr" in body else None,   # ★ P0-cam-3 阈值覆盖
                reset_background=bool(body.get("reset_background")))
            return self._json({"ok": True, "status": st})
        if path == "/vision/background":
            if body.get("reset"):
                ok, msg = SERVICE.clear_background()
            else:
                ok, msg = SERVICE.capture_background()
            if ok and SERVICE.color_enabled and SERVICE.running:
                SERVICE._start_color_thread()
            return self._json({"ok": ok, "message": msg, "status": SERVICE.status()})
        if path == "/vision/calibrate/gray":
            ok, msg, info = SERVICE.calibrate_gray(target=body.get("target", 118.0))
            return self._json({"ok": ok, "message": msg, "info": info,
                               "status": SERVICE.status()})
        if path == "/vision/calibrate/sample":
            ok, msg, info = SERVICE.calibrate_sample(
                str(body.get("name") or ""), lab=body.get("lab"))
            return self._json({"ok": ok, "message": msg, "info": info,
                               "status": SERVICE.status()})
        if path == "/vision/records/clear":
            ok, msg = SERVICE.clear_records()
            return self._json({"ok": ok, "message": msg})
        return self._json({"ok": False, "error": "未知路径"}, 404)

    def _stream(self):
        """MJPEG: 只发送编码线程产出的 latest_jpeg, 不在此处编码。"""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        with SERVICE.lock:
            SERVICE.stream_clients += 1
        last = None
        try:
            while SERVICE.running:
                jpg = SERVICE.get_jpeg()
                if jpg is not None and jpg is not last:
                    last = jpg
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpg)).encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                else:
                    time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with SERVICE.lock:
                SERVICE.stream_clients -= 1


def main():
    print("=" * 56)
    print("  EFORT 视觉检测服务  (海康 GigE + YOLO)")
    print("  http://127.0.0.1:%d/stream" % PORT)
    print("  SDK: %s" % ("OK" if _SDK_OK else _SDK_ERR))
    print("  模型目录: %s" % MODELS_DIR)
    print("  MVS 封装: %s%s" % (REF_SDK, "" if os.path.isdir(REF_SDK) else "  (目录不存在!)"))
    print("=" * 56)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVICE._do_close()


if __name__ == "__main__":
    main()
