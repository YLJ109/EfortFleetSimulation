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
      <span class="h3-sub">{{ canControl ? auth.ttlText : "未就绪" }}</span>
    </h3>

    <div class="row">
      <span class="k">状态</span>
      <span class="v" :style="{ color: granted ? 'var(--ok)' : 'var(--warn)' }">
        {{ granted ? "已获得控制权限"
           : (!robot.connected ? "机器人离线"
              : (!canControl ? "未获得控制权限" : "已授权 · 待一键就绪")) }}
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

    <!-- ★ 真空吸放：电平保持（写 40135.Bit1~Bit4 触发位，控制器 210 服务执行） -->
    <hr class="sp-hr" />
    <div class="btns vac-btns">
      <button class="vac-suck" :class="{ 'is-on': exec.vacuumState === 'suck' }"
              :disabled="!canControl || exec.vacuumBusy"
              @click="exec.vacuum('suck')">
        <Icon name="download" :size="14" /> 吸气
      </button>
      <button class="vac-release" :disabled="!canControl"
              @click="exec.vacuum('release')">
        <Icon name="upload" :size="14" /> 停止吸气
      </button>
      <button class="vac-blow" :class="{ 'is-on': exec.vacuumState === 'blow' }"
              :disabled="!canControl"
              @click="exec.vacuum('blow')">
        <Icon name="eject" :size="14" /> 放气
      </button>
      <button class="vac-unblow" :disabled="!canControl"
              @click="exec.vacuum('unblow')">
        <Icon name="close" :size="14" /> 停止放气
      </button>
    </div>

    <!-- 急停 / 复位急停 -->
    <div class="btns" style="margin-top:8px">
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
.vac-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
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
