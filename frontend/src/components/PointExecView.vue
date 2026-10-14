<script setup>
// =====================================================================
// 点位执行（从原「点位执行」页拆分出来的"点位"部分）
//   滑块示教（关节角 / 直角坐标 + 残影预演）
//   J1~J6 点动（增量 + 连续，带看门狗）
//   预设点位 CRUD / 导入导出 / 单点执行
//   + 摄像头窗口：★ 直接复用「真实监控」的同一个 CameraPanel，不另写一份
//
// 拆分的理由：示教/点动是"把机器人挪到位"的慢操作，程序执行是"跑一串动作"
// 的连续操作，两者混在一页时右侧栏要滚很长，且运行程序期间还允许拖滑块示教，
// 互相抢控制权。分开后每页只做一件事。
//
// 安全规则（前端拦截，后端再校验）全部在 stores/exec.js 里统一实现。
// =====================================================================
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from "vue";
import MonitorLayout from "./MonitorLayout.vue";
import Icon from "./Icon.vue";
import CameraPanel from "./CameraPanel.vue";
import ExecControlCard from "./ExecControlCard.vue";
import TwinResidualCard from "./TwinResidualCard.vue";
import WhatIfCard from "./WhatIfCard.vue";
import { useAuthStore } from "../stores/auth.js";
import { useRobotStore } from "../stores/robot.js";
import { useSafetyStore } from "../stores/safety.js";
import { useExecStore, JOG_STEPS } from "../stores/exec.js";
import { useRcReadyStore, NOT_READY_HINT } from "../stores/rcReady.js";
import { apiUrl } from "../config.js";
import { apiControl } from "../net/control.js";
import { highlightJoint } from "../three/manager.js";

const auth = useAuthStore();
const robot = useRobotStore();
const safe = useSafetyStore();
const exec = useExecStore();
const rc = useRcReadyStore();

// ★ 控制权限必须同时满足：有令牌 + 机器人已连接
const canControl = computed(() => auth.controlActive && robot.connected && !robot.telemetryStale);

// ★ 需求：滑块示教 / 点动 J1~J6 的**所有组件**（含角速度、步长等设置项）只有在
//   「一键就绪」成功后（伺服上电 + 程序运行）才允许修改/点击 —— 否则整块锁死。
//   伺服没上电时改这些也没用，还会让人以为"点了没反应 = 坏了"。
const readyOk = computed(() => rc.ready);

const fileInput = ref(null);

// ---- 编辑表单（页面局部 UI 状态，不必进 store）----
const editing = ref(null);            // null | 'new' | pointId
const form = reactive({
  id: null, name: "", group: "默认", kind: "joint",
  joints: [0, 0, 0, 0, 0, 0],
  tcp: { x: 300, y: 0, z: 700, rx: 0, ry: 0, rz: 0 },
  note: "",
});
const formErr = ref("");

const LIMITS = computed(() => exec.limits);

function openNew() {
  editing.value = "new";
  form.id = null; form.name = ""; form.group = "默认"; form.kind = "joint";
  form.joints = [0, 0, 0, 0, 0, 0];
  form.tcp = { x: 300, y: 0, z: 700, rx: 0, ry: 0, rz: 0 };
  form.note = ""; formErr.value = "";
}
function openEdit(p) {
  editing.value = p.id;
  form.id = p.id; form.name = p.name; form.group = p.group; form.kind = p.kind;
  form.joints = (p.joints || [0, 0, 0, 0, 0, 0]).slice();
  form.tcp = p.tcp && typeof p.tcp === "object"
    ? { ...form.tcp, ...p.tcp } : { x: 300, y: 0, z: 700, rx: 0, ry: 0, rz: 0 };
  form.note = p.note || ""; formErr.value = "";
}
function cancelEdit() { editing.value = null; }

async function savePoint() {
  formErr.value = "";
  if (!form.name.trim()) { formErr.value = "请填写点位名称"; return; }
  let joints = form.joints.slice();
  if (form.kind === "cartesian") {
    const r = await exec.solveTcp(form.tcp);
    if (!r.ok) { formErr.value = r.error; return; }
    joints = r.joints;
  }
  const res = await exec.savePoint({
    name: form.name, group: form.group, kind: form.kind,
    joints, note: form.note,
    tcp: form.kind === "cartesian" ? form.tcp : null,
  }, editing.value === "new" ? null : form.id);
  if (res.ok) editing.value = null;
  else formErr.value = res.error;
}

async function delPoint(p) {
  if (!confirm(`确认删除点位「${p.name}」？`)) return;
  await exec.delPoint(p);
}

/** 把当前示教姿态填进"新建点位"表单。 */
function saveTeachAsPoint() {
  const seed = exec.teachAsPointPayload();
  openNew();
  form.kind = seed.kind;
  form.joints = seed.joints;
  form.name = seed.name;
}

