// =====================================================================
// 语音播报（TTS）—— 用浏览器原生 speechSynthesis（零依赖、离线可用）。
//
// 用户口径（2026-10-09）：
//   "增强语音播报，每次动作都要播报一下，报错，执行过程，颜色，
//    下一个点去哪儿，已到达等等"
//
// 设计：
//   · **播报内容走"口语短句"**，不是把日志原文念出来 ——
//     "阶段：取料·下落" 念出来很怪，要说"去 A1 取料"。
//     映射表 `stageSpeech()` 有回归脚本钉着（改了 stage 文案它会红）。
//   · **同一阶段只播一次**：调用方（logTick/_logTick）已经有"签名变化才触发"
//     的节流，这里不再重复节流 —— 但 `speak()` 自身也做 8s 内同文本去重兜底。
//   · **静音开关**持久化到 localStorage（用户偏好，合理持久）。
//   · node 环境 / 不支持 TTS 的浏览器 → 全部安全降级为 no-op（绝不抛异常，
//     这与 events.py 的"永不抛异常"同一哲学：播报是锦上添花，不能影响主流程）。
// =====================================================================

const MUTE_KEY = "ai.speak.muted";

let _muted = null;          // lazy 读 localStorage（SSR/测试安全）
let _lastText = "";
let _lastAt = 0;

function _ls() {
  try {
    return (typeof localStorage !== "undefined") ? localStorage : null;
  } catch (e) { return null; }
}

export function isMuted() {
  if (_muted === null) {
    const ls = _ls();
    _muted = ls ? ls.getItem(MUTE_KEY) === "1" : false;
  }
  return _muted;
}

export function setMuted(m) {
  _muted = !!m;
  const ls = _ls();
  try { if (ls) ls.setItem(MUTE_KEY, _muted ? "1" : "0"); } catch (e) { /* 忽略 */ }
  if (_muted) _cancel();              // 静音的瞬间把正在念的停掉
}

/** 播报一句。重复文本 8s 内不重复念（防"刷屏式播报"）。 */
export function speak(text) {
  const t = String(text || "").trim();
  if (!t || isMuted()) return false;
  const now = Date.now();
  if (t === _lastText && now - _lastAt < 8000) return false;
  _lastText = t;
  _lastAt = now;
  try {
    const synth = (typeof window !== "undefined" && window.speechSynthesis) || null;
    if (!synth) return false;
    const u = new SpeechSynthesisUtterance(t);
    u.lang = "zh-CN";
    u.rate = 1.15;                      // 稍快：现场播报要跟得上动作
    u.volume = 1.0;
    synth.speak(u);
    return true;
  } catch (e) {
    return false;                        // TTS 不可用 → 静默降级（绝不影响主流程）
  }
}

/** 停掉正在念的（切静音时用）。 */
export function _cancel() {
  try {
    if (typeof window !== "undefined" && window.speechSynthesis) {
      window.speechSynthesis.cancel();
    }
  } catch (e) { /* 忽略 */ }
}

/**
 * 执行状态 → **口语短句**。
 *
 * ★ 播报的是"操作员要听的"，不是"日志里写的"：
 *     日志 `取料·下落` → 说 "去 A1 取料"
 *     日志 `放料·正上方` → 说 "送到 B1"
 *     出错 → 说错误原因（截断到 40 字）
 *   ★ stage 文案是后端定的，改了它这里必须跟 —— 有回归脚本钉着
 *     （tools/verify_aichat.mjs 会断言"每个已知 stage 都能映射出非空播报"）。
 */
export function stageSpeech(state) {
  const s = state || {};
  const t = String(s.stage || "");
  const src = s.src || "";
  const tgt = s.target || "";
  if (s.error) {
    const why = s.error.length > 40 ? s.error.slice(0, 40) : s.error;
    return "出错了：" + why;
  }
  if (t.includes("取料")) return (src ? "去 " + src + " " : "") + "取料";
  if (t.includes("判色")) return s.color ? "颜色是" + s.color : "正在判色";
  if (t.includes("放料")) return (tgt ? "送到 " + tgt + " " : "") + "放料";
  if (t.includes("回起点") || t.includes("回开始")) return "回到起点";
  if (t.includes("结束")) return s.ok ? "完成" : "停止";
  if (t.includes("抬起")) return "抬起";
  if (t.includes("送检")) return "送到检测位";
  if (t.includes("停顿")) return "等待检测";
  if (t.includes("准备")) return "";
  return t;                              // 认不出的 stage 原样播（总比没声音好）
}

/** 播报测试权重：供 UI 显示"当前要说的话"（调试/演示用）。 */
export function previewSpeech(state) {
  const muted = isMuted();
  return { text: stageSpeech(state), muted };
}
