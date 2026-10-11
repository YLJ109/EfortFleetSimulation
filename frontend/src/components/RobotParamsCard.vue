<script setup>
// =====================================================================
// 机器人实时参数（只读）—— 真实监控 / 点位执行 / 程序执行 三页共用。
//
// 为什么要共用一个组件：
//   以前只有「真实监控」页有关节角与 TCP 读数，操作者一旦切到「点位执行」去
//   示教/点动，就**看不到机器人此刻在哪**了 —— 而恰恰是这一页最需要知道位置
//   （拖动滑块前该先看当前姿态、点动后该立刻确认动了没有）。
//   但**不能**把这份读数在三个页面各写一遍：三份实现必然漂移（取色、量程归一、
//   悬停高亮这些判据只要有一处不同，现场就会怀疑"到底哪个是真的"）。
//   所以判据只有这一处，三个页面引用同一个组件。
//
// ★ 只读：本卡片不提供任何修改入口（姿态由 3D/示教/点动去改，这里只显示）。
//   唯一的可选动作是「重连机器人」，只有真实监控页开着（exec 两页不显示）。
//
// 数据来源：robot store 的 latestPose / latestTcp —— 由 WS 全局推送，
//   **与当前在前台的是哪个视图无关**，所以切到执行页同样在实时刷新。
// =====================================================================
import { computed, ref, onBeforeUnmount } from "vue";
import Icon from "./Icon.vue";
import { useRobotStore, DEFAULT_LIMITS } from "../stores/robot.js";
import { sanitizeLimits, barWidthStyle } from "../utils/jointScale.js";
import { highlightJoint } from "../three/manager.js";
import { apiUrl } from "../config.js";

const props = defineProps({
  /** 显式传入的位姿（真实监控页传自己的本地副本）；不传则取 store 的实时姿态。 */
  pose: { type: Array, default: null },
  /** 是否渲染「连接状态」卡（含机型 / 数据来源 / 校准提示）。 */
  connection: { type: Boolean, default: true },
  /** 是否渲染「关节角」卡。 */
  joints: { type: Boolean, default: true },
  /** 是否渲染「末端 TCP」卡。 */
  tcp: { type: Boolean, default: true },
  /** 是否显示「重连机器人」按钮（只有真实监控页需要；执行页不需要写操作入口）。 */
  reconnect: { type: Boolean, default: false },
});
const emit = defineEmits(["reconnected"]);

const robot = useRobotStore();

const hovered = ref(-1);
const reconnecting = ref(false);
const reconnectMsg = ref("");

/** 关节角读数：优先用外部传入的位姿，否则用 store 的**读数姿态**。
 *  ★ 读数姿态（readoutQ）≡ 3D 显示姿态（displayQ）—— 保证"数字和画面对得上"。
 *    阶段 7 之前这里读的是 latestPose，而 latestPose 会被 exec 页的 localDemo 冻结，
 *    所以「点位执行」页的读数会停在最后一次下发那一刻。 */
const q = computed(() => props.pose || robot.readoutQ || [0, 0, 0, 0, 0, 0]);
// ★ 变量名必须避开 props.tcp —— 同名的 setup 绑定会**遮蔽**同名 prop，
//   模板里的 `v-if="tcp"` 会绑到这份读数对象（恒为真），`:tcp="false"` 直接失效。
const tcpReadout = computed(() => robot.readoutTcp || { x: 0, y: 0, z: 0 });

/** 读数来源标签：与 robot.poseSource 一一对应，现场一眼看出"这个数字是谁给的"。
 *  ★ 判据只在 store（poseSource），这里只做 文案 映射，不得再写一遍优先级逻辑。 */
const SOURCE_TEXT = {
  override: "沙盘覆盖",
  demo: "未连接",          // 真实监控页在离线时把姿态钉成全 0（无真机位姿可读）
  command: "指令目标",
  telemetry: "遥测",
};
const sourceLabel = computed(() => {
  const src = robot.poseSource;
  if (src === "telemetry") {
    return robot.simulated ? "仿真遥测" : "真机遥测";
  }
  return SOURCE_TEXT[src] || "遥测";
});

// 量程兜底：meta 没回来（或不是 6 轴）时用出厂限位，保证条宽算得出、不跳。
// ★ 判据在 utils/jointScale.js（纯函数，边界已被 headless 钉死）——
//   这里只负责"选哪份限位"，不在这里再写一遍钳位数学。
const LIMITS = computed(() => sanitizeLimits(robot.limits) || DEFAULT_LIMITS);

function barStyle(i) { return barWidthStyle(q.value[i], LIMITS.value[i]); }

// 悬停读数行 → 同步高亮 3D 里对应的关节段（"看哪是哪个轴"）
function hoverJoint(i) { hovered.value = i; highlightJoint(i); }
function unhoverJoint() { hovered.value = -1; highlightJoint(-1); }

