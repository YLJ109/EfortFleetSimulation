// =====================================================================
// Pinia store：视觉检测（海康相机 + 颜色分拣）。
// 纯状态 + 请求动作；MJPEG 流的 <img> 管理由 CameraPanel 组件负责。
//
// ★ 状态可信度是这一版的重点：以前 refresh() 失败时**什么都不写**，
//   status 保持上一次的快照 —— 服务已经挂了，界面还写着"已开启"，
//   前端照旧去连 MJPEG，于是画面永远黑着（现场报的"摄像头死机/捕捉不到"）。
//   现在：失败即 serviceDown=true + status 清空，界面据此显示"服务不可用"。
// =====================================================================
import { defineStore } from "pinia";
import { cameraBase } from "../config.js";

const camBase = cameraBase;

// 超时（ms）：服务端在重连相机/切换网口时可能长时间阻塞，
// 没有超时的话 fetch 会一直挂着，轮询与按钮全部"点了没反应"。
const POST_TIMEOUT_MS = 8000;
const STATUS_TIMEOUT_MS = 4000;

/**
 * 带超时的 fetch。
 * ★ 用 AbortController 而不是"race 一个 Promise"：后者只是让调用方提前返回，
 *   底层请求仍在跑，连开几个就会把浏览器连接数占满。
 */
async function fetchWithTimeout(url, opts, ms) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ms);
  try {
    return await fetch(url, { ...opts, signal: ac.signal });
  } finally {
    clearTimeout(timer);
  }
}

