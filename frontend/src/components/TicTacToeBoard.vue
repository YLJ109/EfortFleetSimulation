<script setup>
// =====================================================================
// 井字棋 2D 棋盘浮层 —— 挂在分拣页 3D 视口的 #hud 插槽上（Sentinel 深色半透明）
//
// 方案：docs/方案-井字棋对弈（机器人真实落子）.md §3.3
//
// ★ 输入口径（用户口径：键盘 + 鼠标都要）：
//   · ↑↓←→ 移动光标（边界夹住、不环绕）；Enter 在光标格落子；
//   · 鼠标点格直接落子（悬停与光标同步）。
//   · **只在「轮到你」且机器人空闲时生效**；机器人执行/思考期间一律忽略（并能给轻微提示）。
//   · **绝不占用 Space**（Space 已用于语音）；焦点在输入框/文本域时**不拦截**。
//
// ★ 棋盘 = B 区 3×3（cell i ↔ B 槽位 B(i+1)），与后端同口径（games/tictactoe.js）。
//   ★「回放」态显示的是**按棋谱逐手重建的棋盘**（只查看、不动机器人）；
//     「真实复盘」是后端 POST /tictactoe/replay（机器人真的动、逐子搬回原位），
//     它开始前会退出回放态 —— 于是本浮层始终跟随**真实**棋盘（不编故事）。
// =====================================================================
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { useTicTacToeStore } from "../stores/tictactoe.js";
import { useRobotStore } from "../stores/robot.js";
import { moveCursor, resultCn, symCn } from "../games/tictactoe.js";
import { subscribeGamepad } from "../games/gamepad.js";

const ttt = useTicTacToeStore();
const robot = useRobotStore();

const cursor = ref(4);      // 光标默认放中点
const hint = ref("");

const visible = computed(() => ttt.gameVisible);
const board = computed(() => ttt.displayBoard);
const canPlay = computed(() => ttt.canPlay);
const winLine = computed(() => (ttt.inReplay ? (ttt.replay.win_line || []) : ttt.winLine));
const result = computed(() => (ttt.inReplay ? ttt.replay.result : ttt.result));
const userWin = computed(() => result.value === "user_win");
const pieceNo = computed(() => (ttt.st && ttt.st.piece_no) || 0);

const turnText = computed(() => {
  if (ttt.inReplay) {
    const n = (ttt.replay && ttt.replay.moves ? ttt.replay.moves.length : 0);
    return "复盘 " + ttt.replayIdx + "/" + n + " 手";
  }
  if (result.value) return resultCn(result.value);
  if (!ttt.running) {
    if (ttt.st && ttt.st.voided) return "本局已作废";
    if (ttt.st && ttt.st.finished) return "本局已结束";
    return "未开局";
  }
  if (ttt.paused) return "已暂停";
  if (ttt.turn === "user") return "轮到你（" + symCn(ttt.userSym) + "）";
  if (ttt.turn === "robot") return "机器人思考中…";
  return "准备中…";
});

const mark = (i) => symCn(board.value[i]);
const isWin = (i) => winLine.value.indexOf(i) >= 0;

/** 点/悬停某一格。 */
function onCell(i) {
  cursor.value = i;
  hint.value = "";
  if (ttt.inReplay) return;
  if (!ttt.running) { hint.value = "先点「开始」"; return; }
  if (ttt.paused) { hint.value = "已暂停，先「继续」"; return; }
  if (ttt.turn !== "user") { hint.value = "现在是机器人的回合"; return; }
  if (board.value[i]) { hint.value = "这格已经有子了"; return; }
  ttt.move(i);
}

function isTyping(el) {
  if (!el) return false;
  const tag = String(el.tagName || "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable === true;
}

const KEY_DIR = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };

/** 全局键盘：仅当分拣页激活 + 轮到你时处理；输入框内不拦截；不占用 Space。 */
function onKey(e) {
  if (!visible.value || robot.activeView !== "sort") return;
  if (isTyping(e.target)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (ttt.inReplay || !canPlay.value) return;
  if (KEY_DIR[e.key]) {
    cursor.value = moveCursor(cursor.value, KEY_DIR[e.key], 3);
    e.preventDefault();      // 只挡方向键的页面滚动
    return;
  }
  if (e.key === "Enter") {
    const i = cursor.value;
    if (board.value[i]) { hint.value = "这格已经有子了"; return; }
    ttt.move(i);
    e.preventDefault();
  }
}

// 轮到用户时，若光标停在已占格上 → 自动落到第一个空位（省得手动挪）
watch(canPlay, (on) => {
  if (!on) return;
  if (!board.value[cursor.value]) return;
  const i = board.value.findIndex((v) => !v);
  if (i >= 0) cursor.value = i;
});

// ---------------------------------------------------------------- 手柄（Xbox）
// ★ 与键盘**同一套语义**：方向移动光标、A(=confirm) 落子；其余动作（开始/暂停/
//   停止/复盘/复位）由右侧卡片订阅处理 —— 两块互不重叠，避免一次按键触发两处。
function onPad(action) {
  if (!visible.value || robot.activeView !== "sort") return;
  if (ttt.inReplay || !canPlay.value) return;
  if (action === "up" || action === "down" || action === "left" || action === "right") {
    cursor.value = moveCursor(cursor.value, action, 3);
    return;
  }
  if (action === "confirm") {
    const i = cursor.value;
    if (board.value[i]) { hint.value = "这格已经有子了"; return; }
    ttt.move(i);
  }
}

let unsubPad = null;
onMounted(() => {
  window.addEventListener("keydown", onKey);
  unsubPad = subscribeGamepad(onPad);
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKey);
  if (unsubPad) unsubPad();
});
</script>

