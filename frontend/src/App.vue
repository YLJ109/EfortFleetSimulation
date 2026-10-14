﻿<script setup>
// 根组件：Tab 栏 + 三视图（v-show 保持 3D 场景存活）+ 全局渲染循环 + 安全围栏报警。
//
// 三视图共用 MonitorLayout 模板 —— 同一个 scene、同一张 canvas、同一套模型。
import { onMounted, onBeforeUnmount, ref, computed, watch, nextTick } from "vue";
import { useRobotStore } from "./stores/robot.js";
import { useSafetyStore } from "./stores/safety.js";
import { useUiStore } from "./stores/ui.js";
import {
  switchView, renderFrame, resizeView, onSafety, applyRobotPose, disposeAll, onGlState,
  startIdleSafetyEval, stopIdleSafetyEval,
} from "./three/manager.js";
import Icon from "./components/Icon.vue";
import { useAuthStore } from "./stores/auth.js";
// ★ 速度边界来自**唯一来源**（core/safetyConst.js，与后端 safety_const.py 同口径）。
//   以前这里写死 min="1"、而 store 里写死 5 —— 两处会各自漂移（就出过
//   "滑块能拖到 1%，后端却按 5% 跑"的事故）。现在都引用常量，只有一个地方能改。
import { SPEED_MIN, SPEED_MAX, SPEED_DEV_TOL } from "./core/safetyConst.js";
import { useAsrStore } from "./stores/asr.js";
import { useCameraStore } from "./stores/camera.js";
import RealMonitor from "./components/RealMonitor.vue";
import SimMonitor from "./components/SimMonitor.vue";
import PointExecView from "./components/PointExecView.vue";
import ProgramExecView from "./components/ProgramExecView.vue";
import EventsView from "./components/EventsView.vue";
import SortingView from "./components/SortingView.vue";
import PadExecView from "./components/PadExecView.vue";   // ★ 手柄执行页（2D 选点 + 气路 + 井字棋）
import AiControlView from "./components/AiControlView.vue";
import SettingsView from "./components/SettingsView.vue";
import AboutView from "./components/AboutView.vue";
import AnnouncePanel from "./components/AnnouncePanel.vue";
import GuideBar from "./components/GuideBar.vue";
import StatusStrip from "./components/StatusStrip.vue";
import { announce, unlockAudio, annCfg } from "./services/announcer.js";
import { BRAND, brandSubtitle } from "./brand.js";
import {
  safetyText as safetyTextOf, safetySub as safetySubOf,
} from "./utils/safetyLabels.js";
import { useLinkStore } from "./stores/link.js";
import { useExecStore } from "./stores/exec.js";
import { useRcReadyStore, NOT_READY_HINT } from "./stores/rcReady.js";
import { useSorting3dStore } from "./stores/sorting3d.js";
import { useMetricsStore } from "./stores/metrics.js";   // ★ B1：后端进程级指标（只读轮询）
// ★ C3：关键参数的短趋势 = **纯前端环形缓冲**（不新增后端存储；方案 §C3「不做什么」）。
import { sample as trendSample } from "./utils/paramTrend.js";
import {
  resolveKey, isTypingTarget, tabKeyFor, SHORTCUT_BY_ID,
} from "./shortcuts.js";
import {
  HOME_VIEW, isKnownView, safeView,
  visibleTabs as visibleTabsOf, allowedKeys as allowedKeysOf,
  navTabs as navTabsOf,
} from "./tabs.js";
import { wsEventsUrl } from "./config.js";
import { apiControl } from "./net/control.js";   // ★ P0-3：围栏上报需带控制令牌

const robot = useRobotStore();
const safe = useSafetyStore();
const ui = useUiStore();
const cam = useCameraStore();   // ★ 2026-10-11 口径：相机/实时捕捉**默认关**，拿到控制权限后自动开
// 链路自检（机器人/摄像头/示教器档位）：★ 由本组件做**唯一**的启停者，
// 底栏状态灯与引导条都只是它的消费者 —— 两个轮询者必然出现自相矛盾的界面。
const link = useLinkStore();
// ★ B1：后端进程级指标（GET /api/system/metrics，只读、不需令牌）。
//   启停同样收敛在本组件（与 link 同一个闸门）—— 两个轮询者必然出现自相矛盾的界面。
const metrics = useMetricsStore();

// ★ 2026-10-11：真机链路面板已并入底部状态区（StatusStrip 的「真机链路」片，
//   面板内容仍是 RcReadyCard）—— 原来独立的 showLinkPanel / 切换按钮 / LinkPanel 浮窗已移除。
//   ★ 同日第二轮：底部原「机器人」片与「真机链路」片**合并为一个「真机链路」片**
//     （用户原话："底部有两个真机链路两个合并掉"），机器人链路内容迁入其面板。

// ★ 权限相关的页面，没拿到权限时**不显示入口**（定义与门控逻辑见 src/tabs.js）：
//   - needAuth  (点位执行 / 程序执行)：要有控制令牌（管理员或操作员都行）。
//     理由：这两页的全部按钮在无令牌时都是灰的，摆在那里只会让现场以为是"坏了"。
//   - needAdmin (运维审计)：要有**管理员**令牌。里面是改围栏配置、清审计日志、
//     导入备份、回滚围栏版本这类高风险动作，操作员点进去只会看到一串 403。
//   拿到权限后自动出现，不需要刷新页面；权限收回时自动隐藏并退回真实监控。
//   门控逻辑抽在 tabs.js 里是为了能在 node 里断言（tools/verify_tabs.mjs）。

const RANK = { safe: 0, warn: 1, danger: 2, hit: 3 };

// 安全围栏状态（由 three 管理器评估，状态或余量变化时才回调）
const safety = ref({ state: "safe", ratio: 1, clearance: 0, zoneId: "", zoneName: "" });

// ★ 文案全部抽到 utils/safetyLabels.js（纯函数，可 headless 断言）：
//   顶部报警条与视口右下角的状态片读的是**同一份**评估结果，
//   判据只能有一处 —— 否则两边的"危险/接近"表述会各自漂移。
//   本组件只留报警条用到的两条；状态片在 components/SafetyChip.vue 里自己取。
const safetyText = computed(() => safetyTextOf(safety.value));
const safetySub = computed(() => safetySubOf(safety.value));

// ★★ A6 评估健康：三态 never / fresh / stale / stopped（判定见 utils/safetyLabels.js）。
//   报警条在 stale/stopped 时**必须**改口说"数据不可信"，而不是继续显示上一帧的
//   "安全：余量正常" —— 那正是 §5.5 铁律③禁的"把不知道塌成安全"。
const health = computed(() => safe.health || { state: "never", ok: false, notice: "", label: "未评估" });
const healthStalled = computed(() => !health.value.ok && health.value.state !== "never");

// ---------- 控制权限（阶段 1） ----------
const auth = useAuthStore();
const asr = useAsrStore();
const pw = ref("");
const pwInput = ref(null);
function requestControl() { auth.requestLogin(); }
watch(() => auth.uiLogin, (v) => { if (v) nextTick(() => pwInput.value && pwInput.value.focus()); });
async function doLogin() {
  if (await auth.login(pw.value)) { auth.closeLogin(); pw.value = ""; }
}
function releaseControl() { auth.logout(); }

// ★★ 2026-10-11 用户口径：相机与「实时捕捉」**默认都关**（不再启动即开相机）；
//   但**一旦拿到控制权限** → 自动开启「实时捕捉」+ 自动开启相机。
//   · 只在"权限从无到有"的**上升沿**触发（login 成功 / 刷新时本地已存着有效令牌 → immediate 即命中）；
//   · **幂等**：setLiveCapture 是赋值、ensureOpen 先探状态（已开/过渡中直接返回），不会重复发 /open；
//   · 权限**消失不强制关**（保留现场正在看的画面；关闭由用户手动 / 相机面板负责）。
//   · 相机不可用时 ensureOpen 静默返回（原因写在 cam.error，界面自己如实显示）。
watch(() => auth.controlActive, (now, was) => {
  if (!now || was) return;          // 只在上升沿动作（was=undefined 的 immediate 视为"从无到有"）
  cam.setLiveCapture(true);
  cam.ensureOpen();
}, { immediate: true });

