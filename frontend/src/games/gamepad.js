// =====================================================================
// 游戏手柄输入（Xbox / W3C "standard" 布局）—— 井字棋的第三套输入
// （与键盘、鼠标**同一套逻辑动作**，组件只认动作名，不认按钮号）
//
// ★ 为什么单独抽一层：手柄的"按键边沿 / 摇杆死区 / 方向连发节流"都是**纯逻辑**，
//   抽出来后能被 node 直接钉死（tools/verify_tictactoe.mjs），**不需要真手柄**；
//   DOM 侧只保留一件事：轮询 navigator.getGamepads() 并把动作广播给订阅者。
//
// ★ 标准映射（Xbox，Chrome/Edge 下 gp.mapping === "standard"）：
//   按钮 0=A 1=B 2=X 3=Y 4=LB 5=RB 6=LT 7=RT 8=View/Back 9=Menu/Start
//        10=LS 11=RS 12=↑ 13=↓ 14=← 15=→
//   轴   axes[0]=左摇杆X（右为正） axes[1]=左摇杆Y（**下为正**，故"上"= 负）
//
// ★ 逻辑动作（组件按名字处理；未映射的按钮不产生动作）：
//   up/down/left/right  移动光标（十字键优先，其次左摇杆）
//   confirm(A) 落子 | start(Menu) 开始 | pause(X) 暂停⇄继续
//   stop(B) 停止 | replay(Y) 复盘⇄退出 | reset(View) 复位（需二次确认）
//   ★ l1(LB) / r1(RB)：**中性动作名**，本文件不规定含义 —— 由消费方（页面/tab）解释
//     （手柄执行页：LB=停止吸气、RB=停止放气；井字棋页：无这两个键）。
// =====================================================================

/** 标准布局按钮下标。 */
export const BTN = {
  A: 0, B: 1, X: 2, Y: 3, LB: 4, RB: 5, LT: 6, RT: 7,
  VIEW: 8, MENU: 9, LS: 10, RS: 11, UP: 12, DOWN: 13, LEFT: 14, RIGHT: 15,
};

/** 按钮下标 → 逻辑动作（不在表里 = 不产生动作）。
 *  ★★ 2026-10-11：新增 **LB(4)/RB(5) → "l1"/"r1"**。
 *    动机：手柄执行页要用 LB=停止吸气、RB=停止放气，而它俩此前**根本没有映射**
 *    （按了不产生任何动作）。★ 用**中性命** l1/r1 而不是 stop_suck/stop_blow：
 *    同一个动作名在不同页/不同 tab 里含义不同（LB 在执行 tab = 停止吸气；井字棋 tab 无此键），
 *    本文件只负责"哪个键被按了"，**含义由消费方（页面）解释**。 */
export const BTN_ACTION = {
  [BTN.A]: "confirm",
  [BTN.B]: "stop",
  [BTN.X]: "pause",
  [BTN.Y]: "replay",
  [BTN.LB]: "l1",
  [BTN.RB]: "r1",
  [BTN.VIEW]: "reset",
  [BTN.MENU]: "start",
};

/** 按下判定阈值（LT/RT 是 0~1 模拟量，其它按钮通常 0/1）。 */
export const PRESS_T = 0.5;
/** 左摇杆死区（超过才认方向，避免漂移乱动光标）。0.4 ≈ 手柄推到 40% 才动，兼顾灵敏与防抖。 */
export const STICK_DEADZONE = 0.4;
/** ★ 右摇杆死区（**明显更大**）：右摇杆触发的是"下棋/停止"这类动作，
 *  误触一次就可能中断对局 —— 要求推到底（70%）才认，宁可不灵，不可误触。 */
export const RSTICK_DEADZONE = 0.7;
/**
 * ★ 右摇杆方向 → 逻辑动作（用户口径 2026-10-10："右侧是下棋，暂停/继续，停止，复盘等等"）。
 *   按用户列出的顺序**从上方起顺时针**：↑ 下棋 → → 暂停⇄继续 → ↓ 停止 → ← 复盘⇄退出。
 *   ★ **边沿触发**：推一次只发一次，必须回中后才能再次触发（不会"推着不放疯狂下棋"）。
 */
