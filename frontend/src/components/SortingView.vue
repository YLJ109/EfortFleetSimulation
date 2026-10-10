<script setup>
// =====================================================================
// 分拣页（左：仿真机器人 · 右：点位设置与分拣操作）
//
//   左侧：MonitorLayout 的 3D 视口（与其它页共用同一个 scene/canvas），
//         示教时看着机器人动，右边点「存点 / 到点」。
//   右侧：① 就绪（复用现成 RcReadyCard：一键就绪）  ② 页签「执行 / 点位」
//         · 执行：参数 + 空跑/开始/暂停/继续/停止 + 进度 + 判色 + 三列占用
//         · 点位：精简点动 + A/B 各 9 格 × 2 槽（正上位 & 料位）+ C + 开始/结束点
//                 每个点位槽：存点（记当前位置）/ 到点（复核）/ 清空
//
//   38 个点位：A1–A9+A1上–A9上(18) + B1–B9+B1上–B9上(18) + C检测(1) + 开始/结束点(1)
//   方案：docs/方案-分拣页改版（左仿真·右点位设置与分拣操作）.md
//   后端：/api/sorting/{points,teach,goto,point,start,state,cancel}
//
//   ★ 示教只写配置（不下发运动）；「到点」是运动，走围栏互锁/轴锁/速度夹取，可被打断。
//   ★ 分拣运行期间：存点/到点/点动都会被后端拒绝（抢同一个 40135 触发位）。
// =====================================================================
import { ref, computed, onMounted, onBeforeUnmount, watch } from "vue";
import MonitorLayout from "./MonitorLayout.vue";
import CameraPanel from "./CameraPanel.vue";   // ★ 复用现成的摄像头窗口，绝不重做
import Icon from "./Icon.vue";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "../stores/auth.js";
import { useRobotStore } from "../stores/robot.js";
import { useExecStore } from "../stores/exec.js";
import { useRcReadyStore } from "../stores/rcReady.js";
import { useSorting3dStore } from "../stores/sorting3d.js";
import { useTicTacToeStore } from "../stores/tictactoe.js";
import { speak, stageSpeech } from "../ai/speak.js";
import SortFlow from "./SortFlow.vue";       // ★ 执行流程可视化（for 循环步骤条）
import TicTacToeBoard from "./TicTacToeBoard.vue";   // ★ 井字棋 2D 棋盘浮层（挂 #hud）
import TicTacToeCard from "./TicTacToeCard.vue";     // ★ 井字棋右侧卡片
import GamepadTestCard from "./GamepadTestCard.vue"; // ★ 手柄测试盒（自检：连上没 / 谁触发什么）
import { updateSortingState } from "../three/manager.js";

const auth = useAuthStore();
const robot = useRobotStore();
const exec = useExecStore();
const rc = useRcReadyStore();
// ★ 分拣 3D 的**装填者**是全局 store（App 级驱动，各页共用同一份显示）；
//   本页只做两件事：① 把轮询到的 state 喂给 3D；② 显示 store 的自检信息。
const s3d = useSorting3dStore();
// ★ 井字棋（机器人真实落子）：棋盘就是 B 区 3×3，与分拣同页、同一条搬运链路。
//   轮询随本页（sort）进出：进入启、离开停（照 s3d/ai 的门控写法）。
const ttt = useTicTacToeStore();

const tab = ref("run");           // run | point
const chk = ref(null);            // 38 点位体检
const chkErr = ref("");
const st = ref(null);             // 分拣进度
const running = ref(false);
const busy = ref(false);
// ★ 2026-10-09 用户需求：暂停/继续按钮 —— 原地冻结当前分拣（步边界生效），可继续；
//   「停止」语义不变（取消 + 回开始/结束点）。暂停态来自 /control/run-state 的 paused。
const paused = ref(false);
const pauseBusy = ref(false);
const msg = ref("");
const err = ref("");
const showMiss = ref(false);

// 运行参数（与后端 StartIn 对齐）
const dryRun = ref(true);         // ★ 默认空跑：先不动机器
const dwellMs = ref(500);
const frames = ref(3);
const blowMs = ref(500);
// ★ 吸取后停留（ms）：等真空吸牢再抬起。与后端 StartIn.suck_hold_ms 默认一致，
//   界面上写死"500"会与真实参数脱节，所以跟着一起送、一起显示。
//   ★ 2026-10-10 用户口径：**0.5 秒**。
const suckHoldMs = ref(500);
const maxPieces = ref(9);

// ★ 分拣模式（2026-10-10 用户需求）：正常 = 现状（单面，一趟一件）；快速 = J6 双面（一趟两件）。
//   · 它只映射到后端 StartIn.dual_face 一个字段（false/true），别的一概不动。
//   · **不做持久化**：这个开关会改变**真机动作**（多一次 J6 翻转、多取/多放一件）。
//     若存进 localStorage，上次误选「快速」、这次进页面没注意，就会带着双面流程真机开跑 ——
//     每次进页面回到「正常」最安全，代价只是多按一下。
const mode = ref("normal");       // normal | fast
const dualFace = computed(() => mode.value === "fast");

// 示教（点动操作已移交点位执行页；这里只保留存点/到点/清点的忙态与反馈）
const pointBusy = ref("");
const pointMsg = ref("");

let timer = null;
let authWatcher = null;    // controlActive 监听（用于令牌变化时启停轮询），卸载时解除

const A_SLOTS = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9"];
const B_SLOTS = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"];

/** 每个格子的两个点位槽：正上位 + 料位（A 叫原料位、B 叫放料位）。 */
const aCells = A_SLOTS.map((s) => ({ key: s, slots: [
  { name: s + "上", label: "正上位" }, { name: s, label: "原料位" }] }));
const bCells = B_SLOTS.map((s) => ({ key: s, slots: [
  { name: s + "上", label: "正上位" }, { name: s, label: "放料位" }] }));
const extraSlots = [
  { name: "C检测", label: "摄像头位" },
  { name: "开始/结束点", label: "开始/结束" },
  // ★ 2026-10-10 用户口径：新增两个**只有 J6 不同**的可录入姿态，供 J6 双面分拣
  //   在「正面 / 反面」之间切换（用同一位置覆盖左右两侧）。
  //   名字必须与后端 services/sorting.py 的 OPTIONAL_EXTRA_POINTS、
  //   api/sorting.py 的 DUAL_FACE_FLIP_POINT 完全一致（回归钉会比对源码）。
  { name: "J6正面", label: "J6 正面（下板 · 正常面）" },
  { name: "J6反面", label: "J6 反面（上板 · 翻转面）" },
];

/** 未示教名单（体检结果里就是"缺哪些"）→ 用来判断每个点位槽的状态。 */
const missing = computed(() => new Set((chk.value && chk.value.missing) || []));
// ★ 可选附加点（不在 38 个体检名单里、允许缺失）：判定"是否已示教"**不能**用 38 的 missing，
//   否则它们会恒为"已示教"（绿点常亮 /「到点」可点）—— 那是界面在编故事。
//   改为看点位库(exec.points)里是否真有这个名字（存过点才有）。
//   ★ 对 38 个必备点，下面的 isTaught 表达式与改动前**逐字一致**。
const OPTIONAL_POINTS = new Set(["J6正面", "J6反面"]);
const isTaught = (name) => (OPTIONAL_POINTS.has(name)
  ? !!ptOf(name)
  : (!!(chk.value && chk.value.ok) || !missing.value.has(name)));

