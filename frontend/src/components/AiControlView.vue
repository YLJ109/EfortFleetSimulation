<script setup>
// =====================================================================
// AI 执行页：**对话式**语音控制台
//   （自然语言 → 计划 → 说「执行」即跑 / 执行中来新指令自动接替）
//
// 方案：docs/方案-AI执行页对话化与语音直执（2026-10-09）.md
//
// ★ 布局（2026-10-09 二次改版，照用户给的参考图做的"聊天窗"）：
//     ┌ 标题栏：对话 · 状态（在线/空跑）· 清除 ────────────┐
//     │  消息区（flex:1 撑满，空状态时居中放提示 + 快捷指令）  │
//     │  ...消息...                                        │
//     ├ 输入栏（**固定在底部**，输入框内嵌麦 + 发送）────────┤
//     └──────────────────────────────────────────────────┘
//   ★ 上一版把"指令提示"排在输入框**下面**、开场白又长 —— 那不是聊天窗的样子。
//     现在的规则：**快捷指令属于空状态**（有消息就让位），**输入栏永远在最底下**。
//
// ★ 四条安全口径（改这个页面前务必理解）：
//   1. **解析 ≠ 执行**：仍分两步，界面必须先把"我要做什么"摊开给人看。
//      说「执行」只是**代替点按钮**，不是跳过回显。
//   2. **确认词严进**：整句匹配（`执行` / `执行吧` / `go`），
//      `执行分拣` 是**新指令**不是确认；`好/嗯/可以` 一律不触发（闲聊误触发会动机器）。
//   3. **没有待确认计划时说「执行」→ 什么都不做**，只提示"请先说做什么"。
//   4. **打断 = 步边界停止 + 回起点**（不是急停）。开场只说明一次。
//
// ★ AI 不是权限后门：全部接口走控制令牌；围栏/轴锁/速度/运行中互锁与手点页面一致。
// =====================================================================
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from "vue";
import MonitorLayout from "./MonitorLayout.vue";
import Icon from "./Icon.vue";
import CameraPanel from "./CameraPanel.vue";
import { useRobotStore } from "../stores/robot.js";
import { useAuthStore } from "../stores/auth.js";
import { useAiStore } from "../stores/ai.js";
import { useAsrStore } from "../stores/asr.js";
import { createAiChat, MSG } from "../ai/aiChat.js";
import { classifyUtterance, CONFIRM_WORDS } from "../ai/confirmWords.js";
import { isMuted, setMuted } from "../ai/speak.js";

const robot = useRobotStore();
const auth = useAuthStore();
const ai = useAiStore();
const asr = useAsrStore();
const chat = createAiChat();

// 快捷指令（★ 每条都来自后端 nl_intent 的真实能力，有回归脚本从后端源码解析断言）
//   ★ 放在**空状态**里（有消息就收起）—— 它就是"第一句话的灵感"，不是常驻工具栏。
const PRESET_GROUPS = [
  { k: "搬运", items: [
    "把A区第1行第1列放到B区红列",
    "把A区全部搬到B区",
    "把A区3个红色搬到B区",
    "B区全部矩形随机摆放回A区",
  ] },
  { k: "查询", items: ["A区第1行第1列是什么颜色"] },
  { k: "流程", items: ["开始分拣", "暂停", "继续", "停止执行"] },
  { k: "安全", items: ["急停"] },
];
const CONFIRM_HINT = CONFIRM_WORDS.slice(0, 3).join(" / ");

const msgs = computed(() => chat.messages.value);
const atBottom = ref(true);
const scroller = ref(null);

async function scrollBottom(force = false) {
  await nextTick();
  const el = scroller.value;
  if (el && (force || atBottom.value)) el.scrollTop = el.scrollHeight;
}
function onScroll() {
  const el = scroller.value;
  if (!el) return;
  atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
}