// ---------- 全局键盘快捷键 ----------
// ★ 解析逻辑全在 src/shortcuts.js（纯函数，被 tools/verify_shortcuts.mjs 逐种上下文钉死）。
//   本组件只负责"取上下文 → 分派动作"，不在这里写任何键位判断 ——
//   键盘是"按下去就出事"的东西，判据散在组件里没法断言。
const exec = useExecStore();

// ★★ 2026-10-11（用户口径「**应该电脑的速度去执行**」）：**拖滑块 → 长流程下一步生效**。
//   分拣 / 井字棋 / 文件执行 / AI 这类**长流程**原来是"点开始那一刻定速"的，
//   运行中改滑块**不生效**（现场表现："我调了 40 它按 10 跑"）。这里把滑块值同步给
//   后端的内存登记处（`POST /control/ui-speed`，**只写一个数、不下发任何运动**），
//   各流程每一步取点位时读它 → **下一步就按新速度走**（只换速、**不打断**当前这句运动）。
//   ★ 节流 150ms：拖一次滑块不要打一串请求；★ 无控制令牌时不推（必然 401，纯噪音）。
let uiSpeedTimer = null;
let uiSpeedLast = null;
watch(() => exec.speed, (v) => {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n) || n === uiSpeedLast) return;
  uiSpeedLast = n;
  clearTimeout(uiSpeedTimer);
  uiSpeedTimer = setTimeout(async () => {
    if (!auth.controlActive) return;      // 没权限不推（后端也要令牌）
    try {
      await apiControl("/control/ui-speed", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ speed_pct: n }),
      });
    } catch (e) { /* 网络抖动忽略：下一步还会再推；流程仍用开始时那份 */ }
  }, 150);
});
// ★ 需求：底栏"控制中"也要以控制器就绪为准 —— 只授权、未就绪时显示"已授权·待就绪"。
//   rcReady 的 rc_status 快照由 robot store 经 WS 全局注入，不依赖是否打开过点位执行页。
const rc = useRcReadyStore();
// ★★ C3：把 AGENTS.md 的"速度电脑 ≠ 实际"排查法搬上屏 ——
//   **设定值（写入 40103 = 右上角滑块）**与**控制器 40003 实读**同屏并列 + 差值。
//   ★ 诚实口径：后端快照里**没有** 40103 的回读（控制器不提供），所以"目标"一侧引用的
//     就是滑块值本身；这一点在界面上明写，不假装读到了设定寄存器。
//   ★ 容差唯一出处 = core/safetyConst.js 的 SPEED_DEV_TOL（**提示档**，不是真机安全限）：
//     现场判定 1:1 的口径是 40103 == 40003，超出容差就值得去查 210 里的 vNNperc 与示教器倍率。
const speedActual = computed(() => {
  const v = rc.snap && !rc.stale ? rc.snap.speed_pct : null;
  return Number.isFinite(v) ? v : null;
});
const speedDev = computed(() =>
  (speedActual.value === null ? null : speedActual.value - exec.speed));
const speedDevTitle = computed(() => {
  if (speedActual.value === null) return "控制器 40003 实读：未知（快照过期或读不到控制器）";
  const d = speedDev.value;
  if (d === 0) return "实读与设定一致（1:1 直通）";
  return `实读 ${speedActual.value}% − 设定 ${exec.speed}% = ${d > 0 ? "+" : ""}${d}`
    + `（设定=右上角滑块，写入 40103 · 控制器不回读该寄存器）`
    + (Math.abs(d) >= SPEED_DEV_TOL
        ? ` · 偏差 ≥ ${SPEED_DEV_TOL}：按 AGENTS.md 查 210 内 vNNperc 与示教器速度倍率` : "");
});
// ★ 需求（用户 2026-10-08）："真实监控／模拟仿真／点位执行／程序执行／分拣／AI 执行
//   全部要显示"18 块料 + A/B 托盘盒 + C 区摄像头。以前只有「分拣」页挂载时才拉一次
//   /api/sorting/blocks 并在离开时隐藏 —— 别的页面看到的是空机器人。
//   现在装填与状态轮询收敛到本 store，由这里（App 级）驱动：拿到控制令牌即装填，
//   之后**切页面不再隐藏**，各页共用同一份 3D 显示。
const s3d = useSorting3dStore();

/** 快捷键反馈提示（一次性，2 秒后自清）。 */
const keyHint = ref("");
let keyHintTimer = null;
function flashKeyHint(text) {
  keyHint.value = text;
  if (keyHintTimer) clearTimeout(keyHintTimer);
  keyHintTimer = setTimeout(() => { keyHint.value = ""; }, 2000);
}

/** 有弹层开着时 Esc/Enter 归弹层，不归机器人。
 *  ★ 2026-10-11：底部状态区默认开着「运行日志」（被动读数面板，不算弹层）——
 *    这里用 ui.popoverModal（"log" 除外），保证 Esc 仍能触发急停。 */
const anyModalOpen = computed(() => !!auth.uiLogin || showAnnounce.value || ui.popoverModal);

/** 动作分派表：id → 处理函数。表里没有的 id 一律忽略（新增快捷键时不会静默失效）。 */
const KEY_ACTIONS = {
  toggle_side() { ui.toggleSide(); },
  toggle_camera() {
    ui.toggleCamVisible();
    flashKeyHint(ui.camVisible ? "摄像头画面：已显示" : "摄像头画面：已隐藏");
  },
  program_run_or_stop() {
    exec.runOrAbort();
    flashKeyHint(exec.running ? "程序：正在停止" : "程序：已请求运行");
  },
  estop() {
    exec.doEstop();
    flashKeyHint("急停已下发 · 按 Enter 复位");
  },
  estop_reset() {
    exec.resetEstop();
    flashKeyHint("急停复位已下发");
  },
  close_modal() {
    if (auth.uiLogin) auth.closeLogin();
    else if (showAnnounce.value) showAnnounce.value = false;
    else ui.closeStatusPopover();
  },
  submit_modal() {
    if (auth.uiLogin) doLogin();
    else if (showAnnounce.value) showAnnounce.value = false;
  },
};

function onKeydown(ev) {
  const action = resolveKey(ev, {
    // ★ typing 排在最前：在密码框里按 e 必须是打字母，不是开合侧栏。
    typing: isTypingTarget(document.activeElement),
    modalOpen: anyModalOpen.value,
    view: robot.activeView,
    stopped: !!(exec.estopState && exec.estopState.stopped),
  });
  if (!action) return;

  // 切模块：按**可见**模块列表解序号（没权限的模块不显示，序号顺延）
  if (action.startsWith("tab:")) {
    const key = tabKeyFor(action, nav.value.map((t) => t.key));
    if (!key) {
      flashKeyHint("该序号在当前权限下没有对应模块");
      return;
    }
    if (key === robot.activeView) return;
    setView(key);
    const t = nav.value.find((x) => x.key === key);
    flashKeyHint(t ? `已切到「${t.label}」` : "");
    return;
  }

  const fn = KEY_ACTIONS[action];
  if (!fn) return;
  // 需要授权的动作：没令牌时给一句明确的话，而不是"按了没反应"
  // ★ 审计修复 P1-D9②：**急停（Esc）不受 needAuth 前置拦截**。
  //   原实现把 Esc 一起拦下 → 没令牌时按 Esc 只闪一句"需要控制权限"，
  //   而急停恰恰是"不管三七二十一先按下去"的动作。现在放行到 doEstop，
  //   由它负责：先停本地点动，再明确记一行"急停未发出"并弹出验证框。
  const def = SHORTCUT_BY_ID[action];
  if (def && def.needAuth && !auth.controlActive
      && action !== "close_modal" && action !== "estop") {
    flashKeyHint(`「${def.desc}」需要控制权限，请先请求控制`);
    if (auth.requestLogin) auth.requestLogin();
    return;
  }
  // 浏览器默认行为：空格会翻页、Esc 会退出全屏/关地址栏下拉，拦下来
  ev.preventDefault();
  fn();
}