// ★ 快速分拣（双面）必须的两个可录入点 —— 名字与后端一致的唯一口径：
//   services/sorting.py 的 OPTIONAL_EXTRA_POINTS / api/sorting.py 的 DUAL_FACE_FLIP_POINT。
//   为什么两个都要查、且**沿用上面的 isTaught**：后端跑到翻转会按名字找点，
//   少一个就是"跑到一半找不到点"；而这两个点不在 38 体检名单里，必须看点位库(exec.points)
//   里是否真存过（isTaught 对可选点就是这个语义），不能另写一套查询。
const DUAL_FACE_POINTS = ["J6正面", "J6反面"];
const dualFaceMissing = computed(() => DUAL_FACE_POINTS.filter((n) => !isTaught(n)));
// 选了快速、却缺点位 → 拦下启动（正常模式不拦，保持现状）。
const dualFaceBlocked = computed(() => dualFace.value && dualFaceMissing.value.length > 0);

// ---------- 点位坐标 与 残影 ----------
// ★ 为什么接残影：只看一行数字看不出"这个点到底摆在哪"。悬停/点选让 3D 里的残影
//   直接摆到该点位姿 —— 示教对不对、有没有撞工装，一眼就看得出来。
//   复用 exec store 里现成的 hoverPoint（临时预演）/ pinPoint（常驻选中），不另造轮子。
const pinnedName = ref("");    // 残影 A（安全判定以它为准）
const pinnedBName = ref("");   // ★ 残影 B（仅用于两个点位对比）

/** 按名字取点位（exec.points 是 /points 的全量列表，loadPoints 后可用）。 */
const ptOf = (name) => (exec.points || []).find((p) => p.name === name) || null;

/** 点位存的是什么：关节角或直角坐标（kind=cartesian 的点存 tcp）。 */
function coordText(p) {
  if (!p) return "未示教";
  if (p.kind === "cartesian" && p.tcp) {
    const t = p.tcp;
    const f = (v) => (v === null || v === undefined ? "-" : Number(v).toFixed(1));
    return "XYZ " + f(t.x) + " " + f(t.y) + " " + f(t.z) + " / RPY " + f(t.rx) + " " + f(t.ry) + " " + f(t.rz);
  }
  const j = p.joints;
  if (!j || !j.length) return "无坐标";
  return j.map((v) => Number(v).toFixed(1)).join("  ");
}

/**
 * 悬停 → 残影临时预演该点。
 *
 * ★ 2026-10-09：原先还有个 `coordShort()`（把 6 个关节角压成整数）——
 *   它是为**横向 3×3 网格**（每格仅 ~200px）做的妥协。
 *   改成竖排行列表后每行足够宽，**直接用完整坐标** `coordText()`（保留 1 位小数），
 *   现场要的就是这个精度，所以删掉了那个降精度版本。
 */
function hover(name) {
  const p = ptOf(name);
  if (p) exec.hoverPoint(p);
}
function unhover() {
  // 常驻选中的那个要保留，别被移开鼠标清掉
  if (pinnedName.value) { const p = ptOf(pinnedName.value); if (p) exec.pinPoint(p); }
  else exec.unhoverPoint();
}

/**
 * 点名字 → 残影**常驻**跟随该点（再点一次取消）。
 * ★ **Shift + 点** → 摆到**第二个残影**（淡橙），实现"两个点位同屏对比"。
 *   为什么要分两个：同一个位置只放一个残影时，无法回答"从正上方落到槽位会不会撞"，
 *   两个残影同屏才能一眼看出落差/干涉。B 只做视觉对比，**不参与围栏判定**。
 */
function pin(name, ev) {
  const p = ptOf(name);
  if (!p) return;
  const toB = !!(ev && ev.shiftKey);
  if (toB) {
    if (pinnedBName.value === name) { pinnedBName.value = ""; exec.unpinPointB(); }
    else { pinnedBName.value = name; exec.pinPointB(p); }
    return;
  }
  if (pinnedName.value === name) {
    pinnedName.value = "";
    exec.unpinPoint();
    exec.hideGhost();
  } else {
    pinnedName.value = name;
    exec.pinPoint(p);
  }
}
function clearPin(side) {
  if (side === "a") { pinnedName.value = ""; exec.unpinPoint(); exec.hideGhost(); }
  else { pinnedBName.value = ""; exec.unpinPointB(); }
}
const pinnedPoint = computed(() => (pinnedName.value ? ptOf(pinnedName.value) : null));
const pinnedB = computed(() => (pinnedBName.value ? ptOf(pinnedBName.value) : null));
/** 两点距离（mm）—— 同屏对比时最想知道的就是"差多少"。 */
const distAB = computed(() => {
  const a = pinnedPoint.value, b = pinnedB.value;
  if (!a || !b) return null;
  if (a.kind === "cartesian" || b.kind === "cartesian") return null;
  const ja = a.joints, jb = b.joints;
  if (!ja || !jb || ja.length !== 6 || jb.length !== 6) return null;
  let s2 = 0;
  for (let i = 0; i < 6; i++) { const d = Number(jb[i]) - Number(ja[i]); s2 += d * d; }
  return Math.sqrt(s2);
});

/** 示教/清空后要同时刷新体检与点位坐标（残影也跟着更新）。 */
async function refreshPoints() {
  await loadCheck();
  await exec.loadPoints();
  for (const [nm, side] of [[pinnedName, "a"], [pinnedBName, "b"]]) {
    if (!nm.value) continue;
    const p = ptOf(nm.value);
    if (!p) { clearPin(side); continue; }
    if (side === "a") exec.pinPoint(p); else exec.pinPointB(p);
  }
}

// ---------- 接口 ----------
async function loadCheck() {
  chkErr.value = "";
  try {
    if (!auth.controlActive) return;   // ★ 未授权不发：/sorting/points 挂 require_control
    const r = await apiControl("/sorting/points");
    if (r.status === 401 || r.status === 403) { chkErr.value = "需要控制令牌"; return; }
    const d = await r.json();
    if (r.ok) { chk.value = d; } else { chkErr.value = d.message || ("HTTP " + r.status); }
  } catch (e) { chkErr.value = "体检失败：" + ((e && e.message) || e); }
}

async function poll() {
  try {
    // ★ 未授权不发轮询：/sorting/state 挂着 require_control，无令牌必然 401。
    //   每次 tick 都判而不是只在启动时判 —— 运行中被收回令牌也立即停发。
    if (!auth.controlActive) return;
    const r = await apiControl("/sorting/state");
    if (!r.ok) return;
    const d = await r.json();
    running.value = !!d.running;
    st.value = d.state || null;
    // ★ 暂停态在 /control/run-state（RunContext 统一进度快照）——
    //   只在运行中多打一次轻量 GET；空闲直接清掉，不发多余请求。
    if (running.value) {
      try {
        const rr = await apiControl("/control/run-state");
        if (rr.ok) {
          const rd = await rr.json();
          paused.value = !!(rd.state && rd.state.paused);
        }
      } catch (e2) { /* 暂停态读不到不影响主轮询 */ }
    } else {
      paused.value = false;
    }
    logTick();
    // ★ 3D 方块跟随执行：把 state 直接喂给 3D（幂等，同状态不重复改场景）
    updateSortingState(st.value);
  } catch (e) { /* 轮询失败不影响流程 */ }
}

