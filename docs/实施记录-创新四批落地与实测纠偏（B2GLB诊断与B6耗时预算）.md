# 实施记录 · 创新四批落地 + 两处实测纠偏（B2 GLB 诊断 · B6 回归耗时预算）

日期：2026-10-12 · 对应方案：`docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md`
范围：A（服务端安全）· B（性能/可观测）· C（高性能画面 + 报警合理化）· D（**只保留数值体检**，摆放类已撤）· E（创新功能）
＋ 本轮补做的两项**实测**（方案里一直挂着"待测"的 B2 / B6）。

---

## 0. 本轮收尾数字（全部由我自己跑出来，不是转述）

| 项 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `cd backend && .venv\Scripts\python.exe -m pytest` | **1037 collected / 全绿（exit 0）**，约 44s |
| 前端构建 | `cd frontend && npm run build` | **通过**（eslint + check_imports + check_cjk_bare + vite build） |
| 前端回归全链 | `cd frontend && npm run verify` | **47 步全绿**（3 道前置检查 + **44 个** `verify_*.mjs`） |
| 回归耗时预算 | `cd frontend && npm run verify:time` | **45 个计时步骤 · 合计 3.0s · 最慢单步 0.2s · 断言 341 过 / 0 败** |

> ★ 与方案/AGENTS.md 旧数字的差：后端测试从 **751** 涨到 **1037**，前端回归脚本从 **31** 涨到 **43**，
> 文档从 **58** 涨到 **61**，前端源文件从 **80** 涨到 **94**。AGENTS.md 的规模行、命令注释与 §6 表已同步改掉。

---

## 1. 落地清单（每行都能对上"唯一判据在哪 + 钉在哪"）

| 批 | 做了什么 | 唯一判据 / 唯一入口 | 回归钉 |
|---|---|---|---|
| A1 | 服务端**独立**安全评估（不信前端上报） | `services/safety_eval.py` | `verify_safety.mjs` / `tests/test_safety_server_guard.py` |
| A2 | 保守包络**扫掠**（不是单点检查） | `safety_eval.sweep()` | 同上 + E3 复用 |
| A3 | SSM 式动态限速**建议**（**只提示、绝不闭环写速度**） | `safety_eval.ssm_advice()` | `test_ssm_never_writes_speed`（速度唯一口径 = 右上角滑块） |
| A5 | 事件**取证包**（danger/hit 上升沿落盘） | `api/safety.py` + 前端只读 | `verify_evidence.mjs` |
| A6 | 数据**新鲜度三态**（ok / stalled / 未知） | `utils/safetyLabels.js::freshState/evalHealth` | `verify_hmi_palette.mjs` I 段（阈值 2000/5000 不许改） |
| B1 | 只读**运行指标**（四元组带来源） | `stores/metrics.js::quad()` | `verify_metrics.mjs`（37 条） |
| C1 | **高性能画面模式**（ISA-101 灰阶 + 非颜色冗余） | `utils/colorSemantics.js::hiPerfRef`（全站唯一 ref） | `verify_hmi_palette.mjs`（62 条，含本轮新增 G8/G9） |
| C2 | **报警合理化**（字典/折叠/延时/搁置/确认） | `services/alarms.py`（判据）+ `api/alarms.py`（读免令牌 / 写要令牌） | `verify_alarms.mjs`（**67 条**）+ `tests/test_alarms.py` |
| C5 | **可访问性**（对比度审计 + 色盲双编码）★ 方案标的 **P0**，初版漏做、追问后补 | `tools/verify_a11y.mjs`（**真算 WCAG 相对亮度**，颜色从 `colorSemantics.js` + `index.html` `:root` **同源解析**，脚本里没有第二份色表） | `verify_a11y.mjs`（**37 条**，含量具自证 A 段）；见 §9 |
| D2 | 点位**数值体检**（只查数，不查摆放） | `api/points.py` 体检端点 | `verify_point_audit.mjs` |
| D5′ | 立足面高度进配置 | `config/robot.yaml` | `verify_pad_nav.mjs` |
| E1 | 自然语言 → **意图编译 + 计划校验** | `services/intent_plan.py`（LLM 零指令权零否决权） | `verify_intent_plan.mjs` |
| E2 | **到位自证**（每步回读 + 偏差如实） | `services/motion.py` / `twin_residual.py` | `verify_twin_residual.mjs` / `verify_hold_state.mjs` |
| E3 | **反事实试跑（what-if 沙盒）** | `services/whatif.py`（**零下发**） | `tests/test_whatif.py`（30 条）+ `verify_whatif.mjs`（**64 条**） |
| E4 | **判色样本环 + 建议面** | `services/vision_samples.py`（唯一写入方）+ `color_advice.advice()` | `tests/test_color_advice.py` + `verify_color_advice.mjs`（**33 条**） |
| E5′ | 固定相机**反用法**（持件/吸空证据三档） | `services/hold_state.py` | `verify_hold_state.mjs` |
| E6 | **机器人自述日报**（只读汇总） | `services/daily_report.py` | `tests/test_daily_report.py` + `verify_daily_report.mjs`（**47 条**） |
| E7 | **一格三证**（单件/双面落料 + 放回原位） | `services/cell_evidence.py` | `verify_one_cell.mjs`（48 条） |

