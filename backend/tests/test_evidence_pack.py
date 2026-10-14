# -*- coding: utf-8 -*-
"""A5 事件取证包：danger/hit 上升沿自动落盘的**取证口径**钉。

取证包是"每一步都能自证"的落地物，它最容易犯的错有三种，本文件一条一条钉死：
  ① **为了取证把采集线程拖住**（丢帧 = 画面停在最后一帧，比不取证更危险）；
  ② **缺了东西不吭声**（相机没开就不写 frame.jpg、也不说 → 复盘时以为"当时没拍"）；
  ③ **把口令/令牌原样写进盘**（取证目录是要能导给别人看的东西）。

★★ 本文件**不在模块顶层 import 任何 app.* 业务模块**：pytest 在 collection 阶段就会
   import 测试模块，而 `app.services.collector` 会连带 import `app.db.database` ——
   那会在 `client` 夹具把 EFORT_DB_URL 指到临时库**之前**就把会话绑到生产
   `data/robot.db`（AGENTS.md §5.5：测试必须与生产隔离的三样之一）。一律函数内导入。
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import time

import pytest


@pytest.fixture()
def guard(client):
    """围栏守卫单例（放在夹具里 = 晚于 `client`，DB 已指向临时库）。"""
    from app.services import safety_guard
    return safety_guard


@pytest.fixture()
def ev(client, tmp_path, monkeypatch):
    """取证根目录跟着 **EFORT_LOGS_DIR** 走（测试隔离三样之一）+ 相机一律"没开"。"""
    from app.services import camera_client
    from app.services import safety_evidence as se
    from app.services import safety_guard as g

    monkeypatch.setattr(se, "logs_dir", lambda: str(tmp_path), raising=True)
    monkeypatch.setattr(camera_client, "get_binary",
                        lambda *a, **k: (0, None, "测试环境无相机"), raising=True)
    # ★ 冻结**采集循环**那一拍的独立评估（只冻结本例期间，teardown 自动还原）。
    #   `client` 夹具会走一遍 lifespan → collector 线程在整个会话里每 0.2s 调一次
    #   `safety_update_server` / `safety_note`。它和本例是**同一个全局单例**：
    #   仿真位姿算出 safe 就会把 prev 顶回去，"danger 再报 danger 不出第二个包"
    #   这条判据于是变成看时机的赌博（全量跑必红、单跑碰巧绿）。
    #   打的是 collector 模块全局名 —— 循环里每次调用都按全局解析，所以真的生效。
    from app.services import collector as collector_mod
    monkeypatch.setattr(collector_mod, "safety_update_server",
                        lambda res: {}, raising=True)
    monkeypatch.setattr(collector_mod, "safety_note", lambda s: None, raising=True)
    g.reset_server()
    se.reset_buffer()
    yield se
    se.reset_buffer()
    g.reset_server()


def _packs(se) -> list:
    return se.list_packs(limit=200)["packs"]


def _res(state="danger", zone="z1", clearance=0.02):
    return {"state": state, "zone_id": zone, "zone_name": f"围栏{zone}",
            "clearance": clearance, "ratio": 0.3, "measured": True,
            "calibrated": True, "ground": {"clearance_mm": 190.0}}


def _wait(se, n: int = 1, timeout: float = 8.0) -> list:
    """等后台线程写完包（异步落盘本身就是被测行为，不许用 blocking 绕过）。

    ★ 落盘是"先写暂存目录、写完再改名入场"，所以这里看到的包**一定是完整的**
      （不会出现"目录有了、manifest 还没写"的半成品）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = _packs(se)
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"取证包没在 {timeout}s 内落盘（当前 {len(_packs(se))} 个）")


# ---------------------------------------------------------------- 上升沿
def test_danger_rising_edge_writes_pack_once(ev, guard):
    """★ safe→danger 出**一个**包；danger 再报 danger（没有上升沿）不出第二个。

    "每拍都出包"会把盘刷爆、也让复盘看不出哪一刻是事发时刻；
    "根本不出包"则等于把主张里的"自证"掏空。
    """
    guard.update_server(_res(state="safe"))
    assert _packs(ev) == []
    guard.update_server(_res(state="danger"))
    rows = _wait(ev, 1)
    assert len(rows) == 1, rows
    seq = rows[0]["seq"]
    guard.update_server(_res(state="danger"))
    guard.update_server(_res(state="danger"))
    time.sleep(0.3)
    assert len(_packs(ev)) == 1, "同一状态重复上报不该再出包"
    assert _packs(ev)[0]["seq"] == seq

    m = ev.get_pack(rows[0]["id"])["manifest"]
    assert m["trigger"]["to_state"] == "danger" and m["trigger"]["source"] == "server"
    assert m["trigger"]["from_state"] == "safe"
    assert m["seq"] == seq and m["schema"] == ev.SCHEMA
    d = os.path.join(ev.base_dir(), rows[0]["id"])
    for f in ("manifest.json", "samples.jsonl", "events.jsonl"):
        assert os.path.isfile(os.path.join(d, f)), f
    # 三态必须原样带进包里（不许塌成"当时安全/不安全"两值）
    assert m["fence"]["client"]["freshness"] in ("never", "fresh", "stale")
    assert m["fence"]["server"]["freshness"] in ("never", "fresh", "stale")
    assert m["fence"]["mode"] in ("off", "shadow", "enforce")


