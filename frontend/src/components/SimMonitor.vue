<script setup>
// 模拟仿真视图：键盘控制 + 关节/直角指令预演（只算不发）。
// ★ 外观固定使用官方数模（ER8-700H GLB 可动版），本页不再提供模型切换。
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useRobotStore } from "../stores/robot.js";
import { getTcp } from "../three/manager.js";
import { apiUrl } from "../config.js";
import { dancePose } from "../utils/dance.js";
import MonitorLayout from "./MonitorLayout.vue";
import Icon from "./Icon.vue";

const robot = useRobotStore();

// =====================================================================
// 只读接口的响应处理（/control/preview 与 /control/ik）
//
// ★ 这两个接口是**公开只读**的（后端 control.py 的 router_ro，不挂 require_control）：
//   模拟仿真页在 tabs.js 里是公开页，让"只算不发"的预演去要控制令牌，只会让未登录
//   访客的控制台刷满 401，按钮本身也永远点不动。
// ★ 但仍必须处理失败：FastAPI 的校验错误（422）把 detail 放成数组、
//   HTTPException 把 detail 放成字符串或对象。不解析就只能显示"undefined"，
//   现场拿着这句话没法排查（这正是之前踩过的坑）。
// =====================================================================
async function readJson(r) {
  try { return await r.json(); } catch (e) { return {}; }
}

function errText(d, status) {
  const detail = d && d.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    // pydantic 校验错误：[{loc:[...], msg:"..."}] —— 只取第一条，够定位了
    const m = detail[0] && detail[0].msg;
    if (m) return String(m);
  }
  if (detail && typeof detail === "object") {
    const m = detail.message || detail.error;
    if (m) return String(m);
  }
  return (d && (d.message || d.error)) || ("后端返回 " + status);
}

const simTcp = ref({ x: 0, y: 0, z: 0 });

// 操控模式：joint = 关节模式；cart = 机器人模式（末端 XYZ 移动，经后端 IK 反解）
const controlMode = ref("joint");
const cartAxis = ref(0);          // 0=X 1=Y 2=Z 3=A 4=B 5=C（埃夫特机器人坐标系六方向）
const ikMsg = ref("");
const cartBusy = ref(false);      // 一次 IK 请求进行中（防止连点叠加）
const canCartMove = computed(() => !cartBusy.value);

// =====================================================================
// 机器人模式（= 埃夫特手册的"机器人坐标系"= 笛卡尔）：**六个**滑动条
//
// 官方口径（EFORT 机器人学院 · 机器人坐标系下操作机器人）：
//   机器人坐标系是空间直角坐标系，除 XYZ 外还有 A/B/C 三个旋转方向描述绕 TCP 的转动：
//     X/Y/Z = 末端位置(mm)     A = 绕 Z 旋转     B = 绕 Y 旋转     C = 绕 X 旋转
//   示教器按键 1±=X 2±=Y 3±=Z 4±=A 5±=B 6±=C —— 所以这里是**六个滑块**（不是关节角）。
//   拖动任一滑块 = 末端沿该方向直线/定轴旋转 → 六个关节由 IK 一起联动（**整臂整体运动**）。
//
// ★ 位姿来源 `POST /control/fk`（后端 kinematics.pose_of）：与 /ik 同一套约定，
//   避免前端自算欧拉角导致"拖的方向不对"。
// ★ 姿态字段对应：A↔rz、B↔ry、C↔rx（ZYX 欧拉角，与后端 rpy_to_matrix 一致）。
// =====================================================================
// ★ 范围：ER8-700H 工作半径 706mm，限位圈定 ±500（保守）避免到极限位
// ★ 步长：X/Y/Z 用 5mm（每格幅度可控），A/B/C 用 1°
// ★ 拖动停止（@change）才执行 IK，拖动期间只更新显示不调后端
const CART6 = [
  { i: 0, k: "X", k6: "x", unit: "mm", min: -500, max: 500, step: 5 },
  { i: 1, k: "Y", k6: "y", unit: "mm", min: -500, max: 500, step: 5 },
  { i: 2, k: "Z", k6: "z", unit: "mm", min: 0, max: 1000, step: 5 },
  { i: 3, k: "A", k6: "rz", unit: "°", min: -90, max: 90, step: 1 },
  { i: 4, k: "B", k6: "ry", unit: "°", min: -90, max: 90, step: 1 },
  { i: 5, k: "C", k6: "rx", unit: "°", min: -90, max: 90, step: 1 },
];
/** 当前末端位姿（DH 基座系：x/y/z mm + rx/ry/rz 度）—— 机器人模式滑块的"真值"。 */
const pose = reactive({ x: 0, y: 0, z: 0, rx: 0, ry: 0, rz: 0 });
// ★ 细节修复（用户指出"动一个，其余 5 个也跟着变"）：
//   ① poseReady：FK 没读到之前**绝不允许**下发六维目标 —— 否则 pose 全 0，
//      拖一个滑块会把其余 5 个一起送到 0（严重事故）。未就绪就禁用滑块并提示。
//   ② 姿态"等价表示"会跳变：同一姿态既可写成 (180,-90,0) 也可写成 (-165,…)
//      （万向锁 / ±180° 回绕）。回读后必须把角度**展开到与上一帧最近**的等价角，
//      否则只拖 X，A/C 的读数也会跳几十度。
const poseReady = ref(false);
// ★ 修复"拖动一个轴，其他轴也跟着变"：记录当前正在拖动的轴，拖动期间不刷新其他轴
const draggingAxis = ref(null);

