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
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from "vue";
import { registerView, resizeView } from "../three/manager.js";
import { useRobotStore } from "../stores/robot.js";
import { useUiStore, SIDE_DEF, COMPACT_PX } from "../stores/ui.js";
import { useExecStore } from "../stores/exec.js";
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
const exec = useExecStore();
const auth = useAuthStore();
const viewEl = ref(null);
const dragging = ref(false);

// 底部日志面板：程序执行 / 真实监控 / 点位执行 / 分拣 且有日志时显示（统一到左下角浮动）
// ★ 需求：运行日志从右侧栏移到左下角 —— 这些"会产日志"的视图共用同一块浮动面板，
//   右侧栏不再出现"运行日志"卡片（见 ProgramExecView）。
// ★ 2026-10-08：分拣（sort）也纳入 —— 分拣页此前完全没有运行日志入口。
const showBottomLog = computed(() =>
  (props.view === 'program' || props.view === 'live'
   || props.view === 'point' || props.view === 'sort'
   || props.view === 'ai') && exec.log.length > 0
);
const bottomLogCollapsed = ref(false);

// ---------- 运行日志：自动跟随最新（"粘底"） ----------
// ★ 需求：每次来了新日志都滚到最新一行；**但用户手动往上翻看历史时不要抢他的滚动条**。
//   判据：滚动条是否已离开底部超过一个阈值（24px）→ 离开过就认为"用户在用滚动条"，停止跟随。
//   ── 与 ProgramExecView 早先那套 stickBottom 同一思路（那份卡片已并入本浮动面板）。
const blpBody = ref(null);
const blpStick = ref(true);          // true = 跟随最新（贴底）
const BLP_BOTTOM_EPS = 24;

function onBlpScroll() {
  const el = blpBody.value;
  if (!el) return;
  blpStick.value = (el.scrollHeight - el.scrollTop - el.clientHeight) <= BLP_BOTTOM_EPS;
}

/** 立即贴底（展开面板 / 重新开始跟随时用）。 */
async function blpScrollToBottom() {
  await nextTick();
  const el = blpBody.value;
  if (el) el.scrollTop = el.scrollHeight;
}

watch(() => exec.log.length, () => {
  if (!blpStick.value) return;       // 用户正在往上翻 → 不抢滚动条
  blpScrollToBottom();
});
// 展开面板 / 首次出现日志 → 恢复跟随并贴底
watch([bottomLogCollapsed, showBottomLog], ([collapsed, show]) => {
  if (!collapsed && show) { blpStick.value = true; blpScrollToBottom(); }
});

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

  // ★ 需求：左下角运行日志面板不得碰撞底部 #bottom-zone。
  //   实测底栏高度写进 CSS 变量 --bottom-zone-h，面板 bottom 取"底栏高 + 间隙"。
  syncBottomH();
  window.addEventListener("resize", syncBottomH);
  if (typeof ResizeObserver !== "undefined") {
    ro = new ResizeObserver(scheduleResize);
    if (viewEl.value) ro.observe(viewEl.value);
  }
});

/** 把 #bottom-zone 的实测高度 + 间隙写进 :root，供浮动日志面板定位使用。 */
function syncBottomH() {
  const bz = document.getElementById("bottom-zone");
  const h = bz ? Math.ceil(bz.getBoundingClientRect().height) : 44;
  document.documentElement.style.setProperty("--bottom-zone-h", (h + 12) + "px");
}

