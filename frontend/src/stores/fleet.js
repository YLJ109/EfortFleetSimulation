// =====================================================================
// 机房（12 台阵列）状态：名单 → 3D 建层 → 大屏卡片。
//
// 三条数据路，各有各的节奏，别混：
//   1) 名单 /api/fleet     —— 开机拉一次（+ 掉线兜底轮询）。它决定"画几个工位、
//      摆在哪、每台上哪具身体"，是机房地图的唯一来源；
//   2) 姿态 WS fleet_pose  —— 20Hz，写进 net/fleetBuffer（**不进响应式**），
//      由 App 的渲染循环每帧取走喂给 three 层；
//   3) 卡片 4Hz            —— 只有大屏打开时才跑定时器，把缓冲里的最新值
//      拷成 12 个纯对象给模板。数字和画面同源（都读 poseMap），不会各说各话。
//
// ★ 只读：本 store 没有任何写机器人的动作，连"重连某一台"都没有
//   （重连由后端采集线程自己做，见 services/fleet.py 的 fails%20 分支）。
// =====================================================================
import { defineStore } from "pinia";
import { apiUrl } from "../config.js";
import { apiControl } from "../net/control.js";
import { initFleet, setFleetMode, onFleetSafety, fleetModeOn, loadElfMeshes } from "../three/manager.js";
import {
  ingestFrame, ingestStatus, poseMap, stateOf, poseAgeMs, clear as clearBuffer,
} from "../net/fleetBuffer.js";
import { makeCard } from "../utils/fleetCards.js";
import { scopeColumns, scopeIds as scopeIdsOf } from "../utils/fleetScope.js";

const CARD_MS = 250;          // 卡片 4Hz：数字看得清，又不至于 12 张卡每帧重排
const LINK_STALE_MS = 3000;   // 姿态帧停多久判"链路冻住"（后端 batch 50ms，余量 60 帧）
// 编号顺序与显示顺序**相反**，这是两件事：
//   编号 —— 用户口径"从下面 1 开始" → 下排 = 1~3、上排 = 4~6；
//   显示 —— 列从上往下读要正好是 A6…A1，所以上排排在前面。
// 用同一个常量会得出"A3 在最上面、A6 在最下面"，与平面图对不上。
const SEQ_ORDER = { lower: 0, upper: 1 };

// 定时器与"正在拉名单"的标记**不进 state**：
// 前者是句柄、后者是并发闸，都不是界面要渲染的东西，放进响应式只会多一层代理开销。
let cardTimer = null;
let inflight = null;

