// =====================================================================
// 统一播报中心：分级音效（WebAudio 合成）+ 中文语音（Web Speech API）。
//
// 设计要点：
//   - 零素材、零依赖：音效全部用 Oscillator 合成，语音用浏览器原生 TTS，可离线。
//   - 优先级队列：critical > error > warn > info；高优先级抢占当前语音。
//   - 节流去重：同一 key 在 minInterval 内只播一次；状态未变化不重复播报。
//   - 配置持久化到 localStorage；静音时 critical 可配置"强制突破"。
//   - 只做表现层：关掉它不影响任何安全链路（安全由后端互锁兜底）。
//
// ★ 对外导出名一律加 `ann` 前缀（annCfg / annHistory）：cfg / history 这类通用名
//   会和別处的函数形参、对象键撞名，触发 tools/check_imports.mjs 的假阳性守卫。
// =====================================================================
import { ref } from "vue";

const LS_KEY = "efort.announce";

export const LEVEL_RANK = { info: 1, warn: 2, error: 3, critical: 4 };

// 各级别最小重复间隔（毫秒）
const MIN_INTERVAL = { info: 15000, warn: 30000, error: 10000, critical: 5000 };

const DEFAULT_CFG = {
  enabled: true,        // 总开关
  sound: true,          // 音效
  voice: true,          // 中文语音
  volume: 0.5,          // 0~1（同时作用于音效与语音）
  rate: 1.0,            // 语速
  pitch: 1.0,
  voiceURI: "",         // 空 = 自动挑中文音色
  forceCritical: true,  // 静音时 critical 仍然播报
  quiet: { info: true, warn: false, error: false, critical: false, vision: false },
};

// ---- 配置（响应式，面板直接绑） ----
function loadCfg() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEY) || "null");
    if (s && typeof s === "object") {
      return { ...DEFAULT_CFG, ...s, quiet: { ...DEFAULT_CFG.quiet, ...(s.quiet || {}) } };
    }
  } catch (e) { /* 忽略 */ }
  return { ...DEFAULT_CFG, quiet: { ...DEFAULT_CFG.quiet } };
}
export const annCfg = ref(loadCfg());

/**
 * 播报中心面板是否开着。
 * 放在服务层而不是组件里，是因为**两个地方要知道它**：大屏标题条上的按钮要开它，
 * App 的全局 Esc/Enter 分派要判断"弹层开着时按键归弹层、不归急停"。
 * 用 provide/inject 也能做，但 Esc 的判据是安全相关的，不该依赖组件树形状。
 */
export const announceOpen = ref(false);

export function setConfig(patch) {
  annCfg.value = { ...annCfg.value, ...patch, quiet: { ...annCfg.value.quiet, ...(patch.quiet || {}) } };
  try { localStorage.setItem(LS_KEY, JSON.stringify(annCfg.value)); } catch (e) { /* 忽略 */ }
  if (!annCfg.value.enabled) stopLoop();
}

// ---- 播报历史（面板展示） ----
export const annHistory = ref([]);
function pushHistory(level, text) {
  annHistory.value.unshift({ t: new Date(), level, text });
  if (annHistory.value.length > 20) annHistory.value.pop();
}
export function clearHistory() { annHistory.value = []; }

// ---------------- WebAudio 音效 ----------------
let ctx = null;
let unlocked = false;

function audio() {
  // ★ 用户手势之前**绝不**创建 AudioContext。
  //   浏览器自动播放策略下，无手势创建会直接抛控制台告警
  //   "The AudioContext was not allowed to start"，而且创建出来的实例会永久停在
  //   suspended（后续也不会因为有了手势就自动恢复）。所以未解锁时直接返回 null，
  //   静默跳过本次发声，等首次手势由 unlockAudio() 把上下文建好。
  if (!unlocked) return null;
  if (!ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { ctx = new AC(); } catch (e) { return null; }
  }
  if (ctx.state === "suspended") ctx.resume();
  return ctx;
}

