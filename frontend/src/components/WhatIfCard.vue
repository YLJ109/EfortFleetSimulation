<script setup>
// =====================================================================
// ★ E3 反事实试跑（What-if 沙盒）—— 只读实验台卡：**不碰真机**，回答「这样改值不值得上真机」。
//
// 与同类只读卡（TwinResidualCard / CellEvidenceCard / HoldEvidence）同一套纪律：
//   · **判据只有后端那一份**（`services/whatif.py` + `tests/test_whatif.py` 的静态钉），
//     本卡只渲染：不复算分位数、不把"没数据"改成"通过"（§5.5 铁律②④）；
//   · **三态不塌成布尔**（铁律③）：`ok / partial / failed / no_data` 四种各有明显不同的样式，
//     全部用 `=== 「...」` 严格判等；`no_data` 是灰虚线，**绝不渲染成绿色**；
//   · **样本数 = 后端实际跑完的那个**（`res.n`），被墙钟预算截断时原样贴出后端那句说明；
//   · **节拍那一档**没有实测样本 ⇒ 原样显示后端的「无实测样本·不给节拍估计」；
//     本卡**不生成**任何"要多久 / 多久完成"的话（上位机只写目标+触发位，拿不到速度曲线）；
//   · **每个数字旁边都有采样时刻**（`ranAt`）；读不到就写「—（没测）」，绝不合成 0；
//   · **本卡没有任何**「应用 / 下发 / 去执行」按钮 —— 方案 §E3 写的是"回答值不值得上真机"，
//     决定权在人。发起口只有 `stores/whatif.js` 的 `run()`（唯一一处 POST）。
// =====================================================================
import { computed, onMounted, ref } from "vue";
import Icon from "./Icon.vue";
import { apiUrl } from "../config.js";
import { ORDERS, FACES, useWhatIfStore } from "../stores/whatif.js";

const wi = useWhatIfStore();

/** 参与点位候选：只读 GET `/api/points`（与点位列表同一个只读口，不需要令牌、不产生动作）。 */
const pointNames = ref([]);
const pointsErr = ref("");
async function loadPointNames() {
  try {
    const r = await fetch(apiUrl("/points"));
    if (!r.ok) { pointsErr.value = `点位列表读取失败（HTTP ${r.status}）`; return; }
    const d = await r.json();
    pointNames.value = (Array.isArray(d) ? d : [])
      .map((p) => (p && p.name ? String(p.name) : "")).filter((x) => x);
    pointsErr.value = "";
  } catch (e) {
    pointsErr.value = `点位列表读取失败：${e.message}`;
  }
}
onMounted(loadPointNames);

/** ★ 数字 → 文案：**读不到就是「—（没测）」**，绝不拿 0 / "—" 冒充一个测出来的值。 */
function num(v, digits = 1) {
  if (v === null || v === undefined || v === "") return "—（没测）";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : "—（没测）";
}
/** 采样时刻（本地时钟）：每出现一个数字就把它旁边的那个时刻贴出来。 */
const atText = computed(() => {
  const t = wi.ranAt;
  if (t === null || t === undefined || !Number.isFinite(Number(t))) return "—（没测）";
  const d = new Date(Number(t));
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
});

/** 三态徽标：**显式判等**（`x ? a : b` 会把"没数据"染成"通过"，那是假显示）。 */
const st = computed(() => wi.status);
const isOk = computed(() => st.value === "ok");
const isPartial = computed(() => st.value === "partial");
const isFailed = computed(() => st.value === "failed");
const isNoData = computed(() => st.value === "no_data");

/** 节拍档三态：true / false / 拿不到 —— 三种写法必须不一样。 */
const cycle = computed(() => wi.cycle);
const cycleHas = computed(() => !!cycle.value && cycle.value.available === true);
const cycleNone = computed(() => !!cycle.value && cycle.value.available === false);

