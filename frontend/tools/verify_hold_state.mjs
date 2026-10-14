// =====================================================================
// E5′「固定相机反用法 = 持件/吸空证据」守卫（headless 静态检查）
//
// 这条功能填补的是 AGENTS.md §7 明文的空白：**现场没有真空传感器**，
// "手上吸没吸住"过去只能靠流程推断。现在把本来就要抓的送检帧再读一遍当观测值 ——
// 于是最要命的四类问题也全在这里：
//   ① 把"没观测到"说成"已确认"（§5.5 铁律③）；
//   ② 观测件长出手（自动暂停 / 自动重试 / 自动急停 —— §5.1 刹车必须人点）；
//   ③ 拿"帧里有候选"就当"吸住了"（现场**红色吸盘**自己就会成一个候选）；
//   ④ 件尺寸未卡尺实测、相机未标定却报毫米（方案 §10.2：毫米换算**已后置**）。
// 所以这里既钉后端判据，也钉界面文案与接线（空跑不许产证据、双面逐面各一条）。
//
// 运行： cd frontend && node tools/verify_hold_state.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8").split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

/** 剥块注释 / HTML 注释 / 整行 //（注释里写"不会自动暂停"不该把红线判红）。 */
function stripComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}

/** Python：只在字符串外遇到 # 才砍到行尾（判"真有没有调用"，不能被注释里提一句吓住）。 */
function stripPy(s) {
  let out = "", q = null, i = 0;
  while (i < s.length) {
    const c = s[i];
    if (q) {
      out += c;
      if (c === "\\") { out += s[i + 1] || ""; i += 2; continue; }
      if (s.startsWith(q, i)) { out += q; i += q.length; q = null; continue; }
      i++; continue;
    }
    if (c === "#") { while (i < s.length && s[i] !== "\n") i++; continue; }
    if (c === '"' || c === "'") {
      q = s.startsWith(c.repeat(3), i) ? c.repeat(3) : c;
      out += q; i += q.length; continue;
    }
    out += c; i++;
  }
  return out;
}

const hs = read("../backend/app/services/hold_state.py");
const hsCode = stripPy(hs);
const api = read("../backend/app/api/sorting.py");
const apiCode = stripPy(api);
const sys = read("../backend/app/api/system.py");
const svcSort = read("../backend/app/services/sorting.py");
const cfg = read("../config/robot.yaml");
const tests = read("../backend/tests/test_hold_state.py");
const comp = read("src/components/HoldEvidence.vue");
const compCode = stripComments(comp);
const sort = read("src/components/SortingView.vue");
const ai = read("src/components/AiControlView.vue");
const pkg = read("package.json");

