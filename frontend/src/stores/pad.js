// =====================================================================
// Pinia store：手柄执行页（2D 选点 + 单次气路）—— 前端唯一状态出口
//
// 方案：docs/方案-手柄执行页（2D选点+气路+井字棋）.md
//
// ★ 本 store 只做三件事：
//   ① 记「当前 tab / 光标选中的点 / 手柄是否已启动（armed）」；
//   ② 把**手柄动作**翻译成对已有接口的调用（不新建任何后端接口）；
//   ③ 把结果如实写进全局运行日志（exec.log），不自己另起一套提示。
//
// ★ 四条不绕过的口径：
//   1. 真实下发走 `apiControl`（带控制令牌）—— 本页**不是权限后门**；
//   2. **Start = 总闸（armed）**：默认落锁，`A/Y/X/B/LB/RB` 一律不动作；
//      只有方向键（纯前端选点，不动机器人）永远可用。用户口径：「通过 Start 启动和关闭」。
//   3. 去点用 `POST /sorting/goto` —— 原料位由后端 `face_for_target` 自动 `keep`、
//      **绝不翻面**；★ 2026-10-12 起 **未就绪在前端就拦**（`_gate` 见 `rc.ready`）+
//      后端统一闸门（409）双保险，不再"按了才被拒"。
//      ★ LB/RB 去的 `J6正面` / `J6反面` 是**虚拟切面命令**（2026-10-12 终版，不读库：
//      基姿态 = 当前实时关节，**只覆盖 J6**，J1~J5 不动；正面 = 该点自身录制 J6、
//      反面 = 当前 J6 − 180，自动折算）—— 与"整姿态到点"不是一回事，且**无需示教**。
//   4. 气路用 `exec.vacuum()`（**唯一咽喉**：分面锁存 + 停止类请求排队不丢弃 + 日志）。
//      本页**不再**自己 fetch `/control/vacuum`，免得两处口径分叉。
// =====================================================================
import { defineStore } from "pinia";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "./auth.js";
import { useExecStore } from "./exec.js";
import { useRcReadyStore, NOT_READY_HINT } from "./rcReady.js";
import {
  CURSOR_HOME, CAMERA, abovePoint, isSlot, longLabel, moveCursor,
} from "../pad/nav.js";

/** 页内两个 tab。 */
export const PAD_TAB_EXEC = "exec";
export const PAD_TAB_TTT = "ttt";

/** J6 原地切面点位名 —— 必须与 `backend/app/services/sorting.py::OPTIONAL_EXTRA_POINTS` 完全一致。
 *  ★ 它们**只有 J6 不同**、且是在起点录的 → 后端会把它转成"原地只改 J6"（J1~J5 不动）。 */
export const FACE_POINT_FRONT = "J6正面";
export const FACE_POINT_BACK = "J6反面";

/** 统一取后端错误文案（异常体是 {code,message,detail}，message 可能是对象）。 */
function errText(d, status) {
  const m = d && (d.message || d.detail || d.error);
  if (typeof m === "string" && m) return m;
  if (m && typeof m === "object") return m.message || JSON.stringify(m);
  return "HTTP " + status;
}

