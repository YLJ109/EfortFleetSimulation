// =====================================================================
// 全站**唯一**颜色语义表 + 高性能画面模式（ISA-101 风格的工业 HMI 配色纪律）
//
// 方案：docs/方案-创新功能四批（…·高性能画面·摆放体检）.md §C1
//
// 立意（为什么不是"再调一版配色"）：
//   操作员要长期盯屏。花花绿绿会让**真正危险的信号失去突显** —— 当所有东西都在闪、
//   都在着色，"红"就不再意味着危险。所以这里做的是**一个非破坏式的模式开关**：
//     · 开启后**装饰性颜色退成灰阶**（按 CIE L* 分层，不是"同一个灰"）；
//     · 但**危险 / 预警 / 业务物料色必须更突出**（红保留 + 边框加粗 + 角标）；
//     · 「推断 / 未知」这类"**上位机在猜**"的状态拿到**冗余编码**
//       （虚线边框 + 斜纹填充 + 文字后缀），不再只靠色相差别 ——
//       色弱操作员 / 强光现场 / 远端投屏都分不清色相（AGENTS.md §5.5 铁律③④）。
//
// ─────────────────────────────────────────────────────────────────────
// ★★ 模式判定的**唯一一处**状态：本文件模块级的 `hiPerfRef`
//   · 读：只许调 `hiPerfMode()`（模板里也可直接绑 `hiPerfRef`，那是同一个 ref，不是副本）。
//   · 写：只许调 `setHiPerfMode(on)` / `toggleHiPerfMode()`。
//   · **另一处不许再判一遍**：不要再往 localStorage / Pinia / props 里各存一份布尔，
//     那就会出现"横幅按新模式着色、按钮还是旧模式"（§5.5 铁律④「同一屏同一结论必须同源」）。
//   · **默认关**（`hiPerfRef = false`）—— 不改变现有操作员的习惯；关掉时本表给出的
//     就是各界面**现在**已经在用的值（`var(--ok)` 等既有的 CSS 变量，同一个来源，
//     不复制色值），所以"接了表"≠"改了外观"。
//
// ─────────────────────────────────────────────────────────────────────
// ★ 根节点属性约定（开关已接线，见下面①；index.html 仍归别人，全局规则待接）：
//   `setHiPerfMode()` 在浏览器里把 `<html data-hmi="hi-perf">` 写上，关闭时写成
//   `data-hmi="normal"`（属性名/值 = 导出的 `HMI_ATTR` / `HMI_ATTR_ON` / `HMI_ATTR_OFF`；
//   用"改成另一个值"而不是"删掉"，是为了让属性始终如实反映当前模式）。
//   其它视图要在全站做灰阶，选择器写成：
//       html[data-hmi="hi-perf"] .xxx { … }
//   或由组件自己绑 `clsFor(semantic, mode)` 返回的 class（本次安全信号链就是这么做的：
//   `#safety-chip.hmi-hi-perf.sem-danger` 这类**只在 hiPerf 下存在**的选择器，
//   ⇒ 关掉模式时**一条样式都不会命中**，零回归）。
//
//   ⚠ 诚实声明：**全站灰阶并没有"开个关就全灰了"**。本文件只交付语义表 + 纯函数 +
//     安全信号链三处迁移（SafetyChip.vue / SafetyPanel.vue / safetyLabels.js）。
//     **已接线**（2026-10-12 集成轮接上，钉：verify_hmi_palette G8/G9）：
//       ① 界面开关：`stores/ui.js` 的 `hiPerf`（**只存这台浏览器的 localStorage，
//          不写后端配置**）→ `setHiPerfMode(on)`；`App.vue` 挂载时 `ui.applyHiPerf()`；
//          SettingsView.vue「界面模式」勾选框调 `ui.setHiPerf(on)`。**默认关**。
//          ★ 它只改显示，不参与任何安全判据（判据一个都没变）。
//       ⑤ `tools/verify_hmi_palette.mjs` 已并入 `npm run verify`（不挂链的守卫等于没钉）。
//     **仍待接线**（不归本文件改，接了才真生效）：
//       ② 顶部报警条 `#safety-banner`（App.vue + index.html 负责人）：判据仍是
//          safetyLabels 那一份，着色出口换成本表（danger/hit = danger 语义、
//          stalled = unknown 语义），选择器挂 `html[data-hmi="hi-perf"]`。
//       ③ 其余视图（监控/点位/分拣/AI/手柄…）的装饰彩底：各自按
//          `clsFor()/styleVarsFor()` 接，或在 index.html 补全局规则。
//       ④ 全局装饰动画：index.html 里的 `ctrl-settle`（入场）/ `h3-spin`（刷新转圈）
//          在 hiPerf 下应关掉 —— 口径就是 `decorationAllowed(animName, mode)`
//          （白名单只有 `hit`）；本任务改不到那两个 @keyframes 所在文件。
//
// ★ 物料色（七色分拣）**永不灰阶**：业务语义，灰了就把"红件"分不到红槽上。
//   本表**只引用**既有的 `SORT_COLOR_HEX`（`three/sortingBlocks.js`），
//   绝不复制第二份七色字面量（钉：tools/verify_hmi_palette.mjs B 段）。
// =====================================================================
import { ref } from "vue";
import { SORT_COLOR_HEX } from "../three/sortingBlocks.js";

