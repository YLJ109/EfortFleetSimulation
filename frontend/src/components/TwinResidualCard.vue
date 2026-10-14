<script setup>
// =====================================================================
// 到位自证（E2 · 只读观测卡）—— 每一段下发"到底走没走到"，用实测残差说话。
//
// 为什么值得有这张卡：控制器收了目标 ≠ 机器人动了（常驻程序 210 没在 WAIT、
// 档位不对、被外部暂停时，接口都可能"看起来成功"）。这里把**段级**收敛过程摊开：
// 动没动 / 峰值残差 / 收敛耗时 / 到位后回不回弹，全部来自逐拍实测回读。
//
// ★ 硬边界（与后端 services/twin_residual.py 同口径，别在这儿加动作）：
//   · 只观测、不参与控制 —— 超差只发一条 warn 事件并在本卡提示，
//     **不会自动暂停、不会自动重试、不会急停**（刹车必须人点）；
//   · 残差是**关节角差（°）**不是 mm，所以写"到位自证"不写"轨迹跟踪精度"
//     （执行者是控制器里的常驻程序，PC 拿不到速度曲线 → 没有逐拍期望轨迹）；
//   · "带" = 到位容差 + 一拍采样延迟的不确定度：采集只有 10Hz，落在带内不算超差
//     —— 不把上位机自己的采样延迟说成"机器不准"；
//   · 一帧实测都没拿到 → 显示「无实测数据」，**不显示"到位"**。
// 判据只有后端那一份：实时值走 WS 位姿帧（robot.residual），
// 历史与口径文案走 GET /api/twin/residual（同屏同源，前端不复算一遍）。
// =====================================================================
import { computed, onMounted, ref } from "vue";
import Icon from "./Icon.vue";
import { useRobotStore } from "../stores/robot.js";
import { apiUrl } from "../config.js";

const robot = useRobotStore();

const meta = ref(null);        // GET /api/twin/residual：容差/采样率/说明/最近段历史
const histErr = ref("");
const busy = ref(false);

async function loadHistory() {
  busy.value = true;
  histErr.value = "";
  try {
    const res = await fetch(apiUrl("/twin/residual"));
    if (!res.ok) { histErr.value = `历史读取失败（HTTP ${res.status}）`; return; }
    meta.value = await res.json();
  } catch (e) {
    histErr.value = `历史读取失败：${e.message}`;
  } finally {
    busy.value = false;
  }
}
onMounted(loadHistory);

const live = computed(() => robot.residual);
const recent = computed(() => (meta.value && meta.value.recent) || []);

// 判据文案：**未知不塌成"正常"**（认不出的判据一律灰虚线）
const VERDICT = {
  pending: ["待观测", "info"],
  moving: ["收敛中", "info"],
  arrived: ["已到位", "ok"],
  slow: ["到位（慢）", "warn"],
  overshoot: ["到位后回弹", "bad"],
  stuck: ["没动", "bad"],
  not_reached: ["未到位", "bad"],
  no_data: ["无实测数据", "unknown"],
  aborted: ["已中止", "unknown"],
};
function verdictText(v) {
  const hit = VERDICT[String(v || "")];
  return hit ? hit[0] : "未知";
}
function verdictCls(v) {
  const hit = VERDICT[String(v || "")];
  return hit ? `v-${hit[1]}` : "v-unknown";
}

function num(v, digits = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
}

// 残差条的刻度：带至少要看得见，峰值不能被截断
const scaleMax = computed(() => {
  const r = live.value;
  if (!r) return 1;
  const band = Number(r.band_deg) || 0;
  const peak = Number(r.peak_deg) || 0;
  return Math.max(band * 2, peak * 1.15, 1);
});
function barPct(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "0%";
  return `${Math.min(100, Math.max(0, (n / scaleMax.value) * 100)).toFixed(1)}%`;
}
const bandPct = computed(() => barPct(live.value ? live.value.band_deg : 0));

const tolHint = computed(() => {
  const m = meta.value;
  if (!m) return "口径读取中…";
  return `容差 ${num(m.tol_deg)}° · 采样 ${num(m.sample_hz, 1)}Hz（带 = 容差 + 一拍延迟的不确定度）`;
});

const sourceText = computed(() => {
  const r = live.value;
  if (!r) return "";
  const s = String(r.source || "").trim();
  return s || "未标注来源";
});
</script>

