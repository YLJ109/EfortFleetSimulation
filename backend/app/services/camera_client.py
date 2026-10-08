# -*- coding: utf-8 -*-
"""
相机服务(视觉分拣) HTTP 客户端 —— 零新增依赖, 只用标准库 urllib。

为什么不用 requests/httpx:
  后端 requirements 里没有 HTTP 客户端库, 而这里只需要 4 个动作
  (GET JSON / POST JSON / GET 二进制 / 探测存活),
  urllib 足够, 而且不用为这个小功能给生产环境增加一个依赖。

★ 超时一定要短(config.vision.timeout_s, 默认 3s):
  相机服务没起时, 每次调用都必须快速失败 —— 否则后端接口会被拖住,
  前端轮询会在"相机服务没开"这种最常见的情形下卡成一片空白。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from app.core.config import get_config
from app.core.logger import get_logger

log = get_logger("camera_client")

_CACHE: Dict[str, Any] = {"base": None, "ts": 0.0}
_CACHE_TTL = 5.0


def base_url() -> str:
    """相机服务基址（带 5 秒缓存, 避免每个请求都读一遍配置）。"""
    now = time.time()
    if _CACHE["base"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["base"]
    url = str(get_config().vision.get("base_url") or "http://127.0.0.1:8100").rstrip("/")
    _CACHE["base"] = url
    _CACHE["ts"] = now
    return url


def invalidate() -> None:
    """立刻丢弃地址缓存。

    ★ 改了 `camera.host` / `camera.port` 之后必须调用：否则最长 5 秒内还在往**旧地址**
      发请求，现场表现为"保存了没生效，过一会儿又自己好了" —— 最难查的那种时序问题。
      设置页保存与重置都会调它。
    """
    _CACHE["base"] = None
    _CACHE["ts"] = 0.0


def _timeout() -> float:
    try:
        return float(get_config().vision.get("timeout_s") or 5.0)  # ★ 5s：相机服务重连/加载模型可能久一点
    except Exception:
        return 5.0


# ★★ 相机服务永远是**本机**（默认 127.0.0.1:8100）：请求必须**绕开 HTTP 代理**。
#   踩过的坑（2026-10-08）：本机 shell 里设了 https_proxy（如 127.0.0.1:56638），
#   urllib 默认会把**连本机 8100 的请求也丢给代理** → 代理拒绝/中止连接
#   → WinError 10053 → 相机服务"不可达"、11 条 vision 用例整片红。
#   项目里早就把这条写进纪律（真机脚本必须禁代理），但生产代码这里一直没做 —— 现在补上。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ★★ 相机服务永远是**本机**（默认 127.0.0.1:8100）：请求必须**绕开 HTTP 代理**。
#   踩过的坑（2026-10-08）：本机 shell 里设了 https_proxy（如 127.0.0.1:56638），
#   urllib 默认会把**连本机 8100 的请求也丢给代理** → 代理拒绝/中止连接
#   → WinError 10053 → 相机服务"不可达"、11 条 vision 用例整片红。
#   项目里早就把这条写进纪律（真机脚本必须禁代理），但生产代码这里一直没做 —— 现在补上。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def _request(method: str, path: str, body: Optional[dict] = None,
             timeout: Optional[float] = None, base: Optional[str] = None) -> Tuple[int, Any, Optional[str]]:
    """返回 (status, 解析后的内容, 错误信息)。

    `base` 用于"试连一个还没保存的地址"（设置页的连通性测试）：只对本次调用生效，
    绝不写进缓存 —— 试连失败的地址不该污染后续所有请求。

    ★ 一律不抛异常: 调用方(代理接口)只需要把错误原样透给前端,
      后端不该因为"相机服务没开"而返回 500 堆栈。
    """
    url = (str(base).rstrip("/") if base else base_url()) + path
    data = None
    headers = {"Accept": "*/*"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout or _timeout()) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type")
            if "application/json" in (ctype or ""):
                try:
                    return r.status, json.loads(raw.decode("utf-8")), None
                except Exception as e:
                    return r.status, None, "响应不是合法 JSON: %s" % e
            return r.status, raw, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
                msg = payload.get("error") or payload.get("message") or str(payload)[:120]
            except Exception:
                msg = raw[:120].decode("utf-8", "replace")
        except Exception:
            msg = "HTTP %s" % e.code
        return e.code, None, msg
    except Exception as e:
        return 0, None, "无法连接相机服务(%s): %s" % (url.rsplit("/", 1)[0], e)


def get_json(path: str, timeout: Optional[float] = None,
             base: Optional[str] = None) -> Tuple[int, Any, Optional[str]]:
    return _request("GET", path, None, timeout, base)


def post_json(path: str, body: Optional[dict] = None,
              timeout: Optional[float] = None,
              base: Optional[str] = None) -> Tuple[int, Any, Optional[str]]:
    return _request("POST", path, body if body is not None else {}, timeout, base)


def get_binary(path: str, timeout: Optional[float] = None,
               base: Optional[str] = None) -> Tuple[int, Any, Optional[str]]:
    return _request("GET", path, None, timeout, base)


def alive() -> bool:
    """轻量存活探测: 相机服务在不在（/vision/status 已支持返回 supported）。"""
    st, _, err = get_json("/vision/status", timeout=1.5)
    return st == 200 and err is None
