# -*- coding: utf-8 -*-
"""系统设置 API 回归（阶段 6）。

覆盖的是 `app/api/settings.py` + `app/core/app_settings.py` —— 这一层做的事都很"安静"：
写一个 JSON 覆盖层、清几个缓存、删一个文件。出错的后果却都不安静：
  - 权限判漏了 → 操作员能改真实下发开关；
  - 合并语义错了 → 改一个关节限位把整段配置替换掉；
  - 覆盖层不再稀疏 → 以后没法判断"哪一项被改过"；
  - 重置没拦住 → 现场配置被一键清空。

## ★★ 覆盖层落在哪儿：测试写临时文件，**绝不碰生产 config/app_settings.json**
2026-10-13 之前：`app_settings.SETTINGS_PATH` 是硬编码生产路径的模块级常量，本文件的写操作
会真的落到 `config/app_settings.json`，所以这里必须有 autouse 的 `_keep_overlay`
（测试前备份、测试后原样还原）。那是"自我修复"式的保护 —— 真写错了也会被还原掉，没人知道。

现在：`SETTINGS_PATH` 可由环境变量 `EFORT_SETTINGS_PATH` 覆盖，`tests/conftest.py` 在
import app 之前把它指到临时目录，于是本文件的写操作全部落在临时文件上；
保护生产文件的职责移到 conftest 的 `_production_overlay_untouched`
（**比对内容、变了就红**，不还原、不吞事故），路径解析本身由本文件末尾的
`test_settings_path_*` 钉住。默认行为（不设环境变量）仍是生产路径，一条都没变。
"""
from __future__ import annotations

import json
import os

import pytest

from app.api import auth as auth_mod
from app.core import app_settings as st


@pytest.fixture(autouse=True)
def _clean_overlay():
    """每个用例从"没有覆盖层"开始，避免用例之间互相串味。

    ★ 清的是 `st.SETTINGS_PATH`（测试会话里 = 临时目录），生产覆盖层由 conftest 的
    `_production_overlay_untouched` 守着，本夹具**不会**去删它。
    """
    path = st.SETTINGS_PATH
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
    try:
        from app.core.config import reload_config
        reload_config()
    except Exception:
        pass
    yield


def _admin(client):
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


@pytest.fixture
def op_token():
    """临时开一个操作员口令，用完恢复原状（None = 禁用）。"""
    prev = auth_mod._ROLE_HASH.get(auth_mod.ROLE_OPERATOR)
    auth_mod.set_password(auth_mod.ROLE_OPERATOR, "op123456")
    yield "op123456"
    auth_mod._ROLE_HASH[auth_mod.ROLE_OPERATOR] = prev


def _operator(client, op_token):
    r = client.post("/api/auth/login", json={"password": op_token})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("role") == "operator", d
    return {"X-Control-Token": d["token"]}


# ---------------------------------------------------------------------------
# 读：公开
# ---------------------------------------------------------------------------
def test_describe_is_public(client):
    """读接口必须公开：现场排查时"现在到底生效的是哪个值"要随时能看到。"""
    r = client.get("/api/settings")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["role"] == ""                  # 没带令牌 → 不知道来者是谁
    assert len(d["groups"]) >= 8, "字段分组太少，说明 SCHEMA 没被完整返回"
    assert "runtime" in d and "overlay_path" in d


def test_field_metadata_is_complete(client):
    """每个字段都要带齐前端渲染需要的元数据 —— 缺一项界面上就是一个灰格子。"""
    groups = client.get("/api/settings").json()["groups"]
    required = ["key", "label", "type", "value", "admin", "apply", "editable", "source"]
    seen = set()
    for g in groups:
        assert g["id"] and g["label"]
        assert isinstance(g["fields"], list) and g["fields"]
        for f in g["fields"]:
            for k in required:
                assert k in f, f"{f.get('key')} 缺字段 {k}"
            assert f["type"] in ("str", "text", "ip", "int", "float", "bool",
                                 "enum", "signs6", "limits6", "host", "url", "ro"), f["type"]
            assert f["apply"] in ("live", "reload", "reconnect", "restart"), f["apply"]
            assert f["source"] in ("yaml", "settings", "env", "derived", "default"), f["source"]
            assert f["key"] not in seen, f"字段 {f['key']} 重复出现"
            seen.add(f["key"])
            # 只读项必须给出原因，否则用户只会看到"改不了"而不知道为什么
            if f["editable"] is False and f["source"] != "env":
                assert f["reason"], f"{f['key']} 只读但没有 reason"
    # 关键字段必须存在（改了会真正影响行为的那几个）
    for k in ("connection.host", "connection.port", "camera.host", "camera.port",
              "motion.real_write", "motion.mode_claim", "axis_sign", "joint_limits",
              "motion.jog.max_speed_dps", "server.port", "database.url"):
        assert k in seen, f"SCHEMA 里缺少关键字段 {k}"