export const RSTICK_ACTION = {
  up: "confirm",     // 下棋
  right: "pause",    // 暂停 ⇄ 继续
  down: "stop",      // 停止（作废）
  left: "replay",    // 复盘 ⇄ 退出
};
/** 方向连发：首按后停 STICK_FIRST_MS，随后每 STICK_REPEAT_MS 一次。 */
export const STICK_FIRST_MS = 280;
export const STICK_REPEAT_MS = 140;

/** 某个按钮是否按下（兼容 {pressed,value} 与裸数字两种形态）。 */
function btnDown(b) {
  if (b && typeof b === "object") return !!(b.pressed || Number(b.value || 0) >= PRESS_T);
  return Number(b || 0) >= PRESS_T;
}

/** 当前按下的按钮下标集合（数组，便于 node 断言）。 */
export function pressedButtons(gp) {
  const out = [];
  const btns = (gp && gp.buttons) || [];
  for (let i = 0; i < btns.length; i++) if (btnDown(btns[i])) out.push(i);
  return out;
}

/** 按钮**边沿**：本次新按下（上次没有、这次有）。 */
export function edgeButtons(prevSet, curArr) {
  const out = [];
  for (const i of curArr) if (!prevSet.has(i)) out.push(i);
  return out;
}

/** 十字键方向（按下即方向；多键同按按 上→下→左→右 优先级）。 */
export function dpadDir(curSet) {
  for (const [idx, name] of [[BTN.UP, "up"], [BTN.DOWN, "down"],
                             [BTN.LEFT, "left"], [BTN.RIGHT, "right"]]) {
    if (curSet.has(idx)) return name;
  }
  return null;
}

/** 左摇杆方向（死区内 → null；取绝对值更大的主轴）。axes[1] 下为正。 */
export function stickDir(gp, deadzone = STICK_DEADZONE) {
  const ax = (gp && gp.axes) || [];
  const x = Number(ax[0] || 0);
  const y = Number(ax[1] || 0);
  const axx = Math.abs(x);
  const axy = Math.abs(y);
  if (axy >= deadzone && axy >= axx) return y < 0 ? "up" : "down";
  if (axx >= deadzone) return x < 0 ? "left" : "right";
  return null;
}

/** 右摇杆方向（axes[2]=rx、axes[3]=ry；死区内 → null）。ry 同样是"下为正"。 */
export function rstickDir(gp, deadzone = RSTICK_DEADZONE) {
  const ax = (gp && gp.axes) || [];
  const x = Number(ax[2] || 0);
  const y = Number(ax[3] || 0);
  const axx = Math.abs(x);
  const axy = Math.abs(y);
  if (axy >= deadzone && axy >= axx) return y < 0 ? "up" : "down";
  if (axx >= deadzone) return x < 0 ? "left" : "right";
  return null;
}

/** 一组新按下的按钮下标 → 逻辑动作（去重、保持顺序）。 */
export function actionsOf(btnIdxList) {
  const out = [];
  for (const i of btnIdxList) {
    const a = BTN_ACTION[i];
    if (a && out.indexOf(a) < 0) out.push(a);
  }
  return out;
}

// ---------------------------------------------------------------- 轮询与广播
// DOM 侧实现：模块级单例（页面同时只跑一个轮询，多订阅者共享）。
let subscribers = [];
let connCbs = new Set();
let rafId = null;
let prevSet = new Set();
let lastDir = null;      // 上一次发出的方向（用于连发节流）
let lastDirAt = 0;       // 下一次允许连发的时刻
let lastRDir = null;     // 上次的右摇杆方向（边沿触发：回中后才能再触发）
let connected = false;
/**
 * 按键"余晖"（**只给界面显示用，不参与动作判定**）。
 * ★ 为什么需要：界面每 200ms 才刷一次，而一次短按往往只有 ~100ms —— 直接读"此刻按住的键"
 *   会**看不见它亮**（动作照样触发，但灯是灭的）。这里记"按过 → 亮 400ms"，
 *   保证"按哪个亮哪个"看得见。
 */
const LIT_MS = 400;
let litMap = new Map();  // 按钮下标 → 失效时刻

function nowMs() {
  return (typeof performance !== "undefined" && performance.now)
    ? performance.now() : Date.now();
}
function pruneLit(t) {
  for (const [k, v] of litMap) if (v <= t) litMap.delete(k);
}
function markLit(idxs, t) {
  for (const i of idxs) litMap.set(Number(i), t + LIT_MS);
  pruneLit(t);
}
/** 当前"亮着"的按键（含余晖），升序。 */
export function litButtons() {
  pruneLit(nowMs());
  return [...litMap.keys()].sort((a, b) => a - b);
}