def test_client_report_rising_edge_also_writes_pack(ev, guard):
    """前端那一路（浏览器里的数模评估）变严重同样出包 —— 两路都是证据来源。"""
    guard.update("safe")
    guard.update("hit", "z2", "围栏Z2", 0.0, 0.1)
    rows = _wait(ev, 1)
    assert rows[0]["to_state"] == "hit"
    m = ev.get_pack(rows[0]["id"])["manifest"]
    assert m["trigger"]["source"] == "client"
    assert m["trigger"]["zone_name"] == "围栏Z2"


def test_capture_never_blocks_the_caller(ev, guard, monkeypatch):
    """★★ 写盘（读库 + 抓相机图）全在后台线程：采集线程调它必须**立刻返回**。

    这条是"取证不能反过来伤害安全"的硬边界 —— 在采集循环里做磁盘/网络 IO 就会丢
    姿态帧，而丢帧正是本项目明令禁止的"画面停在最后一帧"那一类事故。
    """
    from app.services import camera_client

    def slow_snapshot(*a, **k):
        time.sleep(0.5)
        return 200, b"\xff\xd8" + b"jpg" * 200, None

    monkeypatch.setattr(camera_client, "get_binary", slow_snapshot, raising=True)
    guard.reset_server()
    t0 = time.time()
    guard.update_server(_res(state="danger"))
    spent = time.time() - t0
    assert spent < 0.15, f"上升沿调用被写盘拖住了 {spent:.2f}s（采集线程会丢帧）"
    rows = _wait(ev, 1)
    assert rows[0]["frame"] == "frame.jpg"        # 后台线程里才去等那张慢图


# ---------------------------------------------------------------- seq
def test_seq_is_monotonic_and_never_reused(ev):
    """★ 序号只增不减、**删包也不回填**（铁律①）：前端刷新水位靠它，不能用列表长度。"""
    ids, seqs = [], []
    for i in range(3):
        r = ev.request_capture({"source": "test", "from_state": "safe",
                                "to_state": "danger", "zone_id": f"z{i}",
                                "clearance": 0.02}, blocking=True)
        assert r["ok"]
        ids.append(r["pack"]["id"])
        seqs.append(r["pack"]["seq"])
    assert seqs == sorted(seqs) and len(set(seqs)) == 3

    # 把最新那个包删掉（模拟 keep_days 清理），下一个号必须比历史最大值更大
    shutil.rmtree(os.path.join(ev.base_dir(), ids[-1]))
    r = ev.request_capture({"source": "test", "to_state": "hit", "zone_id": "zx"},
                           blocking=True)
    assert r["pack"]["seq"] > max(seqs), "序号被复用 = 前端会以为没有新包"
    assert r["pack"]["id"] not in ids

    listing = ev.list_packs(limit=50)
    assert listing["latest_seq"] == r["pack"]["seq"]
    assert listing["packs"][0]["seq"] == listing["latest_seq"]      # 最新在前


def test_list_carries_latest_seq_independent_of_total(ev):
    """`latest_seq` 与 `total` 各自独立：列表被裁短时水位仍然前进（接口契约）。"""
    ev.request_capture({"source": "test", "to_state": "danger", "zone_id": "a"}, blocking=True)
    ev.request_capture({"source": "test", "to_state": "danger", "zone_id": "b"}, blocking=True)
    d = ev.list_packs(limit=1)                     # 只给 1 条，看水位还在不在
    assert d["total"] == 2 and len(d["packs"]) == 1
    assert d["latest_seq"] == d["packs"][0]["seq"] >= 2
    assert {"seq", "id", "to_state", "frame", "honest_gaps"} <= set(d["packs"][0])


