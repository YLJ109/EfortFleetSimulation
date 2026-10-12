# -*- coding: utf-8 -*-
"""视觉颜色分拣后端测试（代理 / 摄入 / 记录 / 规则 / 复核 / 权限）。

策略：
  - 相机服务不可用是**最常见**的现实情形，这里用一个假相机服务(同进程 HTTP)覆盖，
    同时留一个用例断言"相机服务没起时接口不 500、只返回 camera_ok=False"。
  - 规则文件写到临时路径(EFORT_VISION_RULES)，绝不污染现场 config/vision_rules.json。
  - 全局自动执行开关默认关 —— 断言"未开启时只记录不下发"，这是安全底线。
"""
from __future__ import annotations

import json
import os
import threading

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# =====================================================================
# 假相机服务：只实现被测到的 /vision/* 端点
# =====================================================================
_CAM = {"seq": 0, "events": {}, "config": {}, "seen": []}


class _CamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        _CAM["seen"].append(("GET", self.path))
        if p == "/vision/status":
            return self._json({
                "supported": True, "enabled": True, "seg": "MOG2", "roi": None,
                "count": len(_CAM["events"]), "seq": _CAM["seq"], "ms": 3.0,
                "card": {n: "#000000" for n in
                         ("红 橙 黄 草绿 深绿 青 天蓝 蓝 深蓝 紫 粉 棕 金 米白 白 银灰 灰 黑").split()},
                "achromatic": ["白", "银灰", "灰", "黑"],
                "calib": {"gains": None, "card_n": 0},
                "last": None, "records": len(_CAM["events"]),
            })
        if p == "/vision/last":
            since = 0
            for kv in (self.path.split("?")[1].split("&") if "?" in self.path else []):
                if kv.startswith("since="):
                    since = int(kv[6:])
            ev = _CAM["events"].get(_CAM["seq"])
            if ev and ev.get("seq", 0) > since:
                return self._json({"ok": True, "seq": _CAM["seq"], "event": ev})
            return self._json({"ok": True, "seq": _CAM["seq"], "event": None})
        if p == "/vision/records":
            return self._json({"ok": True,
                               "records": list(_CAM["events"].values()),
                               "stats": {"total": len(_CAM["events"]), "by_color": {}}})
        if p == "/vision/image":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            body = b"\xff\xd8\xff\xe0JPEGDATA"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._json({"ok": False, "error": "未知路径"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        p = self.path.split("?")[0]
        _CAM["seen"].append(("POST", self.path, body))
        if p == "/vision/config":
            _CAM["config"].update(body)
            return self._json({"ok": True, "status": {"vision": {"enabled": True}}})
        if p == "/vision/background":
            return self._json({"ok": True, "message": "ok",
                               "status": {"vision": {"seg": "MOG2"}}})
        if p == "/vision/calibrate/gray":
            return self._json({"ok": True, "message": "ok", "info": {"gains": [1.0, 1.0, 1.0]}})
        if p == "/vision/calibrate/sample":
            if not body.get("name"):
                return self._json({"ok": False, "message": "缺少颜色名"})
            return self._json({"ok": True, "message": "ok",
                               "info": {"name": body["name"], "lab": body.get("lab")}})
        return self._json({"ok": False, "error": "未知路径"}, 404)


@pytest.fixture(scope="module")
def fake_camera():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CamHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    _CAM["seq"] = 0
    _CAM["events"] = {}
    _CAM["config"] = {}
    _CAM["seen"] = []
    old = os.environ.get("EFORT_CAMERA_URL")
    os.environ["EFORT_CAMERA_URL"] = "http://127.0.0.1:%d" % port
    # 清掉 base_url 缓存，让新地址立刻生效
    from app.services import camera_client
    camera_client._CACHE["base"] = None
    camera_client._CACHE["ts"] = 0.0
    yield _CAM
    if old is None:
        os.environ.pop("EFORT_CAMERA_URL", None)
    else:
        os.environ["EFORT_CAMERA_URL"] = old
    camera_client._CACHE["base"] = None
    camera_client._CACHE["ts"] = 0.0
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def tmp_rules_path(tmp_path, monkeypatch):
    """规则文件必须落到临时路径：绝不覆盖现场调好的 config/vision_rules.json。"""
    from app.services import vision_rules as vr
    p = tmp_path / "vision_rules.json"
    monkeypatch.setattr(vr, "_rules_path", lambda: str(p))
    yield p


@pytest.fixture(autouse=True)
def reset_ingest_seq():
    """摄入游标是模块级状态，用例之间必须复位（否则第二个用例什么都收不到）。"""
    from app.services.vision_ingest import ingest
    ingest.last_seq = 0
    ingest._backfilled = False
    ingest.ingested = 0
    yield


def _tok(client, password="test1234"):
    r = client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(client, password="test1234"):
    return {"X-Control-Token": _tok(client, password)}


def _push_event(seq, color="红", **kw):
    ev = {
        "seq": seq, "ts": 1.0, "time": "2026-09-23 20:00:00", "color": color,
        "hex": "#E02020", "conf": 0.92, "de": 3.1, "alt": None, "alt_de": None,
        "chroma": 60.0, "ratio": 0.88, "lab": [53.2, 80.1, 67.2], "reason": None,
        "box": [[10, 10], [100, 10], [100, 80], [10, 80]], "center": [55.0, 45.0],
        "area": 5600, "objects": 1, "dir": "x", "images": {"full": "a/full.jpg"},
    }
    ev.update(kw)
    _CAM["seq"] = seq
    _CAM["events"][seq] = ev
    return ev


# =====================================================================
# 状态与代理
# =====================================================================
def test_status_when_camera_down(client, monkeypatch):
    """相机服务没起时必须优雅降级，不能 500 —— 这是最常见的现场情形。"""
    from app.services import camera_client
    monkeypatch.setattr(camera_client, "base_url", lambda: "http://127.0.0.1:1")
    r = client.get("/api/vision/status")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["camera_ok"] is False
    assert j["camera"] is None
    assert j["camera_error"]


def test_status_with_camera(client, fake_camera):
    r = client.get("/api/vision/status")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["camera_ok"] is True
    assert len(j["camera"]["card"]) == 18
    assert "ingest" in j


def test_config_proxy_requires_admin(client, fake_camera):
    r = client.post("/api/vision/config", json={"enabled": True})
    assert r.status_code == 401
    r = client.post("/api/vision/config", json={"enabled": True}, headers=_h(client))
    assert r.status_code == 200, r.text
    assert fake_camera["config"]["enabled"] is True


def test_background_and_calib_endpoints(client, fake_camera):
    h = _h(client)
    r = client.post("/api/vision/background", json={"reset": False}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/api/vision/calibrate/gray", json={"target": 118.0}, headers=h)
    assert r.json()["info"]["gains"] == [1.0, 1.0, 1.0]
    r = client.post("/api/vision/calibrate/sample", json={"name": "红", "lab": [1, 2, 3]},
                    headers=h)
    assert r.json()["ok"] is True
    r = client.post("/api/vision/calibrate/sample", json={"name": "", "lab": [1, 2, 3]},
                    headers=h)
    assert r.status_code == 422          # pydantic min_length 拦住


def test_image_proxy_and_traversal(client, fake_camera):
    r = client.get("/api/vision/image", params={"p": "a/full.jpg"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content.startswith(b"\xff\xd8")


def test_card_endpoint(client, fake_camera):
    r = client.get("/api/vision/card")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(r.json()["card"]) == 18


# =====================================================================
# 摄入（轮询 → 落库 → 事件）
# =====================================================================
def test_ingest_writes_record_and_event(client, fake_camera):
    _push_event(1001, "蓝", hex="#1F4FD8")
    from app.services.vision_ingest import ingest
    ingest._tick()                        # 第一跳：backfill
    ingest._tick()                        # 第二跳：增量
    r = client.get("/api/vision/records?limit=10")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(x["color"] == "蓝" and x["seq"] == 1001 for x in items), items
    r = client.get("/api/events?category=vision&limit=20", headers=_h(client))
    acts = [e["action"] for e in r.json()["items"]]
    assert "vision.detect" in acts


def test_ingest_is_idempotent(client, fake_camera):
    """后端重启会从 since=0 重放，必须靠 seq 幂等，否则每次都多记一条。"""
    from app.services.vision_ingest import ingest
    _push_event(1002, "黄")
    ingest.last_seq = 0
    ingest._backfilled = False
    for _ in range(3):
        ingest._tick()
        ingest.last_seq = 0
        ingest._backfilled = False
    r = client.get("/api/vision/records?color=黄&limit=50")
    seqs = [x["seq"] for x in r.json()["items"]]
    assert seqs.count(1002) == 1, seqs


def test_ingest_waits_for_event_not_bare_seq(client, fake_camera):
    """★ 审计修复 P0-cam-2 回归：`{seq: N, event: None}` 绝不能推进游标。

    这就是"摄像头偶尔才检测到"的根因：相机侧 seq 曾在**落盘三张图之前**自增，
    而 event 要等落盘之后才发布 —— 该窗口内 `/vision/last` 返回的正是
    {seq: N, event: None}（或 event 还是上一条）。旧实现无条件
    `last_seq = max(last_seq, 响应里的 seq)`，游标一旦越过 N，事件 N 之后永远不满足
    `event.seq > since` → 这次检测被当成已消费而**永久丢弃**。

    同一条用例顺带覆盖 P0-cam-2b：两条事件挤在一个轮询窗口里时，
    `/vision/last` 只留最新一条，中间那条必须靠 /vision/records 补洞回来。
    """
    from app.services.vision_ingest import ingest
    _push_event(1006, "红")
    ingest._tick()                                  # 首跳 backfill → 游标应到 1006
    assert ingest.last_seq == 1006, ingest.last_seq

    # 模拟竞态窗口：序号已经自增到 1099，但事件尚未发布（event=None）
    fake_camera["seq"] = 1099
    fake_camera["events"].pop(1099, None)
    ingest._tick()
    assert ingest.last_seq == 1006, \
        "裸 seq 推进了游标(旧实现): %s —— 事件 1099 将被永久丢弃" % ingest.last_seq

    # 窗口过去、两条事件真的发布了：中间的 1050 与最新的 1099 都必须入库
    _push_event(1050, "蓝")
    _push_event(1099, "绿")
    ingest._tick()
    assert ingest.last_seq == 1099, ingest.last_seq
    recs = client.get("/api/vision/records?limit=200").json()["items"]
    assert any(x["seq"] == 1050 for x in recs), "P0-cam-2b 补洞失败: 1050 丢了"
    assert any(x["seq"] == 1099 for x in recs), "P0-cam-2: 最新事件 1099 丢了"


def test_records_filter_and_stats(client, fake_camera):
    _push_event(1003, "红")
    from app.services.vision_ingest import ingest
    ingest._tick()
    r = client.get("/api/vision/stats")
    assert r.status_code == 200
    assert r.json()["total"] >= 1
    r = client.get("/api/vision/records?color=红")
    assert all(x["color"] == "红" for x in r.json()["items"])


def test_export_csv_has_bom(client, fake_camera):
    r = client.get("/api/vision/export?format=csv")
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf")        # Excel 打开不乱码
    assert b"color" in r.content


def test_export_json(client, fake_camera):
    r = client.get("/api/vision/export?format=json")
    assert r.status_code == 200
    j = json.loads(r.content.decode("utf-8"))
    assert j["format"] == "efort-vision/v1"
    assert isinstance(j["items"], list)


# =====================================================================
# 规则
# =====================================================================
def test_rules_default_and_save(client, fake_camera):
    r = client.get("/api/vision/rules")
    assert r.status_code == 200
    assert r.json()["rules"] == []
    # 没有程序时也要能查
    assert isinstance(r.json()["programs"], list)

    r = client.put("/api/vision/rules", json={"rules": [{"color": "红"}]})
    assert r.status_code == 401                       # 需管理员

    r = client.put("/api/vision/rules",
                   json={"rules": [{"color": "红", "program_id": 1, "auto": True,
                                    "speed_pct": 999, "note": "x"}]},
                   headers=_h(client))
    assert r.status_code == 200, r.text
    got = r.json()["rules"][0]
    assert got["color"] == "红" and got["program_id"] == 1
    assert got["speed_pct"] == 100                     # 越界被夹到合法范围
    # 非法条目被丢弃
    r = client.put("/api/vision/rules", json={"rules": [{"program_id": 3}, "垃圾", 5]},
                   headers=_h(client))
    assert r.json()["rules"] == []


def test_rules_missing_speed_defaults_to_safe_min(client):
    """★★ 2026-10-11 修复（"参数写死 → 安全缺省失效"这一类）：

    没写 `speed_pct` 的视觉规则**不许落盘成 100%** —— 视觉链路是"相机事件**无人值守**触发"，
    缺省跑满速是最危险的默认值。原实现是 `int(it.get("speed_pct") or 100)`，于是
    `execute_rule` 里的 `clamp_speed(rule["speed_pct"])`（注释声称"缺省改为安全下限"）
    **从未生效**：拿到 100 还是 100。现在缺省/非法 → `SPEED_MIN`，越界仍夹到工程上限。
    """
    from app.core.safety_const import SPEED_MIN
    r = client.put("/api/vision/rules",
                   json={"rules": [
                       {"color": "红", "program_id": 1},                    # 没设速度
                       {"color": "绿", "program_id": 2, "speed_pct": 999},  # 越界
                       {"color": "蓝", "program_id": 3, "speed_pct": 30},   # 显式
                       {"color": "黑", "program_id": 4, "speed_pct": 0},    # 非法/0
                   ]},
                   headers=_h(client))
    assert r.status_code == 200, r.text
    got = {x["color"]: x["speed_pct"] for x in r.json()["rules"]}
    assert got["红"] == SPEED_MIN, ("没设速度必须回落安全下限，不能是 100%%：%r" % got)
    assert got["绿"] == 100, "越界仍夹到工程上限"
    assert got["蓝"] == 30, "显式设的速度要原样保留"
    assert got["黑"] == SPEED_MIN, "0/非法也要回落安全下限（绝不静默归零或归百）"
    # 再读一次：落盘的值就是安全下限（不是"读时才算对"）
    again = {x["color"]: x["speed_pct"] for x in client.get("/api/vision/rules",
                                                            headers=_h(client)).json()["rules"]}
    assert again["红"] == SPEED_MIN, "落盘值也必须是安全下限"


def test_rule_not_executed_without_global_switch(client, fake_camera, tmp_rules_path):
    """★ 安全底线：全局开关为关时，命中规则也绝不运动，只记录。"""
    from app.services import vision_rules as vr
    from app.services.motion import motion
    vr.save_rules({"rules": [{"color": "红", "program_id": 1, "auto": True}]})
    rule = vr.rule_for("红")
    assert rule is not None
    calls = []
    orig = motion.command
    motion.command = lambda *a, **k: (calls.append(a), {"ok": True})[1]
    try:
        res = vr.execute_rule(rule)
    finally:
        motion.command = orig
    assert calls == []                                 # 一次都没下发
    assert res["ok"] is False
    assert "auto_execute" in res["note"]
    assert res["outcome"] == "skipped"


def test_rule_disabled_not_executed(client, fake_camera):
    from app.services import vision_rules as vr
    vr.save_rules({"rules": [{"color": "红", "program_id": 1, "auto": True,
                              "enabled": False}]})
    rule = vr.rule_for("红")
    assert rule is None                                # 停用的规则压根不匹配


def test_execute_rule_respects_fence_interlock(client, fake_camera, monkeypatch):
    """★ 开启自动执行后，围栏 danger 仍必须拦住——这是防"视觉误判→意外动作"的兜底。"""
    from app.services import vision_rules as vr
    from app.services.motion import motion
    from app.services import safety_guard
    from app.db.database import SessionLocal
    from app.db import crud

    monkeypatch.setattr(vr, "auto_execute_enabled", lambda: True)
    monkeypatch.setattr(vr, "guard_check", lambda: (False, "围栏 danger", {}))

    db = SessionLocal()
    try:
        pid = crud.create_program(db, name="测试程序", items_json=json.dumps(
            [{"joints": [1, 2, 3, 4, 5, 6]}])).id
    finally:
        db.close()
    try:
        vr.save_rules({"rules": [{"color": "红", "program_id": pid, "auto": True}]})
        rule = vr.rule_for("红")
        calls = []
        orig = motion.command
        motion.command = lambda *a, **k: (calls.append(a), {"ok": True})[1]
        try:
            res = vr.execute_rule(rule)
        finally:
            motion.command = orig
        assert calls == []
        assert res["outcome"] == "blocked"
        assert "围栏" in res["note"]
    finally:
        db = SessionLocal()
        try:
            crud.delete_program(db, pid)
        finally:
            db.close()
        safety_guard.update("safe", "", "", 0.0, 1.0)


def test_execute_rule_runs_program_when_all_allowed(client, fake_camera, monkeypatch):
    from app.services import vision_rules as vr
    from app.services.motion import motion
    from app.db.database import SessionLocal
    from app.db import crud

    monkeypatch.setattr(vr, "auto_execute_enabled", lambda: True)
    monkeypatch.setattr(vr, "guard_check", lambda: (True, "", {}))
    monkeypatch.setattr(vr.time, "sleep", lambda *_: None)

    db = SessionLocal()
    try:
        pid = crud.create_program(db, name="规则程序", items_json=json.dumps(
            [{"joints": [1, 2, 3, 4, 5, 6]}, {"joints": [7, 8, 9, 10, 11, 12]}])).id
    finally:
        db.close()
    try:
        vr.save_rules({"rules": [{"color": "红", "program_id": pid, "auto": True,
                                  "speed_pct": 50}]})
        rule = vr.rule_for("红")
        calls = []
        orig = motion.command
        # ★ 接受 abort_check 关键字：execute_rule 现在会传中止判据
        #   （运行中关闭 auto_execute 开关可立即停下发），mock 必须兼容该签名。
        motion.command = lambda joints, sp=100, dw=0, abort_check=None: (
            calls.append(([float(x) for x in joints], sp)), {"ok": True,
                                                             "mode": "simulate"})[1]
        try:
            res = vr.execute_rule(rule)
        finally:
            motion.command = orig
        assert res["ok"] is True and res["outcome"] == "executed"
        assert len(calls) == 2
        assert calls[0][0][0] == 1.0 and calls[0][1] == 50   # 用了规则里的速度
    finally:
        db = SessionLocal()
        try:
            crud.delete_program(db, pid)
        finally:
            db.close()


# =====================================================================
# 复核 / 清理
# =====================================================================
def test_review_requires_admin_and_updates(client, fake_camera):
    from app.services.vision_ingest import ingest
    _push_event(1004, "未识别色")
    ingest.last_seq = 0
    ingest._backfilled = False
    ingest._tick()
    ingest._tick()
    r = client.get("/api/vision/records?limit=5")
    rec = next(x for x in r.json()["items"] if x["seq"] == 1004)

    assert client.post("/api/vision/records/%d/review" % rec["id"],
                       json={"color": "红"}).status_code == 401
    r = client.post("/api/vision/records/%d/review" % rec["id"],
                    json={"color": "红", "teach": True}, headers=_h(client))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["color"] == "红" and j["outcome"] == "reviewed"
    assert j["taught"] is True


def test_review_missing_record_404(client, fake_camera):
    r = client.post("/api/vision/records/999999/review", json={"color": "红"},
                    headers=_h(client))
    assert r.status_code == 404


def test_clear_records_requires_admin(client, fake_camera):
    assert client.delete("/api/vision/records").status_code == 401
    r = client.delete("/api/vision/records", headers=_h(client))
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.get("/api/vision/records")
    assert r.json()["items"] == []


def test_clear_records_days_keeps_recent(client, fake_camera):
    from app.services.vision_ingest import ingest
    _push_event(1005, "青")
    ingest.last_seq = 0
    ingest._backfilled = False
    ingest._tick()
    ingest._tick()
    r = client.delete("/api/vision/records?days=7", headers=_h(client))
    assert r.json()["deleted"] == 0                    # 刚才那条是"现在"的，不该被删
    r = client.get("/api/vision/records")
    assert any(x["seq"] == 1005 for x in r.json()["items"])
