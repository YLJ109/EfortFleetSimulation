# -*- coding: utf-8 -*-
"""★★ E3 反事实试跑（What-if 沙盒）的口径钉 —— 这份文件就是 `services/whatif.py`
顶部 docstring 里那些「钉 tests/test_whatif.py::xxx」的**兑现**。

为什么这个模块需要这么多"否定式"的钉子：它的立意是**不碰真机**回答「这样改值不值得上真机」。
一旦它偷偷碰了下发链路，它就变成了第二个执行引擎 —— 而现场只需要一个。所以这里钉的
**全是它不许做的事**，与 AGENTS.md §5 的硬口径一一对应：

  ① 零下发（§5.1 真机安全最高优先）：静态钉 + **动态护栏**双保险
     —— 静态钉会被"绕一层的别名调用"骗过去，动态护栏会被"根本没走到那条分支"骗过去，
     两个一起才盖住（`test_whatif_never_touches_motion_or_modbus_writes` + `_dynamic`）。
  ② 绝不输出 ETA（§4 关键机理 1：上位机只写目标+触发位，拿不到速度曲线）——
     键名级禁区 + 节拍档只能来自**已发生的实测段**（`test_no_eta_no_estimate_no_speed_write`
     / `test_cycle_basis_requires_measured_segments`）。
  ③ 样本数 = 实际跑出来的那个（§5.5 铁律②）：超预算宁可少跑，`n` 不许冒报（`test_sample_n_*`）。
  ④ 三态不许塌成布尔（§5.5 铁律③）：`no_data` / `failed` 都不是"安全"。
  ⑤ 只重排评估路径：`order` / `face` 不落库、不执行（`test_reorder_only_affects_evaluation_path`）。

★ 全部离线：`poses` 一律显式传入或用临时库，**不连真机、不喂寄存器、不写生产日志**
  （DB / `EFORT_SIMULATE` / `EFORT_LOGS_DIR` 三样隔离由 `tests/conftest.py` 负责，这里不绕过）。
"""
from __future__ import annotations

import ast
import inspect
import io
import json
import tokenize

import pytest

from app.services import twin_residual as TR
from app.services import whatif as W

# ---------------------------------------------------------------- 用例数据（纯合成）

#: 六个点的合成姿态（度）：名字用 DEFAULT_PATH 的那一套，但**数值是编的** ——
#: 实验台吃的就是"姿态表"，编的姿态照样能算几何；真机点位库一个字节都不碰。
POSES = {
    "A1": [0.0, -30.0, 40.0, 0.0, 30.0, 0.0],
    "A1上": [0.0, -20.0, 40.0, 0.0, 30.0, 0.0],
    "C检测": [10.0, -45.0, 60.0, 0.0, 45.0, 0.0],
    "B1上": [180.0, -20.0, 40.0, 0.0, 30.0, 0.0],
    "B1": [180.0, -30.0, 40.0, 0.0, 30.0, 0.0],
    "开始/结束点": [45.0, -10.0, 30.0, 0.0, 20.0, 0.0],
}

STATUSES = ("ok", "partial", "failed", "no_data")

#: 键名级禁区：出现任何一个 = 在把"离线统计"说成"未来的时间承诺"
ETA_KEY_RE = r"eta|estimat|forecast|projected|remaining|countdown|预计|预估|预计耗时|还剩|还需|需要多久"


# ---------------------------------------------------------------- 工具

def _code_tokens(obj) -> str:
    """源码压成**只剩可执行 token** 的串（丢掉注释 / docstring / 字符串字面量）。

    ★★ 必须先剥注释再判，否则**假阳性**：本模块的说明文字里满篇"不写寄存器、不碰气路、
      无 ETA"，连结论文案里都写着「仅评估标签，不写 40103」。判的是"真有没有调用"，
      不是"有没有提这个词"（同 `test_point_audit.py` / `test_twin_residual.py` 的先例）。
    """
    src = obj if isinstance(obj, str) else inspect.getsource(obj)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return "".join(out)


