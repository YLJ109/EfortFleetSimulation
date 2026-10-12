# -*- coding: utf-8 -*-
"""控制权限时长（TTL）语义回归。

阶段 7 需求：
- 获取管理权限默认**不限时**（TTL=0），直到后端重启 / 主动登出；
- 时长可在设置页由管理员调整（POST /api/settings/control-ttl），并持久化到 .env；
- 调整**不追溯**：已签发的令牌保持签发时的有效期；
- 无论设多长，令牌都只存在于后端进程内存 —— 重启后端必须重新获取权限
  （令牌存 `_TOKENS` 字典，进程没了自然清空，这是结构保证，无需测试）。
"""
from __future__ import annotations

import time

import pytest

from app.api import auth as auth_mod

PASSWORD = "test1234"


@pytest.fixture(autouse=True)
def _reset_ttl_state():
    """模块级全局状态（_TTL_SEC 与 _TOKENS）在测试间复位（坑 24）。

    conftest 的 client 是 session 级的，这里把时长钉回 0（不限时），
    避免本文件把 TTL 改成 60 秒后泄漏给别的测试文件。
    """
    auth_mod.set_ttl(0)
    auth_mod._TOKENS.clear()
    yield
    auth_mod.set_ttl(0)
    auth_mod._TOKENS.clear()


def _login(client):
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- 单元：时长 setter ----------------

def test_set_ttl_rejects_negative():
    with pytest.raises(ValueError):
        auth_mod.set_ttl(-1)


def test_set_ttl_caps_huge_values():
    assert auth_mod.set_ttl(10**9) == auth_mod.TTL_MAX
    auth_mod.set_ttl(0)


def test_set_ttl_zero_is_unlimited():
    assert auth_mod.set_ttl(0) == 0
    assert auth_mod.get_ttl() == 0


# ---------------- 单元：签发与校验 ----------------

def test_default_ttl_zero_issues_never_expiring_token():
    auth_mod.set_ttl(0)
    out = auth_mod.issue_token()
    assert out["expires_at"] is None
    assert out["ttl"] == 0
    assert auth_mod.token_role(out["token"]) == "admin"      # 永不因时间过期


def test_limited_ttl_issues_expiring_token():
    auth_mod.set_ttl(60)
    out = auth_mod.issue_token()
    assert out["expires_at"] is not None
    assert abs(out["expires_at"] - (time.time() + 60)) < 5
    assert auth_mod.token_role(out["token"]) == "admin"


def test_ttl_change_is_not_retroactive():
    """改时长不追溯：永久令牌在改成 60 秒后依然有效。"""
    auth_mod.set_ttl(0)
    permanent = auth_mod.issue_token()["token"]
    auth_mod.set_ttl(60)
    assert auth_mod.token_role(permanent) == "admin"


# ---------------- API：/auth/status 与 /settings/control-ttl ----------------

def test_status_reports_ttl(client):
    body = client.get("/api/auth/status").json()
    assert body["ttl"] == 0                      # autouse 夹具钉回 0
    assert body["control_active"] is False


def test_login_unlimited_returns_null_expires_at(client):
    d = _login(client)
    assert d["ttl"] == 0
    assert d["expires_at"] is None


def test_control_ttl_requires_admin(client):
    r = client.post("/api/settings/control-ttl", json={"ttl_sec": 1800})
    assert r.status_code == 401                  # 没令牌不给改


