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
                             request_cancel)
from app.api.sorting import (_Stop, _air, _capture, _move,  # 复用同一套原语
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
SUCK_HOLD_MS = 500      # 吸牢再抬（与分拣同口径，2026-10-10 起 0.5 秒）
BLOW_MS = 500           # 吹气时长
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
               min_conf: float = 0.3, symbol_color: Optional[Dict[str, str]] = None) -> Dict:
    """一局全新的 state（每次「开始」都重建，绝不继承上一局残留）。"""
    sc = dict(SYMBOL_COLOR)
    sc.update({k: v for k, v in (symbol_color or {}).items() if v in ALLOWED_COLORS})
    return {
        "dry_run": bool(dry),
        "difficulty": difficulty,
        "speed_pct": int(speed_pct),      # ★ 本局速度（= 右上角全局速度）
        # ★ 两种模式（用户 2026-10-10）：默认模式不判色；自证模式取件后拍照核对颜色
        "verify_color": bool(verify_color),
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
        "error": "", "finished": False, "ok": None,
    }


def _snap(st: Dict) -> Dict:
    """启动响应回**线程启动前的快照**（别把活动字典交出去）。"""
    return copy.deepcopy(st)


# ---------------------------------------------------------------- 搬运原语
def _carry(ctx, pmap, st, dry, src_slot: str, dst_slot: str, who_cn: str,
           via_start: bool = False, verify: bool = False, expect_color: str = "",
           min_conf: float = 0.3) -> None:
    """把一件从 src_slot 搬到 dst_slot（= 分拣搬一件的**精简版**：无判色、无 C 检测）。

    取料：src上方 → src → 吸 → 等吸牢 → 抬起
    ★ 搬运行程（用户口径 2026-10-10）：**不许「正上方 → 正上方」直接平移**。
      A 区取到件抬起后，先回 **开始/结束点（起点）**，再从起点去 B 区正上方落子 ——
      避免抬着件在 A/B 两个"正上方"之间横扫（会扫过托盘/两侧工装）。
      `via_start=True` 时启用（下棋 A→B 用；复位 B→A 保持原路，见 _reset_run）。
    落子：dst上方 → 停止吸 → 放气 → 等 → 停止放气 → 回起点

    ★ 速度一律取 `st["speed_pct"]`（= 右上角全局速度）——
      不在这里另立参数，避免"两条路各写一份速度"再出偏差。
    """
    speed = int(st.get("speed_pct") or DEFAULT_SPEED_PCT)
    if ctx.wait_pause():
        raise _Stop("已被停止", "暂停")
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·取料上方" % who_cn, st)
    _move(ctx, pmap, src_slot, speed, dry, "%s·下落取料" % who_cn, st)
    _air(ctx, "suck", dry, "%s·吸" % who_cn, st)
    if SUCK_HOLD_MS > 0:
        ctx.sleep(SUCK_HOLD_MS / 1000.0)
    _set_state(st, holding=True)
    _move(ctx, pmap, src_slot + "上", speed, dry, "%s·抬起" % who_cn, st)
    if verify and not dry:
        # ★★ 拍照自证（用户 2026-10-10 口径 · 两种模式里的"自证"那一种）：
        #    抬起后先去 C 检测位拍 3 帧 → 投票判色。
        #    · 判不出色      → **原路放回取料格 + 停止**（与分拣同一条统一口径）
        #    · 判出但不是该格应有的颜色 → 同样放回 + 停止，但文案点明"不符"
        #      （两句话必须分清，否则现场会往错的方向查：一个查相机、一个查装料）
        _move(ctx, pmap, S.CAMERA_POINT, speed, dry, "%s·送检" % who_cn, st)
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
                      prefix="放回原位 %s" % src_slot, st=st)
            raise _Stop("颜色无法识别（%s），已放回原位 %s"
                        % (v.get("reason") or "无有效判定", src_slot), "自证")
        if expect_color and v.get("color") != expect_color:
            _set_state(st, stage="自证不符·放回原位")
            _put_back(ctx, pmap, src_slot, speed, dry, blow_ms=BLOW_MS,
                      prefix="放回原位 %s" % src_slot, st=st)
            raise _Stop("自证不符：该格应为 %s，实拍 %s，已放回原位 %s"
                        % (expect_color, v.get("color"), src_slot), "自证")
        _set_state(st, stage="自证通过")
    if via_start:
        # ★ 经起点再过去：抬起后不直接横移到 dst 正上方，先回原点把"抬着件横扫"这段消掉
        _move(ctx, pmap, S.START_POINT, speed, dry, "%s·经起点" % who_cn, st)
    _move(ctx, pmap, dst_slot + "上", speed, dry, "%s·落子" % who_cn, st)
    _air(ctx, "release", dry, "%s·停止吸" % who_cn, st)
    _air(ctx, "blow", dry, "%s·放气" % who_cn, st)
    if BLOW_MS > 0:
        ctx.sleep(BLOW_MS / 1000.0)
    _air(ctx, "unblow", dry, "%s·停止放气" % who_cn, st)
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
                  "by": "user" if sym == st["user_sym"] else "robot", "src": src})
    _set_state(st, piece_no=len(moves))


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
    difficulty = st.get("difficulty") or "normal"
    speed = int(st.get("speed_pct") or DEFAULT_SPEED_PCT)   # ★ 右上角全局速度
    board: List[Optional[str]] = st["board"]
    user_sym = st["user_sym"]
    robot_sym = st["robot_sym"]
    turn = st["first"]
    phase, stop_reason = "done", ""
    try:
        _set_state(st, stage="回开始/结束点", turn="none")
        _move(ctx, pmap, S.START_POINT, speed, dry, "回开始/结束点", st)
        while True:
            if ctx.cancelled():
                stop_reason, phase = "已被停止", "cancelled"
                break
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
                       ok=(phase == "done"), phase=phase, turn="none", holding=False)
            emit_event("tictactoe", "info" if phase == "done" else "warn", "tictactoe.stop",
                       ("井字棋完成：%s" % st.get("result") or "已结束") if phase == "done"
                       else ("井字棋结束：%s" % stop_reason),
                       {"phase": phase, "result": st.get("result"), "dry_run": dry,
                        "moves": len(st.get("moves") or [])}, actor="control")
        except Exception:  # noqa: BLE001
            log.exception("井字棋收尾异常（运行登记已注销，不影响后续指令）")