// ---------- 3D 可视化（A/B 托盘盒 + 18 槽位 + C 区摄像头） ----------
// ★ 装填者已收敛为全局 store（App.vue 拿到控制令牌即装填，**各页共用、切页不再隐藏**）：
//   真实监控 / 模拟仿真 / 点位执行 / 程序执行 / 分拣 / AI 执行都能看到。
//   本页只读它的自检结果；摆位来自 /api/sorting/blocks（后端 FK 由示教点位算出），
//   ★ 位置是"按示教点位示意"，**不是相机识别的实际位置**（界面已标注）。
const blocksInfo = computed(() => s3d.info);
const blocksErr = computed(() => s3d.err);

// ---------- 运行日志 ----------
// ★ 需求：分拣页也要有运行日志。日志写进全局 exec.log —— 由 MonitorLayout
//   左下角那块浮动面板统一显示（与程序执行/点位执行同一块，不另做一套）。
//   为什么按"签名变化"记而不是每 800ms 记一次：轮询很密，逐帧记会把面板刷爆，
//   只在**阶段/件数/判色/落料槽/停止原因**发生变化时写一行。
let _logKey = "";
let _logPrev = {};
let _logPrimed = false;   // ★ 首轮只对齐游标：不回放上一次分拣运行的旧 error/阶段
function logTick() {
  const s = st.value;
  if (!s) return;
  const key = [s.stage, s.piece, s.color, s.conf, s.target, s.error,
               s.finished, s.ok, s.sim_color].join("|");
  if (key === _logKey) return;
  const prev = _logPrev || {};
  _logKey = key;
  if (!_logPrimed) {
    // ★ 2026-10-09：与 ai.js 同口径 —— 后端 /sorting/state 是内存态（保留上次
    //   运行的 error），页面刷新后首轮若照常记日志，就会"开机凭空出现一句
    //   [分拣] 已停止：…"（用户没执行也被吓一跳）。首轮只记游标不播报。
    _logPrimed = true;
    _logPrev = { stage: s.stage, piece: s.piece, color: s.color,
                 conf: s.conf, target: s.target, error: s.error,
                 finished: s.finished, ok: s.ok, sim_color: s.sim_color };
    return;
  }

  if (s.stage && s.stage !== prev.stage) {
    exec.logLine("info", `[分拣] 阶段：${s.stage}` +
      (s.last_point ? `（${s.last_point}）` : ""));
  }
  if (s.color && (s.color !== prev.color || s.piece !== prev.piece)) {
    const note = s.sim_color
      ? "（空跑占位色·未判色）"
      : (s.conf ? `（conf ${Number(s.conf).toFixed(2)}）` : "");
    exec.logLine("ok", `[分拣] 判色：${s.color}${note}` +
      (s.target ? ` → 落料 ${s.target}` : ""));
  }
  // ★ 语音播报（用户口径 2026-10-09：动作/颜色/去哪儿/已到达都要播）
  //   触发点选在"阶段/颜色/错误"变化处（与日志同一套签名，天然只播一次）。
  if ((s.stage && s.stage !== prev.stage) || (s.color && s.color !== prev.color)
      || (s.error && s.error !== prev.error)) {
    speak(stageSpeech(s));
  }
  if (s.error && s.error !== prev.error) {
    exec.logLine("err", `[分拣] 已停止：${s.error}`);
  } else if (s.finished && !prev.finished) {
    exec.logLine(s.ok ? "ok" : "warn",
      s.ok ? `[分拣] 完成（共 ${s.piece || s.total || 0} 件）` : "[分拣] 本次结束");
  }
  _logPrev = { stage: s.stage, piece: s.piece, color: s.color,
               conf: s.conf, target: s.target, error: s.error,
               finished: s.finished, ok: s.ok, sim_color: s.sim_color };
}

/** 统一取后端错误文案（异常体是 {code,message,detail}，message 可能是对象）。 */
function errText(d, status) {
  const asStr = (v) => (typeof v === "string" ? v
    : (v && typeof v.message === "string" ? v.message : ""));
  const base = asStr(d && d.message) || asStr(d && d.detail) || ("HTTP " + status);
  let miss = [];
  for (const v of [d && d.detail, d && d.message]) {
    if (v && typeof v === "object" && Array.isArray(v.missing)) { miss = v.missing; break; }
  }
  return { base, miss };
}

async function start() {
  err.value = ""; msg.value = ""; busy.value = true;
  try {
    const r = await apiControl("/sorting/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dry_run: dryRun.value, speed_pct: exec.speed, dwell_ms: dwellMs.value,
        frames: frames.value, blow_ms: blowMs.value, max_pieces: maxPieces.value,
        suck_hold_ms: suckHoldMs.value,
        // ★ 模式 → 后端 StartIn.dual_face：正常=false（走原单件编排）；快速=true（J6 双面一趟两件）。
        //   必须是这个映射，不能写死；默认 normal 时送 false，与后端默认值一致（零回归）。
        dual_face: dualFace.value,
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const { base, miss } = errText(d, r.status);
      err.value = miss.length
        ? base + "（缺：" + miss.slice(0, 8).join("、")
          + (miss.length > 8 ? " 等 " + miss.length + " 个" : "") + "）"
        : base;
      showMiss.value = true;
      return;
    }
    st.value = d.state || null;
    running.value = true;
    msg.value = dryRun.value ? "空跑已启动（不下发运动）" : "分拣已启动";
    // ★★ "每次运行必须从头开始"：用后端回的**启动前快照**（不是活动状态）把界面与 3D 复位。
    //   后端已保证这份 state 是干净的（进度 0 / 三列占位全空 / 无上轮错误）。
    //   这里必须**主动喂一次** 3D —— 否则要等下一次轮询（最长 0.8s）才复位，
    //   期间画面还留着上一轮的方块（A 格空着、B 格有料），看起来就像"没从头开始"。
    updateSortingState(st.value);
    _logKey = ""; _logPrev = {};      // 日志游标也归零：新一轮的"阶段变化"重新开始记
    logTick();
    exec.logLine("info", `[分拣] 本轮从头开始：${dryRun.value ? "空跑（不下发运动）" : "真实下发"}`
      + `，速度 ${exec.speed}%，${maxPieces.value} 件，抓 ${frames.value} 帧`
      + (d.state && d.state.judge === false ? "；本次不判色（物体不会到 C 区）" : ""));
  } catch (e) {
    err.value = "启动异常：" + ((e && e.message) || e);
  } finally { busy.value = false; }
}

async function stop() {
  busy.value = true;
  try {
    await apiControl("/sorting/cancel", { method: "POST" });
    msg.value = "已请求停止（当前动作结束后停，并回开始/结束点）";
    exec.logLine("warn", "[分拣] 已请求停止（步边界生效，随后回开始/结束点）");
  } catch (e) { err.value = "停止异常：" + ((e && e.message) || e); }
  finally { busy.value = false; }
}