**没做（方案里明确划出去的）**

- **D 批摆放体检**：用户口径「物体摆放没啥问题呀」→ **整批撤回**，只留 D2 / D5′。
- **B2 产物压缩**（本轮只出诊断报告，见 §3）· **B3 按需渲染** · **B4 实例化** · **B5 拆包**：P1/P2，等 B1 面板现场证据。
- **C1 的②③④**（报警条 / 其余视图装饰彩底 / 全局装饰动画）：仍待接线，逐条写在 `colorSemantics.js` 文件头。
- **C3 关键参数四元组上屏**（P2，方案原话"要 C1 的词表先稳"）：**没做**。
  现在只有事件页顺带显示 `40003`（`EventsView.vue:481`），速度卡片上的
  "目标 + 实读 + 差值 + 60s 趋势"没有上屏 —— 别把 B1 的 `quad()` 当成 C3 已交付。
- **C4 四层导航 / 报警 ≤3 击**（P2）：**没做**（方案要求的 `verify_nav_depth.mjs` 不存在，导航收敛评审也没写结论）。
- **C5 可访问性**（★ 方案里我自己标的 **P0**）：初版**漏做**，负责人追问"全部完成了吗"后补完 → 见 §9。
  （第一版收尾汇报的"没做清单"只列了 B2~B5 和 C1②③④，**漏了 C3/C4/C5 与已做的 A3** ——
  那是汇报缺陷，不是代码缺陷；本条就是把它如实记下来。）
- **E4 的自动写回阈值**：建议只到"提案 + 人工签字"，`applied` 永远 False（铁律⑤）。

---

## 2. ★ 两条被实测推翻的方案假设（当场改掉，不留错误口径过夜）

### 2.1 方案 §134 / §413 写「本项目 3D 依赖 `J1…J6` 命名节点」——**不成立**

实测（`node frontend/tools/inspect_glb.mjs` + 直接解 GLB JSON chunk 看码点）：

- `frontend/public/models/robot_full.glb` 的 25 个命名节点是：
  `ER8-700H` · `part_0 … part_20`（20 个无意义占位名）· **5 个中文零件名**。
- **没有** `J1…J6` 任何一个 → 方案据此推出的"Draco 破坏命名 = 功能级回归"这条论证**站不住**。

**真实的命名契约**（代码里核对过，两处同源）：

| 位置 | 依赖 |
|---|---|
| `frontend/src/three/manager.js:56-69` | `fix()` 把 GLTFLoader 按 latin1 读出的**GBK 字节**还原成中文，再查 `NAME_TO_LINK = { 转座:1, 大臂:2, 手腕体:4, 手腕:5, 法兰:6 }` |
| `frontend/tools/verify_official_model.mjs:62` | `NAME2LINK` 同一套规则（钉与生产同源） |

⇒ 压缩要保住的是**这 5 个中文节点名**（外加根节点 `ER8-700H`），而**分类主要靠几何包围盒**
（`classify()` 第一档就是 `maxDim > 900 → -1`），`part_N` 那 20 个名字**没有任何消费者**。

### 2.2 「meshopt 保留命名 / Draco 破坏命名」——**未证实**，且风险不在这儿

两种压缩都只动**几何 buffer**，节点名与层级不变；真正会破坏上面那份契约的是
`weld` / `dedup` / `prune` 这类**合并 primitive** 的步骤（不同零件被并成一个 mesh →
名字与几何同时失效 → 连杆归类错 = **3D 与真机不一致的"假孪生"回归**）。

⇒ 方案 §412 的优先顺序（`weld + prune + dedup` → meshopt）**要倒过来**：
先只做"量化压缩、不合并"，并靠 `verify_official_model.mjs` + `verify_kinematics.mjs` +
`npm run preview:3d` 三样证明**命名与对齐都没变**，才谈下一步。

