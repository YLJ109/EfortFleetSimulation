﻿<script setup>
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
import MetricsPanel from "./MetricsPanel.vue";   // ★ B1：运行指标一张表（当前值+60s 最大+采样时刻）
import DailyReportCard from "./DailyReportCard.vue";   // ★ E6：机器人自述 · 今日现场日报（只读，零下发）
import ColorAdviceCard from "./ColorAdviceCard.vue";   // ★ E4：判色体检（样本环 + 建议 + 人工复核，只登记不生效）

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

// ---------------- 报警台账（C2：分级 / 折叠去重 / 延时确认 / 搁置 / 确认） ----------------
// ★★ 三条口径写死在这里，不在前端另造一套：
//   ① 刷新水位用后端的 `latest_seq`（只增不减），**不用 items.length** —— 列表会被
//      折叠 / 清除 / 过滤，长度不是单调的（§5.5 铁律①，与下面取证包的 packLatestSeq 同理）。
//   ② 状态一律**以后端为准**：pending / shelved / acked 全部读后端给的字段，
//      前端不许自己猜「都过了这么久应该好了吧」（§5.5 铁律②）。
//   ③ 未知就显示未知：优先级可能是 unclassified（字典里没登记过），时间戳可能是 null，
//      三态不许塌成布尔（§5.5 铁律③）。
// ★ 这块只是**显示与处置层**：确认只清显示，不解除任何互锁 / 就绪闸门 / 扫掠结论；
//   急停 / 停止 / 暂停那些「刹车」都在别处，永远可点 —— 本页不参与、也禁不了它们。
const alarms = ref([]);
const alarmSeq = ref(0);              // 已经展示到的水位（只增不减）
const alarmMeta = ref({});            // 后端给的分档计数 / 折叠窗 / 搁置默认值
const alarmRead = ref(false);         // ★ 有没有**成功读到过**一次：没读到过=未知，不许说「没有报警」
const alarmStale = ref("");           // 读失败时如实写明，并保留上一次列表（不空成「一切正常」）
const alarmBusy = ref(false);
const alarmMsg = ref("");
const alarmOpen = ref(null);          // 展开逐条明细的报警 key
const alarmHist = ref(null);
const shelving = ref(null);           // 正在填搁置参数的报警 key
const shelveMin = ref(30);
const shelveWhy = ref("");

// ★ ISA-101 克制配色：一色一义、不用闪动、不用多彩。颜色只是快捷线索，
//   每一档同时给中文标签（紧急/高/中/低/未分级）—— 看不见颜色也必须读得懂。
//   critical=红 沿用全局 --err；high=橙；medium=黄；low=蓝灰；未分级=中性灰 + 虚线边。
const ALARM_PRIO = {
  critical: { t: "紧急", cls: "p-critical" },
  high: { t: "高", cls: "p-high" },
  medium: { t: "中", cls: "p-medium" },
  low: { t: "低", cls: "p-low" },
  unclassified: { t: "未分级", cls: "p-unknown" },
};
function prioText(p) { return (ALARM_PRIO[p] || {}).t || "未知"; }
function prioClass(p) { return (ALARM_PRIO[p] || {}).cls || "p-unknown"; }
// ★ 后端给的状态词（明细里每一态都要能读懂，尤其「折叠」这一态）
const HSTATE = {
  active: "新出现", pending: "候选中", folded: "折叠计数", shelved: "搁置中",
  cleared: "已清除", shelve_expired: "搁置到期",
};
// 三态不许塌成布尔（§5.5 铁律③）：pending/shelved 读不到就照实说不知道
function alarmStateText(a) {
  if (a.pending === true) return "候选中（未达持续时长）";
  if (a.pending === null || a.pending === undefined) return "状态未知";
  if (a.shelved === true) return "已搁置" + shelveLeft(a);
  return "活动中";
}
function alarmStateClass(a) {
  if (a.pending === true) return "pend";
  if (a.shelved === true) return "shel";
  return "";
}
function shelveLeft(a) {
  const s = a && a.shelve_left_sec;
  if (s === null || s === undefined) return "";       // 未知就留空，不猜「已经到期」
  return s <= 0 ? "（正在回到活动态）" : `（剩 ${Math.ceil(s / 60)} 分）`;
}
function durText(sec) {
  if (sec === null || sec === undefined) return "—";
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return `${s}秒`;
  if (s < 3600) return `${Math.floor(s / 60)}分${s % 60 ? ` ${s % 60}秒` : ""}`;
  return `${Math.floor(s / 3600)}小时${Math.floor((s % 3600) / 60)}分`;
}

