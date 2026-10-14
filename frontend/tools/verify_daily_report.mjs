// =====================================================================
// ★★ E6「机器人自述 = 每天一页现场日报」守卫（headless 静态检查）
//
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §E6
//
// 这条功能的命门是「**每一个数字都能自证**」，所以钉子分五组：
//   A 只读、零下发、不写事件（AGENTS.md §5.1 真机纪律：不许连机器人 / 不许下发运动 /
//     不许启动相机 / 不许在真机分拣运行期做任何打断动作）
//   B 零 LLM 依赖（本轮纯模板；将来接也只当讲解员，零指令权零否决权）
//   C 无数据不塌成 0（§5.5 铁律③：三态不许塌成布尔；「没数据」必须明写）
//   D 数字一致性自检真的接线（UI 与 API 都必须显示不一致项，非空即红，不许静默）
//   E 接线（DailyReportCard 挂到 EventsView，中文串里无 ASCII 双引号等）
//
// 运行： cd frontend && node tools/verify_daily_report.mjs
// ★★ 本脚本**不**在 package.json 的 npm run verify 里（frontend/package.json 是禁止清单，
//    不能改）；跑法只能是直接 node 执行。这个限制必须在收尾报告里如实说明。
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

function stripJs(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}
function stripPy(s) {
  // 先按行去 `#` 注释，再去 `"""..."""` docstring
  return s.split(String.fromCharCode(10))
          .filter((l) => !/^\s*#/.test(l))
          .join(String.fromCharCode(10))
          .replace(/""".*?"""/gs, "");
}

const SVC = be("app/services/daily_report.py");
const SYSC = be("app/api/system.py");
const TESTS = be("tests/test_daily_report.py");
const STORE = fe("src/stores/report.js");
const CARD = fe("src/components/DailyReportCard.vue");
const EVENTS = fe("src/components/EventsView.vue");
const PKG = fe("package.json");

const SVCC = stripPy(SVC);
const SYSCC = stripPy(SYSC);
const STOREC = stripJs(STORE);
const CARDc = stripJs(CARD);

// 只截两个新端点函数体（避免整片 system.py 里的 require_control 干扰判据）
const epJson = (SYSC.split('@router.get("/daily-report")')[1] || "").split('@router.get("/daily-report.md")')[0];
const epMd = (SYSC.split('@router.get("/daily-report.md")')[1] || "").split("\n@router.");

console.log("\n=== E6 机器人自述 · 每天一页现场日报 ===\n");

// ---------------------------------------------------------------- A 只读、零下发、不写事件
ok("A1 日报服务不 import 运动 / Modbus 写路径",
   !/from app\.services\.motion|import motion|from app\.services import motion|\bmodbus\b/.test(SVCC));
ok("A2 日报服务不调用 events.emit（观测件不写事件表）",
   !/from app\.services\.events import emit|emit_event\(|events\.emit\(/.test(SVCC));
ok("A3 日报服务不引用 vision_samples / color_advice（同事在写、接口未定）",
   !/\bvision_samples\b/.test(SVCC) && !/\bcolor_advice\b/.test(SVCC));
ok("A4 判色样本量走可注入参数，默认 None",
   /def build_report\([\s\S]{0,300}color_samples\s*:\s*Optional\[/.test(SVC)
   && /color_samples\s*:\s*Optional\[[^\n]*\]\s*=\s*None/.test(SVC));
ok("A5 GET /api/system/daily-report 不带 require_control（观测不属于控制）",
   /@router\.get\("\/daily-report"\)/.test(SYSC)
   && !/require_control/.test(epJson));
ok("A6 GET /api/system/daily-report.md 同样不带 require_control",
   /@router\.get\("\/daily-report\.md"\)/.test(SYSC)
   && !/require_control/.test(epMd));
ok("A7 md 端点回 PlainTextResponse 且 media_type 明确写 text/markdown",
   /PlainTextResponse\(/.test(epMd) && /text\/markdown/.test(epMd));
ok("A8 没有新建 router，也没改 main.py（本轮硬约束）",
   /from app\.services import daily_report/.test(SYSC)
   && !/daily_report_router/.test(SYSCC)
   && !/api\.daily_report\b/.test(SYSCC)
   && !/daily_report/.test(be("app/main.py")));
ok("A9 前端 store 只有一条 GET（无 method、无 apiControl）",
   /fetch\(apiUrl\(`\/system\/daily-report/.test(STOREC)
   && !/method:/.test(STOREC) && !/apiControl/.test(STOREC));
ok("A10 卡面上没有任何机器人动作按钮（只有刷新与导出 markdown）",
   (CARDc.match(/@click="/g) || []).length === 2
   && /@click="report\.load\(\)"/.test(CARDc)
   && /@click="report\.exportMarkdown\(\)"/.test(CARDc)
   && !/\/control\/|motion\.|apiControl|sorting\/start|sorting\/goto|reconnect/.test(CARDc));

// ---------------------------------------------------------------- B 零 LLM 依赖
ok("B1 日报服务源码没有 LLM import",
   !/from app\.services\.llm|import .*llm|from app\.services import .*llm/.test(SVCC));
ok("B2 日报服务源码没有 LLM 调用符号（ask_llm / llm_complete / chat_completion）",
   !/\bask_llm\b|\bllm_complete\b|\bchat_completion\b/.test(SVCC));
ok("B3 后端测试有「零 LLM 且 survives」这条钉子",
   /def test_report_has_no_llm_dependency_and_survives_without_it/.test(TESTS));
ok("B4 前端 store / 卡都不调用 LLM API（fetch 里没有 /llm /ask_llm /chat_completion 端点）",
   !/\/(llm|ask_llm|chat_completion)\b/.test(STOREC) && !/\/(llm|ask_llm|chat_completion)\b/.test(CARDc)
   && !/\bask_llm\s*\(|\bchat_completion\s*\(|\bllm_complete\s*\(/.test(STOREC + CARDc));

// ---------------------------------------------------------------- C 无数据不塌成 0（§5.5 铁律③）
ok("C1 服务里定义了统一的「无此项数据」文案，且没有 || 0 降级",
   /NO_DATA_TEXT\s*=\s*"无此项数据"/.test(SVC)
   && !/\|\|\s*0\b/.test(SVCC));
ok("C2 空日不写「0 条事件 / 共 0 段」（后端源码有自觉声明）",
   /不许.*塌成 0|不许在任何地方塌成 0/.test(SVC));
ok("C3 _fmt_num(None) 输出「—」而不是 0",
   /def _fmt_num\([\s\S]{0,120}if v is None:\s*\n\s*return "—"/.test(SVC));
ok("C4 判色样本没接入时写「判色样本环尚未接入」，不编样本量",
   /判色样本环尚未接入/.test(SVC));
ok("C5 内存态数据源都注明「本机本次会话」",
   (SVC.match(/本机本次会话/g) || []).length >= 3);
ok("C6 后端测试有「空日不塌成 0」这条钉子",
   /def test_empty_day_says_no_data_not_zero/.test(TESTS));
ok("C7 前端 store 不把 sections / attention / fields 之类数据 || 0 降级（只允许元数据 count/fresh 兜底）",
   !/sections\s*\|\|\s*0/.test(STOREC)
   && !/attention\s*\|\|\s*0/.test(STOREC)
   && !/fields\s*\|\|\s*0/.test(STOREC)
   && !/value\s*\|\|\s*0/.test(STOREC)
   && !/total\s*\|\|\s*0/.test(STOREC));
ok("C8 前端卡面对 status===「no_data」与「unavailable」分支分开处理（三态不塌）",
   /sec\.status === 'no_data'/.test(CARDc) && /sec\.status === 'ok'/.test(CARDc)
   && /STATUS_LABEL\[sec\.status\]/.test(CARDc));

// ---------------------------------------------------------------- D 数字一致性自检真的接线
ok("D1 consistency_check 是纯函数，签名 (structured, markdown) → list[str]",
   /def consistency_check\(\s*structured\s*:[^)]+,\s*markdown\s*:\s*str\s*\)\s*->\s*List\[str\]/.test(SVC));
ok("D2 _walk_numbers 跳过展示专用键（markdown/lines/basis），防止自检自证",
   /def _walk_numbers\([\s\S]{0,600}?"markdown"[\s\S]{0,60}"lines"[\s\S]{0,60}"basis"/.test(SVC));
ok("D3 build_report 真的调 consistency_check 并把结果放进 report 的「consistency」字段",
   /consistency_check\(/.test(SVCC) && /report\["consistency"\]/.test(SVCC));
ok("D4 一致性未通过时 markdown 里追加显式小节（不许静默）",
   /一致性自检未通过/.test(SVC) && /不许发布/.test(SVC));
ok("D5 后端测试有「注入假数必须被抓出来」这条钉子",
   /def test_consistency_check_flags_fabricated_number/.test(TESTS)
   && /8760\.5|412\.9/.test(TESTS));
ok("D6 后端测试有「所有数字都能追溯」与「markdown ⊂ 结构化」两颗钉子",
   /def test_report_numbers_all_traceable_to_structured_sources/.test(TESTS)
   && /def test_markdown_numbers_are_subset_of_structured/.test(TESTS));
ok("D7 前端卡面在非空不一致时红字显示（dr-cons-bad）",
   /hasInconsistency/.test(CARDc) && /dr-cons-bad/.test(CARD)
   && /report\.inconsistent/.test(CARDc));
ok("D8 前端卡面把每一条 inconsistent 逐条列出（不合并 / 不静默）",
   /v-for="\(x, i\) in report\.inconsistent"/.test(CARDc));
ok("D9 每节都渲染 source 标签（可追溯性在 UI 上显形）",
   /来源：\{\{ sec\.source \}\}/.test(CARDc) && /it\.source/.test(CARDc));
ok("D10 每节都有 status 徽标（ok / no_data / unavailable 三态）",
   /dr-tag/.test(CARD) && /statusClass\(sec\.status\)/.test(CARDc));

// ---------------------------------------------------------------- E 接线与其它硬约束
ok("E1 EventsView 已挂载 DailyReportCard（import + 渲染）",
   /import DailyReportCard from "\.\/DailyReportCard\.vue"/.test(EVENTS)
   && /<DailyReportCard \/>/.test(EVENTS));
// ★ 原来这条写成 `… || true` —— **永远为真**的死钉（§5.5 铁律⑤：钉了却判不出任何东西）。
//   换成真判据：只看 dependencies / devDependencies 两个块，包名集合必须还是那 8 个。
//   （本脚本挂进 `npm run verify` 只动 scripts —— 链里出现脚本名是接线，不是依赖。）
const DEP_BLOCKS = (PKG.match(/"[A-Za-z]*[Dd]ependencies"\s*:\s*\{[\s\S]*?\}/g) || [])
  .join(String.fromCharCode(10));
const depNames = [...DEP_BLOCKS.matchAll(/^\s*"([A-Za-z@][\w@\/.-]*)":\s*"/gm)]
  .map((m) => m[1]).sort().join(",");
ok("E2 依赖清单一个字没多（还是那 8 个包；E6 前端零新增第三方，导出用原生 Blob + a.download）",
   depNames === "@eslint/js,@vitejs/plugin-vue,eslint,eslint-plugin-vue,pinia,three,vite,vue"
   && !/file-saver|markdown-it|dompurify|marked|jspdf|xlsx|pdfmake/i.test(PKG));
// 导出用的库一个都不许出现（file-saver / markdown-it / dompurify / marked …）：
ok("E2b 没有为导出 markdown 新增第三方依赖（无 file-saver / markdown-it / dompurify）",
   !/file-saver|markdown-it|dompurify|marked/.test(PKG));
ok("E3 客户端导出走 Blob + a.download（零新依赖）",
   /new Blob\(\[r\.markdown\]/.test(STOREC) && /a\.download/.test(STOREC)
   && /URL\.createObjectURL/.test(STOREC));
ok("E4 后端日期窗口是本地日期换 UTC 且 timezone 口径写明",
   /本地日期/.test(SVC) && /UTC/.test(SVC)
   && /def _day_window_utc/.test(SVC));
ok("E5 attention 是纯规则排序（数值降序 + 同值按登记顺序），每条带 basis 与 source",
   /\.sort\(key=lambda x: \(-x\[3\], x\[0\]\)\)/.test(SVC)
   && /"basis":/.test(SVC) && /"source":/.test(SVC)
   && /cands:\s*List\[/.test(SVC));
ok("E6 关注项零值不进列表（不凑数）",
   /if v is not None and v > 0/.test(SVC));
ok("E7 中文串里不嵌裸 ASCII 双引号（Python 与 JS 都靠「」代替，防 SyntaxError）",
   (() => {
     // 简易判据：抓每一行 Python 里形如 `"…"…"`（同一行里出现 3 个及以上 `"`，
     // 且这一行有中文）时，多半是在中文串里塞了裸 ASCII 双引号。docstring 起止 `"""`
     // 与多行 concat 不算（那种行没有中文或只有 `"""`）。
     const lines = SVC.split(String.fromCharCode(10));
     for (const l of lines) {
       if (!/[\u4e00-\u9fff]/.test(l)) continue;
       if (/"""/.test(l)) continue;                       // 三引号（docstring）跳过
       const q = (l.match(/"/g) || []).length;
       if (q % 2 === 1) return false;
     }
     return true;
   })());
ok("E8 后端测试有 autouse 内存清理 + 独占 logs 目录",
   /@pytest\.fixture\(autouse=True\)[\s\S]{0,1500}twin_residual\.reset\(\)[\s\S]{0,300}CE\.reset\(\)[\s\S]{0,300}AL\.reset\(\)/.test(TESTS)
   && /EFORT_LOGS_DIR/.test(TESTS));
ok("E9 后端测试有「端点只读不要令牌」这条钉子（覆盖两个端点）",
   /def test_report_endpoint_is_readonly_without_control_token/.test(TESTS)
   && /\/api\/system\/daily-report\.md/.test(TESTS)
   && /text\/markdown/.test(TESTS));
ok("E10 后端测试有「服务不写事件、不碰写路径」这条钉子",
   /def test_service_never_touches_write_paths_or_emits_events/.test(TESTS));
ok("E11 后端测试有「色样本可注入默认为 None」与「attention 规则排序」两条钉子",
   /def test_color_samples_is_injectable_default_none/.test(TESTS)
   && /def test_attention_is_rule_sorted_with_basis_numbers/.test(TESTS));
ok("E12 卡面「一致性通过」与小节都显示（不是只有未通过时才出现）",
   /一致性自检：通过/.test(CARD) && /一致性自检未通过/.test(SVC));
ok("E13 「无此项数据」在卡面上原样写，绝不 || 0",
   /no_data:\s*"无此项数据"/.test(CARD) && !/sec\.status === 'no_data'[\s\S]{0,80}\|\|\s*0/.test(CARDc));

ok("E14 本脚本已并入 `npm run verify`（文件头承诺的那条接线也必须是事实）",
   /node tools\/verify_daily_report\.mjs/.test(PKG));

console.log(`\n机器人自述日报（E6）守卫：${pass} 过 / ${fail} 败`);
if (fail) process.exit(1);
