// =====================================================================
// 顶部导航（Tab）定义 + 权限门控 —— 纯逻辑，不依赖 Vue / DOM。
//
// ★ 为什么单独抽一个文件：
//   「没拿到权限就把页面藏起来」是一条**安全相关的行为约定**，藏错方向会很难受：
//     - 藏少了 → 现场点进执行页，看到一片灰按钮 / 一串 403，以为系统坏了；
//     - 藏多了 → 用户明明已经授权，却找不到功能入口。
//   写在 App.vue 的 <script setup> 里没法在 node 里断言，只能靠人手点。抽成纯函数后
//   由 tools/verify_tabs.mjs 把每一种权限组合下的"可见集合"钉死，改坏了立刻红。
//
// 权限档位（与后端 backend/app/api/auth.py 的角色模型严格对齐）：
//   - 无令牌                  → 只有只读页
//   - operator（操作员）      → + 点位执行 / 程序执行 / 系统设置（能操控机器人；设置页里
//                               标了「需管理员」的项仍然改不了，后端逐字段拦）
//   - admin（管理员）         → + 运维审计（改围栏配置 / 清审计日志 / 导备份 / 回滚版本）
//   - 令牌过期（expiresAt 已过）→ 与"无令牌"完全一致
//
// ★ 这里只决定"显不显示入口"，绝不承担权限判定：每个写接口仍然由后端
//   require_control(401) / require_admin(403) 把关。前端藏入口是体验，不是安全边界。
//
// ★ 数字键 1~8 对应「从左到右第 N 个**可见**模块」。授权齐全时 1~8 恰好就是上面
//   8 个模块；没权限时后面的不显示、序号顺延（解析与断言见 src/shortcuts.js）。
// =====================================================================

/** 被收回权限时的回落页。必须排在 TABS 第一个，且永不需要令牌。 */
export const HOME_VIEW = "live";

/**
 * Tab 定义。
 *   icon      —— 必须存在于 components/Icon.vue 的 ICONS（不存在会静默渲染空 SVG）
 *   needAuth  —— 需要有效控制令牌（admin 或 operator 均可）
 *   needAdmin —— 需要 **管理员** 令牌（隐含 needAuth）
 */
export const TABS = [
  { key: "live", icon: "camera", label: "真实监控" },
  { key: "sim", icon: "layers", label: "模拟仿真" },
  { key: "point", icon: "target", label: "点位执行", needAuth: true },
  { key: "program", icon: "play", label: "程序执行", needAuth: true },
  { key: "sort", icon: "layers", label: "分拣", needAuth: true },
  { key: "ops", icon: "activity", label: "运维审计", needAdmin: true },
  { key: "settings", icon: "sliders", label: "系统设置", needAuth: true },
  { key: "about", icon: "info", label: "关于" },
];

/** 数字键能直达的模块数（与 src/shortcuts.js 的 TAB_KEY_COUNT 必须一致）。 */
export const TAB_KEY_COUNT = 8;

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
