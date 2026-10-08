<script setup>
// =====================================================================
// 分拣页（三色方块分拣 · 38 个示教点位 · 经控制器 210 执行）
//
//   流程：A 区逐个取料 → 送 C 区摄像头停顿判色 → 按颜色放 B 区
//         （第 1 列红 / 第 2 列绿 / 第 3 列蓝）
//   异常（吸不到 / 空跑 / 检测不到 / 该列已满）→ **立即停止 → 回开始/结束点**
//   不做任何自动恢复；不引入真空反馈硬件（现场没有）。
//
//   方案：docs/方案-三色方块分拣页（38点位·经210执行）.md
//   后端：/api/sorting/points（体检）、/start（可 dry_run 空跑）、/state、/cancel
//
//   ★ 空跑（dry_run）只校验点位与读帧判色，**绝不下发运动** —— 第一次务必先空跑。
// =====================================================================
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import Icon from "./Icon.vue";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "../stores/auth.js";

const auth = useAuthStore();

const chk = ref(null);          // 点位体检（38 点位）
const chkErr = ref("");
const st = ref(null);           // 后端分拣进度快照
const running = ref(false);
const busy = ref(false);
const msg = ref("");
const err = ref("");

// 参数（与后端 StartIn 对齐）
const dryRun = ref(true);       // ★ 默认空跑：先不动机器
const speed = ref(5);
const dwellMs = ref(1000);
const frames = ref(3);
const blowMs = ref(400);
const maxPieces = ref(9);

let timer = null;

async function loadCheck() {
  chkErr.value = "";
  try {
    const r = await apiControl("/sorting/points");
    if (r.status === 401 || r.status === 403) { chkErr.value = "需要控制令牌"; return; }
    const d = await r.json();
    if (r.ok) chk.value = d; else chkErr.value = d.detail || ("HTTP " + r.status);
  } catch (e) { chkErr.value = "体检失败：" + ((e && e.message) || e); }
}

async function poll() {
  try {
    const r = await apiControl("/sorting/state");
    if (!r.ok) return;
    const d = await r.json();
    running.value = !!d.running;
    st.value = d.state || null;
  } catch (e) { /* 轮询失败不影响流程 */ }
}

/** 统一取出后端的错误文案与"缺哪些点位"。
 *
 *  ★ 后端全局异常处理器会包一层 `{code, message, detail}`，而 `message` **既可能是
 *    字符串，也可能是对象**（`HTTPException(detail={...})` 时整个对象被塞进 message，
 *    detail 反而是 null）。两种形状都要认 —— 否则最有用的一句"缺 38 个点位"会被吞掉，
 *    只剩"启动失败 HTTP 400"，用户不知道差什么。
 */
function errText(d, status) {
  const asStr = (v) => (typeof v === "string" ? v
    : (v && typeof v.message === "string" ? v.message : ""));
  const base = asStr(d && d.message) || asStr(d && d.detail) || ("HTTP " + status);
  let miss = [];
  for (const v of [d && d.detail, d && d.message]) {
    if (v && typeof v === "object" && Array.isArray(v.missing)) { miss = v.missing; break; }
  }
  return { base, miss };
}

async function start() {
  err.value = ""; msg.value = ""; busy.value = true;
  try {
    const r = await apiControl("/sorting/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dry_run: dryRun.value, speed_pct: speed.value, dwell_ms: dwellMs.value,
        frames: frames.value, blow_ms: blowMs.value, max_pieces: maxPieces.value,
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const { base, miss } = errText(d, r.status);
      err.value = miss.length
        ? base + "（缺：" + miss.slice(0, 8).join("、")
          + (miss.length > 8 ? " 等 " + miss.length + " 个" : "") + "）"
        : base;
      return;
    }
    st.value = d.state || null;
    running.value = true;
    msg.value = dryRun.value ? "空跑已启动（不下发运动）" : "分拣已启动";
  } catch (e) {
    err.value = "启动异常：" + ((e && e.message) || e);
  } finally { busy.value = false; }
}