function exportPoint(p) { window.open(apiUrl(`/points/${p.id}/export`), "_blank"); }

/** 点列表里的点 → 选中（残影常驻跟随该点位姿）。与悬停临时预演区分开。 */
function onSelectPoint(p) { exec.pinPoint(p); }
/** 鼠标移出某点：若已选中（pin）其它点则回到该选中点的残影；否则收起。 */
function onLeavePoint(p) {
  if (exec.pinnedPointId != null) {
    const pp = exec.points.find((x) => x.id === exec.pinnedPointId);
    if (pp) exec.pinPoint(pp); else exec.hideGhost();
  } else {
    exec.unhoverPoint();
  }
}
/** 是否「标记当前点」生成的标记点（用于显示「已标记」徽标）。 */
function isMarked(p) { return !!(p && p.name && p.name.startsWith("标记点")); }

// ★★ 2026-10-11：`J6正面`/`J6反面` = "只看 J6" 的两点（名字与后端
//   services/sorting.py 的 OPTIONAL_EXTRA_POINTS / api/sorting.py 的 DUAL_FACE_FLIP_POINT 一致）。
//   · 后端已把这两点的下发一律转成**原地切面**（基姿态=当前实时关节，只覆盖第 6 轴），
//     禁止整姿态 → 界面这里**只显示第 6 轴**，并把按钮标成「切面」，
//     不再把"其余 5 轴写 0"当成坐标列出来（否则会让人误以为那 5 轴也参与）。
const J6_FACE_POINTS = new Set(["J6正面", "J6反面"]);
const isJ6Face = (p) => !!(p && J6_FACE_POINTS.has(p.name));

// ---- D2 点位数值体检（★ **只读**：接口不写库、不改点，界面也只显示建议）----
//   防的是真发生过的事故：库里 J6 录成 −360 → encode_angle 拒绝 → 整条流程终止。
//   体检给三档：±327 可表示 / 软限位裕度 / 粗略奇异指示（相对信号，不当严格判据）。
const audit = ref(null);
const auditErr = ref("");
const auditBusy = ref(false);
/** 「最该重录在前」开关：默认开 —— 体检的价值就是第一眼看到该处理的点。 */
const worstFirst = ref(true);
/** 上一次体检对应的点位签名（见 loadAudit 的签名去重） */
let auditSig = "";

async function loadAudit(force) {
  // 签名 = 体检真正消费的字段（id/名称/关节值/类型）。存/删/改点后签名会变 → 自动重跑；
  // 没变（例如只是重新拉了一次同样的库）就不重复算。
  const sig = exec.points.map((p) => `${p.id}|${p.name}|${p.kind}|${(p.joints || []).join(",")}`).join("#");
  if (!force && sig === auditSig) return;
  auditSig = sig;
  auditBusy.value = true;
  auditErr.value = "";
  try {
    // 与 GET /api/points 同口径：只读、不需令牌（体检不新暴露任何数据）
    const r = await fetch(apiUrl("/points/audit"));
    if (!r.ok) { auditErr.value = `体检读取失败（HTTP ${r.status}）`; return; }
    audit.value = await r.json();
  } catch (e) {
    auditErr.value = `体检读取失败：${e.message}`;
  } finally {
    auditBusy.value = false;
  }
}

/** 按 **id** 对齐（同名点位不能靠名字映射）。 */
const auditById = computed(
  () => new Map(((audit.value && audit.value.points) || []).map((a) => [a.id, a])));
const auditRank = (p) => {
  const a = auditById.value.get(p.id);
  if (!a) return null;      // 体检没覆盖到这个点 → 不假装绿
  return GRADE_RANK[a.grade];
};
const GRADE_RANK = { bad: 0, warn: 1, unknown: 2, ok: 3 };

/** 列表显示顺序：最该重录的在前；**体检缺失的点排最后但不丢弃**（不许静默消失）。 */
const shownPoints = computed(() => {
  const list = exec.points.slice();
  if (!worstFirst.value) return list;
  return list.sort((a, b) => {
    const ra = auditRank(a), rb = auditRank(b);
    if (ra !== rb) return (ra === null ? 1e9 : ra) - (rb === null ? 1e9 : rb);
    const ma = auditById.value.get(a.id), mb = auditById.value.get(b.id);
    const va = ma && ma.worst_margin_deg != null ? ma.worst_margin_deg : 1e9;
    const vb = mb && mb.worst_margin_deg != null ? mb.worst_margin_deg : 1e9;
    return va - vb;
  });
});

