<script setup>
// =====================================================================
// 作用域控制台（单台）：六轴滑块 + 步长 + 速度 + 残影预判 + 执行。
//
// 为什么单独一个组件：这块是全站**唯一**会产生真机运动的地方，
// 它的"能不能按"比"按下去做什么"更重要，所以把所有拦阻条件集中在一个文件里，
// 一条一条列给用户看，而不是散在 v-if 里让人猜。
//
// 三道闸（顺序固定，缺一不可）：
//   ① 控制授权   没令牌 → 整个控制台不出现，只出现"申请控制"（只读模式）
//   ② 残影安全   目标姿态撞到围栏 → 执行禁用，残影自己变红
//   ③ 写通道开   后端三闸（EFORT_REAL_MOTION / motion.real_write / fleet.allow_write）
//                没开 → 执行禁用并说明是没开，不是坏了
//
// ★ 拖动滑块**只画残影，绝不下发**。下发必须点"执行"。
// =====================================================================
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { fleetGhost, fleetDrive } from "../three/manager.js";
import { apiControl } from "../net/control.js";
import { fmtNum } from "../utils/fleetCards.js";
import Icon from "./Icon.vue";

const props = defineProps({
  card: { type: Object, required: true },        // 选中的那台（fleetCards.makeCard 的产物）
  limits: { type: Array, default: () => [] },     // [{name,min,max}]，来自该型号的档案
  authActive: { type: Boolean, default: false },
  writeReady: { type: Boolean, default: false },
  writeNote: { type: String, default: "" },
});
const emit = defineEmits(["sim", "request-control"]);

/**
 * 两种控制模式，判据只有一个：这台**有没有真机姿态在流**。
 *   在线 → 滑块只是"目标"，画面上出残影，必须点"执行"才下发（绝不让拖动直接动真机）。
 *   离线 → 这台压根没连，滑块就是沙盘：推多少画面动多少，不需要执行，也发不出去。
 * ★ 为什么离线反而给直驱：现场调姿态、预判围栏、教新人认轴，本来就不需要真机在线；
 *   把它锁成"不能动"只会让人去开真机来试。但代价是**必须让画面自己说清楚**：
 *   卡片挂"仿真"标、控制台顶部写"仿真直驱"，退出时把本体放回登记摆位。
 */
const AXIS = ["J1", "J2", "J3", "J4", "J5", "J6"];
const STEPS = [1, 5, 10];
/** 真机安全口径：所有动真机的速度默认最慢档 5%。 */
const SPEED_MIN = 5;
const SPEED_MAX = 100;

/** 没有该轴限位档案时的兜底行程。★ 兜底值必须标出来，不能装作是官方数据。 */
const FALLBACK_LIMIT = { min: -180, max: 180 };

function limitOf(i) {
  const l = props.limits && props.limits[i];
  return l && Number.isFinite(Number(l.min)) && Number.isFinite(Number(l.max))
    ? { min: Number(l.min), max: Number(l.max), known: true }
    : { ...FALLBACK_LIMIT, known: false };
}
const LIMITS = computed(() => AXIS.map((_, i) => limitOf(i)));
/** 有任何一轴用的是兜底行程 → 抬头挂"限位未核"，别让它看起来像官方数。 */
const limitsUnverified = computed(() => LIMITS.value.some((l) => !l.known));

/** 目标姿态（度）。初值 = 这台**当前**真机姿态，不是 0 —— 从 0 开始拖等于先做一次大跳。 */
const q = ref([0, 0, 0, 0, 0, 0]);
const step = ref(5);
const speed = ref(SPEED_MIN);
const ghost = ref({ state: "safe", clearance: null });
const busy = ref(false);
const result = ref(null);        // { ok, text }

