<script setup>
// 机房设置抽屉：改 12 台的登记信息（名称 / IP / 端口 / 型号 / 区排号 / 全场编号）。
//
// 形态沿用大屏那套 IDC 口径：深底、钢蓝细线、切角、青绿激活态；不放装饰性图标。
//
// ★ 为什么值得单独一个组件而不是塞进 FleetView：FleetView 已经同时管 3D 容器、
//   作用域、选中台、KPI 与 toast，再叠一张 12 行 × 9 列的可编辑表格，
//   任何一个改动都要重新推理"这页现在到底在干什么"。
// ★ 比对与校验都在 utils/fleetRegistry.js（能被 node 断言跑），这里只管渲染。
// ★ 后端才是权限与安全边界：id 改不了、driving/write_allowed 一律拒、
//   保存前整表校验（两台踩同一位置会被拦下）。这里做的一切都只是"少跑一趟"。
import { computed, reactive, ref, watch } from "vue";
import { useFleetStore } from "../stores/fleet.js";
import { useAuthStore } from "../stores/auth.js";
import { diffRows, diffLayout, validateRows } from "../utils/fleetRegistry.js";
import Icon from "./Icon.vue";

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(["close"]);

const fleet = useFleetStore();
const auth = useAuthStore();

const draft = reactive({});           // id → { 字段: 新值 }（只存被动过的键）
const layout = reactive({});          // 阵列间距草稿
const note = ref("");                 // 保存结果（成功也要给一句，别让人猜）
const failed = ref("");               // 后端原样回来的拒绝原因

const GROUP_LABEL = { lower: "下排", upper: "上排" };

watch(() => props.open, (on) => {
  if (on) {
    note.value = ""; failed.value = "";
    fleet.loadConfig().catch(() => { /* cfgError 已在 store 里，底部那行会显示 */ });
  }
});

// 名单到货后把草稿清掉：抽屉开着时后端若被别处改过，留着的草稿会"复活"旧改动。
watch(() => fleet.cfgRows, () => {
  for (const k of Object.keys(draft)) delete draft[k];
  for (const k of Object.keys(layout)) layout[k] = "";
});

/**
 * 格子里显示什么：改过就显示草稿，没改就显示**生效值**。
 * ★ 一开始写成 `cell()` 直接返回 undefined，整张表打开就是一片空白 ——
 *   设置页看不见现状就等于不敢改（而且"清空"和"没填"也会长得一模一样）。
 */
function val(row, field) {
  const d = draft[row.id];
  if (d && field in d) return d[field];
  const v = row[field];
  return v === null || v === undefined ? "" : v;
}
function onInput(id, field, ev) {
  const raw = ev && ev.target ? ev.target.value : ev;
  const d = draft[id] || (draft[id] = {});
  const row = (fleet.cfgRows || []).find((r) => r.id === id) || {};
  // 与生效值一致时把这条从草稿里摘掉 —— 否则"待保存 1 处改动"会是假的
  const same = String(raw ?? "").trim() === String(row[field] ?? "").trim();
  if (same) { delete d[field]; if (!Object.keys(d).length) delete draft[id]; }
  else d[field] = raw;
  failed.value = "";
}
const touchedRows = computed(() => diffRows(fleet.cfgRows || {}, draft));
const layoutPatch = computed(() => diffLayout(fleet.cfgMeta.layout || {}, layout));
const issues = computed(() => validateRows(fleet.cfgRows || {}, draft, {
  ...fleet.cfgMeta,
  layout: fleet.cfgMeta.layout,
}));
const patchCount = computed(() => touchedRows.value.length + Object.keys(layoutPatch.value).length);
const dirty = computed(() => patchCount.value > 0);

const canWrite = computed(() => auth.controlActive && auth.isAdmin);
const needAdmin = computed(() => auth.controlActive && !auth.isAdmin);
const canSave = computed(() => canWrite.value && dirty.value && !issues.value.length
  && !fleet.cfgSaving && !fleet.cfgBusy);

