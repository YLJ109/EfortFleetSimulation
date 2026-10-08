# -*- coding: utf-8 -*-
"""实时语音字幕（腾讯云 ASR）回归测试。

覆盖：
  1. 签名：待签名串格式（host+path?按 key 升序、值不做 urlencode）+ HMAC-SHA1 正确性
  2. 配置：未启用 / 缺密钥 / 齐备 三种状态，且**绝不回传密钥内容**
  3. 结果解析：slice_type 0/1/2、final、错误码、脏数据不炸
  4. HTTP 接口：/api/asr/config 可读且不含密钥片段

★ 全程不联网：签名与解析都是纯函数；不触发任何云端调用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

import pytest

from app.services import asr_tencent as A


# ---------------------------------------------------------------- 1. 签名
def test_sign_str_format_and_signature():
    """待签名串 = host+path + '?' + 按 key 升序的 k=v（值不做 urlencode）。"""
    url, params = A.build_url("1234567890", "AKIDxxxx", "SECRETKEY", engine="16k_zh",
                              voice_id="vid-fixed", now=1700000000.0, nonce=42)
    assert url.startswith("wss://asr.cloud.tencent.com/asr/v2/1234567890?")
    assert "signature=" in url

    # 1) 关键字段原样落地
    assert params["secretid"] == "AKIDxxxx"
    assert params["engine_model_type"] == "16k_zh"
    assert params["voice_format"] == 1          # PCM
    assert params["needvad"] == 1
    assert params["voice_id"] == "vid-fixed"
    assert params["timestamp"] == 1700000000
    assert params["expired"] == 1700000000 + 86400
    assert params["nonce"] == 42

    # 2) 待签名串必须按 key 升序、且**不含 signature 自身**
    expect_sign_str = ("asr.cloud.tencent.com/asr/v2/1234567890?"
                       + "&".join("%s=%s" % (k, params[k])
                                  for k in sorted(params) if k != "signature"))
    expect_sig = base64.b64encode(
        hmac.new(b"SECRETKEY", expect_sign_str.encode("utf-8"),
                 hashlib.sha1).digest()).decode("utf-8")
    assert params["signature"] == expect_sig, "签名与手工计算结果不一致"
    # 顺序确实是升序（用第一个键名验证：engine_model_type 应排在最前）
    sorted_keys = [k for k in sorted(params) if k != "signature"]
    assert sorted_keys[0] == "engine_model_type"


def test_signature_changes_with_secret_and_params():
    """换密钥 / 换参数 → 签名必须变（防止把 signature 写死成常量）。"""
    u1, p1 = A.build_url("1", "sid", "KEY_A", now=1700000000.0, nonce=1)
    u2, p2 = A.build_url("1", "sid", "KEY_B", now=1700000000.0, nonce=1)
    u3, p3 = A.build_url("1", "sid", "KEY_A", now=1700000001.0, nonce=1)
    assert p1["signature"] != p2["signature"]
    assert p1["signature"] != p3["signature"]


def test_voice_id_is_unique_by_default():
    _, p1 = A.build_url("1", "sid", "KEY")
    _, p2 = A.build_url("1", "sid", "KEY")
    assert p1["voice_id"] != p2["voice_id"], "默认 voice_id 必须每次不同（否则云端串话）"


# ---------------------------------------------------------------- 2. 配置
def _clear_env(monkeypatch):
    for k in ("EFORT_ASR_PROVIDER", "EFORT_ASR_APPID", "EFORT_ASR_SECRET_ID",
              "EFORT_ASR_SECRET_KEY", "EFORT_ASR_ENGINE"):
        monkeypatch.delenv(k, raising=False)


def test_config_disabled_without_provider(monkeypatch):
    _clear_env(monkeypatch)
    c = A.asr_config()
    assert c["enabled"] is False and c["configured"] is False
    assert "EFORT_ASR_PROVIDER" in c["reason"]
    assert c["engine"] == "16k_zh"          # 默认引擎仍要能给出来


def test_config_reports_missing_credentials(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("EFORT_ASR_PROVIDER", "tencent")
    monkeypatch.setenv("EFORT_ASR_APPID", "1234567890")
    # 故意只给 AppID，缺 SecretId/SecretKey
    c = A.asr_config()
    assert c["configured"] is True and c["has_creds"] is False and c["enabled"] is False
    assert "EFORT_ASR_SECRET_ID" in c["reason"] and "EFORT_ASR_SECRET_KEY" in c["reason"]


def test_config_enabled_and_never_leaks_secret(monkeypatch):
    """★★ 密钥齐备时 enabled=True，但返回体里**不能出现任何密钥内容**。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("EFORT_ASR_PROVIDER", "tencent")
    monkeypatch.setenv("EFORT_ASR_APPID", "1234567890")
    monkeypatch.setenv("EFORT_ASR_SECRET_ID", "AKIDsecretidXXXX")
    monkeypatch.setenv("EFORT_ASR_SECRET_KEY", "Sup3rSecretKeyYYYY")
    monkeypatch.setenv("EFORT_ASR_ENGINE", "16k_zh_large")
    c = A.asr_config()
    assert c["enabled"] is True and c["has_creds"] is True
    assert c["engine"] == "16k_zh_large"
    blob = json.dumps(c, ensure_ascii=False)
    assert "Sup3rSecretKeyYYYY" not in blob, "密钥泄露到接口返回体！"
    assert "AKIDsecretidXXXX" not in blob, "SecretId 泄露到接口返回体！"
    assert "secret" not in blob.lower().replace("efort_asr_secret", ""), \
        "返回体不应包含任何密钥字段值"