/** 间隙直方图：后端已经分好箱，前端只画不重算（条形高度按最大计数归一）。 */
const bars = computed(() => {
  const list = ((wi.dist && wi.dist.bins) || []).slice();
  const max = list.reduce((a, b) => Math.max(a, Number(b.count) || 0), 0);
  return list.map((b) => ({
    lo: b.lo, hi: b.hi, count: b.count,
    h: max > 0 ? Math.max(4, (Number(b.count) || 0) / max * 100) : 0,
  }));
});
/** 状态词（与后端 STATE_LABEL 同一套字面，认不出就写「判不了」，不塌成"安全"）。 */
const worstStateText = computed(() => {
  const s = String((wi.dist && wi.dist.worst_state) || "");
  const ZH = { safe: "全程在警戒线外", warn: "警戒", danger: "危险",
               hit: "触界（包络进入围栏）", unknown: "判不了" };
  return ZH[s] || "判不了（后端给的状态认不出来）";
});
const worstStateClass = computed(() => {
  const s = String((wi.dist && wi.dist.worst_state) || "");
  if (s === "safe") return "w-ok";
  if (s === "warn") return "w-warn";
  if (s === "danger" || s === "hit") return "w-bad";
  return "w-unknown";
});
function assumedText(s) {
  if (s && s.assumed === true) return "假设值";
  if (s && s.assumed === false) return "实测口径";
  return "未标注";                       // 三态：缺字段就照实说没标注
}
function assumedClass(s) {
  if (s && s.assumed === true) return "w-warn";
  if (s && s.assumed === false) return "w-ok";
  return "w-unknown";
}
</script>

