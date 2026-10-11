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
import { highlightJoint } from "../three/manager.js";
import CameraPanel from "./CameraPanel.vue";
import MonitorLayout from "./MonitorLayout.vue";
import RobotParamsCard from "./RobotParamsCard.vue";

const robot = useRobotStore();

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
      <!-- ★ R1：本页是只读监控，右栏只有关节角 / TCP 两张读数卡，相对其它页偏窄。
           这里显式加一行"本页能做什么"的指路（不新增任何按钮/装饰），补足信息与高度。 -->
      <p class="small muted">本页为<b>只读监控</b>：示教 / 点动 / 执行请到「点位执行」页；点动前先到底栏「真机链路」一键就绪。</p>
      <!-- 关节角 + 末端 TCP：只读读数。连接状态已整合到底栏「机器人链路」状态灯，
           这里不再重复展示（重连动作也从底部状态灯里点开）。 -->
      <RobotParamsCard :connection="false" />
    </div>
  </MonitorLayout>
</template>