// ---------------------------------------------------------------------
console.log("");
console.log("== A. 后端判据：未知≠确认 · 单向收紧 ==");
{
  ok("A1 三档只有三个词（confirmed/inferred/contradiction），界面不许另造",
     /STATES = \(CONFIRMED, INFERRED, CONTRADICTION\)/.test(hs) &&
     /CONFIRMED = "confirmed"/.test(hs) && /INFERRED = "inferred"/.test(hs) &&
     /CONTRADICTION = "contradiction"/.test(hs));
  ok("A2 ★ 拿不到帧 → inferred，且 present 保持 None（未知不塌成布尔，铁律③）",
     /return \{"state": INFERRED,/.test(hs) &&
     /"present": val,/.test(hs) &&
     /if present:\n\s+val: Optional\[bool\] = True/.test(hs));
  ok("A3 ★ absent 只在相机明确回了空结果时给出；传输失败/结构异常一律 unknown",
     /if f\.get\("ok"\) is False:[\s\S]{0,160}"kind": "unknown"/.test(hs) &&
     /if not results:\n\s+return \{"kind": "absent"/.test(hs));
  ok("A4 ★ 单向收紧：absent 混着 unknown → 不报矛盾（不许把没读到说成没有）",
     /elif n and absent == n:\n\s+val = False/.test(hs) &&
     /else:\n\s+val = None/.test(hs));
  ok("A5 ★「帧里有候选」不等于吸住了：确认必须是 判色成立 且 多帧面积一致",
     /def judge_supported\([\s\S]{0,900}return bool\(vote_ok\)/.test(hs) &&
     /presence\.get\("present"\) is not True/.test(hs));
  ok("A6 吸盘歧义在判据里写进了 note（界面因此不用自己复述理由）",
     /吸盘/.test(hs));
  ok("A7 面积一致带上限来自配置（不是硬编码）",
     /_cfg\(\)\["area_ratio_max"\]/.test(hs) || /area_ratio_max else _cfg\(\)/.test(hs));
  ok("A8 正反两向共用同一个判据（放件后复拍也走 hold_state_of，不另写一套）",
     (hs.match(/def hold_state_of/g) || []).length === 1 &&
     /if expect_present:\n\s+if supported:/.test(hs));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== B. 观测件不许长出手（方案点名的红线）==");
{
  ok("B1 不 import 任何写路径（motion/modbus/jog/rc_ready/sim_robot/camera_client）",
     !/from app\.services\.(motion|modbus|jog|rc_ready|sim_robot)/.test(hsCode) &&
     !/camera_client/.test(hsCode));
  ok("B2 源码里没有任何下发/急停/取消调用（剥注释后判）",
     !/\.command\(|rc_jog|rc_vacuum|estop\(|get_json\(/.test(hsCode));
  ok("B3 快照里没有任何「该做什么」的指令字段",
     !/"(action|should|next|directive|advice|instruct)"\s*:/.test(hsCode));
  ok("B4 矛盾 → 一条 warn 事件（category=safety / level=warn），且不发任何其他级别",
     /emit_event\("safety", "warn", "hold_state_contradiction"/.test(hs));
  ok("B5 事件发射失败被吞掉（观测件绝不把主流程带崩）",
     /except Exception as e:[^\n]*\n\s*log\.debug\("持件证据事件发射失败/.test(hs));
  ok("B6 note 明写「不会自动暂停 / 不会重试 / 不会急停」+「不是传感器读数」",
     /不会自动暂停/.test(hs) && /不会急停/.test(hs) && /真空传感器/.test(hs));
  ok("B7 ★ 不给毫米：快照字段只允许像素（换算前提未具备，方案 §10.2 后置）",
     /"area_px"/.test(hsCode) &&
     !/_mm\b/.test(hsCode.replace(/platform_height_mm/g, "")) &&
     !/standoff/.test(hsCode));
  ok("B8 历史是环形 + id 单调（水位/取证包靠它）",
     /deque\(maxlen=/.test(hs) && /self\._seq \+= 1/.test(hs));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== C. 接线：只复用已有帧 · 空跑不产证据 ==");
{
  ok("C1 判色咽喉之后登记证据（_capture 里 _hold_record）",
     /v = S\.vote_color\(got, min_conf, valid_colors=valid_colors\)[\s\S]{0,200}_hold_record\(stage, got, v, st\)/.test(api));
  ok("C2 ★ 只消费已经抓到的那几帧：不新增一次 get_json",
     (apiCode.match(/_capture\(|_capture_per_face\(/g) || []).length >= 1 &&
     /def _hold_record\(source: str, frames: List\[Dict\], vote/.test(api) &&
     !/def _hold_record[\s\S]{0,700}get_json/.test(apiCode));
  ok("C3 登记包在 try 里：观测件坏了照常判色",
     /def _hold_record[\s\S]{0,900}except Exception[\s\S]{0,160}log\.debug\("持件证据登记失败/.test(api));
  ok("C4 ★ 双面 = 每面各登记一条（两件各自吸没吸住不能被合成一条）",
     /for _slot, _grp, _v in zip\(list\(faces\), groups, vs\):\n\s+_hold_record\("%s·%s" % \(stage, _slot\), _grp, _v, st\)/.test(api));
  ok("C5 ★ 空跑 / 双闸未开不产证据：两处采集调用都在 `if not judge:` 的 else 分支里",
     apiCode.includes("if not judge:") &&
     /else:[\s\S]{0,1600}v = _capture\(ctx, int\(body\.frames\)/.test(apiCode) &&
     /else:[\s\S]{0,2600}verdicts = _capture_per_face\(/.test(apiCode));
  ok("C6 证据同时落进流程 state（hold_evidence）→ 与「手上有件」同一份 state",
     /_set\(st, hold_evidence=\{/.test(api));
  ok("C7 state 里初始化了 hold_evidence=None（前端不会把 undefined 当成没证据就是正常）",
     /"hold_evidence": None,/.test(svcSort));
  ok("C8 import 顺序上不经任何写路径模块拿证据（只 import hold_state）",
     /from app\.services\.hold_state import hold_state/.test(api));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== D. 只读接口与配置 ==");
{
  ok("D1 GET /api/system/metrics/hold-state 存在且直接返回那份 state()",
     /@router\.get\("\/metrics\/hold-state"\)/.test(sys) &&
     /return hold_state\.state\(\)/.test(sys));
  ok("D2 该接口**不要求控制令牌**（观测不属于控制）",
     !/@router\.get\("\/metrics\/hold-state"\)[\s\S]{0,200}Depends\(require_/.test(sys));
  ok("D3 D5′：立足面高度由后端给（前端 three/cell.js 与后端 A1 读同一个数）",
     /@router\.get\("\/metrics\/platform"\)/.test(sys) &&
     /platform_height_mm/.test(sys));
  ok("D4 config/robot.yaml 有 hold_state 段（enabled / history_max / area_ratio_max）",
     /^hold_state:/m.test(cfg) && /history_max:/.test(cfg) && /area_ratio_max:/.test(cfg));
  ok("D5 配置注释写清「不新增拍摄、不新增移动」与「不会自动暂停/重试/急停」",
     /不新增拍摄/.test(cfg) && /不新增移动/.test(cfg) && /不会自动暂停/.test(cfg));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== E. 界面：三档诚实显示 ==");
{
  ok("E1 组件只吃 props（不 fetch、不轮询第二路 → 同屏同源）",
     !/fetch\(|axios|apiControl|apiUrl/.test(compCode) &&
     /defineProps\(\{/.test(comp));
  ok("E2 ★ 没有证据时明说「未观测」，不显示绿色确认",
     /v-else-if="holding"/.test(comp) && /未观测/.test(comp) &&
     /不等于<\/b>\**已吸住|\*\*不等于\*\*已吸住/.test(comp));
  ok("E3 ★ 未知档位（认不出的 state）落回 inferred —— 绝不塌成 confirmed",
     /STATE\[e\.state\] \|\| STATE\.inferred/.test(comp));
  ok("E4 三档文案：确认写成「有视觉证据支持」而不是已确认已吸住",
     /有视觉" \+ EV \+ "支持|有视觉证据支持/.test(comp) &&
     !/已确认吸住|确定吸住/.test(comp));
  ok("E5 矛盾 = 红 + alert 图标；确认 = 绿；仅推断 = 灰（三种颜色三种语义）",
     /confirmed:[^}]*cls: "ok"/.test(comp) && /contradiction:[^}]*cls: "err"/.test(comp) &&
     /inferred:[^}]*cls: "muted"/.test(comp));
  ok("E6 ★ 组件里没有任何动作入口（无 POST / 无急停 / 无重试按钮）",
     !/method:\s*"POST"|\/control\/|\/sorting\/|estop|<button/.test(compCode));
  ok("E7 空跑如实标注「本轮不产持件证据」（不假装观测过）",
     /空跑/.test(comp) && /不产持件证据/.test(comp));
  ok("E8 挂在智能分拣页（与「手上有件」同一份 state）",
     /import HoldEvidence/.test(sort) && /<HoldEvidence :evidence="holdEv" :holding="handHold" :dry="isDry" \/>/.test(sort) &&
     /st\.value\.hold_evidence/.test(sort));
  ok("E9 AI 执行页转述同一档位（数字/判据都取自后端，不复述判据）",
     /s\.hold_evidence && s\.hold_evidence\.state_label/.test(ai) &&
     /持件证据：\$\{s\.hold_evidence\.state_label\}/.test(ai));
  ok("E10 界面不承诺传感器：出现「真空传感器」处不写成实测读数那一方向",
     !/真空传感器已确认|传感器实测/.test(compCode));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== F. 体系接线 ==");
{
  ok("F1 后端回归钉覆盖方案点名的两条 + 单向收紧 + 不给毫米",
     /def test_no_camera_never_reports_confirmed/.test(tests) &&
     /def test_empty_frame_after_suck_is_contradiction_not_safe/.test(tests) &&
     /def test_absent_mixed_with_unknown_is_not_contradiction/.test(tests) &&
     /def test_candidate_without_vote_is_not_confirmation/.test(tests) &&
     /def test_no_millimetre_field_in_snapshot/.test(tests));
  ok("F2 钉住了「空跑不登记」与「双面逐面登记」",
     /def test_dry_run_never_records_evidence_source_pin/.test(tests) &&
     /def test_dual_face_records_one_evidence_per_face/.test(tests));
  ok("F3 钉住了「只读接口不要令牌」",
     /def test_endpoint_is_readonly\(client\)/.test(tests));
  ok("F4 本脚本已进 npm run verify 链", /verify_hold_state\.mjs/.test(pkg));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
console.log("");
process.exit(fail ? 1 : 0);
