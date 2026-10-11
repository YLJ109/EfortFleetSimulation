# 交接文档：AI 新对话快速上手

> 用途：给**新开的对话 / 新接手的 AI** 看的"开机即用"交接档。
> 目标：只看这一份 + 按需跳转知识库，就能安全、正确地接手本项目。
> 更新：2026-09-25

---

## 0. 一句话提示词（直接粘贴到新对话）

我会在下面给你**加此外**，每次说"继续干活"即可：

```
你是 EFORT ER8-700H 工业机器人全栈 Web 监控与仿真系统（后端 FastAPI +
前端 Vue3/Pinia/Three.js）的维护助手。接手前提条件与安全红线见
projects 的 memory 与 docs/交接文档-AI新对话快速上手.md，必须逐条遵守：
1. 真机安全：程序执行/测试一律"测试跑=空跑校验"，只做 IK/限位/可达预演，
   绝不下发、不加载控制器程序、不移动机器人；未明确允许禁止一切真实运动。
2. 速度：所有动真机/示教/点动的速度必须锁定最慢档（5% 或 5°/s），
   J1-J5 关节锁定仅允许 J6 运动；J1-J5 偏差>0.5° 直接拒绝并返回明确错误。
3. 速度写 40103 地址（写 40102 无效）；**一切下发/点动都跟随右上角滑块**（唯一速度口径，
   1:1 直通），启动默认 **10%**、下限 5%、上限 100%。
4. 改动后必须跑：backend pytest + frontend npm run verify + 重新 build，
   并强制刷新（Ctrl+F5）验证 CSS/JS 哈希。
5. 后端每次改完代码必须重启进程才生效（.bat 无 --reload），且注意 8000 端口
   不要被多个 uvicorn 重复占用（会造成"真机卡显示 AUTO、底栏却未确认"这类假象）。
```

---

## 1. 项目定位与仓库

EFORT ER8-700H 工业机器人：**真实监控 + 3D 仿真的全栈 Web 系统**，三视图
（真实监控 / 模拟仿真 / 点位执行 / 程序执行 / 运维审计 / 系统设置 / 关于）共用同一个
Three.js scene 与机器人模型；后台连 PLC/控制器，走 **Modbus TCP** 读姿态、下发运动。

| 服务 | 端口 | 说明 |
|---|---|---|
| 后端 API + WS | `8000` | FastAPI，托管前端构建产物 `frontend/dist` |
| 视觉/相机 | `8100` | OpenCV +（禁用）YOLO，独立进程按需启动 |
| Vite dev | `5173` | 仅开发调试；生产由后端托管 dist |

机器人链路 Modbus TCP：`192.168.1.12:502`，配置在 `config/robot.yaml`。

目录：
```
backend/        FastAPI（app/core 配置 · api 路由 · services 业务 · db 模型/CRUD · middleware 中间件）
frontend/       Vue3 + Pinia + Three.js（src/three 3D · src/stores 状态 · src/net WS/控制请求）
config/         robot.yaml（DH/限位/连接/真实下发）+ frames.json 等
camera/         视觉服务（独立进程）
programs/       示教器导出程序（200.XPL、2001.XPL、411.XPL 等）+ color_sort.py
data/           SQLite（robot.db + WAL）；EFORT_DB_URL 可指独立盘
scripts/        启动/自检脚本；logs/ 运行日志
efort_panel_pyqt6/  独立 git 仓库（PyQt6 控制面板），自带版本管理，不在主仓库提交
```

---

## 2. 快速启动

```bat
:: 后端（先建 .venv；.env 复制自 .env.example，至少设 EFORT_ADMIN_PASSWORD）
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

:: 前端（改了前端代码）
cd frontend
npm install
npm run verify          % 112+ 项静态断言
npm run build           % 产出 frontend/dist
```

> **注意**：`.bat` 启动脚本**不带 `--reload`**，改后端代码后**必须重启进程**才生效。
> 本机历史踩坑：两个 uvicorn 同时绑 8000 → WS（真机卡）和 /system/guide（底栏）
> 由不同进程各自 state 承担，导致"卡显示 AUTO、底栏未确认"。改完代码**杀掉旧进程、
> 确认端口释放、再起一个新的**，然后 Ctrl+F5 验证。

---

## 3. 安全红线（最高优先级，逐条不可违反）

1. **写机器人控制相关代码时：禁止执行任何代码、机器人保持静止**——先出方案，确认后再优化执行。
2. 程序执行页的「测试跑」= **空跑校验**：只解析 + IK/限位/可达预演，**绝不下发、不加载、不移动**。
3. 速度：**全部跟随右上角滑块（唯一口径，1:1 直通）**，启动默认 **10%**、下限 5%、上限 100%。
   - 程序执行 move 的 `speed_pct` **必须统一用全局 `this.speed`**，
     禁用 `it.speed_pct || 100` 这类写死 100% 的写法。
   - 速度写 **40103**（40102 无效）。
4. **J1-J5 锁定，仅允许 J6 运动**：任何请求若 J1-J5 与当前偏差 >0.5° 直接拒绝，
   返回"J1-J5锁定，仅允许J6运动"。模拟环境同样锁定 J1-J5，保持一致。
