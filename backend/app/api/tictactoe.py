# -*- coding: utf-8 -*-
"""井字棋对弈（机器人真实落子）—— 接口 + 执行线程。

玩法与映射见 docs/方案-井字棋对弈（机器人真实落子）.md：
  · 棋盘 = B 区 3×3（cell i ↔ B(i+1)）
  · 棋子来源：A1–A5 = 圆(O)、A6–A9 = 叉(X)
  · 先手执圆；开局随机决定谁先手
  · 用户键盘/鼠标落子 → 机器人真机搬运 → AI 按难度应对 → 循环到终局

★ 复用而非新造：运动/吸放一律走 `api/sorting.py` 的同一套原语（`_move`/`_air`），
  暂停/取消走 `api/control.py` 的 `RunContext`（`/control/run-pause|run-resume|run-state`）。
  本模块**不新增任何下发通道** —— 与分拣/执行共享同一个 40135 触发位与执行互斥。

★ 一条长线程跑完整局：`/start` 起线程 → 线程在"轮到用户"时阻塞等 `Queue`
  （每 0.2s 检查取消/暂停），`/move` 把一个 cell 投进队列。这样暂停/停止/进度
  天然覆盖全局（与分拣同一套 RunContext 语义），不用为"每一手"各起一个 run。
"""
from __future__ import annotations

import copy
import queue
import random
import threading
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_control
from app.api.control import (_is_paused, _reject_if_run_active, new_run,
                             reject_if_not_ready, request_cancel)
from app.api.sorting import (_Stop, _air, _capture, _move,  # 复用同一套原语
                             _dual_drop, _move_flip_j6, DUAL_FACE_FLIP_POINT,
                             _put_back, _set as _set_state, vision_preflight)
from app.core.logger import get_logger
from app.db.crud import list_points
from app.db.database import SessionLocal
from app.services import sorting as S
from app.services import tictactoe as TT
from app.services.events import emit as emit_event
from app.services.motion import real_write_enabled

log = get_logger("tictactoe")
router = APIRouter(prefix="/api/tictactoe", tags=["tictactoe"])

# 同时只允许一局（与分拣/执行共用一个"在跑"的世界观）
_gate = threading.Lock()
_job: Dict[str, object] = {"thread": None, "ctx": None, "state": None,
                           "q": None, "started_at": 0.0}

# ★ 速度：**沿用右上角那份全局速度**（与分拣同口径，2026-10-10 用户纠正：
#   "右上角多少，机器人速度多少"）。前端把 `exec.speed` 经 `speed_pct` 传进来；
#   下面这个只是**客户端没带时**的兜底默认（5% 安全档）。
DEFAULT_SPEED_PCT = 5
# ★★ 2026-10-11：时序常量**取工艺时序的唯一来源** `services/sorting.py`，
#   不在本模块再抄一份（原来 AI / 井字棋 / 分拣三处各写 500，改一处会漏两处）。
SUCK_HOLD_MS = S.SUCK_HOLD_MS   # 吸牢再抬（0.5s）
BLOW_MS = S.BLOW_MS             # 吹气/放气时长（0.5s）
# ★ 拍照自证（2026-10-10 用户口径："要两种模式，一个是拍照自证，一个是默认的"）：
#   默认模式**不判色、不去 C 位**（符号本来就由格位决定，A1–A5=圆 / A6–A9=叉）；
#   自证模式取件抬起后先去 C 检测位拍照，颜色必须等于"该格应有的颜色"。
VERIFY_FRAMES = 3       # 送检拍几帧（与分拣判色同量级，再投票）
VERIFY_DWELL_MS = 600   # 到 C 位后停顿再拍（等机械停稳，避免拖影判错）
# ★ 装料约定的**默认值**：圆=红（A1–A5 装红件）/ 叉=蓝（A6–A9 装蓝件）。
#   ★ 2026-10-10 用户口径："对井字棋就是那样！也可以切换别的颜色，用户自己选吧" ——
#     所以它只是缺省，**可从界面按符号各选红/绿/蓝**（经 `/start` 的 `colors` 传进来）。
SYMBOL_COLOR = {"O": "红", "X": "蓝"}
ALLOWED_COLORS = ("红", "绿", "蓝")


def is_game_running() -> bool:
    """本模块是否有一局在跑（供分拣/AI 起步前做互斥检查，避免交错下发）。"""
    with _gate:
        th = _job.get("thread")
        return bool(th and th.is_alive())


# ---------------------------------------------------------------- 点位
def _needed_points(verify: bool = False) -> List[str]:
    """井字棋需要的点位（默认 = 分拣 38 点减去 C 检测位，共 37 个）。

    ★ 默认模式不判色 → 不需要 C 检测位；**拍照自证模式要送检 → 必须要有**。
      缺哪个就报哪个，不给"跑了但做不了"的假象。
    ★★ 2026-10-12 **终版**：快速模式（一趟两子）的翻转 = **虚拟切面命令**
      （`J6正面`/`J6反面` 按点名驱动、**不再读库**，用户口径「不用录制正反面」）——
      因此**不再**把这两个名字列进"必需点位"（旧实现会因库里没这两行而拒绝开局，
      与"去掉录制值"直接矛盾）。
    """
    out: List[str] = []
    for s in S.A_SLOTS:
        out += [s, s + "上"]
    for s in S.B_SLOTS:
        out += [s, s + "上"]
    if verify:
        out.append(S.CAMERA_POINT)
    out.append(S.START_POINT)
    return out


# ---------------------------------------------------------------- 状态
def _new_state(dry: bool, difficulty: str, user_sym: str, robot_sym: str,
               first: str, speed_pct: int, verify_color: bool = False,
               min_conf: float = 0.3, symbol_color: Optional[Dict[str, str]] = None,
               fast: bool = False) -> Dict:
    """一局全新的 state（每次「开始」都重建，绝不继承上一局残留）。"""
    sc = dict(SYMBOL_COLOR)
    sc.update({k: v for k, v in (symbol_color or {}).items() if v in ALLOWED_COLORS})
    return {
        "dry_run": bool(dry),
        "difficulty": difficulty,
        "speed_pct": int(speed_pct),      # ★ 本局速度（= 右上角全局速度）
        # ★ 两种模式（用户 2026-10-10）：默认模式不判色；自证模式取件后拍照核对颜色
        "verify_color": bool(verify_color),
        # ★★ 2026-10-11（方案 §D）「正常 / 快速」：快速 = 一趟取两子
        #   （机器人子正面 + 用户子反面）。默认 False = 正常（一趟一子，零回归）。
        #   ★ 本轮：先手**随机**（不再强制 user）：机器人先手时**先提前显示**其落子再等用户。
        "fast": bool(fast),
        "symbol_color": sc,               # ★ 自证约定（圆/叉各是什么颜色，界面可改）
        "min_conf": float(min_conf),      # 自证判色门槛（跟随视觉服务配置）
        "board": TT.new_board(),          # 9 格，与 B1..B9 同序
        "user_sym": user_sym, "robot_sym": robot_sym,
        "first": first,                   # "user" | "robot"
        "turn": "none",                   # user | robot | none
        "stage": "准备",
        "piece_no": 0,
        "moves": [],                      # 棋谱（复盘用）{cell,sym,by,src}
        "used": [],                       # 已取用过的 A 格
        "result": "",                     # user_win|robot_win|draw|""
        "win_line": [],
        "voided": False,                  # 「停止」= 作废（不影响胜负）
        "holding": False, "suspect_pick": False,
        # ★★ 预告态（2026-10-11 / 方案 §2C）：快速模式**机器人先手**时，机器人会**先把落子
        #   写进棋盘**（2D/3D 同步显示）、提示「已显示，待搬子」，**此刻尚未真搬**。
        #   这些格子下标（0–8）放这里，前端把对应棋子渲染成**半透明、不坠落** ——
        #   免得"格子已有子 + 手上还挂着一块"被看成"棋子一直在跟着机器人走"。
        #   真搬完成后立刻清空（`_play` / `_play_two` / 收尾 / 复原）。
        "pending": [],
        "error": "", "finished": False, "ok": None,
    }


