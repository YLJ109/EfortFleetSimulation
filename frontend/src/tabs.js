// =====================================================================
// 模块导航定义 + 权限门控 —— 纯逻辑，不依赖 Vue / DOM。
//
// ★ 为什么单独抽一个文件：
//   「没拿到权限就把入口藏起来」是一条**安全相关的行为约定**，藏错方向会很难受：
//     - 藏少了 → 现场点进操作页，看到一片灰按钮 / 一串 403，以为系统坏了；
//     - 藏多了 → 用户明明已经授权，却找不到功能入口。
//   写在 App.vue 的 <script setup> 里没法在 node 里断言，只能靠人手点。抽成纯函数后
//   由 tools/verify_tabs.mjs 把每一种权限组合下的"可见集合"钉死，改坏了立刻红。
//   门控机制（tabAllowed / needAuth / needAdmin）整套保留 —— 后面按台授权还要用，
//   不能因为"现在只剩一个入口"就把判据删掉。
//
// ★ 2026-10-06 用户口径「就要一个机房大屏，其他的不需要」：
//   产品收敛成**单页应用 = 机房大屏**。原来的「真实监控 / 模拟仿真 / 关于」整体移除，
//   「点位执行 / 程序执行 / 运维审计 / 系统设置」也不再作为页面挂载 —— 组件文件保留在
//   磁盘上（本仓库没有 git，删文件不可逆；真机排查时还要拿单机页对照），但导航里没有它们。
//   控制与设置能力改在大屏内部以面板形式回来（见 FleetView 的作用域控制台与设置抽屉）。
//
// ★ 这里只决定"显不显示入口"，绝不承担权限判定：每个写接口仍然由后端
//   require_control(401) / require_admin(403) 把关。前端藏入口是体验，不是安全边界。
// =====================================================================

/** 被收回权限时的回落页。必须排在 TABS 第一个，且永不需要令牌。 */
export const HOME_VIEW = "fleet";

/**
 * Tab 定义。
 *   icon      —— 必须存在于 components/Icon.vue 的 ICONS（不存在会静默渲染空 SVG）
 *   needAuth  —— 需要有效控制令牌（admin 或 operator 均可）
 *   needAdmin —— 需要 **管理员** 令牌（隐含 needAuth）
 */
export const TABS = [
  { key: "fleet", icon: "robot", label: "机房大屏" },
];

/** 数字键能直达的模块数（与 src/shortcuts.js 的 TAB_KEY_COUNT 必须一致）。 */
export const TAB_KEY_COUNT = 1;

/**
 * 某个 tab 对当前权限是否开放。
 * @param {{needAuth?:boolean, needAdmin?:boolean}} tab
 * @param {{controlActive?:boolean, isAdmin?:boolean}|null} auth  auth store（或其快照）
 */
export function tabAllowed(tab, auth) {
  if (!tab) return false;
  if (tab.needAdmin) return !!(auth && auth.isAdmin);        // isAdmin 已含"未过期"
  if (tab.needAuth) return !!(auth && auth.controlActive);
  return true;
}

/** 当前权限下可见的 tab 列表（保持 TABS 的原始顺序）。 */
export function visibleTabs(auth) {
  return TABS.filter((t) => tabAllowed(t, auth));
}

/** 当前权限下允许停留的 view key 集合。 */
export function allowedKeys(auth) {
  return visibleTabs(auth).map((t) => t.key);
}

/**
 * 把"想去的页面"校正到当前权限允许的范围内；越权则回落到 HOME_VIEW。
 * 用于：刷新恢复上次页面、以及权限被收回时把用户送走。
 */
export function safeView(want, auth) {
  return allowedKeys(auth).includes(want) ? want : HOME_VIEW;
}

/** 某个 key 是否是一个合法 tab（防手改 localStorage 塞进垃圾值）。 */
export function isKnownView(key) {
  return TABS.some((t) => t.key === key);
}