// ---------------------------------------------------------------- 语音 → 意图
async function handleUtterance(text, { fromVoice = false } = {}) {
  const t = String(text || "").trim();
  if (!t) return;
  void fromVoice;   // 语音进来的内容本身就是"我说的话"，气泡里直接显示，不再另挂一行原话
  const kind = classifyUtterance(t, { hasPendingPlan: chat.hasPendingPlan.value });
  chat.sayUser(t);
  scrollBottom(true);

  if (kind === "stop") {
    chat.sayAI("好，停止。当前动作结束、回到起点后生效。");
    await ai.cancel();
    return;
  }
  if (kind === "confirm") { await doExec(); return; }
  if (kind === "confirm_no_plan") {
    chat.sayWarn("还没有待执行的指令 —— 先说做什么。");
    return;
  }
  await doSend(t);
}

watch(() => asr.finalText, (t) => {
  if (t && asr.active) {
    ai.setAsrText(t);
    handleUtterance(t, { fromVoice: true });
    // ★ 语音命中即视为"已发送"：清空输入框，与打字发送（submitInput 里 ai.text = ""）
    //   行为一致 —— 否则说出去的话还留在输入栏，让人以为没发出去。
    ai.text = "";
  }
});

/**
 * 发送一句话（2026-10-10 对话化，用户口径："能正常聊天的大模型"）：
 * 后端 /ai/chat **指令先行** —— 是指令出计划卡（照旧回显 + 确认，ai.parsed 同步给 doExec）；
 * 不是指令 → 大模型自由回话（chat 气泡，左侧）。聊天只许说话，不碰控制。
 * 历史只带 user/ai/chat 的**文本**气泡（计划卡/进度/结果不进大模型上下文），最多 10 条。
 */
async function doSend(text) {
  const t = String(text ?? ai.text ?? "").trim();
  if (!t) { chat.sayWarn("先说一句话。"); return null; }
  ai.text = t;
  const hist = chat.messages.value
    .filter((m) => m.type === MSG.USER || m.type === MSG.AI || m.type === MSG.CHAT)
    .slice(-10)
    .map((m) => ({ role: m.type === MSG.USER ? "user" : "assistant",
                   content: String(m.text || "") }));
  const d = await ai.sendChat(t, hist);
  if (!d) {
    chat.sayAI(`没能理解：${ai.err || "请求失败"}`, { bad: true });
    scrollBottom(true);
    return null;
  }
  if (d.mode === "plan") {
    chat.sayPlan(d.parsed);
    ai.parsed = d.parsed;          // 「执行」按钮 / 确认词读的是 ai.parsed（plan_id 在里面）
  } else {
    chat.sayChat(d.reply);
  }
  scrollBottom(true);
  return d;
}

async function doExec() {
  const ok = await ai.exec(true);
  if (!ok) { chat.sayAI(`没能执行：${ai.err || "被拒绝"}`, { bad: true }); return; }
  chat.sayAI(ai.queued ? "已排队 —— 当前任务回起点后自动接着跑。" : "开始执行。");
  scrollBottom(true);
}

function submitInput() {
  const t = (ai.text || "").trim();
  if (!t) return;
  ai.parsed = null;
  handleUtterance(t);
  ai.text = "";
}
function usePreset(p) {
  ai.text = p; ai.parsed = null; ai.err = "";
  doSend(p);
}
function toggleMic() {
  if (asr.active || asr.connecting) asr.stop();
  else { asr.loadConfig(); asr.start(); }
}
function clearChat() {
  chat.clear();
  ai.parsed = null;
  ai.err = "";
}

// ---------------------------------------------------------------- 语音播报开关
const speakMuted = ref(isMuted());
function toggleSpeak() {
  speakMuted.value = !speakMuted.value;
  setMuted(speakMuted.value);
}

// ---------------------------------------------------------------- 空格键说话
function onGlobalKey(e) {
  const el = e.target;
  const typing = !!(el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA"
    || el.isContentEditable));
  if (typing) return;                       // ★ 输入框里空格照常输入
  if (e.code === "Space" || e.key === " ") { e.preventDefault(); toggleMic(); }
}
function onInputKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitInput(); }
}

// ---------------------------------------------------------------- 进度 → 气泡
watch([() => ai.st, () => ai.running, () => ai.queued], () => {
  const moved = chat.updateRun(ai.st || {}, ai.running, ai.queued);
  if (moved) scrollBottom();
}, { deep: false });

let _wasRunning = false;
watch(() => ai.running, (now) => {
  if (_wasRunning && !now) {
    const s = ai.st || {};
    if (s.finished) {
      const okDone = s.ok === true;
      chat.sayResult(okDone ? "完成。" : `${s.error || "未完成"}`, okDone);
      scrollBottom(true);
    }
  }
  _wasRunning = now;
});

