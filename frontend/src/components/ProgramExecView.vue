<script setup>
// =====================================================================
// 程序执行（从原「点位执行」页拆分出来的"程序"部分）
//   点位序列程序的运行 / 中止 / 进度 / 逐步状态
//   按文件名执行（数据库程序 / 预设点位 / programs 目录 JSON）+ 试运行 dry-run
//   运行日志（带级别与时间戳，便于现场对节拍）
//   + 摄像头窗口：★ 直接复用「真实监控」的同一个 CameraPanel，不另写一份
//
// 运行逻辑全在 stores/exec.js（execProgram / runFile / abortRun），
// 与点位执行页共用同一套权限、围栏互锁与急停。
// =====================================================================
import { ref, computed, onMounted, onBeforeUnmount, watch } from "vue";
import MonitorLayout from "./MonitorLayout.vue";
import Icon from "./Icon.vue";
import CameraPanel from "./CameraPanel.vue";
import { useAuthStore } from "../stores/auth.js";
import { useRobotStore } from "../stores/robot.js";
import { useSafetyStore } from "../stores/safety.js";
import { useRcReadyStore } from "../stores/rcReady.js";
import { useExecStore } from "../stores/exec.js";
import { apiUrl } from "../config.js";

const auth = useAuthStore();
const robot = useRobotStore();
const safe = useSafetyStore();
const rc = useRcReadyStore();
const exec = useExecStore();

// 就绪提示盒：把「链路连接 + 控制器就绪 + 控制权限」合成一个结论；
// 未就绪时只给一句最该先处理的原因，不罗列整面格表。
// ★★ 2026-10-11 修复"同一页两个判据打架"：横幅与「直接运行」按钮**必须同源**。
//   原来横幅 = `robot.connected && rc.ready && auth.controlActive`，而按钮用 `exec.canExec`
//   （多含"残影预演不安全"/"遥测过期"）→ 遥测冻结或残影告警时横幅写着"可以执行代码"，
//   按钮却是灰的，同一屏两个结论。现在统一以 `exec.canExec` 为准，原因取 `exec.blockReason`。
const isReady = computed(() => exec.canExec);
const readyText = computed(() => {
  if (!robot.connected) return "机器人未连接，请先在底部「机器人链路」重连";
  const why = exec.blockReason;                 // 单一判据下的"为什么不能下发"
  if (why) return why;
  return "未获得控制权限，无法下发";
});

/** 当前展开步骤表的程序（页面局部 UI 状态）。 */
const selProgram = ref(null);

/** 文件扩展名（小写），无扩展名返回 ""。 */
function extOf(name) {
  return (name || "").includes(".")
    ? (name.split(".").pop() || "").toLowerCase() : "";
}

/**
 * 「本地程序」统一清单：数据库点位序列 + programs 目录 .json + 预设点位。
 *   （示教器 .XPL 由 exec.teachFiles 单独成卡，不在此列。）
 *   后端 /control/files 里 group="local" 的候选，再与 exec.programs 按 name 去重，
 *   避免数据库程序出现两次。
 * ★ 任一来源为空都如实显示，不拼接成假数据。
 */
const localItems = computed(() => {
  const out = [];
  const seen = new Set();
  const push = (it) => { if (!seen.has(it.name)) { seen.add(it.name); out.push(it); } };
  // 1) 数据库点位序列（公开接口 /programs，无需令牌就有）
  for (const pr of exec.programs || []) {
    push({ kind: "program", name: pr.name, id: pr.id, no: null,
           steps: pr.items_count || (pr.items || []).length || 0,
           tag: "点位序列", isXpl: false, obj: pr });
  }
  // 2) 本地文件 + 预设点位（/control/files，需控制令牌）
  for (const f of exec.localFiles || []) {
    const ext = f.file_ext || extOf(f.name);
    const label = f.kind === "point" ? "点位"
      : (ext === "json" ? "JSON" : "文件");
    push({ kind: f.kind, name: f.name, no: f.no != null ? f.no : null,
           steps: f.steps, tag: label, isXpl: false, obj: f });
  }
  return out;
});

// 「测试跑」（空跑校验）可用性：持有控制令牌且当前没有其它运行在进行。
// ★ 测试跑无论残影是否危险都能做 —— 它不下发、不移动，只是解析 + IK/限位预演。
const canTest = computed(() => auth.controlActive && !exec.fileBusy && !exec.runBusy);

