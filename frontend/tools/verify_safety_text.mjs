// =====================================================================
// 安全围栏文案回归（headless，纯函数，无需 Vue / WebGL）
//
// 背景：安全围栏的文案有两处消费者 ——
//   ① 顶部报警条 #safety-banner（App.vue，只在危险/碰撞时出现）
//   ② 视口右下角状态片 #safety-chip（SafetyChip.vue，常驻）
// 它们读的是**同一份**评估结果。文案一旦各写一份，就会漂移成
// "报警条说危险、状态片说接近"这种自相矛盾的界面 —— 现场看到会直接不敢动。
// 所以判据收敛到 src/utils/safetyLabels.js，并由本文件逐状态钉死。
//
// 覆盖：
//  A. 四个状态 × 围栏/地面 都要有非空且互不相同的正文
//  B. 未知 / 缺字段必须**安全回退**（本项目最怕的回归：缺配置 → 全部退化成空白或绿）
//  C. 余量换算 mm 与 0 边界；区域名只在围栏分支出现
//  D. ★ 一致性铁律：正文、两字标签、悬停提示三者不许互相矛盾
//  E. 输出里不许出现 undefined / NaN / null 字面量
//
// 运行： node tools/verify_safety_text.mjs
// =====================================================================
import {
  STATE_LABEL, isGroundState,
  safetyText, safetySub, safetyLabel, safetyScope, chipTitle,
} from "../src/utils/safetyLabels.js";

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const STATES = ["safe", "warn", "danger", "hit"];
const fence = (st, clearance = 0.2, zoneName = "") =>
  ({ state: st, ratio: 0.2, clearance, zoneId: "z1", zoneName });
const ground = (st, clearance = 0.2) =>
  ({ state: st, ratio: 0.2, clearance, zoneId: "ground", zoneName: "地面" });

console.log("== 安全围栏文案回归 ==");

// ---------- A. 四状态正文齐备且互不相同 ----------
console.log("\n[A] 四个状态 × 围栏/地面 的正文");
for (const kind of ["fence", "ground"]) {
  const mk = kind === "fence" ? fence : ground;
  const texts = STATES.map((st) => safetyText(mk(st)));
  ok(kind + ": 四个状态正文都非空", texts.every((t) => typeof t === "string" && t.length > 0));
  ok(kind + ": 四个状态正文互不相同（不会两个状态显示同一句）",
    new Set(texts).size === 4, JSON.stringify(texts));
  const labels = STATES.map((st) => safetyLabel(mk(st)));
  ok(kind + ": 标签 = " + JSON.stringify(STATES.map((s) => STATE_LABEL[s])),
    labels.join(",") === STATES.map((s) => STATE_LABEL[s]).join(","), labels.join(","));
}
ok("围栏与地面在同样 state 下正文不同（不会串话术）",
  STATES.every((st) => safetyText(fence(st)) !== safetyText(ground(st))));

// ---------- B. 未知 / 缺字段必须安全回退 ----------
console.log("\n[B] 缺字段 / 未知状态的安全回退");
const BAD = [undefined, null, {}, { state: "???" }, { state: "" }, { state: "safe" }];
for (const bad of BAD) {
  const tag = JSON.stringify(bad) || String(bad);
  ok("safetyLabel(" + tag + ") 不返回空 → 回退「安全」",
    safetyLabel(bad) === "安全", "得到 " + JSON.stringify(safetyLabel(bad)));
  ok("safetyText(" + tag + ") 非空", typeof safetyText(bad) === "string" && safetyText(bad).length > 0);
  ok("safetySub(" + tag + ") 非空", typeof safetySub(bad) === "string" && safetySub(bad).length > 0);
  ok("safetyScope(" + tag + ") 非空", typeof safetyScope(bad) === "string" && safetyScope(bad).length > 0);
  ok("chipTitle(" + tag + ") 非空", typeof chipTitle(bad) === "string" && chipTitle(bad).length > 0);
  ok("isGroundState(" + tag + ") 为 false（不误判成地面）", isGroundState(bad) === false);
}

// ---------- C. 余量与区域名 ----------
console.log("\n[C] 余量换算 mm / 0 边界 / 区域名");
ok("围栏 200mm：0.2 m → 「剩约 200 mm」",
  safetySub(fence("safe", 0.2)).indexOf("剩约 200 mm") === 0, safetySub(fence("safe", 0.2)));
ok("围栏 12mm 小数四舍五入：0.0125 → 「剩约 13 mm」",
  safetySub(fence("safe", 0.0125)).indexOf("剩约 13 mm") === 0, safetySub(fence("safe", 0.0125)));
ok("围栏余量 0 → 「已越界，请立即停止」",
  safetySub(fence("hit", 0)).indexOf("已越界，请立即停止") === 0, safetySub(fence("hit", 0)));
ok("围栏负余量（已越过内表面）也走越界分支",
  safetySub(fence("hit", -0.03)).indexOf("已越界，请立即停止") === 0, safetySub(fence("hit", -0.03)));
ok("地面净高 150mm → 「最低点离台面 150 mm」",
  safetySub(ground("safe", 0.15)).indexOf("最低点离台面 150 mm") === 0, safetySub(ground("safe", 0.15)));
ok("地面净高 0 → 「已触台面，请立即停止」",
  safetySub(ground("hit", 0)).indexOf("已触台面，请立即停止") === 0, safetySub(ground("hit", 0)));