/** 用户首次手势时调用（浏览器自动播放策略要求）。 */
export function unlockAudio() {
  if (unlocked && ctx) return true;
  unlocked = true;                 // 先置位：audio() 内部据此才肯真正创建上下文
  const ac = audio();
  if (!ac) unlocked = false;       // 创建失败（浏览器不支持）→ 退回未解锁，下次手势再试
  return !!ac;
}
export function audioReady() { return unlocked && !!ctx; }

function tone(freq, dur, wave = "sine", vol = 0.12, at = 0) {
  const ac = audio();
  if (!ac) return;
  const t0 = ac.currentTime + at;
  const osc = ac.createOscillator();
  const g = ac.createGain();
  osc.type = wave;
  osc.frequency.setValueAtTime(freq, t0);
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(Math.max(0.0002, vol), t0 + 0.012);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g);
  g.connect(ac.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.03);
}

/** 音效库：全部合成，无外部文件。 */
export const SOUNDS = {
  ok: () => { tone(660, 0.12, "sine", 0.10); tone(880, 0.16, "sine", 0.10, 0.11); },
  info: () => { tone(880, 0.08, "sine", 0.07); },
  warn: () => { tone(660, 0.10, "triangle", 0.10); tone(660, 0.10, "triangle", 0.10, 0.15); },
  error: () => { tone(520, 0.11, "square", 0.09); tone(520, 0.11, "square", 0.09, 0.15); tone(520, 0.11, "square", 0.09, 0.30); },
  critical: () => {
    for (let i = 0; i < 6; i++) tone(i % 2 ? 1180 : 880, 0.15, "sawtooth", 0.11, i * 0.17);
  },
  vision: () => { tone(990, 0.09, "sine", 0.08); tone(1320, 0.11, "sine", 0.08, 0.08); },
};

function playSound(name) {
  if (!annCfg.value.enabled || !annCfg.value.sound) return;
  const f = SOUNDS[name] || SOUNDS.info;
  try { f(); } catch (e) { /* 忽略 */ }
}

// ---------------- 语音 ----------------
let voices = [];
function loadVoices() {
  if (typeof speechSynthesis === "undefined") return;
  voices = speechSynthesis.getVoices() || [];
}
if (typeof speechSynthesis !== "undefined") {
  loadVoices();
  speechSynthesis.onvoiceschanged = loadVoices;
}

export function listVoices() {
  if (!voices.length) loadVoices();
  return voices.filter((v) => (v.lang || "").toLowerCase().startsWith("zh"));
}

function pickVoice() {
  if (!voices.length) loadVoices();
  if (!voices.length) return null;
  if (annCfg.value.voiceURI) {
    const hit = voices.find((v) => v.voiceURI === annCfg.value.voiceURI);
    if (hit) return hit;
  }
  const zh = voices.filter((v) => (v.lang || "").toLowerCase().startsWith("zh"));
  return zh[0] || voices[0];
}

function speakUtter(text) {
  return new Promise((resolve) => {
    if (typeof speechSynthesis === "undefined") { resolve(); return; }
    try {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      const v = pickVoice();
      if (v) u.voice = v;
      u.lang = (v && v.lang) || "zh-CN";
      u.rate = annCfg.value.rate;
      u.pitch = annCfg.value.pitch;
      u.volume = annCfg.value.volume;
      u.onend = () => resolve();
      u.onerror = () => resolve();
      speechSynthesis.speak(u);
    } catch (e) { resolve(); }
  });
}

// ---------------- 队列 ----------------
const queue = [];
let pumping = false;
const lastAt = new Map();   // key -> 时间戳

