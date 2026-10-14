<script setup>
// =====================================================================
// ★ E6「机器人自述 = 每天一页现场日报」卡（**只读**）
//
// 这张卡回答的是现场每天最想问的那句话：「这台机器今天到底干了什么、卡在哪」。
// 数据一份来自后端 `build_report()`，界面上每一个数字旁边都能看到它的出处。
//
// ★ 硬边界（改动前先读，勿绕）：
//   · **只读、零下发**：唯一一条请求是 GET `/api/system/daily-report`（不需要控制
//     令牌，同 `/api/sorting/cells` 口径），卡面上**没有任何机器人动作按钮** —— 只有
//     「刷新」「导出 markdown」，都是本地/读取行为；
//   · **数字不重算**：`fields` / `attention.items` 里的值直接显示，前端不再判一遍
//     （§5.5 铁律④：同一屏同一结论必须同源）；
//   · **无数据不塌成 0**：`status === "no_data"` 就照写「无此项数据」，`unavailable`
//     就写「取不到」；绝不 `x || 0`（§5.5 铁律③）；
//   · **一致性自检必须显形**：`consistency.count > 0` → 顶部红字，正文里逐条列出。
//     这是"每个数字都能追溯到结构化源"的最后一颗钉子，绝不允许 UI 静默通过；
//   · **内存态如实**：后端在 source / note 里已经写了「本机本次会话」，这里原样展示，
//     不美化成"历史"；
//   · **不引入新依赖**：导出 markdown 走浏览器 Blob + a.download（见 store.exportMarkdown）。
//   · **不接 LLM**：本轮纯模板；就算接了讲解员 LLM，它也不能改数字（后端已把住）。
// =====================================================================
import { onMounted } from "vue";
import Icon from "./Icon.vue";
import { useReportStore } from "../stores/report.js";

const report = useReportStore();

onMounted(() => { report.load(); });

const STATUS_LABEL = {
  ok: "有数据",
  no_data: "无此项数据",
  unavailable: "取不到（源接口不可用）",
};
function statusClass(s) {
  if (s === "ok") return "dr-st-ok";
  if (s === "no_data") return "dr-st-none";
  return "dr-st-bad";
}
// 数字直接显示后端给的字符串（fields 已经过 _fmt_num），不再本地格式化
function v(x) {
  if (x === null || x === undefined) return "—";
  if (typeof x === "boolean") return x ? "是" : "否";
  return String(x);
}
</script>

<template>
  <div class="card dr">
    <h3>
      <Icon name="activity" :size="14" /> 机器人自述 · 今日现场日报
      <span class="h3-sub">E6 · 每个数字都能追溯到结构化源；无数据不塌成 0</span>
    </h3>

    <div class="dr-head">
      <span class="small muted">
        日期 <b>{{ report.report?.date || "—" }}</b>
        <template v-if="report.timezone">· {{ report.timezone }}</template>
      </span>
      <span class="dr-spacer"></span>
      <button class="mini" :disabled="report.busy"
              title="重新读取今日日报（只读，不下发任何动作）" @click="report.load()">
        <Icon name="refresh" :size="13" /> {{ report.busy ? "读取中" : "刷新" }}
      </button>
      <button class="mini" :disabled="!report.markdown"
              title="导出 markdown 到本地（浏览器 Blob，不走后端新路由）"
              @click="report.exportMarkdown()">
        <Icon name="download" :size="13" /> 导出 markdown
      </button>
    </div>

    <p v-if="report.err" class="dr-err">
      <Icon name="alert" :size="12" /> {{ report.err }}
      <span class="muted"> —— 读不到就是读不到，绝不用猜的数字凑一份"日报"。</span>
    </p>

    <!-- 一致性自检：命中即红字，正文里逐条列出；UI 不许静默 -->
    <div v-if="report.hasInconsistency" class="dr-cons dr-cons-bad">
      <div class="dr-cons-h">
        <Icon name="alert" :size="13" />
        ★ 一致性自检未通过（{{ report.consistency.count }} 处）——
        <b>不许发布</b>：以下数字在正文里出现，结构化源里找不到同值。
      </div>
      <ul class="dr-cons-list">
        <li v-for="(x, i) in report.inconsistent" :key="i">{{ x }}</li>
      </ul>
    </div>
    <div v-else-if="report.report" class="dr-cons dr-cons-ok" title="正文里每一个数字都能追溯到结构化源">
      <Icon name="check" :size="12" /> 一致性自检：通过（正文数值均可追溯到结构化源）
    </div>

    <!-- 今日三件最该关注的事：纯规则排序，每条带"依据数字"与来源；不凑数 -->
    <div v-if="report.report" class="dr-att">
      <div class="dr-sec-h">
        <Icon name="target" :size="13" /> 今日最该关注的事
        <span class="small muted">（结构化数字从大到小的规则排序，非玄学）</span>
      </div>
      <ol v-if="report.attention.length" class="dr-att-list">
        <li v-for="it in report.attention" :key="it.rank" class="dr-att-item">
          <b class="dr-att-rank">#{{ it.rank }}</b>
          <span class="dr-att-label">{{ it.label }}</span>
          <span class="dr-att-val">{{ v(it.value) }} {{ it.unit }}</span>
          <div class="dr-att-basis muted">依据：{{ it.basis }}</div>
          <div class="dr-att-src muted">来源：{{ it.source }}</div>
        </li>
      </ol>
      <p v-else class="dr-att-none small muted">
        {{ report.attentionNote || "无关注项 —— 所有候选指标今日皆为 0 或无数据（不凑数、不编一句安慰人的话）" }}
      </p>
    </div>

    <!-- 逐节展示（顺序与后端 _SECTION_BUILDERS 一致） -->
    <div v-for="sec in report.sections" :key="sec.key" class="dr-sec">
      <div class="dr-sec-h">
        <Icon name="layers" :size="13" /> {{ sec.title }}
        <span class="dr-tag" :class="statusClass(sec.status)">
          {{ STATUS_LABEL[sec.status] || "状态未知" }}
        </span>
      </div>
      <div class="dr-sec-src muted">来源：{{ sec.source }}</div>
      <div v-if="sec.note" class="dr-sec-note muted">{{ sec.note }}</div>

      <ul v-if="sec.status === 'ok' && sec.lines && sec.lines.length" class="dr-sec-lines">
        <li v-for="(ln, i) in sec.lines" :key="i">{{ ln }}</li>
      </ul>
      <p v-else-if="sec.status === 'ok'" class="small muted">本节结构化字段为空 —— 不猜内容。</p>
      <p v-else-if="sec.status === 'no_data'" class="dr-sec-none">
        无此项数据：<span class="muted">{{ (sec.lines && sec.lines[0]) || "本节没有可追溯的读数" }}</span>
      </p>
      <p v-else class="dr-sec-none">
        取不到：<span class="muted">{{ (sec.lines && sec.lines[0]) || "源接口不可用（日报不猜数、不填 0）" }}</span>
      </p>
    </div>

    <p v-if="report.report" class="small note muted">
      本卡片是"机器人自述"的模板版：<b>零 LLM 依赖</b>、<b>零下发</b>、<b>不写事件</b>；
      内存态数据源（到位自证 / 报警台账 / 一格三证）已注明「本机本次会话」。
    </p>
  </div>
