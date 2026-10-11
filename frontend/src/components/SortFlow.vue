<script setup>
// =====================================================================
// 执行流程可视化（分拣页右栏）
//
// ★ 用户口径（2026-10-09）：
//   "右边写进去执行流程吧，for 循环形式那种，可视化简单可以感受流程的那种；
//    截取图片时显示截取的图片颜色 —— 开始 / A1上方 / A1原位 / 吸取 / 等待0.5秒 /
//    A1上方 / C位 / 等待检测一秒 / 捕捉3张（只显示一张置信度最高的）/
//    B（按颜色）上方 / 停止吸气 / 放气0.5秒 / 停止放气 / 来到起点"
//
// 设计原则（三条，缺一不可）：
//   ① **只映射真实 stage，不编造步骤** —— 步骤表里每一项都对应后端 `state.stage`
//      的某个真实取值（见 STAGE_MAP）。流程条要能当"现场对着看机器人走到哪"的依据，
//      一旦里面写了后端不做的动作，它就从"依据"变成"误导"。
//   ② **循环体要看得见** —— for 循环那一段用竖线+缩进圈起来，标注 `for i = 1..N`，
//      让人一眼看出"这 12 步是每一件都要重来一遍的"。
//   ③ **抓帧如实展示** —— 判色步骤把 3 帧的颜色+置信度**都列出来**，
//      只把置信度最高的那张标为「主」（用户："只显示一张置信度最高的"）。
//      判色失败时也要指出"最像的那一张"，否则现场只看到"检测不到"四个字。
//
// 与后端 stage 的对应关系一旦改了后端文案就会失联 —— 有回归钉子守着
// （tools/verify_sortflow.mjs 会断言 STAGE_MAP 覆盖后端全部 stage 字面量）。
// =====================================================================
import { computed } from "vue";
import Icon from "./Icon.vue";
import { LOOP_STEPS, PRE_STEP, POST_STEP, STEP_ORDER, stepOf }
  from "../sorting/flowSteps.js";

const props = defineProps({
  /** `/api/sorting/state` 的 state（含 stage / piece / color / frame_detail / best_frame）。 */
  st: { type: Object, default: null },
  /** 本轮实际参数（用于把"等待xx毫秒"写成真实数字，而不是写死）。 */
  frames: { type: Number, default: 3 },
  dwellMs: { type: Number, default: 500 },
  blowMs: { type: Number, default: 500 },
  suckHoldMs: { type: Number, default: 500 },
});

const s = computed(() => props.st || {});

// ---------------------------------------------------------------- 步骤表
// ★ 步骤与 stage 映射全部来自 src/sorting/flowSteps.js（纯逻辑模块）——
//   放组件里就没法 headless 驱动"真实 stage 序列"跑回归，而这正是最容易错的地方。
//   组件这里只负责：渲染 + 高亮 + 把参数填进文案。
/** 当前所处的步骤 key（映射逻辑见 sorting/flowSteps.js，已由回归脚本用真实 stage 序列驱动）。 */
const curStep = computed(() => stepOf(s.value.stage));

// ---------------------------------------------------------------- 展示派生
const running = computed(() => !!s.value.running);
const piece = computed(() => Number(s.value.piece) || 0);
const total = computed(() => Number(s.value.total) || props.frames || 9);
const ended = computed(() => !!s.value.finished);
const stopReason = computed(() => String(s.value.error || ""));
const target = computed(() => String(s.value.target || ""));

/** 参数对象（供步骤 label/note 取真实数字）。 */
const P = computed(() => ({
  i: piece.value || 1,
  frames: props.frames,
  dwell: props.dwellMs,
  blow: props.blowMs,
  suckHold: props.suckHoldMs,
}));

/** 步骤是否"已完成"（当前步之前的都算走过）。 */
function stepState(step) {
  const cur = curStep.value;
  const ci = STEP_ORDER.indexOf(cur);
  const si = STEP_ORDER.indexOf(step.key);
  if (ci < 0) return "";
  if (si === ci) return "on";
  return si < ci ? "done" : "";
}