function muted(level) {
  if (!annCfg.value.enabled) return !(annCfg.value.forceCritical && level === "critical");
  return false;
}

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    while (queue.length) {
      // 取优先级最高的一条
      queue.sort((a, b) => (LEVEL_RANK[b.level] || 1) - (LEVEL_RANK[a.level] || 1));
      const item = queue.shift();
      if (!item) break;
      pushHistory(item.level, item.text);
      playSound(item.sound || item.level);
      if (annCfg.value.voice && !muted(item.level)) {
        for (let i = 0; i < (item.repeat || 1); i++) {
          await speakUtter(item.text);
          if (i + 1 < (item.repeat || 1)) await new Promise((r) => setTimeout(r, 400));
        }
      } else {
        await new Promise((r) => setTimeout(r, 450));
      }
    }
  } finally {
    pumping = false;
  }
}

/**
 * 播报一条。
 * @param {object} o { level, text, key, sound, repeat, force }
 */
export function announce(o) {
  const level = o.level || "info";
  const text = (o.text || "").trim();
  if (!text) return false;
  const key = o.key || level + ":" + text;
  const min = o.force ? 0 : (MIN_INTERVAL[level] ?? 15000);
  const now = Date.now();
  if (min > 0 && now - (lastAt.get(key) || 0) < min) return false;
  lastAt.set(key, now);
  queue.push({ level, text, sound: o.sound, repeat: o.repeat || 1 });
  pump();
  return true;
}

export function stopSpeak() {
  try { if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel(); } catch (e) { /* 部分浏览器取消会抛，忽略即可 */ }
  queue.length = 0;
}

/** 试听（面板用）：不受节流与静音限制。 */
export function testAnnounce(level, text) {
  playSound(level === "critical" ? "critical" : level);
  return announce({ level, text: text || "播报测试", key: "test:" + level + ":" + Date.now(), force: true });
}

// ---------------- 围栏循环报警（替代旧的单一蜂鸣） ----------------
let loopTimer = null;
let loopState = "safe";
let loopVoiceTimer = null;

export const FENCE_TEXT = {
  warn: "预警：机器人接近警戒区，请注意",
  danger: "报警：已进入危险区，运动已禁止",
  hit: "紧急：机器人越界，请立即急停",
  safe: "安全围栏已恢复正常",
};

function stopLoop() {
  if (loopTimer) { clearInterval(loopTimer); loopTimer = null; }
  if (loopVoiceTimer) { clearInterval(loopVoiceTimer); loopVoiceTimer = null; }
}

/**
 * 围栏状态变化入口（兼容 utils/alarm.js 的 updateAlarm 语义）。
 * 状态不变不会重复播报；hit 才会持续鸣响 + 循环语音。
 */
export function fenceAlarm(state) {
  const s = state || "safe";
  if (s === loopState) return;
  const prev = loopState;
  loopState = s;
  stopLoop();

  if (s === "safe") {
    if (prev === "hit" || prev === "danger" || prev === "warn") {
      playSound("ok");
      announce({ level: "info", text: FENCE_TEXT.safe, key: "fence:safe", force: true });
    }
    return;
  }
  const level = s === "hit" ? "critical" : s === "danger" ? "error" : "warn";
  announce({
    level, text: FENCE_TEXT[s] || ("围栏状态：" + s),
    key: "fence:" + s, force: true,
    repeat: s === "hit" ? 2 : 1,
  });
  if (s === "hit" || s === "danger") {
    // 持续鸣响：hit 330ms 一拍，danger 1s 一拍
    const period = s === "hit" ? 1200 : 3000;
    loopTimer = setInterval(() => playSound(s === "hit" ? "critical" : "error"), period);
    if (s === "hit") {
      loopVoiceTimer = setInterval(() => announce({
        level: "critical", text: FENCE_TEXT.hit, key: "fence:hit", force: true,
      }), 6000);
    }
  }
}

export function fenceState() { return loopState; }

/** 旧 API 兼容：开关声音报警（true=启用）。 */
export function setAlarmEnabled(v) {
  setConfig({ sound: !!v });
  if (!v) stopLoop();
}
export function getAlarmEnabled() { return !!annCfg.value.sound; }