function baseQ() {
  return (props.card.q || []).map((v) => (Number.isFinite(Number(v)) ? Number(v) : 0));
}
function resetToReal() {
  q.value = baseQ();
  result.value = null;
}
// 换台 / 真机姿态刷新（未编辑时）→ 重新对齐基准。
// ★ 不能无条件跟随：用户正在拖滑块时真机帧也在进来，跟着动会把他的手"弹走"。
const editing = ref(false);
watch(() => props.card.id, () => { resetToReal(); editing.value = false; });
watch(() => props.card.q, () => { if (!editing.value) resetToReal(); }, { deep: true });

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** 能不能改目标姿态：沙盘（不动真机）永远可以；在线必须已授权。 */
const editable = computed(() => simMode.value || props.authActive);

function nudge(i, dir) {
  if (!editable.value) return;
  editing.value = true;
  const l = LIMITS.value[i];
  const next = clamp((Number(q.value[i]) || 0) + dir * step.value, l.min, l.max);
  const arr = q.value.slice();
  arr[i] = Math.round(next * 100) / 100;
  q.value = arr;
}
function onSlider(i, v) {
  if (!editable.value) return;
  editing.value = true;
  const l = LIMITS.value[i];
  const arr = q.value.slice();
  arr[i] = clamp(Number(v) || 0, l.min, l.max);
  q.value = arr;
}
function onTyped(i, v) {
  if (!editable.value) return;
  editing.value = true;
  const l = LIMITS.value[i];
  const n = Number(v);
  const arr = q.value.slice();
  arr[i] = Number.isFinite(n) ? clamp(n, l.min, l.max) : 0;
  q.value = arr;
}

const dirty = computed(() => {
  const b = baseQ();
  return q.value.some((v, i) => Math.abs(v - b[i]) > 1e-6);
});
const maxDelta = computed(() => {
  const b = baseQ();
  return Math.max(0, ...q.value.map((v, i) => Math.abs(v - b[i])));
});

// ---- 目标姿态一变：在线走残影预判，离线直接驱动本体 ----
watch([() => props.card.id, q], () => {
  if (!props.card.id) return;
  if (!props.card.online) {
    fleetDrive(props.card.id, q.value);
    ghost.value = { state: "safe", clearance: null };
    return;
  }
  const r = fleetGhost(props.card.id, q.value);
  ghost.value = r || { state: "safe", clearance: null };
}, { deep: true, immediate: true });

// 仿真直驱要登记到 store：卡片要据此显示"仿真"，退出时也要靠它把本体放回登记摆位。
watch([() => props.card.id, () => props.card.online], ([id, online]) => {
  emit("sim", id, !!id && !online);
}, { immediate: true });

const GHOST_LABEL = { safe: "残影安全", warn: "接近围栏", danger: "即将碰撞", hit: "已碰撞" };
const ghostOk = computed(() => ghost.value.state === "safe");

/** 离线 = 沙盘直驱；在线 = 目标 + 残影 + 执行。 */
const simMode = computed(() => !props.card.online);

/** 执行禁用原因：逐条列出来，不要只给一个灰按钮。 */
const blockers = computed(() => {
  if (simMode.value) return [];          // 沙盘不下发，谈不上拦阻
  const out = [];
  if (props.card.frozen) out.push("这台数据已冻结，基准姿态可能不是现在的");
  if (!dirty.value) out.push("目标姿态与当前姿态相同（没有要动的轴）");
  if (!ghostOk.value) out.push(GHOST_LABEL[ghost.value.state] || "残影位置不安全");
  if (!props.writeReady) out.push(props.writeNote || "后端写通道未开启");
  return out;
});
const canRun = computed(() => !simMode.value && blockers.value.length === 0 && !busy.value);

