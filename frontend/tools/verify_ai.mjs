// =====================================================================
// AI 执行页回归（headless，源码扫描）
//
// 钉死几条"改坏了会出真机事故、但界面看着还正常"的契约：
//   A. 两段式：解析（/ai/parse）与执行（/ai/exec）分开，且执行必须带同一个 plan_id
//      —— 防"界面显示的计划"与"真正执行的计划"不是同一份。
//   B. 权限：本页全部请求都走 apiControl（带控制令牌）—— AI 不是权限后门。
//   C. 默认空跑：首次进入是 dry_run=true（先验证解析与路径，再真机）。
//   D. 语音误识兜底：同时保留"识别原话"与"可修改的输入"，且提示要核对。
//   E. 大模型：没有 key 也能用（页面明确显示"未配置（规则解析）"）；后端 sanitize 白名单存在。
//   F. 页签/路由/说明三者一致（tabs.js / App.vue / guide.js）
//
// 运行： node tools/verify_ai.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(root, p), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

console.log("== AI 执行页回归 ==");

const store = read("src/stores/ai.js");
const view = read("src/components/AiControlView.vue");
const app = read("src/App.vue");
const tabs = read("src/tabs.js");
const guide = read("src/guide.js");
const api = read("../backend/app/api/ai.py");
const llmMod = read("../backend/app/services/llm.py");
const mainPy = read("../backend/app/main.py");

