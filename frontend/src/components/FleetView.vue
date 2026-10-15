<script setup>
// 机房大屏：12 台机器人同框 + 每台的六轴坐标 / IP / 状态。
//
// 版式按用户给的 IDC 智能运维大屏参考图（2026-10-06 定稿）：
//   · 中间 2.5D 等轴机房是主角，面板**悬浮**在场景上，不把场景挤走
//   · 顶部：中文主标题 + 超大字距英文全大写小注 + 胶囊导航 + 时间戳
//   · 左右两列：状态环 / 设备详情 + 分区卡片（左 = A 区埃夫特，右 = B 区华沿）
//   · 底部：近乎全黑的 KPI 条，竖线分格，数值大、单位小且下挂
//   · 形态语言：切角多边形 + 角括号描边 + 钢蓝细线 + 低饱和外发光（不用圆角）
//
// ★ 只读：这一页没有任何下发入口（阶段一口径）。点卡片只做一件事 —— 相机推过去。
// ★ 数字与 3D 同源（都读 net/fleetBuffer 的 poseMap），不会画面对、读数错。
import { computed, ref, watch, onMounted, onBeforeUnmount } from "vue";
import { useFleetStore } from "../stores/fleet.js";
import { useRobotStore } from "../stores/robot.js";
import { useAuthStore } from "../stores/auth.js";
import { useRcReadyStore } from "../stores/rcReady.js";
import { announceOpen, annCfg } from "../services/announcer.js";
import { registerView, resizeView, fleetFocus, fleetOverview, fleetRealModel, fleetFit } from "../three/manager.js";
import { SCOPES as SCOPE_LIST, scopeIds as scopeIdsOf } from "../utils/fleetScope.js";
import FleetCard from "./FleetCard.vue";
import FleetConsole from "./FleetConsole.vue";
import FleetSettings from "./FleetSettings.vue";
import AnnouncePanel from "./AnnouncePanel.vue";
import Icon from "./Icon.vue";
import { BRAND } from "../brand.js";

const fleet = useFleetStore();
const robot = useRobotStore();
const auth = useAuthStore();
const rc = useRcReadyStore();
const viewEl = ref(null);
const sel = ref("");                        // 选中的工位 id（点卡片 → 相机推到那台前面）
const settingsOpen = ref(false);            // 机房设置抽屉（改登记表）

const GROUP_LABEL = { lower: "下排", upper: "上排" };
const LEVEL_LABEL = {
  safe: "安全", warn: "接近围栏", danger: "危险", hit: "碰撞",
  alarm: "控制器报警", offline: "离线",
};

const SCOPES = SCOPE_LIST;

/** 组块标题：跨区看时写区名，单看一个区时写排名。 */
/** 列头：跨区看时是"A 区 / B 区"，单看一个区时是"上排 / 下排"。 */
function colTitle(side) {
  const byGroup = fleet.scope === "zoneA" || fleet.scope === "zoneB";
  if (byGroup) {
    return side === "left" ? { zh: "上排", en: "ROW UPPER" } : { zh: "下排", en: "ROW LOWER" };
  }
  return side === "left"
    ? { zh: "A 区", en: "ZONE A · 埃夫特 EFORT" }
    : { zh: "B 区", en: "ZONE B · 华沿 HUAYAN" };
}

function blockTitle(b) {
  if (fleet.scope === "zoneA" || fleet.scope === "zoneB") return GROUP_LABEL[b.group] || b.group;
  return `${b.zone} 区 · ${GROUP_LABEL[b.group] || b.group}`;
}

function pickScope(k) {
  if (fleet.scope === k) return;
  fleet.setScope(k);
  applyScope();
}

/** 换作用域 = 相机推拉 + 必要时取消选中（看不见就不该还能操作它）。 */
function applyScope() {
  const ids = scopeIdsOf(fleet.cards, fleet.scope);
  if (sel.value && !ids.includes(sel.value)) { sel.value = ""; useReal(""); }
  fleetFit(ids);
}

/** 顶部 KPI / 状态环：优先用后端 1Hz 汇总，没有就自己数卡片（口径一致，只是慢一点）。 */
/**
 * KPI 一律按**当前作用域**自己数。
 * ★ 不用后端那份 1Hz 汇总：它是全场口径，切到"A 区"还显示 12 台的数字等于在骗人，
 *   而屏幕上明明只有 6 张卡。卡片本身也是从同一批帧数据来的，口径不会漂。
 */