const GRADE_LABEL = { ok: "正常", warn: "建议重录", bad: "下发会被拒", unknown: "无法体检" };
/** ★ 未知不得塌成"没问题"（§5.5 铁律③）：没有体检结果就照实说"未体检"。 */
function gradeOf(p) {
  const a = auditById.value.get(p.id);
  if (!a) return { key: "none", label: "未体检", title: "该点没有体检结果（点「重跑体检」）" };
  const notApplicable = a.applicable === false || a.evaluable === false;
  return {
    key: a.grade,
    label: notApplicable ? (a.applicable === false ? "不适用" : "无法体检") : GRADE_LABEL[a.grade],
    title: (a.reasons || []).join("\n") || "数值体检通过（可表示、在软限位内、不接近奇异）",
  };
}
/** 裕度条宽度：最紧轴余量 / **显示刻度** AUDIT_SCALE_DEG（只决定条画多长，
 *  ±327 与软限位数值永远来自后端，界面不复制一套）。 */
function marginPct(a) {
  if (!a || a.worst_margin_deg == null) return null;
  const g = Math.max(0, Math.min(a.worst_margin_deg, AUDIT_SCALE_DEG));
  return (g / AUDIT_SCALE_DEG) * 100;
}
const AUDIT_SCALE_DEG = 30;   // 30° 以上就画满（再宽也不算"更该重录"）

/** 该行是否值得展开详情：正常点不占版面；坏/紧/无法体检/未体检都要说清。 */
function auditIssue(p) {
  const a = auditById.value.get(p.id);
  return !!(a && a.grade !== "ok");
}
/** 条件数只在**真算出来**时显示；算不出留空（不写 0 冒充"不奇异"）。 */
function auditCond(p) {
  const a = auditById.value.get(p.id);
  const s = a && a.singularity;
  return s && s.measured && s.cond != null ? Number(s.cond).toExponential(1) : null;
}

function triggerImport() { fileInput.value && fileInput.value.click(); }
async function onImport(e) {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    const r = await apiControl("/points/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: data.name || "导入点位", group: data.group || "导入",
        kind: data.kind || "joint", joints: data.joints || [0, 0, 0, 0, 0, 0],
        tcp: data.tcp || null, note: data.note || "",
      }),
    });
    if (r.ok) await exec.loadPoints();
    else if (r.status === 401 || r.status === 403) { if (auth.requestLogin) auth.requestLogin(); }
    else alert("导入失败");
  } catch (err) { alert("导入文件解析失败：" + err.message); }
  e.target.value = "";
}

// ---- 页面生命周期：进入时同步示教基准，离开时收尾（残影/点动/姿态接管）----
watch(() => robot.activeView, (v) => {
  if (v === "point") exec.syncTeachFromRobot();
  else exec.leaveView();
});
onBeforeUnmount(() => exec.dispose());

onMounted(async () => {
  await exec.loadAll();
  if (exec.pointsErr) exec.logLine?.("warn", exec.pointsErr);
  if (auth.controlActive) exec.refreshState();
  setTimeout(() => exec.syncTeachFromRobot(), 300);
  // 体检只读、不需令牌，也不动机器人 —— 进页面就顺手跑一次（签名去重，不会重复算）
  loadAudit();
});

// ★ 点位一有增删改（存点 / 删除 / 导入 / 标记当前点都会重新拉列表）就重新体检，
//   免得"刚录的 −360 那个点"要刷新页面才知道有问题。
watch(() => exec.points, () => { loadAudit(); });

// 挂载时若还没有控制权限，loadFiles 会被 apiControl 在本地拦下（不发请求、不打 401）；
// 拿到权限后再补一次，用户不必为了看文件清单去刷新页面。
watch(() => auth.controlActive, (v) => {
  if (v) { exec.loadFiles(); exec.refreshState(); }
});
</script>