export const usePadStore = defineStore("pad", {
  state: () => ({
    tab: PAD_TAB_EXEC,        // exec | ttt
    cursor: CURSOR_HOME,      // 当前选中的点名（20 点之一）
    armed: false,             // ★ Start 总闸：默认落锁（真机动作一律不响应）
    busy: "",                 // 正在下发的点名（按钮禁用用）
    msg: "",                  // 操作提示（成功/进行中）
    err: "",                  // 错误提示（原样展示，不美化）
  }),

  getters: {
    /** 当前选中点的显示名。 */
    cursorLabel: (s) => longLabel(s.cursor),
    /** 当前选中点是否有「正上位」（只有 A/B 槽位有）。 */
    hasAbove: (s) => !!abovePoint(s.cursor),
    /** 选中点的类型说明（面板显示，别让操作员猜）。 */
    cursorKind: (s) => {
      if (s.cursor === CAMERA) return "摄像头检测位";
      if (!isSlot(s.cursor)) return "起点（开始/结束点）";
      return s.cursor[0] === "A" ? "A 原料区 · 第 " + s.cursor[1] + " 格" : "B 目标区 · 第 " + s.cursor[1] + " 格";
    },
  },

  actions: {
    setTab(t) {
      this.tab = (t === PAD_TAB_TTT) ? PAD_TAB_TTT : PAD_TAB_EXEC;
      this.err = "";
    },

    /** 光标复位到起点（进页面 / 离开页面时调用）。 */
    resetCursor() {
      this.cursor = CURSOR_HOME;
    },

    /** 真机动作总闸：离开本页 / 切到井字棋 tab 时**必须落锁**（安全）。 */
    disarm(reason = "") {
      if (!this.armed) return;
      this.armed = false;
      this.msg = reason || "手柄已关闭（真机动作已落锁）";
    },

    /** Start：启动 ⇄ 关闭（toggle）。 */
    toggleArmed() {
      const auth = useAuthStore();
      if (!this.armed) {
        if (!auth.controlActive) {
          this.err = "需要控制权限后才能启动手柄";
          return;
        }
        this.armed = true;
        this.err = "";
        this.msg = "手柄已启动：A 原位 / Y 正上位 / X 吸气 / B 放气 / LB 正面 / RB 反面";
        useExecStore().logLine("warn", "[手柄] 已启动（armed）");
      } else {
        this.armed = false;
        this.msg = "手柄已关闭（真机动作已落锁）";
        useExecStore().logLine("info", "[手柄] 已关闭（disarmed）");
      }
    },

    /**
     * 真机动作闸门：**未授权 / 未启动（未 armed） / 未就绪** 时一律拒绝，并给出明确原因。
     * ★ 方向键（选点）不走这里 —— 它纯前端、不动机器人。
     * ★★ 2026-10-12（用户口径「**手柄的、井字棋的哪些执行按钮也不让点（没有就绪的话）**」）：
     *   新增第三道 —— **完整就绪**（`rc.ready`：伺服 + 210 已运行 + AUTO + 无报警/急停）。
     *   未就绪时机器人不可能动，故 A/Y/X/B/LB/RB 一律拒绝（文案统一用 NOT_READY_HINT）。
     */
    _gate() {
      if (!useAuthStore().controlActive) {
        this.err = "需要控制权限（先请求控制）";
        return false;
      }
      if (!this.armed) {
        this.err = "手柄未启动：先按 Start 启动";
        return false;
      }
      if (!useRcReadyStore().ready) {
        this.err = NOT_READY_HINT;
        return false;
      }
      this.err = "";
      return true;
    },

    /** 方向键：移动 2D 光标（纯前端，不需要 armed / 权限）。 */
    moveDir(dir) {
      const to = moveCursor(this.cursor, dir);
      if (to !== this.cursor) {
        this.cursor = to;
        this.msg = "已选中 " + longLabel(to);
      }
    },

    /** 直接选中某个点（鼠标点格子用；只改选中，不动机器人）。 */
    select(name) {
      if (!name) return;
      this.cursor = name;
      this.msg = "已选中 " + longLabel(name);
    },

    /** A：去选中点的**原位**（槽位本身；原料位由后端 `keep`、绝不翻面）。 */
    async gotoOrig() {
      await this._goto(this.cursor, "原位");
    },

    /** Y：去选中点的**正上位**（`选中点 + "上"`）；非槽位点没有正上位 → 明确拒绝、不乱指。 */
    async gotoAbove() {
      const up = abovePoint(this.cursor);
      if (!up) {
        this.err = "「" + longLabel(this.cursor) + "」没有正上位（只有 A/B 各 9 个槽位才有）";
        return;
      }
      await this._goto(up, "正上位");
    },

    /** 去点统一实现（速度取右上角滑块，全项目唯一速度口径）。 */
    async _goto(name, kind) {
      if (!this._gate()) return;
      this.busy = name;
      this.err = "";
      this.msg = "正在移到「" + name + "」（" + kind + "）…";
      try {
        const r = await apiControl("/sorting/goto", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, speed_pct: useExecStore().speed }),
        });
        const d = await r.json().catch(() => ({}));
        if (r.status === 401 || r.status === 403) {
          useAuthStore().requestLogin && useAuthStore().requestLogin();
          this.err = "控制令牌已失效，请重新验证";
          return;
        }
        if (r.ok) {
          // ★ J6 正/反面是"原地切面"（只改 J6）—— 回执里有 j6_only 标记，文案必须跟着区分，
          //   否则会让人以为"跑到起点去了"（这正是现场踩过的坑）。
          // ★ 2026-10-12：原位→另一原位时后端自动拆"正上位安全路径"，如实显示（不是 bug）。
          this.msg = d.j6_only
            ? ("已在原地切到「" + name + "」（仅 J6，J1~J5 未动）")
            : (Array.isArray(d.path)
               ? ("已到「" + name + "」（经正上位安全路径：" + d.path.join(" → ") + "）")
               : ("已到「" + name + "」（" + kind + "）"));
          useExecStore().logLine("ok",
            d.j6_only ? ("[手柄] 切面 " + name + "（仅 J6）")
                      : ("[手柄] 到点 " + name + "（" + kind + "）"));
        } else {
          this.err = errText(d, r.status);
          useExecStore().logLine("err", "[手柄] 到点失败：" + this.err);
        }
      } catch (e) {
        this.err = "到点异常：" + ((e && e.message) || e);
      } finally {
        this.busy = "";
      }
    },

    /**
     * X：吸气 ⇄ 再按**停止吸气**（toggle）。
     * ★ 走 `exec.vacuum`（唯一咽喉：分面锁存 + 停止类请求排队不丢弃）。
     */
    toggleSuck() {
      if (!this._gate()) return;
      const exec = useExecStore();
      exec.vacuum(exec.vacuumState === "suck" ? "release" : "suck");
    },

    /** B：放气 ⇄ 再按**停止放气**（toggle）。 */
    toggleBlow() {
      if (!this._gate()) return;
      const exec = useExecStore();
      exec.vacuum(exec.vacuumState === "blow" ? "unblow" : "blow");
    },

    /**
     * LB：切 J6 **正面**（原地只改 J6，J1~J5 不动）。
     * ★ 2026-10-12 终版：`J6正面` 是**虚拟切面命令**（按点名驱动、不读点位库）——
     *   基姿态 = 当前实时关节、只覆盖第 6 轴（所以**不会**跑回起点）；
     *   正面 = 该点自身录制的 J6、反面 = 当前 J6 − 180，后端自动折算，**无需示教**。
     */
    async gotoFaceFront() {
      await this._goto(FACE_POINT_FRONT, "J6 正面");
    },

    /** RB：切 J6 **反面**（同上，原地只改 J6）。 */
    async gotoFaceBack() {
      await this._goto(FACE_POINT_BACK, "J6 反面");
    },

    /**
     * ★ 手柄动作统一入口（本页**只在 exec tab** 时订阅）。
     * 动作名沿用 games/gamepad.js 的既有词汇（A=confirm / B=stop / X=pause / Y=replay /
     * LB=l1 / RB=r1 / Start=start），**含义由本页解释**（井字棋 tab 另有一套）。
     */
    onGamepad(action) {
      switch (action) {
        case "up": case "down": case "left": case "right":
          this.moveDir(action); return;
        case "start": this.toggleArmed(); return;
        case "confirm": this.gotoOrig(); return;       // A  → 原位
        case "replay": this.gotoAbove(); return;       // Y  → 正上位
        case "pause": this.toggleSuck(); return;       // X  → 吸气 ⇄ 停止吸气
        case "stop": this.toggleBlow(); return;        // B  → 放气 ⇄ 停止放气
        case "l1": this.gotoFaceFront(); return;       // LB → J6 正面（原地切面）
        case "r1": this.gotoFaceBack(); return;        // RB → J6 反面（原地切面）
        default: return;                                // 本页不接的动作（reset/View 等）一律忽略
      }
    },
  },
});
