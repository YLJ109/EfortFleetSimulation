<script setup>
// =====================================================================
// 井字棋卡片（**手柄执行页**右侧栏「井字棋」tab）—— 难度 / 开始 / 暂停⇄继续 / 停止 / 复原 / 回放
//   ★ 2026-10-11：从「智能分拣」页**整体搬入**「手柄执行」页（用户口径"井字棋要搬走"）。
//
// 方案：docs/方案-井字棋对弈（机器人真实落子）.md §3.2
//       docs/方案-井字棋3D棋子视觉与复原（颜色·圆叉·坠落·真机）.md（本轮「复原」合并 + 默认真机）
//
// ★ 安全口径：
//   · ★★ 2026-10-11（用户："**不用空跑啊，要真机移动**"）：**默认真实下发**（会真搬棋子），
//     开局前**二次确认**；勾「空跑」才只走 2D/3D 演示（排练用）。硬安全网 = 后端未就绪闸门
//     （伺服未上电 → 统一 409）+ 顶栏速度 + 停止/急停。
//   · 全部控制走 store（带令牌）；未授权时按钮禁用，不发请求。
//   · 暂停⇄继续**复用** SortingView 的琥珀色 .pause-btn 样式与 togglePause 语义：
//     暂停=原地冻结可续；停止=作废本局（棋子留在棋盘，可点「复原」搬回 A 区）。
//   · ★★ 「复原」= **复位 + 复盘合并**（用户 2026-10-11 口径）：机器人**真的动**，
//     按落子逆序逐个把棋子搬回**原位**（来源 A 格）；真实局要**二次确认**。
//     空跑局没有真实搬动过棋子 → 后端只清棋盘并**如实说明**（不假装搬过）。
//   · 「回放」是**只读的棋谱查看**（逐手回看，不动机器人），与「复原」（真机）分开、互不混淆。
// =====================================================================
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import Icon from "./Icon.vue";
import { useAuthStore } from "../stores/auth.js";
import { useTicTacToeStore } from "../stores/tictactoe.js";
import { useRobotStore } from "../stores/robot.js";
import { useExecStore } from "../stores/exec.js";
import { usePadStore, PAD_TAB_TTT } from "../stores/pad.js";
import { useRcReadyStore } from "../stores/rcReady.js";
import { diffCn, resultCn, symCn } from "../games/tictactoe.js";
import { onGamepadConnection, subscribeGamepad } from "../games/gamepad.js";

const auth = useAuthStore();
const ttt = useTicTacToeStore();
const robot = useRobotStore();
// ★ 井字棋卡片现在挂在「手柄执行」页的右栏 tab 里 —— 手柄归属由 pad.tab 决定。
const pad = usePadStore();
// ★★ 2026-10-11：速度**实时跟随右上角滑块**（长流程下一步生效）——
//   以前这里显示的是"**开局快照**"，跑起来后拖滑块不变，现场看到就是"我设 40 它按 10"。
const exec = useExecStore();
// ★★ 2026-10-12：井字棋的「执行类」按钮（**开始 / 复原**）在**未就绪**时也一律灰掉
//   （用户口径「手柄的、井字棋的哪些执行按钮也不让点（没有就绪的话）」）。
//   ★ 「停止 / 暂停继续」**不**加这道门 —— 它们是"刹车"，任何情况下都必须能按。
const rc = useRcReadyStore();

const DIFFS = ["easy", "normal", "hard"].map((k) => ({ k, cn: diffCn(k) }));
// ★ 自证约定的可选颜色（2026-10-10 用户口径："也可以切换别的颜色，用户自己选吧"）
const COLORS = ["红", "绿", "蓝"];

const running = computed(() => ttt.running);
const paused = computed(() => ttt.paused);
const locked = computed(() => running.value || paused.value);
// ★ 本局模式文案：开局后以**后端 state** 为准（本地开关只用于"开工前"）。
//   空跑不判色 —— 必须如实标出来，别让人以为"自证已经在跑"（本项目铁律：不知道就说不知道）。
const modeText = computed(() => {
  const on = ttt.st ? !!ttt.st.verify_color : !!ttt.verifyColor;
  if (!on) return "默认（不判色）";
  return ttt.dryRun ? "拍照自证（空跑不判色）" : "拍照自证";
});
const canCtrl = computed(() => auth.controlActive && !ttt.busy);
// ★★ 2026-10-12：**执行类**动作（开始 / 复原）额外要求**完整就绪**（`rc.ready`）。
//   未就绪时机器人不可能动 → 按钮先灰掉，别让人"按了才被后端 409 拒"。
const canRun = computed(() => canCtrl.value && rc.ready);

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
  // ★★ 2026-10-11 用户口径（原话「**这个不需要啊**」，附图 = 开局那个 confirm 弹窗）：
  //   **去掉开局的动作确认弹窗** —— 点一下就开，不再每次挡一下。
  //   ★ 真机保护一个都没少：统一「未就绪闸门」（伺服未上电一律 409）+ 安全围栏 +
  //     界面上「真实下发 / 空跑」开关本身（状态肉眼可见）。
  await ttt.start(ttt.difficulty, ttt.dryRun);
}

