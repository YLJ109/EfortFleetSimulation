<script setup>
// =====================================================================
// 分拣页（左：仿真机器人 · 右：点位设置与分拣操作）
//
//   左侧：MonitorLayout 的 3D 视口（与其它页共用同一个 scene/canvas），
//         示教时看着机器人动，右边点「存点 / 到点」。
//   右侧：① 就绪（复用现成 RcReadyCard：一键就绪）  ② 页签「执行 / 点位」
//         · 执行：参数 + 空跑/开始/停止 + 进度 + 判色 + 三列占用
//         · 点位：精简点动 + A/B 各 9 格 × 2 槽（正上位 & 料位）+ C + 开始/结束点
//                 每个点位槽：存点（记当前位置）/ 到点（复核）/ 清空
//
//   38 个点位：A1–A9+A1上–A9上(18) + B1–B9+B1上–B9上(18) + C检测(1) + 开始/结束点(1)
//   方案：docs/方案-分拣页改版（左仿真·右点位设置与分拣操作）.md
//   后端：/api/sorting/{points,teach,goto,point,start,state,cancel}
//
//   ★ 示教只写配置（不下发运动）；「到点」是运动，走围栏互锁/轴锁/速度夹取，可被打断。
//   ★ 分拣运行期间：存点/到点/点动都会被后端拒绝（抢同一个 40135 触发位）。
// =====================================================================
import { ref, computed, onMounted, onBeforeUnmount, watch } from "vue";
import MonitorLayout from "./MonitorLayout.vue";
import RcReadyCard from "./RcReadyCard.vue";
import CameraPanel from "./CameraPanel.vue";   // ★ 复用现成的摄像头窗口，绝不重做
import Icon from "./Icon.vue";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "../stores/auth.js";
import { useRobotStore } from "../stores/robot.js";
import { useExecStore, JOG_STEPS } from "../stores/exec.js";

const auth = useAuthStore();
const robot = useRobotStore();
const exec = useExecStore();

const tab = ref("run");           // run | point
const chk = ref(null);            // 38 点位体检
const chkErr = ref("");
const st = ref(null);             // 分拣进度
const running = ref(false);
const busy = ref(false);
const msg = ref("");
const err = ref("");
const showMiss = ref(false);

// 运行参数（与后端 StartIn 对齐）
const dryRun = ref(true);         // ★ 默认空跑：先不动机器
const dwellMs = ref(1000);
const frames = ref(3);
const blowMs = ref(400);
const maxPieces = ref(9);

// 示教
const axis = ref(6);              // 当前点动轴
const pointBusy = ref("");
const pointMsg = ref("");

let timer = null;

const A_SLOTS = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9"];
const B_SLOTS = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"];

/** 每个格子的两个点位槽：正上位 + 料位（A 叫原料位、B 叫放料位）。 */
const aCells = A_SLOTS.map((s) => ({ key: s, slots: [
  { name: s + "上", label: "正上位" }, { name: s, label: "原料位" }] }));
const bCells = B_SLOTS.map((s) => ({ key: s, slots: [
  { name: s + "上", label: "正上位" }, { name: s, label: "放料位" }] }));
const extraSlots = [
  { name: "C检测", label: "摄像头位" },
  { name: "开始/结束点", label: "开始/结束" },
];

/** 未示教名单（体检结果里就是"缺哪些"）→ 用来判断每个点位槽的状态。 */
const missing = computed(() => new Set((chk.value && chk.value.missing) || []));
const isTaught = (name) => !!(chk.value && chk.value.ok) || !missing.value.has(name);

// ---------- 点位坐标 与 残影 ----------
// ★ 为什么接残影：只看一行数字看不出"这个点到底摆在哪"。悬停/点选让 3D 里的残影
//   直接摆到该点位姿 —— 示教对不对、有没有撞工装，一眼就看得出来。
//   复用 exec store 里现成的 hoverPoint（临时预演）/ pinPoint（常驻选中），不另造轮子。
const pinnedName = ref("");    // 残影 A（安全判定以它为准）
const pinnedBName = ref("");   // ★ 残影 B（仅用于两个点位对比）

/** 按名字取点位（exec.points 是 /points 的全量列表，loadPoints 后可用）。 */
const ptOf = (name) => (exec.points || []).find((p) => p.name === name) || null;

