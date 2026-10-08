<script setup>
// 摄像头面板：一键启停 + 颜色分拣 + 截图 / 重连。
// 浮在 3D 视口内，可拖动标题栏移位、拖右下角缩放；位置/尺寸记忆到 localStorage。
//
// ★ 本组件在四个视图里各挂一份（真实监控 / 点位执行 / 程序执行），全部 v-show 保活。
//   因此"我是不是当前活跃的那一份"必须判准，否则会同时打出多条 MJPEG 连接。
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from "vue";
import { useCameraStore } from "../stores/camera.js";
import { useRobotStore } from "../stores/robot.js";
import { cameraBase } from "../config.js";
import Icon from "./Icon.vue";

const props = defineProps({
  views: { type: Array, default: () => ["live"] },
});

const cam = useCameraStore();
const robot = useRobotStore();
// 审计修复 P0-ui-1：不再持有单一 imgEl —— MJPEG 改为双缓冲（imgA/imgB，见下方
// "MJPEG 流"一节），重连绝不再改写/移除**显示路**的 src。
const liveFlag = ref(false);
const expanded = ref(false);
const snapMsg = ref("");

const CAM_URL = cameraBase();

// ★★ 只认**第一个**视图（= 面板自己所在的那个视图容器）。
//   props.views 里还带着 'live'（如 ['point','live']），若把数组里所有视图都当活跃，
//   那么在「真实监控」页上，RealMonitor / PointExecView / ProgramExecView 三个实例
//   会**同时**认为自己在活跃 —— 于是 3 条 /status 轮询 + 3 条 MJPEG 连接一起打到
//   相机服务，浏览器还要同时解码 3 路视频。这正是现场"摄像头死机 / 实时捕捉不到"
//   的直接原因之一：面板越多，服务端与前端越先被自己压死。
const OWN_VIEW = props.views[0];
const isActive = () => robot.activeView === OWN_VIEW;

// ---------- 拖拽 / 缩放 ----------
const pos = reactive({ x: 12, y: 12 });
const size = reactive({ w: 300, h: 260 });
const LS_KEY = "efort.campanel";

function loadLayout() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEY) || "null");
    if (s && typeof s.x === "number") {
      pos.x = s.x; pos.y = s.y; size.w = s.w; size.h = s.h; return;
    }
  } catch (e) { /* 忽略 */ }
  pos.x = 12; pos.y = 12;
}
function saveLayout() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      x: Math.round(pos.x), y: Math.round(pos.y),
      w: Math.round(size.w), h: Math.round(size.h),
    }));
  } catch (e) { /* 忽略 */ }
}

let drag = null;
function onHeadDown(e) {
  if (e.target.closest(".cam-toggle")) return;
  drag = { sx: e.clientX, sy: e.clientY, ox: pos.x, oy: pos.y, moved: false };
  window.addEventListener("pointermove", onDragMove);
  window.addEventListener("pointerup", onDragUp);
}
function onDragMove(e) {
  if (!drag) return;
  const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;
  pos.x = Math.max(0, Math.min(window.innerWidth - 80, drag.ox + dx));
  pos.y = Math.max(0, Math.min(window.innerHeight - 44, drag.oy + dy));
}
function onDragUp() {
  window.removeEventListener("pointermove", onDragMove);
  window.removeEventListener("pointerup", onDragUp);
  if (drag && drag.moved) saveLayout();
  drag = null;
}

let rz = null;
function onResizeDown(e) {
  e.stopPropagation();
  rz = { sx: e.clientX, sy: e.clientY, ow: size.w, oh: size.h };
  window.addEventListener("pointermove", onResizeMove);
  window.addEventListener("pointerup", onResizeUp);
}
function onResizeMove(e) {
  if (!rz) return;
  const dx = e.clientX - rz.sx, dy = e.clientY - rz.sy;
  size.w = Math.max(240, Math.min(760, rz.ow + dx));
  size.h = Math.max(160, Math.min(620, rz.oh + dy));
}
function onResizeUp() {
  window.removeEventListener("pointermove", onResizeMove);
  window.removeEventListener("pointerup", onResizeUp);
  saveLayout();
  rz = null;
}

// ---------- 状态计算 ----------
const device = computed(() => {
  const d = cam.status && cam.status.device;
  return d && d.model ? `${d.model} · ${d.ip}` : "—";
});

const dotCls = computed(() => {
  if (cam.serviceDown) return "err";
  const s = cam.status;
  if (!s) return "";
  if (s.opened && !s.closing) return "ok";
  if (s.opening || s.closing) return "warn";
  return "";
});

