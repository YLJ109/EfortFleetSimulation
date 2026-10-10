# -*- coding: utf-8 -*-
"""摆放**约束求解**（纯逻辑、不碰硬件 → 可单测）。

★ 为什么要有它（2026-10-10 用户口径）：
  「从A区拿三个蓝，两个绿，一个红放到B区，**按同颜色一行或者一列摆放**」
  以前"颜色→列"是**写死**的（services/sorting.py 的 DEFAULT_COLUMN_SLOTS：
  红→B1/B4/B7、绿→B2/B5/B8、蓝→B3/B6/B9），用户说了"同色成行/成列"也**没有落点**；
  而且同一约束下**有很多种摆法**（用户原话："还有好多种摆放方式"）——
  多解必须由**确定性规则**选一个、并**回显**，不能由大模型瞎编。

设计边界：
  · 只解 3×3（B 区 9 格）；"线"只有 3 行 + 3 列 = 6 条。
  · `occupied`（已有料）与 `banned`（禁止放）**一视同仁**：都不可落料。
  · 解不出**显式失败**并说清卡在哪，绝不硬来。
  · `pattern="any"` = **保持现状**（按既定颜色列），只有指令里明确说了摆放方式才走约束求解。
"""
from __future__ import annotations

from itertools import combinations
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

COLS = 3                       # B 区列数（3×3）
ROWS = 3
ZONE_SLOTS: List[str] = ["B%d" % i for i in range(1, 10)]

# 摆放方式（规则/大模型解析出来的枚举值）
PATTERNS = ("row_or_col", "row", "col", "adjacent_row", "adjacent_col", "adjacent", "any")
PATTERN_ZH = {
    "row_or_col": "同行或同列",
    "row": "同一行",
    "col": "同一列",
    # ★ 2026-10-10 用户口径（"相邻"）：同色落在**连续相邻**的一行/一列 ——
    #   ① 不能隔空（中间不能跳格）；② 中间不能有盒子（已有料/禁放格隔断）。
    "adjacent_row": "同一行且相邻（不隔空、中间不能有盒子）",
    "adjacent_col": "同一列且相邻（不隔空、中间不能有盒子）",
    "adjacent": "同一行或一列且相邻（不隔空、中间不能有盒子）",
    "any": "按颜色列（默认）",
}
ADJACENT = ("adjacent_row", "adjacent_col", "adjacent")


def _idx(slot: str) -> int:
    return int(str(slot).strip().upper()[1:]) - 1


def _row(slot: str) -> int:
    return _idx(slot) // COLS


def _col(slot: str) -> int:
    return _idx(slot) % COLS


def lines(pattern: str = "row_or_col") -> List[Tuple[str, List[str]]]:
    """允许的"线"：同色只能落在**同一条线**上。返回 [(线名, [格...]), ...]。

    ★ 顺序即优先级（确定性）：`row_or_col` **列优先** —— 与现场"竖着排更像一列"的直觉一致，
      也让"同一句话每次跑出同一个摆法"可回归。
    """
    rows = [("第%d行" % (r + 1), [ZONE_SLOTS[r * COLS + c] for c in range(COLS)])
            for r in range(ROWS)]
    cols = [("第%d列" % (c + 1), [ZONE_SLOTS[r * COLS + c] for r in range(ROWS)])
            for c in range(COLS)]
    if pattern in ("row", "adjacent_row"):
        return rows
    if pattern in ("col", "adjacent_col"):
        return cols
    return cols + rows


def _any_columns() -> Dict[str, Sequence[str]]:
    """`pattern="any"` 用的**既定颜色列**（与分拣页同一份，不复制第二份）。"""
    from .sorting import DEFAULT_COLUMN_SLOTS      # 延迟导入，避免模块环
    return DEFAULT_COLUMN_SLOTS


def _line_name(slots: Sequence[str]) -> str:
    ss = list(slots)
    if len(ss) >= 3:
        return "第%d列" % (_col(ss[0]) + 1) if len({_col(s) for s in ss}) == 1 else "第%d行" % (_row(ss[0]) + 1)
    if len({_row(s) for s in ss}) == 1:
        return "第%d行" % (_row(ss[0]) + 1)
    if len({_col(s) for s in ss}) == 1:
        return "第%d列" % (_col(ss[0]) + 1)
    return "任意"


def describe(plan: Dict[str, List[str]]) -> str:
    """人话摆法：`蓝→B1·B4·B7（第1列）｜绿→B2·B3（第1行）｜红→B6`。"""
    parts = []
    for color, slots in (plan or {}).items():
        cells = "·".join(slots)
        parts.append("%s→%s（%s）" % (color, cells, _line_name(slots)))
    return "｜".join(parts)