/** 把角度回读值展开到与 prev 最近的等价角（±180° 回绕）。 */
function unwrapAngle(prev, val) {
  let v = val;
  while (v - prev > 180) v -= 360;
  while (v - prev < -180) v += 360;
  return v;
}

const selHud = computed(() => {
  if (controlMode.value === "cart") {
    const ax = CART6[cartAxis.value] || CART6[0];
    return `机器人模式 · 轴 ${ax.k}（${ax.unit}）  ·  末端 (${pose.x.toFixed(0)}, ${pose.y.toFixed(0)}, ${pose.z.toFixed(0)}) mm · `
      + `姿态 (${pose.rx.toFixed(1)}, ${pose.ry.toFixed(1)}, ${pose.rz.toFixed(1)})°`;
  }
  const lim = robot.limits[robot.selIdx] || { min: -180, max: 180 };
  return `关节模式 · J${robot.selIdx + 1} = ${robot.simQ[robot.selIdx].toFixed(1)}°  (限位 ${lim.min} ~ ${lim.max}°)`;
});

onMounted(() => {
  window.addEventListener("keydown", onKey);
  refreshPose();       // 机器人模式六滑块初值 = 当前末端位姿（后端 FK）
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKey);
  if (tcpTimer) clearTimeout(tcpTimer);
  stopDance();
  robot.clearOverride("sim");
});

// 切到机器人模式时，把滑块拉到当前末端坐标（避免沿用上一次的旧目标）
watch(() => controlMode.value, (v) => { if (v === "cart") refreshPose(); });

function clampQ(i, v) {
  const lim = robot.limits[i] || { min: -180, max: 180 };
  return Math.min(lim.max, Math.max(lim.min, v));
}

// ★ Pose authority model: this page no longer drives 3D directly -- registers sandbox pose as override(owner="sim"),
//   only takes effect when this view is in foreground; App.vue render loop reads displayQ for 3D.
function applySimPose() {
  if (robot.activeView !== "sim") return;   // 单模型：只有本视图在前台才登记
  robot.setOverride("sim", robot.simQ, getTcp());
  simTcp.value = getTcp();
}

watch(() => robot.simQ, applySimPose, { deep: true, immediate: true });
// 切回模拟仿真时补一次姿态 + TCP；切走时停止跳舞并交还姿态（override 只认前台 owner，
// 但主动清掉更干净：回来时会由 immediate watch 重新登记）
watch(() => robot.activeView, (v) => {
  if (v === "sim") applySimPose();
  else { stopDance(); robot.clearOverride("sim"); }
});

function onKey(e) {
  if (robot.activeView !== "sim") return;
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (controlMode.value === "cart") return onKeyCart(e);
  const step = e.shiftKey ? 0.1 : 1.0;
  let handled = true;
  const q = robot.simQ.slice();
  if (e.key >= "1" && e.key <= "6") {
    robot.setSelIdx(parseInt(e.key, 10) - 1);
  } else if (e.key === "ArrowUp" || e.key === "+" || e.key === "=") {
    q[robot.selIdx] = clampQ(robot.selIdx, q[robot.selIdx] + step);
    robot.setSimQ(q);
  } else if (e.key === "ArrowDown" || e.key === "-" || e.key === "_") {
    q[robot.selIdx] = clampQ(robot.selIdx, q[robot.selIdx] - step);
    robot.setSimQ(q);
  } else if (e.key === "0") {
    robot.setSimQ([0, 0, 0, 0, 0, 0]);
  } else {
    handled = false;
  }
  if (handled) e.preventDefault();
}