<template>
  <MonitorLayout view="point">
    <template #hud>
      <div class="exec-hud">
        点位执行 · {{ safe.lastSource === "ghost" ? "残影预演" : "实时位姿" }} ·
        {{ safe.lastState === "safe" ? "安全" : "告警：" + safe.lastState }}
      </div>
      <!-- 摄像头：与「真实监控」用的是同一个 CameraPanel（同一份状态与开关） -->
      <CameraPanel :views="['point', 'live']" />
    </template>

    <!-- ★ 右栏顶部：与「程序执行」共用的页内 tab 条（由 App.vue 传入 —— 两页合并成一个入口） -->
    <slot name="sidebarTop" />
    <ExecControlCard />
    <!-- ★ E2 到位自证（只读）：点动/到点之后就在这里看"这一段走没走到"。
         判据在后端一份（WS 位姿帧捎带 + GET /api/twin/residual），这里只渲染。 -->
    <TwinResidualCard />

    <!-- 关节点动（合并：滑块示教 + 点动 J1~J6） -->
    <div class="card">
      <h3><Icon name="sliders" :size="15" /> 关节点动 J1~J6
        <!-- ★ 副标题只报参数（当前步长），操作说明统一放在卡片底部一行，不重复三遍 -->
        <span class="h3-sub">步长 {{ exec.jogStepDeg }}°</span>
      </h3>
      <!-- ★ 需求：所有组件在"未就绪"时一律不可修改/不可点 -->
      <p v-if="!readyOk" class="lock-hint">
        <Icon name="lock" :size="13" /> {{ NOT_READY_HINT }}（伺服上电 + 程序运行）后，才可点动。
      </p>

      <!-- 步长 + 残影预演（同一行：步长靠左，残影预演由全局 margin-left:auto 推到行尾） -->
      <div class="pf-row" style="margin:8px 0">
        <label>步长</label>
        <div class="seg mini">
          <button v-for="s in JOG_STEPS" :key="s" :class="{ on: exec.jogStepDeg === s }"
                  :disabled="!readyOk" @click="exec.jogStepDeg = s">{{ s }}°</button>
        </div>
        <label class="ghost-toggle">
          <input type="checkbox" v-model="exec.teachGhost" :disabled="!readyOk"
                 @change="exec.teachGhost ? exec.schedulePreview() : exec.hideGhost()" />
          残影预演
        </label>
      </div>

      <!-- 六个关节：每行 [−] 滑块 [+] [数值]
           ★ 按钮内字符用真正的 − / +，不依赖 Icon 组件（之前 Icon.minus/plus 不存在 → 空白） -->
      <div class="jog-axis" v-for="(n, i) in robot.axes" :key="n"
           @mouseenter="highlightJoint(i)" @mouseleave="highlightJoint(-1)">
        <span class="ja-name">{{ n }}</span>
        <button class="ja-btn neg" :disabled="!readyOk || exec.jogBusy"
                @pointerdown.prevent="exec.jogPress(i + 1, -1)"
                @pointerup="exec.jogRelease" @pointerleave="exec.jogRelease"
                @pointercancel="exec.jogRelease"
                :title="'反向走一格（按住连续）'">
          −
        </button>
        <input class="ja-range" type="range"
               :min="LIMITS[i].min" :max="LIMITS[i].max" step="0.5"
               :disabled="!readyOk"
               :value="exec.teachQ[i]"
               @input="exec.onTeachSlider(i, $event)" />
        <button class="ja-btn pos" :disabled="!readyOk || exec.jogBusy"
                @pointerdown.prevent="exec.jogPress(i + 1, 1)"
                @pointerup="exec.jogRelease" @pointerleave="exec.jogRelease"
                @pointercancel="exec.jogRelease"
                :title="'正向走一格（按住连续）'">
          +
        </button>
        <span class="ja-val">{{ Number(exec.teachQ[i]).toFixed(1) }}°</span>
      </div>

      <p class="small muted mt8">
        拖动滑块实时预演（残影跟随），松手即停；点击 −/+ 走一格，按住连续走。
      </p>

      <!-- 操作按钮 -->
      <div class="btns mt8">
        <button class="primary" :disabled="!exec.canExec || exec.teachBusy"
                @click="exec.applyTeach()">
          <Icon name="target" :size="13" /> 执行到位姿
        </button>
        <!-- ★★ 2026-10-12（用户口径「**没有就绪可以录点，但是不能移动**」）：
             「取当前位姿」只**读**实时姿态（不动机器人）→ 只要遥测可信即可；
             「存为点位」只**写配置**（点位库里多一行）→ 只要持有控制令牌即可 ——
             **未就绪也能录点**（不能点的永远是"移动/执行"那类）。 -->
        <button :disabled="!canControl || exec.teachBusy" @click="exec.syncTeachFromRobot()">
          <Icon name="refresh" :size="13" /> 取当前位姿
        </button>
        <button :disabled="!auth.controlActive" @click="saveTeachAsPoint">
          <Icon name="file" :size="13" /> 存为点位
        </button>
      </div>
    </div>

    <!-- 预设点位 -->
    <div class="card">
      <h3><Icon name="database" :size="15" /> 预设点位
        <span class="h3-sub">{{ exec.points.length }} 个</span>
      </h3>
      <div class="btns" style="margin:10px 0">
        <button class="primary" @click="openNew"><Icon name="edit" :size="14" /> 新建点位</button>
        <button :disabled="!canControl"
                :title="canControl ? '读取当前位姿存为点位（不运动）'
                                  : '需控制权限且机器人已连接、遥测可信'"
                @click="exec.markCurrentPoint">
          <Icon name="pin" :size="14" /> 标记当前点
        </button>
        <button @click="triggerImport"><Icon name="upload" :size="14" /> 导入</button>
        <button v-if="exec.runBusy" class="warn" @click="exec.abortRun()">
          <Icon name="stop" :size="14" /> 停止
        </button>
        <input ref="fileInput" type="file" accept="application/json" style="display:none"
               @change="onImport" />
      </div>
      <p class="small pt-hint">
        <Icon name="pin" :size="12" /> 「标记当前点」读取机器人实时位姿（T1/T2 示教器手动对位也可用，仅记录、不发送运动）；
        点列表里任意点位 → 残影常驻跟随其位置；「执行」需在 AUTO 下一键就绪后。
      </p>

      <!-- ★ D2 数值体检（±327 可表示 / 软限位裕度 / 粗略奇异指示）
           防的是真事故：库里 J6 录成 −360 → encode_angle 拒绝 → 整条流程被终止。
           ★ 只读：不写库、不改点、不下发；界面显示的永远是**建议**。 -->
      <div class="pt-aud">
        <div class="pa-head">
          <span class="pa-title"><Icon name="shield" :size="13" /> 数值体检</span>
          <template v-if="audit">
            <span class="pa-chip g-bad">下发会被拒 {{ audit.by_grade.bad }}</span>
            <span class="pa-chip g-warn">建议重录 {{ audit.by_grade.warn }}</span>
            <span class="pa-chip g-unknown">无法体检 {{ audit.by_grade.unknown }}</span>
            <span class="pa-chip g-ok">正常 {{ audit.by_grade.ok }}</span>
          </template>
          <span v-else-if="auditErr" class="pa-chip g-unknown">{{ auditErr }}</span>
          <span v-else class="pa-chip g-unknown">还没跑过 —— 下方徽标是「未体检」，不假装全绿</span>
          <span class="pa-spacer" />
          <label class="pa-sort" title="按 bad → warn → unknown → ok 排；体检没覆盖的点排最后但不丢弃">
            <input v-model="worstFirst" type="checkbox" /> 最该重录在前
          </label>
          <button :disabled="auditBusy" @click="loadAudit(true)">
            <Icon name="refresh" :size="13" /> {{ auditBusy ? "体检中…" : "重跑体检" }}
          </button>
        </div>
        <p v-if="audit" class="small pa-not">
          <Icon name="info" :size="12" />
          <b>本体检不查：</b>{{ (audit.not_checked || []).join("；") }}。
          {{ audit.note }}
        </p>
      </div>

      <div v-if="editing" class="pt-form">
        <div class="pf-row">
          <label>名称</label>
          <input v-model="form.name" type="text" placeholder="点位名称" />
          <label>分组</label>
          <input v-model="form.group" type="text" />
        </div>
        <div class="pf-row">
          <label>类型</label>
          <select v-model="form.kind">
            <option value="joint">关节角</option>
            <option value="cartesian">直角坐标</option>
          </select>
        </div>
        <template v-if="form.kind === 'joint'">
          <!-- ★ pf-grid6：六等分列（标签一行 + 输入一行），六个框等宽；
               不能用 pf-grid 的 28px/1fr 成对列模板（会宽窄不一，见现场截图） -->
          <div class="pf-grid6">
            <label v-for="i in 6" :key="i">J{{ i }}</label>
            <input v-for="i in 6" :key="'j' + i" type="number" step="1"
                   v-model.number="form.joints[i - 1]" />
          </div>
        </template>
        <template v-else>
          <div class="pf-grid">
            <label>X</label><input type="number" v-model.number="form.tcp.x" />
            <label>Y</label><input type="number" v-model.number="form.tcp.y" />
            <label>Z</label><input type="number" v-model.number="form.tcp.z" />
            <label>Rx</label><input type="number" v-model.number="form.tcp.rx" />
            <label>Ry</label><input type="number" v-model.number="form.tcp.ry" />
            <label>Rz</label><input type="number" v-model.number="form.tcp.rz" />
          </div>
          <p class="small">直角坐标将在保存时经逆运动学解析为关节角（需控制权限）。</p>
        </template>
        <div class="pf-row">
          <label>备注</label>
          <input v-model="form.note" type="text" placeholder="可选" />
        </div>
        <p v-if="formErr" class="pf-err"><Icon name="alert" :size="13" /> {{ formErr }}</p>
        <div class="btns">
          <button class="primary" @click="savePoint"><Icon name="check" :size="14" /> 保存</button>
          <button @click="cancelEdit">取消</button>
        </div>
      </div>

      <div class="pt-list">
        <div v-for="p in shownPoints" :key="p.id" class="pt-item"
             :class="{ selected: exec.pinnedPointId === p.id }"
             @mouseenter="exec.hoverPoint(p)" @mouseleave="onLeavePoint(p)"
             @click="onSelectPoint(p)">
          <div class="pt-main">
            <span class="pt-name">{{ p.name }}</span>
            <div class="pt-tags">
              <span v-if="isMarked(p)" class="pt-tag marked"><Icon name="pin" :size="11" /> 已标记</span>
              <span class="pt-tag">{{ p.group }}</span>
              <span class="pt-tag" :class="{ warn: p.kind === 'cartesian' }">
                {{ p.kind === "cartesian" ? "直角" : "关节" }}
              </span>
              <!-- ★ D2 数值体检徽标：unknown/未体检**绝不显示成绿**（§5.5 铁律③） -->
              <span class="pt-tag audit" :class="'g-' + gradeOf(p).key"
                    :title="gradeOf(p).title">{{ gradeOf(p).label }}</span>
            </div>
          </div>
          <div class="pt-coords">
            <!-- ★ J6 正/反面：只显示第 6 轴（其余轴不参与切面），不把"其余轴为 0"当坐标列出 -->
            <template v-if="isJ6Face(p)">
              <span class="coord"><b>J6</b>{{ Number((p.joints || [])[5]).toFixed(3) }}</span>
              <span class="coord j6-only">仅 J6 有效 · 其余轴不使用</span>
            </template>
            <template v-else>
              <span class="coord" v-for="(jv, ji) in p.joints" :key="ji">
                <b>J{{ ji + 1 }}</b>{{ Number(jv).toFixed(3) }}
              </span>
            </template>
          </div>
          <!-- ★ D2 体检详情：只在**有问题/无法体检**时展开一行（正常点不占版面）。
               裕度条 = 最紧轴离软限位还剩几度；文字是后端的 reasons/advice，
               **只是建议**，界面不会替操作员改点。 -->
          <div v-if="auditIssue(p)" class="pt-audit" @click.stop>
            <div v-if="auditById.get(p.id).worst_margin_deg != null" class="pa-bar"
                 :title="'最紧轴离软限位 ' + auditById.get(p.id).worst_margin_deg + '°'">
              <i :class="'g-' + auditById.get(p.id).grade"
                 :style="{ width: (marginPct(auditById.get(p.id)) || 0) + '%' }" />
              <span class="pa-num">余量 {{ auditById.get(p.id).worst_margin_deg }}°</span>
            </div>
            <div v-if="auditCond(p) != null" class="pa-cond"
                 title="雅可比条件数：相对指标，只用于点位之间横向比较，不是严格奇异判据">
              条件数 {{ auditCond(p) }}（相对指标）
            </div>
            <ul v-if="(auditById.get(p.id).reasons || []).length" class="pa-why">
              <li v-for="(w, wi) in auditById.get(p.id).reasons" :key="wi">{{ w }}</li>
            </ul>
            <p v-if="auditById.get(p.id).advice" class="pa-adv">
              <Icon name="alert" :size="12" /> {{ auditById.get(p.id).advice }}
            </p>
          </div>
          <div v-if="p.note" class="pt-note"><Icon name="note" :size="12" /> {{ p.note }}</div>
          <div class="pt-ops" @click.stop>
            <button :disabled="!exec.canExec || exec.runBusy" @click="exec.execPoint(p)"
                    :title="isJ6Face(p) ? '原地切面：只在当前位置转 J6（J1~J5 不动）' : '移到该点位'">
              <Icon name="target" :size="13" /> {{ isJ6Face(p) ? "切面" : "执行" }}
            </button>
            <button :disabled="!canControl" title="把残影摆到该点位预演"
                    @click="exec.previewPointGhost(p.id)">
              <Icon name="sliders" :size="13" /> 预演
            </button>
            <button @click="openEdit(p)"><Icon name="edit" :size="13" /> 编辑</button>
            <!-- ★ P6：导出 / 删除收进「⋯」更多菜单 —— 原来 5 颗按钮平铺、密度高且删除易误点。
                 删除仍在菜单里保持红色 + confirm（二次确认不放宽）。 -->
            <details class="pt-more">
              <summary class="mini-more" title="更多">⋯</summary>
              <div class="pt-more-pop">
                <button @click="exportPoint(p)"><Icon name="download" :size="13" /> 导出</button>
                <button class="pt-del" @click="delPoint(p)"><Icon name="trash" :size="13" /> 删除</button>
              </div>
            </details>
          </div>
        </div>
        <p v-if="!exec.points.length" class="small">
          暂无点位，点击「新建点位」创建，或用滑块示教后「存为点位」。
        </p>
      </div>
    </div>

    <!-- ★ E3 反事实试跑（只读实验台）：与上面的「数值体检」配套 ——
         体检管"这个点数本身能不能下发"，试跑管"这样改（偏移/面/次序）值不值得上真机"。
         本卡**没有任何**执行入口：唯一请求是 POST /api/points/whatif（离线算分布），
         决定权在人（见 stores/whatif.js 与 backend/tests/test_whatif.py 的静态钉）。 -->
    <WhatIfCard />
  </MonitorLayout>
