<script setup>
// =====================================================================
// 2D 执行图（手柄执行页 · 浮在 3D 视口左下角）
//
// 布局严格按用户口径：「左边 B 区 / 中间下面 C 检测 / 上面起点 / 右边 A 区」。
//
// ★ 只做"**选点 + 显示**"，不动机器人 —— 真正的运动由手柄按键 A/Y 触发（见 stores/pad.js）。
//   鼠标点格子 = **仅把光标挪过去**（与方向键等价），这样没接手柄时也能先看懂选中态；
//   它**不会**下发任何运动。
//
// ★ "按一下往哪走"的判据全在 pad/nav.js（纯函数，headless 钉死），本组件只画格子。
// =====================================================================
import { usePadStore } from "../stores/pad.js";
import { CAMERA, START, ZONE_A, ZONE_B, shortLabel } from "../pad/nav.js";

const pad = usePadStore();

/** 鼠标点格 = 仅改选中（不动机器人）。 */
function pick(name) { pad.select(name); }

/** 该格是否显示「有正上位」小标（只有 A/B 槽位有）。 */
const isCur = (name) => pad.cursor === name;
</script>

<template>
  <div class="pad-map">
    <div class="pm-head">
      <span class="pm-title">执行图</span>
      <span class="pm-hint">↑↓←→ 选点</span>
    </div>

    <!-- 上：起点 -->
    <div class="pm-toprow">
      <button class="pm-pt start" :class="{ cur: isCur(START) }" @click="pick(START)">
        {{ shortLabel(START) }}
      </button>
    </div>

    <!-- 左 B 区 | 中下 C 检测 | 右 A 区 -->
    <div class="pm-mid">
      <div class="pm-zone">
        <div class="pm-zt">B 区</div>
        <div class="pm-grid">
          <template v-for="(row, ri) in ZONE_B" :key="'b' + ri">
            <button v-for="name in row" :key="name" class="pm-pt"
                    :class="{ cur: isCur(name) }" @click="pick(name)">{{ shortLabel(name) }}</button>
          </template>
        </div>
      </div>

      <div class="pm-zone c">
        <div class="pm-zt">C 检测</div>
        <button class="pm-pt cam" :class="{ cur: isCur(CAMERA) }" @click="pick(CAMERA)"
                :title="CAMERA">C</button>
      </div>

      <div class="pm-zone">
        <div class="pm-zt">A 区</div>
        <div class="pm-grid">
          <template v-for="(row, ri) in ZONE_A" :key="'a' + ri">
            <button v-for="name in row" :key="name" class="pm-pt"
                    :class="{ cur: isCur(name) }" @click="pick(name)">{{ shortLabel(name) }}</button>
          </template>
        </div>
      </div>
    </div>

    <div class="pm-foot">
      <template v-if="pad.hasAbove">A 原位 · Y 正上位</template>
      <template v-else>该点只有原位（A），没有正上位</template>
    </div>
  </div>
</template>

<style scoped>
/* 浮在 3D 视口**左下角**（左上=相机面板/执行 HUD、右上=井字棋棋盘、右下=安全围栏状态片） */
.pad-map { position: absolute; left: 12px; bottom: 12px; z-index: 12;
  width: 268px; padding: 8px;
  background: var(--glass); border: 1px solid var(--glass-line); border-radius: 10px;
  box-shadow: var(--shadow-hud);
  backdrop-filter: blur(14px) saturate(140%);
  -webkit-backdrop-filter: blur(14px) saturate(140%);
  font-family: var(--font); color: var(--txt); }
.pm-head { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.pm-title { color: var(--accent); font-weight: 600; }
.pm-hint { margin-left: auto; font-size: 11px; color: var(--muted); }

.pm-toprow { display: flex; justify-content: center; margin-top: 7px; }
.pm-mid { display: grid; grid-template-columns: 1fr auto 1fr; gap: 8px; margin-top: 6px;
  align-items: start; }
.pm-zone { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.pm-zone.c { justify-content: center; }
.pm-zt { font-size: 10.5px; color: var(--muted); text-align: center; }
.pm-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }

.pm-pt { min-width: 0; padding: 4px 2px; font-size: 11px; font-family: var(--mono);
  border: 1px solid var(--line); border-radius: 6px; background: rgba(255, 255, 255, .03);
  color: var(--txt); cursor: pointer; transition: all .12s ease; }
.pm-pt:hover { border-color: var(--accent); background: var(--accent-soft); }
.pm-pt.cur { border-color: var(--accent); background: var(--accent-soft2); color: var(--accent);
  font-weight: 700; box-shadow: 0 0 10px var(--accent-soft2); }
.pm-pt.start { padding: 4px 12px; }
.pm-pt.cam { padding: 4px 8px; }

.pm-foot { margin-top: 7px; font-size: 10.5px; color: var(--muted); text-align: center; }
</style>
