// =====================================================================
// AI 执行页「对话式语音控制台」回归（headless，node 直接跑）
//
//   钉三类最容易"看着正常、实际很危险"的东西：
//   A. 确认词判定 —— 一句话就能动机器人，判松了会在闲聊里误触发：
//        · "执行/开始/执行吧"必须**能**触发；
//        · "执行分拣"（这是一条新指令）必须**不能**被当成确认；
//        · "好/嗯/可以"必须**不能**触发（用户口径之外的口头语）；
//   B. 判定顺序 —— 停止词优先于确认词（"停"永远该真的停）；
//   C. 可执行指令提示必须**真覆盖后端支持的意图**（从 nl_intent.py 解析出来断言），
//      否则界面提示里会写着"能做但实际做不到"的例子，现场白试一次。
//
// 运行： cd frontend && node tools/verify_aichat.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isConfirm, isStop, classifyUtterance, norm, CONFIRM_WORDS,
         STOP_WORDS, REJECTED_AS_CONFIRM } from "../src/ai/confirmWords.js";
import { stageSpeech, isMuted, setMuted, speak } from "../src/ai/speak.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

console.log("== A0. 「回到起点」是真实动作（不是闲聊、更不许谎报） ==");
{
  const aiApi = read("../backend/app/api/ai.py");
  const nl = read("../backend/app/services/nl_intent.py");
  const llmSrc = read("../backend/app/services/llm.py");
  const view = read("src/components/AiControlView.vue");
  ok("规则层直接命中「回到起点」→ goto_start（规则优先，不绕大模型）",
     /intent="goto_start"/.test(nl) && /"action": "goto_start"/.test(nl));
  ok("后端 _run 真的会走到开始/结束点（不是一张空计划）",
     /if act == "goto_start":/.test(aiApi)
     && /_move\(ctx, pmap, start, speed, dry, "回开始\/结束点", st\)/.test(aiApi));
  ok("动作白名单含 goto_start（表里没有的，大模型不许发明）",
     /ALLOWED_ACTIONS = \([^)]*goto_start/.test(llmSrc));
  ok("对话人格含「不许谎报动作」铁律（此前模型编造过'已回到起点'）",
     /没有任何执行权/.test(llmSrc) && /禁止\*\*说/.test(llmSrc));
  ok("快捷指令含「回到起点」", /回到起点/.test(view));
  // ---- 2026-10-10：不只是执行 —— 文字描述过程 + 可视化代码过程 ----
  const chatSrc = read("src/ai/aiChat.js");
  ok("对话支持进度气泡带上下文（updateRun 收第 4 个参数 ctx）",
     /function updateRun\(st, running, queued, ctx\)/.test(chatSrc));
  ok("计划的**代码式步骤**与当前步高亮（planSteps / stepIndex 已导出）",
     /export function planSteps/.test(chatSrc) && /export function stepIndex/.test(chatSrc));
  ok("进度气泡显示「你问 / 我理解」+ 步骤条",
     /你问：/.test(view) && /我理解：/.test(view) && /planSteps\(m\.ctx/.test(view));
  ok("执行时把上下文挂到气泡（doExec 设 runCtx）", /runCtx\.value = \{/.test(view));
}

console.log("== A. 确认词判定（严进：整句匹配，不是包含） ==");
{
  const yes = ["执行", "执行吧", "执行一下", "开始", "开始吧", "确认", "跑", "go",
               "GO", " go ", "执行。", "执行！", "执 行"];
  const bad = yes.filter((t) => !isConfirm(t));
  ok("这些**必须**能触发执行：" + yes.join(" / "), bad.length === 0, "未触发: " + JSON.stringify(bad));

  // ★ 关键反例：这是一条**新指令**，不是确认
  const no = ["执行分拣", "开始分拣", "执行搬运", "把A1放到B1", "A区第1行是什么颜色",
              "停止执行", "急停", "", "   "];
  const wrong = no.filter((t) => isConfirm(t));
  ok("这些**绝不能**被当成确认：" + no.slice(0, 5).join(" / ") + " …",
     wrong.length === 0, "误判: " + JSON.stringify(wrong));

  // ★ 口头语必须不触发（写在这里是为了让后来者知道这是有意排除）
  const casual = REJECTED_AS_CONFIRM.filter((t) => isConfirm(t));
  ok("闲聊口头语（" + REJECTED_AS_CONFIRM.join("/") + "）一律不触发",
     casual.length === 0, "误判: " + JSON.stringify(casual));

  ok("确认词表非空且不含口头语",
     CONFIRM_WORDS.length >= 3
     && !CONFIRM_WORDS.some((w) => REJECTED_AS_CONFIRM.includes(w)));
}

console.log("");
console.log("== B. 停止词与判定顺序（顺序即安全） ==");
{
  ok("停止词能识别", ["停", "停止", "停下来", "别动", "取消"].every(isStop));
  ok("“急停”不被当成普通停止词（它是一条独立的高优先级指令）", !isStop("急停"));

  ok("★ 顺序：停止优先于确认（\"停\"无论如何都该真停）",
     classifyUtterance("停止", { hasPendingPlan: true }) === "stop");
  ok("有待确认计划时，说\"执行\"→ confirm",
     classifyUtterance("执行", { hasPendingPlan: true }) === "confirm");
  ok("★ 没有待确认计划时说\"执行\"→ 单独一类（界面要提示\"先说要做什么\"，绝不能瞎跑）",
     classifyUtterance("执行", { hasPendingPlan: false }) === "confirm_no_plan");
  ok("普通句子 → command",
     classifyUtterance("把A区第1行第1列放到B区红列", { hasPendingPlan: true }) === "command");
  ok("norm() 能吃掉 ASR 常见的标点与空格",
     norm(" 执行 。 ") === "执行" && norm("Go！") === "go");
}

console.log("");
console.log("== C. 可执行指令提示必须覆盖后端真实能力 ==");
{
  // ★ 从后端源码解析意图白名单（手写会漏 —— 后端加了能力而界面没跟上，
  //   现场就会"照着提示说却解析失败"）
  const nl = read("../backend/app/services/nl_intent.py");
  const m = nl.match(/INTENTS\s*=\s*\(([^)]+)\)/);
  const intents = m ? [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]) : [];
  ok("解析到后端意图白名单（" + intents.join("/") + "）", intents.length >= 4);
  ok("意图里含 estop 与 sorting（安全与流程两条必须有）",
     intents.includes("estop") && intents.includes("sorting"));

  const view = read("src/components/AiControlView.vue");
  // 每类意图在界面提示里至少要有 1 条真正能用的例子
  const need = {
    move_one: /把A区第?\d+行第?\d+列放到/,
    // ★ 批量（move_all）的关键是"**批量**"语义，不是非得写到"回A区"——
    //   原来钉 `/回A区|整个.*A区/` 会把"把A区全部搬到B区"这种合法例子判成缺失（假警报）。
    move_all: /全部|所有|整个/,
    look_color: /是什么颜色/,
    sorting: /分拣|暂停|继续/,
    estop: /急停/,
  };
  const missing = Object.keys(need).filter((k) => !need[k].test(view));
  ok("五类能力在界面提示里都有真实例子", missing.length === 0,
     "缺: " + JSON.stringify(missing));
}

