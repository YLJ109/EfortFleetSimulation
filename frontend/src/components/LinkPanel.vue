<script setup>
// =====================================================================
// 真机链路面板（可折叠浮窗）。
//
// ★ 底部「真机链路」按钮点开后浮出的**完整内容 = RcReadyCard**：
//   逐行寄存器状态（双闸/档位/伺服/报警/急停/程序/点动）+ 一键就绪/取消就绪/清除报错。
//   —— 不再展示三盏灯（那块内容在底栏 StatusStrip 常驻、展开可看）。
//
// ★ RcReadyCard 完全自足（无 props，只依赖 pinia store），可在任意容器内嵌：
//   · rcReady 数据经 WS 全局广播共享同一份 store，浮窗实例**不会**产生第二路轮询；
//   · 本组件只做外层定位 + 毛玻璃 + 收起按钮，卡片的样式/逻辑全部复用 RcReadyCard。
// =====================================================================
import RcReadyCard from "./RcReadyCard.vue";
import Icon from "./Icon.vue";

/** 面板显隐：由 owner（App.vue）里的切换按钮控制。 */
defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(["close"]);

function close() { emit("close"); }
</script>

<template>
  <transition name="lp-pop">
    <div v-if="open" class="link-panel">
      <!-- 右上收起按钮（浮在卡片之上，不占布局流） -->
      <button class="lp-close" aria-label="收起真机链路面板" title="收起" @click="close">
        <Icon name="close" :size="13" />
      </button>
      <!-- ★ 完整复用 RcReadyCard —— 一键就绪的逻辑不重复造 -->
      <RcReadyCard />
    </div>
  </transition>
</template>

<style scoped>
/* ---- 浮窗：毛玻璃 + 青色主题，浮在 3D 视口上（不占布局流）。
       内嵌 RcReadyCard（自带 .card 卡片背景），这里只做衬底与定位。 ---- */
.link-panel {
  position: fixed;
  left: 12px;
  bottom: 52px;                 /* 置于底栏上方，不遮三盏灯/控制区 */
  z-index: 122;
  width: min(400px, 92vw);
  border-radius: 12px;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.55);
}

/* ★ 右上收起按钮：绝对定位到卡片右上，避开卡片标题 */
.lp-close { position: absolute; top: 8px; right: 8px; z-index: 2;
  width: 24px; height: 24px; padding: 0;
  display: flex; align-items: center; justify-content: center;
  background: var(--panel2); border: 1px solid var(--line); border-radius: 6px;
  color: var(--muted); cursor: pointer; }
.lp-close:hover { color: var(--txt); border-color: var(--accent); }

/* ★ 2026-10-09 用户反馈："关闭按钮和黄色文字重了" ——
   右上关闭按钮会压在卡片标题右侧的"未就绪"黄字徽标（h3-sub）上。
   给卡片标题留出右侧安全区，徽标不再被按钮盖住。 */
.link-panel .card :deep(h3) { padding-right: 28px; }
.link-panel .card :deep(h3 .h3-sub) { margin-left: auto; }

/* ---- 折叠动画 ---- */
.lp-pop-enter-active, .lp-pop-leave-active { transition: opacity .18s ease, transform .18s ease; }
.lp-pop-enter-from, .lp-pop-leave-to { opacity: 0; transform: translateY(6px); }

/* ---- 窄屏适配 ---- */
@media (max-width: 760px) {
  .link-panel { left: 8px; right: 8px; width: auto; }
}
</style>