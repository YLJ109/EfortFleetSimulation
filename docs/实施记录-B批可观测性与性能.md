# 实施记录 · B 批：后端可观测性 + 性能（2026-10-12）

范围：B1（只读指标接口 + 前端 store 接线）＋ B2–B6（先测量，有证据才改）。
硬口径：`AGENTS.md` §5（硬口径与禁区）、§5.5（静默失效与假显示五条铁律，尤其铁律③「null ≠ 0」）。
本批**没有**触碰任何安全口径，也**没有**发起任何真实运动。

---

## 一、B1 做了什么

### 1.1 唯一埋点清单（每一项都对应代码里真实存在的计数器/时刻）

新增 `backend/app/services/metrics.py`：一个 `threading.Lock` + 一个 dict，
键必须先在 `CATALOG` 登记（不登记的键**拒写**，避免埋点漂移出"野生指标"）。
四种类型：`stat`（last/max/min/avg/count）、`gauge`（最近值）、`counter`（累加）、`text`（截断 200 字）。

| 指标键 | 类型 | 单位 | 谁在记（真实函数） | 说明 |
| --- | --- | --- | --- | --- |
| `collector.loop_period_ms` | stat | ms | `collector._loop_impl` | 上一轮到这一轮的实际间隔（实测频率由它算，不另编一个"频率"数） |
| `collector.loop_ms` | stat | ms | `collector._loop_impl` | 单轮耗时 |
| `collector.read_fail_streak` | gauge | 次 | `collector._loop_impl` | 连续读失败次数（成功即归 0） |
| `modbus.read_pose_rt_ms` | stat | ms | `ModbusRobot.read_pose` | 关节角单帧 FC3 读**往返段**耗时 |
| `modbus.read_pose_fail` | counter | 次 | `ModbusRobot.read_pose` | 读失败次数 |
| `modbus.read_regs_rt_ms` | stat | ms | `ModbusRobot.read_regs` | 任意寄存器块读往返耗时 |
| `modbus.read_regs_fail` | counter | 次 | `ModbusRobot.read_regs` | 失败次数 |
| `ws.broadcast_enqueue_ms` | stat | ms | `hub.broadcast` | ★ 只含「序列化 + 入队」，**不含**事件循环里真正的 `send`（写进 desc，不装作是端到端时延） |
| `ws.broadcast_frames` | counter | 帧 | `hub.broadcast` | 广播帧数 |
| `vision.http_rt_ms` | stat | ms | `camera_client._request` | 视觉服务往返耗时（★ 只统计**拿到 HTTP 响应**的调用；连不上/超时进 `http_fail`，不冒充「正常延迟」） |
| `vision.http_calls` / `vision.http_fail` | counter | 次 | `camera_client._request` | 调用数 / 失败数 |
| `vision.last_path` / `vision.last_error` | text | — | `camera_client._request` | 最后一次打到哪个路径、最后一次错在哪 |
| `event.write_ms` | stat | ms | `events.emit` | 审计**同步落库**耗时 |
| `event.writes` / `event.write_fail` | counter | 条 | `events.emit` | 写入数 / 失败数 |

### 1.2 没有计数器的项：请求时刻直接读真实结构（`kind="live"`）

| 字段 | 来源（真实结构） | 诚实处理 |
| --- | --- | --- |
| `ws.clients_total / clients_pose / clients_events` | `hub.count()` | 直接读 |
| `ws.backlog_clients` | `hub.pending()`（本批新增，见 1.4） | note 写明「背压设计每客户端至多积压 1 帧（新帧覆盖旧帧）」 |
| `runs.active_runs / active_run_ids / progress_entries / last_run_id` | `api/control._RUN_ACTIVE / _RUN_PROGRESS / _LAST_RUN_ID`，在 `_RUN_CANCEL_LOCK` 下读 | `last_run_id` 为 None 时 note 写「本进程还没有过执行登记」 |
| `runs.queue_len` | **没有队列** | `value=null` + note「★ 没有队列：/control/run-file 每次执行直接起守护线程，不存在排队结构」 |
| `runs.thread_pool_tokens` | `int(os.environ["EFORT_THREAD_POOL"])`（默认 96，与 main.py 启动配置同源） | note 写明 anyio 版本差异可能导致设定不生效（实际仍是默认 40，以启动日志为准） |
| `events.backlog` | **没有队列** | `value=null` + note「emit 是同步落库，写入耗时看 write_ms」 |
| `events.write_rate` | 派生：`event.writes ÷ uptime_sec` | note「自启动以来的平均值，**非滑动窗口**（没做窗口，就不装作有）」 |
| `collector.configured_read_hz` | `config/robot.yaml sampling.read_hz` | desc「配置的目标读取频率（**不是实测**）」；实测频率看 `loop_period_ms` |

