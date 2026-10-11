# 方案 — 前端 UI 全面体检与整改（文案 · 盒子 · 布局）

> **本次只出方案，不改任何代码。** 全文所有问题均带 `文件:行号` + 关键代码/文案摘录，
> 无证据的不写；某页确实干净就明确写「未发现问题」。
> 体检口径对齐 `AGENTS.md` §5/§8、`README.md` §9：**紧凑（gap/padding ≤ 10px）、组件不要 margin-bottom、
> 不引入 UI 框架、不动控制/速度/安全逻辑、指示必须取自真实状态（不得"编故事"）**。

体检范围：`frontend/index.html`（全局样式与设计令牌）+ `frontend/src/App.vue` + `components/*`（28 个）
+ `stores/*.js`（写给用户看的字符串）+ `tabs.js / guide.js / linkview.js`。

---

## 一、总览（按页面分节 · 问题条数）

> 条数 = 本页 P0+P1+P2 合计；P0/P1/P2 定义见 §二表头。

| # | 页面 / 组件 | 主文件 | 问题条数 | 其中 P0 |
|---|---|---|---:|---:|
| 1 | 顶栏（品牌 + 左右 tab + 速度滑块 + 登录弹窗） | `App.vue` / `index.html` | 5 | 1 |
| 2 | 真实监控 | `RealMonitor.vue` + `RobotParamsCard.vue` | 4 | 0 |
| 3 | 摄像头面板 | `CameraPanel.vue` | 5 | 0 |
| 4 | 点位执行 | `PointExecView.vue` + `ExecControlCard.vue` | 6 | 0 |
| 5 | 程序执行 | `ProgramExecView.vue` | 8 | 1 |
| 6 | 智能分拣（井字棋 / 执行 / 点位 三 tab） | `SortingView.vue` + `SortFlow.vue` + `TicTacToe*.vue` + `GamepadTestCard.vue` | 12 | 1 |
| 7 | AI 执行 | `AiControlView.vue` | 3 | 0 |
| 8 | 模拟仿真 | `SimMonitor.vue` | 4 | 1 |
| 9 | 系统设置 | `SettingsView.vue` | 5 | 0 |
| 10 | 运维审计 | `EventsView.vue` | 2 | 0 |
| 11 | 关于 | `AboutView.vue` + `guide.js` | 6 | 1 |
| 12 | 底部状态区（五选一片条 + 真机链路 + 运行日志） | `StatusStrip.vue` + `RcReadyCard.vue` + `linkview.js` | 5 | 0 |
| 13 | 全局（令牌样式 / 死代码 / 死样式 / 口径） | `index.html` + 各组件 | 6 | 1 |
| | **合计** | | **71** | **6** |

> 说明：同一条问题若跨页出现（如全局 `gap>10px`），只在「全局」页计入一次，避免重复计数。

---

## 二、问题清单表

