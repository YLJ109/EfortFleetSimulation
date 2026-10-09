// =====================================================================
// Pinia store：分拣 3D（A/B 托盘盒 + 18 槽位 + C 区摄像头）的**全局唯一装填者**。
//
// ★ 为什么要有这个 store（用户需求 2026-10-08）：
//   "真实监控，模拟仿真，点位执行，程序执行，分拣，AI执行**全部要显示**"
//   —— 改造前只有「分拣」页在挂载时拉一次 /api/sorting/blocks，并在离开页面时
//      showSortingBlocks(false)，于是别的页面看到的是一台"空机器人"。
//   现在把「装填 + 状态轮询 + 常显」收敛到这里：App.vue 拿到控制令牌后调一次
//   ensure()，之后**切页面不再隐藏**，任何一页都能看到托盘/方块/摄像头在动。
//
// ★ 分工（不新增状态机，只是搬运既有接口）：
//   · 摆位（一次性）：GET /api/sorting/blocks → manager.setSortingSlots()
//   · 状态（轮询）  ：GET /api/sorting/state  → manager.updateSortingState()
//   · 分拣页自己 800ms 拉一次状态并喂 3D —— 本 store 在分拣页**不重复打**（省一半请求）
//
// ★ 为什么要"离开分拣页也不隐藏"：方块是**执行状态的显示**，不是分拣页的装饰。
//   操作员在真实监控页看机器人跑分拣时，也需要看到哪一格已取、手上是什么颜色。
// =====================================================================
import { defineStore } from "pinia";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "./auth.js";
import { useRobotStore } from "./robot.js";
import { useExecStore } from "./exec.js";
import {
  setSortingSlots, updateSortingState, showSortingBlocks, sortingBlocksReady,
} from "../three/manager.js";

/** 状态轮询周期（ms）。1000ms：够快看出取放动作，又不会把后端打满。 */
export const SORT3D_POLL_MS = 1000;

let timer = null;

export const useSorting3dStore = defineStore("sorting3d", {
  state: () => ({
    info: null,      // setSortingSlots() 的自检结果（平面/尺寸/已画槽位/托盘/摄像头）
    err: "",         // 装填失败原因（界面如实显示，不糊过去）
    loaded: false,   // 摆位是否已灌进 3D
    busy: false,     // 正在装填（防重复请求）
    overlapLogged: false,
  }),
  getters: {
    /** 画不出来的槽位原因（未示教 / 重叠风险 / 共面性偏差）。 */
    warned: (s) => (s.info && s.info.warned) || [],
    /** A/B 两个托盘盒的尺寸(mm)：[[宽,深,壁高], ...]。 */
    traySizes: (s) => (s.info && s.info.trays) || [],
    cameraBase: (s) => (s.info && s.info.camBase) || null,
    ready: (s) => !!s.info,
  },
  actions: {
    /**
     * 拉一次摆位并灌进 3D（幂等：已加载就跳过，除非 force）。
     * 令牌未就绪时**不打**（否则必然 401 刷屏），交给 controlActive 监听再调。
     */
    async ensure(force = false) {
      const auth = useAuthStore();
      if (!auth.controlActive) { this.err = "需要控制令牌"; return false; }
      if (this.busy) return false;
      // ★ `loaded` 不够：manager.disposeAll()（整页卸载 / HMR）会把 3D 对象清成 null，
      //   此时 loaded 仍为 true → 永远不再重建，界面变成"托盘和料都没了"。
      //   所以还要问一句"3D 对象是否真的还在"。
      if (this.loaded && !force && sortingBlocksReady()) {
        showSortingBlocks(true);
        return true;
      }
      if (this.loaded && !sortingBlocksReady()) this.loaded = false;   // 已被释放，重新装填
      this.busy = true;
      this.err = "";
      try {
        const r = await apiControl("/sorting/blocks");
        if (r.status === 401 || r.status === 403) {
          this.err = "需要控制令牌";
          this.loaded = false;
          return false;
        }
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) {
          this.err = d.message || d.detail || ("HTTP " + r.status);
          this.loaded = false;
          return false;
        }
        this.info = setSortingSlots(d);
        this.loaded = true;
        showSortingBlocks(true);
        // 重叠/未示教只提示一次（每次轮询都刷会把运行日志淹掉）
        if (!this.overlapLogged && (d.overlaps || []).length) {
          this.overlapLogged = true;
          const names = d.overlaps.slice(0, 3).map((o) => o.a + "/" + o.b).join("、");
          try {
            useExecStore().logLine("warn",
              `[分拣] 3D：${d.overlaps.length} 对槽位有重叠风险，已跳过不画（${names}…）`);
          } catch (e) { /* 运行日志不可用时忽略，不影响 3D */ }
        }
        return true;
      } catch (e) {
        this.err = "3D 槽位加载失败：" + ((e && e.message) || e);
        return false;
      } finally {
        this.busy = false;
      }
    },

    /** 拉一次执行状态喂给 3D（各页共用）。 */
    async tick() {
      const auth = useAuthStore();
      const robot = useRobotStore();
      if (!auth.controlActive || !this.loaded) return;
      // 分拣页自带的 800ms 轮询已经在喂 3D，这里不重复打
      if (robot.activeView === "sort") return;
      try {
        const r = await apiControl("/sorting/state");
        if (!r.ok) return;
        const d = await r.json();
        updateSortingState(d.state || null);
      } catch (e) { /* 轮询失败不影响其它页面 */ }
    },

    start() {
      if (timer) return;
      this.ensure();
      this.tick();
      timer = setInterval(() => this.tick(), SORT3D_POLL_MS);
    },
    stop() {
      if (timer) { clearInterval(timer); timer = null; }
    },
    /** 登录/令牌变化后调用：重新装填（令牌可能换了）。 */
    async reload() {
      this.loaded = false;
      this.overlapLogged = false;
      return this.ensure(true);
    },
  },
});
