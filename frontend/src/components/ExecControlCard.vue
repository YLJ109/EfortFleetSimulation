<script setup>
// =====================================================================
// 执行域公用卡片：控制权限 + 真空吸放 + 急停/复位。
//
// ★ 点位执行 与 程序执行 两页共用同一份（逻辑全在 stores/exec.js）。
//   安全相关的 UI 只有一处实现，改判据时不可能漏掉其中一个页面。
// =====================================================================
import { computed } from "vue";
import Icon from "./Icon.vue";
import { useAuthStore } from "../stores/auth.js";
import { useExecStore } from "../stores/exec.js";
import { useRobotStore } from "../stores/robot.js";
import { useRcReadyStore } from "../stores/rcReady.js";

const auth = useAuthStore();
const exec = useExecStore();
const robot = useRobotStore();
const rc = useRcReadyStore();

// ★ 控制权限必须同时满足：有令牌 + 机器人已连接 + 遥测未冻结
const canControl = computed(() => auth.controlActive && robot.connected && !robot.telemetryStale);

// ★ 「已获得控制权限」只有一键就绪成功后显示
const granted = computed(() => canControl.value && rc.ready);

// ★ 急停/复位只看令牌（不能被机器人离线禁掉）
const canEstop = computed(() => auth.controlActive);
</script>

<template>
  <div class="card">
    <h3><Icon name="shield" :size="15" /> 控制权限
      <!-- ★ A4：角色与剩余时长由**底栏控制区**统一表达（「控制中 · 角色 · 剩余时间」）——
           本卡不再重复，只报本页这一步的状态（已就绪 / 待一键就绪）。 -->
      <span class="h3-sub">{{ granted ? "已就绪" : "待一键就绪" }}</span>
    </h3>

    <div class="row">
      <span class="k">状态</span>
      <span class="v" :style="{ color: granted ? 'var(--ok)' : 'var(--warn)' }">
        {{ granted ? "已就绪（可示教 / 点动）"
           : (!robot.connected ? "机器人离线"
              : (!canControl ? "未获得控制权限" : "待一键就绪")) }}
      </span>
    </div>

    <!-- ★ 2026-10-09 用户需求："释放控制按钮移除掉" —— 控制令牌的释放走底栏/权限区，不在执行卡内重复 -->
    <div class="btns">
      <button v-if="auth.controlActive" @click="auth.logout()">
        <Icon name="power" :size="14" /> 重新请求控制
      </button>
      <button v-else class="primary" @click="auth.requestLogin && auth.requestLogin()">
        <Icon name="lock" :size="14" /> 请求控制
      </button>
    </div>

    <!-- ★ 真空吸放：电平保持（写 40135 触发位，控制器 210 服务执行）。
         ★★ 2026-10-11（用户权威位表）：动作集扩到 8 个，两面**各自独立气路** ——
           上一排 = 下板/正面（Bit1~4）；下一排 = 反面/上板（Bit5~8，标签带「反面」一眼可辨）。 -->
    <hr class="sp-hr" />
    <div class="btns vac-btns">
      <button class="vac-suck" :class="{ 'is-on': exec.vacuumState === 'suck' }"
              :disabled="!canControl || exec.vacuumBusy"
              @click="exec.vacuum('suck')"
              title="下板/正面 吸气（40135.Bit1）">
        <Icon name="download" :size="14" /> 吸气
      </button>
      <button class="vac-release" :disabled="!canControl"
              @click="exec.vacuum('release')"
              title="下板/正面 停止吸气（40135.Bit2）">
        <Icon name="upload" :size="14" /> 停止吸气
      </button>
      <button class="vac-blow" :class="{ 'is-on': exec.vacuumState === 'blow' }"
              :disabled="!canControl"
              @click="exec.vacuum('blow')"
              title="下板/正面 放气（40135.Bit3）">
        <Icon name="eject" :size="14" /> 放气
      </button>
      <button class="vac-unblow" :disabled="!canControl"
              @click="exec.vacuum('unblow')"
              title="下板/正面 停止放气（40135.Bit4）">
        <Icon name="close" :size="14" /> 停止放气
      </button>
    </div>
    <!-- 反面/上板那一路（与下板独立，放一件不影响另一件） -->
    <div class="btns vac-btns vac-btns-back">
      <button class="vac-suck vac-back" :class="{ 'is-on': exec.vacuumStateBack === 'suck' }"
              :disabled="!canControl || exec.vacuumBusy"
              @click="exec.vacuum('suck_back')"
              title="反面/上板 吸气（40135.Bit5）">
        <Icon name="download" :size="14" /> 反面吸气
      </button>
      <button class="vac-release vac-back" :disabled="!canControl"
              @click="exec.vacuum('release_back')"
              title="反面/上板 停止吸气（40135.Bit6）">
        <Icon name="upload" :size="14" /> 反面停止吸气
      </button>
      <button class="vac-blow vac-back" :class="{ 'is-on': exec.vacuumStateBack === 'blow' }"
              :disabled="!canControl"
              @click="exec.vacuum('blow_back')"
              title="反面/上板 放气（40135.Bit7）">
        <Icon name="eject" :size="14" /> 反面放气
      </button>
      <button class="vac-unblow vac-back" :disabled="!canControl"
              @click="exec.vacuum('unblow_back')"
              title="反面/上板 停止放气（40135.Bit8）">
        <Icon name="close" :size="14" /> 反面停止放气
      </button>
    </div>

    <!-- 急停 / 复位急停 -->
    <div class="btns mt8">
      <button class="rec-on" :disabled="!canEstop || exec.estopBusy"
              @click="exec.doEstop()">
        <Icon name="power" :size="14" /> 急停
      </button>
      <button :disabled="!canEstop || exec.estopBusy" @click="exec.resetEstop()">
        <Icon name="refresh" :size="14" /> 复位急停
      </button>
    </div>
  </div>
</template>

<style scoped>
/* ★ P5：inline margin 收敛为类 */
.mt8 { margin-top: 8px; }
.vac-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
/* ★★ 2026-10-11：反面/上板那一路单起一排（与下板独立），用虚线边框一眼区分"另一路"。 */
.vac-btns-back { margin-top: 6px; }
.vac-back { border-style: dashed; }
.vac-suck { color: var(--c-suck, #2f9e6f); border-color: var(--c-suck-line, #2f9e6f55); }
.vac-suck:hover:not(:disabled) { background: var(--c-suck, #2f9e6f); color: #fff; }
.vac-suck.is-on { background: var(--c-suck, #2f9e6f); color: #fff; border-color: var(--c-suck, #2f9e6f); }
.vac-release { color: var(--c-release, #d98a2b); border-color: var(--c-release-line, #d98a2b55); }
.vac-release:hover:not(:disabled) { background: var(--c-release, #d98a2b); color: #fff; }
.vac-blow { color: var(--c-blow, #3b7dd8); border-color: var(--c-blow-line, #3b7dd855); }
.vac-blow:hover:not(:disabled) { background: var(--c-blow, #3b7dd8); color: #fff; }
.vac-unblow { color: var(--c-blow, #3b7dd8); border-color: var(--c-blow-line, #3b7dd855); }
.vac-unblow:hover:not(:disabled) { background: var(--c-blow, #3b7dd8); color: #fff; }
.vac-blow.is-on { background: var(--c-blow, #3b7dd8); color: #fff; border-color: var(--c-blow, #3b7dd8); }
</style>
