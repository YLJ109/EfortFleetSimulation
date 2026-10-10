// =====================================================================
// Pinia store：井字棋对弈（机器人真实落子）—— 前端唯一状态出口
//
// 方案：docs/方案-井字棋对弈（机器人真实落子）.md
// 后端：/api/tictactoe/{start,state,move,stop,reset,replay}
//       暂停/继续复用 /control/run-pause | run-resume（不新建通道）
//
// ★ 四条不绕过的口径（与 stores/ai.js 一致）：
//   1. 全部请求走 apiControl（带控制令牌）—— 井字棋**不是权限后门**；
//      未授权时**不发请求**（先 requestLogin），避免 401 刷屏。
//   2. 后端 state 是**内存态**（保留上一次对局的 error/stage）—— 页面刷新后
//      **首轮静默**：只对齐日志游标，不回放旧 error/阶段（照 ai.js 的 poll(silent)）。
//   3. 运行日志写进全局 exec.log（左下角浮动面板统一显示），只在阶段/结果/错误
//      变化时写一行 —— 轮询很密，逐帧记会刷屏。
//   4. 用户赢 → 语音播报祝贺语**一次**（在结果变化处触发，天然只播一次）。
// =====================================================================
import { defineStore } from "pinia";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "./auth.js";
import { useExecStore } from "./exec.js";
import { speak } from "../ai/speak.js";
import { newBoard, diffCn, resultCn } from "../games/tictactoe.js";
import { setTttBoardState } from "../three/manager.js";

/** 轮询周期（ms）：够快看出"轮到谁/机器人在搬"，又不把后端打满。 */
export const TTT_POLL_MS = 800;

// 定时器放模块级（与 stores/sorting3d.js 同做法）：避免塞进响应式 state。
let pollTimer = null;
let replayTimer = null;

/** 统一取后端错误文案（异常体是 {code,message,detail}，message 可能是对象）。 */
function errMsg(d, status) {
  const m = d && (d.message || d.detail || d.error);
  if (typeof m === "string" && m) return m;
  if (m && typeof m === "object") return m.message || JSON.stringify(m);
  return "HTTP " + status;
}

