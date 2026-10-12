# -*- coding: utf-8 -*-
"""实时语音字幕（腾讯云 ASR）接口层。

  GET  /api/asr/config   功能是否可用（**不回传任何密钥片段**）
  WS   /ws/asr           浏览器麦克风音频上行 → 云端识别 → 字幕下行

★ 安全红线：
  1) 密钥只在本进程读 .env，绝不下发浏览器（本项目已因硬编码口令踩过坑）；
  2) 识别文本**只用于显示**，不解析成任何指令、不参与机器人控制；
  3) WS 需要**控制令牌**（带内握手，令牌不进查询串/访问日志）；
  4) 单会话 + 最长时长上限：避免多个标签页/忘记关闭把云端额度烧掉。
"""
from __future__ import annotations

import asyncio
import json
import os

import websockets
from fastapi import APIRouter, WebSocket

from app.api.ws import _deny, _origin_ok, _wait_token
from app.core.logger import get_logger
from app.services.asr_tencent import asr_config, build_url, error_hint, parse_result
from app.services.events import emit as emit_event

log = get_logger("asr")

router = APIRouter(prefix="/api/asr", tags=["asr"])          # 普通 HTTP
ws_router = APIRouter(tags=["ws"])                           # WebSocket（无前缀）

# 同时只允许一路识别（进程内单例）。多标签页同时开＝重复计费 + 麦克风抢占。
_sessions = {"n": 0}


def _max_sec() -> float:
    """单次识别时长上限（秒）。腾讯云按秒计费，防止"开着忘关"跑一天。"""
    try:
        return max(30.0, float(os.environ.get("EFORT_ASR_MAX_SEC", "600") or 600))
    except (TypeError, ValueError):
        return 600.0


@router.get("/config")
def api_asr_config():
    """语音字幕功能状态。★ 只回 enabled/engine/有没有密钥，绝不回密钥内容。"""
    return asr_config()


async def _send(ws: WebSocket, obj: dict) -> None:
    try:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))
    except Exception:
        pass


async def _pump_up(ws: WebSocket, cloud) -> None:
    """浏览器 → 云端：二进制音频帧直通；收到 {"type":"stop"} 发 end 收尾。"""
    while True:
        try:
            msg = await ws.receive()
        except Exception:
            break
        if msg.get("type") == "websocket.disconnect":
            break
        data = msg.get("bytes")
        if data:
            await cloud.send(data)          # 16k/16bit/单声道 PCM，直通
            continue
        text = msg.get("text")
        if not text:
            continue
        try:
            d = json.loads(text)
        except Exception:
            continue
        if d.get("type") == "stop":
            try:
                await cloud.send(json.dumps({"type": "end"}))
            except Exception:
                pass
            break
    # 浏览器断了也要让云端收尾（否则云端会一直等音频）
    try:
        await cloud.send(json.dumps({"type": "end"}))
    except Exception:
        pass


