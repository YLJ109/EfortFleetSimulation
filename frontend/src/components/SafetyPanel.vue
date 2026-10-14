<script setup>
// =====================================================================
// 安全围栏调试面板（三视图共用，右下侧栏常驻）。
//
// 交互：所有控件即时生效（只改本地 state 并推给 three），点「保存」才落盘。
// 覆盖：形状(矩形/四点/圆形) · 多区域 · 阈值双向 · 颜色 · 闪烁 · 角柱
//       · 报警条/状态片/声音 · 包围盒线框 · 视角保存复位 · 报警记录时间线
// =====================================================================
import { ref, computed, watch } from "vue";
import { useSafetyStore } from "../stores/safety.js";
import { setAlarmEnabled } from "../utils/alarm.js";
// C1 高性能画面模式：徽标的**颜色出口**统一走全站唯一语义表（判据一个字没改，见下）
import {
  hiPerfMode, clsFor, styleVarsFor, labelSuffix, markOf, MODE_HI_PERF,
} from "../utils/colorSemantics.js";
import { safetySemantic, healthSemantic } from "../utils/safetyLabels.js";
// ★ C3：阈值线(mm) 由**分级基准的唯一实现**换算，面板不再自己算一遍
import { basisOf } from "../three/safety.js";
import Icon from "./Icon.vue";
import ParamQuad from "./ParamQuad.vue";   // ★ C3：四元组唯一实现

const safety = useSafetyStore();

const open = ref(false);
const sec = ref({ shape: true, thr: true, color: false, global: false, alarm: false, events: false });
const curIdx = ref(0);

const cfg = computed(() => safety.config);
const zones = computed(() => (cfg.value && cfg.value.zones) || []);

// 全局：四面透明玻璃开关
const wallsOn = computed(() => (cfg.value && cfg.value.walls) !== false);
// 地面碰撞检测
const groundEnabled = computed(() => (cfg.value && cfg.value.ground && cfg.value.ground.enabled) !== false);
const groundWarn = computed(() => Math.round((cfg.value && cfg.value.ground && cfg.value.ground.warn_mm) || 100));
const groundDanger = computed(() => Math.round((cfg.value && cfg.value.ground && cfg.value.ground.danger_mm) || 50));
const groundHit = computed(() => Math.round((cfg.value && cfg.value.ground && cfg.value.ground.hit_mm) || 20));
const groundColors = computed(() => (cfg.value && cfg.value.ground && cfg.value.ground.colors) || {});
const groundOpacity = computed(() => (cfg.value && cfg.value.ground && cfg.value.ground.opacity) || {});
const GROUND_STATES = [
  { k: "safe", t: "安全" }, { k: "warn", t: "接近" },
  { k: "danger", t: "危险" }, { k: "hit", t: "碰撞" },
];
const cur = computed(() => {
  const zs = zones.value;
  if (!zs.length) return null;
  const i = Math.min(curIdx.value, zs.length - 1);
  return zs[i];
});

const STATE_TEXT = { safe: "安全", warn: "接近", danger: "危险", hit: "碰撞" };

// ★ A6：面板顶部那颗状态徽标同样受"评估健康"约束 —— 评估不新鲜时**不许**显示"安全"，
//   要显示"评估已停止/延迟"（与 SafetyChip、顶部报警条同一个判据：store 的 health）。
const headState = computed(() => (safety.health && !safety.health.ok
  ? { cls: "stalled", text: `评估${safety.health.label}` }
  : { cls: safety.last.state, text: STATE_TEXT[safety.last.state] || "安全" }));

// ---------------- C1：徽标的语义着色出口（分级判定仍是上面那一份，没另立） ----------------
//   stalled → `unknown`（这个数不可信）；其余按 STATE_SEMANTIC（safe→normal / warn / danger / hit→danger）。
const headSem = computed(() => (safety.health && !safety.health.ok
  ? healthSemantic(safety.health) : safetySemantic(safety.last)));