export const useFleetStore = defineStore("fleet", {
  state: () => ({
    robots: [],            // /api/fleet 的 robots[]（含 pos / host / model）
    byId: {},              // id → robots[i]，卡片查名单用（免得 12×N 次 find）
    kinematics: {},        // 型号 → 运动学档案（3D 身体的来源，两种）
    labels: {},            // 型号 → { label, unverified }，卡片抬头用
    seq: {},               // 工位 id → 区内代号（A1…A6 / B1…B6，用户口径的编号）
    layout: {},
    streaming: {},
    enabled: false,        // 后端 fleet.yaml 是否启用机房
    running: false,        // 采集线程是否在跑（false = 只有名单，全是离线）
    loadError: "",
    unbuilt: [],           // 3D 里建不起来的工位/型号 —— 必须显式暴露，不能整层消失
    stations: 0,           // 3D 实际建出几个工位
    summary: null,         // fleet_status 的 1Hz 汇总
    safetyById: {},        // id → 围栏等级（three 层算，只在等级变化时回调）
    linkAlive: false,      // 姿态帧还在来吗
    scope: "all",          // 当前作用域（一页看哪几台）：all|upper|lower|zoneA|zoneB
    write: { ready: false, mode: "off", reason: "还没拿到机房配置" },
    session: null,         // 当前控制作用域（服务端算的 ids，不是前端约定）
    active: false,         // 大屏是否正在显示
    simDriven: {},         // 工位 id → true：这台正被本地沙盘驱动（没连真机也能拖着看）
    cards: [],
    // ---- 机房设置（/api/fleet/config）：只在抽屉打开时才拉 ----
    cfgRows: [],           // 生效的登记表（名称/IP/端口/型号/区排号 + 哪几项被改过）
    cfgMeta: {},           // 型号清单、阵列参数、可改字段说明
    cfgError: "",
    cfgBusy: false,
    cfgSaving: false,
  }),

  getters: {
    total: (s) => s.robots.length,
    onlineCount: (s) => s.cards.filter((c) => c.online).length,
    alarmCount: (s) => s.cards.filter((c) => c.level === "alarm").length,
    dangerCount: (s) => s.cards.filter((c) => c.level === "hit" || c.level === "danger").length,
    movingCount: (s) => s.cards.filter((c) => c.moving).length,
    /**
     * 左右两列：由当前作用域现算（判据全在 utils/fleetScope.js，可被 node 断言）。
     * ★ 列方向与编号方向恒定：左 = A 区或上排、右 = B 区或下排，列内恒 A6→A1。
     *   换页只换内容，不换阅读习惯 —— 这是"六套页面不乱"的唯一保证。
     */
    columns: (s) => scopeColumns(s.cards, s.scope),

    /** 当前作用域里的卡片：KPI 与报警条按它算，"在线 4/6"说的就是这一页。 */
    scoped: (s) => s.cards.filter((c) => scopeIdsOf(s.cards, s.scope).includes(c.id)),

    /** 全场最严重等级（顶部横条用）。 */
    worstLevel: (s) => {
      const rank = { alarm: 4, hit: 3, danger: 2, warn: 1, safe: 0, offline: -1 };
      let w = "safe";
      for (const c of s.cards) if ((rank[c.level] ?? -1) > (rank[w] ?? -1)) w = c.level;
      return w;
    },
  },

  actions: {
    /**
     * 拉机房名单并建 3D 层。
     * ★ 必须在 safe.load() **之后**调用：initFleet 的围栏配置取自单机层那份
     *   fence.cfg（12 台跟单机用同一套阈值），先建机房会拿到 null → 围栏不着色。
     */
    async loadFleet() {
      // 并发闸：sweep 每秒补拉一次、页面切回来也会拉 —— 同时跑两次 initFleet
      // 会建两层阵列（旧的那层已从场景摘掉但几何仍被 clone 引用，白占显存）。
      if (inflight) return inflight;
      inflight = this._loadFleet().finally(() => { inflight = null; });
      return inflight;
    },

    async _loadFleet() {
      try {
        const r = await fetch(apiUrl("/fleet"));
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        this.robots = Array.isArray(d.robots) ? d.robots : [];
        this.byId = {};
        for (const it of this.robots) if (it && it.id) this.byId[it.id] = it;
        this.kinematics = d.kinematics || {};
        this.layout = d.layout || {};
        this.streaming = d.streaming || {};
        this.enabled = !!d.enabled;
        if (d.write) this.write = d.write;
        this.running = !!d.running;
        this.summary = d.summary || this.summary;
        this.loadError = "";
      } catch (e) {
        // 后端不可达时**保留上一次的名单**：大屏宁可显示"离线"，也不要变成空屏。
        this.loadError = `机房名单读取失败：${String((e && e.message) || e)}`;
        return { stations: this.stations, unbuilt: this.unbuilt };
      }

      // 每台型号的显示名与"机型未核实"标记，卡片要用（华沿的型号是靠外形猜的）
      const labels = {};
      for (const k of Object.keys(this.kinematics)) {
        const p = this.kinematics[k] || {};
        labels[k] = { label: p.display_name || k, unverified: !!p.variant_unverified };
      }
      this.labels = labels;

      // 区内代号：A 区从下排 1 号数到上排 3 号 = A1…A6，B 区同理。
      // 登记里的 slot 是**组内**序号（两组各 1~3），直接显示会出现两张"A下1"，
      // 而用户要的是 A1~A6 这种一口气数得完的编号。
      {
        const byZone = {};
        for (const it of this.robots) {
          if (!it || !it.id) continue;
          (byZone[it.zone || "?"] = byZone[it.zone || "?"] || []).push(it);
        }
        const seq = {};
        for (const zone of Object.keys(byZone)) {
          byZone[zone]
            .sort((a, b) => ((SEQ_ORDER[a.group] ?? 9) - (SEQ_ORDER[b.group] ?? 9))
              || (a.slot - b.slot))
            .forEach((it, i) => { seq[it.id] = `${zone}${i + 1}`; });
        }
        this.seq = seq;
      }

      // 华沿那 6 台：有官方 STL 就换成 1:1 真身体（到货后 initFleet 会自动重建一次）。
      // ★ 不阻塞首屏：骨架版先上，网格到了再换 —— 读数和 3D 同源，不会因为等模型而空屏。
      for (const k of Object.keys(this.kinematics)) {
        const p = this.kinematics[k] || {};
        const m = p.mesh || {};
        if (p.chain === "urdf" && m.dir && m.files && m.files.links && m.files.links.length === 6) {
          loadElfMeshes(m.dir, m.files);
        }
      }

      onFleetSafety((r) => this.ingestSafety(r));
      const res = initFleet({
        robots: this.robots, layout: this.layout, kinematics: this.kinematics,
      });
      this.stations = res.stations;
      this.unbuilt = res.unbuilt || [];
      setFleetMode(this.active);          // 名单来晚了也要按当前页面决定显隐
      this.refreshCards();
      return res;
    },

    /** WS 姿态帧：只写缓冲，不碰响应式（60Hz 的读取在 App 的渲染循环里）。 */
    ingestPose(robots) { ingestFrame(robots); },

    /** WS 汇总帧（1Hz）。 */
    ingestSummary(summary) {
      const s = ingestStatus(summary);
      if (s) this.summary = s;
    },

    /** three 层的围栏评估：只在"某台等级变了"时被回调（键里不含净空数值）。 */
    ingestSafety(r) {
      if (!r || !Array.isArray(r.list)) return;
      const next = {};
      for (const x of r.list) next[x.id] = x.state;
      this.safetyById = next;
      if (this.active) this.refreshCards();
    },

    /** 大屏开/关：切 3D 层 + 卡片定时器（关了还刷卡片纯属白烧）。 */
    setActive(on) {
      const want = !!on;
      if (want === this.active) { setFleetMode(want); return; }
      this.active = want;
      setFleetMode(want);
      if (want) {
        this.refreshCards();
        if (!cardTimer) cardTimer = setInterval(() => this.refreshCards(), CARD_MS);
      } else if (cardTimer) {
        clearInterval(cardTimer);
        cardTimer = null;
      }
    },

    /** 把缓冲里的最新值拷成 12 个纯对象。模板只读 cards，不碰缓冲。 */
    refreshCards() {
      if (!this.robots.length) { this.cards = []; return; }
      const pose = poseMap();
      const labels = this.labels || {};
      const now = Date.now();
      const out = [];
      for (const entry of this.robots) {
        const lb = labels[entry.model] || {};
        out.push(makeCard(entry, pose[entry.id] || null, stateOf(entry.id),
          this.safetyById[entry.id] || "safe",
          {
            now, modelLabel: lb.label, unverified: lb.unverified,
            short: this.seq[entry.id], simDriven: !!this.simDriven[entry.id],
          }));
      }
      out.sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || (a.id < b.id ? -1 : 1));
      this.cards = out;
    },

    /** App 的 1 秒时钟驱动：判定"WS 通了但没人推帧"。 */
    sweep() {
      this.linkAlive = poseAgeMs() < LINK_STALE_MS;
      // 名单拉过一次就够；但若当时后端还没起来（名单为空），每秒补一次直到拿到。
      // loadFleet 自带并发闸，这里不用再加标记。
      if (!this.robots.length) this.loadFleet();
    },

    /**
     * 声明/取消"这台正被本地沙盘驱动"。
     * ★ 为什么要专门记一笔：没连真机的工位，滑块推多少画面就动多少（仿真），
     *   这时卡片不能还写"离线"——操作员会以为画面在骗他；更不能写"在线"。
     */
    setSimDriven(id, on) {
      if (!id) return;
      if (on) this.simDriven = { ...this.simDriven, [id]: true };
      else if (this.simDriven[id]) {
        const next = { ...this.simDriven };
        delete next[id];
        this.simDriven = next;
      }
    },

    /**
     * 申请控制作用域（单台 / A区 / B区 / 上排 / 下排 / 全部）。
     * ★ ids 由服务端按当前名单现算，前端只报意图 —— 名单刚改过时，
     *   前端缓存的 id 列表可能已经过期，信它就等于把控制权发给一台不存在的机器。
     */
    async acquireScope(scope) {
      const r = await apiControl("/fleet/scope", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(scope),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.message || d.detail || `HTTP ${r.status}`);
      this.session = d;
      return d;
    },

    async releaseScope() {
      try {
        await apiControl("/fleet/scope", { method: "DELETE" });
      } finally {
        this.session = null;
      }
    },

    /**
     * 拉机房登记表（设置抽屉打开时才拉）。
     * GET 是公开的，但仍走 apiControl：带着令牌没坏处，而 401 能顺手把死令牌收回来。
     */
    async loadConfig() {
      this.cfgBusy = true;
      this.cfgError = "";
      try {
        const r = await apiControl("/fleet/config");
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        this.cfgRows = Array.isArray(d.robots) ? d.robots : [];
        this.cfgMeta = d;
        return d;
      } catch (e) {
        this.cfgError = `登记表读取失败：${String((e && e.message) || e)}`;
        throw e;
      } finally {
        this.cfgBusy = false;
      }
    },

    /**
     * 保存改动 → 后端落覆盖层并**整片重建采集** → 前端把名单与 3D 一起刷新。
     *
     * ★ 保存后必须重新 loadFleet：改的可能是 IP，也可能是区/排/序号 —— 后者会挪
     *   画面上的工位，而 3D 层是按名单建的。不刷新的话"数字对了、位置还是旧的"，
     *   围栏评估用的却是新坐标，等于画面与判据各说一套。
     * ★ 后端会顺手收回控制作用域，这里同步清掉本地那份（否则控制台还显示"我持权"）。
     */
    async saveConfig(payload) {
      this.cfgSaving = true;
      this.cfgError = "";
      try {
        const r = await apiControl("/fleet/config", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.message || d.detail || `HTTP ${r.status}`);
        this.cfgRows = Array.isArray(d.robots) ? d.robots : this.cfgRows;
        if (d.scope_released) this.session = null;
        await this.loadFleet();
        return d;
      } catch (e) {
        this.cfgError = String((e && e.message) || e);
        throw e;
      } finally {
        this.cfgSaving = false;
      }
    },

    /** 恢复登记默认：删覆盖层，一切回到 config/fleet.yaml。 */
    async resetConfig() {
      this.cfgSaving = true;
      this.cfgError = "";
      try {
        const r = await apiControl("/fleet/config/reset", { method: "POST" });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.message || d.detail || `HTTP ${r.status}`);
        this.cfgRows = Array.isArray(d.robots) ? d.robots : this.cfgRows;
        await this.loadFleet();
        return d;
      } catch (e) {
        this.cfgError = String((e && e.message) || e);
        throw e;
      } finally {
        this.cfgSaving = false;
      }
    },

    /** 3D 是否正处在机房模式（给别的组件做只读判断）。 */
    /** 换作用域。认不出的值回落 all —— 空屏会被当成后端挂了。 */
    setScope(v) {
      this.scope = ["all", "upper", "lower", "zoneA", "zoneB"].includes(v) ? v : "all";
    },

    isFleetMode() { return fleetModeOn(); },

    dispose() {
      if (cardTimer) { clearInterval(cardTimer); cardTimer = null; }
      setFleetMode(false);
      clearBuffer();
    },
  },
});