function payload() {
  const out = { robots: touchedRows.value };
  if (Object.keys(layoutPatch.value).length) out.layout = layoutPatch.value;
  return out;
}

async function save() {
  if (!canSave.value) return;
  failed.value = ""; note.value = "";
  try {
    const d = await fleet.saveConfig(payload());
    for (const k of Object.keys(draft)) delete draft[k];
    for (const k of Object.keys(layout)) layout[k] = "";
    note.value = (d && d.message) || "已保存";
    if (d && d.collector && d.collector.started === false) {
      note.value += " · 但机房采集没起来（fleet.enabled / EFORT_FLEET）";
    }
    if (d && d.scope_released) note.value += " · 控制作用域已收回，需重新申请";
  } catch (e) {
    // ★ 后端给什么就说什么，不翻译成"保存失败"了事：
    //   "A1-1 与 A1-2 踩在同一个位置" 这种话正是操作员下一步要知道的。
    failed.value = String((e && e.message) || e);
  }
}

async function reset() {
  if (!canWrite.value || fleet.cfgSaving) return;
  failed.value = ""; note.value = "";
  try {
    const d = await fleet.resetConfig();
    for (const k of Object.keys(draft)) delete draft[k];
    for (const k of Object.keys(layout)) layout[k] = "";
    note.value = (d && d.message) || "已恢复默认";
  } catch (e) {
    failed.value = String((e && e.message) || e);
  }
}

function discard() {
  for (const k of Object.keys(draft)) delete draft[k];
  for (const k of Object.keys(layout)) layout[k] = "";
  note.value = ""; failed.value = "";
}

function errOf(id, field) {
  const hit = issues.value.find((x) => x.id === id && x.field === field);
  return hit ? hit.msg : "";
}
function modelOpts(row) {
  const ms = fleet.cfgMeta.models || [];
  return ms.length ? ms : [{ key: row.model, display_name: row.model }];
}

const LAYOUT_ITEMS = [
  { key: "cell_pitch_m", zh: "单台围栏边长", en: "CELL PITCH", min: 0.6, max: 10, step: 0.01, unit: "m" },
  { key: "group_gap_m", zh: "上下排净空", en: "ROW GAP", min: 0, max: 20, step: 0.05, unit: "m" },
  { key: "zone_gap_m", zh: "A/B 区通道", en: "ZONE GAP", min: 0, max: 20, step: 0.05, unit: "m" },
  { key: "yaw_deg", zh: "安装朝向", en: "MOUNT YAW", min: -360, max: 360, step: 1, unit: "°" },
];
</script>