def _snap(st: Dict) -> Dict:
    """启动响应回**线程启动前的快照**（别把活动字典交出去）。"""
    return copy.deepcopy(st)


# ---------------------------------------------------------------- 搬运原语
def _carry(ctx, pmap, st, dry, src_slot: str, dst_slot: str, who_cn: str,
           via_start: bool = False, verify: bool = False, expect_color: str = "",
           min_conf: float = 0.3, face: str = S.FACE_FRONT) -> None:
    """把一件从 src_slot 搬到 dst_slot（= 分拣搬一件的**精简版**：无判色、无 C 检测）。

    取料：src上方 → src → 吸 → 等吸牢 → 抬起
    ★ 搬运行程（用户口径 2026-10-10）：**不许「正上方 → 正上方」直接平移**。
      A 区取到件抬起后，先回 **开始/结束点（起点）**，再从起点去 B 区正上方落子 ——
      避免抬着件在 A/B 两个"正上方"之间横扫（会扫过托盘/两侧工装）。
      `via_start=True` 时启用（下棋 A→B 用；「复原」B→A 保持原路，见 `_restore_run`）。
    落子：dst上方 → 停止吸 → 放气 → 等 → 停止放气 → 回起点

    ★★ 2026-10-11（用户口径「放回的时候也不要乱旋转」）**`face` 参数化**：
      这一件**用哪个面取、就用哪个面放**（正面 → 下板气路 Bit1~4；反面 → 反面气路 Bit5~8）。
      · 为什么必须参数化：井字棋「复原」要取回的那件**当初是用哪个面放下的**，就得用哪个面去取 ——
        此前这里**写死正面**，于是"反面放下的那件"是"正面姿态 + 正面气路"去够 → 姿态不匹配、
        吸不到/摆乱（现场反馈的"放回去时乱了"）。
      · **翻转只发生在允许旋转的位置**：`src` 是**原料位**（`face_for_target` 强制 `keep`、严禁旋转），
        所以这里**先在它的正上方（`src+"上"`，允许旋转）把 J6 摆到 `face`**，再 `keep` 下去取 ——
        全程只有这一次必要翻转，不做多余的来回翻。

    ★ 速度一律取 `st["speed_pct"]`（= 右上角全局速度）——
      不在这里另立参数，避免"两条路各写一份速度"再出偏差。
    """
    speed = int(st.get("speed_pct") or DEFAULT_SPEED_PCT)
    face = S.norm_face(face)
    slot = "lower" if face == S.FACE_FRONT else "upper"
    if ctx.wait_pause():
        raise _Stop("已被停止", "暂停")
    # ★ 先在**正上方**（允许旋转）摆到该件的面 —— 再 keep 下去取（原料位严禁旋转）。
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·取料上方" % who_cn, st, face=face)
    # ★★ 统一口径（2026-10-11 用户原话「我在正上位反面，去原料区是变成了正面」）：
    #   `src_slot` 是**原料位**（A 区取料格 / B 区棋盘格）→ 一律 `keep`（保持当前面、绝不翻面）。
    #   井字棋取子/复原的"下落"都走这里（`_play`/`_play_two`/`_restore_run` 共用 `_carry`）。
    _move(ctx, pmap, src_slot, speed, dry, "%s·下落取料" % who_cn, st,
          face=S.face_for_target(src_slot, face))
    _air(ctx, "suck", dry, "%s·吸" % who_cn, st, face=face)
    if SUCK_HOLD_MS > 0:
        ctx.sleep(SUCK_HOLD_MS / 1000.0)
    _set_state(st, holding=True)
    # ★★ 2026-10-11：把"哪一面吸着件"**如实登记**（与分拣双面同一口径 `S.face_slot_put`）——
    #   以前只写 `holding=True`，3D 永远当成"下板那一件"，反面件会被画成正面件（显示与实际不符）。
    S.face_slot_put(st, slot, piece=src_slot, ok=True)
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·抬起" % who_cn, st, face=face)
    if verify and not dry:
        # ★★ 拍照自证（用户 2026-10-10 口径 · 两种模式里的"自证"那一种）：
        #    抬起后先去 C 检测位拍 3 帧 → 投票判色。
        #    · 判不出色      → **原路放回取料格 + 停止**（与分拣同一条统一口径）
        #    · 判出但不是该格应有的颜色 → 同样放回 + 停止，但文案点明"不符"
        #      （两句话必须分清，否则现场会往错的方向查：一个查相机、一个查装料）
        _move(ctx, pmap, S.CAMERA_POINT, speed, dry, "%s·送检" % who_cn, st, face=face)
        _set_state(st, stage="拍照自证", target="")
        if ctx.sleep(VERIFY_DWELL_MS / 1000.0):
            raise _Stop("已被停止", "自证")
        v = _capture(ctx, VERIFY_FRAMES, min_conf, "自证", st)
        _set_state(st, color=v.get("color") or "", conf=float(v.get("conf") or 0.0),
                   votes=v.get("votes") or {},
                   frame_detail=v.get("frame_detail") or [],
                   best_frame=v.get("best_frame"))
        if not v.get("ok"):
            _set_state(st, stage="自证失败·放回原位")
            _put_back(ctx, pmap, src_slot, speed, dry, blow_ms=BLOW_MS,
                      prefix="放回原位 %s" % src_slot, st=st, face=face)
            raise _Stop("颜色无法识别（%s），已放回原位 %s"
                        % (v.get("reason") or "无有效判定", src_slot), "自证")
        if expect_color and v.get("color") != expect_color:
            _set_state(st, stage="自证不符·放回原位")
            _put_back(ctx, pmap, src_slot, speed, dry, blow_ms=BLOW_MS,
                      prefix="放回原位 %s" % src_slot, st=st, face=face)
            raise _Stop("自证不符：该格应为 %s，实拍 %s，已放回原位 %s"
                        % (expect_color, v.get("color"), src_slot), "自证")
        _set_state(st, stage="自证通过")
    if via_start:
        # ★ 经起点再过去：抬起后不直接横移到 dst 正上方，先回原点把"抬着件横扫"这段消掉。
        #   ★ 2026-10-11：**保持这一件的面**过去（起点是允许旋转的位置，但这里不需要翻面）。
        _move(ctx, pmap, S.START_POINT, speed, dry, "%s·经起点" % who_cn, st, face=face)
    _move(ctx, pmap, dst_slot + "上", speed, dry, "%s·落子" % who_cn, st, face=face)
    _air(ctx, "release", dry, "%s·停止吸" % who_cn, st, face=face)
    _air(ctx, "blow", dry, "%s·放气" % who_cn, st, face=face)
    if BLOW_MS > 0:
        ctx.sleep(BLOW_MS / 1000.0)
    _air(ctx, "unblow", dry, "%s·停止放气" % who_cn, st, face=face)
    S.face_slot_take(st, slot)      # ★ 与 put 对称：登记"这面已放下"（3D 立刻不再显示手上那件）
    _set_state(st, holding=False)
    _move(ctx, pmap, S.START_POINT, speed, dry, "回起点", st)


