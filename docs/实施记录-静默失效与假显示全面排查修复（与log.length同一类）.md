# 实施记录 · 静默失效与「假显示」全面排查修复（与 `log.length` 同一类）

> 起因：修完「运行日志满 240 行后不再自动滚到底」后，用户问"**还有哪些这种问题，全面检查一下？**"，
> 随后拍板"**全部进行修复**"。
>
> **"这种问题"= 一类共性缺陷**（不是零散 bug）：
> 1. **水位/变化判据不可靠** —— 用了"不会随内容变化而变化"的值判"变了没"；
> 2. **参数写死** → 开关/控件**静默失效**（点了没用，界面却以为有用）；
> 3. **界面与实际不符（编故事）** —— 显示的是本地推断/承诺，不是后端真实口径；
> 4. **同一状态多处判据** → 同一屏两个结论打架；
> 5. **死状态/假功能** —— 定义了没人用，或描述的功能早已不存在。

**总原则（本项目的"不去编故事"）**：**下发必须回读确认；推断必须标注来源；不知道就说不知道。**
下面每一处修复都对应这条原则的一条具体违反。

---

## 一、安全相关（最高优先）

| # | 问题 | 根因 | 修法 |
|---|---|---|---|
| A1 | **视觉规则"安全缺省速度"从未生效** —— 一条没设 `speed_pct` 的视觉规则命中后，仍以 **100%** 下发程序（相机事件**无人值守**触发，这是最危险的默认值） | `vision_rules.py::normalize_rules` 里 `int(it.get("speed_pct") or 100)` 把"没设"**落盘成 100**；而 `execute_rule` 里 `clamp_speed(rule["speed_pct"])`（注释声称"缺省改为安全下限"）拿到 100 还是 100 | 缺省/非法 → `clamp_speed(None)` = `SPEED_MIN(5)`；越界仍夹到 `[5,100]`。**唯一速度口径** |
| A2 | 底栏「真机链路」把**未知**报成**未上电** | `rc.servoOn` 在后端未返回 `servo_on` 时是 `null`，而 `rc.servoOn ? "未就绪" : "未上电"` 把 `null`（三元的 falsy）当成"没上电" | 三态：`null → "未知"` / `true → "未就绪"` / `false → "未上电"`（与 `RcReadyCard` 的 null 回落口径对齐） |
| A3 | `link.stale`（自检数据过期守卫）**在掉线时自己失效** → 摄像头/示教器灯保持"假绿" | `stale` 用 `tick` 计时，而 `tick` 只在 `load()` 里推进；掉线时 `App.vue` 会 `link.stop()`（不再轮询）→ `tick` 冻结 → 过期永远算不出 `true` | 新增 `link.beat()`，由 **App 的 1s 时钟无条件调用**；"多久没更新"才真正随真实时间增长 |
| A4 | `rcReady.stale` 是**死守卫**（定义后无人读），且判据用 `Date.now()`（非响应式依赖 → 只会在 `snap/wsAt` 变化时重算，而它们**恰好在 WS 帧停掉时不再变化**） | 同上"水位靠数据推进" | 新增 `clk` + `rcReady.beat()`（App 1s 时钟推进）；`stale` 改比 `clk`；并在**底栏真正消费它**：过期 → 灯压黄 + 显示"数据过期"（**绝不报绿**） |

**新增回归钉**
- `tests/test_vision.py::test_rules_missing_speed_defaults_to_safe_min`（缺省→5 / 越界→100 / 显式→原值 / 0→5，且**落盘值**就是 5）
- `frontend/tools/verify_bottom_dock.mjs` **F 组**（beat 接线 / `stale` 用 `clk` 不用 `Date.now()` / 过期不报绿 / `servoOn` 三态）

---

## 二、界面与实际不符（"编故事"）