// 机器人模式（= 埃夫特"机器人坐标系"）：1..6 选轴（X/Y/Z/A/B/C），↑↓ 沿轴走一步。
// 平移步进 10mm、姿态步进 5°（Shift 微调：1mm / 1°），全部经 IK 整臂联动。
function onKeyCart(e) {
  const ax = CART6[cartAxis.value] || CART6[0];
  const step = e.shiftKey
    ? (ax.unit === "mm" ? 1 : 1)
    : ax.step;
  let handled = true;
  if (e.key >= "1" && e.key <= "6") {
    cartAxis.value = parseInt(e.key, 10) - 1;
  } else if (e.key === "ArrowUp" || e.key === "+" || e.key === "=") {
    moveCart(cartAxis.value, +step);
  } else if (e.key === "ArrowDown" || e.key === "-" || e.key === "_") {
    moveCart(cartAxis.value, -step);
  } else {
    handled = false;
  }
  if (handled) e.preventDefault();
}

async function moveCart(axis, delta) {
  if (cartBusy.value) return;          // 防抖：上一次 IK 还没回来就别叠加
  cartBusy.value = true;
  try {
    const ax = CART6[axis] || CART6[0];
    pose[ax.k6] = Number((pose[ax.k6] + delta).toFixed(3));
    const ok = await applyCart();
    if (!ok) refreshPose();            // 不可达 → 回弹到真实位姿
  } finally {
    cartBusy.value = false;
  }
}

let tcpTimer = null;

/** 从后端取当前位姿（正运动学）：唯一真值来源，与 /ik 同一套约定。 */
async function refreshPose() {
  try {
    const r = await fetch(apiUrl("/control/fk"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ joints: robot.simQ }),
    });
    if (!r.ok) { poseReady.value = false; return false; }
    const d = await readJson(r);
    if (typeof d.x !== "number") { poseReady.value = false; return false; }
    pose.x = d.x; pose.y = d.y; pose.z = d.z;
    // ★ 姿态展开到与上一帧最近的等价角：避免"只拖 X，A/C 读数却跳几十度"
    pose.rx = unwrapAngle(pose.rx, d.rx);
    pose.ry = unwrapAngle(pose.ry, d.ry);
    pose.rz = unwrapAngle(pose.rz, d.rz);
    poseReady.value = true;
    return true;
  } catch (e) {
    poseReady.value = false;
    return false;
  }
}

/** 把当前 pose（六维）交给后端逆解 → 设回仿真姿态（整臂联动）。 */
async function applyCart() {
  // ★ 位姿未就绪（/control/fk 不可用）→ 绝不下发六维目标：
  //   否则 pose 里的 0 会被当成真值，"拖一个、其余五个全归零"。
  if (!poseReady.value) {
    ikMsg.value = "末端位姿未读取到（需后端 /control/fk，请重启后端）：暂不能按机器人坐标系移动";
    return false;
  }
  ikMsg.value = "计算中…";
  try {
    const r = await fetch(apiUrl("/control/ik"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current: robot.simQ,
        // 六维目标：位置 + 姿态（A/B/C 对应 rz/ry/rx）
        tcp: { x: pose.x, y: pose.y, z: pose.z, rx: pose.rx, ry: pose.ry, rz: pose.rz },
      }),
    });
    const d = await readJson(r);
    if (!r.ok) { ikMsg.value = "IK 请求失败：" + errText(d, r.status); return false; }
    if (d.joints && d.in_limits) {
      robot.setSimQ(d.joints);
      await refreshPose();             // 以真值刷新滑块（IK 有微小误差时也保持一致）
      ikMsg.value = `末端 (${pose.x.toFixed(0)}, ${pose.y.toFixed(0)}, ${pose.z.toFixed(0)}) mm · `
        + `姿态 (${pose.rx.toFixed(1)}, ${pose.ry.toFixed(1)}, ${pose.rz.toFixed(1)})°`;
      return true;
    }
    if (d.violations && d.violations.length) {
      ikMsg.value = "不可达/超限: " + d.violations.map((v) => v.joint).join(", ");
    } else {
      ikMsg.value = "IK 未收敛 (误差 " + (d.pos_err_mm || 0).toFixed(1) + " mm"
        + (d.rot_err_deg ? " / 姿态 " + d.rot_err_deg.toFixed(1) + "°" : "") + ")";
    }
    return false;
  } catch (err) {
    ikMsg.value = "IK 失败: " + err.message;
    return false;
  }
}

/** 拖动中：实时更新目标并节流触发 IK（每 100ms 最多打一次后端）。
 *  这样 3D 模型能实时跟随，幅度通过较小步长（5mm / 1°）控制。 */
