<script setup>
// =====================================================================
// 井字棋卡片（分拣页右侧栏）—— 难度 / 开始 / 暂停⇄继续 / 停止 / 复位 / 复盘
//
// 方案：docs/方案-井字棋对弈（机器人真实落子）.md §3.2
//
// ★ 安全口径：
//   · **默认空跑**（dry_run=true）：只走 2D/3D 演示，不动机器人；
//     切到「真实下发」要**二次确认**（会真的搬棋子）。
//   · 全部控制走 store（带令牌）；未授权时按钮禁用，不发请求。
//   · 暂停⇄继续**复用** SortingView 的琥珀色 .pause-btn 样式与 togglePause 语义：
//     暂停=原地冻结可续；停止=作废本局（棋子留在棋盘，可点「复位」搬回 A 区）。
//   · 复盘**只放 2D**，不动机器人（方案 §9）。
// =====================================================================
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import Icon from "./Icon.vue";
import { useAuthStore } from "../stores/auth.js";
import { useTicTacToeStore } from "../stores/tictactoe.js";
import { useRobotStore } from "../stores/robot.js";
import { diffCn, resultCn, symCn } from "../games/tictactoe.js";
import { onGamepadConnection, subscribeGamepad } from "../games/gamepad.js";

const auth = useAuthStore();
const ttt = useTicTacToeStore();
const robot = useRobotStore();

const DIFFS = ["easy", "normal", "hard"].map((k) => ({ k, cn: diffCn(k) }));

const running = computed(() => ttt.running);
const paused = computed(() => ttt.paused);
const locked = computed(() => running.value || paused.value);
const canCtrl = computed(() => auth.controlActive && !ttt.busy);

const st = computed(() => ttt.st || {});
const firstText = computed(() => {
  if (!ttt.st) return "—";
  return st.value.first === "user" ? "你（先手执圆）" : "机器人（先手执圆）";
});
const stageText = computed(() => st.value.stage || "—");
const pieceNo = computed(() => st.value.piece_no || 0);
const resultText = computed(() => {
  if (ttt.inReplay) return resultCn(ttt.replay && ttt.replay.result);
  return resultCn(st.value.result);
});
const hasMoves = computed(() => Array.isArray(st.value.moves) && st.value.moves.length > 0);
const canReplay = computed(() => !running.value && !!st.value.finished && hasMoves.value);

const userSymText = computed(() => symCn(st.value.user_sym));
const robotSymText = computed(() => symCn(st.value.robot_sym));

async function doStart() {
  await ttt.start(ttt.difficulty, ttt.dryRun);
}

/** 空跑 → 真实下发 的切换要二次确认（真实会动机器人）。 */
function onDryChange(e) {
  const want = !!e.target.checked;
  if (!want) {
    const ok = window.confirm("切到「真实下发」会真动机器人搬棋子（全程 5% 速度）。\n确认继续？");
    if (!ok) { e.target.checked = true; ttt.dryRun = true; return; }
  }
  ttt.dryRun = want;
}

/** 复位：把 B 区棋子搬回 A 区。force=true 跳过弹窗（手柄走"二次确认"，见下）。 */
async function doReset(force = false) {
  if (running.value) return;
  if (!force && !window.confirm("把 B 区已落的棋子搬回 A 区（真实下发时机器人会逐个搬回）？")) return;
  await ttt.reset();
}

async function doReplay() {
  if (ttt.inReplay) ttt.stopReplay();
  else await ttt.loadReplay();
}

// ---------------------------------------------------------------- 手柄（Xbox）
// ★ 与棋盘组件**分工不重叠**：棋盘接「方向 + A 落子」；本卡片接
//   「Start 开始 / X 暂停⇄继续 / B 停止 / Y 复盘 / View 复位」。
// ★ 复位会真动机器人：手柄没法点 window.confirm 弹窗，所以改成**二次确认** ——
//   第一次按 View 只提示"再按一次"，3 秒内再按才真的复位（等效于弹窗的确认动作）。
const padOn = ref(false);
let padResetArm = 0;

function onPad(action) {
  if (robot.activeView !== "sort") return;      // 只在分拣页响应，别把全局手柄劫走
  if (!auth.controlActive) return;
  if (action === "start") { if (!running.value) doStart(); return; }
  if (action === "pause") { if (running.value || paused.value) ttt.togglePause(); return; }
  if (action === "stop") { if (running.value || paused.value) ttt.stop(); return; }
  if (action === "replay") { if (ttt.inReplay || canReplay.value) doReplay(); return; }
  if (action === "reset") {
    if (running.value || !hasMoves.value) return;
    const now = Date.now();
    if (now - padResetArm < 3000) { padResetArm = 0; doReset(true); }
    else { padResetArm = now; ttt.msg = "手柄复位：请再按一次 View 确认（3 秒内）"; }
  }
}