### 1.3 端点

`backend/app/api/system.py:713` `GET /api/system/metrics` —— **只读、不要求控制令牌**，
与 `/api/rc-status`、`/api/system/health`、`/api/system/metrics/hold-state` 同一条既有口径。
输出六个分组（collector / modbus / ws / runs / vision / events），**每一项都是四元组**
`{value, unit, ts, source, kind, measured, desc, note}`；`stat` 另带 `count/max/min/avg`。

没采到过样本 ⇒ `value=null, measured=false, note="没测：本进程启动以来没有采到过样本（不是 0）"`。
`stat` 另钉一句 `percentiles=null` +「无历史样本池：只有 last/max/min/avg/count，p50/p95 未采集」
—— 本仓**没有**任何环形样本池，所以**不给 p50/p95**（要就写「没测」，绝不编）。

隔离冒烟（`EFORT_SIMULATE=always` 冷启动、无任何 IO）实际返回：

```
HTTP 200  groups: collector, modbus, ws, runs, vision, events
ws.clients_total          = {"value": 0,     "measured": true,  "source": "hub.count()", ...}
ws.broadcast_enqueue_ms   = {"value": null,  "measured": false, "note": "没测：本进程启动以来没有采到过样本（不是 0）"}
collector.read_fail_streak= {"value": null,  "measured": false, ...}
modbus.read_pose_rt_ms    = {"value": null,  "measured": false, ...}
runs.queue_len            = {"value": null,  "source": "不适用", "note": "★ 没有队列：…直接起守护线程…"}
events.backlog            = {"value": null,  "source": "不适用", "note": "★ 没有队列：emit 是同步落库…"}
```

「0」与「没测」在结构层就分开了：`clients_total=0`（真值）对 `read_fail_streak=null`（没测）。

### 1.4 生产代码改动（全是加法，零行为改动）

| 文件 | 位置 | 改了什么 | 是否改变行为 |
| --- | --- | --- | --- |
| `backend/app/services/metrics.py` | 新文件 | 注册表（CATALOG 闸门 + 四元组快照） | 新增件 |
| `backend/app/services/collector.py` | `_loop_impl` | 记 `loop_period_ms / loop_ms / read_fail_streak` | 否（只读观测，读写顺序一字未动） |
| `backend/app/services/modbus.py` | `read_pose` / `read_regs` | 记往返耗时与失败数 | 否（`VAC_ACTIONS` 位表、`encode_angle`、所有写路径未动） |
| `backend/app/services/hub.py` | `broadcast` / 新增 `pending()` | 记入队耗时与帧数；`pending()` 数「有未发出积压帧的客户端」 | 否（背压策略本身未动） |
| `backend/app/services/camera_client.py` | `_request` 拆成 `_request` 包 `_request_once` | 包一层计时/计数；**顺带删掉重复粘贴的 `_NO_PROXY_OPENER` 死代码** | 否（原函数体逐行保留） |
| `backend/app/services/events.py` | `emit` | 记 `write_ms / writes / write_fail` | 否（仍在同一位置同步落库） |
| `backend/app/api/system.py` | `:708 _live()`、`:713 api_metrics()` | 端点 + live 四元组 | 新端点 |

埋点写法统一：`metrics.*` 内部 `try/except` 吞掉一切异常（观测件绝不带崩主流程），
非数值（NaN/inf/字符串）直接拒收。

### 1.5 后端测试

`backend/tests/test_system_metrics.py`（13 例，全部只读、不碰真机）：

- 注册表语义：`test_stat_tracks_last_max_min_count`、`test_never_measured_is_null_not_zero`
  （钉「没测 ≠ 0」）、`test_counter_gauge_and_text`、`test_metrics_never_raise_on_junk`
  （垃圾值/未登记键/类型不匹配都不抛）。