onBeforeUnmount(() => {
  if (raf) cancelAnimationFrame(raf);
  clearTimeout(debTimer);
  window.removeEventListener("resize", syncBottomH);
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
  <div class="monitor" :class="{ dragging, 'side-off': collapsed, 'has-bottom-log': showBottomLog && !bottomLogCollapsed }"
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

    <!-- 底部浮动日志面板：仅在程序执行/真实监控且有日志时显示
         左下角浮动，不遮挡底部状态栏，可折叠 -->
    <div v-if="showBottomLog && !bottomLogCollapsed" class="bottom-log-panel">
      <div class="blp-head" @click="bottomLogCollapsed = true">
        <span class="blp-title"><Icon name="terminal" :size="13" /> 运行日志 ({{ exec.log.length }})</span>
        <span class="blp-actions">
          <button class="blp-btn" @click.stop="exec.clearLog()" title="清空"><Icon name="trash" :size="12" /></button>
          <button class="blp-btn" @click.stop="bottomLogCollapsed = true" title="收起"><Icon name="chevronDown" :size="12" /></button>
        </span>
      </div>
      <div class="blp-body" ref="blpBody" @scroll="onBlpScroll">
        <div v-for="(l, i) in exec.log" :key="i" class="blp-line" :class="l.level">
          <span class="blp-time">{{ l.t }}</span>
          <span class="blp-text">{{ l.text }}</span>
        </div>
      </div>
    </div>

    <!-- 收起时的小触发条 -->
    <div v-if="showBottomLog && bottomLogCollapsed" class="bottom-log-tab" @click="bottomLogCollapsed = false">
      <Icon name="terminal" :size="13" /> 运行日志 ({{ exec.log.length }}) <Icon name="chevronUp" :size="11" />
    </div>
  </div>
</template>

<style scoped>
/* 底部浮动日志面板 — Sentinel 风格：顶部青色发光线 + 毛玻璃 */
.bottom-log-panel {
  position: fixed;
  left: 12px;
  bottom: var(--bottom-zone-h, 56px);   /* ★ 抬高到 #bottom-zone（底栏）之上一段空隙，避免碰撞 */
  width: 420px;
  max-width: calc(100vw - 24px);
  max-height: min(320px, calc(100vh - 200px));
  background: linear-gradient(180deg, rgba(12,19,32,0.96), rgba(8,14,25,0.92));
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-hud);
  backdrop-filter: blur(14px) saturate(140%);
  -webkit-backdrop-filter: blur(14px) saturate(140%);
  z-index: 25;           /* 低于 CameraPanel(30)、关节HUD(10)、状态片(14) */
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: blp-slide-up .2s cubic-bezier(.2,.9,.3,1);
  isolation: isolate;
}
/* 顶部青色发光线 */
.bottom-log-panel::before {
  content: '';
  position: absolute;
  top: 0; left: 8%; right: 8%;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent-line), transparent);
  pointer-events: none;
  z-index: 1;
}
@keyframes blp-slide-up {
  from { opacity: 0; transform: translateY(20px); }
  to { opacity: 1; transform: translateY(0); }
}
.has-bottom-log .view { padding-bottom: 0; } /* 3D 视口无需预留空间，面板浮动 */

.blp-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 10px;
  font-size: 12px; font-weight: 600; color: var(--txt);
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, var(--veil), transparent);
  cursor: pointer; user-select: none;
}
.blp-title { display: flex; align-items: center; gap: 6px; }
.blp-actions { display: flex; gap: 4px; }
.blp-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; padding: 0;
  border: 1px solid var(--line); border-radius: 5px;
  background: var(--panel); color: var(--muted); cursor: pointer;
}
.blp-btn:hover { border-color: var(--accent); color: var(--accent); }
.blp-body {
  flex: 1; overflow-y: auto; padding: 6px 10px;
  font-size: 11px; line-height: 1.6; font-family: Consolas, monospace;
}
.blp-line { display: flex; gap: 8px; padding: 2px 0; white-space: nowrap; overflow: hidden; }
.blp-time { flex: none; color: var(--muted); font-variant-numeric: tabular-nums; }
.blp-text { color: var(--txt); min-width: 0; text-overflow: ellipsis; overflow: hidden; }
.blp-line.ok .blp-text { color: var(--ok); }
.blp-line.warn .blp-text { color: var(--warn); }
.blp-line.err .blp-text { color: var(--err); }
.blp-line.info .blp-text { color: var(--accent); }

/* ★ 需求：左下角运行日志固定**最多 7 行**（11px × 1.6 行高 + 上下 padding ≈ 21.6px/行），
   第 8 行起出现滚动条。**所有视图统一**。
   ★★ 关键坑：`.blp-body` 基础样式里有 `flex: 1`（= `flex-basis: 0`），而弹性项目在主轴上的
      尺寸由 `flex-basis` 决定、会**压过 `height`** —— 只写 height 是无效的，面板会随日志条数
      无限变高（这就是"限制 7 行没生效"的真因）。必须同时把 flex 主轴尺寸钉死：
      `flex: 0 0 auto; height: 152px`。 */
.bottom-log-panel .blp-body { flex: 0 0 auto; height: 152px; }
.bottom-log-panel { max-height: none; }        /* 高度贴合 7 行，不留空档 */

/* 收起时的小触发条 */
.bottom-log-tab {
  position: fixed;
  left: 12px;
  bottom: var(--bottom-zone-h, 56px);   /* ★ 收起态触发条同样避开底栏 */
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 10px;
  font-size: 11px; font-weight: 600; color: var(--accent);
  background: var(--glass-solid);
  border: 1px solid var(--accent-line);
  border-radius: 20px;
  box-shadow: var(--shadow-hud);
  backdrop-filter: blur(12px) saturate(140%);
  -webkit-backdrop-filter: blur(12px) saturate(140%);
  z-index: 25;
  cursor: pointer; user-select: none;
  animation: blp-tab-pop .15s ease;
}
@keyframes blp-tab-pop {
  from { opacity: 0; transform: scale(.9) translateY(8px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}
.bottom-log-tab:hover { background: var(--accent-soft); }
</style>
