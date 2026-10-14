// =====================================================================
// D2「点位数值体检」守卫（headless 静态检查）
//
// 这一条防的是**真发生过的事故**：点位库里 J6 录成 −360 → `encode_angle` 拒绝
// → 整条流程被终止（AGENTS.md §5.3 已固化）。体检只查数值本身
// （±327 可表示 / 软限位裕度 / 粗略奇异指示），**不查摆放**（负责人确认现场摆放没问题）。
//
// 界面这一侧最容易犯的四类错，逐条钉死：
//   ① **把"不知道"画成绿** —— 没体检结果 / 数据坏 / 笛卡尔不适用，
//      一律要显式说"未体检/无法体检/不适用"（§5.5 铁律③：未知不得塌成布尔）；
//   ② **替操作员改点** —— 体检只能给建议；自动"顺手把 −360 折成 0"就是替人改数据；
//   ③ **前端复制一套限位** —— ±327 的唯一事实来源是 `services/limits.py`，
//      界面写死一个数字就会在后端收窄/放宽时悄悄说假话；
//   ④ **靠名字映射点位** —— 库里允许同名点，映射必须按 **id**，
//      而且体检没覆盖到的点**排到最后但不许静默消失**。
//
// 运行： cd frontend && node tools/verify_point_audit.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const view = read("src/components/PointExecView.vue");
const pkg = read("package.json");
const svc = read("../backend/app/services/point_audit.py");
const ep = read("../backend/app/api/points.py");
const tests = read("../backend/tests/test_point_audit.py");

/** 剥掉注释（块注释 / HTML 注释 / 整行注释）—— 文档里写 ±327 是**说明**，
 *  代码里出现 327 才是"界面自己复制了一套限位"。 */
const stripComments = (s) => s
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/<!--[\s\S]*?-->/g, "")
  .replace(/^[ \t]*\/\/.*$/gm, "");

// 只截体检那一段（这个组件还有示教/点动/CRUD，拿整文件判"有没有写操作"会误判）
const secStart = view.indexOf("// ---- D2 点位数值体检");
const secEnd = view.indexOf("function triggerImport");
const panel = secStart >= 0 && secEnd > secStart ? view.slice(secStart, secEnd) : "";
ok("A0 体检代码段能定位到（脚本注释与函数顺序变了要同步改这里）",
   panel.length > 400, `panel=${panel.length}`);