<template>
  <div class="card tr">
    <h3>
      <Icon name="activity" :size="14" /> 到位自证
      <span class="h3-sub" :class="live ? verdictCls(live.verdict) : ''">
        {{ live ? verdictText(live.verdict) : "无在飞段" }}
      </span>
    </h3>

    <!-- ① 在飞的这一段（实时，来自 WS 位姿帧） -->
    <div v-if="live" class="tr-live">
      <div class="tr-row">
        <b>{{ sourceText }}</b>
        <span class="small muted">速度 {{ live.speed_pct == null ? "—" : live.speed_pct }}%
          · 采样 {{ live.samples }} 帧</span>
      </div>
      <div class="tr-bar" :title="`残差是关节角差（°），不是 mm`">
        <div class="tr-band" :style="{ left: bandPct }"
             :title="`带 ± ${num(live.band_deg)}°（容差 ${num(live.tol_deg)}° + 采样不确定度 ${num(live.uncertainty_deg)}°）`"></div>
        <i class="fill" :class="verdictCls(live.verdict)" :style="{ width: barPct(live.last_deg) }"></i>
      </div>
      <div class="tr-nums">
        <span>当前 <b>{{ num(live.last_deg) }}°</b></span>
        <span>峰值 <b>{{ num(live.peak_deg) }}°</b></span>
        <span>带 ± <b>{{ num(live.band_deg) }}°</b></span>
        <span v-if="live.settled">收敛 <b>{{ num(live.settle_ms, 0) }}ms</b></span>
        <span v-else class="muted">尚未收敛到带内</span>
      </div>
      <p v-if="live.verdict === 'stuck' || live.verdict === 'not_reached' || live.verdict === 'overshoot'"
         class="tr-warn">
        这一段超差已记入审计（一条 warn 事件）。系统<b>不会</b>自动暂停 / 重试 / 急停 ——
        要不要停由你点。
      </p>
    </div>
    <p v-else class="small muted">
      当前没有在飞的下发段。下发一段（点动 / 到点 / 分拣）后这里会出现逐拍收敛过程。
    </p>

    <!-- ② 最近段（REST，只读） -->
    <div class="tr-hist">
      <div class="tr-hist-head">
        <span class="small">最近 {{ recent.length }} 段</span>
        <button class="mini" :disabled="busy" title="重新读取段历史（只读，不下发任何动作）"
                @click="loadHistory">
          <Icon name="refresh" :size="13" /> {{ busy ? "读取中" : "重查" }}
        </button>
      </div>
      <p v-if="histErr" class="tr-err">{{ histErr }}</p>
      <table v-else-if="recent.length" class="tr-table">
        <thead>
          <tr><th>来源</th><th>判定</th><th>峰值°</th><th>耗时ms</th><th>帧</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in recent" :key="row.id">
            <td class="src">{{ row.source || "未标注来源" }}</td>
            <td><span class="tag" :class="verdictCls(row.verdict)">{{ verdictText(row.verdict) }}</span></td>
            <td>{{ num(row.peak_deg) }}</td>
            <td>{{ row.settle_ms == null ? "—" : num(row.settle_ms, 0) }}</td>
            <td>{{ row.samples }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="small muted">还没有已收尾的下发段（本卡不会自己编故事）。</p>
    </div>

    <p class="small muted">{{ tolHint }}</p>
    <p v-if="meta && meta.note" class="small note">{{ meta.note }}</p>
    <p v-else class="small note">口径未加载：点「重查」或等后端 /api/twin/residual 可用。</p>
  </div>
</template>

<style scoped>
.tr .tr-row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.tr .tr-bar {
  position: relative; height: 8px; margin: 8px 0 6px;
  background: var(--bg-3, #1c2230); border: 1px solid var(--line, #2a3245); border-radius: 4px;
}
.tr .tr-bar i.fill { display: block; height: 100%; transition: width .12s linear; }
.tr .tr-band {
  position: absolute; top: -2px; bottom: -2px; width: 1px;
  background: var(--line-strong, #5a6b8c);
}
.tr .tr-nums { display: flex; flex-wrap: wrap; gap: 10px; font-size: 12px; }
.tr .tr-nums b { font-weight: 600; }
.tr-warn { margin: 6px 0 0; font-size: 12px; color: #ff9c6b; }
.tr-hist { margin-top: 10px; }
.tr-hist-head { display: flex; justify-content: space-between; align-items: center; }
button.mini {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; font-size: 12px; background: none;
  border: 1px solid var(--line, #2a3245); border-radius: 4px; color: var(--text-2, #a9b4c9);
  cursor: pointer;
}
button.mini:disabled { opacity: .55; cursor: default; }
.tr-table { width: 100%; border-collapse: collapse; margin-top: 6px; font-size: 12px; }
.tr-table th, .tr-table td { padding: 2px 4px; text-align: right; border-bottom: 1px solid var(--line, #21283a); }
.tr-table th { font-weight: 500; color: var(--text-3, #7e8ba3); }
.tr-table td.src { text-align: left; max-width: 92px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 11px; border: 1px solid transparent; }
.v-ok { color: #6ee7a0; background: rgba(110, 231, 160, .1); }
.v-warn { color: #ffd479; background: rgba(255, 212, 121, .1); }
.v-bad { color: #ff8a8a; background: rgba(255, 138, 138, .12); }
.v-info { color: #8ec5ff; background: rgba(142, 197, 255, .1); }
/* 未知 / 无数据 / 已中止：虚线灰 —— 看着就"没结论"，不冒充通过 */
.v-unknown { color: var(--text-3, #7e8ba3); border-style: dashed; border-color: var(--line, #2a3245); }
.tr .fill.v-ok { background: #3f9d6a; }
.tr .fill.v-warn { background: #b98a2f; }
.tr .fill.v-bad { background: #b5493f; }
.tr .fill.v-info { background: #3f6ea8; }
.tr .fill.v-unknown { background: #4a5468; }
.tr-err { margin: 6px 0 0; font-size: 12px; color: #ff8a8a; }
.note { margin-top: 6px; line-height: 1.45; }
</style>
