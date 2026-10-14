// =====================================================================
// E2「到位自证（段级残差带）」前端接线守卫（headless 静态检查）
//
// 这个功能的全部价值就是"每一步都能自证"，所以界面这一侧最不能犯的错，
// 恰好是本项目反复踩过的四类：
//   ① **水位用数组长度 / 对象引用** —— 残差帧每拍都是新对象，用引用或 length 做
//      "变了没"的判据会悄悄不再更新（§5.5 铁律①，log.length 同一个坑）；
//   ② **未知塌成"正常"** —— `no_data / aborted / 认不出的判据` 若染成绿，
//      就等于"没观测到"冒充"已到位"（铁律③）；
//   ③ **把上位机自己的采样延迟说成机器不准** —— "带"必须含采样不确定度，
//      界面也得把它如实标出来（°，不是 mm）；
//   ④ **观测件长出手** —— 这张卡里绝不能出现任何下发 / 暂停 / 重试 / 急停的入口，
//      刹车必须人点（AGENTS.md §5.1）。
// 另外钉后端那条"只读"链路：不 import 写路径、stale 帧不喂、同屏同源。
//
// 运行： cd frontend && node tools/verify_twin_residual.mjs
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

/** 剥掉块注释 / HTML 注释 / 整行 // —— 注释里写"不会自动暂停"不该把静态钉判红。 */
function stripComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}

/** Python 版：逐字扫描，只在**字符串外**遇到 # 才砍到行尾。
 *  A2 那条红线（观测件不许长出手）判的是"真有没有调用"，
 *  拿 `注释里提到 rc_jog_execute` 判红是假阳性，拿它放过真调用是假阴性 —— 都不行。 */
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

const svc = read("../backend/app/services/twin_residual.py");
const svcCode = stripPy(svc);
const api = read("../backend/app/api/robot.py");
const col = read("../backend/app/services/collector.py");
const mot = read("../backend/app/services/motion.py");
const cfg = read("../config/robot.yaml");
const store = read("src/stores/robot.js");
const card = read("src/components/TwinResidualCard.vue");
const cardCode = stripComments(card);
const live = read("src/components/RealMonitor.vue");
const point = read("src/components/PointExecView.vue");
const pkg = read("package.json");

// 只截 applyPose 那一段（store 里还有别的功能）
const apStart = store.indexOf("applyPose(p)");
const apEnd = store.indexOf("setDemo(on");
const apply = apStart >= 0 ? store.slice(apStart, apEnd > apStart ? apEnd : apStart + 1500) : "";