const stateText = computed(() => {
  if (cam.serviceDown) return "服务不可用";
  const s = cam.status;
  if (!s) return "未连接";
  if (s.opening) return "打开中…";
  if (s.closing) return "关闭中…";
  if (s.opened) {
    // 审计修复 P0-ui-1：区分"画面中断（显示路已判死）"与"正在建连（等首帧）"
    if (streamStalled.value) return "画面中断";
    if (connecting.value) return "连接中…";
    return "已开启";
  }
  return "已关闭";
});

const placeholderTitle = computed(() => {
  if (cam.serviceDown) return "相机服务不可用";
  const s = cam.status;
  if (s && s.opening) return "正在打开相机…";
  if (s && s.closing) return "正在关闭…";
  if (s && s.opened) {
    // 审计修复 P0-ui-1：占位层永远带文字 + 深色底（见 .cam-placeholder），
    // "连接中"不再表现为一块没有任何提示的纯黑
    if (streamStalled.value) return "画面中断，正在自动重连…";
    if (connecting.value) return "正在连接画面…";
    return "等待画面…";
  }
  return "相机未开启";
});

const placeholderSub = computed(() => {
  if (cam.serviceDown) return "请启动 camera/camera_service.py 后点「重试」";
  if (streamStalled.value) return "MJPEG 连接已断开，正在重新建连";
  if (connecting.value) return "首帧到达后自动显示画面";
  return "";
});

// 颜色检测状态
const colorStatus = computed(() => {
  const v = cam.status?.vision;
  if (!v || !v.supported) return { text: "不支持", ok: false };
  if (v.enabled) return { text: "运行中", ok: true };
  return { text: "已关闭", ok: false };
});

// ---------- 轮询 ----------
// ★ 与旧版的关键差别：**永不主动停**（只要本面板还是活跃视图）。
//   旧版遇到错误就 return，服务恢复后界面永远停在"未连接" —— 必须手点刷新。
let pollTimer = null;
function stopPoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}
async function poll() {
  pollTimer = null;
  if (!isActive()) return;
  const busy = await cam.refresh();
  let delay;
  if (cam.serviceDown) delay = 3000;        // 服务不可达：慢速重试，恢复后自动接上
  else if (busy) delay = 400;               // 开关过渡中：加速轮询
  else if (cam.opened) delay = 1000;        // 正常取流：1s 一次状态
  else delay = 3000;                        // 服务在线但相机没开：慢速保活
  if (document.hidden) delay = Math.max(delay, 5000);   // 后台标签页降频
  pollTimer = setTimeout(poll, delay);
}
function startPoll(delay = 300) {
  stopPoll();
  pollTimer = setTimeout(poll, delay);
}

// ---------- MJPEG 流 ----------
// 断流看门狗：MJPEG 是长连接，被服务端/网络悄悄掐断时浏览器**既不报错也不重连**，
// 画面就那么定住（"实时捕捉不到"）。所以必须自己盯着"最后一帧的时间"。
const STREAM_STALL_MS = 15000;           // ★ 15s 超时：工业相机偶尔卡顿，太短会误触发
const RECONNECT_GRACE_MS = 8000;         // ★ 重连后 8s 稳定期：新连接第一帧可能慢，别急着又断
let lastFrameAt = 0;
let lastReconnectAt = 0;                 // ★ 记录上次重连时间，稳定期内不看门狗
let streamRetryMs = 1000;
let streamRetryTimer = null;
const streamStalled = ref(false);

// 审计修复 P0-ui-1：MJPEG 双缓冲（两路 <img> 叠放，消除重连黑帧窗口）。
// 旧实现重连时**直接改写/移除显示用 <img> 的 src**：src 一改，浏览器立刻丢弃旧图，
// 新连接首帧到达前该 <img> 处于"无图"状态 —— 叠上 .cam-img 的 var(--screen)(#000)
// 黑底就是一段几百 ms ~ 数秒的纯黑窗口；onFrameError 又只清时间戳，得等 2.5s 兜底
// tick 才发现断流。现在改为：
//   front（显示路）= 正在显示的那一路，重连期间**始终保留旧帧**，绝不动它的 src；
//   back （预连接路）= .cam-back（透明底）的一路，重连时在它身上挂新 URL(cache-bust)，
//        首帧到达即"换正"；旧路随后被清掉 src（它此时已退居幕后，被新路的不透明底
//        完全遮住，清 src 不会露出黑底）。
// 于是重连全程要么是"旧帧 + 半透明重连覆盖层"，要么是"深色占位层 + 文字"，
// 任何时刻都不露纯黑底。
const imgA = ref(null);
const imgB = ref(null);
const front = ref("a");                  // 当前显示的是哪一路（"a" | "b"）
const connecting = ref(false);           // 预连接路已挂上 URL、正在等首帧

const elOf = (slot) => (slot === "a" ? imgA.value : imgB.value);
const backSlot = () => (front.value === "a" ? "b" : "a");

