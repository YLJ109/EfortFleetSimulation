<script setup>
// =====================================================================
// ★ E7 一格三证（只读复盘卡）
//
// 回答的是现场最常问的那句：「**这一格当时到底看到了什么、机器人真到了没有**」。
// 数据早就有（判色结论 / 到位自证残差 / 照片档），缺的是**装订** —— 三样按同一个
// 格号（`B3` 与 `B3上` 是同一格）钉成一条，缺哪一样就写缺哪一样 + 为什么缺。
//
// ★ 硬边界（改动前先读）：
//   · **只读**：这张卡不产生任何动作，唯一的请求是 GET `/api/sorting/cells`；
//   · **判据不在这儿**：齐全度、缺的原因、到位结论全部来自后端那一份，
//     前端只渲染（§5.5 铁律④：同一屏同一结论必须同源）；
//   · **三态不塌成布尔**：`ok===true` 到手 / `===false` 缺 / 其它 = 未知，
//     未知的样子必须和"缺"不一样（§5.5 铁律③）；
//   · **没有记录 ≠ 没有发生**：内存只留最近的，历史在日志目录的 jsonl 里，
//     卡面如实这么写，不写"今天没有异常"这种话。
//   · 照片这一证**不在界面里显示图**：档名来自相机服务的目录，取图要另开一路
//     相机请求（现场相机默认关、还可能带令牌），这里只列档名与来源，不假装"看得到图"。
// =====================================================================
import { computed, onMounted, onUnmounted } from "vue";
import Icon from "./Icon.vue";
import { useCellStore } from "../stores/cell.js";

const cell = useCellStore();

const EV_LABEL = { photo: "照片", colour: "判色", pose: "位姿" };
// 到位自证的结论（与后端 `twin_residual.VERDICTS` 对齐；认不出的一律「未知」）
const VERDICT_ZH = {
  arrived: "已到位", slow: "到位（慢）", overshoot: "到位后回弹",
  stuck: "没动", not_reached: "未到位", moving: "收敛中", pending: "待观测",
  no_data: "无实测数据", aborted: "已中止",
};

onMounted(() => cell.start());
onUnmounted(() => cell.stop());

