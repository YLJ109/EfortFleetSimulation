<script setup>
// 安全围栏常驻状态片 —— 浮在 **3D 视口右下角**。
//
// ★ 位置为什么在 .view 里（由 MonitorLayout 渲染）而不是全局底栏：
//   用户要求"放在画面右下角，不要在底部"。放进视口容器后它会自动跟随
//   右栏的拖宽/折叠与窄屏堆叠，不需要任何 JS 去算右栏宽度。
//   代价：设置/关于/运维审计这些**没有 3D 视口**的数据页看不到状态片。
//
// ★ 数据来源：safety store 的 last（由 App.vue 的 onSafety 回调喂进来）。
//   不自己起定时器、不自己算围栏 —— 全站只有一份评估结果。
//   文案统一取 utils/safetyLabels.js，与顶部报警条共用同一套判据。
//
// ★ C1 高性能画面模式：本片**只上色到语义**，不写颜色字面量。
//   分级判定（safe/warn/danger/hit + A6 三态健康）**一个字节都没搬走**：
//     · 判据 = store.health（三态）+ safetyLabels 的 STATE_SEMANTIC 映射；
//     · 颜色 = utils/colorSemantics.js 的语义表（normal / warn / danger / unknown / inferred）；
//     · 模式关（默认）→ 语义键照常挂到 class 上，但 hiPerf 的灰阶与冗余样式**一条都不命中**，
//       外观与本片过去完全一致；
//     · 模式开 → 装饰彩底退成灰阶、危险保留红并**加粗边框 + ▲ 角标**、
//       「未知（评估停摆）」与「推断（残影预演）」拿到虚线 + 斜纹 + 文字后缀
//       （不只靠颜色 —— 色弱/强光/投屏都分不出色相）。
//   ★ 闪烁只认白名单：`decorationAllowed("hit", mode)` —— 碰撞的闪是信号，其余装饰退掉。
import { computed } from "vue";
import { useSafetyStore } from "../stores/safety.js";
import {
  safetyLabel, safetyScope, safetySub, chipTitle, evalHealth,
  safetySemantic, healthSemantic, isInferredSource,
} from "../utils/safetyLabels.js";
import {
  hiPerfMode, clsFor, styleVarsFor, labelSuffix, markOf, decorationAllowed, MODE_HI_PERF,
} from "../utils/colorSemantics.js";

const safe = useSafetyStore();

// 配置里可以整个关掉状态片（alarm.chip === false），与后端 schema 对齐。
const show = computed(() => !safe.config || !safe.config.alarm || safe.config.alarm.chip !== false);
const st = computed(() => safe.last || { state: "safe" });

// ★★ A6：评估不新鲜时**不许**继续显示上一帧的"安全"。
//   状态片改灰、话术改成"评估已停止 · 数据不可信"—— 判据来自 store 的三态读数
//   （由 App 的 1s 外部时钟刷新），不是"看起来还有数"就当有数。
const health = computed(() => safe.health || evalHealth({ ageMs: null }));
const stalled = computed(() => !health.value.ok);
const scope = computed(() => (stalled.value ? "安全评估" : safetyScope(st.value)));
const label = computed(() => (stalled.value ? health.value.label : safetyLabel(st.value)));
const num = computed(() => (stalled.value ? "数据不可信" : safetySub(st.value)));
const title = computed(() => (stalled.value
  ? `${health.value.notice} · ${health.value.title}` : chipTitle(st.value)));

// ---------------- C1：语义 → class / CSS 变量（判据不动，只换着色出口）----------------
const mode = computed(() => hiPerfMode());
const hiPerf = computed(() => mode.value === MODE_HI_PERF);

/**
 * 本片的语义键。优先级：**先"这个数还可信吗"，再"余量剩多少"**。
 *   评估不新鲜 → `unknown`（A6 三态里 never/stale/stopped 都不是"安全"，绝不塌成布尔）；
 *   否则按围栏分级 → `normal` / `warn` / `danger`（hit 归 danger，见 safetyLabels）。
 */