// ---------------------------------------------------------------------
console.log("");
console.log("== A. 后端：唯一事实来源 + 纯只读 ==");
{
  ok("A1 ±327 来自 limits.ENCODE_LIMIT_DEG（不另立数字）",
     /from app\.services\.limits import ENCODE_LIMIT_DEG/.test(svc)
     && !/=\s*327(\.|\s*$)/m.test(svc.replace(/ENCODE_LIMIT_DEG\s*=\s*327\.0/g, "")));
  ok("A2 软限位来自 load_limits（同一来源，不是本地表）",
     /load_limits/.test(svc) && /bounds_of\(/.test(svc));
  ok("A3 体检不碰执行链路：不 import motion / modbus",
     !/\bfrom app\.services (motion|modbus)\b/.test(svc)
     && !/import (motion|modbus)/.test(svc));
  ok("A4 体检不写库：没有 create/update/delete_point 与 commit",
     !/create_point|update_point|delete_point|\.commit\(/.test(svc));
  ok("A5 体检不下发：没有 encode_angle 调用（只按同一口径自己判）",
     !/encode_angle\(/.test(svc));
  ok("A6 接口排在 /{pid} 之前（否则 \"audit\" 会被当成 pid → 422 永不命中）",
     ep.indexOf('@router.get("/audit")') > 0
     && ep.indexOf('@router.get("/audit")') < ep.indexOf("@router.get(\"/{pid}"));
  ok("A7 接口只读、与 GET /api/points 同口径（不要求令牌）",
     /@router\.get\("\/audit"\)\ndef api_points_audit\([\s\S]{0,240}?db: Session = Depends\(get_db\)\)/.test(ep)
     && !/api_points_audit[\s\S]{0,200}require_control/.test(ep));
  ok("A8 −360 事故已固化成后端钉",
     /def test_j6_360_point_is_flagged_unencodable/.test(tests));
  ok("A9 反证钉在场：±327 之内的值不许被误报",
     /def test_just_inside_encode_range_is_not_flagged/.test(tests));
  ok("A10 坏数据钉在场：NaN/缺轴 → unknown 而不是绿",
     /def test_nan_short_and_non_numeric_are_unknown_not_green/.test(tests));
  ok("A11 诚实边界在后端就写清（不查摆放 / 能不能到 / 撞围栏）",
     /not_checked/.test(svc) && /A2/.test(svc) && /safety_guard|safety_eval/.test(svc));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== B. 前端接线：只读 + 按 id 对齐 + 增删改后自动重跑 ==");
{
  ok("B1 体检走 GET /points/audit（fetch，不经 apiControl → 不需要令牌）",
     /fetch\(apiUrl\("\/points\/audit"\)\)/.test(panel));
  ok("B2 体检段里没有任何写操作（无 apiControl / POST / DELETE / savePoint）",
     !/apiControl|method:\s*"(POST|DELETE|PUT)"|savePoint|delPoint/.test(panel));
  ok("B3 界面代码里不硬编码 327（限位口径永远来自后端；注释里提这个数字是说明，不算复制）",
     !/327/.test(stripComments(panel)) && !/327/.test(stripComments(view)));
  ok("B4 列表 v-for 用 shownPoints（不是直接用 exec.points）",
     /v-for="p in shownPoints"/.test(view));
  ok("B5 映射按 **id**（同名点位不能靠名字对齐）",
     /new Map\(\(\(audit\.value && audit\.value\.points\) \|\| \[\]\)\.map\(\(a\) => \[a\.id, a\]\)\)\)/.test(panel)
     && !/\.map\(\(a\) => \[a\.name/.test(panel));
  ok("B6 体检没覆盖的点排最后但**不丢弃**",
     /ra === null \? 1e9 : ra/.test(panel) && /rb === null \? 1e9 : rb/.test(panel));
  ok("B7 增删改点后自动重跑（watch exec.points）",
     /watch\(\(\) => exec\.points, \(\) => \{ loadAudit\(\); \}\)/.test(view));
  ok("B8 签名含所有被消费的字段（id/名称/类型/关节值）—— 漏一个就会跳过真正需要的重算",
     /p\.id\}\|\$\{p\.name\}\|\$\{p\.kind\}\|\$\{\(p\.joints \|\| \[\]\)\.join\(","\)/.test(panel));
  ok("B9 有手动「重跑体检」入口且带 force（同签名也能重算）",
     /@click="loadAudit\(true\)"/.test(view) && /async function loadAudit\(force\)/.test(panel));
  ok("B10 读取失败如实报错，不静默留白",
     /体检读取失败（HTTP \$\{r\.status\}）/.test(panel) && /体检读取失败：\$\{e\.message\}/.test(panel));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== C. ★ 未知绝不塌成绿（§5.5 铁律③）==");
{
  ok("C1 没有体检结果 → key=none + 「未体检」（不是 ok/正常）",
     /if \(!a\) return \{ key: "none", label: "未体检"/.test(view));
  ok("C2 四档文案齐备，unknown 文案是「无法体检」而不是「正常」",
     /GRADE_LABEL = \{ ok: "正常", warn: "建议重录", bad: "下发会被拒", unknown: "无法体检" \}/.test(view));
  ok("C3 笛卡尔点/坏数据分别显示「不适用」「无法体检」，不并进 ok",
     /a\.applicable === false \? "不适用" : "无法体检"/.test(view));
  ok("C4 grade 三态比较用 === 判定，没有把 null 塌成布尔",
     /a\.applicable === false \|\| a\.evaluable === false/.test(view));
  ok("C5 条件数只在真算出来时显示（算不出留空，不写 0 冒充不奇异）",
     /s && s\.measured && s\.cond != null/.test(view) && /auditCond\(p\) != null/.test(view));
  ok("C6 unknown/未体检 的样式是**虚线**（视觉上就区别于四档实色）",
     /\.pa-chip\.g-unknown, \.pt-tag\.audit\.g-unknown, \.pt-tag\.audit\.g-none \{ border-style: dashed; \}/.test(view));
  ok("C7 没有把 g-unknown 上色的规则（防止将来误加绿色）",
     !/g-unknown[^}]*--ok/.test(view));
  ok("C8 四档计数都在表头（含 0 也显示，避免「只剩红字」看不出总量）",
     /by_grade\.bad/.test(view) && /by_grade\.warn/.test(view)
     && /by_grade\.unknown/.test(view) && /by_grade\.ok/.test(view));
  ok("C9 表头空态文案明说「不假装全绿」",
     /还没跑过[\s\S]{0,40}不假装全绿/.test(view));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== D. 诚实边界与「只建议、不改点」进界面 ==");
{
  ok("D1 not_checked（本体检不查什么）渲染到界面",
     /\(audit\.not_checked \|\| \[\]\)\.join\("；"\)/.test(view));
  ok("D2 后端 note（只读体检：不写库、不下发、不改点）跟着显示",
     /\{\{ audit\.note \}\}/.test(view));
  ok("D3 advice 逐行显示（建议重录，由人决定）",
     /auditById\.get\(p\.id\)\.advice/.test(view));
  ok("D4 reasons 逐行显示（为什么是这个档）",
     /v-for="\(w, wi\) in auditById\.get\(p\.id\)\.reasons"/.test(view));
  ok("D5 裕度条有**数字**（余量 xx°），不是只靠颜色",
     /余量 \{\{ auditById\.get\(p\.id\)\.worst_margin_deg \}\}°/.test(view));
  ok("D6 奇异是「相对指标」这句话在界面上（不当严格判据）",
     /相对指标/.test(view));
  ok("D7 界面没有任何「自动修正点位」入口（绝不替操作员改数据）",
     !/自动修正|一键修点|自动折算|帮我改点/.test(view));
  ok("D8 体检段不参与就绪门控：既有「未就绪也能录点 / 执行才要就绪」口径不变",
     /:disabled="!auth\.controlActive" @click="saveTeachAsPoint"/.test(view)
     && /:disabled="!exec\.canExec \|\| exec\.runBusy" @click="exec\.execPoint\(p\)"/.test(view));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== E. 体系接线 ==");
{
  ok("E1 本脚本已进 npm run verify 链", /verify_point_audit\.mjs/.test(pkg));
  ok("E2 体检徽标不会因为 J6 正/反面切面点而误列 6 轴坐标（切面点仍只显示 J6）",
     /isJ6Face\(p\)/.test(view) && /仅 J6 有效/.test(view));
  ok("E3 后端把坏数据说成「无法体检」而不是「没问题」（文案与前端一致口径）",
     /无法体检/.test(svc) && /不是「没问题」/.test(svc));
}

console.log("");
console.log(`== 合计：PASS=${pass}  FAIL=${fail} ==`);
process.exit(fail === 0 ? 0 : 1);