def _play(ctx, pmap, st, dry, sym: str, cell: int) -> None:
    """落一手：取该符号的下一格料 → 搬到 B{cell+1} → 登记棋盘与棋谱。"""
    used: List[str] = st["used"]
    board: List[Optional[str]] = st["board"]
    moves: List[Dict] = st["moves"]
    src = TT.pick_source(sym, used)
    if src is None:
        who = "圆" if sym == TT.O else "叉"
        raise _Stop("没有可用的棋子料了（%s 的来源格已取空，请先「复位」）" % who, "取料")
    dst = TT.cell_to_slot(cell)
    who_cn = "你" if sym == st["user_sym"] else "机器人"
    # ★ A→B 下棋：经起点再落子（不许"正上方→正上方"直接平移，用户口径 2026-10-10）
    # ★ 自证模式（`verify_color`）：取件抬起后先去 C 位拍照核对颜色；不符/判不出 → 放回原位并停
    _carry(ctx, pmap, st, dry, src, dst, who_cn, via_start=True,
           verify=bool(st.get("verify_color")),
           expect_color=((st.get("symbol_color") or SYMBOL_COLOR).get(sym, "")),
           min_conf=float(st.get("min_conf") or 0.3))
    # ★ 就地更新（保持 st 里的 list 引用稳定，供 /state 深拷贝读出）；
    #   真正的"落子"发生在搬运完成之后 —— 界面不会出现"还没搬就说已落子"。
    used.append(src)
    board[cell] = sym
    moves.append({"cell": int(cell), "sym": sym,
                  "by": "user" if sym == st["user_sym"] else "robot", "src": src,
                  # ★★ 2026-10-11：**记下这件是用哪个面放下的** —— 复原时必须用同一个面去取
                  #   （单件趟永远是正面；没有这个字段的话复原只能瞎猜，见 `_restore_run`）。
                  "face": S.FACE_FRONT})
    # ★ 真搬已完成 → 该格不再是"预告态"（清空 pending，3D 恢复实心）
    _set_state(st, piece_no=len(moves), pending=[])


def _play_two(ctx, pmap, st, dry, robot_cell: int, user_cell: int) -> None:
    """★★ 2026-10-11（方案 §D）：**快速模式的一趟取两子** —— 机器人子（正面/下板）+
    用户子（反面/上板），复用分拣的双面原语（`_move_flip_j6` / `_dual_drop`）。

    物理顺序（与分拣双面同源）：
      ① 取机器人子（**正面**/下板）→ ② 在正上位**翻到反面（只覆盖 J6）** →
      ③ 取用户子（**反面**/上板，发**反面那一路吸气** Bit5）→
      ④ 先落**用户子**（反面，`_dual_drop(face=FACE_BACK)`）→
      ⑤ 再落**机器人子**（正面，`_dual_drop(face=FACE_FRONT)`）→ ⑥ 回起点。
    ★★ 2026-10-11 **更新口径（用户：「先放回第二次，然后放回第一次，中间不要乱反转」）**：
      取完两件时 J6 **正停在反面** → **先落反面那件（用户子）不用翻面**，落完再翻到正面落机器人子。
      这与分拣双面同一顺序（`S.order_for_drop`：upper 在前），全程只翻必要的那几次。
      （旧实现是"先落机器人子（正面）"，会多绕一次"回起点 + 翻面"。）
    ★ 件与件之间经**起点**再走，避免抬着件在 A/B 正上方之间横扫（与 `_carry` 的 via_start 同口径）。
    ★ 人话口径：**用哪面取的就用哪面放**（机器人子=正面、用户子=反面），`face` 由 `_move(..., face=...)`
      贯穿全程 —— 不会被点位默认正面顶掉（与分拣 B 块同一根因修复）。
    ★★ 2026-10-11 前提更正：两面**各自独立气路**（下板 1~4 位 / 反面 5~8 位）——
      放下板**不会**影响还挂着的反面件，故原先"放一件 → 立刻复吸另一件"的 `_dual_drop(keep=…)` **已取消**。
    ★ 现场没有真空传感器 → 手上状态仍是**流程推断**；异常一律先收气再回起点（沿用既有口径）。
    """
    speed = int(st.get("speed_pct") or DEFAULT_SPEED_PCT)
    robot_sym, user_sym = st["robot_sym"], st["user_sym"]
    used: List[str] = st["used"]
    r_src = TT.pick_source(robot_sym, used)
    if r_src is None:
        who = "圆" if robot_sym == TT.O else "叉"
        raise _Stop("没有可用的机器人棋子料了（%s 的来源格已取空，请先「复位」）" % who, "取料")
    u_src = TT.pick_source(user_sym, list(used) + [r_src])
    if u_src is None:
        who = "圆" if user_sym == TT.O else "叉"
        raise _Stop("没有可用的用户棋子料了（%s 的来源格已取空，请先「复位」）" % who, "取料")
    r_dst, u_dst = TT.cell_to_slot(robot_cell), TT.cell_to_slot(user_cell)
    if ctx.wait_pause():
        raise _Stop("已被停止", "暂停")
    # ① 取机器人子（正面/下板）
    _move(ctx, pmap, r_src + "上", speed, dry, "机器人·取料上方", st, face=S.FACE_FRONT)
    # ★★ 统一口径：`r_src` 是**原料位** → 用 `keep`（保持当前面、绝不翻面；此刻当前=正面 ← 上面那步）。
    _move(ctx, pmap, r_src, speed, dry, "机器人·下落取料", st,
          face=S.face_for_target(r_src, S.FACE_FRONT))
    _air(ctx, "suck", dry, "机器人·吸", st, face=S.FACE_FRONT)
    if SUCK_HOLD_MS > 0:
        ctx.sleep(SUCK_HOLD_MS / 1000.0)
    _set_state(st, holding=True)
    S.face_slot_put(st, "lower", piece=r_src, ok=True)     # ★ 3D 如实显示"下板（正面）那件"
    _move(ctx, pmap, r_src + "上", speed, dry, "机器人·抬起", st, face=S.FACE_FRONT)
    # ② 翻到反面（只覆盖 J6）—— 此时上/下板角色互换
    _move_flip_j6(ctx, pmap, DUAL_FACE_FLIP_POINT, speed, dry, "翻转·J6反面（仅J6）", st)
    # ③ 取用户子（反面/上板）：★ 发**反面那一路的吸气**（Bit5）——
    #    两面**独立气路**，只动反面、不碰下板（此前的"保持吸气不断真空"基于"两面共用一路"
    #    的错误前提，已按用户权威位表纠正：反面那一路此前从未被驱动）。
    _move(ctx, pmap, u_src + "上", speed, dry, "用户·取料上方", st, face=S.FACE_BACK)
    # ★★ 统一口径：`u_src` 是**原料位** → 用 `keep`（保持当前面、绝不翻面；此刻当前=反面 ← 上面那步 + ②的翻转）。
    _move(ctx, pmap, u_src, speed, dry, "用户·下落取料", st,
          face=S.face_for_target(u_src, S.FACE_BACK))
    _air(ctx, "suck", dry, "用户·吸", st, face=S.FACE_BACK)
    if SUCK_HOLD_MS > 0:
        ctx.sleep(SUCK_HOLD_MS / 1000.0)
    S.face_slot_put(st, "upper", piece=u_src, ok=True)     # ★ 3D 如实显示"上板（反面）那件"
    _move(ctx, pmap, u_src + "上", speed, dry, "用户·抬起", st, face=S.FACE_BACK)
    # ④ **先落用户子（反面/上板）**：此刻 J6 就停在反面（取完②件）→ **不用翻面**，
    #    直接经起点 → 落子 → **只放反面那一路**（不动还挂着的下板那件）。
    #   ★ 2026-10-11 更新口径（用户：「先放回第二次，然后放回第一次，中间不要乱反转」）：
    #     拍完/取完停在反面 → 先放反面那件最省事；与分拣双面 `S.order_for_drop`（upper 在前）同序。
    _move(ctx, pmap, S.START_POINT, speed, dry, "经起点", st, face=S.FACE_BACK)
    _move(ctx, pmap, u_dst + "上", speed, dry, "用户·落子", st, face=S.FACE_BACK)
    _dual_drop(ctx, dry, face=S.FACE_BACK, st=st)
    S.face_slot_take(st, "upper")
    # ⑤ 再落机器人子（正面/下板）：**翻到正面** → 落子 → **只放下板那一路**
    _move(ctx, pmap, r_dst + "上", speed, dry, "机器人·落子", st, face=S.FACE_FRONT)
    _dual_drop(ctx, dry, face=S.FACE_FRONT, st=st)
    S.face_slot_take(st, "lower")
    _set_state(st, holding=False)
    _move(ctx, pmap, S.START_POINT, speed, dry, "回起点", st)
    # ---- 登记（就地更新，保持 list 引用稳定，供 /state 深拷贝读出）----
    used.append(r_src)
    used.append(u_src)
    st["board"][robot_cell] = robot_sym
    st["board"][user_cell] = user_sym
    # ★★ 2026-10-11：**记下每件用哪个面放的**，且**按"实际放下的先后"追加**（先用户子/反面、后机器人子/正面）
    #   —— 复原要按面取（`S.order_for_restore`）；两处口径一致，别各排一遍。
    st["moves"].append({"cell": int(user_cell), "sym": user_sym, "by": "user", "src": u_src,
                        "face": S.FACE_BACK})
    st["moves"].append({"cell": int(robot_cell), "sym": robot_sym, "by": "robot", "src": r_src,
                        "face": S.FACE_FRONT})
    # ★ 两子都真搬完成 → 清空预告态（3D 由半透明恢复实心）
    _set_state(st, piece_no=len(st["moves"]), pending=[])