const kpi = computed(() => {
  const cards = fleet.scoped;
  const n = (f) => cards.filter(f).length;
  return {
    total: cards.length,
    online: n((c) => c.online),
    offline: n((c) => !c.online),
    alarm: n((c) => c.level === "alarm"),
    danger: n((c) => c.level === "hit" || c.level === "danger"),
    moving: n((c) => c.moving),
    rate: Math.round(1000 / (Number(fleet.streaming.batch_interval_ms) || 50)),
  };
});

const selCard = computed(() => fleet.cards.find((c) => c.id === sel.value) || null);

/**
 * 选中那台的六轴限位档案（joint_limits_display）。
 * ★ 必须按**型号**取，不能沿用单机页那份 ER8 的全局限位：
 *   把埃夫特的行程套到华沿滑块上，滑块能拖到一个真机根本到不了、
 *   或者反过来早已超限的位置 —— 那是会出事的"看着正常"。
 */
const selLimits = computed(() => {
  const c = selCard.value;
  if (!c) return [];
  const prof = (fleet.kinematics || {})[c.model] || {};
  return Array.isArray(prof.joint_limits_display) ? prof.joint_limits_display : [];
});

/** 播报按钮的"关"态：整体静音或音效语音都关了，图标要跟着变。 */
const annOff = computed(() => !annCfg.value.enabled
  || (!annCfg.value.sound && !annCfg.value.voice));

/** 上一次开了真网格的那台（LOD 只允许一台开，见 three/fleet.js 的 setReal）。 */
let realId = "";
function useReal(id) {
  if (realId === id) return;
  if (realId) fleetRealModel(realId, false);
  realId = id || "";
  if (realId) fleetRealModel(realId, true);
}

/**
 * 选中一台 = 申请"只控这一台"的作用域（有令牌时）。
 * ★ 为什么在选人的时候就去申请，而不是点执行的时候：作用域是服务端状态，
 *   点执行才申请会让"我明明选的是 A2、却被 B1 的作用域拦下"变成运行时才发现。
 *   失败也不挡界面 —— 控制台会把拒绝原因原样列出来。
 */
function ensureScope(id) {
  if (!auth.controlActive || !id) return;
  fleet.acquireScope({ kind: "single", robot_id: id }).catch(() => { /* 原因在控制台里显示 */ });
}

function pick(c) {
  // 点机器 = 要看这台：设置抽屉压在同一侧，开着会挡住卡片与刚选中的那台
  if (settingsOpen.value) settingsOpen.value = false;
  if (sel.value === c.id) { overview(); return; }
  sel.value = c.id;
  useReal(c.id);
  ensureScope(c.id);
  fleetFocus(c.id);
}
function overview() {
  sel.value = "";
  useReal("");
  if (auth.controlActive && fleet.session) fleet.releaseScope();
  fleetOverview();
}

// ---- 3D 容器登记 + 尺寸跟随 ----
// 不用 MonitorLayout（那是「左视口 + 右侧栏」的壳），大屏要的是面板悬浮在场景上，
// 所以这里自己登记容器、自己挂 ResizeObserver。
let ro = null;
onMounted(() => {
  registerView("fleet", viewEl.value);
  // 首帧按"全部"取景；等位姿到货后 watch 会再框一次（名单是异步拉的）
  applyScope();
  if (typeof ResizeObserver !== "undefined" && viewEl.value) {
    ro = new ResizeObserver(() => {
      // 只在自己是当前视图时重算：隐藏视图的 clientWidth 是 0，算了也是白算
      if (robot.activeView === "fleet") resizeView();
    });
    ro.observe(viewEl.value);
  }
});
watch(() => fleet.stations, () => { applyScope(); });

onBeforeUnmount(() => {
  useReal("");
  if (ro) { ro.disconnect(); ro = null; }
});
</script>

