<!-- =====================================================================
 ★★ C3「当前值 + 目标值 + 偏差 + 短趋势」四元组（方案 §C3，IEC 63303 显示要素）

 为什么是"一个组件"而不是各卡片各写一遍：§5.5 铁律④「同一屏同一结论必须同源」。
 速度卡片 / 就绪卡片 / 围栏 / J6 这几处过去各写各的"一个数"，于是出现
 "横幅说可以执行、按钮却是灰的"那一类分裂界面。四元组只在这里判一次：

   当前值（必须带**来源**标签）· 目标值（没有就明写"未设目标"）
   · 偏差（带符号 + 超容差才转 danger）· 60s 迷你趋势（样本 <2 一律不画，如实说没有）

 ★ 三条不许放宽的口径（都对应一条回归钉 tools/verify_param_quad.mjs）：
   ① 三态：`null` 显示「未知」，**不显示 0**、也不塌成"正常"（铁律③）；
   ② 无来源不显示：给了值但没给来源标签 → 该值**不上屏**（方案原话
      "不显示无法标注来源的数"）；
   ③ 趋势只读 `utils/paramTrend.js`（前端环形缓冲），本组件**不发请求、不写后端**。
==================================================================== -->
<template>
  <div class="pq" :class="semCls">
    <span class="pq-label">{{ label }}</span>

    <span class="pq-cell">
      <em>当前</em>
      <b :title="curSource || ''">{{ curText }}</b>
      <i v-if="curSource" class="pq-src">{{ curSource }}</i>
    </span>

    <span class="pq-cell">
      <em>目标</em>
      <b :title="tgtSource || ''">{{ tgtText }}</b>
      <i v-if="tgtSource" class="pq-src">{{ tgtSource }}</i>
    </span>

    <span class="pq-cell" :class="{ off: devBeyond }">
      <em>偏差</em><b>{{ devText }}</b>
    </span>

    <!-- 迷你趋势：样本不足 2 个**不画**（单点会被读成"这一分钟都很平"= 编故事） -->
    <svg v-if="points" class="pq-spark" :width="W" :height="H" aria-hidden="true">
      <polyline :points="points" fill="none" stroke="currentColor" stroke-width="1" />
    </svg>
    <span v-else class="pq-src">{{ trendNote }}</span>

    <span v-if="stale" class="pq-stale" title="最后成功采样的时刻见悬浮">数据已过期</span>
  </div>
</template>

<script setup>
import { computed } from "vue";
import { clsFor } from "../utils/colorSemantics.js";
import { sparkPoints, hasTrend, wrapAngleDev } from "../utils/paramTrend.js";

const props = defineProps({
  label: { type: String, default: "" },
  // 当前值 / 目标值：null = 未知（**不是 0**）
  current: { type: Number, default: null },
  target: { type: Number, default: null },
  // ★ 来源标签：没有来源就不许上屏（①的判据在 curText/tgtText 里）
  curSource: { type: String, default: "" },
  tgtSource: { type: String, default: "" },
  unit: { type: String, default: "" },
  decimals: { type: Number, default: 0 },
  // 偏差容差：|偏差| ≥ 它 → 转 danger 语义（不给容差 = 只报数不判色）
  warnDev: { type: Number, default: null },
  // ★ 环形偏差（转动轴用）：J6 整圈是**同一姿态**，直接相减会把"差 1°"显示成"差 359°"。
  //   取 ±180 内的最短角差 —— 与后端 `services/sorting.py::current_raw_slot`（J6 按环形差 ±360
  //   等价）同一口径；调用方不得自己再折一遍（两处算法必然漂移）。
  wrap: { type: Boolean, default: false },
  // 趋势（来自 paramTrend 的键）
  trendKey: { type: String, default: "" },
  trendWindowMs: { type: Number, default: 60000 },
  trendDomain: { type: Array, default: null },
  stale: { type: Boolean, default: false },
  // ★ 由父组件传"当下时刻"（App 的 1 秒时钟），组件自己调 Date.now() 不是响应式依赖
  //   —— 那样趋势线会停在渲染那一秒（和 A6 修过的"过期永远算不出来"是同一类 bug）。
  asOfNow: { type: Number, default: 0 },
  // 目标值为 null 时的文案：调用方语义不同（速度=未设目标 / 净空=没配阈值）
  targetNullText: { type: String, default: "未设目标" },
});

const W = 60, H = 16;
const nowOf = () => props.asOfNow || Date.now();

const fmt = (v) => {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return v.toFixed(props.decimals) + props.unit;
};
// ★ 无来源不显示（方案原话），未知显示「未知」，两者都不是 0
const curText = computed(() =>
  (props.curSource ? fmt(props.current) : null) ?? "未知");
const tgtText = computed(() =>
  (props.tgtSource ? fmt(props.target) : null) ?? props.targetNullText);
const dev = computed(() => {
  if (!Number.isFinite(props.current) || !Number.isFinite(props.target)) return null;
  // 环形（转动轴）走 utils 的**唯一**折圈实现；线性才是"当前 − 目标"
  return props.wrap ? wrapAngleDev(props.current - props.target)
    : props.current - props.target;
});
const devText = computed(() => {
  if (dev.value === null) return "—";
  const s = dev.value > 0 ? "+" : "";
  return s + dev.value.toFixed(props.decimals) + props.unit;
});
const devBeyond = computed(() =>
  dev.value !== null && Number.isFinite(props.warnDev) && Math.abs(dev.value) >= props.warnDev);

const points = computed(() => (props.trendKey
  ? sparkPoints(props.trendKey, W, H, {
      windowMs: props.trendWindowMs,
      domain: props.trendDomain || undefined,
      now: nowOf(),
    })
  : null));
const trendNote = computed(() => {
  if (!props.trendKey) return "";
  return hasTrend(props.trendKey, props.trendWindowMs, nowOf())
    ? "趋势采集中…" : "暂无趋势（样本不足）";
});

// 语义类走全站唯一颜色语义表（未知 → unknown 的虚线边 + 后缀，不另写一套灰）
const semCls = computed(() => clsFor(devBeyond.value ? "danger" : (curText.value === "未知" ? "unknown" : "normal")));
</script>

<style scoped>
.pq { display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 12px; line-height: 1.5; padding: 2px 6px; border-radius: 6px;
  border: 1px solid transparent; }
.pq-label { color: var(--txt2); }
.pq-cell { display: inline-flex; align-items: baseline; gap: 3px; }
.pq-cell em { font-style: normal; color: var(--muted); }
.pq-cell b { color: var(--txt); font-weight: 600; }
.pq-cell.off b { color: var(--err); }
.pq-src { font-style: normal; color: var(--muted); font-size: 11px; }
.pq-spark { color: var(--accent); vertical-align: middle; }
.pq-stale { color: var(--warn); font-size: 11px; border: 1px dashed var(--warn-line);
  border-radius: 4px; padding: 0 4px; }
</style>
