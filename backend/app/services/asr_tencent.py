# -*- coding: utf-8 -*-
"""腾讯云「实时语音识别」客户端支撑（签名 / 结果解析 / 配置）。

设计要点（见 docs/方案-实时语音字幕（腾讯云ASR）.md）：
  · 只做**纯函数**：签名与结果解析不碰网络 → 可单测、可离线验签；
    真正的 WebSocket 双向转发放在 api/asr.py（那里才有 async 上下文）。
  · 密钥只在后端读 .env，**绝不下发浏览器**。
  · 本模块与机器人控制**完全无关**：识别文本不解析成任何指令。

腾讯云协议（asr/v2）：
  待签名串 = "asr.cloud.tencent.com/asr/v2/<appid>?" + 按 key 升序的 k=v 以 & 连接
  signature = Base64(HMAC-SHA1(SecretKey, 待签名串))
  ★ 参与签名的值**不做 urlencode**（这是最容易搞错的一点）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import time
import uuid
from typing import Dict, Optional, Tuple

# 云端 WebSocket 端点（host + path 前缀，签名串要用同一串）
ASR_HOST_PATH = "asr.cloud.tencent.com/asr/v2/%s"
ASR_URL = "wss://" + ASR_HOST_PATH
# 音频格式：1 = PCM（16bit / 单声道）；采样率由 engine_model_type 决定（16k_zh → 16kHz）
VOICE_FORMAT_PCM = 1

# 引擎可选值（前端只用于展示，实际以 .env 为准）
ENGINES = ("16k_zh", "16k_zh_large", "16k_en", "16k_zh_PY", "16k_zh_dialect")


def asr_config() -> Dict:
    """读取 .env 里的 ASR 配置。

    ★ 返回值**绝不含** SecretId / SecretKey 内容，只回"有没有配"。
      密钥一旦回传前端就等于公开 —— 本项目已因硬编码口令踩过一次坑。
    """
    provider = (os.environ.get("EFORT_ASR_PROVIDER", "") or "").strip().lower()
    appid = (os.environ.get("EFORT_ASR_APPID", "") or "").strip()
    sid = (os.environ.get("EFORT_ASR_SECRET_ID", "") or "").strip()
    skey = (os.environ.get("EFORT_ASR_SECRET_KEY", "") or "").strip()
    engine = (os.environ.get("EFORT_ASR_ENGINE", "") or "16k_zh").strip()

    enabled = provider == "tencent"
    has_creds = bool(appid and sid and skey)
    reason = ""
    if not provider:
        reason = "未启用语音识别（在 .env 里设 EFORT_ASR_PROVIDER=tencent）"
    elif provider != "tencent":
        reason = "暂不支持的服务商：%s（当前仅 tencent）" % provider
    elif not has_creds:
        missing = [k for k, v in (("EFORT_ASR_APPID", appid), ("EFORT_ASR_SECRET_ID", sid),
                                  ("EFORT_ASR_SECRET_KEY", skey)) if not v]
        reason = "缺少腾讯云密钥：%s（写入项目根 .env，勿提交）" % "、".join(missing)
    return {
        "provider": provider or "",
        "enabled": bool(enabled and has_creds),
        "configured": bool(enabled),      # 服务商已选，但密钥可能还没填
        "has_creds": has_creds,
        "engine": engine,
        "engines": list(ENGINES),
        "host": "asr.cloud.tencent.com",
        "sample_rate": 16000,
        "reason": reason,
    }


def sign_params(secret_key: str, sign_str: str) -> str:
    """腾讯云签名：Base64(HMAC-SHA1(SecretKey, 待签名串))。"""
    return base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), sign_str.encode("utf-8"),
                 hashlib.sha1).digest()).decode("utf-8")


def build_url(appid: str, secret_id: str, secret_key: str, engine: str = "16k_zh",
              voice_id: Optional[str] = None, expired_sec: int = 86400,
              now: Optional[float] = None, nonce: Optional[int] = None,
              need_vad: int = 1) -> Tuple[str, Dict]:
    """构造带签名的 wss 地址 → (url, params)。

    参数 now/nonce 可显式传入 → 单测里能得到**确定性**结果。
    """
    ts = int(now if now is not None else time.time())
    params: Dict[str, object] = {
        "secretid": secret_id,
        "timestamp": ts,
        "expired": ts + int(expired_sec),
        "nonce": int(nonce if nonce is not None else random.randint(1, 1000000000)),
        "engine_model_type": engine or "16k_zh",
        "voice_id": voice_id or str(uuid.uuid4()),
        "voice_format": VOICE_FORMAT_PCM,
        "needvad": int(need_vad),
    }
    # ★ 待签名串：host+path + "?" + 按 key 升序（值不做 urlencode）
    sign_str = (ASR_HOST_PATH % appid) + "?" + "&".join(
        "%s=%s" % (k, params[k]) for k in sorted(params))
    params["signature"] = sign_params(secret_key, sign_str)
    query = "&".join("%s=%s" % (k, params[k]) for k in sorted(params))
    return (ASR_URL % appid) + "?" + query, params


def parse_result(raw: str) -> Dict:
    """解析云端下行一帧 → 归一化事件。

    返回 {"ok": bool, "text": str, "slice": int, "final": bool,
          "code": int, "message": str}
      slice: 0=整句开始 / 1=中间（部分结果，边说边出） / 2=整句结束
             -1 = 这一帧没有文本（心跳/状态）
      final: 1 = 整段会话结束
    下游（api/asr.py）只认这个结构，腾讯云改字段也不会波及其他代码。
    """
    try:
        d = json.loads(raw)
    except Exception:
        return {"ok": False, "text": "", "slice": -1, "final": False,
                "code": -1, "message": "无法解析云端返回"}
    if not isinstance(d, dict):
        return {"ok": False, "text": "", "slice": -1, "final": False,
                "code": -1, "message": "云端返回不是对象"}

    code = int(d.get("code") or 0)
    msg = str(d.get("message") or "")
    final = bool(d.get("final") or 0)
    res = d.get("result") if isinstance(d.get("result"), dict) else {}
    text = str(res.get("voice_text_str") or "")
    st = res.get("slice_type")
    try:
        st = int(st)
    except (TypeError, ValueError):
        st = -1
    ok = (code == 0)
    return {"ok": ok, "text": text, "slice": st, "final": final,
            "code": code, "message": msg}


# 腾讯云实时语音识别错误码 → 现场能直接照做的人话。
# ★ 为什么必须有这张表（2026-10-08 现场）：账号没开通服务时云端回
#   {"code":4003,"message":"账号未开通本服务，请在控制台开通服务"} 并**立刻断链**，
#   而这个原因被吞掉后，界面只显示"语音识别连接已断开" —— 用户根本无从下手。
ERROR_HINTS = {
    4001: "参数不合法（检查引擎/采样率等参数）",
    4002: "鉴权失败：SecretId / SecretKey 或签名不正确",
    4003: "★ 账号未开通「实时语音识别」服务 —— 请到腾讯云控制台开通该服务后重试",
    4004: "资源包已用尽或计费未就绪（检查腾讯云余额/资源包）",
    4005: "请求过于频繁，请稍后重试",
    4006: "腾讯云服务内部错误，请稍后重试",
    4007: "音频格式不匹配（需 16k / 16bit / 单声道 PCM）",
    4008: "音频数据错误",
    4009: "静音超时（长时间无语音，云端已收尾）",
    4010: "音频时长超限",
    4011: "并发超限（同一账号同时识别路数过多）",
    4012: "音频解码失败（采样率/位深不符）",
}


def error_hint(code: int, message: str = "", default: str = "云端返回错误") -> str:
    """错误码 → 可照做的人话；表里没有就退回云端原文，最后才是通用文案。"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        c = -1
    hint = ERROR_HINTS.get(c)
    if hint:
        return "%s（code %d）" % (hint, c)
    if message:
        return "%s（code %d）" % (message, c)
    return "%s（code %d）" % (default, c)


def pcm_bytes_per_frame(ms: int = 40, sample_rate: int = 16000) -> int:
    """一帧音频应有字节数（16bit 单声道）。40ms @16k = 1280 字节。"""
    return int(sample_rate * (ms / 1000.0)) * 2