ok("围栏分支拼上区域名", safetySub(fence("warn", 0.08, "东侧围栏")).indexOf("东侧围栏") !== -1);
ok("地面分支**不**拼区域名（地面没有「区域名」这个概念）",
  safetySub(ground("warn", 0.08)).indexOf("地面") === -1, safetySub(ground("warn", 0.08)));

// ---------- D. 一致性铁律 ----------
// ★ 判据不能写成"正文含「碰撞」二字就必须是 hit"—— 危险态的正文是
//   「危险：即将碰撞安全围栏」，这里的「碰撞」说的是**后果**不是**当前状态**。
//   正确做法：把正文的**严重度**解析出来，再和标签的严重度比对。
console.log("\n[D] 正文 / 标签 / 悬停提示 严重度不许互相矛盾");
// ★ 两套链要分清：safetyLabel() 返回的是**中文**两字标签（安全/接近/危险/碰撞），
//   而内部状态键是英文（safe/warn/danger/hit）。曾经这里直接 RANK[label] 取，
//   中文键取不到值 → undefined → 分支永远走 else，断言形同虚设。
// 中文做对象键是合法 JS 标识符（不是"串里夹引号"），显式豁免本行。
const LABEL_RANK = { 安全: 0, 接近: 1, 危险: 2, 碰撞: 3 };   // cjk-ok
/** 从正文反推它宣称的严重度（只看开头/特征词，不看正文里的解释性用词）。 */
function severityOfText(t) {
  if (t.indexOf("碰撞报警") !== -1) return 3;
  if (t.slice(0, 2) === "危险") return 2;
  if (t.slice(0, 2) === "注意") return 1;
  return 0;
}
// ★ 注意：本断言曾经失败过 —— 围栏分支的 safe 掉进了 warn 的兜底 return，
//   于是"安全"状态输出的是"注意：接近…"。顶部报警条在 safe 时不显示，
//   所以线上看不出来，纯靠这条断言才把语义不自洽钉出来。
console.log("  （围栏/地面两条分支的四个状态都必须各有专属正文）");
for (const st of STATES) {
  for (const mk of [fence, ground]) {
    const s = mk(st, st === "hit" ? 0 : 0.2);
    const t = safetyText(s), l = safetyLabel(s), ti = chipTitle(s);
    const kind = mk === fence ? "围栏" : "地面";
    const lr = LABEL_RANK[l], tr = severityOfText(t);
    if (lr >= 2) {
      ok(kind + "/" + st + ": 标签=" + l + " → 正文严重度也必须是 " + lr,
        tr === lr, "正文=" + t + "（解析出 " + tr + "）");
    } else {
      ok(kind + "/" + st + ": 标签=" + l + " → 正文不得喊到危险/碰撞",
        tr <= 1, "正文=" + t + "（解析出 " + tr + "）");
    }
    // 悬停提示必须与作用域同一套二分
    ok(kind + "/" + st + ": 悬停提示与作用域一致",
      (safetyScope(s) === "地面") === (ti.indexOf("地面") !== -1), "scope=" + safetyScope(s) + " title=" + ti);
    // 作用域与悬停提示必须与 zoneId 严格同源，禁止各自判断
    ok(kind + "/" + st + ": 作用域与 isGroundState 同源",
      isGroundState(s) === (safetyScope(s) === "地面"));
  }
}
ok("标签值域封闭（只可能取四档中文标签之一）",
  STATES.map((s) => safetyLabel(fence(s)))
    .every((l) => Object.prototype.hasOwnProperty.call(LABEL_RANK, l)));
ok("标签严重度与状态严格对应（不会 hit 却显示「安全」）",
  STATES.every((st) => LABEL_RANK[safetyLabel({ state: st })] === { safe: 0, warn: 1, danger: 2, hit: 3 }[st]));
ok("地面与围栏在 hit 时是**不同**的正文（触台面 vs 触围栏）",
  safetyText(ground("hit", 0)) !== safetyText(fence("hit", 0)));
ok("safe 的围栏正文不含「危险」（不吓人）", safetyText(fence("safe")).indexOf("危险") === -1);
ok("safe 的围栏正文不含「碰撞报警」（没报警就不是报警）",
  safetyText(fence("safe")).indexOf("碰撞报警") === -1);
ok("safe 的地面正文明确说「安全」",
  safetyText(ground("safe")).indexOf("安全") === 0, safetyText(ground("safe")));

// ---------- E. 输出不含 undefined / NaN / null ----------
console.log("\n[E] 任何输入下输出都不含 undefined / NaN / null");
const DIRTY = [];
for (const st of [...STATES, "??", ""]) {
  for (const mk of [fence, ground]) {
    for (const c of [undefined, null, 0, 0.123, -0.5, NaN]) {
      const s = { state: st, ratio: 0, clearance: c, zoneId: mk === fence ? "z1" : "ground", zoneName: null };
      for (const fn of [safetyText, safetySub, safetyLabel, safetyScope, chipTitle]) {
        const out = String(fn(s));
        if (/undefined|NaN|null/.test(out)) DIRTY.push(fn.name + "(" + st + "," + c + ")=" + out);
      }
    }
  }
}
ok("无脏输出", DIRTY.length === 0, DIRTY.slice(0, 5).join(" | "));

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
