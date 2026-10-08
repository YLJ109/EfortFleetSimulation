// 前端运行配置: WebSocket 地址推断 + 兜底 DH (后端 /api/meta 不可用时使用)。
export function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/pose`;
}

// ★ P1-B6：审计事件流是**独立**通道，需持控制令牌。
//   令牌不放查询串（会进后端访问日志），改为连接后首条消息带内鉴权。
export function wsEventsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/events`;
}

export function apiUrl(path) {
  return `${location.origin}/api${path}`;
}

// ★ 实时语音字幕（腾讯云 ASR）：音频上行 / 字幕下行。
//   与事件流同款：令牌走**带内握手**首帧，不放查询串（避免进后端访问日志）。
export function asrWsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/asr`;
}

// 视觉检测服务地址(独立进程, 默认 :8100)。
// 端口可用构建期变量 VITE_CAMERA_PORT 覆盖(见 frontend/.env.example)。
const CAMERA_PORT = import.meta.env?.VITE_CAMERA_PORT || "8100";

export function cameraBase() {
  return `http://${location.hostname || "127.0.0.1"}:${CAMERA_PORT}`;
}

// 兜底 DH: 与 config/robot.yaml 一致 (零位: J2=0 大臂垂直, J3=0 小臂水平)。
// 优先使用后端下发的 dh。
export const FALLBACK_DH = {
  calibration_pending: true,
  joints: [
    { name: "J1", d: 376.0, a: 49.5932, alpha: 90.0, theta_offset: 0.0 },
    { name: "J2", d: 0.2852, a: 330.1834, alpha: 0.0, theta_offset: 90.0 },
    { name: "J3", d: 0.0, a: 40.2714, alpha: 90.0, theta_offset: 0.0 },
    { name: "J4", d: 329.2414, a: 0.0, alpha: -90.0, theta_offset: 0.0 },
    { name: "J5", d: 0.0, a: 0.0, alpha: 90.0, theta_offset: 0.0 },
    { name: "J6", d: 80.0, a: 0.0, alpha: 0.0, theta_offset: 0.0 },
  ],
};

export const MOUNTING = "floor";