function emit(action) {
  for (const fn of subscribers.slice()) {
    try { fn(action); } catch (e) { /* 单个订阅者异常不影响其它（也不该打断轮询） */ }
  }
}

function setConnected(v) {
  if (v === connected) return;
  connected = v;
  for (const cb of Array.from(connCbs)) {
    try { cb(connected); } catch (e) { /* 忽略订阅者异常 */ }
  }
}

function pickPad() {
  if (typeof navigator === "undefined" || !navigator.getGamepads) return null;
  const pads = navigator.getGamepads();
  for (let i = 0; i < pads.length; i++) {          // 用下标循环：GamepadList 只保证 length
    const p = pads[i];
    if (p && p.connected) return p;
  }
  return null;
}

/** 浏览器当前可见的**所有**手柄（诊断用：连了几个、都叫什么）。 */
export function readPads() {
  const out = [];
  if (typeof navigator === "undefined" || !navigator.getGamepads) return out;
  const pads = navigator.getGamepads();
  for (let i = 0; i < pads.length; i++) {
    const p = pads[i];
    if (p && p.connected) {
      out.push({ index: i, id: String(p.id || ""), mapping: String(p.mapping || "") });
    }
  }
  return out;
}

// ★ 除了轮询，还要听浏览器的连接事件：
//   ① 有些环境下 `getGamepads()` 要等 gamepadconnected 之后才真的给数据；
//   ② 事件能第一时间拿到设备名（现场排查"到底认到没有"最有用的一行）。
function _onGpConnected(e) {
  const id = (e && e.gamepad && e.gamepad.id) || "";
  try { if (typeof console !== "undefined") console.info("[手柄] 浏览器已识别：" + id); } catch (err) { /* 忽略 */ }
  setConnected(true);
}
function _onGpDisconnected() {
  if (!pickPad()) setConnected(false);
}
let evtBound = false;
function bindEvents() {
  if (evtBound || typeof window === "undefined" || !window.addEventListener) return;
  window.addEventListener("gamepadconnected", _onGpConnected);
  window.addEventListener("gamepaddisconnected", _onGpDisconnected);
  evtBound = true;
}
function unbindEvents() {
  if (!evtBound || typeof window === "undefined" || !window.removeEventListener) return;
  window.removeEventListener("gamepadconnected", _onGpConnected);
  window.removeEventListener("gamepaddisconnected", _onGpDisconnected);
  evtBound = false;
}

// ---------------------------------------------------------------- 后端来源（XInput 直读）
// ★ 现场实况（2026-10-10）：Windows 认到手柄，但 Edge/Chrome 的 getGamepads() 始终 0 个。
//   于是后端用 XInput 直读（/api/gamepad/state），前端**轮询后端**拿按键/摇杆，
//   再喂进与浏览器手柄**完全相同**的 processPad() —— 语义只有一套。
//   规则：**浏览器能认就用浏览器**；浏览器没有才回退后端（一旦浏览器认到就停掉后端轮询，不白耗）。
const BE_POLL_MS = 40;          // 后端轮询周期（25Hz；后端另有 120Hz 采样，短按不会漏）
let backendUrl = "";
let beTimer = null;
let beSnap = null;              // 后端最近一次"已连接"的读数（未连接 → null）
let beAvailable = null;         // 后端能否读手柄（null=还没问到；false=非 Windows/无 XInput）
let source = "none";            // browser | backend | none（界面显示用）

/** 组件注入后端地址（`apiUrl("/gamepad/state")`）；不注入就只用浏览器手柄。 */
export function enableBackendFallback(url) { backendUrl = url || ""; }

async function bePoll() {
  if (!backendUrl || typeof fetch === "undefined") return;
  try {
    const r = await fetch(backendUrl, { cache: "no-store" });
    if (!r.ok) { beSnap = null; return; }
    const d = await r.json();
    beAvailable = !!d.available;
    beSnap = (d && d.connected) ? d : null;
  } catch (e) { beSnap = null; }
}