function onFrameLoad(e) {
  const t = e.target;
  lastFrameAt = Date.now();
  streamRetryMs = 1000;                    // 有帧回来 → 退避复位
  if (t !== elOf(backSlot()) || !connecting.value) return;   // 显示路在出帧（正常状态）
  // 预连接路首帧到达 → 换正：显示路从头到尾一帧都没丢过
  const old = front.value;
  front.value = backSlot();
  liveFlag.value = true;
  connecting.value = false;
  streamStalled.value = false;
  lastReconnectAt = Date.now();
  // ★★ 退回：必须清掉旧路的 src。
  //    曾经为了"消除换帧闪黑"改成"保留旧帧作底衬"，结果是**严重回归**：
  //    dom 顺序里 imgB 在 imgA 之上（后面的元素盖前面的），而 .cam-back 只把 CSS 背景设成
  //    透明 —— **图片本身仍然是不透明地画在上层的**。于是旧帧一直盖着新显示路，
  //    画面冻在旧帧、检测框自然"看着不动了"（现场表现为"怎么不检测了"）。
  //    正解仍是：换正之后立刻释放旧路，让它不再绘制。
  //    先让 Vue 把 .cam-back 类更新到 DOM（"先遮后放"），再清 src。
  nextTick(() => {
    const oldEl = elOf(old);
    if (oldEl && front.value !== old) oldEl.removeAttribute("src");
  });
}

// ★ 现场问题（画面左上角偶尔闪出一个白色小方块 + 浏览器"破图"图标）：
//   失败的 <img> 会渲染浏览器的破图占位。直接 removeAttribute("src") 有两个毛病：
//     ① 迟到/已被忽略的错误会走 early-return，**根本没清** → 图标留在那儿；
//     ② 有些浏览器对"曾经失败过的 img"仍保留破图状态。
//   改成：只要不是"正在显示的活帧"，一律把 src 换成 **1×1 透明像素**（真实可解码的图，
//   浏览器会立刻退出破图状态，且不占带宽、不建立任何连接）。
const BLANK_PX = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";

function onFrameError(e) {
  // 审计修复 P0-ui-1：错误**即时处置**。旧版只 `lastFrameAt = 0`，要等 2.5s 兜底
  // tick 才发现断流，期间画面层已经没有帧、露着 #000 黑底且没有任何提示。
  const t = e.target;
  const killBroken = () => { try { t.src = BLANK_PX; } catch (err) { /* 忽略 */ } };
  if (t === elOf(backSlot())) {
    // ★ 先把破图占位干掉，再判"这次尝试是否还需要处理" ——
    //   迟到的错误（connecting 已复位）以前直接 return，图标就留下了。
    killBroken();
    if (!connecting.value) return;
    connecting.value = false;              // 预连接失败：显示路旧帧不动，退避后重试
    scheduleRetry();
    return;
  }
  if (!liveFlag.value) { killBroken(); return; }    // 显示路还没画面 → 交给 checkStall 自愈
  // 显示路已出过帧：**保留最后一帧**（不清 src，避免黑一下），仅标记断流并另起一路预连接
  lastFrameAt = 0;
  streamStalled.value = true;
  if (!connecting.value) beginConnect();
}

/** 在**预连接路**上挂新 URL（时间戳 cache-bust）；显示路的 src 一律不动。 */
function beginConnect() {
  if (connecting.value) return true;
  const back = elOf(backSlot());
  if (!back) return false;
  lastReconnectAt = Date.now();
  lastFrameAt = Date.now();                // 与旧版一致：给新连接一个起点
  connecting.value = true;
  back.src = CAM_URL + "/stream?t=" + Date.now();
  return true;
}

/** 指数退避重连（1s 起，封顶 8s）。 */
function scheduleRetry() {
  const d = streamRetryMs;
  streamRetryMs = Math.min(streamRetryMs * 2, 8000);   // 指数退避，避免疯狂重连
  if (streamRetryTimer) clearTimeout(streamRetryTimer);
  streamRetryTimer = setTimeout(() => {
    streamRetryTimer = null;
    if (isActive() && !document.hidden && cam.canStream) connectStream();
  }, d);
}

/** 停止取流（关相机 / 切走视图 / 页面隐藏）：两路都释放，回到带文字的深色占位层。 */
function releaseStream() {
  if (streamRetryTimer) { clearTimeout(streamRetryTimer); streamRetryTimer = null; }
  ["a", "b"].forEach((s) => { const el = elOf(s); if (el) el.removeAttribute("src"); });
  liveFlag.value = false;
  connecting.value = false;
  streamStalled.value = false;
}