<template>
  <div class="card wi">
    <h3>
      <Icon name="flask" :size="14" /> 反事实试跑
      <span class="h3-sub">离线实验台 · 不碰真机 · 不执行</span>
      <span class="tag" :class="wi.statusClass">{{ wi.statusText }}</span>
    </h3>

    <!-- ① 一句话结论（后端原文）+ 采样时刻 -->
    <p class="wi-head" :class="wi.statusClass">
      <Icon name="info" :size="12" />
      <b>结论</b>
      <span v-if="wi.conclusion">{{ wi.conclusion }}</span>
      <span v-else class="muted">—（没测）：还没跑过一轮，或后端没给结论</span>
      <span class="small muted">· 采样 {{ atText }}</span>
    </p>

    <!-- ② 场景表单：只有**描述**，没有任何动作字段 -->
    <div class="wi-form">
      <div class="wf-row">
        <label title="托盘偏移（mm，加在机器人基座系上；托盘自身坐标系夹角未实测）">偏移 X</label>
        <input v-model.number="wi.form.dx" type="number" step="1" />
        <label>Y</label>
        <input v-model.number="wi.form.dy" type="number" step="1" />
        <label>Z</label>
        <input v-model.number="wi.form.dz" type="number" step="1" />
        <label title="面策略只重排**评估用的姿态**；18 个原料位照旧严禁翻面">面</label>
        <select v-model="wi.form.face">
          <option v-for="f in FACES" :key="f.key" :value="f.key">{{ f.label }}</option>
        </select>
        <label title="取放次序只重排评估路径：不落库、不执行">次序</label>
        <select v-model="wi.form.order">
          <option v-for="o in ORDERS" :key="o.key" :value="o.key">{{ o.label }}</option>
        </select>
      </div>
      <div class="wf-row">
        <label title="留空 = 用后端缺省评估路径（一件：取 → 检测 → 放 → 回起点）">参与点位</label>
        <select v-model="wi.form.points" multiple size="4" class="wf-multi">
          <option v-for="n in pointNames" :key="n" :value="n">{{ n }}</option>
        </select>
        <div class="wf-right">
          <label title="蒙特卡洛样本数；留空 = 后端缺省（缺省值的依据是那条离线计时实测）">样本数 n</label>
          <input v-model.number="wi.form.n" type="number" min="1" max="300" placeholder="留空=缺省" />
          <label title="只是评估用的档位标签：不写速度寄存器、不改右上角滑块口径">速度档 %</label>
          <input v-model.number="wi.form.gear" type="number" min="0" max="100" placeholder="留空=不设" />
        </div>
      </div>
      <p v-if="pointsErr" class="wi-err">{{ pointsErr }}<span class="muted"> —— 拿不到点位名就只能用缺省路径。</span></p>
      <div class="wf-act">
        <button class="primary" :disabled="wi.busy"
                title="离线跑一轮参数扫描 + 蒙特卡洛（只读：不写寄存器、不碰触发位与气路、不改点位库）"
                @click="wi.run()">
          <Icon name="flask" :size="14" /> {{ wi.busy ? "试跑中…" : "离线试跑" }}
        </button>
        <button :disabled="wi.busy" @click="wi.resetForm()">
          <Icon name="refresh" :size="13" /> 复位参数
        </button>
        <span class="small muted">重跑一次 = 再发一次只读请求；本卡<b>没有</b>任何「应用 / 去执行」的出口</span>
      </div>
    </div>

    <p v-if="wi.err" class="wi-err">{{ wi.err }}
      <span class="muted"> —— 请求失败不等于「没问题」，也不等于「有问题」。</span></p>

    <!-- ③ 分布：P5 / P50 / P95 + 直方图 + **实际跑完的样本数** -->
    <div v-if="wi.hasResult" class="wi-body">
      <div class="wi-block">
        <div class="wb-title"><Icon name="activity" :size="12" /> 最小间隙分布（mm · 保守包络）
          <span class="small muted">采样 {{ atText }}</span></div>
        <template v-if="wi.dist && wi.dist.p5 !== null && wi.dist.p5 !== undefined">
          <div class="wb-pcts">
            <span>P5 <b>{{ num(wi.dist.p5) }}</b></span>
            <span>P50 <b>{{ num(wi.dist.p50) }}</b></span>
            <span>P95 <b>{{ num(wi.dist.p95) }}</b></span>
            <span class="tag" :class="worstStateClass">{{ worstStateText }}</span>
          </div>
          <div class="wb-hist" :title="`每箱计数来自后端 bins（前端不重算）· 采样 ${atText}`">
            <div v-for="(b, i) in bars" :key="i" class="hb"
                 :style="{ height: b.h + '%' }"
                 :title="`${b.lo}~${b.hi}mm · ${b.count} 个样本 · 采样 ${atText}`"></div>
          </div>
          <p v-if="!bars.length" class="small muted">后端没给分箱（bins 是空表）—— 不画一根都不画。</p>
        </template>
        <p v-else class="small wb-nodata">
          —（没测）：这一档没有算出来。<b>没有数据 ≠ 安全</b>，不代表这个改动可以做。
        </p>
        <p class="small">
          样本数：<b class="wi-n">{{ wi.nRan === null ? "—（没测）" : wi.nRan }}</b>
          <span class="muted">（本次请求 {{ wi.nRequested === null ? "—（没测）" : wi.nRequested }}）</span>
          <span class="muted">· 每拍 {{ num(wi.sampleMs, 3) }}ms · 整轮 {{ num(wi.runMs, 1) }}ms</span>
        </p>
        <p v-if="wi.truncated" class="wi-warn-line">
          <Icon name="alert" :size="12" /> 被墙钟预算截断：{{ wi.truncNote || `只跑完 ${wi.nRan} 轮（请求 ${wi.nRequested}）` }}
        </p>
      </div>

      <!-- ④ 节拍档：只代**已发生的实测段** -->
      <div class="wi-block">
        <div class="wb-title"><Icon name="clock" :size="12" /> 节拍（只代实测段）
          <span class="small muted">采样 {{ atText }}</span></div>
        <template v-if="cycleHas">
          <div class="wb-pcts">
            <span>实测 P50 <b>{{ num(cycle.p50_ms, 0) }}ms</b></span>
            <span>实测 P90 <b>{{ num(cycle.p90_ms, 0) }}ms</b></span>
            <span class="muted">样本 {{ cycle.n_samples }} 段 · 档 {{ (cycle.speed_pct || []).join("/") || "—（没测）" }}</span>
          </div>
          <p class="small muted wi-basis">{{ cycle.basis }}</p>
        </template>
        <p v-else-if="cycleNone" class="wi-nodata">
          <b>无实测样本·不给节拍估计</b>
          <span class="muted">（{{ cycle.n_samples === 0 ? "0" : num(cycle.n_samples, 0) }} 段实测，少于所需
            {{ cycle.need_samples === null || cycle.need_samples === undefined ? "—（没测）" : cycle.need_samples }} 段）</span>
        </p>
        <p v-else class="wi-nodata">—（没测）：后端没回节拍这一档。</p>
      </div>

      <!-- ⑤ 最可疑的那一段（几何风险比例，不是控制器实测到位率） -->
      <div v-if="wi.worst && wi.worst.point" class="wi-block">
        <div class="wb-title"><Icon name="alert" :size="12" /> 最需要看的那一段
          <span class="small muted">采样 {{ atText }}</span></div>
        <p class="small"><b>{{ wi.worst.segment }}</b>
          · P5 间隙 {{ num(wi.worst.min_mm_p5) }}mm
          · 触界样本占比 {{ num(wi.worst.p_not_arrived * 100, 1) }}%</p>
        <p class="small muted wi-basis">{{ wi.worst.basis }}</p>
      </div>

      <!-- ⑥ 逐段（点名 + 数字 + 时刻成对出现） -->
      <div v-if="wi.segments.length" class="wi-block">
        <div class="wb-title"><Icon name="layers" :size="12" /> 逐段
          <span class="small muted">采样 {{ atText }}</span></div>
        <table class="wi-table">
          <thead>
            <tr><th>#</th><th>段</th><th>名义mm</th><th>P5mm</th><th>P50mm</th><th>触界%</th><th>采样</th></tr>
          </thead>
          <tbody>
            <tr v-for="s in wi.segments" :key="s.i">
              <td>{{ s.i }}</td>
              <td class="src">{{ s.from }} → {{ s.to }}</td>
              <td>{{ num(s.nominal_mm) }}</td>
              <td>{{ num(s.p5_mm) }}</td>
              <td>{{ num(s.p50_mm) }}</td>
              <td>{{ num(Number(s.p_not_arrived) * 100, 1) }}</td>
              <td class="muted">{{ atText }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ⑦ 抖动来源：逐项列明，假设值必须带依据 -->
      <div v-if="wi.sources.length" class="wi-block">
        <div class="wb-title"><Icon name="shield" :size="12" /> 抖动来源（实际用到的那一份）
          <span class="small muted">采样 {{ atText }}</span></div>
        <table class="wi-table">
          <thead><tr><th>来源</th><th>σ</th><th>单位</th><th>性质</th><th>依据</th></tr></thead>
          <tbody>
            <tr v-for="s in wi.sources" :key="s.key">
              <td class="src">{{ s.name }}</td>
              <td>{{ num(s.sigma, 3) }}</td>
              <td>{{ s.unit }}</td>
              <td><span class="tag" :class="assumedClass(s)">{{ assumedText(s) }}</span></td>
              <td class="src basis">{{ s.basis }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ⑧ 如实的缺失项（每一条都点名的那个对象） -->
      <div v-if="wi.failures.length" class="wi-block">
        <div class="wb-title"><Icon name="alert" :size="12" /> 没算到 / 算不全的（{{ wi.failures.length }} 条）
          <span class="small muted">采样 {{ atText }}</span></div>
        <ul class="wi-list">
          <li v-for="(f, i) in wi.failures" :key="i">{{ f }}</li>
        </ul>
      </div>

      <!-- ★ 三态各自的明显不同样式（灰虚线 = 没结论；绝不给 no_data 上绿章） -->
      <p v-if="isNoData" class="wi-state st-nodata">
        <b>没有可评估的数据</b> —— 这<b>不代表安全</b>：点位库为空 / 未示教 / 姿态不可用，
        什么都还没算出来。补齐点位再跑一次。
      </p>
      <p v-else-if="isFailed" class="wi-state st-failed">
        <b>本轮没算出来</b>（评估本身跑不起来，见上面「没算到 / 算不全的」）—— 同样不代表安全。
      </p>
      <p v-else-if="isPartial" class="wi-state st-partial">
        <b>算完了，但有缺失项</b>：下面的清单逐条点名了「没算它」的对象。
      </p>
      <p v-else-if="isOk" class="wi-state st-ok">
        <b>算完了，本轮没有缺失项</b>（仍然只是离线统计，不是认证、不是许可）。
      </p>
      <p v-else class="wi-state st-unknown">
        <b>状态认不出来</b>（后端给的 status 不在这四种里）—— 按「判不了」处理，不假装通过。
      </p>
    </div>
    <p v-else-if="!wi.err" class="small muted wi-none">
      还没跑过一轮。填好场景点「离线试跑」——它只发一次只读请求，<b>不会</b>让机器人动一下。
    </p>

    <!-- ⑨ 底部一行：口径 + 未建模清单（原样列出） -->
    <p class="small note">
      <Icon name="info" :size="12" /> <b>离线统计估计 · 非认证</b>；未建模：
      <template v-if="wi.notModelled.length">{{ wi.notModelled.join("；") }}。</template>
      <template v-else>—（没测）：后端没给未建模清单，别把空白当成"都建模了"。</template>
      <span v-if="wi.note" class="muted">{{ wi.note }}</span>
    </p>
    <p class="small note">采样时刻 {{ atText }} · 水位 {{ wi.seq }}（只增不减）·
      决定权在人：本页不应用、不下发、不代替真机验证。</p>
  </div>
</template>

<style scoped>
.wi .wi-head {
  margin: 6px 0 10px; font-size: 12px; line-height: 1.5;
  border: 1px solid var(--line, #2a3245); border-radius: 5px; padding: 5px 8px;
}
.wi .wi-head b { margin-right: 4px; }
.wi-form { display: flex; flex-direction: column; gap: 6px; }
.wf-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.wf-row label { font-size: 12px; color: var(--text-2, #a9b4c9); }
.wf-row input, .wf-row select {
  background: var(--bg-2, #151a26); color: var(--text-1, #e6ebf5);
  border: 1px solid var(--line, #2a3245); border-radius: 4px; padding: 2px 5px; font-size: 12px;
}
.wf-row input[type="number"] { width: 62px; }
.wf-multi { min-width: 132px; max-width: 220px; }
.wf-right { display: flex; align-items: center; gap: 6px; margin-left: auto; flex-wrap: wrap; }
.wf-act { display: flex; align-items: center; gap: 8px; margin-top: 4px; flex-wrap: wrap; }
.wf-act .small { flex: 1 1 100%; }
.wi-err { margin: 6px 0 0; font-size: 12px; color: #ff8a8a; }
.wi-body { margin-top: 10px; display: flex; flex-direction: column; gap: 10px; }
.wi-block { border: 1px solid var(--line, #21283a); border-radius: 6px; padding: 6px 8px; }
.wb-title { display: flex; align-items: baseline; gap: 6px; font-size: 12px; color: var(--text-2, #a9b4c9); flex-wrap: wrap; }
.wb-pcts { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; margin-top: 4px; }
.wb-pcts b { font-weight: 600; }
.wb-hist { display: flex; align-items: flex-end; gap: 2px; height: 34px; margin-top: 6px;
  border-bottom: 1px solid var(--line, #2a3245); }
.wb-hist .hb { flex: 1 1 0; min-width: 6px; background: #3f6ea8; border-radius: 2px 2px 0 0; }
.wb-nodata { margin: 4px 0 0; font-size: 12px; color: var(--text-3, #7e8ba3); }
.wi-n { font-weight: 600; }
.wi-warn-line { margin: 4px 0 0; font-size: 12px; color: #ffb86b; }
.wi-basis { margin: 4px 0 0; line-height: 1.45; }
.wi-nodata { margin: 4px 0 0; font-size: 12px; color: var(--text-3, #7e8ba3); border-style: dashed; }
.wi-table { width: 100%; border-collapse: collapse; margin-top: 4px; font-size: 12px; }
.wi-table th, .wi-table td { padding: 2px 4px; text-align: right; border-bottom: 1px solid var(--line, #21283a); }
.wi-table th { font-weight: 500; color: var(--text-3, #7e8ba3); }
.wi-table td.src { text-align: left; }
.wi-table td.basis { font-size: 11px; line-height: 1.4; }
.wi-list { margin: 4px 0 0; padding-left: 18px; font-size: 12px; }
.wi-list li { line-height: 1.5; }
.wi-state { margin: 4px 0 0; font-size: 12px; padding: 5px 8px; border-radius: 5px; border: 1px solid transparent; }
.st-ok { color: #6ee7a0; background: rgba(110, 231, 160, .1); }
.st-partial { color: #ffd479; background: rgba(255, 212, 121, .1); }
.st-failed { color: #ff8a8a; background: rgba(255, 138, 138, .12); }
/* no_data / unknown：灰虚线 —— 看着就"没结论"，绝不给绿章（§5.5 铁律③） */
.st-nodata, .st-unknown {
  color: var(--text-3, #7e8ba3); background: none;
  border-style: dashed; border-color: var(--line, #2a3245);
}
.wi-none { margin-top: 8px; }
.tag { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 11px; border: 1px solid transparent; }
.w-ok { color: #6ee7a0; background: rgba(110, 231, 160, .1); }
.w-warn { color: #ffd479; background: rgba(255, 212, 121, .1); }
.w-bad { color: #ff8a8a; background: rgba(255, 138, 138, .12); }
.w-unknown { color: var(--text-3, #7e8ba3); border-style: dashed; border-color: var(--line, #2a3245); }
.note { margin-top: 8px; line-height: 1.5; color: var(--text-3, #7e8ba3); }
.small { font-size: 12px; }
.muted { color: var(--text-3, #7e8ba3); }
</style>
