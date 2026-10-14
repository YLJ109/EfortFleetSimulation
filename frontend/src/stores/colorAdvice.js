// =====================================================================
// Pinia store：E4「现场自学习判色闭环」的前端唯一读/写入口。
//
// 三个后端端点族，本 store 全权代管（组件不自己 fetch，避免两处各拼一套字段）：
//   · GET  /api/vision/samples/advice   —— 建议面（纯读，无需令牌）
//   · GET  /api/vision/samples          —— 样本环列表（纯读，无需令牌）
//   · POST /api/vision/samples/{id}/review            —— 人工标签回流（要控制令牌）
//   · POST /api/vision/samples/advice/propose|rollback —— 提案账目（要控制令牌）
//
// ★ 为什么这里**不自己算**任何判定：
//   样本够不够、能不能给方向、现场生效值知不知道 —— 全是后端 `color_advice.advice()`
//   那一份的产物（§5.5 铁律②④：状态口径以后端为准、同一屏同一结论必须同源）。
//   前端一旦再算一遍"够不够"，就会出现"表里说够、按钮却是灰的"。
//
// ★ 三态不许塌成布尔（铁律③）：
//   `from === null` = 现场生效值**不知道**，与 `from === 0` 是两件事；
//   `snapshot.state` 是 'unknown' | 'camera' | 'manual'，渲染一律按字符串分支。
//
// ★ 本 store 结构上**没有**"已生效"这个状态：后端每条建议/提案都带 `applied: false`，
//   这里也不提供任何写阈值的动作（生效只在既有的相机阈值面板，由人显式点一次）。
// =====================================================================
import { defineStore } from "pinia";
import { apiUrl } from "../config.js";
import { apiControl } from "../net/control.js";

/** 样本列表一次取多少条（界面只列最近的，历史在库里）。 */
export const SAMPLE_LIST_LIMIT = 24;

function errText(r, fallback) {
  return `${fallback}（HTTP ${r.status}）`;
}

function bodyError(r, fallback) {
  return r.json()
    .then((d) => (d && d.detail) || fallback)
    .catch(() => fallback);
}