// 报警条：按配置的 banner_min 决定最低触发级别
const showBanner = computed(() => {
  // ★ 停摆提示**不受** alarm.banner 开关影响：那个开关关的是"报警横幅"，
  //   而"此刻读数不可信"是数据完整性问题，操作员没有关掉"被蒙在鼓里"的选项。
  if (healthStalled.value) return true;
  const a = safe.config && safe.config.alarm;
  if (!a || !a.banner) return false;
  const min = RANK[a.banner_min] ?? RANK.danger;
  return RANK[safety.value.state] >= min && RANK[safety.value.state] >= RANK.warn;
});
const bannerClass = computed(() => (healthStalled.value ? "stalled" : safety.value.state));
const bannerText = computed(() => (healthStalled.value ? health.value.notice : safetyText.value));
const bannerSub = computed(() => (healthStalled.value
  ? `上一帧读数：${safetySub.value}（可能已过期）` : safetySub.value));
// ★ 状态片（chip）的显示开关已随组件一起搬到 components/SafetyChip.vue
//   （它自己读 safe.config.alarm.chip），这里不再保留第二份。

// ---------- 播报中心（分级音效 + 中文语音） ----------
const showAnnounce = ref(false);
const annOff = computed(() => !annCfg.value.enabled || (!annCfg.value.sound && !annCfg.value.voice));
function toggleAnnounce() { showAnnounce.value = !showAnnounce.value; }

// 后端事件总线（WS）→ 语音播报：warn 及以上才出声，info 只进历史
// ★ P1-B6：事件帧已拆到 /ws/events，需带内出示控制令牌；未登录时不建连，
//   拿到权限后再由 controlActive 监听重建，避免每 2s 打一次被 4401 的无用重连。
let evWs = null;
let evTimer = null;
function disconnectEvents() {
  if (evTimer) { clearTimeout(evTimer); evTimer = null; }
  // ★ P1-E13（eslint no-empty）：这里两处空 catch 都是"关一个可能已经坏掉的 socket"，
  //   再抛也无处可去 —— 填上注释说明**为什么允许它为空**，而不是让人以为是漏写。
  if (evWs) { try { evWs.close(); } catch (e) { /* 关闭已断开的连接，失败无影响 */ } evWs = null; }
}
function connectEvents() {
  disconnectEvents();
  if (!auth.controlActive) return;
  try {
    evWs = new WebSocket(wsEventsUrl());
    evWs.onopen = () => {
      try { evWs.send(JSON.stringify({ type: "auth", token: auth.token })); }
      catch (e) { /* 发不出去，等后端超时关闭即可 */ }
    };
    evWs.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (!m || m.type !== "event") return;
        const lv = String(m.level || "info").toLowerCase();
        if (lv === "warn" || lv === "error" || lv === "critical") {
          announce({
            level: lv,
            text: m.message || m.action || "系统事件",
            key: "event:" + (m.action || "") + ":" + (m.message || "").slice(0, 20),
          });
        }
      } catch (e) { /* 忽略坏帧 */ }
    };
    // 4401 = 令牌缺失/失效：交给 controlActive 监听重建，不在此空转重连
    evWs.onclose = (e) => {
      if (e && e.code === 4401) return;
      evTimer = setTimeout(connectEvents, 2000);
    };
    evWs.onerror = () => { try { evWs.close(); } catch (e) { /* 已在错误态，close 再抛也不管 */ } };
  } catch (e) { /* 忽略 */ }
}
// ★ P1-B6：登录/登出都要重建事件流（onMounted 时可能还没令牌）。
// ★ 同一处顺带管分拣 3D：令牌到手才装填（没令牌必然 401），令牌失效即停轮询。
watch(() => auth.controlActive, (on) => {
  connectEvents();
  if (on) s3d.start(); else s3d.stop();
});

// ---------- 围栏实时状态上报（阶段 4：服务端互锁） ----------
// 围栏余量是在前端 3D 里逐帧算出来的，后端看不见。这里每秒把状态喂给后端，
// /api/control/move 就能在危险状态下"服务端兜底"拒绝下发 —— 就算有人绕过界面
// 直接调 API 也发不下去。断/弱网上报失败不影响使用，只是互锁失效（后端会记 warns）。
let liveTimer = null;
async function reportLive() {
  // ★ 未登录不上报：POST /safety/live 挂着 require_control，没令牌必然 401。
  //   这里是每秒一次的轮询，不挡住的话控制台会被 401 刷满（登录后自然恢复）。
  if (!auth.controlActive) return;
  const s = safety.value;
  try {
    // ★ apiUrl() 会自动补 /api 前缀，这里不要再写一遍，否则变成 /api/api/...（405）
    // ★ 审计修复 P0-3：后端已给 /safety/live 挂上 require_control，
    //   上报必须带控制令牌，否则互锁数据会被 401 挡掉（未登录时不上报即可）。
    await apiControl("/safety/live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state: s.state || "safe",
        zone_id: s.zoneId || "",
        zone_name: s.zoneName || "",
        clearance: +(s.clearance || 0).toFixed(4),
        ratio: +(s.ratio || 1).toFixed(4),
      }),
    });
  } catch (e) { /* 后端不可用时静默 */ }
}

function setView(v) {
  // 越权 / 未知 key 一律回落真实监控（这里也兜住"手改 localStorage 指定了个不存在页"）
  if (!allowedKeys.value.includes(v)) v = HOME_VIEW;
  if (v === robot.activeView) return;
  robot.setActiveView(v);
  ui.setView(v);                     // 记忆当前页面：刷新后回到同一页
  // ★ ops 是纯数据页，没有 3D 容器：switchView 查不到容器会直接返回，
  //   画布留在上一个视图里（被 v-show 隐藏），切换回去时原样恢复。
  switchView(v);
  // 审计修复 P0-ui-3：switchView 是**同步**执行的，此刻 Vue 还没把新视图 v-show 显示
  // 出来 —— 目标容器仍 display:none，three/manager.resizeView 会因尺寸 0 早退，
  // 画布停在旧缓冲/空帧上，要等 MonitorLayout 的 ResizeObserver + 240ms debounce
  // 才补 setSize（窗口期内就是"3D 黑屏闪一下"）。
  // 这里等 v-show 生效（nextTick，仍在本帧绘制之前）补一次 resizeView + 强制渲染
  // 一帧，把 0 尺寸早退窗口彻底关掉。
  nextTick(() => {
    resizeView();
    if (!glLost && !document.hidden) renderFrame();
  });
}

// ---------- 页面持久化 + 权限显隐 ----------
// 门控逻辑全部来自 src/tabs.js（纯函数，可被 node 断言）
const allowedKeys = computed(() => allowedKeysOf(auth));

const visibleTabs = computed(() => visibleTabsOf(auth));

// ★ 2026-10-10 用户口径：顶栏改成"左 2 / 右 6"的可读分组 ——
//   · 数字键 1~8 走 **nav**（顶栏实际显示的那些，不含 hidden 的「程序执行」）；
//   · 左右分组由 tabs.js 的 `side` 字段决定（不在这里写死 slice 下标）。
const nav = computed(() => navTabsOf(auth));
const tabsLeft = computed(() => nav.value.filter((t) => t.side !== "right"));
const tabsRight = computed(() => nav.value.filter((t) => t.side === "right"));
/** 顶栏高亮：把 alias 也算上（「点位执行」合并了「程序执行」）。 */
function isTabActive(t) {
  return [t.key, ...(t.alias || [])].includes(robot.activeView);
}

