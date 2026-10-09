<script setup>
// =====================================================================
// 连接自检 / 操作引导条：把现场高频故障翻译成"下一步该干什么"。
//
//   - 网线没插              → "请连接机器人网线（控制器 ↔ 工控机），并确认 IP 同网段"
//   - 网通但无数据          → "请把示教器打到 AUTO（远程）模式，确认急停复位、伺服上电"
//   - 有数据但没控制权      → "请点『请求控制』输入管理员密码获取限时控制令牌"
//   - 急停 / 围栏危险       → 对应复位与处置指引
//
// ★ 数据来自 stores/link.js —— 与底栏三盏状态灯**共用同一份快照**。
//   以前这里自己 setInterval 拉一次 /system/guide，状态灯再拉一次，两份状态
//   迟早会出现"灯是绿的、这条还是红的"的自相矛盾界面，现场两个都不敢信。
//   现在只有一个轮询者（link store），本组件只消费。
//
// 本组件**只**在有未通过项时出现（常态由底栏三盏灯承担显示职责），
// 因此它天生比状态灯"矮一头"：不占位、不刷屏、说完就走。
// =====================================================================
import { computed } from "vue";
import Icon from "./Icon.vue";
import { useLinkStore } from "../stores/link.js";

const link = useLinkStore();

const primary = computed(() => link.primary);

const ICON_OF = {
  net: "plug", link: "wifi", mode: "terminal", token: "lock",
  estop: "power", fence: "shield", real_write: "database", run_mode: "terminal",
};
</script>

<template>
  <!-- ★ 常态不渲染任何东西：正常情况下视口紧贴 Tab 栏，底栏只有状态灯 + 控制区。
       出问题时才出现，且只报最高优先级的一条。 -->
  <div v-if="primary" class="gb">
    <div class="gb-bar" :class="primary.level === 'error' ? 'err' : 'warn'">
      <Icon :name="ICON_OF[primary.key] || 'alert'" :size="15" />
      <span class="gb-title">{{ primary.title }}</span>
      <span class="gb-hint">{{ primary.hint }}</span>
    </div>
  </div>
</template>

<style scoped>
/* Sentinel 风格引导条：毛玻璃 + 发光线 */
.gb { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.gb-bar { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: var(--radius-sm);
  font-size: 12px; border: 1px solid var(--line); background: var(--glass-solid);
  backdrop-filter: blur(12px) saturate(140%); -webkit-backdrop-filter: blur(12px) saturate(140%);
  box-shadow: var(--shadow); position: relative; overflow: hidden; }
.gb-bar::before { content: ''; position: absolute; top: 0; left: 10%; right: 10%; height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent-line), transparent); pointer-events: none; }
.gb-bar.warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.gb-bar.warn::before { background: linear-gradient(90deg, transparent, var(--warn-line), transparent); }
.gb-bar.err { color: var(--err); border-color: var(--err-line); background: var(--err-soft); }
.gb-bar.err::before { background: linear-gradient(90deg, transparent, var(--err-line), transparent); }
.gb-title { font-weight: 600; flex: none; line-height: 1.5; }
.gb-hint { color: var(--muted); flex: 1; min-width: 0; line-height: 1.5;
  white-space: normal; overflow-wrap: anywhere; }
</style>
