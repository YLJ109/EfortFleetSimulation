// WebSocket 客户端: 断线指数退避重连 + 消息看门狗 + 心跳。
//
// onPose：姿态帧（type==="pose"）；onRcStatus：控制器寄存器快照帧（type==="rc_status"）；
// onFleetPose / onFleetStatus：机房 12 台的合批姿态帧与 1Hz 汇总帧（后端
//   services/fleet.py::_batch_loop 推，走的是同一条 /ws/pose 流，hub 按 type 分流）；
// onStatus：连接通断回调。rc-status 经此通道后，前端不再单独轮询 /api/rc-status。
//
// ★ 机房帧**不复用** onPose：单机那条帧是 j1..j6 平铺的一台，机房帧是 robots[] 的 12 台，
//   两者字段口径不同，混在一个回调里就要靠"猜有没有 robots 字段"分派。
//
// ★ 全维度审查 F-06：原实现**没有任何存活探测**。
//   当 TCP 处于「半开」状态（服务端进程死了、但客户端 TCP 还没收到 FIN/RST）时，
//   onopen 早已触发、onclose 又迟迟不触发 → robot.connected 一直为 true，
//   而画面其实早已冻住 —— 用户以为连着、控制按钮全开，实际上后端早死了。
//   修复：维护 lastMsgAt，看门狗每 2s 检查，超过 WS_STALE_MS 无新帧就主动 close()
//   （close → onclose → onStatus(false) + 指数退避重连），同时周期性发 ping 保活。
import { wsUrl } from "../config.js";

// ★ 多久没收到任何一帧就判定链路已死（半开 TCP / 后端卡死）。
//   后端 ws_push_hz 默认 20Hz（每 50ms 一帧），取 8s 是 160 帧余量，
//   普通网络抖动、切后台不会误杀；只有后端真的不再推帧才触发。
const WS_STALE_MS = 8000;
const WATCHDOG_MS = 2000;
const PING_MS = 15000;

export function connectWs({ onPose, onStatus, onRcStatus, onFleetPose, onFleetStatus }) {
  let ws = null;
  let retry = 0;
  let closed = false;
  let timer = null;
  let watchdog = null;
  let pinger = null;
  let lastMsgAt = 0;

  function clearTimers() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (watchdog) { clearInterval(watchdog); watchdog = null; }
    if (pinger) { clearInterval(pinger); pinger = null; }
  }

  function markAlive() { lastMsgAt = Date.now(); }

  function open() {
    // ★ 防止重复连接：先关闭旧的 WebSocket 和定时器
    if (timer) { clearTimeout(timer); timer = null; }
    if (ws) {
      try { ws.close(); } catch (e) { /* 关旧连接失败无影响 */ }
      ws = null;
    }

    ws = new WebSocket(wsUrl());
    ws.onopen = () => {
      retry = 0;
      markAlive();
      onStatus && onStatus(true);
      // ★ 心跳：周期发 ping（服务端可忽略，只为穿过代理/负载均衡的空闲超时）
      if (!pinger) pinger = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) { /* 忽略 */ }
        }
      }, PING_MS);
    };
    ws.onmessage = (ev) => {
      markAlive();               // ★ F-06：任何一帧都刷新"存活"时间戳
      try {
        const msg = JSON.parse(ev.data);
        if (!msg || !msg.type) return;
        if (msg.type === "pong") return;   // 心跳回应，不进业务分发
        if (msg.type === "pose") { onPose && onPose(msg); }
        else if (msg.type === "rc_status") { onRcStatus && onRcStatus(msg); }
        else if (msg.type === "fleet_pose") { onFleetPose && onFleetPose(msg.robots); }
        else if (msg.type === "fleet_status") { onFleetStatus && onFleetStatus(msg.summary); }
      } catch (e) {
        /* 忽略坏帧 */
      }
    };
    ws.onclose = () => {
      onStatus && onStatus(false);
      ws = null;  // ★ 清理引用
      scheduleReconnect();
    };
    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* 关旧连接失败无影响 */ }
    };
  }

  // ★ F-06：消息看门狗。半开 TCP 不会触发 onclose，只能靠"没新帧"来判定死亡。
  function startWatchdog() {
    if (watchdog) return;
    watchdog = setInterval(() => {
      if (closed || !ws) return;
      // 还没 open 完（readyState !== OPEN）不计时：刚建连、后端还没推第一帧是常态。
      if (ws.readyState !== WebSocket.OPEN) return;
      if (Date.now() - lastMsgAt > WS_STALE_MS) {
        // 链路已死：强制 close → onclose → onStatus(false) + 重连；
        // 不清 lastMsgAt，重连成功后 onopen 会重置，避免重连瞬间误杀。
        try { ws.close(); } catch (e) { /* 忽略 */ }
      }
    }, WATCHDOG_MS);
  }

  function scheduleReconnect() {
    if (closed) return;
    // ★ 防止重复调度：先清除旧定时器
    if (timer) { clearTimeout(timer); timer = null; }
    retry = Math.min(retry + 1, 6);
    const delay = Math.min(500 * 2 ** retry, 8000);
    timer = setTimeout(open, delay);
  }

  startWatchdog();
  open();

  return {
    close() {
      closed = true;
      clearTimers();
      if (ws) {
        try { ws.close(); } catch (e) { /* 关旧连接失败无影响 */ }
        ws = null;
      }
    },
  };
}