// 权限丢失（主动释放）/ 令牌到期 / 角色降级（admin→operator）时，把停留在无权限页面的
// 用户送回真实监控，否则会看到一个"整页按钮全灰"的空壳页面。
// 同时监听 role：操作员令牌对"运维审计"是无效的，只盯 controlActive 会漏掉这一种。
watch(() => [auth.controlActive, auth.isAdmin], () => {
  if (!allowedKeys.value.includes(robot.activeView)) setView(HOME_VIEW);
});

/** 恢复上次停留的页面（容器已由子组件在 mounted 时登记，此时切换是安全的）。 */
function restoreView() {
  const v = ui.view;
  if (!v || v === HOME_VIEW) return;
  if (!isKnownView(v)) return;                  // localStorage 被塞了垃圾值
  const safe = safeView(v, auth);               // 越权 → 回落真实监控
  if (safe !== HOME_VIEW) setView(safe);
}

// 令牌到期看门狗：
// controlActive 是「纯计算属性」（expiresAt > Date.now()），时间自己流逝**不会**触发重算，
// 也没人会去 await 它。所以到期后必须主动清一次令牌，Tab 才会真的收回去 —— 否则
// 页面会一直显示"控制中"、执行页入口也一直在，点进去全是 401。
// ★ expiresAt === 0 = 不限时（阶段 7 默认），直接跳过。
let ttlTimer = null;
let healthTimer = null;   // ★ A6 评估健康的外部时钟（1s；判定三态用，不参与评估本身）
function checkTtl() {
  if (auth.token && auth.expiresAt !== 0 && auth.expiresAt <= Date.now()) auth._clear();
  // ★ 审计修复 P1-D8：同一个 1 秒时钟顺带扫一次遥测新鲜度。
  //   后端卡死时 WS 不一定断（TCP 还在），画面会定格但 connected 仍为 true。
  try { robot.sweepTelemetry(); } catch (e) { /* store 未就绪 */ }
  // ★★ 2026-10-11：同一个 1 秒时钟再推两个"数据新鲜度"水位 —— 必须**无条件**推进，
  //   否则掉线时（link 停轮询 / 控制器 WS 帧停）水位冻结 → `stale` 永远算不出 true
  //   → 底栏挂着"旧绿"（最危险的假正常）。详见 stores/link.js::beat / rcReady.js::beat。
  try { link.beat(); } catch (e) { /* store 未就绪 */ }
  try { rc.beat(); } catch (e) { /* store 未就绪 */ }
  // ★ B1：指标新鲜度水位同样必须**无条件**推进（同一病根：Date.now() 不是响应式依赖，
  //   后端挂掉时 snap/at 冻结 → stale 永远算不出 true）。见 stores/metrics.js::beat。
  try { metrics.beat(); } catch (e) { /* store 未就绪 */ }
  // ★★ C3：关键参数的 60s 短趋势采样（utils/paramTrend.js 的环形缓冲）。
  //   挂在这同一个 1 秒时钟上是刻意的：绑渲染循环 → 隐藏标签页就断档；
  //   绑 WS 帧 → 掉线前最后一帧被复制成一条"刚刚还正常"的平线（§5.5 铁律①）。
  try { sampleTrends(); } catch (e) { /* store 未就绪 */ }
}

/**
 * ★ C3 趋势采样：**只采"这一路还新鲜"的数**。
 *   数据冻结时不入环 —— 否则时间窗里会攒出一段假平线，比没有线更误导。
 *   非有限数（未知/读不到）由 `trendSample` 自己拒收，这里不再判一遍（避免两处口径分叉）。
 */
function sampleTrends() {
  if (!rc.stale && rc.snap) trendSample("speed", rc.snap.speed_pct);
  if (robot.connected && !robot.telemetryStale) trendSample("j6", robot.telemetry.q[5]);
  if (safe.health && safe.health.ok) trendSample("clearance", safe.last.clearance * 1000);
}

let rafId = null;
let glLost = false;      // WebGL 上下文丢失中：暂停渲染，恢复后自动重启

function loop() {
  // ★★ 页面隐藏 / WebGL 上下文丢失时**彻底停帧**（不再自续 RAF）：
  //   以前隐藏标签页照样全速跑 RAF，GPU 显存被多标签页挤爆，直接导致
  //   D3D 设备移除 → context lost → 黑屏。这是"黑屏闪屏"的第一根因。
  //   停帧后由 onVisibility / onGlState 负责重新拉起。
  if (document.hidden || glLost) { rafId = null; return; }
  rafId = requestAnimationFrame(loop);
  // ==========================================================================
  // ★★ 全站**唯一**的姿态下发点（阶段 7 姿态权威模型）
  //
  // 改造前：7 个地方各自调 applyRobotPose（真实监控跳舞、模拟仿真、
  //   exec 的 7 处指令回执），谁最后写谁赢；而「点位执行/程序执行」只在指令
  //   回执时"一跳写入"，跑程序时根本看不到机器人真的在走。
  // 现在：各视图只负责把姿态写进 store 的三个来源（telemetry / demo / override），
  //   由 store 的 displayQ getter 按**唯一一份判据**算出该显示哪个，
  //   本循环帧帧下发 —— 于是不可能再出现两个写者抢姿态，也不可能漏页。
  // 守卫：tools/verify_pose_authority.mjs 断言本仓库只有这里与 three/manager.js
  //   出现 applyRobotPose 调用。
  // ==========================================================================
  applyRobotPose(robot.displayQ);
  renderFrame();
}

function startLoop() {
  if (rafId || document.hidden || glLost) return;
  rafId = requestAnimationFrame(loop);
}
function stopLoop() {
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}

function onResize() {
  resizeView();
}

// ---------- 轮询总闸：机器人 WS 断开 / 页面隐藏 → 停一切 HTTP 轮询 ----------
// 用户要求：机器人没连上就不要一直请求，连上后再自动恢复。
// 判据用 robot.connected（WS onStatus 推的真值）：后端活着但机器人断线时
// WS 仍通（这时链路自检/围栏互锁有存在意义，轮询保留）；后端断网/挂掉时
// WS 断开，此时 guide / safety-live / health 全部注定失败，只会刷屏 —— 全停。
// WS 自身的重连由 net/ws.js 的指数退避负责，恢复后这里 watch 会把轮询拉起来。
function startLiveReports() {
  if (liveTimer) return;
  reportLive();
  liveTimer = setInterval(reportLive, 1000);   // 维持后端围栏互锁的有效性
}
function stopLiveReports() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}
watch(() => robot.connected, (ok) => {
  // ★ 加 !document.hidden：后台标签页里 WS 恢复时不拉起轮询 ——
  //   visibilitychange 只管切页瞬间，管不了"隐藏期间重连成功"这种情况。
  if (ok && !document.hidden) { startLiveReports(); link.start(); metrics.start(); }
  else { stopLiveReports(); link.stop(); metrics.stop(); }
});
// 多标签页场景下每个隐藏页继续轮询会把后端和 GPU 打满（现场出现过
// WebGL context lost / ERR_INSUFFICIENT_RESOURCES），隐藏即停、可见即恢复。
// ★ 渲染循环与 HTTP 轮询共用这一个闸门：GPU 与网络要停一起停。
document.addEventListener("visibilitychange", onVisibility);
function onVisibility() {
  if (document.hidden) {
    stopLiveReports();
    link.stop();
    metrics.stop();              // ★ B1：指标轮询同样隐藏即停（多标签页不许各打各的）
    s3d.stop();                  // ★ 分拣 3D 状态轮询同样隐藏即停（与渲染帧一起停）
    stopLoop();                  // ★ 隐藏即停渲染帧（后台标签页不再空耗 GPU）
  } else {
    if (robot.connected) { startLiveReports(); link.start(); metrics.start(); }
    if (auth.controlActive) s3d.start();
    startLoop();                 // 回到前台：场景与模型原地保留，直接续帧
  }
}

