import { defineStore } from "pinia";
import { apiControl } from "../net/control.js";

// =====================================================================
// ★ E3 反事实试跑（What-if 沙盒）的**唯一**发起口 —— 只读实验台，不是执行器。
//
// 这张卡回答的只有一句话：「这样改（托盘偏移 / 面 / 次序 / 样本数）**值不值得上真机**」。
// 决定权在人：本 store 里**没有任何**执行入口 ——
//   · 不 import `stores/exec.js`、不碰 `/control/move`、`/sorting/start`、点动、气路；
//   · 唯一的请求 = `POST /api/points/whatif`（后端那一侧同样不写寄存器、不碰触发位与气路，
//     见 `backend/app/services/whatif.py` 顶部三条硬口径与 `tests/test_whatif.py` 的静态钉）。
// 要控制令牌只是因为它会把点位库与围栏几何读给调用方看（与写操作同一道门）。
//
// ★★ 口径纪律（AGENTS.md §5.5，别在界面这一侧重算一遍）：
//   ① 水位只用只增不减的 `seq`（不用数组长度、不用对象引用）—— 否则"结果不再更新"会静默失效；
//   ② 样本数 = 后端**实际跑完**的那个（`res.n`），界面绝不显示"请求数"冒充样本数；
//   ③ 三态不塌成布尔：`ok / partial / failed / no_data` 四种各有样子，
//      「没有数据」绝不能渲染成绿色，也绝不能写成"安全"；
//   ④ 数字旁边必须有**采样时刻**（`ranAt`）；读不到就写「—（没测）」，绝不合成 0；
//   ⑤ 节拍那一档没有实测样本时，原样显示后端的「无实测样本·不给节拍估计」，
//      界面**自己绝不**生成任何"要多久"的话（上位机拿不到速度曲线 → 那是编故事）。
// =====================================================================

export const FACES = [
  { key: "keep", label: "保持当前面（不翻面）" },
  { key: "front", label: "正面" },
  { key: "back", label: "反面" },
];
export const ORDERS = [
  { key: "as_listed", label: "按列出的次序" },
  { key: "reverse", label: "倒序（先放后取）" },
  { key: "by_zone", label: "按区分组（A→B→C→其它）" },
];

const STATUS_TEXT = {
  ok: "算完了（没有失败项）",
  partial: "算完了，但有如实的缺失项",
  failed: "没算出来（评估跑不起来）",
  no_data: "没有可评估的数据",
};

/** ★ 数字取用只走这一条：读不到（null / undefined / 空串 / 非有限）⇒ 返回 null，
 *  由界面渲染成「—（没测）」。特别地 `Number("")` 是 0，所以空串必须先判掉：
 *  后端给 None（JSON null）时若图省事写成 `Number(v)`，界面就会显示「0.0 ms」，
 *  那是**把没测合成测到**（§5.5 铁律③的另一种塌法）。 */
