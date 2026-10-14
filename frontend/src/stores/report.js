import { defineStore } from "pinia";
import { apiUrl } from "../config.js";

// =====================================================================
// ★ E6「机器人自述 = 每天一页现场日报」Store（**只读**）
//
// 唯一一条请求：GET `/api/system/daily-report`。不带控制令牌、不下发任何动作、
// 不 emit 事件 —— 观测不属于控制（与 `/api/sorting/cells`、`/api/twin/residual` 同口径）。
//
// ★ 命门（改动前先读，勿绕）：
//   · 页面**不重算数字**：日报里每一个数字都来自后端 `sections[key].fields` 与
//     `attention.items`；前端只做展示（§5.5 铁律④：同一屏同一结论必须同源）；
//   · 「无此项数据」不许塌成 0：`section.status === "no_data"` 时按原样写，
//     绝不做 `|| 0` / `|| "无"` 之类的降级；
//   · `consistency.count > 0` 必须**显式**在界面红字显示，不许静默 ——
//     这是"每个数字都能自证"的最后一颗钉子；
//   · 不 import LLM 相关：日报是纯模板产物，本轮不接讲解员；就算接了 LLM 也只能
//     讲人话，不能改数字（后端 `consistency_check()` 已经把住）。
//   · 不引入任何新的第三方依赖：导出 markdown 走浏览器 Blob + a.download。
// =====================================================================

export const useReportStore = defineStore("report", {
  state: () => ({
    report: null,        // 后端 build_report() 的原样 JSON
    date: "",            // 当前查看的日期（""=今天，由后端决定）
    err: "",
    busy: false,
    loaded: false,
  }),
  getters: {
    sections(state) {
      const r = state.report;
      if (!r || !r.sections) return [];
      // 保持后端 _SECTION_BUILDERS 里的注册顺序（flows/twin/evidence/alarms/points/cells/color）
      return Object.keys(r.sections).map((k) => ({ key: k, ...r.sections[k] }));
    },
    attention(state) {
      const a = state.report && state.report.attention;
      return (a && a.items) || [];
    },
    attentionNote(state) {
      const a = state.report && state.report.attention;
      return (a && a.note) || "";
    },
    consistency(state) {
      const c = state.report && state.report.consistency;
      return c || { rule: "", count: 0, inconsistent: [] };
    },
    inconsistent(state) {
      const c = state.report && state.report.consistency;
      return (c && c.inconsistent) || [];
    },
    hasInconsistency(state) {
      const c = state.report && state.report.consistency;
      return !!(c && (c.count || 0) > 0);
    },
    markdown(state) {
      return (state.report && state.report.markdown) || "";
    },
    title(state) {
      return (state.report && state.report.title) || "";
    },
    generatedAt(state) {
      return (state.report && state.report.generated_at) || "";
    },
    timezone(state) {
      return (state.report && state.report.timezone) || "";
    },
    window(state) {
      return (state.report && state.report.window) || {};
    },
    hasData(state) { return state.loaded && !state.err && !!state.report; },
  },
  actions: {
    async load() {
      this.busy = true;
      try {
        const q = this.date ? `?date=${encodeURIComponent(this.date)}` : "";
        const res = await fetch(apiUrl(`/system/daily-report${q}`));
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
          // 后端诚实回 400（"不是日期"这种），这里如实显示，不假装"今天没数据"
          const msg = (d && (d.detail || d.message)) || `HTTP ${res.status}`;
          this.err = `日报读取失败：${msg}`;
          this.report = null;
          return;
        }
        if (!d || d.ok !== true) {
          this.err = "日报返回结构不完整 —— 不猜内容";
          this.report = null;
          return;
        }
        this.report = d;
        this.err = "";
      } catch (e) {
        this.err = `日报读取失败：${(e && e.message) || e}`;
        this.report = null;
      } finally {
        this.loaded = true;
        this.busy = false;
      }
    },
    setDate(d) {
      // 只改本地"要看哪天"的口径；真值以请求参数发给后端，日期换算由后端做
      this.date = d || "";
    },
    // ★ 客户端导出 markdown：Blob + a.download，零新依赖，不新建后端路由
    exportMarkdown() {
      const r = this.report;
      if (!r || !r.markdown) return false;
      const stamp = (r.date || this.date || "unknown");
      const blob = new Blob([r.markdown], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `daily-report-${stamp}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 0);
      return true;
    },
  },
});
