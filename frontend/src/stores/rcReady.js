// =====================================================================
// Pinia store：真机链路就绪（消费 WS 广播的 rc_status 帧 + POST /api/ready 一键就绪）。
//
// ★ 数据通道：后端采集线程按 4s 经 /ws/pose 广播 rc_status 帧
//   （net/ws.js → robot store → 本 store 的 ingest）。前端不再单独轮询 /api/rc-status；
//   load() 保留作「种子 + 一键就绪/声明档位后的即时刷新」。
//
// ★ 为什么独立于 link store：
//   link store 消费的是 /system/guide（链路自检，含网线/令牌/围栏），
//   本 store 消费的是控制器**寄存器**快照（模式/伺服/报警/程序/触发位）。
//   前者回答"这条链路通不通"，后者回答"控制器现在让不让我动"。
//
// 声明档位委托给 link.claimMode()：档位声明只有一条通道（/control/run-mode），
// 若在这里再写一份，会出现"引导条声明了 AUTO、本卡还停在未确认"的分裂界面。
// =====================================================================
import { defineStore } from "pinia";
import { apiUrl } from "../config.js";
import { apiControl } from "../net/control.js";
import { useAuthStore } from "./auth.js";
import { useLinkStore } from "./link.js";

/**
 * 寄存器快照新鲜度窗口：超过它没收到 WS 帧，即判定数据过期（stale）。
 * 它不是轮询周期了 —— 后端按 RC_STATUS_INTERVAL(≈4s) 推帧。
 */
export const RC_POLL_MS = 4000;