def solve(counts: Dict[str, int], pattern: str = "any",
          occupied: Iterable[str] = (), banned: Iterable[str] = (),
          max_alts: int = 3) -> Dict:
    """把"各色几件 + 摆放约束"解成**具体格位**。

    @param counts   颜色→件数（>0 才参与）
    @param pattern  row_or_col / row / col / any
    @param occupied 已知**已有料**的格（不可落）
    @param banned   明确**禁止放**的格（不可落）
    @returns {ok, plan:{color:[slot]}, note, alts:[note…], reason}
    """
    cnt = {c: int(n) for c, n in (counts or {}).items() if int(n) > 0}
    blocked = {str(s).strip().upper() for s in list(occupied or ()) + list(banned or ())}
    if not cnt:
        return {"ok": False, "plan": {}, "note": "", "alts": [], "reason": "没有要摆的件"}
    if pattern not in PATTERNS:
        pattern = "any"
    if pattern == "any":
        r = _solve_any(cnt, blocked)
    else:
        r = _solve_lines(cnt, pattern, blocked, max_alts)
    r["pattern"] = pattern
    r["pattern_zh"] = PATTERN_ZH.get(pattern, pattern)
    if r.get("ok"):
        r["note"] = describe(r["plan"])
    return r


def _solve_any(cnt: Dict[str, int], blocked: set) -> Dict:
    """保持现状：每色按**既定颜色列**依次落格（跳过已有料/禁放格）。

    ★ 与分拣页口径一致；列不够 → 明确失败（不偷偷换到别的颜色列）。
    """
    cmap = _any_columns()
    plan: Dict[str, List[str]] = {}
    used = set(blocked)
    for color, n in cnt.items():
        cells = [s for s in (cmap.get(color) or []) if s not in used]
        if len(cells) < n:
            why = ("「%s」列不够用：该列可用 %d 格，要放 %d 件%s"
                   % (color, len(cells), n,
                      "（被已有料/禁放格占掉了）" if any(s in blocked for s in (cmap.get(color) or [])) else ""))
            return {"ok": False, "plan": {}, "alts": [], "reason": why}
        pick = cells[:n]
        plan[color] = pick
        used.update(pick)
    return {"ok": True, "plan": plan, "alts": [], "reason": ""}


def _solve_lines(cnt: Dict[str, int], pattern: str, blocked: set, max_alts: int) -> Dict:
    """约束求解：同色**全部**落在同一条线（行或列）上。穷举 + 确定性优先。

    ★ 相邻模式（`adjacent_*`）：同色必须落在**连续相邻**格上 —— 中间不能隔空、
      也不能被已有料/禁放格隔断。实现 = 在线上滑一个长度 n 的窗口，整窗都空闲才算。
    """
    lines_ok = lines(pattern)
    adjacent = pattern in ADJACENT
    colors = sorted(cnt, key=lambda c: (-cnt[c], c))      # 件数多的先占（可行性关键）
    used: set = set(blocked)
    cur: Dict[str, List[str]] = {}
    line_of: Dict[str, str] = {}
    sols: List[Tuple[Dict[str, List[str]], Dict[str, str]]] = []

    def pool(color: str, n: int) -> List[Tuple[str, List[str]]]:
        out: List[Tuple[str, List[str]]] = []
        if n == 1:
            # 单件：任意可用格都算"落在一条线上"；按线序去重（保证确定性）
            seen = set()
            for name, cells in lines_ok:
                for s in cells:
                    if s in used or s in seen:
                        continue
                    seen.add(s)
                    out.append((name, [s]))
            return out
        for name, cells in lines_ok:
            if adjacent:
                # ★ 连续相邻：滑窗，整窗必须空闲（不隔空、中间无盒子）
                for i in range(0, len(cells) - n + 1):
                    win = cells[i:i + n]
                    if all(s not in used for s in win):
                        out.append((name, list(win)))
            else:
                free = [s for s in cells if s not in used]
                if len(free) < n:
                    continue
                for pick in combinations(free, n):
                    out.append((name, list(pick)))
        return out

    def bt(i: int) -> None:
        if len(sols) > max_alts:            # 只需要前几个解（给人看"还有别的摆法"）
            return
        if i == len(colors):
            sols.append(({k: list(v) for k, v in cur.items()}, dict(line_of)))
            return
        c = colors[i]
        for name, pick in pool(c, cnt[c]):
            if any(s in used for s in pick):
                continue
            cur[c] = pick
            line_of[c] = name
            used.update(pick)
            bt(i + 1)
            for s in pick:
                used.discard(s)
            cur.pop(c, None)
            line_of.pop(c, None)

    bt(0)
    if not sols:
        return {"ok": False, "plan": {}, "alts": [],
                "reason": _why_no_solution(cnt, pattern, blocked)}
    best, _ = sols[0]
    alts = [describe(s[0]) for s in sols[1:max_alts + 1]]
    return {"ok": True, "plan": best, "alts": alts, "reason": ""}


