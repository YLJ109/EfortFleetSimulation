﻿<script setup>
// =====================================================================
// 「系统设置」页：config/robot.yaml 的界面化编辑（走 config/app_settings.json 覆盖层）。
//
// ## 这一页要回答的三个问题
//   1. "这个值现在到底是多少、哪来的？" → 每字段带来源徽标（yaml / settings / env / derived）
//   2. "我改了为什么没用？" → 每字段带生效方式徽标（live / reload / reconnect / restart），
//      并单独列出被环境变量顶掉、界面上改不了的项
//   3. "改坏了怎么办？" → 单字段「恢复出厂」+ 整页「重置所有参数」
//
// ## 为什么不实时保存
//   输入框改的是 store 里的**草稿**，点「保存」才落盘。理由：改 connection.host 会自动
//   触发断线重连 —— 拖动数字框就改真实配置，现场会看到"我就摸了一下界面，机器人掉线了"。
//   所以拆成 保存（只落盘）→ 立即生效（reload / reconnect）两步，出错时能分清是哪一步。
//
// ★ 本页不碰机器人运动：只改参数，不产生任何运动指令（后端 api/settings.py 里没有任何
//   motion.command 调用）。「测试连接」也只做 TCP 握手与相机服务 /status 探测。
// ★ 用 admin 口令拿到的操作员令牌改不了标了「需管理员」的项 —— 后端**逐字段整批拒绝**，
//   所以这里提前置灰，不让用户白填一遍再被 403。
// =====================================================================
import { computed, onMounted, reactive, ref } from "vue";
import Icon from "./Icon.vue";
import { useSettingsStore } from "../stores/settings.js";
import { useAuthStore } from "../stores/auth.js";
import { useUiStore } from "../stores/ui.js";
import { apiUrl } from "../config.js";

const st = useSettingsStore();
const auth = useAuthStore();
const ui = useUiStore();

// ---- 分组图标（SCHEMA 只给 id/label/desc，图标属于展示层） ----
const GROUP_ICON = {
  robot: "robot", net: "wifi", modbus: "plug", axes: "sliders",
  sampling: "activity", camera: "camera", motion: "target", jog: "grip",
  server: "server", database: "database",
};
const iconOf = (id) => GROUP_ICON[id] || "gear";

// ---- 来源徽标 ----
const SOURCE_META = {
  yaml: { text: "yaml", cls: "yaml", title: "取自 config/robot.yaml（出厂值）" },
  settings: { text: "settings", cls: "settings", title: "被界面改过，存在 config/app_settings.json 覆盖层里" },
  env: { text: "env", cls: "env", title: "被环境变量顶掉，界面上改不了（改 .env 后重启）" },
  derived: { text: "自动", cls: "derived", title: "由其它字段合成，不是独立配置项" },
  default: { text: "默认", cls: "default", title: "yaml 里没有，用的是代码内置默认值" },
};
const sourceOf = (f) => SOURCE_META[f.source] || SOURCE_META.default;

// ---- 生效方式徽标 ----
const APPLY_META = {
  live: { text: "立即", cls: "live", title: "每次用到都重新读配置，保存即生效" },
  reload: { text: "重载", cls: "reload", title: "需要「立即生效」重读配置（清缓存）" },
  reconnect: { text: "重连", cls: "reconnect", title: "需要「立即生效」断开并重连控制器" },
  restart: { text: "需重启", cls: "restart", title: "★ 后端不能重启自己，必须手动重启后端进程" },
};
const applyOf = (f) => APPLY_META[f.apply] || APPLY_META.reload;

/** 字段是否能改：只读项不行；env 锁定的不行；需管理员而当前不是管理员的不行。 */
function lockedReason(f) {
  if (f.editable === false) return f.reason || "该项为只读";
  if (f.source === "env") return `被环境变量 ${f.env || ""} 顶掉，改 .env 后重启后端`;
  if (f.admin && !st.isAdmin) return st.role ? "需要管理员权限（当前是操作员）" : "需要先获取控制权限";
  return "";
}
const isLocked = (f) => !!lockedReason(f);

// ---- 值的展示 ----
function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "开" : "关";
  if (Array.isArray(v)) {
    if (v.every((x) => typeof x !== "object")) return v.join(", ");
    return v.map((x, i) => `J${i + 1}[${x.min}, ${x.max}]`).join("  ");
  }
  return String(v);
}

// ---- 草稿读写 ----
function valOf(f) {
  const v = st.draftOf(f.key);
  return v === undefined || v === null ? "" : v;
}

function setNum(f, ev) {
  const raw = ev.target.value;
  st.setValue(f.key, raw === "" ? "" : Number(raw));
}

function toggleBool(f) {
  st.setValue(f.key, !st.draftOf(f.key));
}

function cycleSign(f, i) {
  const cur = (st.draftOf(f.key) || []).slice();
  const base = Array.isArray(cur) && cur.length === 6 ? cur : [1, 1, 1, 1, 1, 1];
  base[i] = base[i] === 1 ? -1 : 1;
  st.setValue(f.key, base);
}

function setLimit(f, i, which, ev) {
  const cur = (st.draftOf(f.key) || []).slice();
  const base = Array.isArray(cur) && cur.length === 6
    ? cur.map((x, j) => ({ name: x.name || `J${j + 1}`, min: x.min, max: x.max }))
    : [0, 1, 2, 3, 4, 5].map((j) => ({ name: `J${j + 1}`, min: -170, max: 170 }));
  const raw = ev.target.value;
  base[i][which] = raw === "" ? "" : Number(raw);
  st.setValue(f.key, base);
}