<template>
  <transition name="fsd">
    <section v-if="open" class="fsd card">
      <header class="fsd-h">
        <div class="t">
          <b>机房设置</b>
          <em>FLEET REGISTRY · {{ (fleet.cfgRows || []).length }} 台</em>
        </div>
        <button class="x" title="关闭" @click="emit('close')"><Icon name="close" :size="13" /></button>
      </header>

      <p class="fsd-ro">{{ fleet.cfgMeta.readonly_note || "加载中…" }}</p>

      <div class="fsd-body">
        <!-- 表头 -->
        <div class="fsd-row hd">
          <span>代号</span><span>名称</span><span>IP</span><span>端口</span>
          <span>型号</span><span>区</span><span>排</span><span>位</span><span>编号</span>
        </div>

        <div v-if="fleet.cfgBusy && !(fleet.cfgRows || []).length" class="fsd-empty">正在读登记表…</div>
        <div v-else-if="fleet.cfgError" class="fsd-empty err">{{ fleet.cfgError }}</div>

        <div v-for="r in fleet.cfgRows || []" :key="r.id" class="fsd-row"
             :class="{ changed: (r.overridden || []).length }">
          <span class="code">
            <i :title="`登记 id ${r.id}（主键，不可改）`">{{ r.id }}</i>
            <em v-if="(r.overridden || []).length" class="ov"
                :title="`覆盖层已改过：${r.overridden.join('、')}`">改</em>
          </span>
          <input :value="val(r, 'name')" maxlength="40" spellcheck="false"
                 :class="{ bad: errOf(r.id, 'name') }" :title="r.name"
                 @input="onInput(r.id, 'name', $event)">
          <input :value="val(r, 'host')" spellcheck="false" placeholder="IPv4"
                 :class="{ bad: errOf(r.id, 'host') }" @input="onInput(r.id, 'host', $event)">
          <input :value="val(r, 'port')" inputmode="numeric" class="num"
                 :class="{ bad: errOf(r.id, 'port') }"
                 :title="`默认来自驱动段：${r.port}`" @input="onInput(r.id, 'port', $event)">
          <select :value="val(r, 'model')" :class="{ bad: errOf(r.id, 'model') }"
                  @change="onInput(r.id, 'model', $event.target.value)">
            <option v-for="m in modelOpts(r)" :key="m.key" :value="m.key">{{ m.display_name }}</option>
          </select>
          <select :value="val(r, 'zone')" :class="{ bad: errOf(r.id, 'zone') }"
                  @change="onInput(r.id, 'zone', $event.target.value)">
            <option v-for="z in (fleet.cfgMeta.zones || ['A', 'B'])" :key="z" :value="z">{{ z }}</option>
          </select>
          <select :value="val(r, 'group')" :class="{ bad: errOf(r.id, 'group') }"
                  @change="onInput(r.id, 'group', $event.target.value)">
            <option v-for="g in (fleet.cfgMeta.groups || ['lower', 'upper'])" :key="g" :value="g">
              {{ GROUP_LABEL[g] || g }}
            </option>
          </select>
          <input :value="val(r, 'slot')" inputmode="numeric" class="num tiny"
                 :class="{ bad: errOf(r.id, 'slot') }" @input="onInput(r.id, 'slot', $event)">
          <input :value="val(r, 'order')" inputmode="numeric" class="num tiny"
                 :class="{ bad: errOf(r.id, 'order') }" @input="onInput(r.id, 'order', $event)">
        </div>

        <!-- 阵列：只放能改的四项 -->
        <div class="fsd-lay">
          <div class="lay-h">阵列间距 <em>ARRAY LAYOUT</em>
            <span class="gs">每排台数 {{ fleet.cfgMeta.group_size || "?" }} · yaml 定，界面不可改</span>
          </div>
          <label v-for="it in LAYOUT_ITEMS" :key="it.key" class="lay">
            <i>{{ it.zh }}<u>{{ it.en }}</u></i>
            <input type="number" :min="it.min" :max="it.max" :step="it.step"
                   :placeholder="fleet.cfgMeta.layout && fleet.cfgMeta.layout[it.key]"
                   v-model="layout[it.key]">
            <u class="u">{{ it.unit }}</u>
          </label>
        </div>
      </div>

      <footer class="fsd-f">
        <div class="msgs">
          <p v-if="issues.length" class="err">
            <Icon name="alert" :size="12" />
            {{ issues.slice(0, 3).map((x) => `${x.id} ${x.msg}`).join("；") }}
            <em v-if="issues.length > 3">…共 {{ issues.length }} 处</em>
          </p>
          <p v-else-if="failed" class="err"><Icon name="alert" :size="12" /> {{ failed }}</p>
          <p v-else-if="note" class="ok"><Icon name="check" :size="12" /> {{ note }}</p>
          <p v-else-if="fleet.cfgError" class="err"><Icon name="alert" :size="12" /> {{ fleet.cfgError }}</p>
          <p v-else-if="needAdmin" class="warn">
            <Icon name="lock" :size="12" /> 当前是操作员令牌：改登记表需要管理员口令
          </p>
          <p v-else-if="!auth.controlActive" class="mut">
            未授权：可看不可改。保存需要管理员令牌（改 IP 等于改"在读谁的数"）。
          </p>
          <p v-else class="mut">覆盖层只存与 fleet.yaml 不同的项 · config/fleet_settings.json</p>
        </div>
        <div class="acts">
          <button class="b ghost" :disabled="!dirty" @click="discard">放弃修改</button>
          <button class="b ghost" :disabled="!canWrite || fleet.cfgSaving" @click="reset"
                  :title="canWrite ? '删掉覆盖层，回到 fleet.yaml 登记值' : '需要管理员令牌'">
            恢复默认
          </button>
          <button v-if="!auth.controlActive" class="b" @click="auth.requestLogin()">
            <Icon name="lock" :size="12" /> 申请控制
          </button>
          <button class="b primary" :disabled="!canSave" @click="save"
                  :title="canSave ? '保存并重建 12 条采集链路' : (issues.length ? '先修完上面标红的格子' : '没有待保存的改动')">
            <Icon v-if="fleet.cfgSaving" name="refresh" :size="12" class="spin" />
            <Icon v-else name="check" :size="12" />
            保存<em v-if="patchCount">{{ patchCount }}</em>
          </button>
        </div>
      </footer>
    </section>
  </transition>