const headMode = computed(() => hiPerfMode());
const headCls = computed(() => clsFor(headSem.value, headMode.value));
const headVars = computed(() => styleVarsFor(headSem.value, headMode.value));
//   模式关闭时以下三项都退成"不显示"，徽标外观与改动前完全一致（非破坏式）。
const headHiPerf = computed(() => headMode.value === MODE_HI_PERF);
const headMark = computed(() => (headHiPerf.value ? markOf(headSem.value, headMode.value) : ""));
const headSuffix = computed(() => (
  headHiPerf.value && headSem.value === "unknown" ? labelSuffix("unknown") : ""));

// ---------------- 数值字段 ----------------
const zoneFields = computed(() => {
  const z = cur.value;
  if (!z) return [];
  const f = [{ k: "height", label: "围栏高 (m)", min: 0.3, max: 3, step: 0.05 }];
  const center = [
    { p: "center", k: "x", label: "中心 X (m)", min: -3, max: 3, step: 0.01 },
    { p: "center", k: "z", label: "中心 Z (m)", min: -3, max: 3, step: 0.01 },
  ];
  if (z.shape === "rect") {
    f.unshift(
      ...center,
      { p: "half", k: "x", label: "半宽 X (m)", min: 0.2, max: 3, step: 0.01 },
      { p: "half", k: "z", label: "半深 Z (m)", min: 0.2, max: 3, step: 0.01 },
    );
  } else if (z.shape === "circle") {
    f.unshift(...center, { k: "radius", label: "半径 (m)", min: 0.2, max: 3, step: 0.01 });
  }
  return f;
});

function getVal(z, f) {
  const v = f.p ? z[f.p][f.k] : z[f.k];
  return typeof v === "number" ? v : 0;
}

function setVal(z, f, e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch(() => {
    if (f.p) z[f.p][f.k] = v;
    else z[f.k] = v;
    if (z.shape === "rect" && (f.k === "half" || f.p === "half")) {
      z.corners = [
        [-z.half.x, -z.half.z], [z.half.x, -z.half.z],
        [z.half.x, z.half.z], [-z.half.x, z.half.z],
      ];
    }
  });
}

function setShape(z, v) {
  safety.touch(() => { z.shape = v; });
}

function setCorner(z, i, j, e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch(() => { z.corners[i][j] = v; });
}

function setZoneField(z, k, e) {
  const v = e.target.checked;
  safety.touch(() => { z[k] = v; });
}

function setPost(z, k, e) {
  const el = e.target;
  const v = el.type === "checkbox" ? el.checked
    : (el.type === "color" ? el.value : parseFloat(el.value));
  if (typeof v === "number" && !Number.isFinite(v)) return;
  safety.touch(() => { z.posts[k] = v; });
}

// ---------------- 阈值（百分比滑块，双向可调） ----------------
const warnPct = computed(() => Math.round(((cur.value && cur.value.thresholds.warn) || 0.3) * 100));
const dangerPct = computed(() => Math.round(((cur.value && cur.value.thresholds.danger) || 0.1) * 100));
const basis = computed(() => (cur.value && cur.value.thresholds.basis) || "halfwidth");

// ---------------- ★ C3：净空「当前 + 阈值线 + 偏差 + 30s 趋势 + 结论来源」 ----------------
//   方案 §C3 的围栏那一行。三条口径（都有回归钉 tools/verify_param_quad.mjs）：
//   ① **评估不新鲜就不给数**：`safety.last.clearance` 在停摆时会冻在上一帧，
//      照读就是把"页面卡住的 30 秒"显示成实测余量（§5.5 铁律①③）。
//   ② **阈值线只由 basisOf × warn 换算**（`three/safety.js::basisOf` 是唯一实现），
//      不在面板里再算一遍分级基准 —— 两处算法必然漂移。
//   ③ 来源标签写的是**这一路到底量的是谁**（实体机实测 / 残影预演=推断），
//      服务端的独立评估（A1）另有结论，界面上不混为一句。
const CLEARANCE_TREND_MS = 30000;
/** 当前净空来自"最严重的那一面墙/那一个区"—— 阈值线也必须取**同一个区**，否则两边不同源。 */
const clearanceZone = computed(() => {
  const id = safety.last && safety.last.zoneId;
  if (!id || id === "ground") return null;
  return zones.value.find((z) => (z.id || "") === id) || null;
});
const warnLineMm = computed(() => {
  const id = safety.last && safety.last.zoneId;
  if (id === "ground") return groundWarn.value;      // 地面按固定 mm 配置，没有比例基准
  const z = clearanceZone.value || cur.value;
  if (!z) return null;
  const b = basisOf(z);
  const w = z.thresholds && Number.isFinite(z.thresholds.warn) ? z.thresholds.warn : 0.3;
  return Number.isFinite(b) ? Math.round(b * w * 1000) : null;
});
const clearanceMm = computed(() => {
  if (!safety.health || !safety.health.ok) return null;
  const c = safety.last && safety.last.clearance;
  return Number.isFinite(c) ? Math.round(c * 1000) : null;
});
const clearanceSrc = computed(() =>
  (safety.lastSource === "ghost" ? "残影预演（流程推断）" : "实体机实测")
  + (safety.last && safety.last.zoneName ? "·" + safety.last.zoneName : ""));