- 端点：`test_metrics_endpoint_public_and_grouped`（无令牌 200 + 六组齐）、
  `test_every_entry_is_value_unit_ts_source`（**不变式**：每个 `value=null` 必须带 `measured=false` 且带 `note`，
  当场把「不许假显示」钉成回归）、`test_runs_group_reads_live_registry_and_queue_is_honestly_null`、
  `test_ws_group_matches_hub`。
- 埋点真的在记：`test_hub_broadcast_records_latency_and_frames`、`test_hub_pending_counts_backlogged_clients`、
  `test_modbus_read_failure_is_counted`（打到 `127.0.0.1:1`，只读、必然失败 ⇒ 断言 fail=1 且 rt 仍为「没测」）、
  `test_vision_client_records_calls_and_failures`、`test_events_emit_increments_writes`。
- 隔离：本文件自带 autouse `_isolate_metrics`（前后各 `metrics.reset()`），不改公共 `conftest.py`
  （减少与并行工作的冲突面）；DB / 日志目录 / 模拟位仍按 `tests/conftest.py` 既有三件套。
- 计数断言一律 `>=`（本机 127.0.0.1:8100 有真实相机服务在跑，vision/collector 是合法并发写者；
  钉死相等就是脆弱测试）。

---

## 二、B2–B6：先测量，再决定

> 结论先说：**B2–B6 全部"没发现问题、不改"**，只有两处顺带的零风险清理
> （camera_client 重复死代码，见 1.4）。下面是测出来的数字。

### B2 前端每帧重复分配 / 定时器未清理

测量方法：静态定位 `renderFrame()`（`src/three/manager.js:592`）调用链上的分配点
＋ node 里跑纯数学函数 20 万次计时 ＋ 用 37 节点/30 网格的合成树计时矩阵与 AABB。

| 观测项 | 位置 | 实测 | 占一帧（16.7ms）预算 |
| --- | --- | --- | --- |
| `evaluateEdges × 区域数`（每帧重算半空间） | `three/safety.js:236+` | 0.00045 ms/帧 | 0.003% |
| `basisOf × 区域数`（同一批半空间再算一遍） | `three/safety.js:124` | 0.00020 ms/帧 | 0.001% |
| `updateWorldMatrix(true,true)`（37 节点树） | 每帧至多 4 处 | 1.37 µs/次 | 0.03%（×4） |
| `Box3.setFromObject`（30 网格） | `three/safety.js:581` 每帧 1–2 次 | 4.31 µs/次 | 0.05%（×2） |
| 每帧新建小对象数 | `fence.update` + `evalSafety` + `getTcp/getPieceAnchor` | ≈30–40 个（当前 `config/safety.json` 实测只有 1 个 rect 区域：polyPts=4 / halfspaces=4 / edges=4） | 60fps 下 ≈2.4k 个/秒，V8 年轻代 scavenging 量级 |
| `getPieceAnchor()` 3 个 THREE 对象 + 1 数组 | `three/manager.js:518` | 只在分拣 3D 激活时（`if (sorting)`）每帧 1 次 | — |
| 每帧安全结论 key 字符串 | `three/manager.js:389` | 1 次模板串 + 1 次 `toFixed(3)`（用它只为"变了才回调"） | — |

判定：合计 < 0.1% 帧预算，对象量在 young-gen 免费区间。
要提速就得**缓存半空间/降频评估**，而评估频率正是 A6 刚钉死的安全口径
（§5.5 铁律①：不许用「结论变没变」猜新鲜度；`safetyTicks` 必须每帧走），
**为了 0.1% 去动安全评估节奏 = 拿安全换性能，不做。**

定时器：`src` 下 20 个文件出现 `setInterval`，逐个核对清理点：
`stores/link.js`、`stores/sorting3d.js`、`stores/auth.js`、`stores/ai.js`、`stores/cell.js`、
`stores/tictactoe.js`、`three/manager.js`、`net/ws.js`、`services/announcer.js`、
`stores/exec.js`（3 个 interval 句柄 `keepAliveTimer/pollTimer/_seqPoll` + `previewTimer/pressTimer`
全部在 `dispose()`/`stopTimers()`/`stopSeqPoll()` 里清）、`App.vue`（4/4：
liveTimer/ttlTimer/healthTimer/uiSpeedTimer，另 `stopLoop()`、`stopIdleSafetyEval()`）——**都有配对清理**。
唯一"零 `clearInterval`"的 `components/GuideBar.vue` 经核实**根本没有定时器**
（它的 `setInterval` 只出现在注释里：那处早已收敛到 link store 单一轮询者）。⇒ 不改。