async function doReconnect() {
  if (reconnecting.value) return;
  reconnecting.value = true;
  reconnectMsg.value = "重连中…";
  try {
    const r = await fetch(apiUrl("/reconnect"), { method: "POST" });
    const d = await r.json();
    robot.connected = d.connected;
    robot.simulated = d.simulated;
    reconnectMsg.value = d.connected && !d.simulated ? "已连上机器人(真实)" : "机器人不可达(保持模拟)";
  } catch (e) {
    reconnectMsg.value = "重连失败: " + e.message;
  } finally {
    reconnecting.value = false;
    emit("reconnected");
  }
}

// 组件卸载时务必取消高亮：否则切页面后某个关节段会一直亮着
onBeforeUnmount(() => highlightJoint(-1));
</script>

<template>
  <div v-if="connection" class="card">
    <h3>连接状态 <span class="ro-tag">只读</span></h3>
    <div class="status hoverable">
      <span class="dot" :class="robot.connected ? (robot.simulated ? 'warn' : 'ok') : 'err'"></span>
      <span>{{ robot.connected ? (robot.simulated ? '已连接(模拟)' : '已连接(真实)') : '连接断开, 重连中…' }}</span>
    </div>
    <div class="row hoverable"><span class="k">机型</span><span class="v">{{ robot.robotName }}</span></div>
    <div class="row hoverable">
      <span class="k">数据来源</span>
      <span class="v">{{ robot.simulated ? '模拟(离线)' : '真实 Modbus' }}</span>
    </div>
    <div v-if="reconnect" class="btns" style="margin-top:8px">
      <button :disabled="reconnecting" @click="doReconnect">
        <Icon name="refresh" :size="15" /> 重连机器人
      </button>
    </div>
    <div v-if="reconnectMsg" class="small" style="margin-top:6px">{{ reconnectMsg }}</div>
    <div v-if="robot.calibrationPending" class="calib">
      <Icon name="alert" :size="14" /> DH 尚未校准，形态为近似值
    </div>
  </div>

  <div v-if="joints" class="card">
    <h3>关节角 (deg) <span class="ro-tag">只读</span>
      <span class="h3-sub">{{ sourceLabel }}</span>
    </h3>
    <div class="rp-axis" v-for="(n, i) in robot.axes" :key="n"
         :class="{ hot: hovered === i }"
         @mouseenter="hoverJoint(i)" @mouseleave="unhoverJoint">
      <label>{{ n }}</label>
      <div class="rp-bar"><span class="rp-fill" :style="barStyle(i)"></span></div>
      <span class="rp-deg">{{ (Number(q[i]) || 0).toFixed(1) }}</span>
    </div>
    <div class="small" style="margin-top:8px">
      条宽按各轴行程归一化。鼠标移到某一行会同时高亮 3D 里对应的关节段。
    </div>
  </div>

  <div v-if="tcp" class="card">
    <h3>末端 TCP (mm) <span class="ro-tag">只读</span></h3>
    <div class="row hoverable"><span class="k">X</span><span class="v">{{ tcpReadout.x.toFixed(1) }}</span></div>
    <div class="row hoverable"><span class="k">Y</span><span class="v">{{ tcpReadout.y.toFixed(1) }}</span></div>
    <div class="row hoverable"><span class="k">Z</span><span class="v">{{ tcpReadout.z.toFixed(1) }}</span></div>
  </div>
</template>

<style scoped>
.ro-tag { font-size: 10px; font-weight: 400; color: var(--muted); border: 1px solid var(--line);
  border-radius: 4px; padding: 0 5px; margin-left: 6px; }
.hoverable { transition: background .12s; border-radius: 4px; }
.hoverable:hover { background: var(--accent-soft); }

/* ★ 用 rp- 前缀而不是复用全局 .axis：全局 .axis 为 `26px 1fr 52px`（见 index.html:279），
   而本卡读数行用 34px/1fr/64px 的列宽更舒展；读数行属于只读展示，不该跟示教滑块抢同一套列宽。 */
.rp-axis { display: grid; grid-template-columns: 34px 1fr 64px; align-items: center;
  gap: 8px; margin: 6px 0; padding: 2px 4px; border-radius: 4px; transition: background .12s; }
.rp-axis label { font-size: 13px; color: var(--muted); }
.rp-axis.hot { background: var(--accent-soft2); }
.rp-bar { position: relative; flex: 1; height: 8px; border-radius: 4px; overflow: hidden;
  background: var(--glass-line); }
.rp-fill { display: block; height: 100%; border-radius: 4px;
  background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width .15s linear; }
.rp-axis.hot .rp-fill { background: linear-gradient(90deg, #ffd166, #ff9f45); }
.rp-deg { text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; color: var(--txt); }
</style>