async function stop() {
  busy.value = true;
  try {
    await apiControl("/sorting/cancel", { method: "POST" });
    msg.value = "已请求停止（会在当前动作结束后停，并回开始/结束点）";
  } catch (e) { err.value = "停止异常：" + ((e && e.message) || e); }
  finally { busy.value = false; }
}

onMounted(() => {
  loadCheck();
  poll();
  timer = setInterval(() => { if (!running.value) poll(); else poll(); }, 800);
});
onBeforeUnmount(() => { if (timer) clearInterval(timer); });

// ---------- 展示辅助 ----------
const stage = computed(() => (st.value && st.value.stage) || "—");
const piece = computed(() => (st.value && st.value.piece) || 0);
const total = computed(() => (st.value && st.value.total) || maxPieces.value);
const progressPct = computed(() => Math.min(100, Math.round((piece.value / (total.value || 1)) * 100)));
const colorNow = computed(() => (st.value && st.value.color) || "");
const confNow = computed(() => (st.value && st.value.conf) || 0);
const targetNow = computed(() => (st.value && st.value.target) || "");
const placement = computed(() => (st.value && st.value.placement) || []);
const stopReason = computed(() => (st.value && st.value.error) || "");
const canStart = computed(() => auth.controlActive && !busy.value && !running.value
  && !!(chk.value && chk.value.ok));

/** B 区槽位 → 是否已放 + 颜色（给 3×3 网格上色） */
const bCell = computed(() => {
  const map = {};
  for (const c of placement.value || []) {
    for (const s of (c.used || [])) map[s] = c.color;
  }
  return map;
});
const A_SLOTS = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9"];
/** A 区已取：按"做到第几件"推（第 i 件对应 A{i}），正在做的那个高亮 */
function aState(i) {
  if (!running.value && !st.value) return "";
  const n = i + 1;
  if (n < piece.value) return "done";
  if (n === piece.value) return "on";
  return "";
}
function colorClass(c) {
  return c === "红" ? "c-red" : c === "绿" ? "c-green" : c === "蓝" ? "c-blue" : "";
}
</script>