onMounted(() => { ai.start(); window.addEventListener("keydown", onGlobalKey); });
onBeforeUnmount(() => { ai.stop(); window.removeEventListener("keydown", onGlobalKey); });
watch(() => robot.activeView, (v) => { if (v === "ai") ai.start(); else ai.stop(); });
// ★ 令牌到手/失效 → 自动启停轮询（store.start 已带令牌门槛，未授权时静默；
//   但已在 AI 页时需在令牌到位后补拉起一次，避免停在页上却一直不自刷新）
watch(() => auth.controlActive, (on) => {
  if (on && robot.activeView === "ai") { ai.start(); ai.loadConfig(); }
  else if (!on) ai.stop();
});

const stageLine = (m) => {
  const s = m.st || {};
  const bits = [s.stage || "—"];
  if (s.piece) bits.push(`${s.piece}/${s.total || 1} 件`);
  if (s.color) bits.push(`${s.color}${s.conf ? " " + Number(s.conf).toFixed(2) : ""}`);
  if (s.sim_color) bits.push("空跑未判色");
  if (s.target) bits.push("→ " + s.target);
  return bits.join(" · ");
};
</script>

<template>
  <MonitorLayout view="ai">
    <template #hud>
      <div class="exec-hud">
        AI 执行 · {{ ai.dryRun ? "空跑" : "真实下发" }} ·
        大模型 {{ ai.llmOn ? "已启用" : "未配置（规则解析）" }} ·
        <b>按空格说话</b>
      </div>
      <CameraPanel :views="['ai', 'live']" />
    </template>

    <!-- ================= 对话窗：撑满右侧栏整块，就是"一个完整对话框" =================
         ★ 一键就绪入口在底栏（任何页面都能点），本页右侧栏完全交给对话框，
           不再叠 RcReadyCard 那些工具栏卡片 —— 专注"自然语言 ↔ 执行"对话。
         ★ 直接就是对话框：不再包一层 `card` 盒子（去掉那颗盒子的边框/底色/圆角），
           右键栏即聊天窗本体，与右栏背景融为一体。 -->
    <div class="chatwin chatwin-full">
      <!-- ① 标题栏 -->
      <div class="cw-head">
        <Icon name="megaphone" :size="15" />
        <span class="cw-title">对话</span>
        <span class="cw-state" :class="{ live: asr.active }">
          <i class="cw-dot"></i>
          {{ asr.active ? "正在听…（再按空格停止）" : (ai.running ? "执行中" : "待命") }}
        </span>
        <!-- ★ 空跑开关移到对话区右侧（消息区旁）：切换见 hUD 顶栏（空跑/真实下发）。
             执行中禁止切换，避免跑到一半改口径。 -->
        <button class="cw-clear dry" :class="{ on: ai.dryRun }" :disabled="ai.running"
                :title="ai.dryRun ? '空跑：不下发运动、不判色。点击切回真实下发' : '真实下发。点击切到空跑（不下发运动、不判色）'"
                @click="ai.dryRun = !ai.dryRun">
          <Icon :name="ai.dryRun ? 'flask' : 'cpu'" :size="12" />
          {{ ai.dryRun ? "空跑" : "真实" }}
        </button>
        <!-- ★ 语音播报开关（动作/报错/颜色/到达都会念出来；现场嫌吵可静音） -->
        <button class="cw-clear" :class="{ mutedBtn: speakMuted }"
                :title="speakMuted ? '语音播报已静音，点这里恢复' : '语音播报开着，点这里静音'"
                @click="toggleSpeak">
          <Icon :name="speakMuted ? 'micOff' : 'volume'" :size="12" />
          {{ speakMuted ? "已静音" : "播报" }}
        </button>
        <button class="cw-clear" :disabled="!msgs.length" title="清空对话" @click="clearChat">
          <Icon name="eraser" :size="12" /> 清除
        </button>
      </div>

      <!-- ② 消息区（撑满剩余高度） -->
      <div ref="scroller" class="cw-body" @scroll="onScroll">
        <!-- 空状态：**一行提示 + 快捷胶囊**（照参考图；有消息就收起） -->
        <div v-if="!msgs.length" class="cw-empty">
          <div class="cw-hi">用一句话描述要做的事，我来摊开执行计划并执行</div>
          <div class="cw-presets">
            <div v-for="g in PRESET_GROUPS" :key="g.k" class="cw-pg">
              <button v-for="p in g.items" :key="p" class="cw-chip" :disabled="ai.busy"
                      @click="usePreset(p)">{{ p }}</button>
            </div>
          </div>
          <div class="cw-tip">
            我会先给出执行计划，说「{{ CONFIRM_HINT }}」就开跑；执行中说「停」或直接下新指令，
            都会等当前动作跑完、回到起点后再执行。
          </div>
        </div>

        <!-- 消息流 -->
        <div v-for="m in msgs" :key="m.id" class="row" :class="m.type">
          <div class="ava">
            <Icon :name="m.type === MSG.USER ? 'user' : 'robot'" :size="13" />
          </div>
          <div class="bub" :class="{ bad: m.bad }">
            <template v-if="m.type === MSG.PLAN">
              <div class="plan-t">
                {{ (m.parsed && m.parsed.echo && m.parsed.echo.title) || "我理解为" }}
                <span class="muted">· {{ (m.parsed && m.parsed.echo && m.parsed.echo.detail) || "" }}</span>
              </div>
              <div v-if="m.parsed && m.parsed.echo && m.parsed.echo.note" class="plan-n">
                {{ m.parsed.echo.note }}
              </div>
              <ul v-if="m.parsed && m.parsed.warnings && m.parsed.warnings.length" class="warns">
                <li v-for="(w, i) in m.parsed.warnings" :key="i">{{ w }}</li>
              </ul>
              <div class="plan-b">
                <button class="primary" :disabled="!ai.canExec" @click="doExec">
                  <Icon name="play" :size="12" /> 执行
                </button>
                <span class="muted">或说「执行」</span>
              </div>
            </template>

            <template v-else-if="m.type === MSG.RUN">
              <div class="run">{{ m.running ? "▶ " : "" }}{{ stageLine(m) }}</div>
              <div v-if="m.queued" class="queued">
                <Icon name="refresh" :size="12" /> 已排队：{{ m.queued.raw }}
              </div>
            </template>

            <template v-else>{{ m.text }}</template>
          </div>
        </div>
      </div>

      <!-- ③ 输入栏（**固定在底部**。textarea 固定 75px 高：Enter 发送 / Shift+Enter 换行） -->
      <div class="cw-input">
        <textarea class="cw-txt" v-model="ai.text" :disabled="ai.busy" rows="2"
               placeholder="输入指令或随便聊点什么，例如：把A区第1行第1列放到B区红列（Enter 发送 / Shift+Enter 换行）"
               @keydown="onInputKey"></textarea>
        <!-- ★ 右侧操作：麦 + 发送/停止 改为**上下垂直摆放** -->
        <div class="cw-side">
          <button class="cw-ic" :class="{ on: asr.active }" @click="toggleMic"
                  :title="asr.active ? '停止语音（或按空格）' : '开始语音（或按空格）'">
            <Icon name="mic" :size="15" />
          </button>
          <!-- ★★ 发送/停止**同一个按钮**（照聊天应用惯例，用户口径）：
               · 没在跑 → 发送（➤）
               · 正在执行/已排队 → **停止**（■，红色）—— 位置不变，手不用挪 -->
          <button v-if="!ai.running" class="cw-send" :disabled="ai.busy || !ai.text" title="发送（Enter）"
                  @click="submitInput">
            <Icon name="chevronRight" :size="15" />
          </button>
          <button v-else class="cw-send stop" title="停止执行" @click="ai.cancel()">
            <Icon name="stop" :size="14" />
          </button>
        </div>
      </div>
    </div>
  </MonitorLayout>
