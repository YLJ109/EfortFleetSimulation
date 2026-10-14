<template>
  <div class="ops-sec metrics-sec">
    <div class="ops-head">
      <Icon name="activity" :size="16" />
      <span class="ops-title">运行指标</span>
      <span class="ops-chip">{{ statusLine }}</span>
      <span class="ops-spacer"></span>
      <label class="switch" :title="SAMPLER_HINT">
        <input type="checkbox" :checked="sampling" @change="toggleSampling" />
        采集浏览器渲染指标
      </label>
      <button class="ops-btn" @click="metrics.refresh()">刷新</button>
    </div>

    <p class="small muted tip">
      每条都是<b>当前值 + 60s 最大 + 采样时刻</b>三件一起给（没有时刻的数字不算数）。
      「—（没测）」与「0」是两回事：后端没采到样本就写没测，前端不合成 0。
      本表<b>只读</b>，不产生任何下发，也不改速度（速度只认右上角滑块）。
    </p>

    <!-- 浏览器侧：默认关，勾选才采（开销如实写在下面那行） -->
    <div v-if="sampling" class="tbl">
      <div class="tr th"><span class="c1">浏览器侧</span><span class="c2">当前值</span>
        <span class="c3">60s 最大</span><span class="c4">采样时刻</span></div>
      <div v-for="r in perfRows" :key="r.key" class="tr">
        <span class="c1" :title="r.note">{{ r.label }}</span>
        <span class="c2">{{ r.cur }}</span>
        <span class="c3">{{ r.max }}</span>
        <span class="c4">{{ r.at }}</span>
      </div>
      <div class="tr cost">
        <span class="c1">采样器自身</span>
        <span class="c2 c2wide">窗口 {{ perf.windowSec ?? "—" }}s ·
          已记录 {{ perf.frames ?? 0 }} 帧 · 样本 {{ perf.samples ?? 0 }}
          · renderer.info 每 30 帧读一次</span>
        <span class="c3"></span><span class="c4"></span>
      </div>
    </div>
    <p v-else class="small muted">浏览器侧未采集（勾选上面才采 —— 默认关，没人看时不吃性能）。</p>

    <!-- 后端侧：进程级指标（stores/metrics.js 5s 轮询，quad() 是唯一读数据入口） -->
    <div class="tbl">
      <div class="tr th"><span class="c1">后端进程侧</span><span class="c2">当前值</span>
        <span class="c3">60s 最大</span><span class="c4">采样时刻 · 来源</span></div>
      <template v-for="g in GROUPS" :key="g">
        <div class="tr grp"><span class="c1">{{ GROUP_LABEL[g] || g }}</span>
          <span class="c2"></span><span class="c3"></span><span class="c4"></span></div>
        <div v-for="r in rowsOf(g)" :key="g + '.' + r.key" class="tr">
          <span class="c1" :title="r.desc">{{ r.label }}</span>
          <span class="c2">{{ r.cur }}</span>
          <span class="c3">{{ r.max }}</span>
          <span class="c4"><span class="mono">{{ r.at }}</span>
            <span class="src">{{ r.source }}</span>
            <span v-if="r.note" class="src nt">· {{ r.note }}</span></span>
        </div>
      </template>
    </div>

    <p v-if="!metrics.hasData" class="small err">
      {{ metrics.statusText }} —— 这不代表后端健康，只代表<b>这块表还没有数</b>。
    </p>
    <p v-else-if="metrics.stale" class="small err">
      {{ metrics.statusText }}：下面这些数**是旧的**（保留最后一次读数便于排障），
      别当当前状态看。
    </p>
  </div>
</template>

<script setup>
// ===================================================================
// ★ B1 运行指标一张表（挂在「运维审计」页）。方案 §4 B1 的口径：
//   一张表 + 每条一行「当前值 | 60s 最大 | 采样时刻」。
//
//   ① 后端进程级：唯一取数入口 = stores/metrics.js::quad(分组, 键) ——
//      任何缺字段都返回 value=null 的四元组。组件**不许**自己下钻
//      `snap.groups.ws.backlog_clients?.value || 0`（那会把"没测"渲染成"0"，
//      §5.5 铁律③）。
//   ② 浏览器侧：utils/perfSampler.js（帧时 / draw call / 停帧秒数），
//      **默认关闭**，勾选才采；开销本身要显示（下面「采样器自身」那一行）。
//
//   「60s 最大」在**本组件**算：store 明确不做均值/最差（两处各算一套必打架），
//   滚动窗口只保留 60s 的读数。
//   ★ 非数值项（last_path / active_run_ids 这类）**不比大小**，
//     最大列写"—（不是数值）"，不假装能比。
// ===================================================================
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import Icon from "./Icon.vue";
import { useMetricsStore, formatQuad, METRICS_STALE_MS } from "../stores/metrics.js";
import { samplerEnabled, enable as startSampler, disable as stopSampler, summary }
  from "../utils/perfSampler.js";