async function loadAlarms(force = false) {
  // 读接口在后端是**免令牌**的监控类（没登录的观察者也必须看得见围栏 hit），
  // 所以这里不卡 auth.controlActive —— 卡了就成了「不登录就看不见报警」的假安静。
  try {
    const r = await fetch(apiUrl("/alarms"));
    if (!r.ok) {
      alarmStale.value = `报警台账读取失败（HTTP ${r.status}）：下面保留上一次读到的内容，不等于「没有报警」`;
      return;
    }
    const d = await r.json();
    const seq = Number(d.latest_seq || 0);
    if (!force && seq === alarmSeq.value) { alarmRead.value = true; return; }   // 水位没动 = 台账没变
    if (seq < alarmSeq.value) {
      // 水位只增不减；变小了说明后端重启过（台账是内存态）—— 如实说，不静默重置
      alarmStale.value = `水位从 #${alarmSeq.value} 回到 #${seq}（后端重启过，台账是本次会话的内存态）`;
    } else {
      alarmStale.value = "";
    }
    alarmSeq.value = seq;
    alarms.value = d.items || [];          // ★ 顺序也由后端定（优先级水位→最近发生），前端不再排
    alarmMeta.value = {
      total_all: d.total_all, by_priority: d.by_priority || {}, pending: d.pending,
      shelved: d.shelved, safety_class: d.safety_class, unregistered: d.unregistered,
      window_ms: d.window_ms, shelve_default_min: d.shelve_default_min,
      history_seq: d.history_seq, note: d.note,
    };
    alarmRead.value = true;
    if (shelving.value && !alarms.value.some((a) => a.key === shelving.value)) shelving.value = null;
    if (alarmOpen.value && !alarms.value.some((a) => a.key === alarmOpen.value)) alarmOpen.value = null;
  } catch (e) {
    alarmStale.value = "报警台账读取失败（" + ((e && e.message) || "网络异常") + "）：下面保留上一次读到的内容";
  }
}

// 事件帧来得密（一条 5005 会连排好几条），合并成一次读取；再配一个 3s 轮询兜底
// （WS 只在持令牌时才连，没登录的观察者也必须看得见报警 —— 所以轮询不能省）。
let alarmDeb = null;
let alarmTick = null;
function kickAlarms() {
  if (alarmDeb) return;                 // 已经在等着了，不重复排
  alarmDeb = setTimeout(() => { alarmDeb = null; loadAlarms(); }, 800);
}