</template>

<style scoped>
/* ===================== 对话窗：标题栏 + 消息区 + 底部输入栏 ===================== */
/* 2026-10-09 二次改版：照用户给的参考图做成**聊天窗**——
   消息区撑满、输入栏固定底部、快捷指令只在空状态出现。
   上一版把指令提示排在输入框下方、开场白又长，不是聊天窗的样子。 */
.chatwin { display: flex; flex-direction: column;
  /* ★ 用户口径："给盒子限制高度，不要超出一定的高度，添加滚动条"。
     用固定高度（clamp：视口的 52%，夹在 360~520px 之间）—— 比只写 max-height
     更明确，不会因为消息多把整页撑长。 */
  height: clamp(360px, 52vh, 520px); padding: 0; overflow: hidden; }
/* ★ AI 页用：右侧栏整块 = 一个对话框（去掉 RcReadyCard 堆叠后撑满剩余高度）。
   底栏 .side 是 flex 列容器，卡片默认 flex:none —— 这里覆盖成 flex:1 让它吃满
   剩余高度，消息区自动铺开，标题/输入栏钉在顶/底。
   ★ 2026-10-09 不再包 `.card`：去掉了外层盒子的边框/底色/圆角/圆角投影，
     让对话框就是右侧栏本体，背景透出右栏底色。 */
