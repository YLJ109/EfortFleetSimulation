// =====================================================================
// ★★ C5「可访问性（色盲双编码 + 对比度审计）」守卫（headless：真算 WCAG 相对亮度）
//
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §C5
// 优先级：**P0**（方案原话「便宜且是硬伤」）。
//
// 为什么必须有这个脚本（而不是"看着挺清楚"）：
//   本项目的画面在**强光车间 / 色弱操作员 / 远端投屏**三种条件下都要能读出
//   "危险 / 预警 / 推断 / 未知"。这四类里除了物料色，全部只靠**色相 + 一层很淡的边**区分
//   —— 那正是最先失效的两种编码。对比度是唯一能被客观量出来的东西，所以它必须是**算出来的**，
//   不是形容词。
//
// 判据口径（逐条写明，不放宽）：
//   ① **文字**（每个语义的 fg 相对它真正落在的底色）≥ 4.5:1 —— WCAG AA 正文档，两种模式都判。
//   ② **信号形状**：被"当作信号用"的那几条边（hiPerf 的 inferred / unknown / danger）≥ 3:1
//      —— WCAG 1.4.11 非文本对比度档。★ 这一条就是本轮**改代码**改出来的：
//      「推断 / 未知」的 2px 虚线边过去取 line 档（L*32 → 1.89:1），
//      虚线比实线更难看见，而这条虚线是「不知道就说不知道」在画面上唯一的可见载体，
//      所以提到 dot 档（L*55）。钉在 B 段，别改回去。
//   ③ **非颜色冗余（双编码）**：hiPerf 下 warn / danger / target 必须有角标字形，
//      inferred / unknown 必须同时有「角标 + 斜纹 + 文字后缀」—— 灰阶模式里色相已经没了，
//      没有形状/文字就**什么都读不出来**。
//   ④ **物料七色永不灰阶**（业务语义：红/绿/蓝 = 分拣目的地，不是装饰）+ 分拣格子必须带
//      **字母/文字**（A1…B9 / C检测 / 开始结束点），不许"只有一块颜色"。
//   ⑤ **键盘全可达**沿用既有快捷键守卫（verify_shortcuts.mjs），本脚本只钉它还挂在链上。
//
// ★ 诚实豁免（写在代码里而不是偷偷放宽阈值）：
//   **纯容器线**（normal 的分组边、gray("line") 的层级底纹）**不受 3:1 约束** ——
//   ISA-101 有意把容器显著性压到最低，让"真正危险的信号"突显。把它们判成 FAIL 会逼出
//   满屏高对比边框，反而毁掉这个模式的目的。所以：容器线只**打印实测值**，不参与判定。
//   base 模式（现状外观）同理：文字一律判（①），边框不达标的只打印成 WARN 清单，
//   因为 C1 的**非破坏式承诺**是"关着开关时一个像素都不变"—— 本轮不动既有外观，
//   要看清"推断/未知"请开高性能画面模式（那里②③齐全）。
//
// 运行： cd frontend && node tools/verify_a11y.mjs
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

// ---------------------------------------------------------------- 取数：**同源**
// ★ 颜色一律从 `src/utils/colorSemantics.js` + `index.html` 的 :root 现场解析，
//   本脚本**没有第二份色表**（铁律④：同一屏同一结论必须同源；自己抄一份就是假审计）。
const CS = await import("../src/utils/colorSemantics.js");
const SEM_SRC = read("src/utils/colorSemantics.js");
const HTML = read("index.html");
const VARS = {};
for (const m of HTML.matchAll(/^[\s]*(--[\w-]+):[\s]*([^;]+);/gm)) VARS[m[1]] = m[2].trim();

const CARD = VARS["--panel"];          // 卡片底（语义样式实际落在这上面）