/** 模式字面量（只有两个值，别造第三个）。 */
export const MODE_BASE = "base";
export const MODE_HI_PERF = "hiPerf";

/** 根节点属性约定（见文件头）：`<html data-hmi="hi-perf">`。 */
export const HMI_ATTR = "data-hmi";
export const HMI_ATTR_ON = "hi-perf";
export const HMI_ATTR_OFF = "normal";
/** 组件侧的模式 class（所有灰阶样式都挂它，关掉模式即一条都不命中）。 */
export const HMI_CLASS = "hmi-" + HMI_ATTR_ON;

// ---------------------------------------------------------------- 唯一一处模式状态
/** ★ 全站只有这一个 ref 表示"高性能画面模式开没开"；默认**关**。 */
export const hiPerfRef = ref(false);

/** 当前模式（`"base"` / `"hiPerf"`）。任何组件/函数都只通过它问模式。 */
export function hiPerfMode() {
  return hiPerfRef.value ? MODE_HI_PERF : MODE_BASE;
}

/** 把模式同步到根元素属性（非浏览器环境直接跳过：headless 回归也要能 import 本文件）。 */
function syncHmiAttribute() {
  if (typeof document === "undefined" || !document.documentElement) return;
  if (hiPerfRef.value) document.documentElement.setAttribute(HMI_ATTR, HMI_ATTR_ON);
  else document.documentElement.setAttribute(HMI_ATTR, HMI_ATTR_OFF);
}

/** 唯一写入者。返回写入后的模式。 */
export function setHiPerfMode(on) {
  hiPerfRef.value = !!on;          // ★ 三态不塌：调用方给的是布尔闸门，这里只认 `!!`
  syncHmiAttribute();
  return hiPerfMode();
}

export function toggleHiPerfMode() {
  return setHiPerfMode(!hiPerfRef.value);
}

// ---------------------------------------------------------------- 灰阶（L* 分层）
/**
 * CIE L* → 中性灰十六进制（灰的 sRGB 三通道相等 ⇒ Y 就是线性值，公式精确不是近似）。
 * 为什么要按 L* 而不是"随手取几个灰"：操作员要能看出**层级**，
 * 而 sRGB 数值等差在感知上并不等差（暗端挤成一团）。
 */
export function lstarToHex(L) {
  const l = Number.isFinite(L) ? Math.min(100, Math.max(0, L)) : 0;
  const y = l > 8 ? Math.pow((l + 16) / 116, 3) : l / 903.3;
  const v = y > 0.0031308 ? 1.055 * Math.pow(y, 1 / 2.4) - 0.055 : 12.92 * y;
  const n = Math.round(Math.min(1, Math.max(0, v)) * 255);
  const h = n.toString(16).padStart(2, "0");
  return "#" + h + h + h;
}

/** 灰阶层级（L*）：底 → 线 → 点 → 文字 → 高亮文字。**必须是分层的，不是同一个灰。** */
export const GRAY_TIERS = { bg: 12, line: 32, dot: 55, fg: 82, hi: 92 };

/** 取某一档灰（传层级名或任意 L* 数）。 */
export function gray(tier) {
  const v = GRAY_TIERS[tier];
  return lstarToHex(Number.isFinite(v) ? v : tier);
}

/** 斜纹填充（「推断 / 未知」的形状冗余之一）：45° 细纹，两种模式共用同一支实现。 */
export function hatchFill(lineColor, bandPx = 2, gapPx = 6) {
  return `repeating-linear-gradient(45deg, ${lineColor} 0 ${bandPx}px, `
       + `transparent ${bandPx}px ${bandPx + gapPx}px)`;
}

// ---------------------------------------------------------------- 语义键
/** 七个语义键（顺序固定；钉：verify_hmi_palette A 段）。 */
export const SEMANTIC_KEYS = Object.freeze([
  "normal", "warn", "danger", "target", "inferred", "unknown", "material",
]);

/** 「推断 / 未知」的文字后缀词表（**只给词，不看模式**；要不要渲染由调用方按模式决定）。 */
export const SUFFIX_LABEL = { inferred: "（流程推断）", unknown: "（未知）" };