let unsubPad = null;
let unsubConn = null;
onMounted(() => {
  unsubPad = subscribeGamepad(onPad);
  unsubConn = onGamepadConnection((v) => { padOn.value = v; });
});
onBeforeUnmount(() => {
  if (unsubPad) unsubPad();
  if (unsubConn) unsubConn();
});
</script>

<template>
  <div class="card ttt-card">
    <h3><Icon name="grid" :size="15" /> 井字棋
      <span class="h3-sub">棋盘 = B 区 3×3</span>
    </h3>

    <!-- 难度：三档单选，开局后锁定 -->
    <div class="ttt-row">
      <span class="k">难度</span>
      <span class="seg">
        <button v-for="d in DIFFS" :key="d.k" :class="{ on: ttt.difficulty === d.k }"
                :disabled="locked" @click="ttt.difficulty = d.k">{{ d.cn }}</button>
      </span>
    </div>

    <!-- 空跑 / 真实下发：默认空跑 -->
    <label class="dry-toggle">
      <input type="checkbox" :checked="ttt.dryRun" :disabled="locked" @change="onDryChange" />
      空跑（<b>不下发运动、不动机器人</b>；取消勾选 = 真实下发）
    </label>

    <!-- 操作按钮 -->
    <div class="btns">
      <button class="primary" :disabled="!canCtrl || running" @click="doStart">
        <Icon name="play" :size="13" /> {{ st.result || running ? "重新开始" : "开始" }}
      </button>
      <!-- ★ 暂停⇄继续：琥珀色（同 SortingView .pause-btn 语义） -->
      <button v-if="running || paused" class="pause-btn" :disabled="!canCtrl" @click="ttt.togglePause"
              :title="paused ? '从暂停处继续' : '在当前动作结束后暂停（步边界生效）；想让机器人立刻停住请用停止/急停'">
        <Icon :name="paused ? 'play' : 'pause'" :size="13" /> {{ paused ? "继续" : "暂停" }}
      </button>
      <button v-if="running || paused" class="stop-btn" :disabled="!canCtrl" @click="ttt.stop">
        <Icon name="stop" :size="13" /> 停止
      </button>
      <button class="mini-btn" :disabled="!canCtrl || locked || !hasMoves" title="把 B 区棋子搬回 A 区"
              @click="doReset">
        <Icon name="refresh" :size="13" /> 复位
      </button>
      <button class="mini-btn" :disabled="!canCtrl || !canReplay" title="逐手回放（只放 2D，不动机器人）"
              @click="doReplay">
        <Icon name="history" :size="13" /> {{ ttt.inReplay ? "退出复盘" : "复盘" }}
      </button>
    </div>

    <!-- 状态行 -->
    <div class="ttt-row"><span class="k">先手</span><span class="v">{{ firstText }}</span></div>
    <div class="ttt-row"><span class="k">执子</span>
      <span class="v">你 {{ userSymText }} · 机器人 {{ robotSymText }}</span>
    </div>
    <div class="ttt-row"><span class="k">难度</span>
      <span class="v">{{ diffCn(ttt.difficulty) }}</span>
    </div>
    <!-- ★ 本局速度 = 右上角全局速度（用户口径："右上角多少，机器人速度多少"） -->
    <div class="ttt-row"><span class="k">速度</span>
      <span class="v">{{ ttt.speedPct ? ttt.speedPct + "%" : "—" }}
        <span class="muted">（跟顶栏）</span></span>
    </div>
    <div class="ttt-row"><span class="k">阶段</span><span class="v">{{ stageText }}</span></div>
    <div class="ttt-row"><span class="k">进度</span>
      <span class="v">第 {{ pieceNo }} 手{{ paused ? " · 已暂停" : "" }}</span>
    </div>
    <div class="ttt-row"><span class="k">结果</span>
      <span class="v" :class="{ win: resultText === '你赢了', lose: resultText === '你输了' }">{{ resultText }}</span>
    </div>

    <!-- 复盘控制条 -->
    <div v-if="ttt.inReplay" class="replay-bar">
      <button class="mini-btn" @click="ttt.stepReplay(-1)"><Icon name="chevronLeft" :size="12" /> 上一步</button>
      <button class="mini-btn" @click="ttt.stepReplay(1)">下一步 <Icon name="chevronRight" :size="12" /></button>
      <button class="mini-btn" v-if="!ttt.replayPlaying" @click="ttt.playReplay">
        <Icon name="play" :size="12" /> 自动播放
      </button>
      <button class="mini-btn" v-else @click="ttt.stopReplay"><Icon name="stop" :size="12" /> 停止</button>
    </div>

    <!-- ★ 手柄（Xbox / 标准映射）：插上即用；浏览器要求"按任意键"唤醒手柄后才上报 -->
    <div class="pad-row">
      <Icon name="gamepad" :size="12" :class="{ live: padOn }" />
      <span v-if="padOn" class="pad-on">手柄已连接 · 左摇杆 移动 · 右摇杆 ↑下棋 →暂停 ↓停止 ←复盘 · A 落子 · Start 开始 · View×2 复位</span>
      <span v-else>未检测到手柄：接上 Xbox 手柄后按任意键即可</span>
    </div>

    <p v-if="ttt.err" class="small err"><Icon name="alert" :size="12" /> {{ ttt.err }}</p>
    <p v-else-if="ttt.msg" class="small ok">{{ ttt.msg }}</p>
    <p v-if="!auth.controlActive" class="small muted">
      <Icon name="lock" :size="12" /> 需要控制权限才能操作（与其它页同一套令牌）。
    </p>
    <p v-else-if="st.dry_run" class="small muted">
      <Icon name="flask" :size="12" /> 当前为空跑：棋盘 2D/3D 会变，但机器人不动、不判色。
    </p>
  </div>
