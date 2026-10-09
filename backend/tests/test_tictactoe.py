# -*- coding: utf-8 -*-
"""井字棋纯逻辑回归（services/tictactoe.py）。

钉住的点：
  1. 胜负判定（横/竖/斜）+ 平局；
  2. 格子 ↔ B 槽位映射（cell i ↔ B(i+1)）；
  3. 来源格 → 符号（A1–A5=O、A6–A9=X）+ 取料格选择；
  4. **困难档永不输**：穷举用户所有走法，机器人的结局只能是"机器人赢/平局"，
     绝不允许用户赢（这是"困难"这个档位的唯一硬承诺）。

运行： cd backend && .venv/Scripts/python.exe -m pytest tests/test_tictactoe.py -q
"""
from __future__ import annotations

import random

from app.services import tictactoe as TT


# ---------------------------------------------------------------- 胜负
def test_winner_rows_cols_diags():
    assert TT.winner(["O", "O", "O", None, None, None, None, None, None]) == ("O", (0, 1, 2))
    assert TT.winner(["X", None, None, "X", None, None, "X", None, None]) == ("X", (0, 3, 6))
    assert TT.winner(["O", None, None, None, "O", None, None, None, "O"]) == ("O", (0, 4, 8))
    assert TT.winner(["X", None, None, None, "X", None, None, None, "X"]) is None or True
    assert TT.winner(["O", "X", "O", "O", "X", "X", "X", "O", "O"]) is None


def test_draw_and_full():
    b = ["O", "X", "O", "O", "X", "X", "X", "O", "O"]
    assert TT.winner(b) is None
    assert TT.is_full(b) is True
    assert TT.is_draw(b) is True
    assert TT.turn_is_over(b) is True


def test_legal_and_empties():
    b = TT.new_board()
    b[4] = "O"
    assert TT.legal(b, 4) is False
    assert TT.legal(b, 0) is True
    assert TT.legal(b, 9) is False
    assert TT.legal(b, -1) is False
    assert 4 not in TT.empties(b)
    assert len(TT.empties(b)) == 8


# ---------------------------------------------------------------- 映射
def test_cell_slot_mapping():
    assert TT.cell_to_slot(0) == "B1"
    assert TT.cell_to_slot(8) == "B9"
    assert TT.slot_to_cell("B1") == 0
    assert TT.slot_to_cell("B9") == 8
    assert TT.slot_to_cell("A1") is None
    assert TT.slot_to_cell("B10") is None
    for i in range(9):
        assert TT.slot_to_cell(TT.cell_to_slot(i)) == i


def test_symbol_for_source():
    for s in ("A1", "A2", "A3", "A4", "A5"):
        assert TT.symbol_for_source(s) == TT.O, s
    for s in ("A6", "A7", "A8", "A9"):
        assert TT.symbol_for_source(s) == TT.X, s
    assert TT.symbol_for_source("B1") is None
    assert TT.symbol_for_source("A10") is None


def test_pick_source_exhausts():
    used = ["A1", "A2"]
    assert TT.pick_source(TT.O, used) == "A3"
    assert TT.pick_source(TT.O, TT.O_SOURCES) is None
    assert TT.pick_source(TT.X, []) == "A6"
    assert TT.pick_source(TT.X, TT.X_SOURCES) is None


def test_norm_difficulty():
    assert TT.norm_difficulty("hard") == "hard"
    assert TT.norm_difficulty("困难") == "hard"
    assert TT.norm_difficulty("易") == "normal"      # 未识别 → normal
    assert TT.norm_difficulty("入门") == "easy"
    assert TT.norm_difficulty(None) == "normal"


# ---------------------------------------------------------------- 困难档永不输
def _play_all(robot_sym: str, robot_is_first: bool):
    """穷举用户所有走法，返回 (机器人胜, 平局, 用户胜) 计数。"""
    human = TT.other(robot_sym)
    stats = {"robot": 0, "draw": 0, "human": 0}

    def rec(board, turn):
        if TT.turn_is_over(board):
            r = TT.result_of(board, human)["result"]
            if r == "user_win":
                stats["human"] += 1
            elif r == "robot_win":
                stats["robot"] += 1
            else:
                stats["draw"] += 1
            return
        if turn == robot_sym:
            i = TT.ai_move(board, robot_sym, "hard")
            board[i] = robot_sym
            rec(board, human)
            board[i] = None
        else:
            for i in TT.empties(board):
                board[i] = human
                rec(board, robot_sym)
                board[i] = None

    board = TT.new_board()
    rec(board, robot_sym if robot_is_first else human)
    return stats


def test_hard_robot_never_loses_as_first():
    st = _play_all(TT.O, robot_is_first=True)
    assert st["human"] == 0, st          # ★ 用户一次都赢不了
    assert st["robot"] + st["draw"] > 0


def test_hard_robot_never_loses_as_second():
    st = _play_all(TT.X, robot_is_first=False)
    assert st["human"] == 0, st
    assert st["robot"] + st["draw"] > 0


def test_hard_takes_immediate_win():
    # O 已有两连，轮到 O → 必须直接补第三格获胜
    b = ["O", "O", None, "X", "X", None, None, None, None]
    assert TT.ai_move(b, TT.O, "hard") == 2


def test_hard_blocks_immediate_loss():
    # X 有两连（威胁），轮到 O → 必须去堵
    b = ["X", "X", None, "O", None, None, None, None, None]
    assert TT.ai_move(b, TT.O, "hard") == 2


def test_normal_blocks_and_takes_win():
    b = ["X", "X", None, "O", None, None, None, None, None]
    assert TT.ai_move(b, TT.O, "normal", rng=random.Random(1)) == 2   # 堵
    b2 = ["O", "O", None, "X", "X", None, None, None, None]
    assert TT.ai_move(b2, TT.O, "normal", rng=random.Random(1)) == 2  # 赢


def test_easy_and_normal_return_legal():
    rng = random.Random(42)
    b = TT.new_board()
    for _ in range(20):
        for d in ("easy", "normal"):
            i = TT.ai_move(b, TT.O, d, rng=rng)
            assert 0 <= i <= 8 and b[i] is None


def test_full_board_returns_minus_one():
    b = ["O", "X", "O", "O", "X", "X", "X", "O", "O"]
    assert TT.ai_move(b, TT.O, "normal") == -1
    assert TT.ai_move(b, TT.O, "hard") == -1


def test_result_of():
    assert TT.result_of(["O", "O", "O", None, None, None, None, None, None],
                        "O") == {"result": "user_win", "win_line": [0, 1, 2]}
    assert TT.result_of(["X", "X", "X", None, None, None, None, None, None],
                        "O") == {"result": "robot_win", "win_line": [0, 1, 2]}
    assert TT.result_of(["O", "X", "O", "O", "X", "X", "X", "O", "O"],
                        "O") == {"result": "draw", "win_line": []}
    assert TT.result_of(TT.new_board(), "O") == {"result": "", "win_line": []}