| # | 问题 | 根因 | 修法 |
|---|---|---|---|
| B1 | AI 页「开始分拣」**永远空跑**，运行日志却写"**真实下发**"；且**速度不跟滑块** | `api/ai.py` 写死 `StartIn(dry_run=True, dual_face=dual)`（**不读** `body.dry_run`，也没传 `speed_pct`）；前端 `stores/ai.js` 按**本地**开关打印口径 | 后端：**尊重 `dry_run`** + 传 `speed_pct`，回包带回**实际** `dry_run` 与如实文案；前端：口径**以后端回包为准**（`typeof d.dry_run === "boolean" ? d.dry_run : this.dryRun`） |
| B2 | Q 键「显示 / 隐藏摄像头画面」是**假功能** —— 只弹提示、画面照旧 | `ui.camVisible` 只被读写，**没有任何组件消费** | 让 `CameraPanel` 真正响应（根节点 `v-show="ui.camVisible"`，**保活**取流与拖拽/尺寸状态） |
| B3 | 手柄帮助卡与**真实动作**不符（**涉及运动**）—— 仍写「Y = 复盘 ⇄ 退出 / View ×2 = 复位」，实际两者都是**真机「复原」** | 上一轮把 `replay`/`reset` 合并为「复原」后忘改文案（`gamepad.js::ACTION_CN`、`GamepadTestCard.vue::MAP_ROWS/SIMS`） | 三处文案统一改为「复原（真机）」；动作**名**保留（多处映射与回归依赖） |
| B4 | 程序执行页**同一屏两个结论**：就绪横幅写"可以执行代码"、按钮却是灰的 | 横幅 `isReady = robot.connected && rc.ready && auth.controlActive`；按钮用 `exec.canExec`（还含"残影预演不安全/遥测过期"）；序列「执行」按钮**根本不含就绪门控** | 横幅改 `computed(() => exec.canExec)`、原因取 `exec.blockReason`；`runSeqDisabled` 用 `!exec.canExec`；并在 `stores/exec.js::runSeq` 里对**非空跑**加同一道门控（不只靠按钮置灰） |
| B5 | 底栏「控制中」**注释承诺了但代码没做**（引入 `rc` 后零引用）—— 拿到令牌就只显示"管理员"，不管控制器是否就绪 | 注释写了"要以控制器就绪为准"，实现没跟 | 未就绪显示 **"· 待就绪"** 并压琥珀色，`title` 带统一未就绪文案 |
| B6 | 3D 的「绿」与**权威色卡不是同一个绿**（而该表注释自称"与视觉/后端同名同色"） | `SORT_COLOR_HEX.绿 = 0x2e9e4f` vs 色卡 `#1FA74A`（`camera/vision_color.py`、`SortFlow.vue`） | 统一为 `0x1fa74a`（井字棋「叉=绿」复用本表，一并纠正） |
| B7 | 分拣页顶部注释说"右侧 ① 就绪（复用 RcReadyCard）"，模板里**没有** | 注释漂移 | 更正注释：本页**没有**页内就绪入口，由底部「真机链路」片承担；并写明 `canStart` 不含就绪门控（由后端统一闸门兜底） |

**新增回归钉**
- `tests/test_ai.py::test_ai_exec_start_honors_dry_run_and_speed`（dry_run 透传 + 速度透传 + 回包如实）
- `frontend/tools/verify_aichat.mjs`（E 段扩：三者透传；**G 段**：前端口径以后端为准）
- `frontend/tools/verify_live_capture.mjs` **E4/E5**（Q 键真生效）
- `frontend/tools/verify_not_ready.mjs`（横幅/按钮/`runSeq` 三门控同源；底栏"待就绪"）
- `frontend/tools/verify_sorting3d.mjs`（3D 绿 = 色卡绿 = 流程色标绿）

---

## 三、水位/签名不可靠（与 `log.length` 同一病根）

