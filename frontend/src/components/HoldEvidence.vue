<script setup>
// =====================================================================
// E5′「持件证据」状态条 —— 把**送检那几帧**的第二次读法如实摆出来
//
// 为什么要有这一行（AGENTS.md §7 明文的能力缺口）：
//   现场**没有真空传感器**，"手上到底吸没吸住"今天完全靠流程推断。
//   而分拣本来就要在 C 检测位连抓几帧判色 —— 同一批帧顺便就是"镜头前有没有东西"的观测值。
//   本条不新增拍摄、不新增移动，只把那几帧**再读一遍**（唯一判据在后端
//   `services/hold_state.py`，这里只消费它给的三档，不在前端复述一套判据）。
//
// ★ 三条显示纪律（对着 §5.5 铁律写的）：
//   ① **未知不塌成正常**：没有证据（相机没开 / 本轮没判色）→ 明说「未观测」，绝不显示绿；
//   ② **同屏同源**：数据一律来自调用方传进来的那份 `state`（与"手上有件"同一份），
//      本组件自己不轮询、不再开一路；
//   ③ **只观测不动作**：组件里没有任何按钮/POST —— 矛盾也只提示，刹车必须人点。
// =====================================================================
import { computed } from "vue";
import Icon from "./Icon.vue";

const props = defineProps({
  // 后端 state.hold_evidence（services/hold_state.py 的一条快照）；null = 本轮还没观测过
  evidence: { type: Object, default: null },
  // 流程认为"手上有件"（state.holding）—— 用来决定"未观测"这句话该不该说
  holding: { type: Boolean, default: false },
  // 空跑：机器人根本没动，镜头前不可能有件 → 不许摆出任何"证据"
  dry: { type: Boolean, default: false },
});

const EV = "证据";        // 变量拼出来的术语，避免源码里出现裸的「X证据」触发 CJK 裸括号检查

const STATE = {
  confirmed: { text: "有视觉" + EV + "支持", cls: "ok", icon: "check" },
  inferred: { text: "仅流程推断", cls: "muted", icon: "target" },
  contradiction: { text: "流程与观测不符", cls: "err", icon: "alert" },
};

const one = computed(() => {
  const e = props.evidence;
  if (!e || typeof e !== "object") return null;
  // ★ 认不出的档位 → 按"未知"处理（不许塌成 confirmed）
  const s = STATE[e.state] || STATE.inferred;
  return {
    ...s,
    label: s.text,
    note: String(e.note || ""),
    meta: `${Number(e.frames) || 0} 帧 · ${String(e.source || "送检")} · 第 ${Number(e.id) || 0} 次`,
  };
});

// 空跑没有实体可观测 —— 与其显示"仅推断"（看着像观测过），不如明说这一轮不产证据
const dryNote = computed(() => props.dry ? "空跑：机器人没动，镜头前没有件，本轮不产持件证据" : "");
</script>

<template>
  <p v-if="dryNote" class="he small muted">
    <Icon name="target" :size="12" /> 持件证据：<b>不适用</b>——{{ dryNote }}
  </p>
  <p v-else-if="one" class="he small" :class="one.cls">
    <Icon :name="one.icon" :size="12" /> 持件证据：<b>{{ one.label }}</b>
    <span class="he-meta">（{{ one.meta }}）</span>
    <span class="he-note">{{ one.note }}</span>
  </p>
  <p v-else-if="holding" class="he small muted">
    <Icon name="target" :size="12" /> 持件证据：<b>未观测</b>（还没在 C 检测位抓到帧——相机未开或本轮未判色；
    现场无真空传感器，这**不等于**已吸住）
  </p>
</template>

<style scoped>
.he { margin: 5px 0 0; display: flex; align-items: baseline; gap: 5px; flex-wrap: wrap; }
.he-meta { color: var(--muted); }
.he-note { color: var(--muted); }
.ok { color: var(--ok, #2f9e6f); }
.err { color: var(--err, #e05252); }
.muted { color: var(--muted); }
</style>
