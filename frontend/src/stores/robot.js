// =====================================================================
// Pinia store：机器人状态（元数据 / 限位 / 姿态 / 连接 / 模拟关节）
// 纯数据层，不直接操作 Three.js。
//
// ================== ★★ 姿态权威模型（阶段 7 重构） ==================
//
// 改造前的问题（三页实时性不一致的**总根源**）：
//   store 里只有一个 `latestPose`，而"是否允许远端姿态写入"由一个叫
//   `localDemo` 的隐性全局开关控制。`stores/exec.js` 有 5 处 `setLocalDemo(true)`
//   （每次下发后都把远端姿态掐掉），只有 `leaveView()` 会置回 false。
//   ⇒ 只要在「点位执行」页做过一次下发，该页的关节/TCP 读数就**永久冻结**；
//     而「真实监控」页却能实时刷新 —— 同一个 store，两个行为。
//
// 改造后的模型：把"谁在提供姿态"显式拆成三个**互斥**来源，并明确优先级。
//
//   ① override   本地沙盘覆盖（模拟仿真这类"自己演自己"的模型）
//                 ★ 只在 owner === activeView 时生效，切页后残留不会污染别页。
//   ② demo       未连接占位（真机不可达时把显示姿态钉成全 0）
//                 ★ 唯一允许"接管显示姿态"的特例，只有 RealMonitor 能设。
//   ③ telemetry  后端遥测 —— **唯一真值，永远不被任何视图抑制**。
//                 真机模式下它是 Modbus 真实读回；模拟模式下它是仿真机体位。
//
// 三个派生值（全站只此一份判据，任何视图都不得自己再算一个）：
//   · displayQ / displayTcp —— 3D 应当显示什么（由 App.vue 的渲染循环统一下发）
//   · readoutQ / readoutTcp —— 读数卡显示什么（≡ displayQ，保证"数字和画面对得上"）
//   · poseSource            —— 当前姿态的来源标签（界面上显示，便于现场排障）
//
// ★ 铁律：本文件之外**不允许**调用 three 的 applyRobotPose()。
//   改造前 7 个组件/store 各自调 applyRobotPose，于是"谁最后写谁赢"，
//   跑程序时 3D 只在指令回执那一跳动一下、看不到机器人真的在走。
//   现在只有 App.vue 的渲染循环调一次（tools/verify_pose_authority.mjs 守着这条）。
// =====================================================================
import { defineStore } from "pinia";
import { apiUrl } from "../config.js";
import { connectWs } from "../net/ws.js";
import { initRobots, loadOfficialModel } from "../three/manager.js";
import { resolvePose } from "./poseAuthority.js";
import { useRcReadyStore } from "./rcReady.js";

export const DEFAULT_LIMITS = [
  { name: "J1", min: -170, max: 170 },
  { name: "J2", min: -170, max: 90 },
  { name: "J3", min: -85, max: 150 },
  { name: "J4", min: -180, max: 180 },
  { name: "J5", min: -115, max: 115 },
  { name: "J6", min: -360, max: 360 },
];

/** 六轴零位 / 零 TCP 的构造器（必须每次新建，Pinia state 不能共享同一引用）。 */
export const zeroQ = () => [0, 0, 0, 0, 0, 0];
export const zeroTcp = () => ({ x: 0, y: 0, z: 0 });

/**
 * ★ P1-D8：多久没收到新遥测就算"冻住"。
 *   后端 `ws_push_hz` 默认 20Hz（config/robot.yaml），正常每 50ms 一帧；
 *   取 3s 是 60 帧的余量 —— 只有后端真的卡死 / 采集线程挂了才会触发，
 *   网络抖动、切页、标签页后台都不会误报（后台页 RAF 停帧不影响收帧）。
 */
export const TELEM_STALE_MS = 3000;