def _wait_move(ctx, q: "queue.Queue") -> Optional[int]:
    """轮到用户：阻塞等一个 cell（每 0.2s 检查取消/暂停）。返回 None = 被停止。"""
    while True:
        if ctx.cancelled():
            return None
        if ctx.wait_pause():        # 暂停时冻结在这里；期间被停止 → True
            return None
        try:
            v = q.get(timeout=0.2)
        except queue.Empty:
            continue
        return None if v is None else int(v)


def _release_air(ctx, dry, st) -> None:
    """best-effort 收气（停止吸 + 停止放）—— 绝不让阀留在开着。"""
    if dry:
        return
    for act, lab in (("unblow", "收尾·停止放"), ("release", "收尾·停止吸")):
        try:
            _air(ctx, act, False, lab, st)
        except Exception as exc:  # noqa: BLE001
            log.warning("井字棋%s失败：%s", lab, exc)


# ---------------------------------------------------------------- 主线程
def _run(ctx, q: "queue.Queue", st: Dict, dry: bool, pmap: Dict[str, object]) -> None:
    """★ 井字棋 = **旋转白名单④**（用户口径「J6 如果……**游戏过程中**可以旋转」）。

    整局（含快速模式的两面翻 J6）都在 `with S.allow_rotate():` 里跑 —— 这是本流程**唯一**
    的旋转授权声明；出了这个 with 立刻回到"默认不旋转"。外层再不动任何判据。
    """
    with S.allow_rotate():
        return _run_body(ctx, q, st, dry, pmap)