// ---------------------------------------------------------------------
console.log("");
console.log("== A. 后端：观测件不许长出手（方案点名的红线）==");
{
  ok("A1 不 import 任何写路径模块（motion/modbus/jog/sim_robot/rc_ready）",
     !/from app\.services\.(motion|modbus|jog|sim_robot|rc_ready)/.test(svcCode) &&
     !/import app\.services\.(motion|modbus|jog|sim_robot)/.test(svcCode));
  ok("A2 源码里没有急停/取消/下发/写寄存器一类调用（剥注释后判）",
     !/estop\(|reset_estop\(|\.command\(|write_reg|rc_jog|sim_robot\./.test(svcCode),
     "出现动作类调用 = 观测件变成了闸");
  ok("A3 快照字段里没有任何「该做什么」的指令字段",
     !/"(action|should|next|directive|advice)"\s*:/.test(svcCode));
  ok("A4 超差只发**一条** warn 事件（每段最多一条）",
     /if seg\.warned or not self\._events_enabled:\s*\n\s*return/.test(svc) &&
     /seg\.warned = True/.test(svc));
  ok("A5 事件发射失败被吞掉（观测件绝不把主流程带崩）",
     /except Exception as e:\s*(#[^\n]*)?\n\s*log\.debug\("到位自证事件发射失败/.test(svc));
  ok("A6 时间由调用方传入（可确定性重放，不拿 time.time() 当判据）",
     /def begin\([^)]*at: Optional\[float\] = None[^)]*\)/.test(svc) &&
     /def observe\([^)]*at: Optional\[float\] = None[^)]*\)/.test(svc));
  ok("A7 一帧实测都没拿到 → no_data（不猜「到位」两个字）",
     /seg\.verdict = "no_data"/.test(svc));
  ok("A8 被打断（急停/新指令顶掉）单独判 aborted，不与「机器没到位」混",
     /reason in \("superseded", "aborted", "estop"\)/.test(svc));
  ok("A9 J6 用环形最短弧（整圈等价 = 同一姿态，2026-10-11 事故口径）",
     /def ring_diff_deg/.test(svc) && /%\s*360\.0/.test(svc) &&
     /i == 5/.test(svc));
  ok("A10 不确定带含「一拍采样延迟」（rate×period），不把延迟说成机器不准",
     /seg\.rate_est \* period/.test(svc) && /_sample_period\(\)/.test(svc));
  ok("A11 带宽封顶且从**本段**速率起算（跨段单调 max 会把带永久撑宽 = 假自证）",
     /min\(seg\.rate_est \* period,/.test(svc) && /band_max_deg/.test(svc) &&
     !/self\._rate_est = max\(self\._rate_est, seg\.rate_est\)/.test(svc));
  ok("A12 点动顶掉的微段不进历史（40 条环形不被刷屏）",
     /reason == "superseded" and seg\.n < 2/.test(svc));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== B. 口径进配置（不是硬编码在代码里）==");
{
  ok("B1 config/robot.yaml 有 twin_residual 段", /^twin_residual:/m.test(cfg));
  ok("B2 容差/窗口/封顶都在配置里（tol_deg / start_timeout_ms / band_max_deg）",
     /tol_deg:/.test(cfg) && /start_timeout_ms:/.test(cfg) && /band_max_deg:/.test(cfg));
  ok("B3 配置注释写清「只读观测 · 不自动暂停/重试/急停」与「°不是 mm」",
     /只读观测/.test(cfg) && /不自动暂停/.test(cfg) && /关节角差/.test(cfg));
  ok("B4 观测窗口不超过下发上限（≤30s）",
     /max_ms:\s*(\d+)/.test(cfg) && Number(cfg.match(/max_ms:\s*(\d+)/)[1]) <= 30000);
}

// ---------------------------------------------------------------------
console.log("");
console.log("== C. 采集器接线：只喂新鲜帧 + 同屏同源 ==");
{
  ok("C1 每拍喂帧（twin_observe(joints, loop_start)）",
     /twin_observe\(joints, loop_start\)/.test(col));
  ok("C2 ★ stale 帧不喂（冻结的旧姿态会伪装「停止收敛」）",
     /if not read_stale:\s*\n\s*twin_observe\(joints, loop_start\)/.test(col));
  ok("C3 喂帧异常被吞掉（观测坏了不带的崩采集）",
     /def twin_observe\(joints, at\)/.test(col) &&
     /except Exception as e:[^\n]*\n\s*log\.debug\("到位自证喂帧失败/.test(col));
  ok("C4 残差快照并入**同一条位姿帧**（前端不再另开一路轮询）",
     /payload\["residual"\] = residual_seg/.test(col) &&
     /residual_seg = twin_residual\.ws_view\(\)/.test(col));
  ok("C5 WS 只送紧凑快照（不含 profile 曲线，曲线走 REST）",
     /def ws_view/.test(svc) && /WS_KEYS = \([^)]*\)/.test(svc) &&
     !/WS_KEYS = \([^)]*profile[^)]*\)/.test(svc));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== D. 执行引擎：只登记段边界，不改控制语义 ==");
{
  ok("D1 下发前登记 begin（带来源 source，现场对账用）",
     /self\._twin_begin\(self\.last_target, speed_pct, source\)/.test(mot));
  ok("D2 真机分支收尾 done/aborted（与下发结果一致，不粉饰）",
     /self\._twin_end\("done" if ok else "aborted"\)/.test(mot));
  ok("D3 失败/异常路径也收尾（不留僵尸段）",
     (mot.match(/self\._twin_end\("aborted"\)/g) || []).length >= 3);
  ok("D4 急停那一刻如实收尾为 estop", /self\._twin_end\("estop"\)/.test(mot));
  ok("D5 begin/end 都包在 try 里：观测件坏了**照常下发**",
     /def _twin_begin[\s\S]{0,240}except Exception[\s\S]{0,120}log\.debug\("到位自证登记失败/.test(mot) &&
     /def _twin_end[\s\S]{0,200}except Exception[\s\S]{0,120}log\.debug\("到位自证收尾失败/.test(mot));
  ok("D6 段收尾在 motion._lock **之外**（缩进不比锁头部更深 = 不在锁体内，不给观测件引入嵌套锁路径）",
     (() => {
       const at = mot.indexOf('self._twin_end("done" if ok else "aborted")');
       const head = mot.lastIndexOf("with self._lock:", at);
       if (at < 0 || head < 0) return false;
       const ind = (i) => i - mot.lastIndexOf("\n", i) - 1;
       return ind(at) <= ind(head) && mot.slice(at, at + 220).includes("fail is not None");
     })());
  ok("D7 source 是**可选关键字参数**（老调用点一行没改也能跑）",
     /abort_check: Optional\[Callable\[\[\], bool\]\] = None,\n\s*source: str = ""\) -> dict:/.test(mot));
  ok("D8 分拣唯一咽喉把点位名传进 source（哪一段卡住现场看得见）",
     /abort_check=lambda: ctx\.cancelled\(\), source=name/.test(read("../backend/app/api/sorting.py")));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== E. 只读接口 ==");
{
  ok("E1 GET /api/twin/residual 存在", /@router\.get\("\/twin\/residual"\)/.test(api));
  ok("E2 该接口**不要求控制令牌**（观测不属于控制）",
     /@router\.get\("\/twin\/residual"\)\s*\ndef twin_residual_state\(\):/.test(api) &&
     !/@router\.get\("\/twin\/residual"\)[\s\S]{0,120}require_control/.test(api));
  ok("E3 接口返回的就是那份 state()（同屏同源，不另算一套）",
     /return twin_residual\.state\(\)/.test(api));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== F. store：水位单调 + 空帧如实清空 ==");
{
  ok("F1 有 residualSeq 且**只增不减**（不用 length / 对象引用做判据）",
     /residualSeq: 0/.test(store) && /this\.residualSeq \+= 1/.test(store) &&
     !/this\.residualSeq =/.test(apply.replace(/\+= 1/, "")));
  ok("F2 变化判据用 (段 id, 采样帧数) —— 两个都单调递增",
     /r\.id !== prev\.id \|\| r\.samples !== prev\.samples/.test(apply));
  ok("F3 帧里没有 residual → 清空（上一段的「已到位」不留在屏上冒充事实）",
     /this\.residual = r;/.test(apply) &&
     /p\.residual && typeof p\.residual === "object" \? p\.residual : null/.test(apply));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== G. 卡片：诚实显示 ==");
{
  ok("G1 判据文案齐全（arrived/slow/overshoot/stuck/not_reached/no_data/aborted）",
     /arrived:/.test(card) && /slow:/.test(card) && /overshoot:/.test(card) &&
     /stuck:/.test(card) && /not_reached:/.test(card) && /no_data:/.test(card) &&
     /aborted:/.test(card));
  ok("G2 ★ 未知/无数据/已中止 = 虚线灰，**不塌成绿**（铁律③）",
     /\.v-unknown \{[^}]*border-style: dashed/.test(card) &&
     /function verdictCls\(v\)[\s\S]{0,160}return hit \? `v-\$\{hit\[1\]\}` : "v-unknown"/.test(card));
  ok("G3 认不出的判据显示「未知」而不是「正常」",
     /return hit \? hit\[0\] : "未知";/.test(card));
  ok("G4 残差单位写 °（关节角差），不写 mm、不叫「轨迹跟踪精度」",
     /当前 <b>\{\{ num\(live\.last_deg\) \}\}°/.test(card) &&
     !/mm/.test(cardCode.replace(/不是 mm|mm）/g, "")) &&
     !/轨迹跟踪精度/.test(cardCode));
  ok("G5 带 = 容差 + 采样不确定度 在界面上如实标出",
     /带 ± <b>\{\{ num\(live\.band_deg\) \}\}°<\/b>/.test(card) &&
     /采样[^]{0,8}不确定度/.test(card));
  ok("G6 没有实测帧时显示「无实测数据」，不显示「到位」",
     /no_data: \["无实测数据", "unknown"\]/.test(card));
  ok("G7 ★ 卡片里没有任何动作入口（不下发/不暂停/不重试/不急停）—— 刹车必须人点",
     !/apiControl|apiPost|method:\s*"POST"|\/control\/|\/sorting\/|estop|\/jog/.test(cardCode));
  ok("G8 历史只读 GET（fetch apiUrl → /twin/residual），失败时如实报错不静默",
     /fetch\(apiUrl\("\/twin\/residual"\)\)/.test(cardCode) &&
     /histErr\.value = `历史读取失败（HTTP \$\{res\.status\}）`/.test(card) &&
     /<p v-if="histErr" class="tr-err">\{\{ histErr \}\}<\/p>/.test(card));
  ok("G9 超差时明说「系统不会自动暂停/重试/急停 —— 要不要停由你点」",
     /不会<\/b>自动暂停 \/ 重试 \/ 急停/.test(card));
  ok("G10 口径文案来自后端 note（同一屏同一结论，不在前端复述一套）",
     /\{\{ meta\.note \}\}/.test(card) &&
     /口径未加载/.test(card));
  ok("G11 空态不假装全绿：没有在飞段就明说",
     /当前没有在飞的下发段/.test(card) && /还没有已收尾的下发段（本卡不会自己编故事）/.test(card));
  ok("G12 挂在「真实监控」与「点位执行」两页（下发处就近可见）",
     /<TwinResidualCard \/>/.test(live) && /<TwinResidualCard \/>/.test(point));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== H. 体系接线 ==");
{
  ok("H1 后端回归钉存在且覆盖方案点名的四条",
     (() => {
       try {
         const t = readFileSync(join(root, "../backend/tests/test_twin_residual.py"), "utf8");
         return /def test_stuck_arm_is_detected/.test(t) &&
                /def test_sampling_lag_alone_is_not_flagged/.test(t) &&
                /def test_j6_ring_difference_is_shortest_arc/.test(t) &&
                /def test_never_emits_auto_stop/.test(t);
       } catch (e) { return false; }
     })());
  ok("H2 本脚本已进 npm run verify 链", /verify_twin_residual\.mjs/.test(pkg));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
console.log("");
process.exit(fail ? 1 : 0);