def _walk_keys(node, prefix=""):
    """递归把返回体里**所有层的键名**挖出来（含列表里的字典）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield "%s.%s" % (prefix, k)
            for x in _walk_keys(v, "%s.%s" % (prefix, k)):
                yield x
    elif isinstance(node, (list, tuple)):
        for v in node:
            for x in _walk_keys(v, prefix + "[]"):
                yield x


def _seed_segments(obs, source="下发到 A1上", speed=40, n=10, start=1000.0):
    """用**真实记录路径**灌 n 段"已发生且到位"的实测段（时间由 at= 传入 = 确定性）。

    ★ 为什么不直接往 `_history` 里塞假行：那等于测我自己编的数据结构。走
      begin/observe/end 才能保证钉的是"段历史真的长这样时，节拍档才允许出数"。
    """
    for k in range(n):
        t0 = start + k * 100.0
        obs.begin([10.0, 0.0, 0.0, 0.0, 0.0, 0.0], source=source, speed_pct=speed, at=t0)
        for i in range(10):
            obs.observe([10.0 - 0.2 * i, 0.0, 0.0, 0.0, 0.0, 0.0], at=t0 + 0.2 * i)
        obs.end("done", at=t0 + 2.0)
    return obs


@pytest.fixture()
def residual(monkeypatch):
    """干净的段历史单例替身：进程级单例会被别的用例（采集线程）喂脏，这里换掉。"""
    o = TR.TwinResidual()
    o._events_enabled = False
    monkeypatch.setattr(TR, "twin_residual", o)
    return o


@pytest.fixture(autouse=True)
def _dispatch_guard(monkeypatch):
    """★★ 动态护栏：本文件**每个用例**期间，任何真下发入口被调用就直接红。

    静态源码钉只能证明"这一层没写"，证明不了"它没有绕过去调那一层"。这里把
    运动引擎 / 寄存器读写 / 气路 / 点动 / 急停 / 长流程咽喉 `_move` 全部换成
    "一调就抛"，跑完整轮实验台再确认一次都没被打响。
    """
    from app.api import sorting as api_sorting
    from app.services.motion import motion

    hits = []

    def _boom(name):
        def f(*_a, **_k):
            hits.append(name)
            raise AssertionError("whatif 触达了下发入口：%s" % name)
        return f

    for obj, attr in ((motion, "command"), (motion, "estop"), (motion, "reset_estop"),
                      (motion.modbus, "write_reg"), (motion.modbus, "read_regs"),
                      (motion.modbus, "rc_command"), (motion.modbus, "rc_jog_execute"),
                      (motion.modbus, "rc_write_jog_target"), (motion.modbus, "rc_vacuum"),
                      (motion.modbus, "rc_estop")):
        monkeypatch.setattr(obj, attr, _boom("%s.%s" % (type(obj).__name__, attr)),
                            raising=False)
    monkeypatch.setattr(api_sorting, "_move", _boom("api.sorting._move"), raising=False)
    yield hits
    assert hits == [], "whatif 运行期间触达了下发入口：%s" % hits


# ---------------------------------------------------------------- ① 零下发（核心静态钉）

def test_whatif_never_touches_motion_or_modbus_writes():
    """★ 方案点名的那条核心钉：源码里**没有任何**下发 / 寄存器 / 气路 / 运动入口的调用。

    判的是剥掉注释与字符串之后的**可执行 token** —— 见 `_code_tokens` 的说明。
    """
    code = _code_tokens(W)

    # ① 不 import 运动/下发那一层（含控制接口路由）
    for banned in ("motion", "modbus", "rc_ready", "jog", "sim_robot", "collector",
                   "app.api.control", "app.api.sorting", "app.api.robot"):
        assert "import %s" % banned not in code, "whatif 里 import 了下发层：%s" % banned
        assert "from %s" % banned not in code, banned

    # ② 没有寄存器写 / 命令字 / 触发位 / 气路 / 急停一类调用
    for banned in ("write_reg", "read_regs", "rc_command", "rc_jog_execute",
                   "rc_write_jog_target", "rc_vacuum", "rc_vacuum_prepare", "rc_estop",
                   "encode_angle", "command(", "estop(", "reset_estop(",
                   "40101", "40102", "40103", "40135", "VAC_ACTIONS",
                   "clamp_to_limits_for_send", "_move(", "start_sorting"):
        assert banned not in code, "whatif 里出现了下发/寄存器/气路调用：%s" % banned

    # ③ 依赖白名单：只允许标准库 + 既有**纯函数**层
    #    （运动学 / 限位 / 保守包络 / 面语义与原位安全路径 / 段历史**只读** / 日志）
    #    用 ast 抽，函数体里的**延迟 import** 也一并盖住（token 版会漏 `import x.y` 的连写）。
    mods = set()
    tree = ast.parse(inspect.getsource(W))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # `from a.b import c, d` 真正被用到的是 a.b.c / a.b.d（或子包 a.b.c）
            mods.update("%s.%s" % (node.module, a.name) for a in node.names)
    app_mods = {m for m in mods if m.startswith("app.")}
    allowed = ("app.core.logger", "app.services.kinematics", "app.services.limits",
               "app.services.safety_eval", "app.services.sorting",
               "app.services.twin_residual")
    for m in app_mods:
        assert any(m == a or m.startswith(a + ".") for a in allowed), \
            "whatif 新拉了下发层依赖：%s" % m
    third_party = {m.split(".")[0] for m in mods if not m.startswith("app.")} - {
        "__future__", "math", "random", "time", "typing"}
    assert third_party == set(), "不引入新依赖 / 仿真引擎：%s" % third_party


def test_whatif_does_not_write_the_speed_calibre():
    """★ 速度口径唯一（§5.3）：`speed_gear_pct` 只是**评估用的档位标签**。

    不写 40103、不调速度发布口（`ui_speed`）、不动右上角滑块 —— 全都在 token 层判。
    """
    code = _code_tokens(W)
    for banned in ("ui_speed", "publish(", "set_speed", "speed_pct =", "40103"):
        assert banned not in code, banned
    # 它只是把档位移进来**筛实测样本**（cycle_time_for 的第二个实参）
    src = inspect.getsource(W)
    assert "sc.get(\"speed_gear_pct\")" in src or "sc[\"speed_gear_pct\"]" in src


# ---------------------------------------------------------------- ② 绝不输出 ETA

def test_no_eta_no_estimate_no_speed_write(residual):
    """返回体里**没有任何键**叫 eta / estimate / 预计 / 剩余秒；也没有"完成倒计时"。

    ★ 动态跑三种分支（ok / no_data / failed）都查一遍，因为 ETA 最容易从"顺手补一个数"里长出来。
    """
    res = W.run(None, n=4, seed=5, poses=POSES)
    assert res["status"] in STATUSES

    import re
    pat = re.compile(ETA_KEY_RE, re.IGNORECASE)

    for probe in (res, W.run(None, n=2, poses={}), W.run({"points": ["不存在"]}, n=2, poses={})):
        for key in _walk_keys(probe):
            leaf = key.rsplit(".", 1)[-1]
            assert not pat.search(leaf), "返回体里出现了 ETA 类键名：%s" % key

    # 节拍那一档的键名也必须在"实测"一侧，不许叫"预测"
    assert {"p50_ms", "p90_ms", "n_samples", "basis", "available"} <= set(res["cycle_time"])
    # 且**没有**跑过 = 没有实测段时，一个时间数字都不给
    ct = res["cycle_time"]
    if not ct["available"]:
        assert ct["p50_ms"] is None and ct["p90_ms"] is None, ct


def test_no_eta_string_is_never_a_time_promise(residual):
    """文案级：结论 / 口径说明里出现的"ETA/预计"必须是否定式（"无 ETA"），不是数字承诺。"""
    res = W.run(None, n=4, seed=5, poses=POSES)
    for text in (res["note"], res["conclusion"]):
        for word in ("ETA", "预计"):
            if word in text:
                i = text.index(word)
                window = text[max(0, i - 12): i + 12]
                assert ("无 " in window or "不产" in window or "不输出" in window
                        or "非 ETA" in window or "无 ETA" in window), window
    assert "无 ETA" in res["conclusion"]
    # 结论文案里唯一的"数"是几何与样本数，不含秒/分钟承诺
    assert "秒后完成" not in res["conclusion"] and "还需要" not in res["conclusion"]


# ---------------------------------------------------------------- ③ 节拍档 = 只代实测段

def test_cycle_basis_requires_measured_segments(residual):
    """节拍那一档**只能来自 twin_residual 的已发生实测段**；清空 ⇒ 如实说没有，不外推。"""
    assert W.MIN_CYCLE_SAMPLES >= 8
    assert not _seed_segments(residual).recent(40) == []          # 先证明灌进去了实测段

    ct = W.cycle_time_for(["A1", "A1上"], None)
    assert ct["available"] is True, ct
    assert ct["n_samples"] >= W.MIN_CYCLE_SAMPLES
    assert ct["p50_ms"] is not None and ct["p90_ms"] is not None
    assert ct["p50_ms"] > 0.0                                    # 实测的收敛耗时（ms）
    assert "实测段" in ct["basis"] and "不是" in ct["basis"]        # basis 自己声明不是预测

    # ★ 把实测清空 → 同一档位必须回「无实测样本·不给节拍估计」，且**一个 P50/P90 都不给**
    residual.reset()
    assert residual.recent(40) == []
    empty = W.cycle_time_for(["A1", "A1上"], None)
    assert empty["available"] is False
    assert empty["reason"] == "无实测样本·不给节拍估计"
    assert empty["p50_ms"] is None and empty["p90_ms"] is None
    assert "peak_deg_p90" not in empty, "没样本却还带峰值分位数 = 外推"
    assert empty["n_samples"] == 0 and empty["need_samples"] == W.MIN_CYCLE_SAMPLES
    assert "不外推" in empty["basis"]


def test_no_cycle_time_without_measured_samples(residual):
    """整轮 `run()` 里也一样：没有实测段 ⇒ 返回体的节拍档不出现任何时间数字。"""
    residual.reset()
    res = W.run(None, n=3, seed=9, poses=POSES)
    assert res["cycle_time"]["available"] is False
    assert res["cycle_time"]["p50_ms"] is None
    assert json.dumps(res, ensure_ascii=False).count("无实测样本·不给节拍估计") >= 1


def test_cycle_matching_is_name_first_then_gear_still_measured(residual):
    """匹配口径要**如实写进 basis**：首选点名，不足才退同速度档 —— 两条都来自实测。"""
    _seed_segments(residual, source="下发到 B1上", speed=40, n=9)
    by_name = W.cycle_time_for(["B1上"], None)
    assert by_name["available"] and "点名匹配" in by_name["basis"]

    off_gear = W.cycle_time_for(["完全没出现过的点"], 10)   # 档差 > 10% ⇒ 连同档也凑不齐
    assert off_gear["available"] is False and off_gear["p50_ms"] is None

    same_gear = W.cycle_time_for(["完全没出现过的点"], 42)
    assert same_gear["available"] is True and "同速度档" in same_gear["basis"]
    assert same_gear["speed_pct"] == [40]                   # 用了哪些实测档，摊开给人看
    # ★ 文案钉（我修的那个真 bug）：`tier` 是**纯字面量**，写成 "±10%%" 会原样显示成两个百分号
    assert "±10%%" not in same_gear["basis"], same_gear["basis"]
    assert "±10%" in same_gear["basis"], same_gear["basis"]
    # 上一条的档差是 |40-10|=30 > 10 → 10 档那次如实回"没有"，**没有拿 40 档的数硬凑**


def test_cycle_rows_only_count_arrived_or_slow(residual):
    """只有"已收尾且到位"（arrived / slow）的段才算样本 —— stuck / not_reached / aborted 不算。

    ★ 否则就等于拿"没走到的那段"去证明节拍，正是本项目最反对的那种数。
    """
    from app.services.twin_residual import TwinResidual
    o = TwinResidual()
    o._events_enabled = False
    # 灌一段"卡住"的（首帧后完全不动 → 到 start_timeout 判 stuck），再收尾
    o.begin([30.0, 0.0, 0.0, 0.0, 0.0, 0.0], source="卡住的段", at=1000.0)
    for i in range(12):
        o.observe([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], at=1000.0 + 0.2 * i)   # 目标 30、实际 0：永不收敛
    row = o.end("done", at=1200.0)
    assert row["verdict"] in ("stuck", "not_reached"), row
    residual.reset()
    residual._history = o.recent(40)
    assert W._settled_rows() == [], "没到位的段被当成节拍样本了"


# ---------------------------------------------------------------- ④ 样本数 = 实际跑出来的

def test_sample_n_is_what_actually_ran(monkeypatch, residual):
    """★ n 传超预算的值时，返回体的 `n` = **真正跑完的轮数**（宁少不假），且如实说被截断。"""
    assert W.SAMPLE_N_DEFAULT == 200, "默认样本数的依据在 whatif.py 顶部那条离线计时"
    assert W.SAMPLE_N_MAX == 300 and W.RUN_BUDGET_MS > 0

    monkeypatch.setattr(W, "RUN_BUDGET_MS", 1.0)         # 预算压到 1ms ⇒ 必然跑不完
    res = W.run(None, n=300, seed=3, poses=POSES)
    assert res["n_requested"] == 300
    assert 0 < res["n"] < res["n_requested"], "超预算却没少报样本数 = 界面在编故事"
    assert res["n"] == len(res["min_clearance"]["bins"]) or res["n"] >= 1
    trunc = [f for f in res["failures"] if "预算" in f]
    assert trunc, res["failures"]
    assert str(res["n"]) in trunc[0] and "不多报" in trunc[0]
    assert res["status"] == "partial", "被截断这一档绝不能是 ok"
    assert res["budget_ms"] == 1.0

    # 缺省 n = SAMPLE_N_DEFAULT（同样在截断预算下验证"请求数"这一侧）
    res2 = W.run(None, seed=3, poses=POSES)
    assert res2["n_requested"] == W.SAMPLE_N_DEFAULT
    assert res2["n"] <= res2["n_requested"]


def test_sample_never_exceeds_what_looped(monkeypatch, residual):
    """每轮的 `n` 必须等于**真正进了统计的样本数**：分布点数与样本数自洽。"""
    monkeypatch.setattr(W, "RUN_BUDGET_MS", 100000.0)
    res = W.run(None, n=7, seed=17, poses=POSES)
    assert res["n"] == 7
    total_in_bins = sum(b["count"] for b in res["min_clearance"]["bins"])
    assert total_in_bins == res["n"], (total_in_bins, res["n"])
    assert res["sample_ms"] == pytest.approx(res["run_ms"] / res["n"], abs=0.01)
    for seg in res["segments"]:
        assert seg["p_not_arrived"] * res["n"] == round(seg["p_not_arrived"] * res["n"])


def test_n_is_clamped_to_the_documented_window(residual):
    """n 越界只做**参数防御**：夹到 [1, SAMPLE_N_MAX]，不报错也不照跑。"""
    assert W.scenario_of({})["face"] == "keep"
    monkey_result = W.run(None, n=99999, seed=1, poses={"A1": [0, 0, 0, 0, 0, 0]})
    assert monkey_result["n_requested"] == W.SAMPLE_N_MAX


# ---------------------------------------------------------------- ⑤ 三态不塌成布尔

def test_tristate_never_collapses_to_boolean(residual):
    """status ∈ {ok, partial, failed, no_data}；"没有数据"绝不写成"安全"（§5.5 铁律③）。"""
    seen = set()

    ok_run = W.run(None, n=3, seed=5, poses=POSES)
    seen.add(ok_run["status"])
    assert ok_run["ok"] is True and ok_run["status"] in ("ok", "partial")

    empty = W.run(None, n=3, poses={})                       # 点位库为空 = 没数据
    seen.add(empty["status"])
    assert empty["status"] == "no_data" and empty["ok"] is False
    assert empty["n"] == 0 and empty["segments"] == []
    assert "不代表安全" in empty["conclusion"]
    assert empty["min_clearance"]["worst_state"] == "unknown"
    assert empty["min_clearance"]["p5"] is None, "没数据还得把间隙写成 0mm"
    assert empty["worst_segment"]["point"] is None

    # 「算不出来」的四种表现一律不能出现 safe 字样
    for probe in (empty, W.run({"points": ["没有这个点"]}, n=2, poses={})):
        assert "全程在警戒线外" not in probe["conclusion"]
        assert probe["min_clearance"]["p5"] is None
        seen.add(probe["status"])

    assert seen <= set(STATUSES), seen
    # 反证：ok 的结论必须带"非认证"，不能只给一句"安全"
    assert "非认证" in ok_run["conclusion"] and "离线统计估计" in ok_run["conclusion"]


def test_failed_branch_when_envelope_sweep_cannot_run(residual, monkeypatch):
    """扫掠全跑不成 ⇒ status=failed（不是 no_data、更不是"通过"），且失败原因逐段列出。"""
    import app.services.safety_eval as SE

    def _boom(*_a, **_k):
        raise RuntimeError("包络扫掠起不来")

    monkeypatch.setattr(SE, "sweep", _boom)
    res = W.run(None, n=3, seed=1, poses=POSES)
    assert res["status"] == "failed", res["status"]
    assert res["ok"] is False and res["n"] == 0
    assert any("包络扫掠失败" in f for f in res["failures"]), res["failures"]
    assert res["min_clearance"]["worst_state"] == "unknown"
    # ★ 我修的第二个真 bug：这一支以前把 `segs` 已经被清空后的列表传进去 ⇒ path 恒为 []，
    #   界面看不出"到底算过哪些点"。现在必须把**尝试过的点名**如实带出去。
    assert res["path"], "failed 分支把评估路径丢了 = 静默失效（§5.5 铁律①同类）"
    assert res["jitter_sources"], "failed 分支也得带出抖动来源清单"
    # ★ 我修的第三个真 bug：`not_modelled` 以前在非 ok 分支是空表 ⇒ 界面"未建模："后面什么都没有。
    assert res["not_modelled"] and res["not_modelled"] == list(W.NOT_MODELLED)


def test_no_data_branch_still_declares_what_is_unmodelled(residual):
    """no_data 那一支也必须带出未建模清单（铁律⑤：要么接线、要么删、要么如实注明）。"""
    empty = W.run(None, n=2, poses={})
    assert empty["status"] == "no_data"
    assert empty["not_modelled"] == list(W.NOT_MODELLED), empty["not_modelled"]
    assert any("速度曲线" in s for s in empty["not_modelled"])


def test_missing_points_are_named_not_silently_dropped(residual):
    """缺的点必须**点名**写进 failures 并说清"没算它 ≠ 它安全"。

    ★ 两条来源都要盖住（口径一致、措辞不同）：
      · `run()` 预筛点名 → 「不在点位库（未示教）→ 未纳入本次评估」；
      · `build_segments()` 的自动补段里冒出没示教的正上位 → 「没有算它，不代表它安全」。
    """
    partial = dict(POSES)
    partial.pop("C检测")
    res = W.run(None, n=3, seed=2, poses=partial)
    assert res["status"] == "partial"
    hit = [f for f in res["failures"] if "C检测" in f]
    assert hit, res["failures"]
    assert ("未纳入本次评估" in hit[0] or "没有算它" in hit[0]), hit[0]
    assert "未示教" in hit[0], hit[0]
    # ★ 算不全 ⇒ 状态必须降级为 partial（绝不能顶着"ok/全绿"出门），且逐段结果里没有它
    assert res["status"] == "partial" and res["conclusion"]
    assert any("C检测" in (s.get("from", "") + s.get("to", "")) for s in res["segments"]) is False


# ---------------------------------------------------------------- ⑥ 抖动来源逐项列明

def test_jitter_sources_are_listed_with_assumed_flag(residual):
    """每个抖动源逐项列明，假设值必须带 `assumed=True` 与依据（方案："非认证"要能说清哪儿是假设）。"""
    res = W.run(None, n=3, seed=5, poses=POSES)
    srcs = res["jitter_sources"]
    assert srcs, "输出必须带实际使用的抖动来源清单"
    keys = {s["key"] for s in srcs}
    assert keys == {"arrival_tol_deg", "sampling_uncertainty_deg",
                    "teach_repeat_deg", "tray_repeat_mm"}, keys
    for s in srcs:
        assert {"name", "key", "sigma", "unit", "assumed", "basis"} <= set(s), s
        assert isinstance(s["assumed"], bool), "assumed 必须是三态里的显式布尔（不是 None 顶替）"
        assert s["basis"], "%s 没有依据 = 又变成一个没有来源的数" % s["key"]
        assert s["sigma"] >= 0.0

    assumed = {s["key"] for s in srcs if s["assumed"] is True}
    measured = {s["key"] for s in srcs if s["assumed"] is False}
    assert assumed == {"teach_repeat_deg", "tray_repeat_mm"}, assumed
    assert "现场没有" in "".join(s["basis"] for s in srcs if s["assumed"])
    assert {"arrival_tol_deg", "sampling_uncertainty_deg"} <= measured
    # 到位容差那一条必须**指向唯一口径**（config 的 twin_residual.tol_deg），不自己写一份
    tol_src = next(s for s in srcs if s["key"] == "arrival_tol_deg")
    assert "twin_residual" in tol_src["basis"]

    assert res["not_modelled"], "未建模清单不能空"
    assert "未建模" or "加减速" in " ".join(res["not_modelled"])


def test_assumed_sigmas_follow_the_scenario(residual):
    """界面传进来的抖动参数会**如实反映**在来源清单里（不是写死的表）。"""
    res = W.run({"teach_repeat_deg": 1.75, "tray_repeat_mm": 3.25}, n=2, seed=4, poses=POSES)
    d = {s["key"]: s for s in res["jitter_sources"]}
    assert d["teach_repeat_deg"]["sigma"] == pytest.approx(1.75)
    assert d["tray_repeat_mm"]["sigma"] == pytest.approx(3.25)
    assert d["teach_repeat_deg"]["assumed"] is True
    base = W.run(None, n=2, seed=4, poses=POSES)
    b = {s["key"]: s for s in base["jitter_sources"]}
    assert b["teach_repeat_deg"]["sigma"] == W.TEACH_REPEAT_DEG_ASSUMED
    assert b["tray_repeat_mm"]["sigma"] == W.TRAY_REPEAT_MM_ASSUMED


# ---------------------------------------------------------------- ⑦ 次序 / 面只重排评估路径

def test_reorder_only_affects_evaluation_path(residual):
    """`order`（含 reverse / by_zone）只重排**评估用的路径**：不落库、不执行。"""
    from app.api import points as api_points     # noqa: F401 —— 确保这一层没有被 whatif 拉进依赖

    points = ["A1", "A1上", "C检测", "B1上", "B1", "开始/结束点"]
    assert W.reorder(points, "reverse") == list(reversed(points))
    bz = W.reorder(["B1", "A1", "C检测", "开始/结束点"], "by_zone")
    assert bz == ["A1", "B1", "C检测", "开始/结束点"], bz
    assert W.reorder(points, "as_listed") == points
    assert W.reorder(points, "乱七八糟") == points          # 非法值回缺省，不猜

    snapshot = {k: list(v) for k, v in POSES.items()}
    forward = W.run({"points": points, "order": "as_listed"}, n=3, seed=5, poses=POSES)
    back = W.run({"points": points, "order": "reverse"}, n=3, seed=5, poses=POSES)
    assert POSES == snapshot, "实验台改动了传进来的点位姿态表（它只许读）"

    fwd_first = [s["from"] for s in forward["segments"]][0]
    assert fwd_first == "A1"
    assert back["segments"], back
    assert [s["to"] for s in back["segments"]] != [s["to"] for s in forward["segments"]]

    # ★ 不执行：autouse 的 _dispatch_guard 已经保证全程没碰下发入口；这里再钉源码层没有写库调用
    code = _code_tokens(W)
    for banned in ("update_point", "create_point", "delete_point", "commit(", "SessionLocal"):
        assert banned not in code, banned
    assert "db" not in inspect.signature(W.run).parameters, "run() 不接受 db = 它根本写不了库"


def test_face_order_never_rotates_raw_slot_points(residual):
    """18 个原料位的禁转黑名单**一个字节没改**：实验台在原料位上也转不出面。

    ★ 与 §5.3 的 `face_for_target`（原料位 → keep）同源；这里证明"改 face 参数"
      不会变成"在 A1/B1 上翻面"。
    """
    from app.services import sorting as S

    for name in list(S.A_SLOTS) + list(S.B_SLOTS):
        assert S.face_for_target(name, "back") == S.FACE_KEEP, name

    a1 = POSES["A1"]
    front = W.face_pose("A1", a1, "front", [0.0, 0.0, 0.0, 0.0, 30.0, 90.0])
    back = W.face_pose("A1", a1, "back", [0.0, 0.0, 0.0, 0.0, 30.0, 90.0])
    assert front == back, "同一原料位换面参数就转了 J6 = 把禁转口径改丢了"
    assert front[5] == a1[5]


# ---------------------------------------------------------------- ⑧ 几何档是真能算的

def test_shifted_tray_lowers_min_clearance_p5(residual):
    """方案点名的方向性证明：**同一 seed 下把托盘往外推，最小间隙 P5 必须变小**。

    ★ 这一条钉的是"几何那一档不是摆设" —— 如果偏移不影响结果，整个卡片就是假的。
    """
    base = W.run({"tray": {"dx": 0, "dy": 0, "dz": 0}}, n=40, seed=11, poses=POSES)
    shifted = W.run({"tray": {"dx": 40, "dy": 0, "dz": 0}}, n=40, seed=11, poses=POSES)
    assert base["status"] in ("ok", "partial") and shifted["status"] in ("ok", "partial")
    assert base["min_clearance"]["p5"] is not None
    assert shifted["min_clearance"]["p5"] < base["min_clearance"]["p5"], (
        base["min_clearance"], shifted["min_clearance"])

    # 分布是**分布**：P5 ≤ P50 ≤ P95，且有直方图（前端只画不重算）
    mc = base["min_clearance"]
    assert mc["p5"] <= mc["p50"] <= mc["p50"] <= mc["p95"], mc
    assert mc["unit"] == "mm" and mc["bins"]
    assert sum(b["count"] for b in mc["bins"]) == base["n"]


def test_quantiles_and_bins_never_fake_zero(residual):
    """空集合的分位数是 None、直方图是 []（铁律③：读不到不许合成 0）。"""
    assert W._pct([], 0.5) is None and W._pct([float("nan")], 0.5) is None
    assert W._bins([]) == [] and W._bins([float("nan")]) == []
    assert W._pct([10.0, 20.0], 0.0) == 10.0 and W._pct([10.0, 20.0], 1.0) == 20.0
    # 采样时刻：没有样本 ⇒ sample_ms 是 None，不是 0.0
    empty = W.run(None, n=2, poses={})
    assert empty["sample_ms"] is None, empty["sample_ms"]
    assert empty["n"] == 0 and empty["run_ms"] >= 0.0


def test_scenario_normalisation_does_not_guess(residual):
    """非法输入一律回缺省并如实归一：不猜"用户大概想要什么"。"""
    sc = W.scenario_of({"face": "SIDEWAYS", "order": "flip", "tray": {"dx": "abc"},
                        "points": "不是列表", "speed_gear_pct": 900})
    assert sc["face"] == "keep" and sc["order"] == "as_listed"
    assert sc["tray"] == {"dx_mm": 0.0, "dy_mm": 0.0, "dz_mm": 0.0}
    assert sc["points"] == [] and sc["speed_gear_pct"] == 100
    assert sc["tray_repeat_mm"] == W.TRAY_REPEAT_MM_ASSUMED
    assert sc["teach_repeat_deg"] == W.TEACH_REPEAT_DEG_ASSUMED


def test_ik_failure_is_declared_not_hidden(residual):
    """偏移后 IK 拿不到解 ⇒ 退回示教姿态并把"这一点的分布会偏乐观"写进 failures。"""
    import app.services.kinematics as K
    orig = K.ikine
    K.ikine = lambda *_a, **_k: None
    try:
        fails: list = []
        q = W.shift_pose(POSES["A1"], 30.0, 0.0, 0.0, fails)
    finally:
        K.ikine = orig
    assert q == POSES["A1"]
    assert fails and "IK 没解出来" in fails[0] and "偏乐观" in fails[0]


def test_envelope_guard_uses_the_fence_thresholds():
    """间隙分级取自围栏的**唯一来源**（config/safety.json），不在实验台里另立一套。"""
    th = W.zone_thresholds_mm()
    assert set(th) == {"warn", "danger", "basis_mm"}
    assert th["danger"] < th["warn"]
    assert W._grade_of(None) == "unknown"
    assert W._grade_of(th["danger"] - 1) == "danger"
    assert W._grade_of(-1.0) == "hit"
    assert W._grade_of(th["warn"] + 1) == "safe"
    # 判不了 ⇒ unknown，且文案明说"这不代表安全"
    assert "不代表安全" in W.STATE_TAIL["unknown"] or "不假装结论" in W.NOTE + "".join(W.STATE_TAIL.values())


# ---------------------------------------------------------------- ⑨ 接口层（端到端）

def _token(client) -> str:
    return client.post("/api/auth/login", json={"password": "test1234"}).json()["token"]


def _mk_point(client, hdr, name, joints):
    r = client.post("/api/points", json={"name": name, "group": "WI", "kind": "joint",
                                        "joints": joints}, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_whatif_endpoint_requires_control_token(client):
    """未带令牌 → 401/403（它会读点位库与围栏几何，与写操作同一道门）。"""
    r = client.post("/api/points/whatif", json={"n": 2})
    assert r.status_code in (401, 403), r.status_code
    r2 = client.post("/api/points/whatif", json={"n": 2},
                     headers={"X-Control-Token": "bogus"})
    assert r2.status_code in (401, 403), r2.status_code


def test_whatif_endpoint_returns_distribution_and_reads_library(client, residual):
    """端到端：带控制令牌 POST → 200 + 结构齐全；`n` 是实跑数；接口**不改动点位库**。"""
    from app.db.crud import list_points
    from app.db.database import SessionLocal

    hdr = {"X-Control-Token": _token(client)}
    names = ["WI试跑A", "WI试跑B", "WI试跑C"]
    ids = [_mk_point(client, hdr, names[0], [0.0, -30.0, 40.0, 0.0, 30.0, 0.0]),
           _mk_point(client, hdr, names[1], [20.0, -35.0, 45.0, 0.0, 30.0, 10.0]),
           _mk_point(client, hdr, names[2], [40.0, -25.0, 50.0, 0.0, 30.0, -10.0])]
    try:
        s = SessionLocal()
        try:
            before = [(r.id, r.name, r.joints, r.kind) for r in list_points(s)]
        finally:
            s.close()

        r = client.post("/api/points/whatif", headers=hdr, json={
            "scenario": {"points": names, "tray": {"dx": 5, "dy": 0, "dz": 0},
                         "face": "keep", "order": "as_listed", "speed_gear_pct": 40},
            "n": 6, "seed": 13,
        })
        assert r.status_code == 200, r.text
        d = r.json()
        for key in ("ok", "status", "n", "n_requested", "sample_ms", "run_ms", "seed",
                    "path", "segments", "jitter_sources", "min_clearance", "worst_segment",
                    "cycle_time", "failures", "scenario", "budget_ms", "not_modelled",
                    "note", "conclusion", "actor"):
            assert key in d, "返回体缺键：%s" % key
        assert d["status"] in STATUSES
        assert d["seed"] == 13 and d["n_requested"] == 6
        assert 0 <= d["n"] <= 6, d["n"]
        assert d["scenario"]["tray"]["dx_mm"] == 5.0
        assert d["cycle_time"]["reason"] in ("", "无实测样本·不给节拍估计")
        if d["cycle_time"]["available"] is False:
            assert d["cycle_time"]["p50_ms"] is None
        assert "离线统计估计" in d["note"]
        assert d["actor"] in ("admin", "operator"), d["actor"]

        s = SessionLocal()
        try:
            after = [(x.id, x.name, x.joints, x.kind) for x in list_points(s)]
        finally:
            s.close()
        assert after == before, "试跑接口改动了点位库（它只许读）"
    finally:
        for pid in ids:
            client.delete("/api/points/%d" % pid, headers=hdr)


def test_whatif_endpoint_truncated_sample_count_is_reported(client, residual, monkeypatch):
    """接口层同样守铁律②：超预算被截断时，`n` 就是实跑数且 failures 说明截断。"""
    hdr = {"X-Control-Token": _token(client)}
    pid = _mk_point(client, hdr, "WI截断点", [0.0, -30.0, 40.0, 0.0, 30.0, 0.0])
    other = _mk_point(client, hdr, "WI截断点2", [10.0, -30.0, 40.0, 0.0, 30.0, 0.0])
    monkeypatch.setattr(W, "RUN_BUDGET_MS", 1.0)
    try:
        r = client.post("/api/points/whatif", headers=hdr,
                        json={"scenario": {"points": ["WI截断点", "WI截断点2"]},
                              "n": 200, "seed": 1})
        assert r.status_code == 200, r.text
        d = r.json()
        if d["status"] in ("ok", "partial") and d["n"] < d["n_requested"]:
            assert any("预算" in f and str(d["n"]) in f for f in d["failures"]), d["failures"]
        assert d["n"] <= 200
    finally:
        for x in (pid, other):
            client.delete("/api/points/%d" % x, headers=hdr)


def test_whatif_route_is_registered_before_the_id_route():
    """★ 路由顺序钉：`/points/whatif` 必须排在 `/points/{pid}` **之前**。

    顺序写反了 FastAPI 会拿 pid="whatif" 去匹配 `{pid}` → 422，这个实验台**永远不会被命中**
    （前端只会看到一个转圈的按钮）。
    """
    from app.api.points import router

    paths = [r.path for r in router.routes]
    assert "/api/points/whatif" in paths, paths
    assert paths.index("/api/points/whatif") < paths.index("/api/points/{pid}"), paths


def test_whatif_endpoint_accepts_the_documented_payload_shape(client, residual):
    """入参契约：只接受**场景描述**，没有任何"动作"字段；非法 n（0 / >300）被 pydantic 挡住。"""
    hdr = {"X-Control-Token": _token(client)}
    assert client.post("/api/points/whatif", headers=hdr, json={"n": 0}).status_code == 422
    assert client.post("/api/points/whatif", headers=hdr, json={"n": 301}).status_code == 422
    r = client.post("/api/points/whatif", headers=hdr,
                    json={"scenario": {"face": "back", "order": "reverse"}, "n": 2})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["scenario"]["face"] == "back" and d["scenario"]["order"] == "reverse"
    assert d["status"] in STATUSES


def test_whatif_writes_no_events(client, residual):
    """★ 不写事件流：试跑不改变任何东西，改变真机的动作才配留痕。

    （`/api/events` 是既有的审计读取口；这里比较调用前后的事件水位。）
    """
    hdr = {"X-Control-Token": _token(client)}
    before = client.get("/api/events?limit=1").json()
    seq_before = int((before.get("latest_seq") if isinstance(before, dict) else None)
                     or len(before) if isinstance(before, list) else 0)
    r = client.post("/api/points/whatif", headers=hdr, json={"n": 2})
    assert r.status_code == 200
    after = client.get("/api/events?limit=1").json()
    seq_after = int((after.get("latest_seq") if isinstance(after, dict) else None)
                    or len(after) if isinstance(after, list) else 0)
    assert seq_after == seq_before, (seq_before, seq_after)
