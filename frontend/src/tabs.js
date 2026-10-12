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
// ★ 数字键 1~9 对应「顶栏从左到右第 N 个**可见**模块」（hidden 的不占位）。
//   没权限时后面的不显示、序号顺延（解析与断言见 src/shortcuts.js）。
// =====================================================================

/** 被收回权限时的回落页。必须排在 TABS 第一个，且永不需要令牌。 */
export const HOME_VIEW = "live";

/**
 * Tab 定义。
 *   icon      —— 必须存在于 components/Icon.vue 的 ICONS（不存在会静默渲染空 SVG）
 *   needAuth  —— 需要有效控制令牌（admin 或 operator 均可）
 *   needAdmin —— 需要 **管理员** 令牌（隐含 needAuth）
 *   side      —— 顶栏摆哪边："left" 在标题左侧、"right" 在标题右侧（2026-10-10 用户布局）
 *   hidden    —— **不在顶栏出现**，但仍是合法页面（刷新能恢复、权限照旧、可被别的入口带过去）
 *   alias     —— 顶栏高亮的"别名 key"（例：「点位执行」合并了「程序执行」→ 选 program 时
 *                顶栏也应高亮 point 那个 tab）
 */
export const TABS = [
  // ---------- 顶栏左侧 ----------
  { key: "live", icon: "camera", label: "真实监控", side: "left" },
  // ★ 2026-10-10 用户口径：**「点位执行」与「程序执行」合并成一个入口** ——
  //   顶栏只出这一个 tab，两者在**右栏顶部的 tab 条**里切换（见 ExecTabBar 用法）。
  { key: "point", icon: "target", label: "点位执行", needAuth: true, side: "left",
    alias: ["program"] },
  // 与「点位执行」合并：**不在顶栏出现**，靠右栏 tab 条切换（紧跟 point 便于「关于」页成对说明）
  { key: "program", icon: "play", label: "程序执行", needAuth: true,
    hidden: true, side: "left" },
  // ★ 用户 2026-10-10 二次口径：**智能分拣 / AI 执行 放在标题左侧**（模拟仿真留在右侧）。
  { key: "sort", icon: "layers", label: "智能分拣", needAuth: true, side: "left" },
  { key: "ai", icon: "megaphone", label: "AI 执行", needAuth: true, side: "left" },
  // ★ 2026-10-11 用户口径：新增「手柄执行」页（2D 选点 + 单次气路 + 井字棋搬入），
  //   放在标题**左侧**、排在「AI 执行」之后（"应该是左边吧"）。
  //   会下发真实运动 → needAuth（与点位/分拣/AI 同一把令牌，不是权限后门）。
  { key: "pad", icon: "gamepad", label: "手柄执行", needAuth: true, side: "left" },
  // ---------- 顶栏右侧 ----------
  { key: "sim", icon: "layers", label: "模拟仿真", side: "right" },
  { key: "ops", icon: "activity", label: "运维审计", needAdmin: true, side: "right" },
  { key: "settings", icon: "sliders", label: "系统设置", needAuth: true, side: "right" },
  { key: "about", icon: "info", label: "关于", side: "right" },
];

/** 顶栏实际显示的 tab 数（数字键 1~N 直达）—— 与 src/shortcuts.js 的 TAB_KEY_COUNT 必须一致。 */
export const TAB_KEY_COUNT = TABS.filter((t) => !t.hidden).length;     // = 9

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

/**
 * **顶栏要显示的** tab（权限够 **且** 非 hidden）—— 顶栏渲染与数字键 1~N 都用它。
 * ★ 与 visibleTabs 的区别只有一条：hidden（如「程序执行」）不占顶栏位置，
 *   但它是**合法页面**（allowedKeys 里仍然有它，刷新能恢复、右栏 tab 能切过去）。
 */
export function navTabs(auth) {
  return TABS.filter((t) => !t.hidden && tabAllowed(t, auth));
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
