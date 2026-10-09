# -*- coding: utf-8 -*-
"""测试与生产的**隔离**回归（P0）。

背景（2026-10-09 实发事故）：
  跑 `pytest` 时 `app` 会被真实构造，日志 handler 指向生产 `logs/app.log` ——
  于是测试里**注入的异常** traceback 写进了生产日志：

      File "backend\\tests\\test_audit_p0.py", line 486, in boom
          raise RuntimeError("注入的循环异常")

  现场同事查日志时会看到一堆"凭空出现的报错"，分不清是真故障还是测试噪音 ——
  在真机排障时这种干扰代价很高。

  修法：`logs_dir()` 支持 `EFORT_LOGS_DIR` 覆盖，conftest 在**任何 app 模块被 import
  之前**把它指到系统临时目录。这里把两条都钉住。
"""
from __future__ import annotations

import inspect
import os

from app.core import config


def test_logs_dir_can_be_overridden(monkeypatch, tmp_path):
    """★ 日志目录必须能被 EFORT_LOGS_DIR 覆盖（这是隔离的前提）。"""
    target = str(tmp_path / "mylogs")
    monkeypatch.setenv("EFORT_LOGS_DIR", target)
    assert config.logs_dir() == target
    assert os.path.isdir(target), "覆盖后应自动建目录"


def test_logs_dir_default_is_project_logs(monkeypatch):
    """没设环境变量时仍回落到项目 logs/（生产行为不变）。"""
    monkeypatch.delenv("EFORT_LOGS_DIR", raising=False)
    d = config.logs_dir()
    assert d.replace("\\", "/").endswith("/logs")


def test_logs_dir_reads_env_each_call(monkeypatch, tmp_path):
    """★ 必须**每次调用都读环境变量**，不能缓存成模块级常量 ——
    缓存的话 conftest 再早设置也没用（handler 早就在 import 时定死了）。
    """
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    monkeypatch.setenv("EFORT_LOGS_DIR", a)
    assert config.logs_dir() == a
    monkeypatch.setenv("EFORT_LOGS_DIR", b)
    assert config.logs_dir() == b, "换了环境变量必须跟着变"


def test_conftest_redirects_logs_away_from_prod():
    """★★ conftest 必须在 app 被 import 之前设置 EFORT_LOGS_DIR。

    这条用**源码断言**（而不是运行期断言）：conftest 是 pytest 的基础设施，
    运行期它的效果已经体现在"生产日志没被写脏"上，但那条判据需要跑完整套测试
    才能观察到 —— 用源码断言可以在collect 阶段就挡住"有人把这段删了"。
    """
    import tests.conftest as cf

    src = inspect.getsource(cf)
    assert "EFORT_LOGS_DIR" in src, "conftest 必须设置 EFORT_LOGS_DIR（否则测试日志会写进生产）"
    # 必须在 import app 之前设置：定位两处位置比较
    i_env = src.index("EFORT_LOGS_DIR")
    i_app = src.find("from app.main import app")
    assert i_app == -1 or i_env < i_app, "设置必须早于 `from app.main import app`"


def test_db_is_also_isolated():
    """同一条思路的既有保护：测试库不能是生产库。"""
    import tests.conftest as cf

    src = inspect.getsource(cf)
    assert "EFORT_DB_URL" in src
    assert "EFORT_SIMULATE" in src, "必须强制模拟，避免连真实 Modbus"
    assert "EFORT_REAL_MOTION" in src, "必须关掉真实下发总闸"