<template>
  <div class="view fv" ref="viewEl">
    <!-- ================= 顶部：标题 + 刷新（就一个按钮，不放导航） ================= -->
    <div class="fv-top">
      <div class="fv-brand">
        <div class="fv-brand-t">
          <!-- 圆形校徽：图在 frontend/public/logo.png，必须用**根路径** /logo.png。
             写成 ./logo.png 会被 Vite 解析成 src/components/logo.png（不存在）→ 图标碎掉。 -->
        <img class="fv-logo" src="/logo.png" alt="校徽" />
          <b>{{ BRAND.name }}({{ BRAND.room }})</b>
        </div>
        <span>ROBOT FLEET INTELLIGENT OPERATION AND MAINTENANCE PLATFORM</span>
      </div>
      <!-- 作用域：一页看哪几台。换页 = 相机推拉 + 两列内容变化，不是换一套模板。 -->
      <nav class="fv-scopes">
        <button v-for="k in SCOPES" :key="k.key" class="sc" :class="{ on: fleet.scope === k.key }"
                :title="k.hint" @click="pickScope(k.key)">
          {{ k.label }}<em>{{ k.en }}</em>
        </button>
      </nav>

      <div class="fv-acts">
        <!-- 控制授权：原来在底栏，底栏随单页化一起删了，入口搬到这里。
             ★ 只读模式是默认态 —— 没令牌时这页什么都不许动（见 FleetConsole）。 -->
        <span v-if="auth.controlActive" class="fv-auth" :class="{ op: !auth.isAdmin }"
              :title="auth.isAdmin
                ? '管理员令牌：可操控机器人、改围栏配置'
                : '操作员令牌：只能操控机器人，改配置需管理员'">
          <Icon name="shield" :size="13" />
          {{ rc.ready ? "控制中" : "已授权 · 待就绪" }}
          <em v-if="auth.roleLabel"> · {{ auth.roleLabel }}</em>
          <b> · {{ auth.ttlText }}</b>
        </span>
        <button v-if="auth.controlActive" class="fv-ghost-btn" @click="auth.logout()"
                title="释放控制权限">
          <Icon name="power" :size="13" /> 释放
        </button>
        <button v-else class="fv-auth-btn" :disabled="!auth.required"
                @click="auth.requestLogin()" title="连接真机控制前需管理员密码授权">
          <Icon name="lock" :size="13" /> 申请控制
        </button>
        <button class="fv-ico-btn" :class="{ off: annOff }" @click="announceOpen = !announceOpen"
                :title="annOff ? '播报已关闭（点击配置）' : '播报中心：音效 / 中文语音'">
          <Icon :name="annOff ? 'volumeOff' : 'volume'" :size="14" />
        </button>
      </div>
    </div>

    <!-- ================= 左列：运行状态 + A 区 ================= -->
    <!-- ★ 控制台是**浮在左列外侧的独立盒子**，不塞进卡片列表里：
         插在列表里会把 A 区卡片整体往下顶，选中一台就等于把列表挪一次位置，
         想再点隔壁台子得先找它被挤到哪儿去了。 -->
    <div v-if="selCard" class="fv-pop">
      <button class="fv-pop-x" @click="overview()" title="关闭并回到整间机房">
        <Icon name="close" :size="13" />
      </button>
      <FleetConsole :key="selCard.id"
                    :card="selCard" :limits="selLimits" :auth-active="auth.controlActive"
                    :write-ready="fleet.write.ready"
                    :write-note="fleet.write.reason"
                    @sim="fleet.setSimDriven($event[0], $event[1])"
                    @request-control="auth.requestLogin()" />
    </div>

    <div class="fv-col left">
      <div class="fv-zone-h"><b>{{ colTitle("left").zh }}</b><span>{{ colTitle("left").en }}</span></div>
      <div v-for="g in fleet.columns.left" :key="g.key" class="fv-grp">
        <div class="fv-grp-h">{{ blockTitle(g) }}<em>{{ g.cards.length }} 台</em></div>
        <FleetCard v-for="c in g.cards" :key="c.id" :c="c" :sel="sel === c.id"
                   :level-label="LEVEL_LABEL[c.level] || c.level" @pick="pick(c)" />
      </div>
    </div>

    <!-- ================= 右列 ================= -->
    <div class="fv-col right">
      <div class="fv-zone-h"><b>{{ colTitle("right").zh }}</b><span>{{ colTitle("right").en }}</span></div>
      <div v-for="g in fleet.columns.right" :key="g.key" class="fv-grp">
        <div class="fv-grp-h">{{ blockTitle(g) }}<em>{{ g.cards.length }} 台</em></div>
        <FleetCard v-for="c in g.cards" :key="c.id" :c="c" :sel="sel === c.id"
                   :level-label="LEVEL_LABEL[c.level] || c.level" @pick="pick(c)" />
      </div>
    </div>

    <!-- ================= 异常提示（左下角 toast） ================= -->
    <div class="fv-notes">
      <div v-if="!fleet.robots.length" class="fv-note err">
        <Icon name="alert" :size="12" />
        还没拿到机房名单{{ fleet.loadError ? "：" + fleet.loadError : "（后端 /api/fleet 不可达，正在每秒重试）" }}
      </div>
      <div v-else-if="!fleet.running" class="fv-note warn">
        <Icon name="alert" :size="12" />
        机房采集未启动：画面是名单摆位、读数为零。启用见 config/fleet.yaml 的 fleet.enabled
      </div>
      <div v-else-if="fleet.unbuilt.length" class="fv-note warn">
        <Icon name="alert" :size="12" />
        {{ fleet.unbuilt.length }} 个工位在 3D 里建不出来：
        <code>{{ fleet.unbuilt.map((u) => (u.id || u.model) + "(" + u.reason + ")").join("、") }}</code>
      </div>
    </div>

    <!-- ================= 底部 KPI 条（近黑底 + 竖线分格） ================= -->
    <div class="fv-kpi">
      <div class="k">
        <i>机器人总数 TOTAL</i>
        <b>{{ kpi.total }}<em>台</em></b>
      </div>
      <div class="k">
        <i>在线 ONLINE</i>
        <b class="ok">{{ kpi.online }}<em>/{{ kpi.total }}</em></b>
      </div>
      <div class="k">
        <i>告警 ALARM</i>
        <b :class="kpi.alarm ? 'err blink' : ''">{{ kpi.alarm }}<em>台</em></b>
      </div>
      <div class="k">
        <i>围栏危险 FENCE</i>
        <b :class="kpi.danger ? 'err' : ''">{{ kpi.danger }}<em>台</em></b>
      </div>
      <div class="k">
        <i>运动中 MOVING</i>
        <b :class="kpi.moving ? 'acc' : ''">{{ kpi.moving }}<em>台</em></b>
      </div>
      <div class="k">
        <i>数据链路 LINK</i>
        <b :class="fleet.linkAlive ? 'ok' : 'err'">{{ fleet.linkAlive ? "正常" : "无数据" }}<em>{{ kpi.rate }}Hz</em></b>
      </div>
      <!-- 返回总览：原来立在标题条上的那颗大按钮收在这里，选中某台时才亮 -->
      <button class="fv-back" :class="{ on: !!sel }" @click="overview()"
              :disabled="!sel" :title="sel ? '回到整间机房' : '先点一台机器人'">
        <Icon name="expand" :size="13" />
      </button>
      <!-- 机房设置：改 12 台的 IP / 端口 / 名称 / 型号 / 区排号。放底栏是因为
           它不是"每帧都要看一眼"的东西，而顶栏已经给了作用域与授权。 -->
      <button class="fv-back" :class="{ on: settingsOpen }" @click="settingsOpen = !settingsOpen"
              title="机房设置：名称 / IP / 端口 / 型号 / 区排号（保存需管理员）">
        <Icon name="gear" :size="13" />
      </button>
    </div>

    <!-- 播报中心设置面板（顶栏右侧那个喇叭按钮打开） -->
    <AnnouncePanel :open="announceOpen" @close="announceOpen = false" />

    <!-- 机房登记表抽屉（底栏齿轮打开）：改完是"保存 → 后端重建 12 条链路 → 前端重刷名单" -->
    <FleetSettings :open="settingsOpen" @close="settingsOpen = false" />
  </div>