</template>

<style scoped>
/* ★ P5：把模板里零散的 inline margin 收敛为类（不再逐处硬编码 style） */
.mt8 { margin-top: 8px; }
/* ★ 未就绪时示教/点动区的"锁"提示条（说明为什么整块点不动） */
.lock-hint {
  display: flex; align-items: center; gap: 6px;
  margin: 2px 0 8px; padding: 7px 10px;
  font-size: 11px; line-height: 1.5;
  color: var(--warn); background: var(--warn-soft);
  border: 1px solid var(--warn-line); border-radius: 8px;
}
/* ★ 残影预演（与步长同一行）：全局 .pf-row label 强制 width:40px 会把"残影预演"
   四个字挤成竖排（当初截图里的"空位"就是这么来的），这里放开宽度 + 禁止换行；
   右对齐由全局 .ghost-toggle 的 margin-left:auto 负责。 */
.pf-row .ghost-toggle { width: auto; white-space: nowrap; }
/* ★ 点位卡片：T1/T2 提示条 + 选中高亮 + 备注行 */
.pt-hint {
  display: flex; align-items: flex-start; gap: 5px;
  margin: -2px 0 8px; line-height: 1.5;
}
.pt-hint svg { color: var(--accent); flex: none; margin-top: 2px; }
/* ★ 点位列表：纵向堆叠（上下关系），取消默认横向内联 */
.pt-item {
  display: block;                 /* 取消 flex：名称 / 标签 / 坐标 / 备注 / 操作 上下排列 */
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
  cursor: pointer;
  transition: border-color .15s, background .15s;
}
.pt-item:hover { border-color: var(--accent-line); }
.pt-item.selected {
  border-color: var(--accent);
  background: var(--accent-soft);
  box-shadow: inset 3px 0 0 var(--accent);
}
.pt-main {
  display: flex; flex-direction: column; align-items: flex-start; gap: 4px;
}
.pt-name { font-size: 13px; font-weight: 600; color: var(--txt); }
.pt-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.pt-tag {
  font-size: 11px; padding: 1px 7px; border-radius: 20px;
  color: var(--muted); border: 1px solid var(--line); background: var(--panel2);
}
.pt-tag.warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.pt-tag.marked {
  color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft);
  display: inline-flex; align-items: center; gap: 3px;
}
.pt-tag.marked svg { flex: none; }
.pt-coords { display: flex; flex-wrap: wrap; gap: 4px 8px; margin: 6px 0 2px; }
.coord {
  font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums;
  background: var(--panel2); border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px;
}
.coord b { color: var(--accent); font-weight: 600; margin-right: 3px; }
/* ★ J6 正/反面：坐标区只挂"仅 J6 有效"这句说明（区别于普通数值块） */
.coord.j6-only { color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft); }
.pt-note {
  display: flex; align-items: center; gap: 5px;
  margin: 5px 2px 2px; font-size: 11px; color: var(--muted); line-height: 1.4;
}
.pt-note svg { color: var(--muted); flex: none; }

