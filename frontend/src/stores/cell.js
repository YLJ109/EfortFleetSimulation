import { defineStore } from "pinia";
import { apiUrl } from "../config.js";

// =====================================================================
// ★ E7 一格三证（只读复盘）：照片 / 判色结论 / 机器人位姿 按**同一个格号**装订。
//
// 这里**没有任何动作**：只有 GET `/api/sorting/cells`（不需要控制令牌 ——
// 观测不属于控制，与 `/api/twin/residual` 同口径）。
//
// ★ 水位只用后端给的 `latest_seq`（**只增不减**），不用 `records.length`
//   —— 内存有上限（200 条），满了长度就恒定，用长度判断"有没有新记录"会静默失效
//   （AGENTS.md §5.5 铁律①，与 `log.length` 那次同类事故）。
//   后端重启后 `latest_seq` 有意从 0 重新开始（本机开发环境不是数据库，不假装持久），
//   这种情况**如实标注**"水位归零（后端重启）"，不悄悄丢掉差异。
// =====================================================================

const POLL_MS = 2500;
const LIMIT = 40;

export const useCellStore = defineStore("cell", {
  state: () => ({
    records: [],
    stats: { total: 0, cells: [] },
    latestSeq: 0,
    fresh: 0,             // 距上次读取新增了几条（用水位算，不用数组长度）
    rebooted: false,      // 水位倒退 = 后端重启（如实说明，不静默）
    err: "",
    busy: false,
    loaded: false,
    selected: "",         // 选中的格子（点格子表切换）
    note: "",
    timer: null,
  }),
  getters: {
    cells(state) { return state.stats.cells || []; },
    hasData(state) { return state.loaded && !state.err; },
    selectedRecords(state) {
      if (!state.selected) return [];
      return state.records.filter((r) => r.cell_id === state.selected);
    },
    // 最近几条（没选格子时展示）
    latestRecords(state) { return state.records.slice(0, 6); },
    summary(state) {
      const c = state.stats.cells || [];
      const sum = c.reduce((a, x) => ({
        n: a.n + (x.n || 0),
        complete: a.complete + (x.complete || 0),
      }), { n: 0, complete: 0 });
      return { cells: c.length, ...sum };
    },
  },
  actions: {
    async load() {
      this.busy = true;
      try {
        const res = await fetch(apiUrl(`/sorting/cells?limit=${LIMIT}`));
        const d = await res.json().catch(() => ({}));
        if (!res.ok || !d.ok) {
          this.err = `三证读取失败（HTTP ${res.status}）`;
          return;
        }
        const seq = Number(d.latest_seq) || 0;
        if (seq < this.latestSeq) {
          this.rebooted = true;
          this.fresh = seq;
        } else {
          this.rebooted = false;
          this.fresh = seq - this.latestSeq;
        }
        this.latestSeq = seq;
        this.records = d.records || [];
        this.stats = d.stats || { total: 0, cells: [] };
        this.note = d.note || "";
        this.err = "";
        if (!this.selected && this.cells.length) this.selected = this.cells[0].cell_id;
      } catch (e) {
        this.err = `三证读取失败：${(e && e.message) || e}`;
      } finally {
        this.loaded = true;
        this.busy = false;
      }
    },
    pick(cell) { this.selected = this.selected === cell ? "" : cell; },
    start() {
      if (this.timer) return;
      this.load();
      this.timer = setInterval(() => { this.load(); }, POLL_MS);
    },
    stop() {
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
    },
  },
});