.chatwin-full { flex: 1; min-height: 0; height: auto; border: none;
  background: transparent; border-radius: 0; box-shadow: none; }

/* ① 标题栏 */
.cw-head { display: flex; align-items: center; gap: 7px;
  padding: 9px 10px; border-bottom: 1px solid var(--line); }
.cw-title { font-size: 14px; color: var(--txt); }
.cw-state { display: inline-flex; align-items: center; gap: 5px;
  margin-left: auto; font-size: 11.5px; color: var(--muted); }
.cw-dot { width: 6px; height: 6px; border-radius: 50%;
  background: var(--ok, #2f9e6f); }
.cw-state.live .cw-dot { background: var(--err, #e05252); animation: cwpulse 1s infinite; }
@keyframes cwpulse { 50% { opacity: .3; } }
.cw-clear { font-size: 11.5px; display: inline-flex; align-items: center; gap: 4px; }
/* ★ 空跑开关（移到对话区右侧）：空跑时青色高亮，一眼看出当前口径 */
.cw-clear.dry.on { color: var(--accent); border-color: var(--accent-line);
  background: var(--accent-soft); }

/* ② 消息区：撑满，可滚。内边距压到 10px 以内（用户口径：gap/padding 一律 ≤10px） */
.cw-body { flex: 1; min-height: 0; overflow-y: auto;
  padding: 10px; display: flex; flex-direction: column; gap: 9px; }
.cw-body::-webkit-scrollbar { width: 8px; }
.cw-body::-webkit-scrollbar-track { background: transparent; }
.cw-body::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, .14);
  border-radius: 4px; }
.cw-body::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, .24); }