export const useRcReadyStore = defineStore("rcReady", {
  state: () => ({
    snap: null,        // rc_status 原始快照（来自 WS 或种子 load）
    error: "",         // 快照不可用原因
    at: 0,             // 最近一次成功时间戳
    busy: false,       // 一键就绪执行中
    lastResult: null,  // 最近一次 /ready 或 /ready/cancel 的完整结果（steps/summary/error）
    lastAction: "",    // 上一次结果属于哪个动作："ready" | "cancel"（文案分流用）
    wsAt: 0,           // 最近一次收到 WS rc_status 帧的时间（stale 判定依据）
    // ★ 一键就绪成功计数器：各页面 watch 它来「就绪后自动刷新点位」。
    //   为什么用计数器而不是布尔：连续两次就绪也要各触发一次刷新（布尔会因值相同不触发）。
    readyTick: 0,
  }),

  getters: {
    ok: (s) => !!(s.snap && s.snap.ok),
    bits: (s) => (s.snap && s.snap.bits) || null,
    realEnabled: (s) => !!(s.snap && s.snap.real_enabled),
    /** 就绪 = 伺服上电 + 程序已加载且运行 + 自动/远程档 + 无报警 + 非急停（后端判定）。 */
    ready: (s) => !!(s.snap && s.snap.ready),
    // ★★ 2026-10-09 新增：**伺服是否上电**（只看伺服位，不看程序/运行位）。
    //   按钮"一键就绪 ↔ 取消就绪"跟它走 —— 用户口径：
    //   "伺服上电就该显示取消就绪；没上电才需要一键就绪"。
    //   旧代码用 `ready`（伺服+程序+运行+AUTO+无报警），导致
    //   "伺服上电但程序没加载"时按钮还写着「一键就绪」，操作员不知道该点哪个。
    //   ⚠ 后端未返回 `servo_on` 时返回 null（不是 false）—— 调用方据此回落旧判据，
    //     避免"老后端 → 按钮永远显示一键就绪"这种静默错。
    servoOn: (s) => {
      const v = s.snap && s.snap.servo_on;
      return (typeof v === "boolean") ? v : null;
    },
    /** 点动触发位为 1 = 上一发点动还没收尾，此时禁止一键就绪。 */
    jogTrig: (s) => !!(s.snap && s.snap.jog_trig),
    /** 数据是否过期：超过 RC_POLL_MS 没收到 WS 帧（或根本没有快照）判定为旧数据。 */
    stale: (s) => (s.snap ? (Date.now() - s.wsAt > RC_POLL_MS) : true),
    modeText: (s) => {
      const m = s.snap && s.snap.mode;
      if (m === "auto") return "AUTO 自动";
      if (m === "remote") return "远程控制";
      if (m === "manual") return "手动（T1/T2）";
      return m ? m : "未知";
    },
    /** 实测语义反转：只有 AUTO / 远程 接受上位机指令（T1/T2 写得进但不动）。 */
    joggable: (s) => {
      const m = s.snap && s.snap.mode;
      return m === "auto" || m === "remote";
    },
    progText: (s) => {
      if (!s.snap || !s.snap.ok) return "—";
      const cur = s.snap.prog;
      const svc = s.snap.service_program;
      if (!svc) return String(cur);
      return cur === svc
        ? (cur + "（点动服务 ✓）")
        : (cur + "／服务 " + svc);
    },
  },

  actions: {
    async load() {
      try {
        const r = await fetch(apiUrl("/rc-status"));
        if (!r.ok) { this.error = "rc-status 返回 " + r.status; return; }
        this.snap = await r.json();
        this.error = "";
        this.at = Date.now();
      } catch (e) {
        this.error = "后端不可用（" + (e && e.message ? e.message : "网络错误") + "）";
      }
    },
    /** 消费后端经 WS 广播的 rc_status 帧（net/ws.js → robot store → 本方法）。 */
    ingest(payload) {
      if (!payload || typeof payload !== "object") return;
      this.wsAt = Date.now();
      if (!payload.ok) {
        // ok=false：读不到控制器（模拟/离线）。保留错误但不展示假快照。
        this.snap = null;
        this.error = payload.error || "读不到控制器";
        return;
      }
      // 去掉传输层字段；补回 ok:true（后端快照里本就没有 ok 字段，是被薄封装传进来的）
      const { type, t, ...rest } = payload;
      this.snap = { ...rest, ok: true };
      this.error = "";
      this.at = Date.now();
    },
    /** 数据改为经 WS 实时注入；start 只拉一次种子兜底（WS 尚未握手/断线时也有数）。 */
    start() {
      this.load();
    },
    stop() {
      /* 无定时器需清理；保留空实现兼容组件卸载调用。 */
    },

    /** 一键就绪：清报警 → 伺服上电 → 加载点动服务程序 → 运行挂起 WAIT。 */
    async doReady(prog) {
      if (this.busy) return false;
      this.busy = true;
      this.lastResult = null;
      this.lastAction = "ready";
      try {
        const body = prog ? { prog: Number(prog) } : {};
        const r = await apiControl("/ready", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (r.status === 401 || r.status === 403) {
          const auth = useAuthStore();
          if (auth.requestLogin) auth.requestLogin();
          this.lastResult = { ok: false, error: "需要控制权限（一键就绪会写控制器寄存器）" };
          return false;
        }
        const d = await r.json().catch(() => ({}));
        this.lastResult = d;
        await this.load();
        // ★ 就绪成功 → 递增信号，各页面 watch 它自动刷新点位（用户需求：
        //   "一键就绪后点位自动刷新"）。就绪后控制器可上报实时姿态/可示教，
        //   刷新能让页面立刻反映"现在能存点了 / 点位体检状态变化"。
        if (d.ok) this.readyTick += 1;
        return !!d.ok;
      } catch (e) {
        this.lastResult = { ok: false, error: (e && e.message) || "网络错误" };
        return false;
      } finally {
        this.busy = false;
      }
    },

    /** 声明档位（委托 link store，唯一通道），完成后立刻刷新寄存器快照。 */
    async claimMode(mode) {
      const link = useLinkStore();
      const ok = await link.claimMode(mode);
      await this.load();
      return ok;
    },
    /** 取消就绪：停止运行中的程序，可选关闭伺服。 */
    async cancelReady(servoOff = false) {
      if (this.busy) return false;
      this.busy = true;
      this.lastResult = null;
      this.lastAction = "cancel";
      try {
        const r = await apiControl("/ready/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ servo_off: servoOff }),
        });
        if (r.status === 401 || r.status === 403) {
          const auth = useAuthStore();
          if (auth.requestLogin) auth.requestLogin();
          this.lastResult = { ok: false, error: "需要控制权限" };
          return false;
        }
        const d = await r.json().catch(() => ({}));
        this.lastResult = d;
        await this.load();
        return !!d.ok;
      } catch (e) {
        this.lastResult = { ok: false, error: (e && e.message) || "网络错误" };
        return false;
      } finally {
        this.busy = false;
      }
    },
  },
});