def test_unknown_key_rejected(client):
    r = client.put("/api/settings", json={"values": {"no.such.key": 1}},
                   headers=_admin(client))
    assert r.status_code == 400
    assert "未知配置项" in (r.json().get("detail") or r.json().get("message") or "")


def test_readonly_field_rejected(client):
    """DH 参数是标定数据，界面上不允许改（改它没有意义，还要走标定流程）。"""
    r = client.put("/api/settings", json={"values": {"dh.calibration_pending": False}},
                   headers=_admin(client))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 写：权限
# ---------------------------------------------------------------------------
def test_put_requires_token(client):
    r = client.put("/api/settings", json={"values": {"robot.model": "X"}})
    assert r.status_code == 401


def test_operator_blocked_on_admin_field_and_nothing_written(client, op_token):
    """★ 整批拒绝：不能"能改的改了、不能改的静默丢掉"。"""
    h = _operator(client, op_token)
    r = client.put("/api/settings", json={"values": {
        "robot.model": "ER8-700H-TEST",      # 非管理员项
        "connection.host": "10.0.0.9",       # 管理员项
    }}, headers=h)
    assert r.status_code == 403
    msg = r.json().get("message") or r.json().get("detail") or ""
    assert "管理员" in msg
    # 关键：非管理员项也不能被写进去
    assert st.load_overlay().get("robot", {}).get("model") in (None, ""), \
        "整批拒绝失效：非管理员项被写进了覆盖层"


def test_operator_can_write_non_admin_field(client, op_token):
    h = _operator(client, op_token)
    r = client.put("/api/settings", json={"values": {"robot.model": "ER8-700H-OP"}}, headers=h)
    assert r.status_code == 200, r.text
    assert st.load_overlay().get("robot", {}).get("model") == "ER8-700H-OP"


# ---------------------------------------------------------------------------
# 写：校验
# ---------------------------------------------------------------------------
def test_validation_rejects_bad_values(client):
    h = _admin(client)
    cases = [
        ({"connection.host": "999.1.1.1"}, "IP"),
        ({"connection.port": 70000}, "端口"),
        ({"motion.mode_claim": "T3"}, "T1"),
        ({"motion.real_write": "maybe"}, "开/关"),
        ({"axis_sign": [1, 1, 1]}, "6 个数"),
        ({"axis_sign": [1, 1, 1, 1, 1, 2]}, "不能大于"),
        ({"axis_sign": [1, 1, 1, 1, 1, 0]}, "-1"),
        ({"robot.payload_kg": "abc"}, "数字"),
    ]
    for values, needle in cases:
        r = client.put("/api/settings", json={"values": values}, headers=h)
        assert r.status_code == 400, f"{values} 应该被拒，实际 {r.status_code}"
        msg = (r.json().get("detail") or r.json().get("message") or "")
        assert needle in msg, f"{values} 的报错没提 {needle}：{msg}"


def test_no_change_reports_empty(client):
    """提交与生效值相同的值 → changed 为空，并给出说明（不是静默成功）。"""
    cur = None
    for g in client.get("/api/settings").json()["groups"]:
        for f in g["fields"]:
            if f["key"] == "robot.model":
                cur = f["value"]
    r = client.put("/api/settings", json={"values": {"robot.model": cur}},
                   headers=_admin(client))
    assert r.status_code == 200
    d = r.json()
    assert d["changed"] == []
    assert d["note"]


