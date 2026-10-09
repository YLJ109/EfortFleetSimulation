# -*- coding: utf-8 -*-
"""/api/ai/chat 对话端点回归（2026-10-10，用户口径"能正常聊天的大模型"）。

钉住四条口径：
  1. **不是权限后门**：对话同样要控制令牌；
  2. **指令先行**：规则能解析的话必须出计划卡（mode=plan + plan_id），
     与 /parse 完全同一份"回显 + 确认"链路 —— 闲聊人格绝不替代执行确认；
  3. **聊天只许说话**：非指令才回 reply；返回体不带 plan（聊天无权碰控制）；
  4. **降级要诚实**：没 key 给固定引导话术；key 在但大模型挂了如实说"暂时不可用"，
     不装死、不编话。

运行： cd backend && .venv/Scripts/python.exe -m pytest tests/test_ai_chat.py -q
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.services import llm


def _token(c: TestClient) -> str:
    r = c.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_chat_requires_token(client: TestClient):
    """AI 不是权限后门：对话接口裸调必须 401。"""
    assert client.post("/api/ai/chat", json={"text": "你好"}).status_code == 401


def test_chat_rule_command_returns_plan(client: TestClient, monkeypatch):
    """规则命中的指令 → mode=plan + plan_id（与 /parse 同一份口径）。"""
    monkeypatch.delenv("EFORT_LLM_API_KEY", raising=False)
    h = {"X-Control-Token": _token(client)}
    r = client.post("/api/ai/chat",
                    json={"text": "把A区第1行第1列放到B区红列"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["mode"] == "plan"
    assert d["parsed"]["ok"] is True and d["parsed"]["plan_id"]
    assert d["parsed"]["plan"]["action"] == "move_one"


def test_chat_falls_back_without_key(client: TestClient, monkeypatch):
    """没 key：闲聊给固定引导话术（mode=chat），不是 500、也不假装是大模型说的。"""
    monkeypatch.delenv("EFORT_LLM_API_KEY", raising=False)
    h = {"X-Control-Token": _token(client)}
    r = client.post("/api/ai/chat", json={"text": "今天天气怎么样"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["mode"] == "chat"
    assert "EFORT_LLM_API_KEY" in d["reply"]
    assert d["source"] == "rule"
    assert "plan" not in d            # 闲聊返回体绝不带计划


def test_chat_llm_free_talk(client: TestClient, monkeypatch):
    """大模型在线：闲聊走对话人格；返回体**必须不带 plan**（聊天无权碰控制）。"""
    monkeypatch.setenv("EFORT_LLM_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "parse",
                        lambda text, timeout=None: {"ok": False, "error": "不是指令"})
    monkeypatch.setattr(llm, "chat",
                        lambda text, history=None, timeout=None:
                        {"ok": True, "reply": "你好呀，我是小埃。"})
    h = {"X-Control-Token": _token(client)}
    r = client.post("/api/ai/chat",
                    json={"text": "你好",
                          "history": [{"role": "user", "content": "在吗"}]}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "chat" and d["reply"] == "你好呀，我是小埃。"
    assert d["source"] == "llm" and "plan" not in d


def test_chat_llm_down_is_honest(client: TestClient, monkeypatch):
    """key 在但大模型挂了：如实说"暂时不可用"并给指令引导 —— 不装死。"""
    monkeypatch.setenv("EFORT_LLM_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "parse",
                        lambda text, timeout=None: {"ok": False, "error": "HTTP 503"})
    monkeypatch.setattr(llm, "chat",
                        lambda text, history=None, timeout=None:
                        {"ok": False, "error": "HTTP 503"})
    h = {"X-Control-Token": _token(client)}
    r = client.post("/api/ai/chat", json={"text": "在吗"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["mode"] == "chat"
    assert "暂时不可用" in d["reply"] and "HTTP 503" in d["reply"]
    assert d["source"] == "fallback"


def test_chat_empty_text_rejected(client: TestClient):
    """空话直接 422（pydantic min_length 兜底，别浪费一次大模型调用）。"""
    h = {"X-Control-Token": _token(client)}
    assert client.post("/api/ai/chat", json={"text": "   "}, headers=h).status_code == 422