// ---------- A. 两段式 + plan_id 绑定 ----------
ok("store 调 /ai/parse（只解析）", /apiControl\("\/ai\/parse"/.test(store));
ok("store 调 /ai/exec（执行）", /apiControl\("\/ai\/exec"/.test(store));
ok("执行时带上解析返回的 plan_id（不是自己重造一份计划）",
  /plan_id:\s*p\.plan_id/.test(store));
// ★ 2026-10-09：界面改成**对话流**后文案变了，但**意图必须不变** ——
//   "解析 ≠ 执行"是本页的安全设计（语音会把「第1行」听成「第7行」，必须让人先核对）。
//   所以这里钉的是"两个动作都还在"，不是钉某句具体文案。
ok("界面区分「解析」与「执行」两个动作",
  view.includes("解析") && /执行/.test(view) && /ai\.exec\(/.test(view));
ok("后端 /exec 校验 plan_id 并查缓存（过期/不存在 → 400）",
  /计划已过期或不存在/.test(api) && api.includes("_plans.get(body.plan_id)"));
ok("后端 /parse 落地 plan_id（uuid）", api.includes("uuid.uuid4().hex"));

// ---------- B. 权限：不是后门 ----------
const ctrlCalls = [...store.matchAll(/apiControl\(/g)].length;
const rawFetch = [...store.matchAll(/fetch\(/g)].length;
ok(`store 全部请求走 apiControl（带令牌）：${ctrlCalls} 处，裸 fetch ${rawFetch} 处`,
  ctrlCalls >= 5 && rawFetch === 0);
ok("页面对无令牌给出明确说明（不是静默失败）",
  store.includes("需要控制权限") && store.includes("不是权限后门"));

// ---------- C. 默认空跑 ----------
// ★★ 2026-10-09 用户口径："空跑按钮的默认选勾移除掉" → 默认**真实下发**。
//   （原来钉的是 `dryRun: true` —— 口径变了，断言跟着变。
//    「空跑」开关仍在页面上，需要验证路径时勾上。）
ok("默认 dryRun = false（用户口径：默认真实下发，空跑开关保留可勾）",
  /dryRun:\s*false/.test(store));
ok("空跑开关有说明（不下发运动、不判色）",
  view.includes("不下发运动、不判色") || view.includes("不下发运动"));
ok("后端空跑不判色（与分拣同口径：机器人没动就不判色）",
  /judge = \(not dry\) and real_write_enabled\(\)/.test(api));

// ---------- D. 语音误识兜底 ----------
ok("单独保留语音识别原话（asrText）", /asrText/.test(store));
ok("语音结果落到输入框（可人工改）", /setAsrText\(/.test(store));
// ★★ 2026-10-09：界面改成**对话流**后，"语音原话"不再单独挂一行（用户要求精简）。
//   但"能对照"这个**意图必须保留** —— 现在的对照链是：
//     用户气泡（识别出的原话）→ 紧跟计划卡（AI 理解的结果）→ 人核对无误说「执行」
//   所以这里断言的是**这条链**的结构存在，而不是某句具体文案。
//   ★ 2026-10-10 对话化：指令解析并入 /ai/chat（doSend），计划卡数据来自
//     返回体的 parsed 字段 —— 对照链结构不变，sayPlan 的实参变为 d.parsed。
ok("语音/文字输入都有「原话 → 计划」的对照链（气泡原话 + 计划卡）",
  /chat\.sayUser\(t\)/.test(view) && /chat\.sayPlan\(d\.parsed\)/.test(view));
ok("解析失败时原样透传后端原因（界面只在后端没给原因时才用兜底文案）",
  /this\.err = d\.error \|\|/.test(store));
ok("后端 /parse 对空指令也给出明确原因（不是 200 + 空计划）",
  /没有收到指令/.test(read("../backend/app/services/nl_intent.py")));
ok("后端注明不猜（提示语里写死了 绝不猜 / 没听懂）",
  /绝不猜|没听懂/.test(api));

// ---------- E. 大模型（可选增强，不挡路） ----------
ok("后端 sanitize 白名单存在（动作/流程/格号三道）",
  /ALLOWED_ACTIONS/.test(llmMod) && /ALLOWED_SORTING/.test(llmMod)
  && /def _loc/.test(llmMod));
ok("没有 key 时 enabled()=False（纯规则可用，不报错）",
  /def enabled\(\)[\s\S]{0,220}return bool\(_env\("EFORT_LLM_API_KEY"\)\)/.test(llmMod));
ok("页面对未配置大模型给出明确状态", /llmOn \? "已启用" : "未配置（规则解析）"/.test(view));
ok("大模型只在规则解析失败后才调用（规则优先）",
  /if not res.get\("ok"\) and llm\.enabled\(\):/.test(api));
ok("key 不回传前端（config 只说开没开）",
  /llm_enabled/.test(api) && !/api_key|apiKey|EFORT_LLM_API_KEY/.test(
    api.slice(api.indexOf("def api_config"), api.indexOf("def api_config") + 400)));

// ---------- F. 页签 / 路由 / 说明 三者一致 ----------
ok("tabs.js 有 ai 页签且需要令牌", /\{\s*key:\s*"ai"[^}]*needAuth:\s*true/.test(tabs));
ok("App.vue 注册了 AiControlView 且按 activeView 显示",
  app.includes("AiControlView") && /activeView === 'ai'/.test(app));
ok("guide.js 有 ai 模块说明（否则关于页与顶栏对不上）", /key:\s*"ai"/.test(guide));
ok("main.py 挂载了 ai 路由", /include_router\(ai\.router\)/.test(mainPy));

// ---------- G. 批量/危险动作的额外闸门 ----------
ok("批量/看颜色必须显式确认（后端强制，不只靠前端）",
  /if not body\.confirm and act != "move_one"/.test(api));
ok("批量件数上限存在且可配（默认 3）", /EFORT_AI_BATCH_MAX/.test(api));
ok("批量放到同一个具体格会被拒绝（会叠件）",
  /会把件叠在一起，已拒绝/.test(api));
ok("急停复用既有 /control/estop 通道（未送达会 503，不谎报）",
  /from app\.api\.control import api_estop/.test(api));
ok("执行中再来一条 AI 指令会被拒绝（一次只跑一件事）",
  /AI 执行正在运行/.test(api));
ok("状态里带 src/dst（不是关键字 from —— 那会直接把模块编译不过）",
  /"src": ""/.test(api) && !/from=src/.test(api));

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
