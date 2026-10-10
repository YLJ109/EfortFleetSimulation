# 方案：B1 根治（`/run-file` 异步化）· B2 复核（执行锁分段 —— 建议不做）

> 2026-10-10 · 状态：**B1 已实现并验证；B2 复核结论 = 不做**（见 §5）
>
> 来源：静态评审 B1 / B2 的后续。B1 = **做**；B2 = 复核后**建议不做**（证据见 §2）。

---

## 1. B1 —— `/control/run-file` 异步化（让长任务不再占线程池）

### 1.1 现状（已核实）

| 事实 | 证据 |
|---|---|
| `/control/run-file` 是**同步**端点，**整段执行都在请求线程内** | [control.py:1443 起](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/api/control.py#L1443)（登记 → 逐步下发 → 组装 payload → finally 注销） |
| 一次程序执行可能**几分钟** → 一直占着 anyio 的 1 个 worker | 与 `motion._exec_lock` 的等待无关；纯粹"请求不返回" |
| 前端**已经**在 500ms 轮询 `/control/run-state` 画进度 | [exec.js `startSeqPoll`](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/stores/exec.js#L1536-L1552)（读 `s.index / s.total / s.paused`） |
| 但**逐步结果**仍来自 `/run-file` 的**响应体** | `runSeq`：`d.count` / `d.steps` / `d.ok`；`runFile`：`this.fileResult = d` |
| 404（未找到可执行目标）**必须同步返回**（前端读 `detail.candidates` 给候选列表） | [exec.js:1036-1039](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/stores/exec.js#L1036-L1039) |

### 1.2 要做的（后端）

1. **解析/校验留在请求内**（不动 404 + candidates 语义）——
   即 `_resolve_file` / `_norm_seq_items` 仍同步做，失败照旧 `raise HTTPException(404, ...)`。
2. 校验通过后：
   - 用 `ctx = RunContext(run_id, disp_name, kind="file")` 取代现在手写的
     `_RUN_ACTIVE[run_id] = {...}`（**run_id 仍用前端传的**，P0-7 精确中止不变）；
   - `th = threading.Thread(target=_run_file_job, args=(ctx, ...), daemon=True)` + `th.start()` + `ctx.set_thread(th)`；
   - **立即**返回 `{"ok": True, "started": True, "run_id": run_id, "name": disp_name}`。
3. 线程内：`db = SessionLocal()` **在线程里开/关**（请求返回后不能再用请求作用域的 session）；
   `tok` 字符串捕获进线程（`emit_event(actor=_actor(tok))` 用）。
4. 线程的 `finally`：**第一行 `ctx.finish(phase)`**（沿用本轮死机修复的口径），
   并把最终结果写进进度快照：
   `_set_progress(run_id, running=False, phase=phase, result=payload)`
   （`payload` = 现在那一个 `{ok,name,kind,count,passed,run_id,steps,...}`）。
5. 异常路径：把 `{"ok": False, "error": ...}` 也写进 `result`，避免"跑挂了但界面什么都没有"。

### 1.3 要做的（前端）

1. `runFile` / `runSeq`：POST 返回后若 `d.started` → **不再**把它当结果；
   置 `fileResult = null`、`runBusy = true`，然后 `startSeqPoll()`（**试运行也轮询** —— 它也可能耗时）。
   404 / 401 分支保持不动。
2. `startSeqPoll` 增强：当 `s.phase ∈ {done, cancelled, error}` 且 `s.result` 存在时：
   - `this.fileResult = s.result`；
   - 补 `runStep/runTotal/lastRun`（原来从响应体拿的那几项，改从 `s.result` 取）；
   - `this.running = false; this.runBusy = false;`；
   - `stopSeqPoll()`（终态后停止轮询）。
3. 轮询超时/断网：保留现有"轮询失败不影响执行本身"的容错；另加一条"超过 N 秒仍无终态 → 如实提示"。

### 1.4 明确**不做**

- ❌ 不改执行体（逐步下发逻辑、触发位时序、`_exec_lock` 用法）—— **一行都不动**。
- ❌ 不改 `RunContext` / `_purge_dead_runs` / `force-release` 的语义。
- ❌ 不改 `/control/run-state` 的既有字段（只在 `state` 里**新增** `result`）。
- ❌ 不引入任务队列/线程池框架（就用 `threading.Thread`，与 `/ai/exec`、`/sorting/start` 同款）。

### 1.5 验收

| 项 | 手段 | 期望 |
|---|---|---|
| 立即返回 | 新增 pytest | `POST /run-file` 在**执行体还没跑完**时就返回 `started=True` |
| 结果不丢 | 新增 pytest | 跑完后 `/control/run-state` 的 `state.result.steps` 与现在响应体里的 `steps` 等价 |
| 405/404 语义不变 | 现有 pytest（`test_seq_editor` 等） | 全绿 |
| 并发保护不变 | 现有 pytest | 执行期间手动下发仍被 `_reject_if_run_active` 拒 |
| 死机自愈仍生效 | `test_run_recovery.py` | 全绿 |
| 前端回归 | `npm run verify` + `npm run build` | 全绿 |
| 手工 | 跑一条 ~30 步序列 | 进度条/逐步结果/暂停/停止 照旧 |

---

## 2. B2 —— 执行锁分段：**复核后建议不做**

### 2.1 复核结果（与评审时的推断不同，这里更准）

| 事实 | 证据 | 含义 |
|---|---|---|
| `motion.command()` 用 **`acquire(blocking=False)`** → 并发请求**立刻失败**（回 `busy`），**不是**排队等待 | [motion.py:114-121](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/services/motion.py#L114-L121) | "别的请求被锁卡住 30s"**不成立** |
| 锁内那段"等完成位"在 `modbus.rc_jog_execute` 里，**已经可被急停/watchdog 打断** | `should_abort=_abort_now`（[motion.py:247-260](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/services/motion.py#L247-L260)） | 不是"不可中断的长阻塞" |
| **唯一**会等锁的是"一键就绪"的补救路径（`acquire(timeout=wait_s)`），且它**本身就是设计好的行为** | [rc_ready.py:153](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/services/rc_ready.py#L153)、§5.3 口径 | 属"正确拦截 + 有补救"，不是缺陷 |
| 分段 = 要把 `_exec_lock` 拆成"通道占用标志 + 短临界区互斥" | — | 这是 §5.3 **安全互锁**的重构 |

### 2.2 结论

**收益小（本来就快速失败，不是慢）、风险大（真机互锁）→ 建议不做。**

若将来确有需求（例如点动要"边等边让别的读操作进去"），必须：
① 单独出方案；② 明确"已触发、未确认"窗口内哪些操作**允许**、哪些**必须继续拒绝**；
③ 真机**空跑**验证 + 全量回归；④ 现场确认后再上。

> 我现在**不改** `motion.py` / `rc_ready.py` 的任何锁语义。

---

## 3. 落地顺序

1. 先做 **B1**（后端 + 前端 + 测试 + build），全量回归；
2. B2 **只落这份"不做的结论"** 到 `AGENTS.md` §7（已有一行，补一句"复核后不做，原因见方案"）。

---

## 5. 实施结果（2026-10-10）

### B1 —— 已落地

**后端**（[control.py](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/api/control.py#L1451-L1500)）
- `POST /control/run-file` 改为**立即返回**：请求内只做**同步预检**（解析目标；拿不到照旧 404+候选 / 400）；
  通过后 `RunContext(run_id, disp_name, kind="file")` → 起后台线程 → 返回
  `{ok, started, run_id, name, kind}`（多带一个 `kind` 供前端步骤高亮）。
- 原执行体**一行未改**地搬进 `_run_file_job(ctx, body, tok, run_id, disp_name)`；
  其 `finally` 第一件事是 `ctx.finish(...)`，随后把**最终 payload** 写进
  `_set_progress(run_id, result=payload)` → 前端从 `/control/run-state` 的 `state.result` 取。
- DB 会话在线程内开/关；`tok` 捕获进线程（事件审计的 actor 不变）。

**前端**（[exec.js](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/stores/exec.js)）
- 新增 `_awaitRunResult()`：边等边把进度喂给界面（`runState/runStep/runTotal/runPaused`），
  拿到 `state.result` 就返回（**字段与老响应体完全一致**），并有超时兜底（如实回报，不假装成功）。
- `runFile` / `runSeq`：POST 后若 `d.started` → 走上面那条路；否则按老逻辑（**兼容老后端**）。
  `runSeq` 会先 `stopSeqPoll()`，避免两个轮询打架。

**验证**

| 项 | 手段 | 结果 |
|---|---|---|
| 立即返回 + 结果不丢 | `tests/test_seq_editor.py::test_runfile_items_mode_dry_run`（改写为**新契约**） | PASS（断言 `started` 且 **响应体不再有 `steps`**，结果从 run-state 取） |
| 404 / 400 同步语义不变 | 同文件 `test_runfile_needs_a_target` / `test_runfile_items_rejects_unknown_op` | PASS |
| 前端契约钉子 | `verify_stores.mjs` 新增 **D1~D3** | 8 passed / 0 |
| 后端全量 | `pytest -q` | 全绿 |
| 前端全量 / 构建 | `npm run verify` / `npm run build` | 全绿 / 成功 |

### B2 —— 只落结论（未改任何锁代码）
`motion.py` / `rc_ready.py` **一行未动**。结论与证据见 §2；已同步进 `AGENTS.md` §7。

---

## 6. 待确认

- ~~B1 按上面做，可以吗？~~ → **已确认并落地**
- ~~B2 **不做**（只记录结论），可以吗？~~ → **已确认**