export const useTicTacToeStore = defineStore("tictactoe", {
  state: () => ({
    st: null,             // 后端 state（可能为 null）
    running: false,       // 是否有对局在跑
    paused: false,        // 暂停态（来自 state.paused，省一次 /control/run-state 请求）
    err: "",              // 后端 error/message（**原样展示，不美化**）
    msg: "",              // 操作提示
    busy: false,
    // 界面本地两档（开始前可改；开局后卡片锁定）
    difficulty: "normal",
    dryRun: true,         // ★ 默认空跑（与全项目"默认空跑"安全口径一致）
    // ★ 两种模式（2026-10-10 用户口径）：false = 默认（不判色、不去 C 位）；
    //   true = 拍照自证（取件后到 C 位拍照核对颜色，不符/判不出 → 放回原位并停）
    verifyColor: false,
    // 复盘（仅 2D 回放，不动机器人）
    replay: null,
    replayIdx: 0,
    replayPlaying: false,
    _lastLogKey: "",
    _logPrev: {},
  }),

  getters: {
    /** 是否有对局相关显示（棋盘浮层据此显隐：无对局且未开始时不显示）。 */
    gameVisible: (s) => !!s.st || !!s.replay,
    inReplay: (s) => !!s.replay,
    /** 当前该显示的棋盘（复盘态 = 按复盘进度重建，否则 = 后端棋盘）。 */
    displayBoard: (s) => {
      if (s.replay) {
        const b = newBoard();
        const moves = s.replay.moves || [];
        for (let i = 0; i < s.replayIdx && i < moves.length; i++) {
          const m = moves[i];
          if (m && Number.isInteger(m.cell)) b[m.cell] = m.sym;
        }
        return b;
      }
      return s.st && Array.isArray(s.st.board) ? s.st.board : newBoard();
    },
    turn: (s) => (s.st && s.st.turn) || "none",
    result: (s) => (s.st && s.st.result) || "",
    winLine: (s) => (s.st && Array.isArray(s.st.win_line) ? s.st.win_line : []),
    userSym: (s) => (s.st && s.st.user_sym) || "O",
    robotSym: (s) => (s.st && s.st.robot_sym) || "X",
    finished: (s) => !!(s.st && s.st.finished),
    /** 本局速度（= 右上角全局速度）；开局前为 0。界面显示用。 */
    speedPct: (s) => (s.st && s.st.speed_pct) || 0,
    /** 现在能否落子：运行中 + 轮到用户 + 非暂停 + 非忙 + 非复盘。 */
    canPlay: (s) => s.running && s.turn === "user" && !s.paused && !s.busy && !s.replay,
  },

  actions: {
    /** 令牌门控：未授权时不发请求，弹登录并给明确说明。 */
    _authed() {
      const auth = useAuthStore();
      if (!auth.controlActive) {
        if (auth.requestLogin) auth.requestLogin();
        this.err = "需要控制权限（井字棋与其它页同一套令牌，不是权限后门）";
        return false;
      }
      return true;
    },

    /** 把当前棋盘喂给 3D（B 区按格子显隐、A 区按已取用隐藏）。3D 未装填时空操作。 */
    _sync3d() {
      const s = this.st;
      if (!s) { setTttBoardState(null); return; }
      setTttBoardState({
        board: Array.isArray(s.board) ? s.board : null,
        used: Array.isArray(s.used) ? s.used : [],
        holding: !!s.holding,
      });
    },

    /** 状态变化才写日志/播报（silent=true 时只对齐游标）。 */
    _logTick(silent = false) {
      const s = this.st;
      if (!s) return;
      const key = [s.stage, s.turn, s.piece_no, s.result, s.error,
                   s.finished, s.voided, this.paused].join("|");
      if (key === this._lastLogKey) return;
      this._lastLogKey = key;
      const snap = { stage: s.stage, turn: s.turn, piece_no: s.piece_no,
                     result: s.result, error: s.error, finished: s.finished };
      if (silent) { this._logPrev = snap; return; }
      const exec = useExecStore();
      const prev = this._logPrev || {};
      if (s.stage && s.stage !== prev.stage) {
        exec.logLine("info", "[井字棋] 阶段：" + s.stage);
      }
      if (s.result && s.result !== prev.result) {
        exec.logLine("ok", "[井字棋] 结果：" + resultCn(s.result));
        // ★ 用户赢 → 祝贺语只播一次（结果签名变化处触发，天然去重）
        if (s.result === "user_win") speak("恭喜你赢了，真棒，是个好榜样！");
      }
      if (s.error && s.error !== prev.error) {
        exec.logLine("err", "[井字棋] 已停止：" + s.error);
      } else if (s.finished && !prev.finished && !s.result) {
        exec.logLine(s.ok ? "ok" : "warn", "[井字棋] 本局结束");
      }
      this._logPrev = snap;
    },

    /** 拉一次对局进度。silent=true：首轮只对齐游标（不回放旧状态）。 */
    async poll(silent = false) {
      const auth = useAuthStore();
      if (!auth.controlActive) return;
      try {
        const r = await apiControl("/tictactoe/state");
        if (!r.ok) return;
        const d = await r.json();
        this.running = !!d.running;
        this.st = d.state || null;
        this.paused = !!(d.state && d.state.paused);
        this._logTick(silent);
        this._sync3d();
      } catch (e) { /* 轮询失败不影响流程 */ }
    },

    /** 进入分拣页时启动轮询（未授权不发；令牌到手由组件 watch 兜底拉起）。 */
    async startPolling() {
      if (!useAuthStore().controlActive) return;
      if (pollTimer) return;
      await this.poll(true);   // ★ 首轮静默对齐：不回放上一次对局的旧 error/阶段
      pollTimer = setInterval(() => this.poll(false), TTT_POLL_MS);
    },
    stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    },

    /** 开始一局。difficulty/dryRun 由卡片传入（空跑默认 true）。 */
    async start(difficulty, dryRun) {
      if (!this._authed()) return false;
      this.busy = true; this.err = ""; this.msg = "";
      if (difficulty) this.difficulty = difficulty;
      this.dryRun = !!dryRun;
      this.stopReplay();
      try {
        const r = await apiControl("/tictactoe/start", {
          method: "POST", headers: { "Content-Type": "application/json" },
          // ★ 速度沿用**右上角那份全局速度**（用户口径："右上角多少，机器人速度多少"）
          body: JSON.stringify({ difficulty: this.difficulty, dry_run: this.dryRun,
                                 speed_pct: useExecStore().speed,
                                 verify_color: this.verifyColor }),
        });
        if (r.status === 401 || r.status === 403) { this._needAuth(); return false; }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) {
          this.err = errMsg(d, r.status);
          useExecStore().logLine("err", "[井字棋] 开局被拒：" + this.err);
          return false;
        }
        this.st = d.state || null;
        this.running = true;
        this.paused = false;
        this._lastLogKey = ""; this._logPrev = {};
        this._logTick(false);
        this._sync3d();
        const first = this.st && this.st.first === "user" ? "你" : "机器人";
        this.msg = (this.dryRun ? "已开局（空跑，不动机器人）" : "已开局（真实下发）")
          + " · 先手 " + first;
        useExecStore().logLine("info",
          "[井字棋] 开局 · 难度 " + diffCn(this.difficulty) + " · "
          + (this.dryRun ? "空跑" : "真实下发") + " · 先手 " + first);
        return true;
      } catch (e) {
        this.err = "开局异常：" + ((e && e.message) || e);
        return false;
      } finally { this.busy = false; }
    },

    /** 用户落子（仅「轮到你」时可下，后端也会二次校验）。 */
    async move(cell) {
      if (!this._authed()) return false;
      if (!this.running) { this.err = "对局未开始"; return false; }
      if (this.paused) { this.err = "已暂停，先「继续」"; return false; }
      if (this.turn !== "user") { this.err = "现在是机器人的回合，稍候"; return false; }
      this.busy = true; this.err = "";
      try {
        const r = await apiControl("/tictactoe/move", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cell: Number(cell) }),
        });
        if (r.status === 401 || r.status === 403) { this._needAuth(); return false; }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) { this.err = errMsg(d, r.status); return false; }
        await this.poll(false);      // 立刻刷新（不等下一个轮询周期）
        return true;
      } catch (e) {
        this.err = "落子失败：" + ((e && e.message) || e);
        return false;
      } finally { this.busy = false; }
    },

    /** 停止 = 作废本局（棋盘保留实际已落的子，可再点「复位」搬回 A 区）。 */
    async stop() {
      if (!this._authed()) return false;
      this.busy = true; this.err = "";
      try {
        const r = await apiControl("/tictactoe/stop", { method: "POST" });
        if (r.status === 401 || r.status === 403) { this._needAuth(); return false; }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) { this.err = errMsg(d, r.status); return false; }
        this.msg = "已请求停止（作废本局；棋子留在棋盘上，可点「复位」搬回 A 区）";
        useExecStore().logLine("warn", "[井字棋] 已请求停止（作废本局）");
        await this.poll(false);
        return true;
      } catch (e) {
        this.err = "停止异常：" + ((e && e.message) || e);
        return false;
      } finally { this.busy = false; }
    },

    /** 复位：把 B 区棋子搬回对应 A 格（空跑则直接清棋盘）。进行中会 409。 */
    async reset() {
      if (!this._authed()) return false;
      this.busy = true; this.err = "";
      try {
        const r = await apiControl("/tictactoe/reset", {
          method: "POST", headers: { "Content-Type": "application/json" },
          // ★ 复位也用"此刻"的右上角速度（中途调过滑块也算数）
          body: JSON.stringify({ speed_pct: useExecStore().speed }),
        });
        if (r.status === 401 || r.status === 403) { this._needAuth(); return false; }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) { this.err = errMsg(d, r.status); return false; }
        this.stopReplay();
        this.msg = d.dry_run ? "已复位（空跑：直接清空棋盘）" : "已开始把棋子搬回 A 区";
        useExecStore().logLine("info", "[井字棋] " + this.msg);
        await this.poll(false);
        return true;
      } catch (e) {
        this.err = "复位异常：" + ((e && e.message) || e);
        return false;
      } finally { this.busy = false; }
    },

    /** 暂停 / 继续（复用全局 /control/run-pause|run-resume）。 */
    async _setPause(want) {
      if (!this._authed()) return false;
      this.busy = true; this.err = "";
      try {
        const path = want ? "/control/run-pause" : "/control/run-resume";
        const r = await apiControl(path, { method: "POST" });
        if (r.status === 401 || r.status === 403) { this._needAuth(); return false; }
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          this.paused = !!want;
          if (want) this.msg = "已请求暂停（步边界生效，原地冻结可继续）";
          else this.msg = "";
          useExecStore().logLine(want ? "warn" : "ok",
            want ? "[井字棋] 已请求暂停" : "[井字棋] 已继续");
        } else {
          this.err = d.error || ("HTTP " + r.status);
          if (!want) this.paused = false;   // 后端说没有暂停态 → 界面对齐
        }
        await this.poll(false);
        return true;
      } catch (e) {
        this.err = "暂停/继续异常：" + ((e && e.message) || e);
        return false;
      } finally { this.busy = false; }
    },
    async pause() { return this._setPause(true); },
    async resume() { return this._setPause(false); },
    async togglePause() { return this._setPause(!this.paused); },

    /** 令牌被后端判无效时的统一处理。 */
    _needAuth() {
      const auth = useAuthStore();
      if (auth.requestLogin) auth.requestLogin();
      this.err = "控制令牌已失效，请重新验证";
    },

    /** 载入复盘棋谱（仅终局后有数据；只放 2D，不动机器人）。 */
    async loadReplay() {
      if (!this._authed()) return false;
      this.err = "";
      try {
        const r = await apiControl("/tictactoe/replay");
        if (r.status === 401 || r.status === 403) { this._needAuth(); return false; }
        if (r.status === 404) { this.err = "没有可复盘的棋局"; return false; }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) { this.err = errMsg(d, r.status); return false; }
        this.replay = d;
        this.replayIdx = (d.moves || []).length;   // 默认停在终局（看最终结果）
        return true;
      } catch (e) {
        this.err = "复盘加载失败：" + ((e && e.message) || e);
        return false;
      }
    },

    /** 复盘步进（±1 手），夹在 [0, 总手数]。 */
    stepReplay(delta) {
      if (!this.replay) return;
      const n = (this.replay.moves || []).length;
      let i = this.replayIdx + Number(delta || 0);
      if (i < 0) i = 0;
      if (i > n) i = n;
      this.replayIdx = i;
    },

    /** 自动播放（约 0.9s 一步，播完自动停）。 */
    playReplay() {
      if (!this.replay || this.replayPlaying) return;
      const n = (this.replay.moves || []).length;
      if (this.replayIdx >= n) this.replayIdx = 0;
      this.replayPlaying = true;
      replayTimer = setInterval(() => {
        if (this.replayIdx < n) this.replayIdx += 1;
        else this._stopPlayTimer();
      }, 900);
    },
    _stopPlayTimer() {
      if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
      this.replayPlaying = false;
    },
    /** 退出复盘：停自动播放并清空复盘数据（回到实际棋盘）。 */
    stopReplay() {
      this._stopPlayTimer();
      this.replay = null;
      this.replayIdx = 0;
    },
  },
});
