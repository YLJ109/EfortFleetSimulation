# -*- coding: utf-8 -*-
"""
UI 可编辑配置：`config/app_settings.json`（**稀疏覆盖层**）。

## 为什么不让界面直接改 robot.yaml

`config/robot.yaml` 里几乎每个字段都带着成段的手写注释（DH 的 measured/estimate 状态、
寄存器映射的实测说明、TODL 待核对项……）。用 `yaml.safe_dump` 回写会**把这些注释全部抹掉**，
而这个项目恰恰靠那些注释在做真机联调 —— 代价远大于收益。

所以采用覆盖层：

    robot.yaml（人工维护、带注释、只读）
        ↑ 被深合并覆盖
    app_settings.json（界面写、稀疏、只存"与 yaml 不同的值"）

- 只存**差异**：某字段的值等于 yaml 里的值时不写进覆盖层 → 删掉整个文件即"全部恢复默认"，
  单个字段改回 yaml 原值也会自动从覆盖层消失（这就是"逐字段重置"）。
- 合并规则：dict 递归合并；**list 整体替换**（关节限位这类数组不能逐元素合，否则删除一项会删不掉）。
- 写盘：先写 `.tmp` 再 `os.replace`，原子替换，断电不会留下半个文件。
- 路径可被环境变量 `EFORT_SETTINGS_PATH` 覆盖（**不设就是 `config/app_settings.json`，与历史一致**）；
  存在的唯一理由是让**测试进程把覆盖层写到临时目录**，不再删生产那个真实文件
  （AGENTS.md §5.5 的"第四项共享状态"）。

## schema 是唯一事实来源

下面的 `SCHEMA` 同时驱动三件事：后端校验、`GET /api/settings` 的字段元数据（标签/单位/范围/说明）、
以及权限判定。前端不再硬编码任何字段列表 —— 加一个配置项只改这一个文件。

字段属性：
  key      点号路径，必须与 robot.yaml 的层级一致
  type     str | text | ip | int | float | bool | enum | signs6 | limits6
  min/max  数值范围（int/float）
  maxlen   字符串最大长度
  options  enum 取值
  unit     单位（仅展示）
  admin    True = 只有管理员能改；False = 操作员也能改（仅限"改了不会出事"的项）
  apply    改完需要做什么才生效：
             live      立即（每次用到都重新读配置）
             reload    重载配置（后端 reload_config）
             reconnect 重连机器人
             restart   必须重启后端进程
  env      可覆盖该项的环境变量名。★ 环境变量优先级最高，此时该项在界面上**只读**
           （否则用户改了没效果，会以为是 bug）
  editable False = 只读展示（标定数据等），reason 给出原因
  help     界面上的说明文案

## 安全取向

- 写操作默认需要管理员；只有少数"纯展示 + 运行时开关"放开给操作员（见各处 admin=False）。
- 任何敏感值（口令类）**只回传状态不回传内容**，见 `secret` 与 `runtime_flags()`。
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, List, Optional

from app.core.config import CONFIG_PATH, project_root

# ★★ 生产口径（改动需负责人点头）：界面改的配置默认落在 <项目根>/config/app_settings.json。
#    这个相对路径写死在这里，是因为它是"配置覆盖层唯一事实来源的位置"——
#    改了它，界面保存的是 A 文件、后端读的是 B 文件，现场表现就是"改了没生效"。
PRODUCTION_SETTINGS_REL = os.path.join("config", "app_settings.json")
PRODUCTION_SETTINGS_PATH = os.path.join(project_root(), PRODUCTION_SETTINGS_REL)


def resolve_settings_path() -> str:
    """覆盖层文件路径的**唯一解析入口**（由环境变量 `EFORT_SETTINGS_PATH` 覆盖）。

    - **未设该环境变量 → 逐字节等于历史默认值** `project_root()/config/app_settings.json`
      （生产口径，见 `PRODUCTION_SETTINGS_REL`）。
    - 设了就用它：绝对路径**原样**使用；相对路径按**项目根**解析（与默认路径同一套解析风格），
      并做一次 `normpath` —— 免得写出 `D:\root\tmp_overlay/app_settings.json` 这种混合分隔符。
    - ★ 为什么要这个开关：这个文件过去只是**一个硬编码的模块级常量**，于是它是测试与生产之间的
      **第四项共享状态**（另三项见 AGENTS.md §5.5：`EFORT_DB_URL` / `EFORT_SIMULATE` /
      `EFORT_LOGS_DIR`）。测试靠"每个用例前后删掉那个真实文件"来保证干净起点，
      并发跑两个 pytest 就会同时读写同一个真实文件 → 随机假故障。
      现在测试进程把覆盖层写到临时目录，**生产文件不再被测试删除**。
    """
    env = (os.environ.get("EFORT_SETTINGS_PATH") or "").strip()
    if env:
        if os.path.isabs(env):
            return env
        return os.path.normpath(os.path.join(project_root(), env))
    return PRODUCTION_SETTINGS_PATH


# ★ 模块级常量保持**可变**：所有读写函数在调用时才读这个全局名，
#   所以运行期 monkeypatch（`app_settings.SETTINGS_PATH = ...`）依旧有效。
SETTINGS_PATH = resolve_settings_path()
SETTINGS_VERSION = 1

_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9._:\-\[\]/]{1,200}$")


class SettingsError(ValueError):
    """配置非法（消息会直接展示给用户，必须写清是哪一项、期望什么）。"""


def _f(key: str, label: str, type: str = "str", **kw) -> Dict[str, Any]:
    d: Dict[str, Any] = {"key": key, "label": label, "type": type}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# 字段定义（唯一事实来源）
# ---------------------------------------------------------------------------
SCHEMA: List[Dict[str, Any]] = [
    {
        "id": "robot",
        "label": "机器人信息",
        "desc": "机型铭牌与规格。负载/臂展参与运动学校验，改动需谨慎。",
        "fields": [
            _f("robot.model", "机器人型号", "str", maxlen=64, admin=False, apply="live"),
            _f("robot.serial", "出厂序列号", "str", maxlen=64, admin=False, apply="live"),
            _f("robot.payload_kg", "额定负载", "float", min=0.1, max=500, unit="kg",
               admin=True, apply="reload", help="用于负载校验与界面提示"),
            _f("robot.reach_mm", "最大臂展", "float", min=100, max=5000, unit="mm",
               admin=True, apply="reload", help="超出该半径的直角点位会被判定为不可达"),
            _f("robot.notes", "备注", "text", maxlen=200, admin=False, apply="live"),
            _f("mounting.type", "安装方式", "enum", options=["floor", "wall", "ceiling"],
               admin=True, apply="reload", help="影响底座变换与模型抬升高度，改完请刷新页面"),
        ],
    },
    {
        "id": "net",
        "label": "机器人网络（Modbus TCP）",
        "desc": "与控制器通信的连接参数。改完点『立即生效』会断开并重连一次。",
        "fields": [
            _f("connection.host", "控制器 IP", "ip", admin=True, apply="reconnect"),
            _f("connection.port", "控制器端口", "int", min=1, max=65535, admin=True, apply="reconnect"),
            _f("connection.unit_id", "从站号 (Unit ID)", "int", min=1, max=255, admin=True, apply="reconnect"),
            _f("connection.timeout_s", "通信超时", "float", min=0.2, max=10, unit="s",
               admin=True, apply="reconnect"),
            _f("connection.simulate", "数据来源", "enum", options=["auto", "always", "never"],
               admin=True, apply="reconnect", env="EFORT_SIMULATE",
               help="auto=连得上读真机、否则模拟；always=强制模拟；never=只读真机"),
        ],
    },
    {
        "id": "modbus",
        "label": "寄存器映射",
        "desc": "★ 按控制器手册核对后再改。地址填错会读到错误数据（或写入错误位置）。",
        "fields": [
            _f("modbus.func", "读功能码", "int", min=1, max=4, admin=True, apply="reconnect",
               help="保持寄存器读通常为 3"),
            _f("modbus.base_addr", "起始地址", "int", min=0, max=65535, admin=True, apply="reconnect",
               help="6 关节角起始寄存器（每关节 2 个寄存器）"),
            _f("modbus.count", "寄存器数量", "int", min=2, max=64, admin=True, apply="reconnect",
               help="6 关节 × 2 = 12"),
            _f("modbus.unit", "数值单位", "enum", options=["deg", "rad"], admin=True, apply="reload"),
            _f("modbus.word_order", "字序", "enum", options=["swap", "normal"], admin=True, apply="reload",
               help="float32 高低字的排列方式，与控制器实现相关"),
        ],
    },
    {
        "id": "axes",
        "label": "轴校准与限位",
        "desc": "方向符号与关节行程。★ 这两项直接参与运动学与安全判定，改错会导致方向相反或误报。",
        "fields": [
            _f("axis_sign", "轴方向符号", "signs6", admin=True, apply="reload",
               help="某轴在界面上转向与真机相反时，把对应位改成 -1"),
            _f("joint_limits", "关节行程限位", "limits6", unit="deg", admin=True, apply="reload",
               help="官方规格，用于点动/执行时的限位夹紧"),
            _f("dh", "DH 参数", "ro", editable=False, apply="restart",
               reason="标定数据，且当前 d1/d6/α 仍为估算值；请走标定流程或直接编辑 config/robot.yaml"),
            _f("dh.calibration_pending", "DH 标定状态", "ro", editable=False, apply="restart",
               reason="由配置声明，改它没有意义"),
        ],
    },
    {
        "id": "sampling",
        "label": "采样与推送",
        "desc": "数据采集频率与历史保留。频率越高界面越顺滑，但总线负载与库增长速度也越高。",
        "fields": [
            _f("sampling.read_hz", "Modbus 读取频率", "float", min=0.5, max=50, unit="Hz",
               admin=True, apply="reconnect"),
            _f("sampling.db_write_hz", "入库频率", "float", min=0.1, max=20, unit="Hz",
               admin=True, apply="reload", help="与读取频率分开，防止数据库膨胀"),
            _f("sampling.ws_push_hz", "WebSocket 推送频率", "float", min=1, max=60, unit="Hz",
               admin=True, apply="live"),
            _f("sampling.history_retention_days", "历史保留天数", "int", min=1, max=3650, unit="天",
               admin=True, apply="reload"),
        ],
    },
    {
        "id": "camera",
        "label": "相机与视觉服务",
        "desc": "相机服务是**独立进程**（camera/camera_service.py，默认 :8100），后端通过 HTTP 访问它。"
                "这里填的是后端去连它的地址；相机本身的型号/序列号/IP 由服务枚举后回传，只能查看。",
        "fields": [
            _f("camera.host", "相机服务 IP", "ip", admin=True, apply="reload",
               help="相机服务所在主机的 IP。与本服务同机时填 127.0.0.1"),
            _f("camera.port", "相机服务端口", "int", min=1, max=65535, admin=True, apply="reload",
               help="相机服务监听端口，默认 8100；须与启动相机服务时的 CAMERA_PORT 一致"),
            _f("vision.base_url", "生效地址（合成）", "ro", editable=False, apply="reload",
               reason="由上方 IP 与端口自动合成，改上面两项即可；如需带路径的完整地址，"
                      "用环境变量 EFORT_CAMERA_URL（优先级最高）"),
            _f("vision.poll_ms", "事件拉取周期", "int", min=100, max=10000, unit="ms",
               admin=True, apply="reload", help="后端拉取相机 /vision/last 的周期"),
            _f("vision.timeout_s", "调用超时", "float", min=0.5, max=30, unit="s",
               admin=True, apply="reload", help="相机服务没起时快速失败，不拖慢后端"),
            _f("vision.enabled", "摄入相机事件", "bool", admin=False, apply="live",
               help="关掉只保留图片代理，不落库、不广播、不联动"),
            _f("vision.auto_execute", "命中规则自动下发", "bool", admin=True, apply="live",
               help="★ 默认关闭。视觉误判 + 自动运动 = 现场事故，开启前确认围栏互锁有效"),
            _f("vision.retention_days", "视觉记录保留天数", "int", min=1, max=3650, unit="天",
               admin=True, apply="reload"),
        ],
    },
    {
        "id": "motion",
        "label": "运动执行与安全下发",
        "desc": "★ 高风险区。real_write 决定「能不能碰真机」；写路径走点动通道（Stage D 实测寄存器表）。",
        "fields": [
            _f("motion.real_write", "启用真实下发", "bool", admin=True, apply="restart",
               help="★ 还需同时设置环境变量 EFORT_REAL_MOTION=1 才会真正写控制器；两者缺一即保持模拟"),
            _f("motion.require_live_safety", "围栏互锁严格模式", "bool", admin=True, apply="live",
               help="开启后，前端未实时上报围栏状态（或状态过期）就拒绝下发运动"),
            # ★ 取消 `motion.collision_tolerance_deg`（碰撞预测容差）：
            #   现场最终口径是"残影跟随变红即拒绝，不做角度放宽" —— 该字段没有任何
            #   消费方，留着只会让人以为"调大就能过"。真撞上去没有撤销键，宁可不给这个旋钮。
            _f("motion.mode_claim", "当前运行模式", "enum",
               options=["T1", "T2", "AUTO", "REMOTE"], admin=True, apply="live",
               help="★ 2026-09-23 实机确认：只有 AUTO/远程档接受上位机指令；"
                    "T1/T2 下控制器忽略 PC 点动/下发"),
            # ⚠ 旧 write_func / write_base_addr / write_speed_addr / estop_func / estop_addr
            #   已实机证伪并从界面移除（写路径重写为点动通道，地址表硬编码在
            #   services/modbus.py 顶部并逐条注释出处）。
        ],
    },
    {
        "id": "jog",
        "label": "点动（示教）参数",
        "desc": "对齐示教器 T1 教学模式的限速与死人开关语义。",
        "fields": [
            _f("motion.jog.max_speed_dps", "关节角速度上限", "float", min=1, max=180, unit="°/s",
               admin=True, apply="live", help="T1 教学限速，超过会被引擎夹紧"),
            _f("motion.jog.max_speed_mmps", "直角线速度上限", "float", min=1, max=1000, unit="mm/s",
               admin=True, apply="live", help="T1 下官方上限 50 mm/s；直角系点动按此换算"),
            _f("motion.jog.watchdog_ms", "死人开关超时", "int", min=200, max=10000, unit="ms",
               admin=True, apply="live", help="连续点动期间超过该时间未收到保活信号即自动停"),
            _f("motion.jog.tick_ms", "点动下发周期", "int", min=40, max=500, unit="ms",
               admin=True, apply="live", help="越小越顺滑，但总线负载越高"),
            _f("motion.jog.step_angles", "关节系步长档位", "str", maxlen=64, admin=True, apply="live",
               help="逗号分隔的角度档位（度），例如 0.1,1,5,10"),
            _f("motion.jog.step_mm", "直角系步长档位", "str", maxlen=64, admin=True, apply="live",
               help="逗号分隔的距离档位（mm），例如 0.1,1,5,10"),
        ],
    },
    {
        "id": "server",
        "label": "服务与端口",
        "desc": "后端服务自身的监听参数。★ 改动需要重启后端进程才会生效。",
        "fields": [
            _f("server.host", "监听地址", "host", admin=True, apply="restart",
               help="0.0.0.0 = 允许局域网访问；127.0.0.1 = 仅本机"),
            _f("server.port", "监听端口", "int", min=1, max=65535, admin=True, apply="restart",
               help="改动后需重启后端；重启后请用新端口访问本页面"),
            _f("server.ws_path", "WebSocket 路径", "str", maxlen=64, admin=True, apply="restart"),
        ],
    },
    {
        "id": "database",
        "label": "数据库",
        "desc": "SQLite 存储位置。改动需要重启后端才会切换。",
        "fields": [
            _f("database.url", "连接串", "str", maxlen=300, admin=True, apply="restart",
               env="EFORT_DB_URL", help="形如 sqlite:///data/robot.db（相对项目根目录）"),
        ],
    },
]

# 环境变量 → 运行时标记的说明（只读展示，不是可编辑字段）
RUNTIME_ENV_KEYS = [
    ("EFORT_REAL_MOTION", "真实运动总开关", "必须为 1 且 motion.real_write=true 才真正写控制器"),
    ("EFORT_ADMIN_PASSWORD", "管理员口令", "已在 .env 配置（内容不回显）；修改请用下方『修改管理员密码』"),
    ("EFORT_OPERATOR_PASSWORD", "操作员口令", "未配置则操作员角色禁用，所有令牌都是管理员"),
    ("EFORT_CONTROL_TTL", "控制令牌有效期", "秒，默认 7200（2 小时），0=不限时"),
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def field_index() -> Dict[str, Dict[str, Any]]:
    return {f["key"]: f for g in SCHEMA for f in g["fields"]}


def default_settings() -> Dict[str, Any]:
    return {"version": SETTINGS_VERSION, "values": {}, "updated_at": ""}


def _flatten(tree: Any, prefix: str = "", out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """嵌套 dict → {点号路径: 叶子值}。list 视为叶子（整体替换语义）。"""
    if out is None:
        out = {}
    if isinstance(tree, dict):
        for k, v in tree.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = tree
    return out


def _unflatten(flat: Dict[str, Any]) -> Dict[str, Any]:
    tree: Dict[str, Any] = {}
    for key, val in flat.items():
        cur = tree
        parts = key.split(".")
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = val
    return tree


def deep_merge(base: Any, over: Any) -> Any:
    """深合并：dict 递归、其余（含 list）整体替换。返回新对象，不修改入参。"""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = deep_merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def load_raw() -> Dict[str, Any]:
    """读取覆盖层文件；缺失/损坏一律回退空覆盖层（**绝不因此让服务起不来**）。"""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return default_settings()
        vals = d.get("values")
        if not isinstance(vals, dict):
            vals = {}
        return {"version": SETTINGS_VERSION, "values": vals, "updated_at": str(d.get("updated_at") or "")}
    except (OSError, json.JSONDecodeError):
        return default_settings()


def load_overlay() -> Dict[str, Any]:
    return load_raw()["values"]


def base_config() -> Dict[str, Any]:
    """robot.yaml 原文（不叠加覆盖层）—— 用于判断"某字段是否等于出厂值"。"""
    import yaml
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = yaml.safe_load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def merged_config() -> Dict[str, Any]:
    """robot.yaml + 覆盖层，供 Config 使用。"""
    return deep_merge(base_config(), load_overlay())


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _need_str(f, v) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise SettingsError(f"{f['label']} 必须是文本")
    s = str(v).strip()
    if not s and f["type"] != "str":
        raise SettingsError(f"{f['label']} 不能为空")
    ml = int(f.get("maxlen", 0) or 0)
    if ml and len(s) > ml:
        raise SettingsError(f"{f['label']} 最长 {ml} 个字符（当前 {len(s)}）")
    return s


def _need_num(f, v, integer: bool):
    if isinstance(v, bool):
        raise SettingsError(f"{f['label']} 必须是数字")
    if isinstance(v, str) and not v.strip():
        raise SettingsError(f"{f['label']} 不能为空")
    try:
        n = int(v) if integer else float(v)
    except (TypeError, ValueError):
        raise SettingsError(f"{f['label']} 必须是{'整数' if integer else '数字'}")
    if integer and isinstance(v, float) and not float(v).is_integer():
        raise SettingsError(f"{f['label']} 必须是整数")
    if n != n:  # NaN
        raise SettingsError(f"{f['label']} 不是有效数字")
    lo, hi = f.get("min"), f.get("max")
    if lo is not None and n < lo:
        raise SettingsError(f"{f['label']} 不能小于 {lo}{f.get('unit', '')}")
    if hi is not None and n > hi:
        raise SettingsError(f"{f['label']} 不能大于 {hi}{f.get('unit', '')}")
    return n


def _coerce(f: Dict[str, Any], v: Any) -> Any:
    t = f["type"]
    if t == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            return v.strip().lower() in ("true", "1", "yes", "on")
        raise SettingsError(f"{f['label']} 必须是开/关")
    if t in ("int", "float"):
        return _need_num(f, v, t == "int")
    if t == "enum":
        opts = f.get("options") or []
        s = _need_str(f, v)
        if s not in opts:
            raise SettingsError(f"{f['label']} 只能是 {'/'.join(opts)} 之一（收到 {s}）")
        return s
    if t == "ip":
        s = _need_str(f, v)
        if not _IP_RE.match(s):
            raise SettingsError(f"{f['label']} 不是合法 IPv4 地址（形如 192.168.1.12）")
        if any(int(x) > 255 for x in s.split(".")):
            raise SettingsError(f"{f['label']} 每段必须在 0~255 之间")
        return s
    if t == "host":
        s = _need_str(f, v)
        if not _HOST_RE.match(s):
            raise SettingsError(f"{f['label']} 只允许字母数字与 . _ -（最长 64）")
        return s
    if t == "url":
        s = _need_str(f, v)
        if not _URL_RE.match(s):
            raise SettingsError(f"{f['label']} 必须是 http(s):// 开头的地址")
        return s.rstrip("/")
    if t == "signs6":
        if not isinstance(v, (list, tuple)) or len(v) != 6:
            raise SettingsError(f"{f['label']} 必须是 6 个数")
        out = []
        for i, x in enumerate(v):
            n = _need_num({"label": f"{f['label']} J{i + 1}", "type": "int", "min": -1, "max": 1}, x, True)
            if n not in (1, -1):
                raise SettingsError(f"{f['label']} J{i + 1} 只能是 1 或 -1")
            out.append(int(n))
        return out
    if t == "limits6":
        if not isinstance(v, (list, tuple)) or len(v) != 6:
            raise SettingsError(f"{f['label']} 必须是 6 个关节")
        base_names = [f"J{i + 1}" for i in range(6)]
        out = []
        for i, item in enumerate(v):
            if not isinstance(item, dict):
                raise SettingsError(f"{f['label']} J{i + 1} 必须是 {min,max} 对象")
            nm = str(item.get("name") or base_names[i])
            lo = _need_num({"label": f"{f['label']} J{i + 1} 下限", "type": "float",
                            "min": -360, "max": 360}, item.get("min"), False)
            hi = _need_num({"label": f"{f['label']} J{i + 1} 上限", "type": "float",
                            "min": -360, "max": 360}, item.get("max"), False)
            if lo >= hi:
                raise SettingsError(f"{f['label']} J{i + 1} 的下限必须小于上限（{lo} ≥ {hi}）")
            out.append({"name": nm, "min": lo, "max": hi})
        return out
    return _need_str(f, v)


def validate_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """校验 {点号路径: 值} 形式的补丁。值传 None 表示"恢复该字段的出厂值"。"""
    if not isinstance(patch, dict):
        raise SettingsError("请求体必须是对象")
    idx = field_index()
    out: Dict[str, Any] = {}
    for key, val in patch.items():
        f = idx.get(key)
        if f is None:
            raise SettingsError(f"未知配置项：{key}")
        if f.get("editable") is False:
            raise SettingsError(f"{f['label']} 为只读项，不能修改：{f.get('reason', '')}")
        if val is None:
            out[key] = None            # 恢复出厂
            continue
        out[key] = _coerce(f, val)
    return out


def assert_editable_keys(keys: List[str]) -> None:
    idx = field_index()
    bad = [k for k in keys if k not in idx]
    if bad:
        raise SettingsError(f"未知配置项：{', '.join(bad)}")


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _write_raw(values: Dict[str, Any]) -> None:
    import time
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    payload = {
        "version": SETTINGS_VERSION,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "values": values,
    }
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
    os.replace(tmp, SETTINGS_PATH)


def save_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """应用一个补丁。返回 {changed: {key: {old,new}}, apply: [生效方式], values: 覆盖层}。

    ★ 与出厂值相同的项会从覆盖层里**移除**（这就是逐字段"恢复默认"的实现）。
    """
    patch = validate_patch(patch)
    base = base_config()
    flat_base = _flatten(base)

    old_flat = _flatten(load_overlay())
    new_flat = dict(old_flat)

    changed: Dict[str, Any] = {}
    for key, val in patch.items():
        if val is None:
            if key in new_flat:
                changed[key] = {"old": new_flat.pop(key), "new": flat_base.get(key)}
            continue
        before = new_flat.get(key, flat_base.get(key))
        if before == val:
            continue
        changed[key] = {"old": before, "new": val}
        new_flat[key] = val

    # 与出厂值相同 → 不写进覆盖层（保持"稀疏"）
    for key in list(new_flat.keys()):
        if key in flat_base and new_flat[key] == flat_base[key]:
            new_flat.pop(key)

    values = _unflatten(new_flat)
    _write_raw(values)

    idx = field_index()
    applies: List[str] = []
    for key in changed:
        a = (idx.get(key) or {}).get("apply", "reload")
        if a not in applies:
            applies.append(a)
    # 生效顺序：越靠后越"重"，调用方按最重的那个动作即可
    order = ["live", "reload", "reconnect", "restart"]
    applies.sort(key=lambda x: order.index(x) if x in order else 99)

    return {"changed": changed, "apply": applies, "values": values}


def reset_settings() -> Dict[str, Any]:
    """清空覆盖层 → 全部回到 robot.yaml 的值。"""
    old = _flatten(load_overlay())
    try:
        if os.path.isfile(SETTINGS_PATH):
            os.remove(SETTINGS_PATH)
    except OSError:
        _write_raw({})
    return {"removed": sorted(old.keys()), "values": {}}


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------
def _env_override(f: Dict[str, Any]) -> Optional[str]:
    name = f.get("env")
    if not name:
        return None
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def _effective_overrides() -> Dict[str, Any]:
    """少数字段的"生效值"不是配置原文，而是运行时**合成**出来的。

    目前只有相机服务地址：由 `camera.host` + `camera.port` 合成，且可能被
    `EFORT_CAMERA_URL` 顶掉（与 `core/config.py` 的 `Config.vision` 同一套优先级）。
    如果这里不算，界面就会显示 robot.yaml 里的旧地址 —— 用户明明改了 IP 却看不到变化，
    只会以为"保存没生效"。
    """
    m = merged_config()
    host = (m.get("camera") or {}).get("host")
    port = (m.get("camera") or {}).get("port")
    base = (m.get("vision") or {}).get("base_url") or "http://127.0.0.1:8100"
    if host and port:
        try:
            base = "http://%s:%d" % (str(host).strip(), int(port))
        except (TypeError, ValueError):
            pass
    env = os.environ.get("EFORT_CAMERA_URL")
    if env:
        base = env
    return {"vision.base_url": base}


def describe() -> Dict[str, Any]:
    """给前端的完整描述：分组 + 字段元数据 + 当前生效值 + 来源 + 是否可编辑。"""
    merged = merged_config()
    flat = _flatten(merged)
    overlay_flat = _flatten(load_overlay())
    base_flat = _flatten(base_config())
    derived = _effective_overrides()

    groups = []
    for g in SCHEMA:
        fields = []
        for f in g["fields"]:
            env_v = _env_override(f)
            key = f["key"]
            editable = f.get("editable", True) and env_v is None
            if env_v is not None:
                source = "env"
            elif key in derived and derived[key] != flat.get(key):
                source = "derived"
            elif key in overlay_flat:
                source = "settings"
            elif f["type"] == "ro":
                source = "yaml"
            elif key in base_flat:
                source = "yaml"
            else:
                source = "default"
            val = env_v if env_v is not None else flat.get(key)
            if key in derived:
                val = derived[key]
            item = {
                "key": key,
                "label": f["label"],
                "type": f["type"],
                "value": val,
                "unit": f.get("unit", ""),
                "min": f.get("min"),
                "max": f.get("max"),
                "maxlen": f.get("maxlen"),
                "options": f.get("options"),
                "help": f.get("help", ""),
                "reason": f.get("reason", ""),
                "admin": bool(f.get("admin", True)),
                "apply": f.get("apply", "reload"),
                "editable": bool(editable),
                "source": source,
                "env": f.get("env", ""),
            }
            if f.get("secret"):
                item["value"] = "••••" if val else ""
                item["editable"] = False
            fields.append(item)
        groups.append({
            "id": g["id"],
            "label": g["label"],
            "desc": g.get("desc", ""),
            "admin": any(x["admin"] for x in fields),
            "fields": fields,
        })

    return {
        "groups": groups,
        "apply_order": ["live", "reload", "reconnect", "restart"],
        "overlay_path": SETTINGS_PATH,
        "overlay_keys": sorted(overlay_flat.keys()),
        "overridden": len(overlay_flat),
    }


def runtime_flags() -> Dict[str, Any]:
    """只读运行时状态：环境变量、真实下发是否被双重放行等（**不回传任何口令内容**）。"""
    from app.core.config import dotenv_loaded
    from app.services.motion import real_motion_env_active   # ★ P1-2：真值表唯一实现
    real_write = bool(merged_config().get("motion", {}).get("real_write", False))
    return {
        "env": [
            {
                "name": name,
                "label": label,
                "help": help_,
                "set": bool(os.environ.get(name)),
                "value": "" if "PASSWORD" in name else os.environ.get(name, ""),
            }
            for name, label, help_ in RUNTIME_ENV_KEYS
        ],
        "dotenv_loaded": bool(dotenv_loaded),
        "real_motion_env": real_motion_env_active(),
        "real_write": real_write,
        # 双确认缺一不可：env 打开 + config 打开
        "real_motion_active": bool(real_motion_env_active() and real_write),
        "settings_path": SETTINGS_PATH,
        "config_path": CONFIG_PATH,
    }