console.log("");
console.log("== D. 对话流消息模型（纯逻辑） ==");
{
  const src = read("src/ai/aiChat.js");
  ok("消息类型齐备（user/ai/plan/run/result/warn）",
     ["USER", "AI", "PLAN", "RUN", "RESULT", "WARN"].every((k) => src.includes(k + ":")));
  ok("★ 有节流常量（进度不准每帧刷屏）", /RUN_THROTTLE_MS\s*=\s*\d+/.test(src));
  ok("★ 进度是**原地更新**同一个气泡（不是每次追加）",
     src.includes("last.type === MSG.RUN"));
  ok("★ 计划卡之前的 run/result 会让 pendingPlan 失效（不会拿着旧计划执行）",
     /MSG\.RUN \|\| m\.type === MSG\.RESULT/.test(src));
  ok("对话**不落库**（源码里没有 localStorage / 后端持久化调用）",
     !/localStorage|sessionStorage/.test(src));
}

console.log("");
console.log("== E. 页面接线（对话流 + 空格键 + 说\"执行\"直执） ==");
{
  const view = read("src/components/AiControlView.vue");
  ok("页面引入了对话流与确认词模块",
     /from "\.\.\/ai\/aiChat\.js"/.test(view) && /from "\.\.\/ai\/confirmWords\.js"/.test(view));
  ok("★ 空格键开关语音（按一次说、再按一次停）",
     /e\.code === "Space"|e\.key === " "/.test(view) && /preventDefault/.test(view));
  ok("★ 空格键在输入框内不拦截（否则打不了空格）",
     /tagName|isEditable|INPUT|TEXTAREA/.test(view));
  ok("★ 听到确认词 → 直接执行（不必点按钮）",
     /classifyUtterance/.test(view) && /=== "confirm"/.test(view));
  // takeover 在 **store** 里（页面只管交互，网络参数归 store —— 断言要打在真正的位置上）
  ok("★ 执行中下新指令 → 带 takeover 调 exec（后端排队接替）",
     /takeover: true/.test(read("src/stores/ai.js")));
  ok("界面明确写了\"当前动作跑完才停\"（不能让操作员以为一点就停）",
     /跑完|步边界|动作结束/.test(view));
  ok("后端 ExecIn 有 takeover 字段",
     /takeover: bool = Field\(default=True/.test(read("../backend/app/api/ai.py")));
}

console.log("");
console.log("== F. 语音播报（用户口径：动作/报错/颜色/去哪儿/已到达都要播） ==");
{
  const view = read("src/components/AiControlView.vue");   // E 段里的 view 在那个块里
  // stage → 口语短句（**播的是操作员要听的，不是日志原文**）
  ok("取料 → 「去 A1 取料」",
     stageSpeech({ stage: "取料·上方", src: "A1" }) === "去 A1 取料");
  ok("判色 → 「颜色是红」", stageSpeech({ stage: "判色", color: "红" }) === "颜色是红");
  ok("放料 → 「送到 B1 放料」",
     stageSpeech({ stage: "放料·正上方", target: "B1" }) === "送到 B1 放料");
  ok("回起点 → 「回到起点」", stageSpeech({ stage: "回起点" }) === "回到起点");
  ok("完成 → 「完成」", stageSpeech({ stage: "结束", ok: true }) === "完成");
  ok("★ 出错 → 「出错了：原因」（截 40 字）",
     stageSpeech({ stage: "结束", error: "检测不到颜色（真图回放显示 A 区没料了）" })
       .startsWith("出错了："));
  // ★ 未知 stage **原样播**：总比没声音好（宁可念得机械，也不能漏）
  ok("认不出的 stage 原样播（不静默）", stageSpeech({ stage: "某个新阶段" }) === "某个新阶段");
  // 接线：AI 页与分拣页都要触发（漏一处 = 那一页哑巴）
  const aiStore = read("src/stores/ai.js");
  const sortView = read("src/components/SortingView.vue");
  ok("★ AI 执行页接线了播报", /speak\(stageSpeech\(s\)\)/.test(aiStore));
  ok("★ 分拣页接线了播报", /speak\(stageSpeech\(s\)\)/.test(sortView));
  ok("页面上有播报开关（现场嫌吵要能静音）",
     /toggleSpeak/.test(view) && /isMuted|setMuted/.test(view));
  // 静音状态机
  const m0 = isMuted();
  setMuted(true);
  ok("静音开关生效", isMuted() === true);
  setMuted(m0);
  ok("恢复原状态", isMuted() === m0);
  // node 环境没有 speechSynthesis → 安全降级为 no-op（绝不抛异常）
  ok("★ node/不支持 TTS 的浏览器安全降级（不抛异常）", (() => {
    try { speak("测试播报"); return true; } catch (e) { return false; }
  })());
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
