# -*- coding: utf-8 -*-
"""井字棋**纯逻辑**（不含任何硬件/线程/HTTP）。

★ 为什么要单独一层：井字棋的"算法正确"与"机器人搬运"是两件事。
  本模块只负责"棋盘上该怎么下"，**永远不碰运动通道** ——
  这样三档 AI 算法、胜负判定、格子映射都能被 pytest 直接穷举验证，
  不需要真机、不需要起服务（与 services/sorting.py 的 `build_piece_plan` 同一设计口径）。

★ 映射口径（与方案 docs/方案-井字棋对弈（机器人真实落子）.md §2 一致）：
  - 棋盘 = B 区 3×3，**行优先**编号：cell i ↔ B 槽位 `B(i+1)`（B1..B9）
  - 棋子来源：A1–A5 → 圆(O)，A6–A9 → 叉(X)
  - 先手执圆（由 api 层掷随机决定谁先手，本层不关心"谁是人"）
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 常量
O = "O"                      # 圆（来源 A1–A5）
X = "X"                      # 叉（来源 A6–A9）
SYMBOLS: Tuple[str, str] = (O, X)

# 棋子来源格（与方案 §2.1 严格对应：圆 5 个、叉 4 个）
O_SOURCES: Tuple[str, ...] = ("A1", "A2", "A3", "A4", "A5")
X_SOURCES: Tuple[str, ...] = ("A6", "A7", "A8", "A9")

# 8 条三连线（行/列/对角），下标与 cell 0..8 一致
WIN_LINES: Tuple[Tuple[int, int, int], ...] = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),        # 行
    (0, 3, 6), (1, 4, 7), (2, 5, 8),        # 列
    (0, 4, 8), (2, 4, 6),                   # 对角
)

DIFFICULTIES: Tuple[str, str, str] = ("easy", "normal", "hard")
_DIFF_CN = {"easy": "入门", "normal": "普通", "hard": "困难"}

CENTER = 4
CORNERS: Tuple[int, ...] = (0, 2, 6, 8)


def diff_cn(d: str) -> str:
    """难度英文 key → 中文名（界面/事件用）。"""
    return _DIFF_CN.get(str(d), "普通")


def norm_difficulty(d: str) -> str:
    """归一化难度（非法值一律回落到 normal，不抛）。"""
    d = str(d or "").strip().lower()
    if d in ("easy", "入门", "简单"):
        return "easy"
    if d in ("hard", "困难", "难"):
        return "hard"
    return "normal"


# ---------------------------------------------------------------- 棋盘
def new_board() -> List[Optional[str]]:
    """9 格空棋盘（行优先）。"""
    return [None] * 9


def other(sym: str) -> str:
    return X if sym == O else O


def legal(board: Sequence[Optional[str]], i: int) -> bool:
    """cell i 是否可落子（越界/已占 → False）。"""
    return isinstance(i, int) and 0 <= i < 9 and board[i] is None


def empties(board: Sequence[Optional[str]]) -> List[int]:
    return [i for i in range(9) if board[i] is None]


def winner(board: Sequence[Optional[str]]) -> Optional[Tuple[str, Tuple[int, int, int]]]:
    """返回 (符号, 三连线) 或 None。"""
    for line in WIN_LINES:
        a, b, c = line
        s = board[a]
        if s and s == board[b] == board[c]:
            return s, line
    return None


def is_full(board: Sequence[Optional[str]]) -> bool:
    return all(v is not None for v in board)


def is_draw(board: Sequence[Optional[str]]) -> bool:
    return winner(board) is None and is_full(board)


def turn_is_over(board: Sequence[Optional[str]]) -> bool:
    return winner(board) is not None or is_full(board)


# ---------------------------------------------------------------- 格子 ↔ 槽位
def cell_to_slot(i: int) -> str:
    """棋盘格 → B 槽位名（cell 0 → B1 … cell 8 → B9）。"""
    if not (0 <= int(i) <= 8):
        raise ValueError("棋盘格下标越界：%s" % i)
    return "B%d" % (int(i) + 1)


def slot_to_cell(name: str) -> Optional[int]:
    """B 槽位名 → 棋盘格（非 B1..B9 → None）。"""
    s = str(name or "").strip().upper()
    if len(s) < 2 or s[0] != "B":
        return None
    try:
        n = int(s[1:])
    except ValueError:
        return None
    return n - 1 if 1 <= n <= 9 else None


# ---------------------------------------------------------------- 来源格（物料）
def sources_for(sym: str) -> Tuple[str, ...]:
    """某符号的取料格（O → A1–A5；X → A6–A9）。"""
    return O_SOURCES if sym == O else X_SOURCES


def symbol_for_source(slot: str) -> Optional[str]:
    """A 格 → 它代表的符号（A1–A5=O、A6–A9=X；其它 → None）。"""
    s = str(slot or "").strip().upper()
    if s in O_SOURCES:
        return O
    if s in X_SOURCES:
        return X
    return None


def pick_source(sym: str, used: Sequence[str]) -> Optional[str]:
    """该符号下一个**未用过**的取料格；用尽 → None。"""
    u = {str(v).strip().upper() for v in (used or [])}
    for s in sources_for(sym):
        if s not in u:
            return s
    return None


# ---------------------------------------------------------------- AI 三档
def _winning_move(board: List[Optional[str]], sym: str) -> Optional[int]:
    """sym 立刻三连的落点（没有 → None）。"""
    for i in range(9):
        if board[i] is None:
            board[i] = sym
            won = winner(board) is not None
            board[i] = None
            if won:
                return i
    return None


def _minimax(board: List[Optional[str]], me: str, turn: str, depth: int = 0) -> int:
    """从 me 视角打分（+快赢、-慢输、0 平）。depth 让"快赢/慢输"更优。"""
    w = winner(board)
    if w:
        # 已分胜负：winner 是 turn 的**对手**（上一手刚下完）
        return (10 - depth) if w[0] == me else (depth - 10)
    if is_full(board):
        return 0
    scores: List[int] = []
    for i in range(9):
        if board[i] is None:
            board[i] = turn
            scores.append(_minimax(board, me, other(turn), depth + 1))
            board[i] = None
    return max(scores) if turn == me else min(scores)


def best_move(board: List[Optional[str]], me: str) -> int:
    """Minimax 最优着（困难档）—— **永不输**。"""
    best_i, best_s = None, -10 ** 9
    for i in range(9):
        if board[i] is None:
            board[i] = me
            s = _minimax(board, me, other(me), 1)
            board[i] = None
            if s > best_s:
                best_s, best_i = s, i
    return best_i if best_i is not None else -1


def _normal_move(board: List[Optional[str]], me: str,
                 rng: random.Random) -> int:
    """普通档启发式：能赢就赢 > 堵对方三连 > 中点 > 角 > 随机。"""
    m = _winning_move(board, me)
    if m is not None:
        return m
    m = _winning_move(board, other(me))
    if m is not None:
        return m
    if board[CENTER] is None:
        return CENTER
    free_corners = [c for c in CORNERS if board[c] is None]
    if free_corners:
        return rng.choice(free_corners)
    return rng.choice(empties(board))


def _easy_move(board: List[Optional[str]], me: str,
               rng: random.Random) -> int:
    """入门档：八成乱下、两成才拦（会输，符合"入门"定位）。"""
    if rng.random() < 0.2:
        m = _winning_move(board, me)
        if m is None:
            m = _winning_move(board, other(me))
        if m is not None:
            return m
    return rng.choice(empties(board))


def ai_move(board: Sequence[Optional[str]], me: str, difficulty: str = "normal",
            rng: Optional[random.Random] = None) -> int:
    """按难度选一个落点；棋盘已满/无路可走 → -1。

    ★ rng 可注入：测试用固定种子钉死行为（默认用全局 random）。
    """
    b = list(board)
    free = empties(b)
    if not free:
        return -1
    d = norm_difficulty(difficulty)
    r = rng or random
    if d == "hard":
        return best_move(b, me)
    if d == "easy":
        return _easy_move(b, me, r)
    return _normal_move(b, me, r)


# ---------------------------------------------------------------- 结果
def result_of(board: Sequence[Optional[str]], user_sym: str) -> Dict:
    """给界面用的终局结果。返回 {result, win_line}：
      result ∈ {"", "user_win", "robot_win", "draw"}；win_line 为三连下标或 []。
    """
    w = winner(board)
    if w:
        sym, line = w
        return {"result": "user_win" if sym == user_sym else "robot_win",
                "win_line": list(line)}
    if is_full(board):
        return {"result": "draw", "win_line": []}
    return {"result": "", "win_line": []}
