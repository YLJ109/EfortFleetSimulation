// =====================================================================
// Pinia store：执行域（示教 / 点动 / 点位库 / 程序执行 / 按文件名执行 / 急停）
//
// ★ 为什么把逻辑从组件里搬出来：
//   "点位执行"与"程序执行"是两页，但底层完全是同一套东西（控制权限、围栏互锁、
//   速度、残影预演、急停、后端接口）。放在 store 里 = 一份实现、两处入口；
//   如果各写一份，改安全判据时必然漏掉一边 —— 这是最容易出事故的地方。
//
// 安全规则（前端先拦，后端再校验一次）：
//   1. 写操作必须持有管理员控制令牌（auth.controlActive），否则弹登录。
//   2. **点位 / 示教 / 程序**：按**残影预演的目标位姿**判定 —— 残影不撞、不泛红才放行
//      （gateAt 在下发前同步量一次，不依赖"用户先悬停过"）。
//      ★ 不按实体机**当前位姿**拦：它现在在哪与"要去哪"无关，用它会拦掉"从危险区撤出来"
//        这种正当动作。实时位姿只在**点动**上仍作判据（点动是就地增量走，没有目标可预演）。
//   3. 真实下发默认关闭；未开启时后端只做校验 + 模拟动画。
//   4. 点动是独立线程，必须先 stop 再急停（后端 /control/estop 内部已处理）。
//
// 定时器必须放模块作用域而不是 state：Pinia 的 state 会被代理/序列化，
// 计时器句柄放进去既没意义又容易被响应式系统搅乱。
// =====================================================================
import { defineStore } from "pinia";
import { apiUrl } from "../config.js";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "./auth.js";
import { useRobotStore } from "./robot.js";
import { useSafetyStore } from "./safety.js";
import { useRcReadyStore } from "./rcReady.js";
import { setGhostPose, setGhostPoseB, showGhost, showGhostB, highlightJoint,
         evalSafety } from "../three/manager.js";
import { STATE_RANK } from "../three/safety.js";
import { DEFAULT_ONLY_JOINT, JOINT_LOCK_TOL_DEG, SPEED_DEFAULT }
  from "../core/safetyConst.js";

export const JOG_STEPS = [0.1, 1, 5, 10];
const HOLD_MS = 260;      // 按住超过该时长 → 连续点动；否则视为单击走一格
const LOG_MAX = 240;

let previewTimer = null;
let keepAliveTimer = null;
let pollTimer = null;
let pressTimer = null;
let pressInfo = null;
// ★ 全维度审查 F-01：按压"世代"令牌。每按一次 +1、松手 +1。
//   startJog 是异步的，返回时用户可能早已松手 —— 没有这个令牌，
//   松手后仍会把连续点动跑起来（机器人在按钮已松开时继续运动）。
let pressToken = 0;

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

function hhmmss() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

