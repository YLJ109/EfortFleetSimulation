<script setup>
// =====================================================================
// 运维审计视图（阶段 3 监控运维 + 阶段 4 企业级加固）
//
// 一页四件事：
//   1) 健康总览 —— 服务/数据库/机器人链路/执行引擎/互锁是否生效，一键重连
//   2) 事件时间线 —— 统一事件总线(services/events.py)落库的所有记录,
//      支持类别/级别/操作者/时间窗/关键字过滤，WebSocket 实时追加，可导出 JSON/CSV
//   3) 备份恢复 —— 系统配置包(围栏+点位+程序)导出 / 导入
//   4) 配置版本 —— 安全围栏历史版本列表与一键回滚
//
// ★ 权限：写操作全部走 apiControl(自动带 X-Control-Token)，
//   被 401/403 拒绝时统一弹管理员登录框，不让用户对着一句报错发呆。
// =====================================================================
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from "vue";
import Icon from "./Icon.vue";
import { apiUrl, wsEventsUrl } from "../config.js";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "../stores/auth.js";

const auth = useAuthStore();

// ---------------- 时间线 ----------------
const items = ref([]);
const stats = ref({ total: 0, by_category: {}, by_level: {} });
const loading = ref(false);
const live = ref(true);            // 实时开关：订阅 WS 广播的 event 帧
const filt = reactive({
  category: "", level: "", actor: "", hours: "", q: "", limit: 200,
});

const CATS = [
  { v: "", t: "全部类别" },
  { v: "system", t: "系统" },
  { v: "connection", t: "链路" },
  { v: "auth", t: "鉴权" },
  { v: "control", t: "控制" },
  { v: "config", t: "配置" },
  { v: "safety", t: "安全" },
];
const LEVELS = [
  { v: "", t: "全部级别" },
  { v: "info", t: "信息+" },
  { v: "warn", t: "警告+" },
  { v: "error", t: "错误+" },
  { v: "critical", t: "严重" },
];
const ACTORS = [
  { v: "", t: "全部操作者" },
  { v: "system", t: "系统" },
  { v: "admin", t: "管理员" },
  { v: "operator", t: "操作员" },
  { v: "anonymous", t: "匿名" },
];
const HOURS = [
  { v: "", t: "全部时间" },
  { v: 1, t: "最近 1 小时" },
  { v: 24, t: "最近 24 小时" },
  { v: 72, t: "最近 3 天" },
  { v: 168, t: "最近 7 天" },
];

const CAT_TEXT = { system: "系统", connection: "链路", auth: "鉴权",
  control: "控制", config: "配置", safety: "安全" };
const LV_TEXT = { debug: "调试", info: "信息", warn: "警告",
  error: "错误", critical: "严重" };
const ACTOR_TEXT = { system: "系统", admin: "管理员", operator: "操作员",
  anonymous: "匿名" };

function qs() {
  const p = new URLSearchParams();
  if (filt.category) p.set("category", filt.category);
  if (filt.level) p.set("level", filt.level);
  if (filt.actor) p.set("actor", filt.actor);
  if (filt.hours !== "") p.set("hours", String(filt.hours));
  if (filt.q.trim()) p.set("q", filt.q.trim());
  p.set("limit", String(filt.limit));
  return p.toString();
}

async function loadEvents() {
  // ★ 修 401：/api/events 后端已挂 require_control（安全改动），裸 fetch 不带令牌 →
  //   开机即挂载时每个未登录访客都会带出一条 401（浏览器对非 2xx 会自己打日志，JS 屏蔽不掉）。
  //   与同文件 loadVersions 一致：未持有效令牌就不发请求；持令牌时用 apiControl 自动带 X-Control-Token。
  if (!auth.controlActive) { items.value = []; return; }
  loading.value = true;
  try {
    const r = await apiControl("/events?" + qs());
    if (r.ok) items.value = (await r.json()).items || [];
  } catch (e) { /* 静默 */ }
  finally { loading.value = false; }
}

async function loadStats() {
  if (!auth.controlActive) { stats.value = { total: 0, by_category: {}, by_level: {} }; return; }
  try {
    const h = filt.hours !== "" ? `?hours=${filt.hours}` : "";
    const r = await apiControl("/events/stats" + h);
    if (r.ok) stats.value = await r.json();
  } catch (e) { /* 静默 */ }
}