function setWarn(z, pct) {
  const w = Math.min(99, Math.max(2, pct)) / 100;
  safety.touch(() => {
    z.thresholds.warn = w;
    if (z.thresholds.danger >= w) z.thresholds.danger = Math.max(0.005, w - 0.01);
  });
}

function setDanger(z, pct) {
  const d = Math.min(98, Math.max(1, pct)) / 100;
  safety.touch(() => {
    const w = z.thresholds.warn || 0.3;
    z.thresholds.danger = Math.min(d, Math.max(0.005, w - 0.01));
  });
}

function setBasis(z, v) { safety.touch(() => { z.thresholds.basis = v; }); }

function setFixed(z, e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch(() => { z.thresholds.fixed_mm = Math.min(2000, Math.max(50, v)); });
}

function setColor(z, k, e) {
  const v = e.target.value;
  safety.touch(() => { z.colors[k] = v; });
}

// ---------------- 全局项 ----------------
function setTop(k, e) {
  const el = e.target;
  const v = el.type === "checkbox" ? el.checked : el.value;
  safety.touch((c) => { c[k] = v; });
}

function setBlink(k, e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch((c) => { c.blink[k] = v; });
}

function setAlarm(k, e) {
  const el = e.target;
  const v = el.type === "checkbox" ? el.checked : el.value;
  safety.touch((c) => { c.alarm[k] = v; });
  if (k === "sound") setAlarmEnabled(!!v);
}

function setOverlay(e) {
  const v = e.target.checked;
  safety.touch((c) => { c.overlay.bbox = v; });
}

// ---------------- 全局：四面透明玻璃 + 地面碰撞 ----------------
function setWalls(e) {
  const v = e.target.checked;
  safety.touch((c) => { c.walls = v; });
}

function setGroundEnabled(e) {
  const v = e.target.checked;
  safety.touch((c) => { (c.ground = c.ground || {}).enabled = v; });
}

function setGroundWarn(e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch((c) => {
    const g = (c.ground = c.ground || {});
    g.warn_mm = v;
    if (g.danger_mm > v) g.danger_mm = v;            // 危险 ≤ 接近
    if (g.hit_mm > g.danger_mm) g.hit_mm = g.danger_mm; // 碰撞 ≤ 危险
  });
}

function setGroundDanger(e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch((c) => {
    const g = (c.ground = c.ground || {});
    const warn = Number.isFinite(g.warn_mm) ? g.warn_mm : 100;
    const hit = Number.isFinite(g.hit_mm) ? g.hit_mm : 20;
    g.danger_mm = Math.max(hit, Math.min(v, warn));   // 碰撞 ≤ 危险 ≤ 接近
  });
}

function setGroundHit(e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch((c) => {
    const g = (c.ground = c.ground || {});
    g.hit_mm = v;
    if (g.danger_mm < v) g.danger_mm = v;             // 危险 ≥ 碰撞
    if (g.warn_mm < g.danger_mm) g.warn_mm = g.danger_mm; // 接近 ≥ 危险
  });
}

function setGroundColor(k, e) {
  const v = e.target.value;
  safety.touch((c) => {
    const g = (c.ground = c.ground || {});
    g.colors = g.colors || {};
    g.colors[k] = v;
  });
}