function okState(ev) {
  if (!ev) return "unknown";
  if (ev.ok === true) return "ok";
  if (ev.ok === false) return "bad";
  return "unknown";
}
function okText(ev, key) {
  const s = okState(ev);
  if (s === "ok") return key === "pose" ? "到位" : "有";
  if (s === "bad") return "缺";
  return "未知";
}
function num(v, digits = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
}
function hhmmss(at) {
  const n = Number(at);
  if (!Number.isFinite(n)) return "—";
  const d = new Date(n * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function verdictZh(v) {
  return VERDICT_ZH[String(v || "")] || "未知";
}
function votesText(v) {
  const o = (v && v.votes) || {};
  const ks = Object.keys(o);
  if (!ks.length) return "";
  return ks.map((k) => `${k}${o[k]}`).join(" / ");
}
function imgText(ev) {
  const o = (ev && ev.images) || {};
  const ks = Object.keys(o);
  return ks.length ? ks.map((k) => `${k}=${o[k]}`).join("  ") : "";
}
const recordsToShow = computed(() =>
  cell.selectedRecords.length ? cell.selectedRecords : cell.latestRecords);
const listTitle = computed(() => (cell.selected
  ? `格子「${cell.selected}」最近 ${cell.selectedRecords.length} 条`
  : `最近 ${cell.latestRecords.length} 条`));
</script>

<template>
  <div class="card ce">
    <h3>
      <Icon name="layers" :size="14" /> 一格三证
      <span class="h3-sub">照片 · 判色 · 位姿（只读复盘，不执行）</span>
    </h3>

    <div class="ce-head">
      <span class="small muted">水位 <b>{{ cell.latestSeq }}</b>
        <template v-if="cell.fresh > 0">（新增 {{ cell.fresh }}）</template>
        <template v-if="cell.rebooted">· 后端重启后水位归零</template>
      </span>
      <button class="mini" :disabled="cell.busy"
              title="重新读取三证（只读，不下发任何动作）" @click="cell.load()">
        <Icon name="refresh" :size="13" /> {{ cell.busy ? "读取中" : "重查" }}
      </button>
    </div>
    <p v-if="cell.err" class="ce-err">{{ cell.err }}<span class="muted"> —— 读不到就是读不到，这不代表没有隐患。</span></p>

    <!-- ① 逐格齐全度（数字只来自后端 stats，前端不再判一遍） -->
    <div v-if="cell.cells.length" class="ce-grid">
      <button v-for="c in cell.cells" :key="c.cell_id" class="ce-cell"
              :class="{ on: cell.selected === c.cell_id }"
              :title="`${c.cell_id}：${c.n} 条 · 齐全 ${c.complete} · 缺 照片${c.missing.photo} 判色${c.missing.colour} 位姿${c.missing.pose}`"
              @click="cell.pick(c.cell_id)">
        <b>{{ c.cell_id }}</b>
        <span class="n">{{ c.complete }}/{{ c.n }}</span>
        <span class="tags">
          <i v-for="k in ['photo', 'colour', 'pose']" :key="k"
             :class="c.missing[k] ? 't-bad' : 't-ok'">{{ EV_LABEL[k] }}</i>
        </span>
      </button>
    </div>
    <p v-else class="small muted">
      还没有三证记录（本机本次会话内没往任何格放过件、也没放回过件）——
      <b>没有记录 ≠ 没有发生</b>，历史看日志目录 `cells/cells-YYYYMMDD.jsonl`。
    </p>

    <!-- ② 装订好的记录（缺哪样写哪样 + 为什么缺） -->
    <div v-if="recordsToShow.length" class="ce-list">
      <div class="small muted">{{ listTitle }}</div>
      <div v-for="r in recordsToShow" :key="r.seq" class="ce-rec">
        <div class="ce-rec-h">
          <b>{{ r.cell_id }}</b>
          <span class="small muted">{{ hhmmss(r.at) }}</span>
          <span class="small" :class="r.complete ? 't-ok' : 't-bad'">
            {{ r.complete ? "三证齐全" : `缺 ${r.missing.map((k) => EV_LABEL[k]).join("、")}` }}
          </span>
          <span v-if="r.extra && r.extra.purpose" class="small muted">{{ r.extra.purpose }}</span>
          <span v-if="r.extra && r.extra.color" class="small">{{ r.extra.color }}→{{ r.extra.slot }}</span>
        </div>
        <div v-for="k in ['photo', 'colour', 'pose']" :key="k" class="ce-ev">
          <span class="tag" :class="`v-${okState(r.evidence[k])}`">
            {{ EV_LABEL[k] }}·{{ okText(r.evidence[k], k) }}
          </span>
          <span class="ce-ev-b">
            <template v-if="k === 'photo'">
              <template v-if="imgText(r.evidence[k])">
                <span class="muted">档名（不在本页取图）：</span>{{ imgText(r.evidence[k]) }}
              </template>
              <span v-else class="muted">{{ r.evidence[k].reason || "没有照片" }}</span>
            </template>
            <template v-else-if="k === 'colour'">
              <template v-if="okState(r.evidence[k]) === 'ok'">
                {{ r.evidence[k].color || "—" }} · conf {{ num(r.evidence[k].conf) }}
                <span v-if="votesText(r.evidence[k])" class="muted">{{ votesText(r.evidence[k]) }}</span>
              </template>
              <span v-else class="muted">{{ r.evidence[k].reason || "没有判色" }}</span>
            </template>
            <template v-else>
              <template v-if="r.evidence[k].dev_max_deg != null">
                偏差 <b>{{ num(r.evidence[k].dev_max_deg, 1) }}°</b>
              </template>
              <span v-if="r.evidence[k].verdict" class="muted">
                到位自证：{{ verdictZh(r.evidence[k].verdict) }}
              </span>
              <span v-if="r.evidence[k].simulated" class="t-warn small">空跑（没真下发）</span>
              <span v-if="r.evidence[k].reason" class="muted">{{ r.evidence[k].reason }}</span>
            </template>
          </span>
        </div>
      </div>
    </div>

    <p v-if="cell.note" class="small note">{{ cell.note }}</p>
    <p v-else class="small note">口径未加载：点「重查」或等后端 `/api/sorting/cells` 可用。</p>
  </div>
</template>

<style scoped>
.ce-head { display: flex; justify-content: space-between; align-items: center; margin-top: 2px; }
button.mini {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; font-size: 12px;
  background: none; border: 1px solid var(--line, #2a3245); border-radius: 4px;
  color: var(--text-2, #a9b4c9); cursor: pointer;
}
button.mini:disabled { opacity: .55; cursor: default; }
.ce-err { margin: 6px 0 0; font-size: 12px; color: #ff8a8a; }
.ce-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); gap: 6px; margin: 8px 0 4px; }
.ce-cell {
  display: flex; flex-direction: column; gap: 2px; align-items: flex-start;
  padding: 4px 6px; text-align: left; cursor: pointer;
  background: var(--bg-2, #151a26); border: 1px solid var(--line, #2a3245); border-radius: 5px;
  color: var(--text-1, #e6ebf5); font-size: 12px;
}
.ce-cell.on { border-color: var(--accent, #4c8dff); }
.ce-cell b { font-size: 13px; }
.ce-cell .n { color: var(--text-3, #7e8ba3); }
.ce-cell .tags { display: flex; gap: 3px; flex-wrap: wrap; }
.ce-cell .tags i { font-style: normal; font-size: 10px; padding: 0 3px; border-radius: 3px; }
.t-ok { color: #7ee2a8; }
.t-bad { color: #ff9c9c; }
.t-warn { color: #ffb86b; }
.ce-list { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; }
.ce-rec { border: 1px solid var(--line, #21283a); border-radius: 6px; padding: 6px 8px; }
.ce-rec-h { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.ce-ev { display: flex; gap: 6px; align-items: baseline; margin-top: 4px; font-size: 12px; }
.ce-ev-b { flex: 1; min-width: 0; word-break: break-all; }
.tag { font-size: 11px; padding: 0 5px; border-radius: 3px; border: 1px solid var(--line, #2a3245); white-space: nowrap; }
.v-ok { color: #7ee2a8; border-color: #2f5d46; }
.v-bad { color: #ff9c9c; border-color: #5d2f34; }
.v-unknown { color: var(--text-3, #7e8ba3); border-style: dashed; }
.note { margin: 8px 0 0; color: var(--text-3, #7e8ba3); }
.small { font-size: 12px; }
.muted { color: var(--text-3, #7e8ba3); }
</style>
