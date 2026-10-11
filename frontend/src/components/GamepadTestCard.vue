<script setup>
// =====================================================================
// 手柄测试盒（分拣页右侧栏，可折叠）—— 井字棋手柄输入的自检工具
//
// ★ 为什么要有它：手柄这东西"接上了没 / 哪个键对应哪个动作 / 到底触发了没有"，
//   现场肉眼很难判断（浏览器还要求先按一次键才会唤醒手柄上报）。这个盒子把三件事摊开：
//     ① 连接状态：**有手柄就显示「手柄已连接」**，还给出型号与是否 standard 映射
//     ② 实时读数：当前按下的键（A/↑/Menu…）+ 左摇杆 X/Y —— 现场对号入座
//     ③ 模拟触发：点按钮 = 按手柄（走**完全相同**的广播路径），
//        于是没接手柄也能验证"这个动作会触发什么条件"（比如现在是不是该你落子）
//   再加一份「按键 → 动作」对照表，等于现场的使用说明。
//
// ★ 模拟按钮走 gamepad.js 的 simulateAction —— 它不是"另抄一套逻辑"，
//   而是与真手柄按键汇入同一个 emit，测出来的就是真行为。
// =====================================================================
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import Icon from "./Icon.vue";
import { apiUrl } from "../config.js";
import {
  ACTION_CN, BUTTON_CN, readPad, simulateAction, subscribeGamepad,
  onGamepadConnection, enableBackendFallback,
} from "../games/gamepad.js";

const open = ref(false);                 // 默认收起（现场先看"连上没"，要细看再展开）
/** 实时快照（未连接时也安全：connected=false）。 */
const pad = ref({ connected: false, id: "", mapping: "", buttons: [], axes: [0, 0],
                  count: 0, ids: [], source: "none", backendAvailable: null, idleSec: null });
const log = ref([]);                     // 最近触发的动作
const MAX_LOG = 8;

/** 模拟触发的按钮（覆盖全部 10 个逻辑动作）。 */
const SIMS = [
  { a: "up", cn: "↑" }, { a: "down", cn: "↓" },
  { a: "left", cn: "←" }, { a: "right", cn: "→" },
  { a: "confirm", cn: "A 落子" }, { a: "start", cn: "Start 开始" },
  { a: "pause", cn: "X 暂停" }, { a: "stop", cn: "B 停止" },
  { a: "replay", cn: "Y 复盘" }, { a: "reset", cn: "View 复位" },
];

/** 按键 → 动作 对照（现场使用说明）。 */
const MAP_ROWS = [
  { k: "左摇杆 / 十字键", v: "移动光标" },
  { k: "右摇杆 ↑", v: "下棋（落子）" },
  { k: "右摇杆 →", v: "暂停 ⇄ 继续" },
  { k: "右摇杆 ↓", v: "停止（作废）" },
  { k: "右摇杆 ←", v: "复盘 ⇄ 退出" },
  { k: "A", v: "落子（同右摇杆↑）" },
  { k: "Start", v: "开始" },
  { k: "X", v: "暂停 ⇄ 继续" },
  { k: "B", v: "停止（作废）" },
  { k: "Y", v: "复盘 ⇄ 退出" },
  { k: "View ×2", v: "复位" },
];

// ★ 200ms 常轮询（收起也刷）：状态行/来源行要一直是最新的，现场才敢信
let timer = null;
function poll() { pad.value = readPad(); }
function startPoll() { if (!timer) { poll(); timer = setInterval(poll, 200); } }
function stopPoll() { if (timer) { clearInterval(timer); timer = null; } }

/** 键帽灯条要摆的键（标准布局下标）——按下即亮，就是"按哪个亮哪个"。 */
const KEYCAPS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];

/** 记录一次动作（真手柄按键与模拟按钮都经这里，来源一致）。 */
const flash = ref("");        // 刚触发的动作 → 对应的模拟按钮闪一下
let flashTimer = null;
function onAction(a) {
  log.value = [{ at: new Date().toLocaleTimeString(), action: a, cn: ACTION_CN[a] || a }]
    .concat(log.value).slice(0, MAX_LOG);
  flash.value = a;
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { flash.value = ""; }, 650);
}
function onSim(a) { simulateAction(a); }

/** 重新扫描：立刻读一次并写进日志 —— 现场按了手柄没反应时，点它就知道"浏览器到底看见了几个"。 */
function rescan() {
  poll();
  const n = pad.value.count || 0;
  log.value = [{
    at: new Date().toLocaleTimeString(), action: "rescan",
    cn: pad.value.connected
      ? ("已识别 " + n + " 个手柄：" + (pad.value.id || "?"))
      : ("浏览器可见手柄 " + n + " 个 —— 未识别到（先按手柄任意键 / 见下方排查）"),
  }].concat(log.value).slice(0, MAX_LOG);
}