def test_control_ttl_updates_and_affects_new_tokens_only(client, _isolate_env_file):
    """★★ 改 TTL 要持久化，但**绝不能写真实的 .env** ★★

    这个用例历史上就是"设置被莫名改回 1800"的元凶：它调的接口会回写项目根 `.env`，
    而测试没做隔离 → 跑一次测试就把操作员的设置踩掉，重启后回到 30 分钟。
    现在由 conftest 的 `_isolate_env_file` 单点收口，这里显式断言"写的是隔离文件"。
    """
    d = _login(client)                           # 永久令牌
    old_token = d["token"]
    r = client.post("/api/settings/control-ttl",
                    json={"ttl_sec": 1800},
                    headers={"X-Control-Token": old_token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["ttl"] == 1800
    assert "新签发" in body["note"]
    # ★ 必须写在隔离文件里，且真实项目根 .env 不受影响
    assert body["persisted"] is True, body
    assert str(_isolate_env_file) in (body.get("path") or ""), (
        "TTL 写盘必须落在隔离文件里，实测 path=%r" % body.get("path"))
    assert _isolate_env_file.is_file() and "EFORT_CONTROL_TTL=1800" in \
        _isolate_env_file.read_text(encoding="utf-8")

    # 旧永久令牌不被追溯
    assert client.get("/api/auth/status",
                      headers={"X-Control-Token": old_token}).json()["control_active"]

    # 新签发的令牌按新时长（≈1800s）
    new = client.post("/api/auth/login", json={"password": PASSWORD}).json()
    assert new["ttl"] == 1800
    assert abs(new["expires_at"] - (time.time() + 1800)) < 5


def test_logout_still_works_for_permanent_token(client):
    tok = _login(client)["token"]
    r = client.post("/api/auth/logout", headers={"X-Control-Token": tok})
    assert r.status_code == 200
    assert auth_mod.token_role(tok) is None


# =====================================================================
# ★★ 2026-09-29：TTL 的"启动默认值"必须与现场口径一致，且改完要能落盘
#   事故背景：.env 里长期写着 EFORT_CONTROL_TTL=1800 →
#     操作员在设置页改成 2 小时，一重启后端又回到 30 分钟
#     （启动只读 .env 那一行；设置页的改动虽然会回写，但被旧值挡着）。
# =====================================================================




def test_env_default_ttl_is_two_hours(monkeypatch):
    """没有 EFORT_CONTROL_TTL 时，代码默认必须是 **7200 = 2 小时**（现场口径）。"""
    monkeypatch.delenv("EFORT_CONTROL_TTL", raising=False)
    assert auth_mod._env_ttl() == 7200, (
        "代码默认必须是 7200（2 小时）；改这块前先确认现场口径，别改回 30 分钟")
    # 垃圾值/负数也不该把后端起不来，且仍回落到 2 小时
    monkeypatch.setenv("EFORT_CONTROL_TTL", "abc")
    assert auth_mod._env_ttl() == 7200
    monkeypatch.setenv("EFORT_CONTROL_TTL", "-1")
    assert auth_mod._env_ttl() == 0            # 负值归 0 = 不限时（原设计）
    monkeypatch.setenv("EFORT_CONTROL_TTL", "0")
    assert auth_mod._env_ttl() == 0            # 0 = 不限时
    monkeypatch.setenv("EFORT_CONTROL_TTL", "7200")
    assert auth_mod._env_ttl() == 7200


def test_env_example_ships_two_hours():
    """★ `.env.example` 是**新部署的起点**：它写 1800 就会让新现场一路回到 30 分钟。

    直接读文件断言，防止有人在模板里改回去（本仓 .env 不被 git 跟踪，
    只有 .env.example 能作为"出厂默认"的证据）。
    """
    import os
    import re as _re
    from app.core.config import project_root

    p = os.path.join(project_root(), ".env.example")
    assert os.path.isfile(p), "缺少 .env.example（新部署的默认值来源）"
    with open(p, "r", encoding="utf-8") as f:
        txt = f.read()
    m = _re.search(r"^\s*EFORT_CONTROL_TTL\s*=\s*(\d+)", txt, _re.M)
    assert m, ".env.example 里缺少 EFORT_CONTROL_TTL"
    assert m.group(1) == "7200", (
        ".env.example 的 EFORT_CONTROL_TTL 必须是 7200（2 小时），实测=%s" % m.group(1))


def test_control_ttl_endpoint_persists_to_env(client, monkeypatch, tmp_path):
    """★ 设置页改 TTL 必须**真的落盘**（否则重启就回退，用户会以为设置没用）。

    这里把 .env 路径指到临时文件，验证：① 接口写盘成功；② 写的是同一行（不追加重复键，
    不抹掉其它行）；③ 回报 persisted=true。
    """
    import app.api.settings as S

    env = tmp_path / ".env"
    env.write_text("# 注释要保留\nEFORT_OTHER=1\nEFORT_CONTROL_TTL=1800\n", encoding="utf-8")
    monkeypatch.setattr(S, "_env_path", lambda: str(env), raising=True)

    tok = _login(client)["token"]
    r = client.post("/api/settings/control-ttl", json={"ttl_sec": 7200},
                    headers={"X-Control-Token": tok})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ttl"] == 7200 and body["persisted"] is True, body

    txt = env.read_text(encoding="utf-8")
    assert "EFORT_CONTROL_TTL=7200" in txt
    assert txt.count("EFORT_CONTROL_TTL") == 1, "不能追加重复键：%r" % txt
    assert "# 注释要保留" in txt and "EFORT_OTHER=1" in txt, "不能抹掉其它行：%r" % txt


def test_suite_never_touches_the_real_env_file(client, _isolate_env_file):
    """★★★ 守卫：跑测试**绝不能**改动项目根的 `.env`（真实运维配置）★★★

    这两个接口都会写 .env（本意是持久化）：
      · POST /api/settings/control-ttl → EFORT_CONTROL_TTL
      · POST /api/settings/password    → EFORT_ADMIN_PASSWORD
    历史上前者没被隔离，导致"任何人跑一次测试，操作员的 2 小时设置就回到 30 分钟"，
    用户反复设置都无效（后端启动只读 .env）。
    本用例把两个接口都打一遍，然后断言真实 .env **逐字节不变**。
    """
    import os
    from app.core.config import project_root

    real = os.path.join(project_root(), ".env")
    before = None
    if os.path.isfile(real):
        with open(real, "rb") as f:
            before = f.read()

    tok = _login(client)["token"]
    h = {"X-Control-Token": tok}
    client.post("/api/settings/control-ttl", json={"ttl_sec": 28800}, headers=h)
    # 口令接口：先给正确当前口令，确保真的走到写盘那一步
    client.post("/api/settings/password",
                json={"role": "admin", "current": PASSWORD, "password": PASSWORD},
                headers=h)

    if before is None:
        assert not os.path.isfile(real), "测试凭空创建了真实 .env —— 必须被隔离"
        return
    with open(real, "rb") as f:
        after = f.read()
    assert after == before, (
        "测试改动了真实项目根 .env！写 .env 的路径必须被 conftest._isolate_env_file 隔离。"
        "（历史上正是这里把 EFORT_CONTROL_TTL 踩回 1800，导致设置页改完重启失效）")