async function run() {
  if (!canRun.value) return;
  busy.value = true;
  result.value = null;
  try {
    // ★ 必须走 apiControl 而不是裸 fetch：控制令牌由它统一附带，
    //   且后端一返回 401（重启/改口令 → 令牌已死）就立刻收回本地权限。
    //   裸 fetch 会让"明明显示控制中、发出去全是 401"这种状态长期存在。
    const r = await apiControl(`/fleet/${encodeURIComponent(props.card.id)}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ joints: q.value, speed_pct: speed.value, source: "fleet_console" }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    result.value = { ok: true, text: d.message || "已下发" };
    editing.value = false;
  } catch (e) {
    result.value = { ok: false, text: String((e && e.message) || e) };
  } finally {
    busy.value = false;
  }
}

// 卸载 / 换台：收掉残影，并把沙盘驱动过的本体放回登记摆位（全 0）。
// ★ 不复位的话，"点了别的台，上一台还摆着刚才拖出来的姿势"，而卡片写着离线 ——
//   那画面看着就像真机自己动了。
onBeforeUnmount(() => {
  const id = props.card.id;
  fleetGhost(id, null);
  if (simMode.value) { fleetDrive(id, [0, 0, 0, 0, 0, 0]); emit("sim", id, false); }
});
</script>

<template>
  <section class="fs">
    <header class="fs-h">
      <div class="fs-t">
        <b>{{ card.short }}</b>
        <span>AXIS CONSOLE · {{ card.modelLabel }}</span>
      </div>
      <span class="fs-mode" :class="simMode ? 'sim' : (authActive ? 'on' : 'ro')">
        <Icon :name="simMode ? 'layers' : (authActive ? 'unlock' : 'lock')" :size="12" />
        {{ simMode ? "沙盘模式" : (authActive ? "在线控制" : "在线 · 只读") }}
      </span>
    </header>

    <div class="fs-axes" :class="{ locked: !editable }">
        <div v-for="(a, i) in AXIS" :key="a" class="ax">
          <label :title="('J' + (i + 1) + ' 行程 ' + LIMITS[i].min + '° ~ ' + LIMITS[i].max + '°')
                         + (LIMITS[i].known ? '' : '（档案缺该轴限位，用的是 ±180° 兜底）')">
            {{ a }}<i v-if="!LIMITS[i].known">*</i>
          </label>
          <button class="pm" @click="nudge(i, -1)" :title="`-${step}°`">−</button>
          <input class="rng" type="range" :min="LIMITS[i].min" :max="LIMITS[i].max"
                 :step="0.1" :value="q[i]"
                 @input="onSlider(i, $event.target.value)" @pointerdown="editing = true">
          <button class="pm" @click="nudge(i, 1)" :title="`+${step}°`">+</button>
          <input class="num" type="number" :min="LIMITS[i].min" :max="LIMITS[i].max"
                 :step="step" :value="q[i]" @change="onTyped(i, $event.target.value)">
        </div>
      </div>

      <!-- 步长 / 速度 / 基准 -->
      <div class="fs-ctl">
        <span class="g">
          <i>步长</i>
          <button v-for="s in STEPS" :key="s" class="seg" :class="{ on: step === s }"
                  @click="step = s">{{ s }}°</button>
        </span>
        <span class="g sp">
          <i>速度</i>
          <input class="rng" type="range" :min="SPEED_MIN" :max="SPEED_MAX" :step="1"
                 v-model.number="speed" @pointerdown="editing = true">
          <b>{{ speed }}<u>%</u></b>
          <em v-if="speed > SPEED_MIN" class="warn">已高于最慢档</em>
        </span>
        <button class="fs-btn mini" @click="resetToReal" title="放弃当前目标，回到真机现在的姿态">
          <Icon name="rotateLeft" :size="12" /> 回到实际位置
        </button>
      </div>

      <!-- 在线：残影状态 + 执行。离线（沙盘）：没有可下发的目标，只说明现在在干什么。 -->
      <div v-if="simMode" class="fs-run">
        <span class="gh sim"><i></i>仿真直驱 · 滑块即动画面</span>
        <span class="dz">这台未连真机，不会下发任何动作</span>
        <button class="fs-btn mini" @click="resetToReal">
          <Icon name="rotateLeft" :size="12" /> 回到登记摆位
        </button>
      </div>
      <div v-else-if="!authActive" class="fs-run">
        <span class="gh ro"><i></i>未授权 · 只能看</span>
        <span class="dz">设目标与执行需要控制权限</span>
        <button class="fs-btn exec primary" @click="emit('request-control')">
          <Icon name="lock" :size="13" /> 申请控制
        </button>
      </div>
      <div v-else class="fs-run">
        <span class="gh" :class="ghost.state">
          <i></i>{{ GHOST_LABEL[ghost.state] || ghost.state }}
          <em v-if="ghost.clearance !== null && Number.isFinite(ghost.clearance)">
            余量 {{ (ghost.clearance * 1000).toFixed(0) }} mm
          </em>
        </span>
        <span class="dz">最大变化 {{ fmtNum(maxDelta, 1) }}°</span>
        <button class="fs-btn exec primary" :disabled="!canRun" @click="run"
                :title="canRun ? '按当前目标姿态下发' : blockers[0]">
          <Icon v-if="busy" name="refresh" :size="13" class="spin" />
          <Icon v-else name="play" :size="13" /> 执行
        </button>
      </div>

      <!-- 拦阻原因：逐条列，别只给一个灰按钮 -->
      <ul v-if="blockers.length" class="fs-blk">
        <li v-for="b in blockers" :key="b"><Icon name="alert" :size="11" /> {{ b }}</li>
      </ul>
      <p v-if="result" class="fs-res" :class="result.ok ? 'ok' : 'err'">
        <Icon :name="result.ok ? 'check' : 'alert'" :size="12" /> {{ result.text }}
      </p>
      <p v-if="limitsUnverified" class="fs-note">
        带 <b>*</b> 的轴没有官方限位档案，用的是 ±180° 兜底行程 —— 下发前请先在真机上核对。
      </p>
  </section>
</template>

<style scoped>
.fs { flex: none; position: relative; padding: 8px 10px 10px;
  background: var(--glass-solid); border: 1px solid var(--accent-line);
  backdrop-filter: blur(12px) saturate(140%);
  -webkit-backdrop-filter: blur(12px) saturate(140%);
  box-shadow: 0 0 14px var(--glow-soft);
  clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 12px, 100% 100%, 12px 100%, 0 calc(100% - 12px)); }

.fs-h { display: flex; align-items: baseline; gap: 8px;
  padding-bottom: 6px; margin-bottom: 8px; border-bottom: 1px solid var(--glass-line); }
.fs-t { display: flex; flex-direction: column; min-width: 0; }
.fs-t b { font-family: var(--num); font-size: 16px; font-weight: 700; letter-spacing: 1.4px;
  color: var(--accent); }
.fs-t span { font-size: 7px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fs-mode { margin-left: auto; flex: none; display: inline-flex; align-items: center; gap: 4px;
  font-size: 10px; padding: 2px 7px; border: 1px solid; }
.fs-mode.on { color: var(--ok); border-color: var(--ok-line); background: var(--ok-soft); }
.fs-mode.sim { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.fs-mode.ro { color: var(--muted); border-color: var(--line); background: var(--cap); }
.gh.ro { color: var(--muted); border-color: var(--line); background: var(--cap); }
.fs-axes.locked .pm, .fs-axes.locked .rng, .fs-axes.locked .num { opacity: .45; pointer-events: none; }
.gh.sim { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.fs-run .mini { margin-left: auto; flex: none; }
.fs-run .exec { margin-left: auto; flex: none; }

/* 六轴行：名 / − / 滑块 / + / 数值 / 行程 */
.fs-axes { display: flex; flex-direction: column; gap: 3px; }
.ax { display: grid; grid-template-columns: 20px 20px minmax(0, 1fr) 20px 52px;
  align-items: center; gap: 5px; padding: 2px 3px; }
/* ★ 滑块必须是那一行里唯一会伸长的东西。原来行程文字 + 数值框一共吃掉 130px，
   滑块被挤成一小截 —— 想微调 0.5° 时手指根本落不准，那比没有滑块更糟。 */
.ax:hover { background: var(--accent-soft3); }
.ax label { font-size: 10px; color: var(--muted); letter-spacing: .4px; }
.ax label i { font-style: normal; color: var(--warn); }
.pm { width: 20px; padding: 1px 0; line-height: 1.1; font-size: 13px; border-radius: 0;
  background: var(--cap); border: 1px solid var(--line); color: var(--txt2); }
.pm:hover { color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft); }
.rng { width: 100%; min-width: 0; height: 18px; accent-color: var(--accent); }
.num { background: var(--panel); color: var(--accent); border: 1px solid var(--line);
  padding: 2px 3px; font-size: 11px; font-family: var(--num); text-align: right;
  width: 100%; min-width: 0; font-variant-numeric: tabular-nums; border-radius: 0; }
.num::-webkit-outer-spin-button, .num::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }

/* 步长 / 速度 */
.fs-ctl { display: flex; flex-direction: column; gap: 6px; margin-top: 8px;
  padding-top: 7px; border-top: 1px solid var(--glass-line); font-size: 10px; }
.fs-ctl .g { display: flex; align-items: center; gap: 4px; }
.fs-ctl .g > i { font-style: normal; color: var(--muted); letter-spacing: .8px; flex: none; width: 30px; }
.seg { padding: 2px 8px; font-size: 10px; border-radius: 0; background: var(--cap);
  border: 1px solid var(--line); color: var(--txt2); }
.seg.on { color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft2); }
.fs-ctl .sp b { font-family: var(--num); font-size: 13px; color: var(--accent); flex: none; }
.fs-ctl .sp b u { text-decoration: none; font-size: 9px; color: var(--muted); }
.fs-ctl .sp .warn { font-style: normal; color: var(--warn); flex: none; font-size: 9px; }
.mini { width: auto; padding: 3px 8px; font-size: 10px; }

/* 残影 + 执行 */
.fs-run { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px;
  margin-top: 8px; padding-top: 7px; border-top: 1px solid var(--glass-line); }
/* 状态徽标与说明各占一行时也要左对齐，否则换行后中间裂开一条缝 */
.fs-run .gh { white-space: nowrap; }
.fs-run .dz { white-space: normal; line-height: 1.5; }
.gh { display: inline-flex; align-items: center; gap: 5px; font-size: 10px;
  padding: 3px 8px; border: 1px solid; }
.gh i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.gh em { font-style: normal; font-family: var(--num); font-size: 9px; opacity: .85; }
.gh.safe { color: var(--ok); border-color: var(--ok-line); background: var(--ok-soft); }
.gh.warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.gh.danger, .gh.hit { color: var(--err); border-color: var(--err-line); background: var(--err-soft); }
.gh.hit { animation: fs-blink 1s steps(2, end) infinite; }
@keyframes fs-blink { 50% { opacity: .35; } }
.dz { font-family: var(--num); font-size: 10px; color: var(--muted); }
.fs-btn { display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  border-radius: 0; font-size: 11px; letter-spacing: .6px; padding: 5px 12px;
  background: var(--cap); border: 1px solid var(--line); color: var(--txt2);
  clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px); }
.fs-btn:hover:not(:disabled) { color: var(--accent); border-color: var(--accent-line);
  background: var(--accent-soft); }
.fs-btn.primary { color: var(--accent); background: var(--accent-soft2);
  border-color: var(--accent-line); }

.fs-btn:disabled { opacity: .45; cursor: not-allowed; }
.fs-btn.spin :deep(svg) { animation: fs-spin .8s linear infinite; }
@keyframes fs-spin { to { transform: rotate(360deg); } }

.fs-blk { list-style: none; margin: 7px 0 0; padding: 0; display: flex;
  flex-direction: column; gap: 3px; }
.fs-blk li { display: flex; align-items: center; gap: 5px; font-size: 10px; color: var(--warn); }
.fs-res { margin: 7px 0 0; font-size: 10px; display: flex; align-items: center; gap: 5px; }
.fs-res.ok { color: var(--ok); } .fs-res.err { color: var(--err); }
.fs-note { margin: 7px 0 0; font-size: 9px; line-height: 1.6; color: var(--muted); }
.fs-note b { color: var(--warn); }
</style>
