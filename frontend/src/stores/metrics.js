// =====================================================================
// Pinia store：后端进程级指标（GET /api/system/metrics）的唯一轮询者。
//
// ★ B 批·B1 的前端接线件。后端每一项都是「值 + 单位 + 时间戳 + 来源」四元组，
//   本 store 只做三件事：取数、判新鲜、把「没测」如实透下去。
//
// ★ 为什么必须再包一层（而不是让组件直接 fetch）：
//   后端已经保证「没采到过样本 ⇒ value=null + note 写明没测」，但 HTTP 层还会坏：
//   接口 500 / 后端没起 / 返回的是**上个版本**的 JSON（没有这个字段）。
//   这时若组件里写 `res.groups.ws.backlog_clients.value || 0`，
//   「读不到」就被写成了「0」—— 正是 §5.5 铁律③禁止的假显示。
//   所以取数一律走 `quad(分组, 键)`：任何缺字段都返回 value=null 的四元组，
//   并在 note 里写清是哪种缺法（没测 / 接口挂了 / 后端版本不含此项）。
//
// ★ 为什么这里**不做**均值/最差值/红黄绿判定：
//   阈值口径属于界面（同一份「广播入队耗时」，底栏想看趋势、运维页想看告警），
//   store 只负责把原始四元组和新鲜度交出去，避免两处各算一套互相矛盾。
//
// ★ 数据通道是**轮询**而不是 WS：指标是进程级观测，不是逐帧遥测；
//   跟 /system/guide 同一条口径（见 stores/link.js），5 秒足够反映插拔与积压。
// =====================================================================
import { defineStore } from "pinia";
import { apiUrl } from "../config.js";

/** 轮询周期：与链路自检同档（5 秒）—— 观测接口不该把后端打满。 */
export const METRICS_POLL_MS = 5000;

/** 新鲜度窗口：连续 3 个周期取不到成功响应即判过期（stale）。 */
export const METRICS_STALE_MS = METRICS_POLL_MS * 3;

/** 后端分组清单（与 api/system.py::api_metrics 的 groups 一一对应）。 */
export const METRIC_GROUPS = ["collector", "modbus", "ws", "runs", "vision", "events"];

/**
 * 一个「什么都没有」的四元组。
 * ★ 关键：value 一定是 null（不是 0、不是空串、不是 "-"），
 *   这样组件侧 `q.value === null` 与 `q.value === 0` 永远分得开。
 */
function emptyQuad(note, source) {
  return {
    kind: "missing",
    value: null,
    unit: "-",
    ts: null,
    source: source || "本地：未取到后端数据",
    desc: "",
    measured: false,
    stale: true,
    note,
  };
}

/**
 * 四元组的可读文本：「0.0 个」与「—（没测）」必须长得不一样。
 * 组件用它渲染即可，不要自己拼 `value + unit`（那会把 null 拼成 "null 个"）。
 */
export function formatQuad(q) {
  // ★ 只输出「没测」这一个口径：note 里区分的是给运维看的（没采到 / 接口挂了 /
  //   后端版本不含），界面文案统一 —— 两处都显示 note 会互相打架。
  if (!q || !q.measured || q.value === null || q.value === undefined) return "—（没测）";
  const v = typeof q.value === "number"
    ? (Number.isInteger(q.value) ? String(q.value) : q.value.toFixed(1))
    : String(q.value);
  return q.unit && q.unit !== "-" ? v + " " + q.unit : v;
}

let timer = null;