const numOrNull = (v) => {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

export const useWhatIfStore = defineStore("whatif", {
  state: () => ({
    form: {
      dx: 0, dy: 0, dz: 0,          // 托盘偏移（mm，基座系 —— 后端就是这么加的）
      face: "keep",
      order: "as_listed",
      points: [],                    // 参与点位（空 = 用后端缺省评估路径）
      n: null,                       // 样本数（null = 后端缺省，见 SAMPLE_N_DEFAULT 的计时依据）
      gear: null,                    // 速度档：**只是评估标签**，界面不写速度口径
    },
    res: null,                       // 最近一轮返回体（后端那一份，前端不改写）
    err: "",                         // 请求层错误（与后端的 no_data 是两回事）
    busy: false,
    ranAt: null,                     // ★ 本轮结果的采样时刻（ms 本地时钟）；null = 没跑过
    seq: 0,                          // ★ 只增不减的水位
  }),
  getters: {
    hasResult: (s) => s.res !== null && s.res !== undefined,
    /** 三态判据**只来自后端**的 `status`；认不出来的一律 unknown（不塌成 ok / 安全）。 */
    status: (s) => {
      const v = s.res && s.res.status;
      return Object.prototype.hasOwnProperty.call(STATUS_TEXT, String(v)) ? String(v) : "unknown";
    },
    statusText: (state) => {
      if (state.err) return "请求失败";
      if (!state.res) return "还没跑过";
      return STATUS_TEXT[state.status] || "认不出的状态（不假装通过）";
    },
    statusClass: (s) => {
      if (s.err) return "w-bad";
      if (!s.res) return "w-unknown";
      if (s.status === "ok") return "w-ok";
      if (s.status === "partial") return "w-warn";
      if (s.status === "failed") return "w-bad";
      // no_data / unknown ⇒ 灰虚线：看着就"没结论"，不冒充通过（铁律③）
      return "w-unknown";
    },
    /** ★ 界面显示的样本数 = 后端**实际跑完**的那个（不是请求数）。 */
    nRan: (s) => (s.res && Number.isFinite(Number(s.res.n)) ? Number(s.res.n) : null),
    nRequested: (s) => (s.res && Number.isFinite(Number(s.res.n_requested))
      ? Number(s.res.n_requested) : null),
    truncated: (s) => {
      if (!s.res) return false;
      const ran = Number(s.res.n), want = Number(s.res.n_requested);
      return Number.isFinite(ran) && Number.isFinite(want) && ran < want;
    },
    /** 后端 failures 里那句"被预算截断"的原文（有就原样贴，不自己编）。 */
    truncNote: (s) => {
      const list = (s.res && s.res.failures) || [];
      const hit = list.find((f) => String(f || "").indexOf("预算") >= 0);
      return hit ? String(hit) : "";
    },
    dist: (s) => (s.res && s.res.min_clearance) || null,
    worst: (s) => (s.res && s.res.worst_segment) || null,
    segments: (s) => (s.res && s.res.segments) || [],
    sources: (s) => (s.res && s.res.jitter_sources) || [],
    notModelled: (s) => (s.res && s.res.not_modelled) || [],
    conclusion: (s) => (s.res && s.res.conclusion) || "",
    note: (s) => (s.res && s.res.note) || "",
    failures: (s) => (s.res && s.res.failures) || [],
    /** 节拍档：available 三态（true / false / 拿不到），数字只在 available===true 时才存在。 */
    cycle: (s) => (s.res && s.res.cycle_time) || null,
    /** 每拍耗时 / 整轮耗时：null 就如实 null（后端在 no_data 那一支给的就是 null）。
     *  ★ 没跑过（`res` 为空）时也返回 null —— 不写成 `numOrNull(s.res && s.res.x)`，
     *  那里的 `&&` 会交回 `false`，而 `Number(false)` 是 0 = 把「没有这一项」印成「0 ms」。 */
    sampleMs: (s) => (s.res ? numOrNull(s.res.sample_ms) : null),
    runMs: (s) => (s.res ? numOrNull(s.res.run_ms) : null),
  },
  actions: {
    /** 组装请求体：只有**场景描述**，没有任何"动作"字段。 */
    payload() {
      const f = this.form;
      const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
      const scenario = {
        tray: { dx: num(f.dx), dy: num(f.dy), dz: num(f.dz) },
        face: String(f.face || "keep"),
        order: String(f.order || "as_listed"),
        points: (f.points || []).slice(),
      };
      if (f.gear !== null && f.gear !== undefined && Number.isFinite(Number(f.gear))) {
        scenario.speed_gear_pct = Number(f.gear);   // ★ 评估标签：不写速度寄存器、不动滑块
      }
      const body = { scenario, seed: 1 };
      if (f.n !== null && f.n !== undefined && Number.isFinite(Number(f.n)) && Number(f.n) >= 1) {
        body.n = Math.min(300, Math.max(1, Math.round(Number(f.n))));
      }
      return body;
    },
    /** ★ 唯一的发起口：离线试跑（不产生任何动作、不改点位库、不改速度口径）。 */
    async run() {
      if (this.busy) return;
      this.busy = true;
      this.err = "";
      try {
        const r = await apiControl("/points/whatif", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.payload()),
        });
        const d = await r.json().catch(() => null);
        if (r.status !== 200 || !d || typeof d !== "object") {
          this.err = `试跑请求失败（HTTP ${r.status}）`;
          this.res = null;             // 清掉旧结果：别让上一轮的数冒充这一轮
          this.ranAt = null;
          return;
        }
        this.res = d;
        this.ranAt = Date.now();       // ★ 采样时刻：本轮结果真正到手的那一刻
        this.seq += 1;                 // ★ 水位只增不减（铁律①）
      } catch (e) {
        this.err = `试跑请求失败：${(e && e.message) || e}`;
        this.res = null;
        this.ranAt = null;
      } finally {
        this.busy = false;
      }
    },
    resetForm() {
      this.form.dx = 0; this.form.dy = 0; this.form.dz = 0;
      this.form.face = "keep"; this.form.order = "as_listed";
      this.form.points = []; this.form.n = null; this.form.gear = null;
    },
  },
});
