<template>
  <div class="card adv-card">
    <h3><Icon name="activity" :size="15" /> 判色体检（现场样本自学习）
      <span class="h3-sub">{{ ca.statusText }}</span>
      <button class="mini" :disabled="ca.loading" @click="ca.load()">
        <Icon name="refresh" :size="13" /> {{ ca.loading ? "读取中" : "刷新" }}
      </button>
    </h3>

    <!-- ★ 三件必须一起显示的口径（与后端同一份，界面不重算）：
         样本数 / 复核数 / 置信分布 —— "够不够、能不能给方向"全部来自后端。 -->
    <p class="small muted note">{{ noteText }}</p>

    <!-- 现场生效值三态：不知道就写"不知道"，绝不拿出厂默认冒充现场（§5.5 铁律③） -->
    <p class="small thr">
      现场生效阈值来源：<b>{{ thrLabel }}</b>
      <span class="muted">{{ thrDetail }}</span>
    </p>

    <!-- ① 七色样本统计（后端逐色给，含"缺多少条"与"为什么不给建议"） -->
    <div class="tbl">
      <div class="tr th"><span class="c1">色</span><span class="c2">样本</span>
        <span class="c2">已复核</span><span class="c2">误判</span>
        <span class="c2">conf p50</span><span class="c2">fill p50(判对/改判)</span>
        <span class="c3">标定态</span><span class="c4">建议 / 不给建议的原因</span></div>
      <div v-for="c in ca.colors" :key="c.color" class="tr">
        <span class="c1"><i v-if="hexOf(c.color)" class="dot" :style="{ background: hexOf(c.color) }" />{{ c.color }}</span>
        <span class="c2">{{ c.n }}<span v-if="!c.enough" class="muted">（还缺 {{ c.missing }}）</span></span>
        <span class="c2">{{ c.reviewed }}</span>
        <span class="c2">{{ c.misjudged }}</span>
        <span class="c2">{{ num(c.conf_stats && c.conf_stats.p50) }}</span>
        <span class="c2">{{ num(c.fill_ok_p50) }} / {{ num(c.fill_bad_p50) }}</span>
        <span class="c3" :class="{ warn: c.calib_state !== 'calibrated' }">{{ c.calib_label }}</span>
        <span class="c4">
          <span v-if="(c.advice || []).length">
            <span v-for="(a, i) in c.advice" :key="i" class="sug">
              {{ a.key }} {{ fromTo(a) }}<em v-if="!a.actionable" class="muted">（只给方向，未落数值）</em>
            </span>
          </span>
          <span v-else class="muted">{{ (c.no_advice_reason || []).join('；') || '—' }}</span>
        </span>
      </div>
    </div>

    <!-- ② 建议项与提案账目（★ 结构上不存在"已生效"这一列） -->
    <div class="box">
      <div class="box-h">
        建议 {{ ca.items.length }} 项 · 当前提案：
        <b v-if="ca.proposal">#{{ ca.proposal.id }}（{{ ca.proposal.status }}）</b>
        <span v-else class="muted">无</span>
      </div>
      <div v-if="!ca.items.length" class="small muted">
        没有可提案的建议 —— {{ (ca.reasons[0] || '样本不足或证据不指向任何阈值') }}
      </div>
      <div v-else class="tbl">
        <div class="tr th"><span class="c1">阈值项</span><span class="c2">现场值</span>
          <span class="c2">建议值</span><span class="c2">依据 n / 复核 / 误判</span>
          <span class="c4">说明</span></div>
        <div v-for="(it, i) in ca.items" :key="i" class="tr">
          <span class="c1">{{ it.color }} · {{ it.key }}</span>
          <span class="c2">{{ fromText(it) }}</span>
          <span class="c2">{{ num(it.to) }}</span>
          <span class="c2">{{ it.n }} / {{ it.reviewed }} / {{ it.misjudged }}</span>
          <span class="c4 muted">applied 恒为 false：本卡不下发任何阈值</span>
        </div>
      </div>

      <div class="btns">
        <button :disabled="!canWrite || ca.acting || !ca.items.length"
                :title="canWrite ? '存为待确认提案（不改现场阈值）' : writTip"
                @click="doPropose">存为提案（不下发）</button>
        <button :disabled="!canWrite || ca.acting || !ca.proposal"
                :title="canWrite ? '把提案账目退回上一版（现场阈值不变）' : writTip"
                @click="doRollback">回滚提案账目</button>
        <button v-if="!auth.controlActive" @click="auth.requestLogin()">获取控制令牌</button>
      </div>

      <p v-if="ca.actionError" class="small err">{{ ca.actionError }}</p>
      <p v-if="ca.actionMessage" class="small ok">{{ ca.actionMessage }}</p>

      <div v-if="ca.history.length" class="hist">
        <div class="small muted">提案历史（账面，最近 {{ ca.history.length }} 条）</div>
        <div v-for="p in ca.history" :key="p.id" class="hrow">
          <span class="mono">#{{ p.id }}</span><span>{{ p.status }}</span>
          <span class="muted">{{ p.created_at }}</span><span>{{ p.actor }}</span>
          <span class="muted sum">{{ p.summary }}</span>
        </div>
      </div>
    </div>

    <!-- ③ 人工标签回流：判对 / 改判（改判必须给真色，后端会拒空） -->
    <div class="box">
      <div class="box-h">
        待复核样本 {{ pendingCount }} 条（列表最近 {{ ca.sampleItems.length }} 条 ·
        库内共 {{ sampleTotal }} 条）
      </div>
      <p v-if="ca.samplesError" class="small err">样本列表不可用：{{ ca.samplesError }}</p>
      <div v-for="s in ca.sampleItems" :key="s.id" class="srow">
        <span class="mono">#{{ s.id }}</span>
        <span>{{ s.timestamp }}</span>
        <span><i v-if="hexOf(s.color)" class="dot" :style="{ background: hexOf(s.color) }" />结论 {{ s.color || '（无）' }}</span>
        <span>conf {{ num(s.conf) }}</span>
        <span class="muted">{{ s.cell || '格未知' }} · {{ s.source }} · {{ s.stage }}</span>
        <span>{{ outcomeText(s) }}</span>
        <span v-if="s.placeholder" class="muted">（空跑占位色：不参与统计）</span>
        <span class="grow">
          <template v-if="s.outcome === 'unreviewed' && !s.placeholder">
            <button class="mini" :disabled="!canWrite || ca.acting" :title="writTip"
                    @click="ca.review(s.id, 'correct')">判对</button>
            <select v-model="s._true">
              <option value="">改判真色…</option>
              <option v-for="c in ca.colors" :key="c.color" :value="c.color">{{ c.color }}</option>
            </select>
            <button class="mini" :disabled="!canWrite || ca.acting || !s._true" :title="writTip"
                    @click="ca.review(s.id, 'wrong', s._true)">改判</button>
          </template>
        </span>
      </div>
    </div>

    <p class="small muted">
      ★ 本卡**不产生任何动作**：不改阈值、不下发、不动机器人。生效只有一条路 ——
      人先在<b>现场</b>跑 <code>camera/tools/vision_e2e_replay.py</code>（现场真图回放），
      确认三色检出率不掉，再自己到相机阈值面板显式点一次。
    </p>
  </div>