# ---------------------------------------------------------------------------
# 覆盖层语义：稀疏 + 逐字段还原
# ---------------------------------------------------------------------------
def test_overlay_is_sparse_and_field_reset(client):
    h = _admin(client)
    # 改一个字段 → 进覆盖层
    r = client.put("/api/settings", json={"values": {"connection.timeout_s": 2.5}}, headers=h)
    assert r.status_code == 200
    assert r.json()["changed"][0]["key"] == "connection.timeout_s"
    assert st.load_overlay().get("connection", {}).get("timeout_s") == 2.5

    # 传 null = 恢复该项出厂值 → 从覆盖层移除（这才是"逐字段重置"）
    r2 = client.put("/api/settings", json={"values": {"connection.timeout_s": None}}, headers=h)
    assert r2.status_code == 200
    assert "connection" not in st.load_overlay() or \
        "timeout_s" not in st.load_overlay().get("connection", {}), \
        "恢复出厂值后该项仍留在覆盖层里 —— 覆盖层不再稀疏"


def test_saving_yaml_value_removes_it_from_overlay(client):
    """把值改回 yaml 原值 → 自动从覆盖层消失（不写冗余条目）。"""
    h = _admin(client)
    yaml_val = None
    base = st.base_config()
    node = base
    for part in "connection.port".split("."):
        node = (node or {}).get(part) if isinstance(node, dict) else None
    yaml_val = node
    if yaml_val is None:
        pytest.skip("robot.yaml 里没有 connection.port，跳过")
    other = int(yaml_val) + 1
    client.put("/api/settings", json={"values": {"connection.port": other}}, headers=h)
    assert st.load_overlay().get("connection", {}).get("port") == other
    client.put("/api/settings", json={"values": {"connection.port": int(yaml_val)}}, headers=h)
    assert "port" not in st.load_overlay().get("connection", {}), \
        "改回 yaml 原值后仍在覆盖层里"


def test_apply_reports_actions(client):
    """生效动作必须如实回报做了什么、以及"需要重启"这种不能自动做的部分。"""
    h = _admin(client)
    client.put("/api/settings", json={"values": {"connection.timeout_s": 3.5}}, headers=h)
    r = client.post("/api/settings/apply", json={"actions": ["reload"]}, headers=h)
    assert r.status_code == 200
    assert "reload" in r.json()["actions"]

    # restart 不由后端代劳（后端不能重启自己），必须给出 note
    r2 = client.post("/api/settings/apply", json={"actions": ["restart"]}, headers=h)
    assert r2.status_code == 200
    d2 = r2.json()
    assert "restart" in d2["actions"]
    assert any("重启" in n for n in d2["notes"]), "restart 动作没有提示用户手动重启"


# ---------------------------------------------------------------------------
# 相机服务地址：由 camera.host + camera.port 合成
# ---------------------------------------------------------------------------
def test_camera_base_url_is_derived(client):
    h = _admin(client)
    client.put("/api/settings", json={"values": {
        "camera.host": "192.168.1.77", "camera.port": 8123}}, headers=h)
    fields = {f["key"]: f for g in client.get("/api/settings").json()["groups"] for f in g["fields"]}
    base = fields["vision.base_url"]
    assert base["value"] == "http://192.168.1.77:8123", f"合成地址不对：{base['value']}"
    assert base["source"] == "derived"
    assert base["editable"] is False, "合成值必须只读（改它无效会让用户反复尝试）"
    assert base["reason"], "合成值只读但没说明改哪里"


# ---------------------------------------------------------------------------
# 测试连接
# ---------------------------------------------------------------------------
def test_connection_test_robot_does_not_raise(client):
    """机器人不可达时要返回 ok:false + hint，不能抛异常。"""
    r = client.post("/api/settings/test", json={"target": "robot"}, headers=_admin(client))
    assert r.status_code == 200
    d = r.json()
    assert d["target"] == "robot"
    assert "ok" in d and "message" in d
    if not d["ok"]:
        assert d["hint"], "测试失败却没给排查提示"


def test_connection_test_bad_target(client):
    r = client.post("/api/settings/test", json={"target": "printer"}, headers=_admin(client))
    assert r.status_code == 400


def test_connection_test_requires_token(client):
    assert client.post("/api/settings/test", json={"target": "robot"}).status_code == 401


# ---------------------------------------------------------------------------
# 重置所有参数
# ---------------------------------------------------------------------------
def test_reset_requires_confirm(client):
    r = client.post("/api/settings/reset", json={"confirm": False}, headers=_admin(client))
    assert r.status_code == 400