function connectStream() {
  // 页面隐藏时不断 MJPEG：隐藏页仍会持续解码视频流（GPU/网络大头）
  const want = cam.canStream && isActive() && !document.hidden;
  if (want) {
    // ★ 有在途连接、或正处在退避等待期 → 都不动（否则 1s 一次的状态轮询会把
    //   指数退避冲掉，服务一挂就变成每秒猛敲）。
    if (connecting.value || streamRetryTimer) return;
    if (!liveFlag.value) { beginConnect(); return; }   // 还没画面：占位层 + 预连接
    if (streamStalled.value) beginConnect();           // 显示路已判死：另一路预连接（旧帧继续显示）
  } else {
    // 审计修复 P0-ui-1：无条件释放（幂等），连退避中的重连定时器也一起清掉，
    // 否则关掉相机后还挂着一个到点的重连定时器。
    releaseStream();
  }
}

/** 预连接首帧超时：放弃这一路（清的是隐藏路的连接，不露黑底），退避后重试。 */
function checkConnectTimeout() {
  if (Date.now() - lastReconnectAt < RECONNECT_GRACE_MS) return;   // 新连接首帧给 8s 宽限
  const back = elOf(backSlot());
  if (back) back.removeAttribute("src");
  connecting.value = false;
  scheduleRetry();
}

function checkStall() {
  if (document.hidden || !isActive()) return;
  // ★ 该取流却没在取流 → 立刻自愈建连。
  //   以前这里遇到 !liveFlag 直接 return，建连**只**由 cam.status 变化触发；
  //   只要那一次触发落空（imgEl 尚未就绪、正好在切换视图、状态恰好没变），
  //   画面就再也不会出来 —— 表现就是"一直显示正在打开相机，刷新页面才见画面"。
  //   现在看门狗每 1s 兜一次（审计修复 P0-ui-1 把 2.5s 缩到 1s），最迟 1s 自动接上。
  if (!liveFlag.value) {
    if (connecting.value) { checkConnectTimeout(); return; }
    if (cam.canStream) connectStream();
    return;
  }
  // 有画面：要么盯着预连接首帧超时，要么盯着显示路是否断流
  if (connecting.value) { checkConnectTimeout(); return; }
  if (streamStalled.value) {
    if (!streamRetryTimer) beginConnect();   // 判死后没有在途连接也没在退避 → 立刻补一次
    return;
  }
  // ★ 重连后稳定期：新连接刚建立，第一帧可能慢（浏览器建连 + 服务端编码），
  //   这时不看门狗，避免"刚连上又断"的闪来闪去循环。
  if (Date.now() - lastReconnectAt < RECONNECT_GRACE_MS) return;
  if (Date.now() - lastFrameAt < STREAM_STALL_MS) return;
  // 15 秒没有新帧 → 判死显示路。**不清显示路的 src**：旧帧继续留在屏幕上，
  // 换正之前用户看到的是"冻结画面 + 正在重连覆盖层"，而不是黑屏。
  streamStalled.value = true;
  if (!beginConnect()) scheduleRetry();
}

watch(() => cam.status, connectStream);

// 页面隐藏 → 断流省资源；回到前台 → 重新拉流
function onVis() { connectStream(); }
document.addEventListener("visibilitychange", onVis);

let stallTimer = null;
onMounted(() => {
  loadLayout();
  poll();
  cam.loadDevices();  // 加载可用相机设备列表
  // 审计修复 P0-ui-1：兜底 tick 从 2.5s 缩到 1s —— 真正的断流已由 onFrameError
  // 即时处置，这里只剩"无声卡死"（连接还活着但不再出帧）的检测，密一点代价可忽略。
  stallTimer = setInterval(checkStall, 1000);
});
onBeforeUnmount(() => {
  stopPoll();
  if (stallTimer) { clearInterval(stallTimer); stallTimer = null; }
  if (streamRetryTimer) { clearTimeout(streamRetryTimer); streamRetryTimer = null; }
  document.removeEventListener("visibilitychange", onVis);
});

watch(() => robot.activeView, (v) => {
  if (v === OWN_VIEW) {
    if (!drag && !rz) loadLayout();
    poll();
    connectStream();
  } else {
    stopPoll();
    // 审计修复 P0-ui-1：两路一起释放（原来只清显示路的 src）。
    // 本面板已不在活跃视图，界面被 v-show 藏着，回到深色占位层不会被看见。
    releaseStream();
  }
});

// ---------- 操作 ----------
const acting = ref(false);
async function onToggle() {
  if (acting.value) return;
  acting.value = true;
  try {
    await cam.toggle();
  } catch (e) {
    cam.error = e.message;
  }
  acting.value = false;
  startPoll();
}