/**
 * 诊断文本（一行行可读，可一键复制）。
 * ★ 为什么做这个：现场排查往往要把"浏览器到底看到什么"发出来，
 *   而 F12 控制台会弹「禁止粘贴」的自我防护提示（Chrome 的 self-XSS 保护），
 *   对手不熟的人很卡。这里把同样的信息直接摆到界面上，点一下就能复制。
 */
function diagLines() {
  const n = pad.value.count || 0;
  const apiOk = typeof navigator !== "undefined"
    && typeof navigator.getGamepads === "function";
  return [
    "手柄诊断 " + new Date().toLocaleString(),
    "浏览器: " + (typeof navigator !== "undefined" ? navigator.userAgent : "-"),
    "Gamepad API: " + (apiOk ? "可用" : "不可用（多为内嵌浏览器，请用 Chrome/Edge 打开）"),
    "代码判定: " + (pad.value.connected ? "已连接" : "未连接"),
    "输入来源: " + sourceText.value,
    "后端可读(XInput): " + (pad.value.backendAvailable === null
      ? "未知" : (pad.value.backendAvailable ? "是" : "否")),
    "距上次真实输入: " + (pad.value.idleSec === null
      ? "从未收到（设备在、数据不动）" : pad.value.idleSec + " 秒前"),
    "浏览器可见手柄数: " + n,
    "设备: " + ((pad.value.ids || []).join(" | ") || "（无）"),
    "当前按键: " + ((pad.value.buttons || []).map((i) => BUTTON_CN[i] || i).join(" ") || "（无）"),
  ];
}
const diagText = computed(() => diagLines().join("\n"));

const copyMsg = ref("");
async function copyDiag() {
  poll();
  const text = diagLines().join("\n");
  let ok = false;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok) {                        // 回退：临时 textarea + execCommand（老浏览器/无剪贴板权限时）
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (e) { ok = false; }
  }
  copyMsg.value = ok ? "已复制，可直接发我" : "复制失败 —— 请选中下方诊断文本手动复制";
  log.value = [{
    at: new Date().toLocaleTimeString(), action: "diag",
    cn: "可见 " + (pad.value.count || 0) + " 个 · " + (ok ? "已复制" : "复制失败"),
  }].concat(log.value).slice(0, MAX_LOG);
}

let unsubPad = null;
let unsubConn = null;
onMounted(() => {
  // ★ 先开后端回退，再订阅：浏览器读不到手柄时（本项目现场就是这样），
  //   前端会轮询 /api/gamepad/state，由**后端用 XInput 直读**把按键喂进来。
  enableBackendFallback(apiUrl("/gamepad/state"));
  startPoll();                                     // 收起也刷，状态/来源行一直可信
  unsubPad = subscribeGamepad(onAction);
  unsubConn = onGamepadConnection(() => poll());   // 连接变化 → 立刻刷新状态行
});
onBeforeUnmount(() => {
  stopPoll();
  if (flashTimer) clearTimeout(flashTimer);
  if (unsubPad) unsubPad();
  if (unsubConn) unsubConn();
});

/** 当前亮着的键（含 400ms 余晖，短按也看得见）→ 键帽灯条用。 */
const litSet = computed(() => new Set(pad.value.buttons || []));

/** 输入来源的中文说明（现场一眼看出"谁在驱动"）。 */
const sourceText = computed(() => {
  const s = pad.value.source;
  if (s === "browser") return "浏览器";
  if (s === "backend") return "后端（XInput 直读）";
  if (pad.value.backendAvailable === false) return "无（后端也读不到：非 Windows/无 XInput）";
  return "无";
});

const btnText = computed(() => (pad.value.buttons || [])
  .map((i) => BUTTON_CN[i] || ("#" + i)).join(" ") || "—");
const axText = computed(() => {
  const a = pad.value.axes || [];
  const f = (v) => (Number(v) >= 0 ? "+" : "") + Number(v || 0).toFixed(2);
  return "X " + f(a[0]) + "   Y " + f(a[1]);
});
const simTitle = "点这里 = 按手柄（走同一套广播，用来验证会触发什么条件）";
</script>