function startBackendPoll() {
  if (beTimer || !backendUrl || typeof setInterval === "undefined") return;
  bePoll();
  beTimer = setInterval(bePoll, BE_POLL_MS);
}

function stopBackendPoll() {
  if (beTimer) { clearInterval(beTimer); beTimer = null; }
  beSnap = null;
}

/** 后端读数 → 标准布局的"假手柄对象"（这样它与浏览器手柄走同一条处理路径）。 */
function backendPad() {
  const s = beSnap;
  if (!s || !s.connected) return null;
  const set = new Set(Array.isArray(s.buttons) ? s.buttons : []);
  const buttons = [];
  for (let i = 0; i < 16; i++) buttons.push({ pressed: set.has(i), value: set.has(i) ? 1 : 0 });
  const ax = Array.isArray(s.axes) ? s.axes : [];
  return {
    connected: true, id: s.name || "后端 XInput", mapping: "standard", buttons,
    axes: [Number(ax[0] || 0), Number(ax[1] || 0), Number(ax[2] || 0), Number(ax[3] || 0)],
  };
}

/** 手柄松开/无手柄时清干净，避免"上次按着不放"的残留。 */
function releaseAll() {
  if (prevSet.size) prevSet = new Set();
  lastDir = null;
  lastRDir = null;
}

/** 把一个"手柄对象"喂进同一套逻辑：按钮边沿 → 动作；方向（十字键优先/摇杆）带连发。 */
function processPad(gp, now) {
  const cur = pressedButtons(gp);
  const curSet = new Set(cur);
  markLit(cur, now);                       // ★ 界面余晖：按下即亮（短按也看得见）
  // ① 按钮边沿 → 动作（A/B/X/Y/View/Menu）
  for (const a of actionsOf(edgeButtons(prevSet, cur))) emit(a);

  // ② ★ 右摇杆 → 动作"虚拟十字键"（**边沿**：推一次发一次，回中后才能再触发）
  //    为什么不做连发：它触发的是"下棋/停止"，连发会连下好几手、甚至瞬间停掉对局。
  const rd = rstickDir(gp);
  if (rd && rd !== lastRDir) {
    const act = RSTICK_ACTION[rd];
    if (act) emit(act);
  }
  lastRDir = rd;

  // ③ 左摇杆 / 十字键 → 移动光标（首次立即，长按按节流连发）
  const dir = dpadDir(curSet) || stickDir(gp);
  if (dir !== lastDir) {
    if (dir) {
      emit(dir);
      lastDir = dir;
      lastDirAt = now + STICK_FIRST_MS;      // 首按已发 → 下次连发等 STICK_FIRST_MS
    } else {
      lastDir = null;
    }
  } else if (dir && now >= lastDirAt) {
    emit(dir);
    lastDirAt = now + STICK_REPEAT_MS;
  }
  prevSet = curSet;
}

function frame() {
  rafId = (typeof requestAnimationFrame !== "undefined")
    ? requestAnimationFrame(frame) : null;
  const now = (typeof performance !== "undefined" && performance.now)
    ? performance.now() : Date.now();

  // ① 优先用浏览器手柄
  const gp = pickPad();
  if (gp) {
    source = "browser";
    if (beTimer) stopBackendPoll();          // 浏览器认到了 → 不再轮询后端
    processPad(gp, now);
    setConnected(true);
    return;
  }

  // ② 浏览器没有 → 回退后端（Windows XInput 直读）
  if (backendUrl && !beTimer) startBackendPoll();
  const bp = backendPad();
  if (bp) {
    source = "backend";
    processPad(bp, now);
    setConnected(true);
    return;
  }

  // ③ 两路都没有
  source = "none";
  releaseAll();
  setConnected(false);
}

function startLoop() {
  if (rafId !== null) return;
  bindEvents();                                                // 事件与轮询双保险
  if (typeof requestAnimationFrame === "undefined") return;     // node 环境：只做纯逻辑
  rafId = requestAnimationFrame(frame);
}