// ---------- WebGL 上下文健康 ----------
// 上下文丢失时暂停渲染并给出明确提示；浏览器恢复后自动续帧（不必再按 F5）。
const glState = ref("ok");
onGlState((s) => {
  glState.value = s;
  glLost = (s === "lost");
  if (glLost) stopLoop();
  else startLoop();
});

onMounted(async () => {
  // ★ 必须**先**确认令牌是否仍有效：allowedKeys / visibleTabs 都依赖 auth.controlActive。
  //   若放在 restoreView 之后，刚刷新时 controlActive 还是 false，
  //   上次停在"点位执行/程序执行"的用户会被判为无权限而永远按在真实监控页。
  await auth.fetchStatus();
  // ★ 需求：底部"管理员剩余时间"要逐秒更新。fetchStatus 成功时会启动 1s 心跳；
  //   但若本次 /auth/status 失败（后端刚起/网络抖动）而本地仍持有有效令牌，
  //   心跳就不会启动 → 倒计时看起来"冻住"。这里再兜一次。
  if (auth.controlActive) auth.startTtlTick();
  // ★ 分拣 3D 装填 + 状态轮询（各页共用；无令牌时 store 内部直接不打）
  if (auth.controlActive) s3d.start();
  onSafety((s) => { safety.value = s; safe.ingest(s); });
  asr.loadConfig();        // 语音字幕配置（是否启用/有没有密钥）→ 底栏按钮才能如实提示
  // ★ C1：把"高性能画面模式"的本机偏好**应用**到渲染层（默认关 → 外观与今天逐字一致）。
  //   只存 localStorage、不写后端配置：这是操作员各自的看法，不该互相影响。
  ui.applyHiPerf();
  await robot.loadMeta();  // 初始化机器人模型（子组件 onMounted 已登记容器）
  robot.connect();         // 建立 WebSocket（连上后由上面的 watch 拉起轮询）
  // ★ 2026-10-11 口径变更：**不再"每次启动项目自动打开摄像头"**。
  //   相机与「真实监控」页的「实时捕捉」**默认都关**；两者都由用户手动开启（不需要权限），
  //   或"拿到控制权限"时由上面的 auth.controlActive watch 自动开启（幂等）。
  await safe.load();       // 拉取安全围栏配置并应用到 3D
  switchView(HOME_VIEW);   // canvas 挂到 live 视图（restoreView 若不动作就停在这里）
  restoreView();           // 恢复上次停留的页面：刷新后回到同一页
  startLoop();             // 渲染循环（隐藏页/上下文丢失时自行停帧）
  // ★★ A6：评估与渲染**解耦**。渲染循环隐藏即停（黑屏第一根因的修法，不许回退），
  //   以前一停帧连围栏评估一起停 —— 后台标签页里机器人真撞了台面，界面还挂着
  //   上一帧的"安全"。这条 250ms 低频评估只做几何不渲染（渲染循环在跑时会自动让位），
  //   配套的三态健康度由下面的 1s 外部时钟判定（§5.5 铁律①：不许用"结论变没变"猜新鲜）。
  startIdleSafetyEval();
  safe.tickHealth();
  healthTimer = setInterval(() => safe.tickHealth(), 1000);
  window.addEventListener("resize", onResize);
  reportLive();             // 打开页面先报一次互锁状态；后续定时器交给 watch 拉起
  checkTtl();
  ttlTimer = setInterval(checkTtl, 1000);      // 令牌到期 → 自动收回执行页入口
  connectEvents();                              // 订阅后端事件（用于语音播报）
  link.start();                                 // 链路自检轮询（状态灯 + 引导条共用）
  metrics.start();                              // ★ B1：后端指标轮询（只读接口，无令牌）
  window.addEventListener("keydown", onKeydown); // 全局快捷键（1~9 / E / Q / 空格 / Esc / Enter）
  // 浏览器自动播放策略：首次手势（点击 / 按键）后再允许出声。
  // ★ AudioContext 必须在这个手势里创建（见 services/announcer.js），
  //   纯键盘操作的用户不会点鼠标，所以 pointerdown 与 keydown 都要挂。
  window.addEventListener("pointerdown", unlockAudio, { once: true });
  window.addEventListener("keydown", unlockAudio, { once: true });
});

/** 语音字幕开关：一次点击 = 开/关（换页不中断，配置在挂载时拉过）。 */

function toggleAsr() {
  if (asr.active || asr.connecting) asr.stop();
  else { asr.loadConfig(); asr.start(); }
}
/** 底栏按钮的悬停说明 —— 失败原因如实写出来，不静默。 */
const asrTip = computed(() => {
  if (!auth.controlActive) return "需要控制令牌（点右侧「请求控制」）";
  if (asr.err) return asr.err;
  if (!asr.available) return asr.reason || "语音识别未启用";
  if (asr.connecting) return "正在连接云端…";
  if (asr.active) return "语音字幕：识别中（点击停止）";
  return "语音字幕：点击开始（说话实时转文字，显示在 3D 画面底部）";
});

onBeforeUnmount(() => {
  stopLoop();
  if (liveTimer) clearInterval(liveTimer);
  if (ttlTimer) clearInterval(ttlTimer);
  if (healthTimer) clearInterval(healthTimer);      // ★ A6 评估健康时钟
  stopIdleSafetyEval();                             // ★ A6 低频评估定时器（卸载后不许在后台继续量）
  if (uiSpeedTimer) clearTimeout(uiSpeedTimer);   // ★ 速度同步的节流定时器也要清
  disconnectEvents();
  // ★ 审计修复 P1-D11：卸载必须主动关掉姿态 WS 与执行域定时器。
  //   原实现只 clear 了本地定时器，robot store 的 WebSocket 句柄根本没人管
  //   （_ws 挂在 store 里，HMR / 路由级卸载后依然活着）→ 断线重连定时器
  //   继续在后台空转，连回一个已经销毁的页面，控制台一路红。
  try { robot.disconnect(); } catch (e) { /* 卸载阶段异常不阻塞其他清理 */ }
  try { exec.dispose(); } catch (e) { /* 同上 */ }
  link.stop();
  metrics.stop();      // ★ B1：指标轮询的 setInterval 也必须随组件卸载清掉（模块级句柄）
  s3d.stop();          // ★ 分拣 3D 的状态轮询同样必须停（否则卸载后仍在打后端）
  document.removeEventListener("visibilitychange", onVisibility);
  if (keyHintTimer) clearTimeout(keyHintTimer);
  window.removeEventListener("keydown", onKeydown);
  window.removeEventListener("resize", onResize);
  // ★ 释放整套 3D 资源（几何/材质/贴图/WebGL 上下文）。
  //   只 remove 不 dispose 会把显存留给下一个页面实例，反复挂载最终黑屏。
  try { disposeAll(); } catch (e) { /* 卸载阶段的异常不该阻塞其他清理 */ }
});
</script>

