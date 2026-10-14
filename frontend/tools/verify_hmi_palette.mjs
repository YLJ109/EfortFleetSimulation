// =====================================================================
// ★★ C1「高性能画面模式 + 全站唯一颜色语义表」守卫（headless 静态 + 纯函数检查）
//
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §C1
//
// 立意（ISA-101 风格的工业 HMI 配色纪律）：操作员长期盯屏，"花花绿绿"会让**真正危险的
// 信号失去突显**。所以这个模式是**非破坏式开关**：开启后装饰色退成灰阶（L* 分层），
// 但危险/预警/物料色**更突出**，而「推断 / 未知」拿到**冗余编码**（虚线 + 斜纹 + 文字后缀），
// 不再只靠色相差别（色弱 / 强光现场 / 远端投屏都分不清色相）。
//
// 本脚本钉的就是这件事最容易被做坏的六个方式：
//   ① 语义表**只有一份**，七个语义键都有、两套值（base / hiPerf）都有；
//   ② 物料七色**只引用** SORT_COLOR_HEX，表里不许出现第二份七色字面量（业务色绝不灰阶）；
//   ③ hiPerf 下 danger 必须有**非颜色**的冗余、warn 必须**保留黄**、inferred/unknown
//      必须与"正常"**不同形**（虚线 + 后缀）；
//   ④ 装饰动画白名单只有 `hit`；
//   ⑤ **默认关**且模式判定**只有一个来源**（组件不许再自己判一遍）；
//   ⑥ 迁移过的三个文件里不再散写语义色字面量，而 A6 的**判据函数一个字节没动**。
//
// 运行： cd frontend && node tools/verify_hmi_palette.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8")
  .split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const SEM_SRC = read("src/utils/colorSemantics.js");
const CHIP_SRC = read("src/components/SafetyChip.vue");
const PANEL_SRC = read("src/components/SafetyPanel.vue");
const LABELS_SRC = read("src/utils/safetyLabels.js");
const BLOCKS_SRC = read("src/three/sortingBlocks.js");
const APP_SRC = read("src/App.vue");const SETTINGS_SRC = read("src/components/SettingsView.vue");
const UI_SRC = read("src/stores/ui.js");

const mod = await import("../src/utils/colorSemantics.js");
const labels = await import("../src/utils/safetyLabels.js");
const blocks = await import("../src/three/sortingBlocks.js");

const KEYS = ["normal", "warn", "danger", "target", "inferred", "unknown", "material"];
const BASE = mod.semantics("base");
const HI = mod.semantics("hiPerf");

console.log("\n=== C1 高性能画面模式 + 唯一颜色语义表 ===\n");

// ---------------------------------------------------------------- A1 表存在、键齐、两套值齐
ok("A1 语义表导出在（COLOR_SEMANTICS / semantics / clsFor / styleVarsFor / labelSuffix / decorationAllowed / hiPerfMode）",
  ["COLOR_SEMANTICS", "semantics", "clsFor", "styleVarsFor", "labelSuffix",
    "decorationAllowed", "hiPerfMode", "setHiPerfMode", "materialColor"]
    .every((k) => mod[k] !== undefined));
ok("A2 七个语义键一个不少，且顺序固定",
  KEYS.every((k) => !!mod.COLOR_SEMANTICS[k]) && mod.SEMANTIC_KEYS.join(",") === KEYS.join(","));
ok("A3 每个语义都有 base 与 hiPerf 两套值（不许只写一套再兜底）",
  KEYS.every((k) => mod.COLOR_SEMANTICS[k].base && mod.COLOR_SEMANTICS[k].hiPerf));
ok("A4 semantics() 两种模式都返回整套七键",
  KEYS.every((k) => BASE[k] && HI[k]) && KEYS.every((k) => HI[k] && BASE[k]));
ok("A5 语义表是**唯一出处**：其它三个被迁移的文件里没有第二份语义定义",
  !/COLOR_SEMANTICS\s*=/.test(CHIP_SRC + PANEL_SRC + LABELS_SRC));
ok("A6 认不得的语义键不塌成 normal（fail-safe 落 unknown = 如实说不知道）",
  mod.clsFor("qwerty", "base").indexOf("sem-unknown") >= 0
  && mod.styleVarsFor("qwerty", "hiPerf")["--hmi-border-style"] === "dashed");