# ---------------------------------------------------------------- 复位线程
def _reset_run(ctx, st: Dict, pmap: Dict[str, object]) -> None:
    """把 B 区已落的子**逆向搬回**各自来源 A 格（LIFO，避免互相碰撞）。"""
    phase, stop_reason = "done", ""
    try:
        moves = list(st.get("moves") or [])
        for i, mv in enumerate(reversed(moves), start=1):
            if ctx.cancelled():
                stop_reason, phase = "已被停止", "cancelled"
                break
            _set_state(st, stage="复位 %d/%d" % (i, len(moves)), piece_no=len(moves) - i)
            cell = int(mv.get("cell"))
            src = str(mv.get("src") or "")
            _carry(ctx, pmap, st, True, TT.cell_to_slot(cell), src, "复位")
        if phase == "done":
            st.update({"board": TT.new_board(), "moves": [], "used": [],
                       "result": "", "win_line": [], "voided": False, "piece_no": 0})
    except _Stop as e:
        stop_reason, phase = e.reason, "cancelled" if "停止" in e.reason else "error"
    except Exception as e:  # noqa: BLE001
        log.exception("井字棋复位异常")
        stop_reason, phase = "内部异常：%s" % str(e)[:150], "error"
    finally:
        # ★★ 2026-10-10：**注销运行登记最先做且不可跳过**（与 ai.py 同名死机修复同因）
        ctx.finish(phase)
        try:
            _set_state(st, stage="复位结束", error=stop_reason, finished=True,
                       ok=(phase == "done"), phase=phase, turn="none")
            emit_event("tictactoe", "info" if phase == "done" else "warn", "tictactoe.reset",
                       "井字棋复位%s%s" % ("完成" if phase == "done" else "结束",
                                         "" if phase == "done" else "：" + stop_reason),
                       {"phase": phase}, actor="control")
        except Exception:  # noqa: BLE001
            log.exception("井字棋复位收尾异常（运行登记已注销，不影响后续指令）")