def _run_body(ctx, q: "queue.Queue", st: Dict, dry: bool, pmap: Dict[str, object]) -> None:
    difficulty = st.get("difficulty") or "normal"
    speed = int(st.get("speed_pct") or DEFAULT_SPEED_PCT)   # ★ 右上角全局速度
    board: List[Optional[str]] = st["board"]
    user_sym = st["user_sym"]
    robot_sym = st["robot_sym"]
    turn = st["first"]
    first = st["first"]           # ★ 先手（本轮：不再被强制成 user；快速模式靠它决定"每趟由谁发起"）
    fast = bool(st.get("fast"))
    phase, stop_reason = "done", ""
    try:
        _set_state(st, stage="回开始/结束点", turn="none")
        _move(ctx, pmap, S.START_POINT, speed, dry, "回开始/结束点", st)
        while True:
            if ctx.cancelled():
                stop_reason, phase = "已被停止", "cancelled"
                break
            # ★★ 2026-10-11（方案 §D）「免等待」：**只剩一个空位** → 不管轮到谁都免等待、
            #   直接完成（唯一选择）。放在回合分派**之前** —— 正常/快速两种模式都适用。
            empties = TT.empties(board)
            if len(empties) <= 1:
                if not empties:
                    break
                cell = empties[0]
                who = user_sym if turn == "user" else robot_sym
                _set_state(st, stage="只剩一个空位，直接完成（免等待）",
                           turn=("user" if turn == "user" else "robot"))
                _play(ctx, pmap, st, dry, who, cell)
                _set_state(st, turn="none")
                r = TT.result_of(board, user_sym)
                if r["result"]:
                    _set_state(st, result=r["result"], win_line=r["win_line"], stage="结束")
                break
            # ★★ 2026-10-11（本轮）快速模式：先手**随机**（不再强制用户先行）。
            #   · 机器人先手（turn=="robot"）：机器人**先决定并提前把落子显示出来**（后端棋盘先写上，
            #     2D 棋盘 / 3D 场景**同源**显示），提示「机器人已下棋完成（已显示，待搬子）」，
            #     然后**等用户落子**；用户落子到位后**一趟两子**同步完成（机器人子 + 用户子）。
            #     ⚠ 此刻**尚未真搬子** —— 界面文案如实写「已显示，待搬子」，不含糊。
            #   · 用户先手：等用户落子 → 算机器人应对 → 一趟两子（沿用上一轮逻辑）。
            if fast:
                cell_r = None
                if turn == "robot":
                    cell_r = TT.ai_move(board, robot_sym, difficulty)
                    if cell_r < 0:
                        break
                    # ★ 提前显示：后端棋盘先落子（2D/3D 与棋盘同源），**尚未真搬子**。
                    board[cell_r] = robot_sym
                    # ★ 同时登记为**预告态**：前端把这格棋子画成半透明、不坠落 ——
                    #   别让"格子已有子 + 手上还挂着一块"被看成"棋子跟着机器人走"。
                    _set_state(st, pending=[int(cell_r)])
                    if TT.result_of(board, user_sym)["result"]:
                        # 机器人这一手直接定胜负 → 不必等用户；本趟只搬这一子（单件）
                        _set_state(st, stage="机器人落子", turn="robot")
                        _play(ctx, pmap, st, dry, robot_sym, cell_r)
                        _set_state(st, turn="none")
                        r = TT.result_of(board, user_sym)
                        if r["result"]:
                            _set_state(st, result=r["result"], win_line=r["win_line"], stage="结束")
                        break
                # 等用户落子（机器人先手时：机器人子已显示，将与本子合并成"一趟两子"）
                _set_state(st, turn="user",
                           stage=("机器人已下棋完成（已显示，待搬子）；轮到你落子"
                                  if cell_r is not None else "轮到你落子"))
                cell_u = _wait_move(ctx, q)
                if cell_u is None:
                    stop_reason, phase = "已被停止", "cancelled"
                    break
                if not TT.legal(board, cell_u):
                    continue                     # 并发下已被占 → 继续等
                if cell_r is not None:
                    # 机器人先手：机器人子已提前显示 → **一趟两子**（机器人子正面 + 用户子反面）
                    _set_state(st, stage="一趟取两子（机器人子 + 你的子）", turn="user")
                    _play_two(ctx, pmap, st, dry, cell_r, cell_u)
                else:
                    # 用户先手：这一手若定胜负 → 只需搬用户这一子；否则机器人应对 → 一趟两子
                    tmp = list(board)
                    tmp[cell_u] = user_sym
                    if TT.result_of(tmp, user_sym)["result"]:
                        _set_state(st, stage="你落子", turn="user")
                        _play(ctx, pmap, st, dry, user_sym, cell_u)
                    else:
                        cell_ai = TT.ai_move(tmp, robot_sym, difficulty)
                        if cell_ai < 0:
                            _set_state(st, stage="你落子", turn="user")
                            _play(ctx, pmap, st, dry, user_sym, cell_u)
                        else:
                            _set_state(st, stage="一趟取两子（你 + 机器人）", turn="user")
                            _play_two(ctx, pmap, st, dry, cell_ai, cell_u)
                _set_state(st, turn="none")
                r = TT.result_of(board, user_sym)
                if r["result"]:
                    _set_state(st, result=r["result"], win_line=r["win_line"], stage="结束")
                    break
                # ★ 每个"趟"由**先手方**发起：先手方继续下一手（robot 先手 → 机器人再提前显示下一手）
                turn = first
                continue
            # ---- 正常模式（一趟一子；与既有逻辑逐字一致）----
            if turn == "robot":
                _set_state(st, stage="机器人思考", turn="robot")
                cell = TT.ai_move(board, robot_sym, difficulty)
                if cell < 0:
                    break
                _set_state(st, stage="机器人落子", turn="robot")
                _play(ctx, pmap, st, dry, robot_sym, cell)
            else:
                _set_state(st, stage="轮到你落子", turn="user")
                cell = _wait_move(ctx, q)
                if cell is None:
                    stop_reason, phase = "已被停止", "cancelled"
                    break
                if not TT.legal(board, cell):
                    continue                     # 并发下已被占 → 继续等
                _set_state(st, stage="你落子", turn="user")
                _play(ctx, pmap, st, dry, user_sym, cell)
            _set_state(st, turn="none")
            r = TT.result_of(board, user_sym)
            if r["result"]:
                _set_state(st, result=r["result"], win_line=r["win_line"], stage="结束")
                break
            turn = "user" if turn == "robot" else "robot"
    except _Stop as e:
        stop_reason = e.reason
        phase = "cancelled" if "停止" in e.reason else "error"
        if phase == "error":
            _set_state(st, holding=False, suspect_pick=True)
            # ★ 手上没料（holding=False）才回起点：若正夹着件，别把它拖回起点（更危险）
            if not dry:
                try:
                    _set_state(st, stage="异常·回开始/结束点", error=e.reason)
                    _move(ctx, pmap, S.START_POINT, speed, dry, "异常回原点", st)
                except _Stop as e2:
                    log.warning("井字棋异常后回原点失败：%s", e2.reason)
    except Exception as e:  # noqa: BLE001
        log.exception("井字棋异常")
        stop_reason, phase = "内部异常：%s" % str(e)[:150], "error"
    finally:
        # ★★ 2026-10-10：**注销运行登记最先做且不可跳过**（与 ai.py 同名死机修复同因）
        ctx.finish(phase)
        try:
            # ★ 收尾：气路绝不留在开着（电平保持型，最容易出的现场事故）
            if not dry and st.get("holding"):
                _release_air(ctx, dry, st)
            _set_state(st, stage="结束", error=stop_reason, finished=True,
                       ok=(phase == "done"), phase=phase, turn="none", holding=False,
                       pending=[])      # ★ 收尾必清预告态（否则格子会一直半透明）
            emit_event("tictactoe", "info" if phase == "done" else "warn", "tictactoe.stop",
                       ("井字棋完成：%s" % st.get("result") or "已结束") if phase == "done"
                       else ("井字棋结束：%s" % stop_reason),
                       {"phase": phase, "result": st.get("result"), "dry_run": dry,
                        "moves": len(st.get("moves") or [])}, actor="control")
        except Exception:  # noqa: BLE001
            log.exception("井字棋收尾异常（运行登记已注销，不影响后续指令）")


# ---------------------------------------------------------------- 「复原」线程
def _restore_run(ctx, st: Dict, pmap: Dict[str, object]) -> None:
    """★ 「复原」也是**游戏流程**（旋转白名单④）→ 整段在 `with S.allow_rotate():` 里跑
    （复原要按每件**自己那个面**搬回去，必须允许翻 J6）。"""
    with S.allow_rotate():
        return _restore_run_body(ctx, st, pmap)