function refreshAll() { loadEvents(); loadStats(); }

// ---------------- 健康 ----------------
const health = ref(null);
async function loadHealth() {
  try {
    const r = await fetch(apiUrl("/system/health"));
    if (r.ok) health.value = await r.json();
  } catch (e) { health.value = null; }
}

const healthClass = computed(() => {
  const s = health.value && health.value.status;
  return s === "ok" ? "ok" : s === "degraded" ? "warn" : "err";
});
const healthText = computed(() => ({
  ok: "正常", degraded: "降级运行（机器人未连接）", down: "异常",
}[(health.value && health.value.status) || ""] || "未知"));

async function doReconnect() {
  const r = await apiControl("/system/reconnect", { method: "POST" });
  if (r.status === 401 || r.status === 403) return auth.requestLogin();
  await loadHealth();
}

// ---------------- 备份 / 版本 ----------------
const versions = ref([]);
const backupBusy = ref(false);
const backupMsg = ref("");
const fileInput = ref(null);

async function loadVersions() {
  // ★ 未持有效令牌时不发请求：/safety/versions 在后端是 require_control，必然 401，
  //   而浏览器对非 2xx 的 fetch 会自己往控制台打一行 Unauthorized（JS 屏蔽不掉）。
  //   本页也是开机即挂载（v-show 保活），不拦的话每个未登录访客都会带出一条 401。
  if (!auth.controlActive) { versions.value = []; return; }
  const r = await apiControl("/safety/versions");
  if (r.ok) versions.value = await r.json();
  else versions.value = [];
}