---

## 3. B2 · GLB 诊断报告（`inspect_glb.mjs` 只读输出，原样贴）

```
文件：public\models\robot_full.glb
体积：4238.1 KB（实际读取 4238.1 KB）· GLB v2 · BIN chunk 4216.5 KB · JSON chunk 21.6 KB

节点 25（有名字的 25）· 网格 24 · primitive 24（带索引 24）· 三角面约 137,558
材质 24 · 纹理 0 · 图像 0 · 贴图合计 0.0 KB（占 BIN 的 0.0%）
动画 0 · buffer 1 · bufferView 72 · accessor 72
关节命名节点（J1…J6）：未找到（改名了？先别压缩）
局部坐标范围（含网格顶点 min/max，未叠加节点变换）： x -220.2…467.5 y -129.0…127.0 z -1.0…810.3
```

**读数结论（三条，全部有上表数字支撑）**

1. **贴图优化（KTX2/basis）无对象**：纹理 0 / 图像 0 / 贴图合计 0.0 KB。
2. **体积 100% 在几何**：BIN 4216.5 KB ÷ 总体积 4238.1 KB = **99.5%**；24 primitive / 137,558 三角形。
3. **无动画**：`animations: 0` → 与本项目"姿态由 Modbus 实时驱动、动画由我们自己插值"的口径一致，压缩不会丢动画。

**为什么本轮不动产物（诚实交代）**

- 收益面：唯一访问路径是**本机 `http://localhost:8000`**（后端托管 dist），一次性 4.2 MB 内网传输；
  方案原本预期"省 3 MB 值得做"，在**这个部署形态**下收益主要是首屏等待，不是带宽。
- 风险面：见 §2 —— 合并/改名会直接造成**3D 与真机不一致**，那是本项目最贵的一类 bug（假显示）。
- 决策条件（写死，别凭印象动）：先由 **B1 指标面板**在现场量到"模型加载确实慢"，
  再按 §2.2 的倒序（只量化、不合并）做，并且必须过
  `verify_official_model.mjs` / `verify_kinematics.mjs` / `npm run preview:3d` / `npm run measure:ee`。

**不做什么**：不重绘或减面"为了好看"；不动 DH 对齐；不引入 `@gltf-transform/cli` 作为依赖
（诊断用自研只读脚本 `frontend/tools/inspect_glb.mjs`，零依赖、零产物）。

---

## 4. B6 · 回归套件自身的耗时预算

口径（`frontend/tools/time_verify.mjs`）：只计 `npm run verify` 链上的 `node tools/*.mjs` 步骤，
逐个跑并计时；**单步 > 20s 或总计 > 180s 只 WARN、不 FAIL**，但**任何一步失败仍然失败退出**
（预算是性能问题，正确性是另一回事——不能因为"跑得快"把红钉放绿）。

本轮实测（挂链 43 个脚本之后）：

| 指标 | 实测 | 预算 | 占用 |
|---|---|---|---|
| 计时步骤数 | **44**（43 个 `verify_*` + `check_imports`） | — | — |
| 合计 | **3.0s** | 180s | **1.7%** |
| 最慢单步 | **0.2s**（`check_imports.mjs`） | 20s | 1% |

**结论**

- 预算**完全不紧张** ⇒ 不需要拆链、不需要并行、不需要"快/全"两档。
  "慢到没人愿意跑 = 回归钉形同虚设"这个担忧目前不成立（整套 3 秒，比一次后端 pytest 的 44 秒便宜得多）。
- 新并入链的**五个**守卫（之前写完就没人再跑，属于 §5.5 铁律⑤那类"钉了但永远不执行"）：
  `verify_hmi_palette.mjs` · `verify_alarms.mjs` · `verify_daily_report.mjs` ·
  `verify_color_advice.mjs` · `verify_whatif.mjs`。
  并且**五个脚本各自加了一条"我已在链上"的钉**（G9 / G1 / E14 / E4 / G5）——
  链是口头承诺不算接线，钉住"链里真的有我"才不会再次悄悄掉链。

---

## 5. ★ 测试隔离的**第四项**共享状态（本轮排查出来的，建议负责人拍板）

AGENTS.md §5.5 要求测试隔离三样：`EFORT_DB_URL` · `EFORT_SIMULATE` · `EFORT_LOGS_DIR`。
实测发现**还有第四样没隔离**：

- `backend/app/core/app_settings.py:60` 的 `SETTINGS_PATH` 是**模块级常量**，
  = `project_root()/config/app_settings.json` —— 也就是**生产配置文件本身**。