/** 真实下发 ⇄ 空跑 的切换：**不再弹确认**（同一条用户口径）。 */
function onDryChange(e) {
  ttt.dryRun = !!e.target.checked;
}

/** ★「复原」（= 复位 + 复盘合并，用户 2026-10-11 口径）：把 B 区已落的子**逐个搬回原位**。
 *  · 真实局 → **真机搬回**（后端真起线程；伺服未上电会被统一闸门 409）；
 *  · 空跑局 → 本局没有真实搬动过棋子（无实体可搬）→ 后端只清棋盘并如实说明，无需确认。
 *  ★ 2026-10-11：`window.confirm` 已按用户口径移除（「这个不需要啊」）。 */
async function doRestore() {
  if (running.value || !hasMoves.value) return;
  await ttt.reset();
}

/** 回放 = **只读棋谱查看**（逐手回看，不动机器人）；与「复原」（真机）分开。 */
function doViewReplay() {
  if (ttt.inReplay) ttt.stopReplay();
  else ttt.loadReplay();
}

// ---------------------------------------------------------------- 手柄（Xbox）
// ★ 与棋盘组件**分工不重叠**：棋盘接「方向 + A 落子」；本卡片接
//   「Start 开始 / X 暂停⇄继续 / B 停止 / Y、View 复原」。
// ★ 复原会真动机器人：**界面已不再用弹窗做确认**（2026-10-11 用户口径「这个不需要啊」），
//   但手柄**按不到任何弹窗**，所以这里保留手柄专有的**二次确认** ——
//   第一次按只提示"再按一次"，3 秒内再按才真的执行；这是手柄这一路**唯一的**确认手段。
//   （空跑局不动机器人 → 复原无需二次确认。）
//   ★ 若哪天用户说"手柄也不要按两次"，把 `padRestoreArm` 这段删掉即可（一处，勿散开写）。
const padOn = ref(false);
let padRestoreArm = 0;

