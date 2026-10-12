<script setup>
// =====================================================================
// 手柄执行页 —— 纯手柄操作：2D 图上选点 → 手柄按一下真机移动 / 吸放
//
// 方案：docs/方案-手柄执行页（2D选点+气路+井字棋）.md
//
//   左/中：3D 视口（沿用 MonitorLayout，与其它页共用同一 scene/canvas）
//   浮层 ：2D 执行图（pad/PadMap2D.vue）—— 手柄 ↑↓←→ 在这里选点
//   右栏 ：顶部 tab「执行 / 井字棋」
//          · 执行  = 手柄状态 + 按键图例 + 手柄测试（含模拟键，等于鼠标兜底）
//          · 井字棋 = 现有 TicTacToeCard 整体搬入（本页**唯一**入口）
//
// ★★ 手柄归属（防"一次按键两个动作"）：
//   本页自己的执行逻辑只在 `tab === 'exec'` 时响应；
//   `TicTacToeCard` / `TicTacToeBoard` 只在 `pad.tab === 'ttt'` 时响应（见各自组件内的守卫）。
//   ⇒ 同一时刻只有一个消费者，判据互不重叠。
//
// ★★ 安全：Start = 真机动作总闸（默认落锁）。**离开本页 / 切到井字棋 tab 一律落锁**，
//   免得"人去看棋、手柄还 armed"——那是最容易误碰出事的状态。
// =====================================================================
import { computed, onBeforeUnmount, onMounted, watch } from "vue";
import MonitorLayout from "./MonitorLayout.vue";
import CameraPanel from "./CameraPanel.vue";
import Icon from "./Icon.vue";
import PadMap2D from "../pad/PadMap2D.vue";
import TicTacToeBoard from "./TicTacToeBoard.vue";
import TicTacToeCard from "./TicTacToeCard.vue";
import GamepadTestCard from "./GamepadTestCard.vue";
import { usePadStore, PAD_TAB_EXEC, PAD_TAB_TTT } from "../stores/pad.js";
import { useTicTacToeStore } from "../stores/tictactoe.js";
import { useRobotStore } from "../stores/robot.js";
import { useExecStore } from "../stores/exec.js";
import { useAuthStore } from "../stores/auth.js";
import { useRcReadyStore, NOT_READY_HINT } from "../stores/rcReady.js";
import { subscribeGamepad } from "../games/gamepad.js";

const pad = usePadStore();
const ttt = useTicTacToeStore();
const robot = useRobotStore();
const exec = useExecStore();
const auth = useAuthStore();
// ★★ 2026-10-12：手柄 A/Y/X/B/LB/RB 的**真机动作**需完整就绪（未就绪由 pad._gate 拒绝）——
//   这里只负责**如实告知**原因，别让操作员以为"手柄坏了"。
const rc = useRcReadyStore();

/** 2D 执行图只在「执行」tab 显示；井字棋棋盘只在「井字棋」tab 显示（互不遮挡）。 */
const showMap = computed(() => pad.tab === PAD_TAB_EXEC);
const showTttBoard = computed(() => pad.tab === PAD_TAB_TTT && ttt.gameVisible);

/** 执行 tab 的手柄订阅：只在本页 + exec tab + 有权限时把动作交给 pad store。 */
function onPad(action) {
  if (robot.activeView !== "pad") return;
  if (pad.tab !== PAD_TAB_EXEC) return;
  pad.onGamepad(action);
}

let unsubPad = null;

/** 进入本页：订阅手柄 + 启动井字棋轮询（未授权时 store 内部自发早退）。 */
function enter() {
  if (!unsubPad) unsubPad = subscribeGamepad(onPad);
  if (auth.controlActive) ttt.startPolling();
}
/** 离开本页：**必须落锁**（安全），并停井字棋轮询。 */
function leave() {
  pad.disarm("已离开手柄执行页：真机动作自动落锁");
  pad.resetCursor();
  ttt.stopPolling();
}

watch(() => robot.activeView, (v) => { if (v === "pad") enter(); else leave(); });
// 权限到手后把井字棋轮询补起来（进页面时可能还没令牌）
watch(() => auth.controlActive, (on) => {
  if (on && robot.activeView === "pad") ttt.startPolling();
});
// 切到井字棋 tab → 真机动作落锁（看棋时手柄不该还 armed 着）
watch(() => pad.tab, (t) => { if (t === PAD_TAB_TTT) pad.disarm("已切到井字棋：手柄真机动作自动落锁"); });

onMounted(() => { if (robot.activeView === "pad") enter(); });

onBeforeUnmount(() => {
  if (unsubPad) { unsubPad(); unsubPad = null; }
  ttt.stopPolling();
});

/** 气路状态中文（如实显示，不猜）。 */
const suckText = computed(() => exec.vacuumState === "suck" ? "吸（保持中）" : "关");
const blowText = computed(() => exec.vacuumState === "blow" ? "放（保持中）" : "关");
</script>