字段固定：`# | 类别 | 严重度 | 文件:行号 | 问题（一句话） | 建议改法`
- 类别 ∈ {文案, 盒子, 布局, 一致性, 状态真实性, 可用性}
- 严重度 ∈ {P0 明显破损/重叠/溢出/**误导**, P1 一致性与冗余, P2 打磨}

### 1. 顶栏 `App.vue` / `index.html`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| A1 | 文案 | **P0** | `App.vue:679`、`:698-700`、`:716` | 登录入口与弹窗只说「管理员密码」：按钮 title「连接真机控制前需管理员密码授权」、弹窗正文「需输入**管理员**密码获取限时控制令牌」、placeholder「请输入**管理员**密码」；但 `:706` 明确有「操作员密码」分支（`auth.rolesEnabled.operator`）——把操作员排除在外，属**与事实不符的提示**。 | 三处统一改为「请输入口令（管理员 / 操作员）」，title 改为「需输入管理员或操作员口令授权」；弹窗 placeholder 用「请输入口令」。 |
| A2 | 一致性 | P1 | `index.html:129`、`:136` | 顶栏 tab `.tab { padding: 12px 22px }`，padding>10px，违反本项目「gap/padding 一律 ≤10px」口径。 | 收成 `padding: 10px 18px`（或 `10px 16px`），并同步 `@media(max-width:1200px)` 的 `:575`。 |
| A3 | 布局 | P2 | `index.html:572-578` | 窄屏（≤1200px）tab padding 改成 `12px` 后，左右两组 tab + 居中品牌 + 速度滑块同排，宽度紧张时易挤压/换行。 | 窄屏时把速度滑块 `.speed-label` 隐藏、滑块宽度 90→64px，给 tab 让位。 |
| A4 | 文案 | P2 | `App.vue:671-673` | 底栏「控制中 · 角色 · 剩余时间」与「点位执行」页 `ExecControlCard` 的「已获得控制权限」表达同一件事，两页各说一遍。 | 保留底栏那处；`ExecControlCard` 内改为只显示"待一键就绪/已就绪"这一步的本页状态，不再重复角色与时长。 |
| A5 | 布局 | P2 | `index.html:399-402`、`App.vue:613-623` | `#guide-zone`(fixed bottom:110px)、`#key-hint`(fixed bottom:92px) 与底栏向上弹出的 `.link-pop`（见 §12-B1）在窄屏可能视觉重叠。 | 见 §12-B1 统一处理浮层层级与安全区（本页不单独改）。 |

### 2. 真实监控 `RealMonitor.vue` + `RobotParamsCard.vue`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| R1 | 盒子 | P2 | `RealMonitor.vue:55-59` | 只读页右栏仅「关节角 + 末端 TCP」两张卡（`<RobotParamsCard :connection="false" />`），相比其它页右栏大面积留白，"空位"感明显。 | 二选一：① 允许把 TCP 卡与关节卡并成一张（同一张卡内两栏）；② 显式在右栏顶部加一条「本页为只读监控」说明补足高度（本项目已有类似一行指路文案）。**不要**为填空而加无功能按钮。 |
| R2 | 文案 | P2 | `RobotParamsCard.vue:129`、`:138-141` | 关节角卡：h3 右侧已有来源标签（`sourceLabel`，如「真机遥测」），卡片底部小字又写「读数与 3D 画面同源（**真机遥测**）」——同一信息两遍。 | 底部小字删去括号里的来源，只留「条宽按各轴行程归一化；鼠标移到某一行会同时高亮 3D 对应关节段。」 |
| R3 | 文案 | P2 | `RobotParamsCard.vue:158-160` | 注释称"全局 `.axis` 在 `index.html` 里被定义过**两次**（34px 1fr 64px 与 26px 1fr 52px）" —— 实测全局 `.axis` **只有一处**（`index.html:279`，`26px 1fr 52px`）。说明与实际不符（误导维护者）。 | 更新注释为"全局 `.axis` 为 `26px 1fr 52px`；本卡用 `rp-` 前缀以免与示教滑块抢列宽"。 |
| R4 | 一致性 | P2 | `index.html:332-384`、`:587` | `.hud-joints*`（≈52 行，含 `@media` 里的 `:587`）在模板中**无任何使用**（真实监控已改用 `RobotParamsCard`）→ 死样式。 | 删除 `.hud-joints / .hud-head / .hud-title / .hud-src / .hud-fps / .hud-dot / .hud-toggle / .hud-body / .hud-row*` 及 `:587` 媒体查询行。 |

### 3. 摄像头面板 `CameraPanel.vue`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| C1 | 文案 | P2 | `CameraPanel.vue:663-664` | 信息网格标签「**分割**」语义不明（实为 `vision.seg` 颜色分割线程状态），现场看不出是什么。 | 改为「颜色分割」或「判色链路」。 |
| C2 | 一致性 | P2 | `CameraPanel.vue:894-901` | `.color-unavail`（灰度相机提示）css 仍在，但模板里该说明已迁到「关于」页（见 `:564-565` 注释）→ 死样式。 | 删除 `.color-unavail` 相关 4 条规则。 |
| C3 | 可用性 | P2 | `CameraPanel.vue:718-723`、`:969-975` | 折叠按钮 `.cam-toggle` 24×24、缩放手柄 `.cam-resize` 18×18，点击区偏小；拖动标题栏与展开按钮相邻（`onHeadDown` 已排除 `.cam-toggle`，但边界靠手指/鼠标精确）。 | 折叠按钮放大到 28×28；缩放手柄热区放大到 24×24（视觉图标可不变）。 |
| C4 | 盒子 | P2 | `CameraPanel.vue:498` | 收起态画面固定 200px 高、展开态用 `size.h`（默认 260），收起/展开高度跳变较大。 | 收起态高度用 `min(size.h, 200)` 收敛，避免展开瞬间"长高"。 |
| C5 | 文案 | P2 | `CameraPanel.vue:596`、`:631-635` | 「三色置信度」滑杆 title 与「矩形填充率」滑杆 title 都是一大段（后者 5 行 + ⚠），现场悬停才看得到、且极长。 | 长说明压缩为一句；详细阈值说明（含 2026-10-08 事故）移到「关于 → 3D 说明」或运维审计，滑杆只留「低于此值判非矩形；过高会误杀真方块」。 |

### 4. 点位执行 `PointExecView.vue` + `ExecControlCard.vue`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| P1 | 文案 | P1 | `PointExecView.vue:182`、`:212`、`:224`、`:230-232` | 关节点动卡对"−/+ 走一格 / 按住连续"讲**三遍**：h3 副标题「拖动滑块 / 按 −/+ 走一格」+ 底部 `<p>`「…点击 −/+ 走一格，按住连续走。」+ 两个按钮 title「− 走一格 / 按住连续反向」。 | 只保留底部一行操作说明；按钮 title 简化为「反向走一格（按住连续）」/「正向走一格（按住连续）」，h3 副标题改为参数类信息（如「步长 {{ exec.jogStepDeg }}°」）。 |
| P2 | 文案 | P1 | `PointExecView.vue:257-258`、`:269-272` | 「标记当前点」按钮 title 与下方 `.pt-hint` 同义两遍（"读取实时位姿，T1/T2 示教器手动对位也可用，仅记录不运动"）。 | 保留 `.pt-hint`（可见），按钮 title 只留「读取当前位姿存为点位（不运动）」。 |
| P3 | 盒子 | P1 | `PointExecView.vue:384` + `index.html:518` | `.pt-item` scoped `margin-bottom:8px` 与全局 `.pt-list { gap:6px }` 叠加 → 列表项间距实际 14px，与"统一间距"不一致，且违反"组件 margin-bottom 应移除"。 | 删 `.pt-item { margin-bottom }`，间距统一交给 `.pt-list { gap }`。 |
| P4 | 一致性 | P2 | `PointExecView.vue:431-462` + `index.html:475-484` | 关节点动行用 `.jog-axis/.ja-btn/.ja-range/.ja-val`（scoped），但全局 `index.html` 仍留着旧 `.jog-grid/.jog-row/.jog-name/.jog-btn`（无引用）→ 死样式 + 两套点动样式并存。 | 删除 `index.html` 的 `.jog-grid/.jog-row/.jog-name/.jog-btn/.jog-btn.neg/.jog-btn.pos`。 |
| P5 | 一致性 | P2 | `PointExecView.vue:190`、`:254`、`ExecControlCard.vue:79` | 模板内联 `style="margin-bottom:8px / 10px"`、`.btns` 里 `style="margin-top:8px"` 多处（间距用 inline style 硬编码，不随令牌/主题）。 | 收敛为类：`.mt-8/.mb-8` 或直接给相邻元素统一 `gap`，把 inline margin 清掉。 |
| P6 | 可用性 | P2 | `PointExecView.vue:323-351` | 每个点位项 5 个操作按钮（执行/预演/编辑/导出/删除）平铺，密度高；误点删除仅靠 `confirm`。 | 保持功能，但把「导出」收进「…」更多菜单；删除按钮保持红色 + confirm（现状 OK）。 |

### 5. 程序执行 `ProgramExecView.vue`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| Pr1 | 文案 | **P0** | `ProgramExecView.vue:350-352` vs `:292-311` | 空状态写「还没有步骤。用上面**四个按钮**添加…」，而实际有**六个**按钮（标记点 / 吸 / 停止吸 / 放 / 停止放 / 等待）。**与事实不符。** | 改为「用上面六个按钮添加，再用 ▲▼ 排好顺序 —— 顺序就是执行顺序。」 |
| Pr2 | 文案 | P1 | `ProgramExecView.vue:486-516` | 「测试 vs 直接运行」讲**三遍**：h3 副标题「填文件名 · 先测试再运行」、底部 `<p>`「『测试』只解析校验（IK/限位/可达），**绝不下发**；『直接运行』才会真下发。」、以及 h3 注释。 | 保留底部那一行（信息最全），h3 副标题改为中性「填文件名」或去掉副标题。 |
| Pr3 | 盒子 | P1 | `ProgramExecView.vue:236-240` + `RcReadyCard.vue:202-205` | 「清除报错」按钮在**同一页出现两处**：本页 `:237` 一颗；底栏「真机链路」浮层里的 `RcReadyCard` 又一颗（底栏任何页都能点开）。动作同名、说明同义。 | 二选一：删本页那颗（统一走底栏真机链路面板），或本页那颗保留、`RcReadyCard` 内注释说明分工。建议前者，减少重复入口。 |
| Pr4 | 布局 | P1 | `ProgramExecView.vue:244` | 「运行状态」卡 `v-if="exec.runTotal || exec.lastRun"` —— 首跑前不存在，一旦开始运行就**插入**，导致其下方「序列编辑/示教器程序/本地程序/手动执行」整列下移（布局跳动）。 | 改成常驻卡片，内部用 `v-if` 切换"空闲/运行"两种内容（盒高稳定）。 |
| Pr5 | 文案 | P2 | `ProgramExecView.vue:101-104`、`:135-137`、`:536-537` | 代码注释仍写"只有四类操作（标记点/吸气/停止吸气/等待）"、"日志卡片已移除"，与现状（六类、日志已迁底栏）不符，误导维护者。 | 更新注释为"六类"，去掉日志迁移的历史说明。 |
| Pr6 | 盒子 | P2 | `ProgramExecView.vue:541-571` | `.ready-box`、`.clear-err`、`.seq-add` 等 scoped 样式与全局 `.card`/`button` 令牌并置；`.clear-err` 用的是 `--warn` 兜底 `#d48806` 硬编码 hex。 | `#d48806` 改为令牌（如 `var(--warn)`），去掉硬编码兜底。 |
| Pr7 | 一致性 | P2 | `ProgramExecView.vue:282` | h3 副标题「六类：标记点 / 吸 / 停止吸 / 放 / 停止放 / 等待」—— 每类都带 icon 的大按钮已直观表达，"六类：…"属冗余枚举。 | 副标题可改为「{{ exec.seqItems.length }} 步」。 |
| Pr8 | 布局 | P2 | `ProgramExecView.vue:466` | 步骤明细 `v-if="selProgram"` 也是插入式（选中程序才出现），在「本地程序」卡内追加，撑高卡片。 | 可接受；若要更稳，把步骤明细做成固定高度区（`max-height` + 滚动）。 |

### 6. 智能分拣 `SortingView.vue`（+ `SortFlow.vue` / 井字棋 / 手柄）

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| S1 | 状态真实性 | **P0** | `SortingView.vue:126-134`（用法见 `:836`、`:879`、`:911`） | `missing = new Set((chk.value && chk.value.missing) || [])`；当体检未返回（首次未加载 / `/sorting/points` 失败 → `chk` 为 null）时 `missing` 是**空集**，`isTaught = !!(chk && chk.ok) || !missing.has(name)` 对**全部**点位返回 `true` → **绿点全亮**、且「到点/删除」按钮被判为可点。界面在谎称"已示教"。 | `isTaught` 必须要求体检**已就绪**：如 `if (!chk.value) return false;`（未体检一律按"未知/未示教"呈灰点，按钮禁用）。这与本页"缺一个都不允许启动"的既有口径一致。 |
| S2 | 文案 | P1 | `SortingView.vue:933`、`:937-939` | J6 正面/反面"只用 J6、其余轴不用、翻转时 J1~J5 保持实时姿态"讲**三遍**：坐标行内「（仅 J6 有效，其余轴不使用）」+ 下面两段 `<p class="small muted">`。 | 只留一段（合并为一个 `<p>`）："J6 正面/反面只用 J6；翻转时 J1~J5 保持当前实时姿态。" |
| S3 | 文案 | P1 | `SortingView.vue:714`、`:748-750`、`:782-785`、`SortFlow.vue:166-168` | 「空跑不判色 / 方块仅示意、机器人不动」在**同一页**讲 4 遍（开关 label、3D 角标、判色卡的 judgeOff 段、流程卡帧说明）。 | 保留"开关 label（操作时看得到）+ 3D 角标（画面旁看得到）"两处；`judgeOff` 段与 `SortFlow` 帧说明合并为一句或复用同一常量文案。 |
| S4 | 文案 | P1 | `SortingView.vue:705-707`、`:719-721` | 「暂停=冻结可续 / 停止=步边界生效」讲两遍（暂停按钮 title 与运行中的 `<p>`）。 | 保留一处（建议 `<p>`，常显），按钮 title 缩短为「暂停 / 继续」。 |
| S5 | 盒子 | P2 | `SortingView.vue:728-762` | 「3D 可视化」卡内有 `rows2` 三行 + 4 段 `small`（`:734`、`:751-754`、`:755-757`、`:758-761`），内容与"3D 显示"关联弱、堆叠高。 | 把"手上是否夹件/疑似没取到"两类流程推断提示合并为一个状态条（最多两类：正常 / 异常），减少常驻小字。 |
| S6 | 布局 | P2 | `SortingView.vue:947-950`、`GamepadTestCard.vue:225-243` | 井字棋 tab 内 `GamepadTestCard` 即使**收起**也渲染整段"排查清单 + 诊断 pre"（未连接时），内容很长。 | 收起态只留状态/来源两行；诊断清单与 `diag-pre` 仅在展开时渲染（`v-if="open"`）。 |
| S7 | 一致性 | P2 | `SortingView.vue:962` | 分拣模式分段控件 `.mode-seg .srt-tab { padding: 5px 14px }`，padding>10px。 | 收成 `padding: 5px 10px`。 |
| S8 | 一致性 | P2 | `SortingView.vue:1018-1020`、`:1108-1111` | `.jog-axes`、`.sel-grid/.sel-j` 在模板中无引用 → 死样式。 | 删除这 6 条规则。 |
| S9 | 盒子 | P2 | `SortingView.vue:1033-1034` | A 区/B 区各一张卡，各含 9 格 × 2 槽（18 行 × 2 = 两张很长的卡），页面滚动很长。 | 可接受（信息密度需要）；若要缩短，A/B 区格默认两列折叠其余（展开看全），**但会牵动 `isTaught` 渲染**，需同步回归（见 §五）。 |
| S10 | 可用性 | P2 | `SortingView.vue:1062-1064` | 每格动作按钮 `.pacts { opacity:.5 }` hover 才提亮，默认几乎看不见（现场"不知道能点"）。 | 默认 `opacity:.75`，hover 才 1；或对"已示教"格常显。 |
| S11 | 布局 | P2 | `SortingView.vue:596` | 视口 HUD `分拣 · 三色方块 · 运行中/空闲` 与右栏"运行状态"卡 h3 副标题（`:766`）都报"运行中/空闲"，两处同义。 | HUD 保留（画面旁），右栏卡副标题改为进度（如 `{{ piece }}/{{ total }}`）。 |
| S12 | 一致性 | P2 | `SortingView.vue:1004-1005`、`:1077-1079` | 暂停按钮/坐标等直接写 `rgba(255,176,32,...)`、`var(--font-mono, ...)` 兜底等硬编码色；与"组件只引用令牌"的品牌口径不符（且 `index.html:17` 声称有 `tools/verify_theme.mjs` 守卫，实际**该文件不存在**，见 §13-G6）。 | 把硬编码色替换为令牌；若确要保留硬编码，需先补齐（或删除）所谓 theme 守卫的说明。 |

### 7. AI 执行 `AiControlView.vue`

> **本页整体质量最好，未发现 P0/P1。** 仅 3 条打磨项。

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| Ai1 | 文案 | P2 | `AiControlView.vue:275`、`:298-303` | 「空跑」信息在视口 HUD 与标题栏按钮各说一遍。 | HUD 保留；标题栏按钮保持图标+「空跑/真实」即可（现状基本 OK，可不改）。 |
| Ai2 | 一致性 | P2 | `AiControlView.vue:509` | `.cw-chip { padding: 5px 11px }` padding>10px；`.cw-body { padding:10px; gap:9px }` 合规。 | 胶囊 padding 收成 `5px 10px`。 |
| Ai3 | 文案 | P2 | `AiControlView.vue:345-346`、`:354`、`:363` | 内联 `#f2c500 / #ff6b6b / #000 / #555` 硬编码色（来源标注、无法实现、件数、禁用方块）。 | 换令牌（`var(--warn)` / `var(--err)` / `var(--ink)` 等）。 |

### 8. 模拟仿真 `SimMonitor.vue`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| Si1 | 布局 | **P0** | `SimMonitor.vue:369-370` vs `:443` | 机器人模式"当前点动轴高亮"**失效**：模板把 `hot` 加在 `<div class="axis">` 上，CSS 选择器却是 `.row.hot`（`.axis` 与 `.row` 不是一个类）→ 高亮永不生效。功能"看起来该有却没有"。 | 选择器改为 `.axis.hot { background: var(--accent-soft2); border-radius: 4px; }`。 |
| Si2 | 一致性 | P2 | `SimMonitor.vue:402-403`、`:406-438`、`:445-447` | 死样式：`.sim-floating`、`.xpl-card/.xpl-badge/.xpl-field/.xpl-label/.xpl-hint/.xpl-select/.xpl-ta/.xpl-actions/.xpl-status/.xpl-suck/.xpl-err/.xpl-speed*`、`.linkq` 均无引用（XPL 卡已移除）。 | 整体删除这批规则（≈40 行）。 |
| Si3 | 盒子 | P2 | `SimMonitor.vue:383-393` | 「末端 TCP」卡（本页自建）与 `RobotParamsCard` 的 TCP 卡内容重复（都是 X/Y/Z 只读）。 | 本页直接复用 `RobotParamsCard`（`tcp` 卡），去掉自建版。 |
| Si4 | 文案 | P2 | `SimMonitor.vue:345-348` | 「一段预设的关节轨迹（J1 摆头 / J2·J3 舒展起伏 / …）」属解释性长文，与"演示动作"按钮功能关联弱。 | 压缩为「预设关节轨迹，姿态平滑循环。」，细节留「关于」。 |

### 9. 系统设置 `SettingsView.vue`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| St1 | 盒子 | P2 | `SettingsView.vue:721-722` | 每个配置字段都是独立小盒子（`.stg-field` 带边框/背景/内边距），`.stg-fields` 用 `minmax(330px,1fr)` → 大量"卡片套卡片"，视觉碎片化。 | 弱化字段盒子：去掉 `.stg-field` 边框，改用行分隔（`border-bottom: 1px solid var(--line)`），保留分组卡外层。 |
| St2 | 布局 | P2 | `SettingsView.vue:696-698`、`AboutView.vue:650-652` | `.stg-nav`/`.ab-nav` `position:sticky; top:-14px`，而容器 `.stg/.about { padding:10px }` → 吸附点相对容器为负，滑到顶部时可能盖住上一卡底边。 | 改为 `top: 0`（相对滚动容器）或与 padding 对齐（`top: 10px`）。 |
| St3 | 盒子 | P2 | `SettingsView.vue:278-290` | 实时信息卡只在管理员时加载（`:293 if (st.isAdmin) st.loadLive()`），操作员/未授权时整块不出现 → 与"内容与盒子尺寸不匹配"无关，但造成同一页不同角色**布局差异大**。 | 明确文案：非管理员时该区显示一行「实时信息需管理员」占位（避免"这页怎么比别人的少一块"）。 |
| St4 | 文案 | P2 | `SettingsView.vue:305-311`、`:353-359` | 顶部副标题与 env 锁定提示、"保存/立即生效"说明多处重复解释"覆盖层/只读 yaml"。 | 顶部只留一句；细则移到各分组 `.stg-group-d`。 |
| St5 | 一致性 | P2 | `SettingsView.vue:625-633` | `.stg-btn` 自成一套按钮样式（与全局 `button` 令牌不同），全站出现第 N 套按钮外观。 | 尽量复用全局 `button` 令牌，仅保留 `.stg-btn.primary/.danger` 的语义色差异。 |

### 10. 运维审计 `EventsView.vue`

> **本页基本未发现问题**，仅 2 条打磨项。

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| Ev1 | 文案 | P2 | `EventsView.vue:407-411` | 顶部「共 N 条」含全部级别，但紧随的 chips 只统计 `critical/error/warn` → 三片之和 ≠ 总数，易被误读为"少了"。 | chips 加一个「信息/其它」计数，或总数旁标注"（含信息级）"。 |
| Ev2 | 布局 | P2 | `EventsView.vue:552-556` | 窄屏 ≤1200 `.ops-item` 列宽与 `.oi-actor{display:none}`，事件正文列变窄后长消息更容易截断（`:541` 已 `ellipsis`）。 | 可接受；若要更稳，窄屏把时间列（84px）也收窄。 |

### 11. 关于 `AboutView.vue` + `guide.js`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| Ab1 | 文案 | **P0** | `AboutView.vue:477-488`（`:480` `{{ i + 1 }}`）+ `guide.js:50-119`（9 项）+ `tabs.js:38-59` | 「九个模块」给每个模块编号 1..9，但 `MODULE_GUIDE` 里第 3 项是**不在顶栏的「程序执行」**（`tabs.js:46 hidden:true`）；顶栏实际只有 8 个、数字键 `1~8` 与之对齐。于是编号**整体错位**：读者按"3"以为切程序执行，实际切到"智能分拣"。属**误导性映射**。 | 模块列表按 `tabs.js` 的 **navTabs（不含 hidden）** 顺序编号，hidden 的「程序执行」不占数字位、单独标注"（入口在点位执行页内）"。 |
| Ab2 | 文案 | P1 | `AboutView.vue:473-475` | 标题「九个模块」与副标题「1 ~ 8 从左到右直达」自相矛盾（9 vs 8）。 | 与 Ab1 一并改：标题「九个模块（顶栏 8 个 + 程序执行并入点位执行）」。 |
| Ab3 | 文案 | P1 | `guide.js:11-44`(QUICKSTART) vs `:224-318`(PROCEDURES) | 「快速上手」与「标准操作流程」内容高度重叠（启动服务 / 请求控制 / 档位声明 / 先模拟后真机 各讲一遍）。 | 明确分工：快速上手只留"3 步起步"，其余指向 SOP；或把 QUICKSTART 折叠为一行指引。 |
| Ab4 | 一致性 | P2 | `AboutView.vue:710`、`:733`、`:780`、`:796` | `.ab-sop-h`/`.ab-prec-item`/`.ab-mod`/`.ab-note-item` padding 为 11~12px（>10px）。 | 统一收成 10px。 |
| Ab5 | 盒子 | P2 | `AboutView.vue:262-622` | 单页 11 节、极长（快速上手/检查清单/SOP/注意事项/异常速查/快捷键/模块/3D/安全/系统信息），滚动很长。 | 已有章节导航（`:285-289`）+ sticky；可接受。若要减负，把「异常速查」与「注意事项」合为一节。 |
| Ab6 | 文案 | P1 | 渲染点 `AboutView.vue:302`（`{{ s.detail }}`）、`:490`（`{{ t }}`）；源文案 `guide.js:14/20/69-71/79-92/126/211/246/…` | 数据里大量用 `**粗体**` markdown 记号（如 `guide.js:126` tips「海康型号尾缀 -GM/UM/EM 为**灰度相机**」、`:69`「**示教器程序**」等）——`AboutView` 直接 `{{ }}` 渲染为纯文本，`**` 会**原样显示**在界面上。 | 去掉文案里的 `**`，或渲染时用 `<b>`；可复用 `AiControlView` 的 `plain()` 去星号思路。**需逐条扫 `guide.js` 全部字符串**。 |

### 12. 底部状态区 `StatusStrip.vue` + `RcReadyCard.vue`

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| B1 | 布局 | P2 | `StatusStrip.vue:379-381` + `index.html:399-402` | 五选一片条的面板 `.link-pop { position:absolute; bottom:calc(100% + 8px); left:0 }` 向上弹，落在 3D 视口内；`#guide-zone`(bottom:110px 居中)、`#key-hint`(bottom:92px 居中) 也在同一带 —— 窄屏下日志面板（宽 460）可能与引导条重叠。 | 未实测，**需浏览器核对**；若重叠：给 `#guide-zone` 让位（引导条在面板展开时隐藏）或把日志面板锚点右移。 |
| B2 | 文案 | P2 | `RcReadyCard.vue:202-205`、`:213-216`、`:219-221` | 「清除报错」title 与"点动触发位仍为 1"提示都较长；另 `RcReadyCard` 的「清除报错」与程序执行页那颗重复（见 §5-Pr3）。 | 随 §5-Pr3 一起收敛。 |
| B3 | 状态真实性 | P2 | `StatusStrip.vue:59-71` | 日志片灯色取"最新一条日志级别" —— 若最后一条是历史 error（未清日志），面板收起后灯一直红，可能被当"当前故障"。 | 可接受（有"清空日志"）；若求更准，加时限（如 30s 内无新日志则灯回灰）。 |
| B4 | 一致性 | P2 | `StatusStrip.vue:79-98` + `RcReadyCard.vue:88-91` | 「真机链路」片用 `rc.joggable/rc.ready/servoOn` 拼 value，`RcReadyCard` 按钮用 `servoOn` 判变脸 —— 同一状态两处口径，虽不矛盾但需对照才能理解。 | 在片 title 里补一句"（面板内为详细就绪链）"，或让片 value 与按钮用同一判据。 |
| B5 | 盒子 | P2 | `StatusStrip.vue:157-162` | 各片面板宽度硬编码（log 460 / link 420 / 其余 320），与右栏宽度无关，窄屏靠 `min(...,92vw)` 兜底。 | 可接受；窄屏已处理。 |

### 13. 全局（令牌 / 死代码 / 死样式 / 口径）

| # | 类别 | 严重度 | 文件:行号 | 问题 | 建议改法 |
|---|---|---|---|---|---|
| G1 | 一致性 | **P0** | `index.html:129`、`:136`、`:162`、`:205`、`:259`、`:290`、`:341`、`:389`、`:407`、`:573-584`；`AboutView.vue:710/733/780/796`；`SettingsView.vue:625-633`；`AiControlView.vue:509`、`SortingView.vue:962` | **全站 gap/padding >10px 违反项目明确口径**：`.side{padding:14px;gap:12px}`、`.side-cards{gap:12px}`、`.card{padding:12px}`、`.sp{padding:10px 12px}`、`.hud-joints{padding:10px 12px}`、`.ev-item` 等，以及各页 scoped 中的 11~14px。口径是"gap/padding 一律 ≤10px"。 | 出一张"超标清单"，统一收敛到 ≤10px（如 `12→10`、`14→10`）。**注意**：`button{padding:8px 12px}`（`:290`）改动会影响所有按钮高度，需整体走查一次。 |
| G2 | 一致性 | P1 | `index.html:346`、`:446`、`:498`；`PointExecView.vue:190/254/384`；`SafetyPanel.vue:289/394/426/485`；`SimMonitor.vue:353/357/412/424`；`SimObjectsPanel.vue:86/151` | 项目口径"组件的 margin-bottom 应移除"，但仍有 **14 处** `margin-bottom`（含全局 `.hud-head`、`.seg`、`.pt-form` 与多处模板 inline）。 | 逐个删除 margin-bottom，间距改由父容器 `gap` 承担。 |
| G3 | 一致性 | P2 | `components/RobotProgramPanel.vue`、`components/SimObjectsPanel.vue`、`components/LinkPanel.vue` | **死组件**：`RobotProgramPanel.vue`、`SimObjectsPanel.vue` 在 `src/`、`tools/` 内**无任何 import/引用**；`LinkPanel.vue` 也未被挂载（App 已改为底栏「真机链路」片），仅被 `tools/verify_rc_ready.mjs:38,80` 断言"文件存在且内嵌 RcReadyCard"。 | 三选一：① 确认无用 → 删除文件并同步改 `verify_rc_ready.mjs`；② 明确保留（如作归档）→ 在文件头注明"未挂载，仅回归占位"。**不要**留成"看着像在用其实没挂"的状态。 |
| G4 | 一致性 | P2 | `index.html:332-384`、`:475-484`、`:549-559`；`CameraPanel.vue:894-901`；`SimMonitor.vue:402-447`；`SortingView.vue:1018-1020`、`:1108-1111` | **死样式**：`.hud-joints*`、`.run-log*`、`.jog-*`（index.html）；`.color-unavail`（CameraPanel）；`.xpl-*`/`.sim-floating`/`.linkq`（SimMonitor）；`.sel-grid`/`.sel-j`/`.jog-axes`（SortingView）均无引用。 | 统一清理，减少样式漂移来源。 |
| G5 | 一致性 | P2 | `SimMonitor.vue:353`、`TicTacToeCard.vue:130/140`、`CameraPanel.vue:866-891`、`SettingsView.vue:751-761` | **分段/开关控件多套**：全站已统一 `.srt-tabs`（分拣/点位执行/程序执行），但模拟仿真与井字棋仍用 `.seg`，CameraPanel 用自定义 `.switch`，SettingsView 用 `.stg-sw`。 | 分段控件尽量统一到 `.srt-tabs`（或至少 `.seg` 一套）；开关控件抽一份共用。需评估改动面。 |
| G6 | 一致性 | P1 | `index.html:16-17` + 各组件（`SortingView.vue:1067-1070/1098/1101/1115-1118`、`CameraPanel.vue:920-928`、`TicTacToeCard.vue:180-181`、`AiControlView.vue:99/346/354/363`、`GamepadTestCard.vue:323-325` 等） | `index.html` 声称"组件里只准引用变量，不准写 hex；守卫 `tools/verify_theme.mjs` 会扫描失败" —— 但 **`tools/` 下没有 `verify_theme.mjs`**（只有 25 个 `verify_*` + 2 个 `check_*`，见 `frontend/tools/`），且组件里**确实存在大量硬编码 hex**。**文档承诺与实现不符。** | 二选一：① 补齐 `verify_theme.mjs` 守卫并清理 hex；② 删除/更正 `index.html` 与相关注释里对该守卫的承诺（诚实原则）。 |

---

## 三、整改分批（每批可独立验证）

> 每批结束都跑：`cd frontend && npm run build`（含静态检查）→ `npm run verify`；涉及页面的改动在浏览器点一遍。
> **勾选/折叠/`v-if` 相关改动**必须对照 §五 的风险项。

### 第 0 批 · P0 修正（"与事实不符/编故事"，最高优先）
- **S1** 分拣点位绿点全亮 → `isTaught` 未体检时返回 false。
- **Pr1** 程序执行「四个按钮」→「六个按钮」。
- **A1** 登录/授权文案补上"操作员"。
- **Ab1+Ab2** 关于页模块编号改为按 navTabs 顺序（hidden 不占位），消除 "九 vs 8" 矛盾。
- **Si1** 模拟仿真 `.row.hot` → `.axis.hot`（当前轴高亮生效）。
- **G1** 全局 gap/padding >10px 收敛（先出清单，批量替换 `12/14→10`）。
- **验收点**：
  1. 分拣页在"未体检/无令牌"时点位槽**全灰**，且「到点」按钮禁用；体检 OK 后仅已示教的变绿。
  2. 程序执行空序列文案为"六个按钮"。
  3. 登录弹窗/按钮文案出现"操作员"。
  4. 关于页模块编号与顶栏数字键一致（按 3 切到"智能分拣"）。
  5. 模拟仿真机器人模式下当前轴行有高亮背景。
  6. `grep` 全站无 `gap:12/14px`、无 `padding:1?[1-9]px`（白名单除外）；build + verify 全绿。

### 第 1 批 · P1 一致性与冗余
- **文案重复**：P1、P2、Pr2、S2、S3、S4（同一说明讲 2~4 遍的一律收敛为一处）。
- **重复入口/卡**：Pr3（清除报错两处）、Pr4（运行状态卡常驻化，消布局跳动）。
- **口径**：G2（移除全部 margin-bottom）、G6（theme 守卫补或删）、Ab3（快速上手与 SOP 分工）。
- **验收点**：
  1. 上述每个文件里对应说明文字**只出现一次**（可 grep 关键词计数）。
  2. 程序执行页在"首跑前 → 运行中 → 结束"三种状态下，右栏卡片顺序与高度不跳。
  3. 全站 `grep -c margin-bottom` = 0（模板 inline 也清零）。
  4. `verify_rc_ready.mjs` 与代码一致（若删 LinkPanel 则同步改断言）。

### 第 2 批 · P2 打磨（死代码 / 死样式 / 盒子 / 可用性）
- **死代码/死样式**：G3、G4、R4、C2、Si2、S8。
- **盒子/布局**：R1、R3、St1、St2、St3、S5、S6、S9、S11、Pr8、B1、C4。
- **可用性**：C3、S10、P6、Si3、Ai2、St5。
- **验收点**：
  1. `npx vite build` 后产物无对应死类（或人工 `grep` 模板确认无引用）。
  2. 删除的死样式不引起任何页面视觉回退（浏览器逐页对照截图）。
  3. `npm run verify`、`npm run build` 全绿。

---

## 四、做什么 / 不做什么

### 做
1. **文案**：`guide.js`、`AboutView`、各 `*View.vue` 模板、`tabs.js`/`linkview.js` 里的用户可见字符串——
   去重复（同义说明只留一处）、修误导（与事实不符的当场改掉提示语本身）、去 markdown 星号残留。
2. **盒子**：统一同类卡片高度/尺寸；右下/右栏"空位"用信息或收敛尺寸解决；`pt-item`/字段卡等"卡片套卡片"弱化。
3. **布局**：统一对齐/间距；修 `v-if` 插入导致的跳动（运行状态卡、步骤明细）；核对浮层与底栏配合。
4. **一致性**：`gap/padding ≤10px`、移除 `margin-bottom`、分段/开关控件统一、清理死代码与死样式。
5. **状态真实性**：修正一切"未取到真实状态却显示为正常/绿色"的指示（S1 为首）。
6. **可用性**：放大过小点击区、补关键反馈；保留既有二次确认（不新增弹窗淹没操作）。

### 不做（明确排除）
1. **不换主题、不改设计令牌取色**（Sentinel 深色主题、`--accent` 等一律不动）。
2. **不重写任何功能**，不改组件间数据流；不重构 `stores/*` 的业务逻辑。
3. **不动控制 / 速度 / 安全逻辑**：`40101/40103/40135` 下发链路、双闸、围栏互锁、急停、
   AI 两段式确认、分拣判色与选槽——一行不碰。
4. **不做纯审美的个人偏好式改动**：不引入 UI 框架/组件库；不加大留白；不"为了好看"改配色/字号/圆角；
   **绝不把 gap/padding 往上调成 24px 这类**。
5. **不为填空位而加无功能按钮/装饰**。
6. **不改后端、不改配置、不跑 `tools/` 真机脚本、不启服务**（本次仅出方案；后续实现也遵守 `AGENTS.md` §8）。

---

## 五、风险与注意

1. **折叠态 / `v-if` 卸载导致订阅解绑**（最高风险）：
   - `SortingView` 的井字棋 tab 用 `v-else` 模板切换（`:947`），`TicTacToeCard`/`GamepadTestCard`
     若被改成 `v-if` 卸载，其在 `onMounted` 里注册的手柄订阅（`subscribeGamepad`）会随之解绑 →
     手柄失效。改动须保持"组件常挂、仅 `v-show`/内容切换"。
   - `SafetyPanel` 仅 `v-if="view==='live' && auth.isAdmin"`（`MonitorLayout.vue:172`）；任何"按页卸载面板"
     的改动都要确认围栏事件刷新（`watch(open)`）不丢。
   - `RcReadyCard` 的 `onMounted(() => rc.start())`：它只应存在**一份**活动实例。若把它从底栏浮层改为常驻，
     注意不要出现两个实例同时 `rc.start()`（重复轮询）。

2. **指示口径（状态真实性）**：
   - S1 的修法（未体检→灰）会**改变分拣页首屏观感**，需确认"体检中"与"体检失败"都有明确文案，别让用户以为"点位都没了"。
   - 底栏日志片灯色、真机链路片 value 的改动属于"口径"，改动前先读 `tools/verify_link.mjs`、
     `tools/verify_safety_text.mjs` 的注释（每个现场事故都固化成了一条钉子）。

3. **需同步回归断言**（改文案/结构前先读对应脚本注释，避免"改了代码没改断言"红掉）：
   | 改动对象 | 关联回归 |
   |---|---|
   | `guide.js` / `AboutView`（模块、快速上手、检查清单） | `tools/verify_about.mjs`（断言 `guide.js` 被真实渲染） |
   | `tabs.js` 顺序/门控、模块编号 | `tools/verify_tabs.mjs` |
   | 底栏片（link pills / 真机链路片） | `tools/verify_link.mjs`、`tools/verify_bottom_dock.mjs` |
   | `RcReadyCard`（就绪按钮/文案） | `tools/verify_rc_ready.mjs`（还会读取 `LinkPanel.vue`，见 G3） |
   | 分拣 `isTaught` / 点位渲染 | `tools/verify_sorting3d.mjs`、`tools/verify_sortflow.mjs` |
   | 快捷键表（若动到 About 快捷键文案） | `tools/verify_shortcuts.mjs` |
   | 速度滑块文案/位置 | `tools/verify_speed.mjs` |
   | 姿态来源标签（`RobotParamsCard`） | `tools/verify_robot_params.mjs` |
   | Pinia 用法（若动 store） | `tools/verify_stores.mjs` |
   | 中文引号（`ABOUT` 等新增中文串） | `tools/check_cjk_bare.mjs`（构建期）+ `check_imports.mjs` |

4. **令牌/主题**：G1/G2 是全站批量替换，改 `index.html` 的 `button{padding}`、`.card{padding}` 会**影响所有页面**，
   必须整体走查；`AboutView`/`SortingView` 里大量硬编码 hex（G6）建议与 theme 守卫一起处理，避免"改一半"。

5. **诚实汇报**：本方案所有问题均基于静态阅读源码（未运行、未截图）。**B1（浮层重叠）、C3/C4（尺寸跳变）、
   S9/S10（密集/低可见）**等属"需浏览器实测确认"的条目，落地前应先在浏览器复核，不做无据改动。

---

*本文件为体检方案，不含任何代码改动。落实时请按 §三 分批推进，每批以 `build + npm run verify` 收尾。*