/* 空状态：一行提示 + 横排胶囊（照参考图，按能力分组但**不显示组名**——那是菜单不是聊天） */
.cw-empty { margin: auto 0; padding: 8px 2px; }
.cw-hi { font-size: 13.5px; color: var(--txt); }
.cw-presets { margin-top: 14px; display: flex; flex-direction: column; gap: 8px; }
.cw-pg { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.cw-chip { font-size: 12px; padding: 5px 11px; border-radius: 14px;
  border: 1px solid var(--line); background: transparent; color: var(--muted);
  cursor: pointer; transition: color .15s, border-color .15s; }
.cw-chip:hover { color: var(--accent); border-color: var(--accent); }
/* 规则说明放在胶囊**下面**（不占主视觉，但需要时可读到） */
.cw-tip { margin-top: 16px; font-size: 11.5px; line-height: 1.7; color: var(--muted); }

/* ③ 输入栏：固定在底部 */
/* 输入栏：textarea **固定 75px 高**（用户口径）。Enter 发送 / Shift+Enter 换行。
   ★ 上下内边距压到 5px（用户口径）：与消息区一起收得更紧、更利落。 */
.cw-input { display: flex; align-items: flex-end; gap: 7px;
  padding: 5px 10px; border-top: 1px solid var(--line); }
/* 输入栏：textarea **固定 75px 高**（用户口径），不用再自动增高。
   Enter 发送 / Shift+Enter 换行。 */
.cw-txt { flex: 1; min-width: 0; padding: 7px 10px; font-size: 13px;
  line-height: 1.5; border-radius: 12px; border: 1px solid var(--line);
  background: rgba(255, 255, 255, .04); color: var(--txt);
  resize: none; overflow-y: auto; height: 75px; }
/* ★ 右侧操作：麦 + 发送/停止 **垂直摆放**（用户口径）。列贴着 textarea 底部对齐。 */
.cw-side { flex: none; display: flex; flex-direction: column; gap: 7px; align-items: center; }
.cw-side .cw-ic { width: 30px; height: 30px; }
.cw-side .cw-send { width: 30px; height: 30px; }
.cw-txt::placeholder { color: var(--muted); }
.cw-txt:focus { outline: none; border-color: var(--accent); }
.cw-ic { width: 34px; height: 34px; border-radius: 50%; flex: none;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--line); background: transparent; color: var(--muted); }
.cw-ic:hover { color: var(--txt); border-color: var(--accent); }
.cw-ic.on { background: var(--err, #e05252); border-color: var(--err, #e05252); color: #fff; }
.cw-send { width: 34px; height: 34px; border-radius: 50%; flex: none;
  display: flex; align-items: center; justify-content: center;
  border: none; background: var(--accent); color: #fff; }
.cw-send:disabled { opacity: .35; }
/* 停止态（同一个按钮的另一种状态）：红底白图标 —— 与"发送"一眼区分开 */
.cw-send.stop { background: var(--err, #e05252); }
.cw-send.stop:hover { filter: brightness(1.1); }

/* ===================== 消息气泡（AI 左 / 我右） =====================
   2026-10-09 用户口径："对话我要**气泡的效果**" —— 按即时通讯的通用形态做：
   · 头像（AI = 机器人图标，我 = user 图标）
   · 气泡圆角做成"不对称"：靠头像一侧小圆角，另一侧大圆角 —— 这才像聊天
   · 我的气泡用主色底 + 白字（一眼分清谁说的）；AI 用面板色
   · 行间距拉开（消息之间 10px），气泡内 7/11 */
/* ★ 2026-10-10 修复"气泡贴左、头像贴右的大空隙"（用户截图标注）：
   index.html 的全局 `.row { justify-content: space-between }` 穿透命中了聊天行 ——
   用户行是 row-reverse，被 space-between 拉成"气泡贴最左、头像贴最右"。
   scoped 规则 (.row[data-v-…]) 特异性高于全局 (.row)，这里显式收回 flex-start：
   AI 行 [头像][气泡] 贴左；用户行 row-reverse 从右起排 [头像][气泡] 贴右。 */
.row { display: flex; gap: 7px; align-items: flex-start; justify-content: flex-start; }
.row .ava { flex: none; width: 26px; height: 26px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: rgba(255, 255, 255, .07); color: var(--muted); }
.row .bub { max-width: 76%; font-size: 13px; line-height: 1.6;
  padding: 8px 10px; border-radius: 4px 14px 14px 14px;
  background: rgba(255, 255, 255, .06); border: 1px solid var(--line); }
.row.user { flex-direction: row-reverse; }
.row.user .bub { border-radius: 14px 4px 14px 14px;
  background: var(--accent); border-color: var(--accent); color: #fff; }
.row.user .ava { background: rgba(79, 168, 255, .18); color: var(--accent); }
.row.warn .bub { background: rgba(212, 136, 6, .13); border-color: rgba(212, 136, 6, .45);
  color: #e0a83a; }
.row.result .bub { background: rgba(47, 158, 111, .12); border-color: rgba(47, 158, 111, .4); }
.row.result .bub.bad, .row .bub.bad { background: rgba(224, 82, 82, .12);
  border-color: rgba(224, 82, 82, .45); color: var(--err, #e05252); }
/* 我的气泡是主色底 → 里面的弱化文字要变白（不然看不清） */
.row.user .bub .muted { color: rgba(255, 255, 255, .75); }

.plan-t { font-weight: 700; color: var(--txt); font-size: 13px; }
.plan-n { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
.plan-b { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.warns { margin: 4px 0 0; padding-left: 16px; font-size: 11.5px; color: var(--warn, #d48806); }
.run { font-size: 12.5px; }
.queued { display: flex; align-items: center; gap: 5px; margin-top: 4px;
  font-size: 12px; color: var(--accent); }

.mini-btn { font-size: 11px; }
.muted { color: var(--muted); }
</style>