</template>

<style scoped>
/* 大屏是"面板浮在场景上"：容器本身透明，canvas 由 .view canvas 铺满（index.html） */
.fv { overflow: hidden; }
.fv-col { container-type: inline-size; }

/* ============ 顶部条（标题 + 一个刷新按钮） ============ */
.fv-top { position: absolute; left: 0; right: 0; top: 0; z-index: 22;
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 7px 14px;
  background: linear-gradient(180deg, var(--glass-solid), transparent);
  border-bottom: 1px solid var(--glass-line);
  pointer-events: none; }
.fv-top > * { pointer-events: auto; }

.fv-brand { flex: none; display: flex; flex-direction: column; min-width: 0; }
.fv-brand-t { display: flex; align-items: center; gap: 8px; }
/* 圆形校徽：不加边框，靠自身留白压住深色标题条 */
.fv-logo { width: 26px; height: 26px; border-radius: 50%; object-fit: contain; flex: none; }
.fv-brand b { font-size: 17px; font-weight: 700; letter-spacing: 4px; color: var(--txt);
  line-height: 1.1; text-shadow: 0 0 12px var(--glow); }
.fv-brand span { font-size: 7px; letter-spacing: 1.9px; text-transform: uppercase;
  color: var(--muted); margin-top: 3px; white-space: nowrap; }