def test_reset_requires_admin(client, op_token):
    h = _operator(client, op_token)
    r = client.post("/api/settings/reset", json={"confirm": True}, headers=h)
    assert r.status_code == 403


def test_reset_clears_overlay_and_emits_critical_event(client):
    h = _admin(client)
    client.put("/api/settings", json={"values": {"connection.timeout_s": 4.5}}, headers=h)
    assert st.load_overlay(), "前置条件失败：覆盖层应该是非空的"

    r = client.post("/api/settings/reset", json={"confirm": True}, headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["count"] >= 1
    assert "connection.timeout_s" in d["removed"]
    assert st.load_overlay() == {}, "重置后覆盖层应为空"

    # 审计留痕：不可撤销操作必须以 critical 级别落库，并把改动前的内容留下来
    events = client.get("/api/events", headers=_admin(client)).json()["items"]
    hit = [e for e in events if e["action"] == "config.settings_reset"]
    assert hit, "重置没有写审计事件"
    assert hit[0]["level"] == "critical"
    detail = hit[0].get("detail") or {}
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert "before" in detail, "重置事件里没有保留改动前的内容（无法人工恢复）"


# ---------------------------------------------------------------------------
# 修改口令
# ---------------------------------------------------------------------------
def test_password_change_wrong_current_rejected(client):
    r = client.post("/api/settings/password", json={
        "role": "admin", "current": "definitely-wrong", "password": "newpass1234",
    }, headers=_admin(client))
    assert r.status_code == 403
    # 口令没被改掉：原口令仍可登录
    assert client.post("/api/auth/login", json={"password": "test1234"}).status_code == 200


def test_password_change_revokes_other_tokens(client, monkeypatch):
    """★ 改口令后，其它会话持有的该角色令牌必须作废（当前会话保留）。"""
    h = _admin(client)
    other = client.post("/api/auth/login", json={"password": "test1234"}).json()["token"]

    # 别真写 .env：把落盘那一层拦掉
    from app.api import settings as settings_api
    monkeypatch.setattr(settings_api, "_write_env_key", lambda k, v: "/tmp/.env")

    r = client.post("/api/settings/password", json={
        "role": "admin", "current": "test1234", "password": "newpass1234",
    }, headers=h)
    assert r.status_code == 200, r.text

    # 别人的令牌已失效
    assert client.get("/api/settings/summary",
                      headers={"X-Control-Token": other}).status_code in (200,)  # summary 是公开读
    assert client.put("/api/settings", json={"values": {"robot.model": "X"}},
                      headers={"X-Control-Token": other}).status_code == 401, \
        "改口令后旧令牌仍然可用 —— 越权风险"
    # 当前会话保留（否则用户会被自己踢出去）
    assert client.put("/api/settings", json={"values": {"robot.model": "Y"}},
                      headers=h).status_code == 200

    # 收尾：把口令还原，避免影响同一 session 里后续的用例
    auth_mod.set_password(auth_mod.ROLE_ADMIN, "test1234")


def test_password_change_persist_failure_is_reported(client, monkeypatch):
    """★ .env 写失败必须如实告知"只在内存生效、重启会回到旧口令"。"""
    from app.api import settings as settings_api

    def boom(k, v):
        raise OSError("disk full")

    monkeypatch.setattr(settings_api, "_write_env_key", boom)
    r = client.post("/api/settings/password", json={
        "role": "admin", "current": "test1234", "password": "another1234",
    }, headers=_admin(client))
    assert r.status_code == 200
    d = r.json()
    assert d["persisted"] is False
    assert "重启" in d["note"] and "内存" in d["note"]
    auth_mod.set_password(auth_mod.ROLE_ADMIN, "test1234")


# ---------------------------------------------------------------------------
# summary：一眼看全
# ---------------------------------------------------------------------------
def test_summary_lists_restart_items(client):
    h = _admin(client)
    client.put("/api/settings", json={"values": {"server.ws_path": "/ws/test"}}, headers=h)
    d = client.get("/api/settings/summary").json()
    assert d["ok"] is True
    assert d["overridden"] >= 1
    keys = [x["key"] for x in d["need_restart"]]
    assert "server.ws_path" in keys, "改了需重启项却没在 summary 里提示"


# ---------------------------------------------------------------------------
# ★ 安全不变式：设置模块不允许碰机器人运动
# ---------------------------------------------------------------------------
def test_settings_module_never_calls_motion_command():
    """设置页能改的是"参数"，不是"动作"。

    这条用 **AST** 静态检查写成测试（不是字符串搜索 —— 模块自己的 docstring 里就
    写着"没有任何 motion.command 调用"，字符串搜索会把自己的说明当成违规）。
    一旦 `api/settings.py` 出现真正的运动下发，就意味着"点一次保存可能动一下机器人"，
    而这正是本页设计要排除的事，但代码上很容易顺手加进去（比如"保存后自动校验一下"）。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "api" / "settings.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # 收集所有"属性链"，例如 motion.command / collector.reconnect
    chains = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts = []
            cur = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                chains.add(".".join(reversed(parts)))

    # 1) 不允许出现任何指向"运动指令"的属性链
    forbidden_chain = sorted(c for c in chains if c.split(".")[-1] in (
        "command", "move", "jog", "estop", "plan_step", "plan_tick", "write_target",
    ))
    assert not forbidden_chain, f"settings.py 出现了运动相关调用：{forbidden_chain}"

    # 2) 不允许出现运动类子路径字面量。★ 查的是**子路径**而不是 "/control/move"：
    #    路由是 APIRouter(prefix=...) + @router.post("/move") 拼出来的，
    #    完整路径在源码里不存在，查完整字符串等于什么都没查。
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    leaked = [s for s in literals if s.strip("/") in ("move", "estop", "jog", "run-file")
              or s.strip("/").startswith("jog/")]
    assert not leaked, f"settings.py 里出现了运动路由子路径：{leaked}"

    # 3) 反证：这条检查不是恒真 —— 换一个**真的会下发**的模块，必须被抓出来。
    #    ★ 不能拿 "/control/move" 这种完整路径去找：路由是 prefix + 子路径拼出来的
    #      （router = APIRouter(prefix="/api/control") + @router.post("/move")），
    #      完整字符串在源码里根本不存在 —— 用它做反证会得到"判据有问题"的错误结论。
    #      直接对 api/control.py 跑同一套 AST 判据：它必须命中。
    ctree = ast.parse((root / "app" / "api" / "control.py").read_text(encoding="utf-8"))
    cchains = set()
    for node in ast.walk(ctree):
        if isinstance(node, ast.Attribute):
            parts, cur = [], node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                cchains.add(".".join(reversed(parts)))
    confirmed = sorted(c for c in cchains if c.split(".")[-1] in (
        "command", "move", "jog", "estop", "plan_step", "plan_tick", "write_target",
    ))
    assert confirmed, "反证失败：api/control.py 里找不到任何运动调用，说明上面的判据有问题"
    assert any(c.startswith("motion.") or c.startswith("jog.") for c in confirmed), \
        f"反证失败：命中的链不像运动调用：{confirmed[:8]}"


def test_real_motion_needs_double_confirmation(client):
    """★ 真实下发必须"环境变量 + 配置项"双确认。

    测试环境在 conftest 里强制 EFORT_REAL_MOTION=0（绝不允许测试写真机），
    所以无论配置项开不开，real_motion_active 必须恒为 False —— 这正是双确认的语义。
    （robot.yaml 的 real_write 出厂值已随真机联调改为 true，故不再断言默认 False。）
    """
    h = _admin(client)
    client.put("/api/settings", json={"values": {"motion.real_write": True}}, headers=h)
    after = client.get("/api/settings/summary").json()
    assert after["real_motion_env"] is False          # 测试进程环境强制 0
    assert after["real_motion_active"] is False, \
        "只打开配置项就生效了 —— 真实下发的双重确认失效"


# ---------------------------------------------------------------------------
# ★★ 覆盖层路径解析（EFORT_SETTINGS_PATH）与"测试不碰生产文件"隔离钉
#    （AGENTS.md §5.5 的第四项共享状态；2026-10-13 负责人批准后落地）
# ---------------------------------------------------------------------------
# ★★ 这是**生产口径**的字面量：界面改的配置默认落在 <项目根>/config/app_settings.json。
#    故意写死在这里 —— 它一旦变，"保存的文件"和"后端读的文件"就不是同一个，
#    现场表现是"改了没生效"。**改动需负责人点头。**
PRODUCTION_REL = os.path.join("config", "app_settings.json")


def _production_path() -> str:
    from app.core.config import project_root
    return os.path.join(project_root(), PRODUCTION_REL)


def _snap(path: str):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def test_settings_path_defaults_to_the_production_path(monkeypatch):
    """未设 EFORT_SETTINGS_PATH → 必须是历史那个生产路径（**逐字节一致，不许改**）。"""
    monkeypatch.delenv("EFORT_SETTINGS_PATH", raising=False)
    expected = _production_path()
    assert st.resolve_settings_path() == expected
    assert st.PRODUCTION_SETTINGS_PATH == expected, "生产路径常量本身漂移了"


def test_settings_path_honours_env_override_absolute(monkeypatch, tmp_path):
    """设了绝对路径 → 原样用它（测试隔离走的就是这一条）。"""
    target = str(tmp_path / "overlay" / "app_settings.json")
    monkeypatch.setenv("EFORT_SETTINGS_PATH", target)
    assert st.resolve_settings_path() == target
    assert st.resolve_settings_path() != _production_path()


def test_settings_path_honours_env_override_relative(monkeypatch):
    """相对路径按**项目根**解析（并 normpath，与默认路径同一套分隔符风格）。"""
    from app.core.config import project_root
    monkeypatch.setenv("EFORT_SETTINGS_PATH", "tmp_overlay/app_settings.json")
    assert st.resolve_settings_path() == os.path.normpath(os.path.join(
        project_root(), "tmp_overlay", "app_settings.json"))


def test_test_session_overlay_path_is_not_the_production_file():
    """★★ 本测试会话必须写到临时目录：并发跑两个 pytest 不再抢同一个真实文件。

    这条是"6→2→0 个随机假故障"的根治钉（AGENTS.md §5.5）。
    """
    env = os.environ.get("EFORT_SETTINGS_PATH")
    assert env, "conftest 必须在 import app 之前设置 EFORT_SETTINGS_PATH"
    assert os.path.abspath(st.SETTINGS_PATH) == os.path.abspath(env)
    assert os.path.abspath(st.SETTINGS_PATH) != os.path.abspath(_production_path()), \
        "隔离失效：SETTINGS_PATH 仍指向生产覆盖层 → 用例会在删/写真实配置"
    # 读写函数用的是**模块全局名**（调用时才取），所以运行期改它依然有效
    assert st.runtime_flags()["settings_path"] == st.SETTINGS_PATH


def test_api_write_lands_on_tmp_and_never_the_production_file(client):
    """直接证据：走 API 真写一次覆盖层，临时文件里看得到、生产文件一个字节没动。"""
    prod = _production_path()
    before = _snap(prod)
    try:
        r = client.put("/api/settings", json={"values": {"robot.model": "OVERLAY-PIN"}},
                       headers=_admin(client))
        assert r.status_code == 200, r.text
        assert st.load_overlay().get("robot", {}).get("model") == "OVERLAY-PIN"
        assert os.path.abspath(st.SETTINGS_PATH) != os.path.abspath(prod)
        assert _snap(prod) == before, "测试把覆盖层写进了生产文件"
    finally:
        # 生产文件原本不存在就**必须仍然不存在**（这是本钉的核心断言，不靠还原兜底）
        assert _snap(prod) == before


def test_module_level_path_is_monkeypatchable(monkeypatch, tmp_path):
    """★ 兼容"运行期改 SETTINGS_PATH"的用法：读写函数每次都读模块全局名。"""
    target = str(tmp_path / "nested" / "app_settings.json")
    monkeypatch.setattr(st, "SETTINGS_PATH", target, raising=True)
    out = st.save_patch({"robot.model": "ER8-700H-MP"})
    assert out["changed"]["robot.model"]["new"] == "ER8-700H-MP"
    assert os.path.isfile(target), "写入没有跟着 monkeypatch 的路径走"
    assert st.load_overlay().get("robot", {}).get("model") == "ER8-700H-MP"
    # 重置 = 删当前指向的文件（不是生产那个）
    st.reset_settings()
    assert not os.path.isfile(target)