def _restore_run_body(ctx, st: Dict, pmap: Dict[str, object]) -> None:
    """★「复原」（2026-10-11 用户口径：「**复原和复盘合并，就是复原，不用空跑啊，要真机移动**」）
    —— 机器人**真的动**：把 B 区已落的子**按落子逆序（LIFO）逐个搬回各自来源 A 格（原位）**。

    ★ 为什么只有**一个**实现：以前「复位」与「复盘」是**两条路**，而"复位"那条把 `dry`
      **写死成 True** → 真实局点「复位」机器人**根本不动**（现场 bug）。
      现在合并为一处，`/reset` 与 `/replay` 都走这里 —— **不新造顺序、不两条路各写一份**。
    ★ 顺序（★★ 2026-10-11 **更新口径**，用户原话「先放回第二次，然后放回第一次，
      放回的时候也不要乱旋转」）：**先反面那件、后正面那件** ——
      · 唯一判据 = `S.order_for_restore`（与放料次序 `S.order_for_drop` **同序**，不在流程里再排一遍）；
      · 同一面内保持**落子逆序**（后放的先回）；
      · 每一件**用它自己当初那个面**去取（`moves[].face`）——
        "反面放下的那件"必须用**反面姿态 + 反面气路(Bit5~8)** 取，否则姿态不匹配 → 吸不到/摆乱
        （这正是现场反馈"放回去时乱了"的根因；此前 `_carry` 写死正面）。
      · **不做多余的来回翻**：`_carry` 只在"原料位正上方"（允许旋转处）摆一次面，之后一路 `keep`。
    ★ 每格独立、不堆叠。
    ★ 复用而非新造：搬子一律走 `_carry`（= 分拣同一套 `_move`/`_air`），
      **不新增任何下发通道**、不绕开 `motion.command`、不新增触发位。
    ★ 「原位」= 该手棋谱里的 `src`（取料来源 A 格，见 `services/tictactoe.py::pick_source`）。
    ★ 空跑（`st["dry_run"]`）：只走预演路径（`_carry(..., dry=True)` 全程只 `_set` 阶段、
      不下发真实运动）—— 正常流程下 `_launch_restore` 已在**真实局**才起本线程，
      这里保留 dry 分支只为自检/离线调用不炸。
    ★ 停止/异常沿用既有口径：步边界停止（`ctx.cancelled()`）、异常不收件就回起点，
      **不许悬空带料**（`holding` 为真时收尾 `_release_air` 先后关两路气）。
    ★ 收尾**必须先 `ctx.finish(...)`**（否则注销不到 → 手动指令被永久拒绝，见 test_run_recovery）。
    """
    phase, stop_reason = "done", ""
    dry = bool(st.get("dry_run"))
    speed = int(st.get("speed_pct") or DEFAULT_SPEED_PCT)
    try:
        moves = list(st.get("moves") or [])
        n = len(moves)
        # ★ 顺序：先反面件、后正面件（同面内=落子逆序）；每件按自己的 face 取。
        plan = S.order_for_restore(list(reversed(moves)))
        _set_state(st, stage="复原：回开始/结束点", turn="none")
        # 空跑也走这一步的"预演路径"（dry=True → 只写阶段，不下发）
        _move(ctx, pmap, S.START_POINT, speed, dry, "复原·回开始/结束点", st)
        for i, mv in enumerate(plan, start=1):
            if ctx.cancelled():
                stop_reason, phase = "已被停止", "cancelled"
                break
            cell = int(mv.get("cell"))
            src = str(mv.get("src") or "")          # 「原位」= 来源 A 格
            face = S.norm_face(mv.get("face"))
            # ★ 回执（用现有 stage 机制）：第几个子 / 去向（原位 A?）/ 用哪面取
            _set_state(st, stage="复原：搬回第 %d/%d 个棋子 → %s（原位·%s）"
                                % (i, n, src, S.face_zh(face)),
                       piece_no=n - i)
            _carry(ctx, pmap, st, dry, TT.cell_to_slot(cell), src, "复原", face=face)
        if phase == "done":
            # ★ 终态如实：子都回原位 → 棋盘清空/复位到初始（与 3D / 2D 一致）
            st.update({"board": TT.new_board(), "moves": [], "used": [],
                       "result": "", "win_line": [], "voided": False, "piece_no": 0,
                       "pending": []})
    except _Stop as e:
        stop_reason = e.reason
        phase = "cancelled" if "停止" in e.reason else "error"
        if phase == "error":
            _set_state(st, holding=False, suspect_pick=True)
            # ★ 手上没料（holding=False）才回起点：若正夹着件，别把它拖回起点（更危险）
            if not dry:
                try:
                    _set_state(st, stage="复原异常·回开始/结束点", error=e.reason)
                    _move(ctx, pmap, S.START_POINT, speed, dry, "复原异常回原点", st)
                except _Stop as e2:
                    log.warning("井字棋复原异常后回原点失败：%s", e2.reason)
    except Exception as e:  # noqa: BLE001
        log.exception("井字棋复原异常")
        stop_reason, phase = "内部异常：%s" % str(e)[:150], "error"
    finally:
        # ★★ 注销运行登记必须最先做且不可跳过（与 _run 同一口径）
        ctx.finish(phase)
        try:
            # ★ 收尾：气路绝不留在开着（停止时手上若还吸着件 → 先收气，不许悬空带料）
            if not dry and st.get("holding"):
                _release_air(ctx, dry, st)
            _set_state(st, stage="复原结束", error=stop_reason, finished=True,
                       ok=(phase == "done"), phase=phase, turn="none", holding=False,
                       pending=[])
            emit_event("tictactoe", "info" if phase == "done" else "warn", "tictactoe.restore",
                       "井字棋复原%s%s" % ("完成" if phase == "done" else "结束",
                                        "" if phase == "done" else "：" + stop_reason),
                       {"phase": phase, "dry_run": dry}, actor="control")
        except Exception:  # noqa: BLE001
            log.exception("井字棋复原收尾异常（运行登记已注销，不影响后续指令）")


# ---------------------------------------------------------------- 接口
class StartIn(BaseModel):
    difficulty: str = Field(default="normal", max_length=16)
    # ★ 后端默认**仍是空跑**（脚本/直连 API 的兜底，不经界面也能安全调用）；
    #   ★ 界面（`stores/tictactoe.js`）默认是**真实下发**（用户口径「不用空跑啊，要真机移动」）
    #     —— 两层默认不同，**别把这里改成 False**（那会让不带 dry_run 的脚本突然真动机器人）。
    dry_run: bool = True
    # ★ 沿用右上角那份全局速度（与分拣同口径）：**右上角多少，机器人多少**。
    speed_pct: int = Field(default=DEFAULT_SPEED_PCT, ge=5, le=100,
                           description="沿用右上角那份全局速度")
    # ★ 两种模式（用户 2026-10-10）：false = 默认（不判色，直接下）；true = 拍照自证
    verify_color: bool = Field(default=False,
                               description="拍照自证：取件后到 C 位拍照核对颜色")
    # ★ 自证约定（用户 2026-10-10："也可以切换别的颜色，用户自己选"）：
    #   {"O":"红","X":"蓝"} —— 缺省即默认；只接受 红/绿/蓝，非法值忽略（回落缺省）
    colors: Optional[Dict[str, str]] = Field(default=None,
                                             description="圆/叉各自应有的颜色（自证用）")
    # ★★ 2026-10-11（方案 §D）「正常 / 快速」两模式（**界面开关**）：
    #   false（默认）= 正常（一趟一子，现状）；true = 快速（一趟取两子，机器人子正面 + 用户子反面）。
    #   ★ 本轮：先手**随机**（不再为"等用户"而强制 user）；机器人先手时**先提前显示**其落子再等用户。
    #   **默认正常**，不改既有默认行为。
    fast: bool = Field(default=False,
                       description="快速模式：一趟取两子（J6 双面，需 J6正面/J6反面）")