</template>

<script setup>
// ===================================================================
// ★ E4「判色体检」卡（挂在「运维审计」页，与 B1 运行指标同一屏）。
//
//   方案 §E4 的最小可交付切片 = "样本环 + 每色样本量与置信分布"的**显示**，
//   建议 diff 与写回是第二步 —— 所以这一版把三件事分开摆：
//     ① 逐色统计（事实）→ ② 建议项与提案账目（账面，applied 永远 false）
//     ③ 人工标签回流（复核）—— 这是唯一能让样本变"证据"的动作。
//
//   ★ 为什么所有判定都来自后端（本组件一行都不算）：
//     「够不够 20 条」「能不能给方向」「现场生效值知不知道」都是 `color_advice.advice()`
//     那一份的产物；前端再算一遍就会出现"表里说够、按钮却是灰的"（§5.5 铁律④）。
//   ★ 三态不塌成布尔（铁律③）：`from === null` = 现场生效值**不知道**，
//     与 `from === 0` 长得不一样（本文件里所有 `?? ` / `=== null` 分支都是为这一条）。
//   ★ 没有"已生效"这个状态：后端每条建议/提案都带 applied:false，
//     这里也不显示"已应用"，结构上不给假显示留位置（铁律⑤）。
// ===================================================================
import { computed, onMounted } from "vue";
import Icon from "./Icon.vue";
import { useColorAdviceStore } from "../stores/colorAdvice.js";
import { useAuthStore } from "../stores/auth.js";
import { materialColor } from "../utils/colorSemantics.js";

const ca = useColorAdviceStore();
const auth = useAuthStore();

const canWrite = computed(() => auth.controlActive);
const writTip = computed(() => canWrite.value
  ? "" : "需要控制令牌（点右侧「获取控制令牌」登录后再操作）");

const noteText = computed(() => (ca.advice && ca.advice.note)
  || "建议只是提案：落库不等于生效。阈值只能由人在现场按真图回放结果显式改。");

