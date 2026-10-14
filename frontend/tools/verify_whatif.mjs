// =====================================================================
// ★★ E3「反事实试跑（What-if 沙盒）」前端接线守卫（headless 静态检查）
//
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §E3
// 后端：backend/app/services/whatif.py（顶部 docstring 就是需求书）
// 接口：POST /api/points/whatif · 前端：stores/whatif.js + components/WhatIfCard.vue
//
// 这张卡的立意是「**不碰真机**回答值不值得上真机」，所以它能坏掉的方式恰好是本项目
// 反复踩过的那几类（骨架照抄 verify_one_cell.mjs / verify_twin_residual.mjs）：
//   ① **观测件长出手**（§5.1）：试跑口一旦能"顺手应用/去执行"，它就变成第二个执行引擎；
//   ② **把离线统计说成时间承诺**（§4 关键机理 1：上位机只写目标+触发位、拿不到速度曲线）
//      —— 界面里出现"预计/还剩/需要多久"这类字，就是在编故事；
//   ③ **三态塌成布尔**（§5.5 铁律③）：`no_data` 染成绿色 = "没数据"冒充"通过"；
//   ④ **界面一个数、实际另一个数**（铁律②）：样本数必须显示**真跑完**的那个，
//      被墙钟预算截断要如实贴出来；
//   ⑤ **数字没有出处**：每个数旁边必须有采样时刻，读不到写「—（没测）」而不是合成 0。
//
// 运行： cd frontend && node tools/verify_whatif.mjs
// =====================================================================
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SL = "/";                      // 统一用正斜杠比路径，免得 Windows 反斜杠把正则吃了

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

/** JS/Vue：剥掉块注释 / HTML 注释 / 整行 //（注释里写"不会下发"不该把静态钉判红）。 */
function stripJs(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}
/** 全前端扫描：把 src 下每个 .js/.vue 里出现 `"<pat>` 字样的文件挑出来（路径用正斜杠）。
 *  A2b 用它证明"发起口只有一个"不是嘴上说说 —— 别人新加一个偷偷下发的文件就会被钉住。 */
function requestSites(pat) {
  const out = [];
  const walk = (dir) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, e.name);
      if (e.isDirectory()) { walk(p); continue; }
      if (!/\.(js|vue)$/.test(e.name)) continue;
      if (stripJs(read(p)).indexOf(pat) >= 0) out.push(p.split("\\").join(SL));
    }
  };
  walk(join(root, "src"));
  return out;
}
/** Python：逐字扫描，只在**字符串外**遇到 # 才砍到行尾；再把 docstring 压掉。
 *  ★ 只判"真有没有调用"：whatif.py 的连结论文案里就写着「不写 40103」「无 ETA」。 */
function stripPy(s) {
  let out = "", q = null, i = 0;
  while (i < s.length) {
    const c = s[i];
    if (q) {
      // ★ 先判闭合再落字符：否则收尾引号会被写两遍（"n": ran → "n"": ran），静态钉全被打歪
      if (c === "\\") { out += c + (s[i + 1] || ""); i += 2; continue; }
      if (s.startsWith(q, i)) { out += q; i += q.length; q = null; continue; }
      out += c; i++; continue;
    }
    if (c === "#") { while (i < s.length && s[i] !== "\n") i++; continue; }
    if (c === '"' || c === "'") {
      q = s.startsWith(c.repeat(3), i) ? c.repeat(3) : c;
      out += q; i += q.length; continue;
    }
    out += c; i++;
  }
  // 再把**剩下的**三引号串（docstring）整体压掉：里面的中文全是说明，不是代码
  return out.replace(/"""[\s\S]*?"""/g, "''").replace(/'''[\s\S]*?'''/g, "''");
}

const store = fe("src/stores/whatif.js");
const storeCode = stripJs(store);
const card = fe("src/components/WhatIfCard.vue");
const cardCode = stripJs(card);
const point = fe("src/components/PointExecView.vue");
const control = fe("src/net/control.js");
const beSvc = be("app/services/whatif.py");
const beSvcCode = stripPy(beSvc);
const beApi = be("app/api/points.py");
const beTest = join(beRoot, "tests", "test_whatif.py");

// 执行入口的字面（**前端这一侧一律不许出现**）
const EXEC_PATHS = ["/control/move", "/control/run-file", "/control/jog", "/control/vacuum",
  "/control/point-target", "/control/clear-error", "/control/force-release",
  "/sorting/start", "/sorting/goto", "/sorting/state", "/ai/exec", "/ai/cancel",
  "/tictactoe/start", "/ready", "/estop"];