<template>
  <div v-if="visible" class="ttt-board" :class="{ over: !!result, 'win-you': userWin, replay: ttt.inReplay }">
    <div class="ttt-head">
      <span class="ttt-title">井字棋</span>
      <span class="ttt-turn" :class="{ you: canPlay }">{{ turnText }}</span>
    </div>

    <div class="ttt-grid">
      <button v-for="(v, i) in board" :key="i" class="ttt-cell"
              :class="{ cur: i === cursor && canPlay, win: isWin(i), o: v === 'O', x: v === 'X' }"
              :disabled="!canPlay" @click="onCell(i)" @mouseenter="cursor = i">
        <span class="mk">{{ mark(i) }}</span>
      </button>
    </div>

    <div class="ttt-foot">
      <span class="ttt-no">第 {{ pieceNo }} 手</span>
      <span v-if="hint" class="ttt-hint">{{ hint }}</span>
      <span v-else-if="result" class="ttt-res" :class="{ win: userWin, lose: result === 'robot_win' }">
        {{ resultCn(result) }}
      </span>
      <span v-else-if="!ttt.inReplay && canPlay" class="ttt-hint">左摇杆/↑↓←→ 移动 · A/右摇杆↑ 落子</span>
    </div>

    <div v-if="ttt.inReplay" class="ttt-rep">回放（棋谱记录 · 只查看不动机器人）</div>
  </div>
</template>

<style scoped>
/* 浮在 3D 视口右上角（exec-hud 在左上、状态片在右下，互不遮挡） */
.ttt-board { position: absolute; right: 12px; top: 12px; z-index: 13;
  width: 196px; padding: 8px;
  background: var(--glass); border: 1px solid var(--glass-line);
  border-radius: 10px; box-shadow: var(--shadow-hud);
  backdrop-filter: blur(14px) saturate(140%);
  -webkit-backdrop-filter: blur(14px) saturate(140%);
  font-family: var(--font); color: var(--txt); }
.ttt-head { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.ttt-title { color: var(--accent); font-weight: 600; }
.ttt-turn { margin-left: auto; color: var(--muted); font-size: 11.5px; }
.ttt-turn.you { color: var(--accent); }
.ttt-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; margin-top: 7px; }
.ttt-cell { aspect-ratio: 1 / 1; display: flex; align-items: center; justify-content: center;
  padding: 0; border: 1px solid var(--line); border-radius: 7px;
  background: rgba(255, 255, 255, .03); color: var(--txt);
  font-size: 22px; line-height: 1; cursor: pointer; transition: all .12s ease; }
.ttt-cell:hover:not(:disabled) { border-color: var(--accent); background: var(--accent-soft); }
.ttt-cell:disabled { cursor: default; }
.ttt-cell.cur { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.ttt-cell.o .mk { color: #7fe3ff; }
.ttt-cell.x .mk { color: #ffc861; }
/* ★ 获胜三连线高亮 + 轻微脉冲（用户赢时更强） */
.ttt-cell.win { border-color: var(--accent); background: rgba(47, 214, 240, .16);
  animation: ttt-win 0.9s ease-in-out infinite alternate; }
.ttt-board.win-you .ttt-cell.win { background: rgba(47, 214, 240, .3); }
.ttt-board.win-you { border-color: var(--accent); }
@keyframes ttt-win {
  from { box-shadow: 0 0 0 rgba(47, 214, 240, 0); }
  to { box-shadow: 0 0 10px var(--accent); }
}
.ttt-foot { display: flex; align-items: center; gap: 6px; margin-top: 6px; font-size: 11px; }
.ttt-no { color: var(--muted); }
.ttt-hint { margin-left: auto; color: var(--muted); }
.ttt-res { margin-left: auto; font-weight: 600; }
.ttt-res.win { color: var(--accent); }
.ttt-res.lose { color: var(--err, #e05252); }
.ttt-rep { margin-top: 5px; font-size: 10.5px; color: var(--warn, #ffc861); }
</style>