const THR_STATE = {
  camera: "相机服务回显的生效值（可信）",
  manual: "人工告知的生效值",
  unknown: "不知道（相机服务没回显阈值 —— 只给方向、不落数值）",
};
const thrLabel = computed(() => THR_STATE[ca.snapshotState] || ("未知状态：" + ca.snapshotState));
const thrDetail = computed(() => {
  const snap = (ca.advice && ca.advice.threshold_snapshot) || {};
  const thr = snap.thr || {};
  const keys = Object.keys(thr);
  if (ca.snapshotState !== "camera" && ca.snapshotState !== "manual") return "";
  if (!keys.length) return "（回显里没有阈值明细）";
  const min = snap.min_conf;
  return "· min_conf=" + (min === null || min === undefined ? "—" : min)
    + " · " + keys.slice(0, 6).map((k) => k + "=" + thr[k]).join(" ");
});

const pendingCount = computed(() => ca.pending.length);
const sampleTotal = computed(() => {
  const t = ca.samples && ca.samples.total;
  return typeof t === "number" ? t : "—";
});

/** 物料色取色走唯一出口（`utils/colorSemantics.js` → `SORT_COLOR_HEX`，**不在本卡复制色值**）。
 *  ★ 橙/黄/青/紫在前端**没有**既定色值（现场未标定，§7 已知限制）→ 返回 ''，
 *    模板用 `v-if` 干脆不画圆点，绝不拿一个"看着像橙色"的数顶上（铁律③）。 */
function hexOf(c) {
  return materialColor(c) || "";
}
function num(v, d = 3) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : "—";
}
/** 现场值三态：null 必须显示成"不知道"，不能显示成 0 或出厂默认。 */
function fromText(it) {
  if (it.from === null || it.from === undefined) return "不知道（" + (it.from_state || "unknown") + "）";
  return num(it.from);
}
function fromTo(it) {
  return fromText(it) + "→" + num(it.to);
}
const OUTCOME_ZH = {
  unreviewed: "未复核", confirmed: "已确认判对", corrected: "已改判",
  put_back: "已放回", not_judged: "未判色（空跑）",
};
function outcomeText(s) {
  const base = OUTCOME_ZH[s.outcome] || ("未知标签：" + (s.outcome || "—"));
  return s.true_color ? base + "（真色 " + s.true_color + "）" : base;
}

function doPropose() { ca.propose(""); }
function doRollback() { ca.rollback(); }

onMounted(() => { ca.load(); });
</script>

<style scoped>
.adv-card .note { margin: 4px 0 6px; line-height: 1.5; }
.adv-card .thr { margin: 0 0 8px; }
.tbl { display: flex; flex-direction: column; gap: 1px; margin: 6px 0 10px; }
.tr { display: grid; grid-template-columns: 0.9fr 0.8fr 0.6fr 0.5fr 0.6fr 1fr 1.1fr 1.8fr;
  gap: 6px; padding: 3px 4px; font-size: 12px; border-bottom: 1px solid var(--line, #2a3340); }
.tr.th { color: var(--muted, #8b98a8); font-size: 11px; }
.c1 { font-weight: 600; }
.c2, .c3 { text-align: right; font-variant-numeric: tabular-nums; }
.c3.warn { color: var(--warn, #e0b04a); }
.c4 { color: var(--muted, #8b98a8); }
.sug { display: block; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 4px; vertical-align: middle; }
.box { border: 1px solid var(--line, #2a3340); border-radius: 6px; padding: 6px 8px; margin: 8px 0; }
.box-h { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.btns { display: flex; gap: 6px; flex-wrap: wrap; margin: 6px 0; }
.err { color: var(--danger, #e57373); }
.ok { color: var(--ok, #66bb6a); }
.hist { margin-top: 6px; }
.hrow { display: flex; gap: 8px; font-size: 11px; border-bottom: 1px solid var(--line, #2a3340);
  padding: 2px 0; }
.hrow .sum { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.srow { display: flex; gap: 8px; align-items: center; font-size: 11px;
  border-bottom: 1px solid var(--line, #2a3340); padding: 3px 0; flex-wrap: wrap; }
.srow .grow { margin-left: auto; display: flex; gap: 4px; align-items: center; }
.mono { font-variant-numeric: tabular-nums; }
.mini { padding: 2px 6px; font-size: 11px; }
select { font-size: 11px; background: var(--bg2, #1c2230); color: inherit;
  border: 1px solid var(--line, #2a3340); border-radius: 4px; }
h3 .mini { margin-left: auto; }
h3 { display: flex; align-items: center; gap: 6px; }
</style>