const metrics = useMetricsStore();
const sampling = ref(samplerEnabled());
const perf = ref(summary());

const HIST_MS = 60000;
const hist = new Map();      // key -> [{v, t}]
const pushed = new Map();    // key -> 最后一次入窗的"值|时刻"签名（重复渲染不叠样本）
let timer = null;

const SAMPLER_HINT = "帧时环形缓冲 + longtask 观察器 + renderer.info 抽样。"
  + "默认关：没人看的仪表不该长期吃性能；离开本页自动关。";

const GROUPS = ["collector", "modbus", "ws", "runs", "vision", "events"];
const GROUP_LABEL = { collector: "采集器", modbus: "Modbus 链路", ws: "WebSocket 广播",
                      runs: "执行登记", vision: "视觉服务", events: "事件写入" };
const LABEL = {
  loop_period_ms: "采集节拍", loop_ms: "单轮耗时", read_fail_streak: "连续读失败",
  configured_read_hz: "配置读取频率",
  read_pose_rt_ms: "位姿读耗时", read_pose_fail: "位姿读失败累计",
  read_regs_rt_ms: "寄存器读耗时", read_regs_fail: "寄存器读失败累计",
  clients_total: "在线客户端", clients_pose: "位姿流客户端", clients_events: "事件流客户端",
  backlog_clients: "积压客户端数", broadcast_enqueue_ms: "广播入队耗时",
  broadcast_frames: "广播帧数累计",
  active_runs: "在飞执行数", active_run_ids: "在飞执行号", progress_entries: "进度条目",
  last_run_id: "最近执行号", queue_len: "执行队列长度", thread_pool_tokens: "线程池上限",
  http_rt_ms: "视觉 HTTP 耗时", http_calls: "视觉调用数", http_fail: "视觉失败数",
  last_path: "最近视觉路径", last_error: "最近视觉错误",
  writes: "事件写入数", write_fail: "事件写入失败", write_ms: "事件写入耗时",
  write_rate: "事件写入速率", backlog: "事件积压",
};
const PERF_LABEL = {
  fps: "帧率 fps", frame_mean_ms: "帧时均值 (ms)", frame_p50_ms: "帧时 p50 (ms)",
  frame_p95_ms: "帧时 p95 (ms)", frame_worst_ms: "帧时最差 (ms)",
  stalled_sec: "停帧累计 (s)", worst_stall_ms: "单次最长停帧 (ms)",
  long_tasks: "长任务次数", long_task_worst_ms: "最长长任务 (ms)",
  draw_calls: "draw call", triangles: "三角形数",
  geometries: "几何体数", textures: "纹理数", heap_used_mb: "JS 堆 (MB)",
};

function fmtNum(v) {
  if (v === null || v === undefined) return null;
  return typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v);
}

/** 滚动 60s 最大：只收数值；同一份读数被重复渲染多次也只入一次窗。 */
function pushHist(key, v, tMs) {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  const sig = `${v}|${tMs}`;
  if (pushed.get(key) !== sig) {
    pushed.set(key, sig);
    let a = hist.get(key);
    if (!a) { a = []; hist.set(key, a); }
    a.push({ v, t: tMs });
    const cut = tMs - HIST_MS;
    while (a.length && a[0].t < cut) a.shift();
  }
  const a = hist.get(key) || [];
  let mx = -Infinity;
  for (const p of a) if (p.v > mx) mx = p.v;
  return a.length ? mx : null;
}

const clockAt = computed(() => (metrics.at ? new Date(metrics.at).toLocaleTimeString() : "—"));