// ---------------------------------------------------------------------
console.log("");
console.log("== A. 零下发：试跑口不许长出手（§5.1 真机安全最高优先）==");
{
  for (const p of EXEC_PATHS) {
    ok(`A1 发起口/卡片里不出现执行路径 ${p}`,
       storeCode.indexOf(p) < 0 && cardCode.indexOf(p) < 0);
  }
  ok("A2 唯一的控制级请求 = POST /api/points/whatif，只由 stores/whatif.js 发出",
     /apiControl\("\/points\/whatif"/.test(storeCode) &&
     /method:\s*"POST"/.test(storeCode) &&
     (storeCode.match(/apiControl\(/g) || []).length === 1 &&
     (cardCode.match(/apiControl\(|axios/g) || []).length === 0 &&
     (cardCode.match(/fetch\(/g) || []).length === 1);
  ok("A2b 全前端只有 stores/whatif.js 引用这个请求路径（没有第二个发起口偷偷下发）",
     requestSites("points/whatif").length === 1 &&
     requestSites("points/whatif").every((f) => f.endsWith("stores" + SL + "whatif.js")),
     requestSites("points/whatif").join(" , "));
  ok("A2c 卡片这一侧连令牌口都没碰（只有一条只读 GET，写在 A3）",
     !/control\.js/.test(cardCode) && !/X-Control-Token/.test(cardCode));
  ok("A3 只读地点位名用 GET（普通 fetch，不带令牌、不写库）",
     /fetch\(apiUrl\("\/points"\)\)/.test(cardCode));
  ok("A4 不 import 执行层（stores/exec.js / three/manager / 手柄 store 都不在依赖里）",
     !/stores\/exec\.js/.test(storeCode) && !/stores\/exec\.js/.test(cardCode) &&
     !/useExecStore/.test(storeCode) && !/useExecStore/.test(cardCode) &&
     !/stores\/pad\.js/.test(storeCode) && !/three\/manager/.test(storeCode));
  ok("A5 卡片没有任何「应用 / 下发 / 去执行 / 保存场景」按钮（决定权在人）",
     !/应用|下发到机器人|去执行|立即执行|写入点位|保存场景/.test(
       cardCode.replace(/不应用、不下发|没有「应用 \/ 去执行」的出口|不代替真机验证/g, "")) ||
     !/<button[^>]*>(\s|[^<])*(应用|去执行|立即执行)/.test(card));
  ok("A6 后端实验台不碰下发层（剥注释与 docstring 后判调用）",
     !/write_reg\(|read_regs\(|rc_jog_execute\(|rc_vacuum\(|rc_command\(|motion\.command\(/
        .test(beSvcCode) &&
     !/from app\.services\.(motion|modbus|jog|rc_ready|sim_robot)|import app\.services\.(motion|modbus)/
        .test(beSvcCode));
  ok("A7 后端接口要控制令牌，且 /points/whatif 排在 /points/{pid} 之前",
     /@router\.post\("\/whatif"\)[\s\S]{0,220}Depends\(require_control\)/.test(beApi));
  ok("A8 接口层也不碰长流程咽喉（_move / command / 写库）",
     !/_move\(|motion\.command\(|update_point\(|create_point\(|db\.commit\(/
        .test(beApi.slice(beApi.indexOf('class WhatIfIn'), beApi.indexOf("# ---------- 导出"))));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== B. ETA 禁区：界面绝不把离线统计说成时间承诺（§4 关键机理 1）==");
{
  // ★ 中文串里绝不嵌 ASCII 双引号（会被 check_cjk_bare 拦下），这里的正则也不带中文引号
  const ETA = /[\b_]eta[\b_]|ETA|estimate|预计|预估|还剩|还需|需要多久|秒后完成|倒计时|还要多久/g;
  for (const [nm, src] of [["stores/whatif.js", storeCode], ["WhatIfCard.vue", cardCode]]) {
    const hits = src.match(ETA) || [];
    ok(`B1 ${nm} 源码（剥注释后）里不出现 ETA / 预计 / 还剩 / 需要多久 一类字样`,
       hits.length === 0, hits.join(","));
  }
  ok("B2 节拍那一档没有实测样本 ⇒ 原样显示后端的「无实测样本·不给节拍估计」",
     /无实测样本/.test(cardCode) && /available === true/.test(cardCode) &&
     /available === false/.test(cardCode));
  ok("B3 界面自己不做任何时间推算（没有 settle×段数 / 除以速度 这类算式）",
     !/settle_ms\s*[*×]/.test(cardCode) && !/cycle_time[^"]*\/\s*speed/.test(cardCode) &&
     !/Math\.round\([^)]*speed/.test(cardCode));
  ok("B4 后端返回体里没有以 eta/estimate 命名的键（键名级禁区）",
     !/"(eta|eta_ms|estimate|estimated_[a-z_]*|remaining_sec)"/.test(beSvcCode));
  ok("B5 后端结论文案里的 ETA 全是否定式（「无 ETA」），不是数字承诺",
     !/ETA[^。，]{0,6}\d/.test(beSvc) && /无 ETA/.test(beSvc));
  ok("B6 卡片底部必须写「离线统计估计 · 非认证」",
     /离线统计估计 · \*?\*?非认证/.test(card) || /离线统计估计/.test(cardCode));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== C. 三态不塌成布尔（§5.5 铁律③）==");
{
  const four = ["ok", "partial", "failed", "no_data"];
  ok("C1 store 的状态表把四种都列出来（认不出来 ⇒ unknown，不塌成 ok）",
     four.every((k) => new RegExp(`\\b${k}:`).test(storeCode)) && /unknown/.test(storeCode));
  ok("C2 状态判据取自后端 status，前端不复算一遍",
     /s\.res\.status/.test(storeCode) && !/status = computed\(\(\) => .*busy/.test(storeCode));
  ok("C3 样式映射用**显式判等**（没有 `status ?` / `if (status)` 这种塌法）",
     !/status\s*\?\s*["']w-/.test(storeCode) && !/if\s*\(\s*\w*\.status\s*\)\s*return\s+["']w-ok/
        .test(storeCode) &&
     /s\.status === "ok"/.test(storeCode) && /s\.status === "partial"/.test(storeCode) &&
     /s\.status === "failed"/.test(storeCode));
  ok("C4 no_data / unknown ⇒ 灰虚线（w-unknown），**绝不落到 w-ok**",
     /return "w-unknown"/.test(storeCode) &&
     !/case "no_data"[\s\S]{0,40}w-ok/.test(storeCode));
  ok("C5 卡片对四种状态各有明显不同的样式（含 no_data 的「不代表安全」）",
     four.every((k) => new RegExp(`st\.value === "${k}"`).test(cardCode)) &&
     /不代表安全/.test(cardCode) && /\.st-nodata/.test(card) &&
     !/\.st-nodata\s*\{[^}]*#6ee7a0/.test(card));
  ok("C6 卡片三态块用 v-if/v-else-if 严格判等链，不是真值判断",
     cardCode.indexOf('v-if="isNoData"') >= 0 &&
     !/v-if="wi\.res\.status"/.test(card) && !/v-if="wi\.status"/.test(card));
  ok("C7 请求失败与后端 no_data 分得开（err 有独立文案，不并成「没问题」）",
     /wi\.err/.test(cardCode) && /请求失败不等于/.test(cardCode));
  ok("C8 抖动来源的 assumed 也按三态判（=== true / === false / 未标注）",
     /assumed === true/.test(cardCode) && /assumed === false/.test(cardCode) &&
     /未标注/.test(cardCode));
  ok("C9 后端四态齐全且 no_data 的 ok 必为 false",
     /"no_data"/.test(beSvcCode) && beSvc.indexOf('"ok": False, "status": status') >= 0);
}

// ---------------------------------------------------------------------
console.log("");
console.log("== D. 样本数 = 实际跑出来的那个（§5.5 铁律②）==");
{
  ok("D1 store 的 nRan 取 res.n（**不是** n_requested）",
     /nRan:[\s\S]{0,140}s\.res\.n\b/.test(storeCode) &&
     !/nRan:[\s\S]{0,80}n_requested/.test(storeCode));
  ok("D2 truncated 用「实跑 < 请求」判，不靠猜",
     /truncated:[\s\S]{0,220}ran < want/.test(storeCode));
  ok("D3 界面显示的样本数是 nRan；n_requested 只作为「本次请求」并列出现",
     /样本数[：:][\s\S]{0,60}wi\.nRan/.test(cardCode) && /wi\.nRequested/.test(cardCode));
  ok("D4 被预算截断时把后端那句原文贴出来（不自己改写数字）",
     /wi\.truncated/.test(cardCode) && /truncNote/.test(storeCode) &&
     /预算/.test(storeCode) && /被墙钟预算截断/.test(cardCode));
  ok("D5 后端把实跑数写回 res.n（宁少不假），并在 failures 里说明截断",
     /"n": ran,/.test(beSvcCode) && /超预算/.test(beSvc.replace(/\s+/g, "")) ||
     /墙钟超过预算/.test(beSvc));
  ok("D6 缺省样本数与后端 SAMPLE_N_DEFAULT 同一口径（界面留空 = 后端缺省）",
     /SAMPLE_N_DEFAULT = 200/.test(beSvc) &&
     /留空=缺省|留空 = 后端缺省/.test(card + store));
  ok("D7 n 的上限由后端 SAMPLE_N_MAX 兜（前端只做同值夹取，不放行更大的数）",
     /SAMPLE_N_MAX = 300/.test(beSvc) && /Math\.min\(300,/.test(storeCode));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== E. 分布不是单点 + 数字与采样时刻成对出现 ==");
{
  ok("E1 显示 P5 / P50 / P95 三个分位（不是单点）",
     /wi\.dist\.p5\b/.test(cardCode) && /wi\.dist\.p50/.test(cardCode) && /wi\.dist\.p95/.test(cardCode));
  ok("E2 直方图来自后端 bins（前端只画不重算）",
     /dist\.bins|wi\.dist && wi\.dist\.bins/.test(cardCode) && !/new Array\(\d+\)\.fill/.test(cardCode));
  ok("E3 每个数字块旁边都有采样时刻（至少 5 处）",
     (card.match(/采样 \{\{ atText \}\}/g) || []).length >= 5,
     String((card.match(/采样 \{\{ atText \}\}/g) || []).length));
  ok("E4 逐段表把「段 + 数字 + 采样时刻」钉在同一行",
     /<td class="muted">\{\{ atText \}\}<\/td>/.test(card));
  ok("E5 采样时刻读不到 ⇒「—（没测）」，绝不合成 0 / 不写当前时间糊上去",
     /ranAt[\s\S]{0,200}—（没测）/.test(cardCode) &&
     /function num/.test(cardCode) && /=== null \|\| .*=== undefined/.test(cardCode));
  ok("E6 后端没有样本时 sample_ms 是 None（不是 0.0），界面据此显示没测",
     /"sample_ms": None/.test(beSvcCode) &&
     /v === null \|\| v === undefined \|\| v === ""/.test(storeCode));
  ok("E7 每段最坏占比标明是**几何风险比例**、不是控制器实测到位率",
     /触界样本占比|几何风险/.test(cardCode) && /几何/.test(beSvc));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== F. 挂载与同源 ==");
{
  ok("F1 卡片挂在「点位执行」页（与 D2 数值体检同页就近）",
     /import WhatIfCard from "\.\/WhatIfCard\.vue"/.test(point) && /<WhatIfCard \/>/.test(point));
  ok("F2 挂载点在「预设点位」卡之后（数值体检就在它里面）",
     point.indexOf("<WhatIfCard />") > point.indexOf("数值体检"));
  ok("F3 发起口经 net/control.js 的 apiControl（自动带 X-Control-Token；401 立刻收回）",
     /from "..\/net\/control\.js"/.test(storeCode.replace(/["']/g, '"')) &&
     /apiControl/.test(storeCode) && /X-Control-Token/.test(control));
  ok("F4 卡片不依赖就绪门控（它不动机器人，谈何「现在能不能动」）—— 不引入 rc.ready 依赖",
     !/useRcReadyStore|rc\.ready/.test(storeCode) && !/useRcReadyStore/.test(cardCode));
  ok("F5 本卡与既有的 TwinResidualCard 同页共存、互不复算",
     /<TwinResidualCard \/>/.test(point));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== G. 回归钉本身在 ==");
{
  ok("G1 后端 tests/test_whatif.py 存在", existsSync(beTest));
  const t = existsSync(beTest) ? read(beTest) : "";
  ok("G2 后端钉住方案点名的核心那条（零下发静态钉 + 无 ETA + 节拍只代实测）",
     /def test_whatif_never_touches_motion_or_modbus_writes/.test(t) &&
     /def test_no_eta_no_estimate_no_speed_write/.test(t) &&
     /def test_cycle_basis_requires_measured_segments/.test(t) &&
     /def test_sample_n_is_what_actually_ran/.test(t) &&
     /def test_tristate_never_collapses_to_boolean/.test(t) &&
     /def test_jitter_sources_are_listed_with_assumed_flag/.test(t) &&
     /def test_reorder_only_affects_evaluation_path/.test(t));
  ok("G3 后端有 e2e（带令牌 200 + 未带令牌 401/403）",
     /def test_whatif_endpoint_requires_control_token/.test(t) &&
     /def test_whatif_endpoint_returns_distribution_and_reads_library/.test(t));
  ok("G4 后端测试里有「一调就抛」的动态护栏（不只是源码字符串判）",
     /_dispatch_guard/.test(t) && /rc_jog_execute/.test(t));
  ok("G5 本脚本已并入 `npm run verify` 链（不并入的守卫永远不会跑 = 等于没钉）",
     existsSync(join(root, "tools", "verify_whatif.mjs")) &&
     /node tools\/verify_whatif\.mjs/.test(read(join(root, "package.json"))));
}

console.log("");
console.log(`反事实试跑（E3）守卫：${pass} 过 / ${fail} 败`);
if (fail) process.exit(1);
