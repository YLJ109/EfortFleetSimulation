// =====================================================================
// Pinia store：界面偏好（右栏宽度 / 右栏折叠 / 当前页面 / 叠加窗开关）
//
// ★ 为什么单独一个 store 而不是各组件各存一份 localStorage：
//   - 右栏宽度被"真实监控/模拟仿真/点位执行/程序执行"四个视图共用，
//     各存一份会出现"在 A 页面拖宽了，切到 B 页面又变回去"的割裂感；
//   - 当前页面要在 App.vue 的 Tab 与各视图之间共享；
//   - 刷新页面后恢复，是用户的明确要求。
//
// 所有写入都做 try/catch：无痕模式 / 禁用 localStorage 时不能整个页面崩掉。
// =====================================================================
import { defineStore } from "pinia";

export const LS_KEY = "efort.ui.v1";

export const SIDE_MIN = 240;   // 右栏最小宽度（再窄卡片就不可用了）
export const SIDE_MAX = 760;   // 右栏最大宽度（再宽 3D 视口就没意义了）
export const SIDE_DEF = 400;   // 默认宽度：400 是"点位执行/程序执行"两页卡片不换行的舒适宽度
export const SIDE_DEF_LEGACY = 320;   // 上一版默认值，仅用于升级迁移（见 resolveSideWidth）

/**
 * 视口窄于该宽度时右栏改为上下堆叠，拖拽/折叠都不适用（由组件用 matchMedia 判断）。
 * ★ 数值 = 3D 视口最小宽度(720) + 默认侧栏宽度。默认侧栏一改，这里必须跟着改，
 *   否则中等尺寸窗口下 3D 视口会被悄悄压扁（例如仍是 1040 时视口只剩 640）。
 */
export const COMPACT_PX = 1120;

/** 3D 视口的最小可用宽度，COMPACT_PX 由它推出（便于自查，勿单独改）。 */
export const VIEW_MIN = 720;

export function clampSideWidth(v) {
  // ★ 先挡掉"看着像没值、Number() 却会转成 0"的输入（null / "" ）：
  //   不挡的话会被夹到最小值 240，用户看到侧栏突然缩成最窄，像是界面坏了。
  if (v === null || v === undefined || v === "") return SIDE_DEF;
  const n = Number(v);
  if (!Number.isFinite(n)) return SIDE_DEF;
  return Math.max(SIDE_MIN, Math.min(SIDE_MAX, Math.round(n)));
}

/**
 * 解析持久化宽度，并把「上一版的默认值」升级成本版默认值。
 *
 * 为什么需要：宽度持久化在 localStorage 里，**只改 SIDE_DEF 的话老用户永远看不到新默认值** ——
 * 他本地存着 320，启动时照样读回 320，表现就是"你根本没改"。
 *
 * 判定依据 `defW`：每次保存时把**当时的默认值**一起记下来，于是
 *   - 存的宽度 == 存的那一版的默认值 → 用户**从没手动调过** → 跟随新默认值；
 *   - 存的宽度 != 存的那一版的默认值 → 用户**自己拖过** → 原样保留，绝不覆盖。
 * 老数据没有 `defW` 字段，只可能是旧默认值 320 这一种情况，故按 SIDE_DEF_LEGACY 判定。
 *
 * 代价（可接受）：升级那一次，手动拖到过 320 的人也会被挪到 400，重新拖一下即可；
 * 之后因为记了 `defW`，再改默认值也不会影响他。
 *
 * @param {*} saved      localStorage 里存的宽度
 * @param {*} savedDef   存这份数据时的默认值（缺省视为旧版默认值）
 * @param {number} def   本版默认值
 */
export function resolveSideWidth(saved, savedDef = SIDE_DEF_LEGACY, def = SIDE_DEF) {
  const w = clampSideWidth(saved);
  const untouched = clampSideWidth(savedDef) === w;
  return untouched ? def : w;
}

function loadRaw() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEY) || "null");
    return s && typeof s === "object" ? s : {};
  } catch (e) {
    return {};
  }
}

export const useUiStore = defineStore("ui", {
  state: () => {
    const s = loadRaw();
    return {
      sideWidth: resolveSideWidth(s.sideWidth, s.defW),
      sideCollapsed: s.sideCollapsed === true,
      /** 当前页面 key（由 App.vue 校验是否在已知 tab 里，非法值回落到 live） */
      view: typeof s.view === "string" ? s.view : "live",
      /** 执行页叠加的摄像头窗口是否展开（点位执行 / 程序执行共用） */
      camVisible: s.camVisible !== false,
      /**
       * 底栏状态区（五选一）当前展开的片 key（"" = 全部收起）。
       * 片顺序：log(运行日志) / robot / camera / run_mode / link(真机链路)。
       *
       * ★ 放在 store 而不是 StatusStrip 的局部 ref：App.vue 的全局键盘处理要知道
       *   "现在有没有**用户主动打开的**浮层"，才能在按 Esc 时先关浮层而不是直接急停。
       * ★ **不持久化**：这是瞬时状态，刷新后回到默认（见初值 "log"）。
       * ★★ 2026-10-11 用户口径：**进入页面默认开「运行日志」**（运行日志在最左侧）。
       */
      statusPopover: "log",
    };
  },
  getters: {
    /**
     * 当前展开的是否算"弹层"（Esc 应先关它，而不是急停）。
     * ★★ "log" 不算：它是**默认常开**的被动读数面板，不是用户主动打开的浮层 ——
     *   若把它也算弹层，则 Esc 会被永久吃掉、再也触发不了急停（安全回归）。
     *   其余四片（robot/camera/run_mode/link）是用户主动点开的 → 算弹层。
     */
    popoverModal: (s) => !!s.statusPopover && s.statusPopover !== "log",
  },
  actions: {
    _persist() {
      try {
        localStorage.setItem(LS_KEY, JSON.stringify({
          v: 2,
          defW: SIDE_DEF,     // ★ 记下写入时的默认值，供下次升级判断"用户有没有手动调过"
          sideWidth: this.sideWidth,
          sideCollapsed: this.sideCollapsed,
          view: this.view,
          camVisible: this.camVisible,
        }));
      } catch (e) { /* 忽略：无痕模式等场景下存不了也不影响使用 */ }
    },
    setSideWidth(v) {
      const w = clampSideWidth(v);
      if (w === this.sideWidth) return w;
      this.sideWidth = w;
      this._persist();
      return w;
    },
    setSideCollapsed(v) {
      this.sideCollapsed = !!v;
      this._persist();
    },
    toggleSide() { this.setSideCollapsed(!this.sideCollapsed); },
    resetSide() { this.setSideWidth(SIDE_DEF); },
    setView(v) {
      if (typeof v !== "string" || !v) return;
      if (this.view === v) return;
      this.view = v;
      this._persist();
    },
    setCamVisible(v) {
      this.camVisible = !!v;
      this._persist();
    },
    toggleCamVisible() { this.setCamVisible(!this.camVisible); },
    /** 底栏状态灯浮层：同一时刻只允许开一个（三个并排弹会挡住整个底栏）。 */
    setStatusPopover(k) { this.statusPopover = typeof k === "string" ? k : ""; },
    toggleStatusPopover(k) { this.statusPopover = this.statusPopover === k ? "" : k; },
    closeStatusPopover() { this.statusPopover = ""; },
  },
});