export const useExecStore = defineStore("exec", {
  state: () => ({
    // ---- 数据 ----
    points: [],
    /** ★ 点位执行新需求：被「点选」常驻跟随的点位 id（残影钉在该点位姿，直到点别的点/离开）。 */
    pinnedPointId: null,       // 残影 A 选中的点位（安全判定以它为准）
    pinnedPointIdB: null,      // ★ 残影 B：仅用于"两个点位对比"，不参与围栏判定
    programs: [],
    files: [],
    pointsErr: "",
    programsErr: "",

    // ---- 公共执行参数 ----
    // ★★ 全局执行速度（全项目唯一速度口径）：**所有下发都用这一份**。
    //   ★ 2026-10-09 修正两处与后端不一致的地方（现场报"速度不对/像被写死"）：
    //     ① **下限必须与后端 `SPEED_MIN` 一致**（=5）。旧代码这里放行 `>= 1`、
    //        顶栏滑块也是 `min="1"` —— 操作员拖到 1~4 时，后端 `ge=5` 会**直接拒绝**
    //        （或按 5 夹取），表现就是"我设了 1% 但它按 5% 跑"。
    //     ② **不再持久化**：以前拖过一次 35% 就会"每次启动都是 35%"。
    //   ★ 2026-10-11 用户口径（以这次为准）：**每次启动默认 10%、下限仍 5%**。
    //     下限由滑块 `min = SPEED_MIN(5)` 保证（需要更慢还能往下拖）；
    //     启动初值 = SPEED_DEFAULT(10)。★ 5%→10% 属**真机提速一倍**，是用户明确要求。
    //   ★ 改这里的任何一个数，都要同步 `App.vue` 的滑块 min/max 与后端 SPEED_MIN，
    //     有回归脚本 tools/verify_speed.mjs 盯着。
    speed: (() => {
      // ★★ 2026-10-09 用户口径：**不读也不写 localStorage**，每次启动一律从默认档起步。
      //   ★ 2026-10-11 默认档由 SPEED_MIN 改为 SPEED_DEFAULT（10%）。
      //   ★ 为什么这样更安全：速度是**每次开机最该保守**的参数 ——
      //     上一次调试时的高速不该被"继承"到下一次（尤其是在别人接手、或隔天开机时）。
      //     当前会话内拖到多少就一直是多少，刷新即归 SPEED_DEFAULT。
      return SPEED_DEFAULT;
    })(),
    dryRun: true,
    fileName: "",
    fileBusy: false,
    fileResult: null,

    // ---- 滑块示教 ----
    teachMode: "joint",                       // joint | cartesian
    teachQ: [0, 0, 0, 0, 0, 0],
    teachTcp: { x: 300, y: 0, z: 700 },
    teachBusy: false,
    teachResult: null,
    teachGhost: true,

    // ---- 点动 ----
    jogStepDeg: 1,
    jogBusy: false,
    jogState: null,
    holding: -1,                              // 当前按住的轴 1..6，-1 未按住
    holdingDir: 0,

    // ---- 轴锁模式（来自后端 /api/system/health 的 joint_lock）----
    //   ★ null = 尚未拉取；enabled=false（默认）时 jointLocked() 恒返回 false → J1–J6 全轴可动。
    //     仅 AI 测试模式（由你开 config motion.joint_lock.enabled=true 并重启）下 enabled=true，
    //     此时除 only 轴外其余轴被锁定。后端 motion.command 同样强制，前端只做即时提示。
    jointLock: null,

    // ---- 急停 / 引擎 ----
    estopState: null,
    estopBusy: false,
    execState: null,

    // ---- 真空吸放（Web 触发 40135.Bit1/Bit2 → 控制器常驻服务程序 200 执行，不移动机器人）----
    //   ★ 与 jog 同真实下发双闸；控制器须 AUTO/远程且常驻程序(200)运行中才生效。
    //   ★ 2026-09-29：吸放与点动共用 200（不再有 210）；吸气为**电平保持**，状态锁存。
    //   idle = 真空已关断（默认态）；suck = 吸气保持中。无 release 中间态。
    vacuumState: "idle",   // idle | suck
    // ---- 序列编辑（程序执行页）----
    //   seqItems 元素恒为四类之一：
    //     {type:"point",  point_id:N}
    //     {type:"suck"} / {type:"release"}
    //     {type:"wait",   seconds:S}          ← 秒；落盘时后端转成 dwell_ms
    seqItems: [],
    seqName: "",
    seqBusy: false,
    seqErr: "",
    seqMsg: "",
    seqDirty: false,          // 有未保存改动 → 载入/清空前二次确认
    runState: null,           // /control/run-state 的快照（进度/暂停态）
    runPaused: false,
    _seqPoll: null,           // 进度轮询句柄
    vacuumBusy: false,
    vacuumErr: null,
    // ★ 「停止吸气」/「放气」在上一发还飞时被按下 → 记下待补发的动作，
    //   收尾后立刻执行，绝不丢弃（"" = 无待补发）。
    vacuumPending: "",

    // ---- 程序执行运行态 ----
    running: false,
    runAbort: false,
    runBusy: false,
    runName: "",
    runKind: "",                              // program | file
    runStep: 0,
    runTotal: 0,
    runDry: true,
    lastRun: null,
    /**
     * 最近一次被运行的"点位序列"程序对象。
     * ★ 只给快捷键（空格）用：空格键不应该自己去列表里猜一个程序运行 —— 那是隐式选择，
     *   现场会变成"按了空格不知道跑了哪个"。所以只在用户**主动跑过一次之后**，
     *   空格才有可重复的目标；从没跑过就只提示一句，不自己挑。
     */
    lastRunTarget: null,
    log: [],
  }),

  getters: {
    /** 关节限位（后端未返回时用兜底值，保证滑块永远能拖）。 */
    limits() {
      const l = useRobotStore().limits;
      return (l && l.length === 6)
        ? l
        : [{ min: -170, max: 170 }, { min: -170, max: 90 }, { min: -85, max: 150 },
           { min: -180, max: 180 }, { min: -115, max: 115 }, { min: -360, max: 360 }];
    },
    /**
     * 实体机**当前位姿**是否危险（实时读数）。
     * ★ 只给点动用：点动是"就地增量走"，没有目标位姿可预演，只能看现在在哪。
     */
    UNSAFE() {
      const s = useSafetyStore();
      // ★ 全维度审查 F-02：点动判据必须限定 source === "robot"。
      //   残影预演（source==="ghost"）会把 lastState 改写成残影的结论，而点动动的是
      //   实体机当前位姿 —— 判据与动作对象错位：实体机明明安全却被灰掉按钮，
      //   或实体机已在危险区而按钮仍亮。
      if (s.lastSource !== "robot") return false;
      return ["danger", "hit"].includes(s.lastState);
    },
    /**
     * 残影**预演的目标位姿**是否危险 —— 点位 / 示教 / 程序的拦截依据。
     * ★ 现场口径：这三类操作**不按实体机当前位姿拦截**（它现在在哪与"要去哪"无关），
     *   只看残影预演：不撞、不泛红，就允许运动。
     */
    ghostUnsafe() {
      const s = useSafetyStore();
      return s.lastSource === "ghost" && ["danger", "hit"].includes(s.lastState);
    },
    /**
     * ★ 全维度审查 F-05：可下发的唯一判据（组件不再各写一份 canControl）。
     *   原实现只判令牌 —— 机器人断链或遥测冻结（读数不可信）时按钮照样可用。
     */
    canControl() {
      const a = useAuthStore(), r = useRobotStore();
      return !!a.controlActive && !!r.connected && !r.telemetryStale;
    },
    /**
     * ★ 需求（硬顺序）：示教 / 点动 / 下发 一律要求「控制器已就绪」——
     *   即"一键就绪"成功过（伺服已上电 + 点动服务程序在运行 + AUTO + 无报警）。
     *   只拿到令牌是不够的：伺服没上电时点下去也不动，用户会以为"坏了"；
     *   把这条固化成门控，也就固化了"先一键就绪、再操作"的现场纪律。
     *   ★ 取消就绪（程序停/伺服下电）后 ready=false → 这些按钮立即回到不可用。
     */
    readyGate() {
      return !!useRcReadyStore().ready;
    },
    canExec() {
      return this.canControl && !this.ghostUnsafe && this.readyGate;
    },
    /** 点动按钮的可用性：与点位/示教分开判据（点动没有目标预演），但同样要求已就绪。 */
    canJog() {
      return this.canControl && !this.UNSAFE && this.readyGate;
    },
    /** ★ 轴锁：模式驱动。默认关闭 → J1~J6 全轴可动（操作员需求）。
     *  ★ 这是 **getter**（计算属性，是个"值"）：外部/内部一律按 `this.jointLockEnabled`
     *    访问，**绝不能加括号 `()`** —— 否则报 "this.jointLockEnabled is not a function"。 */
    jointLockEnabled() {
      return !!(this.jointLock && this.jointLock.enabled);
    },
    /** 顶栏/执行页徽标文案：让操作员随时知道当前是哪一种轴策略。 */
    axisHint() {
      return this.jointLockEnabled
        ? `轴锁模式：仅 J${this.jointLock?.only || DEFAULT_ONLY_JOINT} 可动`
        : "J1–J6 全轴可动";
    },
    /** 禁止下发的具体原因（给界面显示用，避免用户猜）。 */
    blockReason() {
      const s = useSafetyStore();
      if (this.ghostUnsafe) return `残影预演的目标位姿处于 ${s.lastState}，已禁止下发`;
      if (!useAuthStore().controlActive) return "未获得控制权限，无法下发";
      if (!this.readyGate) return "控制器未就绪：请先在「真机链路」点「一键就绪」（伺服上电 + 程序运行 + 无报警）";
      return "";
    },
    jogActive: (s) => !!(s.jogState && s.jogState.active),
    runPct(s) {
      if (!s.runTotal) return 0;
      return Math.max(0, Math.min(100, Math.round((s.runStep / s.runTotal) * 100)));
    },
    /**
     * 示教器（控制器）程序：后端 /control/files 里 group="teach" 的候选，
     *   即 programs 目录下的 .XPL 文件（200/JOGSVC、411 等示教器导出的程序）。
     */
    teachFiles: (s) => (s.files || []).filter((f) => f.group === "teach"),
    /**
     * 本地程序（非示教器）：数据库预设点位 + programs 目录下的 .json 文件。
     *   ★ 数据库「点位序列程序」不在 files 里二次出现（它在 exec.programs），
     *     前端用 localItems 统一去重合并。
     */
    localFiles: (s) => (s.files || []).filter((f) => f.group === "local"),
  },

  actions: {
    // ================= 轴锁判定（带参数，必须是 action） =================
    /** 关节 j 在当前轴锁模式下是否被禁止运动。
     *  ★ 必须放在 **actions** 而不是 getters：Pinia 的 getter 不能接收参数，
     *    写成 getter 会被当成"无参计算属性"，`this.jointLocked(joint)` 调用必然失败。 */
    jointLocked(j) {
      if (!this.jointLockEnabled) return false;        // 默认全轴可动
      const only = Number(this.jointLock?.only || DEFAULT_ONLY_JOINT);
      return Number(j) !== only;
    },

    // ================= 日志 =================
    logLine(level, text) {
      this.log.push({ t: hhmmss(), level, text: String(text) });
      if (this.log.length > LOG_MAX) this.log.splice(0, this.log.length - LOG_MAX);
    },
    clearLog() { this.log = []; },

    // ================= 清除示教器（控制器）报错 =================
    // ★ 只清报警（写 CMD_CLEAR 0x1009），不重新上电/加载/运行——
    //   那是「一键就绪」的职责。清完若伺服掉电，需再点一键就绪。
    async clearError() {
      this.logLine("info", "清除报错：读报警 → 必要时先停机 → 清报警…");
      try {
        const r = await apiControl("/control/clear-error", { method: "POST" });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          this.logLine("ok", "清除报错完成：" + (d.message || (d.cleared ? "报警已清" : "本无报警")));
        } else {
          this.logLine("err", "清除报错失败：" + ((d && (d.error || d.message)) || ("HTTP " + r.status)));
        }
      } catch (e) {
        this.logLine("err", "清除报错请求失败：" + (e && e.message ? e.message : e));
      }
    },

    /**
     * ★★ 2026-10-10：**强制复位卡死的执行登记**。
     *   用户口径："程序正在执行中，但是没跑代码，就绪和取消都成功，无法下达指令"。
     *   后端只清**已经死掉**的登记（线程不存在）；只要还有活着的执行就一律拒绝 ——
     *   绝不把正在跑的机器人交给手动指令。**只动软件登记，不写任何寄存器。**
     */
    async forceReleaseRuns() {
      try {
        const r = await apiControl("/control/force-release", { method: "POST" });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          this.logLine("ok", "复位执行登记：" + (d.message || "已清掉卡死的执行登记"));
        } else {
          this.logLine("err", "复位执行登记失败："
            + ((d && (d.error || d.message)) || ("HTTP " + r.status)));
        }
      } catch (e) {
        this.logLine("err", "复位执行登记请求失败：" + (e && e.message ? e.message : e));
      }
    },

    // ================= 全局执行速度 =================
    // ★ 2026-10-09：**不再持久化**（用户口径"每次启动默认必须是 5%"）。
    //   保留这个方法只为兼容旧调用点 —— 它现在什么都不做，并在控制台提示一次，
    //   避免"看着像存了、其实没存"的误解。
    saveSpeed() {
      if (!this._speedPersistWarned) {
        this._speedPersistWarned = true;
        console.info("[speed] 速度不再持久化：每次启动从 10%（SPEED_DEFAULT）起步（用户口径）。"
          + "本次会话内调整仍即时生效。");
      }
    },

    // ================= 权限 =================
    needAuth() {
      const auth = useAuthStore();
      if (auth.controlActive) return false;
      if (auth.requestLogin) auth.requestLogin();
      return true;
    },

    // ================= 数据加载 =================
    async loadPoints() {
      try {
        const r = await fetch(apiUrl("/points"));
        if (r.ok) { this.points = await r.json(); this.pointsErr = ""; }
        else this.pointsErr = "点位加载失败：" + r.status;
      } catch (e) { this.pointsErr = "点位加载失败：" + e.message; }
    },
    async loadPrograms() {
      try {
        const r = await fetch(apiUrl("/programs"));
        if (r.ok) { this.programs = await r.json(); this.programsErr = ""; }
        else this.programsErr = "程序加载失败：" + r.status;
      } catch (e) { this.programsErr = "程序加载失败：" + e.message; }
    },
    async loadFiles() {
      // ★ 未持有效令牌时不发请求：/control/files 在后端是 require_control，
      //   必然 401；而浏览器对任何非 2xx 的 fetch 都会自己往控制台打一行
      //   "GET ... 401 (Unauthorized)"，JS 侧屏蔽不掉。执行页是开机即挂载的
      //   （v-show 保活），不拦的话每个未登录访客一进来就带出两条 401。
      if (!useAuthStore().controlActive) { this.files = []; return; }
      try {
        const r = await apiControl("/control/files");
        if (r.status === 401) return;
        if (r.ok) { const d = await r.json(); this.files = d.items || []; }
      } catch (e) { /* 静默 */ }
    },
    async loadAll() {
      await Promise.all([this.loadPoints(), this.loadPrograms(), this.loadFiles(), this.loadLock()]);
    },
    /** 拉取轴锁模式（来自后端 /api/system/health.joint_lock），驱动顶栏徽标与按钮禁用。 */
    async loadLock() {
      try {
        const r = await fetch(apiUrl("/system/health"));
        if (!r.ok) return;
        const d = await r.json();
        if (d && d.joint_lock) this.jointLock = d.joint_lock;
      } catch (e) { /* 后端不可用时静默，保持 null → 全轴可动默认 */ }
    },

    // ================= 滑块示教 =================
    /**
     * ★ P0-1 修复：滑块/基准一变，缓存的 teachResult 立即作废。
     *   改造前 applyTeach 会直接复用上一次预演的 target —— 滑块改动晚于预演时，
     *   下发的就是**旧目标**（想走 A 结果走了 B）。现在任何改变目标的入口都先作废。
     */
    invalidateTeach() {
      this.teachResult = null;
      if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
    },
    /** 用当前实时姿态初始化示教滑块。 */
    syncTeachFromRobot() {
      const robot = useRobotStore();
      // ★ 读 readoutQ（≡ 3D 画面姿态），不用 latestPose 遥测别名 —— 保证滑块基准与画面一致。
      const q = (robot.readoutQ && robot.readoutQ.length === 6)
        ? robot.readoutQ : [0, 0, 0, 0, 0, 0];
      for (let i = 0; i < 6; i++) this.teachQ[i] = Number(Number(q[i] || 0).toFixed(1));
      const t = robot.readoutTcp || { x: 0, y: 0, z: 0 };
      this.teachTcp.x = Number(Number(t.x || 0).toFixed(1));
      this.teachTcp.y = Number(Number(t.y || 0).toFixed(1));
      this.teachTcp.z = Number(Number(t.z || 0).toFixed(1));
      this.invalidateTeach();
    },
    onTeachSlider(i, e) {
      this.teachQ[i] = parseFloat(e.target.value) || 0;
      this.invalidateTeach();
      this.schedulePreview();
    },
    schedulePreview(delay = 220) {
      if (previewTimer) clearTimeout(previewTimer);
      previewTimer = setTimeout(() => { previewTimer = null; this.previewTeach(); }, delay);
    },
    /** 预演：把目标解算/校验一遍，并把残影摆到目标位上。只读，绝不下发。 */
    async previewTeach() {
      if (!useAuthStore().controlActive) return;
      this.teachBusy = true;
      try {
        const body = this.teachMode === "joint"
          ? { mode: "joint", joints: this.teachQ.map(Number), duration_ms: 1500, steps: 40 }
          : { mode: "cartesian",
              tcp: { x: +this.teachTcp.x, y: +this.teachTcp.y, z: +this.teachTcp.z },
              duration_ms: 1500, steps: 40 };
        const r = await apiControl("/control/preview", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        const d = await r.json();
        this.teachResult = d;
        if (d.ok && d.target && this.teachGhost) setGhostPose(d.target);
        else showGhost(false);
      } catch (e) {
        this.teachResult = { ok: false, error: e.message };
      } finally { this.teachBusy = false; }
    },
    /** 直角坐标 → 关节角（IK），成功后回填关节滑块。 */
    async solveCartesian() {
      if (this.needAuth()) return;
      this.teachBusy = true;
      try {
        const r = await apiControl("/control/ik", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tcp: { x: +this.teachTcp.x, y: +this.teachTcp.y, z: +this.teachTcp.z },
            keep_orientation: true,
          }),
        });
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        const d = await r.json();
        if (d.ok && d.joints) {
          for (let i = 0; i < 6; i++) this.teachQ[i] = Number(Number(d.joints[i] || 0).toFixed(1));
          this.invalidateTeach();          // ★ 滑块被回填 → 旧预演作废
          await this.previewTeach();
        } else {
          this.teachResult = { ok: false, error: "目标不可达或超出关节限位" };
        }
      } catch (e) { this.teachResult = { ok: false, error: e.message }; }
      finally { this.teachBusy = false; }
    },
    /** 执行示教目标（按下发）。★ P0-1：无条件重算，绝不信任缓存的目标。 */
    async applyTeach() {
      if (this.needAuth()) return;
      this.teachBusy = true;
      try {
        // 无条件重新预演：滑块可能在最后一次预演之后又被动过。
        await this.previewTeach();
        if (!this.teachResult || !this.teachResult.ok || !this.teachResult.target) return;
        const target = this.teachResult.target;
        // 断言：joint 模式下预演目标必须与滑块一致（±0.05°），否则拒绝下发。
        if (this.teachMode === "joint") {
          let dev = 0;
          for (let i = 0; i < 6; i++) {
            dev = Math.max(dev, Math.abs((+target[i] || 0) - (+this.teachQ[i] || 0)));
          }
          if (dev > 0.05) {
            this.teachResult = { ok: false, error: "预演目标与滑块偏差 " + dev.toFixed(3) + "°，已拒绝下发" };
            this.logLine("err", "示教校验失败：目标与滑块偏差 " + dev.toFixed(3) + "°");
            return;
          }
        }
        // ★ 残影门控：按目标位姿量围栏/地面，危险或碰撞直接拒绝（不按实体机当前位姿拦）。
        const gate = this.gateAt(target);
        if (!gate.ok) { this.teachResult = { ok: false, error: gate.reason }; return; }
        // ★ 全维度审查 F-03 v2.1：轴锁模式（仅 AI 测试）开启时，示教也只有 J6 能偏离当前位姿。
        //   默认关闭（jointLock.enabled=false）→ 不拦截，J1–J6 全轴可动（操作员需求）。
        //   后端 motion.command 同样会强制，这里只是让前端即时给出原因、不必等点下去才报。
        if (this.jointLockEnabled && this.teachMode === "joint") {
          const cur = useRobotStore().readoutQ || [];
          const only = Number(this.jointLock?.only || DEFAULT_ONLY_JOINT);
          const bad = [];
          for (let i = 0; i < 6; i++) {
            if ((i + 1) === only) continue;
            const dev = Math.abs((+target[i] || 0) - (+cur[i] || 0));
            if (dev > JOINT_LOCK_TOL_DEG) bad.push(`J${i + 1}`);
          }
          if (bad.length) {
            this.teachResult = { ok: false,
              error: `轴锁模式：仅 J${only} 可动，${bad.join("/")} 偏离当前位姿被拒绝` };
            this.logLine("err", `示教被轴锁拦截：${bad.join("/")}`);
            return;
          }
        }
        const r = await apiControl("/control/move", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ joints: target, speed_pct: this.speed }),
        });
        const d = await r.json();
        this.execState = d;
        if (d.ok) {
          // ★ 姿态权威模型：不再直接驱动 3D —— 记录指令目标，
          //   真机由遥测跟上，模拟由"指令即位置"兜底（Stage C 后由仿真机走位）。
          useRobotStore().noteCommand(d.target.map(Number));
          this.logLine("ok", "示教到位：" + d.target.map((v) => (+v).toFixed(1)).join(", "));
        } else {
          this.logLine("err", "示教下发失败：" + (d.message || d.error || "未知"));
        }
      } catch (e) { this.execState = { ok: false, error: e.message }; }
      finally { this.teachBusy = false; }
    },

    // ================= 点动 =================
    stopTimers() {
      if (keepAliveTimer) { clearInterval(keepAliveTimer); keepAliveTimer = null; }
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
      pressInfo = null;
      this.holding = -1;
      this.holdingDir = 0;
    },
    async refreshJog() {
      try {
        const r = await apiControl("/control/jog");
        if (r.ok) this.jogState = await r.json();
      } catch (e) { /* 静默 */ }
    },
    /**
     * 点动前置闸：权限不足 / 实体机当前位姿不安全时**拒绝并留痕**。
     * ★ 原来这三处都是静默 `return` —— 点半天没反应、运行日志里一条都没有，
     *   用户根本分不清是被"没控制权限"拦了，还是被"围栏/碰撞判定"拦了。
     *   点动是 J1~J6 最常用的操作，必须能在日志里看到"点了什么、被什么拦了"。
     * @returns {boolean} true = 已拦截（调用方直接 return）
     */
    _jogGuard() {
      if (!useAuthStore().controlActive) {
        this.needAuth();     // 弹出验证框
        this.logLine("warn", "点动被拦截：未获得控制权限（已弹出验证框）");
        return true;
      }
      if (this.UNSAFE) {
        const s = useSafetyStore();
        this.logLine("warn", `点动被拦截：实体机当前位姿判定为 ${s.lastState}（围栏/碰撞），已拒绝下发`);
        return true;
      }
      return false;
    },
    /** 按住 → 连续点动；周期性 keepalive（死人开关）+ 轮询目标姿态驱动 3D。 */
    async startJog(joint, dir, token) {
      if (this._jogGuard()) return;
      // ★ F-03 v2.1：轴锁只在模式开启时生效（默认全轴可动）
      if (this.jointLocked(joint)) {
        this.logLine("warn", `轴锁模式：J${joint} 不可动，仅 J${this.jointLock?.only || DEFAULT_ONLY_JOINT} 可动`);
        return;
      }
      this.stopTimers();
      this.jogBusy = true;
      try {
        const r = await apiControl("/control/jog/start", {
          method: "POST", headers: { "Content-Type": "application/json" },
          // ★ 速度只认右上角全局速度（%）：不再传 °/s，避免"两个旋钮"口径不一致
          body: JSON.stringify({ joint, dir, speed_pct: this.speed }),
        });
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        const d = await r.json();
        if (!r.ok || !d.ok) {
          const msg = d.message || d.error || "点动被拒绝";
          this.execState = { ok: false, error: msg };
          this.logLine("err", `连续点动 J${joint} 被拒绝：${msg}`);
          return;
        }
        // ★ 全维度审查 F-01：接口返回时用户可能已经松手。
        //   此时必须立即发一次 stop，绝不建立 keepalive/poll 定时器，
        //   否则机器人会在按钮已松开的情况下持续运动。
        if (token !== undefined && token !== pressToken) {
          try { await apiControl("/control/jog/stop", { method: "POST" }); } catch (e) { /* 静默 */ }
          this.logLine("warn", "松手早于启动返回，已取消连续点动");
          return;
        }
        this.holding = joint;
        this.holdingDir = dir;
        this.logLine("info", `连续点动 J${joint} ${dir > 0 ? "正向" : "反向"} @${this.speed}%`);
        // 死人开关：0.4s 一次使能保持，1.5s 收不到后端自动停
        keepAliveTimer = setInterval(async () => {
          try { await apiControl("/control/jog/keepalive", { method: "POST" }); }
          catch (e) { /* 静默 */ }
        }, 400);
        pollTimer = setInterval(async () => {
          try {
            const rr = await apiControl("/control/jog");
            if (!rr.ok) return;
            const s = await rr.json();
            this.jogState = s;
            // ★ 不再直接驱动 3D：把点动引擎的当前目标记入指令通道，
            //   由 App.vue 渲染循环统一显示；真机模式下遥测会跟上真实位置。
            if (s && s.active && s.target) useRobotStore().noteCommand(s.target.map(Number));
            if (s && this.holding >= 0 && !s.active) {
              const j = this.holding;
              this.stopTimers(); this.refreshSessionPose();
              this.logLine("info", `连续点动 J${j} 已结束（控制器报告已停）`);
            }
          } catch (e) { /* 静默 */ }
        }, 180);
      } catch (e) { this.execState = { ok: false, error: e.message }; }
      finally { this.jogBusy = false; }
    },
    async endJog() {
      if (this.holding < 0) return;
      const j = this.holding;          // ★ 先记下来：stopTimers() 会把它清成 -1
      this.stopTimers();
      let ok = false;
      try {
        const r = await apiControl("/control/jog/stop", { method: "POST" });
        if (r.ok) { this.jogState = await r.json(); ok = true; }
      } catch (e) { /* 静默 */ }
      this.refreshSessionPose();
      this.logLine(ok ? "info" : "warn",
        `连续点动 J${j} 已停止` + (ok ? "" : "（停止请求未确认，请确认机器人已停）"));
    },
    /** 增量点动：按一次走固定角度。 */
    async stepJog(joint, dir) {
      if (this._jogGuard()) return;
      if (this.jointLocked(joint)) {
        this.logLine("warn", `轴锁模式：J${joint} 不可动，仅 J${this.jointLock?.only || DEFAULT_ONLY_JOINT} 可动`);
        return;
      }
      await this.endJog();
      this.jogBusy = true;
      try {
        const r = await apiControl("/control/jog/step", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ joint, dir, angle_deg: this.jogStepDeg,
                                 speed_pct: this.speed }),
        });
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        const d = await r.json();
        if (!r.ok || !d.ok) {
          const msg = d.message || d.error || "增量点动被拒绝";
          this.execState = { ok: false, error: msg };
          this.logLine("err", `增量点动 J${joint} 被拒绝：${msg}`);
          return;
        }
        this.execState = { ok: true, target: d.target, mode: d.mode };
        useRobotStore().noteCommand(d.target.map(Number));
        if (d.warnings && d.warnings.length) this.execState.warning = d.warnings[0];
        // ★ 成功也要留痕：原来只有"被拒绝"才写日志，于是正常点动在运行日志里
        //   一条都看不到（用户报的"点动 J1~J6 为什么不显示"就是这条）。
        {
          const jIdx = Number(joint) - 1;
          const after = Array.isArray(d.target) ? Number(d.target[jIdx]) : NaN;
          this.logLine("ok", `增量点动 J${joint} ${dir > 0 ? "+" : "−"}${this.jogStepDeg}°`
            + (Number.isFinite(after) ? ` → ${after.toFixed(1)}°` : "")
            + `（${d.mode === "real" ? "真实" : "模拟"} @${this.speed}%）`);
        }
        this.syncTeachFromRobot();
      } catch (e) { this.execState = { ok: false, error: e.message }; }
      finally { this.jogBusy = false; }
    },
    // 点动按钮的按压模型：★ 不能同时用 @pointerdown + @click ——
    //   长按会先 start(连续)，松手又触发一次 click(步进)，两个动作打架。
    //   这里统一到 pointer 事件：按住 >260ms 视为连续点动，短按视为走一格。
    jogPress(joint, dir) {
      if (this._jogGuard()) return;
      if (this.jointLocked(joint)) {
        this.logLine("warn", `轴锁模式：J${joint} 不可动，仅 J${this.jointLock?.only || DEFAULT_ONLY_JOINT} 可动`);
        return;
      }
      const my = ++pressToken;                 // ★ F-01：新一次按压
      pressInfo = { joint, dir, token: my };
      if (pressTimer) clearTimeout(pressTimer);
      pressTimer = setTimeout(() => {
        pressTimer = null;
        const info = pressInfo;
        pressInfo = null;                      // ★ 起步即作废，避免松手后被当成短按
        if (info) this.startJog(info.joint, info.dir, my);
      }, HOLD_MS);
    },
    jogRelease() {
      pressToken++;                            // ★ F-01：松手即作废在途的 startJog
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
      const info = pressInfo;
      pressInfo = null;
      if (this.holding >= 0) { this.endJog(); return; }   // 连续点动中 → 停
      if (info) this.stepJog(info.joint, info.dir);       // 短按 → 走一格
    },
    // ================= 残影预演 =================
    /** 把残影摆到某个点位（按 point_id 在已加载点位里找关节角）。 */
    previewPointGhost(pointId) {
      const p = this.points.find((x) => x.id === pointId);
      if (p && p.joints && p.joints.length === 6) setGhostPose(p.joints);
      else showGhost(false);
    },
    /** 在点位列表上悬停 → 残影预演该点位。 */
    hoverPoint(p) {
      if (p && p.joints && p.joints.length === 6) setGhostPose(p.joints);
    },
    unhoverPoint() { if (!this.runBusy) showGhost(false); },
    hideGhost() { showGhost(false); highlightJoint(-1); },
    /** ★ 点选（选中）某点位：残影**常驻**跟随该点位姿，直到点别的点或离开页面。
     *  与 hoverPoint（悬停临时预演）的区别：pin 是持久化选中态，鼠标移开不消失。 */
    pinPoint(p) {
      if (p && p.joints && p.joints.length === 6) {
        setGhostPose(p.joints);
        showGhost(true);
        this.pinnedPointId = p.id;
      } else {
        this.pinnedPointId = null;
        showGhost(false);
      }
    },
    unpinPoint() { this.pinnedPointId = null; },

    /**
     * ★ 残影 B（对比用）：把第二个残影摆到另一个点位，与 A 同屏对比。
     *   **刻意不参与围栏/地面判定** —— 安全结论永远只反映 A（当前正在评估的目标），
     *   否则界面上"红/绿"到底说的是哪个残影就说不清了。
     */
    pinPointB(p) {
      if (p && p.joints && p.joints.length === 6) {
        setGhostPoseB(p.joints);
        showGhostB(true);
        this.pinnedPointIdB = p.id;
      } else {
        this.pinnedPointIdB = null;
        showGhostB(false);
      }
    },
    unpinPointB() { this.pinnedPointIdB = null; showGhostB(false); },
    /** 两个残影一起收掉（离页/切视图时用）。 */
    hideGhosts() {
      showGhost(false); showGhostB(false); highlightJoint(-1);
      this.pinnedPointId = null; this.pinnedPointIdB = null;
    },

    /**
     * **下发门控**：把残影摆到目标位姿，立刻量一次围栏/地面 —— 不撞才放行。
     *
     * ★ 这是"点位执行不用拦截，只要残影预演没有碰撞/不变红就能动"的落点：
     *   - 判据来自**残影的目标位姿**，不是实体机当前位姿；
     *   - 同步执行（不依赖渲染循环那一帧），所以"没悬停过就直接点执行"也拦得住；
     *   - 侧栏围栏面板与残影同源变色，拒绝时用户看到的就是那片红。
     *
     * ★ 判据（现场最终口径）：**残影跟随目标位姿实测，变红即拒绝 —— 没有角度容差。**
     *   四墙/四角：`ratio < 10%` → danger；`ratio <= 0` → hit。
     *   地面：最低点 ≤ danger_mm → danger；≤ hit_mm → hit。
     *   只要有一处变红就不放行；不红就放行。不引入"±N° 内视为不碰撞"之类的
     *   放宽 —— 那类阈值在默认值下会把整道拦截关掉，而真撞上去没有撤销键。
     *   与此配套：三个执行入口（点位/示教/程序）的按钮绑的是 `canExec`
     *   （= 控制权限 && !ghostUnsafe），变红时**按钮直接点不动**，不必等到点下去才报。
     *   gateAt 仍保留 —— 它是同步兜底，管"没悬停过就直接点执行"这一种。
     *
     * @param {number[]} qDeg 目标关节角(度)
     * @returns {{ok:boolean, state?:string, reason?:string}}
     */
    gateAt(qDeg) {
      if (!qDeg || qDeg.length !== 6) return { ok: true };   // 没有目标可预演 → 不拦
      setGhostPose(qDeg);
      const r = evalSafety();
      if (!r || STATE_RANK[r.state] < STATE_RANK.danger) {
        return { ok: true, state: r ? r.state : "safe" };
      }
      const why = r.state === "hit" ? "目标位姿会碰撞" : "目标位姿危险";
      const where = r.zoneName ? `（${r.zoneName}）` : "";
      const reason = `${why}${where}，已拒绝下发`;
      this.logLine("err", "残影预演拦截：" + reason);
      return { ok: false, state: r.state, reason };
    },

    // ================= 点位 CRUD 辅助 =================
    /** 把示教位姿填进"新建点位"表单（返回填充好的 payload，表单由页面持有）。 */
    teachAsPointPayload() {
      return {
        kind: "joint",
        joints: this.teachQ.map(Number),
        name: "示教点 " + new Date().toLocaleTimeString("zh-CN", { hour12: false }),
      };
    },
    async savePoint(payload, id) {
      const isNew = !id;
      const opts = { headers: { "Content-Type": "application/json" },
                     body: JSON.stringify(payload) };
      const r = isNew
        ? await apiControl("/points", { ...opts, method: "POST" })
        : await apiControl(`/points/${id}`, { ...opts, method: "PUT" });
      if (r.ok) { await this.loadPoints(); return { ok: true }; }
      const j = await r.json().catch(() => ({}));
      if (r.status === 401 || r.status === 403) {
        this.needAuth();
        return { ok: false, error: r.status === 403 ? "该操作需要管理员权限" : "需要控制权限才能管理点位" };
      }
      return { ok: false, error: j.message || j.detail || ("保存失败：" + r.status) };
    },
    async delPoint(p) {
      const r = await apiControl(`/points/${p.id}`, { method: "DELETE" });
      if (r.ok) { await this.loadPoints(); return true; }
      if (r.status === 401 || r.status === 403) this.needAuth();
      return false;
    },
    /**
     * ★ 一键标记当前点：读取机器人**当前实时位姿**（readoutQ 六关节角）存成新点位。
     *
     * ★ 为什么 T1/T2 也能用：标记动作**只读当前位姿 + 写库**，**不向机器人下发任何
     *   运动指令**——而真正需要 AUTO + 伺服上电的是"执行/点动"那类下发链路。所以这里
     *   只拦「控制令牌 + 已连接 + 遥测可信」，**不拦 readyGate（不要求一键就绪 / AUTO）**。
     *   现场最常见的用法正是：操作员在示教器上把机器人手动对到 T1/T2 的某个位置，
     *   然后在网页点一下「标记当前点」就把这个点存下来，回 AUTO 后再挑出来执行。
     *   备注留空，可在列表「编辑」里补；标记成功后自动 pin（残影跳到该点，便于对照）。
     */
    async markCurrentPoint() {
      if (this.needAuth()) return false;
      const robot = useRobotStore();
      if (!robot.connected) {
        this.logLine("warn", "无法标记当前点：机器人未连接，读不到当前位姿");
        return false;
      }
      if (robot.telemetryStale) {
        this.logLine("warn", "无法标记当前点：遥测超时（位姿读数不可信），请确认机器人在线后再标记");
        return false;
      }
      const q = (robot.readoutQ && robot.readoutQ.length === 6) ? robot.readoutQ : null;
      if (!q) {
        this.logLine("warn", "无法标记当前点：当前位姿读数不可用");
        return false;
      }
      const joints = q.map((v) => Number(v || 0));   // ★ 保留完整精度浮点，不四舍五入（标记点要精准数据）
      const name = "标记点 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
      const res = await this.savePoint(
        { kind: "joint", joints, name, group: "默认", note: "" }, null);
      if (res.ok) {
        await this.loadPoints();
        const np = this.points.find((x) => x.name === name);
        if (np) {
          this.pinPoint(np);
          const shown = (np.joints || joints).map((v) => Number(v).toFixed(3)).join(", ");
          this.logLine("ok", `已标记当前点「${name}」[${shown}]（T1/T2 示教对位亦可；可在「编辑」补备注）`);
        }
        return true;
      }
      this.logLine("err", "标记当前点失败：" + (res.error || "未知"));
      return false;
    },
    /** 直角坐标点位保存前先解算成关节角（后端只吃 joints）。 */
    async solveTcp(tcp) {
      if (this.needAuth()) return { ok: false, error: "直角坐标解析需先获取控制权限" };
      const r = await apiControl("/control/ik", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tcp, keep_orientation: true }),
      });
      if (!r.ok) return { ok: false, error: "IK 解析失败" };
      const d = await r.json();
      if (!d.ok) return { ok: false, error: "目标不可达或超限位" };
      return { ok: true, joints: d.joints };
    },

    // ================= 执行：单点 / 程序 / 按文件名 =================
    /** 指令成功后的显示钩子：只记录指令目标，绝不直接驱动 3D（App 循环统一写）。 */
    animateTo(target) {
      if (!target) return;
      useRobotStore().noteCommand(target.map((v) => +v));
    },
    async execPoint(p) {
      if (this.needAuth()) return;
      // ★ 残影门控：先把残影摆到该点位量一次，不撞才发（替代原先"按实体机位姿拦"）。
      const gate = this.gateAt(p && p.joints);
      if (!gate.ok) { this.execState = { ok: false, error: gate.reason }; return; }
      this.runBusy = true;
      this.execState = null;
      try {
        this.hideGhost();
        const r = await apiControl("/control/move", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ point_id: p.id, speed_pct: this.speed }),
        });
        const d = await r.json();
        this.execState = d;
        if (d.ok) {
          this.animateTo(d.target, d.mode);
          this.logLine("ok", `点位「${p.name}」已下发 @${this.speed}%`);
        } else {
          this.logLine("err", `点位「${p.name}」下发失败：${d.message || d.error || ""}`);
        }
      } catch (e) {
        this.execState = { ok: false, error: e.message };
        this.logLine("err", "点位下发异常：" + e.message);
      } finally { this.runBusy = false; }
    },
    /**
     * ★ 审计修复 P1-D2：把"当前选中的程序"登记为空格键（runOrAbort）的执行目标。
     *   原实现里 lastRunTarget **只有 execProgram 自己写** —— 也就是说只有"已经跑过
     *   一次"的程序才能被空格重复运行；刚在列表里选中的程序按空格只会回一句
     *   "请先…选一个程序跑一次"，快捷键等于空转。选中即登记，语义也更直白。
     */
    setRunTarget(pr) {
      this.lastRunTarget = pr || null;
    },
    /** 运行"点位序列"程序：逐步下发 + 残影预演下一步 + 逐步日志 + 可中止。 */
    async execProgram(pr) {
      if (this.needAuth()) return;
      const items = pr.items || [];
      if (!items.length) {
        this.logLine("warn", `程序「${pr.name}」没有任何步骤`);
        return;
      }
      this.runBusy = true;
      this.running = true;
      this.runAbort = false;
      this.runKind = "program";
      this.runName = pr.name;
      this.lastRunTarget = pr;              // 记住目标：空格键要能重复运行它
      this.runTotal = items.length;
      this.runStep = 0;
      this.runDry = false;
      this.logLine("info", `开始运行程序「${pr.name}」（${items.length} 步）`);
      const t0 = Date.now();
      let passed = 0;
      let failed = null;
      try {
        for (let i = 0; i < items.length; i++) {
          if (this.runAbort) { this.logLine("warn", "已被用户中止"); break; }
          const it = items[i];
          this.runStep = i + 1;
          // ★ 逐步门控：每一步都先按"这一步的目标位姿"量一次残影，不撞才发。
          //   某一步过不去 → 就地中断，绝不"带病"往下跑。
          const pj = (this.points.find((x) => x.id === it.point_id) || {}).joints;
          const gate = this.gateAt(pj);
          if (!gate.ok) {
            failed = `第 ${i + 1} 步${gate.reason}`;
            this.execState = { ok: false, error: gate.reason };
            this.logLine("err", failed);
            break;
          }
          const nxt = items[i + 1];
          if (nxt) this.previewPointGhost(nxt.point_id);
          else showGhost(false);
          const r = await apiControl("/control/move", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ point_id: it.point_id, speed_pct: this.speed }),
          });
          const d = await r.json();
          if (d.ok) {
            passed++;
            this.animateTo(d.target, d.mode);
            this.logLine("ok", `第 ${i + 1}/${items.length} 步完成`
              + (it.dwell_ms ? `（停留 ${it.dwell_ms}ms）` : ""));
            await sleep((it.dwell_ms || 0) + 400);
          } else {
            failed = d.message || d.error || `第 ${i + 1} 步失败`;
            this.execState = d;
            this.logLine("err", failed);
            break;
          }
        }
      } catch (e) {
        failed = e.message;
        this.logLine("err", "运行异常：" + e.message);
      } finally {
        this.running = false;
        this.runBusy = false;
        showGhost(false);
        const ms = Date.now() - t0;
        const okAll = !failed && passed === items.length;
        this.lastRun = {
          name: pr.name, kind: "program", ok: okAll, dryRun: false,
          passed, count: items.length, duration_ms: ms, ts: Date.now(), error: failed,
        };
        this.logLine(okAll ? "ok" : "err",
          okAll ? `程序「${pr.name}」运行完成：${passed}/${items.length} 步，${(ms / 1000).toFixed(1)}s`
                : `程序「${pr.name}」中断：${passed}/${items.length} 步`);
      }
    },
    /**
     * 「测试跑」：对某个可执行目标做一次**空跑校验**（dry_run=true）。
     *
     * ★ 与「执行」的硬区别：永远不下发、不加载、不运行控制器程序、不移动机器人 ——
     *   只把目标解析一遍，对每个移动步做 IK / 限位 / 可达预演，io 步只计步。
     *   用来回答"这个程序能不能跑、跑得对不对"，不产生任何真实运动。
     *   复用同一套 runFile 通道（结果落在 fileResult / lastRun / 日志），
     *   所以示教器程序、本地点位序列、json 都能用它验。
     */
    async testRun(name) {
      if (this.needAuth()) return;
      const n = (name || "").trim();
      if (!n) return;
      this.fileName = n;
      this.dryRun = true;
      await this.runFile();
    },

    /** 按文件名执行：程序 / 点位 / programs 目录 JSON；dryRun 只校验不下发。 */
    async runFile() {
      if (this.needAuth()) return;
      // ★ 审计修复 P1-D4：中止后立即再点"执行"会开第二个并发循环
      //   （两套 runStep/日志互相覆盖）。busy 时直接拒绝重入。
      if (this.runBusy || this.fileBusy) {
        this.logLine("warn", "已有执行在进行中，先中止或等待其结束");
        return;
      }
      if (!this.fileName.trim()) {
        this.fileResult = { ok: false, error: "请先选择或输入文件名" };
        return;
      }
      // ★ 只按残影预演拦：按文件名执行事先不知道目标序列，前端拦不了"要去哪"，
      //   真正的兜底在后端 guard_check（它看的是前端上报的同一份围栏状态）。
      if (this.ghostUnsafe && !this.dryRun) return;
      this.fileBusy = true;
      this.runBusy = true;
      // ★ P0-7：每次执行分配一个 run_id，中止时精确指向本次执行
      this.runId = `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      this.runAbort = false;
      this.runKind = "file";
      this.runName = this.fileName.trim();
      this.runTotal = 0;
      this.runStep = 0;
      this.runDry = this.dryRun;
      this.running = !this.dryRun;
      this.logLine("info", this.dryRun
        ? `试运行「${this.runName}」（只校验，绝不下发）`
        : `执行「${this.runName}」@${this.speed}%`);
      const t0 = Date.now();
      try {
        const r = await apiControl("/control/run-file", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: this.runName, dry_run: this.dryRun,
            speed_pct: this.speed, steps: 40,
            run_id: this.runId,               // ★ P0-7：供 /control/run-cancel 精确中止
          }),
        });
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        let d = await r.json();
        // ★★ 2026-10-10（B1）：后端已**异步化** —— POST 只回 `{started, run_id, kind}`，
        //   真正的逐步结果去 `/control/run-state` 取（字段与老的响应体**一致**）。
        if (d && d.started) {
          if (d.run_id) this.runId = d.run_id;
          if (d.kind) this.runKind = d.kind;
          this.fileResult = null;
          d = await this._awaitRunResult();
        }
        this.fileResult = d;
        // FastAPI 的 HTTPException(404, detail={...}) 会把 detail 包进响应体的
        // "detail" 字段；message/candidates 实际在 d.detail 内层。这里先解一层，
        // 否则 name/passed/count/candidates 全是 undefined（见"undefined/undefined 步"）。
        const body = (d && typeof d === "object" && d.detail !== undefined
                      && typeof d.detail === "object" && d.detail !== null)
          ? d.detail : d;
        const candidates = (body && body.candidates) || d.candidates;
        if (!r.ok) {
          const msg = (body && (body.message || body.error || body.detail))
            || (typeof d.detail === "string" ? d.detail : "")
            || ("后端返回 " + r.status);
          this.fileResult = { ok: false, error: msg, candidates };
          this.logLine("err", `按文件名执行失败：${msg}`);
          if (candidates && candidates.length) {
            this.logLine("info", `可执行目标 ${candidates.length} 个：`
              + candidates.map((c) => c && (c.name || c.id)).join("、"));
          }
        } else {
          if (d.run_id) this.runId = d.run_id;
          // ★ P1-D3：runKind 以**后端返回的真实 kind**为准（program/file/point），
          //   原来 runFile 一律写死 "file"，导致"程序"分支的步骤高亮永远进不去。
          if (d.kind) this.runKind = d.kind;
          this.runTotal = d.count || 0;
          this.runStep = this.runTotal;
          const name = (d && (d.name || (d.target && d.target.name))) || this.runName;
          const stepTxt = (d && Number.isFinite(d.count)) ? `（${d.passed || 0}/${d.count} 步）` : "";
          if (d.cancelled) {
            // ★ P0-7：后端确认已中止 —— 这是"真正停下"的回执，不是失败
            this.logLine("warn", `已中止「${name}」${stepTxt}（机器人已停止继续下发）`);
          } else {
            this.logLine(d.ok ? "ok" : "err",
              (d.dry_run ? "试运行" : "执行") + (d.ok ? "通过" : "发现问题") + `：${name}${stepTxt}`);
            if (!d.ok && d.message) this.logLine("err", String(d.message));
          }
        }
        if (d.ok && !d.dry_run && d.steps && d.steps.length) {
          const last = [...d.steps].reverse().find((s) => s.target);
          if (last) useRobotStore().noteCommand(last.target.map(Number));
        }
        this.lastRun = {
          name: (d && d.name) || this.runName, kind: "file",
          ok: !!(d && d.ok), dryRun: !!this.dryRun,
          passed: (d && d.passed) || 0, count: (d && d.count) || 0,
          duration_ms: (d && d.duration_ms) || (Date.now() - t0),
          ts: Date.now(), error: d && d.error,
        };
      } catch (e) {
        this.fileResult = { ok: false, error: e.message };
        this.logLine("err", "执行异常：" + e.message);
      } finally {
        this.fileBusy = false;
        this.runBusy = false;
        this.running = false;
      }
    },
    /**
     * 中止当前执行。
     *
     * ★ 审计修复 P0-7：原实现只把 runAbort 置 true —— 这个标志**只有前端
     *   execProgram 的 for 循环在看**，runFile 的真实下发在后端请求线程里跑，
     *   压根不知道有人点了中止 → 界面显示"已中止"、机器人把整份文件跑完。
     *   现在必须**同时通知后端**（POST /control/run-cancel），由后端在每步之间
     *   检查取消表并就地 break；后端回执 cancelled=true 才算真的停了。
     *   接口失败也不吞：软标志照常置位，日志里明说"中止可能未生效"。
     */
    async abortRun() {
      this.runAbort = true;
      this.running = false;
      this.logLine("warn", "已请求中止…");
      try {
        const r = await apiControl("/control/run-cancel", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            run_id: this.runId || null,
            filename: this.runKind === "file" ? this.runName : null,
          }),
        });
        if (r.ok) {
          const d = await r.json();
          this.logLine("ok", d.matched && d.matched.length
            ? `后端已受理中止（${d.matched.length} 个执行）`
            : "后端已受理中止（当前没有在跑的执行）");
        } else if (r.status === 401 || r.status === 403) {
          this.needAuth();
          this.logLine("err", "中止失败：控制令牌无效，请重新登录后再试");
        } else {
          this.logLine("err", `中止请求返回 ${r.status}，中止可能未生效！`);
        }
      } catch (e) {
        this.logLine("err", "中止请求失败，中止可能未生效：" + e.message);
      }
    },

    /**
     * 空格键的落点：运行中 → 中止；空闲 → 重复运行上一次的点位序列程序。
     *
     * ★ 三条自我约束（对应现场最容易出的三种误操作）：
     *   1) 空闲时**绝不**自己挑程序：没有 lastRunTarget 就只记一行日志，不做任何下发；
     *   2) 运行中只调用 abortRun()（软中止，当前步走完再停），不重复下发；
     *   3) 文件程序（runFile）不受空格控制 —— 它的运行态与点位序列不同，
     *      乱接会让"空格停下来的其实是另一条通道"。这里显式挡掉并给出提示。
     */
    runOrAbort() {
      if (this.running) {
        if (this.runKind !== "program") {
          this.logLine("warn", "当前运行的是文件程序，请在「程序执行」页用按钮停止");
          return;
        }
        this.abortRun();
        return;
      }
      if (!this.lastRunTarget) {
        this.logLine("info", "空格 = 运行/停止程序；请先在「程序执行」页选一个程序跑一次");
        return;
      }
      this.execProgram(this.lastRunTarget);
    },

    // ================= 急停 / 引擎状态 =================
    async doEstop() {
      this.estopBusy = true;
      try {
        this.stopTimers();                 // ★ 点动是独立线程，先停它再急停
        this.runAbort = true;
        // ★ 审计修复 P1-D9①：先停本地再问权限 —— 就算没令牌，点动线程也已经停了。
        // ★ 审计修复 P1-D6/D9②：急停**任何一条路径都必须有反馈**。
        //   原实现是 `if (needAuth()) return;` + 无 catch：没令牌 / 网络炸 / 后端 500
        //   三种情况界面都是"按了没反应"，现场会以为急停坏了去拍硬急停（或更糟：
        //   以为已经停了）。
        if (this.needAuth()) {
          this.logLine("err", "急停未发出：没有控制令牌（已弹出验证框，验证后请再次按下 Esc）");
          return;
        }
        const r = await apiControl("/control/estop", { method: "POST" });
        if (r.ok) {
          this.estopState = await r.json();
          this.logLine("err", "已下发急停");
        } else {
          this.logLine("err", `急停请求失败（HTTP ${r.status}），请立即改用硬急停并检查后端`);
          if (r.status === 401 || r.status === 403) this.needAuth();
        }
        await this.refreshJog();
      } catch (e) {
        this.logLine("err", "急停异常："
          + (e && e.message ? e.message : e)
          + "（未收到后端确认，请立即改用硬急停）");
      } finally { this.estopBusy = false; }
    },
    async resetEstop() {
      if (this.needAuth()) return;
      try {
        const r = await apiControl("/control/estop/reset", { method: "POST" });
        if (r.ok) {
          this.estopState = await r.json();
          this.logLine("ok", "急停已复位");
        } else {
          this.logLine("err", `急停复位失败（HTTP ${r.status}）`);
        }
      } catch (e) {
        this.logLine("err", "急停复位异常：" + (e && e.message ? e.message : e));
      }
    },
    /**
     * 吸气 / 停止吸气（不移动机器人）：写 40135.Bit1/Bit2 触发位 → 控制器常驻服务程序(200)执行。
     * ★ 与 jog 同真实下发双闸；控制器须 AUTO/远程且常驻程序(200)运行中才生效。
     *   action: "suck"（吸真空，**电平保持**，不自动停）| "release"（立即关断真空）。
     * ★ 2026-09-29：吸气取消 0.5s 自动停 → 状态必须**锁存**（latch）。
     *   旧实现 2s 后自动回 idle；保持型语义下阀还开着、界面却显示"空闲"，
     *   操作员会误判 → 属安全隐患。现在只有 release 成功才回 idle。
     */
    async vacuum(action) {
      if (!["suck", "release", "blow", "unblow"].includes(action)) return;
      if (this.needAuth()) return;

      // ★★ 安全优先：**停止吸气 / 放气请求绝不丢弃** ★★
      //   上一发（通常是吸气）还在飞时，后端 rc_vacuum 的前置守卫会以
      //   「吸触发位仍为 1（上一发未收尾），已拒绝」回 502 ——
      //   操作员慌乱中按"停止吸气/放气"，看到的是"按了没反应还报错"，而阀其实还开着。
      //   这里记下意图，等上一发收尾后立刻补发，保证"脱件"的语义最终一定生效。
      //   四种气路动作（吸/停止吸/放/停止放）里，后三种都是"让状态往安全/脱件方向走"的，
      //   一律排队不丢弃。
      if (action !== "suck" && this.vacuumBusy) {
        this.vacuumPending = action;
        this.logLine("info", "已排队：等上一发收尾后立即执行");
        return;
      }

      const prev = this.vacuumState;   // 失败时回退，绝不谎报阀状态
      const LABEL = { suck: "吸", release: "停止吸", blow: "放", unblow: "停止放" };
      const BIT = { suck: 1, release: 2, blow: 3, unblow: 4 };
      this.vacuumBusy = true;
      this.vacuumErr = null;
      this.vacuumState = action;
      this.logLine("info", "Web 触发" + LABEL[action] + "（40135.Bit" + BIT[action] + "）"
        + (action === "suck" || action === "blow" ? "，电平保持，需手动停止" : ""));
      try {
        const r = await apiControl("/control/vacuum", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action }),
        });
        if (r.status === 401 || r.status === 403) { this.vacuumState = prev; this.needAuth(); return; }
        if (r.ok) {
          // ★ 保持型语义：吸/放 都是"开了就一直保持"，直到对应的"停止"动作；
          //   停止吸/停止放 → 回到 idle（两者都关掉了）。
          if (action === "suck") this.vacuumState = "suck";
          else if (action === "blow") this.vacuumState = "blow";
          else this.vacuumState = "idle";
          this.logLine("ok", {
            suck: "吸已开启（保持中）",
            release: "已停止吸（真空阀关断）",
            blow: "放已开启（吹气保持中）",
            unblow: "已停止放（吹气阀关断）",
          }[action] || (LABEL[action] + "完成"));
        } else {
          let msg = "吸放触发失败";
          try {
            const d = await r.json();
            const inner = (d && d.detail !== undefined && typeof d.detail === "object" && d.detail !== null) ? d.detail : d;
            msg = (inner && (inner.message || inner.error || inner.detail))
              || (typeof d.detail === "string" ? d.detail : "") || ("HTTP " + r.status);
          } catch (e) { msg = "HTTP " + r.status; }
          this.vacuumErr = String(msg);
          this.vacuumState = prev;
          this.logLine("err", LABEL[action] + "失败：" + msg);
        }
      } catch (e) {
        this.vacuumErr = e && e.message ? String(e.message) : String(e);
        this.vacuumState = prev;
        this.logLine("err", "吸放触发异常：" + this.vacuumErr);
      } finally {
        this.vacuumBusy = false;
        // ★ 收尾即补发排队中的"停止吸气/放气"，不让它被丢掉
        if (this.vacuumPending) {
          const nx = this.vacuumPending;
          this.vacuumPending = "";
          this.vacuum(nx);
        }
      }
    },
    // =================================================================
    // ★ 2026-09-29 序列编辑器（「程序执行」页）
    //   设计见 docs/方案-程序执行序列编辑器（五类操作·经210执行）.md
    //   ★★ 六类操作：标记点 point / 吸 suck / 停止吸 release / 放 blow / 停止放 unblow / 等待 wait。
    //      四路气路全部**电平保持**（与控制器 210 的 Bit1~Bit4 一一对应）：
    //      只关真空阀挡不住残余负压、工件会吸住不掉 → 必须能单独"放"（吹气）；
    //      但也不能并入"停止吸"（有些场合只需松手不想吹气）。
    //      想"吹一下就收"：序列里排 [放, 等待 0.4s, 停止放]。
    //      后端 POST /control/seq 会硬校验，前端这里也不给别的入口。
    //   执行一律走 /control/run-file（items 模式）→ 后端逐步下发：
    //      标记点 → 写目标+触发 Bit0 → 控制器常驻 210 执行 MJOINT
    //      吸气/停止吸气 → 触发 Bit1/Bit2 → 210 写 io.DOut[N]
    //      等待 → 软件计时（可暂停冻结、可停止打断）
    // =================================================================

    /** 追加一步。type 只接受四类。 */
    addSeqStep(type) {
      const TYPES = ["point", "suck", "release", "blow", "unblow", "wait"];
      if (!TYPES.includes(type)) return;
      if (this.seqItems.length >= 200) { this.seqErr = "步骤数已达上限 200"; return; }
      if (type === "point") {
        const p = (this.points || [])[0];
        if (!p) {
          this.seqErr = "还没有已保存的点位 —— 请先在「点位执行」页示教并保存点位";
          return;
        }
        this.seqItems.push({ type: "point", point_id: p.id });
      } else if (type === "wait") {
        this.seqItems.push({ type: "wait", seconds: 1 });
      } else {
        this.seqItems.push({ type });
      }
      this.seqDirty = true; this.seqErr = ""; this.seqMsg = "";
    },
    removeSeqStep(i) {
      if (i < 0 || i >= this.seqItems.length) return;
      this.seqItems.splice(i, 1);
      this.seqDirty = true; this.seqMsg = "";
    },
    /** delta=-1 上移 / +1 下移（顺序就是执行顺序，用户自己排）。 */
    moveSeqStep(i, delta) {
      const j = i + delta;
      if (i < 0 || i >= this.seqItems.length || j < 0 || j >= this.seqItems.length) return;
      const [it] = this.seqItems.splice(i, 1);
      this.seqItems.splice(j, 0, it);
      this.seqDirty = true; this.seqMsg = "";
    },
    clearSeq(force = false) {
      if (!this.seqItems.length) return;
      if (!force && this.seqDirty && typeof window !== "undefined"
          && !window.confirm("清空当前编辑器里的 " + this.seqItems.length + " 步？")) return;
      this.seqItems = [];
      this.seqDirty = true; this.seqErr = ""; this.seqMsg = "";
    },
    setSeqPoint(i, pointId) {
      const it = this.seqItems[i];
      if (!it || it.type !== "point") return;
      it.point_id = Number(pointId);
      this.seqDirty = true; this.seqMsg = "";
    },
    setSeqWait(i, seconds) {
      const it = this.seqItems[i];
      if (!it || it.type !== "wait") return;
      let v = Number(seconds);
      if (!isFinite(v)) v = 1;
      it.seconds = Math.min(3600, Math.max(0.1, v));
      this.seqDirty = true; this.seqMsg = "";
    },
    /** 步骤 → 界面上一行文字（列表显示用）。 */
    seqLabel(it) {
      if (!it) return "";
      if (it.type === "point") {
        const p = (this.points || []).find((x) => x.id === it.point_id);
        return p ? p.name : ("点位 #" + it.point_id);
      }
      if (it.type === "wait") return it.seconds + " 秒";
      if (it.type === "suck") return "打开真空（保持）";
      if (it.type === "blow") return "开吹气（保持）";
      if (it.type === "unblow") return "关吹气";
      return "只关真空阀（不吹气）";
    },

    /** 保存成本地文件 programs/<名称>.json（保存后立刻出现在「本地程序」列表）。 */
    async saveSeq() {
      if (this.seqBusy) return;
      const name = (this.seqName || "").trim();
      if (!name) { this.seqErr = "请先填序列名称"; return; }
      if (!this.seqItems.length) { this.seqErr = "序列为空，请先添加步骤"; return; }
      this.seqBusy = true; this.seqErr = ""; this.seqMsg = "";
      try {
        const r = await apiControl("/control/seq", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, items: this.seqItems, speed_pct: this.speed }),
        });
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          this.seqDirty = false;
          this.seqMsg = (d.existed ? "已覆盖保存：" : "已保存：") + d.file + "（" + d.steps + " 步）";
          this.logLine("ok", "序列已保存为本地文件：" + d.file);
          await this.loadFiles();          // 让「本地程序」列表立刻可见
        } else {
          this.seqErr = this._msgOf(d, r.status);
          this.logLine("err", "保存序列失败：" + this.seqErr);
        }
      } catch (e) {
        this.seqErr = String((e && e.message) || e);
      } finally {
        this.seqBusy = false;
      }
    },

    /** 载入已保存的序列文件到编辑器。 */
    async loadSeq(file) {
      const nm = String(file || "").trim();
      if (!nm) return;
      if (this.seqDirty && typeof window !== "undefined"
          && !window.confirm("编辑器里有未保存的改动，载入会覆盖它们。继续？")) return;
      this.seqBusy = true; this.seqErr = ""; this.seqMsg = "";
      try {
        // ★ 必须 encodeURIComponent：序列名含中文，未编码的 URL 会被拒
        const r = await apiControl("/control/seq?name=" + encodeURIComponent(nm));
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) { this.seqErr = this._msgOf(d, r.status); return; }
        // 落盘格式（point_id / op / dwell_ms）→ 编辑器格式（type / seconds）
        this.seqItems = (d.items || []).map((it) => {
          if (!it || typeof it !== "object") return null;
          if (it.point_id != null) return { type: "point", point_id: Number(it.point_id) };
          const op = String(it.op || "");
          if (op === "suck") return { type: "suck" };
          if (op === "release") return { type: "release" };
          if (op === "wait") {
            const sec = Math.max(0.1, Math.round(((Number(it.dwell_ms) || 0) / 1000) * 10) / 10);
            return { type: "wait", seconds: sec };
          }
          return null;
        }).filter(Boolean);
        this.seqName = d.name || nm.replace(/\.json$/i, "");
        this.seqDirty = false;
        this.seqMsg = "已载入 " + (d.file || nm) + "（" + this.seqItems.length + " 步）";
        this.logLine("ok", "已载入序列：" + this.seqName);
      } catch (e) {
        this.seqErr = String((e && e.message) || e);
      } finally {
        this.seqBusy = false;
      }
    },

    /** 取出后端错误文案（detail 可能是字符串或对象）。 */
    _msgOf(d, status) {
      const dt = d && d.detail;
      if (typeof dt === "string" && dt) return dt;
      if (dt && typeof dt === "object") return dt.message || JSON.stringify(dt);
      if (d && (d.error || d.message)) return String(d.error || d.message);
      return "HTTP " + status;
    },

    /** 执行序列（dryRun=true 只校验不下发）。 */
    async runSeq(dryRun = false) {
      if (!this.seqItems.length) { this.seqErr = "序列为空，请先添加步骤"; return; }
      if (this.seqBusy || this.fileBusy || this.runBusy) {
        this.seqErr = "已有执行在进行，请先等它结束或点「停止执行」";
        return;
      }
      if (this.needAuth()) return;
      const name = (this.seqName || "").trim() || "未命名序列";
      this.seqBusy = true; this.seqErr = ""; this.seqMsg = "";
      this.runBusy = true; this.dryRun = !!dryRun;
      this.running = !dryRun; this.runDry = !!dryRun;
      this.runKind = "program";        // 与点位序列同型 → 步骤高亮走同一套
      this.runName = name; this.runRunName = name;
      this.runTotal = this.seqItems.length; this.runStep = 0;
      this.lastRun = null; this.runPaused = false; this.runState = null;
      this.logLine("info", (dryRun ? "试运行" : "执行") + "序列「" + name + "」："
                    + this.seqItems.length + " 步");
      if (!dryRun) this.startSeqPoll();
      try {
        const r = await apiControl("/control/run-file", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            items: this.seqItems, name, dry_run: !!dryRun, speed_pct: this.speed,
          }),
        });
        if (r.status === 401 || r.status === 403) { this.needAuth(); return; }
        let d = await r.json().catch(() => ({}));
        if (!r.ok) {
          this.seqErr = this._msgOf(d, r.status);
          this.logLine("err", "序列执行被拒：" + this.seqErr);
          return;
        }
        // ★★ 2026-10-10（B1）：异步化 —— 结果去轮询里取（下面的 startSeqPoll 让位给它）
        if (d && d.started) {
          if (d.run_id) this.runId = d.run_id;
          this.stopSeqPoll();
          d = await this._awaitRunResult();
        }
        this.fileResult = d;
        this.runStep = d.count || this.runTotal;
        if (d.count) this.runTotal = d.count;
        const bad = (d.steps || []).find((s) => s.ok === false) || null;
        this.lastRun = {
          name: d.name || name, kind: "program", ok: !!d.ok, dryRun: !!dryRun,
          passed: d.passed || 0, count: d.count || 0,
          duration_ms: d.duration_ms || 0, error: bad ? (bad.error || "") : "",
        };
        if (d.cancelled) {
          this.seqMsg = "已停止（执行到第 " + (d.count || 0) + " 步）";
          this.logLine("warn", "序列已被停止：" + this.seqMsg);
        } else if (d.ok) {
          this.seqMsg = (dryRun ? "试运行通过：" : "执行完成：")
                        + (d.passed || 0) + "/" + (d.count || 0) + " 步";
          this.logLine("ok", this.seqMsg);
        } else {
          this.seqMsg = "中断于第 " + (bad ? bad.index : "?") + " 步"
                        + (bad && bad.name ? "（" + bad.name + "）" : "")
                        + "：" + (bad && bad.error ? bad.error : "未知原因");
          this.logLine("err", "序列中断：" + this.seqMsg);
        }
      } catch (e) {
        this.seqErr = String((e && e.message) || e);
        this.logLine("err", "序列执行异常：" + this.seqErr);
      } finally {
        this.stopSeqPoll();
        this.seqBusy = false; this.runBusy = false;
        this.running = false; this.runPaused = false;
      }
    },

    /** 暂停。★ 语义如实：当前步结束后生效；等待步立即冻结计时。要立刻停用急停。 */
    async pauseSeq() {
      if (!this.running) { this.seqErr = "当前没有正在执行的序列"; return; }
      try {
        const r = await apiControl("/control/run-pause", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          this.runPaused = true;
          this.seqMsg = "已暂停（当前步结束后生效）";
          this.logLine("warn", "已请求暂停：当前步结束后生效（等待步则立即冻结计时）");
        } else {
          this.logLine("err", "暂停失败：" + this._msgOf(d, r.status));
        }
      } catch (e) { this.logLine("err", "暂停请求异常：" + ((e && e.message) || e)); }
    },
    async resumeSeq() {
      try {
        const r = await apiControl("/control/run-resume", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          this.runPaused = false;
          this.seqMsg = "已继续执行";
          this.logLine("ok", "已继续执行");
        } else {
          this.logLine("err", "继续失败：" + this._msgOf(d, r.status));
        }
      } catch (e) { this.logLine("err", "继续请求异常：" + ((e && e.message) || e)); }
    },
    /** 停止执行（复用 run-cancel：能打断飞行中的那一发）。 */
    async stopSeq() {
      this.stopSeqPoll();
      this.runPaused = false;
      await this.abortRun();
      this.seqMsg = "已请求停止执行";
    },

    /** 执行中轮询进度（画进度条 + 当前步高亮 + 暂停态）。 */
    /**
     * ★★ 2026-10-10（B1 异步化）：等后台执行跑完。
     *
     * 后端 `/control/run-file` 已改成"立即返回 + 后台线程执行"，POST 只回
     * `{started, run_id, kind}`；**最终结果**在 `/control/run-state` 的 `state.result` 里
     * （字段与老的 HTTP 响应体**完全一致**：ok/count/passed/steps/cancelled/...）。
     * 这里边等边把进度喂给界面（替代 startSeqPoll，避免两个轮询打架）。
     * ⚠ 纯前端 await，不占后端 worker；超时如实回报，**不假装成功**。
     */
    async _awaitRunResult(timeoutMs = 30 * 60 * 1000) {
      const t0 = Date.now();
      while (Date.now() - t0 < timeoutMs) {
        await new Promise((res) => setTimeout(res, 400));
        try {
          const r = await apiControl("/control/run-state");
          if (!r.ok) continue;
          const d = await r.json();
          const s = d && d.state;
          if (!s) continue;
          this.runState = s;
          if (s.index) this.runStep = s.index;
          if (s.total) this.runTotal = s.total;
          this.runPaused = !!s.paused;
          if (s.result) return s.result;
        } catch (e) { /* 轮询失败不影响执行本身 */ }
      }
      return { ok: false,
               error: "等待执行结果超时（执行可能仍在后台进行，请稍后刷新查看运行日志）" };
    },

    startSeqPoll() {
      this.stopSeqPoll();
      if (typeof setInterval !== "function") return;
      this._seqPoll = setInterval(async () => {
        try {
          const r = await apiControl("/control/run-state");
          if (!r.ok) return;
          const d = await r.json();
          const s = d && d.state;
          if (!s) return;
          this.runState = s;
          if (s.index) this.runStep = s.index;
          if (s.total) this.runTotal = s.total;
          this.runPaused = !!s.paused;
        } catch (e) { /* 轮询失败不影响执行本身 */ }
      }, 500);
    },
    stopSeqPoll() {
      if (this._seqPoll) { clearInterval(this._seqPoll); this._seqPoll = null; }
    },

    async refreshState() {
      if (!useAuthStore().controlActive) { this.estopState = null; return; }
      const r = await apiControl("/control/state");
      if (r.ok) this.estopState = await r.json();
      await this.refreshJog();
    },

    // ================= 会话收尾 =================
    /** 用后端最新目标刷新本地姿态基准。 */
    refreshSessionPose() {
      setTimeout(() => this.syncTeachFromRobot(), 120);
    },
    /** 离开执行类视图：停点动、收残影。
     *  ★ 姿态权威模型下不再有"交还姿态控制权"这回事 —— 遥测永远不被抑制，
     *    demo 只有 RealMonitor 能设，exec 无权也不需要碰它。 */
    leaveView() {
      this.stopTimers();
      showGhost(false);
      highlightJoint(-1);
      this.pinnedPointId = null;
    },
    dispose() {
      this.stopTimers();
      this.stopSeqPoll();
      if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
      this.leaveView();
    },
  },
});

// ★ 审计修复 P1-D1：点动是"按住才动"的死人开关（后端 1.5s 收不到 keepalive 自动停）。
//   但窗口失焦 / 切到后台时浏览器不再派发 keyup，前端的 endJog 永远不会被触发，
//   只能干等后端 keepalive 超时 —— guide 里承诺的"失焦即停"就成了空话。
//   这里在离开窗口（blur / hidden）时立刻补一发 stop，双保险。
if (typeof window !== "undefined") {
  const stopJogIfHolding = () => {
    try {
      const ex = useExecStore();
      if (ex && ex.holding >= 0) ex.endJog();
    } catch (e) { /* pinia 未就绪 / 已卸载 */ }
  };
  window.addEventListener("blur", stopJogIfHolding);
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopJogIfHolding();
    });
  }
}