const sem = computed(() => (stalled.value ? healthSemantic(health.value) : safetySemantic(st.value)));
const cls = computed(() => [
  stalled.value ? "stalled" : st.value.state,
  clsFor(sem.value, mode.value),
].join(" "));
const vars = computed(() => styleVarsFor(sem.value, mode.value));
// 角标只在 hiPerf 下出现（关闭时本片外观零变化）；danger 的 ▲ 就是"不只靠颜色"的形状冗余。
const mark = computed(() => (hiPerf.value ? markOf(sem.value, mode.value) : ""));
const suffix = computed(() => (hiPerf.value ? labelSuffix(sem.value) : ""));
// 残影 = 量的是预演目标位姿（上位机在猜）→ 「推断」的冗余编码挂在 .sc-src 那一小块上。
const ghostInferred = computed(() => !stalled.value && isInferredSource(st.value));
const ghostCls = computed(() => clsFor("inferred", mode.value));
const ghostVars = computed(() => styleVarsFor("inferred", mode.value));
const ghostSuffix = computed(() => (hiPerf.value ? labelSuffix("inferred") : ""));
// 碰撞闪烁：唯一进白名单的装饰动画。
const blinkName = computed(() => (
  st.value.state === "hit" && decorationAllowed("hit", mode.value) ? "sa-blink" : "none"));
</script>

<template>
  <div v-if="show" id="safety-chip" :class="cls" :style="vars" :title="title">
    <span v-if="mark" class="sc-mark" aria-hidden="true">{{ mark }}</span>
    <span class="sc-dot" :style="{ animationName: blinkName }"></span>
    <span
      v-if="ghostInferred"
      class="sc-src"
      :class="ghostCls"
      :style="ghostVars"
      title="这一路的余量按残影预演的目标位姿算，不是实体机实测"
    >残影{{ ghostSuffix }}</span>
    <span>{{ scope }} · {{ label }}</span>
    <span class="sc-num">{{ num }}{{ suffix }}</span>
  </div>
</template>

<style scoped>
/* =====================================================================
   C1 高性能画面模式的**覆盖层**：全部选择器都带 .hmi-hi-perf，
   该类只在模式开启时由 clsFor() 挂上 ⇒ 默认（关）以下规则**一条都不命中**，
   本片行为与颜色与改动前完全一致（非破坏式）。
   ★ 这里**不出现任何颜色字面量** —— 值全部来自 styleVarsFor() 的 CSS 变量
     （唯一出处 = utils/colorSemantics.js）。
   ===================================================================== */
#safety-chip.hmi-hi-perf {
  color: var(--hmi-fg);
  border-width: var(--hmi-border-width);
  border-style: var(--hmi-border-style);
  border-color: var(--hmi-border);
  background-color: var(--hmi-bg);
  /* 斜纹填充：只有「推断 / 未知」拿得到（danger/warn/normal 这里是 none） */
  background-image: var(--hmi-bg-image);
}
#safety-chip.hmi-hi-perf .sc-dot {
  background: var(--hmi-dot);
  box-shadow: var(--hmi-dot-glow);
}
#safety-chip.hmi-hi-perf .sc-num { color: var(--hmi-fg); }
#safety-chip.hmi-hi-perf .sc-mark {
  font-size: 11px; line-height: 1; color: var(--hmi-fg);
}
/* ★ 冗余编码**全部由表给出**，样式层不重复判一遍（这里再写一遍就是第二套判据）：
     danger  = 2px 实框（--hmi-border-width）+ ▲ 角标（.sc-mark）+ 红保留；
     unknown = 虚线 + 斜纹 + 「（未知）」后缀；inferred = 同一套 + 「（流程推断）」。
   「推断」的冗余只挂在 .sc-src 那一小块上（严重度仍是整片的色），
   这样"残影预演的读数"与"实体机实测的读数"形态上分得开，而危险仍然是危险。 */
#safety-chip.hmi-hi-perf .sc-src {
  border-width: var(--hmi-border-width);
  border-style: var(--hmi-border-style);
  border-color: var(--hmi-border);
  color: var(--hmi-fg);
  background-image: var(--hmi-bg-image);
}
</style>
