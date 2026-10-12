// =====================================================================
// Pinia store：AI 执行页（自然语言/语音 → 解析 → 确认 → 执行）
//
// 方案：docs/方案-点位推导与AI执行页（语音文字控机器人）.md
//
// ★ 三条不绕过的口径：
//   1. 解析（/api/ai/parse）**只解析不执行**；执行必须带同一个 plan_id
//      —— 保证"界面显示的计划"和"真正执行的计划"是同一份。
//   2. 解析失败**不猜**：直接显示后端给的"没听懂"原因（含大模型未配置时的原话）。
//   3. 权限/围栏/轴锁/速度全部走既有通道：本 store 只调 `apiControl`（带令牌），
//      不自己造任何命令字。
// =====================================================================
import { defineStore } from "pinia";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "./auth.js";
import { useExecStore } from "./exec.js";
import { useRcReadyStore } from "./rcReady.js";
import { speak, stageSpeech } from "../ai/speak.js";

export const useAiStore = defineStore("ai", {
  state: () => ({
    text: "",              // 输入框/语音识别落到这里的原文
    asrText: "",           // 语音识别到的原话（单独留一份，便于对照"听到的 vs 改过的"）
    parsed: null,          // /parse 的完整返回（含 plan_id / echo / warnings）
    err: "",               // 解析或执行失败的原因（**原样展示，不美化**）
    busy: false,
    running: false,
    st: null,              // /ai/state 的 state
    // ★ 接管排队信息（执行中来了新指令 → 等旧任务回起点后自动执行）
    queued: null,          // {message, raw} 或 null
    occ: null,             // 本页占位表
    cfg: null,             // /ai/config
    // ★★ 2026-10-09 用户口径："空跑按钮的默认选勾**移除掉**" → 默认**真实下发**。
    //   ⚠ 说「执行」就会直接动机器 —— 这是用户明确要求的行为（他不想每次先空跑）。
    //   「空跑」开关仍在（页面上可勾），需要验证路径时勾上即可。
    dryRun: false,
    autoExec: false,       // 单件明确指令免确认（默认关，现场可开）
    timer: null,
    _lastLogKey: "",
  }),

  getters: {
    /** 这次解析是否可以直接执行（批量/看颜色必须显式确认）。 */
    needConfirm: (s) => {
      const a = s.parsed && s.parsed.plan && s.parsed.plan.action;
      return a === "move_all" || a === "look_color";
    },
    // ★★ 2026-10-12（用户口径「**AI 也不能执行，直接恢复，没有机器人没有就绪就行了**」）：
    //   「执行」是**真实下发** → 除"解析成功 / 未在跑"外，还必须**持有控制令牌**且**完整就绪**
    //   （`rc.ready` = 伺服 + 210 已运行 + AUTO + 无报警/急停）。未就绪时按钮直接灰掉，
    //   不再"按了才被后端 409 拒"（后端统一闸门仍作兜底）。
    canExec() {
      const a = useAuthStore();
      const rc = useRcReadyStore();
      return !!(this.parsed && this.parsed.ok && !this.running && !this.busy
                && a.controlActive && rc.ready);
    },
    echo: (s) => (s.parsed && s.parsed.echo) || null,
    llmOn: (s) => !!(s.cfg && s.cfg.llm_enabled),
  },

  actions: {
    async loadConfig() {
      try {
        const r = await apiControl("/ai/config");
        if (r.ok) this.cfg = await r.json();
      } catch (e) { /* 静默：配置读不到不影响用规则解析 */ }
    },

    /** 语音识别结果落到输入框（保留原话一份，便于对照人工修改）。 */
    setAsrText(t) {
      const s = String(t || "").trim();
      if (!s) return;
      this.asrText = s;
      this.text = s;
    },

    async parse() {
      const txt = (this.text || "").trim();
      if (!txt) { this.err = "先输入或说一条指令"; return null; }
      this.busy = true; this.err = ""; this.parsed = null;
      try {
        const r = await apiControl("/ai/parse", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: txt }),
        });
        if (r.status === 401 || r.status === 403) {
          const auth = useAuthStore();
          if (auth.requestLogin) auth.requestLogin();
          this.err = "需要控制权限（AI 页不是权限后门，与其它页同一套令牌）";
          return null;
        }
        const d = await r.json().catch(() => ({}));
        this.parsed = d;
        if (!d.ok) {
          this.err = d.error || "没听懂这条指令";
          if (d.llm_error) this.err += `（大模型：${d.llm_error}）`;
          return null;
        }
        return d;
      } catch (e) {
        this.err = "解析请求失败：" + ((e && e.message) || e);
        return null;
      } finally { this.busy = false; }
    },

    /**
     * 对话入口（2026-10-10，用户口径："要一个能正常聊天的大模型"）。
     * 后端 /ai/chat：指令先行（→ 返回 mode:"plan"，parsed 与 /parse 同构）；
     * 不是指令 → 大模型自由回话（mode:"chat"，reply）。聊天只许说话，不碰控制。
     * @param {string} text 用户这句话
     * @param {Array<{role:"user"|"assistant",content:string}>} history 最近对话（≤10 条，供大模型连上下文）
     */
    async sendChat(text, history = []) {
      const txt = String(text || "").trim();
      if (!txt) { this.err = "先输入或说一句话"; return null; }
      this.busy = true; this.err = "";
      try {
        const r = await apiControl("/ai/chat", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: txt, history: history.slice(-10) }),
        });
        if (r.status === 401 || r.status === 403) {
          const auth = useAuthStore();
          if (auth.requestLogin) auth.requestLogin();
          this.err = "需要控制权限（AI 页不是权限后门，与其它页同一套令牌）";
          return null;
        }
        const d = await r.json().catch(() => ({}));
        if (!d.ok) {
          this.err = d.detail || d.error || "对话失败";
          return null;
        }
        return d;   // {mode:"plan", parsed} | {mode:"chat", reply}
      } catch (e) {
        this.err = "对话请求失败：" + ((e && e.message) || e);
        return null;
      } finally { this.busy = false; }
    },

    /** 执行（必须带上刚解析出的 plan_id）。 */
    async exec(force = false) {
      const p = this.parsed;
      if (!p || !p.ok || !p.plan_id) { this.err = "没有可执行的计划（先解析）"; return false; }
      if (this.needConfirm && !force) {
        this.err = "这条指令需要点「确认执行」（批量 / 看颜色都是运动）";
        return false;
      }
      const exec = useExecStore();
      this.busy = true; this.err = "";
      try {
        const r = await apiControl("/ai/exec", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            plan_id: p.plan_id, confirm: true,
            dry_run: this.dryRun, speed_pct: exec.speed,
            // ★★ 接管（用户口径"丝滑"）：执行中再来指令时，后端**不拒绝**，
            //   而是排队接替（当前任务到步边界停止 → 回起点 → 自动执行新的）。
            //   显式传更清楚：关掉它就是旧的 409 语义。
            takeover: true,
          }),
        });
        if (r.status === 401 || r.status === 403) {
          const auth = useAuthStore();
          if (auth.requestLogin) auth.requestLogin();
          this.err = "需要控制权限";
          return false;
        }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) {
          const msg = (d && (d.message || d.detail)) || ("HTTP " + r.status);
          this.err = typeof msg === "string" ? msg : JSON.stringify(msg);
          exec.logLine("err", "[AI] 执行被拒：" + this.err);
          return false;
        }
        this.running = true;
        this.st = d.state || null;
        // ★ 排队接替：本次没立刻开跑（等旧任务回起点），如实记下来给界面显示。
        //   ⚠ 不能覆盖 st（旧任务还在跑，进度要照旧显示），也不能把 running 置 false。
        this.queued = d.queued
          ? { message: d.message || "已排队：等当前任务回起点后自动执行", raw: this.text }
          : null;
        // ★ "每次运行必须从头开始"：日志游标归零，本轮从干净的初始状态重新记
        this._lastLogKey = "";
        // ★★ 2026-10-11 修复：空跑/真机口径**以后端回包为准**（流程控制类分支会回 `dry_run`），
        //   不再只看本地开关 —— 否则会出现"界面写真实下发、后端其实在空跑"这种编故事
        //   （此前 AI 的「开始分拣」被后端写死 dry_run=True，日志却说真实下发）。
        const usedDry = (typeof d.dry_run === "boolean") ? d.dry_run : this.dryRun;
        exec.logLine("info", `[AI] 本轮从头开始 · ${usedDry ? "空跑" : "真实下发"}：`
          + ((this.echo && this.echo.detail) || ""));
        return true;
      } catch (e) {
        this.err = "执行请求失败：" + ((e && e.message) || e);
        return false;
      } finally { this.busy = false; }
    },

    async cancel() {
      try {
        await apiControl("/ai/cancel", { method: "POST" });
      } catch (e) { /* 停止失败不静默：写日志 */ }
    },

    async resetOcc() {
      try {
        const r = await apiControl("/ai/occ/reset", { method: "POST" });
        const d = await r.json().catch(() => ({}));
        if (r.ok) this.occ = d.occ || { A: {}, B: {} };
      } catch (e) { /* 静默 */ }
    },

    async poll(silent = false) {
      try {
        const r = await apiControl("/ai/state");
        if (!r.ok) return;
        const d = await r.json();
        this.running = !!d.running;
        this.st = d.state || null;
        this.occ = d.occ || null;
        // ★ 排队信息以后端为准（它是权威）：跑完接力后会自然变 null。
        this.queued = d.queued
          ? { message: `已排队：等当前任务回起点后自动执行「${d.queued.raw}」`,
              raw: d.queued.raw }
          : null;
        this._logTick(silent);
      } catch (e) { /* 轮询失败不影响流程 */ }
    },

    /**
     * 状态变化才写日志（轮询很密，逐帧记会刷屏）。
     * ★ silent（2026-10-09）：启动后的**首轮**轮询只对齐游标、不写日志也不播报 ——
     *   后端 /ai/state 是内存态（保留上一次运行的 error），页面刷新后首轮若照常
     *   记日志，就会"开机凭空出现一句 [AI] 已停止：检测不到"（用户没执行也被吓一跳）。
     *   之后真正的状态变化仍照常记录。
     */
    _logTick(silent = false) {
      const s = this.st;
      if (!s) return;
      // ★★ 2026-10-11：签名必须包含**所有**下面日志会用到的字段，否则"只有该字段变化"时
      //   整条日志被跳过（例：只有 conf 或空跑占位色变了 → 颜色/件数/阶段都没变 → 不写日志）。
      const key = [s.stage, s.piece, s.color, s.conf, s.target, s.error,
                   s.finished, s.sim_color].join("|");
      if (key === this._lastLogKey) return;
      this._lastLogKey = key;
      if (silent) return;   // 首轮：只记游标，不回放旧状态
      const exec = useExecStore();
      if (s.stage) exec.logLine("info", `[AI] 阶段：${s.stage}`);
      if (s.color) exec.logLine("ok", `[AI] 判色：${s.color}`
        + (s.conf ? `（conf ${Number(s.conf).toFixed(2)}）` : "")
        + (s.sim_color ? "（空跑占位·未判色）" : "")
        + (s.target ? ` → ${s.target}` : ""));
      if (s.error) exec.logLine("err", `[AI] 已停止：${s.error}`);
      else if (s.finished) exec.logLine("ok", "[AI] 本次执行完成");
      // ★ 语音播报（用户口径 2026-10-09：动作/报错/颜色/去哪儿/已到达都要播）
      //   放在日志签名变化这里：同一阶段只播一次，且与日志内容天然同步。
      speak(stageSpeech(s));
    },

    async start() {
      if (this.timer) return;
      // ★ 未授权不发轮询：loadConfig / poll 调的 /ai/config、/ai/state 挂着
      //   require_control，无令牌必然 401 —— 侦测/轮询不拦会刷满控制台
      //   （拿了令牌拿到控制授权后再 start，组件侧 watch controlActive 负责启停）。
      if (!useAuthStore().controlActive) return;
      this.loadConfig();
      await this.poll(true);   // ★ 首轮静默对齐：不回放上一次运行的旧 error/阶段
      this.timer = setInterval(() => this.poll(), 800);
    },
    stop() {
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
    },
  },
});
