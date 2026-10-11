<script setup>
// 三视图统一模板：左侧 3D 视口（+ HUD 插槽） + 右侧栏（安全调试面板 + 业务卡片）。
//
// 真实监控 / 模拟仿真 / 点位执行 / 程序执行 全部套这个壳 ——
// 同一个 scene、同一张 canvas，切换视图只是把 canvas 搬到对应的 .view 容器里，
// 模型与视角原地不动。
//
// 右栏交互（本次升级）：
//   · 拖中间的细条 → 调宽度（240~760px，记忆到 localStorage）
//   · 单击细条      → 折叠 / 展开（记忆）
//   · 双击细条      → 恢复默认宽度
//   · 宽度变化时用 ResizeObserver 通知 three.js 重算画布尺寸
//     （★ 不能只依赖 window.resize —— 拖右栏并不触发 window.resize，
//       画布会保持旧宽高被拉伸变形）
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { registerView, resizeView } from "../three/manager.js";
import { useRobotStore } from "../stores/robot.js";
import { useUiStore, SIDE_DEF, COMPACT_PX } from "../stores/ui.js";
import { useAuthStore } from "../stores/auth.js";
import SafetyPanel from "./SafetyPanel.vue";
import SafetyChip from "./SafetyChip.vue";
import AsrSubtitle from "./AsrSubtitle.vue";   // ★ 实时语音字幕层（贴 3D 视口底部）
import Icon from "./Icon.vue";

const props = defineProps({
  view: { type: String, required: true },   // live | sim | rec | point | program
});

const robot = useRobotStore();
const ui = useUiStore();
const auth = useAuthStore();
const viewEl = ref(null);
const dragging = ref(false);

// ★ 2026-10-11：**运行日志已并入底部状态区"五选一"（components/StatusStrip.vue 的
//   「运行日志」片）** —— 原来左下角那块浮动日志面板（含粘底/收起/清空）整体迁走，
//   本组件不再渲染日志，也不再维护 --bottom-zone-h（那是为浮动面板定位用的变量）。
//   日志写入统一走全局 exec.log（各页共享），显示由 StatusStrip 负责。

// 窄屏下右栏改为上下堆叠：此时拖拽/折叠都没有意义，直接当作展开。
// ★ 审计修复回归：这段曾在加底部日志面板时被误删，导致 compact/mq 未定义。
const compact = ref(false);
let mq = null;
function onMq() { compact.value = !!mq && mq.matches; }

const collapsed = computed(() => !compact.value && ui.sideCollapsed);

let ro = null;
let raf = 0;
let debTimer = 0;
// 拖拽真实 resize 的最小间隔(ms)：拖拽中 RO 每个指针移动帧都触发，
// 若每帧都 renderer.setSize，WebGL 缓冲被反复重建会清帧 → 3D 黑屏闪烁。
// 这里用时间节流：连续拖拽时约每 DRAG_RESIZE_MS 才真正 setSize 一次，
// 缓冲不再每帧抖动，黑闪随之消失；跟手只差 ~110ms，肉眼几乎不可察觉。
const DRAG_RESIZE_MS = 110;
let lastDragMs = 0;
function doResize() {
  raf = 0;
  debTimer = 0;
  // 只在自己是当前视图时重算（隐藏的视图 clientWidth 为 0，算了也是白算）
  if (robot.activeView === props.view) resizeView();
}
function scheduleResize() {
  // ★ 折叠/展开走 .side 的 CSS 宽度过渡（.side transition width .16s），
  //   期间 ResizeObserver 每个过渡帧都触发。若每帧都 setSize，WebGL 缓冲反复
  //   重建会清帧 → 3D 黑屏闪烁。这里用 debounce：宽度稳定后再一次性 resize。
  //   拖拽调宽已把过渡关掉（.dragging → .side{transition:none}），改走 RAF +
  //   时间节流：既保持跟手，又不在每个指针移动帧都重建缓冲，避免黑闪。
  if (dragging.value) {
    if (raf) return;
    raf = requestAnimationFrame(function dragTick() {
      raf = 0;
      const now = performance.now();
      if (dragging.value && now - lastDragMs < DRAG_RESIZE_MS) {
        raf = requestAnimationFrame(dragTick);   // 太密：顺延到满间隔再真 resize
        return;
      }
      lastDragMs = now;
      doResize();
    });
  } else {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    clearTimeout(debTimer);
    debTimer = setTimeout(doResize, 240);
  }
}