// ---- 抓帧明细（判色步骤里显示） ----
const frameList = computed(() => (Array.isArray(s.value.frame_detail) ? s.value.frame_detail : []));
// ★ 第 4 色「黑」= 无效颜色（识别到了但不分拣，原路放回原位）—— 也要有颜色映射，
//   否则界面上它会掉成灰、看着像"没识别到"。
const COLOR_HEX = { "红": "#E02020", "绿": "#1FA74A", "蓝": "#1F4FD8", "黑": "#1c1f24" };
/** 置信度最高的那一帧下标（后端给了 best_frame 就用它；判色失败时自己找最像的）。 */
const bestIdx = computed(() => {
  const bf = s.value.best_frame;
  if (bf !== null && bf !== undefined && Number.isFinite(Number(bf))) return Number(bf);
  let bi = -1, bc = -1;
  frameList.value.forEach((f, i) => {
    const c = Number(f && f.conf) || 0;
    if (c > bc) { bc = c; bi = i; }
  });
  return bi;
});
const verdict = computed(() => {
  const c = String(s.value.color || "");
  if (!c) return null;
  return { color: c, conf: Number(s.value.conf) || 0, hex: COLOR_HEX[c] || "#8a8f95" };
});

/** 判色步骤是否正在跑 / 已经跑过（决定要不要把帧格显示出来）。 */
const showFrames = computed(() => frameList.value.length > 0 && curStep.value !== "");
</script>

<template>
  <div class="card flow">
    <h3>
      <Icon name="activity" :size="15" /> 执行流程
      <span class="h3-sub" v-if="piece">第 {{ piece }}/{{ total }} 件</span>
      <span class="h3-sub" v-else-if="running">准备中…</span>
      <span class="h3-sub" v-else>未运行</span>
    </h3>

    <p class="small muted">
      盒子里的 12 步是 <b>for 循环体</b> —— 每一件都从头走一遍，走完最后一步（回到起点）再进下一件。
      <b>高亮的方块</b>就是机器人当前所在的那一步。
    </p>

    <!-- 循环外：开始 -->
    <div class="step" :class="stepState(PRE_STEP)">
      <span class="dot"></span>
      <span class="idx">·</span>
      <span class="lbl">{{ PRE_STEP.label }}</span>
    </div>

    <!-- 循环体 -->
    <div class="loop" :class="{ live: curStep && curStep !== 'start' && curStep !== 'end' }">
      <div class="loop-head">
        <Icon name="refresh" :size="11" />
        for 第 <b>i</b> 件（i = 1 … {{ total }}）
      </div>

      <div v-for="(stp, k) in LOOP_STEPS" :key="stp.key" class="step" :class="stepState(stp)">
        <span class="dot"></span>
        <span class="idx">{{ k + 1 }}</span>
        <span class="lbl">
          {{ typeof stp.label === "function" ? stp.label(P) : stp.label }}
          <span v-if="stp.note" class="note">{{ stp.note(P) }}</span>
          <!-- 放料步骤顺带把"按颜色选中的那个槽"写出来，现场一眼能对上 -->
          <span v-if="stp.key === 'b_up' && target" class="pick">
            <b :style="{ color: COLOR_HEX[verdict && verdict.color] || 'var(--muted)' }">
              {{ verdict ? verdict.color : "" }}
            </b> → {{ target }}
          </span>
        </span>
      </div>

      <!-- 抓帧明细：只挂在"捕捉判色"那一步下面 -->
      <div v-if="showFrames" class="frames">
        <div class="frames-head">
          <span>共捕捉 {{ frameList.length }} 张，按置信度取最高的一张为准</span>
          <span v-if="verdict" class="verdict">
            结论 <b :style="{ color: verdict.hex }">{{ verdict.color }}</b>
            <span class="conf">{{ verdict.conf.toFixed(2) }}</span>
          </span>
          <span v-else class="verdict bad">无有效判定</span>
        </div>
        <div class="frame-row">
          <div v-for="(f, i) in frameList" :key="i"
               class="frame" :class="{ main: i === bestIdx, bad: !f.ok }"
               :title="f.ok ? ('第 ' + (i + 1) + ' 张：' + f.color + ' ' + f.conf)
                            : ('第 ' + (i + 1) + ' 张未判出：' + (f.reason || '未知'))">
            <span class="no">{{ i + 1 }}</span>
            <span class="swatch" :style="{ background: COLOR_HEX[f.color] || '#4a5058' }"></span>
            <span class="col">{{ f.color || "—" }}</span>
            <span class="cf">{{ Number(f.conf || 0).toFixed(2) }}</span>
            <span v-if="i === bestIdx" class="crown">主</span>
          </div>
        </div>
        <p v-if="!s.judge" class="small muted" style="margin-top:4px">
          <!-- ★ S3：此处只说明"帧是占位色"；"本轮不判色/空跑或双闸未开"的完整口径在分拣页
               「运行状态」卡那一处讲（不重复第三、第四遍）。 -->
          帧为<b>占位色</b>（本轮未判色），不代表真的看到。
        </p>
      </div>
    </div>

    <!-- 循环外：结束 -->
    <div class="step" :class="stepState(POST_STEP)">
      <span class="dot"></span>
      <span class="idx">·</span>
      <span class="lbl">{{ POST_STEP.label }}</span>
    </div>

    <p v-if="ended && stopReason" class="small err" style="margin-top:6px">
      <Icon name="alert" :size="12" /> 本轮结束原因：{{ stopReason }}
    </p>
  </div>
