<script setup>
// 真实监控视图：3D 场景 + HUD + 侧栏（连接 / 关节角读数 / TCP）。
//
// ★ 本视图是**只读监控视图**：所有关键点（连接状态 / 关节角 / TCP）只做展示，
//   不提供任何修改入口（唯一的「重连机器人」也是为了让链路恢复，不改机器人）。
//   要示教、改姿态、点动，请到「点位执行」视图。
//
// ★ 三张只读卡片（连接 / 关节角 / TCP）来自 RobotParamsCard.vue，
//   与「点位执行」「程序执行」页**共用同一份实现** —— 那两页也各自需要这些参数，
//   但三份独立实现必然漂移（量程归一、悬停高亮、取色只要有一处不同就自相矛盾）。
//
// ★ 姿态来源：本视图**不再自己调 applyRobotPose**（全站唯一的姿态下发点在 App.vue
//   的渲染循环里）。本视图只负责一件事：**未连接真机时把显示姿态钉成全 0**。
//   后端在模拟模式下会推正弦扫掠帧（simulate_pose）—— 那不是"机器人现在在哪"，
//   本页据此把它替换成静止全 0；连上真机后自动转回实时遥测跟随。
//   ⇒ 本页不再有任何"离线跳舞演示"（演示动作只保留在「模拟仿真」页）。
import { computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useRobotStore, zeroQ, zeroTcp } from "../stores/robot.js";
import { useCameraStore } from "../stores/camera.js";
import { highlightJoint } from "../three/manager.js";
import CameraPanel from "./CameraPanel.vue";
import MonitorLayout from "./MonitorLayout.vue";
import RobotParamsCard from "./RobotParamsCard.vue";
import TwinResidualCard from "./TwinResidualCard.vue";
import Icon from "./Icon.vue";

const robot = useRobotStore();
const cam = useCameraStore();

/**
 * ★ 2026-10-11 用户口径：相机与「实时捕捉」**默认都关**（不再启动即开相机）。
 *   「实时捕捉」= 本页是否实时抓取相机画面（MJPEG 取流）的显式开关：
 *     · 默认关；可手动开/关，**不需要控制权限**（按钮不禁用）；
 *     · 关掉时 CameraPanel 会**真正释放取流**（不是只隐藏画面）；
 *     · 状态如实：下方提示文案按"服务不可用 / 未开捕捉 / 相机未开 / 正在抓取"逐级说清。
 *   拿到控制权限后由 App.vue 自动把它与相机一起开启（幂等）。
 */
const captureHint = computed(() => {
  if (cam.serviceDown) return "相机服务不可用（8100 未启动）";
  if (!cam.liveCapture) return "已关闭：不抓取相机画面（点上方按钮开启，不需要权限）";
  if (!cam.opened) return "已开启：等待相机开启后显示实时画面";
  return "已开启：正在实时抓取相机画面";
});

// 离线：未连接真机，或后端已降级为模拟数据（两种情况都没有真机位姿可读）
const offline = computed(() => !robot.connected || robot.simulated);

/**
 * 未连接 → 经 demo 通道把显示姿态钉成全 0（3D 与读数卡同源，数字和画面对得上）。
 * ★ 只有本视图是 demo 通道的 owner；且只在 activeView === "live" 时开、切页立刻关 ——
 *   否则会把「点位执行」「模拟仿真」等页面也一起钉成 0。
 */
function syncOffline() {
  if (robot.activeView === "live" && offline.value) {
    robot.setDemo(true, zeroQ(), zeroTcp());
  } else {
    robot.setDemo(false);
  }
}

// 连接状态 / 视图切换 → 重算是否钉零
// （「重连机器人」按钮改的是 connected/simulated，由 offline 触发本回调，无需额外事件）
watch([() => robot.activeView, offline], syncOffline);

onMounted(syncOffline);
onBeforeUnmount(() => { highlightJoint(-1); robot.setDemo(false); });
</script>

<template>
  <MonitorLayout view="live">
      <template #hud>
        <CameraPanel />
      </template>
    <div class="side-cards">
      <!-- ★ 2026-10-11 用户口径：「实时捕捉」开关（默认关、不需权限、状态如实）。
           放在右栏最上，与 3D 视口右上角的摄像头面板配套：面板管"相机设备开/关"，
           这里管"本页是否实时抓取画面"。关掉时面板会真正释放取流。 -->
      <div class="card live-cap">
        <h3>
          <Icon name="camera" :size="14" /> 实时捕捉
          <span class="h3-sub" :class="{ on: cam.liveCapture }">{{ cam.liveCapture ? "已开启" : "已关闭" }}</span>
        </h3>
        <div class="btns">
          <button :class="{ primary: cam.liveCapture }"
                  :title="cam.liveCapture
                    ? '关闭后停止抓取相机画面（可随时再开，不需要权限）'
                    : '开始实时抓取相机画面（不需要控制权限）'"
                  @click="cam.toggleLiveCapture()">
            <Icon name="power" :size="15" /> {{ cam.liveCapture ? "关闭实时捕捉" : "开启实时捕捉" }}
          </button>
        </div>
        <p class="small" style="margin-top:8px">{{ captureHint }}</p>
      </div>
      <!-- ★ R1：本页是只读监控，右栏只有关节角 / TCP 两张读数卡，相对其它页偏窄。
           这里显式加一行"本页能做什么"的指路（不新增任何按钮/装饰），补足信息与高度。 -->
      <p class="small muted">本页为<b>只读监控</b>：示教 / 点动 / 执行请到「点位执行」页；点动前先到底栏「真机链路」一键就绪。</p>
      <!-- 关节角 + 末端 TCP：只读读数。连接状态已整合到底栏「机器人链路」状态灯，
           这里不再重复展示（重连动作也从底部状态灯里点开）。 -->
      <RobotParamsCard :connection="false" />
      <!-- ★ E2 到位自证（只读）：下发那一段到底走没走到，用逐拍实测残差摊开给人看。
           判据在后端一份（WS 位姿帧捎带 + GET /api/twin/residual），这里只渲染。 -->
      <TwinResidualCard />
    </div>
  </MonitorLayout>
</template>

<style scoped>
/* 「实时捕捉」标题右侧的状态小标：开着用绿、关着沿用全局 .h3-sub 的灰。
   ★ 不做美化兜底 —— 显示的就是 cam.liveCapture 的真实值。 */
.live-cap .h3-sub.on { color: var(--ok); }
</style>