const switching = ref(false);
async function onDeviceChange(e) {
  const serial = e.target.value;
  const device = cam.devices.find(d => d.serial === serial);
  if (!device) return;
  switching.value = true;
  try {
    const r = await cam.switchDevice(device.idx, device.cam_type);
    if (!r.ok) {
      cam.error = r.error || "切换失败";
    }
  } catch (e) {
    cam.error = e.message;
  }
  switching.value = false;
}

async function onSnap() {
  const r = await cam.snap();
  snapMsg.value = r.ok ? ("已保存: " + r.path) : ("保存失败: " + (r.error || "无画面"));
  setTimeout(() => { snapMsg.value = ""; }, 4000);
}

const reconnecting = ref(false);
async function reconnectCam() {
  if (reconnecting.value) return;
  reconnecting.value = true;
  try { await cam.reconnect(); } catch (e) { /* cam.error 已由 store 写入 */ }
  reconnecting.value = false;
  startPoll();
}

/** 服务不可用时的"重试"：直接重拉一次状态，通了就自动恢复轮询与取流。 */
async function retryService() {
  await cam.refresh();
  if (!cam.serviceDown) { startPoll(100); connectStream(); }
}
</script>

<template>
  <div class="cam-panel" :class="{ collapsed: !expanded, down: cam.serviceDown }"
       :style="{ left: pos.x + 'px', top: pos.y + 'px', width: size.w + 'px' }">
    <!-- 标题栏 -->
    <div class="cam-head" @pointerdown="onHeadDown">
      <Icon name="grip" :size="14" class="cam-grip" />
      <Icon name="camera" :size="15" class="cam-ico" />
      <span class="cam-title">摄像头</span>
      <span class="dot" :class="dotCls"></span>
      <span class="cam-state">{{ stateText }}</span>
      <span v-if="cam.status?.fps" class="cam-fps">{{ cam.status.fps }} fps</span>
      <button class="cam-toggle" type="button" :title="expanded ? '收起' : '展开'"
              @click="expanded = !expanded">
        <Icon :name="expanded ? 'chevronDown' : 'chevronRight'" :size="16" />
      </button>
    </div>

    <!-- 服务不可用：与"相机没开"是完全不同的两件事，必须区分显示 -->
    <div v-if="cam.serviceDown" class="svc-down">
      <Icon name="alert" :size="14" />
      <span class="sd-text">相机服务不可用</span>
      <button class="sd-retry" type="button" @click="retryService">
        <Icon name="refresh" :size="12" /> 重试
      </button>
    </div>

    <!-- 画面区域 -->
    <div class="cam-screen" :style="{ height: (expanded ? size.h : 200) + 'px' }">
      <!-- 审计修复 P0-ui-1：双缓冲两路画面 —— front 是显示路（保留旧帧 + 不透明底），
           .cam-back 是预连接路（透明底，拿到首帧才接管显示，重连窗口零黑帧） -->
      <img ref="imgA" class="cam-img" :class="{ 'cam-back': front !== 'a' }" alt=""
           @load="onFrameLoad" @error="onFrameError" />
      <img ref="imgB" class="cam-img" :class="{ 'cam-back': front !== 'b' }" alt=""
           @load="onFrameLoad" @error="onFrameError" />
      <div v-if="!liveFlag" class="cam-placeholder">
        <Icon :name="cam.serviceDown ? 'alert' : 'camera'" :size="32" class="cam-ph-icon" />
        <span class="cam-ph-title">{{ placeholderTitle }}</span>
        <span v-if="placeholderSub" class="cam-ph-sub">{{ placeholderSub }}</span>
      </div>
      <!-- 重连覆盖层：半透明盖在**仍然可见的旧帧**上（不是黑底），
           明确告诉用户"在自动重连"，而不是让人以为死机/黑屏 -->
      <div v-if="liveFlag && streamStalled" class="cam-stall">
        <Icon name="refresh" :size="12" class="spin" /> 正在重连画面…
      </div>
    </div>

    <!-- 主控制区域（收起时也显示） -->
    <div class="cam-controls">
      <!-- 设备选择下拉 -->
      <div class="device-select" v-if="cam.devices.length > 0">
        <label class="device-label">
          <Icon name="camera" :size="13" />
          <span>设备</span>
        </label>
        <select class="device-dropdown" :value="cam.currentDevice?.serial"
                :disabled="cam.busy || cam.opened || cam.serviceDown"
                @change="onDeviceChange">
          <option v-for="d in cam.devices" :key="d.serial" :value="d.serial">
            {{ d.model }} ({{ d.type }} {{ d.serial }})
          </option>
        </select>
      </div>

      <!-- 一键启停 -->
      <button class="power-btn" :class="{ on: cam.opened, busy: cam.busy || acting }"
              :disabled="cam.busy || acting || cam.serviceDown" @click="onToggle">
        <Icon name="power" :size="18" />
        <span>{{ cam.busy ? '处理中…' : (cam.opened ? '关闭相机' : '开启相机') }}</span>
      </button>

      <!-- 快速状态栏 -->
      <div class="quick-stats">
        <div class="stat-item">
          <span class="stat-label">颜色检测</span>
          <span class="stat-value" :class="{ active: cam.colorEnabled }">{{ colorStatus.text }}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">识别次数</span>
          <span class="stat-value">{{ cam.colorCount }}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">画面</span>
          <span class="stat-value" :class="{ active: cam.opened && !streamStalled }">
            {{ cam.opened ? (streamStalled ? '中断' : '正常') : '—' }}
          </span>
        </div>
      </div>
    </div>

    <!-- 展开后的详细控制 -->
    <div v-show="expanded" class="cam-body">
      <!-- ★ 需求：原"当前相机为灰度相机…"说明框已移除，移到「关于」页（见 AboutView）。
           这里不再重复占位。 -->

      <!-- 颜色分拣开关 -->
      <div class="control-row" v-if="cam.colorSupported && cam.colorCapable">
        <label class="control-label">颜色分拣</label>
        <label class="switch">
          <input type="checkbox" :checked="cam.colorEnabled" :disabled="cam.serviceDown"
                 @change="cam.setColor($event.target.checked)" />
          <span class="slider"></span>
        </label>
      </div>

      <!-- ★ 目标检测置信度（YOLO）：误报多就往上调；与下面「三色置信度」是两套判据 -->
      <div class="control-row conf-row">
        <label class="control-label">
          检测置信度&nbsp;≥&nbsp;<b>{{ (cam.detConf * 100).toFixed(0) }}%</b>
        </label>
        <input class="conf-slider" type="range" min="15" max="95" step="5"
               :value="(cam.detConf * 100).toFixed(0)"
               :disabled="cam.serviceDown"
               title="目标检测置信度：低于此值的框不画、不记录（调高可减少误报，太高会漏检）"
               @change="cam.setConf($event.target.value / 100)" />
      </div>

      <!-- 三色识别目标 + 置信度阈值 -->
      <div class="control-row conf-row" v-if="cam.colorSupported && cam.colorCapable">
        <label class="control-label">
          三色置信度&nbsp;≥&nbsp;<b>{{ (cam.colorConf * 100).toFixed(0) }}%</b>
        </label>
        <input class="conf-slider" type="range" min="15" max="95" step="5"
               :value="(cam.colorConf * 100).toFixed(0)"
               :disabled="cam.serviceDown" title="confidence: conf 低于此值的目标不画框/不记录"
               @change="cam.setColorConf($event.target.value / 100)" />
      </div>
      <div class="target-row" v-if="cam.colorSupported && cam.colorCapable && cam.colorTargets.length">
        <span v-for="t in cam.colorTargets" :key="t" class="target-chip"
              :class="'chip-' + t">{{ t }}</span>
      </div>

      <!-- 操作按钮 -->
      <div class="action-btns">
        <button @click="onSnap" :disabled="!cam.opened">
          <Icon name="snapshot" :size="14" /> 截图
        </button>
        <button @click="reconnectCam" :disabled="reconnecting || cam.busy">
          <Icon name="refresh" :size="14" /> 重连
        </button>
      </div>

      <!-- 状态信息 -->
      <div class="info-grid">
        <span class="info-label">设备</span>
        <span class="info-value">{{ device }}</span>
        <span class="info-label">分辨率</span>
        <span class="info-value">{{ cam.status?.resolution || "—" }}</span>
        <span class="info-label">分割</span>
        <span class="info-value">{{ cam.status?.vision?.seg || "—" }}</span>
      </div>

      <!-- 消息提示 -->
      <div v-if="cam.error" class="error-msg">{{ cam.error }}</div>
      <div v-else-if="cam.deviceError" class="error-msg">{{ cam.deviceError }}</div>
      <div v-if="snapMsg" class="info-msg">{{ snapMsg }}</div>
    </div>

    <!-- 缩放手柄 -->
    <div class="cam-resize" @pointerdown="onResizeDown" title="拖动缩放">
      <Icon name="expand" :size="13" />
    </div>
  </div>