export const useRobotStore = defineStore("robot", {
  state: () => ({
    meta: null,
    limits: DEFAULT_LIMITS.map((l) => ({ ...l })),

    // ---- ① 遥测：后端推来的唯一真值（永不被抑制）----
    telemetry: {
      q: zeroQ(),              // [j1..j6]（度）
      tcp: zeroTcp(),          // {x,y,z}（mm）
      simulated: false,        // 本帧是否来自仿真（而非真机读回）
      cmd: null,               // ★ 后端记录的最后一次指令目标（motion.last_target）
      cmdTcp: null,            // ★ 该目标对应的 TCP
      tracking: false,         // ★ 仿真机是否正在**走向** cmd（Stage C：有状态仿真）
      at: 0,                   // 本帧到达时刻（性能排查用）
    },

    // ★ E2 到位自证：后端把"在飞那一段"的残差快照**捎在同一条位姿帧**里推来
    //   （判据只有后端一份 —— 前端只渲染，不再自己算，§5.5 铁律④同屏同源）。
    //   residualSeq = **只增不减**的水位（铁律①：不拿对象引用/长度当"变了没"的判据）。
    residual: null,
    residualSeq: 0,

    // ---- ② 未连接占位：唯一被允许接管显示姿态的特例（未连接 → 全 0）----
    demo: { on: false, q: zeroQ(), tcp: zeroTcp() },

    // ---- ③ 本地沙盘覆盖（模拟仿真）----
    override: null,            // { q, tcp, owner }

    // ---- 连接与元数据 ----
    simulated: false,          // 兼容字段：与 telemetry.simulated 同步
    connected: false,
    // ★ C3：**外部时钟水位**（由 App 的 1s 时钟在 `sweepTelemetry()` 里无条件推进）。
    //   用途 = 给"按时间窗取样本"的趋势组件当 `asOfNow`：`Date.now()` 不是响应式依赖，
    //   组件自己调它就会退化成"停在渲染那一秒"（与 rc.clk / link.clk / metrics.clk 同一治法）。
    clk: 0,
    // ★ 审计修复 P1-D8：遥测是否已"冻住"（WS 还连着，但后端不再推新帧）。
    //   telemetry.at 以前**只写不读** —— 后端卡死 / 采集线程崩溃 / WS 半死时
    //   画面定格，界面却还挂着"已连接(真实)"。因为 Date.now() 不是响应式依赖，
    //   不能只写个 `stale` getter 就完事（它不会自己重新求值）：由 App 的 1 秒
    //   时钟调 sweepTelemetry() 翻转本标记，applyPose 收到新帧时立即复位。
    telemetryStale: false,
    simQ: zeroQ(),             // 模拟仿真页的本地教学模型（与遥测无关）
    selIdx: 0,
    activeView: "live",        // live | sim | point | program | ops | settings | about
  }),

  getters: {
    axes: (s) => (s.meta && s.meta.axes) || ["J1", "J2", "J3", "J4", "J5", "J6"],
    robotName: (s) => (s.meta && s.meta.robot) || "ER8-700H",
    calibrationPending: (s) => !!(s.meta && s.meta.dh_calibration_pending),

    /**
     * ★ 3D 应当显示的姿态 —— 全站唯一的判据。
     * 优先级数学在 stores/poseAuthority.js（纯函数，headless 钉死每个分支），
     * 这里只做接线，不得再写一遍优先级逻辑。
     */
    displayQ(s) {
      return resolvePose(s).q;
    },
    displayTcp(s) {
      return resolvePose(s).tcp;
    },

    /** ★ 读数卡显示什么 —— ≡ displayQ，保证"数字与画面对得上"。 */
    readoutQ: (s) => s.displayQ,
    readoutTcp: (s) => s.displayTcp,

    /** 姿态来源标签（界面展示 + 排障）。 */
    poseSource(s) {
      return resolvePose(s).source;
    },

    /** 兼容别名：老代码读 latestPose / latestTcp / tcp 的地方继续可用（= 遥测）。 */
    latestPose: (s) => s.telemetry.q,
    latestTcp: (s) => s.telemetry.tcp,
    tcp: (s) => s.telemetry.tcp,
  },

  actions: {
    /** 拉取元数据并初始化三套机器人模型。 */
    async loadMeta() {
      try {
        const r = await fetch(apiUrl("/meta"));
        if (r.ok) this.meta = await r.json();
      } catch (e) {
        /* 后端不可用时用默认值兜底 */
      }
      const dh = this.meta && this.meta.dh && this.meta.dh.joints &&
        this.meta.dh.joints.length === 6 ? this.meta.dh : null;
      const mounting = (this.meta && this.meta.mounting) || "floor";
      if (this.meta && this.meta.joint_limits && this.meta.joint_limits.length === 6) {
        this.limits = this.meta.joint_limits;
      }
      initRobots(dh, mounting);
      loadOfficialModel();
    },

    setStatus(ok) {
      this.connected = ok;
    },

    /**
     * ★ P1-D8：由 App 的 1 秒时钟驱动，翻转"遥测冻住"标记。
     *   必须做成 action 而不是 getter —— getter 里的 Date.now() 不是响应式依赖，
     *   帧一停就不会有人重新求值，标记永远停在 false（这正是原审计问题的形态）。
     */
    sweepTelemetry() {
      const at = this.telemetry.at;
      const stale = !!at && (Date.now() - at) > TELEM_STALE_MS;
      if (stale !== this.telemetryStale) this.telemetryStale = stale;
      // ★ C3：无条件推进水位（帧一停，telemetry.at 就不再变，只有这里还在走）
      this.clk = Date.now();
    },

    /**
     * ★ 接收一帧遥测。**绝不在这里做任何抑制** ——
     * 改造前这里有一句 `if (this.localDemo) return;`，是"读数冻结"的直接成因。
     */
    applyPose(p) {
      if (!p) return;
      this.simulated = !!p.simulated;
      this.connected = true;
      this.telemetryStale = false;   // ★ P1-D8：来新帧即复位"冻住"标记
      this.telemetry.q = [p.j1, p.j2, p.j3, p.j4, p.j5, p.j6].map((v) => Number(v) || 0);
      if (p.tcp) this.telemetry.tcp = { x: +p.tcp.x || 0, y: +p.tcp.y || 0, z: +p.tcp.z || 0 };
      this.telemetry.simulated = !!p.simulated;
      // 后端在模拟模式下会把"最近一次指令目标"一并推来（Stage C）；
      // 老后端没有这两个字段 → 保持上一次的值，前端自动降级到"显示 cmd"。
      if (Array.isArray(p.cmd) && p.cmd.length === 6) {
        this.telemetry.cmd = p.cmd.map((v) => Number(v) || 0);
        this.telemetry.cmdAt = Number(p.cmd_at) || Date.now();
      }
      if (p.cmd_tcp && typeof p.cmd_tcp === "object") {
        this.telemetry.cmdTcp = { x: +p.cmd_tcp.x || 0, y: +p.cmd_tcp.y || 0, z: +p.cmd_tcp.z || 0 };
      }
      if (typeof p.tracking === "boolean") this.telemetry.tracking = p.tracking;
      // ★ E2 到位自证：帧里带 residual 就更新；不带 = 当前没有在飞的段 → 如实清空
      //   （绝不让上一段的"已到位"停在屏上冒充这一帧的事实）。
      const r = p.residual && typeof p.residual === "object" ? p.residual : null;
      const prev = this.residual;
      const changed = (!!r) !== (!!prev)
        || (r && prev && (r.id !== prev.id || r.samples !== prev.samples));
      this.residual = r;
      if (changed) this.residualSeq += 1;
      this.telemetry.at = Date.now();
    },

    /** ② 未连接占位：开/关 + 写入占位姿态（只有 RealMonitor 调）。 */
    setDemo(on, q, tcp) {
      this.demo.on = !!on;
      if (q && q.length === 6) this.demo.q = q.map((v) => Number(v) || 0);
      if (tcp) this.demo.tcp = { x: +tcp.x || 0, y: +tcp.y || 0, z: +tcp.z || 0 };
      if (!on) { this.demo.q = zeroQ(); this.demo.tcp = zeroTcp(); }
    },

    /**
     * ★ 记录一次前端下发的指令目标（exec store 专用入口）。
     * 作用：模拟模式下后端还没有推 cmd 字段（Stage C 之前），这里先在**本地**
     * 把 cmd 记上，displayQ 的"指令即位置"分支就有据可依 —— 3D 会走到目标位
     * 并停在那里，而不是被无关的正弦扫掠拽走。
     * 真机模式下 simulated=false，displayQ 走遥测分支，本字段不影响显示。
     * Stage C 后端推 cmd 后，applyPose 会用服务端值覆盖本字段，本地值自然失效。
     */
    noteCommand(target, tcp) {
      if (target && target.length === 6) {
        this.telemetry.cmd = target.slice(0, 6).map((v) => Number(v) || 0);
        this.telemetry.cmdAt = Date.now();
      }
      if (tcp && typeof tcp === "object") {
        this.telemetry.cmdTcp = { x: +tcp.x || 0, y: +tcp.y || 0, z: +tcp.z || 0 };
      }
    },

    /** ③ 本地沙盘覆盖：owner 声明自己是谁（必须等于 activeView 才生效）。 */
    setOverride(owner, q, tcp) {
      this.override = {
        owner: String(owner),
        q: (q || zeroQ()).slice(0, 6).map((v) => Number(v) || 0),
        tcp: tcp ? { x: +tcp.x || 0, y: +tcp.y || 0, z: +tcp.z || 0 } : null,
      };
    },
    clearOverride(owner) {
      if (!this.override) return;
      if (owner === undefined || this.override.owner === owner) this.override = null;
    },

    setSimQ(q) {
      this.simQ = q.slice();
    },
    setSelIdx(i) {
      this.selIdx = i;
    },
    setActiveView(v) {
      this.activeView = v;
    },

    /** 建立 WebSocket 连接，姿态推送到 store。 */
    connect() {
      // ★ 保存连接句柄：改造前直接丢弃返回值，姿态 WS 从此无法关闭
      //   （页面销毁 / 手动重连都做不到，只能等浏览器回收）。
      this._ws = connectWs({
        onStatus: (ok) => this.setStatus(ok),
        onPose: (p) => this.applyPose(p),
        onRcStatus: (rc) => {
          // 经 WS 注入控制器寄存器快照 → rcReady store（替代 /rc-status 轮询）。
          // 回调内惰性取 store，避免顶部循环依赖。
          try { useRcReadyStore().ingest(rc); } catch (e) { /* 忽略 */ }
        },
      });
    },
    /** 主动断开姿态 WS（页面销毁 / 排障用）。 */
    disconnect() {
      if (this._ws && typeof this._ws.close === "function") {
        try { this._ws.close(); } catch (e) { /* 忽略 */ }
      }
      this._ws = null;
    },
  },
});
