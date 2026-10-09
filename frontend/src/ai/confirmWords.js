// =====================================================================
// 语音/文字「确认词」与「停止词」的判定（纯逻辑，可 headless 测）
//
// ★ 为什么单独抽出来：这是**唯一一处"一句话就能让机器人动起来"**的判定，
//   写松了会在闲聊里误触发（现场讲句话顺口说个"好"就动机器，不可接受），
//   写紧了用户喊"执行"没反应（那就不"丝滑"了）。所以：
//     · 判定规则必须**严进**：整句匹配，不是"包含"；
//     · 必须有回归脚本把它钉死（tools/verify_aichat.mjs）。
//
// 用户口径："通过语音确认直接说执行，ai 立刻执行不用等待用户点击按钮"
// =====================================================================

/** 确认词：说了就执行「当前待确认的那条计划」。 */
export const CONFIRM_WORDS = ["执行", "开始", "确认", "跑", "go"];

/**
 * ★★ 刻意**不收**的"确认词"（写在这里是为了让后来者知道这是有意为之，不是漏了）：
 *   好 / 嗯 / 可以 / 行 / ok / 对
 *   理由：这些在中文闲聊里出现频率极高（"好了""嗯""可以吧"），
 *   现场操作员对讲机里说一句就会被当成"确认执行" → 机器突然动作 = 事故。
 *   要执行就明确说「执行」。
 */
export const REJECTED_AS_CONFIRM = ["好", "嗯", "可以", "行", "ok", "对"];

/** 停止词：立刻请求停止（走既有 cancel 通道：步边界生效 → 回起点）。 */
export const STOP_WORDS = ["停", "停止", "别动", "中断", "取消"];

/**
 * 归一化：ASR 常带标点与空格（"执行。" / "执 行" / "Go!"），
 * 全角标点也要处理 —— 不归一化会导致"说了执行却没反应"。
 */
export function norm(s) {
  return String(s || "")
    .trim()
    .toLowerCase()
    .replace(/[\s\u3000]+/g, "")          // 半角/全角空白
    .replace(/[，。、！？：；,.;!?:'"“”‘’（）()[\]【】]/g, "");
}

/** 允许跟在确认词/停止词后面的"语气尾巴"（只有这些才算确认，别的都当新指令）。 */
// ⚠ 交替顺序有意义：长尾巴要排在短的**前面**（`一下` 在 `下` 前、`下来` 在 `下` 前），
//   否则正则会先吃掉短的那个、"停下来"就匹配不上（第一版就踩了）。
const TAIL = "(?:吧|啊|呀|呢|了|一下|下来|下)?";

const CONFIRM_RE = new RegExp("^(?:" + CONFIRM_WORDS.join("|") + ")" + TAIL + "$");
const STOP_RE = new RegExp("^(?:" + STOP_WORDS.join("|") + ")" + TAIL + "$");

/**
 * 是不是「执行确认」。
 *
 * ★ 必须**整句匹配**：`执行` / `执行吧` / `执行一下` / `go` → true；
 *   `执行分拣`（这是一条新指令）→ **false**；`好` → false（见上面刻意排除的理由）。
 * @param {string} text
 * @returns {boolean}
 */
export function isConfirm(text) {
  return CONFIRM_RE.test(norm(text));
}

/**
 * 是不是「停止」。
 * @param {string} text
 * @returns {boolean}
 */
export function isStop(text) {
  return STOP_RE.test(norm(text));
}

/**
 * 一句话该怎么处理 —— 判定顺序**本身就是安全设计**：
 *   ① 停止词最优先（任何情况下"停"都该真的停）
 *   ② 确认词次之（只在有待确认计划时才真的执行）
 *   ③ 其余一律当作新指令去解析（绝不猜）
 * @param {string} text
 * @param {{hasPendingPlan: boolean}} ctx
 * @returns {"stop"|"confirm"|"command"|"confirm_no_plan"}
 */
export function classifyUtterance(text, ctx = {}) {
  if (isStop(text)) return "stop";
  if (isConfirm(text)) return ctx.hasPendingPlan ? "confirm" : "confirm_no_plan";
  return "command";
}