5. 真机握手（伺服上电+200挂起+速度可调）未全部就绪前，后端**永远停在 dry_run(试运行)**，不下发真实运动。
6. 后端 404 / 执行错误信息要解析 `detail` 字段，给用户清晰的失败原因 + 可执行目标候选，避免 undefined。
7. 浏览器缓存：改 CSS/JS 后必须**强制刷新（Ctrl+F5）**取新哈希文件，否则看着像没生效。
8. 遥测默认：未连接机器人时姿态/读数显示 0，仿真演示动作保留、真实监控的离线演示动作移除。

---

## 4. 核心架构速记（新对话最该先懂的三处）

**A. 链路自检（唯一启停者 = App.vue 的 link store）**
底栏状态灯（机器人/摄像头/示教器档位）与引导条都只是 **link store** 的消费者，
只有一处轮询者，避免"两个轮询者界面自相矛盾"。`src/linkview.js` 定义灯色映射（ok/warn/err/off）。

**B. 示教器档位自动确认**
`backend/app/services/runmode.py` 通过 `observe()` 接收控制器状态字的 `auto/remote`，
读到 AUTO/远程即视为**已确认**（底栏绿灯"控制器实测"）。手动声明是兜底。
`/control/preview` 与 `/control/ik` 是**无令牌只读路由**（只算不发），模拟仿真页不因未登录刷 401。

**C. 速度三系统隔离**
| 系统 | 默认 | 用途 |
|---|---|---|
| 示教滑块 teachSpeed | 5% | 点位执行滑块示教下发 |
| 全局执行 speed | 5% | 点位/文件/程序执行共用 |
| 点动角速度 jogSpeed | 5°/s | 连续点动按钮（范围 1~10） |
手动拖滑块才能调，调后仍各自独立。

---

## 5. RPL 程序铁律与寄存器（改示教器程序时必看）

> 完整规范见 `docs/规范-RPL程序编写规范与200服务程序.md`（含铁律1~9、纯文本+XML双版代码）。
> 寄存器实测见 `docs/控制器Modbus寄存器勘察报告.md`。

- **【铁律1】** `WAIT` 只能等读区 `fidbus.mtcp_ro_b[n]`，**绝不能等写区** `fidbus.mtcp_wo_b[n]`，
  否则第一句置 false 永远等不到 → 卡死 WAIT（运行位常亮、40035恒0、机器人不动）。
- **【铁律2】** 多服务（点动/吸气/放气）**禁止串联阻塞式 WAIT**，用 `WHILE <触发位> DO ... END_WHILE;`
  门控，触发位为真才进服务体，主循环不阻塞。
- **【铁律3】** 每个服务独立触发位/完成位，不复用；新增往下取 `[3]`、`[4]`
  （点动已用 `ro_b[0]/wo_b[0]`，吸气 `ro_b[1]/wo_b[1]`，放气 `ro_b[2]/wo_b[2]`）。
- **【铁律4】** 纯文本 RPL：语句以 `;` 结尾、赋值用 `:=`、注释 `(* *)`、关键字全大写、
  布尔小写 `true/false`、取反 `NOT`、`WAIT(条件);`、`DWELL(秒);`。
- **【铁律5】** `.XPL` 是结构化指令 XML（Pous/Pou/Body/while/mjoint...），**禁止手写纯文本改后缀**（报 4902）。
  纯文本只用于示教器指令栏对照稿或墨斗离线 `.pgm` 源码；刻意不用 `<if>`（真实程序里没有）。
- **【铁律6】** 有效速度 = 程序内 `v` 常量 × 速度设定寄存器 **40103**；规范统一程序内写 `v50perc`，
  调速度只改 40103 不改程序（写 40102 无效）。
- **【铁律7】** 文件名以字母开头、仅字母/数字/下划线、≤31 字符；每个 `.pgm` 配同名 `.var`。
- **【铁律8】** 写 40139~40144 后**不能立刻置触发位**，必须回读校验 + 静置 0.15s 再触发。
- **【铁律9】** 改完程序必须点【保存】，否则修改是临时的。

寄存器速查：40001 状态位 · 40006 当前程序号 · 40101 指令字(急停0x1005/停止0x1004/清报0x1008/伺服上0x1001) · 40103 速度设定 · 40104 目标程序号 · 40135~40144 点动用户区。

三速默认最慢档 + J1-J5 锁定的改动**必须记入项目 memory**，防止未来三速串扰复发。

---