@router.post("/start")
def api_start(body: StartIn, tok: str = Depends(require_control)):
    """开始一局（默认 dry_run=true 空跑）。开局随机决定先手，先手执圆。"""
    with _gate:
        if _job.get("thread") and _job["thread"].is_alive():
            raise HTTPException(409, detail="已有一局在进行，请先「停止」")
    # ★ 与分拣/执行互斥：本局会占用 40135 触发位，启动前确认没有别的流程在跑
    _reject_if_run_active("开始井字棋")
    import app.api.sorting as _sort                                  # 延迟导入：避免循环
    with _sort._gate:
        if _sort._job.get("thread") and _sort._job["thread"].is_alive():
            raise HTTPException(409, detail="分拣正在运行，先「停止」分拣再开局")
    if not body.dry_run and not real_write_enabled():
        raise HTTPException(403, detail="真实下发未开启（EFORT_REAL_MOTION=1 且 motion.real_write=true）")
    # ★ 统一"未就绪"闸门：真实对局（非空跑）前先校验伺服是否上电。空跑放行。
    reject_if_not_ready(dry=bool(body.dry_run))
    # ★ 自证只在"真的要判色"时才要求相机：空跑 / 双闸未开时物体到不了 C 区，
    #   判色必然"检测不到" —— 拿相机当门槛只会挡住空跑验证（与分拣同一条口径）。
    need_verify = bool(body.verify_color) and (not body.dry_run) and real_write_enabled()
    fast = bool(body.fast)
    # ★★ 2026-10-11（方案 §D）：**快速 + 拍照自证**暂不合并（快速是"一趟取两子"的双面编排，
    #   自证是"取件后去 C 位拍照"的单件流程 —— 两者叠在一起会互相打断）。
    #   本轮**明确拒绝**这个组合（如实提示），而不是悄悄忽略其一。
    if fast and bool(body.verify_color):
        raise HTTPException(400, detail="快速模式暂不与「拍照自证」合并：请二选一"
                                        "（快速=一趟两子；自证=逐件拍照核对颜色）")

    db = SessionLocal()
    try:
        rows = list_points(db)
        pmap = {p.name: p for p in rows}
    finally:
        db.close()
    # ★ 自证模式比默认模式**多要一个 C 检测位**；快速模式的翻转是虚拟切面命令（不查库，
    #   2026-10-12 终版「不用录制正反面」），不列进必需点位。
    miss = [n for n in _needed_points(need_verify) if n not in pmap]
    if miss:
        raise HTTPException(400, detail={
            "message": "点位不齐，缺 %d 个：%s（先在「点位」页补齐示教）"
                       % (len(miss), "、".join(miss[:8])),
            "missing": miss})
    min_conf = 0.3
    if need_verify:
        # ★ 相机没就绪 → **开局就拒**（不进入任何运动），不给"跑起来才发现判不了色"的假象
        min_conf = vision_preflight(None)

    difficulty = TT.norm_difficulty(body.difficulty)
    # ★★ 2026-10-11（本轮）：先手**不再为"等用户"而硬改** —— 正常/快速都按**原有逻辑**随机决定先手
    #   （用户原话：「先手按原有逻辑，不要为了等用户而硬改先手」）。
    #   快速模式下若**机器人先手**：机器人**先决定并提前把落子显示出来**（后端棋盘与 2D/3D 同步），
    #   提示「机器人已下棋完成（已显示，待搬子）」，然后**等用户落子**；用户落子到位后**一趟两子**
    #   同步完成（机器人子 + 用户子）。详见 `_run` 的快速分支。
    first = random.choice(["user", "robot"])
    # 先手执圆：先手 = O，后手 = X
    user_sym, robot_sym = (TT.O, TT.X) if first == "user" else (TT.X, TT.O)
    st = _new_state(body.dry_run, difficulty, user_sym, robot_sym, first,
                    int(body.speed_pct), verify_color=bool(body.verify_color),
                    min_conf=min_conf, symbol_color=body.colors, fast=fast)
    snap = _snap(st)
    q: "queue.Queue" = queue.Queue()
    ctx = new_run("井字棋", kind="tictactoe")
    with _gate:
        _job.update({"state": st, "ctx": ctx, "q": q, "started_at": time.time()})
        th = threading.Thread(target=_run, args=(ctx, q, st, bool(body.dry_run), pmap),
                              name="tictactoe", daemon=True)
        _job["thread"] = th
    th.start()
    ctx.set_thread(th)      # ★ 绑定执行线程：让"卡死登记"能被 is_alive() 精确识别
    emit_event("tictactoe", "info", "tictactoe.start",
               "开始井字棋（%s，难度 %s，%s%s，先手 %s，速度 %d%%）" % (
                   "空跑" if body.dry_run else "真实下发", TT.diff_cn(difficulty),
                   "快速·一趟两子" if fast else "正常·一趟一子",
                   "·拍照自证" if body.verify_color else "",
                   "你" if first == "user" else "机器人", int(body.speed_pct)),
               {"difficulty": difficulty, "first": first, "dry_run": body.dry_run,
                "speed_pct": int(body.speed_pct), "verify_color": bool(body.verify_color),
                "fast": fast},
               actor="control")
    return {"ok": True, "run_id": ctx.run_id, "state": snap}


@router.get("/state")
def api_state(tok: str = Depends(require_control)):
    """对局进度快照（棋盘/轮到谁/阶段/暂停态/胜负/棋谱）。"""
    with _gate:
        st = _job.get("state")
        ctx = _job.get("ctx")
        running = bool(_job.get("thread") and _job["thread"].is_alive())
        snap = copy.deepcopy(st) if isinstance(st, dict) else None
        rid = getattr(ctx, "run_id", None)
    if snap is not None:
        snap["paused"] = bool(running and rid and _is_paused(rid))
    return {"ok": True, "running": running, "state": snap}


class MoveIn(BaseModel):
    cell: int = Field(..., ge=0, le=8)