/**
 * ★ 需求（手动执行重设计）：两个按钮走同一条 runFile 通道，只差 dryRun 开关。
 *   「测试」= dryRun（只解析/IK/限位校验，绝不下发）；「直接运行」= dryRun=false（真下发）。
 *   不在界面上暴露 dryRun 勾选框，避免"以为在测试其实在下发"的误判。
 */
async function runTest() {
  exec.dryRun = true;
  await exec.runFile();
}
async function runNow() {
  exec.dryRun = false;
  await exec.runFile();
}
/** 某个目标是否正处于测试跑中（用于列表行内"校验中…"提示）。 */
const testBusy = (name) => exec.fileBusy && exec.fileName === name;
const refetching = ref(false);
async function refetchFiles() {
  refetching.value = true;
  try { await exec.loadFiles(); } finally { refetching.value = false; }
}

// ---------- 序列编辑（六类操作）----------
// ★ 需求（2026-09-29）：像编辑器一样自己把顺序排好，然后执行 / 暂停 / 继续 / 停止执行，
//   可保存成本地文件、可选择已保存文件载入并执行。**六类**：标记点 / 吸 / 停止吸 / 放 / 停止放 / 等待。
//   方案见 docs/方案-程序执行序列编辑器（五类操作·经210执行）.md
//   全部执行都在后端逐步下发（经控制器常驻程序 210），前端只负责编排与进度。

/** 可载入的序列文件 = programs 目录里的 .json（后端保存的序列就是这种）。 */
const seqFiles = computed(() =>
  (exec.localFiles || []).filter((f) => String(f.file_ext || "").toLowerCase() === "json"));

const seqLoadFile = ref("");
function onLoadSeq() {
  const v = seqLoadFile.value;
  seqLoadFile.value = "";
  if (v) exec.loadSeq(v);
}

const seqTypeName = (t) => ({
  point: "标记点", suck: "吸", release: "停止吸", blow: "放", unblow: "停止放", wait: "等待",
}[t] || t);

/** 执行中高亮当前步（进度来自后端 run-state 轮询）。 */
function seqStepCls(i) {
  if (!exec.running) return "";
  const cur = (exec.runState && exec.runState.index) || exec.runStep;
  if (!cur) return "";
  if (i + 1 < cur) return "done";
  if (i + 1 === cur) return "on";
  return "";
}

/** 执行按钮可用性：**与「直接运行」同一判据**（`exec.canExec`：权限 + 就绪 + 残影安全）
 *  + 没有别的执行在跑 + 序列非空。
 *  ★★ 2026-10-11 修复：原来不含就绪门控 → 机器人离线/未一键就绪时按钮**可点**，
 *    按下去才被后端 409 拒（"按了却报机器人没就绪"），而同一页另一个下发按钮却是灰的。
 *    `runSeq(false)` 是**真实下发**，所以必须过同一道门控。 */
const runSeqDisabled = computed(() => !exec.canExec || exec.seqBusy
  || exec.fileBusy || exec.runBusy || !exec.seqItems.length);

// ---------- 运行日志 ----------
// ★ 运行日志由 MonitorLayout 左下角浮动面板 / 底部状态区「运行日志」片统一显示，
//   本页不再保留任何本地日志视图。

const steps = computed(() => {
  const pr = selProgram.value;
  if (!pr) return [];
  return (pr.items || []).map((it, i) => {
    const p = exec.points.find((x) => x.id === it.point_id);
    return {
      index: i + 1,
      name: (p && p.name) || ("点位 #" + (it.point_id ?? "?")),
      pointId: it.point_id,
      // ★ 实际执行速度 = 右上角全局速度 exec.speed（后台会按 v…perc 反算 40103，
      //   使"右上角速度 = 实际速度"）；it.speed_pct 是 XPL 内部常量，不代表下发速度，
      //   不能写死 100 误导用户。这里统一显示 exec.speed。
      speed: exec.speed,
      dwell: it.dwell_ms || 0,
      missing: !p,
    };
  });
});

