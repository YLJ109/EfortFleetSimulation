<script setup>
// =====================================================================
// 实时语音字幕层（挂 3D 视口，电影字幕观感）
//
//   位置：视口底部**偏上一点**居中（避开底部状态栏与左下角运行日志面板）
//   两行：上一整句（亮白，定稿后停留几秒淡出） + 当前句（灰白 + 呼吸光标）
//   开关：左上角小麦克风按钮；未启用/出错时**把原因写在旁边**，不静默失败
//
//   ★ 识别文本只用于显示，不解析成任何指令、不参与机器人控制。
//   ★ 本层默认 pointer-events:none，只有按钮可点 —— 不挡 3D 视口的旋转/缩放。
// =====================================================================
import { onMounted, onBeforeUnmount, computed } from "vue";
import Icon from "./Icon.vue";
import { useAsrStore } from "../stores/asr.js";
import { useAuthStore } from "../stores/auth.js";

const asr = useAsrStore();
const auth = useAuthStore();

onMounted(() => { asr.loadConfig(); });
onBeforeUnmount(() => { asr.stop(); });

function toggle() {
  if (asr.active || asr.connecting) asr.stop();
  else asr.start();
}

const tip = computed(() => {
  if (!auth.controlActive) return "需要控制令牌（点右下角「请求控制」）";
  if (asr.err) return asr.err;
  if (!asr.available) return asr.reason || "语音识别未启用";
  if (asr.active) return "点击停止语音字幕";
  if (asr.connecting) return "正在连接云端…";
  return "点击开始语音字幕（说话实时转文字）";
});

// 音量条：识别中给一点"在听"的反馈（1~4 格）
const bars = computed(() => {
  if (!asr.active) return 0;
  const l = asr.level || 0;
  if (l > 0.18) return 4;
  if (l > 0.08) return 3;
  if (l > 0.03) return 2;
  return 1;
});
</script>

<template>
  <div class="asr-layer">
    <!-- 麦克风开关 + 状态：左上角，与右上角的"鼠标操作提示"错开 -->
    <div class="asr-bar" :class="{ on: asr.active, err: !!asr.err }">
      <button class="asr-btn" type="button" :title="tip" @click="toggle">
        <Icon :name="asr.active || asr.connecting ? 'mic' : 'micOff'" :size="15" />
        <span class="asr-bars" v-if="asr.active">
          <i v-for="n in 4" :key="n" :class="{ lit: n <= bars }"></i>
        </span>
      </button>
      <span v-if="asr.err" class="asr-msg err" @click="asr.clearErr()">
        <Icon name="alert" :size="12" /> {{ asr.err }}
      </span>
      <span v-else-if="!asr.available && asr.reason" class="asr-msg">
        {{ asr.reason }}
      </span>
      <span v-else-if="asr.connecting" class="asr-msg">正在连接云端…</span>
    </div>

    <!-- 字幕本体：电影字幕样式（底部偏上、居中、半透明黑底 + 描边） -->
    <transition name="sub">
      <div v-if="asr.visible" class="asr-subs">
        <p v-if="asr.finalText" class="sub-line sub-final">{{ asr.finalText }}</p>
        <p v-if="asr.partial" class="sub-line sub-partial">
          {{ asr.partial }}<i class="caret"></i>
        </p>
      </div>
    </transition>
  </div>
</template>

<style scoped>
/* 整层不挡操作：只有按钮和错误提示可点 */
.asr-layer { position: absolute; inset: 0; pointer-events: none; z-index: 6; }

/* 开关放**右上角**：左上角是位姿 HUD(.sim-hud)、左下角是操作提示(.hud-tip)与
   运行日志面板、右下角是安全围栏片(#safety-chip) —— 只有右上角是空的。
   字幕本体仍在底部居中（"像电影字幕一样"），两者互不干扰。 */
.asr-bar { position: absolute; right: 12px; top: 10px; display: flex;
  flex-direction: row-reverse; align-items: center; gap: 8px; pointer-events: auto; }
.asr-msg { text-align: right; }
.asr-btn { display: flex; align-items: center; gap: 6px; padding: 5px 9px;
  border-radius: 999px; border: 1px solid var(--line);
  background: color-mix(in srgb, var(--panel) 82%, transparent);
  color: var(--muted); cursor: pointer; backdrop-filter: blur(4px); }
.asr-btn:hover { color: var(--txt); border-color: var(--accent); }
.asr-bar.on .asr-btn { color: #fff; background: #c0392b; border-color: #c0392b; }
.asr-bar.err .asr-btn { color: var(--danger, #e05252); border-color: var(--danger, #e05252); }
.asr-bars { display: inline-flex; align-items: flex-end; gap: 2px; height: 12px; }
.asr-bars i { width: 2px; height: 4px; background: currentColor; opacity: .35; }
.asr-bars i.lit { opacity: 1; }
.asr-bars i:nth-child(1) { height: 4px; }
.asr-bars i:nth-child(2) { height: 7px; }
.asr-bars i:nth-child(3) { height: 10px; }
.asr-bars i:nth-child(4) { height: 12px; }
.asr-msg { font-size: 11px; line-height: 1.4; max-width: min(52vw, 560px);
  padding: 4px 8px; border-radius: 6px; color: var(--muted);
  background: color-mix(in srgb, var(--panel) 86%, transparent);
  border: 1px solid var(--line); }
.asr-msg.err { color: #ffb4b4; border-color: #e0525255; cursor: pointer; }

/* 字幕：底部偏上居中 —— "像电影字幕一样" */
.asr-subs { position: absolute; left: 50%; bottom: 13%;
  transform: translateX(-50%); width: max-content; max-width: min(86%, 1100px);
  text-align: center; }
.sub-line { margin: 0 0 4px; font-weight: 600; letter-spacing: .5px;
  line-height: 1.45; padding: 2px 10px; border-radius: 6px;
  background: rgba(0, 0, 0, .48);
  text-shadow: 0 1px 2px #000, 0 0 6px rgba(0, 0, 0, .85);
  font-size: clamp(15px, 2.1vw, 30px); color: #fff; }
/* 上一整句：略暗，表示"已定稿" */
.sub-final { color: #e8e8e8; }
/* 当前句：更亮 + 呼吸光标 */
.sub-partial { color: #fff; opacity: .96; }
.caret { display: inline-block; width: 2px; height: 1em; margin-left: 3px;
  background: currentColor; vertical-align: -2px; animation: blink 1s steps(1) infinite; }
@keyframes blink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0; } }

.sub-enter-active, .sub-leave-active { transition: opacity .28s ease, transform .28s ease; }
.sub-enter-from, .sub-leave-to { opacity: 0; transform: translate(-50%, 6px); }
</style>