/**
 * 物料色：两种模式**完全同一个对象引用** —— 业务语义，不参与灰阶（文件头有理由）。
 * `colorOf()` 从 `SORT_COLOR_HEX` 现推，本表里没有任何七色字面量。
 */
const MATERIAL = Object.freeze({
  kind: "material",
  grayable: false,                    // ★ 装饰性灰阶对物料色不适用
  ref: "SORT_COLOR_HEX",              // 唯一来源的名字（写进表里，便于回归核对）
  byColor: SORT_COLOR_HEX,            // **引用**，不是拷贝
  colorOf: (name) => {
    const v = SORT_COLOR_HEX[name];
    return Number.isFinite(v) ? "#" + v.toString(16).padStart(6, "0") : "";
  },
  mark: "■",
  suffix: "",
  fg: "", dot: "", border: "",
});

/** 一格的样式属性集（缺省字段由 styleVarsFor 兜默认值）。 */
const ENTRY = {
  fg: "", dot: "", glow: "", bg: "", bgImage: "none",
  border: "", borderWidth: "1px", borderStyle: "solid",
  mark: "", suffix: "", hatch: false, blink: false,
};
const mk = (o) => Object.freeze({ ...ENTRY, ...o });

// 说明：`base` 一侧全部指向**既有 CSS 变量**（`var(--ok)` / `var(--warn)` / `var(--err)` …），
// 与 index.html 现在实际用的就是同一个值 —— 这就是"同一屏同一结论同源"，
// 也保证"把字面量换成表里取值"不改变任何现有外观。
export const COLOR_SEMANTICS = Object.freeze({
  /** 正常：常规 = 各视图彩底；hiPerf = 灰阶分层。（不给角标 —— 正常态不该抢注意力） */
  normal: Object.freeze({
    base: mk({ fg: "var(--ok)", dot: "var(--ok)", glow: "0 0 7px var(--ok)",
      border: "var(--ok-line)" }),
    hiPerf: mk({ fg: gray("fg"), dot: gray("dot"), glow: "none",
      bg: gray("bg"), border: gray("line") }),
  }),
  /** 接近 / 预警：黄在两种模式下都**保留**（预警不是装饰，是信号）。 */
  warn: Object.freeze({
    base: mk({ fg: "var(--warn)", dot: "var(--warn)", glow: "0 0 7px var(--warn)",
      border: "var(--warn-line)", mark: "△" }),
    hiPerf: mk({ fg: "var(--warn)", dot: "var(--warn)", glow: "none",
      bg: "var(--warn-soft)", border: "var(--warn-line)", mark: "△" }),
  }),
  /** 危险 / 碰撞：红保留，hiPerf 再加**非颜色**的冗余（更粗实框 + 角标 ▲）。 */
  danger: Object.freeze({
    base: mk({ fg: "var(--err)", dot: "var(--err)", glow: "0 0 7px var(--err)",
      border: "var(--err-line)", mark: "▲" }),
    hiPerf: mk({ fg: "var(--err)", dot: "var(--err)", glow: "none",
      bg: "var(--err-soft)", border: "var(--err)", borderWidth: "2px", borderStyle: "solid",
      mark: "▲" }),
  }),
  /** 目标 / 设定值：蓝（数字孪生的"要去哪"）。设定值不参与"退彩"。 */
  target: Object.freeze({
    base: mk({ fg: "var(--accent)", dot: "var(--accent2)", glow: "0 0 6px var(--accent2)",
      border: "var(--accent-line)", mark: "◆" }),
    hiPerf: mk({ fg: "var(--accent)", dot: "var(--accent2)", glow: "none",
      bg: "var(--accent-soft)", border: "var(--accent-line)", mark: "◆" }),
  }),
  /** 推断：上位机按流程猜的（残影预演、手上有没有件…）。hiPerf = 虚线 + 斜纹 + 后缀。 */
  inferred: Object.freeze({
    base: mk({ fg: "var(--muted)", dot: "var(--muted)", border: "var(--glass-line)" }),
    // ★ 边框用 dot 档（L*55）而不是 line 档（L*32）：虚线本来就比实线难看见，
    //   1.89:1 的层级线在强光屏/远端投屏上等于"没有冗余编码" —— 而这层虚线是
    //   A6/E5′「不知道就说不知道」在画面上的**唯一可见载体**。钉 verify_a11y.mjs B 段。
    hiPerf: mk({ fg: gray("hi"), dot: gray("dot"), border: gray("dot"),
      borderWidth: "2px", borderStyle: "dashed", bg: gray("bg"),
      bgImage: hatchFill(gray("line")), hatch: true, mark: "◌",
      suffix: SUFFIX_LABEL.inferred }),
  }),
  /** 未知：拿不到 / 不可信（A6 新鲜度三态里的 never/stale/stopped）。 */
  unknown: Object.freeze({
    // base = 现状：状态片/面板本来就有一条虚线边（A6 修的就是这个），不写后缀、不斜纹。
    base: mk({ fg: "var(--muted)", dot: "transparent", border: "var(--muted)",
      borderStyle: "dashed" }),
    hiPerf: mk({ fg: gray("hi"), dot: "transparent", border: gray("dot"),   // ★ 同上（dot 档）
      borderWidth: "2px", borderStyle: "dashed", bg: gray("bg"),
      bgImage: hatchFill(gray("line")), hatch: true, mark: "?",
      suffix: SUFFIX_LABEL.unknown }),
  }),
  /** 物料颜色（七色分拣）：永不灰阶。 */
  material: Object.freeze({ base: MATERIAL, hiPerf: MATERIAL }),
});

