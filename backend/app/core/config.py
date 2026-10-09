# -*- coding: utf-8 -*-
"""
集中配置加载: 读取项目根目录 config/robot.yaml
所有模块通过 get_config() 获取统一配置, 不散落硬编码常量。
"""
from __future__ import annotations

import os
from typing import Any, Dict

import yaml

# backend/app/core/config.py -> 上溯 3 级到项目根 (core -> app -> backend -> 项目根)
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config", "robot.yaml")


def _load_dotenv(path: str | None = None) -> int:
    """读取项目根目录 .env, 写入 os.environ（零依赖, 不覆盖已有变量）。

    规则与常见约定一致:
      - 已存在的真实环境变量优先, .env 不覆盖它;
      - 支持 `#` 整行注释与 `KEY=VALUE` 行;
      - 值两端成对的单/双引号会被剥掉;
      - 文件不存在则静默跳过(所有配置项都有内置默认值)。
    返回实际写入的变量个数, 供启动日志使用。
    """
    p = path or os.path.join(_PROJECT_ROOT, ".env")
    if not os.path.isfile(p):
        return 0
    n = 0
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if not key or key in os.environ:
                    continue
                os.environ[key] = val
                n += 1
    except Exception:
        return n
    return n


dotenv_loaded = _load_dotenv()


def project_root() -> str:
    return _PROJECT_ROOT


def data_dir() -> str:
    d = os.path.join(_PROJECT_ROOT, "data")
    os.makedirs(d, exist_ok=True)
    return d


def db_path() -> str:
    """返回 SQLite 文件绝对路径 (data/robot.db), 目录自动创建。"""
    return os.path.join(data_dir(), "robot.db")


def logs_dir() -> str:
    """日志目录。

    ★★ 2026-10-09：支持 `EFORT_LOGS_DIR` 覆盖 —— **测试必须覆盖它**。
      事故：跑 `pytest` 时 app 被真实构造，日志 handler 指向生产 `logs/app.log`，
      于是测试里注入的异常 traceback（`tests/test_audit_p0.py ... raise RuntimeError("注入的循环异常")`）
      写进了**生产日志**。现场同事查日志时会看到一堆"凭空出现的报错"，
      分不清是真故障还是测试噪音 —— 排障时这是很贵的干扰。
      conftest 已把该变量指到临时目录（见 tests/conftest.py）。
    """
    d = os.environ.get("EFORT_LOGS_DIR") or os.path.join(_PROJECT_ROOT, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def public_dir() -> str:
    """前端构建产物目录 (供 FastAPI 静态托管)。"""
    return os.path.join(_PROJECT_ROOT, "frontend", "dist")


class Config:
    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw

    def get(self, *keys, default=None):
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    # 便捷访问
    @property
    def robot_model(self) -> str:
        return self.get("robot", "model", default="ER8-700H")

    @property
    def modbus(self) -> Dict[str, Any]:
        return self.get("modbus", default={})

    @property
    def connection(self) -> Dict[str, Any]:
        conn = dict(self.get("connection", default={}) or {})
        sim = os.environ.get("EFORT_SIMULATE")
        if sim in ("always", "never", "auto"):
            conn["simulate"] = sim
        return conn

    @property
    def dh(self) -> Dict[str, Any]:
        return self.get("dh", default={})

    @property
    def mounting(self) -> Dict[str, Any]:
        return self.get("mounting", default={})

    @property
    def sampling(self) -> Dict[str, Any]:
        return self.get("sampling", default={})

    @property
    def server(self) -> Dict[str, Any]:
        return self.get("server", default={})

    @property
    def vision(self) -> Dict[str, Any]:
        """视觉颜色分拣配置（相机服务地址/轮询/联动）。

        `base_url` 的解析优先级（★ 高到低，界面上会如实标注该项是不是被 env 顶掉的）：
          1. `EFORT_CAMERA_URL` 环境变量 —— 最高，现场临时改地址不用动配置文件；
          2. `camera.host` + `camera.port` —— 界面「相机与视觉服务」里填的 IP 与端口；
          3. `vision.base_url` —— robot.yaml 里的整条 URL（旧位置，向后兼容）。

        为什么要把 IP 与端口拆出来：界面上"摄像头 IP / 端口"是两个输入框，
        而消费方要的是一条 URL。在这里合成一次，比让每个调用点各自拼字符串可靠 ——
        拼漏斜杠、多带一个冒号这类问题只会出现在一个地方。
        """
        v = dict(self.get("vision", default={}) or {})
        host = self.get("camera", "host", default=None)
        port = self.get("camera", "port", default=None)
        if host and port:
            try:
                v["base_url"] = "http://%s:%d" % (str(host).strip(), int(port))
            except (TypeError, ValueError):
                pass
        env = os.environ.get("EFORT_CAMERA_URL")
        if env:
            v["base_url"] = env
        return v


_cfg: Config | None = None


def _read_merged(path: str = CONFIG_PATH) -> dict:
    """读取 robot.yaml，**并在其上叠加 config/app_settings.json 覆盖层**，返回合并后的原始字典。

    ★ 覆盖层 = 界面上改过的那部分配置（见 core/app_settings.py）。robot.yaml 保持人工维护、
      带注释、不被程序回写；程序只写覆盖层。深合并后才是"当前生效配置"。
    ★ 覆盖层损坏时退化为只用 robot.yaml —— 配置文件坏了不能让服务起不来。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if path == CONFIG_PATH:
        try:
            from app.core.app_settings import deep_merge, load_overlay

            overlay = load_overlay()
            if overlay:
                raw = deep_merge(raw, overlay)
        except Exception:        # 覆盖层坏了 → 只用 yaml，但必须留痕
            import logging

            logging.getLogger("config").warning(
                "app_settings.json 覆盖层加载失败，本次仅使用 robot.yaml", exc_info=True
            )
    return raw


def load_config(path: str = CONFIG_PATH) -> Config:
    global _cfg
    _cfg = Config(_read_merged(path))
    return _cfg


def get_config() -> Config:
    if _cfg is None:
        return load_config()
    return _cfg


def reload_config() -> Config:
    """重新读盘（覆盖层改完后调用）。

    ★ 必须在**原 Config 对象上**替换 raw，而不是新建对象：全项目都是
      `get_config()` 后长期持有引用，新建对象会让已经持有旧引用的模块一直看到旧配置
      （表现为"保存了但没生效，重启才对"）。
    """
    cfg = get_config()
    cfg.raw = _read_merged()
    return cfg