def test_config_unknown_provider(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("EFORT_ASR_PROVIDER", "iflytek")
    c = A.asr_config()
    assert c["enabled"] is False and "iflytek" in c["reason"]


# ---------------------------------------------------------------- 3. 解析
@pytest.mark.parametrize("slice_type,text,final,expect_slice", [
    (0, "今天", 0, 0),
    (1, "今天天气", 0, 1),
    (2, "今天天气不错。", 0, 2),
])
def test_parse_result_slices(slice_type, text, final, expect_slice):
    raw = json.dumps({"code": 0, "message": "success", "final": final,
                      "result": {"slice_type": slice_type, "voice_text_str": text}})
    ev = A.parse_result(raw)
    assert ev["ok"] is True
    assert ev["text"] == text
    assert ev["slice"] == expect_slice
    assert ev["final"] is False


def test_parse_result_final_flag():
    raw = json.dumps({"code": 0, "final": 1, "result": {"slice_type": 2,
                                                        "voice_text_str": "结束"}})
    ev = A.parse_result(raw)
    assert ev["ok"] is True and ev["final"] is True and ev["text"] == "结束"


def test_parse_result_error_code():
    raw = json.dumps({"code": 4003, "message": "appid error"})
    ev = A.parse_result(raw)
    assert ev["ok"] is False and ev["code"] == 4003 and "appid" in ev["message"]


def test_parse_result_garbage_does_not_raise():
    for bad in ("", "not json", "[]", "null", '{"code":0}'):
        ev = A.parse_result(bad)          # 不能抛异常
        assert isinstance(ev, dict) and "ok" in ev


def test_frame_size():
    """40ms @16k 单声道 16bit = 1280 字节（给前端对齐用）。"""
    assert A.pcm_bytes_per_frame(40, 16000) == 1280
    assert A.pcm_bytes_per_frame(32, 16000) == 1024


# ---------------------------------------------------------------- 4. HTTP
def test_api_asr_config_endpoint(client, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("EFORT_ASR_PROVIDER", "tencent")
    monkeypatch.setenv("EFORT_ASR_APPID", "1234567890")
    monkeypatch.setenv("EFORT_ASR_SECRET_ID", "AKIDsec")
    monkeypatch.setenv("EFORT_ASR_SECRET_KEY", "KEYKEYKEY")
    r = client.get("/api/asr/config")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["enabled"] is True and d["sample_rate"] == 16000
    txt = r.text
    assert "KEYKEYKEY" not in txt and "AKIDsec" not in txt


def test_asr_module_has_no_robot_control_imports():
    """★ 安全红线：ASR 模块不得引用任何控制/运动链路。

    识别文本只用于显示 —— 一旦有人把 motion/jog/control 引进来做"语音控机器人"，
    这条会立刻报红。
    """
    import inspect
    import app.api.asr as api_mod
    src = inspect.getsource(api_mod)
    for bad in ("motion", "jog", "guard_check", "rc_jog", "estop"):
        assert not re.search(r"\b%s\b" % bad, src), "ASR 模块引用了控制链路：%s" % bad