/* 作用域选择器：斜切胶囊，激活的那颗亮青绿 */
.fv-scopes { display: flex; align-items: center; gap: 5px; flex: none; margin-left: auto; }
.fv-scopes .sc { display: inline-flex; flex-direction: column; align-items: center; gap: 1px;
  padding: 4px 11px; font-size: 11px; letter-spacing: .8px; color: var(--txt2);
  background: var(--cap); border: 1px solid var(--line); border-radius: 0;
  clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px);
  transition: color .14s ease, background .14s ease, border-color .14s ease; }
.fv-scopes .sc em { font-style: normal; font-size: 6.5px; letter-spacing: 1px;
  text-transform: uppercase; color: var(--muted); }
.fv-scopes .sc:hover { color: var(--accent2); border-color: var(--accent-line); }
.fv-scopes .sc.on { color: var(--accent); background: var(--accent-soft2);
  border-color: var(--accent-line); }
.fv-scopes .sc.on em { color: var(--accent2); }

/* 右侧动作组：授权状态 + 申请/释放 + 播报 + 刷新 */
.fv-acts { display: flex; align-items: center; gap: 7px; flex: none; }

.fv-auth { display: inline-flex; align-items: baseline; gap: 3px; font-size: 10px;
  letter-spacing: .5px; padding: 5px 9px; color: var(--ok);
  border: 1px solid var(--ok-line); background: var(--ok-soft); }
.fv-auth.op { color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft); }
.fv-auth em { font-style: normal; }
.fv-auth b { font-family: var(--num); font-weight: 700; }

/* 刷新：斜切两端的胶囊，与参考图的导航胶囊同一套形态 */
.fv-auth-btn, .fv-ghost-btn, .fv-ico-btn {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; letter-spacing: 1.2px; color: var(--accent);
  background: var(--accent-soft); border: 1px solid var(--accent-line); border-radius: 0;
  clip-path: polygon(7px 0, 100% 0, 100% calc(100% - 7px), calc(100% - 7px) 100%, 0 100%, 0 7px);
  transition: background .14s ease, color .14s ease; }
.fv-auth-btn, .fv-ghost-btn { padding: 6px 11px; }
.fv-ico-btn { padding: 6px 8px; color: var(--txt2); background: var(--cap);
  border-color: var(--line); }
.fv-ico-btn.off { color: var(--muted); }
.fv-auth-btn:hover:not(:disabled),
.fv-ghost-btn:hover:not(:disabled), .fv-ico-btn:hover { background: var(--accent-soft2);
  color: var(--accent2); }
.fv-auth-btn:disabled { opacity: .45; cursor: not-allowed; }

/* ============ 左右两列（悬浮在 3D 上） ============ */
.fv-col { position: absolute; top: 56px; bottom: 58px; z-index: 18;
  width: 252px; overflow-y: auto; overflow-x: hidden;
  padding: 8px 9px; display: flex; flex-direction: column; gap: 8px;
  overscroll-behavior: contain;
  background: linear-gradient(90deg, var(--scrim), transparent);
  pointer-events: none; }
.fv-col > * { pointer-events: auto; }
.fv-col.left { left: 0; }
.fv-col.right { right: 0; background: linear-gradient(270deg, var(--scrim), transparent); }

/* 面板：切角 + 角括号（参考图的辨识度所在） */

/* 在线率状态环：conic-gradient 画环，缺口自动被底色盖住 */
.fv-rows dd.mut { color: var(--muted); }

/* 分区标题 */
.fv-zone-h { display: flex; align-items: baseline; gap: 7px; flex: none;
  padding-bottom: 4px; border-bottom: 1px solid var(--accent-line); }