@router.post("/move")
def api_move(body: MoveIn, tok: str = Depends(require_control)):
    """用户落子：把一个 cell 投进对局队列。仅「轮到你」时可下，其余一律拒绝。"""
    with _gate:
        th = _job.get("thread")
        st = _job.get("state")
        q = _job.get("q")
    if not (th and th.is_alive() and isinstance(st, dict) and q is not None):
        raise HTTPException(409, detail="没有进行中的对局，先「开始」")
    if st.get("turn") != "user":
        raise HTTPException(409, detail="现在是机器人的回合（你只能「停止」或「暂停」）")
    if not TT.legal(st.get("board") or [], body.cell):
        raise HTTPException(400, detail="这一格已经有子了（或下标非法）")
    q.put(int(body.cell))
    return {"ok": True, "cell": int(body.cell)}


@router.post("/stop")
def api_stop(tok: str = Depends(require_control)):
    """停止 = **作废**本局（不影响胜负记录），机器人回起点。

    ★ 棋盘**保留实际已落的子**（料真在那儿，界面要如实显示），
      想清干净请点「复位」把棋子搬回 A 区。
    """
    with _gate:
        ctx = _job.get("ctx")
        th = _job.get("thread")
        q = _job.get("q")
        st = _job.get("state")
    rid = getattr(ctx, "run_id", None)
    if not (th and th.is_alive()):
        return {"ok": True, "cancelled": [], "note": "没有进行中的对局"}
    if isinstance(st, dict):
        st["voided"] = True
    hit = request_cancel(rid)
    if q is not None:
        try:
            q.put(None)          # 唤醒可能正在等用户落子的线程
        except Exception:  # noqa: BLE001
            pass
    emit_event("tictactoe", "warn", "tictactoe.void",
               "井字棋：已请求停止（作废）", {"run_id": rid}, actor="control")
    return {"ok": True, "cancelled": hit}


class ResetIn(BaseModel):
    # ★ 复原也沿用右上角那份全局速度（客户端可带；不带就用本局原速度）
    speed_pct: Optional[int] = Field(default=None, ge=5, le=100)


def _launch_restore(st: Dict, speed_pct: Optional[int]) -> Dict:
    """「复原」的**唯一实现**（`/reset` 与 `/replay` 共用，杜绝"两条路各写一份"）。

    ★ 用户口径（2026-10-11）：「**复原和复盘合并，就是复原，不用空跑啊，要真机移动**」。
    · 真实局 → 起 `_restore_run` 线程：机器人**真的动**，逐个把子搬回原位 A 格；
      过统一"未就绪"闸门（伺服未上电 → 409 统一文案）与人机互斥。
    · 空跑局 → **本局没有真实搬动过棋子**（没实体可搬）→ 只清棋盘并**如实说明**，
      不假装搬过（本项目铁律：不知道/没做就说不知道）。
    """
    if speed_pct is not None:
        _set_state(st, speed_pct=int(speed_pct))          # 用"此刻"的右上角速度
    if bool(st.get("dry_run")):
        st.update({"board": TT.new_board(), "moves": [], "used": [],
                   "result": "", "win_line": [], "voided": False, "piece_no": 0,
                   "pending": []})
        emit_event("tictactoe", "info", "tictactoe.restore_skip",
                   "井字棋复原：本局是空跑（没有真实搬动过棋子），只清空棋盘",
                   {"dry_run": True}, actor="control")
        return {"ok": True, "dry_run": True}
    # ★ 真实复原 = 真实下发 → 伺服没上电一律拒绝（与 /start 同一文案）
    reject_if_not_ready(dry=False)
    _reject_if_run_active("井字棋复原")
    pmap = _load_pmap()
    ctx = new_run("井字棋复原", kind="tictactoe")
    with _gate:
        _job["ctx"] = ctx
        th = threading.Thread(target=_restore_run, args=(ctx, st, pmap),
                              name="tictactoe-restore", daemon=True)
        _job["thread"] = th
    th.start()
    ctx.set_thread(th)     # ★ 绑定执行线程：让"卡死登记"能被 is_alive() 精确识别
    emit_event("tictactoe", "info", "tictactoe.restore_start",
               "井字棋复原（真机）：把 %d 颗棋子逐个搬回 A 区原位" % len(st.get("moves") or []),
               {"dry_run": False, "pieces": len(st.get("moves") or [])}, actor="control")
    return {"ok": True, "run_id": ctx.run_id, "dry_run": False}


@router.post("/reset")
def api_reset(body: Optional[ResetIn] = None, tok: str = Depends(require_control)):
    """★「复原」：把 B 区已落的子**按落地逆序逐个搬回各自原位 A 格**。

    ★ 用户口径（2026-10-11）：「复原和复盘合并，就是复原，**不用空跑啊，要真机移动**」——
      真实局**真机搬回**（见 `_launch_restore`）；空跑局没有实体可搬，只清棋盘并明说。
    ★ 进行中先「停止」。停止沿用 `/tictactoe/stop`（步边界停止），不新造语义。
    """
    with _gate:
        th = _job.get("thread")
        st = _job.get("state")
    if th and th.is_alive():
        raise HTTPException(409, detail="对局/复原进行中，先「停止」再复原")
    if not isinstance(st, dict) or not st.get("moves"):
        raise HTTPException(400, detail="没有可复原的棋子")
    return _launch_restore(st, body.speed_pct if body is not None else None)


@router.get("/replay")
def api_replay(tok: str = Depends(require_control)):
    """**棋谱数据**：只读返回 moves/result/win_line（前端按序做 2D 回放查看，
    **本接口不动机器人** —— 本项目的"真实复原"是 `POST /reset`）。"""
    with _gate:
        st = _job.get("state")
    if not isinstance(st, dict) or not st.get("moves"):
        raise HTTPException(404, detail="没有可复盘的棋局")
    return {"ok": True,
            "moves": copy.deepcopy(st.get("moves") or []),
            "result": st.get("result") or "",
            "win_line": st.get("win_line") or [],
            "user_sym": st.get("user_sym"), "robot_sym": st.get("robot_sym"),
            "first": st.get("first")}


class ReplayIn(BaseModel):
    # ★ 复原也沿用右上角那份全局速度（客户端可带；不带就用本局原速度）
    speed_pct: Optional[int] = Field(default=None, ge=5, le=100)


@router.post("/replay")
def api_replay_run(body: Optional[ReplayIn] = None, tok: str = Depends(require_control)):
    """★ **兼容别名**：本项目的"真实复盘"已并入「复原」（`POST /reset`），
    两者**同一实现**（`_launch_restore` → `_restore_run`）—— 界面上已无「复盘」入口。

    保留本路由只为旧的脚本/调用方不 404；**不做第二份实现**。
    """
    with _gate:
        th = _job.get("thread")
        st = _job.get("state")
    if th and th.is_alive():
        raise HTTPException(409, detail="对局/复原进行中，先「停止」")
    if not isinstance(st, dict) or not st.get("moves"):
        raise HTTPException(400, detail="没有可复原的棋子（先下完一局）")
    return _launch_restore(st, body.speed_pct if body is not None else None)


# ---------------------------------------------------------------- 工具
def _load_pmap() -> Dict[str, object]:
    db = SessionLocal()
    try:
        return {p.name: p for p in list_points(db)}
    finally:
        db.close()