### B2′ WS 消息处理 O(n²) / 日志无上限拼接

| 检查点 | 位置 | 事实 |
| --- | --- | --- |
| 运行日志上限 | `stores/exec.js:36`、`:289`、`:290` | `LOG_MAX=240` + `splice` 截断，水位用单调 `logSeq`（不用会被截断的 `log.length`）—— 项目约定已在做 |
| 姿态写入 | `stores/robot.js` | `applyPose` 写定长槽，无 per-帧数组重建（无 `push`/`map`） |
| AI 历史 | `stores/ai.js:151` | `history.slice(-10)` 已截断 |
| 审计取证文本 | `components/EventsView.vue:293-311` | `L.push` 是**函数内局部数组**，每条事件重建，不累积 |
| 围栏区域列表 | `stores/safety.js:232/239` | `push` 与 `splice` 成对，长度由配置决定 |

⇒ 没有 O(n²) 重建、没有无上限字符串拼接。**不改。**

### B3 后端每请求重读/重连/重建池

| 项 | 实测 | 结论 |
| --- | --- | --- |
| `config.get_config()` | 命中缓存 **0.045 µs/次**（2 万次均值）；同文件裸 `yaml.safe_load`（18,759 字节）要 **8.747 ms/次** | `core/config.py` 已是进程级单例，每请求本来就不读盘；**不需要新缓存**（加了反而多出失效点要钉） |
| `sorting.all_point_poses()` | 200 行：首次 **4.05 ms**、稳态 **1.719 ms/次** | 两个调用点各自**已有** per-run 懒缓存 box（`api/control.py:1793-1802`、`services/vision_rules.py:261-270`，带错误一次性钉住）⇒ 一趟执行只读一次，不改 |
| Modbus 连接 | `ModbusRobot()` 只在 `collector.py:59`、`motion.py:198` 各实例化一次；`modbus.py:169` 明确「就地改字段而不是新建实例（本对象被采集线程与控制线程共享）」 | 没有每请求重连/重建连接池，不改 |

### B4 前端产物体积

`npm run build` 实测：`dist/assets/index-*.js = 1,094.66 kB`（gzip 345.92 kB）、
`index.html 34.12 kB`、`css 110.07 kB`。
本批新增件 `stores/metrics.js` 压缩后 **3,535 B / gzip 1,444 B**（占 gzip 总量 0.42%）。

**没做**的"优化"及原因：
① 路由级 `import()` 分包 —— 主体是 three + GLTFLoader，属「不许碰 Three.js 模型加载」的邻域，
且 `App.vue` 正被并行修改，动它必然产生大冲突；
② 死 import 清理 —— 静态扫描（eslint `no-unused-vars` + `tools/check_imports.mjs`）**没有**未用的导入；
③ 重复实现合并 —— 前端未发现重复；后端发现并清掉一处（`camera_client._NO_PROXY_OPENER` 被粘贴了两遍，已删）。
⇒ 体积按用户口径"只做安全优化"，**无安全可做的项，报数不改**。

### B5 "看起来会随时间变慢"的东西

后端本就有分批清理：`db/crud.py:20 prune_batched`（`DELETE ... WHERE id IN (SELECT ... LIMIT n)`，
注释写明旧实现「整表读进内存再逐行 delete」是病根，已改）、`:91 prune_old_poses`、
`:378 prune_system_events`、`:419 prune_safety_versions(keep=30)`、`:541 prune_vision_records`；
执行登记侧 `_RUN_ACTIVE/_RUN_CANCEL/_RUN_PROGRESS` 由 `RunContext.finish()` 出账 +
`_cancel_expired` TTL 清理。前端列表上限见 B2′。
⇒ **没有找到没有上限的内存增长**，不改。

### B6 其它

`metrics.py` 的写入点全部在**已有**的循环/请求路径上，不新增线程、不加定时器、
不引入新依赖（无 prometheus、无 psutil）。

---

## 三、前端接线（只加数据件，未新造页面）