function setGroundOpacity(k, e) {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v)) return;
  safety.touch((c) => {
    const g = (c.ground = c.ground || {});
    g.opacity = g.opacity || {};
    g.opacity[k] = v;
  });
}

function addZone() {
  safety.addZone();
  curIdx.value = safety.zones.length - 1;
}

function removeZone() {
  safety.removeZone(Math.min(curIdx.value, safety.zones.length - 1));
  curIdx.value = Math.max(0, curIdx.value - 1);
}

// ---------------- 报警记录 ----------------
const evStateText = { danger: "危险", hit: "碰撞", clear: "恢复" };
function fmt(ts) { return (ts || "").replace("T", " ").slice(5, 19); }

watch(() => sec.value.events, (v) => { if (v) safety.refreshEvents(); });

// 面板首次展开时拉一次事件列表
watch(open, (v) => { if (v) safety.refreshEvents(); });
</script>

<template>
  <div v-if="cfg && cur" class="card sp">
    <div class="sp-head" @click="open = !open">
      <h3>安全围栏调试</h3>
      <span
        class="sp-state"
        :class="[headState.cls, headCls]"
        :style="headVars"
        :title="safety.health.notice || '评估实时'"
      >{{ headMark ? headMark + " " : "" }}{{ headState.text }}{{ headSuffix }}</span>
      <span v-if="safety.last.zoneName" class="sp-zone">{{ safety.last.zoneName }}</span>
      <span v-if="safety.dirty" class="sp-dot" title="有未保存改动"></span>
      <button class="hud-toggle">{{ open ? "−" : "+" }}</button>
    </div>

    <div v-show="open" class="sp-body">
      <!-- 总开关 + 区域选择 -->
      <div class="sp-line">
        <label>围栏总开关</label>
        <input type="checkbox" :checked="cfg.enabled" @change="setTop('enabled', $event)" />
      </div>
      <div class="sp-line">
        <label>区域</label>
        <select v-model.number="curIdx" class="sp-sel">
          <option v-for="(z, i) in zones" :key="z.id" :value="i">{{ z.name }}</option>
        </select>
        <button class="sp-mini" @click="addZone">＋</button>
        <button class="sp-mini" :disabled="zones.length <= 1" @click="removeZone">－</button>
      </div>
      <div class="sp-line">
        <label>本区启用</label>
        <input type="checkbox" :checked="cur.enabled" @change="setZoneField(cur, 'enabled', $event)" />
      </div>
      <div class="sp-line">
        <label>区域名</label>
        <input type="text" class="sp-txt" :value="cur.name"
               @change="safety.touch(() => { cur.name = $event.target.value || cur.id; })" />
      </div>

      <!-- 形状 -->
      <div class="sp-sec">
        <div class="sp-sec-h" @click="sec.shape = !sec.shape">形状与尺寸 <Icon :name="sec.shape ? 'chevronDown' : 'chevronRight'" :size="14" /></div>
        <div v-show="sec.shape">
          <div class="btns" style="margin:8px 0">
            <button :class="{ primary: cur.shape === 'rect' }" @click="setShape(cur, 'rect')">矩形</button>
            <button :class="{ primary: cur.shape === 'quad' }" @click="setShape(cur, 'quad')">四点</button>
            <button :class="{ primary: cur.shape === 'circle' }" @click="setShape(cur, 'circle')">圆形</button>
          </div>
          <div v-for="f in zoneFields" :key="f.label" class="fld">
            <label>{{ f.label }}</label>
            <input type="range" :min="f.min" :max="f.max" :step="f.step"
                   :value="getVal(cur, f)" @input="setVal(cur, f, $event)" />
            <input type="number" :min="f.min" :max="f.max" :step="f.step"
                   :value="getVal(cur, f)" @change="setVal(cur, f, $event)" />
          </div>
          <div v-if="cur.shape === 'quad'" class="quad">
            <div v-for="(p, i) in cur.corners" :key="'c' + i" class="quad-p">
              <span>P{{ i + 1 }}</span>
              <input type="number" step="0.01" :value="p[0]" @change="setCorner(cur, i, 0, $event)" />
              <input type="number" step="0.01" :value="p[1]" @change="setCorner(cur, i, 1, $event)" />
            </div>
            <div class="small">四个角按 (X, Z) 输入，顺时针或逆时针均可（法线自动取内法线）。</div>
          </div>
          <div class="sp-line">
            <label>围栏面板</label>
            <input type="checkbox" :checked="cur.walls" @change="setZoneField(cur, 'walls', $event)" />
          </div>
          <div class="sp-line">
            <label>角柱</label>
            <input type="checkbox" :checked="cur.posts.enabled" @change="setPost(cur, 'enabled', $event)" />
            <input type="range" min="0.05" max="0.3" step="0.01" :value="cur.posts.size"
                   @input="setPost(cur, 'size', $event)" />
            <input type="color" :value="cur.posts.color" @input="setPost(cur, 'color', $event)" />
          </div>
        </div>
      </div>

      <!-- 阈值 -->
      <div class="sp-sec">
        <div class="sp-sec-h" @click="sec.thr = !sec.thr">接近 / 远离 阈值 <Icon :name="sec.thr ? 'chevronDown' : 'chevronRight'" :size="14" /></div>
        <div v-show="sec.thr">
          <div class="fld">
            <label>接近告警 {{ warnPct }}%</label>
            <input type="range" min="2" max="99" step="1" :value="warnPct"
                   @input="setWarn(cur, +$event.target.value)" />
          </div>
          <div class="fld">
            <label>危险告警 {{ dangerPct }}%</label>
            <input type="range" min="1" max="98" step="1" :value="dangerPct"
                   @input="setDanger(cur, +$event.target.value)" />
          </div>
          <div class="sp-line">
            <label>基准</label>
            <select class="sp-sel" :value="basis" @change="setBasis(cur, $event.target.value)">
              <option value="halfwidth">围栏半宽比例</option>
              <option value="fixed">固定毫米</option>
            </select>
          </div>
          <div v-if="basis === 'fixed'" class="sp-line">
            <label>固定距离 (mm)</label>
            <input type="number" min="50" max="2000" step="10" :value="cur.thresholds.fixed_mm"
                   @change="setFixed(cur, $event)" />
          </div>
          <div class="small">调大 = 更早报警（更"敏感"）；调小 = 更晚报警。</div>
          <!-- ★★ C3（方案 §C3 围栏那一行）：净空 + 阈值线 + 偏差 + 最近 30s 趋势 + 结论来源。
               「当前」在评估停摆时**如实是未知**（冻结的上一帧不是实测余量）；
               趋势采的是全局最严重那一处，与上方阈值线**同一个区**（同源，不各算一遍）。 -->
          <ParamQuad class="pq-clear" label="当前净空"
                     :current="clearanceMm" :target="warnLineMm"
                     :cur-source="clearanceSrc" tgt-source="该区接近告警线"
                     unit="mm" :decimals="0"
                     trend-key="clearance" :trend-window-ms="CLEARANCE_TREND_MS"
                     :as-of-now="safety.clk" :stale="!safety.health || !safety.health.ok"
                     target-null-text="未设阈值" />
          <div class="small muted">服务端另有独立评估（互锁与 safety.server_* 报警用它），
            本行是<b>界面这一路</b>的读数。</div>
        </div>
      </div>

      <!-- 颜色与闪烁 -->
      <div class="sp-sec">
        <div class="sp-sec-h" @click="sec.color = !sec.color">颜色 / 闪烁 / 叠加 <Icon :name="sec.color ? 'chevronDown' : 'chevronRight'" :size="14" /></div>
        <div v-show="sec.color">
          <div class="sp-colors">
            <span v-for="k in ['safe', 'warn', 'danger', 'hit']" :key="k" class="sp-c">
              <input type="color" :value="cur.colors[k]" @input="setColor(cur, k, $event)" />
              <em>{{ { safe: "安全", warn: "接近", danger: "危险", hit: "碰撞" }[k] }}</em>
            </span>
          </div>
          <div class="fld">
            <label>闪烁 {{ cfg.blink.hz }} Hz</label>
            <input type="range" min="0.5" max="10" step="0.5" :value="cfg.blink.hz"
                   @input="setBlink('hz', $event)" />
          </div>
          <div class="fld">
            <label>闪烁下限 {{ cfg.blink.min }}</label>
            <input type="range" min="0.02" max="0.9" step="0.01" :value="cfg.blink.min"
                   @input="setBlink('min', $event)" />
          </div>
          <div class="fld">
            <label>闪烁上限 {{ cfg.blink.max }}</label>
            <input type="range" min="0.02" max="0.9" step="0.01" :value="cfg.blink.max"
                   @input="setBlink('max', $event)" />
          </div>
          <div class="sp-line">
            <label>包围盒线框</label>
            <input type="checkbox" :checked="cfg.overlay.bbox" @change="setOverlay($event)" />
          </div>
        </div>
      </div>

      <!-- 全局围栏与碰撞 -->
      <div class="sp-sec">
        <div class="sp-sec-h" @click="sec.global = !sec.global">全局围栏与碰撞 <Icon :name="sec.global ? 'chevronDown' : 'chevronRight'" :size="14" /></div>
        <div v-show="sec.global">
          <div class="sp-line">
            <label>四面透明玻璃</label>
            <input type="checkbox" :checked="wallsOn" @change="setWalls($event)" />
          </div>
          <div class="small" style="margin:6px 0">关闭后隐藏围栏面板，仍保留线框与角柱，区域边界清晰可见。</div>
          <div class="sp-line">
            <label>地面碰撞检测</label>
            <input type="checkbox" :checked="groundEnabled" @change="setGroundEnabled($event)" />
          </div>
          <div class="fld">
            <label>地面接近 (mm)</label>
            <input type="range" :min="0" :max="1000" :step="5"
                   :value="groundWarn" @input="setGroundWarn($event)" />
            <input type="number" :min="0" :max="1000" :step="5"
                   :value="groundWarn" @change="setGroundWarn($event)" />
          </div>
          <div class="fld">
            <label>地面危险 (mm)</label>
            <input type="range" :min="0" :max="1000" :step="5"
                   :value="groundDanger" @input="setGroundDanger($event)" />
            <input type="number" :min="0" :max="1000" :step="5"
                   :value="groundDanger" @change="setGroundDanger($event)" />
          </div>
          <div class="fld">
            <label>地面碰撞 (mm)</label>
            <input type="range" :min="0" :max="500" :step="1"
                   :value="groundHit" @input="setGroundHit($event)" />
            <input type="number" :min="0" :max="500" :step="1"
                   :value="groundHit" @change="setGroundHit($event)" />
          </div>
          <div class="small">
            按 J1 以上子树最低点的离台面高度分级：≤ 接近 → 黄；≤ 危险 → 红；≤ 碰撞 → 红闪。
            三级自动保持 碰撞 ≤ 危险 ≤ 接近。
          </div>

          <div class="sp-hr"></div>
          <div class="small" style="margin:6px 0">
            <b>地面报警垫</b>铺满「四个角柱围成的那块地面」（跟随主工作区形状，
            矩形 / 正方形 / 四边形 / 圆形都跟随），随上面四级实时变色。
            机器人站在站台上时，垫子自动改铺到台面上。
          </div>
          <div class="sp-line">
            <label>地面报警颜色</label>
            <span class="sp-colors">
              <span v-for="s in GROUND_STATES" :key="'gc' + s.k" class="sp-c">
                <input type="color" :value="groundColors[s.k]" @input="setGroundColor(s.k, $event)" />
                <em>{{ s.t }}</em>
              </span>
            </span>
          </div>
          <div class="small">地面报警垫随状态变色（与围栏面板同套配色）：安全(绿) / 接近(黄) / 危险(红) / 碰撞(红闪)。</div>
          <div class="fld" v-for="s in GROUND_STATES" :key="'go' + s.k">
            <label>地面 {{ s.t }} 透明度</label>
            <input type="range" min="0.02" max="0.9" step="0.01"
                   :value="groundOpacity[s.k]" @input="setGroundOpacity(s.k, $event)" />
          </div>
        </div>
      </div>

      <!-- 报警 -->
      <div class="sp-sec">
        <div class="sp-sec-h" @click="sec.alarm = !sec.alarm">报警与视角 <Icon :name="sec.alarm ? 'chevronDown' : 'chevronRight'" :size="14" /></div>
        <div v-show="sec.alarm">
          <div class="sp-line">
            <label>顶部报警条</label>
            <input type="checkbox" :checked="cfg.alarm.banner" @change="setAlarm('banner', $event)" />
          </div>
          <div class="sp-line">
            <label>底部状态片</label>
            <input type="checkbox" :checked="cfg.alarm.chip" @change="setAlarm('chip', $event)" />
          </div>
          <div class="sp-line">
            <label>报警条最低级别</label>
            <select class="sp-sel" :value="cfg.alarm.banner_min"
                    @change="setAlarm('banner_min', $event)">
              <option value="warn">接近</option>
              <option value="danger">危险</option>
              <option value="hit">碰撞</option>
            </select>
          </div>
          <div class="sp-line">
            <label>声音报警</label>
            <input type="checkbox" :checked="cfg.alarm.sound" @change="setAlarm('sound', $event)" />
          </div>
          <div class="btns" style="margin-top:8px">
            <button @click="safety.saveCamera()"><Icon name="snapshot" :size="15" /> 保存视角</button>
            <button @click="safety.resetCamera()"><Icon name="refresh" :size="15" /> 复位视角</button>
          </div>
        </div>
      </div>

      <!-- 报警记录 -->
      <div class="sp-sec">
        <div class="sp-sec-h" @click="sec.events = !sec.events">报警记录 ({{ safety.events.length }}) <Icon :name="sec.events ? 'chevronDown' : 'chevronRight'" :size="14" /></div>
        <div v-show="sec.events">
          <div class="btns" style="margin:8px 0">
            <button @click="safety.refreshEvents()"><Icon name="refresh" :size="15" /> 刷新</button>
            <button @click="safety.clearEvents()"><Icon name="trash" :size="15" /> 清空</button>
          </div>
          <div class="ev-list">
            <div v-if="!safety.events.length" class="rec-empty">暂无报警记录</div>
            <div v-for="e in safety.events" :key="e.id" class="ev-item" :class="e.state">
              <span class="ev-t">{{ fmt(e.timestamp) }}</span>
              <span class="ev-s">{{ evStateText[e.state] || e.state }}</span>
              <span class="ev-n">{{ e.zone_name || "—" }}</span>
              <span class="ev-c">{{ (e.clearance * 1000).toFixed(0) }} mm</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 保存 / 重置 / 撤销 -->
      <div class="btns" style="margin-top:10px">
        <button class="primary" :disabled="safety.busy" @click="safety.save()">
          <Icon v-if="safety.dirty" name="alert" :size="14" /> 保存
        </button>
        <button :disabled="!safety.dirty" @click="safety.revert()">撤销</button>
        <button :disabled="safety.busy" @click="safety.reset()">重置</button>
      </div>
      <div v-if="safety.error" class="sp-err">{{ safety.error }}</div>
      <div class="small" style="margin-top:6px">拖动即时生效；「保存」写入 config/safety.json（刷新后仍在）。</div>
    </div>
  </div>
</template>

<style scoped>
/* =====================================================================
   C1 高性能画面模式：只覆盖顶部状态徽标的**着色出口**。
   ★ 所有规则都带 .hmi-hi-perf —— 该类只在模式开启时挂上 ⇒ 默认（关）时
     以下一条都不命中，徽标外观与改动前完全一致（非破坏式）。
   ★ 这里不写任何颜色字面量：值全部来自 styleVarsFor()（唯一出处 = utils/colorSemantics.js）。
   ★ 围栏 3D 配色（「颜色 / 闪烁」那一节的取色器）属于**业务配置**（写进 safety.json、
     由 three/safety.js 用来画围栏面板与地面报警垫），不是界面装饰色，**不参与灰阶**。
   ===================================================================== */
.sp-state.hmi-hi-perf {
  color: var(--hmi-fg);
  background-color: var(--hmi-bg);
  background-image: var(--hmi-bg-image);
  border-width: var(--hmi-border-width);
  border-style: var(--hmi-border-style);
  border-color: var(--hmi-border);
}
</style>