export const useMetricsStore = defineStore("metrics", {
  state: () => ({
    snap: null,        // /system/metrics 原始响应（{ time, uptime_sec, groups }）
    error: "",         // 接口本身不可用的原因（如实透给界面）
    at: 0,             // 最近一次**成功**取数的时间戳
    // ★ 外部时钟水位（由 App 的 1s 时钟 beat() 推进）。
    //   为什么不能用 Date.now() 做 stale 判据：它不是响应式依赖，getter 只在
    //   snap/at 变化时重算 —— 而这两者恰恰在「后端挂掉」时不再变化 →
    //   stale 永远算不出 true（旧数据一直当新数据显示）。同 stores/link.js::beat。
    tick: 0,
    busy: false,       // 一次请求在飞（防重入：慢后端 + 5s 轮询会叠请求）
    pollFails: 0,      // 连续失败次数（成功后归零）—— 界面据此写「已连错 N 次」
    seq: 0,            // 成功轮询计数（单调递增）：组件 watch 它刷新，
                       // 不用布尔/长度做水位（布尔会因值相同不触发，见项目约定）
  }),

  getters: {
    groups: (s) => (s.snap && s.snap.groups) || null,
    uptimeSec: (s) => (s.snap && typeof s.snap.uptime_sec === "number" ? s.snap.uptime_sec : null),
    /** 六个分组一次给出（缺哪个就是 null，不合成空对象 —— 空对象会被读成「这组都正常」）。 */
    collector: (s) => (s.snap && s.snap.groups && s.snap.groups.collector) || null,
    modbus: (s) => (s.snap && s.snap.groups && s.snap.groups.modbus) || null,
    ws: (s) => (s.snap && s.snap.groups && s.snap.groups.ws) || null,
    runs: (s) => (s.snap && s.snap.groups && s.snap.groups.runs) || null,
    vision: (s) => (s.snap && s.snap.groups && s.snap.groups.vision) || null,
    events: (s) => (s.snap && s.snap.groups && s.snap.groups.events) || null,
    /** 是否成功取到过数（哪怕已过期）。false 时界面只能写「没有数据」，不许写 0。 */
    hasData: (s) => !!(s.snap && s.snap.groups),
    /** 数据是否过期：超过 METRICS_STALE_MS 没有成功响应（或压根没成功过）。 */
    stale() {
      if (!this.at) return true;
      return (this.tick - this.at) > METRICS_STALE_MS;
    },
    /** 一行状态说明（供底栏/详情面板直接显示，避免各处自造文案分叉）。 */
    statusText() {
      if (!this.hasData) return this.error ? ("指标不可用：" + this.error) : "指标尚未取到数据";
      if (this.stale) return `指标已过期（超过 ${Math.round(METRICS_STALE_MS / 1000)} 秒未更新）`;
      if (this.pollFails) return `指标已恢复（此前连续失败 ${this.pollFails} 次）`;
      return "指标正常";
    },
  },

  actions: {
    /**
     * 取一个四元组视图。**任何**取不到都返回 value=null + 如实 note。
     * 这是本 store 唯一的读数据入口（组件不要自己下钻 snap.groups.* ——
     * 一旦下钻，`?.value || 0` 这类写法就会把「没测」变成「0」。）
     */
    quad(group, key) {
      if (!this.snap || !this.snap.groups) {
        return emptyQuad("没测：前端还没成功读到 /api/system/metrics"
          + (this.error ? ("（" + this.error + "）") : ""), "本地：未取到后端数据");
      }
      const g = this.snap.groups[group];
      if (!g) return emptyQuad(`没测：后端未返回 ${group} 分组（后端版本不含此项）`);
      const q = g[key];
      if (!q || typeof q !== "object") {
        return emptyQuad(`没测：后端 ${group} 分组里没有 ${key} 字段（后端版本不含此项）`);
      }
      const out = { ...q, stale: this.stale };
      if (this.stale) {
        // ★ 过期不抹掉数值（现场常需要看"最后一次是多少"），但必须改写 note：
        //   带着旧值却不声明它是旧的 = 假显示。
        out.note = "数据已过期（最后成功于 " + new Date(this.at).toLocaleTimeString()
          + "），不是当前状态" + (q.note ? ("；原注：" + q.note) : "");
      }
      return out;
    },

    async load() {
      if (this.busy) return false;      // 上一发还在飞：不叠请求
      this.busy = true;
      try {
        const r = await fetch(apiUrl("/system/metrics"));
        if (!r.ok) {
          this.error = "指标接口返回 " + r.status;
          this.pollFails += 1;
          return false;
        }
        const d = await r.json();
        if (!d || !d.groups) {
          this.error = "指标接口返回体没有 groups 字段（后端版本不匹配）";
          this.pollFails += 1;
          return false;
        }
        this.snap = d;
        this.error = "";
        this.at = Date.now();
        this.pollFails = 0;
        this.seq += 1;
        return true;
      } catch (e) {
        this.error = "后端不可用（" + (e && e.message ? e.message : "网络错误") + "）";
        this.pollFails += 1;
        return false;
      } finally {
        this.busy = false;
        // 失败也必须推进水位（stale 靠它计时，见 tick 注释）
        this.tick = Date.now();
      }
    },

    start() {
      if (timer) return;
      this.load();
      timer = setInterval(() => { this.load(); }, METRICS_POLL_MS);
    },
    stop() {
      if (timer) { clearInterval(timer); timer = null; }
    },
    /** ★ 由 App 的 1s 时钟无条件推进新鲜度水位（理由见 state 里 tick 的注释）。 */
    beat() { this.tick = Date.now(); },
    /** 立即刷一次（做完动作后调用，不等下一个周期）。 */
    async refresh() { return this.load(); },
  },
});