<template>
  <div id="app">
    <header id="topbar">
      <!-- 左侧 tab（由 tabs.js 的 side:"left" 决定） -->
      <div class="tabs-left">
        <button v-for="t in tabsLeft" :key="t.key" class="tab"
                :class="{ active: isTabActive(t) }" @click="setView(t.key)">
          <Icon :name="t.icon" :size="15" /> {{ t.label }}
        </button>
      </div>
      <!-- 居中标题 -->
      <div id="brand" :title="BRAND.name + ' · ' + BRAND.subtitle + ' · ' + brandSubtitle()">
        <span class="brand-text">
          <span class="brand-name">{{ BRAND.name }}</span>
          <span class="brand-sub">{{ BRAND.subtitle }}</span>
        </span>
      </div>
      <!-- 右侧 tab（side:"right"）+ 速度滑块 -->
      <div class="tabs-right">
        <button v-for="t in tabsRight" :key="t.key" class="tab"
                :class="{ active: isTabActive(t) }" @click="setView(t.key)">
          <Icon :name="t.icon" :size="15" /> {{ t.label }}
        </button>
        <!-- 速度滑块 -->
        <!-- ★ 全项目唯一速度口径。min/max 直接引用 core/safetyConst.js 的常量
             （与后端 SPEED_MIN/SPEED_MAX 同口径）：以前这里写 min="1"，
             而操作 1~4 会被后端拒绝或夹到 5 —— 表现为"我设了 1% 却按 5% 跑"。
             有静态守卫盯着两处一致（tools/verify_speed.mjs）。 -->
        <div class="speed-control">
          <span class="speed-label">速度</span>
          <input type="range" :min="SPEED_MIN" :max="SPEED_MAX"
                 v-model.number="exec.speed" class="speed-slider" />
          <span class="speed-value">{{ exec.speed }}%</span>
          <!-- ★ C3：设定 vs 实读 同屏（详见上面 speedActual/speedDev 的口径注释）。
               未知就写未知，不拿 0 或滑块值冒充实读（§5.5 铁律③）。 -->
          <span class="speed-actual" :class="{ unknown: speedActual === null, dev: Math.abs(speedDev ?? 0) >= SPEED_DEV_TOL }"
                :title="speedDevTitle">实读 {{ speedActual === null ? "未知" : speedActual + "%" }}</span>
        </div>
      </div>
    </header>
    <div v-if="showBanner" id="safety-banner" :class="bannerClass">
      <span>{{ bannerText }}</span>
      <span class="sa-sub">{{ bannerSub }}</span>
      <!-- ★ A5：报警就地给取证入口（danger/hit 上升沿后端已自动落盘）。
           只在**能看到那一页**时出现 —— 运维审计是 needAdmin，没有管理员令牌还摆个
           点了跳不过去的链接，就是本项目的老毛病"假显示"。 -->
      <button v-if="allowedKeys.includes('ops')" class="sa-ev"
              title="运维审计 → 事件取证包（事发前后的实时样本环 / 寄存器 / 事件）"
              @click="setView('ops')">查看取证</button>
    </div>
    <div id="main">
      <RealMonitor v-show="robot.activeView === 'live'" />
      <SimMonitor v-show="robot.activeView === 'sim'" />
      <!-- ★ 2026-10-10 用户口径：「点位执行」与「程序执行」合并成一个入口 ——
           顶栏只出「点位执行」一个 tab，两者在**右栏顶部的 tab 条**里切换。
           这个 tab 条通过具名插槽塞进两页的右栏（内容在本组件作用域，直接调 setView）。 -->
      <PointExecView v-show="robot.activeView === 'point'">
        <template #sidebarTop>
          <!-- ★ 2026-10-11 用户需求：与「智能分拣」页的 tab 栏**统一** —— 改用全局
               .srt-tabs/.srt-tab（同一套视觉：字号/间距/选中态青色发光 + 底部指示条），
               不再各写一套 .seg.mini（两页样式会各自漂移）。 -->
          <div class="srt-tabs" role="tablist">
            <button class="srt-tab" :class="{ on: robot.activeView === 'point' }"
                    @click="setView('point')">
              <Icon name="target" :size="13" /><span>点位执行</span>
            </button>
            <button class="srt-tab" :class="{ on: robot.activeView === 'program' }"
                    @click="setView('program')">
              <Icon name="play" :size="13" /><span>程序执行</span>
            </button>
          </div>
        </template>
      </PointExecView>
      <ProgramExecView v-show="robot.activeView === 'program'">
        <template #sidebarTop>
          <!-- ★ 2026-10-11 用户需求：与「智能分拣」页的 tab 栏**统一** —— 改用全局
               .srt-tabs/.srt-tab（同一套视觉：字号/间距/选中态青色发光 + 底部指示条），
               不再各写一套 .seg.mini（两页样式会各自漂移）。 -->
          <div class="srt-tabs" role="tablist">
            <button class="srt-tab" :class="{ on: robot.activeView === 'point' }"
                    @click="setView('point')">
              <Icon name="target" :size="13" /><span>点位执行</span>
            </button>
            <button class="srt-tab" :class="{ on: robot.activeView === 'program' }"
                    @click="setView('program')">
              <Icon name="play" :size="13" /><span>程序执行</span>
            </button>
          </div>
        </template>
      </ProgramExecView>
      <SortingView v-show="robot.activeView === 'sort'" />
      <PadExecView v-show="robot.activeView === 'pad'" />
      <AiControlView v-show="robot.activeView === 'ai'" />
      <EventsView v-show="robot.activeView === 'ops'" />
      <!-- 系统设置 / 关于：纯数据页，没有 3D 容器。
           它们也走 v-show 保活（设置页填了一半切走再切回来，草稿不能丢）。 -->
      <SettingsView v-show="robot.activeView === 'settings'" />
      <AboutView v-show="robot.activeView === 'about'" />
    </div>

    <!-- WebGL 上下文丢失：明确告知"正在恢复"，而不是让用户对着黑屏猜。
         浏览器恢复上下文后由 three/manager.js 通知，渲染循环自动续上。 -->
    <transition name="gl-fade">
      <div v-if="glState === 'lost'" id="gl-banner">
        <Icon name="refresh" :size="14" class="spin" /> 3D 画面正在恢复（WebGL 上下文丢失）…
      </div>
    </transition>

    <!-- 连接自检 / 操作引导：浮在 3D 视口底部上方（absolute 浮层，不占底栏、
         不挤 3D 视口高度，见 index.html #guide-zone 样式）。只报最高优先级一条。
         ★ B1 避让：底部「五选一」面板打开时（默认开「运行日志」），面板从底栏向上弹出，
           会盖住这一带 —— 此时给引导条加 .lifted，把它抬到面板之上（静态避让；层级见 index.html）。 -->
    <div id="guide-zone" :class="{ lifted: !!ui.statusPopover }">
      <GuideBar />
    </div>

    <!-- 快捷键反馈：只在按了键之后出现 2 秒。
         位置固定在 3D 视口底部居中偏上，不遮底栏状态灯，也不进主布局流。 -->
    <transition name="kh-fade">
      <div v-if="keyHint" id="key-hint" :class="{ lifted: !!ui.statusPopover }">
        <Icon name="keyboard" :size="13" /> {{ keyHint }}
      </div>
    </transition>

    <!-- 底部状态区：状态片条（运行日志 / 摄像头 / 示教器 / 真机链路）+ 连接引导 + 控制区。
         ★ 顺序（左→右）：
             ① StatusStrip：状态片条 —— **任何情况都在**；点片开面板，点外部收起
             ② 控制区（轴锁徽标 + 播报 + 请求控制/释放）：被顶到最右，窄屏换行也在右下
         ★ 2026-10-11：原独立的「真机链路」按钮已并入 StatusStrip 的「真机链路」片
           （面板内容仍是 RcReadyCard）；运行日志也从左下角浮动面板迁入「运行日志」片。
         ★ 2026-10-11 第二轮：原「机器人」片并入「真机链路」片（"两个真机链路合并"）。
         ★ 安全围栏状态片**已移出底栏** → 现在浮在 3D 视口右下角，
           见 components/SafetyChip.vue（由 MonitorLayout 渲染）。
         ★ 放在 #main **之后**：视口紧贴 Tab 栏、高度稳定，引导条出现/消失不会挤动 3D。 -->
    <div id="bottom-zone">
      <!-- 状态片：运行日志(最左) / 摄像头 / 示教器 / 真机链路(最右，含机器人链路) -->
      <StatusStrip @request-control="requestControl" />

      <!-- 播报中心 + 控制权限（原顶栏 #control-zone，整块移到这里的最右侧） -->
      <div id="control-zone">
        <!-- ★ 全维度审查 N-01：轴锁模式常驻徽标。让操作员随时知道当前是
             「J1–J6 全轴可动」（默认）还是「轴锁模式：仅 J6」（AI 测试模式）。
             后端 /api/system/health 的 joint_lock 经 exec.loadLock() 拉取。 -->
        <span class="lock-badge" :class="{ on: exec.jointLockEnabled }"
              :title="exec.jointLockEnabled
                ? '轴锁已开启：仅 J' + (exec.jointLock?.only || 6) + ' 可动（AI 测试模式）'
                : '当前 J1–J6 全轴可动（操作员操控）'">
          <Icon :name="exec.jointLockEnabled ? 'lock' : 'unlock'" :size="13" />
          {{ exec.axisHint }}
        </span>
        <button class="ann-btn" :class="{ off: annOff }" @click="toggleAnnounce"
                :title="annOff ? '播报已关闭（点击配置）' : '播报中心：音效 / 中文语音'">
          <Icon :name="annOff ? 'volumeOff' : 'volume'" :size="15" />
        </button>
        <!-- ★ 实时语音字幕开关（与播报按钮同款底栏样式）：
             说话实时转文字，识别结果以电影字幕叠在 3D 视口底部。
             识别文本只用于显示，不参与任何控制。 -->
        <!-- ★ 不做 disabled：按钮始终可点 —— 没权限/没配密钥时点一下就在右侧
             显示具体原因（需要控制令牌 / 未配置密钥 / 非安全源无法用麦克风），
             灰着不响应才是最让人困惑的。 -->
        <button class="ann-btn asr-btn" :class="{ off: !asr.active && !asr.connecting, rec: asr.active || asr.connecting }"
                @click="toggleAsr" :title="asrTip">
          <Icon :name="asr.active || asr.connecting ? 'mic' : 'micOff'" :size="15" />
        </button>
        <span v-if="asr.err" class="asr-err" :title="asr.err" @click="asr.clearErr()">
          <Icon name="alert" :size="12" /> {{ asr.err }}
        </span>
        <template v-if="auth.controlActive">
          <!-- ★ 一键就绪 / 取消就绪已收进底部「真机链路」面板（点开即完整 RcReadyCard），
               这里只保留必要的状态与动作，避免同一功能在底栏出现两遍。
               显示：授权角色 + 剩余有效时间；动作：释放控制权限。
               ★★ 2026-10-11 修复（注释承诺了但代码没做）：底栏"控制中"**也要以控制器就绪为准** ——
                 只授权、未就绪时显示"已授权 · 待就绪"，免得现场以为"有权限 = 能下发"。
                 判据用 `rc.ready`（后端判定：伺服+程序运行+档位+无报警+非急停）。 -->
          <span class="ctrl-badge" :class="{ pending: !rc.ready }"
                :title="rc.ready ? ('控制授权有效，剩余：' + auth.ttlText)
                                 : ('已获控制权限，但机器人未就绪（' + NOT_READY_HINT + '）；剩余：' + auth.ttlText)"
                style="font-variant-numeric:tabular-nums">
            {{ auth.roleLabel ? auth.roleLabel + " · " : "" }}<Icon name="clock" :size="13" /> {{ auth.ttlText }}
            <template v-if="!rc.ready"> · 待就绪</template>
          </span>
          <button class="ctrl-release" @click="releaseControl" title="释放控制权限">
            <Icon name="power" :size="14" /> 释放
          </button>
        </template>
        <button v-else class="ctrl-request primary" @click="requestControl"
                :disabled="!auth.required" title="需输入管理员或操作员口令授权">
          <Icon name="lock" :size="14" /> 请求控制
        </button>
      </div>
    </div>

    <!-- 播报中心设置面板 -->
    <AnnouncePanel :open="showAnnounce" @close="showAnnounce = false" />

    <!-- 控制权限登录（阶段 1） -->
    <div v-if="auth.uiLogin" class="modal-mask" @click.self="auth.closeLogin()">
      <div class="modal">
        <div class="modal-head">
          <Icon name="lock" :size="18" />
          <span>身份验证</span>
          <button class="modal-x" @click="auth.closeLogin()" aria-label="关闭">
            <Icon name="close" :size="16" />
          </button>
        </div>
        <p class="modal-tip">
          连接真实机器人进行控制前，需输入口令（管理员 / 操作员）获取限时控制令牌（有效期
          {{ auth.cfgTtlText }}；可在「系统设置 → 控制权限时长」调整）。
        </p>
        <ul class="modal-roles">
          <li>
            <b>管理员密码</b>：可操控机器人、修改安全围栏配置、清理审计日志、导入备份。
          </li>
          <li v-if="auth.rolesEnabled.operator">
            <b>操作员密码</b>：只能操控机器人；运维审计等改配置的页面不会显示。
          </li>
        </ul>
        <!-- ★ 必须包在 <form> 里：裸的 password 输入框会触发浏览器告警
             "Password field is not contained in a form"，且失去回车提交与密码管理器的语义。 -->
        <form class="modal-input" :class="{ invalid: auth.error }" @submit.prevent="doLogin">
          <Icon name="lock" :size="15" />
          <input ref="pwInput" type="password" v-model="pw"
                 autocomplete="current-password"
                 :disabled="auth.busy" placeholder="请输入口令（管理员 / 操作员）" />
        </form>
        <p v-if="auth.error" class="modal-err"><Icon name="alert" :size="14" /> {{ auth.error }}</p>
        <div class="modal-btns">
          <button @click="auth.closeLogin()" :disabled="auth.busy">取消</button>
          <button class="primary" @click="doLogin" :disabled="auth.busy || !pw">
            <Icon v-if="auth.busy" name="refresh" :size="14" class="spin" />
            {{ auth.busy ? "验证中" : "获取控制" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ---- 顶栏：毛玻璃半透明 + 底部发光线 ---- */
#topbar { display: flex; align-items: stretch;
  background: linear-gradient(180deg, rgba(10,16,26,0.94), rgba(8,12,20,0.8));
  backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line); position: relative; }
