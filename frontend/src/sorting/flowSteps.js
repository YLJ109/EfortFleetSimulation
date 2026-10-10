// =====================================================================
// 分拣执行流程的**步骤模型**（纯逻辑，无 Vue / 无 DOM）
//
//   ★ 抽成独立模块的唯一理由：**能拿真实 stage 序列直接跑回归**。
//     流程条的价值全在"和后端真实执行一致"，而这类"映射错一步"的 bug
//     编译不报、界面看着也正常 —— 只有在真机跑起来才发现"高亮不跳"。
//     所以映射必须是可 headless 驱动的纯函数（见 tools/verify_sortflow.mjs）。
//
//   ★ 两套 stage 并存（这是 2026-10-09 实测抓到的坑，别再踩）：
//     · **真机路径**：`_air()` 写的是**气路标签** —— "吸"/"停止吸"/"放"/"停止放"；
//     · **空跑路径**：写的是**语义名 + (空跑)** —— "取料·吸(空跑)"/"放料·放气(空跑)"；
//     · 运动类步骤（`_move()`）两条路径都写语义名 —— "取料·上方"/"送检"…
//     只映射一套 ⇒ 另一套跑起来流程条认不出、不跳步。
//     现场最常看的恰恰是真机那套（空跑只是排练），所以两套都要映射。
// =====================================================================

/** 循环体内的步骤（每一件都要走一遍）。label 是函数，便于把参数写进文案。 */
export const LOOP_STEPS = [
  { key: "a_up", label: (p) => `A${p.i} 上方`, stages: ["取料·上方"] },
  { key: "a_down", label: (p) => `A${p.i} 原位（落到位）`, stages: ["取料·下落"] },
  { key: "suck", label: "吸取（真空开）", stages: ["取料·吸"],
    note: (p) => `吸牢后停 ${p.suckHold}ms` },
  { key: "lift", label: (p) => `抬起回 A${p.i} 上方`, stages: ["抬起"] },
  { key: "to_cam", label: "移到 C 检测位", stages: ["送检"] },
  { key: "dwell", label: "等待检测", stages: ["停顿"], note: (p) => `${p.dwell}ms` },
  { key: "capture", label: "捕捉判色", stages: ["判色", "空跑·未判色"],
    note: (p) => `${p.frames} 张`, frames: true },
  { key: "b_up", label: "B（按颜色）上方", stages: ["放料·正上方"] },
  { key: "stop_suck", label: "停止吸气", stages: ["放料·停止吸"] },
  { key: "blow", label: "放气", stages: ["放料·放气"], note: (p) => `${p.blow}ms` },
  { key: "stop_blow", label: "停止放气", stages: ["放料·停止放气"] },
  { key: "back", label: "回到起点（开始/结束点）",
    // ★ 2026-10-10：判不出色时后端会走「无法识别·放回原位」（原路放回取料格后再回起点）
    //   —— 它同样属于"收尾/回起点"这一步，别让它成为未映射的孤儿 stage。
    stages: ["回起点", "无法识别·放回原位"] },
];

/** 循环外：开始那一步。 */
export const PRE_STEP = { key: "start", label: "开始 · 回开始/结束点",
                          stages: ["回开始/结束点"] };
/** 循环外：结束那一步（正常收尾 / 异常回原点都归这里）。 */
export const POST_STEP = { key: "end", label: "结束 · 收尾回开始/结束点",
                           stages: ["收尾·回开始/结束点", "异常·回开始/结束点",
                                    "异常回原点", "结束"] };

/** 全部步骤在界面上的先后顺序（用于判定"已走过 / 当前"）。 */
export const STEP_ORDER = [PRE_STEP.key, ...LOOP_STEPS.map((s) => s.key), POST_STEP.key];

/**
 * 后端 stage 字面量 → 步骤 key。
 * ★ 顺序无意义（精确命中优先），但**必须有全部真实取值**：
 *   缺一个 ⇒ 那一刻流程条掉出高亮。有回归脚本从后端源码解析真实取值来守着。
 */
export const STAGE_MAP = {
  "准备": "",
  "回开始/结束点": "start",
  // "取料" 是每件开头先落的通用名，紧接着被 "取料·上方" 覆盖 → 归到 a_up
  "取料": "a_up",
  "取料·上方": "a_up",
  "取料·下落": "a_down",
  "取料·吸": "suck",
  "抬起": "lift",
  "送检": "to_cam",
  "停顿": "dwell",
  "判色": "capture",
  "空跑·未判色": "capture",
  "放料·正上方": "b_up",
  "放料·停止吸": "stop_suck",
  "放料·放气": "blow",
  "放料·停止放气": "stop_blow",
  "回起点": "back",
  "收尾·回开始/结束点": "end",
  "收尾回原点": "end",
  "异常·回开始/结束点": "end",
  "异常回原点": "end",
  "结束": "end",
  // ---- 真机路径的气路标签（`_set(st, stage=label)`）----
  "吸": "suck",
  "停止吸": "stop_suck",
  "放": "blow",
  "停止放": "stop_blow",
};

/** 按长度降序的键（前缀兜底用：短标签不许抢在语义名前面命中）。 */
const KEYS_DESC = Object.keys(STAGE_MAP).filter(Boolean)
  .sort((a, b) => b.length - a.length);

/**
 * 当前 stage 落在哪一步。
 * @param {string} stage 后端 `/api/sorting/state` 的 `state.stage`
 * @returns {string} 步骤 key；"" = 还没开始 / 认不出来
 */
export function stepOf(stage) {
  const s = String(stage || "");
  if (!s) return "";
  if (s in STAGE_MAP) return STAGE_MAP[s];
  for (const k of KEYS_DESC) {
    if (s.startsWith(k)) return STAGE_MAP[k];
  }
  return "";
}