function onTcpSlider(i, e) {
  const v = parseFloat(e.target.value);
  if (Number.isNaN(v)) return;
  const ax = CART6[i] || CART6[0];
  pose[ax.k6] = v;
  cartAxis.value = i;
  draggingAxis.value = i;
  // 节流：上一次请求未完成就跳过，不叠加
  if (tcpTimer) return;
  tcpTimer = setTimeout(async () => {
    tcpTimer = null;
    const ok = await applyCart();
    if (!ok) {
      // 不可达 → 滑块回弹
      await refreshPose();
    } else {
      // IK 成功 → 用 FK 同步其他 5 个轴的滑块值
      await refreshPose();
    }
  }, 100);
}

/** 滑块松开：确保执行一次最终同步。 */
async function onTcpSliderEnd(_i) {
  draggingAxis.value = null;
  if (tcpTimer) { clearTimeout(tcpTimer); tcpTimer = null; }
  // 强制执行一次（处理松开时刚好在节流窗口内没执行的情况）
  const ok = await applyCart();
  if (!ok) await refreshPose();
  else await refreshPose();
}

/** 关节角滑块（本页"可操作"的关节角卡用）。 */
function onSlider(i, e) {
  const q = robot.simQ.slice();
  q[i] = parseFloat(e.target.value) || 0;
  robot.setSimQ(q);
}

// ---------- 跳舞演示 ----------
// 轨迹定义在 utils/dance.js（单一来源）。
// J1 摆头 / J2·J3 舒展起伏 / J4 翻腕 / J5 点头 / J6 腕部摆动，平滑无跳变。
const dancing = ref(false);
let danceTimer = null;

function startDance() {
  if (robot.activeView !== "sim") return;
  stopDance();
  dancing.value = true;
  const t0 = performance.now();
  danceTimer = setInterval(() => {
    robot.setSimQ(dancePose((performance.now() - t0) / 1000));
  }, 30);
}

function stopDance() {
  if (danceTimer) { clearInterval(danceTimer); danceTimer = null; }
  dancing.value = false;
}
</script>

<template>
  <div class="sim-root">
    <MonitorLayout view="sim">
      <template #hud>
        <div class="sim-hud">{{ selHud }}</div>
      </template>
      <div class="side-cards">
        <!-- ★ 需求更正：本页移除的是 RobotParamsCard 的**只读**关节角读数卡
             （读数和 3D 姿态同源，属于"已读"展示）；**可操作**的关节角滑块卡
             保留在下方（"关节角 (deg)" 带 range 的那个）。
             TCP 读数由下方独立的"末端 TCP (mm)"卡（与 simTcp 同源）显示，故此处不再引用。 -->
        <div class="card">
        <h3>演示动作</h3>
        <div class="btns">
          <button class="primary" :disabled="dancing" @click="startDance"><Icon name="play" :size="15" /> 跳舞演示</button>
          <button :disabled="!dancing" @click="stopDance"><Icon name="stop" :size="15" /> 停止</button>
        </div>
        <div class="small" style="margin-top:8px">
          一段预设的关节轨迹（J1 摆头 / J2·J3 舒展起伏 / J4 翻腕 / J5 点头 / J6 腕部摆动），
          经姿态平滑丝滑循环。
        </div>
      </div>
      <!-- ★ 合并卡片：关节角和机器人模式，通过 tab 切换 -->
      <div class="card">
        <h3>关节角和机器人模式</h3>
        <div class="seg" style="margin-bottom:10px">
          <button :class="{ on: controlMode === 'joint' }" @click="controlMode = 'joint'">关节模式</button>
          <button :class="{ on: controlMode === 'cart' }" @click="controlMode = 'cart'">机器人模式</button>
        </div>
        <div v-if="ikMsg && ikMsg !== '计算中…'" class="small err-line" style="margin-bottom:8px">{{ ikMsg }}</div>
        <!-- 关节模式：J1~J6 滑块 -->
        <div v-if="controlMode === 'joint'">
          <div class="axis" v-for="(n, i) in robot.axes" :key="n">
            <label>{{ n }}</label>
            <input type="range" :min="robot.limits[i]?.min ?? -180" :max="robot.limits[i]?.max ?? 180"
                   step="0.1" :value="robot.simQ[i]" @input="onSlider(i, $event)" />
            <span class="deg">{{ robot.simQ[i].toFixed(1) }}</span>
          </div>
        </div>
        <!-- 机器人模式：XYZABC 滑块 -->
        <div v-else>
          <div class="axis" v-for="ax in CART6" :key="ax.k"
               :class="{ hot: cartAxis === ax.i }">
            <label>{{ ax.k }}</label>
            <input type="range" :min="ax.min" :max="ax.max" :step="ax.step"
                   :value="pose[ax.k6]" :disabled="!canCartMove || !poseReady"
                   @input="onTcpSlider(ax.i, $event)"
                   @change="onTcpSliderEnd(ax.i)" />
            <span class="deg">{{ pose[ax.k6].toFixed(1) }}</span>
          </div>
          <div v-if="!poseReady" class="small" style="color:var(--warn);margin-top:6px">
            等待位姿数据…
          </div>
        </div>
      </div>
      <div class="card">
        <h3>末端 TCP (mm) 坐标
          <span class="h3-sub">只读</span>
        </h3>
        <div class="row"><span class="k">X</span><span class="v">{{ simTcp.x.toFixed(1) }}</span></div>
        <div class="row"><span class="k">Y</span><span class="v">{{ simTcp.y.toFixed(1) }}</span></div>
        <div class="row"><span class="k">Z</span><span class="v">{{ simTcp.z.toFixed(1) }}</span></div>
        <div v-if="controlMode === 'cart'" class="small" style="margin-top:6px;color:var(--muted)">
          机器人模式拖动上方滑块即可联动修改 TCP 坐标
        </div>
      </div>
    </div>
    </MonitorLayout>
  </div>
