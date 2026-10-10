# -*- coding: utf-8 -*-
"""自然语言 → 结构化意图（**规则解析**，不依赖大模型）。

为什么先做规则解析（而不是直接上大模型）：
  · 现场指令高度集中在几类句式（第X行第Y列 / 红列第N个 / 放到 / 全部 / 开始分拣 / 急停），
    规则能覆盖绝大部分；**没有大模型 key、断网也能用**；
  · 大模型不确定 —— 它只适合"把话翻成 JSON"，绝不该直接生成运动指令。
    所以这里产出的 JSON 就是**唯一的接口契约**，大模型（二期）只需产出同构 JSON。

★ 本模块**只解析、不执行、不碰硬件** —— 纯函数，可单测。
  执行侧另走 api/ai.py 的安全闸（白名单/限位/围栏/速度/件数上限）。

现场口径（用户 2026-10-08 确认）：
  · "看颜色" = **去把件取到 C 区判色**（相机只对 C 区，照不到 A 区）；
  · "随机摆放" = **取件顺序随机**，放置仍**按颜色列**（红列/绿列/蓝列）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# 3×3 槽位编号是**行优先**：第1行第1列=A1 … 第2行第3列=A6 … 第3行第3列=A9
COLS = 3
ROWS = 3

# 三色列：第 1 列红 / 第 2 列绿 / 第 3 列蓝（与分拣口径一致）
COLOR_COLUMNS: Dict[str, int] = {"红": 1, "绿": 2, "蓝": 3}
COLOR_ORDER = ("红", "绿", "蓝")

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

INTENTS = ("look_color", "move_one", "move_all", "move_quota", "sorting", "estop",
           "goto_start", "inventory", "query_state")

# 不支持/需要拒绝的模糊词（速度只认右上角那一份）
VAGUE_SPEED = ("快一点", "快点", "慢一点", "慢点", "加速", "减速", "全速", "高速", "低速")


def _cn2int(s: str) -> Optional[int]:
    """'1' / '一' / '两' → int；解析不出返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    # 十/十一/十二… （本场景只到 9，但顺手支持两位数）
    if "十" in s:
        a, _, b = s.partition("十")
        hi = CN_NUM.get(a, 1) if a else 1
        lo = CN_NUM.get(b, 0) if b else 0
        return hi * 10 + lo
    return None


def slot_of(row: int, col: int, zone: str = "A") -> Optional[str]:
    """行列（1-based）→ 槽位名。越界返回 None。"""
    if not (1 <= row <= ROWS and 1 <= col <= COLS):
        return None
    z = (zone or "A").strip().upper()[:1]
    if z not in ("A", "B"):
        return None
    return "%s%d" % (z, (row - 1) * COLS + col)