</template>

<style scoped>
.dr { display: flex; flex-direction: column; gap: 8px; }
.dr-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 2px; }
.dr-spacer { flex: 1; }
button.mini {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; font-size: 12px;
  background: none; border: 1px solid var(--line, #2a3245); border-radius: 4px;
  color: var(--text-2, #a9b4c9); cursor: pointer;
}
button.mini:disabled { opacity: .55; cursor: default; }
.dr-err { margin: 0; font-size: 12px; color: #ff8a8a; display: flex; align-items: center; gap: 4px; }

.dr-cons { margin: 4px 0; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
.dr-cons-ok { color: #7ee2a8; background: rgba(126, 226, 168, .07); border: 1px solid #2f5d46; }
.dr-cons-bad { color: #ff9c9c; background: rgba(255, 156, 156, .08); border: 1px solid #5d2f34; }
.dr-cons-h { display: flex; align-items: center; gap: 4px; font-weight: 600; }
.dr-cons-list { margin: 4px 0 0 18px; padding: 0; }
.dr-cons-list li { margin: 2px 0; font-family: var(--mono, monospace); font-size: 11.5px; }

.dr-att { margin-top: 6px; padding: 6px 8px; border: 1px solid var(--line, #21283a); border-radius: 6px; }
.dr-att-list { margin: 6px 0 0 0; padding: 0 0 0 20px; }
.dr-att-item { margin: 4px 0; font-size: 12.5px; }
.dr-att-rank { color: var(--text-3, #7e8ba3); margin-right: 4px; }
.dr-att-label { font-weight: 600; }
.dr-att-val { margin-left: 6px; color: #ffb86b; }
.dr-att-basis { font-size: 11.5px; margin-top: 1px; }
.dr-att-src { font-size: 11px; margin-top: 1px; color: var(--text-3, #7e8ba3); }
.dr-att-none { margin: 6px 0 0; }

.dr-sec { margin-top: 8px; padding: 6px 8px; border: 1px solid var(--line, #21283a); border-radius: 6px; }
.dr-sec-h { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; }
.dr-sec-src { font-size: 11.5px; margin-top: 2px; }
.dr-sec-note { font-size: 11.5px; margin-top: 2px; }
.dr-sec-lines { margin: 6px 0 0 0; padding: 0 0 0 20px; font-size: 12.5px; }
.dr-sec-lines li { margin: 2px 0; }
.dr-sec-none { margin: 6px 0 0; font-size: 12.5px; color: #ffb86b; }

.dr-tag { font-size: 10.5px; padding: 0 5px; border-radius: 3px; border: 1px solid var(--line, #2a3245); font-weight: normal; }
.dr-st-ok { color: #7ee2a8; border-color: #2f5d46; }
.dr-st-none { color: #ffb86b; border-color: #5d4a2f; }
.dr-st-bad { color: #ff9c9c; border-color: #5d2f34; }

.note { margin: 8px 0 0; }
.small { font-size: 12px; }
.muted { color: var(--text-3, #7e8ba3); }
</style>