#topbar::after { content: ''; position: absolute; bottom: -1px; left: 6%; right: 6%;
  height: 1px; background: linear-gradient(90deg, transparent, rgba(47,214,240,0.4), transparent);
  pointer-events: none; }

/* ---- 三段式顶栏：左 tabs | 居中标题 | 右 tabs ----
   ★ 左右两组用 `flex: 1 1 0`（不是 flex:1）→ 两组**等宽**，标题才真正居中；
     速度滑块放在右组内（用 margin-left:auto 顶到最右），否则右侧多一块会把标题挤偏。 */
.tabs-left, .tabs-right { flex: 1 1 0; display: flex; min-width: 0; }
.tabs-left { justify-content: flex-end; }
.tabs-right { justify-content: flex-start; }

/* ---- 居中标题（无图标：品牌名在上、副标题在下，两行都居中）---- */
#brand { flex: none; display: flex; align-items: center;
  padding: 0 10px; white-space: nowrap; }
.brand-text { display: flex; flex-direction: column; align-items: center;
  justify-content: center; min-width: 0; overflow: hidden; line-height: 1.18; }
.brand-name { font-size: 18px; font-weight: 800; white-space: nowrap;
  letter-spacing: .04em; overflow: hidden; text-overflow: ellipsis;
  font-family: 'Chakra Petch', var(--font);
  background: linear-gradient(120deg, var(--accent), #6ea8ff);
  -webkit-background-clip: text; background-clip: text; color: transparent;
  filter: drop-shadow(0 0 12px rgba(47,214,240,0.25)); }
.brand-sub { font-size: 10.5px; font-weight: 500; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  letter-spacing: .07em; max-width: 300px; }

/* ---- 速度滑块（在右组内，margin-left:auto 顶到最右） ---- */
.speed-control { margin-left: auto; flex: none; display: flex; align-items: center; gap: 8px;
  padding: 0 4px 0 10px; border-left: 1px solid var(--line); white-space: nowrap; }
.speed-label { font-size: 11px; color: var(--muted); font-weight: 600; }
.speed-slider { width: 90px; height: 4px; -webkit-appearance: none; appearance: none;
  background: var(--panel3); border-radius: 2px; outline: none; cursor: pointer; }
.speed-slider::-webkit-slider-thumb { -webkit-appearance: none; appearance: none;
  width: 14px; height: 14px; border-radius: 50%;
  background: linear-gradient(135deg, var(--accent), var(--accent-deep));
  box-shadow: 0 0 8px rgba(47,214,240,0.4); cursor: pointer; }
.speed-slider::-moz-range-thumb { width: 14px; height: 14px; border-radius: 50%; border: none;
  background: linear-gradient(135deg, var(--accent), var(--accent-deep));
  box-shadow: 0 0 8px rgba(47,214,240,0.4); cursor: pointer; }
.speed-value { font-size: 12px; color: var(--accent); font-weight: 700;
  font-family: var(--num); min-width: 36px; text-align: right; }
/* ★ C3：实读值 —— 与"设定"并列（同一句结论必须同源：都读 rc.snap.speed_pct = 40003）。
   文字本身已带"实读/未知"，颜色只是强化，不做唯一载体（C5 双编码口径）。 */
.speed-actual { font-size: 11px; color: var(--txt2); font-family: var(--num);
  border-left: 1px solid var(--line); padding-left: 8px; }
.speed-actual.unknown { color: var(--muted); font-style: italic; }
.speed-actual.dev { color: var(--warn); font-weight: 700; }

/* ---- 底栏控制区 ---- */
#control-zone { flex: none; display: flex; align-items: center; gap: 10px;
  padding: 0 4px 0 10px; margin-left: auto; border-left: 1px solid var(--line); }