<template>
  <MonitorLayout view="pad">
    <template #hud>
      <div class="exec-hud">
        手柄执行 · {{ pad.armed ? "已启动" : "未启动" }} · 选中 {{ pad.cursorLabel }}
      </div>
      <CameraPanel :views="['pad', 'live']" />
      <PadMap2D v-if="showMap" />
      <TicTacToeBoard v-if="showTttBoard" />
    </template>

    <!-- 右栏顶部：执行 / 井字棋（复用全局 .srt-tabs/.srt-tab，与其它页同一套视觉） -->
    <div class="srt-tabs" role="tablist">
      <button class="srt-tab" :class="{ on: pad.tab === 'exec' }" @click="pad.setTab('exec')">
        <Icon name="target" :size="13" /><span>执行</span>
      </button>
      <button class="srt-tab" :class="{ on: pad.tab === 'ttt' }" @click="pad.setTab('ttt')">
        <Icon name="grid" :size="13" /><span>井字棋</span>
      </button>
    </div>

    <!-- ================= 执行 ================= -->
    <template v-if="pad.tab === 'exec'">
      <div class="card">
        <h3>
          <Icon name="gamepad" :size="15" /> 手柄执行
          <span class="h3-sub">{{ pad.armed ? "已启动" : "未启动（Start 启动）" }}</span>
        </h3>

        <div class="pd-row">
          <span class="k">选中点</span>
          <span class="v hl">{{ pad.cursorLabel }}</span>
        </div>
        <div class="pd-row"><span class="k">类型</span><span class="v">{{ pad.cursorKind }}</span></div>
        <div class="pd-row">
          <span class="k">正上位</span>
          <span class="v">{{ pad.hasAbove ? (pad.cursor + "上") : "无（该点没有正上位）" }}</span>
        </div>
        <div class="pd-row">
          <span class="k">吸气 X</span><span class="v">{{ suckText }}</span>
          <span class="k" style="margin-left:auto">放气 B</span><span class="v">{{ blowText }}</span>
        </div>

        <div class="pd-hr"></div>
        <div class="pd-legend">
          <span><b>↑↓←→</b> 选点</span>
          <span><b>A</b> 原位</span>
          <span><b>Y</b> 正上位</span>
          <span><b>X</b> 吸气⇄停吸</span>
          <span><b>B</b> 放气⇄停放</span>
          <span><b>LB</b> J6 正面</span>
          <span><b>RB</b> J6 反面</span>
          <span><b>Start</b> 启动/关闭</span>
        </div>

        <p v-if="!auth.controlActive" class="small err">
          <Icon name="alert" :size="12" /> 需要控制权限（点右上角「请求控制」）。
        </p>
        <p v-else-if="!pad.armed" class="small muted">
          <Icon name="lock" :size="12" /> 手柄未启动：按手柄 <b>Start</b>（或点测试盒的
          <b>Start 启动/关闭</b>）解锁后，A/Y/X/B/LB/RB 才会真机动作。
        </p>
        <!-- ★ 2026-10-12：已启动但机器人未就绪 → 明确告知（真机动作会被拒绝，别当成"手柄失灵"） -->
        <p v-else-if="!rc.ready" class="small err">
          <Icon name="alert" :size="12" /> {{ NOT_READY_HINT }}：未就绪时 A/Y/X/B/LB/RB 一律不下发。
        </p>
        <p v-if="pad.msg" class="small ok">{{ pad.msg }}</p>
        <p v-if="pad.err" class="small err"><Icon name="alert" :size="12" /> {{ pad.err }}</p>
      </div>

      <GamepadTestCard />
    </template>

    <!-- ================= 井字棋（从「智能分拣」页整体搬来） ================= -->
    <template v-else>
      <TicTacToeCard />
      <GamepadTestCard />
    </template>
  </MonitorLayout>
</template>

<style scoped>
.card { border: 1px solid var(--line); border-radius: 10px; padding: 10px;
  background: var(--panel); }
.card h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 6px;
  font-size: 14px; color: var(--txt); }
.card h3 svg { color: var(--accent); }
.h3-sub { margin-left: auto; font-size: 11px; color: var(--muted); font-weight: 400; }
.pd-row { display: flex; gap: 8px; align-items: center; font-size: 12px; padding: 1px 0; }
.pd-row .k { width: 52px; flex: none; color: var(--muted); }
.pd-row .v { color: var(--txt); }
.pd-row .v.hl { color: var(--accent); font-weight: 600; font-family: var(--mono); }
.pd-hr { height: 1px; background: var(--line); margin: 6px 0; }
.pd-legend { display: flex; flex-wrap: wrap; gap: 5px 10px; font-size: 11px; color: var(--muted); }
.pd-legend b { color: var(--accent); font-family: var(--mono); }
.small { font-size: 11.5px; margin: 6px 0 0; display: flex; align-items: center; gap: 5px; }
.small.muted { color: var(--muted); }
.small.muted b { color: var(--txt); }
.small.ok { color: var(--ok); }
.small.err { color: var(--err); }
</style>