</template>

<style scoped>
/* ★ z-index 30：面板浮在 3D 视口上，必须高于关节 HUD(10)、状态片(14)、
   执行 HUD(12) —— 否则拖动/缩放面板时会被这些浮层"咬"掉一块。 */
.cam-panel {
  position: absolute; z-index: 30;
  background: var(--glass-solid);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: var(--shadow-hud);
  backdrop-filter: blur(12px) saturate(140%);
  -webkit-backdrop-filter: blur(12px) saturate(140%);
  overflow: hidden;
}
.cam-panel.down { border-color: var(--err-line); }

.cam-head {
  display: flex; align-items: center; gap: 7px;
  padding: 8px 10px;
  cursor: move; user-select: none; font-size: 13px;
  background: linear-gradient(180deg, var(--veil), rgba(255,255,255,0));
  border-bottom: 1px solid var(--line);
}
.cam-grip { color: var(--muted); cursor: move; opacity: .8; }
.cam-ico { color: var(--accent); }
.cam-title { font-weight: 600; }
.cam-state { color: var(--muted); margin-left: auto; font-size: 12px; }
.cam-fps { color: var(--ok); font-size: 11px; font-variant-numeric: tabular-nums; }
.cam-toggle {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; margin-left: 6px; padding: 0;
  border: none; border-radius: 6px;
  background: transparent; color: var(--muted); cursor: pointer;
}
.cam-toggle:hover { background: var(--veil2); color: var(--txt); }