# ---------------------------------------------------------------- 缺东西要如实说
def test_missing_camera_is_recorded_as_null_not_absent(ev):
    """★ 相机没开：manifest 里 `frame` 键**存在且为 null**，并在 honest_gaps 写清原因。

    "没有这个键"和"这个键是 null"在复盘时是两回事：前者让人以为程序漏写了，
    后者让人知道"当时确实没有画面"（相机默认就是关的，这是事实不是故障）。
    """
    r = ev.request_capture({"source": "test", "to_state": "hit", "zone_id": "z9",
                            "clearance": 0.0}, blocking=True)
    m = ev.get_pack(r["pack"]["id"])["manifest"]
    assert "frame" in m and m["frame"] is None
    assert m["frame_info"]["captured"] is False
    assert "测试环境无相机" in m["frame_info"]["error"]
    assert any("相机单帧" in x for x in m["honest_gaps"])
    d = os.path.join(ev.base_dir(), r["pack"]["id"])
    assert not os.path.exists(os.path.join(d, "frame.jpg"))
    assert "frame.jpg" not in m["files"]


def test_honest_gap_for_missing_3d_canvas(ev):
    """3D 画布截图没有上传通道 ⇒ **如实写缺**，不编一个不存在的文件（铁律⑤）。"""
    r = ev.request_capture({"source": "test", "to_state": "danger"}, blocking=True)
    pid = r["pack"]["id"]
    m = ev.get_pack(pid)["manifest"]
    assert any("3D 画面截图" in x for x in m["honest_gaps"])
    assert "snapshot.png" not in m["files"]
    d = os.path.join(ev.base_dir(), pid)
    assert [n for n in os.listdir(d) if n.endswith(".png")] == []


def test_stale_frame_is_not_presented_as_truth(ev, monkeypatch):
    """陈旧帧（读控制器失败后冻结的那一帧）进包里必须**自己说明自己陈旧**。"""
    from app.services.collector import collector

    ev.note({"at": time.time(), "joints": [0] * 6, "stale": True})
    monkeypatch.setattr(collector, "get_latest", lambda: {
        "type": "pose", "j1": 0, "j2": 0, "j3": 0, "j4": 0, "j5": 0, "j6": 0,
        "tcp": {"x": 0, "y": 0, "z": 0}, "simulated": False, "stale": True,
        "at": time.time()})
    r = ev.request_capture({"source": "test", "to_state": "danger"}, blocking=True)
    m = ev.get_pack(r["pack"]["id"])["manifest"]
    assert m["robot"]["pose_frame"]["stale"] is True
    assert any("陈旧帧" in x for x in m["honest_gaps"])


def test_samples_are_a_time_window_not_a_count(ev):
    """环形缓冲按**时间**裁剪：采集节拍可调（read_hz），按条数会骗人。"""
    ev.note({"at": time.time(), "state": "safe"})
    ev.note({"at": time.time() - ev.buffer_seconds() - 5.0, "state": "danger"})
    kept = ev.window_samples()
    assert [s["state"] for s in kept] == ["safe"]


def test_note_never_raises_from_the_collector_loop(ev):
    """采集循环里 note() 吞掉一切异常 —— 取证坏了不能带崩采集。"""
    ev.note(None)                       # 不该抛
    ev.note({"at": "不是数字", "joints": None})
    assert ev.buffer_seconds() >= 1.0


# ---------------------------------------------------------------- 脱敏
def test_pack_never_contains_secrets(ev):
    """★ 取证目录是要导给别人看的东西：**口令/令牌一律不许进盘**。

    两层一起钉：① 键名是 token/password/... 的字段直接丢掉；
    ② 事件 detail 里带出的口令明文与 "token":"xxx" 这种键值对打码成 ***。
    """
    from app.services.events import emit as emit_event

    pw = os.environ.get("EFORT_ADMIN_PASSWORD") or "test1234"
    emit_event("control", "warn", "control.debug_leak",
               f"消息里带了口令 {pw}",
               {"token": "deadbeef" * 6, "password": pw,
                "target": [0, -30, 0, 0, 0, 0], "speed_pct": 30})
    r = ev.request_capture({"source": "test", "to_state": "danger", "zone_id": "z7"},
                           blocking=True)
    d = os.path.join(ev.base_dir(), r["pack"]["id"])
    blob = ""
    for name in os.listdir(d):
        fp = os.path.join(d, name)
        if os.path.isfile(fp) and not name.endswith(".jpg"):
            blob += open(fp, encoding="utf-8", errors="replace").read()
    assert pw not in blob, "口令明文进取证包"
    assert "deadbeef" not in blob, "令牌明文进取证包"
    m = json.loads(open(os.path.join(d, "manifest.json"), encoding="utf-8").read())
    flat = json.dumps(m, ensure_ascii=False).lower()
    for banned in ('"token"', '"password"', '"authorization"', '"cookie"'):
        assert banned not in flat, f"取证包里出现了凭据字段 {banned}"
    # 被打码的那条事件仍在（不能为了脱敏把整条证据丢掉）
    assert any(str(e.get("action")) == "control.debug_leak" for e in m["commands_recent"])