function onPad(action) {
  // ★★ 2026-10-11：井字棋已从「智能分拣」页**搬到「手柄执行」页**（右栏 tab），
  //   手柄守卫随之改成"**本页 + 井字棋 tab**"。★ 否则会与同一页「执行」tab 抢按键
  //   （一次按键两个动作 —— 危险）。
  if (robot.activeView !== "pad" || pad.tab !== PAD_TAB_TTT) return;
  if (!auth.controlActive) return;
  if (action === "start") { if (!running.value) doStart(); return; }
  if (action === "pause") { if (running.value || paused.value) ttt.togglePause(); return; }
  if (action === "stop") { if (running.value || paused.value) ttt.stop(); return; }
  // ★ Y(复盘) 与 View(复位) **合并**为「复原」（用户口径：两者就是一个动作）
  if (action === "replay" || action === "reset") {
    if (running.value || !hasMoves.value) return;
    if (st.value.dry_run) { doRestore(); return; }   // 空跑：不动机器人，直接复位
    const now = Date.now();
    if (now - padRestoreArm < 3000) { padRestoreArm = 0; doRestore(); }
    else { padRestoreArm = now; ttt.msg = "手柄复原（真机）：请再按一次确认（3 秒内）"; }
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

    <!-- ★ 两种模式（2026-10-10 用户口径）：默认 / 拍照自证 —— 开局后锁定 -->
    <div class="ttt-row">
      <span class="k">模式</span>
      <span class="seg">
        <button :class="{ on: !ttt.verifyColor }" :disabled="locked"
                title="不判色、不去 C 位：取料后直接去 B 落子（节拍快）"
                @click="ttt.verifyColor = false">默认</button>
        <button :class="{ on: ttt.verifyColor }" :disabled="locked"
                title="取件后先去 C 检测位拍照核对颜色：判不出 / 不是该格应有的颜色 → 放回原位并停止"
                @click="ttt.verifyColor = true">拍照自证</button>
      </span>
    </div>
    <!-- ★★ 2026-10-11（方案 §D）「正常 / 快速」两档（界面开关）：快速 = 一趟两子
         （先手随机；机器人先手时先提前显示其落子，再等你落子后合并一趟完成） -->
    <div class="ttt-row">
      <span class="k">节奏</span>
      <span class="seg">
        <button :class="{ on: !ttt.fast }" :disabled="locked"
                title="一趟一子：与之前一样（机器人子、你的子各搬一趟）"
                @click="ttt.fast = false">正常</button>
        <button :class="{ on: ttt.fast }" :disabled="locked"
                title="一趟两子（机器人子用正面、你的子用反面，J6 双面）：先手随机；机器人先手时先提前显示它的落子，再等你落子后合并一趟完成"
                @click="ttt.fast = true">快速</button>
      </span>
    </div>
    <div v-if="ttt.fast" class="ttt-hint">
      快速模式：<b>先手随机</b>（不再强制你先下）；机器人先手时<b>先把它的落子显示在 2D/3D</b>
      （提示「机器人已下棋完成·已显示，待搬子」），再<b>等你落子</b> → <b>一趟两子</b>
      （机器人子用正面、你落的那子用反面，靠 J6 翻转）。
      无需录制正反面（翻转 = 虚拟切面命令：正面 = 该点自身录制的 J6、反面 = 当前 J6 − 180，
      后端自动折算）；本轮<b>不与「拍照自证」合并</b>（请二选一）；
      「只剩一个空位」时<b>免等待、直接完成</b>（空跑不动机器人）。
    </div>

    <!-- ★ 自证约定的**编辑组件**：圆/叉各一个颜色下拉（红/绿/蓝），开局后锁定 -->
    <div v-if="ttt.verifyColor" class="ttt-hint">
      自证约定（可改）：
      <span class="sc-pick">圆
        <select v-model="ttt.selfColor.O" :disabled="locked">
          <option v-for="c in COLORS" :key="'o' + c" :value="c">{{ c }}</option>
        </select>
      </span>
      <span class="sc-pick">叉
        <select v-model="ttt.selfColor.X" :disabled="locked">
          <option v-for="c in COLORS" :key="'x' + c" :value="c">{{ c }}</option>
        </select>
      </span>
      需 C 检测位已示教、相机三色识别已开启；判不出 / 不符 → <b>放回原位并停止</b>（空跑不判色）。
    </div>

    <!-- 空跑 / 真实下发：★ 默认**真实下发**（用户口径："不用空跑啊，要真机移动"） -->
    <label class="dry-toggle">
      <input type="checkbox" :checked="ttt.dryRun" :disabled="locked" @change="onDryChange" />
      空跑（<b>排练用：不下发运动、不动机器人</b>）；不勾选 = <b>真实下发</b>（默认）
    </label>

    <!-- 操作按钮 -->
    <div class="btns">
      <button class="primary" :disabled="!canRun || running" @click="doStart">
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
      <button class="mini-btn" :disabled="!canRun || locked || !hasMoves"
              title="复原：机器人逐个把棋子搬回各自原位 A 格（真机，跟右上角速度）；空跑局只清棋盘、不动机器人"
              @click="doRestore">
        <Icon name="refresh" :size="13" /> 复原
      </button>
      <button class="mini-btn" :disabled="!canCtrl || !canReplay"
              title="回放：只按棋谱逐手回看（只查看、不动机器人）"
              @click="doViewReplay">
        <Icon name="rotateLeft" :size="13" /> {{ ttt.inReplay ? "退出回放" : "回放" }}
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
    <!-- ★★ 2026-10-11：速度**实时跟随右上角滑块**（长流程"下一步生效"）——
         以前显示的是**开局快照**（跑起来后拖滑块不变），现场看到就是"我设 40 它按 10"。
         现在显示**当前滑块值**；若本局用的是别的值（开局时定下、还没走到下一步），如实并列标出来。 -->
    <div class="ttt-row"><span class="k">速度</span>
      <span class="v">{{ exec.speed }}%
        <span class="muted">（跟顶栏滑块）</span>
        <em v-if="ttt.speedPct && ttt.speedPct !== exec.speed" class="muted">
          · 开局 {{ ttt.speedPct }}%</em></span>
    </div>
    <div class="ttt-row"><span class="k">模式</span>
      <span class="v">{{ modeText }}</span>
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
      <span v-if="padOn" class="pad-on">手柄已连接 · 左摇杆 移动 · 右摇杆 ↑下棋 →暂停 ↓停止 ←复原 · A 落子 · Start 开始 · Y/View×2 复原</span>
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
/* ★ 模式说明（自证约定）—— 只在选中"拍照自证"时出现，别常驻占位 */
.ttt-hint { font-size: 11px; line-height: 1.5; color: var(--muted); margin: 4px 0 6px; }
.ttt-hint b { color: var(--accent); }
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
/* ★ S12：改用语义令牌（--warn* / --err*），不再硬编码 rgba/#hex */
.pause-btn { display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--warn-line); color: var(--warn-fg);
  background: var(--warn-soft); font-family: var(--font); font-size: 13px; font-weight: 600; }
.pause-btn:hover:not(:disabled) { filter: brightness(1.18); }
.pause-btn:disabled { opacity: .5; cursor: not-allowed; }
.stop-btn { display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--err-line); color: var(--err-fg);
  background: var(--err-soft); font-family: var(--font); font-size: 13px; font-weight: 600; }
.stop-btn:hover:not(:disabled) { filter: brightness(1.18); }
.stop-btn:disabled { opacity: .5; cursor: not-allowed; }
.mini-btn { display: inline-flex; align-items: center; gap: 4px; padding: 5px 9px;
  font-size: 12px; border-radius: 6px; }
.replay-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.small { font-size: 12px; line-height: 1.6; margin: 5px 0 0; display: flex; align-items: center; gap: 4px; }
.err { color: var(--err, #e05252); }
.ok { color: var(--ok, #2f9e6f); }
.muted { color: var(--muted); }
</style>