</template>

<style scoped>
/* 右侧抽屉：压住右列卡片之上（z 比 .fv-col 高、比弹层控制台低一档也行 ——
   但设置与控制台不该同时开，见 FleetView 里的互斥） */
.fsd { position: absolute; right: 8px; top: 54px; bottom: 46px; z-index: 30;
  width: 560px; max-width: calc(100vw - 24px);
  display: flex; flex-direction: column; gap: 6px;
  padding: 8px 9px; overflow: hidden;
  background: var(--panel); border: 1px solid var(--line); border-radius: 0;
  box-shadow: 0 0 26px rgba(0, 0, 0, .55); }

.fsd-h { display: flex; align-items: baseline; gap: 8px; flex: none;
  padding-bottom: 6px; border-bottom: 1px solid var(--accent-line); }
.fsd-h .t { display: flex; align-items: baseline; gap: 8px; }
.fsd-h b { font-size: 13px; font-weight: 700; letter-spacing: 2px; color: var(--accent); }
.fsd-h em { font-style: normal; font-size: 7.5px; letter-spacing: 1.4px;
  text-transform: uppercase; color: var(--muted); }
.fsd-h .x { margin-left: auto; width: 22px; height: 22px; padding: 0; display: flex;
  align-items: center; justify-content: center; background: transparent;
  border: none; border-radius: 0; color: var(--muted); }
.fsd-h .x:hover { color: var(--err); }

.fsd-ro { flex: none; margin: 0; font-size: 10px; letter-spacing: .3px; color: var(--muted); }

.fsd-body { flex: 1 1 auto; overflow-y: auto; overflow-x: hidden; padding-right: 2px; }

/* 一行 = 一台。列宽按内容给：代号/型号窄格留给文字，IP 给足（21 位最宽） */
.fsd-row { display: grid; gap: 4px; align-items: center;
  grid-template-columns: 58px minmax(78px, 1fr) 96px 40px 76px 30px 48px 26px 30px;
  padding: 3px 0; border-bottom: 1px dashed var(--glass-line); }
.fsd-row.hd { position: sticky; top: 0; z-index: 1; background: var(--panel);
  font-size: 8px; letter-spacing: 1.2px; text-transform: uppercase; color: var(--muted);
  border-bottom: 1px solid var(--line); padding: 5px 0; }
.fsd-row.changed { background: var(--accent-soft); }

.code { display: flex; align-items: center; gap: 3px; font-family: var(--num);
  font-size: 11px; color: var(--txt2); }
.code i { font-style: normal; }
.code .ov { font-style: normal; font-size: 8px; padding: 0 3px; color: var(--accent);
  border: 1px solid var(--accent-line); }

.fsd-row input, .fsd-row select { min-width: 0; height: 24px; padding: 0 5px;
  font-family: var(--mono); font-size: 11px; color: var(--txt);
  background: var(--cap); border: 1px solid var(--line); border-radius: 0; }