</template>

<style scoped>
.card { border: 1px solid var(--line); border-radius: 10px; padding: 10px;
  background: var(--panel); }
.card h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 6px;
  font-size: 14px; color: var(--txt); }
.h3-sub { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: auto; }
.small { font-size: 12px; line-height: 1.6; margin: 4px 0 0; }
.muted { color: var(--muted); }
.err { color: var(--err, #e05252); }

/* ---- 循环体：左侧竖线 + 缩进，一眼看出"这 12 步要重来" ---- */
.loop { position: relative; margin: 4px 0 4px 10px; padding: 2px 0 2px 10px;
  border-left: 2px solid var(--line); border-radius: 2px; }
.loop.live { border-left-color: var(--accent); }
.loop-head { display: flex; align-items: center; gap: 5px; font-size: 11px;
  color: var(--muted); margin: 2px 0 4px; }
.loop-head b { color: var(--accent); }

/* ---- 单个步骤 ---- */
.step { display: flex; align-items: center; gap: 7px; padding: 3px 7px;
  border-radius: 6px; font-size: 12.5px; color: var(--muted);
  border: 1px solid transparent; transition: background .15s, color .15s, border-color .15s; }
.step .idx { width: 15px; text-align: right; font-size: 10.5px; color: var(--muted);
  flex: none; font-variant-numeric: tabular-nums; }
.step .lbl { flex: 1; min-width: 0; }
.step .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--line);
  flex: none; }

/* 已走过：打勾感的实心点 + 正常字色 */
.step.done { color: var(--txt); }
.step.done .dot { background: var(--ok, #2f9e6f); }

/* ★ 当前步：高亮盒子 + 呼吸动画（用户要的"动画高亮形式"） */
.step.on { color: var(--txt); font-weight: 600;
  background: rgba(79, 168, 255, .14); border-color: rgba(79, 168, 255, .55);
  animation: pulse 1.6s ease-in-out infinite; }
.step.on .dot { background: var(--accent); animation: blink 1.2s ease-in-out infinite; }
@keyframes pulse {
  0%, 100% { background: rgba(79, 168, 255, .10); }
  50%      { background: rgba(79, 168, 255, .22); }
}
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }

.note { margin-left: 6px; font-size: 11px; color: var(--muted); font-weight: 400; }
.pick { margin-left: 6px; font-size: 11px; color: var(--muted); font-weight: 400; }

/* ---- 抓帧明细 ---- */
.frames { margin: 5px 0 3px 22px; padding: 6px 7px; border-radius: 7px;
  background: rgba(255, 255, 255, .03); border: 1px solid var(--line); }
.frames-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 11px; color: var(--muted); }
.frames-head .verdict { margin-left: auto; color: var(--txt); }
.frames-head .verdict .conf { margin-left: 4px; color: var(--muted);
  font-variant-numeric: tabular-nums; }
.frames-head .verdict.bad { color: var(--err, #e05252); }
.frame-row { display: flex; gap: 5px; flex-wrap: wrap; }
.frame { display: flex; align-items: center; gap: 4px; padding: 3px 6px;
  border-radius: 6px; border: 1px solid var(--line); font-size: 11.5px;
  color: var(--muted); background: rgba(0, 0, 0, .12); position: relative; }
.frame .no { font-size: 10px; opacity: .7; }
.frame .swatch { width: 11px; height: 11px; border-radius: 3px;
  border: 1px solid rgba(255, 255, 255, .28); flex: none; }
.frame .cf { font-variant-numeric: tabular-nums; }
/* ★ "只显示一张置信度最高的"：最高那张加亮边框 + 主标记 + 稍大 */
.frame.main { border-color: var(--accent); background: rgba(79, 168, 255, .16);
  color: var(--txt); font-weight: 600; transform: scale(1.06); }
.frame.main .crown { position: absolute; top: -7px; right: -5px; font-size: 9px;
  background: var(--accent); color: #fff; border-radius: 4px; padding: 0 3px;
  line-height: 13px; }
.frame.bad { opacity: .55; }
.frame.bad .swatch { background: repeating-linear-gradient(45deg,
  #4a5058 0 3px, #33383f 3px 6px) !important; }
</style>