- `tests/conftest.py:82` 的 `_reset_settings_overlay`（autouse）在每个用例**前后删除该文件**来兜污染，
  单跑一切正常，所以这个隐患平时看不见。
- **真实现场**：我为了赶进度**并发跑了两个 pytest 进程**，两个进程同时读写**同一个真实文件**
  → `test_settings.py` 出现 6 → 2 → 0 个**随机假故障**。代码没有 bug（探针脚本证明 PUT 落盘正常），
  纯粹是共享状态被打架。

**因此立两条纪律**

1. **同一时刻只允许一个 pytest 进程**（不要为了省时间并行跑两套后端测试）。
2. 建议把覆盖层路径也做成可隔离：`SETTINGS_PATH = os.environ.get("EFORT_SETTINGS_PATH", 默认)`，
  测试 conftest 指到临时目录 —— 这属于配置口径改动，**本轮没动**，等负责人点头。

---

## 6. 本轮顺手修的**钉本身**（钉错了比没钉更糟）

写守卫的人看不见生产代码的真实形状，就会写出"永远不成立的形状匹配"。本轮实测红了的 10 条，
逐条按"到底是代码错还是钉错"判过：

| 钉 | 真相 | 处理 |
|---|---|---|
| `verify_alarms` A1 | 登记表写点确实只有 2 + 1 处，但都在函数里**带缩进**，钉用了 `^_RULES[` | 改成 `^[ \t]*_RULES[` |
| `verify_alarms` B3 | `_log` 是方法，签名带 `self`，钉按无 `self` 写 | 补 `self, ` |
| `verify_alarms` B5 | 真实分区键是 `key = rule.id + (("\|" + scope) if scope else "")`（多一层括号） | 按真实字面量重写，并加钉 `_scope_of` 的"无 scope_fields 就返回空" |
| `verify_alarms` B17 | `_CODE_RE` 的反证写法把自己绕死了（三重转义 + 两个 `=== false`） | 换成"正确形状必须在 + 裸形状必须不在"两条 |
| `verify_alarms` C7 | 注释里的星号是 `★`（U+2605），钉写成 `\*` | 改字面量，并加"水位不许等于 `len(...)`" |
| `verify_alarms` E6 | `def ack/shelve/...` 真实是 **8** 个（类方法 4 + 模块级转发 4），钉写死 4 | 改成"两类各 4 + 消费者清单逐文件核过" |
| `verify_alarms` F9 | 模板是 `×{{ a.count ?? "—" }}`（`??` 两边都有空格） | 补空格 |
| `verify_alarms` F12 | 用"240 字符窗口内不许出现 `controlActive`"判门控 → 邻近代码假命中 | 改成**只取 `loadAlarms` 函数体**再判 |
| `verify_alarms` F17 | 把 JS 注释里的 ASCII 引号也算成隐患（真隐患是串里套串，`check_cjk_bare` 已结构化拦下） | 收窄到"看得见的模板区 + 后端文档串 + 守卫在链上"三条；并把 EventsView 里那句**真**该改的文案 `"这次没采到"` → `「这次没采到」` |
| `verify_daily_report` E2 | 写成 `… \|\| true` —— **永远为真的死钉**（铁律⑤） | 换成真判据：`dependencies`/`devDependencies` 两块里的包名集合必须还是那 8 个 |

另外纠了两处**注释与事实不符**（"写了没接 = 编故事"）：

- `utils/colorSemantics.js` 文件头的待接线清单：① 开关 与 ⑤ 挂链 **本轮真的接了** →
  改成"已接线（并列明是谁接的）"，其余 ②③④ 仍如实留在"仍待接线"；
  新增 `verify_hmi_palette` **G8**（store 持久化 + App 挂载应用 + 设置页勾选 三处串联，缺一就红）
  与 **G9**（脚本确实在链上）把这两句承诺钉死。
- `verify_whatif` G5 原话"并入由负责人统一做，这里不擅自改 package.json" → 改成"已并入"并加链上判据。

---

## 7. 待现场复核 / 已知不足（不留假成功）

1. **B2 的决策数据还缺**：需要 B1 面板在**现场机**上量到"模型加载耗时"，才能判断压缩值不值得做（§3）。
2. **C1 只做完了安全信号链三处 + 开关接线**，②③④（报警条 / 其余视图 / 全局装饰动画）没接，
   所以现在的"高性能画面"是**局部生效**，界面上那句状态字如实写着"高性能画面（ISA-101 灰阶）"
   指的是**着色出口换了**，不是"全站已灰阶"——文件头与卡片都写明了。
