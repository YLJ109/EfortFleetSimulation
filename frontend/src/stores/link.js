// =====================================================================
// Pinia store：链路自检（/api/system/guide）的唯一轮询者。
//
// ★ 为什么必须收敛成一个 store：
//   底栏状态灯和「引导条」看的是同一份自检数据。如果各拉各的，就会出现
//   "状态灯是绿的、引导条还挂着红色未通过项"这种自相矛盾的界面 ——
//   现场看到两个结论，只会两个都不信。所以这里只有一路请求、一份状态。
//
// 提供：三盏灯（pills/tone）、引导条（primary）、档位声明动作。
// 相机/机器人各自的"重连"仍委托给既有 store（camera store）与既有接口，
// 不在本 store 里重写一份。
// =====================================================================
import { defineStore } from "pinia";
import { apiControl } from "../net/control.js";
import {
  allPills, stripTone, worstPill,
} from "../linkview.js";

/** 轮询周期。5 秒：够快地反映插拔网线/开关相机，又不会把后端打满。 */
export const LINK_POLL_MS = 5000;

let timer = null;

export const useLinkStore = defineStore("link", {
  state: () => ({
    guide: null,        // /system/guide 原始快照
    error: "",          // 自检接口本身不可用时的原因
    at: 0,              // 最近一次成功的时间戳
    tick: 0,            // ★ P1-D7：最近一次 load 的时刻（成功/失败都更新）
    busy: false,        // 声明档位中
    actionMsg: "",      // 详情面板里的一行反馈
    claimError: "",
  }),
  getters: {
    links: (s) => (s.guide && s.guide.links) || null,
    /**
     * ★ 审计修复 P1-D7：自检数据是否过期。
     *   原实现是 `Date.now() - s.at > LINK_POLL_MS * 3` —— Date.now() 不是响应式
     *   依赖，这个 getter 只会在 `at` 变化时重算一次；而 `at` **恰恰在轮询失败时
     *   不再更新** → 标记永远停在 false，界面一直显示上一次的绿灯（"旧绿"）。
     *   改成比 `tick`（每次 load 必更新，响应式），并且由组件侧的定时轮询
     *   驱动重算（tick 每 5 秒变一次就够了，因为判断阈值是 15 秒）。
     */
    stale() {
      if (!this.at) return false;
      return (this.tick - this.at) > LINK_POLL_MS * 3;
    },
    /** 三盏灯（机器人 / 摄像头 / 示教器档位）。★ 任何情况下都返回三个元素。 */
    pills() {
      const raw = allPills(this.links);
      // ★ P1-D7 消费方之一：自检过期 → 仍为绿的灯一律压成黄。
      //   "数据早就没更新了还挂着绿灯"是现场最危险的假正常。
      if (!this.stale) return raw;
      return raw.map((p) => (p && p.tone === "ok"
        ? { ...p, tone: "warn", title: (p.title || "") + "（自检数据已过期）" }
        : p));
    },
    robot: (s) => (s.guide && s.guide.links && s.guide.links.robot) || null,
    camera: (s) => (s.guide && s.guide.links && s.guide.links.camera) || null,
    runMode: (s) => (s.guide && s.guide.links && s.guide.links.run_mode) || null,
    /** 底栏整体色调：三盏灯里最差的那一档。 */
    tone() { return stripTone(this.pills); },
    /** 第一个非 ok 的灯（没有就返回 null = 全部正常）。
     *  ★ 2026-10-11 如实注明：当前**没有界面消费方**（底栏统一用 `pills` / `tone`）——
     *    属预留口径，别误以为"已经有这一处显示了"。 */
    attention() { return worstPill(this.pills); },
    /**
     * 引导条主提示（有未通过项才有）。
     * ★ P1-D7 消费方之二：数据过期/接口挂了时**必须顶掉旧结论**，否则
     *   引导条还在教人"接下来该干什么"，而那已经是十几秒前的状态了。
     */
    primary() {
      if (this.stale || this.error) {
        return {
          key: "net",
          level: "error",
          title: this.at ? "自检数据已过期" : "自检接口不可用",
          hint: this.error
            || `后端 /system/guide 已超过 ${LINK_POLL_MS * 3 / 1000} 秒没有新数据，下方状态灯可能不是最新状态`,
        };
      }
      return (this.guide && this.guide.primary) || null;
    },
    hasToken: (s) => !!(s.guide && s.guide.has_token),
    /** "引导条没有未通过项"（≈ 链路全绿）。
     *  ★ 2026-10-11 如实注明：当前**没有消费方**（各页改用 `primary`）—— 预留，勿误以为在用。 */
    ready: (s) => !!s.guide && !s.guide.primary,
  },
  actions: {
    async load() {
      try {
        const r = await apiControl("/system/guide");
        if (!r.ok) {
          this.error = "自检接口返回 " + r.status;
          return;
        }
        this.guide = await r.json();
        this.error = "";
        this.at = Date.now();
      } catch (e) {
        this.error = "后端不可用（" + (e && e.message ? e.message : "网络错误") + "）";
      } finally {
        // ★ P1-D7：失败也必须推进 tick —— "多久没更新"只能靠它计时。
        this.tick = Date.now();
      }
    },
    start() {
      if (timer) return;
      this.load();
      timer = setInterval(() => this.load(), LINK_POLL_MS);
    },
    stop() {
      if (timer) { clearInterval(timer); timer = null; }
    },
    /**
     * ★★ 2026-10-11 修复（"水位不可靠 → 假绿"这一类，与运行日志 `log.length` 同源病根）：
     *   由**外部时钟**无条件推进水位。
     *   背景：`stale` 用 `tick` 计时，而 `tick` 只在 `load()` 里推进 —— 但**掉线时**
     *   `App.vue` 会 `link.stop()`（不再轮询）→ `tick` 冻结 → `stale` **永远算不出 true**
     *   → 摄像头/示教器灯维持"旧绿"。偏偏那正是它要防的场景（"数据早就停了还挂绿灯"）。
     *   现在由 App 的 1s 时钟（与令牌到期扫描同一个）无条件调用本方法，
     *   "多久没更新"才真正随真实时间增长。
     */
    beat() { this.tick = Date.now(); },
    /** 立即刷一次（做完动作后调用，不等下一个轮询周期）。 */
    async refresh() { await this.load(); },

    /** 声明示教器档位（需控制令牌）。 */
    async claimMode(mode) {
      this.busy = true;
      this.claimError = "";
      this.actionMsg = "";
      try {
        const r = await apiControl("/control/run-mode", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
          this.claimError = d.message || d.detail || ("声明失败（" + r.status + "）");
          return false;
        }
        this.actionMsg = "已声明为 " + (d.label || mode);
        await this.load();
        return true;
      } catch (e) {
        this.claimError = "声明失败：" + (e && e.message ? e.message : "网络错误");
        return false;
      } finally {
        this.busy = false;
      }
    },

    /** 撤销档位声明（回到"未确认"）。 */
    async clearMode() {
      this.busy = true;
      this.claimError = "";
      try {
        const r = await apiControl("/control/run-mode", { method: "DELETE" });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          this.claimError = d.message || d.detail || ("撤销失败（" + r.status + "）");
          return false;
        }
        this.actionMsg = "已撤销声明（回到未确认）";
        await this.load();
        return true;
      } catch (e) {
        this.claimError = "撤销失败：" + (e && e.message ? e.message : "网络错误");
        return false;
      } finally {
        this.busy = false;
      }
    },

    /** 重新连接控制器（需控制令牌）。 */
    async reconnect() {
      this.busy = true;
      this.actionMsg = "";
      try {
        const r = await apiControl("/system/reconnect", { method: "POST" });
        const d = await r.json().catch(() => ({}));
        if (r.status === 401 || r.status === 403) {
          this.actionMsg = "需要控制权限才能重连";
          return false;
        }
        // 成功就一句话：现场只要知道"通没通"，链路细节由上面的状态行承担。
        this.actionMsg = d.connected ? "连接成功" : "连接失败，仍处于模拟/离线";
        await this.load();
        return !!d.connected;
      } catch (e) {
        this.actionMsg = "连接失败：" + (e && e.message ? e.message : "网络错误");
        return false;
      } finally {
        this.busy = false;
      }
    },
  },
});