# ---------------------------------------------------------------- 接口
class StartIn(BaseModel):
    difficulty: str = Field(default="normal", max_length=16)
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
    # ★ 自证只在"真的要判色"时才要求相机：空跑 / 双闸未开时物体到不了 C 区，
    #   判色必然"检测不到" —— 拿相机当门槛只会挡住空跑验证（与分拣同一条口径）。
    need_verify = bool(body.verify_color) and (not body.dry_run) and real_write_enabled()

    db = SessionLocal()
    try:
        rows = list_points(db)
        pmap = {p.name: p for p in rows}
    finally:
        db.close()
    # ★ 自证模式比默认模式**多要一个 C 检测位**（要送检拍照）
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
    first = random.choice(["user", "robot"])
    # 先手执圆：先手 = O，后手 = X
    user_sym, robot_sym = (TT.O, TT.X) if first == "user" else (TT.X, TT.O)
    st = _new_state(body.dry_run, difficulty, user_sym, robot_sym, first,
                    int(body.speed_pct), verify_color=bool(body.verify_color),
                    min_conf=min_conf, symbol_color=body.colors)
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
               "开始井字棋（%s，难度 %s，%s，先手 %s，速度 %d%%）" % (
                   "空跑" if body.dry_run else "真实下发", TT.diff_cn(difficulty),
                   "拍照自证" if body.verify_color else "默认模式",
                   "你" if first == "user" else "机器人", int(body.speed_pct)),
               {"difficulty": difficulty, "first": first, "dry_run": body.dry_run,
                "speed_pct": int(body.speed_pct), "verify_color": bool(body.verify_color)},
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
    # ★ 复位也沿用右上角那份全局速度（客户端可带；不带就用本局原速度）
    speed_pct: Optional[int] = Field(default=None, ge=5, le=100)


@router.post("/reset")
def api_reset(body: Optional[ResetIn] = None, tok: str = Depends(require_control)):
    """复位：把 B 区已落的子搬回对应 A 格。进行中先「停止」。"""
    with _gate:
        th = _job.get("thread")
        st = _job.get("state")
    if th and th.is_alive():
        raise HTTPException(409, detail="对局进行中，先「停止」再复位")
    if not isinstance(st, dict) or not st.get("moves"):
        raise HTTPException(400, detail="没有可复位的棋子")
    if body is not None and body.speed_pct is not None:
        _set_state(st, speed_pct=int(body.speed_pct))     # 用"此刻"的右上角速度
    if st.get("dry_run"):
        # 空跑：没真动过，直接清棋盘
        st.update({"board": TT.new_board(), "moves": [], "used": [],
                   "result": "", "win_line": [], "voided": False, "piece_no": 0})
        return {"ok": True, "dry_run": True}
    _reject_if_run_active("井字棋复位")
    pmap = _load_pmap()
    ctx = new_run("井字棋复位", kind="tictactoe")
    with _gate:
        _job["ctx"] = ctx
        th2 = threading.Thread(target=_reset_run, args=(ctx, st, pmap),
                               name="tictactoe-reset", daemon=True)
        _job["thread"] = th2
    th2.start()
    ctx.set_thread(th2)     # ★ 绑定执行线程：让"卡死登记"能被 is_alive() 精确识别
    emit_event("tictactoe", "info", "tictactoe.reset_start",
               "井字棋复位：把 %d 颗棋子搬回 A 区" % len(st.get("moves") or []),
               {}, actor="control")
    return {"ok": True, "run_id": ctx.run_id}


@router.get("/replay")
def api_replay(tok: str = Depends(require_control)):
    """复盘数据：**只有棋谱**（前端按序做 2D 回放，不动机器人）。"""
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


# ---------------------------------------------------------------- 工具
def _load_pmap() -> Dict[str, object]:
    db = SessionLocal()
    try:
        return {p.name: p for p in list_points(db)}
    finally:
        db.close()
