# -*- coding: utf-8 -*-
"""pytest 共享 fixture。

测试环境策略：
- EFORT_SIMULATE=always  强制模拟，避免测试时连接真实 Modbus (192.168.1.12:502)
- EFORT_DB_URL 指向临时 SQLite，不污染生产 data/robot.db
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# ★★ 口令必须与开发者本机的 .env 解耦 ★★
# 测试模块（如 test_ops.py / test_safety_api.py）会在导入期就 import app.core.* ，
# 从而触发 .env 加载；若此时环境变量还没设好，本机 .env 里的管理员口令会把测试
# 用的固定口令顶掉，导致"明明代码没问题却全部 401"。
# 本文件由 pytest 最先导入，因此在这里强制赋值最可靠。
os.environ["EFORT_ADMIN_PASSWORD"] = "test1234"
os.environ["EFORT_SIMULATE"] = "always"
# ★★ 真实下发总闸必须在测试环境强制关闭 ★★
# 开发者本机 .env 可能为了真机联调把 EFORT_REAL_MOTION 设为 1（robot.yaml real_write=true），
# 若测试进程继承它，任何 /control/move、/control/jog 测试都会**真的写控制器寄存器** ——
# 测试绝不允许动机器人。这里在导入任何 app 模块之前强制置 0。
os.environ["EFORT_REAL_MOTION"] = "0"

# ★★ 大模型在测试里**必须关闭**（2026-10-10）★★
#   解析入口已改为"**大模型优先**"（用户口径：每句话都过大模型）。若测试进程继承了本机
#   .env 里的 EFORT_LLM_API_KEY，任何 `_parse_to_plan()` 都会**真的去打 DeepSeek** ——
#   测试变慢、不确定、还依赖网络。这里在任何 app 模块 import 之前强制清空 key。
#   要专门验证大模型行为（sanitize/prompt/思维链），直接用 monkeypatch 打开即可。
os.environ["EFORT_LLM_API_KEY"] = ""

# ★★ 日志必须与生产隔离（2026-10-09 事故）：
#   app 被真实构造时日志 handler 指向生产 `logs/app.log`，于是测试里**注入的异常**
#   traceback 写进了生产日志 —— 现场查日志时会看到"凭空出现的报错"，
#   分不清是真故障还是测试噪音。这里指到临时目录（在任何 app 模块被 import 之前设置）。
os.environ.setdefault("EFORT_LOGS_DIR", os.path.join(tempfile.gettempdir(),
                                                     "efort_test_logs"))


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("testdb") / "test.db"
    os.environ["EFORT_DB_URL"] = f"sqlite:///{db_file}"
    os.environ["EFORT_SIMULATE"] = "always"
    # 固定管理员密码, 供控制类接口(阶段1 鉴权门)测试登录（模块级已设，这里再兜一次）
    os.environ["EFORT_ADMIN_PASSWORD"] = "test1234"

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_real_modbus(monkeypatch):
    """★★ 红线 R3（硬护栏）：测试期间禁止连真实控制器 Modbus ★★

    为什么必须有：`EFORT_SIMULATE=always` 只影响**运动引擎/采集器的降级决策**，
    它**不会**拦住 `ModbusRobot.read_regs/write_reg` —— 那些方法无条件对
    `config/robot.yaml` 里的 host:port 开 TCP socket（modbus.py 没有 simulate 短路）。
    于是：只要某个用例 monkeypatch 掉"真实下发总闸"（如 /api/ready 白名单测试），
    `_ready()` 就会去真机做「读快照 → 清报警 → 上伺服 → 加载 → 运行」。
    真事故：本机的 192.168.1.12:502 可达，红线段测试曾真的连上控制器。

    做法：把单例的 host/port 指向本机不可达端口 —— 任何漏网的调用都会**快速失败**
    （连接被拒），而不是去写真机。用例若需要"看着像能通"的控制器，请自行注入假对象
    （见 tests/test_safety_redline.py 的 _FakeMB / test_audit_p0.py 的 _MB）。
    """
    try:
        from app.services.motion import motion
        monkeypatch.setattr(motion.modbus, "host", "127.0.0.1", raising=False)
        monkeypatch.setattr(motion.modbus, "port", 1, raising=False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_settings_overlay():
    """★ 跨文件状态污染兜底：robot.model 等配置来自 `get_config()` 单例 + 覆盖层文件。

    `test_settings.py::test_operator_can_write_non_admin_field` 会把
    `robot.model="ER8-700H-OP"` 写进 config/app_settings.json 并 reload 到全局
    Config 单例；若该文件在用例结束后残留（或被其它文件的用例读到），
    `/api/health`、`/api/meta` 等只读接口就会读到被污染的机型名而误报失败。

    这里在每个用例前后都清掉覆盖层文件并 reload 配置，保证任何用例都不继承
    上一个用例写下的差异。绝不碰 config/robot.yaml（只读合并来源）。
    """
    from app.core.config import project_root, reload_config
    from app.core.app_settings import SETTINGS_PATH

    def _clean():
        try:
            if os.path.isfile(SETTINGS_PATH):
                os.remove(SETTINGS_PATH)
        except OSError:
            pass
        try:
            reload_config()
        except Exception:
            pass

    _clean()
    yield
    _clean()


@pytest.fixture(autouse=True)
def _isolate_env_file(tmp_path, monkeypatch):
    """★★★ 红线：测试**绝不允许**改写项目根的 `.env` ★★★

    事故（2026-09-29，用户反复反馈很久没找到原因）：
      `tests/test_auth_ttl.py::test_control_ttl_updates_and_affects_new_tokens_only`
      会调 `POST /api/settings/control-ttl {"ttl_sec": 1800}`，而 `api/settings.py`
      的这个接口会把值**回写项目根 `.env`** 的 `EFORT_CONTROL_TTL` 行（本意是好的：
      让设置持久化）。但测试没做隔离 →
      **任何人跑一次测试套件，操作员的设置就被改回 1800（30 分钟）**；
      而且后端启动时只读 `.env`，于是"设置页改成 2 小时 → 跑测试 → 重启 → 回到 30 分钟"，
      用户喊了无数遍都不生效。`/api/settings/password` 同样会写 `.env`
      （`EFORT_ADMIN_PASSWORD`，风险更大：会把真实管理员口令改掉）。

    做法：把 `settings._env_path` 指到临时文件。**所有**写 `.env` 的路径都经过它，
    因此本夹具是"单点收口"——今后新增任何写 `.env` 的接口/测试都自动被隔离。
    """
    import app.api.settings as settings_api

    fake = tmp_path / ".env.test"
    monkeypatch.setattr(settings_api, "_env_path", lambda: str(fake), raising=True)
    yield fake


@pytest.fixture(autouse=True)
def _pin_joint_lock_off(monkeypatch):
    """★★ 轴锁策略与"现场配置"解耦（2026-09-29）★★

    口径（别搞反）：生产默认 `config/robot.yaml → motion.joint_lock.enabled: false`
    = **操作员 J1~J6 全轴可动**；「仅 J6」是**给 AI 做真机验证的临时护栏**。

    但本套件里绝大多数用例测的是点动 / 移动 / 运行模式 / 仿真同步的**机制**，
    它们天然会带动 J1~J5。所以测试环境统一把轴锁**钉在关闭**，让用例只测机制，
    不受"现场当前开关状态"影响（现场若临时打开护栏，套件也不该因此整片红）。
    专门验证轴锁的用例（tests/test_safety_redline.py 的 R1 组）自行 monkeypatch 打开
    —— 用例体内的 setattr 晚于所有 fixture 装配，能正常覆盖本 fixture。

    ★ 必须同时打两处，因为 motion.py 在 import 时把函数绑进了自己的命名空间，
      只打 safety_const 是打不到下发判据的：
        · app.core.safety_const.joint_lock_enabled → /api/system/health 徽标
        · app.services.motion.joint_lock_enabled  → 真实下发前的轴锁判据
    """
    import app.core.safety_const as sc
    import app.services.motion as motion_mod

    monkeypatch.setattr(sc, "joint_lock_enabled", lambda: False, raising=False)
    monkeypatch.setattr(motion_mod, "joint_lock_enabled", lambda: False, raising=False)
    yield