async function doBackupExport() {
  const r = await apiControl("/system/export");
  if (r.status === 401 || r.status === 403) return auth.requestLogin();
  if (!r.ok) { backupMsg.value = "导出失败"; return; }
  const blob = new Blob([JSON.stringify(await r.json(), null, 2)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  const t = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  a.download = `efort-system-backup-${t}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
  backupMsg.value = "备份已导出";
}

async function onBackupImport(e) {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  backupBusy.value = true; backupMsg.value = "";
  try {
    const data = JSON.parse(await f.text());
    const r = await apiControl("/system/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (r.status === 401 || r.status === 403) {
      backupMsg.value = r.status === 403 ? "需要管理员权限" : "请先获取控制令牌";
      auth.requestLogin();
    } else if (r.ok) {
      const d = await r.json();
      backupMsg.value = "导入完成：" +
        `围栏${d.result.safety ? "✓" : "✗"}、` +
        `点位 +${d.result.points.created}/~${d.result.points.updated}、` +
        `程序 +${d.result.programs.created}/~${d.result.programs.updated}`;
      await loadVersions();
    } else {
      backupMsg.value = "导入失败：" + (await r.json().catch(() => ({}))).detail;
    }
  } catch (err) { backupMsg.value = "文件解析失败：" + err.message; }
  finally { backupBusy.value = false; e.target.value = ""; }
}

async function doRollback(v) {
  if (!confirm(`确认回滚安全围栏配置到版本 #${v.id}？\n（当前配置会先归档，可再回滚）`)) return;
  const r = await apiControl(`/safety/versions/${v.id}/rollback`, { method: "POST" });
  if (r.status === 401 || r.status === 403) return auth.requestLogin();
  if (r.ok) { backupMsg.value = `已回滚到版本 #${v.id}`; await loadVersions(); }
  else backupMsg.value = "回滚失败：" + (await r.json().catch(() => ({}))).detail;
}

async function doClear() {
  if (!confirm("确认清空全部审计日志？此操作不可恢复。")) return;
  const r = await apiControl("/events", { method: "DELETE" });
  if (r.status === 401 || r.status === 403) return auth.requestLogin();
  refreshAll();
}

async function exportEvents(fmt) {
  // ★ /events/export 也是 require_control：window.open 带不了请求头 → 必然 401。
  //   改为带令牌 fetch 后转 blob 下载。
  if (!auth.controlActive) { auth.requestLogin(); return; }
  try {
    const r = await apiControl(`/events/export?format=${fmt}&` + qs());
    if (!r.ok) return;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `events.${fmt === "csv" ? "csv" : "json"}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) { /* 静默 */ }
}

// ---------------- WebSocket 实时追加 ----------------
// ★ P1-B6：事件帧已从 /ws/pose 拆到 /ws/events，**必须持控制令牌**（带内握手）。
//   未登录时不建立连接（连了也会被后端 4401 拒），避免每 2s 一次的无谓重连。
let ws = null;
let wsTimer = null;
function connectEvents() {
  disconnectEvents();
  if (!auth.controlActive) return;
  try {
    ws = new WebSocket(wsEventsUrl());
    ws.onopen = () => {
      try { ws.send(JSON.stringify({ type: "auth", token: auth.token })); }
      catch (e) { /* 发不出去等后端超时关闭即可 */ }
    };
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (!m || m.type !== "event") return;
        items.value = [m, ...items.value].slice(0, filt.limit);
        stats.value.total = (stats.value.total || 0) + 1;
      } catch (e) { /* 忽略坏帧 */ }
    };
    // 4401 = 令牌缺失/失效：不重试，等 controlActive 变化时由 watch 重建
    ws.onclose = (ev) => {
      if (ev && ev.code === 4401) return;
      if (live.value) wsTimer = setTimeout(connectEvents, 2000);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* 关旧连接失败无影响 */ } };
  } catch (e) { /* 后端不可用时静默降级为轮询 */ }
}
function disconnectEvents() {
  if (wsTimer) clearTimeout(wsTimer);
  wsTimer = null;
  if (ws) { try { ws.close(); } catch (e) { /* 关旧连接失败无影响 */ } }
  ws = null;
}
watch(live, (v) => { if (v) connectEvents(); else disconnectEvents(); });
// ★ 拿到/失去控制权限都要重建：先连后登录的场景下，否则时间线永远不实时
watch(() => auth.controlActive, (v) => {
  if (v && live.value) connectEvents();
  else if (!v) disconnectEvents();
});

// ---------------- 工具 ----------------
function fmtTime(iso) {
  if (!iso) return "";
  const s = String(iso).replace("T", " ").replace("Z", "");
  return s.slice(5, 19);
}
function actLabel(a) { return ACTOR_TEXT[a] || a || "系统"; }
function detailOf(e) {
  if (!e.detail) return "";
  try {
    const o = JSON.parse(e.detail);
    return typeof o === "object" ? JSON.stringify(o) : String(o);
  } catch (err) { return e.detail; }
}
const expanded = ref(null);
function toggleDetail(id) { expanded.value = expanded.value === id ? null : id; }

let tick = null;
onMounted(() => {
  refreshAll();
  loadHealth();
  loadVersions();
  connectEvents();
  // 健康/统计走轮询兜底（WS 只推事件帧）
  tick = setInterval(() => { loadHealth(); }, 5000);
});

// 挂载时若还没有控制权限，loadVersions 会被 apiControl 在本地拦下（不发请求、不打 401）；
// 拿到权限后再补一次，用户不必为了看围栏版本去刷新页面。
watch(() => auth.controlActive, (v) => { if (v) loadVersions(); });
onBeforeUnmount(() => {
  if (tick) clearInterval(tick);
  disconnectEvents();
});

// 过滤条件变化 → 重新拉取（关键字输入做简单防抖）
let deb = null;
watch(() => [filt.category, filt.level, filt.actor, filt.hours], refreshAll);
watch(() => filt.q, () => {
  if (deb) clearTimeout(deb);
  deb = setTimeout(refreshAll, 300);
});
</script>

<template>
  <div class="ops">
    <!-- ===== 顶部：健康总览 ===== -->
    <div class="ops-sec">
      <div class="ops-head">
        <Icon name="server" :size="16" />
        <span class="ops-title">系统健康</span>
        <span class="ops-badge" :class="healthClass">
          <span class="ops-dot" :class="healthClass"></span>{{ healthText }}
        </span>
        <span class="ops-spacer"></span>
        <button class="ops-btn" @click="loadHealth()"><Icon name="refresh" :size="14" /> 刷新</button>
        <button class="ops-btn primary" @click="doReconnect()"><Icon name="wifi" :size="14" /> 重连控制器</button>
      </div>
      <div class="ops-cards">
        <div class="card kv">
          <Icon name="terminal" :size="14" />
          <div><b>服务</b>
            <span>{{ health ? `v${health.version} · ${health.uptime_sec}s` : "—" }}</span>
          </div>
        </div>
        <div class="card kv">
          <Icon name="database" :size="14" />
          <div><b>数据库</b>
            <span>{{ health && health.db.ok ? health.db.size : "不可用" }}</span>
          </div>
        </div>
        <div class="card kv">
          <Icon :name="health && health.robot && health.robot.connected ? 'wifi' : 'wifiOff'" :size="14" />
          <div><b>机器人链路</b>
            <span :class="health && health.robot && health.robot.connected ? 'ok' : 'err'">
              {{ health ? health.robot.mode : "—" }}
            </span>
          </div>
        </div>
        <div class="card kv">
          <Icon name="activity" :size="14" />
          <div><b>执行引擎</b>
            <span>{{ health ? `${health.motion.mode}${health.motion.stopped ? " · 急停" : ""}` : "—" }}</span>
          </div>
        </div>
        <div class="card kv">
          <Icon name="user" :size="14" />
          <div><b>在线客户端</b><span>{{ health ? health.ws_clients : "—" }}</span></div>
        </div>
        <div class="card kv">
          <Icon name="shieldCheck" :size="14" />
          <div><b>围栏互锁</b>
            <span :class="health && health.safety_interlock && health.safety_interlock.fresh ? 'ok' : 'warn'">
              {{ health && health.safety_interlock
                 ? (health.safety_interlock.fresh ? `生效 · ${health.safety_interlock.state}` : "未上报")
                 : "—" }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 备份恢复 + 配置版本 ===== -->
    <div class="ops-sec">
      <div class="ops-head">
        <Icon name="database" :size="16" />
        <span class="ops-title">备份恢复与配置版本</span>
      </div>
      <div class="ops-split">
        <div class="card">
          <h3>系统配置包</h3>
          <p class="small">导出安全围栏 + 预设点位 + 执行程序为一个 JSON 包，可用于迁移或灾难恢复；导入为安全合并，不会删除既有数据。</p>
          <div class="btns" style="margin-top:8px">
            <button @click="doBackupExport()"><Icon name="download" :size="14" /> 导出备份</button>
            <button @click="fileInput.click()"><Icon name="upload" :size="14" /> 导入备份</button>
          </div>
          <input type="file" ref="fileInput" accept=".json,application/json" class="hidden" @change="onBackupImport" />
          <p v-if="backupMsg" class="ops-msg"><Icon name="check" :size="13" /> {{ backupMsg }}</p>
          <p v-if="backupBusy" class="small">正在导入…</p>
        </div>
        <div class="card">
          <h3>围栏配置历史版本</h3>
          <div class="ops-ver">
            <div v-if="!versions.length" class="small">暂无历史版本（改动围栏配置后自动生成）</div>
            <div v-for="v in versions" :key="v.id" class="ops-ver-item">
              <span class="ops-ver-id">#{{ v.id }}</span>
              <span class="ops-ver-t">{{ fmtTime(v.created_at) }}</span>
              <span class="tag" :class="v.source === 'reset' ? 'sim' : 'real'">{{ v.source }}</span>
              <span class="ops-ver-note">{{ v.note || `${v.zones} 个区域` }}</span>
              <button title="回滚到此版本" @click="doRollback(v)">
                <Icon name="rotateLeft" :size="13" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 事件时间线 ===== -->
    <div class="ops-sec ops-fill">
      <div class="ops-head">
        <Icon name="activity" :size="16" />
        <span class="ops-title">事件时间线</span>
        <span class="ops-chip">共 {{ stats.total }} 条</span>
        <span v-for="lv in ['critical','error','warn']" :key="lv"
              class="ops-chip" :class="lv">
          {{ LV_TEXT[lv] }} {{ stats.by_level?.[lv] || 0 }}
        </span>
        <span class="ops-spacer"></span>
        <label class="ops-live"><input type="checkbox" v-model="live" /> 实时</label>
        <button class="ops-btn" @click="exportEvents('json')"><Icon name="download" :size="14" /> JSON</button>
        <button class="ops-btn" @click="exportEvents('csv')"><Icon name="download" :size="14" /> CSV</button>
        <button class="ops-btn danger" @click="doClear()"><Icon name="trash" :size="14" /> 清空</button>
      </div>

      <div class="ops-filter">
        <select v-model="filt.category">
          <option v-for="c in CATS" :key="c.v" :value="c.v">{{ c.t }}</option>
        </select>
        <select v-model="filt.level">
          <option v-for="l in LEVELS" :key="l.v" :value="l.v">{{ l.t }}</option>
        </select>
        <select v-model="filt.actor">
          <option v-for="a in ACTORS" :key="a.v" :value="a.v">{{ a.t }}</option>
        </select>
        <select v-model="filt.hours">
          <option v-for="h in HOURS" :key="h.v" :value="h.v">{{ h.t }}</option>
        </select>
        <input class="ops-search" v-model="filt.q" placeholder="搜索消息 / 动作名…" />
        <button class="ops-btn" @click="refreshAll()"><Icon name="refresh" :size="14" /> 刷新</button>
      </div>

      <div class="ops-list">
        <div v-if="!items.length" class="ops-empty">暂无匹配事件</div>
        <div v-for="e in items" :key="e.id" class="ops-item" :class="e.level">
          <span class="oi-t">{{ fmtTime(e.timestamp) }}</span>
          <span class="oi-cat" :class="e.category">{{ CAT_TEXT[e.category] || e.category }}</span>
          <span class="oi-lv" :class="e.level">{{ LV_TEXT[e.level] || e.level }}</span>
          <span class="oi-msg">
            {{ e.message }}
            <em class="oi-action">{{ e.action }}</em>
          </span>
          <span class="oi-actor">{{ actLabel(e.actor) }}<i v-if="e.ip"> · {{ e.ip }}</i></span>
          <button v-if="e.detail" class="oi-more" @click="toggleDetail(e.id)"
                  :title="expanded === e.id ? '收起详情' : '展开详情'">
            <Icon :name="expanded === e.id ? 'chevronUp' : 'chevronDown'" :size="13" />
          </button>
          <div v-if="expanded === e.id" class="oi-detail">{{ detailOf(e) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ops { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; padding: 10px;
  display: flex; flex-direction: column; gap: 10px; background: var(--bg); align-items: center; }
/* ★ 需求：运维审计页内容宽度 1200px 并水平居中 */
.ops > * { width: 100%; max-width: 1200px; }
.ops-sec { display: flex; flex-direction: column; gap: 10px; }
.ops-fill { flex: 1; min-height: 260px; }
.ops-head { display: flex; align-items: center; gap: 8px; color: var(--txt); font-size: 13px; }
.ops-title { font-weight: 700; letter-spacing: .5px; }
.ops-spacer { flex: 1; }
.ops-badge { display: flex; align-items: center; gap: 6px; font-size: 12px;
  padding: 2px 8px; border-radius: 6px; border: 1px solid var(--line); color: var(--muted); }
.ops-badge.ok { color: var(--ok); border-color: var(--ok-line); }
.ops-badge.warn { color: var(--warn); border-color: var(--warn-line); }
.ops-badge.err { color: var(--err); border-color: var(--err-line); }
.ops-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--muted); }
.ops-dot.ok { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
.ops-dot.warn { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
.ops-dot.err { background: var(--err); box-shadow: 0 0 6px var(--err); }

.ops-cards { display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.card.kv { display: flex; align-items: center; gap: 9px; padding: 10px; }
.card.kv svg { color: var(--accent); flex: none; }
.card.kv div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.card.kv b { font-size: 11px; color: var(--muted); font-weight: 500; }
.card.kv span { font-size: 13px; font-variant-numeric: tabular-nums;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card.kv span.ok { color: var(--ok); }
.card.kv span.err { color: var(--err); }

.ops-split { display: grid; gap: 10px; grid-template-columns: 1fr 1fr; }
.ops-split h3 { font-size: 13px; color: var(--accent); margin: 0 0 8px; }
.ops-msg { display: flex; align-items: center; gap: 6px; font-size: 12px;
  color: var(--ok); margin: 8px 0 0; }

.ops-ver { display: flex; flex-direction: column; gap: 6px; max-height: 190px; overflow-y: auto; }
.ops-ver-item { display: grid; grid-template-columns: 42px 84px 52px 1fr 30px;
  align-items: center; gap: 6px; font-size: 12px;
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; }
.ops-ver-id { color: var(--accent); font-weight: 700; font-variant-numeric: tabular-nums; }
.ops-ver-t { color: var(--muted); font-variant-numeric: tabular-nums; }
.ops-ver-note { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ops-ver-item button { padding: 3px 6px; }

.ops-chip { font-size: 11px; color: var(--muted); border: 1px solid var(--line);
  border-radius: 5px; padding: 1px 7px; font-variant-numeric: tabular-nums; }
.ops-chip.critical { color: var(--err-fg); border-color: var(--err-line); }
.ops-chip.error { color: var(--err); border-color: var(--err-line); }
.ops-chip.warn { color: var(--warn); border-color: var(--warn-line); }
.ops-live { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.ops-live input { accent-color: var(--accent); }
.ops-btn { flex: none; width: auto; padding: 6px 10px; display: flex; align-items: center; gap: 5px;
  font-size: 12px; }
.ops-btn.danger { color: var(--err); }
.ops-btn.danger:hover { border-color: var(--err); }

.ops-filter { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.ops-filter select { background: var(--panel); color: var(--txt); border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; }
.ops-search { flex: 1; min-width: 140px; background: var(--panel); color: var(--txt);
  border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; font-size: 12px; }

.ops-list { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 4px;
  border: 1px solid var(--line); border-radius: 8px; padding: 8px; background: var(--panel); }
.ops-empty { text-align: center; color: var(--muted); font-size: 12px; padding: 10px 0; }
.ops-item { display: grid; grid-template-columns: 84px 52px 48px 1fr auto 26px;
  align-items: center; gap: 8px; font-size: 12px; padding: 5px 8px;
  border: 1px solid transparent; border-radius: 6px; }
.ops-item:hover { border-color: var(--line); background: var(--veil); }
.oi-t { color: var(--muted); font-variant-numeric: tabular-nums; }
.oi-cat { font-size: 10px; text-align: center; border-radius: 4px; padding: 1px 5px;
  border: 1px solid var(--line); color: var(--muted); }
.oi-cat.auth { color: var(--c-auth); border-color: var(--c-auth-line); }
  .oi-cat.control { color: var(--accent); border-color: var(--accent-line); }
  .oi-cat.safety { color: var(--err); border-color: var(--err-line); }
  .oi-cat.config { color: var(--c-config); border-color: var(--c-config-line); }
  .oi-cat.connection { color: var(--c-conn); border-color: var(--c-conn-line); }
.oi-lv { font-size: 10px; text-align: center; border-radius: 4px; padding: 1px 5px;
  border: 1px solid var(--line); color: var(--muted); }
.oi-lv.warn { color: var(--warn); }
.oi-lv.error { color: var(--err); }
.oi-lv.critical { color: var(--ink); background: var(--err); border-color: var(--err); }
.oi-msg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.oi-action { color: var(--muted); font-style: normal; margin-left: 8px; font-size: 11px; }
.oi-actor { color: var(--muted); font-size: 11px; white-space: nowrap; }
.oi-actor i { font-style: normal; opacity: .75; }
.oi-more { flex: none; width: 24px; padding: 2px 0; background: transparent;
  border: 1px solid transparent; color: var(--muted); }
.oi-more:hover { color: var(--accent); border-color: var(--line); }
.oi-detail { grid-column: 1 / -1; font-size: 11px; color: var(--muted);
  background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
  padding: 6px 8px; word-break: break-all; white-space: pre-wrap; }

@media (max-width: 1200px) {
  .ops-split { grid-template-columns: 1fr; }
  .ops-item { grid-template-columns: 72px 46px 44px 1fr auto 26px; }
  .oi-actor { display: none; }
}
</style>