/* ---- D2 点位数值体检（只读徽标 + 裕度条 + 建议） -------------------
   四档配色里 unknown / 未体检 **一律虚线灰**，不给绿色 —— "不知道"绝不能画成"没问题"。 */
.pt-aud {
  margin: 2px 0 10px; padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel2);
}
.pa-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.pa-title {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; font-weight: 600; color: var(--txt);
}
.pa-spacer { flex: 1 1 auto; }
.pa-sort {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; color: var(--muted); cursor: pointer;
}
.pa-not { margin: 6px 0 0; line-height: 1.5; }
.pa-chip, .pt-tag.audit {
  font-size: 11px; padding: 0 7px; border-radius: 20px;
  border: 1px solid var(--line); background: var(--panel); color: var(--muted);
}
.pa-chip.g-ok, .pt-tag.audit.g-ok { color: var(--ok); border-color: var(--ok-line); background: var(--ok-soft); }
.pa-chip.g-warn, .pt-tag.audit.g-warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.pa-chip.g-bad, .pt-tag.audit.g-bad { color: var(--err); border-color: var(--err-line); background: var(--err-soft); }
/* 无法体检 / 不适用 / 未体检：虚线 = "这里没有结论" */
.pa-chip.g-unknown, .pt-tag.audit.g-unknown, .pt-tag.audit.g-none { border-style: dashed; }
.pt-audit { display: flex; flex-direction: column; gap: 4px; margin: 7px 0 2px; cursor: default; }
.pa-bar { display: flex; align-items: center; gap: 7px; }
.pa-bar i { flex: none; height: 6px; border-radius: 4px; min-width: 2px; }
.pa-bar i.g-ok { background: var(--ok); }
.pa-bar i.g-warn { background: var(--warn); }
.pa-bar i.g-bad { background: var(--err); }
.pa-bar i.g-unknown { background: var(--muted); }
.pa-num { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.pa-cond { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.pa-why { margin: 0; padding-left: 16px; font-size: 11px; color: var(--warn-fg); line-height: 1.5; }
.pa-adv {
  display: flex; align-items: flex-start; gap: 4px; margin: 0;
  font-size: 11px; color: var(--muted); line-height: 1.5;
}
.pa-adv svg { flex: none; margin-top: 2px; }
@media (max-width: 1200px) {
  .pa-head { gap: 4px; }
  .pa-spacer { flex-basis: 100%; }
}
.pt-ops { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
/* ★ P6：「⋯」更多菜单（导出 / 删除） */
.pt-more { position: relative; flex: none; }
.mini-more { list-style: none; cursor: pointer; display: inline-flex;
  align-items: center; justify-content: center; width: 26px; height: 24px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel); color: var(--muted);
  font-weight: 700; font-size: 13px; user-select: none; }
.mini-more::-webkit-details-marker { display: none; }
.mini-more:hover { color: var(--accent); border-color: var(--accent); }
.pt-more[open] .mini-more { color: var(--accent); border-color: var(--accent); }
.pt-more-pop { position: absolute; right: 0; top: calc(100% + 4px); z-index: 5;
  display: flex; flex-direction: column; gap: 4px; padding: 6px; min-width: 96px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  box-shadow: var(--shadow-lg); }
.pt-more-pop button { justify-content: flex-start; width: 100%; }
.btns button.warn {
  color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft);
}
.btns button.warn:hover:not(:disabled) { filter: brightness(0.97); }

/* ★ 合并后的「关节点动 J1~J6」行：每行 = [轴名] [−按钮] [滑块] [+按钮] [数值]
   紧凑横向布局，整体按卡片内宽度自适应，拖动滑块时其余关节不抖 */
.jog-axis {
  /* 用户指定样式（2026-10-09）：收紧间隙与内边距，保证单行装得下 */
  display: flex; align-items: center; gap: 5px; padding: 2px;
}
.ja-name {
  flex: none; width: 20px;           /* 用户指定 20px */
  font-size: 13px; font-weight: 600; color: var(--accent);
  font-family: "Chakra Petch", sans-serif; letter-spacing: 0.5px;
  text-align: left;
}
.ja-btn {
  flex: none; width: 30px; height: 30px;
  display: inline-flex; align-items: center; justify-content: center;
  padding: 0; border-radius: 8px;
  background: var(--panel2); color: var(--txt);
  border: 1px solid var(--line);
  font-size: 16px; font-weight: 700; line-height: 1;
  font-family: "JetBrains Mono", monospace;
  cursor: pointer; transition: border-color .12s, background .12s, color .12s, box-shadow .12s;
}
.ja-btn:hover:not(:disabled) {
  border-color: var(--accent); color: var(--accent);
  box-shadow: 0 0 8px rgba(47, 214, 240, 0.25);
}
.ja-btn:active:not(:disabled) {
  background: var(--accent-soft); color: var(--accent);
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.35);
}
.ja-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.ja-btn.neg { /* 形态略不同，便于一眼分辨方向 */ }
.ja-btn.pos { /* 形态略不同，便于一眼分辨方向 */ }
.ja-range {
  flex: 1 1 auto; min-width: 0;
  height: 6px; padding: 0;
  appearance: none; -webkit-appearance: none;
  background: linear-gradient(90deg, var(--panel2), var(--accent-soft));
  border: 1px solid var(--line); border-radius: 999px;
  cursor: pointer;
  transition: border-color .12s;
}
.ja-range:hover:not(:disabled),
.ja-range:focus:not(:disabled) { border-color: var(--accent); }
.ja-range:disabled { opacity: 0.45; cursor: not-allowed; }
.ja-range::-webkit-slider-thumb {
  appearance: none; -webkit-appearance: none;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--accent);
  border: 2px solid #0a1628;
  box-shadow: 0 0 8px rgba(47, 214, 240, 0.55);
  cursor: grab;
  transition: transform .12s;
}
.ja-range::-webkit-slider-thumb:hover { transform: scale(1.15); }
.ja-range::-webkit-slider-thumb:active { cursor: grabbing; transform: scale(1.05); }
.ja-range::-moz-range-thumb {
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--accent); border: 2px solid #0a1628;
  box-shadow: 0 0 8px rgba(47, 214, 240, 0.55);
  cursor: grab;
}
.ja-range::-moz-range-track { background: transparent; border: 0; }
.ja-val {
  /* 数值列宽：用户指定 45px（够装最宽情形 "-360.0°"） */
  flex: none; width: 45px; text-align: right;
  font-family: "JetBrains Mono", monospace; font-size: 12px;
  color: var(--txt); font-variant-numeric: tabular-nums;
}
</style>