/** 后端一组的行（读 quad()；单位跟着四元组走，不自造）。 */
function rowsOf(g) {
  const grp = metrics.groups ? metrics.groups[g] : null;
  if (!grp) return [];
  const out = [];
  const tMs = metrics.at || Date.now();
  for (const key of Object.keys(grp)) {
    const q = metrics.quad(g, key);
    const num = typeof q.value === "number" && Number.isFinite(q.value);
    const mx = num ? pushHist(g + "." + key, q.value, tMs) : null;
    const unit = q.unit && q.unit !== "-" ? " " + q.unit : "";
    out.push({
      key,
      label: LABEL[key] || key,             // 后端版本新增的字段照样显示原名（不藏）
      desc: q.desc || "",
      cur: formatQuad(q),
      max: mx !== null ? fmtNum(mx) + unit : (num ? "—" : "—（不是数值）"),
      at: q.ts ? String(q.ts).slice(11) : clockAt.value,
      source: q.source || "",
      note: q.note || "",
    });
  }
  return out;
}

const perfRows = computed(() => {
  const s = perf.value || {};
  const at = s.at ? new Date(s.at).toLocaleTimeString() : "—";
  const out = [];
  for (const key of Object.keys(PERF_LABEL)) {
    const v = s[key];
    const num = typeof v === "number" && Number.isFinite(v);
    const mx = num ? pushHist("perf." + key, v, Date.now()) : null;
    out.push({
      key, label: PERF_LABEL[key],
      cur: num ? fmtNum(v) : "—（还没采到样本）",
      max: mx !== null ? fmtNum(mx) : (num ? "—" : "—（不是数值）"),
      at: num ? at : "—",
      note: key === "heap_used_mb" && !num
        ? "本页内核不提供 performance.memory（只有 Chromium 系有）" : (s.note || ""),
    });
  }
  return out;
});

const statusLine = computed(() => {
  const parts = [];
  parts.push(metrics.hasData
    ? (metrics.stale ? `后端指标已过期（>${Math.round(METRICS_STALE_MS / 1000)}s 未更新）`
      : `后端指标正常 · 运行 ${metrics.uptimeSec ?? "—"}s`)
    : "后端指标未取到");
  if (metrics.pollFails) parts.push(`连续失败 ${metrics.pollFails} 次`);
  return parts.join(" · ");
});

function toggleSampling() {
  if (sampling.value) { stopSampler(); sampling.value = false; }
  else { startSampler(); sampling.value = true; }
  perf.value = summary();
}

function tick() {
  if (samplerEnabled()) perf.value = summary();
  // 让 60s 窗口继续滑动（后端 5s 才回一次数，停在这一页也要能看出"最大"是窗口内的）
  const cut = Date.now() - HIST_MS;
  for (const [k, a] of hist) {
    while (a.length && a[0].t < cut) a.shift();
    if (!a.length && !k.startsWith("perf.")) hist.delete(k);
  }
}

onMounted(() => {
  metrics.start();                     // 与 App.vue 的轮询同源（store 内幂等，不会叠两份）
  timer = setInterval(tick, 1000);
});
onBeforeUnmount(() => {
  if (timer) { clearInterval(timer); timer = null; }
  if (samplerEnabled()) stopSampler();   // 离开本页不再采样（默认关 = 不吃性能）
});
</script>

<style scoped>
.metrics-sec .tip { margin: 2px 0 8px; line-height: 1.6; }
.switch { font-size: 12px; display: flex; align-items: center; gap: 5px; cursor: pointer; }
.tbl { display: flex; flex-direction: column; gap: 1px; margin: 6px 0 10px;
  border: 1px solid var(--line, #263041); border-radius: 6px; overflow: hidden; }
.tr { display: grid; grid-template-columns: 1.1fr .8fr .8fr 1.7fr; gap: 6px;
  padding: 3px 8px; font-size: 12px; }
.tr + .tr { border-top: 1px solid var(--line, #212a38); }
.tr.th { color: var(--muted, #8b98a8); font-size: 11px; background: var(--panel, #111722); }
.tr.grp { background: var(--panel, #0f1520); color: var(--muted, #93a1b3); font-size: 11px; }
.tr.cost { color: var(--muted, #8b98a8); font-style: normal; }
.c1 { font-weight: 600; }
.c2, .c3 { text-align: right; font-variant-numeric: tabular-nums; }
.c2wide { text-align: left; }
.c4 { color: var(--muted, #8b98a8); display: flex; flex-direction: column; font-size: 11px; }
.src { opacity: .72; word-break: break-all; font-size: 10px; }
.nt { opacity: .95; }
.mono { font-variant-numeric: tabular-nums; }
.err { color: var(--danger, #e57373); }
</style>