async function postAlarm(path, body, okText) {
  alarmBusy.value = true; alarmMsg.value = "";
  try {
    const r = await apiControl(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (r.status === 401 || r.status === 403) {
      alarmMsg.value = d.message || "需要控制令牌";
      auth.requestLogin();
      return;
    }
    // ★ 409 = 后端**如实拒绝**（安全类禁止搁置那一类）：原因原样贴出来，
    //   不许改写成「操作失败」让人以为按钮坏了，更不许显示成功。
    alarmMsg.value = r.ok ? okText : (d.message || `操作被拒绝（HTTP ${r.status}）`);
  } finally {
    alarmBusy.value = false;
    await loadAlarms(true);
  }
}

function ackAlarm(a) {
  // 确认这件事必须问一句：它只清显示，互锁不会因为确认而放行（写清楚，别让人误读）
  const extra = a.safety_class ? "\n（安全类：确认只清显示，围栏互锁 / 就绪闸门照旧生效）" : "";
  if (!confirm(`确认这条报警？\n确认只把它从列表里清掉，不解除任何互锁。${extra}\n\n${a.title}`)) return;
  postAlarm("/alarms/ack", { keys: [a.key], note: "" },
            "已确认：只清显示，互锁 / 就绪闸门照旧生效");
}
function openShelve(a) {
  if (!a.can_shelve) {         // 后端算出来的 can_shelve（安全类=False），前端只是不复算
    alarmMsg.value = a.note || "这条不允许搁置（安全类报警一律不许静音）";
    return;
  }
  if (shelving.value === a.key) { shelving.value = null; return; }
  shelving.value = a.key; alarmOpen.value = null; alarmHist.value = null;
  shelveMin.value = Number(alarmMeta.value.shelve_default_min || 30);
  shelveWhy.value = "";
}
function doShelve(a) {
  postAlarm("/alarms/shelve",
            { key: a.key, minutes: Number(shelveMin.value) || 30, reason: shelveWhy.value.slice(0, 200) },
            "已搁置：到期会自己回到活动态（搁置不影响互锁与就绪闸门）");
  shelving.value = null;
}
function doUnshelve(a) {
  postAlarm("/alarms/unshelve", { key: a.key }, "已取消搁置：这条回到活动态");
}

async function toggleAlarmDetail(a) {
  if (alarmOpen.value === a.key) { alarmOpen.value = null; alarmHist.value = null; return; }
  alarmOpen.value = a.key; alarmHist.value = []; shelving.value = null;
  try {
    // ★ 折叠掉的原始条目必须还查得到（明细接口），列表折叠 ≠ 丢数据
    const r = await fetch(apiUrl(`/alarms/history?key=${encodeURIComponent(a.key)}&limit=50`));
    alarmHist.value = r.ok ? ((await r.json()).items || []) : null;
  } catch (e) {
    alarmHist.value = null;
  }
}

// ---------------- 事件取证包（A5：danger/hit 上升沿自动落盘） ----------------
// ★★ 刷新水位用 `latest_seq`，**不用 packs.length**：列表会被 keep_days / max_packs
//   裁短，长度不是单调的（§5.5 铁律①，和当年 log.length 满 240 行后水位失效同一个坑）。
// ★ honest_gaps 逐条显示"这次没采到什么、为什么"—— 缺就说缺，界面不许替后端圆场。
const packs = ref([]);
const packTotal = ref(0);
const packLatestSeq = ref(0);
const packBusy = ref(false);
const packOpen = ref(null);           // 展开详情的包 id（null = 都没展开）
const packDetail = ref(null);

async function loadPacks() {
  if (!auth.controlActive) { packs.value = []; packTotal.value = 0; return; }
  const r = await apiControl("/safety/evidence?limit=30");
  if (r.status === 401 || r.status === 403) { packs.value = []; return; }
  if (!r.ok) return;                  // 读不了就保留上一次列表，不空成"没有取证"
  const d = await r.json();
  packs.value = d.packs || [];
  packTotal.value = d.total || 0;
  if ((d.latest_seq || 0) > packLatestSeq.value) packLatestSeq.value = d.latest_seq;
}

async function togglePack(p) {
  if (packOpen.value === p.id) { packOpen.value = null; packDetail.value = null; return; }
  packOpen.value = p.id; packDetail.value = null; packBusy.value = true;
  try {
    const r = await apiControl(`/safety/evidence/${encodeURIComponent(p.id)}`);
    if (r.status === 401 || r.status === 403) return auth.requestLogin();
    packDetail.value = r.ok ? await r.json() : null;
    if (!r.ok) packDetail.value = { ok: false, error: (await r.json().catch(() => ({}))).detail };
  } finally { packBusy.value = false; }
}

async function downloadPack(p) {
  packBusy.value = true;
  try {
    const r = await apiControl(`/safety/evidence/${encodeURIComponent(p.id)}/zip`);
    if (r.status === 401 || r.status === 403) return auth.requestLogin();
    if (!r.ok) return;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${p.id}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } finally { packBusy.value = false; }
}

function packSize(b) {
  const n = Number(b || 0);
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)}MB` : `${Math.round(n / 1024)}KB`;
}

// ★ 三态值不许塌成布尔（§5.5 铁律③）：null = "不知道"，不能显示成"否/关"
function tri(v, yes = "是", no = "否") {
  if (v === null || v === undefined) return "未知";
  return v ? yes : no;
}
function num(v, unit = "") {
  return (v === null || v === undefined) ? "—" : `${v}${unit}`;
}

/** 把 manifest 压成人能读完的几行（缺的字段显示 "—"，不填空成 0）。 */
function packDump(d) {
  const m = (d && d.manifest) || {};
  const t = m.trigger || {}, f = m.fence || {};
  const r = m.robot || {}, pf = r.pose_frame || {}, rc = r.rc_status || {};
  const srv = f.server || {}, cli = f.client || {}, sp = m.speed || {};
  const L = [];
  L.push(`触发：${t.word || `${num(t.from_state)}→${num(t.to_state)}`} · 来源 ${t.source || "—"} · `
         + `区域 ${t.zone_name || t.zone_id || "—"} · 间隙 ${num(t.clearance_mm, "mm")}`);
  L.push(`围栏模式：${f.mode || "—"}${f.mode_reason ? `（${f.mode_reason}）` : ""} · `
         + `配置互锁 ${tri(f.interlocked, "开", "关")} · 合并结论 ${f.merged_state || "unknown"}`
         + `（来自 ${f.merged_source || "无一路"}）`);
  L.push(`后端独立评估：${srv.state || "未评估"} · 新鲜度 ${srv.freshness || "never"} · `
         + `实测 ${tri(srv.measured)} · 已标定 ${tri(srv.calibrated)}`);
  L.push(`前端上报：${cli.reported ? cli.state : "未上报"} · 新鲜度 ${cli.freshness || "never"}`);
  L.push(`实时位姿：J ${pf.joints ? pf.joints.map((v) => num(v)).join(" ") : "—"} · `
         + `TCP ${pf.tcp ? `x${num(pf.tcp.x)} y${num(pf.tcp.y)} z${num(pf.tcp.z)}` : "—"} · `
         + `陈旧帧 ${tri(pf.stale, "是（关节不代表控制器当前位置）", "否")}`);
  L.push(`控制器寄存器：伺服 ${tri(rc.servo_on, "上电", "未上电")} · `
         + `急停位 ${tri(rc.estop_bit, "按下", "未按下")} · 报警位 ${tri(rc.alarm_bit, "有", "无")} · `
         + `程序 ${num(rc.prog)}/服务程序 ${num(rc.service_program)}`);
  L.push(`速度口径：界面滑块 ${num(sp.ui_slider_pct, "%")}（只读，取证不改速度）· `
         + `控制器回读 40003 ${num(rc.controller_speed_pct_40003, "%")}`);
  L.push(`样本 ${(m.samples || {}).count ?? "—"} 条（窗口 ${(m.samples || {}).window_seconds ?? "—"}s）· `
         + `事件 ${(m.events || {}).count ?? "—"} 条 · 单帧 ${m.frame ? m.frame : "未采到"}`);
  if (m.disclaimer) L.push(m.disclaimer);
  return L.join("\n");
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
        // ★ 后端刚落盘就立刻刷新取证列表（不靠轮询撞运气）
        if (m.action === "safety.evidence_written") loadPacks();
        // ★ 报警台账同理：报警刚落盘就去读**水位**（loadAlarms 内部比 latest_seq，
        //   没变就立刻返回）。同源事件会连排到达 → 800ms 合并一次，不去逐条打接口。
        kickAlarms();
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
let tickN = 0;
onMounted(() => {
  refreshAll();
  loadHealth();
  loadVersions();
  loadPacks();
  loadAlarms();                 // ★ 报警台账：读接口免令牌，没登录也必须看得见围栏 hit
  connectEvents();
  // 健康/统计走轮询兜底（WS 只推事件帧）；取证列表 10s 一次（它读盘，不必 5s）
  tick = setInterval(() => {
    loadHealth();
    if (++tickN % 2 === 0) loadPacks();
  }, 5000);
  // 报警 3s 一次：它只在**水位变了**时才动列表（loadAlarms 内部比 latest_seq）
  alarmTick = setInterval(loadAlarms, 3000);
});

// 挂载时若还没有控制权限，loadVersions 会被 apiControl 在本地拦下（不发请求、不打 401）；
// 拿到权限后再补一次，用户不必为了看围栏版本去刷新页面。
watch(() => auth.controlActive, (v) => { if (v) { loadVersions(); loadPacks(); } });
onBeforeUnmount(() => {
  if (tick) clearInterval(tick);
  if (alarmTick) clearInterval(alarmTick);
  if (alarmDeb) { clearTimeout(alarmDeb); alarmDeb = null; }
  if (deb) { clearTimeout(deb); deb = null; }   // ★ 2026-10-11：卸载时也要清掉输入防抖定时器
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

    <!-- ===== ★ B1 运行指标（当前值 + 60s 最大 + 采样时刻；只读，零下发） ===== -->
    <MetricsPanel />

    <!-- ===== ★ E6 机器人自述 · 每天一页现场日报（只读，零 LLM、零下发、数字可追溯） ===== -->
    <DailyReportCard />

    <!-- ===== ★ E4 判色体检（样本环 + 每色统计 + 人工复核 + 提案账目；applied 恒 false） ===== -->
    <ColorAdviceCard />

    <!-- ===== 报警台账（C2：分级 / 折叠去重 / 延时确认 / 搁置 / 确认） ===== -->
    <div class="ops-sec">
      <div class="ops-head">
        <Icon name="siren" :size="16" />
        <span class="ops-title">报警台账</span>
        <span v-if="!alarmRead" class="ops-chip">状态未知</span>
        <template v-else>
          <span v-for="p in ['critical', 'high', 'unclassified', 'medium', 'low']" :key="p"
                class="ops-chip" :class="prioClass(p)">
            {{ prioText(p) }} {{ alarmMeta.by_priority?.[p] || 0 }}
          </span>
          <span class="ops-chip">候选 {{ alarmMeta.pending || 0 }}</span>
          <span class="ops-chip">搁置 {{ alarmMeta.shelved || 0 }}</span>
          <span class="ops-chip">安全类 {{ alarmMeta.safety_class || 0 }}</span>
        </template>
        <span class="ops-chip" title="只增不减的水位：刷新看它，不看列表长度">水位 #{{ alarmSeq }}</span>
        <span class="ops-spacer"></span>
        <button class="ops-btn" :disabled="alarmBusy" @click="loadAlarms(true)">
          <Icon name="refresh" :size="14" /> 刷新
        </button>
      </div>

      <div class="card">
        <p class="small">
          一条报警 = 一个<b>待处置的故障</b>：同源重复在 {{ num(alarmMeta.window_ms, "ms") }} 窗内
          <b>折叠成一条</b>（被折叠的逐条都在「明细」里，一条不丢）。
          <b>确认只清显示</b>，不解除任何互锁 / 就绪闸门 / 扫掠结论；<b>安全类禁止搁置</b>；
          抖动型要先连续存在才升级（标「候选中」）。配色按 ISA-101 一色一义：<b>不闪、不用多彩</b>。
        </p>
        <p v-if="alarmStale" class="al-warn">{{ alarmStale }}</p>
        <p v-if="alarmMsg" class="al-msgline">{{ alarmMsg }}</p>

        <div v-if="!alarmRead" class="ops-empty">
          未知 —— 台账一次都没读到过（<b>读不到 ≠ 没有报警</b>）
        </div>
        <div v-else-if="!alarms.length" class="ops-empty">
          当前没有活动报警（水位 #{{ alarmSeq }}；有没有新状态看水位，不看列表长度）
        </div>
        <div v-else class="al-list">
          <div v-for="a in alarms" :key="a.key" class="al-item"
               :class="[prioClass(a.priority), alarmStateClass(a)]">
            <div class="al-row">
              <span class="al-prio" :class="prioClass(a.priority)">{{ prioText(a.priority) }}</span>
              <span class="al-title" :title="a.key">{{ a.title || a.key }}</span>
              <span class="al-count"
                    :title="`本轮出现 ${a.count} 次，其中 ${Math.max(0, (a.count || 1) - 1)} 条被折叠（明细里都在）`">
                ×{{ a.count ?? "—" }}
              </span>
              <span class="al-state" :class="alarmStateClass(a)">{{ alarmStateText(a) }}</span>
              <span v-if="a.safety_class" class="al-tag safe">安全类</span>
              <span v-if="a.priority_source === 'operator'" class="al-tag"
                    :title="a.priority_reason || ''">人工分级 · {{ a.priority_set_by || "未知" }}</span>
              <span v-else-if="a.priority_source === 'unregistered'" class="al-tag"
                    title="字典里没登记过这条：不知道确切后果，照实显示，不猜也不降级">未登记</span>
              <span class="al-spacer"></span>
              <button class="ops-btn" :disabled="alarmBusy" @click="ackAlarm(a)">确认</button>
              <button v-if="a.shelved === true" class="ops-btn" :disabled="alarmBusy"
                      @click="doUnshelve(a)">取消搁置</button>
              <button v-else class="ops-btn" :class="{ no: a.can_shelve === false }"
                      :disabled="alarmBusy || a.can_shelve === false"
                      :title="a.can_shelve === false ? '安全类报警禁止搁置（后端判据，前端不复算）' : '临时搁置，到期自动回到活动态'"
                      @click="openShelve(a)">搁置</button>
              <button class="oi-more" :title="alarmOpen === a.key ? '收起明细' : '看被折叠的原始条目'"
                      @click="toggleAlarmDetail(a)">
                <Icon :name="alarmOpen === a.key ? 'chevronUp' : 'chevronDown'" :size="13" />
              </button>
            </div>

            <div class="al-body">
              <p><b>后果</b>{{ a.consequence || "未知" }}</p>
              <p><b>要你做的动作</b>{{ a.action_required || "未知" }}</p>
              <p class="al-meta">
                首次 {{ fmtTime(a.first_at) || "—" }} · 最近 {{ fmtTime(a.last_at) || "—" }}
                （已持续 {{ durText(a.duration_sec) }}）· 折叠 {{ a.suppressed ?? "—" }} 条 ·
                来源 {{ a.source || "—" }} ·
                {{ a.delay_ms ? `需连续 ${a.delay_ms}ms 才升级` : "无延时（立刻可见）" }}
              </p>
              <p v-if="a.note" class="al-note">{{ a.note }}</p>
            </div>

            <div v-if="shelving === a.key" class="al-shelve">
              <span>搁置</span>
              <input v-model.number="shelveMin" type="number" min="0.1" max="1440" step="1" />
              <span>分钟</span>
              <input v-model="shelveWhy" class="al-why" maxlength="200"
                     placeholder="为什么搁置（会写进审计）" />
              <button class="ops-btn primary" :disabled="alarmBusy" @click="doShelve(a)">确定</button>
              <button class="ops-btn" @click="shelving = null">取消</button>
              <span class="small">到期自己回到活动态；搁置改不了互锁与就绪闸门。</span>
            </div>

            <div v-if="alarmOpen === a.key" class="al-detail">
              <p v-if="alarmHist === null" class="small">
                明细读取失败（列表照旧；被折叠的原始条目也仍在审计事件表里）
              </p>
              <p v-else-if="!alarmHist.length" class="small">这条还没有逐条明细（不该发生，请报告）</p>
              <div v-for="h in (alarmHist || [])" :key="h.seq" class="al-hitem">
                <span class="oi-t">{{ fmtTime(h.at_iso) }}</span>
                <span class="al-hstate" :class="h.state">{{ HSTATE[h.state] || h.state || "未知" }}</span>
                <span class="al-hmsg">{{ h.message }}</span>
                <span v-if="h.event_id" class="al-eid" title="审计事件号，可在下方时间线里搜到">#{{ h.event_id }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 事件取证包（A5：danger/hit 上升沿自动落盘） ===== -->
    <div class="ops-sec">
      <div class="ops-head">
        <Icon name="shieldCheck" :size="16" />
        <span class="ops-title">事件取证包</span>
        <span class="ops-chip">共 {{ packTotal }} 包 · 最新序号 #{{ packLatestSeq }}</span>
        <span class="ops-spacer"></span>
        <button class="ops-btn" @click="loadPacks()"><Icon name="refresh" :size="14" /> 刷新</button>
      </div>
      <div class="card">
        <p class="small">
          每次 danger/hit <b>上升沿</b>自动落一盘：事发前后的实时样本环 + 事件 + 围栏三态 + 相机单帧。
          只记本机采到的数，<b>不替代控制器安全板</b>；<b>缺什么在包里逐条写明</b>（黄字那几行就是「这次没采到」）。
        </p>
        <div v-if="!packs.length" class="small">
          暂无取证包{{ auth.controlActive ? "（没触发过 danger/hit 就是没有，不会凭空造）" : "（需控制权限才能读）" }}
        </div>
        <div v-else class="ops-packs">
          <div v-for="p in packs" :key="p.id" class="ops-pack" :class="p.to_state">
            <div class="opk-row">
              <span class="opk-seq">#{{ p.seq }}</span>
              <span class="opk-t">{{ fmtTime(p.captured_at_utc) }}</span>
              <span class="opk-lv" :class="p.to_state">{{ p.to_state === "hit" ? "撞击" : "危险" }}</span>
              <span class="opk-zone">{{ p.zone_name || p.zone_id || "未知区域" }}</span>
              <span class="opk-meta">
                {{ p.word || `${p.from_state || "?"}→${p.to_state}` }} ·
                间隙 {{ p.clearance_mm ?? "—" }}mm · 样本 {{ p.samples ?? "—" }} ·
                帧 {{ p.frame ? "有" : "无" }} · {{ packSize(p.bytes) }}
              </span>
              <span v-if="p.honest_gaps && p.honest_gaps.length" class="opk-gap">
                缺 {{ p.honest_gaps.length }}
              </span>
              <button class="ops-btn" :disabled="packBusy" @click="downloadPack(p)">
                <Icon name="download" :size="13" /> 整包
              </button>
              <button class="oi-more" :title="packOpen === p.id ? '收起' : '看包内清单'"
                      @click="togglePack(p)">
                <Icon :name="packOpen === p.id ? 'chevronUp' : 'chevronDown'" :size="13" />
              </button>
            </div>
            <ul v-if="p.honest_gaps && p.honest_gaps.length" class="opk-gaps">
              <li v-for="(g, gi) in p.honest_gaps" :key="gi">{{ g }}</li>
            </ul>
            <div v-if="packOpen === p.id" class="opk-detail">
              <p v-if="packBusy" class="small">正在读取…</p>
              <p v-else-if="!packDetail || packDetail.ok === false" class="small err">
                {{ (packDetail && packDetail.error) || "详情读取失败" }}
              </p>
              <template v-else>
                <div class="opk-files">
                  <span v-for="f in packDetail.files" :key="f" class="opk-file">{{ f }}</span>
                </div>
                <pre class="oi-detail">{{ packDump(packDetail) }}</pre>
              </template>
            </div>
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
        <span class="ops-chip">共 {{ stats.total }} 条（含信息级）</span>
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

/* ---- 报警台账（C2）：ISA-101 克制配色 —— 一色一义、不闪动、不用多彩 ----
   critical=红（沿用全局 --err）· high=橙 · medium=黄 · low=蓝灰 · 未分级=中性灰+虚线。
   ★ 颜色只是快捷线索，每一档同时写中文标签（看不见颜色也必须读得懂）。
   ★ 这里**没有任何 animation / keyframes / 闪烁**：闪动在控制室里是干扰源。 */
.al-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px;
  max-height: 430px; overflow-y: auto; }
.al-item { border: 1px solid var(--line); border-left: 4px solid var(--muted);
  border-radius: 6px; padding: 5px 8px; background: var(--panel2); }
.al-item.p-critical { border-left-color: var(--err); background: rgba(255, 77, 109, 0.07); }
.al-item.p-high { border-left-color: #e8873a; background: rgba(232, 135, 58, 0.06); }
.al-item.p-medium { border-left-color: #d9c14a; background: rgba(217, 193, 74, 0.05); }
.al-item.p-low { border-left-color: #8ea3c4; background: rgba(142, 163, 196, 0.04); }
.al-item.p-unknown { border-left-color: #9aa7bd; border-left-style: dashed; }
/* 候选 / 搁置只做**边缘弱化**，不整行涂黑到看不见（还得读得清它在等什么） */
.al-item.pend { opacity: .86; }
.al-item.shel { opacity: .7; }
.al-row { display: flex; align-items: center; gap: 7px; font-size: 12px; }
.al-prio { flex: none; font-size: 10px; text-align: center; border-radius: 4px;
  padding: 1px 6px; border: 1px solid var(--line); color: var(--muted); }
.al-prio.p-critical { color: var(--ink); background: var(--err); border-color: var(--err); }
/* 橙 / 黄 / 蓝灰这三档底色亮 → 字用深色（--bg）才读得清；critical 沿用全站「红底白字」 */
.al-prio.p-high { color: var(--bg); background: #e8873a; border-color: #e8873a; }
.al-prio.p-medium { color: var(--bg); background: #d9c14a; border-color: #d9c14a; }
.al-prio.p-low { color: var(--bg); background: #8ea3c4; border-color: #8ea3c4; }
.al-prio.p-unknown { color: var(--txt); background: transparent;
  border-color: #9aa7bd; border-style: dashed; }
.al-title { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-weight: 600; }
.al-count { flex: none; color: var(--muted); font-variant-numeric: tabular-nums; }
.al-state { flex: none; font-size: 10px; border: 1px solid var(--line); border-radius: 4px;
  padding: 1px 5px; color: var(--muted); white-space: nowrap; }
.al-state.pend { color: var(--warn); border-color: var(--warn-line); }
.al-state.shel { color: var(--txt2); }
.al-tag { flex: none; font-size: 10px; border: 1px solid var(--line); border-radius: 4px;
  padding: 1px 5px; color: var(--muted); white-space: nowrap; }
.al-tag.safe { color: var(--err); border-color: var(--err-line); }
.al-spacer { flex: none; width: 4px; }
.ops-btn.no { color: var(--muted); }
.al-body { display: flex; flex-direction: column; gap: 2px; margin: 4px 0 0 54px; font-size: 12px; }
.al-body p { margin: 0; color: var(--txt); }
.al-body b { color: var(--muted); font-weight: 500; font-size: 11px; margin-right: 6px; }
.al-meta { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.al-note { color: var(--warn); font-size: 11px; }
.al-warn { margin: 6px 0 0; font-size: 12px; color: var(--warn); }
.al-shelve { display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: 6px 0 0 54px; font-size: 12px; color: var(--muted); }
.al-shelve input[type="number"] { width: 66px; background: var(--panel3); color: var(--txt);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px 6px; font-size: 12px; }
.al-why { flex: 1; min-width: 150px; background: var(--panel3); color: var(--txt);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; font-size: 12px; }
.al-detail { display: flex; flex-direction: column; gap: 3px; margin: 6px 0 0 54px;
  border: 1px solid var(--line); border-radius: 6px; background: var(--bg);
  padding: 6px; max-height: 220px; overflow-y: auto; }
.al-hitem { display: grid; grid-template-columns: 84px 84px 1fr auto; gap: 8px;
  align-items: center; font-size: 11px; }
.al-hstate { text-align: center; border: 1px solid var(--line); border-radius: 4px;
  padding: 1px 4px; color: var(--muted); }
.al-hstate.pending { color: var(--warn); border-color: var(--warn-line); }
.al-hstate.cleared { color: var(--ok); border-color: var(--ok-line); }
.al-hstate.shelve_expired { color: var(--warn); }
.al-hmsg { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.al-eid { color: var(--txt2); font-variant-numeric: tabular-nums; }

/* ---- 事件取证包（A5）---- */
.ops-packs { display: flex; flex-direction: column; gap: 5px; margin-top: 8px;
  max-height: 300px; overflow-y: auto; }
.ops-pack { border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; }
.ops-pack.hit { border-color: var(--err-line); background: var(--veil); }
.opk-row { display: grid; grid-template-columns: 46px 84px 40px 96px 1fr auto auto 26px;
  align-items: center; gap: 8px; font-size: 12px; }
.opk-seq { color: var(--accent); font-weight: 700; font-variant-numeric: tabular-nums; }
.opk-t { color: var(--muted); font-variant-numeric: tabular-nums; }
.opk-lv { font-size: 10px; text-align: center; border-radius: 4px; padding: 1px 4px;
  border: 1px solid var(--warn-line); color: var(--warn); }
.opk-lv.hit { color: var(--ink); background: var(--err); border-color: var(--err); }
.opk-zone { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.opk-meta { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-variant-numeric: tabular-nums; }
.opk-gap { font-size: 10px; color: var(--warn); border: 1px solid var(--warn-line);
  border-radius: 4px; padding: 1px 5px; white-space: nowrap; }
.opk-gaps { margin: 4px 0 0; padding-left: 18px; list-style: none; }
.opk-gaps li { font-size: 11px; color: var(--warn); line-height: 1.5; }
.opk-gaps li::before { content: "▸ "; }
.opk-detail { margin-top: 6px; }
.opk-files { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 5px; }
.opk-file { font-size: 10px; color: var(--muted); border: 1px solid var(--line);
  border-radius: 4px; padding: 1px 6px; }
.opk-detail .oi-detail { max-height: 220px; overflow-y: auto; }

@media (max-width: 1200px) {
  .ops-split { grid-template-columns: 1fr; }
  .ops-item { grid-template-columns: 72px 46px 44px 1fr auto 26px; }
  .oi-actor { display: none; }
  .opk-row { grid-template-columns: 42px 78px 38px 1fr auto auto 26px; }
  .opk-zone { display: none; }
  .al-body, .al-shelve, .al-detail { margin-left: 0; }
  .al-hitem { grid-template-columns: 72px 72px 1fr; }
  .al-eid { display: none; }
}
</style>
