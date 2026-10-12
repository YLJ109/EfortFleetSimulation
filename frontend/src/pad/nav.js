// =====================================================================
// 手柄执行页 · 20 点光标导航（纯逻辑，不依赖 Vue / DOM）
//
// ★ 为什么单独抽一层：手柄"按一下往哪走"必须是**可断言的**。
//   若靠 2D 图的像素坐标去猜（比较 clientRect 找最近的格），现场会出现
//   "按一下光标飞了 / 卡在角落出不来"这种只有上手才发现的问题，还没法 headless 钉。
//   这里改用**显式邻接表**：每个点的上下左右各自指向谁，全部写死；`null` = 不动（不越界）。
//
// ★ 判据唯一：本文件是"光标怎么走"的唯一事实来源。
//   tools/verify_pad_nav.mjs 逐点逐向断言 + 20 点全覆盖 + 任意两点可互达。
// =====================================================================

/** 后端真实点位名（必须与 backend/app/services/sorting.py 的 START_POINT / CAMERA_POINT 一致）。 */
export const START = "开始/结束点";
export const CAMERA = "C检测";

/** A / B 两区 3×3（行优先）—— 只用于 2D 渲染取格子顺序，不参与导航判定。 */
export const ZONE_A = [["A1", "A2", "A3"], ["A4", "A5", "A6"], ["A7", "A8", "A9"]];
export const ZONE_B = [["B1", "B2", "B3"], ["B4", "B5", "B6"], ["B7", "B8", "B9"]];

/** 全部 20 个点（渲染顺序：起点 → C 检测 → B 区 → A 区）。 */
export const POINTS = [START, CAMERA, ...ZONE_B.flat(), ...ZONE_A.flat()];

/** 方向常量（与 games/gamepad.js 的方向动作名一致）。 */
export const DIRS = ["up", "down", "left", "right"];

const N = null;   // 不越界 = 不动

/**
 * 显式邻接表：点名 → { up, down, left, right }（值 = 目标点名，或 null = 不动）。
 *
 * ★ 布局口径（用户 2026-10-11）：「左边 B 区 / 中间下面 C 检测 / 上面起点 / 右边 A 区」——
 *   三块"岛"由 **C 检测**当枢纽连通；A/B 区顶行**中间**那格 ↑ 回到起点（左右对称）。
 * ★ 边界不许翻墙：顶不动就是不动（现场最忌讳"按一下光标飞了"）。
 * ★ A 区在右 → 其左列（A1/A4/A7）← 去 C 检测；B 区在左 → 其右列（B3/B6/B9）→ 去 C 检测。
 */
export const NAV = {
  [START]: { up: N, down: CAMERA, left: "B2", right: "A2" },
  [CAMERA]: { up: START, down: N, left: "B5", right: "A5" },

  // ---- A 区（右）----
  A1: { up: N, down: "A4", left: CAMERA, right: "A2" },
  A2: { up: START, down: "A5", left: "A1", right: "A3" },
  A3: { up: N, down: "A6", left: "A2", right: N },
  A4: { up: "A1", down: "A7", left: CAMERA, right: "A5" },
  A5: { up: "A2", down: "A8", left: "A4", right: "A6" },
  A6: { up: "A3", down: "A9", left: "A5", right: N },
  A7: { up: "A4", down: N, left: CAMERA, right: "A8" },
  A8: { up: "A5", down: N, left: "A7", right: "A9" },
  A9: { up: "A6", down: N, left: "A8", right: N },

  // ---- B 区（左）----
  B1: { up: N, down: "B4", left: N, right: "B2" },
  B2: { up: START, down: "B5", left: "B1", right: "B3" },
  B3: { up: N, down: "B6", left: "B2", right: CAMERA },
  B4: { up: "B1", down: "B7", left: N, right: "B5" },
  B5: { up: "B2", down: "B8", left: "B4", right: "B6" },
  B6: { up: "B3", down: "B9", left: "B5", right: CAMERA },
  B7: { up: "B4", down: N, left: N, right: "B8" },
  B8: { up: "B5", down: N, left: "B7", right: "B9" },
  B9: { up: "B6", down: N, left: "B8", right: CAMERA },
};

/** 光标起点（进页面 / 复位时的位置）。 */
export const CURSOR_HOME = START;

/** 是否 A/B 区的"槽位"点（A1…A9 / B1…B9）—— 只有这些点才有"正上位"。 */
export function isSlot(name) {
  return /^[AB][1-9]$/.test(String(name || "").trim());
}

/** 「正上位」点名（槽位 + "上"）；不是槽位（C 检测 / 起点）→ null（它们没有正上位）。 */
export function abovePoint(name) {
  return isSlot(name) ? String(name).trim() + "上" : null;
}

/**
 * 光标移动。边界（null）→ 原地不动（返回 from），不返回 null、不抛。
 * @param {string} from 当前点名
 * @param {string} dir  up|down|left|right
 */
export function moveCursor(from, dir) {
  const row = NAV[from];
  if (!row) return CURSOR_HOME;          // 未知点 → 回起点（绝不静默卡死）
  return row[dir] || from;
}

/** 从某点出发能走到的全部点（DFS）—— 供回归脚本断言"20 点连通"。 */
export function reachable(from) {
  const seen = new Set([from]);
  const stack = [from];
  while (stack.length) {
    const cur = stack.pop();
    const row = NAV[cur] || {};
    for (const d of DIRS) {
      const nx = row[d];
      if (nx && !seen.has(nx)) { seen.add(nx); stack.push(nx); }
    }
  }
  return seen;
}

/** 2D 格子里显示的短标签。 */
export function shortLabel(name) {
  if (name === START) return "起点";
  if (name === CAMERA) return "C";
  return name;
}

/** 面板"当前选中"用的长标签（如实写出后端点名，不美化）。 */
export function longLabel(name) {
  if (name === START) return START;
  if (name === CAMERA) return CAMERA + "（摄像头）";
  return name;
}