<template>
  <div class="card gpt-card">
    <h3>
      <Icon name="gamepad" :size="15" /> 手柄测试
      <button class="fold" @click="open = !open">{{ open ? "收起" : "展开" }}</button>
    </h3>

    <!-- ★ 连接状态：有手柄就显示「手柄已连接」 -->
    <div class="gpt-row">
      <span class="k">状态</span>
      <span class="v stat" :class="{ on: pad.connected }">
        <i class="dot"></i>{{ pad.connected ? "手柄已连接" : "未检测到手柄" }}
      </span>
    </div>
    <!-- ★ 输入来源：浏览器 / 后端（XInput 直读）—— 现场一眼看出"到底谁在驱动" -->
    <div class="gpt-row">
      <span class="k">来源</span>
      <span class="v" :class="{ on: pad.connected }">{{ sourceText }}</span>
    </div>
    <!-- ★ S6：收起态只留「状态 / 来源」两行；其余（可见数、型号、排查清单、诊断文本）
         全部收进"展开才渲染"（v-if="open"）—— 原来收起也渲染整段清单与长诊断，撑高卡片。 -->
    <template v-if="open">
    <!-- ★ 「浏览器到底看见几个」——排查手柄问题的第一现场数据 -->
    <div class="gpt-row">
      <span class="k">可见</span>
      <span class="v">浏览器可见手柄 <b>{{ pad.count || 0 }}</b> 个{{ pad.count ? "：" + (pad.ids || []).join(" / ") : "" }}</span>
    </div>
    <div v-if="pad.connected" class="gpt-row">
      <span class="k">型号</span>
      <span class="v mono">{{ pad.id || "—" }}</span>
    </div>

    <!-- ★ 「连了却不动」的准确提示：设备在，但一直收不到输入（现场最常见：接收器没配对） -->
    <div v-if="pad.connected && (pad.idleSec === null || pad.idleSec > 6)" class="gpt-warn">
      <Icon name="alert" :size="12" />
      <span>
        已连接，但<b>{{ pad.idleSec === null ? "从未" : Math.round(pad.idleSec) + " 秒" }}</b>收不到任何输入 ——
        系统认到设备、数据却不动。多半是<b>接收器插着但手柄没配对/没开机</b>，或手柄不在 X 模式。
        <br>验证：<b>Win+R → joy.cpl → 双击设备 → 属性 → 测试</b> —— 在那里按手柄若也不动，就是手柄/接收器一侧的问题
        （重新配对 / 插 USB 线直连对比 / 换 USB 口）。
      </span>
    </div>

    <!-- ★ 检测不到时的排查清单（展开才显示；含"重新扫描"即时反馈） -->
    <div v-if="!pad.connected" class="gpt-diag">
      <div class="gpt-sub">
        检测不到？按顺序排查
        <button class="sim rescan" @click="rescan">重新扫描</button>
        <button class="sim" @click="copyDiag">复制诊断</button>
      </div>
      <div v-if="copyMsg" class="gpt-copymsg">{{ copyMsg }}</div>
      <!-- 诊断文本直接摆出来：不用碰 F12 控制台（那边有"禁止粘贴"自我防护提示） -->
      <pre class="diag-pre">{{ diagText }}</pre>
      <ol class="diag-list">
        <li><b>先看上面的「来源」</b>：显示“后端（XInput 直读）”= 后端已读到，浏览器认不到也能玩，下面的都不用管</li>
        <li><b>先按一下手柄任意键</b>，并点一下本页面 —— 浏览器只在页面获得焦点、且手柄有按键后才会上报</li>
        <li>用 <b>Chrome / Edge</b> 打开；应用内嵌的预览窗 / 简易浏览器通常不提供手柄接口</li>
        <li><b>关掉 Steam / 虚拟手柄软件</b>（DS4Windows、ViGEm、input-remapper 等会独占 XInput 手柄，浏览器就读不到）</li>
        <li>Windows 按 <b>Win+R → 输入 joy.cpl</b>：列表里看不到它 = 系统没认到（装驱动 / 换个 USB 口）</li>
        <li>手柄切到 <b>XInput 模式</b>：不少国产手柄默认 DInput，Chrome 不识别（找模式开关或组合键）</li>
        <li>蓝牙手柄：先在系统里连好，再拔插 / 重连一次，然后重开本页面</li>
      </ol>
    </div>
    </template>

    <template v-if="open">
      <div class="gpt-row">
        <span class="k">映射</span>
        <span class="v">{{ pad.mapping === "standard" ? "standard（标准布局）" : (pad.mapping || "—") }}</span>
      </div>
      <div class="gpt-row"><span class="k">按键</span>
        <span class="v mono hl">{{ btnText }}</span></div>
      <!-- ★ 键帽灯条：按哪个亮哪个（含 400ms 余晖，短按也看得见） -->
      <div class="caps">
        <span v-for="i in KEYCAPS" :key="i" class="cap" :class="{ lit: litSet.has(i) }">
          {{ BUTTON_CN[i] }}
        </span>
      </div>
      <div class="gpt-row"><span class="k">摇杆</span>
        <span class="v mono">{{ axText }}</span></div>

      <div class="gpt-hr"></div>
      <div class="gpt-sub">模拟触发 —— 点按钮 = 按手柄（验证会触发什么条件）</div>
      <div class="gpt-sims">
        <button v-for="s in SIMS" :key="s.a" class="sim" :class="{ lit: flash === s.a }"
                :title="simTitle" @click="onSim(s.a)">{{ s.cn }}</button>
      </div>

      <div class="gpt-hr"></div>
      <div class="gpt-sub">最近动作</div>
      <div v-if="!log.length" class="gpt-empty">还没有动作 —— 按手柄，或点上面的模拟按钮</div>
      <div v-for="(l, i) in log" :key="i" class="gpt-log">
        <span class="t mono">{{ l.at }}</span>
        <span class="a">{{ l.action }}</span>
        <span class="cn">{{ l.cn }}</span>
      </div>

      <div class="gpt-hr"></div>
      <div class="gpt-sub">按键 → 动作 对照</div>
      <div v-for="r in MAP_ROWS" :key="r.k" class="gpt-row">
        <span class="k2">{{ r.k }}</span><span class="v">{{ r.v }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.gpt-card h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 6px;
  font-size: 14px; color: var(--txt); }
