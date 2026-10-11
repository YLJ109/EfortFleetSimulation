// =====================================================================
// 统一"未就绪"闸门回归（headless，源码扫描）—— 2026-10-11
//
// 用户口径：「如果没有上电，执行点到点、移到什么的，全部提醒机器人没有就绪，所有的。」
//
// 钉死（源码级，不连真机）：
//  A. 后端只有一个闸门函数 + 一句统一文案；所有"会让机器人动"的真实下发入口都接了它；
//  B. 空跑(dry_run)/模拟模式放行、就绪流程不被拦（防死锁）；
//  C. 前端各入口把后端返回的 message 解析出来显示（绝不出现 undefined），
//     且"未就绪"旁注统一引用 NOT_READY_HINT（同一句开场白）。
//
// 运行： node tools/verify_not_ready.mjs
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

console.log("== 统一「未就绪」闸门回归 ==");

// ---------------- 后端源码 ----------------
const motionPy = read("../backend/app/services/motion.py");
const controlPy = read("../backend/app/api/control.py");
const sortingPy = read("../backend/app/api/sorting.py");
const aiPy = read("../backend/app/api/ai.py");
const tttPy = read("../backend/app/api/tictactoe.py");
const visionRulesPy = read("../backend/app/services/vision_rules.py");
const rcReadyPy = read("../backend/app/services/rc_ready.py");
const exceptionsPy = read("../backend/app/core/exceptions.py");