| # | 问题 | 根因 | 修法 |
|---|---|---|---|
| C1 | 日志/回执在"只有某字段变化"时**不重记** | 签名漏了下面日志会用到的字段：`stores/ai.js` 漏 `conf`/`sim_color`；`SortingView.vue` 漏 `last_point` | 签名补齐（**"签名必须包含所有被消费字段"**） |
| C2 | 语音**连说两遍一模一样的话 → 第二句被静默吞掉** | 定稿句用 `finalText`（**值本身**）当 watch 的水位 → 同文本赋值 Vue 认为没变 → watch 不触发 | 新增单调递增 `finalSeq`，消费方 watch 它（文本仍从 `finalText` 读） |
| C3 | 常量多处重复（改一处漏两处） | 工艺时序 500ms 在 AI / 井字棋 / 分拣 **三处**各写一份；占位色三元组又抄一份 | 统一到 `services/sorting.py`：`SUCK_HOLD_MS / BLOW_MS / DWELL_MS` **唯一来源**；`_DRY_PLACEHOLDER_COLORS = S.COLORS`；`StartIn` 缺省与 `_put_back` 缺省都引用常量。跨语言的槽位名单补**互相指向的注释**（前端 `A_SLOTS/B_SLOTS` ↔ 后端 `services/sorting.py::A_SLOTS`） |
| C4 | 全局速度缺省前端 **10%** / 后端 **5%**（看似不一致） | 两层**故意不同**：前端 = 界面默认（用户要求 10%），后端 = "客户端没带 speed_pct"时的安全兜底 | **不改值**，在 `core/safetyConst.js` 写明"这是有意的、别顺手改后端缺省"（与井字棋那次同一手法） |

---

## 四、死状态 / 假功能（清理）

| # | 处理 | 说明 |
|---|---|---|
| C5-1 | `camera.js` 的 `yoloEnabled / yoloLoaded / yoloLoading` **删除** | YOLO 已从项目移除（现场只用颜色检测），这三个 getter 描述的功能**根本不存在** |
| C5-2 | `camera.js::detConf` 注释更正 | 原注释写"（YOLO）"—— 那是已移除的目标检测，与本字段（颜色检测置信度）无关 |
| C5-3 | `camera.js::failCount`、`link.ready/attention`、`exec.jogState/jogActive/holdingDir/vacuumErr` **注释改为如实** | 这些都是"只写不读 / 只被无消费方的 getter 读"。**不删**（低风险），但把"供界面显示 xx"这种**承诺了不存在显示**的注释改掉 —— 这正是"编故事"的另一种形态 |
| C6 | `CameraPanel` 截图提示定时器、`EventsView` 输入防抖定时器 | 原来未持句柄、卸载时不清 → 卸载后回调仍会跑。现持有句柄并在 `onBeforeUnmount` 清掉 |

---

## 五、验证结果（本轮收尾）

| 层 | 命令 | 结果 |
|---|---|---|
| 后端 | `cd backend && .venv\Scripts\python.exe -m pytest` | **691 passed**（上轮 689，+2 新钉） |
| 前端回归 | `cd frontend && npm run verify` | **全部通过**（新增/加固 F、E4-E5、G、门控同源、绿色一致等钉） |
| 前端构建 | `cd frontend && npm run build` | **0 error**（117 modules） |

---

## 六、明确没做（划清边界）

- **没有**把后端各请求模型的 `speed_pct` 缺省从 5% 改成 10%（那是脚本/直连 API 的**安全兜底**，
  界面默认 10% 是另一层；见 `core/safetyConst.js` 的注释与 §三 C4）。
- **没有**删除 `link.attention/ready`、`exec.jogState/jogActive/holdingDir/vacuumErr` 等"预留但未接线"的字段
  —— 只把误导性注释改成如实（删它们要连带清理多处写入点，收益小、风险高）。
- **没有**改 AI 页/井字棋之外的"空跑默认"口径。
- **没有**动 §5.3 的任何硬口径（40135 位表 / 18 原料位禁转 / `keep` 面语义 / 未就绪闸门 / 速度跟随滑块）。