def _fits(cells: Sequence[str], n: int, blocked: set, adjacent: bool) -> bool:
    """这条线上能不能放下 n 件（相邻模式要求**连续整窗**空闲）。"""
    if adjacent:
        return any(all(s not in blocked for s in cells[i:i + n])
                   for i in range(0, len(cells) - n + 1))
    return len([s for s in cells if s not in blocked]) >= n


def _max_run(cells: Sequence[str], blocked: set, adjacent: bool) -> int:
    """线上最多能放几件：相邻模式 = 最长连续空闲段；非相邻 = 空闲格数。"""
    if not adjacent:
        return len([s for s in cells if s not in blocked])
    best = run = 0
    for s in cells:
        run = 0 if s in blocked else run + 1
        best = max(best, run)
    return best


def _why_no_solution(cnt: Dict[str, int], pattern: str, blocked: set) -> str:
    """解不出时**说清卡在哪**（现场才知道该改约束还是加盘）。"""
    lines_ok = lines(pattern)
    adjacent = pattern in ADJACENT
    need_big = max(cnt.values())
    zh = PATTERN_ZH.get(pattern, pattern)
    detail = []
    for color, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        ok_lines = sum(1 for _, cells in lines_ok if _fits(cells, n, blocked, adjacent))
        detail.append("%s 要 %d 件（放得下的线 %d 条）" % (color, n, ok_lines))
    extra = ""
    if adjacent and blocked:
        # ★ 用户口径："中间有盒子 → 逻辑不通" —— 直接把它点出来，别只说"摆不下"
        extra = ("「相邻」要求中间不能隔空、也不能有盒子；当前被占/禁放：%s。"
                 % "、".join(sorted(blocked)))
    return ("在「%s」约束下摆不下：%s。可用格 %s；已有料/禁放 %s。%s"
            "（最多的一色要 %d 件，而一条线只有 %d 格）"
            % (zh, "；".join(detail),
               "、".join(sorted(set(ZONE_SLOTS) - blocked)) or "无",
               "、".join(sorted(blocked)) or "无", extra, need_big, COLS))


def suggest(counts: Dict[str, int], pattern: str = "row_or_col",
            occupied: Iterable[str] = (), banned: Iterable[str] = (),
            limit: int = 4) -> List[str]:
    """给出"**怎么才行**"的建议（确定性穷举，不是大模型随口说）。

    ★ 用户口径（2026-10-10）："多种摆放规则跑一遍，如果无法实现…跟用户沟通或者给一些很好的建议"。
    顺序：① 换个摆放方式能不能成 → 列出来；② 都不行 → 指出"某色最多能放几件"（建议减件）；
          ③ 再指出是哪些格挡住了（被占/禁放）。
    """
    cnt = {c: int(n) for c, n in (counts or {}).items() if int(n) > 0}
    blocked = {str(s).strip().upper() for s in list(occupied or ()) + list(banned or ())}
    if not cnt:
        return []
    tips: List[str] = []
    for p in PATTERNS:
        if p in (pattern, "any"):
            continue
        r = solve(cnt, p, blocked, ())
        if r.get("ok"):
            tips.append("改按「%s」可以：%s" % (PATTERN_ZH.get(p, p), r.get("note")))
    if tips:
        return tips[:limit]
    adjacent = pattern in ADJACENT
    for color, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        cap = max([_max_run(cells, blocked, adjacent) for _, cells in lines(pattern)] or [0])
        if cap < n:
            if cap <= 0:
                tips.append("「%s」在这种约束下**一格都放不下**（目标区被已占/禁放占满）" % color)
            else:
                tips.append("「%s」最多只能放 %d 件（现在要 %d 件）：建议减少到 %d 件"
                            % (color, cap, n, cap))
    if blocked:
        tips.append("被占/禁放的格：%s —— 先清掉它们，或换一条线" % "、".join(sorted(blocked)))
    return tips[:limit]


def color_lines(pattern: str = "row_or_col") -> List[str]:
    """该约束下**合法的线名**（界面/回显用）。"""
    return [name for name, _ in lines(pattern)]


def alt_hint(alts: Optional[List[str]]) -> str:
    """把"其它可行摆法"拼成一句可展示的话。"""
    alts = alts or []
    if not alts:
        return ""
    return "其它可行摆法：" + " ／ ".join(alts)