</template>

<style scoped>
.sim-root { position: relative; flex: 1; min-height: 0; display: flex; }
/* 左侧浮动物体管理面板：可折叠，靠在 3D 视口左上、避开顶部 HUD 一行 */
.sim-floating { position: absolute; left: 12px; top: 44px; width: 286px; z-index: 30;
  max-height: calc(100% - 60px); overflow-y: auto; }

/* ===== XPL 仿真执行卡片：纵向分区，层次清晰、控件不挤压 ===== */
.xpl-card :deep(h3) { display: flex; align-items: center; gap: 6px; }
.xpl-badge {
  margin-left: auto; font-size: 10px; font-weight: 500; color: var(--warn);
  border: 1px solid var(--warn-line); background: var(--warn-soft);
  border-radius: 20px; padding: 1px 8px; white-space: nowrap;
}
.xpl-field { display: flex; flex-direction: column; gap: 5px; margin-bottom: 10px; }
.xpl-label { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
.xpl-hint { color: var(--muted); opacity: .8; font-size: 10px; line-height: 1.4; }
.xpl-select { width: 100%; background: var(--panel2); color: var(--txt);
  border: 1px solid var(--line); border-radius: 7px; padding: 7px 9px; font-size: 12px;
  font-family: inherit; }
.xpl-select:focus { outline: none; border-color: var(--accent); }
.xpl-ta { width: 100%; resize: vertical; font-family: Consolas, monospace; font-size: 11.5px;
  line-height: 1.55; min-height: 96px; box-sizing: border-box;
  background: var(--panel2); color: var(--txt); border: 1px solid var(--line);
  border-radius: 7px; padding: 8px 10px; }
.xpl-ta:focus { outline: none; border-color: var(--accent); }
.xpl-actions { display: flex; gap: 8px; margin-bottom: 8px; }
.xpl-actions .xpl-btn { flex: 1 1 0; justify-content: center; display: inline-flex;
  align-items: center; gap: 5px; padding: 8px 10px; }
.xpl-status { display: flex; align-items: center; gap: 8px; font-size: 11px;
  color: var(--txt); min-height: 18px; flex-wrap: wrap; }
.xpl-status b { color: var(--accent); }
.xpl-suck { font-size: 10px; color: var(--ok); border: 1px solid var(--ok-line);
  background: var(--ok-soft); border-radius: 20px; padding: 1px 8px; }
.xpl-err { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 11px;
  color: var(--err); border: 1px solid var(--err-line, var(--line)); background: var(--err-soft, var(--panel2));
  border-radius: 7px; padding: 7px 9px; }
.xpl-speed { display: flex; align-items: center; gap: 8px; margin-top: 10px; }
.xpl-speed input[type="range"] { flex: 1; accent-color: var(--accent); }
.xpl-speed-v { width: 44px; text-align: right; font-variant-numeric: tabular-nums;
  font-size: 12px; color: var(--txt); }

/* 操控模式里的"仅出错才显示"提示行 */
.err-line { margin-top: 8px; color: var(--err); }
/* 机器人模式：当前点动轴那一行高亮 */
.row.hot { background: var(--accent-soft2); border-radius: 4px; }
/* 机器人模式：六轴联动结果（整臂运动的佐证） */
.linkq { display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 4px;
  font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.linkq b { color: var(--accent); }
</style>
