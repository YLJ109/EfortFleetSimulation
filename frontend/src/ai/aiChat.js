// =====================================================================
// AI 执行页的「对话流」状态（纯逻辑，可 headless 测）
//
// 设计要点（都来自用户口径）：
//   ① 对话式：用户说 → AI 回 → 计划卡 → 说"执行"就跑；不落库、刷新即清
//      （★ 不落库是**故意的**：现场刷新后还留着旧消息，会让人以为还在跑）
//   ② 进度气泡**节流 + 原地更新**：否则每一帧都追加一条会把页面刷爆、也不"丝滑"
//   ③ 消息类型固定几种，UI 只按类型渲染（不在模板里做判断逻辑）
// =====================================================================
import { ref, computed } from "vue";

/** 消息类型 → 渲染方式（UI 只认这几种）。 */
export const MSG = {
  USER: "user",       // 用户说的/打的
  AI: "ai",           // AI 的文本回复
  CHAT: "chat",       // ★ 大模型自由对话（2026-10-10）：渲染与 AI 同侧（左），语义分开便于回归
  PLAN: "plan",       // 计划卡（含"或直接说：执行"提示）
  RUN: "run",         // 执行进度（同一气泡原地更新）
  RESULT: "result",   // 成功/失败
  WARN: "warn",       // 提示（如"当前动作跑完才停"）
};

/** 进度气泡的最小更新间隔（ms）—— 既看得见变化，又不刷屏。 */
export const RUN_THROTTLE_MS = 500;

/**
 * 对话**最多保留多少条**（用户口径 2026-10-09："只存储前 30 个指令就行了，可以一键清除"）。
 * ★ 为什么要有上限：这是一页"操作记录"，不是档案 —— 越攒越长之后
 *   ① 页面会卡（每条都要渲染）② 翻找当前上下文更费劲。
 *   超限时**丢最旧的**（保留最近 30 条）。
 */
export const MAX_MESSAGES = 30;

let _seq = 0;
const nextId = () => "m" + (++_seq);

export function createAiChat() {
  /** @type {import('vue').Ref<Array<object>>} */
  const messages = ref([]);
  let lastRunAt = 0;
  let lastRunKey = "";

  function push(type, text, extra = {}) {
    const m = { id: nextId(), type, text: String(text ?? ""), at: Date.now(), ...extra };
    let next = messages.value.concat([m]);            // 新数组 → 触发响应式
    if (next.length > MAX_MESSAGES) {
      next = next.slice(next.length - MAX_MESSAGES);  // ★ 只留最近 30 条
    }
    messages.value = next;
    return m;
  }

  const sayUser = (t) => push(MSG.USER, t);
  const sayAI = (t, extra) => push(MSG.AI, t, extra);
  const sayChat = (t) => push(MSG.CHAT, t);
  const sayWarn = (t) => push(MSG.WARN, t);
  const sayResult = (t, ok) => push(MSG.RESULT, t, { ok: !!ok });

  /** 计划卡：plan 是后端 /ai/parse 的结果（含 echo 中文摘要）。 */
  function sayPlan(parsed) {
    return push(MSG.PLAN, "", { parsed });
  }

  /**
   * 执行进度：**同一个气泡原地更新 + 节流**。
   * @param {object} st  /api/ai/state 的 state
   * @param {boolean} running
   * @param {object|null} queued 排队中的接管计划
   * @returns {boolean} 是否真的更新了（供调用方决定要不要滚到底）
   */
  function updateRun(st, running, queued) {
    const key = [running ? 1 : 0, (st && st.stage) || "", (st && st.piece) || "",
                 (st && st.color) || "", (st && st.target) || "",
                 (st && st.error) || "", queued ? (queued.raw || "1") : ""].join("|");
    const now = Date.now();
    // 状态没变 → 不写；变化了但没到节流窗口 → 也不写（除非是"结束"这种关键节点）
    const important = !running || (st && st.finished);
    if (key === lastRunKey) return false;
    if (!important && now - lastRunAt < RUN_THROTTLE_MS) return false;
    lastRunKey = key;
    lastRunAt = now;

    const last = messages.value[messages.value.length - 1];
    if (last && last.type === MSG.RUN) {
      // 原地更新（换对象引用以触发更新）
      messages.value = messages.value.slice(0, -1)
        .concat([{ ...last, st: { ...(st || {}) }, running, queued: queued || null,
                   at: Date.now() }]);
    } else {
      push(MSG.RUN, "", { st: { ...(st || {}) }, running, queued: queued || null });
    }
    return true;
  }

  function clear() {
    messages.value = [];
    lastRunKey = "";
    lastRunAt = 0;
  }

  /** 最近一条「计划卡」（= 待确认的那条）。确认词是否有效就看它。 */
  const pendingPlan = computed(() => {
    for (let i = messages.value.length - 1; i >= 0; i--) {
      const m = messages.value[i];
      if (m.type === MSG.PLAN) return m;
      // 遇到"已开跑/已作废"就更早的计划都不算待确认了
      if (m.type === MSG.RUN || m.type === MSG.RESULT) return null;
    }
    return null;
  });

  const hasPendingPlan = computed(() => !!pendingPlan.value);

  return { messages, push, sayUser, sayAI, sayChat, sayWarn, sayResult, sayPlan,
           updateRun, clear, pendingPlan, hasPendingPlan };
}