# ---------------------------------------------------------------- 目录与生命周期
def test_evidence_dir_follows_logs_dir(ev):
    """★ 取证包必须落在 `logs_dir()` 底下 —— 测试隔离三样之一（EFORT_LOGS_DIR）。

    写进生产 logs/ 的话，测试跑一次就多一堆假取证包，现场排障时分不清真假。
    """
    assert os.path.basename(ev.base_dir()) == "evidence"
    assert os.path.dirname(ev.base_dir()) == os.path.abspath(ev.logs_dir())


def test_half_written_pack_never_shows_up(ev):
    """★ 半成品写在 `.pending-*` 里、写完才改名 ⇒ 列表/详情永远看不到"缺 manifest 的包"。

    （现场表现：取证列表里冒出一条 `corrupt` 的假包，操作员以为取证功能坏了。）
    """
    r = ev.request_capture({"source": "test", "to_state": "danger", "zone_id": "w"},
                           blocking=True)
    rows = _packs(ev)
    assert rows and all("corrupt" not in x for x in rows)
    assert os.path.isdir(os.path.join(ev.base_dir(), r["pack"]["id"]))
    assert [n for n in os.listdir(ev.base_dir()) if n.startswith(".pending-")] == []


def test_keep_days_prunes_old_packs(ev):
    """过期包会被清（方案边界：不做永久保留），但**当日的新包绝不会被清掉**。"""
    r = ev.request_capture({"source": "test", "to_state": "danger", "zone_id": "p"},
                           blocking=True)
    keep = r["pack"]["id"]
    old = os.path.join(ev.base_dir(), "20200101T000000Z-1")
    os.makedirs(old)
    with open(os.path.join(old, "manifest.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))
    assert ev.prune() >= 1
    assert os.path.isdir(os.path.join(ev.base_dir(), keep)), "当日包被误删"
    assert not os.path.isdir(old)


def test_disabled_switch_writes_nothing(ev, monkeypatch):
    """`evidence.enabled=false` ⇒ 一个字节都不落（互锁不受影响）。"""
    monkeypatch.setattr(ev, "enabled", lambda: False)
    r = ev.request_capture({"source": "test", "to_state": "hit"}, blocking=True)
    assert r["queued"] is False and "enabled" in r["reason"]
    assert _packs(ev) == []


# ---------------------------------------------------------------- 接口层
def test_evidence_api_requires_token_and_exposes_latest_seq(ev, client):
    assert client.get("/api/safety/evidence").status_code == 401
    assert client.get("/api/safety/evidence/whatever").status_code == 401
    assert client.get("/api/safety/evidence/x/zip").status_code == 401

    tok = client.post("/api/auth/login", json={"password": "test1234"}).json()["token"]
    h = {"X-Control-Token": tok}
    ev.request_capture({"source": "test", "to_state": "danger", "zone_id": "api"},
                       blocking=True)
    d = client.get("/api/safety/evidence", headers=h).json()
    assert d["total"] >= 1 and d["latest_seq"] >= 1
    pid = d["packs"][0]["id"]
    one = client.get(f"/api/safety/evidence/{pid}", headers=h)
    assert one.status_code == 200 and one.json()["manifest"]["id"] == pid
    z = client.get(f"/api/safety/evidence/{pid}/zip", headers=h)
    assert z.status_code == 200 and len(z.content) > 100
    assert client.get("/api/safety/evidence/no-such-pack", headers=h).status_code == 404


def test_evidence_api_rejects_path_traversal(ev, client):
    """id 是从浏览器来的字符串 —— 越界到 logs/ 外面就等于任意文件读取。"""
    tok = client.post("/api/auth/login", json={"password": "test1234"}).json()["token"]
    h = {"X-Control-Token": tok}
    for bad in ("..%2F..%2F..%2Fetc%2Fpasswd", "..%2Fsecret", "%2e%2e%2fsecret"):
        r = client.get(f"/api/safety/evidence/{bad}", headers=h)
        assert r.status_code in (400, 404), (bad, r.status_code, r.text[:200])


def test_collector_feeds_the_ring_buffer(client):
    """接线钉：采集循环真的把实时样本推给缓冲（不接线 = 包里的 samples 永远是空的）。"""
    import app.services.collector as collector_mod

    src = inspect.getsource(collector_mod)
    assert "safety_note(" in src, "采集循环没往取证缓冲里推样本"
    assert "safety_update_server(" in src, "采集循环没接后端独立评估"
    assert "import note as safety_note" in src
