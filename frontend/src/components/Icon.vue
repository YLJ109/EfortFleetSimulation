<script setup>
// 统一 SVG 线性图标库（去 emoji）。stroke 跟随 currentColor，可整体换色/缩放。
// 用法：<Icon name="camera" :size="18" />
import { computed } from "vue";

const props = defineProps({
  name: { type: String, required: true },
  size: { type: [Number, String], default: 18 },
});

// 24x24 viewBox，线性图标；个别需要填充的用 fill="currentColor" stroke="none"
const ICONS = {
  camera: '<rect x="2" y="6" width="13" height="12" rx="2"/><path d="M15 10l6-3v10l-6-3z"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 12h16M8 4v2M16 4v2M8 18v2M16 18v2" fill="none" stroke="currentColor"/>',
  copy: '<rect x="5" y="3" width="13" height="16" rx="2"/><rect x="9" y="7" width="9" height="12" rx="1.5"/>',
  chevronDown: '<path d="M6 9l6 6 6-6"/>',
  chevronLeft: '<path d="M15 6l-6 6 6 6"/>',
  chevronRight: '<path d="M9 6l6 6-6 6"/>',
  chevronUp: '<path d="M6 15l6-6 6 6"/>',
  play: '<path d="M7 5l12 7-12 7z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor" stroke="none"/><rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor" stroke="none"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>',
  record: '<circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.3"/><path d="M21 4v5h-5"/>',
  power: '<path d="M12 3v9"/><path d="M7.5 7a7 7 0 1 0 9 0"/>',
  eject: '<path d="M5 11l7-7 7 7z" fill="currentColor" stroke="none"/><rect x="5" y="14" width="14" height="5" rx="1"/>',
  snapshot: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.4"/>',
  grip: '<circle cx="9" cy="6" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="6" r="1.3" fill="currentColor" stroke="none"/><circle cx="9" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="9" cy="18" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="18" r="1.3" fill="currentColor" stroke="none"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  unlock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.4-1.8"/>',
  shield: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  alert: '<path d="M12 3l9 16H3z"/><path d="M12 9v5"/><circle cx="12" cy="17" r="1.1" fill="currentColor" stroke="none"/>',
  check: '<path d="M5 12l5 5L19 6"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
  download: '<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/>',
  upload: '<path d="M12 21V9"/><path d="M7 13l5-5 5 5"/><path d="M4 4h16"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  drag: '<path d="M9 4l-2 2 2 2M15 4l2 2-2 2M9 14l-2 2 2 2M15 14l2 2-2 2"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L5 16v4z"/><path d="M14 6l4 4"/>',
  trash: '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6"/>',
  // 清除报错（清控制器报警）：橡皮擦 —— 与"删除"用不同的图形，避免误读成删数据
  eraser: '<path d="M4.4 14.6l7.2-7.2a2 2 0 0 1 2.8 0l4.6 4.6a2 2 0 0 1 0 2.8L14.4 19.4H8.2z"/><path d="M9.8 9.2l6.6 6.6"/><path d="M4.4 19.4h15.2"/>',

  // ---- 阶段 3/4：运维审计页 ----
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  activity: '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
  heart: '<path d="M20.5 8.6c0 4.6-8.5 10.4-8.5 10.4S3.5 13.2 3.5 8.6a4.6 4.6 0 0 1 8.5-2.6 4.6 4.6 0 0 1 8.5 2.6z"/>',
  database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3.2 2"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 8v4.4l3.2 1.9"/>',
  compare: '<path d="M6 4v16"/><circle cx="6" cy="8" r="2.2"/><circle cx="6" cy="16" r="2.2"/><path d="M18 4v6"/><circle cx="18" cy="14" r="2.2"/><path d="M18 4c0 4-12 3-12 8"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4-2v-4z"/>',
  shieldCheck: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
  rotateLeft: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  wifi: '<path d="M5 12.5a10 10 0 0 1 14 0"/><path d="M8.5 16a5.5 5.5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1" fill="currentColor" stroke="none"/>',
  wifiOff: '<path d="M3 3l18 18"/><path d="M8.5 16a5.5 5.5 0 0 1 3-1.5"/><path d="M5 12.5a10 10 0 0 1 4-2.4"/><path d="M12 7.3c1.2 0 2.4.2 3.5.6"/><circle cx="12" cy="19.5" r="1" fill="currentColor" stroke="none"/>',

  // ---- 阶段 5：播报中心 / 操作引导 / 视觉 ----
  volume: '<path d="M11 5L6 9H3v6h3l5 4z" fill="currentColor" stroke="none"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
  volumeOff: '<path d="M11 5L6 9H3v6h3l5 4z" fill="currentColor" stroke="none"/><path d="M22 9l-6 6M16 9l6 6"/>',
  // ★ 实时语音字幕：麦克风开/关（线性图标，与其它图标同款描边风格）
  mic: '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10a7 7 0 0 1-14 0"/><path d="M12 17v5"/>',
  micOff: '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10a7 7 0 0 1-14 0"/><path d="M12 17v5"/><path d="M4 4l16 16"/>',
  bell: '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 8-3 8h18s-3-1-3-8"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
  siren: '<path d="M12 3v3M5 6l2 2M19 6l-2 2M4 13h16"/><rect x="6" y="13" width="12" height="7" rx="2"/><path d="M9 20h6"/>',
  megaphone: '<path d="M3 11v2a2 2 0 0 0 2 2h1l8 5V6L6 11H5a2 2 0 0 0-2 2z"/><path d="M18 8a5 5 0 0 1 0 8"/>',
  plug: '<path d="M9 2v6M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 2-.9 2-2 0-1.1-.9-2-.9-3 0-1.1.9-2 2-2h2.4A4.5 4.5 0 0 0 21 9.5C21 5.9 16.9 3 12 3z"/><circle cx="8" cy="10" r="1.1" fill="currentColor" stroke="none"/><circle cx="12" cy="7.5" r="1.1" fill="currentColor" stroke="none"/><circle cx="16" cy="10" r="1.1" fill="currentColor" stroke="none"/>',
  sliders: '<path d="M4 8h10M18 8h2M4 16h4M12 16h8"/><circle cx="16" cy="8" r="2"/><circle cx="10" cy="16" r="2"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  pin: '<path d="M12 21s-7-7.5-7-12a7 7 0 0 1 14 0c0 4.5-7 12-7 12z"/><circle cx="12" cy="8.5" r="2.6"/>',
  note: '<path d="M5 3h14v18H5z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  flask: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3"/><path d="M7.5 15h9"/>',
  home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
  // 井字棋（3×3 棋盘格）
  grid: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/>',

  // ---- 品牌标识：机器人头（顶部栏 logo 用，也用于"机器人"语义） ----
  robot: '<path d="M12 3.4v2.4"/><circle cx="12" cy="2.3" r="1.1" fill="currentColor" stroke="none"/><path d="M2.6 10.6v3M21.4 10.6v3"/><rect x="4" y="5.8" width="16" height="11.2" rx="3.2"/><circle cx="9" cy="10.8" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="10.8" r="1.3" fill="currentColor" stroke="none"/><path d="M9.6 14.1h4.8"/>',

  // ---- 说明 / 帮助类（「关于」页与快捷键说明用） ----
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 10.6v6"/><circle cx="12" cy="7.6" r="1.05" fill="currentColor" stroke="none"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 1.9-2.8 2.3-2.8 4"/><circle cx="12" cy="17.4" r="1.05" fill="currentColor" stroke="none"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2.2"/><path d="M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01M7.5 13.6h9"/>',
  gamepad: '<path d="M7 8h10a5 5 0 0 1 4.9 4l.6 4.2a2.6 2.6 0 0 1-4.6 2.1l-1-1.3H7l-1 1.3a2.6 2.6 0 0 1-4.6-2.1L2.1 12A5 5 0 0 1 7 8z"/><path d="M7.3 11v3M5.8 12.5h3"/><circle cx="15.4" cy="11.3" r="1" fill="currentColor" stroke="none"/><circle cx="17.6" cy="13.3" r="1" fill="currentColor" stroke="none"/>',
  externalLink: '<path d="M13 4h7v7"/><path d="M20 4l-8.5 8.5"/><path d="M18 14v5a1.8 1.8 0 0 1-1.8 1.8H5A1.8 1.8 0 0 1 3.2 19V7.8A1.8 1.8 0 0 1 5 6h5"/>',
};

const inner = computed(() => ICONS[props.name] || "");
</script>

<template>
  <svg class="icon" :width="size" :height="size" viewBox="0 0 24 24"
       fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round" v-html="inner"
       aria-hidden="true"></svg>
</template>

<style scoped>
.icon { display: inline-block; vertical-align: middle; flex: none; }
</style>
