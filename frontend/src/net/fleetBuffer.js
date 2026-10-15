// =====================================================================
// 机房帧缓冲：WS 20Hz 写入 → RAF 60Hz 读取，中间**不进 Vue 响应式**。
//
// 为什么不让 WS 回调直接写 pinia：
//   后端 batch_interval_ms 默认 50（20Hz），一帧带 12 台 × 6 轴 + TCP + 状态。
//   写进 reactive state 会触发 12 张卡片的依赖重算，而画面本身每 16ms 还要读一次
//   最新角度 —— 两条路都要"最新值"，但只有卡片需要**触发渲染**。
//   所以这里把两者拆开：
//     · pose  —— 纯对象 + 原地覆盖同一个数组，60Hz 读取时零分配、零响应式；
//     · state —— 每台的在线/坐标/报警快照，由 fleet store 按 4Hz 取一次做卡片。
//   真机 12 台同时动，这个分配量级直接决定大屏会不会掉帧。
//
// ★ 坏帧策略：**整台丢弃**，不逐轴补零。
//   少一根轴就补 0，画面上那根轴会瞬间甩到零位 —— 现场看起来像机器人抽了一下，
//   比"停在上一帧"危险得多（操作员会以为真机动了）。
// =====================================================================

/** id → [J1..J6]（度）。原地覆盖，数组对象本身不替换。 */
const pose = Object.create(null);

/** id → 最近一帧的状态快照（整对象替换，读侧只读不写）。 */
const state = Object.create(null);

/** 收到帧的时间戳与计数：判断"WS 通了但没人推"（半开链路 / 机房未启动）。 */
export const stamp = { frame: 0, status: 0, frames: 0, statuses: 0 };

function finite6(a) {
  if (!Array.isArray(a) || a.length !== 6) return false;
  for (let i = 0; i < 6; i++) if (!Number.isFinite(Number(a[i]))) return false;
  return true;
}

/**
 * 吃一帧 `fleet_pose`（后端 services/fleet.py::_batch_loop 推的合批帧）。
 * @param {Array<object>} robots Station.snapshot() 的数组
 * @returns {number} 本帧被采纳的台数（0 = 整帧是坏的）
 */
export function ingestFrame(robots) {
  if (!Array.isArray(robots) || !robots.length) return 0;
  const now = Date.now();
  let ok = 0;
  for (const r of robots) {
    if (!r || typeof r.id !== "string") continue;
    const s = {
      online: !!r.online,
      stale: !!r.stale,
      simulated: !!r.simulated,
      tcp: Array.isArray(r.tcp) && r.tcp.length >= 3 ? r.tcp.slice(0, 3) : null,
      cart: Array.isArray(r.cart) && r.cart.length >= 6 ? r.cart.slice(0, 6) : null,
      velocity: Array.isArray(r.velocity) ? r.velocity.slice(0, 6) : null,
      status: (r.status && typeof r.status === "object") ? r.status : {},
      error: r.error || "",
      at: Number(r.at) ? Number(r.at) * 1000 : now,
    };
    state[r.id] = s;
    if (!finite6(r.joints)) continue;          // 姿态坏 → 状态照样更新，姿态停在上帧
    const a = pose[r.id] || (pose[r.id] = [0, 0, 0, 0, 0, 0]);
    for (let i = 0; i < 6; i++) a[i] = Number(r.joints[i]);
    ok++;
  }
  stamp.frame = now;
  stamp.frames++;
  return ok;
}

/** 吃一帧 `fleet_status`（1Hz 汇总：在线/报警/运动台数）。 */
export function ingestStatus(summary) {
  if (!summary || typeof summary !== "object") return null;
  stamp.status = Date.now();
  stamp.statuses++;
  return summary;
}

/**
 * 给 three 层用的姿态表：{ id: [J1..J6] }。
 * ★ 返回**内部对象本身**（不是拷贝）：fleet.js 只读它，60Hz 拷贝一次纯属浪费。
 */
export function poseMap() { return pose; }

/** 某台的最近状态；没收到过任何帧则 null。 */
export function stateOf(id) { return state[id] || null; }

/** 全部状态快照（内部对象，读侧只读）。 */
export function states() { return state; }

/** 距上一帧姿态多久（ms）；从未收到过帧时返回 Infinity。 */
export function poseAgeMs(now = Date.now()) {
  return stamp.frame ? now - stamp.frame : Infinity;
}

/** 清空（切换机房配置 / 退出大屏 / 单元测试之间隔离）。 */
export function clear() {
  for (const k of Object.keys(pose)) delete pose[k];
  for (const k of Object.keys(state)) delete state[k];
  stamp.frame = 0; stamp.status = 0; stamp.frames = 0; stamp.statuses = 0;
}