</template>

<style scoped>
.card { border: 1px solid var(--line); border-radius: 10px; padding: 10px; background: var(--panel); }
.card h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 6px; font-size: 14px; color: var(--txt); }
.card h3 svg { color: var(--accent); }
.h3-sub { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: auto; }
.ttt-row { display: flex; gap: 10px; font-size: 13px; padding: 1px 0; align-items: center; }
.ttt-row .k { width: 52px; color: var(--muted); flex: none; }
.ttt-row .v { color: var(--txt); }
.ttt-row .v.win { color: var(--accent); font-weight: 600; }
.ttt-row .muted { color: var(--muted); font-size: 11px; }
/* ★ 手柄提示行：未连接=灰、已连接=青（现场一眼看出手柄到底认到没有） */
.pad-row { display: flex; align-items: flex-start; gap: 5px; margin-top: 6px;
  font-size: 11px; color: var(--muted); line-height: 1.45; }
.pad-row svg { flex: none; margin-top: 1px; color: var(--muted); }
.pad-row svg.live { color: var(--accent); }
.pad-on { color: var(--txt); }
.ttt-row .v.lose { color: var(--err, #e05252); font-weight: 600; }
.seg { display: flex; gap: 4px; }
.seg button { flex: none; padding: 3px 10px; font-size: 12px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel2); color: var(--muted); cursor: pointer; }
.seg button:hover:not(:disabled) { color: var(--accent); border-color: var(--accent); }
.seg button.on { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.seg button:disabled { opacity: .5; cursor: not-allowed; }
.dry-toggle { display: inline-flex; align-items: center; gap: 5px; margin-top: 6px;
  font-size: 12px; color: var(--muted); cursor: pointer; }
.dry-toggle b { color: var(--txt); }
.btns { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.pause-btn { display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(255, 176, 32, .45); color: #ffc861;
  background: rgba(255, 176, 32, .10); font-family: var(--font); font-size: 13px; font-weight: 600; }
.pause-btn:hover:not(:disabled) { background: rgba(255, 176, 32, .18); }
.pause-btn:disabled { opacity: .5; cursor: not-allowed; }
.stop-btn { display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(224, 82, 82, .5); color: #ffb4b4;
  background: rgba(224, 82, 82, .12); font-family: var(--font); font-size: 13px; font-weight: 600; }
.stop-btn:hover:not(:disabled) { background: rgba(224, 82, 82, .2); }
.stop-btn:disabled { opacity: .5; cursor: not-allowed; }
.mini-btn { display: inline-flex; align-items: center; gap: 4px; padding: 5px 9px;
  font-size: 12px; border-radius: 6px; }
.replay-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.small { font-size: 12px; line-height: 1.6; margin: 5px 0 0; display: flex; align-items: center; gap: 4px; }
.err { color: var(--err, #e05252); }
.ok { color: var(--ok, #2f9e6f); }
.muted { color: var(--muted); }
</style>