.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
.dot.ok { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
.dot.warn { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
.dot.err { background: var(--err); box-shadow: 0 0 6px var(--err); }

/* ---- 服务不可用横幅 ---- */
.svc-down {
  display: flex; align-items: center; gap: 7px;
  padding: 6px 10px; font-size: 12px;
  color: var(--err); background: var(--err-soft);
  border-bottom: 1px solid var(--err-line);
}
.svc-down svg { flex: none; }
.sd-text { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.sd-retry {
  flex: none; width: auto; display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 9px; font-size: 11px; border-radius: 5px;
  color: var(--txt); background: var(--panel); border: 1px solid var(--line);
  cursor: pointer;
}
.sd-retry:hover { border-color: var(--accent); color: var(--accent); }

.cam-screen {
  position: relative; width: 100%; background: var(--screen);
  border-bottom: 1px solid var(--line); overflow: hidden;
  transition: height .18s ease;
}
/* 审计修复 P0-ui-1：两路 <img> 绝对定位叠放（双缓冲）。
   .cam-back 是预连接路 —— **透明底**（没有 var(--screen) 黑底），所以它在还没拿到
   首帧时完全看不见，绝不会把显示路的旧帧盖成黑块；首帧一到它才自然接管显示。
   不用 opacity/display 隐藏：那样浏览器可能不给它解码出帧，首帧事件就收不到了。 */
.cam-img {
  position: absolute; left: 0; top: 0; width: 100%; height: 100%;
  object-fit: contain; background: var(--screen); display: block;
}
.cam-back { background: transparent; pointer-events: none; }
.cam-placeholder {
  position: absolute; inset: 0;
  /* 审计修复 P0-ui-1：占位层自带深色底（不是 --screen 的纯 #000）——
     "等待/连接中"与真正的黑屏在视觉上必须能一眼分开 */
  background: linear-gradient(160deg, #171d25, #10151b);
  display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 6px;
  color: var(--muted); pointer-events: none; text-align: center; padding: 0 14px;
}
.cam-ph-icon { opacity: .5; }
.cam-ph-title { font-size: 13px; color: var(--txt); }
.cam-ph-sub { font-size: 11px; line-height: 1.5; }
/* 审计修复 P0-ui-1：重连覆盖层 —— 由原来的左下角小角标升级为整屏半透明覆盖，
   盖在仍然可见的旧帧上（不露黑底），明确提示"正在自动重连" */
.cam-stall {
  position: absolute; inset: 0; z-index: 2;
  display: flex; align-items: center; justify-content: center; gap: 6px;
  padding: 0; border-radius: 0; font-size: 12px;
  color: var(--warn); background: rgba(12, 16, 22, .55);
  border: none; pointer-events: none;
}

/* ---- 主控制区域 ---- */
.cam-controls {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--scrim);
}

/* ---- 设备选择下拉 ---- */
.device-select {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 10px; padding: 8px 10px;
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 6px;
}
.device-label {
  display: flex; align-items: center; gap: 5px;
  font-size: 12px; color: var(--muted); white-space: nowrap;
}
.device-dropdown {
  flex: 1; min-width: 0;
  padding: 5px 8px; font-size: 12px;
  background: var(--glass); color: var(--txt);
  border: 1px solid var(--line); border-radius: 4px;
  cursor: pointer; outline: none;
}
.device-dropdown:hover:not(:disabled) { border-color: var(--accent); }
.device-dropdown:disabled { opacity: .5; cursor: not-allowed; }
.device-dropdown option { background: var(--panel); color: var(--txt); }

.power-btn {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  width: 100%; padding: 10px 16px;
  font-size: 14px; font-weight: 600;
  border: none; border-radius: 8px;
  cursor: pointer; transition: all .15s ease;
  background: var(--panel2); color: var(--txt);
}
.power-btn:hover:not(:disabled) { background: var(--panel3, var(--panel)); }
.power-btn:disabled { opacity: .5; cursor: not-allowed; }
.power-btn.on {
  background: linear-gradient(135deg, var(--ok), var(--c-green));
  color: var(--ink);
  box-shadow: var(--shadow);
}
.power-btn.on:hover:not(:disabled) {
  background: linear-gradient(135deg, var(--c-green), var(--ok-btn));
}
.power-btn.busy {
  background: var(--warn); color: var(--ink);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .7; } }

.quick-stats {
  display: flex; justify-content: space-around;
  margin-top: 10px; padding-top: 10px;
  border-top: 1px solid var(--line);
}
.stat-item {
  display: flex; flex-direction: column; align-items: center; gap: 2px;
}
.stat-label { font-size: 10px; color: var(--muted); }
.stat-value { font-size: 12px; font-weight: 500; font-variant-numeric: tabular-nums; }
.stat-value.active { color: var(--ok); }

/* ---- 展开后的详细控制 ---- */
.cam-body {
  padding: 12px;
  display: flex; flex-direction: column; gap: 12px;
}

.control-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px;
}
.control-label {
  font-size: 12px; color: var(--muted);
  white-space: nowrap;
}

/* 开关样式 */
.switch {
  position: relative; display: inline-block;
  width: 40px; height: 22px;
}
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute; cursor: pointer;
  top: 0; left: 0; right: 0; bottom: 0;
  background: var(--panel2);
  border-radius: 22px;
  transition: .2s;
}
.slider:before {
  position: absolute; content: "";
  height: 16px; width: 16px;
  left: 3px; bottom: 3px;
  background: var(--muted);
  border-radius: 50%;
  transition: .2s;
}
input:checked + .slider { background: var(--accent); }
input:checked + .slider:before {
  transform: translateX(18px);
  background: var(--ink);
}
input:disabled + .slider { opacity: .45; cursor: not-allowed; }

