# -*- coding: utf-8 -*-
"""★ B1（B 批·可观测性）：/api/system/metrics 与 services/metrics 的回归钉。

钉住的东西（全是 §5.5「静默失效与假显示」在这一层的落地）：
  1. 端点**只读、不要控制令牌** —— 与 /api/system/health、/api/rc-status 同一口径；
  2. 每一项都是「值 + 单位 + 时间戳 + 来源」四元组；**没测 = null + note**，
     绝不许把「没测」写成 0（反例：拿 `count==0` 输出 value=0 就是假显示）；
  3. 埋点真的接在唯一实现上：hub.broadcast / ModbusRobot.read_pose /
     camera_client._request / events.emit 各记各的计数（不是端点自己编数）；
  4. runs 组直接读 `api/control._RUN_ACTIVE` 真实结构 —— 「队列长度」如实 null，
     因为 run-file 是**直接起守护线程**，根本没有排队结构（不许编一个 0）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """★ 与 conftest 的 ui_speed.reset 夹具同一条路：进程级累计值必须每例前后清，
    否则上一条用例的计数会串进下一条的断言。清的是**测试进程自己**的登记，
    不影响生产（生产进程没有这个夹具）。"""
    from app.services import metrics
    metrics.reset()
    yield
    metrics.reset()


# ---------------------------------------------------------------- 登记处本体
def test_stat_tracks_last_max_min_count():
    from app.services import metrics
    metrics.stat("modbus.read_regs_rt_ms", 10.0)
    metrics.stat("modbus.read_regs_rt_ms", 30.0)
    metrics.stat("modbus.read_regs_rt_ms", 20.0)
    s = metrics.snapshot()["modbus.read_regs_rt_ms"]
    assert s["measured"] is True
    assert s["value"] == 20.0                       # last = 最近一次
    assert s["max"] == 30.0 and s["min"] == 10.0
    assert s["count"] == 3 and s["avg"] == 20.0
    assert s["unit"] == "ms" and s["ts"] and s["source"]
    # 没有历史样本池就不许装作有分位数
    assert s["percentiles"] is None
    assert "p50/p95" in s["note"]


def test_never_measured_is_null_not_zero():
    from app.services import metrics
    # 用 collector.read_fail_streak：测试进程钉死模拟（conftest），真实读从不发生
    #   → 这个 key 不可能被任何共同写者采样，「没测」才是确定性的。
    s = metrics.snapshot()["collector.read_fail_streak"]
    assert s["value"] is None
    assert s["measured"] is False
    assert "没测" in s["note"]          # 「0」与「没测」必须能区分（§5.5 铁律③）


def test_counter_gauge_and_text():
    from app.services import metrics
    metrics.incr("event.writes")
    metrics.incr("event.writes", by=2)
    assert metrics.snapshot()["event.writes"]["value"] == 3
    metrics.gauge("collector.read_fail_streak", 5)
    metrics.gauge("collector.read_fail_streak", 0)
    assert metrics.snapshot()["collector.read_fail_streak"]["value"] == 0
    metrics.text("vision.last_path", "/vision/detect")
    assert metrics.snapshot()["vision.last_path"]["value"] == "/vision/detect"


def test_metrics_never_raise_on_junk():
    """观测件绝不带崩主流程：垃圾输入只会被丢掉，不许抛。"""
    from app.services import metrics
    metrics.stat("modbus.read_pose_rt_ms", "abc")
    metrics.stat("modbus.read_pose_rt_ms", float("nan"))
    metrics.gauge("collector.read_fail_streak", None)
    metrics.incr("not.in.catalog")                 # 目录外的 key：拒收（埋点必须先进 CATALOG）
    metrics.text("collector.loop_ms", "x")         # kind 不符：拒收
    assert metrics.snapshot()["modbus.read_pose_rt_ms"]["value"] is None
    assert "not.in.catalog" not in metrics.snapshot()


# ---------------------------------------------------------------- 端点结构与只读口径
def _get_metrics(client):
    r = client.get("/api/system/metrics")
    assert r.status_code == 200, "★ 指标端点必须**无令牌可读**（观测不属于控制）"
    return r.json()


def test_metrics_endpoint_public_and_grouped(client):
    d = _get_metrics(client)
    g = d["groups"]
    for grp in ("collector", "modbus", "ws", "runs", "vision", "events"):
        assert grp in g
    assert d["uptime_sec"] >= 0


def test_every_entry_is_value_unit_ts_source(client):
    """四元组不变式：有 value 必有 unit/ts/source；null 必带如实 note。"""
    d = _get_metrics(client)
    for grp, items in d["groups"].items():
        for name, m in items.items():
            assert isinstance(m, dict), (grp, name)
            for k in ("value", "unit", "ts", "source"):
                assert k in m, (grp, name, k)
            if m["value"] is None:
                assert m.get("note"), (grp, name, "null 必须如实写明为什么没测/不适用")


def test_runs_group_reads_live_registry_and_queue_is_honestly_null(client):
    from app.api import control as control_api
    d = _get_metrics(client)
    runs = d["groups"]["runs"]
    assert runs["active_runs"]["value"] == len(control_api._RUN_ACTIVE)
    assert runs["active_run_ids"]["value"] == list(control_api._RUN_ACTIVE.keys())
    # 没有队列 = 如实 null + note 说明 run-file 是直接起线程，不许编 0
    assert runs["queue_len"]["value"] is None
    assert "没有队列" in runs["queue_len"]["note"]
    assert runs["thread_pool_tokens"]["value"] == 96 or \
        runs["thread_pool_tokens"]["unit"] == "个"


def test_ws_group_matches_hub(client):
    from app.services.hub import hub
    d = _get_metrics(client)
    ws = d["groups"]["ws"]
    assert ws["clients_total"]["value"] == hub.count()
    assert ws["clients_pose"]["value"] == hub.count("pose")
    assert ws["backlog_clients"]["value"] == hub.pending()


# ---------------------------------------------------------------- 埋点接线（真的记在唯一实现上）
class _FakeLoop:
    """假事件循环：call_soon_threadsafe 同步执行 → _drain 里 ensure_future 会炸，
    由 hub.broadcast 的既有 try/except 吞掉并注销客户端 —— 计时/计数仍要落账。"""
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class _FakeWS:
    async def send_text(self, data):
        pass


def test_hub_broadcast_records_latency_and_frames():
    from app.services import metrics
    from app.services.hub import hub
    ws = _FakeWS()
    hub.register(ws, _FakeLoop(), "pose")
    try:
        hub.broadcast({"type": "pose", "j1": 0})
        hub.broadcast({"type": "event", "id": 1})
        s = metrics.snapshot()
        # ★ 用 >=：会话级 client 夹具的采集线程也会广播（fake 客户端注册期间
        #   它会蹭到同一批计数）—— 这里钉的是「广播确实落账到 hub.broadcast」，
        #   不是钉全局精确值。
        assert s["ws.broadcast_frames"]["value"] >= 2
        assert s["ws.broadcast_enqueue_ms"]["count"] >= 2
    finally:
        hub.unregister(ws)


def test_hub_pending_counts_backlogged_clients():
    from app.services.hub import hub

    class _HoldLoop:
        def call_soon_threadsafe(self, fn, *args):
            pass     # 不入队 → latest 留在原地 = 积压

    ws = _FakeWS()
    hub.register(ws, _HoldLoop(), "pose")
    try:
        # ★ 不断言「广播前 pending==0」：采集线程也可能在我们注册的窗口里推一帧
        #   （它是合法的共同写者）。钉的是「广播之后积压必须可见 / 注销之后必须归零」。
        hub.broadcast({"type": "pose", "j1": 1})
        assert hub.pending() == 1
    finally:
        hub.unregister(ws)
    assert hub.pending() == 0


def test_modbus_read_failure_is_counted():
    """连接失败必须计数、且**不**进往返延迟样本（超时会冒充「正常延迟」）。"""
    from app.services import metrics
    from app.services.modbus import ModbusRobot
    mb = ModbusRobot()
    mb.host = "127.0.0.1"
    mb.port = 1
    mb.timeout = 0.2
    pose, err = mb.read_pose()
    assert pose is None and err
    s = metrics.snapshot()
    assert s["modbus.read_pose_fail"]["value"] == 1
    assert s["modbus.read_pose_rt_ms"]["value"] is None   # 失败不计延迟（不造假）


def test_vision_client_records_calls_and_failures():
    from app.services import metrics
    from app.services import camera_client
    st, _, err = camera_client.get_json("/vision/last", timeout=0.3,
                                        base="http://127.0.0.1:1")
    assert st == 0 and err
    s = metrics.snapshot()
    # ★ 用 >=：本机若真有相机服务在跑，摄入线程是合法共同写者会并发调用。
    #   「失败不进往返样本」这条语义由 `_request` 包装层保证（st==0 只 incr fail），
    #   不在这里钉 rt —— 钉了就会因并发成功调用而随机红。
    assert s["vision.http_calls"]["value"] >= 1
    assert s["vision.http_fail"]["value"] >= 1
    assert s["vision.last_error"]["measured"] is True


def test_events_emit_increments_writes(client):
    from app.services import metrics
    from app.services.events import emit
    before = metrics.snapshot()["event.writes"]["value"] or 0
    emit("system", "info", "test.metrics_emit", "指标回归用事件", broadcast=False)
    after = metrics.snapshot()["event.writes"]["value"]
    # ★ 用 >=：本机环境里视觉摄入线程也可能并发 emit（合法共同写者），
    #   钉的是「我们这条一定落账」，不钉全局精确值。
    assert after >= before + 1
    assert metrics.snapshot()["event.write_ms"]["measured"] is True