.fv-zone-h b { font-size: 12px; font-weight: 700; letter-spacing: 1.6px; color: var(--accent); }
.fv-zone-h span { font-size: 7px; letter-spacing: 1.5px; text-transform: uppercase;
  color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.fv-grp { flex: none; display: flex; flex-direction: column; gap: 5px; }
.fv-grp-h { display: flex; align-items: baseline; gap: 6px;
  font-size: 10px; letter-spacing: 1px; color: var(--txt2); }
.fv-grp-h em { font-style: normal; margin-left: auto; font-size: 9px;
  font-family: var(--num); color: var(--muted); }

/* 详情面板的字段表：字段名(小灰) / 值(青) 两列对齐 */

/* ============ 左下 toast ============ */
.fv-notes { position: absolute; left: 50%; transform: translateX(-50%);
  bottom: 42px; z-index: 21;
  display: flex; flex-direction: column; align-items: center; gap: 5px;
  max-width: 62%; pointer-events: none; }
.fv-note { display: flex; align-items: center; gap: 6px; font-size: 11px;
  padding: 4px 11px; border: 1px solid; background: var(--glass-solid);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  clip-path: polygon(0 0, calc(100% - 9px) 0, 100% 9px, 100% 100%, 0 100%); }
.fv-note.warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.fv-note.err { color: var(--err); border-color: var(--err-line); background: var(--err-soft); }
.fv-note code { font-family: var(--mono); font-size: 10px; color: var(--txt2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ============ 底部 KPI 条 ============ */
.fv-pop { position: absolute; left: 268px; top: 52px; z-index: 26; width: 292px; }
.fv-pop-x { position: absolute; right: 6px; top: 6px; z-index: 2; width: 20px; height: 20px;
  padding: 0; display: flex; align-items: center; justify-content: center;
  background: transparent; border: none; border-radius: 0; color: var(--muted); }
.fv-pop-x:hover { color: var(--err); }

.fv-kpi { position: absolute; left: 0; right: 0; bottom: 0; z-index: 22;
  display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 10px;
  background: var(--kpi); border-top: 1px solid var(--line);
  pointer-events: none; }
.fv-kpi > * { pointer-events: auto; }
.fv-kpi .k { display: inline-flex; align-items: center; gap: 6px; flex: none;
  padding: 6px 10px; border-radius: 8px; font-size: 12px;
  border: 1px solid var(--glass-line); background: var(--panel2); color: var(--muted); }

.fv-kpi .k i { font-style: normal; font-size: 11px; letter-spacing: .3px; color: var(--muted);
  white-space: nowrap; flex: none; }
.fv-kpi .k i::before { content: ""; display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; background: var(--muted); margin-right: 6px; vertical-align: -1px; }
/* 每档色调自己带一颗灯 —— 沿用原底栏"三盏状态灯"的做法，缺配置不猜颜色 */
.fv-kpi .k b.ok ~ i::before, .fv-kpi .k:has(b.ok) i::before { background: var(--ok); box-shadow: 0 0 7px var(--ok); }
.fv-kpi .k:has(b.err) i::before { background: var(--err); box-shadow: 0 0 7px var(--err); }
.fv-kpi .k:has(b.acc) i::before { background: var(--accent); box-shadow: 0 0 7px var(--accent); }
.fv-kpi .k b { font-family: var(--num); font-size: 13px; font-weight: 600; line-height: 1;
  color: var(--txt); font-variant-numeric: tabular-nums; white-space: nowrap; }
.fv-kpi .k b em { font-style: normal; font-size: 8px; font-weight: 600; margin-left: 2px;
  color: var(--muted); }
.fv-kpi .k b.ok { color: var(--ok); text-shadow: 0 0 10px var(--glow-soft); }
.fv-kpi .k b.err { color: var(--err); }
.fv-kpi .k b.acc { color: var(--accent); }
.fv-kpi .k b.blink { animation: fv-blink 1s steps(2, end) infinite; }
@keyframes fv-blink { 50% { opacity: .25; } }

/* 状态点 */
.fv-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--muted);
  display: inline-block; }
.fv-dot.ok, .fv-dot.safe { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
.fv-dot.warn { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
.fv-dot.danger, .fv-dot.err { background: var(--err); box-shadow: 0 0 6px var(--err); }
.fv-dot.hit, .fv-dot.alarm { background: var(--hit); box-shadow: 0 0 8px var(--hit); }

/* 窄屏：列收窄 + KPI 字号降一档，保证中间还留得出机房 */
@media (max-width: 1500px) {
  .fv-col { width: 224px; }
  .fv-pop { left: 240px; width: 262px; }
    .fv-kpi .k { padding: 5px 8px; }
  .fv-kpi .k b { font-size: 12px; }
}
</style>