function resolve(tok, depth = 0) {
  const t = String(tok).trim();
  if (!t || depth > 8) return null;
  let m = t.match(/^var\(\s*(--[\w-]+)\s*\)$/);
  if (m) return VARS[m[1]] ? resolve(VARS[m[1]], depth + 1) : null;
  m = t.match(/^rgba?\(([^)]+)\)$/i);
  if (m) {
    const p = m[1].split(",").map((x) => x.trim());
    const num = (s) => {
      const v = s && s.match(/var\(\s*(--[\w-]+)\s*\)/);
      return Number(v ? (VARS[v[1]] || "").match(/\d+/g)[0] : s);
    };
    return { r: num(p[0]), g: num(p[1]), b: num(p[2]), a: p[3] === undefined ? 1 : Number(p[3]) };
  }
  m = t.match(/^#([0-9a-f]{6})$/i);
  if (m) { const n = parseInt(m[1], 16); return { r: n >> 16 & 255, g: n >> 8 & 255, b: n & 255, a: 1 }; }
  return null;                                            // transparent / currentColor / inherit
}
// sRGB 相对亮度（WCAG 定义）。合成在 sRGB 空间做，对这里用到的低透明度底纹误差 < 0.1 档，
// 判据留了余量（不拿 4.49 当过）。
const lin = (c) => ((c /= 255) <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
const lum = (p) => 0.2126 * lin(p.r) + 0.7152 * lin(p.g) + 0.0722 * lin(p.b);
const over = (fg, bg) => ({ r: fg.r * fg.a + bg.r * (1 - fg.a),
  g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 });
function contrast(fgTok, bgTok) {
  let fg = typeof fgTok === "string" ? resolve(fgTok) : fgTok;
  let bg = typeof bgTok === "string" ? resolve(bgTok) : bgTok;
  if (!fg || !bg) return null;                            // null = 算不出（不许当成"通过"）
  const c = over(fg, bg);
  const L1 = Math.max(lum(c), lum(bg)), L2 = Math.min(lum(c), lum(bg));
  return (L1 + 0.05) / (L2 + 0.05);
}
// 语义样式实际落在的底色：entry.bg 合成到卡片底；没有 bg 就是卡片底本身。
function bgOf(e) {
  const card = resolve(CARD);
  if (!e.bg || e.bg === "transparent") return card;
  const b = resolve(e.bg);
  return b ? over(b, card) : card;
}

// ---------------------------------------------------------------- A 段：量具自证
// 先证明"尺子是准的"，再拿它量颜色 —— 否则全绿只说明公式没跑起来。
const FG_MIN = 4.5;     // WCAG AA 正文
const SIGNAL_MIN = 3.0; // WCAG 1.4.11 非文本（信号边框）
const COLOR = (x) => (x === null ? "  n/a" : x.toFixed(2).padStart(5));
{
  const r = (a, b) => contrast(a, b);
  ok("A1 已知对照：白/黑 = 21.00", Math.abs(r("#ffffff", "#000000") - 21) < 0.01,
     String(r("#ffffff", "#000000")));
  ok("A2 已知对照：同色 = 1.00", Math.abs(r("#2ee6a8", "#2ee6a8") - 1) < 0.001);
  ok("A3 已知对照：#767676/#ffffff = 4.54（AA 边界该过）、#999999/#ffffff = 2.85（该判不过）",
     Math.abs(r("#767676", "#ffffff") - 4.54) < 0.02
     && r("#999999", "#ffffff") > 2.8 && r("#999999", "#ffffff") < 3.0
     && r("#999999", "#ffffff") < FG_MIN);
  // ★ 半透明底纹（`--warn-soft` 之类）**必须先合成到卡片底**再算相对亮度。
  //   三种算法给三个数：合成后 7.90（真值）· 直接对卡片底 10.17（高估）·
  //   把 rgba 原值当不透明底 **1.00**（荒谬，正好证明"错法会被自己的量具抓住"）。
  const warnE = CS.semanticOf("warn", "hiPerf");
  const cComp = contrast(warnE.fg, bgOf(warnE));
  const cCard = contrast(warnE.fg, resolve(CARD));
  const cNaive = contrast(warnE.fg, "var(--warn-soft)");
  ok("A4 半透明底纹先合成再算（合成值 < 直接对卡片底，且仍 ≥ AA；错法必然算出 1.00 的假值）",
     cComp !== null && cComp < cCard && cComp >= FG_MIN && Math.abs(cNaive - 1) < 0.01,
     `合成=${COLOR(cComp)} 卡片=${COLOR(cCard)} 错法=${COLOR(cNaive)}`);
  ok("A5 算不出一律返回 null（不许塌成 0 或「通过」—— 铁律③：未知不得塌成布尔）",
     contrast("currentColor", CARD) === null && contrast("", CARD) === null);
  const gs = ["bg", "line", "dot", "fg", "hi"].map((t) => CS.gray(t));
  const ls = gs.map((h) => lum(resolve(h)));
  ok("A6 灰阶五层按 L* 严格递增（bg<line<dot<fg<hi），层级不是随手取的同一个灰",
     ls.every((v, i) => i === 0 || v > ls[i - 1]));
  ok("A7 lstarToHex 单调且合法（六个十六进制位）",
     [0, 8, 12, 32, 55, 82, 92, 100].map((L) => CS.lstarToHex(L))
       .every((h) => /^#[0-9a-f]{6}$/.test(h))
     && CS.lstarToHex(92) > CS.lstarToHex(12));
}

// ---------------------------------------------------------------- B 段：真判据
const fgFail = [];
for (const mode of ["base", "hiPerf"]) {
  for (const k of CS.SEMANTIC_KEYS) {
    if (k === "material") continue;
    const e = CS.semanticOf(k, mode);
    const c = contrast(e.fg || "var(--txt)", bgOf(e));
    if (c === null) fgFail.push(mode + "/" + k + "=算不出");
    else if (c < FG_MIN) fgFail.push(`${mode}/${k}=${COLOR(c)}`);
    ok(`B1 ${mode}.${k} 文字对比度 ≥ 4.5:1（实测 ${COLOR(c)}）`, c !== null && c >= FG_MIN);
  }
}
// ② 信号边框：只有"被当信号用"的边参与判定（容器线如实豁免，见文件头）
const SIGNAL_BORDERS = {                     // 语义 → 这条边**在说什么**
  danger: "▲ 危险：2px 实线是「碰撞」的形状载体",
  inferred: "◌ 推断：2px 虚线 + 斜纹是「这不是实测」的形状载体",
  unknown: "? 未知：2px 虚线是「拿不到」的形状载体（A6 三态上屏）",
};
for (const [k, why] of Object.entries(SIGNAL_BORDERS)) {
  const e = CS.semanticOf(k, "hiPerf");
  const c = contrast(e.border, bgOf(e));
  ok(`B2 hiPerf.${k} 信号边框 ≥ 3:1（实测 ${COLOR(c)}）—— ${why}`,
     c !== null && c >= SIGNAL_MIN);
}
// ★ 本轮改代码的那一条单独钉死（防止有人"顺手"把虚线降回 line 档）
ok("B3 「推断/未知」的虚线边取 dot 档（不是 line 档）—— 1.89:1 的虚线在强光屏等于没有",
   CS.semanticOf("inferred", "hiPerf").border === CS.gray("dot")
   && CS.semanticOf("unknown", "hiPerf").border === CS.gray("dot"));

// ③ 非颜色冗余（双编码）
for (const k of ["warn", "danger", "target"]) {
  const e = CS.semanticOf(k, "hiPerf");
  ok(`B4 hiPerf.${k} 有角标字形（色相之外还有一层形状）`, !!e.mark);
}
for (const k of ["inferred", "unknown"]) {
  const e = CS.semanticOf(k, "hiPerf");
  ok(`B5 hiPerf.${k} 同时有「角标 + 斜纹 + 文字后缀」三样（灰阶里没色相可用）`,
     !!e.mark && e.hatch === true && !!e.suffix && CS.labelSuffix(k) === e.suffix);
  ok(`B5b ${k} 的后缀词与 SUFFIX_LABEL 同源（组件不许自己编文案）`,
     CS.SUFFIX_LABEL[k] === e.suffix && CS.labelSuffix(k) === CS.SUFFIX_LABEL[k]);
}
ok("B6 danger 在 hiPerf 下边宽升到 2px（形状差异不只是换个色）",
   CS.semanticOf("danger", "hiPerf").borderWidth === "2px"
   && CS.semanticOf("normal", "hiPerf").borderWidth === "1px");

// ④ 物料色 = 业务语义：永不灰阶 + 格子上必须有文字
{
  const m = CS.semanticOf("material", "hiPerf");
  ok("B7 物料色在 hiPerf 下与 base **同一个对象引用**（绝不灰阶 —— 红/绿/蓝是目的地不是装饰）",
     CS.COLOR_SEMANTICS.material.base === CS.COLOR_SEMANTICS.material.hiPerf
     && m.grayable === false && m.ref === "SORT_COLOR_HEX");
  ok("B8 整张语义表里**没有任何 hex 颜色字面量**（物料色只引用 SORT_COLOR_HEX，灰阶由 L* 现算）",
     !/["'`]#[0-9a-fA-F]{6}["'`]/.test(SEM_SRC));
  const PAD = read("src/pad/PadMap2D.vue");
  const NAV = read("src/pad/nav.js");
  ok("B9 2D 格子把点名当**文字**渲染（shortLabel：A1…B9 / C检测 / 起点），不是只有一块颜色",
     /export function shortLabel/.test(NAV) && /import \{[^}]*shortLabel[^}]*\} from "\.\.\/pad\/nav\.js"/.test(PAD)
     && /\{\{\s*shortLabel\(/.test(PAD));
  const TTT = read("src/components/TicTacToeBoard.vue");
  const BLK = read("src/three/sortingBlocks.js");
  ok("B10 井字棋 O/X 走**形状**（2D 按符号分类 + 3D 顶面圆环/十字），色相之外还有一层编码",
     /v === ['"]O['"]/.test(TTT) && /v === ['"]X['"]/.test(TTT)
     && /圆环/.test(BLK) && /十字|交叉横杠/.test(BLK));
}

// ⑤ 键盘可达：沿用既有守卫（本脚本不重抄一遍断言，只钉它还活着）
{
  const pkg = read("package.json");
  ok("B11 键盘全可达由 verify_shortcuts.mjs 守着，且它仍在 npm run verify 链上",
     /node tools\/verify_shortcuts\.mjs/.test(pkg));
}

// ---------------------------------------------------------------- C 段：只报告，不假装
// 容器线 + base 模式边框的实测值**原样打印**（这些不参与判定，理由见文件头「诚实豁免」）。
console.log("\n  〔实测报告（不参与判定）〕");
for (const mode of ["base", "hiPerf"]) {
  const rows = [];
  for (const k of CS.SEMANTIC_KEYS) {
    if (k === "material" || SIGNAL_BORDERS[k] && mode === "hiPerf") continue;
    const e = CS.semanticOf(k, mode);
    const c = contrast(e.border, bgOf(e));
    rows.push(`${k}=${COLOR(c)}`);
  }
  console.log(`    ${mode.padEnd(7)} 边框/底（容器线档，豁免 3:1）：${rows.join("  ")}`);
}
console.log(`    hiPerf   层级底纹 gray(line)/gray(bg) = ${COLOR(contrast(CS.gray("line"), CS.gray("bg")))}:1`
  + ` · gray(dot)/gray(bg) = ${COLOR(contrast(CS.gray("dot"), CS.gray("bg")))}:1`);

// ---------------------------------------------------------------- D 段：在链上
// ★ 本轮教训：**不挂进 npm run verify 的守卫等于没有守卫**。
ok("D1 本脚本已并入 npm run verify 链（不挂链 = 没钉）",
   /node tools\/verify_a11y\.mjs/.test(read("package.json")));

console.log(`\n  可访问性守卫：${pass} 过 / ${fail} 败`
  + (fgFail.length ? `   （文字不达标：${fgFail.join(", ")}）` : ""));
process.exit(fail ? 1 : 0);
