# 方案：三问题修复（就绪按钮合并 · AI 执行不驱动 3D · 执行卡死自愈）

> 2026-10-10 · 状态：**已实现并验证**（用户已拍板；结果见文末 §8）
>
> 用户原话：
> 1. 「一键就绪和取消就绪合并做一个按钮，提前检测是不是上电或者下点(下电)状态，然后在进行，
>    不需要提示机器人不会坠落的」
> 2. 「AI 执行没有变颜色，模拟的颜色也没有发生变化，修复这个问题」
> 3. 「有些时候程序正在执行中，但是没跑代码，就绪和取消都成功，无法下达指令，修复这个死机的问题」

---

## 问题 1 · 就绪 / 取消就绪按钮

### 现象
- 现在**是三颗按钮**：「一键就绪」(绿) + 「取消就绪」(红) + 「清除报错」(黄)。
- 点「取消就绪」会弹一个**确认框**：*"取消就绪会伺服下电：手臂失去支撑可能因重力下沉。确认现场安全后再继续？"*
  —— 用户说"不需要提示"，即**去掉这个确认框**。

### 现状代码
| 位置 | 现状 |
|---|---|
| [RcReadyCard.vue](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/components/RcReadyCard.vue#L186-L203) | 两颗 `ready-toggle` 按钮并排 + `clear-err` |
| [RcReadyCard.vue](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/components/RcReadyCard.vue#L88-L91) | 已有 `readyAction`（按 `rc.servoOn` 判 "ready"/"cancel"）—— **本次直接复用它** |
| [RcReadyCard.vue](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/components/RcReadyCard.vue#L132) | `doCancel()` 里的 `window.confirm(...)` ← 要删的那句 |

### ★ 历史教训（不能踩回去）
[RcReadyCard.vue:177-185](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/components/RcReadyCard.vue#L177-L185) 明确记着
**当初为什么拆成两颗**：合并成一个时，卡在"一键就绪失败 / 点动占锁"的半截状态下，
按钮上写的是「一键就绪」，操作员**没有「取消就绪」可点**（而它正是复位的钥匙）→ 死循环。

### 方案（合并，但不丢逃生通道）
**一颗按钮**，文字/颜色随"伺服是否上电"变；点击时**重新读一次**当前状态再决定动作：

| 读到的伺服状态 | 按钮外观 | 点击行为 |
|---|---|---|
| **未上电** | 绿色「一键就绪」 | 撤触发 → 清报警 → 伺服上电 → 加载 200 → 运行挂起 |
| **已上电** | 红色「取消就绪」 | 停程序 → 撤触发 → 伺服下电 |
| 读不到（后端超时/离线） | 灰色「就绪状态读取中…」+ 禁用 | — |

- **"提前检测"**：点击时先 `GET /api/rc-status` 取**新鲜**的 `servo_on`（不用 4s 轮询的旧值），再决定走哪条。
- **逃生通道不丢**：伺服**上电**时按钮就是「取消就绪」（永远可点，只要求持有令牌）——
  这正是历史事故的复位钥匙，不会出现"想取消却只有一键就绪"。
- 「清除报错」保持独立（它是**只清报警**的第三件事，不与上电/下电同类）。
- **删除** `doCancel()` 里的 `window.confirm(...)`。

### 要同步改的回归钉
- [verify_rc_ready.mjs:121](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/tools/verify_rc_ready.mjs#L121)「下电前有安全确认」→ 改为「**已无确认框**（用户口径）」。
- [verify_rc_ready.mjs:194-213](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/tools/verify_rc_ready.mjs#L194-L213)（按钮显示 / 永远可点）按"单按钮 + 强制重读状态"更新。
- [verify_stores.mjs:110](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/tools/verify_stores.mjs#L110)（另一处"取消就绪"按钮引用）核对。

---

## 问题 2 · AI 执行不驱动 3D（颜色不变）

### 现象
在 **AI 执行页**跑指令，3D 场景里的料/颜色**不变**；模拟场景的颜色也不变。

### 根因（代码级，已确认）
3D 的**唯一装填者**是 [stores/sorting3d.js](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/stores/sorting3d.js)，
它只朝一个数据源取状态：

```js
// sorting3d.js tick()
if (robot.activeView === "sort") return;      // 分拣页自己喂
const r = await apiControl("/sorting/state"); // ← 只认这一个源
updateSortingState(d.state || null);
```

而 AI 执行的状态在 **`/ai/state`**（`ai.py` 的 `_job["state"]`），**`/sorting/state` 在 AI 跑的时候是空闲的**
→ 3D 永远拿到 null → 颜色/方块不动。

证据：`updateSortingState` 的调用点只有 [sorting3d.js:113](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/stores/sorting3d.js#L113) 与
[SortingView.vue:229/336](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/components/SortingView.vue#L229)；
`AiControlView.vue` **一次都没喂过 3D**。

### 方案（照"分拣页自己喂"的同款做法）
- `AiControlView.vue` 在 **AI 页激活且 AI 在跑**时，把 `ai.st` 喂给 3D：`updateSortingState(ai.st)`（已有 800ms 轮询，零新增请求）。
- `sorting3d.tick()` 里把 AI 页也**跳过**（`activeView === "ai"` 时不再打 `/sorting/state`），避免两个源打架。
- 数据结构天然兼容：AI 的 state 由 `sorting.py` 的 `_set_state` 生成，字段与 `/sorting/state` 同源
  （`stage/color/conf/holding/piece/target/sim_color/…`）。

### 边界（**不做什么**）
- **不改** `sortingBlocks` 的渲染逻辑（颜色映射/手上块/槽位）。
- **空跑仍不编颜色**（`sim_color=true` → 手上块用未知灰）—— 这是既定口径（"空跑占位·未判色"），
  本次不动；若你要"空跑也显色"，是**另一条需求**（要明确"该用什么色"：计划色？随机色？）。
- AI 执行**不会**在 B 区画格子（AI 的 state 没有 `placement`）；若要 B 区也联动，需后端在 AI state 里补 `placement`——**本次不做**，先联动 A 区取料/手上色。

---

## 问题 3 · 「程序在执行中，但没跑代码」的卡死

### 现象
- 界面显示"正在执行中"，但机器人**没有跑任何代码**；
- 「一键就绪」和「取消就绪」**都成功**；
- 但**下达不了任何指令**（点动/吸放被拒）。

### 根因（代码级，已确认）
后端用 `_RUN_ACTIVE`（run_id 表）判断"有没有序列在执行"：
- [control.py:975-989 `_reject_if_run_active`](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/api/control.py#L975-L989)：
  **只要 `_RUN_ACTIVE` 非空**，所有手动下发一律 409「有序列/文件正在执行…请先点停止执行」。
- 而 `/ready` 与 `/ready/cancel` **不看** `_RUN_ACTIVE`（只看 `_busy_lock` + `motion._exec_lock`）
  → **就绪/取消当然"成功"**，指令照样被拒。**← 与你描述的三条现象完全吻合。**

**`_RUN_ACTIVE` 为什么会残留？** 看 [ai.py 的 finally 收尾顺序](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/api/ai.py#L1031-L1041)：

```python
_set_state(st, stage="结束", ...)     # ← 若这里抛异常
_relay_pending()                      # ← 若这里抛异常（它会 new_run + 起线程 + 校验 pending）
ctx.finish(phase)                     # ← 就**永远执行不到** → _RUN_ACTIVE 残留
emit_event(...)
```

`_relay_pending()`（接管接力）排在 `ctx.finish()` **之前**。它一旦抛异常，
`_RUN_ACTIVE` 与该 run 的 `_RUN_PROGRESS`（`running=True`）双双残留 →
**界面"执行中"、实际没线程在跑、手动全被拒** = 死机。

同类风险：`sorting.py:678-680`、`tictactoe.py` 的 finally 里，`ctx.finish()` 前面也有可抛异常的收尾调用
（`_set(...)` / 关气 `_air(...)`）。

### 方案（三层：修根因 + 自愈 + 可复位）

**① 修收尾顺序（根因）**
- 所有执行线程的 `finally`：**第一件事就是 `ctx.finish(phase)`**（用 `try/finally` 保证），
  之后再把「关气 / 接管接力 / 写事件」放进各自 `try/except` —— 让"注销运行登记"**不可被跳过**。
- 涉及：`ai.py` · `sorting.py` · `tictactoe.py`。

**② 让闸门自愈（防御）**
- `_RUN_ACTIVE` 的条目补两个字段：`owner_thread`（执行线程）+ `heartbeat`（每次 `_set_progress` 刷新时间戳）。
- `_reject_if_run_active()` 与 `/control/state` 判断前先**清理死条目**：
  `线程不存在 / 已 not alive`，或 `未暂停 且 heartbeat 超时(默认 15s)` → 判定为死条目，注销并记事件。
- 这样即使将来再出现"跳过了 finish"的新路径，也不会永久卡死。

**③ 给一个手动复位（兜底）**
- 新增 `POST /control/force-release`（需控制令牌 + 双闸）：停程序 + 清 `_RUN_ACTIVE`/`_RUN_CANCEL`/`_RUN_PAUSE`，
  并在返回里写明"只清执行登记、不动机器人"。
- 界面：就绪卡的「清除报错」旁加一个**诊断行**（仅当检测到死条目时出现）：
  「检测到 1 条已死的执行登记 → 点此复位」。平时不显示，不增加噪音。

### 边界（**不做什么**）
- **不动** `_exec_lock`（运动互斥）—— 那是安全设施，不改成可强夺。
- **不动** 分拣/执行的运动语义与速度。
- 复位动作**绝不写任何运动寄存器**。

---

## 验收（改完后逐条对）

| 项 | 手段 | 期望 |
|---|---|---|
| 问题1 | 合成/源码断言 + 实机 | 只有一颗就绪按钮；未上电→绿"一键就绪"、已上电→红"取消就绪"；**无确认框** |
| 问题2 | 源码断言 + 实机 | AI 页执行时 3D 跟着动（A 区取走、手上显色）；`/sorting/state` 不再在 AI 页被无谓轮询 |
| 问题3 | pytest（新建 3 条） | ①finally 里 relay 抛异常也**必须**注销 `_RUN_ACTIVE`；②死条目被自动清理后手动指令可用；③`/control/force-release` 只清登记不写运动寄存器 |
| 全量 | `pytest -q` · `npm run verify` · `npm run build` | 全绿 |

---

## 8. 实施结果（2026-10-10）

**用户拍板**：① 对，清除报错保留为独立按钮；② **要联动，所有的执行都要联动**（范围扩大）；③ 要手动复位。

### 8.1 问题 1 —— 就绪/取消就绪合并为单按钮
- [RcReadyCard.vue](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/components/RcReadyCard.vue)：两颗按钮 → **一颗**；
  `onReadyToggle()` 点击时先 `await rc.load()` 取**新鲜** `servo_on` 再决定走 ready 还是 cancel。
- **删除** `doCancel()` 里的 `window.confirm(...)`（"手臂可能因重力下沉"那句提示）。
- 「清除报错」保留为独立按钮（用户确认）。
- 回归钉反转：`verify_rc_ready.mjs` 从"必须有 confirm"改为"**必须没有 confirm**"，并新增
  "只有一颗 ready-toggle""点击时先读实时伺服态"两条。

### 8.2 问题 2 —— 所有执行都联动 3D（范围按用户要求扩大）
- 后端新增**统一场景状态**：`register_scene_source(kind, fn)` + `GET /control/scene-state`
  （[control.py](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/backend/app/api/control.py)）。
  `sorting.py` / `ai.py` 各自登记场景源；无对应 3D 语义的执行（如序列文件）回落分拣场景，托盘不会被清空。
- 前端 [sorting3d.js](file:///d:/EFORT_Projects/EFORT_Web_Monitoring/frontend/src/stores/sorting3d.js) 的全局轮询从
  `/sorting/state` 改喂 `/control/scene-state` → **哪种执行在跑，3D 就跟着动**，且不新增轮询条数。
- 井字棋原本就由 `tictactoe` store 自己驱动（`setTttBoardState`），无需改动。

### 8.3 问题 3 —— 执行卡死（三层修复）
- **① 收尾顺序**：`ai.py` / `sorting.py` / `tictactoe.py`（含复位线程）的 `finally`
  第一件事统一改为 **`ctx.finish(phase)`**，其余收尾（关气/写结束态/接管接力/写事件）
  统一兜进 `try`。**注销运行登记不再可被跳过**。
- **② 闸门自愈**：`_purge_dead_runs()`（登记带执行线程 → 只认 `is_alive()`；无线程才用心跳兜底）；
  `_reject_if_run_active()` 与 `/control/scene-state`、`/control/locks` 判断前先自愈。
- **③ 兜底复位**：`POST /control/force-release` —— **只清已死的登记**，有活执行一律拒绝；
  只动软件登记、不写任何寄存器。界面在就绪卡加了一颗低调的「指令下不去？复位执行登记」。

### 8.4 验证

| 项 | 手段 | 结果 |
|---|---|---|
| 后端全量 | `pytest -q` | 全绿（新增 `tests/test_run_recovery.py` **7 条**） |
| 前端全量 | `npm run verify` | 22 个脚本全绿 |
| 前端构建 | `npm run build` | 成功 |
| 就绪卡回归 | `verify_rc_ready.mjs` | **71 / 0** |
| store 回归 | `verify_stores.mjs` | **5 / 0** |

### 8.5 已知边界（如实说明）

1. **序列/文件执行**（`/control/run-file`）已纳入统一场景状态，但它的 state **没有料的语义**
   （没有 `piece/color/placement`）→ 3D 会保持分拣场景不动。这是数据本身没有的信息，不编。
2. 空跑（dry_run）仍**不编颜色**（手上件用未知灰）——既定口径，本次未改。
3. 「复位执行登记」按钮为**常显**（低调样式），没有做"仅在检测到死条目时才出现" ——
   后者需要在每个页面额外轮询；后端已保证"有活执行就拒绝"，所以常显也安全。