<template>
  <div class="srt">
    <div class="srt-top">
      <span class="srt-mark"><Icon name="layers" :size="20" /></span>
      <div class="srt-ht">
        <div class="srt-title">三色方块分拣</div>
        <div class="srt-sub">
          A 区取料 → C 区摄像头判色 → B 区按颜色放料（第 1 列红 / 第 2 列绿 / 第 3 列蓝）；
          异常即停并回开始/结束点
        </div>
      </div>
      <span class="srt-badge" :class="{ on: running, dry: dryRun }">
        {{ running ? "运行中" : "空闲" }}{{ dryRun ? " · 空跑" : "" }}
      </span>
    </div>

    <!-- 点位体检 -->
    <div class="card" :class="{ bad: chk && !chk.ok }">
      <h3><Icon :name="chk && chk.ok ? 'check' : 'alert'" :size="15" /> 点位体检
        <span class="h3-sub" v-if="chk">{{ chk.found }}/{{ chk.total }}</span>
        <span class="h3-refresh" title="重新体检" @click="loadCheck"><Icon name="refresh" :size="13" /></span>
      </h3>
      <p v-if="chkErr" class="small err">{{ chkErr }}</p>
      <template v-else-if="chk">
        <p v-if="chk.ok" class="small ok">38 个点位齐备，可以开跑。</p>
        <p v-else class="small err">
          缺 {{ chk.missing.length }} 个点位，先到「点位执行」页补齐（缺一个都不允许启动）：
        </p>
        <div v-if="!chk.ok" class="chips">
          <span v-for="m in chk.missing" :key="m" class="chip warn">{{ m }}</span>
        </div>
        <p class="small muted">
          需要：A1–A9 与其“上”、B1–B9 与其“上”、C检测、开始/结束点（共 38）。
        </p>
      </template>
    </div>

    <!-- 参数 -->
    <div class="card">
      <h3><Icon name="sliders" :size="15" /> 运行参数</h3>
      <div class="pf-row">
        <label>速度</label>
        <span class="ro">{{ speed }}%（与右上角全局速度同源）</span>
        <input type="range" min="5" max="100" v-model.number="speed" :disabled="running" />
      </div>
      <div class="pf-row">
        <label>C 区停顿</label>
        <input type="number" min="0" max="10000" step="100" v-model.number="dwellMs" :disabled="running" />
        <span class="unit">ms</span>
        <label style="margin-left:14px">抓帧</label>
        <input type="number" min="1" max="9" v-model.number="frames" :disabled="running" />
        <span class="unit">帧</span>
      </div>
      <div class="pf-row">
        <label>放料吹气</label>
        <input type="number" min="0" max="3000" step="50" v-model.number="blowMs" :disabled="running" />
        <span class="unit">ms</span>
        <label style="margin-left:14px">本轮件数</label>
        <input type="number" min="1" max="9" v-model.number="maxPieces" :disabled="running" />
        <span class="unit">件</span>
      </div>
      <div class="btns" style="margin-top:8px">
        <button class="primary" :disabled="!canStart" @click="start">
          <Icon name="play" :size="13" /> {{ running ? "运行中…" : "开始" }}
        </button>
        <button class="rec-on" :disabled="!running && !busy" @click="stop">
          <Icon name="stop" :size="13" /> 停止
        </button>
        <label class="dry-toggle">
          <input type="checkbox" v-model="dryRun" :disabled="running" />
          空跑（只校验点位与判色，<b>不下发运动</b>）
        </label>
      </div>
      <p v-if="err" class="small err"><Icon name="alert" :size="12" /> {{ err }}</p>
      <p v-else-if="msg" class="small ok">{{ msg }}</p>
      <p v-if="running" class="small muted">
        「停止」会在**当前动作结束后**生效（点动/吸放中途不停，那是急停的语义），然后回开始/结束点。
      </p>
    </div>

    <!-- 进度 + 判色 -->
    <div class="card">
      <h3><Icon name="activity" :size="15" /> 运行状态
        <span class="h3-sub">{{ running ? "运行中" : "空闲" }}</span>
      </h3>
      <div class="row"><span class="k">阶段</span><span class="v">{{ stage }}</span></div>
      <div class="row"><span class="k">进度</span>
        <span class="v">{{ piece }}/{{ total }} 件{{ dryRun ? "（空跑）" : "" }}</span>
      </div>
      <div class="prog-bar"><i :style="{ width: progressPct + '%' }"></i></div>
      <div class="row" style="margin-top:8px">
        <span class="k">本次判色</span>
        <span class="v">
          <span v-if="colorNow" class="dotc" :class="colorClass(colorNow)"></span>
          {{ colorNow || "—" }}
          <span v-if="confNow" class="muted">conf {{ Number(confNow).toFixed(2) }}</span>
        </span>
      </div>
      <div class="row"><span class="k">落料槽位</span><span class="v">{{ targetNow || "—" }}</span></div>
      <div v-if="stopReason" class="teach-res err" style="margin-top:8px">
        <Icon name="alert" :size="13" /> 已停止：{{ stopReason }}
      </div>
    </div>

    <!-- A 区 3×3 -->
    <div class="card">
      <h3><Icon name="target" :size="15" /> A 区 · 原料（取料顺序 1→9）</h3>
      <div class="grid3">
        <div v-for="(s, i) in A_SLOTS" :key="s" class="cell" :class="aState(i)">
          <span class="sl">{{ s }}</span>
          <span class="sm">{{ aState(i) === 'done' ? '已取' : aState(i) === 'on' ? '取料中' : '待取' }}</span>
        </div>
      </div>
    </div>

    <!-- B 区 3×3（列=颜色） -->
    <div class="card">
      <h3><Icon name="layers" :size="15" /> B 区 · 放料（列 1 红 · 列 2 绿 · 列 3 蓝）</h3>
      <div class="grid3">
        <div v-for="s in ['B1','B2','B3','B4','B5','B6','B7','B8','B9']" :key="s"
             class="cell" :class="bCell[s] ? colorClass(bCell[s]) : ''">
          <span class="sl">{{ s }}</span>
          <span class="sm">{{ bCell[s] || "空" }}</span>
        </div>
      </div>
      <div class="cols">
        <span v-for="c in placement" :key="c.color" class="colstat">
          <i class="dotc" :class="colorClass(c.color)"></i>{{ c.color }}
          {{ c.filled }}/{{ c.capacity }}<b v-if="c.full"> 满</b>
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.srt { padding: 14px; display: flex; flex-direction: column; gap: 12px; overflow: auto; }
.srt-top { display: flex; align-items: center; gap: 12px; }
.srt-mark { display: inline-flex; color: var(--accent); }
.srt-ht { flex: 1; min-width: 0; }
.srt-title { font-size: 16px; font-weight: 700; color: var(--txt); }
.srt-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
.srt-badge { padding: 3px 9px; border-radius: 999px; font-size: 12px;
  border: 1px solid var(--line); color: var(--muted); }