export const useCameraStore = defineStore("camera", {
  state: () => ({
    status: null,          // 相机服务 /status 快照（服务不可达时为 null）
    error: "",
    serviceDown: false,    // 相机服务（8100）不可达 / 超时
    failCount: 0,          // 连续失败次数（供界面显示退避状态）
    devices: [],           // 可用相机设备列表
    currentDevice: null,   // 当前已打开的设备信息
  }),
  getters: {
    busy: (s) => !!(s.status && (s.status.opening || s.status.closing)),
    opened: (s) => !!(s.status && s.status.opened && !s.status.closing),
    /** 取流是否可用：服务在线 + 相机已开。画面链路的唯一判据。 */
    canStream: (s) => !s.serviceDown && !!(s.status && s.status.opened && !s.status.closing),
    yoloEnabled: (s) => !!(s.status && s.status.ai_enabled),
    yoloLoaded: (s) => !!(s.status && s.status.ai_loaded),
    yoloLoading: (s) => !!(s.status && s.status.ai_loading),
    colorEnabled: (s) => !!(s.status?.vision?.enabled),
    colorSupported: (s) => !!(s.status?.vision?.supported),
    /** 当前相机是否彩色（灰度相机无法做颜色识别）。 */
    colorCapable: (s) => !!(s.status?.vision?.color_capable),
    /** 三色识别置信度阈值（0~1），未生效默认 0.35。 */
    colorConf: (s) => s.status?.vision?.min_conf ?? 0.35,
    // ★ 目标检测置信度阈值（YOLO）：/status 的 conf。误报多就往上调。
    detConf: (s) => s.status?.conf ?? 0.5,
    /** 三色识别目标（红/绿/蓝）。 */
    colorTargets: (s) => s.status?.vision?.targets || ["红", "绿", "蓝"],
    /** 颜色识别累计次数（"目标"统计用；YOLO 关掉后 det_count 恒为 0）。 */
    colorCount: (s) => s.status?.vision?.count ?? 0,
    /** 相机自身报的错（如"未找到相机"），与 serviceDown 区分开。 */
    deviceError: (s) => (s.status && s.status.error) || "",
    /** 当前设备标签（用于下拉菜单显示）。 */
    currentLabel: (s) => {
      if (!s.currentDevice) return "未选择";
      const d = s.currentDevice;
      return `${d.model} (${d.type} ${d.serial})`;
    },
  },
  actions: {
    _markUp() {
      this.failCount = 0;
      this.serviceDown = false;
    },
    _markDown() {
      this.failCount += 1;
      this.serviceDown = true;
      // ★ 必须清空：留着旧快照会让 opened 仍为 true，前端继续连 MJPEG 黑屏。
      this.status = null;
      this.error = "相机服务未启动或已断开（需运行 camera/camera_service.py，或 run.bat camera）";
    },
    async camPost(path, body = {}) {
      // ★ 失败要分清"服务挂了"和"服务报错"：原来一律 catch 成网络不可用，
      //   服务端 500（如相机被占用）会被误报成"相机服务不可用"，误导排查。
      let r;
      try {
        r = await fetchWithTimeout(camBase() + path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }, POST_TIMEOUT_MS);
      } catch (e) {
        this._markDown();
        throw new Error(e && e.name === "AbortError"
          ? "相机服务响应超时（8 秒无响应，可能正在重连相机，请稍后重试）"
          : "相机服务不可用（需运行 camera/camera_service.py）");
      }
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j.error || j.message || "";
        } catch (e) { /* 响应不是 JSON 时用状态码 */ }
        // ★ 5xx/4xx 说明服务**活着**，只是这一次操作失败 —— 不能判成 serviceDown，
        //   否则界面会显示"服务不可用"，把用户引向错误的方向。
        this._markUp();
        throw new Error(detail ? `服务错误(${r.status}): ${detail}` : `服务错误: HTTP ${r.status}`);
      }
      this._markUp();
      return r.json();
    },
    /** 拉取相机状态，返回是否处于切换过渡中（用于加速轮询）。 */
    async refresh() {
      try {
        const r = await fetchWithTimeout(camBase() + "/status", {}, STATUS_TIMEOUT_MS);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const s = await r.json();
        this.status = s;
        // 服务端的自述错误（如"未找到相机(检查网线/网卡)"）照实透出，
        // 与"服务不可达"是两种完全不同的排查方向。
        this.error = s.error || "";
        this._markUp();
        return !!(s.opening || s.closing);
      } catch (e) {
        this._markDown();
        return false;
      }
    },
    /** 一键切换相机开关（开→关 或 关→开）。 */
    async toggle() {
      if (this.opened) {
        await this.close();
      } else {
        await this.open();
      }
    },
    async open() {
      const r = await this.camPost("/open");
      this.status = r.status;
      await this.refresh();
    },
    async close() {
      const r = await this.camPost("/close");
      this.status = r.status;
    },
    /** 加载可用相机设备列表。 */
    async loadDevices() {
      try {
        const r = await fetchWithTimeout(camBase() + "/devices", {}, STATUS_TIMEOUT_MS);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const d = await r.json();
        if (d.ok) {
          this.devices = d.devices || [];
          this.currentDevice = d.current || null;
        }
      } catch (e) {
        // 静默失败，不影响主流程
      }
    },
    /** 切换到指定索引的相机设备。 */
    async switchDevice(idx, camType = "mvs") {
      try {
        const r = await this.camPost("/switch", { idx, cam_type: camType });
        this.status = r.status;
        await this.loadDevices();
        return { ok: true, message: r.message };
      } catch (e) {
        this.error = e.message;
        return { ok: false, error: e.message };
      }
    },
    /** YOLO 检测开关。 */
    async setAI(enabled) {
      try { await this.camPost("/config", { enabled }); } catch (e) { this.error = e.message; return; }
      await this.refresh();
    },
    async setModel(model) {
      try { await this.camPost("/config", { model }); } catch (e) { this.error = e.message; return; }
      await this.refresh();
    },
    /** ★ 目标检测置信度阈值（0.05 ~ 0.95）。
     *  调高 = 误报更少、但可能漏检（现场"检测误报偏多"就用这个）；
     *  对应摄像头面板的「检测置信度 ≥ x%」滑杆，落点是相机服务 POST /config {conf}。 */
    async setConf(conf) {
      try { await this.camPost("/config", { conf }); } catch (e) { this.error = e.message; return; }
      await this.refresh();   // 立刻回读，滑杆上的数字马上跟着变
    },
    async setImgsz(imgsz) {
      try { await this.camPost("/config", { imgsz }); } catch (e) { this.error = e.message; }
    },
    /** 颜色分拣开关。 */
    async setColor(enabled) {
      try { await this.camPost("/vision/config", { enabled }); } catch (e) { this.error = e.message; return; }
      await this.refresh();
    },
    /** 三色识别置信度阈值（0~1）。 */
    async setColorConf(conf) {
      try { await this.camPost("/vision/config", { min_conf: conf }); } catch (e) { this.error = e.message; return; }
      await this.refresh();
    },
    async snap() {
      try {
        return await this.camPost("/snapshot-save");
      } catch (e) {
        return { ok: false, error: e.message };
      }
    },
    /** 手动重连相机（网线在摄像头/机器人间切换后，点此触发，无需重启服务）。 */
    async reconnect() {
      try {
        await this.camPost("/reconnect");
        await this.refresh();
        return true;
      } catch (e) {
        this.error = "重连失败: " + e.message;
        return false;
      }
    },
    /** 卸载 YOLO 检测模型，释放 torch 常驻内存（可达数 GB）。 */
    async unload() {
      try {
        const r = await this.camPost("/unload");
        await this.refresh();
        return r;
      } catch (e) {
        this.error = "卸载失败: " + e.message;
        return { ok: false, error: e.message };
      }
    },
  },
});