export const useColorAdviceStore = defineStore("colorAdvice", {
  state: () => ({
    advice: null,          // GET /samples/advice 原样响应（含 colors/items/proposal/history）
    samples: null,         // GET /samples 原样响应（items/total/colors/snapshot）
    error: "",             // 建议面读取失败的原因（如实透给界面）
    samplesError: "",
    actionError: "",       // 最近一次**写操作**的失败原因（复核/提案/回滚）
    actionMessage: "",     // 最近一次写操作的成功说明（后端原文，不自造）
    loading: false,
    acting: false,
    at: 0,                 // 最近一次成功读数时刻（界面显示"数据截至"）
  }),

  getters: {
    hasAdvice: (s) => !!s.advice,
    /** 建议项（可能为空数组 = 「没有可提案的建议」，与「没读到」不同：后者看 error）。 */
    items: (s) => (s.advice && Array.isArray(s.advice.items) ? s.advice.items : []),
    colors: (s) => (s.advice && Array.isArray(s.advice.colors) ? s.advice.colors : []),
    /** 不建议的原因列表（后端已经逐条写好中文，界面照搬，不改一个字）。 */
    reasons: (s) => (s.advice && Array.isArray(s.advice.no_advice_reasons)
      ? s.advice.no_advice_reasons : []),
    proposal: (s) => (s.advice && s.advice.proposal) || null,
    history: (s) => (s.advice && Array.isArray(s.advice.history) ? s.advice.history : []),
    sampleItems: (s) => (s.samples && Array.isArray(s.samples.items) ? s.samples.items : []),
    /** 待复核（outcome=unreviewed）且**不是空跑占位**的样本 —— 占位色没有真色可认。 */
    pending: (s) => ((s.samples && s.samples.items) || [])
      .filter((x) => x && x.outcome === "unreviewed" && !x.placeholder),
    /** 阈值快照三态：unknown / camera / manual（'unknown' 时界面必须写"不知道"）。 */
    snapshotState: (s) => {
      const snap = s.advice && s.advice.threshold_snapshot;
      return (snap && snap.state) || "unknown";
    },
    statusText: (s) => {
      if (s.error) return "建议面不可用：" + s.error;
      if (!s.advice) return "建议面尚未取到数据";
      return `样本环共 ${s.advice.samples_total ?? "—"} 条 · 建议 ${
        (s.advice.items || []).length} 项`
        + (s.at ? ` · 截至 ${new Date(s.at).toLocaleTimeString()}` : "");
    },
  },

  actions: {
    async load() {
      this.loading = true;
      const results = await Promise.allSettled([
        fetch(apiUrl("/vision/samples/advice")).then((r) => {
          if (!r.ok) throw new Error(errText(r, "建议接口不可用"));
          return r.json();
        }),
        fetch(apiUrl(`/vision/samples?limit=${SAMPLE_LIST_LIMIT}`)).then((r) => {
          if (!r.ok) throw new Error(errText(r, "样本接口不可用"));
          return r.json();
        }),
      ]);
      const [a, s] = results;
      if (a.status === "fulfilled") {
        this.advice = a.value;
        this.error = "";
        this.at = Date.now();
      } else {
        this.error = String(a.reason && a.reason.message || a.reason || "读取失败");
      }
      if (s.status === "fulfilled") {
        this.samples = s.value;
        this.samplesError = "";
      } else {
        this.samplesError = String(s.reason && s.reason.message || s.reason || "读取失败");
      }
      this.loading = false;
      return this.advice !== null;
    },

    /** 人工标签回流：judgement='correct' | 'wrong'（wrong 必须带真色，后端会拒）。 */
    async review(sid, judgement, trueColor = "", note = "") {
      if (this.acting) return false;
      this.acting = true;
      this.actionError = "";
      this.actionMessage = "";
      try {
        const r = await apiControl(`/vision/samples/${sid}/review`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            judgement,
            true_color: judgement === "wrong" ? trueColor : null,
            note: note || null,
          }),
        });
        if (!r.ok) {
          this.actionError = await bodyError(r, "复核失败");
          return false;
        }
        const d = await r.json();
        this.actionMessage = `样本 #${sid} 已登记为「${d.outcome}」`
          + (d.true_color ? `（真色 ${d.true_color}）` : "")
          + "；★ 只改标签，原始逐帧特征与当时的结论**一个字节没动**";
        await this.load();
        return true;
      } catch (e) {
        this.actionError = "复核失败：" + (e && e.message ? e.message : "网络错误");
        return false;
      } finally {
        this.acting = false;
      }
    },

    /** 存为待确认提案（**不下发**：后端消息原文会说明生效还得人自己点）。 */
    async propose(note = "") {
      if (this.acting) return false;
      this.acting = true;
      this.actionError = "";
      this.actionMessage = "";
      try {
        const r = await apiControl("/vision/samples/advice/propose", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note: note || null }),
        });
        if (!r.ok) {
          this.actionError = await bodyError(r, "提案失败");
          return false;
        }
        const d = await r.json();
        this.actionMessage = d.message || "已存为待确认提案（未下发）";
        await this.load();
        return true;
      } catch (e) {
        this.actionError = "提案失败：" + (e && e.message ? e.message : "网络错误");
        return false;
      } finally {
        this.acting = false;
      }
    },

    async rollback() {
      if (this.acting) return false;
      this.acting = true;
      this.actionError = "";
      this.actionMessage = "";
      try {
        const r = await apiControl("/vision/samples/advice/rollback", { method: "POST" });
        if (!r.ok) {
          this.actionError = await bodyError(r, "回滚失败");
          return false;
        }
        const d = await r.json();
        this.actionMessage = d.message || "提案账目已退回上一版（现场阈值未变）";
        await this.load();
        return true;
      } catch (e) {
        this.actionError = "回滚失败：" + (e && e.message ? e.message : "网络错误");
        return false;
      } finally {
        this.acting = false;
      }
    },

    clearActionText() { this.actionError = ""; this.actionMessage = ""; },
  },
});
