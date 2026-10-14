// =====================================================================
// ★★ C4「四层导航 + 报警 ≤3 击到达能采取行动的画面」的唯一映射表（纯逻辑，可 headless 断言）
//
// 方案：docs/方案-创新功能四批（…）.md §C4
//
// 四层（本项目的实际落位）：
//   L1 总览 = StatusStrip + 安全横幅（**常显**，任何页都在）
//   L2 区域 = 顶栏 tab（`src/tabs.js` 的 TABS）
//   L3 设备 = 页面里的卡片（真机链路卡 / 围栏面板 / 执行卡 …）
//   L4 细节 = 卡片里的动作按钮 / 抽屉 / 取证包
// 「≤3 击」的算法（钉在 tools/verify_nav_depth.mjs）：
//   第 1 击：报警行上的「去处理」（在本表里解析出目标 tab，不靠人记路）
//   第 2 击：进入该页后可见的 L3 卡片（不折叠、不需要先展开）
//   第 3 击：卡片里的行动按钮（停止 / 就绪 / 查看取证 …）
//   ⇒ 从"看到报警"到"能动手"= 3 击封顶。**做不到 3 击的规则一律不许进本表**
//      （`jumpFor()` 返回 null，界面如实显示"这条只能在这里看，没有可直接行动的页面"，
//       不假装能一键跳过去）。
//
// ★ 为什么这张表是"唯一来源"：报警台账（EventsView）与横幅（App.vue）都要给入口，
//   各写一份 target 就会分裂（§5.5 铁律④）。后端 `services/alarms.py` 新增规则时，
//   verify_nav_depth.mjs 会因为"表里没有这条"直接判红 —— 防的是"新增报警没人管去处"。
// =====================================================================
import { TABS } from "../tabs.js";

/** L1 总览页（StatusStrip 在这一页最完整；也是"没权限时的回落页" = tabs.js 的 HOME_VIEW）。 */
export const L1_VIEW = "live";

/** 目标 tab 的合法集（由 tabs.js 派生，不抄第二份 key 列表）。 */
export const VIEW_KEYS = Object.freeze(TABS.map((t) => t.key));

/**
 * 报警 → 能采取行动的画面。
 *   view —— 目标 tab key（必须是 VIEW_KEYS 里的）
 *   card —— 进去之后要看的那张 L3 卡片（写成人话，按钮 title 里给操作员看）
 *   act  —— 第 3 击点的那个动作（必须在该页**可见**，不许藏在折叠/抽屉里）
 *   needsAdmin —— 该页是否需要管理员（决定无权限时按钮是禁用还是可点，禁用要说明为什么）
 */
export const ALARM_TARGETS = Object.freeze({
  // ---- 控制器 / 就绪类：去「真实监控」右栏的真机链路卡 ----
  "control.ready": { view: "live", card: "真机链路", act: "一键就绪" },
  "control.estop": { view: "live", card: "执行卡（急停/复位急停）", act: "复位急停" },
  "control.estop_failed": { view: "live", card: "执行卡（急停/复位急停）", act: "复位急停" },
  "ai.estop": { view: "live", card: "执行卡（急停/复位急停）", act: "复位急停" },
  "control.interlock_absent": { view: "live", card: "执行卡", act: "停止" },
  "control.move_blocked": { view: "live", card: "执行卡", act: "停止" },
  "control.run_file_missing": { view: "program", card: "程序执行", act: "重选程序" },
  "control.run_reaped": { view: "program", card: "程序执行", act: "停止" },
  "control.run_cancelled": { view: "program", card: "程序执行", act: "停止" },
  "control.run_file": { view: "program", card: "程序执行", act: "停止" },
  "control.dry_run": { view: "sort", card: "分拣参数", act: "改成真实下发" },
  // ---- 围栏 / 安全评估类：去「真实监控」左栏的围栏面板（后端只有 hit/danger/warn 三档） ----
  "safety.hit": { view: "live", card: "安全围栏", act: "停止" },
  "safety.danger": { view: "live", card: "安全围栏", act: "停止" },
  "safety.warn": { view: "live", card: "安全围栏", act: "停止" },
  "safety.server_hit": { view: "live", card: "安全围栏（后端独立评估）", act: "停止" },
  "safety.server_danger": { view: "live", card: "安全围栏（后端独立评估）", act: "停止" },
  "safety.server_warn": { view: "live", card: "安全围栏（后端独立评估）", act: "停止" },
  "safety.save_failed": { view: "settings", card: "安全配置", act: "重新保存" },
  "safety.reset": { view: "settings", card: "安全配置", act: "导入现场围栏配置" },
  "safety.rollback": { view: "settings", card: "安全配置", act: "核对版本内容" },
  // ---- 链路 / 采集器类：有明确去处（左栏「链路自检」） ----
  "connection.degraded": { view: "live", card: "链路自检", act: "重新连接" },
  "collector.crashed": { view: "live", card: "链路自检", act: "重新连接" },
  // ---- 孪生 / 持件证据类 ----
  "twin_residual_over": { view: "live", card: "到位自证", act: "停止" },
  "hold_state_contradiction": { view: "sort", card: "持件证据", act: "停止" },
  // ---- 只有"看"、没有"动手"的（AI 交接 / 提示级 / 删除类）**不进本表**：
  //      `jumpFor()` 返回 null，界面如实显示「这条没有可直接行动的页面」。 ----
});

/**
 * 前缀兜底：`rc.alarm.<code>`（由报警码派生的一族，数量不定）→ 真机链路卡清报警。
 * 顺序敏感：**先长后短**（`safety.server_` 已在上表逐条列出，走不到这里）。
 */
export const ALARM_PREFIXES = Object.freeze([
  ["rc.alarm.", { view: "live", card: "真机链路", act: "清报警" }],
  ["safety.", { view: "live", card: "安全围栏", act: "停止" }],
]);

/** 没有去处时**如实返回 null**（不许给一个"看着能点、点了没反应"的按钮）。 */
export function jumpFor(ruleId) {
  const id = String(ruleId || "").trim();
  if (!id) return null;
  const hit = ALARM_TARGETS[id];
  if (hit) return targetOf(id, hit);
  for (const [p, t] of ALARM_PREFIXES) if (id.startsWith(p)) return targetOf(id, t);
  return null;
}

function targetOf(ruleId, t) {
  // 目标 tab 不存在（例如 program 被 hidden / 权限不够）→ 依然给出去处，
  // 但把"能不能真到达"交给 allowedKeys（tabs.js 的可见集）在界面上判，不在这里瞎改。
  return { ruleId, view: t.view, card: t.card, act: t.act,
    exists: VIEW_KEYS.indexOf(t.view) >= 0 };
}

/**
 * 点击深度（从"看到这条报警"起算）。封顶 3，见文件头。
 * 返回 null = 这条没有可行动页面（界面按 null 显示，不显示按钮）。
 */
export function clickDepth(ruleId) {
  const j = jumpFor(ruleId);
  if (!j || !j.exists) return null;
  return 3;
}

/** 该目标页需不需要管理员/操作员令牌（由 tabs.js 派生，不抄第二份权限）。 */
export function viewNeeds(view) {
  const t = TABS.find((x) => x.key === view);
  if (!t) return { known: false, auth: false, admin: false };
  return { known: true, auth: !!t.needAuth, admin: !!t.needAdmin, hidden: !!t.hidden };
}