async def _pump_down(ws: WebSocket, cloud) -> None:
    """云端 → 浏览器：把 slice_type/final 归一化成字幕事件。

    ★ 云端返回非 0 错误码时**必须把原因原样送到界面并结束会话** ——
      这类错误（如 4003 账号未开通）云端会立刻断链，如果这里只是 continue，
      前端最终只看到"连接已断开"，用户完全不知道要去开通服务。
    """
    async for raw in cloud:
        if isinstance(raw, (bytes, bytearray)):
            continue
        ev = parse_result(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
        if not ev["ok"]:
            msg = error_hint(ev["code"], ev["message"])
            await _send(ws, {"type": "error", "code": ev["code"], "message": msg})
            emit_event("asr", "warn", "asr.cloud_error",
                       "语音识别云端错误：%s" % msg,
                       {"code": ev["code"], "cloud_message": ev["message"]})
            return          # 非 0 码视为会话终止，别让界面只看到"连接已断开"
        if not ev["text"] and ev["slice"] < 0:
            continue                        # 心跳/状态帧，不产生字幕
        await _send(ws, {"type": "result", "text": ev["text"],
                         "slice": ev["slice"], "final": ev["final"]})
        if ev["final"]:
            break


@ws_router.websocket("/ws/asr")
async def ws_asr(ws: WebSocket):
    """实时语音识别代理。

    协议：
      ① 客户端首帧：{"type":"auth","token":"<控制令牌>"}
      ② 服务端：{"type":"ready","engine":"16k_zh","sample_rate":16000}
      ③ 客户端：二进制 PCM 帧（16k/16bit/单声道，建议 40ms=1280 字节）
      ④ 服务端：{"type":"result","text":"…","slice":1,"final":false}
                 slice 0=整句开始 1=中间 2=整句结束
      ⑤ 客户端停止：{"type":"stop"}；错误：{"type":"error","message":"…"}
    """
    if not _origin_ok(ws):
        await _deny(ws, 1008, "origin not allowed")
        return
    await ws.accept()

    tok = await _wait_token(ws)
    if not tok:
        await _deny(ws, 4401, "control token required")
        return
    from app.api.auth import valid_token
    if not valid_token(tok):
        await _deny(ws, 4401, "control token required")
        return

    cfg = asr_config()
    if not cfg["configured"]:
        await _send(ws, {"type": "error", "message": cfg["reason"]})
        await _deny(ws, 4400, "asr disabled")
        return
    if not cfg["has_creds"]:
        await _send(ws, {"type": "error", "message": cfg["reason"]})
        await _deny(ws, 4400, "asr credential missing")
        return
    if _sessions["n"] > 0:
        await _send(ws, {"type": "error",
                         "message": "已有语音识别在进行（同一时间只允许一路，避免重复计费）"})
        await _deny(ws, 4429, "asr busy")
        return

    appid = os.environ.get("EFORT_ASR_APPID", "").strip()
    sid = os.environ.get("EFORT_ASR_SECRET_ID", "").strip()
    skey = os.environ.get("EFORT_ASR_SECRET_KEY", "").strip()
    url, params = build_url(appid, sid, skey, engine=cfg["engine"])
    voice_id = params.get("voice_id")

    _sessions["n"] += 1
    emit_event("asr", "info", "asr.session_start",
               "语音识别会话开始（引擎 %s）" % cfg["engine"],
               {"engine": cfg["engine"]})
    try:
        async with websockets.connect(url, max_size=None, ping_interval=20,
                                      close_timeout=3) as cloud:
            await _send(ws, {"type": "ready", "engine": cfg["engine"],
                             "sample_rate": cfg["sample_rate"],
                             "voice_id": voice_id})
            up = asyncio.create_task(_pump_up(ws, cloud))
            down = asyncio.create_task(_pump_down(ws, cloud))
            limit = asyncio.create_task(asyncio.sleep(_max_sec()))
            done, pending = await asyncio.wait({up, down, limit},
                                               return_when=asyncio.FIRST_COMPLETED)
            if limit in done:
                await _send(ws, {"type": "error",
                                 "message": "已达单次识别上限 %.0f 分钟，已自动停止"
                                            % (_max_sec() / 60.0)})
            # ★★ 收尾宽限：用户按「停止」时 `_pump_up` 会立刻结束（它只是发了 end），
            #    但云端随后还会回最后一帧（slice=2 整句结束 + final）。
            #    若此时立刻 cancel(down)，**用户按停止前那句就永远定不了稿** ——
            #    界面上一直停在灰色"正在说"。所以给 down 最多 2 秒把尾帧收完。
            if down in pending and up in done:
                try:
                    await asyncio.wait_for(down, timeout=2.0)
                    pending.discard(down)
                except Exception:
                    pass
            for t in pending:
                t.cancel()
    except Exception as e:  # noqa: BLE001
        log.warning("语音识别云端连接失败: %s", e)
        await _send(ws, {"type": "error",
                         "message": "连接腾讯云失败：%s" % str(e)[:160]})
        emit_event("asr", "warn", "asr.session_error", "语音识别连接失败：%s" % str(e)[:120], {})
    finally:
        _sessions["n"] = max(0, _sessions["n"] - 1)
        emit_event("asr", "info", "asr.session_stop", "语音识别会话结束", {})
        try:
            await ws.close()
        except Exception:
            pass
