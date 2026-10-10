# -*- coding: utf-8 -*-
"""静态评审（2026-10-10）落地项的安全钉子。

这些都是"**改回去就会重新变危险**"的点，用**源码断言**钉住 ——
相机服务跑在另一个 venv（cv2/PIL），后端测试不能 import 它，所以按文本断言。
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- A1 视觉服务
def test_camera_service_binds_loopback_by_default():
    """★ 视觉服务无鉴权 → 默认只能从本机访问（原来硬写 0.0.0.0）。"""
    src = _read("camera/camera_service.py")
    assert 'os.environ.get("EFORT_CAMERA_HOST", "127.0.0.1")' in src
    assert "ThreadingHTTPServer((HOST, PORT), Handler)" in src
    assert 'ThreadingHTTPServer(("0.0.0.0"' not in src, "不能硬写 0.0.0.0：本服务无鉴权"


def test_camera_write_endpoints_require_token_when_configured():
    """★ 配了 EFORT_CAMERA_TOKEN → 写接口必须带 X-Camera-Token，且**在读 body 之前**拦。"""
    src = _read("camera/camera_service.py")
    assert "EFORT_CAMERA_TOKEN" in src and "_camera_token_ok" in src
    i = src.index("def do_POST(self):")
    body = src[i:i + 900]
    assert "_camera_token_ok()" in body
    assert body.index("_camera_token_ok()") < body.index("self._read_json()")


def test_camera_cors_not_wildcard():
    """★ 原 `Allow-Origin: *` 让任意网站都能在操作员浏览器里 POST 本服务（CSRF 类）。"""
    src = _read("camera/camera_service.py")
    assert '"Access-Control-Allow-Origin", "*"' not in src
    assert "X-Camera-Token" in src


def test_camera_stream_has_client_cap():
    """★ MJPEG 并发上限：原来无上限，几十个 /stream 就能把线程/带宽吃光。"""
    src = _read("camera/camera_service.py")
    assert "MAX_STREAM_CLIENTS" in src
    i = src.index("def _stream(self):")
    assert "MAX_STREAM_CLIENTS" in src[i:i + 900]


# ---------------------------------------------------------------- A3 后端绑定
def test_backend_binds_loopback_by_default():
    """★ 默认只绑本机；要开放局域网必须显式设 EFORT_HOST。"""
    bat = _read("scripts/start_backend.bat")
    assert "--host %EFORT_HOST%" in bat
    assert 'if "%EFORT_HOST%"=="" set "EFORT_HOST=127.0.0.1"' in bat


# ---------------------------------------------------------------- A4 令牌上限
def test_auth_token_table_is_bounded():
    """★ 令牌表原来无上限；登录成功后要按"先清过期、再逐最旧"收敛。"""
    src = _read("backend/app/api/auth.py")
    assert "_MAX_TOKENS" in src and "_prune_tokens" in src
    assert "_prune_tokens(keep=tok)" in src


# ---------------------------------------------------------------- A5 软链穿越
def test_path_handlers_resolve_symlinks():
    """★ 目录内若有指向外面的软链，abspath 看不出来 —— 两边都要 realpath。"""
    assert "os.path.realpath(_program_dir())" in _read("backend/app/api/control.py")
    assert "os.path.realpath(VISION_DIR)" in _read("camera/camera_service.py")


# ---------------------------------------------------------------- B1 线程池
def test_backend_raises_threadpool_limiter():
    """★ 长任务（run-file / ready / chat）会占住同步线程池的 worker → 调高上限止血。"""
    src = _read("backend/app/main.py")
    assert "current_default_thread_limiter().total_tokens" in src


# ---------------------------------------------------------------- B3 暗色掩膜
def test_dark_mask_only_computed_when_no_color_candidate():
    """★ 暗色掩膜只服务"无彩色候选"的兜底支路 → 有候选时不该白算（每帧几毫秒）。"""
    src = _read("camera/vision_color.py")
    assert "if self.color_only and not rects:" in src
    assert "elif self.color_only and _looks_dark(" in src


# ---------------------------------------------------------------- 相机后端默认 USB
def test_camera_defaults_to_usb_and_skips_mvs():
    """★ 用户口径："不要启动工业相机那套，用 USB 的就行了"。

    默认 `EFORT_CAMERA_KIND=usb` 时**完全不碰**海康 MVS：不塞 PATH、不 import 封装、不枚举。
    """
    src = _read("camera/camera_service.py")
    assert 'os.environ.get("EFORT_CAMERA_KIND", "usb")' in src
    assert '_MVS_ENABLED = CAMERA_KIND in ("mvs", "auto")' in src
    assert "if _MVS_ENABLED and os.path.isdir(MVS_DLL_DIR)" in src
    assert "if _MVS_ENABLED and os.path.isdir(REF_SDK)" in src
    assert 'order = ("opencv",) if CAMERA_KIND == "usb"' in src


def test_camera_enumeration_noise_is_silenced():
    """★ 探测索引时的 `VIDEOIO(DSHOW) … can't be used to capture by index` 属正常噪音，
    但现场会据此以为"相机坏了" → 探测期间必须静音、探测完恢复原级别。"""
    src = _read("camera/camera_service.py")
    assert "LOG_LEVEL_SILENT" in src
    assert "cv2.utils.logging.setLogLevel(_lvl)" in src


def test_camera_misleading_enumeration_text_is_gone():
    """★ 原 `[相机枚举] 发现 0 个设备` 看着像"没找到相机"（其实只是 MVS 那一支）→ 已改文案。"""
    src = _read("camera/camera_service.py")
    assert 'print(f"[相机枚举] 发现' not in src      # 只查"真打印出来的那句"（注释里提到原文案不算）
    assert 'print(f"[海康工业相机(MVS)] 发现' in src