.fsd-row input:focus, .fsd-row select:focus { outline: none; border-color: var(--accent-line);
  background: var(--panel2); }
.fsd-row input.bad, .fsd-row select.bad { border-color: var(--err-line); background: var(--err-soft);
  color: var(--err); }
.fsd-row input.num { text-align: right; font-family: var(--num); }
.fsd-row input.tiny { padding: 0 2px; }
.fsd-row select { padding: 0 2px; font-size: 10px; }

.fsd-empty { padding: 14px 6px; font-size: 11px; color: var(--muted); }
.fsd-empty.err { color: var(--err); }

.fsd-lay { margin-top: 8px; padding-top: 6px; border-top: 1px solid var(--line);
  display: grid; gap: 5px 8px; grid-template-columns: repeat(2, 1fr); }
.fsd-lay .lay-h { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 7px;
  font-size: 11px; color: var(--txt2); }
.fsd-lay .lay-h em { font-style: normal; font-size: 7.5px; letter-spacing: 1.3px;
  text-transform: uppercase; color: var(--muted); }
.fsd-lay .lay-h .gs { margin-left: auto; font-size: 9px; color: var(--muted); }
.fsd-lay .lay { display: flex; align-items: center; gap: 5px; }
.fsd-lay .lay i { font-style: normal; font-size: 10px; color: var(--muted); flex: none; width: 84px; }
.fsd-lay .lay i u { display: block; font-size: 7px; letter-spacing: 1px; text-transform: uppercase; }
.fsd-lay .lay input { flex: 1 1 auto; min-width: 0; height: 24px; padding: 0 6px;
  font-family: var(--num); font-size: 12px; text-align: right; color: var(--txt);
  background: var(--cap); border: 1px solid var(--line); border-radius: 0; }
.fsd-lay .lay input:focus { outline: none; border-color: var(--accent-line); }
.fsd-lay .lay .u { font-size: 9px; color: var(--muted); flex: none; }

.fsd-f { flex: none; display: flex; align-items: center; gap: 8px;
  padding-top: 6px; border-top: 1px solid var(--line); }
.msgs { flex: 1 1 auto; min-width: 0; }
.msgs p { margin: 0; display: flex; align-items: center; gap: 5px; font-size: 10.5px;
  line-height: 1.45; word-break: break-all; }
.msgs p.err { color: var(--err); }
.msgs p.warn { color: var(--warn); }
.msgs p.ok { color: var(--ok); }
.msgs p.mut { color: var(--muted); }
.msgs p em { font-style: normal; }

.acts { display: flex; align-items: center; gap: 5px; flex: none; }
.b { display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px;
  font-size: 11px; letter-spacing: 1px; color: var(--accent);
  background: var(--accent-soft); border: 1px solid var(--accent-line); border-radius: 0;
  clip-path: polygon(7px 0, 100% 0, 100% calc(100% - 7px), calc(100% - 7px) 100%, 0 100%, 0 7px); }
.b.ghost { color: var(--txt2); background: var(--cap); border-color: var(--line); }
.b.primary { color: #06222a; background: var(--accent); border-color: var(--accent); font-weight: 700; }
.b.primary em { font-style: normal; margin-left: 3px; padding: 0 4px; font-size: 10px;
  background: rgba(6, 34, 42, .28); }
.b:hover:not(:disabled) { filter: brightness(1.12); }
.b:disabled { opacity: .42; cursor: not-allowed; }
.spin { animation: fsd-spin 1s linear infinite; }
@keyframes fsd-spin { to { transform: rotate(360deg); } }

.fsd-enter-active, .fsd-leave-active { transition: opacity .16s ease, transform .16s ease; }
.fsd-enter-from, .fsd-leave-to { opacity: 0; transform: translateX(18px); }

@media (max-width: 1500px) { .fsd { width: 500px; } }
</style>