// A. 唯一闸门 + 统一文案
ok("后端有唯一定义：SERVO_NOT_READY_MSG 以「机器人没有就绪」开头",
  /SERVO_NOT_READY_MSG\s*=\s*"机器人没有就绪/.test(motionPy));
ok("后端有统一闸门函数 servo_not_ready_reason(dry)",
  /def servo_not_ready_reason\(dry:\s*bool\s*=\s*False\)/.test(motionPy));
ok("闸门判据 = 伺服上电位 bits.servo",
  /\.get\("bits"\)[\s\S]{0,120}?get\("servo"\)/.test(motionPy));
ok("闸门：空跑 dry → 放行（不在空跑上拦）",
  /def servo_not_ready_reason[\s\S]{0,400}?if dry:\s*\n\s*return None/.test(motionPy));
ok("闸门：模拟模式（双闸未开）→ 放行",
  /def servo_not_ready_reason[\s\S]{0,600}?if not real_write_enabled\(\):\s*\n\s*return None/.test(motionPy));
ok("闸门：读不到状态按「未就绪」拒绝（fail-safe）",
  /def servo_not_ready_reason[\s\S]{0,1400}?snap is None[\s\S]{0,200}?机器人没有就绪/.test(motionPy));

// A. control.py：统一 raise 帮助函数 + 各入口调用
ok("control 有统一前置校验 reject_if_not_ready（一处实现）",
  /def reject_if_not_ready\(dry:\s*bool\s*=\s*False\)/.test(controlPy));
ok("拒绝用 HTTP 409（不触发前端『需要登录』分支）",
  /reject_if_not_ready[\s\S]{0,600}?raise HTTPException\(409,\s*detail=reason\)/.test(controlPy));
for (const [label, re] of [
  ["/control/move", /def api_move[\s\S]*?reject_if_not_ready\(\)[\s\S]*?motion\.command/],
  ["/control/run-file", /def api_run_file[\s\S]*?reject_if_not_ready\(dry=bool\(body\.dry_run\)\)/],
  ["/control/jog/step", /def api_jog_step[\s\S]*?reject_if_not_ready\(\)/],
  ["/control/jog/start", /def api_jog_start[\s\S]*?reject_if_not_ready\(\)/],
  ["/control/vacuum", /def api_vacuum[\s\S]*?reject_if_not_ready\(\)/],
]) {
  ok(`入口 ${label} 已接闸门`, re.test(controlPy));
}
ok("sorting/api_start 已接闸门（空跑放行）",
  /def api_start[\s\S]{0,1200}?reject_if_not_ready\(dry=bool\(body\.dry_run\)\)/.test(sortingPy));
ok("sorting/api_goto 已接闸门", /def api_goto[\s\S]{0,900}?reject_if_not_ready\(\)/.test(sortingPy));
ok("ai/api_exec 已接闸门（空跑放行）",
  // ★ 2026-10-11：窗口由 2600 放宽到 4200 —— api_exec 里新增了"双面（快速完成）"透传等，
  //   与 `def api_exec` 的距离变长（闸门调用本身**仍在**，只是更靠后）。断言意图不变。
  /def api_exec[\s\S]{0,4200}?reject_if_not_ready\(dry=bool\(body\.dry_run\)\)/.test(aiPy));
ok("tictactoe/api_start 已接闸门（空跑放行）",
  /def api_start[\s\S]{0,1200}?reject_if_not_ready\(dry=bool\(body\.dry_run\)\)/.test(tttPy));
ok("视觉规则自动执行(vision_rules.execute_rule)也接闸门",
  /def execute_rule[\s\S]*?servo_not_ready_reason\(\)/.test(visionRulesPy));

// B. 防死锁：就绪流程绝不引用闸门
ok("★ 就绪流程（rc_ready.py）不引用闸门（否则用户永远无法就绪 = 死锁）",
  !/servo_not_ready_reason/.test(rcReadyPy) && !/reject_if_not_ready/.test(rcReadyPy));

// A. 后端契约：HTTPException.detail → 响应体 message（前端读的就是它）
ok("全局异常处理器把 HTTPException.detail 映射到 message",
  /exception_handler\(HTTPException\)[\s\S]{0,300}?"message":\s*str\(exc\.detail\)/.test(exceptionsPy));

// ---------------- 前端源码 ----------------
const rcReadyJs = read("src/stores/rcReady.js");
const pointView = read("src/components/PointExecView.vue");
const progView = read("src/components/ProgramExecView.vue");
const execJs = read("src/stores/exec.js");
const aiJs = read("src/stores/ai.js");
const tttJs = read("src/stores/tictactoe.js");
const sortView = read("src/components/SortingView.vue");

ok("前端统一文案常量 NOT_READY_HINT 以「机器人没有就绪」开头",
  /export const NOT_READY_HINT\s*=\s*"机器人没有就绪/.test(rcReadyJs));
ok("点位执行页未就绪旁注引用 NOT_READY_HINT",
  pointView.includes("NOT_READY_HINT") && pointView.includes("import { useRcReadyStore, NOT_READY_HINT }"));
ok("程序执行页未就绪结论引用 NOT_READY_HINT",
  progView.includes("NOT_READY_HINT") && /return NOT_READY_HINT/.test(progView));
ok("exec.blockReason 引用 NOT_READY_HINT",
  execJs.includes("NOT_READY_HINT") && /return NOT_READY_HINT/.test(execJs));

// C. 各入口必须把后端 message 解析出来显示（不许 undefined）
ok("exec.startJog 读 d.message（后端 409 文案能显示）",
  /async startJog[\s\S]{0,3000}?d\.message\s*\|\|\s*d\.error/.test(execJs));
ok("exec.stepJog 读 d.message", /async stepJog[\s\S]{0,2000}?d\.message\s*\|\|\s*d\.error/.test(execJs));
ok("exec.doTeach 读 d.message", /示教下发失败[\s\S]{0,80}?d\.message/.test(execJs));
ok("exec.runFile 解析 message/detail",
  /body\.message \|\| body\.error/.test(execJs));
ok("exec.vacuum 解析 message/detail", /inner\.message \|\| inner\.error/.test(execJs));
ok("sorting 页 errText 解析 message/detail", /asStr\(d && d\.message\)/.test(sortView));
ok("ai 执行读 message/detail", /d\.message \|\| d\.detail/.test(aiJs));
ok("tictactoe 读 message/detail", /d\.message \|\| d\.detail \|\| d\.error/.test(tttJs));

// C. 静态兜底：各入口错误回退都是**非空中文**（不会把 undefined 拼进提示语）
ok("点动被拒的回退文案非空且不是 undefined",
  /d\.message \|\| d\.error \|\| "点动被拒绝"/.test(execJs)
  && /d\.message \|\| d\.error \|\| "增量点动被拒绝"/.test(execJs));
ok("错误回退一律回落到 `HTTP <status>` 或中文（无裸 undefined 字面量）",
  /return "HTTP " \+ status;/.test(tttJs)
  && /asStr\(d && d\.message\)[\s\S]{0,60}?"HTTP " \+ status/.test(sortView));

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
