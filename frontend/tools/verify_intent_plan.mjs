// =====================================================================
// ★★ E1「意图编译器 = 执行前的可行性预检卡」守卫（headless 静态检查）
//
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md
//       §E1 + §10.2 **收缩版**（逐字）：
//   「只做「意图 → 段列表 + 每段闸门结论」的展示卡（先不接执行，仍走现有入口）」
//
// 这张卡最容易被自己做成**假功能 / 假报警**，所以钉的是四条红线：
//   ① 只读：后端编译链**不 import** 写入路径，前端卡片**不叫执行**；
//   ② 段序/面**不另立权威**：只从 `raw_slot_safe_path` / `effective_face` 现算；
//   ③ **不产 ETA**（PC 只写目标 + 触发位，运动由控制器程序 210 跑）；
//   ④ **三态不塌成布尔**（§5.5 铁律③）：闸门没校验过 ≠ 校验通过，不给绿章；
//      预检请求失败 ≠ "没有隐患"。
//
// 运行： cd frontend && node tools/verify_intent_plan.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const beRoot = join(root, "..", "backend");
const read = (abs) => readFileSync(abs, "utf8")
  .split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));
const fe = (rel) => read(join(root, rel));
const be = (rel) => read(join(beRoot, rel));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

/** 剥块注释 / 行注释 / HTML 注释：红线判定只看**真代码**，不能被文档字符串吓住。 */
function stripJs(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}
function stripPy(s) {
  return s.split(String.fromCharCode(10))
    .filter((l) => !/^\s*#/.test(l))
    .join(String.fromCharCode(10));
}

const AST = be("app/services/intent_ast.py");
const PLAN = be("app/services/intent_plan.py");
const AIAPI = stripPy(be("app/api/ai.py"));
const STORE = stripJs(fe("src/stores/ai.js"));
const VIEW = fe("src/components/AiControlView.vue");

// 只截「这个端点 / 这个函数 / 这张卡」那一段来判，免得被邻居代码误伤或误放行
const planFn = (AIAPI.split('@router.post("/plan")')[1] || "").split("@router.")[0];
const parseFn = (AIAPI.split('@router.post("/parse")')[1] || "").split("@router.")[0];
const execFn = (AIAPI.split('@router.post("/exec")')[1] || "").split("@router.")[0];
const compileFn = PLAN.split("def compile_plan(")[1] || "";
const pcCardBlock = (() => {
  const i = VIEW.indexOf('<div v-if="cardOf(m)" class="pcard"');
  const j = VIEW.indexOf("<!-- ★ 运行过程", i);
  return i >= 0 ? VIEW.slice(i, j > i ? j : i + 4000) : "";
})();
const compileAction = (STORE.split("async compilePlan(")[1] || "").split("\n    },")[0];

// ---------------------------------------------------------------- 1. 只读：编译链不碰写入路径
ok("A1 intent_ast 不 import motion/modbus/db",
   !/from app\.services import (motion|modbus)/.test(AST) && !/import pyodbc|from app\.db/.test(AST));
ok("A2 intent_plan 不 import motion/modbus/db",
   !/from app\.services import[^\n]*(motion|modbus)/.test(PLAN)
   && !/from app\.services\.motion/.test(PLAN) && !/from app\.db/.test(PLAN));
ok("A3 intent_plan 不出现任何下发/闸门调用",
   !/_move\(/.test(PLAN) && !/motion\.command/.test(PLAN)
   && !/reject_if_not_ready\(/.test(PLAN) && !/write_registers/.test(PLAN));
ok("A4 POST /ai/plan 端点体内不调执行通道 / 未就绪闸门 / 不下发",
   planFn.length > 200 && !/_move\(/.test(planFn) && !/motion\.command/.test(planFn)
   && !/reject_if_not_ready\(/.test(planFn) && !/launch\(/.test(planFn)
   && !/run_sorting|_run_ai/.test(planFn));
ok("A5 /ai/plan 要令牌（与其它控制入口同一套）",
   /def api_plan\([\s\S]{0,160}Depends\(require_control\)/.test(AIAPI));
ok("A6 前端 compilePlan 只打 /ai/plan，不打 /ai/exec",
   /apiControl\("\/ai\/plan"/.test(compileAction) && !/\/ai\/exec/.test(compileAction)
   && !/\/ai\/parse/.test(compileAction));
ok("A7 预检卡里没有执行按钮（执行仍是原来那颗）",
   pcCardBlock.length > 200 && !/<button/.test(pcCardBlock) && !/doExec/.test(pcCardBlock));
ok("A8 预检卡自己不打任何接口（数据全在 store 里取）",
   pcCardBlock.length > 200 && !/apiControl/.test(pcCardBlock) && !/fetch\(/.test(pcCardBlock));

// ---------------------------------------------------------------- 2. 权威只有一个
ok("B1 段序只来自 raw_slot_safe_path（本卡不自己排路径）",
   /S\.raw_slot_safe_path\(cur, poses, name\)/.test(compileFn)
   && !/path = \[[^\]]*,\s*[^\]]*,/.test(compileFn));   // 补段只能是单点回退，不硬编多段
ok("B2 面来自 effective_face（且默认 FACE_AUTO=不翻面）",
   /S\.effective_face\(p, S\.FACE_AUTO\)/.test(PLAN));
ok("B3 目标关节来自唯一解算入口 resolve_target_joints",
   /S\.resolve_target_joints\(/.test(PLAN));
ok("B4 间隙来自 safety_eval.sweep（保守包络，不是控制器轨迹）",
   /SE\.sweep\(/.test(PLAN) && /NOTE_SWEEP/.test(PLAN));
ok("B5 不重排工艺时序（卡片自己就这么声明）",
   /不重排工艺时序/.test(PLAN) && /NOTE_ORDER/.test(PLAN));
ok("B6 AST 字段白名单 + 禁字段清单都在",
   /ALLOWED_KEYS/.test(AST) && /FORBIDDEN_KEYS/.test(AST));
ok("B7 禁字段含速度/气路位号/面/旋转/程序号",
   /"speed"/.test(AST) && /"vac_bit"/.test(AST) && /"face"/.test(AST)
   && /"allow_rotate"/.test(AST) && /"program"/.test(AST));
ok("B8 缺失字段不回填默认值（只登记 missing）",
   /missing/.test(AST) && !/plan\.get\("speed"/.test(AST));
ok("B9 三权同源：速度/气路/面各自的唯一来源仍在他处",
   /VAC_ACTIONS/.test(AST) && /ui_speed/.test(AST) && /sorting/.test(AST));

// ---------------------------------------------------------------- 3. 不产 ETA / 不产假字段
ok("C1 卡片输出没有 eta / duration / seconds 字段",
   !/["'](eta|eta_s|duration|seconds|elapsed)["']\s*:/.test(PLAN));
ok("C2 卡片不声明耗时（且如实写明为什么不给）",
   /不给耗时\/ETA/.test(PLAN) && /NOTE_TIME/.test(PLAN));
ok("C3 输出里没有速度字段（速度只认右上角滑块）",
   !/["']speed_pct["']\s*:/.test(PLAN) && !/["']vperc["']\s*:/.test(PLAN));
ok("C4 输出里没有 40135 位号 / 寄存器地址",
   !/40135/.test(PLAN.replace(/气路位号唯一来源[^\n]*/g, "")) && !/["']bit["']\s*:/.test(PLAN));

// ---------------------------------------------------------------- 4. 三态不塌成布尔
ok("D1 闸门未校验 = None，与「通过」分开",
   /if ready is None:/.test(PLAN) && /"未校验" if ready is None/.test(PLAN));
ok("D2 有一道闸门没校验 → 整卡降为 warn（不给绿章）",
   /verdict = VERDICT_WARN/.test(PLAN) && /不给「可以执行」的绿章/.test(PLAN));
ok("D3 间隙拿不到姿态 → state=unknown（不是 safe）",
   /"state": "unknown"/.test(PLAN));
ok("D4 影子模式不得谎称「执行时会被拒」",
   /SE\.effective_mode\(\)/.test(PLAN) && /影子模式/.test(PLAN)
   && /enforces/.test(PLAN));
ok("D5 读库失败 ≠ 点位未示教（两条文案分开）",
   /poses_error/.test(PLAN) && /读不到点位库/.test(PLAN) && /尚未示教/.test(PLAN));
ok("D6 拿不到实时姿态 → 一路都不预检（不拿名义姿态冒充机器人在这儿）",
   /has_live = cur is not None/.test(PLAN) && /if nxt and has_live:/.test(PLAN));
ok("D7 前端间隙三态显示「未预检」（不显示成安全）",
   /未预检/.test(VIEW) && /state === "unknown"/.test(VIEW));
ok("D8 前端检查项三态：未校验 / 通过 / 被拒 分开",
   /未校验/.test(VIEW) && /const chkOf/.test(VIEW));
ok("D9 预检失败留一张失败卡，不清空成「没隐患」",
   /compiled: false/.test(STORE) && /这不代表没有隐患/.test(VIEW));

// ---------------------------------------------------------------- 5. plan_id 挂勾（看的=确认的）
ok("E1 预检带 plan_id（复用刚解析的那一份）",
   /plan_id: id/.test(STORE) && /body: JSON\.stringify\(\{ plan_id: id \}\)/.test(STORE));
ok("E2 卡片与消息按同一个 plan_id 配对，对不上不显示",
   /const cardOf = \(m\) => \{[\s\S]*?c\.plan_id === pid[\s\S]*?\};/.test(VIEW));
ok("E3 解析成功自动预检一次（纯 watch，不藏按钮）",
   /watch\(\(\) => ai\.parsed && ai\.parsed\.plan_id/.test(VIEW)
   && /ai\.compilePlan\(pid\)/.test(VIEW));
ok("E4 解析时清掉上一张预检卡（避免挂着旧句子）",
   /this\.parsed = null; this\.planCard = null;/.test(STORE));

// ---------------------------------------------------------------- 6. 前端真的把段表摊开了
ok("F1 表格有 点位 / 用途 / J6 面 / 最小余量 / 闸门 五列",
   /<th>点位<\/th><th>用途<\/th><th>J6 面<\/th>/.test(VIEW) && /<th>最小余量<\/th><th>闸门<\/th>/.test(VIEW));
ok("F2 自动补段有单独标记（不是假装只有目标点）",
   /auto_preface/.test(VIEW) && /自动补段/.test(VIEW));
ok("F3 卡面标题写明「不执行」",
   /路径预检（不执行）/.test(VIEW));
ok("F4 指出第几段会被拒（blocked_at 落到文案，不只是变个颜色）",
   /blockedOf/.test(VIEW) && /段「\{\{ blockedOf\(m\)\.point \}\}」执行时会被拒/.test(VIEW));
ok("F5 扫掠口径的免责声明上界面（sweep_note）",
   /sweep_note/.test(VIEW));

// ---------------------------------------------------------------- 7. 后端仍是最初那份契约
ok("G1 /ai/parse 端点仍只解析不执行（预检没把它改成执行）",
   parseFn.length > 40 && /_parse_to_plan\(body\.text\)/.test(parseFn)
   && !/_move\(/.test(parseFn) && !/reject_if_not_ready\(/.test(parseFn));
ok("G2 /ai/exec 的统一闸门原样保留（未就绪 + 围栏互锁）",
   /reject_if_not_ready\(/.test(execFn) && /guard_check/.test(AIAPI));
ok("G3 解析失败时预检如实报错，不硬编一段路径",
   /"compiled": False/.test(planFn) && /segments": \[\]/.test(planFn)
   && /没听懂|error/.test(planFn));
ok("G4 缓存记录带 source（预检卡要如实标注大模型/规则）",
   /"source": source/.test(AIAPI) && /rec\.get\("source"\)/.test(planFn));
ok("G5 预检事件只记账，不产生执行事件",
   /ai\.plan_compile/.test(planFn) && !/ai\.exec/.test(planFn));

// ---------------------------------------------------------------- 8. 模块级"绝不"清单（文档即钉子）
ok("H1 预检模块自带「不接执行/不产 ETA」的声明",
   /刻意不做/.test(PLAN) && /不接执行/.test(PLAN));
ok("H2 AST 模块写明「大模型零指令权」",
   /零指令权|不由指令决定/.test(AST));

console.log(`\n意图编译器（E1）守卫：${pass} 过 / ${fail} 败`);
if (fail) process.exit(1);