.ctrl-badge { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ok);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.lock-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 11px;
  color: var(--ok); white-space: nowrap; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--bg); font-variant-numeric: tabular-nums; }
.lock-badge.on { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
/* ---- 2026-10-11：原「真机链路」独立切换按钮的样式（.link-toggle）已随按钮移除，
        该功能并入 StatusStrip 的「真机链路」片。 ---- */
.ctrl-badge.op { color: var(--accent); }
/* ★★ 2026-10-11：拿到权限但控制器未就绪 → 用琥珀色点出来（不再只显示"管理员"）。 */
.ctrl-badge.pending { color: var(--warn); }
.ctrl-request, .ctrl-release { flex: none; width: auto; padding: 7px 10px;
  display: flex; align-items: center; gap: 6px; white-space: nowrap; }
.ctrl-release { color: var(--warn); }
.ctrl-release:hover { border-color: var(--warn); }
.ctrl-request:disabled { opacity: .4; cursor: not-allowed; }
.ann-btn { flex: none; width: 30px; height: 30px; padding: 0; display: inline-flex;
  align-items: center; justify-content: center; color: var(--accent);
  border-radius: var(--radius-sm); }
.ann-btn.off { color: var(--muted); opacity: .75; }
.ann-btn.rec { color: #fff; background: linear-gradient(135deg, #ff5c7a, #e02a4e);
  border-color: transparent; box-shadow: 0 0 12px rgba(255,77,109,0.4);
  animation: asrPulse 1.6s ease-in-out infinite; }
@keyframes asrPulse { 0%,100% { opacity: 1; } 50% { opacity: .62; } }
.asr-err { display: inline-flex; align-items: center; gap: 4px; max-width: 280px;
  font-size: 11px; color: var(--err-fg); cursor: pointer;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---- 模态框：Sentinel 风格 ---- */
.modal-mask { position: fixed; inset: 0; z-index: 200;
  background: rgba(3, 6, 12, 0.72); backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
  animation: modalFadeIn .18s ease-out; }
@keyframes modalFadeIn { from { opacity: 0; } }
.modal { width: 380px; max-width: 92vw;
  background: linear-gradient(180deg, #111a2a, #0c1320);
  border: 1px solid var(--line2); border-radius: 14px;
  box-shadow: 0 10px 34px rgba(0, 0, 0, 0.5); padding: 10px;
  position: relative; overflow: hidden;
  animation: modalPop .22s cubic-bezier(0.2,0.9,0.3,1.2); }
.modal::before { content: ''; position: absolute; inset: 0 0 auto 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(47,214,240,0.35), transparent);
  opacity: 0.6; pointer-events: none; }
@keyframes modalPop { from { opacity: 0; transform: scale(0.95) translateY(10px); } }
.modal-head { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 700;
  color: var(--txt); font-family: 'Chakra Petch', var(--font); }
.modal-head .modal-x { flex: none; width: 26px; height: 26px; margin-left: auto; display: flex;
  align-items: center; justify-content: center; background: transparent; border: 1px solid var(--line);
  border-radius: 6px; color: var(--muted); cursor: pointer; }
.modal-head .modal-x:hover { color: var(--txt); border-color: var(--accent); }
.modal-tip { font-size: 12px; color: var(--muted); line-height: 1.6; margin: 8px 0 0; }
.modal-roles { margin: 10px 0 0; padding-left: 10px; font-size: 12px; color: var(--muted);
  line-height: 1.65; }
.modal-roles b { color: var(--txt); font-weight: 600; }
.modal-input { display: flex; align-items: center; gap: 8px; background: #080e19;
  border: 1px solid var(--line2); border-radius: 10px; padding: 0 10px; color: var(--muted);
  transition: border-color .18s, box-shadow .18s; }
.modal-input:focus-within { border-color: rgba(47,214,240,0.55);
  box-shadow: 0 0 0 3px rgba(47,214,240,0.1); }
.modal-input.invalid { border-color: var(--err); }
.modal-input input { flex: 1; background: transparent; border: none; outline: none; color: var(--txt);
  font-size: 14px; padding: 10px 0; }
.modal-err { display: flex; align-items: center; gap: 6px; color: var(--err); font-size: 12px;
  margin: 10px 0 0; }
.modal-btns { display: flex; gap: 10px; margin-top: 16px; }
.modal-btns button { flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px; }
.spin { animation: ctrl-spin .8s linear infinite; }
@keyframes ctrl-spin { to { transform: rotate(360deg); } }

/* ---- 快捷键反馈条 ---- */
#key-hint { position: fixed; left: 50%; bottom: 92px; transform: translateX(-50%);
  z-index: 120; display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 10px; border-radius: 20px; font-size: 12px; color: var(--txt);
  background: var(--glass-solid); border: 1px solid var(--line2);
  box-shadow: 0 10px 34px rgba(0, 0, 0, 0.5); pointer-events: none; white-space: nowrap;
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
/* ★ B1：底部五选一面板打开时让位（抬到面板之上，避免与底栏浮层重叠）。
   ⚠ 静态避让值（300px）按日志面板最大高度估算，仍待浏览器实测微调。 */
#key-hint.lifted { bottom: 300px; }
#key-hint svg { color: var(--accent); }
.kh-fade-enter-active, .kh-fade-leave-active { transition: opacity .18s ease, transform .18s ease; }
.kh-fade-enter-from, .kh-fade-leave-to { opacity: 0; transform: translateX(-50%) translateY(6px); }

/* ---- WebGL 上下文丢失提示 ---- */
#gl-banner { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%);
  z-index: 130; display: inline-flex; align-items: center; gap: 8px;
  padding: 10px; border-radius: 10px; font-size: 13px; font-weight: 600;
  color: var(--warn); background: rgba(17,26,42,0.97);
  border: 1px solid var(--warn-line);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5); pointer-events: none; white-space: nowrap;
  backdrop-filter: blur(12px); }
#gl-banner svg { color: var(--warn); }
.gl-fade-enter-active, .gl-fade-leave-active { transition: opacity .18s ease; }
.gl-fade-enter-from, .gl-fade-leave-to { opacity: 0; }
</style>