## 6. 关键 API 端点

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /healthz` | 无 | 存活探针 |
| `GET /api/system/health` | 无 | 健康总览 |
| `GET /api/rc-status` | 无 | 控制器实战状态（mode/bits.auto 等，档位自动确认数据源）|
| `GET /api/system/guide` | 无 | 底栏链路状态（含 run_mode.confirmed/source）|
| `GET /control/files` | 控制令牌 | 可执行候选（示教器 .XPL / 本地 .json / 数据库程序/点位分组）|
| `GET /control/preview`、`POST /control/ik` | **无令牌只读** | 残影预演 / IK 解算（只算不发）|
| `GET /api/programs`、`/api/points` | 无 | 数据库点位序列 / 预设点位 |
| `POST /api/auth/login` 等 | — | X-Control-Token（TTL 默认 1800s，改口令/重启后旧令牌作废）|

前端请求封装：监控类用普通 fetch（`src/config.js` 的 apiUrl）；控制类用
`src/net/control.js` 的 `apiControl`（自动带 X-Control-Token，遇 401 自动收回令牌）。
后端 `auth.py` / `app_settings.py` / 各 services。

---

## 7. 重要聊天记录 / 近期决策（时间线）

- **2026-09-25 程序执行页重构**：把示教器程序（.XPL）与本地程序（数据库点位序列/.json/预设点位）
  **分两栏**展示；每行「测试跑」= 空跑校验。后端 `control.py::_candidates` 分组
  （`teach`=.XPL，`local`=.json/DB），前端 `stores/exec.js` 的 `teachFiles`/`localFiles` getter，
  `ProgramExecView.vue` 双卡。此前曾把两类合并成一张卡被否——用户明确要求"本地和示教器分开"。
- **2026-09-25 档位自动确认 + 401 修复**：`/control/preview` 与 `/control/ik` 从 require_control
  改成**无令牌只读 router**（main.py 注册），消除未登录刷 401；runmode 接控制器实测档位。
- **2026-09-25 底栏"未确认"bug**：根因是**旧进程**还跑着改前代码 + 双 uvicorn 抢 8000，
  非逻辑 bug。杀旧进程、重启、Ctrl+F5 即绿。结论已写入本档第 2 节注意事项。
- **2026-09-25 git 提交**：`1d5874c`（154 文件，+32485/−1360），本机 main 领先 origin 1 提交。
  临时产物（.pytest_tmp*/、build_err.txt、vite timestamp、.pytest_out.txt）和独立仓库
  `efort_panel_pyqt6/`（自己带 git）已加入 .gitignore。
- **UI/UX 全面重构**：新设计令牌、径向渐变背景、卡片动效、三页共用 scene/canvas/模型，
  右侧栏瘦身（移除关节角卡、统一连接状态到机器人链路、程序执行右侧仅保留就绪盒、移除安全围栏调试仅留真实监控）。
- **视觉颜色识别**：移除 YOLO，保留颜色检测（矩形轮廓 + Lab 判色）；画框/置信度/标签**用中文**且美观；
  工业相机强制 BayerRG8；坏帧过滤 + 连续坏帧保护 + 看门狗/断流自愈（魔数 RECONNECT_GRACE_MS=8000、STREAM_STALL_MS=15000）。
- **功能整改项**：摄像头低位仰视、物体摆放 3D 拖拽/坐标定位/碰撞防穿透/自动贴合/持久化 localStorage、
  源盒 3×3 默认 9 物体（深烟灰金属→识别变红绿蓝）、盒宽滑块 0.4~1.0m、机器人步骤录制用类代码时序列表
  （点位 J1~J6、吸气 io.DOut[8]、放气、等待可指定 ms，支持顺序调整/断点回放）。

---

## 8. 常用命令速查

```bat
:: 后端回归
cd backend
.venv\Scripts\python.exe -m pytest tests -q --basetemp=".pytest_tmp" -p no:cacheprovider

:: 前端静态断言 + 构建（改完必跑）
cd frontend
npm run verify
npm run build

:: 强制刷新取新资源（用户浏览器）
Ctrl+F5
```

---

## 9. 知识库索引（按主题跳转）

| 主题 | 文档 |
|---|---|
| 程序执行分栏 / 测试跑 | `ProgramExecView.vue` + `stores/exec.js` + `backend/app/api/control.py` |
| RPL 全套规范 + 200 服务程序（铁律+代码+错误对照）| `docs/规范-RPL程序编写规范与200服务程序.md` |
| Modbus 寄存器实测 | `docs/控制器Modbus寄存器勘察报告.md` |
| 控制器 Modbus 客户端实现 | `backend/app/services/modbus.py` |
| 点动服务程序缺失排障 | `docs/点动服务程序缺失排障.md` |
| 部署/启动/备份/鉴权/回归 | `docs/部署文档.md` |
| 项目全面知识库（AI 接管初版，9/23）| `docs/交接文档-项目全面知识库（AI接管版）.md` |
| 各阶段方案（三页同步、UIUX、点位执行、视觉分拣、物体摆放等）| `docs/方案-*.md` |
| 项目硬约束 / 已跑通的决策 | 本机 memory：`projects/-d-EFORT-Projects-EFORT-Web-Monitoring--p2-*/project_memory.md` 与各会话 `topics.md` |
| 用户偏好（中文/美观/先方案后执行/低速测试等）| memory `user_profile.md` |

---

> **接手提醒**：先读本档第 3 节安全红线 → 第 4 节架构 → 第 7 节近期决策；
> 动手改代码前先明确"只出不碰"（甚至不运行机器人），方案确认后再优化执行。