/** 运行中：已完成 / 正在执行 / 待执行
 *
 *  ★ 审计修复 P1-D3：原实现读 `exec.trackStep` / `exec.trackName` ——
 *    这两个字段**在 store 里根本不存在**（grep 全前端只有这两处引用），
 *    `!exec.trackStep` 恒为 true → 函数永远返回 "" → 步骤高亮从未生效过。
 *    改用真实存在的 runStep / runRunName，并用"当前选中程序名"做归属判断，
 *    避免跑 A 程序时把 B 程序的列表染色。
 */
function stepCls(i) {
  if (exec.runKind !== "program") return "";
  const selName = (selProgram.value && selProgram.value.name) || "";
  if (!selName || exec.runName !== selName) return "";
  if (i < exec.runStep) return "done";
  if (i === exec.runStep) return "on";
  return "";
}

function exportProgram(pr) { window.open(apiUrl(`/programs/${pr.id}/export`), "_blank"); }

function pickProgram(pr) {
  const same = selProgram.value && selProgram.value.id === pr.id;
  selProgram.value = same ? null : pr;
  // ★ 审计修复 P2-D：点选必须同步写 exec.fileName。
  //   原实现只切 selProgram（组件内部状态），store 的 fileName 不变 →
  //   选中态高亮着，点「执行」却弹"请先选择或输入文件名"。
  exec.fileName = same ? "" : String(pr.name || "");
  // ★ 审计修复 P1-D2：选中即登记空格键（runOrAbort）的目标。
  //   否则只有"跑过一次"的程序才能被空格重复运行，刚选中的按空格等于空转。
  exec.setRunTarget(same ? null : pr);
}

watch(() => robot.activeView, (v) => { if (v !== "program") exec.leaveView(); });
onBeforeUnmount(() => { exec.dispose(); rc.stop(); });

onMounted(() => {
  exec.loadPrograms();
  exec.loadFiles();
  rc.start();
  if (auth.controlActive) exec.refreshState();
});

// 挂载时若还没有控制权限，loadFiles 会被 apiControl 在本地拦下（不发请求、不打 401）；
// 拿到权限后再补一次，用户不必为了看文件清单去刷新页面。
watch(() => auth.controlActive, (v) => {
  if (v) { exec.loadFiles(); exec.refreshState(); }
});

// ★ 一键就绪成功 → 自动刷新点位与程序清单（用户需求："一键就绪后点位自动刷新"）。
//   程序页没有 RcReadyCard，所以在这里单独挂一份。
watch(() => rc.readyTick, (n) => {
  if (n > 0) { exec.loadPoints(); exec.loadPrograms(); }
});
</script>