.srt-badge.on { color: #fff; background: var(--ok, #2f9e6f); border-color: transparent; }
.srt-badge.dry { }
.card { border: 1px solid var(--line); border-radius: 10px; padding: 12px;
  background: var(--panel); }
.card.bad { border-color: var(--err, #e05252); }
.card h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 8px;
  font-size: 14px; color: var(--txt); }
.h3-sub { font-size: 11px; color: var(--muted); font-weight: 400; }
.h3-refresh { margin-left: auto; cursor: pointer; color: var(--muted); }
.h3-refresh:hover { color: var(--accent); }
.row { display: flex; gap: 10px; font-size: 13px; padding: 2px 0; }
.row .k { width: 76px; color: var(--muted); flex: none; }
.row .v { color: var(--txt); }
.small { font-size: 12px; line-height: 1.6; margin: 4px 0 0; }
.muted { color: var(--muted); }
.ok { color: var(--ok, #2f9e6f); }
.err { color: var(--err, #e05252); }
.chips { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0 0; }
.chip { font-size: 11px; padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--line); color: var(--muted); }
.chip.warn { color: #ffb4b4; border-color: #e0525255; }
.pf-row { display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 13px; }
.pf-row label { color: var(--muted); min-width: 62px; }
.pf-row input[type="range"] { flex: 1; }
.pf-row input[type="number"] { width: 84px; padding: 3px 6px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel); color: var(--txt); }
.pf-row .ro { color: var(--txt); }
.unit { color: var(--muted); font-size: 12px; }
.btns { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.dry-toggle { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  color: var(--muted); cursor: pointer; }
.prog-bar { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; margin-top: 6px; }
.prog-bar > i { display: block; height: 100%; background: var(--accent); }
.grid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.cell { border: 1px solid var(--line); border-radius: 8px; padding: 8px 6px;
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  background: var(--bg, transparent); min-height: 52px; justify-content: center; }
.cell .sl { font-size: 13px; color: var(--txt); }
.cell .sm { font-size: 11px; color: var(--muted); }
.cell.on { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.cell.done { opacity: .55; }
.cell.c-red { border-color: #e24b4a; }
.cell.c-red .sm { color: #f7c1c1; }
.cell.c-green { border-color: #639922; }
.cell.c-green .sm { color: #c0dd97; }
.cell.c-blue { border-color: #378add; }
.cell.c-blue .sm { color: #b5d4f4; }
.dotc { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
  margin-right: 4px; background: var(--muted); vertical-align: 0; }
.dotc.c-red { background: #e24b4a; }
.dotc.c-green { background: #639922; }
.dotc.c-blue { background: #378add; }
.cols { display: flex; gap: 14px; margin-top: 8px; font-size: 12px; color: var(--muted); }
.colstat b { color: var(--warn, #d98a2b); margin-left: 4px; }
.teach-res { display: flex; align-items: center; gap: 6px; font-size: 12px;
  padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line); }
.teach-res.err { color: #ffb4b4; border-color: #e0525255; }
</style>