function stopLoop() {
  if (rafId !== null && typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(rafId);
  rafId = null;
  prevSet = new Set();
  lastDir = null;
  lastRDir = null;
  litMap = new Map();          // 灯全灭
  stopBackendPoll();
  source = "none";
  unbindEvents();
}

/**
 * 订阅逻辑动作（返回取消订阅函数）。首个订阅者启动轮询、末个取消时停止。
 * @param {(action:string)=>void} fn
 */
export function subscribeGamepad(fn) {
  if (typeof fn === "function") subscribers.push(fn);
  if (subscribers.length === 1) startLoop();
  return () => {
    subscribers = subscribers.filter((f) => f !== fn);
    if (!subscribers.length) stopLoop();
  };
}

/** 是否检测到手柄（订阅连接变化用；立即回调一次当前状态）。 */
export function gamepadConnected() { return connected; }

export function onGamepadConnection(cb) {
  if (typeof cb === "function") {
    connCbs.add(cb);
    try { cb(connected); } catch (e) { /* 忽略 */ }
  }
  return () => connCbs.delete(cb);
}

// ---------------------------------------------------------------- 自检 / 测试用
/** 逻辑动作 → 中文说明（测试盒的"最近动作"日志与对照表用）。
 *  ★★ 2026-10-11：井字棋「复位」与「复盘」已**合并为「复原」**（真机搬回原位，3 秒内二次确认）。
 *     `replay` / `reset` 两个**动作名**保留（多处映射与回归都依赖它们），但语义都是「复原」——
 *     文案必须跟着改，否则帮助卡会让操作员以为 Y 是"只查看的复盘"（实际会真动机器人）。 */
export const ACTION_CN = {
  up: "移动 ↑", down: "移动 ↓", left: "移动 ←", right: "移动 →",
  confirm: "落子", start: "开始", pause: "暂停 ⇄ 继续",
  stop: "停止（作废）",
  replay: "复原（真机）", reset: "复原（真机）",
  // ★ 中性动作名（含义由页面解释）——这里只给"键名"，不假装它等于某个业务动作。
  l1: "LB", r1: "RB",
};

/** 按钮下标 → 键名（测试盒实时读数用）。 */
export const BUTTON_CN = {
  0: "A", 1: "B", 2: "X", 3: "Y", 4: "LB", 5: "RB", 6: "LT", 7: "RT",
  8: "View", 9: "Menu", 10: "LS", 11: "RS",
  12: "↑", 13: "↓", 14: "←", 15: "→",
};

/**
 * 手柄**实时快照**（测试盒展开时轮询显示）。
 * ★ 只读，不动任何状态：不接手柄 → connected=false，界面照样安全。
 */
export function readPad() {
  const list = readPads();
  const count = list.length;
  const ids = list.map((d) => d.id);
  const lit = litButtons();          // ★ 显示用：含 400ms 余晖（"按哪个亮哪个"）
  const gp = pickPad();
  if (gp) {
    return {
      connected: true, source: "browser",
      id: String(gp.id || ""), mapping: String(gp.mapping || ""),
      buttons: lit,
      axes: Array.from(gp.axes || []).map((v) => Number(Number(v).toFixed(3))),
      count, ids, backendAvailable: beAvailable, idleSec: null,
    };
  }
  // ★ 浏览器没有 → 用后端的读数显示（界面要显示"真实生效的那一路"）
  const s = beSnap;
  if (s && s.connected) {
    return {
      connected: true, source: "backend",
      id: s.name || "后端 XInput", mapping: "standard",
      buttons: lit,
      axes: (s.axes || []).slice(0, 4).map((v) => Number(Number(v).toFixed(3))),
      count: 1, ids: [s.name || "后端 XInput"], backendAvailable: beAvailable,
      // ★ 诊断"连了却不动"：距上次输入多少秒（null = 开机以来一次都没收到）
      idleSec: (typeof s.idle_sec === "number") ? s.idle_sec : null,
    };
  }
  return {
    connected: false, source: "none",
    id: "", mapping: "", buttons: lit, axes: [0, 0],
    count, ids, backendAvailable: beAvailable, idleSec: null,
  };
}

/** 当前生效的输入来源：browser | backend | none（界面显示"到底谁在驱动"）。 */
export function gamepadSource() { return source; }

/**
 * 模拟一次逻辑动作 —— 测试盒"点按钮 = 按手柄"用。
 *
 * ★ 它**不是另抄一套**：直接走 `emit()` 这个与真手柄完全相同的广播出口，
 *   所以模拟出来的结果就是真按键的结果（"触发什么条件"测得准）。
 */
export function simulateAction(action) {
  if (typeof action === "string" && action) emit(action);
}