function signList(f) {
  const cur = st.draftOf(f.key);
  return Array.isArray(cur) && cur.length === 6 ? cur : [1, 1, 1, 1, 1, 1];
}

function limitList(f) {
  const cur = st.draftOf(f.key);
  return Array.isArray(cur) && cur.length === 6
    ? cur.map((x, j) => ({ name: x.name || `J${j + 1}`, min: x.min, max: x.max }))
    : [0, 1, 2, 3, 4, 5].map((j) => ({ name: `J${j + 1}`, min: -170, max: 170 }));
}

/** 草稿里有没有这一项（用来显示"已改"小点）。 */
const isDirty = (f) => st.dirtyKeys.includes(f.key);

// ---- 分组导航 ----
function goGroup(id) {
  const el = document.getElementById("stg-" + id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---- 测试连接 ----
const testMsg = ref("");
const testCls = computed(() => {
  const r = st.testResult;
  if (!r) return "";
  return r.ok ? "ok" : "err";
});

/** 机器人：用草稿里的 IP/端口（没改过就用生效值）先试，再决定要不要保存。 */
async function testRobot() {
  const host = st.draftOf("connection.host") || st.draftOf("modbus.host");
  const port = st.draftOf("modbus.port") || st.draftOf("connection.port");
  testMsg.value = "";
  const r = await st.testConnection("robot", host, port ? Number(port) : null);
  testMsg.value = r ? r.message || "" : "";
}

/** 相机服务：同理，用草稿里的 camera.host / camera.port。 */
async function testCamera() {
  const host = st.draftOf("camera.host");
  const port = st.draftOf("camera.port");
  testMsg.value = "";
  const r = await st.testConnection("camera", host, port ? Number(port) : null);
  testMsg.value = r ? r.message || "" : "";
}

// ---- 保存 / 生效 / 放弃 ----
const saveMsg = ref("");
async function doSave() {
  saveMsg.value = "";
  const ok = await st.save();
  if (ok) {
    const n = (st.result && st.result.changed ? st.result.changed.length : 0);
    saveMsg.value = n ? `已保存 ${n} 项改动` : "没有变化";
  }
}

async function doApply() {
  saveMsg.value = "";
  const acts = st.pendingApply.length ? st.pendingApply : ["reload", "reconnect"];
  const ok = await st.apply(acts);
  if (ok) {
    const d = st.applyResult || {};
    const notes = (d.notes || []).join("；");
    saveMsg.value = "已生效：" + (d.actions || []).join(" / ") + (notes ? " —— " + notes : "");
  }
}

function doDiscard() {
  st.discardAll();
  saveMsg.value = "";
}

// ---- 重置所有参数 ----
const resetConfirm = ref(false);
const resetMsg = ref("");
async function doReset() {
  resetMsg.value = "";
  const ok = await st.resetAll();
  if (ok) {
    resetConfirm.value = false;
    resetMsg.value = `已重置全部参数（${(st.result && st.result.count) || 0} 项恢复出厂值）`;
  }
}

// ---- 修改口令 ----
const pw = reactive({ role: "admin", current: "", next: "", again: "" });
const pwMsg = ref("");
const pwErr = ref("");
async function doChangePassword() {
  pwErr.value = "";
  pwMsg.value = "";
  if (pw.next.length < 4) { pwErr.value = "新口令至少 4 位"; return; }
  if (pw.next !== pw.again) { pwErr.value = "两次输入的新口令不一致"; return; }
  if (!pw.current) { pwErr.value = "请输入当前口令"; return; }
  const ok = await st.changePassword(pw.role, pw.current, pw.next);
  if (ok) {
    pw.current = ""; pw.next = ""; pw.again = "";
    pwMsg.value = (st.applyResult && st.applyResult.note) || "口令已更新";
  } else {
    pwErr.value = st.error || "修改失败";
  }
}

// ---- 控制权限时长 ----
// 默认不限时（后端 TTL=0）：拿到权限后一直有效，直到后端重启或主动登出。
// 令牌只存在于后端进程内存 —— 所以无论设多长，重启后端都必须重新获取权限。
const TTL_OPTIONS = [
  { v: 0, label: "不限时（推荐）" },
  { v: 1800, label: "30 分钟" },
  { v: 7200, label: "2 小时" },
  { v: 28800, label: "8 小时" },
];
const ttlSel = ref(0);
const ttlCustom = ref("");
const ttlCurrent = ref(null);     // 后端当前生效值（null = 还没拉到）
const ttlMsg = ref("");
const ttlErr = ref("");
const ttlBusy = ref(false);

function ttlLabel(sec) {
  if (sec === 0) return "不限时";
  return sec >= 60 ? Math.round(sec / 60) + " 分钟" : sec + " 秒";
}
async function loadTtl() {
  try {
    const r = await fetch(apiUrl("/auth/status"));
    if (r.ok) {
      const d = await r.json();
      ttlCurrent.value = typeof d.ttl === "number" ? d.ttl : 0;
      ttlSel.value = ttlCurrent.value;
    }
  } catch (e) { /* 后端不可用时静默 */ }
}
async function applyTtl() {
  ttlErr.value = ""; ttlMsg.value = "";
  let sec;
  if (ttlSel.value === -1) {
    const mins = Number(ttlCustom.value);
    if (!(mins > 0)) { ttlErr.value = "请输入大于 0 的分钟数"; return; }
    sec = Math.round(mins * 60);
  } else {
    sec = ttlSel.value;
  }
  ttlBusy.value = true;
  try {
    const r = await fetch(apiUrl("/settings/control-ttl"), {
      method: "POST",
      headers: { "Content-Type": "application/json", ...auth.controlHeaders() },
      body: JSON.stringify({ ttl_sec: sec }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.status === 401 || r.status === 403) { ttlErr.value = "该设置需要管理员权限"; return; }
    if (!r.ok) { ttlErr.value = d.message || d.detail || ("设置失败（" + r.status + "）"); return; }
    ttlCurrent.value = d.ttl;
    ttlSel.value = d.ttl;
    // ★ 双向绑定：立刻同步到全局 auth store —— 顶栏/登录弹窗里的"有效期 X"
    //   马上跟着变，不必等下一次 /auth/status 回来。
    auth.cfgTtl = d.ttl;
    ttlMsg.value = d.note || "已生效";
  } catch (e) {
    ttlErr.value = "设置失败：" + e.message;
  } finally {
    ttlBusy.value = false;
  }
}
loadTtl();

// ---- 实时信息小卡（不是配置，是"此刻连上了什么"） ----
const liveCards = computed(() => {
  const l = st.live;
  if (!l) return [];
  return [
    { icon: "robot", label: "机器人链路", main: l.robot.label, sub: `${l.robot.host}:${l.robot.port} · unit ${l.robot.unit_id}`, level: l.robot.level },
    { icon: "wifi", label: "网口握手", main: l.robot.net_ok ? "TCP 可达" : "TCP 不可达", sub: l.robot.net_ok ? `${l.robot.host}:${l.robot.port}` : (l.robot.net_error || ""), level: l.robot.net_ok ? "ok" : "err" },
    { icon: "camera", label: "相机服务", main: l.camera.label, sub: l.camera.base, level: l.camera.level },
    { icon: "database", label: "数据库", main: l.database.ok ? `${l.database.size_kb} KB` : "不可用", sub: l.database.url, level: l.database.ok ? "ok" : "err" },
    { icon: "server", label: "本服务", main: `v${l.service.version}`, sub: `${l.service.host}:${l.service.port} · 已运行 ${l.service.uptime_sec}s`, level: "ok" },
    { icon: "target", label: "真实下发", main: l.runtime.real_motion_active ? "已启用" : "未启用（模拟）", sub: `env=${l.runtime.real_motion_env ? 1 : 0} · real_write=${l.runtime.real_write ? "true" : "false"}`, level: l.runtime.real_motion_active ? "err" : "ok" },
  ];
});

onMounted(() => {
  st.load().then(() => { if (st.isAdmin) st.loadLive(); });
});
</script>

<template>
  <div class="stg">
    <!-- ===== 顶部：状态 + 保存条 ===== -->
    <div class="stg-top">
      <div class="stg-top-l">
        <span class="stg-mark"><Icon name="gear" :size="20" /></span>
        <div>
          <div class="stg-title">系统设置</div>
          <div class="stg-sub">
            参数写入覆盖层 <code>config/app_settings.json</code>；<code>robot.yaml</code> 保持只读。
            当前
            <b>已改 {{ st.overridden }} 项</b>
            <span class="stg-dot">·</span>
            {{ st.isAdmin ? "管理员令牌" : (st.role ? "操作员令牌" : "未授权（只读）") }}
          </div>
        </div>
      </div>
      <div class="stg-top-r">
        <button class="stg-btn" :disabled="st.loading" @click="st.load()">
          <Icon name="refresh" :size="14" :class="{ spin: st.loading }" /> 重新读取
        </button>
      </div>
    </div>

    <!-- ===== 未保存改动条 ===== -->
    <div v-if="st.dirtyCount" class="stg-bar">
      <span class="stg-bar-n"><Icon name="edit" :size="14" /> 有 {{ st.dirtyCount }} 项未保存</span>
      <span class="stg-bar-apply">
        保存后需要：
        <span v-for="a in st.pendingApply" :key="a" class="stg-ap" :class="a">{{ applyOf({ apply: a }).text }}</span>
      </span>
      <span v-if="st.pendingRestart" class="stg-bar-warn">
        <Icon name="alert" :size="13" /> 含需重启项，保存后请手动重启后端
      </span>
      <div class="stg-bar-btns">
        <button class="stg-btn" :disabled="st.saving" @click="doDiscard">放弃改动</button>
        <button class="stg-btn primary" :disabled="st.saving" @click="doSave">
          <Icon v-if="st.saving" name="refresh" :size="14" class="spin" />
          {{ st.saving ? "处理中" : "保存" }}
        </button>
        <button class="stg-btn" :disabled="st.saving" @click="doApply" title="重读配置并重连控制器">
          <Icon name="plug" :size="14" /> 立即生效
        </button>
      </div>
    </div>

    <!-- ===== 结果 / 错误 ===== -->
    <div v-if="st.error" class="stg-msg err"><Icon name="alert" :size="14" /> {{ st.error }}</div>
    <div v-else-if="saveMsg" class="stg-msg ok"><Icon name="check" :size="14" /> {{ saveMsg }}</div>
    <div v-if="st.applyResult && st.applyResult.notes && st.applyResult.notes.length" class="stg-msg warn">
      <Icon name="alert" :size="14" /> {{ st.applyResult.notes.join("；") }}
    </div>

    <!-- ===== env 锁定提示 ===== -->
    <div v-if="st.envLocked.length" class="stg-env">
      <div class="stg-env-h"><Icon name="alert" :size="14" /> 以下项被环境变量顶掉，界面上改不了</div>
      <div class="stg-env-list">
        <span v-for="f in st.envLocked" :key="f.key" class="stg-env-item">
          <b>{{ f.label }}</b> ← <code>{{ f.env }}</code>
        </span>
      </div>
      <p class="stg-env-tip">改法：编辑项目根目录 <code>.env</code> 里的对应行，然后重启后端进程。</p>
    </div>

    <!-- ===== 实时信息卡 ===== -->
    <!-- ★ St3：实时信息只在**管理员**时加载 —— 非管理员时不再"整块消失"（会造成同一页不同角色
         布局差异大、像是少了一块）。这里明确给一行占位说明。 -->
    <div v-if="liveCards.length" class="stg-cards">
      <div v-for="c in liveCards" :key="c.label" class="stg-card" :class="c.level">
        <div class="stg-card-h"><Icon :name="c.icon" :size="14" /> {{ c.label }}</div>
        <div class="stg-card-m">{{ c.main }}</div>
        <div class="stg-card-s">{{ c.sub }}</div>
      </div>
    </div>
    <div v-else-if="!st.isAdmin" class="stg-msg">
      <Icon name="lock" :size="14" /> 实时信息（链路 / 相机 / 数据库 / 真实下发）需<b>管理员</b>权限，当前{{ st.role ? "为操作员" : "未授权" }}。
    </div>

    <!-- ===== 分组导航 ===== -->
    <div class="stg-nav">
      <button v-for="g in st.groups" :key="g.id" class="stg-nav-chip" @click="goGroup(g.id)">
        <Icon :name="iconOf(g.id)" :size="12" /> {{ g.label }}
        <span class="stg-nav-n">{{ g.fields.filter((f) => st.overlayKeys.includes(f.key)).length || "" }}</span>
      </button>
    </div>

    <!-- ===== 分组字段 ===== -->
    <div v-for="g in st.groups" :key="g.id" class="stg-group" :id="'stg-' + g.id">
      <div class="stg-group-h">
        <Icon :name="iconOf(g.id)" :size="16" />
        <b>{{ g.label }}</b>
        <span v-if="g.admin" class="stg-tag admin"><Icon name="lock" :size="11" /> 含管理员项</span>
        <span v-if="g.id === 'motion'" class="stg-tag danger"><Icon name="alert" :size="11" /> 高风险区</span>
        <!-- 分组级测试连接：只在有意义的组上出现 -->
        <button v-if="g.id === 'net'" class="stg-btn sm" :disabled="st.testing" @click="testRobot">
          <Icon name="plug" :size="12" /> 测试连接（机器人）
        </button>
        <button v-if="g.id === 'camera'" class="stg-btn sm" :disabled="st.testing" @click="testCamera">
          <Icon name="plug" :size="12" /> 测试连接（相机服务）
        </button>
      </div>
      <p v-if="g.desc" class="stg-group-d">{{ g.desc }}</p>

      <div class="stg-fields">
        <div v-for="f in g.fields" :key="f.key" class="stg-field" :class="{ locked: isLocked(f), dirty: isDirty(f) }">
          <!-- 行首：标签 + 徽标 -->
          <div class="stg-f-head">
            <span class="stg-f-label">
              {{ f.label }}
              <span v-if="isDirty(f)" class="stg-f-dot" title="草稿已修改，未保存"></span>
            </span>
            <span class="stg-badge" :class="sourceOf(f).cls" :title="sourceOf(f).title">{{ sourceOf(f).text }}</span>
            <span class="stg-badge" :class="applyOf(f).cls" :title="applyOf(f).title">{{ applyOf(f).text }}</span>
            <span v-if="f.admin" class="stg-badge admin" title="只有管理员令牌能修改这一项">
              <Icon name="lock" :size="10" /> 管理员
            </span>
          </div>

          <!-- 控件区 -->
          <div class="stg-f-ctl">
            <template v-if="f.editable === false">
              <div class="stg-ro">
                <code>{{ fmtVal(f.value) }}</code>
                <span v-if="f.reason" class="stg-ro-r">{{ f.reason }}</span>
              </div>
            </template>

            <template v-else-if="f.type === 'bool'">
              <button class="stg-sw" :class="{ on: !!st.draftOf(f.key) }"
                      :disabled="isLocked(f)" @click="toggleBool(f)">
                <span class="stg-sw-k"></span>
                <span class="stg-sw-t">{{ st.draftOf(f.key) ? "开" : "关" }}</span>
              </button>
            </template>

            <template v-else-if="f.type === 'enum'">
              <select class="stg-in" :value="valOf(f)" :disabled="isLocked(f)"
                      @change="st.setValue(f.key, $event.target.value)">
                <option v-for="o in f.options" :key="o" :value="o">{{ o }}</option>
              </select>
            </template>

            <template v-else-if="f.type === 'signs6'">
              <div class="stg-signs">
                <button v-for="(s, i) in signList(f)" :key="i" class="stg-sign"
                        :class="{ neg: s === -1 }" :disabled="isLocked(f)" @click="cycleSign(f, i)">
                  <b>J{{ i + 1 }}</b><span>{{ s > 0 ? "+1" : "−1" }}</span>
                </button>
              </div>
            </template>

            <template v-else-if="f.type === 'limits6'">
              <div class="stg-limits">
                <div v-for="(l, i) in limitList(f)" :key="i" class="stg-limit">
                  <span class="stg-limit-n">{{ l.name }}</span>
                  <input class="stg-in num" type="number" step="1" :value="l.min" :disabled="isLocked(f)"
                         @input="setLimit(f, i, 'min', $event)" />
                  <span class="stg-limit-sep">~</span>
                  <input class="stg-in num" type="number" step="1" :value="l.max" :disabled="isLocked(f)"
                         @input="setLimit(f, i, 'max', $event)" />
                  <span class="stg-unit">°</span>
                </div>
              </div>
            </template>

            <template v-else-if="f.type === 'text'">
              <textarea class="stg-in ta" rows="2" :maxlength="f.maxlen || undefined"
                        :value="valOf(f)" :disabled="isLocked(f)"
                        @input="st.setValue(f.key, $event.target.value)"></textarea>
            </template>

            <template v-else-if="f.type === 'int' || f.type === 'float'">
              <div class="stg-num">
                <input class="stg-in num" type="number" :step="f.type === 'int' ? 1 : 'any'"
                       :min="f.min" :max="f.max" :value="valOf(f)" :disabled="isLocked(f)"
                       @input="setNum(f, $event)" />
                <span v-if="f.unit" class="stg-unit">{{ f.unit }}</span>
                <span v-if="f.min !== null || f.max !== null" class="stg-range">
                  {{ f.min !== null ? f.min : "−∞" }} ~ {{ f.max !== null ? f.max : "+∞" }}
                </span>
              </div>
            </template>

            <template v-else>
              <input class="stg-in" :type="f.type === 'ip' ? 'text' : 'text'"
                     :maxlength="f.maxlen || undefined" :value="valOf(f)" :disabled="isLocked(f)"
                     :placeholder="f.type === 'ip' ? '192.168.1.12' : ''"
                     @input="st.setValue(f.key, $event.target.value)" />
            </template>

            <span class="stg-key" :title="'配置路径：' + f.key"><code>{{ f.key }}</code></span>
          </div>

          <!-- 说明 / 锁定原因 -->
          <div v-if="isLocked(f)" class="stg-f-lock"><Icon name="lock" :size="11" /> {{ lockedReason(f) }}</div>
          <div v-else-if="f.help" class="stg-f-help">{{ f.help }}</div>
        </div>
      </div>
    </div>

    <!-- ===== 提示信息（孤儿字段兜底） ===== -->
    <div v-if="testMsg" class="stg-msg" :class="testCls">
      <Icon :name="st.testResult && st.testResult.ok ? 'check' : 'alert'" :size="14" />
      <span>
        {{ testMsg }}
        <em v-if="st.testResult && st.testResult.hint" class="stg-hint">—— {{ st.testResult.hint }}</em>
        <em v-if="st.testResult && st.testResult.base" class="stg-hint">目标 {{ st.testResult.base }}</em>
      </span>
    </div>

    <!-- ===== 修改口令 ===== -->
    <div class="stg-group">
      <div class="stg-group-h">
        <Icon name="shield" :size="16" /><b>修改口令</b>
        <span class="stg-tag admin"><Icon name="lock" :size="11" /> 需管理员</span>
      </div>
      <p class="stg-group-d">改完后该角色其它会话的令牌立即作废，新口令写入 <code>.env</code> 重启仍有效。</p>
      <!-- ★ 密码输入框必须包在 <form> 里：裸的 type="password" 会触发浏览器告警
           "Password field is not contained in a form"，也拿不到回车提交与密码管理器语义。 -->
      <form class="stg-pw" @submit.prevent="doChangePassword">
        <!-- ★ 可访问性修复：Chromium 要求密码表单带（可隐藏的）用户名框，否则报警告。
             这里用视觉隐藏的 username 字段提供语义，不干扰布局。 -->
        <input type="text" name="username" autocomplete="username"
               style="position:absolute;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none;"
               tabindex="-1" aria-hidden="true"
               :value="pw.role" readonly />
        <label class="stg-pw-row">
          <span>角色</span>
          <select class="stg-in" v-model="pw.role">
            <option value="admin">管理员（可改配置与围栏）</option>
            <option value="operator">操作员（只能操控机器人）</option>
          </select>
        </label>
        <label class="stg-pw-row">
          <span>当前口令</span>
          <input class="stg-in" type="password" v-model="pw.current" autocomplete="current-password" />
        </label>
        <label class="stg-pw-row">
          <span>新口令</span>
          <input class="stg-in" type="password" v-model="pw.next" autocomplete="new-password" placeholder="至少 4 位" />
        </label>
        <label class="stg-pw-row">
          <span>确认新口令</span>
          <input class="stg-in" type="password" v-model="pw.again" autocomplete="new-password" />
        </label>
        <div class="stg-pw-btns">
          <button class="stg-btn primary" type="submit" :disabled="!st.isAdmin || st.saving">
            <Icon name="check" :size="14" /> 修改口令
          </button>
          <span v-if="!st.isAdmin" class="stg-pw-tip">需管理员令牌</span>
        </div>
      </form>
      <div v-if="pwErr" class="stg-msg err"><Icon name="alert" :size="14" /> {{ pwErr }}</div>
      <div v-else-if="pwMsg" class="stg-msg ok"><Icon name="check" :size="14" /> {{ pwMsg }}</div>
    </div>

    <!-- ===== 控制权限时长 ===== -->
    <div class="stg-group">
      <div class="stg-group-h">
        <Icon name="clock" :size="16" /><b>控制权限时长</b>
        <span class="stg-tag admin"><Icon name="lock" :size="11" /> 需管理员</span>
      </div>
      <p class="stg-group-d">令牌有效时长（<b>默认不限时</b>）。重启后端后需重新获取权限；设置写入 <code>.env</code> 持续生效。</p>
      <div class="stg-pw">
        <label class="stg-pw-row">
          <span>时长</span>
          <select class="stg-in" v-model.number="ttlSel">
            <option v-for="o in TTL_OPTIONS" :key="o.v" :value="o.v">{{ o.label }}</option>
            <option :value="-1">自定义（分钟）</option>
          </select>
        </label>
        <label class="stg-pw-row" v-if="ttlSel === -1">
          <span>分钟数</span>
          <input class="stg-in" type="number" min="1" step="1" v-model="ttlCustom" placeholder="如 60 = 1 小时" />
        </label>
        <div class="stg-pw-btns">
          <button class="stg-btn primary" :disabled="!st.isAdmin || ttlBusy" @click="applyTtl">
            <Icon name="check" :size="14" /> 应用时长
          </button>
          <span v-if="!st.isAdmin" class="stg-pw-tip">需管理员令牌</span>
          <span v-else class="stg-pw-tip">当前：{{ ttlLabel(ttlCurrent === null ? 0 : ttlCurrent) }}</span>
        </div>
      </div>
      <div v-if="ttlErr" class="stg-msg err"><Icon name="alert" :size="14" /> {{ ttlErr }}</div>
      <div v-else-if="ttlMsg" class="stg-msg ok"><Icon name="check" :size="14" /> {{ ttlMsg }}</div>
    </div>

    <!-- ===== ★ C1 界面模式（本机偏好，存 localStorage，**不写后端配置**） ===== -->
    <div class="stg-group">
      <div class="stg-group-h">
        <Icon name="activity" :size="16" /><b>界面模式（只影响这台浏览器的这一位操作员）</b>
        <span class="stg-tag">{{ ui.hiPerf ? "高性能画面（ISA-101 灰阶）" : "常规（彩色）" }}</span>
      </div>
      <p class="stg-group-d">
        <b>高性能画面模式</b>＝ ISA-101 那套口径：底色灰阶、<b>颜色只留给异常</b>、
        "推断 / 未知"除了颜色还加<b>虚线 + 斜纹 + 文字后缀</b>（不靠颜色也能看出是推断）。
        关掉时外观与今天<b>逐字一致</b>（所有灰阶样式都挂在 <code>.hmi-hi-perf</code> 底下，模式不开一条都不命中）。
        ★ 这是看法不是配置：<b>不写 <code>app_settings.json</code>、不动机器人、不影响别人</b>；
        判据（报警等级、互锁、安全距离）一个都没变。
      </p>
      <label class="stg-confirm">
        <input type="checkbox" :checked="ui.hiPerf" @change="ui.setHiPerf($event.target.checked)" />
        <span>启用高性能画面模式（灰阶底 · 颜色只给异常 · 推断/未知加形状与文字冗余）</span>
      </label>
    </div>

    <!-- ===== 危险区：重置所有参数 ===== -->
    <div class="stg-group stg-danger">
      <div class="stg-group-h">
        <Icon name="trash" :size="16" /><b>重置所有参数</b>
        <span class="stg-tag danger"><Icon name="alert" :size="11" /> 不可撤销</span>
      </div>
      <p class="stg-group-d">
        清空覆盖层，<b>全部参数回到 <code>config/robot.yaml</code> 出厂值</b>（当前已改 <b>{{ st.overridden }}</b> 项）。
        建议先导出配置包；重置前的覆盖层内容会写进审计事件。
      </p>
      <label class="stg-confirm">
        <input type="checkbox" v-model="resetConfirm" />
        <span>我确认要把全部参数恢复出厂值（包括机器人地址、相机地址、点动限速、寄存器与真实下发开关）</span>
      </label>
      <div class="stg-danger-btns">
        <button class="stg-btn danger" :disabled="!resetConfirm || !st.isAdmin || st.saving" @click="doReset">
          <Icon name="trash" :size="14" /> 重置所有参数（{{ st.overridden }} 项）
        </button>
        <span v-if="!st.isAdmin" class="stg-pw-tip">需管理员令牌</span>
      </div>
      <div v-if="resetMsg" class="stg-msg ok"><Icon name="check" :size="14" /> {{ resetMsg }}</div>
    </div>

    </div>
</template>

<style scoped>
.stg { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; padding: 10px;
  display: flex; flex-direction: column; gap: 10px; align-items: center; }
/* ★ 需求：设置页内容宽度 1200px 并水平居中（每张卡片/分组各自居中，超宽屏不再拉满） */
.stg > * { width: 100%; max-width: 1200px; }

/* ---- 顶部 ---- */
.stg-top { display: flex; align-items: center; gap: 10px; padding: 10px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
.stg-top-l { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
.stg-mark { flex: none; width: 40px; height: 40px; display: grid; place-items: center;
  border-radius: 10px; color: var(--accent);
  background: var(--accent-soft); border: 1px solid var(--accent-line); }
.stg-title { font-size: 15px; font-weight: 700; color: var(--txt); }
.stg-sub { font-size: 11px; color: var(--muted); margin-top: 3px; line-height: 1.6; }
.stg-sub b { color: var(--txt); }
.stg-dot { color: var(--line); margin: 0 3px; }
.stg-top-r { flex: none; }

/* ---- 按钮 ---- */
.stg-btn { display: inline-flex; align-items: center; gap: 5px; padding: 6px 10px;
  font-size: 12px; border-radius: 7px; border: 1px solid var(--line);
  background: var(--panel2); color: var(--txt); cursor: pointer; }
.stg-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.stg-btn:disabled { opacity: .45; cursor: not-allowed; }
.stg-btn.primary { color: var(--accent); border-color: var(--accent-line); }
.stg-btn.danger { color: var(--err); border-color: var(--err-line); }
.stg-btn.danger:hover:not(:disabled) { border-color: var(--err); color: var(--err); }
.stg-btn.sm { padding: 4px 9px; font-size: 11px; margin-left: auto; }
.spin { animation: stg-spin .8s linear infinite; }
@keyframes stg-spin { to { transform: rotate(360deg); } }

/* ---- 未保存改动条 ---- */
.stg-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px;
  padding: 10px; border-radius: 9px;
  border: 1px solid var(--accent-line); background: var(--accent-soft3); }
.stg-bar-n { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  font-weight: 600; color: var(--accent); }
.stg-bar-apply { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
.stg-bar-warn { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--warn); }
.stg-bar-btns { margin-left: auto; display: inline-flex; gap: 7px; }

/* ---- 徽标 ---- */
.stg-badge { flex: none; display: inline-flex; align-items: center; gap: 2px; font-size: 10px;
  padding: 1px 6px; border-radius: 20px; border: 1px solid var(--line); color: var(--muted); }
.stg-badge.yaml { color: var(--muted); }
.stg-badge.settings { color: var(--accent); border-color: var(--accent-line); }
.stg-badge.env { color: var(--warn); border-color: var(--warn-line); }
.stg-badge.derived { color: var(--ok); border-color: var(--ok-line); }
.stg-badge.default { color: var(--muted); }
.stg-badge.live { color: var(--ok); border-color: var(--ok-line); }
.stg-badge.reload { color: var(--muted); }
.stg-badge.reconnect { color: var(--warn); border-color: var(--warn-line); }
.stg-badge.restart { color: var(--err); border-color: var(--err-line); }
.stg-badge.admin { color: var(--warn); border-color: var(--warn-line); }
.stg-ap { font-size: 10px; padding: 1px 6px; border-radius: 20px; border: 1px solid var(--line); }
.stg-ap.restart { color: var(--err); border-color: var(--err-line); }
.stg-ap.reconnect { color: var(--warn); border-color: var(--warn-line); }
.stg-ap.live { color: var(--ok); border-color: var(--ok-line); }

/* ---- 消息 ---- */
.stg-msg { display: flex; align-items: center; gap: 6px; font-size: 12px; line-height: 1.6;
  padding: 9px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel2); }
.stg-msg svg { flex: none; }
.stg-msg.ok { color: var(--ok); border-color: var(--ok-line); }
.stg-msg.err { color: var(--err); border-color: var(--err-line); }
.stg-msg.warn { color: var(--warn); border-color: var(--warn-line); }
.stg-hint { font-style: normal; color: var(--muted); margin-left: 6px; }

/* ---- env 锁定 ---- */
.stg-env { padding: 10px; border-radius: 9px; border: 1px solid var(--warn-line);
  background: var(--warn-soft); }
.stg-env-h { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--warn); font-weight: 600; }
.stg-env-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.stg-env-item { font-size: 11px; color: var(--muted); }
.stg-env-item b { color: var(--txt); }
.stg-env-tip { margin: 8px 0 0; font-size: 11px; color: var(--muted); }

/* ---- 实时卡 ---- */
.stg-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 9px; }
.stg-card { border: 1px solid var(--line); border-left-width: 3px; border-radius: 8px;
  background: var(--panel); padding: 10px; }
.stg-card.ok { border-left-color: var(--ok); }
.stg-card.warn { border-left-color: var(--warn); }
.stg-card.err { border-left-color: var(--err); }
.stg-card-h { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
.stg-card-h svg { color: var(--accent); }
.stg-card-m { font-size: 13px; color: var(--txt); font-weight: 600; margin-top: 5px; }
.stg-card-s { font-size: 11px; color: var(--muted); margin-top: 3px; word-break: break-all; }

/* ---- 分组导航 ---- */
/* ★ St2：sticky top 从 -14px 改为 0 —— 容器 .stg 有 padding:10px，原先吸附点为负，
   滑到顶时会盖住上一张卡的底边。改为相对滚动容器顶对齐。 */
.stg-nav { display: flex; flex-wrap: wrap; gap: 6px; padding: 9px 10px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  position: sticky; top: 0; z-index: 3; }
.stg-nav-chip { display: inline-flex; align-items: center; gap: 4px; padding: 4px 9px;
  font-size: 11px; border-radius: 20px; color: var(--muted);
  border: 1px solid var(--line); background: var(--panel2); cursor: pointer; }
.stg-nav-chip:hover { color: var(--accent); border-color: var(--accent); }
.stg-nav-chip svg { color: var(--accent); }
.stg-nav-n { font-size: 10px; color: var(--accent); }

/* ---- 分组 ---- */
.stg-group { border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  padding: 10px; scroll-margin-top: 48px; }
.stg-group-h { display: flex; align-items: center; gap: 7px; font-size: 13px; color: var(--txt); flex-wrap: wrap; }
.stg-group-h svg { color: var(--accent); flex: none; }
.stg-group-d { margin: 8px 0 12px; font-size: 11px; color: var(--muted); line-height: 1.75;
  padding-left: 9px; border-left: 2px solid var(--line); }
.stg-group-d code { font-size: 11px; color: var(--accent); }
.stg-tag { display: inline-flex; align-items: center; gap: 3px; font-size: 10px; padding: 1px 7px;
  border-radius: 20px; border: 1px solid var(--line); color: var(--muted); }
.stg-tag.admin { color: var(--warn); border-color: var(--warn-line); }
.stg-tag.danger { color: var(--err); border-color: var(--err-line); }
.stg-danger { border-color: var(--err-line); }

/* ---- 字段 ---- */
/* ★ St1：弱化"卡片套卡片" —— 每个字段原来都是独立小盒子（边框+底色+圆角），一屏几十个框很碎。
   改为**行分隔**：去掉整圈边框/底色，只留左侧色条（dirty 时点亮作脏标记）+ 底部行分隔线，
   分组卡外层保留。 */
.stg-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 10px; }
.stg-field { border-left: 3px solid transparent; border-bottom: 1px solid var(--line);
  padding: 10px 2px 10px 8px; }
.stg-field.locked { opacity: .68; }
.stg-field.dirty { border-left-color: var(--accent); }
.stg-f-head { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.stg-f-label { font-size: 12px; color: var(--txt); font-weight: 600; }
.stg-f-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent); margin-left: 5px; vertical-align: middle; }
.stg-f-ctl { display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.stg-key { margin-left: auto; }
.stg-key code { font-size: 10px; color: var(--muted); }
.stg-f-help { font-size: 11px; color: var(--muted); margin-top: 7px; line-height: 1.7; }
.stg-f-lock { display: flex; align-items: center; gap: 4px; font-size: 11px; color: var(--warn);
  margin-top: 7px; line-height: 1.6; }
.stg-ro { display: flex; flex-direction: column; gap: 4px; }
.stg-ro code { font-size: 11px; color: var(--txt); word-break: break-all; }
.stg-ro-r { font-size: 11px; color: var(--muted); line-height: 1.65; }

/* ---- 输入控件 ---- */
.stg-in { font-size: 12px; padding: 6px 9px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--bg); color: var(--txt); min-width: 0; }
.stg-in:focus { outline: none; border-color: var(--accent); }
.stg-in:disabled { opacity: .6; cursor: not-allowed; }
.stg-in.num { width: 110px; font-variant-numeric: tabular-nums; }
.stg-in.ta { width: 100%; resize: vertical; font-family: inherit; }
.stg-num { display: flex; align-items: center; gap: 6px; }
.stg-unit { font-size: 11px; color: var(--muted); }
.stg-range { font-size: 10px; color: var(--muted); }

/* 开关 */
.stg-sw { display: inline-flex; align-items: center; gap: 7px; padding: 4px 10px 4px 4px;
  border-radius: 20px; border: 1px solid var(--line); background: var(--bg);
  color: var(--muted); cursor: pointer; font-size: 11px; }
.stg-sw:disabled { opacity: .6; cursor: not-allowed; }
.stg-sw-k { width: 24px; height: 14px; border-radius: 20px; background: var(--line); position: relative;
  transition: background .18s ease; }
.stg-sw-k::after { content: ""; position: absolute; top: 2px; left: 2px; width: 10px; height: 10px;
  border-radius: 50%; background: var(--muted); transition: transform .18s ease, background .18s ease; }
.stg-sw.on { color: var(--ok); border-color: var(--ok-line); }
.stg-sw.on .stg-sw-k { background: var(--ok-line); }
.stg-sw.on .stg-sw-k::after { transform: translateX(10px); background: var(--ok); }

/* 轴符号 */
.stg-signs { display: flex; gap: 5px; flex-wrap: wrap; }
.stg-sign { display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--bg); cursor: pointer; }
.stg-sign:disabled { opacity: .6; cursor: not-allowed; }
.stg-sign b { font-size: 10px; color: var(--muted); }
.stg-sign span { font-size: 11px; color: var(--ok); font-variant-numeric: tabular-nums; }
.stg-sign.neg span { color: var(--err); }

/* 关节限位 */
.stg-limits { display: flex; flex-direction: column; gap: 4px; }
.stg-limit { display: flex; align-items: center; gap: 6px; }
.stg-limit-n { flex: none; width: 26px; font-size: 11px; color: var(--muted); }
.stg-limit .stg-in.num { width: 78px; }
.stg-limit-sep { font-size: 11px; color: var(--muted); }

/* ---- 口令 ---- */
.stg-pw { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 9px; }
.stg-pw-row { display: flex; flex-direction: column; gap: 5px; }
.stg-pw-row > span { font-size: 11px; color: var(--muted); }
.stg-pw-btns { display: flex; align-items: flex-end; gap: 8px; }
.stg-pw-tip { font-size: 11px; color: var(--warn); }

/* ---- 危险区 ---- */
.stg-confirm { display: flex; align-items: flex-start; gap: 8px; font-size: 12px; color: var(--txt);
  line-height: 1.7; padding: 10px; border-radius: 8px;
  border: 1px solid var(--err-line); background: var(--err-soft); }
.stg-confirm input { flex: none; margin-top: 3px; accent-color: var(--err); }
.stg-danger-btns { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
</style>