// ---------------------------------------------------------------- A2 灰阶分层（不是同一个灰）
ok("A7 正常态在 hiPerf 下是灰阶（不含任何 var(--ok) 之类的彩底）",
  ["fg", "dot", "bg", "border"].every((p) => /^#[0-9a-f]{6}$/.test(HI.normal[p] || "")),
  JSON.stringify(HI.normal));
ok("A8 灰阶按 L* **分层**（底/线/点/文字/高亮 五个互不相同）",
  new Set(["bg", "line", "dot", "fg", "hi"].map((t) => mod.gray(t))).size === 5);
ok("A9 灰阶层级是**由暗到亮**（L* 单调，操作员看得出层级）",
  ["bg", "line", "dot", "fg", "hi"].map((t) => mod.GRAY_TIERS[t]).every((v, i, a) => i === 0 || v > a[i - 1]));
ok("A10 lstarToHex 落在合法范围（越界一律夹到 0~100，不产出 NaN）",
  mod.lstarToHex(0) === "#000000" && mod.lstarToHex(100) === "#ffffff"
  && mod.lstarToHex(999) === "#ffffff" && !/NaN/.test(mod.lstarToHex(Number.NaN)));

// ---------------------------------------------------------------- B  物料色只引用、不复制
ok("B1 material 走的是对 SORT_COLOR_HEX 的**引用**（同一个对象，不是拷贝）",
  HI.material.byColor === blocks.SORT_COLOR_HEX && BASE.material.byColor === blocks.SORT_COLOR_HEX);
ok("B2 语义表里**没有第二份七色字面量**（整份源码零个 #rrggbb）",
  !/#[0-9a-fA-F]{6}\b/.test(SEM_SRC), "colorSemantics.js 里出现了十六进制字面量");
ok("B3 materialColor 由 SORT_COLOR_HEX 现推（红/绿/蓝/黑 与 3D 完全同值）",
  Object.keys(blocks.SORT_COLOR_HEX).every((n) =>
    mod.materialColor(n) === "#" + blocks.SORT_COLOR_HEX[n].toString(16).padStart(6, "0")));
ok("B4 物料色两种模式**完全同一个对象**（绝不参与灰阶）",
  HI.material === BASE.material && mod.isGrayable("material") === false);
ok("B5 SORT_COLOR_HEX 本身一个字节没动（本任务不许改既有色卡）",
  /export const SORT_COLOR_HEX = \{ "红": 0xe02020, "绿": 0x1fa74a, "蓝": 0x1f4fd8, "黑": 0x1c1f24 \};/.test(BLOCKS_SRC));
ok("B6 表里**不许**从 sortingBlocks 之外再拿物料色（唯一 import）",
  /import \{ SORT_COLOR_HEX \} from "\.\.\/three\/sortingBlocks\.js";/.test(SEM_SRC));

// ---------------------------------------------------------------- C  hiPerf 下危险更突出
ok("C1 hiPerf 的 danger 仍是红（颜色本身不许退彩 —— 那是信号不是装饰）",
  /--err/.test(HI.danger.fg) && /--err/.test(HI.danger.dot) && /--err/.test(HI.danger.border));
ok("C2 hiPerf 的 danger 有**非颜色**冗余：更粗实边框（2px solid）",
  HI.danger.borderWidth === "2px" && HI.danger.borderStyle === "solid");
ok("C3 hiPerf 的 danger 还有形状冗余（角标 ▲），且 base 侧不显示（非破坏式）",
  !!HI.danger.mark && mod.markOf("danger", "hiPerf") === HI.danger.mark);
ok("C4 warn 在 hiPerf 下**保留黄**（预警不许跟着退彩）",
  /--warn/.test(HI.warn.fg) && /--warn/.test(HI.warn.dot));
ok("C5 target 两套都是蓝（设定值不参与退彩）",
  /--accent/.test(BASE.target.fg) && /--accent/.test(HI.target.fg));
ok("C6 hit（碰撞）归入 danger 语义，不是第五种颜色",
  labels.STATE_SEMANTIC.hit === "danger" && labels.safetySemantic({ state: "hit" }) === "danger");
ok("C7 分级映射只有一份（STATE_SEMANTIC），且与既有两字标签同源",
  Object.keys(labels.STATE_SEMANTIC).join(",") === "safe,warn,danger,hit"
  && labels.STATE_SEMANTIC.safe === "normal" && labels.STATE_SEMANTIC.danger === "danger");

// ---------------------------------------------------------------- D  推断 / 未知：冗余编码
ok("D1 inferred / unknown 在 hiPerf 下是**虚线边框**（正常态是实线 → 形态上就分得开）",
  HI.inferred.borderStyle === "dashed" && HI.unknown.borderStyle === "dashed"
  && HI.normal.borderStyle === "solid");
ok("D2 inferred / unknown 在 hiPerf 下有**斜纹填充**（正常态没有）",
  /repeating-linear-gradient/.test(HI.inferred.bgImage)
  && /repeating-linear-gradient/.test(HI.unknown.bgImage)
  && HI.normal.bgImage === "none");
ok("D3 文字后缀：inferred → 「（流程推断）」、unknown → 「（未知）」，其余空串",
  mod.labelSuffix("inferred") === "（流程推断）" && mod.labelSuffix("unknown") === "（未知）"
  && mod.labelSuffix("normal") === "" && mod.labelSuffix("warn") === ""
  && mod.labelSuffix("material") === "");
ok("D4 后缀不与「正常」同形：hiPerf 的 normal 既没虚线也没后缀",
  HI.normal.suffix === "" && HI.normal.borderStyle === "solid" && HI.normal.hatch === false);
ok("D5 评估不新鲜 → unknown（A6 三态不许塌成「看着还行」）",
  labels.healthSemantic({ ok: false }) === "unknown" && labels.healthSemantic({ ok: true }) === "");
ok("D6 残影预演（source=ghost）= 流程推断 → 该拿 inferred 的冗余标注",
  labels.isInferredSource({ source: "ghost" }) === true
  && labels.isInferredSource({ source: "robot" }) === false);
ok("D7 base 侧的后缀一律空串（关掉模式时界面文案与改动前逐字一致）",
  KEYS.every((k) => BASE[k].suffix === ""));

// ---------------------------------------------------------------- E  装饰动画白名单
ok("E1 decorationAllowed（hit, hiPerf）=== true（碰撞的闪是信号，保留）",
  mod.decorationAllowed("hit", "hiPerf") === true);
ok("E2 其余装饰动画在 hiPerf 下全部关闭（含认不得的名字 = fail-safe）",
  ["float", "breathe", "gradient", "spin", "settle", "pulse", "whatever"]
    .every((a) => mod.decorationAllowed(a, "hiPerf") === false));
ok("E3 base 模式不改现有行为（任何动画名都放行）",
  ["hit", "float", "spin"].every((a) => mod.decorationAllowed(a, "base") === true));
ok("E4 状态片确实用白名单决定闪烁（不是写死 infinite）",
  /decorationAllowed\("hit", mode\.value\)/.test(CHIP_SRC));

// ---------------------------------------------------------------- F  默认关 + 唯一来源
ok("F1 默认**关**（新语义表不改现有操作员习惯）",
  mod.hiPerfRef.value === false && mod.hiPerfMode() === "base");
ok("F2 模式判定只有**一处**状态：全站只有 colorSemantics.js 定义 hiPerfRef",
  (SEM_SRC.match(/const hiPerfRef = ref\(/g) || []).length === 1
  && !/const hiPerfRef\b/.test(CHIP_SRC + PANEL_SRC + LABELS_SRC + APP_SRC + SETTINGS_SRC));
ok("F3 组件不自己另判模式（不读 localStorage / 不建第二个 ref，只调 hiPerfMode()）",
  /hiPerfMode\(\)/.test(CHIP_SRC) && /hiPerfMode\(\)/.test(PANEL_SRC)
  && !/localStorage[^]*hiPerf/.test(CHIP_SRC + PANEL_SRC));
ok("F4 唯一写入者 = setHiPerfMode / toggleHiPerfMode（本任务不改其它文件，组件里零写入）",
  !/setHiPerfMode\(|toggleHiPerfMode\(/.test(CHIP_SRC + PANEL_SRC + LABELS_SRC));
ok("F5 开关会同步根元素属性 data-hmi（其它视图的全局灰阶选择器靠它；此处用假 document 验证）",
  (() => {
    const seen = {};
    globalThis.document = { documentElement: { setAttribute: (k, v) => { seen[k] = v; } } };
    mod.setHiPerfMode(true);
    const on = seen["data-hmi"];
    mod.setHiPerfMode(false);
    const off = seen["data-hmi"];
    delete globalThis.document;
    return mod.hiPerfMode() === "base" && on === "hi-perf" && off === "normal";
  })());

// ---------------------------------------------------------------- G  非破坏式（关了就一条都不命中）
ok("G1 base 模式下 clsFor **不带** hmi-hi-perf（样式覆盖层根本不会出现）",
  KEYS.every((k) => mod.clsFor(k, "base").indexOf("hmi-hi-perf") < 0)
  && mod.clsFor("danger", "hiPerf").indexOf("hmi-hi-perf") >= 0);
const chipStyle = (CHIP_SRC.match(/<style[\s\S]*?<\/style>/) || [""])[0];
const panelStyle = (PANEL_SRC.match(/<style[\s\S]*?<\/style>/) || [""])[0];
ok("G2 状态片新样式全部挂在 .hmi-hi-perf 底下（逐条声明核对）",
  chipStyle.indexOf("--hmi-") >= 0
  && chipStyle.split("}").filter((b) => /--hmi-/.test(b)).every((b) => /\.hmi-hi-perf/.test(b)));
ok("G3 调试面板新样式同样全部挂在 .hmi-hi-perf 底下",
  panelStyle.indexOf("--hmi-") >= 0
  && panelStyle.split("}").filter((b) => /--hmi-/.test(b)).every((b) => /\.hmi-hi-perf/.test(b)));
ok("G4 组件里不写颜色值，只写 CSS 变量出口（值全部来自表）",
  !/#\s?[0-9a-fA-F]{6}\b/.test(CHIP_SRC + PANEL_SRC)
  && /var\(--hmi-fg\)/.test(CHIP_SRC) && /var\(--hmi-border\)/.test(PANEL_SRC));
ok("G5 诚实声明在文件头：全站灰阶**还没接线**（不假装开个关就全灰了）",
  /待接线/.test(SEM_SRC) && /全站灰阶并没有/.test(SEM_SRC));
ok("G6 待接线清单逐条写名（开关 / 报警条 / 其余视图 / 全局装饰动画 / verify 链），不是空话",
  /SettingsView\.vue/.test(SEM_SRC) && /safety-banner/.test(SEM_SRC)
  && /decorationAllowed\(animName, mode\)/.test(SEM_SRC)
  && /verify_hmi_palette\.mjs/.test(SEM_SRC) && /默认关/.test(SEM_SRC));
ok("G7 根属性约定可核对：data-hmi 与选择器写法都在注释里（选择器长什么样 = 由谁挂）",
  /html\[data-hmi="hi-perf"\]/.test(SEM_SRC) && mod.HMI_ATTR === "data-hmi"
  && mod.HMI_ATTR_ON === "hi-perf" && mod.HMI_CLASS === "hmi-hi-perf");
// ★ G8 钉的是注释里那句「① 已接线」是不是**真的**（§5.5 铁律⑤：写了没接就是编故事）。
//   三处缺一不可：store 里存并落地 / App 挂载时应用 / 设置页有能按的那个开关。
ok("G8 ★ 注释说「开关已接线」是真的：ui store 持久化 + App 挂载应用 + 设置页勾选（三者串联）",
  /hiPerf: s\.hiPerf === true/.test(UI_SRC)
  && /setHiPerf\(v\) \{/.test(UI_SRC)
  && /this\.hiPerf = on/.test(UI_SRC)
  && /setHiPerfMode\(on\)/.test(UI_SRC)
  && /hiPerf/.test(UI_SRC.split("_persist()")[1] || "")
  && /ui\.applyHiPerf\(\)/.test(APP_SRC)
  && /ui\.setHiPerf\(\$event\.target\.checked\)/.test(SETTINGS_SRC));
ok("G9 ★ 本脚本已并入 `npm run verify`（注释里承诺的「⑤ 已并入」也必须是事实）",
  /node tools\/verify_hmi_palette\.mjs/.test(read("package.json")));

// ---------------------------------------------------------------- H  迁移的三个文件接的是表
ok("H1 状态片从 colorSemantics 导入（不再散写语义色字面量）",
  /from "\.\.\/utils\/colorSemantics\.js"/.test(CHIP_SRC)
  && /clsFor/.test(CHIP_SRC) && /styleVarsFor/.test(CHIP_SRC));
ok("H2 调试面板同样从 colorSemantics 导入",
  /from "\.\.\/utils\/colorSemantics\.js"/.test(PANEL_SRC) && /styleVarsFor/.test(PANEL_SRC));
ok("H3 safetyLabels 只做**语义映射**、不含任何颜色字面量（判据与着色分层）",
  !/#\s?[0-9a-fA-F]{6}\b/.test(LABELS_SRC) && !/var\(--/.test(LABELS_SRC)
  && /export const STATE_SEMANTIC/.test(LABELS_SRC));
ok("H4 分级判据没有被搬到别处：状态片仍然只吃 store.health",
  /health\.value\.ok/.test(CHIP_SRC) && /stalled/.test(CHIP_SRC)
  && /function freshState/.test(LABELS_SRC) && !/function freshState/.test(CHIP_SRC));
ok("H5 面板顶部徽标的判据没变（headState 仍用 safety.health）",
  /const headState = computed\(\(\) => \(safety\.health && !safety\.health\.ok/.test(PANEL_SRC));

// ---------------------------------------------------------------- I  判据函数一个字节不许改
const freshBody = (LABELS_SRC.split("export function freshState(")[1] || "").split("\n\n\n")[0];
const healthBody = (LABELS_SRC.split("export function evalHealth(")[1] || "").split("// ====")[0];
ok("I1 freshState 的阈值常量还是 2000 / 5000（A6 三态口径没被顺手改）",
  /export const FRESH_MAX_MS = 2000;/.test(LABELS_SRC)
  && /export const FRESH_STOP_MS = 5000;/.test(LABELS_SRC));
ok("I2 freshState 函数体仍引用这两个阈值 + never 的判据原样",
  /FRESH_MAX_MS/.test(freshBody) && /FRESH_STOP_MS/.test(freshBody)
  && /Number\.isFinite\(ageMs\) \|\| ageMs < 0/.test(freshBody));
ok("I3 evalHealth 函数体仍是三态 + ok 只认 fresh",
  /const state = freshState\(age\)/.test(healthBody) && /ok: state === "fresh"/.test(healthBody));
ok("I4 三态实测没变：0/3000/30000 → fresh/stale/stopped，null → never",
  labels.freshState(0) === "fresh" && labels.freshState(3000) === "stale"
  && labels.freshState(30000) === "stopped" && labels.freshState(null) === "never");
ok("I5 两字标签 / 话术表原样（STATE_LABEL / HEALTH_LABEL）",
  labels.STATE_LABEL.hit === "碰撞" && labels.HEALTH_LABEL.stopped === "已停止"
  && labels.safetySemantic({ state: "warn" }) === "warn");
ok("I6 状态片的 stalled 仍然优先于分级（不新鲜的「安全」一律不算数）",
  (() => {
    const semFor = (state, okHealth) => (okHealth ? labels.safetySemantic({ state })
      : labels.healthSemantic({ ok: okHealth }));
    return semFor("safe", false) === "unknown" && semFor("safe", true) === "normal";
  })());

// ---------------------------------------------------------------- J  回归链与本任务边界
ok("J1 样式出口只有 --hmi-* 一个前缀（组件不许自造 CSS 变量名 → 接不上就是静默失效）",
  Object.keys(mod.styleVarsFor("danger", "hiPerf")).every((k) => k.startsWith("--hmi-"))
  && Object.keys(mod.styleVarsFor("normal", "base")).length >= 8);
ok("J2 没碰禁改文件（App.vue / SettingsView.vue 里没有本任务的第二套语义表）",
  !/colorSemantics/.test(APP_SRC + SETTINGS_SRC));
ok("J3 既有安全守卫仍在（不许用本任务替换 verify_safety / verify_safety_text）",
  read("tools/verify_safety.mjs").indexOf("safetyLabels.js") >= 0
  && read("tools/verify_safety_text.mjs").indexOf("safetyLabels.js") >= 0);

console.log(`\n高性能画面模式（C1）守卫：${pass} 过 / ${fail} 败`);
if (fail) process.exit(1);