.gpt-card h3 svg { color: var(--accent); }
.fold { margin-left: auto; padding: 2px 8px; font-size: 11px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel2); color: var(--muted); cursor: pointer; }
.fold:hover { border-color: var(--accent); color: var(--accent); }
.gpt-row { display: flex; gap: 8px; font-size: 12px; padding: 1px 0; align-items: center; }
.gpt-row .k { width: 44px; color: var(--muted); flex: none; }
.gpt-row .k2 { width: 92px; color: var(--muted); flex: none; }
.gpt-row .v { color: var(--txt); }
.gpt-row .v.mono { font-family: var(--mono); }
.gpt-row .v.hl { color: var(--accent); }
.gpt-row .v.stat { color: var(--muted); }
.gpt-row .v.stat.on { color: var(--accent); font-weight: 600; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  margin-right: 5px; background: var(--muted); vertical-align: middle; }
.gpt-row .v.stat.on .dot { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.gpt-hr { height: 1px; background: var(--line); margin: 6px 0; }
/* ★ 排查清单：检测不到时常显，现场照着逐条对 */
.gpt-diag { margin-top: 6px; }
.gpt-diag .gpt-sub { display: flex; align-items: center; gap: 6px; }
.rescan { margin-left: auto; }
.gpt-row .v b { color: var(--accent); }
.diag-list { margin: 5px 0 0; padding-left: 16px; font-size: 11px;
  color: var(--muted); line-height: 1.5; }
.diag-list b { color: var(--txt); }
/* ★ 诊断文本：可选可读，点「复制诊断」一键带走（绕开控制台的粘贴限制） */
.gpt-copymsg { margin-top: 4px; font-size: 11px; color: var(--accent); }
.diag-pre { margin: 5px 0 0; padding: 5px 6px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--panel2); color: var(--muted); font-family: var(--mono);
  font-size: 10.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  user-select: text; max-height: 120px; overflow: auto; }
.gpt-sub { font-size: 11px; color: var(--muted); }
/* ★ "连了却不动"警告（琥珀色，与"暂停"同色系）—— 设备在、却收不到任何输入 */
.gpt-warn { display: flex; gap: 5px; align-items: flex-start; margin-top: 6px;
  padding: 5px 6px; border: 1px solid rgba(255, 176, 32, .45); border-radius: 6px;
  background: rgba(255, 176, 32, .10); color: #ffc861; font-size: 11px; line-height: 1.5; }
.gpt-warn svg { flex: none; margin-top: 1px; }
.gpt-warn b { color: #ffd98a; }
.gpt-sims { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px; }
/* ★ 键帽灯条：按哪个亮哪个（含 400ms 余晖，短按也看得见） */
.caps { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
.cap { min-width: 26px; padding: 2px 6px; text-align: center; font-size: 11px;
  border: 1px solid var(--line); border-radius: 5px; background: var(--panel2);
  color: var(--muted); font-family: var(--mono); transition: all .1s ease; }
.cap.lit { background: var(--accent-soft2); border-color: var(--accent); color: var(--accent);
  box-shadow: 0 0 8px var(--accent-soft2); }
/* 触发的动作 → 对应模拟按钮闪一下（真手柄按键也会闪，一眼看出"到底触发了啥"） */
.sim.lit { border-color: var(--accent); color: var(--accent);
  background: var(--accent-soft2); box-shadow: 0 0 8px var(--accent-soft2); }
.sim { padding: 3px 8px; font-size: 12px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--panel2); color: var(--txt); cursor: pointer; }
.sim:hover { border-color: var(--accent); color: var(--accent); }
.gpt-log { display: flex; gap: 6px; font-size: 11.5px; padding: 1px 0; align-items: center; }
.gpt-log .t { color: var(--muted); }
.gpt-log .a { color: var(--accent); min-width: 62px; font-family: var(--mono); }
.gpt-log .cn { color: var(--txt); }
.gpt-empty { font-size: 11.5px; color: var(--muted); padding: 2px 0; }
</style>