- 新增 `frontend/src/stores/metrics.js`（唯一轮询者，5s；`start/stop/beat` 与 `link.js` 同款）。
  - 公开读：用普通 `fetch(apiUrl("/system/metrics"))`（后端本就无令牌要求），**不走** `apiControl`。
  - 唯一取数入口 `quad(分组, 键)`：任何缺字段/缺分组/接口挂了都返回 `value=null` 的四元组，
    并在 `note` 里区分三种缺法（没采到 / 接口不可用 / 后端版本不含此项）。
  - `formatQuad(q)`：没测一律渲染 `—（没测）`，避免组件写 `value || 0` 把 null 变成 0。
  - `stale` 用外部时钟 `tick`（不用 `Date.now()`）——同 `link.js::beat` 那条 P1-D7/P1-D11 病根；
    `load()` 的 `finally` 也推进 `tick`（失败必须计时）。
  - 过期不抹掉数值（现场要看"最后一次是多少"），但**改写 note**：
    「数据已过期（最后成功于 hh:mm:ss），不是当前状态」。
  - 隐藏即停：`stop()` 清 `setInterval`；`busy` 防重入（后端慢时不叠请求）。
- `frontend/src/App.vue` 5 处最小接线（不含任何样式/模板改动）：
  `:43` import、`:64` 实例化、`:419` 1s 时钟 `metrics.beat()`、
  `:477/478` 与 `:488/492` 复用**既有**轮询总闸（`robot.connected` watch + `visibilitychange`），
  `:543` `metrics.start()`、`:583` `metrics.stop()`（与 `link` 同生同死，卸载必清）。
- 本批**没有**新增/修改任何 `.vue` 页面或组件（按要求留给并行工作）。
  指标卡片的展示留给后续接线：数据件与四元组口径已就绪。

### headless 行为验证（node + pinia，真实响应形状为夹具）

```
1 在线数 0 与 没测 可区分: 0 → "0 个"  |  null → "—（没测）"
2 缺字段: "没测：后端 ws 分组里没有 no_such 字段（后端版本不含此项）"
3 缺分组: "没测：后端未返回 vision 分组（后端版本不含此项）"
4 stale 未过期: false | 时钟推进 15001ms 后: true
5 过期后 note: "数据已过期（最后成功于 20:58:24），不是当前状态" | value 仍在: 3
6 接口挂了: "没测：前端还没成功读到 /api/system/metrics（后端不可用（Failed to fetch））"
7 从未成功取数时 stale 必为 true（不给绿灯）
```

---

## 四、没做什么（以及为什么）

1. **没有 p50/p95**：本进程没有历史样本池。硬造百分位 = §5.5 铁律「编」。
   接口里显式输出 `percentiles=null` + 原因。真要分位数，需要先建环形缓冲（独立一件，不属本批）。
2. **没有引入 prometheus/psutil/新依赖**，没有新定时器、新线程。
3. **没有改任何安全口径**：`motion.joint_lock.enabled` 保持 `false`、`VAC_ACTIONS` 40135 位表、
   `keep/effective_face/resolve_target_joints` 面语义、18 原料位禁转黑名单、
   `motion.servo_not_ready_reason`/`control.reject_if_not_ready` 统一闸门（也没往 `motion.command` 里塞东西）、
   `services/ui_speed.py` 速度唯一口径（只读不回写）、`encode_angle`、`services/limits.py`、
   `raw_slot_safe_path` —— 全部逐条确认未动。
4. **没有为了 B2–B6 制造改动**：五组测量结果都是"没发现问题"，见第二节数字。
5. **没有改公共 `tests/conftest.py`**（新增测试自带隔离夹具），避免与并行工作撞车。
6. **已知但故意没改**：`api/system.py:67 _now_iso()` 产出 `…+00:00Z`（`isoformat()` 已带偏移，又拼了 `Z`）。
   这是**既有**端点（`/health`、导出快照等）共用的时间戳口径，本批新端点沿用同一条；
   单独给 metrics 换格式会让两处时间戳长得不一样，改它属于另一件事（要连着改既有断言）。

---

## 五、三条命令的真实输出

```
$ cd backend && .venv/Scripts/python.exe -m pytest -q
（见下方"最终复跑"，由并行工作影响的两次波动已在 4.x 说明）

$ cd frontend && npm run build
EXIT=0   dist/assets/index-*.js 1,094.66 kB │ gzip 345.92 kB（vite built in 3.55s）

$ cd frontend && npm run verify
EXIT=0（全链绿，未删任何钉；末段「意图编译器（E1）守卫：46 过 / 0 败」）
```