<template>
  <MonitorLayout view="program">
    <template #hud>
      <div class="exec-hud">
        程序执行 · {{ safe.lastSource === "ghost" ? "残影预演" : "实时位姿" }} ·
        {{ safe.lastState === "safe" ? "安全" : "告警：" + safe.lastState }}
      </div>
      <!-- 摄像头：与「真实监控」「点位执行」用的是同一个 CameraPanel -->
      <CameraPanel :views="['program', 'live']" />
    </template>

    <!-- ★ 右栏顶部：与「点位执行」共用的页内 tab 条（由 App.vue 传入 —— 两页合并成一个入口） -->
    <slot name="sidebarTop" />
    <!-- 就绪提示盒：一屏只给一个结论 —— 就绪 / 未就绪 + 一句原因。 -->
    <div class="ready-box" :class="{ ok: isReady }">
      <span class="rb-dot"></span>
      <div class="rb-body">
        <div class="rb-title">机器人{{ isReady ? "已就绪" : "未就绪" }}</div>
        <div class="rb-sub">{{ isReady ? "可以执行代码" : readyText }}</div>
      </div>
      <Icon :name="isReady ? 'check' : 'alert'" :size="18" class="rb-ico" />
    </div>

    <!-- ★ Pr3 修复：本页原另有一颗"清控制器报错"的按钮，与底栏「真机链路」面板里的 RcReadyCard
         重复（同名同义、且底栏任何页都能点开）。现统一走底栏那一个入口，本页不再重复。 -->

    <!-- 运行状态 -->
    <!-- ★ Pr4 修复：原 `v-if="exec.runTotal || exec.lastRun"` → 首跑前不显示、一旦开始运行才
         **插入**，把下方「序列编辑/示教器程序/本地程序/手动执行」整列往下顶（布局跳动）。
         现改为**常驻卡片**，内部用 v-if 区分"空闲/运行"两种内容，盒高稳定不跳。 -->
    <div class="card">
      <h3><Icon name="activity" :size="15" /> 运行状态
        <span class="h3-sub">{{ exec.running ? "运行中" : "空闲" }}</span>
      </h3>
      <div class="row">
        <span class="k">目标</span>
        <span class="v">{{ exec.runName || "—" }}</span>
      </div>
      <div class="row">
        <span class="k">进度</span>
        <span class="v">
          {{ exec.runStep }}/{{ exec.runTotal || "—" }}
          <template v-if="exec.runDry">（试运行）</template>
        </span>
      </div>
      <div class="prog-bar"><i :style="{ width: exec.runPct + '%' }"></i></div>
      <div class="btns" style="margin-top:8px">
        <button class="rec-on" :disabled="!exec.running && !exec.fileBusy" @click="exec.abortRun()">
          <Icon name="close" :size="13" /> 中止
        </button>
      </div>
      <div v-if="exec.lastRun" class="exec-result" :class="exec.lastRun.ok ? 'ok' : 'err'">
        <Icon :name="exec.lastRun.ok ? 'check' : 'alert'" :size="13" />
        <span v-if="exec.lastRun.ok">
          上次{{ exec.lastRun.dryRun ? "试运行" : "运行" }}通过：{{ exec.lastRun.name }}
          （{{ exec.lastRun.passed }}/{{ exec.lastRun.count }} 步，
          约 {{ (exec.lastRun.duration_ms / 1000).toFixed(1) }}s）
        </span>
        <span v-else>
          上次运行中断：{{ exec.lastRun.name }}（{{ exec.lastRun.passed }}/{{ exec.lastRun.count }} 步）
          {{ exec.lastRun.error ? " · " + exec.lastRun.error : "" }}
        </span>
      </div>
      <p v-else class="small muted" style="margin-top:6px">尚未运行 —— 选好程序或填好文件名后点「执行」。</p>
    </div>

    <!-- ★ 序列编辑（六类操作）：自己排序 → 执行 / 暂停 / 继续 / 停止执行 → 保存/载入 -->
    <div class="card">
      <h3><Icon name="layers" :size="15" /> 序列编辑
        <!-- ★ Pr7：每类都带 icon 的大按钮已直观表达，"六类：…"属冗余枚举，改为步骤数 -->
        <span class="h3-sub">{{ exec.seqItems.length }} 步</span>
      </h3>

      <div class="pf-row">
        <label>名称</label>
        <input v-model="exec.seqName" type="text" maxlength="40"
               :disabled="exec.running" placeholder="例如：取件-放件" />
      </div>

      <!-- ★ 六个操作按钮分两行（3 + 3），一行挤不下会换行错位 -->
      <div class="seq-add">
        <button :disabled="exec.seqBusy || exec.running" @click="exec.addSeqStep('point')">
          <Icon name="target" :size="13" /> 标记点
        </button>
        <button :disabled="exec.seqBusy || exec.running" @click="exec.addSeqStep('suck')">
          <Icon name="download" :size="13" /> 吸
        </button>
        <button :disabled="exec.seqBusy || exec.running" @click="exec.addSeqStep('release')">
          <Icon name="upload" :size="13" /> 停止吸
        </button>
        <button :disabled="exec.seqBusy || exec.running" @click="exec.addSeqStep('blow')">
          <Icon name="eject" :size="13" /> 放
        </button>
        <button :disabled="exec.seqBusy || exec.running" @click="exec.addSeqStep('unblow')">
          <Icon name="close" :size="13" /> 停止放
        </button>
        <button :disabled="exec.seqBusy || exec.running" @click="exec.addSeqStep('wait')">
          <Icon name="clock" :size="13" /> 等待
        </button>
      </div>

      <div class="pt-list" style="margin-top:8px">
        <div v-for="(it, i) in exec.seqItems" :key="'seq:' + i" class="pt-item" :class="seqStepCls(i)">
          <div class="pt-main">
            <span class="pt-tag">#{{ i + 1 }}</span>
            <span class="pt-name">{{ seqTypeName(it.type) }}</span>
            <select v-if="it.type === 'point'" :value="it.point_id"
                    :disabled="exec.seqBusy || exec.running"
                    @change="exec.setSeqPoint(i, $event.target.value)">
              <option v-for="p in exec.points" :key="'p' + p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <template v-else-if="it.type === 'wait'">
              <input class="seq-sec" type="number" min="0.1" max="3600" step="0.1"
                     :value="it.seconds" :disabled="exec.seqBusy || exec.running"
                     @change="exec.setSeqWait(i, $event.target.value)" />
              <span class="pt-joints">秒</span>
            </template>
            <span v-else class="pt-joints">
              {{ it.type === 'suck' ? '开真空（保持）'
                 : it.type === 'blow' ? '开吹气（保持）'
                 : it.type === 'unblow' ? '关吹气' : '关真空阀（不吹气）' }}
            </span>
          </div>
          <div class="pt-ops">
            <button :disabled="exec.seqBusy || exec.running || i === 0"
                    title="上移" @click="exec.moveSeqStep(i, -1)">
              <Icon name="chevronUp" :size="13" />
            </button>
            <button :disabled="exec.seqBusy || exec.running || i === exec.seqItems.length - 1"
                    title="下移" @click="exec.moveSeqStep(i, 1)">
              <Icon name="chevronDown" :size="13" />
            </button>
            <button :disabled="exec.seqBusy || exec.running"
                    title="删除" @click="exec.removeSeqStep(i)">
              <Icon name="trash" :size="13" />
            </button>
          </div>
        </div>
        <p v-if="!exec.seqItems.length" class="small">
          还没有步骤。用上面六个按钮添加，再用 ▲▼ 排好顺序 —— 顺序就是执行顺序。
        </p>
      </div>

      <div class="btns" style="margin-top:8px">
        <button :disabled="exec.seqBusy || exec.running || !exec.seqItems.length"
                @click="exec.saveSeq()">
          <Icon name="download" :size="13" /> {{ exec.seqBusy ? "处理中…" : "保存到本地文件" }}
        </button>
        <select v-model="seqLoadFile" :disabled="exec.seqBusy || exec.running" @change="onLoadSeq">
          <option value="">载入已保存的序列…</option>
          <option v-for="f in seqFiles" :key="'sf' + f.name" :value="f.name">{{ f.name }}</option>
        </select>
        <button :disabled="exec.seqBusy || exec.running || !exec.seqItems.length"
                @click="exec.clearSeq()">
          <Icon name="close" :size="13" /> 清空
        </button>
      </div>

      <div class="btns" style="margin-top:8px">
        <button class="primary" :disabled="runSeqDisabled" @click="exec.runSeq(false)">
          <Icon name="play" :size="13" /> 执行
        </button>
        <button :disabled="!exec.running || exec.runPaused" @click="exec.pauseSeq()">
          <Icon name="pause" :size="13" /> 暂停
        </button>
        <button :disabled="!exec.running || !exec.runPaused" @click="exec.resumeSeq()">
          <Icon name="play" :size="13" /> 继续
        </button>
        <button class="rec-on" :disabled="!exec.running && !exec.seqBusy" @click="exec.stopSeq()">
          <Icon name="stop" :size="13" /> 停止执行
        </button>
      </div>

      <div class="prog-bar" style="margin-top:8px">
        <i :style="{ width: exec.runPct + '%' }"></i>
      </div>
      <p class="small muted" style="margin-top:6px">
        进度 {{ exec.runStep }}/{{ exec.runTotal || exec.seqItems.length }}
        · {{ exec.running ? (exec.runPaused ? "已暂停" : "运行中") : "空闲" }}
        · 「暂停」在<b>当前步结束后</b>生效（等待步立即冻结计时）；要让机器人立刻停，请用<b>急停</b>。
      </p>
      <p v-if="exec.seqErr" class="pf-err">
        <Icon name="alert" :size="13" /> {{ exec.seqErr }}
      </p>
      <p v-else-if="exec.seqMsg" class="small">{{ exec.seqMsg }}</p>
    </div>

    <!-- 示教器程序（控制器可执行的 .XPL） -->
    <div class="card">
      <h3><Icon name="terminal" :size="15" /> 示教器程序
        <span class="h3-sub">{{ exec.teachFiles.length }} 个</span>
        <span class="h3-refresh" :class="{ spin: refetching }" title="重新扫描 programs 目录"
              @click="refetchFiles">
          <Icon name="refresh" :size="13" />
        </span>
      </h3>
      <p class="small muted">示教器导出的 .XPL 程序，可在控制器上执行。</p>
      <div class="pt-list">
        <div v-for="f in exec.teachFiles" :key="'teach:' + f.name" class="pt-item"
             :class="{ on: exec.fileName === f.name }">
          <div class="pt-main">
            <span class="pt-num">{{ f.no != null ? f.no : "—" }}</span>
            <span class="pt-tag teach">示教器</span>
            <span class="pt-name">{{ f.name }}</span>
          </div>
          <div class="pt-ops">
            <button :disabled="!canTest" @click="exec.testRun(f.name)">
              <Icon name="flask" :size="13" /> {{ testBusy(f.name) ? "校验中…" : "测试跑" }}
            </button>
          </div>
        </div>
        <p v-if="!exec.teachFiles.length" class="small">
          暂无示教器程序。把示教器导出的 .XPL 放进 programs 目录后点标题右侧刷新。
        </p>
      </div>
    </div>

    <!-- 本地程序（数据库点位序列 + JSON + 预设点位） -->
    <div class="card">
      <h3><Icon name="layers" :size="15" /> 本地程序
        <span class="h3-sub">{{ localItems.length }} 个</span>
        <span class="h3-refresh" :class="{ spin: refetching }" title="重新扫描 programs 目录"
              @click="refetchFiles">
          <Icon name="refresh" :size="13" />
        </span>
      </h3>
      <p class="small muted">本机 programs 目录里的本地文件与数据库点位序列。</p>
      <div class="pt-list">
        <div v-for="it in localItems" :key="'local:' + it.kind + ':' + it.name" class="pt-item"
             :class="{ on: (it.kind === 'program' && selProgram && selProgram.id === it.id) || exec.fileName === it.name }">
          <div class="pt-main" :style="it.kind === 'program' ? 'cursor:pointer' : ''"
               @click="it.kind === 'program' && pickProgram(it.obj)">
            <span class="pt-tag" :class="it.tag === '点位序列' ? 'seq' : (it.tag === '点位' ? '' : '')">{{ it.tag }}</span>
            <span class="pt-name">{{ it.name }}</span>
            <span v-if="it.steps" class="pt-tag">{{ it.steps }} 步</span>
          </div>
          <div class="pt-ops">
            <button :disabled="!canTest" @click="exec.testRun(it.name)">
              <Icon name="flask" :size="13" /> {{ testBusy(it.name) ? "校验中…" : "测试跑" }}
            </button>
            <button v-if="it.kind === 'program'" :disabled="exec.running" @click="pickProgram(it.obj)">
              <Icon name="chevronDown" :size="13" /> 步骤
            </button>
            <button v-if="it.kind === 'program'" @click="exportProgram(it.obj)">
              <Icon name="download" :size="13" /> 导出
            </button>
          </div>
        </div>
        <p v-if="!localItems.length" class="small">
          暂无本地程序。可在点位执行页建立点位序列，或在 programs 目录放 .json；有控制令牌时自动刷新。
        </p>
      </div>

      <!-- 步骤表：运行时高亮当前步（仅点位序列程序展开） -->
      <div v-if="selProgram" class="run-steps" style="margin-top:10px">
        <div class="sp-sec-h" style="cursor:default">
          <span>步骤明细 · {{ selProgram.name }}</span>
        </div>
        <div class="pt-list">
          <div v-for="s in steps" :key="s.index" class="pt-item" :class="stepCls(s.index)">
            <div class="pt-main">
              <span class="pt-tag">#{{ s.index }}</span>
              <span class="pt-name">{{ s.name }}</span>
              <span class="pt-joints">速度 {{ s.speed }}%</span>
              <span v-if="s.dwell" class="pt-joints">停留 {{ s.dwell }}ms</span>
              <span v-if="s.missing" class="pt-tag warn">点位缺失</span>
            </div>
          </div>
          <p v-if="!steps.length" class="small">该程序没有任何步骤。</p>
        </div>
      </div>
    </div>

    <!-- 手动执行：填写文件名 → 测试（只校验不下发）/ 直接运行 -->
    <div class="card">
      <h3><Icon name="file" :size="15" /> 手动执行
        <!-- ★ Pr2：副标题只报中性信息「填文件名」；测试 vs 直接运行的区别只在下方一行讲一次 -->
        <span class="h3-sub">填文件名</span>
      </h3>
      <div class="pf-row">
        <label>文件名</label>
        <input list="efort-files" v-model="exec.fileName" type="text"
               placeholder="填写文件名，或从下拉选择…" />
        <datalist id="efort-files">
          <option v-for="f in exec.files" :key="f.kind + ':' + f.name" :value="f.name">
            {{ f.file_ext === 'xpl' ? '示教器' : f.kind === 'program' ? '程序' : f.kind === 'point' ? '点位' : '文件'
            }}{{ f.no != null ? ' · #' + f.no : '' }}{{ f.steps ? ' · ' + f.steps + ' 步' : '' }}
          </option>
        </datalist>
        <button :disabled="exec.fileBusy" title="重新扫描可执行文件清单"
                @click="exec.loadFiles()">
          <Icon name="refresh" :size="12" />
        </button>
      </div>
      <div class="btns">
        <button :disabled="!canTest || !exec.fileName.trim()" @click="runTest">
          <Icon name="flask" :size="13" /> {{ exec.fileBusy ? "测试中…" : "测试" }}
        </button>
        <button class="primary" :disabled="exec.fileBusy || !exec.canExec || !exec.fileName.trim()"
                @click="runNow">
          <Icon name="play" :size="13" /> 直接运行
        </button>
      </div>
      <p class="small muted" style="margin-top:6px">
        「测试」只解析校验（IK / 限位 / 可达），<b>绝不下发</b>；「直接运行」才会真下发。
      </p>
      <div v-if="exec.fileResult" class="teach-res"
           :class="exec.fileResult.ok ? 'ok' : 'err'">
        <Icon :name="exec.fileResult.ok ? 'check' : 'alert'" :size="13" />
        <span v-if="exec.fileResult.ok">
          {{ exec.fileResult.dry_run ? "试运行通过" : "执行完成" }}：{{ exec.fileResult.name }}
          （{{ exec.fileResult.passed }}/{{ exec.fileResult.count }} 步，
          约 {{ ((exec.fileResult.duration_ms || 0) / 1000).toFixed(1) }}s）
        </span>
        <span v-else>
          {{ exec.fileResult.error || exec.fileResult.message || "失败" }}
        </span>
      </div>
      <div v-if="exec.fileResult && exec.fileResult.candidates
                 && exec.fileResult.candidates.length" class="cands">
        <span v-for="c in exec.fileResult.candidates.slice(0, 8)" :key="c.kind + c.name"
              class="cand" @click="exec.fileName = c.name">{{ c.name }}</span>
      </div>
    </div>

    <!-- ★ 运行日志由底部状态区「运行日志」片统一显示，本页不再重复渲染。 -->
  </MonitorLayout>