/* 灰度相机提示 */
.color-unavail {
  display: flex; align-items: center; gap: 7px;
  font-size: 12px; line-height: 1.5; color: #b3872a;
  background: rgba(214, 158, 46, 0.10); border: 1px solid rgba(214, 158, 46, 0.28);
  border-radius: 8px; padding: 8px 10px; margin-bottom: 8px;
}
.color-unavail .dot { width: 8px; height: 8px; border-radius: 50%; background: #d69e2e; flex: none; }
.color-unavail strong { color: #e8b347; }

/* 三色置信度滑杆 */
.conf-row { padding-top: 4px; }
.conf-slider {
  width: 128px; flex: none; accent-color: var(--accent); cursor: pointer;
}
.conf-slider:disabled { opacity: .45; cursor: not-allowed; }

/* 三色目标 chips */
.target-row { display: flex; gap: 6px; margin-top: 6px; }
.target-chip {
  font-size: 11px; line-height: 1; padding: 4px 10px; border-radius: 10px;
  color: #fff; opacity: .92;
}
.chip-红 { background: #e5404a; }
.chip-绿 { background: #2f9e57; }
.chip-蓝 { background: #2f6fed; }

.action-btns {
  display: flex; gap: 6px;
}
.action-btns button {
  flex: 1; display: flex; align-items: center; justify-content: center; gap: 4px;
  padding: 6px 8px;
  font-size: 11px;
  background: var(--panel); color: var(--txt);
  border: 1px solid var(--line); border-radius: 6px;
  cursor: pointer;
}
.action-btns button:hover:not(:disabled) {
  background: var(--panel2);
  border-color: var(--accent);
}
.action-btns button:disabled {
  opacity: .4; cursor: not-allowed;
}

.info-grid {
  display: grid; grid-template-columns: auto 1fr;
  gap: 4px 10px; font-size: 11px;
}
.info-label { color: var(--muted); }
.info-value { color: var(--txt); font-variant-numeric: tabular-nums; }

.error-msg {
  color: var(--err); font-size: 11px;
  padding: 6px 8px; line-height: 1.5;
  background: var(--err-soft);
  border-radius: 4px;
}
.info-msg {
  color: var(--muted); font-size: 11px;
  padding: 4px 8px;
  background: var(--panel);
  border-radius: 4px;
}

.cam-resize {
  position: absolute; right: 0; bottom: 0;
  width: 18px; height: 18px;
  display: flex; align-items: center; justify-content: center;
  color: var(--muted); cursor: nwse-resize; opacity: .7;
}
.cam-resize:hover { opacity: 1; color: var(--accent); }

.spin { animation: cam-spin .8s linear infinite; }
@keyframes cam-spin { to { transform: rotate(360deg); } }
</style>