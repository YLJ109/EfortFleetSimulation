// =====================================================================
// 实时语音字幕（腾讯云 ASR）—— 前端状态与音频采集
//
// 链路：getUserMedia(麦克风) → AudioContext(16kHz) → ScriptProcessor
//       → Float32→Int16 单声道 PCM → WS /ws/asr → 后端代理 → 腾讯云
//       识别结果回推 → partial（边说边出）/ final（整句定稿）→ 字幕层
//
// ★ 安全红线：识别文本**只用于显示**，不解析成任何指令、不碰机器人控制链路。
// ★ 密钥只在后端 .env：这里拿到的 /api/asr/config 只有 enabled/engine，没有密钥。
// =====================================================================
import { defineStore } from "pinia";
import { apiUrl, asrWsUrl } from "../config.js";
import { useAuthStore } from "./auth.js";

const TARGET_RATE = 16000;   // 云端要求 16k/16bit/单声道
const COMMIT_HOLD_MS = 6000; // 整句定稿后在屏上停留多久再淡出（电影字幕观感）

export const useAsrStore = defineStore("asr", {
  state: () => ({
    cfg: null,          // /api/asr/config 的结果（不含密钥）
    cfgErr: "",
    cfgBusy: false,
    active: false,      // 正在识别
    connecting: false,
    partial: "",        // 当前句（边说边出，灰白 + 光标）
    finalText: "",      // 最近定稿的整句（亮白）
    err: "",
    level: 0,           // 麦克风电平峰值（给按钮做跳动反馈）
    // 内部句柄（不参与渲染）
    _ws: null, _ctx: null, _stream: null, _node: null, _mute: null,
    _hold: null,
  }),

  getters: {
    /** 功能是否可用（后端已启用且密钥齐）。 */
    available: (s) => !!(s.cfg && s.cfg.enabled),
    /** 不可用时的原因（给界面如实显示，绝不静默失败）。 */
    reason: (s) => (s.cfg && s.cfg.reason) || s.cfgErr || "",
    /** 字幕是否该出现（有内容或正在识别）。 */
    visible: (s) => !!(s.active || s.partial || s.finalText),
    /** 状态文案。 */
    stateText: (s) => {
      if (s.err) return "识别异常";
      if (s.connecting) return "连接中…";
      if (s.active) return "识别中";
      return "未开启";
    },
  },

  actions: {
    /** 拉取后端配置（是否启用 / 引擎 / 有没有密钥）。 */
    async loadConfig() {
      this.cfgBusy = true;
      try {
        const r = await fetch(apiUrl("/asr/config"));
        if (!r.ok) { this.cfgErr = "配置读取失败 HTTP " + r.status; return; }
        this.cfg = await r.json();
        this.cfgErr = "";
      } catch (e) {
        this.cfgErr = "配置读取失败：" + ((e && e.message) || e);
      } finally {
        this.cfgBusy = false;
      }
    },

    /** 开始识别：先建云端会话，就绪后再开麦（避免麦克风已开但连不上）。 */
    async start() {
      if (this.active || this.connecting) return;
      this.err = "";
      const auth = useAuthStore();
      if (!auth.controlActive) { this.err = "需要控制令牌才能启用（防止未授权占用云端额度）"; return; }
      if (!this.available) { this.err = this.reason || "语音识别未启用"; return; }

      // ★ 安全上下文检查：http + 局域网 IP 访问时浏览器会直接禁用麦克风，
      //   这里先说清楚，而不是让人看到"没反应"。
      const secure = window.isSecureContext
        || ["localhost", "127.0.0.1", "::1"].includes(location.hostname);
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        this.err = secure ? "当前浏览器不支持麦克风采集（建议 Chrome / Edge）"
          : "浏览器禁止非安全源使用麦克风：请用 http://localhost:8000 访问，"
            + "或给本服务配 HTTPS（用局域网 IP 时浏览器会禁用麦克风）";
        return;
      }

      this.connecting = true;
      try {
        const ws = new WebSocket(asrWsUrl());
        ws.binaryType = "arraybuffer";
        this._ws = ws;
        await new Promise((resolve, reject) => {
          const to = setTimeout(() => reject(new Error("连接超时")), 8000);
          ws.onopen = () => {
            clearTimeout(to);
            ws.send(JSON.stringify({ type: "auth", token: auth.token }));
          };
          ws.onmessage = (ev) => {
            let d = {};
            try { d = JSON.parse(ev.data); } catch (e) { return; }
            if (d.type === "ready") {
              clearTimeout(to);
              this.connecting = false;
              this.active = true;
              resolve();
            } else if (d.type === "error") {
              clearTimeout(to);
              reject(new Error(d.message || "云端拒绝"));
            }
          };
          // ★★ 关闭原因必须具体，不能一律"连接已断开" ★★
          //   现场教训（2026-10-08）：账号未开通实时语音识别时，云端回 4003 并立刻断链，
          //   而界面只显示"语音识别连接已断开" → 用户完全不知道要去控制台开通服务。
          //   规则：云端已给过明确错误（this.err 非空）就**原样保留**，只在没有原因时才兜底。
          ws.onclose = (ev) => {
            clearTimeout(to);
            const code = ev && ev.code;
            const byCode = code === 4401 ? "控制令牌无效或已过期，请重新登录后再试"
              : code === 4400 ? "语音识别未启用或未配置密钥（见项目根 .env）"
              : code === 4429 ? "已有另一路语音识别在进行（同一时间只允许一路）"
              : code === 4500 ? "连接腾讯云失败（服务器无法访问 asr.cloud.tencent.com？）"
              : "";
            if (this.active || this.connecting) {
              if (!this.err) this.err = byCode || "语音识别连接已断开";
              this._teardown("");
            } else {
              reject(new Error(byCode || "连接被拒绝"));
            }
          };
          ws.onerror = () => { clearTimeout(to); reject(new Error("无法连接语音识别服务")); };
        });
        this._bindMessages(ws);
        await this._openMic(ws);
      } catch (e) {
        this.connecting = false;
        this.err = String((e && e.message) || e);
        this._teardown("");
      }
    },

    /** 绑定识别结果下行（与握手分开，避免握手中的 error 被当成运行期错误）。 */
    _bindMessages(ws) {
      ws.onmessage = (ev) => {
        let d = {};
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        if (d.type === "error") {
          this.err = d.message || "云端返回错误";
          if (d.code && d.code !== 0) this.err += "（code " + d.code + "）";
          return;
        }
        if (d.type !== "result") return;
        const txt = String(d.text || "");
        if (d.slice === 2) {
          // 整句结束 → 定稿上屏，停留一会再淡出
          this.finalText = txt || this.partial;
          this.partial = "";
          if (this._hold) clearTimeout(this._hold);
          this._hold = setTimeout(() => {
            this.finalText = "";
            this._hold = null;
          }, COMMIT_HOLD_MS);
        } else {
          this.partial = txt;      // 0=整句开始 / 1=中间，都是"正在说"
        }
      };
    },

    /** 开麦克风并把 16k PCM 帧推给后端。 */
    async _openMic(ws) {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: TARGET_RATE,
                 echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx({ sampleRate: TARGET_RATE });   // 浏览器负责把麦重采样到 16k
      await ctx.resume();
      const src = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor(512, 1, 1);  // 512/16k = 32ms
      const mute = ctx.createGain();
      mute.gain.value = 0;                                  // 别把麦克风回放到扬声器
      const ratio = ctx.sampleRate / TARGET_RATE;           // 1=原生 16k；否则线性抽取
      node.onaudioprocess = (e) => {
        if (!this.active) return;
        const inp = e.inputBuffer.getChannelData(0);
        let f32 = inp;
        if (ratio > 1.001) {
          const n = Math.floor(inp.length / ratio);
          f32 = new Float32Array(n);
          for (let i = 0; i < n; i++) f32[i] = inp[Math.floor(i * ratio)];
        }
        const pcm = new Int16Array(f32.length);
        let peak = 0;
        for (let i = 0; i < f32.length; i++) {
          const v = Math.max(-1, Math.min(1, f32[i]));
          const a = v < 0 ? -v : v;
          if (a > peak) peak = a;
          pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
        }
        this.level = peak;
        if (ws.readyState === 1) ws.send(pcm.buffer);
      };
      src.connect(node);
      node.connect(mute);
      mute.connect(ctx.destination);
      this._stream = stream; this._ctx = ctx; this._node = node;
      this._mute = mute; this._src = src;
    },

    /** 停止识别：先通知云端收尾（拿最后一句），再释放麦克风。 */
    stop() {
      const ws = this._ws;
      if (ws && ws.readyState === 1) {
        try { ws.send(JSON.stringify({ type: "stop" })); } catch (e) { /* 忽略 */ }
      }
      this._teardown("");
      this.partial = "";
    },

    /** 释放一切资源（幂等）。 */
    _teardown(msg) {
      if (this._hold) { clearTimeout(this._hold); this._hold = null; }
      try { if (this._node) { this._node.onaudioprocess = null; this._node.disconnect(); } } catch (e) { /* */ }
      try { if (this._mute) this._mute.disconnect(); } catch (e) { /* */ }
      try { if (this._src) this._src.disconnect(); } catch (e) { /* */ }
      try { if (this._node) this._node.disconnect(); } catch (e) { /* */ }
      try { if (this._stream) this._stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* */ }
      try { if (this._ctx) this._ctx.close(); } catch (e) { /* */ }
      try { if (this._ws && this._ws.readyState <= 1) this._ws.close(); } catch (e) { /* */ }
      this._ws = this._ctx = this._stream = this._node = this._mute = this._src = null;
      this.active = false; this.connecting = false; this.level = 0;
      if (msg) this.err = msg;
    },

    clearErr() { this.err = ""; },
  },
});
