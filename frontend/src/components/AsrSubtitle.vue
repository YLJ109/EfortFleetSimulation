<script setup>
// =====================================================================
// 实时语音字幕层（挂 3D 视口，电影字幕观感）
//
//   位置：视口底部**偏上一点**居中（避开底部状态栏与左下角运行日志面板）
//   两行：上一整句（亮白，定稿后停留几秒淡出） + 当前句（灰白 + 呼吸光标）
//
//   ★ 开关在**最底部状态栏**（与"播报"按钮同款，见 App.vue 的 #control-zone）——
//     本组件只负责"显示"，不再自带按钮，避免同一功能出现两个入口。
//   ★ 本组件是**纯展示**、无生命周期副作用：它在 MonitorLayout 里，会随视图切换
//     卸载/重建；若在这里 stop() 识别，切一次页面就把语音中断了 —— 所以
//     配置拉取与状态保持都放在全局（App.vue / asr store）。
//   ★ 识别文本只用于显示，不解析成任何指令、不参与机器人控制。
//   ★ 本层 pointer-events:none —— 不挡 3D 视口的旋转/缩放。
// =====================================================================
import { useAsrStore } from "../stores/asr.js";

const asr = useAsrStore();
</script>

<template>
  <!-- 字幕本体：电影字幕样式（底部偏上、居中、半透明黑底 + 描边） -->
  <transition name="sub">
    <div v-if="asr.visible" class="asr-subs">
      <p v-if="asr.finalText" class="sub-line sub-final">{{ asr.finalText }}</p>
      <p v-if="asr.partial" class="sub-line sub-partial">
        {{ asr.partial }}<i class="caret"></i>
      </p>
    </div>
  </transition>
</template>

<style scoped>
/* 字幕：底部偏上居中 —— "像电影字幕一样" */
.asr-subs { position: absolute; left: 50%; bottom: 13%;
  transform: translateX(-50%); width: max-content; max-width: min(86%, 1100px);
  text-align: center; pointer-events: none; z-index: 6; }
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