// ---------- 暂停 / 继续（2026-10-09 用户需求） ----------
// ★ 走 /control/run-pause | run-resume —— 与序列/文件执行**同一套** RunContext
//   步边界暂停：分拣线程在每次走点位（_move）边界与吸持/落料/吹气等待处都会被
//   冻结，绝不半截悬停。语义区分要跟现场说清楚：
//     「暂停」= 原地冻结，可继续；   「停止」= 取消 + 回开始/结束点；
//     想让机器人**立刻**停住 → 「停止」或急停，不是暂停。
async function togglePause() {
  pauseBusy.value = true;
  err.value = "";
  try {
    if (!paused.value) {
      const r = await apiControl("/control/run-pause", { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        paused.value = true;
        msg.value = "已请求暂停（当前动作结束后生效；等待步立即冻结）";
        exec.logLine("warn", "[分拣] 已请求暂停（步边界生效，原地冻结可继续）");
      } else {
        err.value = d.error || ("HTTP " + r.status);
      }
    } else {
      const r = await apiControl("/control/run-resume", { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        paused.value = false;
        msg.value = "";
        exec.logLine("ok", "[分拣] 已继续");
      } else {
        paused.value = false;   // 后端说没有暂停态 → 界面对齐
        err.value = d.error || ("HTTP " + r.status);
      }
    }
  } catch (e) { err.value = "暂停/继续异常：" + ((e && e.message) || e); }
  finally { pauseBusy.value = false; }
}

/** 存点：把机器人**当前位置**存为该点位（运行中禁用）。 */
async function teach(name) {
  if (!window.confirm("把机器人当前位置存为「" + name + "」？\n（已示教过将覆盖）")) return;
  pointBusy.value = name; pointMsg.value = "";
  try {
    const r = await apiControl("/sorting/teach", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { pointMsg.value = "存点失败：" + errText(d, r.status).base; return; }
    pointMsg.value = "已保存 " + name + "：" + (d.joints || []).map((v) => Number(v).toFixed(1)).join(", ");
    await refreshPoints();
    s3d.reload();        // ★ 刚改了示教点 → 3D 摆位必须重算（否则盒子/料还是旧位置）
  } catch (e) { pointMsg.value = "存点异常：" + ((e && e.message) || e); }
  finally { pointBusy.value = ""; }
}

/** 到点：移到该点位复核（★ 是运动）。 */
async function goto(name) {
  pointBusy.value = name; pointMsg.value = "正在移到 " + name + " …";
  try {
    const r = await apiControl("/sorting/goto", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const d = await r.json().catch(() => ({}));
    pointMsg.value = r.ok ? ("已到 " + name) : ("到点失败：" + errText(d, r.status).base);
  } catch (e) { pointMsg.value = "到点异常：" + ((e && e.message) || e); }
  finally { pointBusy.value = ""; }
}

async function clearPoint(name) {
  if (!window.confirm("清空点位「" + name + "」？该点位将从点位库删除。")) return;
  pointBusy.value = name; pointMsg.value = "";
  try {
    const r = await apiControl("/sorting/point?name=" + encodeURIComponent(name),
                               { method: "DELETE" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { pointMsg.value = "清空失败：" + errText(d, r.status).base; return; }
    pointMsg.value = "已清空 " + name;
    await refreshPoints();
    s3d.reload();        // ★ 点位被删 → 3D 里那一格（含托盘盒范围）要跟着重算
  } catch (e) { pointMsg.value = "清空异常：" + ((e && e.message) || e); }
  finally { pointBusy.value = ""; }
}

// ---------- 展示 ----------
const stage = computed(() => (st.value && st.value.stage) || "—");
const piece = computed(() => (st.value && st.value.piece) || 0);
const total = computed(() => (st.value && st.value.total) || maxPieces.value);
const progressPct = computed(() => Math.min(100, Math.round((piece.value / (total.value || 1)) * 100)));
const colorNow = computed(() => (st.value && st.value.color) || "");
const confNow = computed(() => (st.value && st.value.conf) || 0);
const targetNow = computed(() => (st.value && st.value.target) || "");
const placement = computed(() => (st.value && st.value.placement) || []);
const stopReason = computed(() => (st.value && st.value.error) || "");
// ★ 空跑占位色：颜色不是相机判出来的，界面必须说清楚（否则会被误当成真实判色结果）
const simColor = computed(() => !!(st.value && st.value.sim_color));
// ★ 3D 提示：疑似没取到（失败停止时方块画回 A 格并标红）+ 3D 画不了的槽位原因
const suspect = computed(() => !!(st.value && st.value.suspect_pick));
const handHold = computed(() => !!(st.value && st.value.holding));
const blocksWarn = computed(() => (blocksInfo.value && blocksInfo.value.warned) || []);
/** 托盘盒尺寸文本（A/B 各一个盒，[宽,深,壁高] mm）。 */
const trayText = computed(() => {
  const t = s3d.traySizes;
  if (!t.length) return "—";
  return t.map((d) => d.map((v) => Math.round(v)).join("×")).join(" / ") + " mm";
});
// ★ 本次是否真的会判色（后端 state.judge；空跑/双闸未开 = false）
const judgeOff = computed(() => !!(st.value && st.value.judge === false));
const missingCount = computed(() => (chk.value ? chk.value.missing.length : 38));
const canStart = computed(() => auth.controlActive && !busy.value && !running.value
  && !!(chk.value && chk.value.ok) && !dualFaceBlocked.value);
const canTeach = computed(() => auth.controlActive && !running.value);
const bCell = computed(() => {
  const map = {};
  for (const c of placement.value || []) for (const s of (c.used || [])) map[s] = c.color;
  return map;
});
// ★ 「黑」= 第 4 色（**无效颜色**，识别到了但原路放回原位）—— 也给个色标，
//   否则现场看到的是灰点，容易误以为"没识别到"。
const colorClass = (c) => (c === "红" ? "c-red" : c === "绿" ? "c-green"
  : c === "蓝" ? "c-blue" : c === "黑" ? "c-black" : "");

watch(() => robot.activeView, (v) => {
  if (v !== "sort") {
    if (timer) { clearInterval(timer); timer = null; }
    // ★ 不再隐藏 3D：托盘盒/方块/摄像头是**执行状态的显示**，不是本页的装饰，
    //   离开本页后由全局 store（stores/sorting3d.js）的 1s 轮询继续喂 state。
    ttt.stopPolling();             // 井字棋轮询也随本页离开停掉
  } else {
    if (!timer && auth.controlActive) { timer = setInterval(poll, 800); }
    if (auth.controlActive) exec.loadPoints();
    s3d.ensure();                  // 幂等：已装填就是零成本，未装填则补一次
    ttt.startPolling();            // 进入本页启动井字棋轮询（未授权内部自发早退）
  }
});
onMounted(() => {
  if (auth.controlActive) { loadCheck(); exec.loadPoints(); }
  s3d.ensure();                    // 兜底：直接刷新落在本页时也要装填（App 级同时也会做）
  // ★ 未授权不启动轮询定时器（拿令牌后由下方 controlActive watch 兜底拉起）
  if (auth.controlActive) { poll(); timer = setInterval(poll, 800); ttt.startPolling(); }
  // ★ 令牌到手/失效 → 自动启停轮询（避免未授权时 800ms 一次 401 刷屏）
  authWatcher = watch(() => auth.controlActive, (on) => {
    if (on) {
      if (!timer) { loadCheck(); exec.loadPoints(); poll(); timer = setInterval(poll, 800); }
      ttt.startPolling();
    } else {
      if (timer) { clearInterval(timer); timer = null; }
      ttt.stopPolling();
    }
  });
});
onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
  if (authWatcher) authWatcher();
  ttt.stopPolling();
  exec.hideGhosts();
  // ★ 3D 不再在此隐藏（各页常显）；几何/材质仍由 manager.disposeAll 统一释放。
});

// ★ 一键就绪成功 → 自动刷新点位列表 + 38 点位体检（用户需求："一键就绪后点位自动刷新"）。
//   RcReadyCard 内部已刷新一次 exec.loadPoints()；这里额外刷新本页的体检结论。
watch(() => rc.readyTick, (n) => {
  if (n > 0) { refreshPoints(); loadCheck(); }
});
</script>

<template>
  <MonitorLayout view="sort">
    <template #hud>
      <div class="exec-hud">分拣 · 三色方块 · {{ running ? "运行中" : "空闲" }}</div>
      <!-- ★ 摄像头画面：直接复用「真实监控/程序执行」同一个 CameraPanel 组件
           （同一个相机服务、同一套开关/设备选择/检测框），**没有另做一份**。
           views[0]='sort' 是本页（面板归属），带上 'live' 让真实监控页共用同一实例，
           避免"同一台相机被两个面板各开一路"。 -->
      <CameraPanel :views="['sort', 'live']" />
      <!-- ★ 井字棋 2D 棋盘浮层：只在有对局/复盘时显示（无对局时组件自身 v-if 隐藏，
           不干扰分拣操作）；键盘 ↑↓←→ + Enter / 鼠标点格落子（见 TicTacToeBoard.vue）。 -->
      <TicTacToeBoard />
    </template>

    <!-- 功能 Tab：执行 / 点位 —— 一张卡两种活儿，用带图标的 tab 栏切换，
         选中态青色发光 + 底部指示条（Sentinel 指挥中心风格），与顶栏语言一致。 -->
    <div class="srt-tabs" role="tablist">
      <button class="srt-tab" :class="{ on: tab === 'run' }" @click="tab = 'run'">
        <Icon name="play" :size="13" /><span>执行</span>
      </button>
      <button class="srt-tab" :class="{ on: tab === 'point' }" @click="tab = 'point'">
        <Icon name="target" :size="13" /><span>点位</span>
      </button>
    </div>

    <!-- ★ 井字棋卡片：常驻于本页右栏（棋盘 = B 区、搬运链路与分拣同源） -->
    <TicTacToeCard />
    <!-- ★ 手柄测试盒：连接状态 + 实时读数 + 模拟触发（点按钮 = 按手柄），常驻右栏 -->
    <GamepadTestCard />

    <!-- ================= 执行 ================= -->
    <template v-if="tab === 'run'">
      <div class="card">
        <!-- ★ 点位体检并入「运行参数」头部：体检是启动前置检查，与参数/启动同一张卡，
             同一屏看完"缺几还没示教"即可就地切到点位页补，不用再往上翻。 -->
        <div class="chk-block">
          <h3 class="chk-title"><Icon :name="chk && chk.ok ? 'check' : 'alert'" :size="14" /> 点位体检
            <span class="h3-sub" v-if="chk">{{ chk.found }}/{{ chk.total }}</span>
            <span class="h3-refresh" title="重新体检" @click="loadCheck"><Icon name="refresh" :size="13" /></span>
          </h3>
          <p v-if="chkErr" class="small err">{{ chkErr }}</p>
          <template v-else-if="chk">
            <p v-if="chk.ok" class="small ok">{{ chk.found }} 个点位齐备，可以开跑。</p>
            <p v-else class="small err">
              缺 {{ missingCount }} 个点位 —— 切到「点位」页就地示教，缺一个都不允许启动。
              <button class="lnk" @click="showMiss = !showMiss">{{ showMiss ? "收起" : "看缺哪些" }}</button>
            </p>
            <div v-if="!chk.ok && showMiss" class="chips">
              <span v-for="m in chk.missing" :key="m" class="chip warn">{{ m }}</span>
            </div>
          </template>
        </div>

        <hr class="sp-hr" />
        <h3><Icon name="sliders" :size="15" /> 运行参数</h3>
        <p class="small muted">速度用右上角那一份（全项目唯一速度口径）：<b>{{ exec.speed }}%</b></p>
        <!-- ★ 分拣模式：复用顶部 Tab 的 .srt-tabs/.srt-tab 分段样式（不新造视觉）。
             两选一：正常分拣（默认，走原单件流程） / 快速分拣（J6 双面一趟两件）。
             ★ 不做持久化：每次进页面都回到「正常」（理由见 script 里 mode 的注释）。
             运行中禁用切换（不允许半截改模式）。 -->
        <div class="pf-row">
          <label>分拣模式</label>
          <div class="srt-tabs mode-seg" role="tablist">
            <button class="srt-tab" :class="{ on: mode === 'normal' }" :disabled="running"
                    @click="mode = 'normal'">正常分拣</button>
            <button class="srt-tab" :class="{ on: mode === 'fast' }" :disabled="running"
                    @click="mode = 'fast'">快速分拣</button>
          </div>
        </div>
        <p v-if="dualFace" class="small muted">
          双面一趟两件：在 <b>J6正面</b>/<b>J6反面</b> 之间翻转，用同一位置覆盖左右两侧。
          <b>首次使用请先空跑。</b>
        </p>
        <!-- ★ 护栏：选了快速却缺 J6正面/J6反面 → 禁用启动并写明原因（绝不带着缺失硬跑）。 -->
        <p v-if="dualFaceBlocked" class="small err">
          <Icon name="alert" :size="12" /> 快速分拣需要先录入 <b>J6正面</b> / <b>J6反面</b> 两个点
          （当前缺：{{ dualFaceMissing.join("、") }}）。切到「点位」页示教后再开跑。
        </p>
        <div class="pf-row">
          <label>C 区停顿</label>
          <input type="number" min="0" max="10000" step="100" v-model.number="dwellMs" :disabled="running" />
          <span class="unit">ms</span>
          <label class="ml">抓帧</label>
          <input type="number" min="1" max="9" v-model.number="frames" :disabled="running" />
          <span class="unit">帧</span>
        </div>
        <div class="pf-row">
          <label>放气时长</label>
          <input type="number" min="0" max="3000" step="50" v-model.number="blowMs" :disabled="running"
                 title="到 B 区正上方后的落料时序：停止吸 → 放气（持续这段时间）→ 停止放气。
送检全程保持吸气；放气吹完**就地**停止放，不再保持到回起点。" />
          <span class="unit">ms</span>
          <label class="ml">本轮件数</label>
          <input type="number" min="1" max="9" v-model.number="maxPieces" :disabled="running" />
          <span class="unit">件</span>
        </div>
        <div class="pf-row">
          <label>吸取后停留</label>
          <input type="number" min="0" max="5000" step="50" v-model.number="suckHoldMs" :disabled="running"
                 title="吸上后先停一下再抬起，等真空吸牢（现场无真空反馈，立刻抬容易半空掉件）。" />
          <span class="unit">ms</span>
        </div>
        <div class="btns">
          <button class="primary" :disabled="!canStart" @click="start">
            <Icon name="play" :size="13" /> {{ running ? "运行中…" : "开始" }}
          </button>
          <!-- ★ 暂停/继续（2026-10-09 用户需求）：原地冻结当前分拣（步边界生效），可继续。
               与「停止」语义区分：暂停=冻结可续；停止=取消+回开始/结束点；
               想让机器人**立刻**停住 → 停止/急停，不是暂停。 -->
          <button v-if="running || paused" class="pause-btn" :disabled="pauseBusy" @click="togglePause"
                  :title="paused ? '从暂停处继续分拣' : '在当前动作结束后暂停（等待步立即冻结）；想让机器人立刻停住请用停止/急停'">
            <Icon :name="paused ? 'play' : 'pause'" :size="13" /> {{ paused ? "继续" : "暂停" }}
          </button>
          <button class="rec-on" :disabled="!running && !busy" @click="stop">
            <Icon name="stop" :size="13" /> 停止
          </button>
          <label class="dry-toggle">
            <input type="checkbox" v-model="dryRun" :disabled="running" />
            空跑（只校验点位与选槽，<b>不下发运动、不判色</b>）
          </label>
        </div>
        <p v-if="err" class="small err"><Icon name="alert" :size="12" /> {{ err }}</p>
        <p v-else-if="msg" class="small ok">{{ msg }}</p>
        <p v-if="running" class="small muted">
          「停止」在当前动作结束后生效（运动/吸放中途不停，那是急停的语义），随后回开始/结束点。
        </p>
      </div>

      <!-- ★ 执行流程可视化：for 循环形式的步骤条，高亮当前步（用户口径 2026-10-09） -->
      <SortFlow :st="st" :frames="frames" :dwell-ms="dwellMs"
                :blow-ms="blowMs" :suck-hold-ms="suckHoldMs" />

      <div class="card">
        <h3><Icon name="layers" :size="15" /> 3D 可视化
          <span class="h3-sub">{{ blocksInfo ? "已加载 · 各页常显" : (blocksErr || "加载中…") }}</span>
          <button class="mini-btn" style="margin-left:auto" :disabled="s3d.busy"
                  @click="s3d.reload()">重新装填</button>
        </h3>
        <p class="small muted">3D 摆位按<b>示教点位</b>示意（<b>非视觉定位</b>），各页常显。</p>
        <div class="rows2">
          <div class="row"><span class="k">料尺寸</span>
            <span class="v">{{ blocksInfo ? blocksInfo.sizeM.map((v) => Math.round(v * 1000)).join("×") + " mm" : "—" }}
            </span>
          </div>
          <div class="row"><span class="k">A/B 已画</span>
            <span class="v">{{ blocksInfo ? blocksInfo.a.length + " / " + blocksInfo.b.length : "—" }}</span>
          </div>
          <div class="row"><span class="k">托盘盒</span>
            <span class="v">{{ trayText }}</span>
          </div>
        </div>
        <!-- ★ 空跑角标：机器人不动、方块却会变 —— 不标注最容易被误当成真机在动 -->
        <div v-if="dryRun || (st && st.dry_run)" class="warn-badge">
          <Icon name="alert" :size="12" /> 空跑：方块仅示意，机器人不动
        </div>
        <p v-if="handHold" class="small muted" style="margin-top:5px">
          <Icon name="pin" :size="12" /> 当前手上有件（按流程推断：取料吸合已完成）——
          现场无真空反馈，不代表真的吸住了。
        </p>
        <p v-if="suspect" class="small err" style="margin-top:5px">
          <Icon name="alert" :size="12" /> 疑似没取到：该方块已画回 A 格并标红（不显示夹持）。
        </p>
        <p v-if="blocksWarn.length" class="small muted" style="margin-top:5px">
          未画：{{ blocksWarn.slice(0, 4).join("；") }}<span v-if="blocksWarn.length > 4"> 等
          {{ blocksWarn.length }} 项</span>
        </p>
      </div>

      <div class="card">
        <h3><Icon name="activity" :size="15" /> 运行状态
          <span class="h3-sub">{{ running ? "运行中" : "空闲" }}</span>
        </h3>
        <div class="row"><span class="k">阶段</span><span class="v">{{ stage }}</span></div>
        <div class="row"><span class="k">进度</span>
          <span class="v">{{ piece }}/{{ total }} 件{{ dryRun ? "（空跑）" : "" }}</span>
        </div>
        <div class="prog-bar"><i :style="{ width: progressPct + '%' }"></i></div>
        <div class="row mt"><span class="k">本次判色</span>
          <span class="v">
            <span v-if="colorNow" class="dotc" :class="colorClass(colorNow)"></span>
            {{ colorNow || "—" }}
            <span v-if="confNow" class="muted">conf {{ Number(confNow).toFixed(2) }}</span>
            <span v-if="simColor" class="muted">· 空跑占位色（未判色）</span>
          </span>
        </div>
        <div class="row"><span class="k">落料槽位</span><span class="v">{{ targetNow || "—" }}</span></div>
        <div v-if="judgeOff" class="small muted" style="margin-top:6px">
          <Icon name="info" :size="12" /> 本次<b>不判色</b>：空跑或双闸未开时方块不会到 C 区，
          故跳过相机判定、也不会因"检测不到"停止（仅验证点位与选槽逻辑）。
        </div>
        <div class="cols">
          <span v-for="c in placement" :key="c.color" class="colstat">
            <i class="dotc" :class="colorClass(c.color)"></i>{{ c.color }}
            {{ c.filled }}/{{ c.capacity }}<b v-if="c.full"> 满</b>
          </span>
        </div>
        <div v-if="stopReason" class="res err"><Icon name="alert" :size="13" /> 已停止：{{ stopReason }}</div>
      </div>
    </template>

    <!-- ================= 点位 ================= -->
    <template v-else>
      <div class="card sel-card">
        <h3><Icon name="layers" :size="15" /> 残影对比
          <span class="h3-sub">点名字=A(蓝) · Shift+点=B(橙)</span>
        </h3>
        <div class="grow a" v-if="pinnedPoint">
          <i class="gdot"></i>
          <span class="gname">A · {{ pinnedName }}</span>
          <span class="gj">{{ pinnedPoint.joints.map((v) => Number(v).toFixed(1)).join("  ") }}</span>
          <button class="mini-btn" @click="clearPin('a')">✕</button>
        </div>
        <p v-else class="small muted">A 未选 —— 点任意点位名即可摆上（蓝）</p>
        <div class="grow b" v-if="pinnedB">
          <i class="gdot"></i>
          <span class="gname">B · {{ pinnedBName }}</span>
          <span class="gj">{{ pinnedB.joints.map((v) => Number(v).toFixed(1)).join("  ") }}</span>
          <button class="mini-btn" @click="clearPin('b')">✕</button>
        </div>
        <p v-else class="small muted">B 未选 —— Shift + 点点位名摆上（橙，用于对比）</p>
        <p v-if="distAB !== null" class="small">
          两残影关节角距离 <b>{{ distAB.toFixed(2) }}</b>（6 轴欧氏距离，越小越接近）
        </p>
        <p class="small muted">A 参与围栏/地面判定；B 只做位置对比，不影响安全结论。</p>
      </div>

      <!-- ★ 点位示教的点动/存点能力已由「点位执行」页的关节点动卡承接（同一 exec store，
           同一份点动逻辑），本页不再重复放示教工具盒子 —— 分拣只负责"看图、体检、开跑"。 -->

      <div class="card">
        <h3><Icon name="layers" :size="15" /> A 区 · 原料
          <span class="h3-sub">每格 2 点：正上位 + 原料位</span>
        </h3>
        <div class="ptable">
          <div v-for="cell in aCells" :key="'a' + cell.key" class="pcell">
            <div class="pkey"><span>{{ cell.key }}</span></div>
            <div class="pitems">
              <div v-for="sl in cell.slots" :key="sl.name" class="pitem"
                   @mouseenter="hover(sl.name)" @mouseleave="unhover()">
                <div class="pi-top">
                  <span class="pdot" :class="isTaught(sl.name) ? 'on' : ''"></span>
                  <span class="pname" :class="{ pinned: pinnedName === sl.name, pinnedb: pinnedBName === sl.name }"
                        :title="sl.name + ' · ' + coordText(ptOf(sl.name))"
                        @click="pin(sl.name, $event)">{{ sl.label }}</span>
                  <span class="pacts">
                    <button class="mini-btn" :disabled="!canTeach || pointBusy === sl.name"
                            title="把机器人当前位置存为该点位" @click="teach(sl.name)">
                      <Icon name="check" :size="11" />
                    </button>
                    <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                            title="移到该点位复核" @click="goto(sl.name)">
                      <Icon name="target" :size="11" />
                    </button>
                    <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                            title="删除该点位" @click="clearPoint(sl.name)">
                      <Icon name="trash" :size="11" />
                    </button>
                  </span>
                </div>
                <div class="pi-coord" :title="sl.name + ' · ' + coordText(ptOf(sl.name))">
                  {{ coordText(ptOf(sl.name)) }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3><Icon name="layers" :size="15" /> B 区 · 放料
          <span class="h3-sub">列 1 红 · 列 2 绿 · 列 3 蓝</span>
        </h3>
        <div class="ptable">
          <div v-for="cell in bCells" :key="'b' + cell.key" class="pcell"
               :class="bCell[cell.key] ? colorClass(bCell[cell.key]) : ''">
            <div class="pkey">
              <span>{{ cell.key }}</span>
              <i v-if="bCell[cell.key]" class="dotc" :class="colorClass(bCell[cell.key])"></i>
            </div>
            <div class="pitems">
              <div v-for="sl in cell.slots" :key="sl.name" class="pitem"
                   @mouseenter="hover(sl.name)" @mouseleave="unhover()">
                <div class="pi-top">
                  <span class="pdot" :class="isTaught(sl.name) ? 'on' : ''"></span>
                  <span class="pname" :class="{ pinned: pinnedName === sl.name, pinnedb: pinnedBName === sl.name }"
                        :title="sl.name + ' · ' + coordText(ptOf(sl.name))"
                        @click="pin(sl.name, $event)">{{ sl.label }}</span>
                  <span class="pacts">
                    <button class="mini-btn" :disabled="!canTeach || pointBusy === sl.name"
                            title="把机器人当前位置存为该点位" @click="teach(sl.name)">
                      <Icon name="check" :size="11" />
                    </button>
                    <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                            title="移到该点位复核" @click="goto(sl.name)">
                      <Icon name="target" :size="11" />
                    </button>
                    <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                            title="删除该点位" @click="clearPoint(sl.name)">
                      <Icon name="trash" :size="11" />
                    </button>
                  </span>
                </div>
                <div class="pi-coord" :title="sl.name + ' · ' + coordText(ptOf(sl.name))">
                  {{ coordText(ptOf(sl.name)) }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3><Icon name="camera" :size="15" /> C 区 与 开始/结束点 · J6 双面</h3>
        <div class="slot wide" v-for="sl in extraSlots" :key="sl.name"
             @mouseenter="hover(sl.name)" @mouseleave="unhover()">
          <span class="sdot" :class="isTaught(sl.name) ? 'on' : ''"></span>
          <span class="sname" :class="{ pinned: pinnedName === sl.name, pinnedb: pinnedBName === sl.name }"
                :title="sl.name + ' · ' + coordText(ptOf(sl.name))"
                @click="pin(sl.name, $event)">{{ sl.label }}<span class="muted"> · {{ sl.name }}</span></span>
          <button class="mini-btn" :disabled="!canTeach || pointBusy === sl.name"
                  title="把机器人当前位置存为该点位" @click="teach(sl.name)">
            <Icon name="check" :size="12" /> 存点
          </button>
          <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                  title="移到该点位复核" @click="goto(sl.name)">
            <Icon name="target" :size="12" /> 到点
          </button>
          <button class="mini-btn" :disabled="!isTaught(sl.name) || !canTeach || pointBusy === sl.name"
                  title="删除该点位" @click="clearPoint(sl.name)">
            <Icon name="trash" :size="12" />
          </button>
        </div>
        <div class="coord wide" v-for="sl in extraSlots" :key="'c-' + sl.name"
             :title="sl.name + ' · ' + coordText(ptOf(sl.name))">
          <b>{{ sl.name }}</b>{{ coordText(ptOf(sl.name)) }}
        </div>
        <p class="small muted">「开始/结束点」是一个点：一轮开始先去、结束与异常都回这里。</p>
        <p class="small muted">J6 正面/反面是<b>同一姿态、只有 J6 不同</b>；双面分拣靠在这两个点之间切换来实现「用同一位置覆盖左右两侧」。</p>
      </div>
    </template>
  </MonitorLayout>
</template>

<style scoped>
/* ---- 功能 Tab（执行 / 点位）：Sentinel 指挥中心风格 ----
   满宽分栏 + 选中态青色发光下划线，与顶栏平行四边形按钮用同一套青色语言。
   用透视的连接线做 aria-tablist 内的分隔，强化"主导航内再分层"的层级感。 */
.srt-tabs { display: flex; margin: 0; border-radius: 9px;
  background: var(--panel2); border: 1px solid var(--line); padding: 3px; gap: 3px; }
.srt-tab { flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
  padding: 9px 0; border-radius: 7px; font-size: 13px; color: var(--muted);
  background: transparent; transition: all .16s ease; position: relative; }
.srt-tab:hover { color: var(--txt); background: rgba(47, 214, 240, .06); }
.srt-tab.on { color: var(--accent); background: linear-gradient(180deg, rgba(47,214,240,.10), rgba(47,214,240,.03));
  box-shadow: inset 0 0 0 1px var(--accent-line, rgba(47,214,240,.28)); }
.srt-tab.on::after { content: ""; position: absolute; left: 18%; right: 18%; bottom: -1px;
  height: 2px; border-radius: 2px; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.srt-tab.on svg { filter: drop-shadow(0 0 3px var(--accent)); }
/* 分拣模式分段控件：复用上面 .srt-tabs/.srt-tab 的选中态视觉，只把它压成行内尺寸
   （默认那两个 tab 是 flex:1 满宽，放进参数行里会撑开）。 */
.mode-seg { flex: none; }
.mode-seg .srt-tab { flex: none; padding: 5px 14px; font-size: 12.5px; }
.mode-seg .srt-tab:disabled { opacity: .5; cursor: not-allowed; }
.card { border: 1px solid var(--line); border-radius: 10px; padding: 10px;
  background: var(--panel); }
.card h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 6px;
  font-size: 14px; color: var(--txt); }
.h3-sub { font-size: 11px; color: var(--muted); font-weight: 400; }
.h3-refresh { margin-left: auto; cursor: pointer; color: var(--muted); }
.h3-refresh:hover { color: var(--accent); }
.row { display: flex; gap: 10px; font-size: 13px; padding: 1px 0; }
/* 3D 卡片：两列小行，省高度 */
.rows2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 10px; margin-top: 6px; }
.rows2 .row .k { width: 62px; }
/* 空跑角标：机器人不动、方块却会变 —— 必须显眼，避免误当成真机在动 */
.warn-badge { display: flex; align-items: center; gap: 5px; margin-top: 7px;
  font-size: 11.5px; font-weight: 600; color: #8a6100;
  background: rgba(212, 136, 6, .14); border: 1px solid rgba(212, 136, 6, .45);
  border-radius: 6px; padding: 4px 7px; }
.row.mt { margin-top: 6px; }
.row .k { width: 72px; color: var(--muted); flex: none; }
.row .v { color: var(--txt); }
.small { font-size: 12px; line-height: 1.6; margin: 4px 0 0; }
.muted { color: var(--muted); }
.ok { color: var(--ok, #2f9e6f); }
.err { color: var(--err, #e05252); }
.lnk { background: none; border: none; color: var(--accent); cursor: pointer;
  font-size: 12px; padding: 0 2px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.chip { font-size: 11px; padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--line); color: var(--muted); }
.chip.warn { color: #ffb4b4; border-color: #e0525255; }
.pf-row { display: flex; align-items: center; gap: 6px; margin: 6px 0; font-size: 13px; }
.pf-row label { color: var(--muted); }
.pf-row label.ml { margin-left: 12px; }
.pf-row input[type="number"] { width: 76px; padding: 3px 6px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel); color: var(--txt); }
.unit { color: var(--muted); font-size: 12px; }
.btns { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
/* ★ 暂停/继续：琥珀色调（区别于开始=青、停止=红）—— 现场一眼分清三态 */
.pause-btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(255,176,32,.45); color: #ffc861;
  background: rgba(255,176,32,.10); font-family: var(--font); font-size: 13px; font-weight: 600;
}
.pause-btn:hover:not(:disabled) { background: rgba(255,176,32,.18); }
.pause-btn:disabled { opacity: .5; cursor: not-allowed; }
.dry-toggle { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  color: var(--muted); cursor: pointer; }
.prog-bar { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; margin-top: 4px; }
.prog-bar > i { display: block; height: 100%; background: var(--accent); }
.cols { display: flex; gap: 10px; margin-top: 6px; font-size: 12px; color: var(--muted); }
.colstat b { color: var(--warn, #d98a2b); margin-left: 3px; }
.res { display: flex; align-items: center; gap: 6px; font-size: 12px; margin-top: 8px;
  padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line); }
.res.err { color: #ffb4b4; border-color: #e0525255; }
.jog-axes { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 4px; }
.jog-axes button { padding: 4px 0; font-size: 12px; }
.jog-axes button.on { border-color: var(--accent); color: var(--accent); }
/* （原 .teach-grid/.tcell/.tcell-h 三件套已随"横向网格 → 竖排行列表"改版移除：
   它们正是"卡片套卡片 + 等宽网格"的来源。B 区列色改由 .prow.c-* 左色条表达。） */
/* =====================================================================
   点位列表（2026-10-09 改版：横向 3×3 网格 → **竖排行列表**）
   为什么改：
     ① 用户明确要求"不要横着，要竖着"；
     ② 网格里每格只有 ~200px，6 个关节角**必然被截断**（"57,-69,-8,5…"）——
        而现场最需要看的恰恰是完整坐标；
     ③ 9 张内容完全相同的卡片并排 = 单调重复，且 card→tcell→slot 三层套娃。
   新版：**一大张卡 + 行分隔线**，格号做左侧锚点，坐标完整可读，
        动作按钮退到最弱一级（hover 才显眼）。
   ===================================================================== */
.ptable { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
  margin-top: 2px; }
/* 每个格子：格号头 + 2 条槽位（正上位/料位） */
.pcell { display: flex; flex-direction: column; gap: 5px; min-width: 0;
  padding: 7px 8px; border: 1px solid var(--line); border-radius: 8px;
  background: rgba(255, 255, 255, .015); }
.pcell:hover { border-color: var(--accent-d, rgba(47,214,240,.5)); }
/* 格号：等宽 + 亮色 —— 扫视时的锚点 */
.pkey { display: flex; align-items: center; gap: 4px; padding-bottom: 1px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12.5px; font-weight: 600; color: var(--txt); }
.pitems { display: flex; flex-direction: column; gap: 3px; }
.pitem { display: flex; flex-direction: column; gap: 1px; padding: 3px 4px;
  border-radius: 5px; }
.pitem:hover { background: rgba(79, 168, 255, .08); }
.pi-top { display: flex; align-items: center; gap: 6px; min-width: 0; }
.pdot { flex: none; width: 6px; height: 6px; border-radius: 50%;
  border: 1px solid var(--line); }
.pdot.on { background: var(--ok, #2f9e6f); border-color: var(--ok, #2f9e6f); }
.pname { flex: 1; min-width: 0; font-size: 12px; color: var(--txt);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
.pname.pinned { color: var(--accent); }
.pname.pinnedb { color: var(--warn, #d48806); }
/* 坐标：等宽暗色小字，放名称下方一行，**悬停看全** */
.pi-coord { font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 10px; color: var(--muted); letter-spacing: -.1px;
  padding-left: 10px; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
/* 动作按钮：图标化，hover 才提亮（9 格 × 2 槽会很多，默认弱化） */
.pacts { flex: none; display: flex; gap: 2px; opacity: .5;
  transition: opacity .15s ease-out; }
.pitem:hover .pacts { opacity: 1; }
.pitem .pacts .mini-btn { padding: 2px 4px; }
/* B 区：列色做成格子上边框色条（红/绿/蓝） */
.pcell.c-red { border-top: 2px solid #e24b4a; }
.pcell.c-green { border-top: 2px solid #639922; }
.pcell.c-blue { border-top: 2px solid #378add; }
.pcell.c-black { border-top: 2px solid #1c1f24; }

.slot { display: flex; align-items: center; gap: 5px; padding: 3px 0; }
/* ★ 2026-10-09 布局优化：坐标并入操作行（原来是**独立的第二行**，9 格共多占 18 行，
   而且 6 个关节角全写必然被截断成"57..."，看不清也没意义）。
   现在：只占行内剩余宽度、小字等宽、超出省略；**完整坐标仍在鼠标悬停的 title 里**。
   效果：每格从 5 行压到 3 行，且坐标"看得见"（虽然省略）+ 悬停可看全。 */
.slot .scoord { flex: 1 1 auto; min-width: 0; font-size: 10.5px; color: var(--muted);
  font-family: var(--font-mono, ui-monospace, monospace); letter-spacing: -.2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: right; }
.slot.wide { padding: 7px 0; }
.sdot { width: 7px; height: 7px; border-radius: 50%; background: var(--line); flex: none; }
.sdot.on { background: var(--ok, #2f9e6f); }
.sname { flex: 1; min-width: 0; font-size: 13px; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sname.pinnedb { color: #ff9a3d; font-weight: 500; }
.mini-btn { padding: 3px 8px; font-size: 12px; border-radius: 6px; }
.sdot { width: 9px; height: 9px; }
.sname { cursor: pointer; }
.sname:hover { color: var(--accent); }
.sname.pinned { color: var(--accent); font-weight: 500; }
.coord { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0 3px 10px; }
.coord b { color: var(--txt); font-weight: 400; margin-right: 4px; }
.coord.wide { padding-left: 0; }
.sel-card { border-color: var(--accent); }
.grow { display: flex; align-items: center; gap: 6px; padding: 5px 7px; margin: 3px 0;
  border: 1px solid var(--line); border-radius: 7px; font-size: 12px; }
.grow.a { border-color: #2f9bff66; }
.grow.b { border-color: #ff9a3d66; }
.grow .gdot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.grow.a .gdot { background: #2f9bff; }
.grow.b .gdot { background: #ff9a3d; }
.grow .gname { flex: none; color: var(--txt); }
.grow .gj { flex: 1; min-width: 0; color: var(--muted); font-variant-numeric: tabular-nums;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grow .mini-btn { flex: none; }
.grow .mini-btn { flex: none; }
.sel-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; }
.sel-j { font-size: 11px; color: var(--muted); display: flex; justify-content: space-between;
  gap: 4px; border: 1px solid var(--line); border-radius: 5px; padding: 2px 5px; }
.sel-j b { color: var(--txt); font-variant-numeric: tabular-nums; font-weight: 500; }
/* （B 区列色已改用 .prow.c-* 的左色条，见上方点位列表样式） */
.dotc { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-left: 4px; background: var(--muted); }
.dotc.c-red { background: #e24b4a; }
.dotc.c-green { background: #639922; }
.dotc.c-blue { background: #378add; }
.dotc.c-black { background: #1c1f24; box-shadow: inset 0 0 0 1px #5a616b; }
</style>