// ---------------------------------------------------------------- 纯函数
function normMode(mode) {
  return mode === MODE_HI_PERF ? MODE_HI_PERF : MODE_BASE;
}

/** 语义键兜底：认不得的键 → `unknown`（**不许塌成 normal** —— 那正是"把不知道说成没事"）。 */
function normSemantic(key) {
  return COLOR_SEMANTICS[key] ? key : "unknown";
}

/** 某模式下的整套语义 → 样式（返回 `{ 语义键: 样式对象 }`）。 */
export function semantics(mode) {
  const m = normMode(mode);
  const out = {};
  for (const k of SEMANTIC_KEYS) out[k] = COLOR_SEMANTICS[k][m];
  return out;
}

/** 单个语义在某个模式下的样式对象。 */
export function semanticOf(semantic, mode) {
  return COLOR_SEMANTICS[normSemantic(semantic)][normMode(mode)];
}

/** 该语义有没有斜纹填充（「推断 / 未知」的形状冗余之一）。 */
export function isHatched(semantic, mode) {
  return semanticOf(semantic, mode).hatch === true;
}

/** 物料色取色（唯一的物料色出口；灰不灰阶都走它）。 */
export function materialColor(name) {
  return MATERIAL.colorOf(name);
}

/**
 * 组件用的 class 名（空格分隔的字符串，Vue 的 `:class` 直接吃）。
 *   hiPerf 多带一个 `hmi-hi-perf` —— 所有灰阶样式都挂在这个类底下，
 *   ⇒ 模式关掉时这些选择器一条都不命中（非破坏式的实现手段）。
 */
export function clsFor(semantic, mode) {
  const cls = ["sem-" + normSemantic(semantic)];
  if (normMode(mode) === MODE_HI_PERF) cls.push(HMI_CLASS);
  return cls.join(" ");
}

/** 组件用的 CSS 自定义属性（配 `:style`；base 模式请传 `{}`，见各组件的 gate）。 */
export function styleVarsFor(semantic, mode) {
  const e = semanticOf(semantic, mode);
  return {
    "--hmi-fg": e.fg || "inherit",
    "--hmi-dot": e.dot || "transparent",
    "--hmi-dot-glow": e.glow || "none",
    "--hmi-bg": e.bg || "transparent",
    "--hmi-bg-image": e.bgImage || "none",
    "--hmi-border": e.border || "currentColor",
    "--hmi-border-width": e.borderWidth,
    "--hmi-border-style": e.borderStyle,
  };
}

/** 文字后缀（`inferred` → 「（流程推断）」、`unknown` → 「（未知）」，其余空串）。不看模式。 */
export function labelSuffix(semantic) {
  return SUFFIX_LABEL[semantic] || "";
}

/** 角标字形（hiPerf 下 danger 的形状冗余）。`material` 也走同一支（■）。 */
export function markOf(semantic, mode) {
  return semanticOf(semantic, mode).mark;
}

/**
 * 装饰动画白名单。**只有 `hit`（碰撞闪烁）在 hiPerf 下保留**，
 * 其余装饰（渐变 / 浮沉 / 呼吸 / 转圈 / 入场）在该模式一律关闭。
 * ★ fail-safe：认不得的动画名 → hiPerf 下 false（宁可少闪，不可乱闪）。
 */
const ANIM_WHITELIST = ["hit"];
export function decorationAllowed(animName, mode) {
  if (normMode(mode) === MODE_BASE) return true;      // 常规模式不改现有行为
  return ANIM_WHITELIST.includes(animName);
}

/** 只有物料色不许灰阶；其余语义在 hiPerf 下都允许退成灰阶。 */
export function isGrayable(semantic) {
  return semanticOf(semantic, MODE_HI_PERF).grayable !== false
    && semantic !== "material";
}