3. **E3 的节拍档仍然只代已发生的实测段**，没有实测就印「无实测样本·不给节拍估计」；
   这是**刻意的**（上位机拿不到控制器速度曲线，任何 ETA 都是编故事）。
4. **E4 的建议永远不自动生效**（`applied: false`），要生效必须走"提案 → 人工签字 → 现场跑
   `camera/tools/vision_e2e_replay.py`"那条路（阈值口径：现场实测值，不按合成图）。
5. **第四项共享状态**（§5）只写了纪律，没改代码。

---

## 8. 一句话给接手的人

这一轮真正新增的不是九个功能，而是**两份实测数字**和**一条被推翻的假设**：
GLB 里没有贴图也没有 `J1…J6`，回归链只花 3 秒 ——
**"先量再改"在这个项目里不是口号，是每个 P1 项的入场券。**

---

## 9. ★ 追问后的补做：C5 可访问性（方案自标 P0，初版漏了）

负责人问「全部完成了吗？」→ 自查发现**上一版收尾汇报的"没做清单"本身不完整**：
漏列了 C3 / C4 / C5（也漏列了已做的 A3）。其中 **C5 是方案里我自己标的 P0**
（原话「便宜且是硬伤」），漏它没有任何借口，当场补完。

### 9.1 交付：`frontend/tools/verify_a11y.mjs`（37 条，已并入 `npm run verify` 链）

**不是形容词，是算出来的**：脚本内实现 WCAG 相对亮度公式，颜色**同源解析**
`src/utils/colorSemantics.js` + `index.html` 的 `:root`（脚本里**没有第二份色表** ——
抄一份就是假审计，违反铁律④）。半透明底纹先合成到卡片底再算（`--*-soft` 直接当不透明底
会算出 1.00:1 的荒谬值，脚本里把这个错法也钉出来了）。

**量具先自证**（A 段）：白/黑 = 21.00、同色 = 1.00、`#767676/#ffffff` = 4.54（AA 边界）、
`#999999/#ffffff` = 2.85（该判不过）、灰阶五层按 L\* 严格递增、算不出返回 `null`（不许塌成"通过"）。

### 9.2 实测结果与一处真整改

| 语义（hiPerf） | 文字/底 | 信号边框/底 | 非颜色冗余 |
|---|---|---|---|
| normal | 10.26 ✓ | 1.89（**容器线，豁免**） | — |
| warn | 7.90 ✓ | 2.77 | △ |
| danger | 4.99 ✓ | 4.99 ✓ | ▲ + 2px 实线 |
| target | 8.85 ✓ | 2.74 | ◆ |
| inferred | 13.45 ✓ | **1.89 → 4.41 ✗→✓** | ◌ + 斜纹 + 「（流程推断）」 |
| unknown | 13.45 ✓ | **1.89 → 4.41 ✗→✓** | ? + 斜纹 + 「（未知）」 |

- **文字**：两种模式 × 六个语义**全部 ≥ 4.5:1**（最低 hiPerf danger 4.99）→ 真绿。
- **改的那一条**：「推断 / 未知」的 2px **虚线**边过去取 `gray("line")`（L\*32 → **1.89:1**）。
  虚线本身比实线更难看见，而这条虚线是 A6/E5′「**不知道就说不知道**」在画面上唯一的可见载体 ——
  强光屏 / 远端投屏上等于没编码。**提到 `gray("dot")`（L\*55 → 4.41:1）**，
  并单独钉 `B3`（防止有人"顺手"降回去）。
- **刻意豁免（写在脚本注释里，不是偷偷放宽阈值）**：纯容器线（`normal` 的分组边、
  `gray("line")` 的层级底纹）**不受 3:1 约束** —— ISA-101 有意把容器显著性压到最低，
  让真正危险的信号突显；给它们都套 3:1 会逼出满屏高对比边框，反而毁掉这个模式。
  base 模式（现状外观）的边框同理只**打印实测值**：因为 C1 的**非破坏式承诺**是
  "关着开关时一个像素都不变"，本轮不动既有外观。

### 9.3 这一条给接手的人的教训

**方案里标了 P0 的项，收尾汇报必须逐条对上"做了/没做"** —— 漏列比做漏更糟：
它让"没做清单"看起来是完整的。补完后 `verify:time` = **45 步 · 3.0s · 断言 341 过 / 0 败**
（新增 37 条断言只多花 0.1s）。