/** 点位存的是什么：关节角或直角坐标（kind=cartesian 的点存 tcp）。 */
function coordText(p) {
  if (!p) return "未示教";
  if (p.kind === "cartesian" && p.tcp) {
    const t = p.tcp;
    const f = (v) => (v === null || v === undefined ? "-" : Number(v).toFixed(1));
    return "XYZ " + f(t.x) + " " + f(t.y) + " " + f(t.z) + " / RPY " + f(t.rx) + " " + f(t.ry) + " " + f(t.rz);
  }
  const j = p.joints;
  if (!j || !j.length) return "无坐标";
  return j.map((v) => Number(v).toFixed(1)).join("  ");
}

/** 悬停 → 残影临时预演该点。 */
function hover(name) {
  const p = ptOf(name);
  if (p) exec.hoverPoint(p);
}
function unhover() {
  // 常驻选中的那个要保留，别被移开鼠标清掉
  if (pinnedName.value) { const p = ptOf(pinnedName.value); if (p) exec.pinPoint(p); }
  else exec.unhoverPoint();
}

/**
 * 点名字 → 残影**常驻**跟随该点（再点一次取消）。
 * ★ **Shift + 点** → 摆到**第二个残影**（淡橙），实现"两个点位同屏对比"。
 *   为什么要分两个：同一个位置只放一个残影时，无法回答"从正上方落到槽位会不会撞"，
 *   两个残影同屏才能一眼看出落差/干涉。B 只做视觉对比，**不参与围栏判定**。
 */
function pin(name, ev) {
  const p = ptOf(name);
  if (!p) return;
  const toB = !!(ev && ev.shiftKey);
  if (toB) {
    if (pinnedBName.value === name) { pinnedBName.value = ""; exec.unpinPointB(); }
    else { pinnedBName.value = name; exec.pinPointB(p); }
    return;
  }
  if (pinnedName.value === name) {
    pinnedName.value = "";
    exec.unpinPoint();
    exec.hideGhost();
  } else {
    pinnedName.value = name;
    exec.pinPoint(p);
  }
}
function clearPin(side) {
  if (side === "a") { pinnedName.value = ""; exec.unpinPoint(); exec.hideGhost(); }
  else { pinnedBName.value = ""; exec.unpinPointB(); }
}
const pinnedPoint = computed(() => (pinnedName.value ? ptOf(pinnedName.value) : null));
const pinnedB = computed(() => (pinnedBName.value ? ptOf(pinnedBName.value) : null));
/** 两点距离（mm）—— 同屏对比时最想知道的就是"差多少"。 */
const distAB = computed(() => {
  const a = pinnedPoint.value, b = pinnedB.value;
  if (!a || !b) return null;
  if (a.kind === "cartesian" || b.kind === "cartesian") return null;
  const ja = a.joints, jb = b.joints;
  if (!ja || !jb || ja.length !== 6 || jb.length !== 6) return null;
  let s2 = 0;
  for (let i = 0; i < 6; i++) { const d = Number(jb[i]) - Number(ja[i]); s2 += d * d; }
  return Math.sqrt(s2);
});

/** 示教/清空后要同时刷新体检与点位坐标（残影也跟着更新）。 */
async function refreshPoints() {
  await loadCheck();
  await exec.loadPoints();
  for (const [nm, side] of [[pinnedName, "a"], [pinnedBName, "b"]]) {
    if (!nm.value) continue;
    const p = ptOf(nm.value);
    if (!p) { clearPin(side); continue; }
    if (side === "a") exec.pinPoint(p); else exec.pinPointB(p);
  }
}

// ---------- 接口 ----------
async function loadCheck() {
  chkErr.value = "";
  try {
    const r = await apiControl("/sorting/points");
    if (r.status === 401 || r.status === 403) { chkErr.value = "需要控制令牌"; return; }
    const d = await r.json();
    if (r.ok) { chk.value = d; } else { chkErr.value = d.message || ("HTTP " + r.status); }
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

/** 统一取后端错误文案（异常体是 {code,message,detail}，message 可能是对象）。 */
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
        dry_run: dryRun.value, speed_pct: exec.speed, dwell_ms: dwellMs.value,
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
      showMiss.value = true;
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
    msg.value = "已请求停止（当前动作结束后停，并回开始/结束点）";
  } catch (e) { err.value = "停止异常：" + ((e && e.message) || e); }
  finally { busy.value = false; }
}