onMounted(() => {
  registerView(props.view, viewEl.value);
  mq = window.matchMedia(`(max-width: ${COMPACT_PX}px)`);
  onMq();
  // 老浏览器没有 addEventListener 版本的 matchMedia → 退回 addListener
  if (mq.addEventListener) mq.addEventListener("change", onMq);
  else if (mq.addListener) mq.addListener(onMq);

  if (typeof ResizeObserver !== "undefined") {
    ro = new ResizeObserver(scheduleResize);
    if (viewEl.value) ro.observe(viewEl.value);
  }
});

onBeforeUnmount(() => {
  if (raf) cancelAnimationFrame(raf);
  clearTimeout(debTimer);
  if (ro) { ro.disconnect(); ro = null; }
  if (mq) {
    if (mq.removeEventListener) mq.removeEventListener("change", onMq);
    else if (mq.removeListener) mq.removeListener(onMq);
  }
});

// ---------- 拖拽调宽 ----------
let drag = null;
function onGripDown(e) {
  if (compact.value) return;
  e.preventDefault();
  drag = { sx: e.clientX, ow: ui.sideWidth, moved: false };
  dragging.value = true;
  window.addEventListener("pointermove", onGripMove);
  window.addEventListener("pointerup", onGripUp);
  window.addEventListener("pointercancel", onGripUp);
}
function onGripMove(e) {
  if (!drag) return;
  const dx = drag.sx - e.clientX;          // 手柄在右栏左侧：往左拖 = 变宽
  if (Math.abs(dx) > 2) drag.moved = true;
  // 反着算：向右拖使右栏变窄
  ui.setSideWidth(drag.ow + dx);
}
function onGripUp() {
  window.removeEventListener("pointermove", onGripMove);
  window.removeEventListener("pointerup", onGripUp);
  window.removeEventListener("pointercancel", onGripUp);
  const moved = drag && drag.moved;
  drag = null;
  dragging.value = false;
  if (!moved) ui.toggleSide();             // 没拖动 = 单击 → 折叠/展开
  scheduleResize();
}
</script>

<template>
  <div class="monitor" :class="{ dragging, 'side-off': collapsed }"
       :style="{ '--side-w': ui.sideWidth + 'px' }">
    <div class="view" ref="viewEl">
      <slot name="hud" />
      <div class="hud-tip">鼠标左键旋转 · 滚轮缩放 · 右键平移</div>
      <!-- ★ 安全围栏状态片：浮在 3D 视口右下角（用户要求，不再占底部状态栏）。
           放在这里而不是 App.vue：.view 就是 3D 视口本身，位置自动跟随
           右栏拖宽/折叠与窄屏堆叠，不需要 JS 去算右栏宽度。 -->
      <SafetyChip />
      <!-- ★ 实时语音字幕：与 SafetyChip 同一套"视口浮层"做法（.view 本身就是定位上下文），
           自动跟随右栏拖宽/折叠与窄屏堆叠。识别文本只用于显示，不参与机器人控制。 -->
      <AsrSubtitle />
    </div>

    <!-- 右栏手柄：拖动调宽 / 单击折叠 / 双击复位 -->
    <div v-if="!compact" class="side-grip" :class="{ dragging }"
         role="separator" aria-orientation="vertical"
         :aria-label="(collapsed ? '展开' : '收起') + '右侧面板'"
         :title="'拖动调整宽度（' + SIDE_DEF + 'px 复位双击） · 单击' + (collapsed ? '展开' : '收起')"
         @pointerdown="onGripDown" @dblclick="ui.resetSide()">
      <span class="grip-bar"></span>
      <span class="grip-ico">
        <Icon :name="collapsed ? 'chevronLeft' : 'chevronRight'" :size="13" />
      </span>
    </div>

    <div class="side" :class="{ collapsed }">
      <!-- ★ 需求：安全围栏调试面板默认隐藏，只有**管理员**（持有未过期的 admin 令牌）才显示。
           其余角色/未授权一律看不到这块调试入口，避免误改围栏参数。 -->
      <SafetyPanel v-if="view === 'live' && auth.isAdmin" />
      <slot />
    </div>

    <!-- ★ 2026-10-11：运行日志已迁到底部状态区（StatusStrip 的「运行日志」片），
         本组件的左下角浮动日志面板已移除（原 --bottom-zone-h 定位变量一并去掉）。 -->
  </div>
</template>