</template>

<style scoped>
/* 就绪提示盒：一屏一个结论，视觉像"状态横幅"而非表单卡片 */
.ready-box { display: flex; align-items: center; gap: 10px; padding: 10px;
  border-radius: 10px; border: 1px solid var(--warn-line);
  background: linear-gradient(160deg, var(--warn-soft), transparent 70%); }
.ready-box.ok { border-color: var(--ok-line);
  background: linear-gradient(160deg, var(--ok-soft), transparent 70%); }
.rb-dot { flex: none; width: 9px; height: 9px; border-radius: 50%;
  background: var(--warn); box-shadow: 0 0 8px var(--warn); }
.ready-box.ok .rb-dot { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.rb-body { flex: 1; min-width: 0; }
.rb-title { font-size: 14px; font-weight: 700; color: var(--txt); }
.rb-sub { font-size: 11px; color: var(--muted); margin-top: 2px; line-height: 1.5; }
.rb-ico { flex: none; color: var(--warn); }
.ready-box.ok .rb-ico { color: var(--ok); }
/* 序列编辑：等待步的秒数输入 */
.seq-sec { width: 72px; padding: 2px 6px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel); color: var(--txt); }
/* ★ 六个添加按钮固定 3 列 × 2 行：一行挤不下会换行错位，看起来像少了按钮 */
.seq-add { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px; margin-top: 8px; }
.seq-add > button { justify-content: center; min-width: 0; white-space: nowrap; }
/* ★ 原 .clear-err 样式已随本页那颗"清控制器报错"按钮移除（Pr3：统一走底栏「真机链路」面板）——
   顺带消除了这里 var(--warn, #d48806) 的硬编码兜底 hex（Pr6）。 */
</style>
