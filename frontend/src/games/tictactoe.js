// =====================================================================
// 井字棋**纯逻辑**（可被 node 直接 import；**不引入 Vue / DOM / three**）
//
// 方案：docs/方案-井字棋对弈（机器人真实落子）.md
//
// ★ 为什么单独一层（与后端 services/tictactoe.py 同一设计口径）：
//   "算法正确"与"机器人搬运"是两件事。这里只管"棋盘上该怎么下/怎么判定"，
//   永远不碰运动通道 —— 于是胜负判定、格子映射、光标边界都能被 node 直接钉死
//   （见 tools/verify_tictactoe.mjs），不需要真机、不需要起后端。
//
// ★ 与后端**同口径**（改这里必须同步后端 services/tictactoe.py）：
//   · 棋盘 = B 区 3×3，行优先编号：cell i ↔ B 槽位 B(i+1)（B1..B9）
//   · 先手执圆（O，来源 A1–A5）；后手执叉（X，来源 A6–A9）
//   · 结果枚举：""（进行中）| user_win | robot_win | draw
//   · 8 条三连线（行 → 列 → 对角，顺序与后端一致）
// =====================================================================

const O = "O";   // 圆（来源 A1–A5）
const X = "X";   // 叉（来源 A6–A9）
// ★ 用命名导出（而非 `export const`）暴露 O/X：单字母常量名撞上
//   tools/check_imports.mjs 对"跨模块 export const"的误报判据
//   （模板/坐标文案里的裸 X 会被当成"用了 X 却没导入"）。命名导出同样可 import。
export { O, X };

/** 8 条三连线（行 3 + 列 3 + 对角 2），下标与 cell 0..8 一致。 */
export const WIN_LINES = [
  [0, 1, 2], [3, 4, 5], [6, 7, 8],   // 行
  [0, 3, 6], [1, 4, 7], [2, 5, 8],   // 列
  [0, 4, 8], [2, 4, 6],              // 对角
];

/** 难度中文名（与后端 _DIFF_CN 一致）。 */
export const DIFF_CN = { easy: "入门", normal: "普通", hard: "困难" };

/** 难度英文 key → 中文名（非法值回落「普通」，与后端 diff_cn 同口径）。 */
export function diffCn(d) {
  return DIFF_CN[String(d || "").toLowerCase()] || "普通";
}

/** 结果枚举 → 中文（界面显示用）。 */
export function resultCn(r) {
  if (r === "user_win") return "你赢了";
  if (r === "robot_win") return "你输了";
  if (r === "draw") return "平局";
  return "进行中";
}

/** 符号 → 形状字符（界面显示用，O=圆、X=叉）。 */
export function symCn(sym) {
  return sym === O ? "○" : sym === X ? "✕" : "—";
}

/** 9 格空棋盘（行优先）。 */
export function newBoard() {
  return [null, null, null, null, null, null, null, null, null];
}

/** 对手符号。 */
export function other(sym) {
  return sym === O ? X : O;
}

/** cell i 是否可落子（越界/已占 → false，不抛）。 */
export function legal(board, i) {
  return Number.isInteger(i) && i >= 0 && i < 9 && !!board && board[i] === null;
}

/** 是否满格。 */
export function isFull(board) {
  return !!board && board.length === 9 && board.every((v) => v !== null && v !== undefined);
}

/**
 * 胜负判定。
 * @returns {{sym:string, line:number[]}|null} 有三连 → 符号与三连线；否则 null。
 */
export function winner(board) {
  if (!board) return null;
  for (const line of WIN_LINES) {
    const [a, b, c] = line;
    const s = board[a];
    if (s && s === board[b] && s === board[c]) return { sym: s, line: line.slice() };
  }
  return null;
}

/** 平局：满格且无三连。 */
export function isDraw(board) {
  return winner(board) === null && isFull(board);
}

/** 棋盘格 → B 槽位名（cell 0 → B1 … cell 8 → B9）；越界返回 ""。 */
export function cellToSlot(i) {
  const n = Number(i);
  if (!Number.isInteger(n) || n < 0 || n > 8) return "";
  return "B" + (n + 1);
}

/** B 槽位名 → 棋盘格；非 B1..B9 → null（大小写不敏感，忽略首尾空白）。 */
export function slotToCell(name) {
  const s = String(name == null ? "" : name).trim().toUpperCase();
  if (s.length < 2 || s[0] !== "B") return null;
  const n = Number(s.slice(1));
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < 1 || n > 9) return null;
  return n - 1;
}

/**
 * 终局结果（给界面用，与后端 result_of 同口径）。
 * @returns {{result:string, winLine:number[]}} result ∈ "" | user_win | robot_win | draw
 */
export function resultOf(board, userSym) {
  const w = winner(board);
  if (w) {
    return { result: w.sym === userSym ? "user_win" : "robot_win", winLine: w.line.slice() };
  }
  if (isFull(board)) return { result: "draw", winLine: [] };
  return { result: "", winLine: [] };
}

/**
 * 2D 棋盘光标移动（↑↓←→）—— **边界夹住、不环绕**（用户口径）。
 * ★ 与后端无关，纯界面导航：↑ 到第一行停住、↓ 到末行停住、左右同理。
 * @param {number} cursor 当前格 0..8
 * @param {"up"|"down"|"left"|"right"} key 方向
 * @param {number} cols 每行列数（默认 3）
 * @returns {number} 新格下标
 */
export function moveCursor(cursor, key, cols = 3) {
  const n = Math.max(1, Math.floor(Number(cols)) || 3);
  const total = 9;
  const rows = Math.ceil(total / n);
  let c = Number.isInteger(cursor) ? cursor : 0;
  if (c < 0 || c >= total) c = 0;
  const row = Math.floor(c / n);
  const col = c % n;
  let r = row;
  let k = col;
  if (key === "up") r = Math.max(0, row - 1);
  else if (key === "down") r = Math.min(rows - 1, row + 1);
  else if (key === "left") k = Math.max(0, col - 1);
  else if (key === "right") k = Math.min(n - 1, col + 1);
  else return c;
  const next = r * n + k;
  return next >= 0 && next < total ? next : c;
}