def slot_pos(name: str) -> Optional[Tuple[int, int]]:
    """'A6' → (2,3)（行,列）；非法返回 None。"""
    m = re.fullmatch(r"([ABab])\s*([1-9])", (name or "").strip())
    if not m:
        return None
    idx = int(m.group(2))
    return ((idx - 1) // COLS + 1, (idx - 1) % COLS + 1)


def color_column_slots(color: str, zone: str = "B") -> List[str]:
    """某颜色列在指定区的三个槽位（按行序）：红→B1/B4/B7。"""
    c = COLOR_COLUMNS.get(color)
    if not c:
        return []
    z = (zone or "B").strip().upper()[:1]
    return [slot_of(r, c, z) for r in range(1, ROWS + 1)]


# ---------------------------------------------------------------- 槽位识别
_ZONE_RE = re.compile(r"([ABab])\s*区?")
_ROWCOL_RE = re.compile(r"第\s*([0-9一二两三四五六七八九十]+)\s*行\s*第?\s*([0-9一二两三四五六七八九十]+)\s*列")
_COLORCOL_RE = re.compile(r"(红|绿|蓝)\s*色?\s*列")
_COLORCOL_NTH_RE = re.compile(r"(红|绿|蓝)\s*色?\s*列[^\d一二两三四五六七八九十]{0,4}第?\s*([0-9一二两三四五六七八九十]+)\s*(?:个|块|格)")
_SLOT_RE = re.compile(r"([ABab])\s*([1-9])")
_PLAIN_COL_RE = re.compile(r"第\s*([0-9一二两三四五六七八九十]+)\s*列")

# ★★ 2026-10-09 新增（用户口径："**全部要学会解析**，如果没有检测到放回原位"）：
#   支持"按颜色配额"的说法，例如
#     "A区的3个红色，2个蓝色，1个绿色搬到B区"
#     "把A区3个红色搬到B区" / "拿两个绿色和1个蓝色到B区"
#   ⚠ 这类指令**不能**在解析阶段就承诺结果：A 区每件是什么颜色**事先未知**
#     （相机只对着 C 区），必须逐件取到 C 区看了才知道。
#     所以执行侧是"试到配额满足为止，不要的件**放回原位**"。
#     「3个红色」里的数字允许阿拉伯与中文。
_QUOTA_RE = re.compile(
    r"([0-9]+|[一二两三四五六七八九十]+)\s*(?:个|块|件|颗|枚)?\s*(红|绿|蓝)\s*色?")


# ---------------------------------------------------------------- 逐格约束（禁取 / 禁放）
# ★ 2026-10-10 用户口径："我有可能 B6 不能放，有可能 A4 不能拿，A5 不能放等等这种
#   无规则的要求，保证全部能解析"。所以下面按"任意格 / 任意个数 / 任意语序"来做：
#     A4不能拿 · A5不能放 · B6不能放 · B5、B9禁止摆放物体 · B5和B9不能放 · 不要拿A4
_SLOT_RE = re.compile(r"([ABab])\s*-?\s*([1-9])(?![0-9])")
_SLOT_BAD_RE = re.compile(r"([A-Za-z])\s*[-]?\s*([0-9]{1,2})")
_BAN_WORD_RE = re.compile(r"不能|不可以|不要|不许|不准|禁止|别")
_BAN_PUT_KW = re.compile(r"放|摆")
_BAN_TAKE_KW = re.compile(r"拿|取|动|碰")
_CHUNK_RE = re.compile(r"[。；;！!\n]+")
_PART_RE = re.compile(r"[，,、和与及]+")
# ★ "裸格小段"：整段只有格号/分隔符/空白（如 "B5"、"B5、B9"）。
#   只有这种才允许"继承后面那一小段的方向" —— 否则 "把A1放到B1" 也会被当成禁放段，
#   把正常的落料格 B1 误判成"禁止放"。
_BARE_SLOT_RE = re.compile(r"[\sABab0-9\-、,，和与及]*")


def _scan_bans(text: str):
    """扫出逐格约束 → ({"from":[...],"to":[...]}, 剥掉约束后的原文, [报错]).

    方向判定：段里有「放/摆」→ 禁放(to)；有「拿/取/动」→ 禁取(from)；
    只有"禁止/不能"没有动词 → **默认禁放**（最常见，且禁放比禁取更安全）。
    "先枚举后动词"（B5、B9禁止摆放物体）靠**裸格小段继承后面最近的方向**实现。
    """
    bans: Dict[str, list] = {"from": [], "to": []}
    bad: list = []
    keep_chunks = []
    for chunk in _CHUNK_RE.split(text or ""):
        parts = [p for p in _PART_RE.split(chunk) if p.strip()]
        if not parts:
            continue
        dirs = []
        for p in parts:
            if not _BAN_WORD_RE.search(p):
                dirs.append("")
            elif _BAN_PUT_KW.search(p):
                dirs.append("to")
            elif _BAN_TAKE_KW.search(p):
                dirs.append("from")
            else:
                dirs.append("to")            # 只说"禁止"→按禁放
        # 裸格小段继承**后面**最近一个有方向的小段（先枚举、后动词）
        for i, p in enumerate(parts):
            if dirs[i]:
                continue
            if not (_BARE_SLOT_RE.fullmatch(p) and _SLOT_RE.search(p)):
                continue          # ★ 只有"纯格号"段才继承；带动作的段不继承
            for j in range(i + 1, len(parts)):
                if dirs[j]:
                    dirs[i] = dirs[j]
                    break
        for p, d in zip(parts, dirs):
            if not d:
                keep_chunks.append(p)
                continue
            hit = 0
            for m in _SLOT_RE.finditer(p):
                s = m.group(1).upper() + m.group(2)
                if s not in bans[d]:
                    bans[d].append(s)
                hit += 1
            # ★ 说了"不能放 B0"这种非法格号 → 明说，不静默忽略
            if not hit:
                bm = _SLOT_BAD_RE.search(p)
                if bm:
                    bad.append("禁放/禁取的格号看不懂：「%s%s」——只支持 A1–A9、B1–B9"
                               % (bm.group(1).upper(), bm.group(2)))
    return bans, "，".join(keep_chunks), bad


def _parse_quotas(text: str) -> Dict[str, int]:
    """解析"3个红色，2个蓝色，1个绿色"这类**按颜色配额**的说法。

    @returns {颜色: 件数}；没解析到就返回 {}（调用方据此走普通搬运逻辑）。
    ★ 同一颜色说两次会**累加**（"2个红色…再加1个红色" → 3）。
    """
    out: Dict[str, int] = {}
    for m in _QUOTA_RE.finditer(text or ""):
        n = _cn2int(m.group(1))
        c = m.group(2)
        if n is None or n <= 0:
            continue
        out[c] = out.get(c, 0) + int(n)
    return out


def _find_zone(text: str, default: str = "A") -> str:
    m = _ZONE_RE.search(text)
    return m.group(1).upper() if m else default


def _parse_slot_phrase(text: str, default_zone: str = "A") -> Optional[Dict]:
    """从一段话里解出"哪个槽位/哪一组槽位"。

    返回 {"kind":"slot"|"color_col"|"column"|"all"|"unknown", "zone", "slot"/"color"/"col", "above":bool}
    """
    above = bool(re.search(r"正上方|上面|上空|上方", text))
    zone = _find_zone(text, default_zone)

    # 1) 显式槽位号：A1 / B 4
    m = _SLOT_RE.search(text)
    if m and not re.search(r"第\s*[0-9一二两三四五六七八九十]+\s*行", text):
        z, idx = m.group(1).upper(), int(m.group(2))
        s = slot_of((idx - 1) // COLS + 1, (idx - 1) % COLS + 1, z)
        if s:
            return {"kind": "slot", "zone": z, "slot": s, "above": above}

    # 2) 第X行第Y列
    m = _ROWCOL_RE.search(text)
    if m:
        r, c = _cn2int(m.group(1)), _cn2int(m.group(2))
        if r is not None and c is not None:
            s = slot_of(r, c, zone)
            if s:
                return {"kind": "slot", "zone": zone, "slot": s, "above": above,
                        "row": r, "col": c}
            return {"kind": "unknown", "zone": zone, "reason": "行列超出 3×3（%s行%s列）" % (r, c)}

    # 3) 红列第2个 / 红列
    m = _COLORCOL_NTH_RE.search(text)
    if m:
        color = m.group(1)
        n = _cn2int(m.group(2)) or 1
        slots = color_column_slots(color, zone)
        if 1 <= n <= len(slots):
            return {"kind": "slot", "zone": zone, "slot": slots[n - 1], "above": above,
                    "color": color, "nth": n}
        return {"kind": "unknown", "zone": zone,
                "reason": "%s列只有 %d 个位置，没有第 %d 个" % (color, len(slots), n)}
    m = _COLORCOL_RE.search(text)
    if m:
        return {"kind": "color_col", "zone": zone, "color": m.group(1), "above": above}

    # 4) 第N列（没写颜色）→ 按列号映射到颜色（1红/2绿/3蓝）
    m = _PLAIN_COL_RE.search(text)
    if m:
        c = _cn2int(m.group(1))
        if c and 1 <= c <= COLS:
            color = COLOR_ORDER[c - 1]
            return {"kind": "color_col", "zone": zone, "color": color, "col": c, "above": above}

    # 5) 全部
    if re.search(r"全部|所有|都|所有矩形|全拿|统统", text):
        return {"kind": "all", "zone": zone, "above": above}

    # ★ 只说了区、没说哪一格（如"回A区"/"放到B区"）→ 当作"放到该区"：
    #   执行侧按颜色列/按顺序落位。"B区的全部矩形随机摆放回A区"的目标就是整个 A 区，
    #   不能当"没看懂"拒掉。
    if re.search(r"区|矩形", text):
        return {"kind": "zone", "zone": zone, "above": above}

    return {"kind": "unknown", "zone": zone, "reason": "没看懂是哪一格"}


# ---------------------------------------------------------------- 主解析
def parse(text: str) -> Dict:
    """原文 → 意图。

    返回：
      {"ok": bool, "intent": str, "plan": {...}, "warnings": [str],
       "raw": str, "error": str}
      intent ∈ INTENTS；ok=False 时 error 说明为什么没听懂（**绝不猜**）。
    """
    raw = (text or "").strip()
    out = {"ok": False, "intent": "", "plan": {}, "warnings": [], "raw": raw, "error": ""}
    if not raw:
        out["error"] = "没有收到指令"
        return out

    # ---- ★ 逐格约束（禁取/禁放）**最先扫**，并从原文剥掉 ----
    #   为什么最先：约束短语里的格号（"B6不能放"）会被后面的"放到哪里"解析抢走，
    #   把目标解析歪（把 B6 当成了落料格）。先剥掉，下游就干净了。
    bans, cleaned, ban_bad = _scan_bans(raw)
    if ban_bad:
        out["error"] = ban_bad[0]
        return out
    if bans["from"] or bans["to"]:
        raw = cleaned or raw
    bk = {"ban": bans} if (bans["from"] or bans["to"]) else {}
    if bk:
        # ★ 约束必须出现在回显里 —— 用户要能一眼看到"系统真听懂了哪个格不能动/不能放"
        _bits = []
        if bans["from"]:
            _bits.append("不能取料：" + "、".join(bans["from"]))
        if bans["to"]:
            _bits.append("不能放料：" + "、".join(bans["to"]))
        out["warnings"].append("已记下逐格约束 —— " + "；".join(_bits) +
                               "（仅在**本次指令内**有效）")

    # ---- 模糊速度词：明确拒绝，而不是"帮忙猜一个速度" ----
    for w in VAGUE_SPEED:
        if w in raw:
            out["error"] = ("不支持「%s」这类模糊速度 —— 速度只有右上角那一份，"
                            "请在页面右上角调，指令里不用带速度" % w)
            return out

    # ---- 急停（最高优先级，任何其它词都不影响）----
    # ★ 口径（安全）：**必须明确说出"急停"** 才走急停。
    #   "停下/停止/别动" 一律走**软停**（停止执行：当前动作结束后停 + 回起点），
    #   因为急停会断伺服、手臂可能因重力下沉 —— 把客气的"停下"当急停是危险的。
    #   回显时必须把"我将【停止执行】（不是急停）"写清楚，让操作员自己再决定。
    if re.search(r"急停|紧急停止|紧急制动|立刻急停|马上急停", raw):
        out.update(ok=True, intent="estop", plan={"action": "estop"})
        return out

    # ---- 回开始/结束点（★ 2026-10-10 用户口径："让它回到起点"）----
    #   此前**没有这个动作**，用户说「回到起点」时规则层听不懂 → 落到大模型闲聊 →
    #   模型只能说"已回到起点位置"（它没有任何执行权，纯属编造）。
    #   现在：规则层直接命中 → 出计划卡 → 说「执行」才动（与其它动作同一条确认链路）。
    #   ★ 它含运动，所以绝不直接执行；也绝不与「停止/急停」混为一谈。
    if re.search(r"回(到)?(起点|开始点|结束点|开始/结束点|原点|零位)|回零"
                 r"|返回(起点|原点)|去起点", raw):
        out.update(ok=True, intent="goto_start", plan={"action": "goto_start"})
        return out

    # ---- 盘点（★ 2026-10-10 用户口径："A区有几个盒子、什么颜色、几个红蓝、几个无效颜色"）----
    #   ★ 它是**运动**：逐件取到 C 位判色再**原格放回**（不搬走、不改变 A 区）——
    #     因为相机只对着 C 检测位，照不到 A 区，A 区颜色**只能靠逐件拍照**才知道。
    if re.search(r"盘点|清点|数一遍|数一下|点一遍|盘一遍|清一遍|点个数|数个数", raw):
        out.update(ok=True, intent="inventory", plan={"action": "inventory"})
        out["warnings"].append("盘点 = 逐件取到 C 位判色、**原格放回**（不搬运、不改变 A 区）；"
                              "每件走一趟，属运动，要确认才动。")
        return out

    # ---- 状态查询（★ **只读**：查已有记录，不动机器人、不弹计划卡）----
    #   ★ 硬事实：A 区的颜色**没有直接观测手段**，只能答"我记录过什么"；
    #     没盘过就老实说不知道（绝不编数量）—— 见 ai.inv_answer。
    _q_words = (r"几个|多少|有几|哪些颜色|剩几|还剩|有没有|空了|满了|几个无效|无效几个"
                r"|放了什么|放的是什么|有什么|有哪些")
    _q_has_slot = re.search(r"[ABab]\s*[1-9]|第[一二三四五六七八九1-9]+行|第[一二三四五六七八九1-9]+列",
                            raw)
    _q_is_move = re.search(r"放到|搬到|放在|摆到|摆放到|挪到|移到", raw)
    # ★ "看看/看一下/看一眼" 是**祈使**（去拍照看），不是查询记录 → 不进只读分支
    _q_is_imperative = re.search(r"看看|看一下|看一眼|去看看|瞅一眼", raw)
    if not _q_is_move and not _q_is_imperative and (re.search(_q_words, raw)
                           or (re.search(r"什么颜色", raw) and not _q_has_slot)):
        out.update(ok=True, intent="query_state", plan={"action": "query_state"})
        return out

    # ---- 分拣流程控制 ----
    if re.search(r"停止|停下|停住|别动|中断|取消", raw) and not re.search(r"放到|搬到|放在|摆放", raw):
        out.update(ok=True, intent="sorting", plan={"action": "stop"})
        return out
    if re.search(r"暂停|先停一下", raw):
        out.update(ok=True, intent="sorting", plan={"action": "pause"})
        return out
    if re.search(r"继续|接着|恢复", raw):
        out.update(ok=True, intent="sorting", plan={"action": "resume"})
        return out
    if re.search(r"开始分拣|执行分拣|分拣一下|开始拣|跑分拣", raw):
        out.update(ok=True, intent="sorting", plan={"action": "start"})
        return out

    # ---- 看颜色 ----
    if re.search(r"什么颜色|是啥颜色|啥颜色|颜色是|看看颜色|看一眼颜色|检测颜色", raw):
        src = _parse_slot_phrase(re.sub(r"什么颜色|是啥颜色|啥颜色|颜色是|看看颜色|看一眼颜色|检测颜色", "", raw))
        if src["kind"] == "slot":
            out.update(ok=True, intent="look_color",
                       plan={"action": "look_color", "from": src})
            out["warnings"].append("相机只对着 C 区：会先取件送到 C 区判色，判完可放回原格（这是运动）")
            return out
        if src["kind"] == "all":
            out["error"] = "看颜色一次只能看一格（要逐件送到相机），请指定具体哪一格"
            return out
        out["error"] = src.get("reason", "没看懂要看哪一格的颜色")
        return out

    # ---- 搬运（单件 / 批量 / 按颜色配额）----
    _SEP = r"放到|搬到|放在|摆到|摆放到|摆放回|摆回|挪到|移到|放回|取回|扔到|丢到"
    if re.search(_SEP, raw):
        # 用连接词把"源"和"目标"切开
        parts = re.split(_SEP, raw, maxsplit=1)
        src_txt, dst_txt = (parts + [""])[0], (parts + [""])[1]

        # ★★ 2026-10-09 优先识别"**按颜色配额**"（用户口径："全部要学会解析"）：
        #   "A区的3个红色，2个蓝色，1个绿色搬到B区" / "把A区3个红色搬到B区"
        #   ⚠ 必须在普通搬运之前判：否则"soc 3个红色"会被当成"槽位解析失败"。
        #   ⚠ 这是**有条件的**任务 —— A 区颜色事先未知，执行时逐件判色、
        #      不要的件放回原位（所以不带 shuffle，语义务实）。
        quotas = _parse_quotas(src_txt or raw)
        if quotas:
            dst_q = _parse_slot_phrase(dst_txt, default_zone="B")
            if dst_q["kind"] == "unknown":
                out["error"] = "没看懂要放到哪里：" + dst_q.get("reason", "")
                return out
            if dst_q["kind"] == "slot":
                out["error"] = ("按颜色配额搬运要放到**颜色列**（例如「搬到B区」或「搬到B区红列」）——"
                                "件是什么颜色要判色后才知道，没法指定某一个具体格")
                return out
            out.update(ok=True, intent="move_quota",
                       plan={"action": "move_quota",
                             "from": {"kind": "all", "zone": "A"},
                             "to": dst_q, "quotas": quotas, **bk})
            out["warnings"].append(
                "按颜色配额取件：A 区颜色**事先未知**，会逐件送到 C 区判色；"
                "不需要的件**放回原位**。若 A 区该颜色不够，会试到没有可取件为止。")
            out["warnings"].append(
                "合计 %d 件；每件都要走一趟「取→判色→放」" % sum(quotas.values()))
            return out

        src = _parse_slot_phrase(src_txt, default_zone="A")
        dst = _parse_slot_phrase(dst_txt, default_zone="B")
        if src["kind"] == "unknown":
            out["error"] = "没看懂要从哪里取：" + src.get("reason", "")
            return out
        if dst["kind"] == "unknown":
            out["error"] = "没看懂要放到哪里：" + dst.get("reason", "")
            return out
        # ★ "随机摆放" = 取件顺序随机（用户口径）；放置仍按颜色列
        shuffle = bool(re.search(r"随机|随便|任意", raw))
        if src["kind"] == "all":
            out.update(ok=True, intent="move_all",
                       plan={"action": "move_all", "from": src, "to": dst,
                             "shuffle": shuffle, **bk})
            out["warnings"].append("批量搬运：会逐件执行，超过安全件数上限需再确认一次")
            return out
        if src["kind"] in ("slot", "color_col"):
            out.update(ok=True, intent="move_one",
                       plan={"action": "move_one", "from": src, "to": dst,
                             "shuffle": shuffle, **bk})
            return out
        out["error"] = "没看懂要从哪里取：" + src.get("reason", "")
        return out

    # ---- 只说了"取/拿"没说放哪 ----
    if re.search(r"取|拿|夹", raw):
        out["error"] = "只说了取，没说放到哪里 —— 请补上目标（例如：放到B区红列）"
        return out

    out["error"] = "没听懂这条指令（支持的：看颜色 / 搬运到某格或某颜色列 / 全部搬运 / 开始·停止·暂停·继续分拣 / 急停）"
    return out


def slot_to_zh(slot: str) -> str:
    """'A6' → 'A区第2行第3列'（回显给人看）。"""
    pos = slot_pos(slot)
    if not pos:
        return slot
    return "%s区第%d行第%d列" % (slot[:1], pos[0], pos[1])