/** 存点：把机器人**当前位置**存为该点位（运行中禁用）。 */
async function teach(name) {
  if (!window.confirm("把机器人当前位置存为「" + name + "」？\n（已示教过将覆盖）")) return;
  pointBusy.value = name; pointMsg.value = "";
  try {
    const r = await apiControl("/sorting/teach", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { pointMsg.value = "存点失败：" + errText(d, r.status).base; return; }
    pointMsg.value = "已保存 " + name + "：" + (d.joints || []).map((v) => Number(v).toFixed(1)).join(", ");
    await refreshPoints();
  } catch (e) { pointMsg.value = "存点异常：" + ((e && e.message) || e); }
  finally { pointBusy.value = ""; }
}

/** 到点：移到该点位复核（★ 是运动）。 */
async function goto(name) {
  pointBusy.value = name; pointMsg.value = "正在移到 " + name + " …";
  try {
    const r = await apiControl("/sorting/goto", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const d = await r.json().catch(() => ({}));
    pointMsg.value = r.ok ? ("已到 " + name) : ("到点失败：" + errText(d, r.status).base);
  } catch (e) { pointMsg.value = "到点异常：" + ((e && e.message) || e); }
  finally { pointBusy.value = ""; }
}

async function clearPoint(name) {
  if (!window.confirm("清空点位「" + name + "」？该点位将从点位库删除。")) return;
  pointBusy.value = name; pointMsg.value = "";
  try {
    const r = await apiControl("/sorting/point?name=" + encodeURIComponent(name),
                               { method: "DELETE" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { pointMsg.value = "清空失败：" + errText(d, r.status).base; return; }
    pointMsg.value = "已清空 " + name;
    await refreshPoints();
  } catch (e) { pointMsg.value = "清空异常：" + ((e && e.message) || e); }
  finally { pointBusy.value = ""; }
}

// ---------- 展示 ----------
const stage = computed(() => (st.value && st.value.stage) || "—");
const piece = computed(() => (st.value && st.value.piece) || 0);
const total = computed(() => (st.value && st.value.total) || maxPieces.value);
const progressPct = computed(() => Math.min(100, Math.round((piece.value / (total.value || 1)) * 100)));
const colorNow = computed(() => (st.value && st.value.color) || "");
const confNow = computed(() => (st.value && st.value.conf) || 0);
const targetNow = computed(() => (st.value && st.value.target) || "");
const placement = computed(() => (st.value && st.value.placement) || []);
const stopReason = computed(() => (st.value && st.value.error) || "");
const missingCount = computed(() => (chk.value ? chk.value.missing.length : 38));
const canStart = computed(() => auth.controlActive && !busy.value && !running.value
  && !!(chk.value && chk.value.ok));
const canTeach = computed(() => auth.controlActive && !running.value);
const bCell = computed(() => {
  const map = {};
  for (const c of placement.value || []) for (const s of (c.used || [])) map[s] = c.color;
  return map;
});
const colorClass = (c) => (c === "红" ? "c-red" : c === "绿" ? "c-green" : c === "蓝" ? "c-blue" : "");

function onAxis(d) { exec.jogPress(axis.value, d); }

watch(() => robot.activeView, (v) => {
  if (v !== "sort") { if (timer) { clearInterval(timer); timer = null; } }
  else if (!timer) { timer = setInterval(poll, 800); }
});
onMounted(() => { loadCheck(); exec.loadPoints(); poll(); timer = setInterval(poll, 800); });
onBeforeUnmount(() => { if (timer) clearInterval(timer); exec.hideGhosts(); });
</script>

<template>
  <MonitorLayout view="sort">
    <template #hud>
      <div class="exec-hud">分拣 · 三色方块 · {{ running ? "运行中" : "空闲" }}</div>
      <!-- ★ 摄像头画面：直接复用「真实监控/程序执行」同一个 CameraPanel 组件
           （同一个相机服务、同一套开关/设备选择/检测框），**没有另做一份**。
           views[0]='sort' 是本页（面板归属），带上 'live' 让真实监控页共用同一实例，
           避免"同一台相机被两个面板各开一路"。 -->
      <CameraPanel :views="['sort', 'live']" />
    </template>

    <!-- ① 就绪：直接复用现成卡片（一键就绪 / 取消 / 明细） -->
    <RcReadyCard />

    <!-- ② 页签：一屏一件事，右栏保持简单 -->
    <div class="seg mini srt-seg">
      <button :class="{ on: tab === 'run' }" @click="tab = 'run'">执行</button>
      <button :class="{ on: tab === 'point' }" @click="tab = 'point'">点位</button>
    </div>

    <!-- ================= 执行 ================= -->
    <template v-if="tab === 'run'">
      <div class="card">
        <h3><Icon :name="chk && chk.ok ? 'check' : 'alert'" :size="15" /> 点位体检
          <span class="h3-sub" v-if="chk">{{ chk.found }}/{{ chk.total }}</span>
          <span class="h3-refresh" title="重新体检" @click="loadCheck"><Icon name="refresh" :size="13" /></span>
        </h3>
        <p v-if="chkErr" class="small err">{{ chkErr }}</p>
        <template v-else-if="chk">
          <p v-if="chk.ok" class="small ok">38 个点位齐备，可以开跑。</p>
          <p v-else class="small err">
            缺 {{ missingCount }} 个点位 —— 切到「点位」页就地示教，缺一个都不允许启动。
            <button class="lnk" @click="showMiss = !showMiss">{{ showMiss ? "收起" : "看缺哪些" }}</button>
          </p>
          <div v-if="!chk.ok && showMiss" class="chips">
            <span v-for="m in chk.missing" :key="m" class="chip warn">{{ m }}</span>
          </div>
        </template>
      </div>

      <div class="card">
        <h3><Icon name="sliders" :size="15" /> 运行参数</h3>
        <p class="small muted">速度用右上角那一份（全项目唯一速度口径）：<b>{{ exec.speed }}%</b></p>
        <div class="pf-row">
          <label>C 区停顿</label>
          <input type="number" min="0" max="10000" step="100" v-model.number="dwellMs" :disabled="running" />
          <span class="unit">ms</span>
          <label class="ml">抓帧</label>
          <input type="number" min="1" max="9" v-model.number="frames" :disabled="running" />
          <span class="unit">帧</span>
        </div>
        <div class="pf-row">
          <label>放料吹气</label>
          <input type="number" min="0" max="3000" step="50" v-model.number="blowMs" :disabled="running" />
          <span class="unit">ms</span>
          <label class="ml">本轮件数</label>
          <input type="number" min="1" max="9" v-model.number="maxPieces" :disabled="running" />
          <span class="unit">件</span>
        </div>
        <div class="btns">
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
          「停止」在当前动作结束后生效（运动/吸放中途不停，那是急停的语义），随后回开始/结束点。
        </p>
      </div>

      <div class="card">
        <h3><Icon name="activity" :size="15" /> 运行状态
          <span class="h3-sub">{{ running ? "运行中" : "空闲" }}</span>
        </h3>
        <div class="row"><span class="k">阶段</span><span class="v">{{ stage }}</span></div>
        <div class="row"><span class="k">进度</span>
          <span class="v">{{ piece }}/{{ total }} 件{{ dryRun ? "（空跑）" : "" }}</span>
        </div>
        <div class="prog-bar"><i :style="{ width: progressPct + '%' }"></i></div>
        <div class="row mt"><span class="k">本次判色</span>
          <span class="v">
            <span v-if="colorNow" class="dotc" :class="colorClass(colorNow)"></span>
            {{ colorNow || "—" }}
            <span v-if="confNow" class="muted">conf {{ Number(confNow).toFixed(2) }}</span>
          </span>
        </div>
        <div class="row"><span class="k">落料槽位</span><span class="v">{{ targetNow || "—" }}</span></div>
        <div class="cols">
          <span v-for="c in placement" :key="c.color" class="colstat">
            <i class="dotc" :class="colorClass(c.color)"></i>{{ c.color }}
            {{ c.filled }}/{{ c.capacity }}<b v-if="c.full"> 满</b>
          </span>
        </div>
        <div v-if="stopReason" class="res err"><Icon name="alert" :size="13" /> 已停止：{{ stopReason }}</div>
      </div>
    </template>

    <!-- ================= 点位 ================= -->
    <template v-else>
      <div class="card sel-card">
        <h3><Icon name="layers" :size="15" /> 残影对比
          <span class="h3-sub">点名字=A(蓝) · Shift+点=B(橙)</span>
        </h3>
        <div class="grow a" v-if="pinnedPoint">
          <i class="gdot"></i>
          <span class="gname">A · {{ pinnedName }}</span>
          <span class="gj">{{ pinnedPoint.joints.map((v) => Number(v).toFixed(1)).join("  ") }}</span>
          <button class="mini-btn" @click="clearPin('a')">✕</button>
        </div>
        <p v-else class="small muted">A 未选 —— 点任意点位名即可摆上（蓝）</p>
        <div class="grow b" v-if="pinnedB">
          <i class="gdot"></i>
          <span class="gname">B · {{ pinnedBName }}</span>
          <span class="gj">{{ pinnedB.joints.map((v) => Number(v).toFixed(1)).join("  ") }}</span>
          <button class="mini-btn" @click="clearPin('b')">✕</button>
        </div>
        <p v-else class="small muted">B 未选 —— Shift + 点点位名摆上（橙，用于对比）</p>
        <p v-if="distAB !== null" class="small">
          两残影关节角距离 <b>{{ distAB.toFixed(2) }}</b>（6 轴欧氏距离，越小越接近）
        </p>
        <p class="small muted">A 参与围栏/地面判定；B 只做位置对比，不影响安全结论。</p>
      </div>

      <div class="card">
        <h3><Icon name="target" :size="15" /> 示教工具
          <span class="h3-sub">把机器人动到位 → 右格点「存」</span>
        </h3>
        <div class="jog-axes">
          <button v-for="n in 6" :key="'ax' + n" :class="{ on: axis === n }"
                  :disabled="!canTeach" @click="axis = n">J{{ n }}</button>
        </div>
        <div class="btns">
          <button :disabled="!canTeach" title="按住连续点动；点一下走一个步长"
                  @pointerdown.prevent="onAxis(-1)" @pointerup="exec.jogRelease()"
                  @pointerleave="exec.jogRelease()">
            <Icon name="chevronDown" :size="14" /> 反向
          </button>
          <button :disabled="!canTeach" title="按住连续点动；点一下走一个步长"
                  @pointerdown.prevent="onAxis(1)" @pointerup="exec.jogRelease()"
                  @pointerleave="exec.jogRelease()">
            <Icon name="chevronUp" :size="14" /> 正向
          </button>
          <span class="unit">步长</span>
          <div class="seg mini">
            <button v-for="s in JOG_STEPS" :key="'st' + s" :class="{ on: exec.jogStepDeg === s }"
                    :disabled="!canTeach" @click="exec.jogStepDeg = s">{{ s }}°</button>
          </div>
        </div>
        <p class="small muted">点动受轴锁/围栏/速度约束；分拣运行中会被拒绝。</p>
        <p v-if="pointMsg" class="small ok">{{ pointMsg }}</p>
      </div>

      <div class="card">
        <h3><Icon name="layers" :size="15" /> A 区 · 原料
          <span class="h3-sub">每格 2 点：正上位 + 原料位</span>
        </h3>
        <div class="teach-grid">
          <div v-for="cell in aCells" :key="'a' + cell.key" class="tcell">
            <div class="tcell-h">{{ cell.key }}</div>
            <div v-for="sl in cell.slots" :key="sl.name" class="slot"
                 @mouseenter="hover(sl.name)" @mouseleave="unhover()">
              <span class="sdot" :class="isTaught(sl.name) ? 'on' : ''"></span>
              <span class="sname" :class="{ pinned: pinnedName === sl.name, pinnedb: pinnedBName === sl.name }"
                    :title="sl.name + ' · ' + coordText(ptOf(sl.name))"
                    @click="pin(sl.name, $event)">{{ sl.label }}</span>
              <button class="mini-btn" :disabled="!canTeach || pointBusy === sl.name"
                      title="把机器人当前位置存为该点位" @click="teach(sl.name)">
                <Icon name="check" :size="12" /> 存
              </button>
              <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                      title="移到该点位复核" @click="goto(sl.name)">
                <Icon name="target" :size="12" /> 到
              </button>
              <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                      title="删除该点位" @click="clearPoint(sl.name)">
                <Icon name="trash" :size="12" />
              </button>
            </div>
            <div v-for="sl in cell.slots" :key="'c-' + sl.name" class="coord"
                 :title="sl.name + ' · ' + coordText(ptOf(sl.name))">
              <b>{{ sl.label }}</b>{{ coordText(ptOf(sl.name)) }}
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3><Icon name="layers" :size="15" /> B 区 · 放料
          <span class="h3-sub">列 1 红 · 列 2 绿 · 列 3 蓝</span>
        </h3>
        <div class="teach-grid">
          <div v-for="cell in bCells" :key="'b' + cell.key" class="tcell"
               :class="bCell[cell.key] ? colorClass(bCell[cell.key]) : ''">
            <div class="tcell-h">{{ cell.key }}<i v-if="bCell[cell.key]" class="dotc" :class="colorClass(bCell[cell.key])"></i></div>
            <div v-for="sl in cell.slots" :key="sl.name" class="slot"
                 @mouseenter="hover(sl.name)" @mouseleave="unhover()">
              <span class="sdot" :class="isTaught(sl.name) ? 'on' : ''"></span>
              <span class="sname" :class="{ pinned: pinnedName === sl.name, pinnedb: pinnedBName === sl.name }"
                    :title="sl.name + ' · ' + coordText(ptOf(sl.name))"
                    @click="pin(sl.name, $event)">{{ sl.label }}</span>
              <button class="mini-btn" :disabled="!canTeach || pointBusy === sl.name"
                      title="把机器人当前位置存为该点位" @click="teach(sl.name)">
                <Icon name="check" :size="12" /> 存
              </button>
              <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                      title="移到该点位复核" @click="goto(sl.name)">
                <Icon name="target" :size="12" /> 到
              </button>
              <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                      title="删除该点位" @click="clearPoint(sl.name)">
                <Icon name="trash" :size="12" />
              </button>
            </div>
            <div v-for="sl in cell.slots" :key="'c-' + sl.name" class="coord"
                 :title="sl.name + ' · ' + coordText(ptOf(sl.name))">
              <b>{{ sl.label }}</b>{{ coordText(ptOf(sl.name)) }}
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3><Icon name="camera" :size="15" /> C 区 与 开始/结束点</h3>
        <div class="slot wide" v-for="sl in extraSlots" :key="sl.name"
             @mouseenter="hover(sl.name)" @mouseleave="unhover()">
          <span class="sdot" :class="isTaught(sl.name) ? 'on' : ''"></span>
          <span class="sname" :class="{ pinned: pinnedName === sl.name, pinnedb: pinnedBName === sl.name }"
                :title="sl.name + ' · ' + coordText(ptOf(sl.name))"
                @click="pin(sl.name, $event)">{{ sl.label }}<span class="muted"> · {{ sl.name }}</span></span>
          <button class="mini-btn" :disabled="!canTeach || pointBusy === sl.name"
                  title="把机器人当前位置存为该点位" @click="teach(sl.name)">
            <Icon name="check" :size="12" /> 存点
          </button>
          <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                  title="移到该点位复核" @click="goto(sl.name)">
            <Icon name="target" :size="12" /> 到点
          </button>
          <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                  title="删除该点位" @click="clearPoint(sl.name)">
            <Icon name="trash" :size="12" />
          </button>
        </div>
        <div class="coord wide" v-for="sl in extraSlots" :key="'c-' + sl.name"
             :title="sl.name + ' · ' + coordText(ptOf(sl.name))">
          <b>{{ sl.name }}</b>{{ coordText(ptOf(sl.name)) }}
        </div>
        <p class="small muted">「开始/结束点」是一个点：一轮开始先去、结束与异常都回这里。</p>
      </div>
    </template>
  </MonitorLayout>
</template>

<style scoped>
.srt-seg { margin: 2px 0; }
.card { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
  background: var(--panel); }
.card h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 6px;
  font-size: 14px; color: var(--txt); }
.h3-sub { font-size: 11px; color: var(--muted); font-weight: 400; }
.h3-refresh { margin-left: auto; cursor: pointer; color: var(--muted); }
.h3-refresh:hover { color: var(--accent); }
.row { display: flex; gap: 10px; font-size: 13px; padding: 1px 0; }
.row.mt { margin-top: 6px; }
.row .k { width: 72px; color: var(--muted); flex: none; }
.row .v { color: var(--txt); }
.small { font-size: 12px; line-height: 1.6; margin: 4px 0 0; }
.muted { color: var(--muted); }
.ok { color: var(--ok, #2f9e6f); }
.err { color: var(--err, #e05252); }
.lnk { background: none; border: none; color: var(--accent); cursor: pointer;
  font-size: 12px; padding: 0 2px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.chip { font-size: 11px; padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--line); color: var(--muted); }
.chip.warn { color: #ffb4b4; border-color: #e0525255; }
.pf-row { display: flex; align-items: center; gap: 6px; margin: 6px 0; font-size: 13px; }
.pf-row label { color: var(--muted); }
.pf-row label.ml { margin-left: 12px; }
.pf-row input[type="number"] { width: 76px; padding: 3px 6px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel); color: var(--txt); }
.unit { color: var(--muted); font-size: 12px; }
.btns { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.dry-toggle { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  color: var(--muted); cursor: pointer; }
.prog-bar { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; margin-top: 4px; }
.prog-bar > i { display: block; height: 100%; background: var(--accent); }
.cols { display: flex; gap: 12px; margin-top: 6px; font-size: 12px; color: var(--muted); }
.colstat b { color: var(--warn, #d98a2b); margin-left: 3px; }
.res { display: flex; align-items: center; gap: 6px; font-size: 12px; margin-top: 8px;
  padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line); }
.res.err { color: #ffb4b4; border-color: #e0525255; }
.jog-axes { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 4px; }
.jog-axes button { padding: 4px 0; font-size: 12px; }
.jog-axes button.on { border-color: var(--accent); color: var(--accent); }
.teach-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.tcell { border: 1px solid var(--line); border-radius: 10px; padding: 9px 10px; }
.tcell-h { font-size: 14px; font-weight: 500; color: var(--txt); margin-bottom: 6px; }
.slot { display: flex; align-items: center; gap: 5px; padding: 3px 0; }
.slot.wide { padding: 7px 0; }
.sdot { width: 7px; height: 7px; border-radius: 50%; background: var(--line); flex: none; }
.sdot.on { background: var(--ok, #2f9e6f); }
.sname { flex: 1; min-width: 0; font-size: 13px; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sname.pinnedb { color: #ff9a3d; font-weight: 500; }
.mini-btn { padding: 3px 8px; font-size: 12px; border-radius: 6px; }
.sdot { width: 9px; height: 9px; }
.sname { cursor: pointer; }
.sname:hover { color: var(--accent); }
.sname.pinned { color: var(--accent); font-weight: 500; }
.coord { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0 3px 12px; }
.coord b { color: var(--txt); font-weight: 400; margin-right: 4px; }
.coord.wide { padding-left: 0; }
.sel-card { border-color: var(--accent); }
.grow { display: flex; align-items: center; gap: 6px; padding: 5px 7px; margin: 3px 0;
  border: 1px solid var(--line); border-radius: 7px; font-size: 12px; }
.grow.a { border-color: #2f9bff66; }
.grow.b { border-color: #ff9a3d66; }
.grow .gdot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.grow.a .gdot { background: #2f9bff; }
.grow.b .gdot { background: #ff9a3d; }
.grow .gname { flex: none; color: var(--txt); }
.grow .gj { flex: 1; min-width: 0; color: var(--muted); font-variant-numeric: tabular-nums;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grow .mini-btn { flex: none; }
.grow .mini-btn { flex: none; }
.sel-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; }
.sel-j { font-size: 11px; color: var(--muted); display: flex; justify-content: space-between;
  gap: 4px; border: 1px solid var(--line); border-radius: 5px; padding: 2px 5px; }
.sel-j b { color: var(--txt); font-variant-numeric: tabular-nums; font-weight: 500; }
.tcell.c-red { border-color: #e24b4a; }
.tcell.c-green { border-color: #639922; }
.tcell.c-blue { border-color: #378add; }
.dotc { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-left: 4px; background: var(--muted); }
.dotc.c-red { background: #e24b4a; }
.dotc.c-green { background: #639922; }
.dotc.c-blue { background: #378add; }
</style>
