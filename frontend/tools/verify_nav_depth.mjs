// =====================================================================
// ★★ C4「四层导航 + 报警 ≤3 击到达能采取行动的画面」守卫（headless 静态交叉核对）
//
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §C4
//
// 这个脚本盯的是**两份表必须互相对得上**（本项目最容易烂的地方就是"文档/表/代码各说各话"）：
//   后端 `services/alarms.py` 登记的报警规则  ←→  前端 `src/nav/deep.js` 的去处映射表
// 具体钉五件事：
//   ① 该有去处的**一条都不能缺**（safety=True 或 P_CRITICAL / P_HIGH —— 这些是要人动手的）；
//   ② 表里**不许有后端没有的规则**（死条目 = 点了没反应的老毛病）；
//   ③ 去处必须指向真实存在的 tab（`src/tabs.js` 派生，不许抄第二份 key 列表）；
//   ④ 深度封顶 3 击；没有去处的必须**如实返回 null**，界面按 null 显示而不是硬编一个；
//   ⑤ 界面真的渲染了入口（不是只有表）、无权限时是"禁用+说明"而不是"静默失败"。
//
// 运行： cd frontend && node tools/verify_nav_depth.mjs
// =====================================================================
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(root, "..");
const lf = (s) => s.split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));
const read = (rel) => lf(readFileSync(join(root, rel), "utf8"));
/** 读**仓库根**下的文件（后端在 frontend 的上一级；用 read() 会拼成 frontend/backend 而 ENOENT）。 */
const readRepo = (rel) => lf(readFileSync(join(repo, rel), "utf8"));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const AL = readRepo("backend/app/services/alarms.py");
const DEEP = read("src/nav/deep.js");
const TABS = read("src/tabs.js");
const EV = read("src/components/EventsView.vue");
const APP = read("src/App.vue");
const ROBOT = read("src/stores/robot.js");
const PKG = read("package.json");

// ---------------------------------------------------------------- 后端规则集
// `_R(Rule("id", P_CRITICAL, …, safety=True))` 与 `Rule(id="rc.alarm.%d" % code, …)` 两种写法都要抓到。
const rules = [];
for (const m of AL.matchAll(/Rule\(\s*(?:id\s*=\s*)?"([^"]+)"\s*,\s*(P_[A-Z]+)/g)) {
  rules.push({ id: m[1], pr: m[2] });
}
const codeRules = /Rule\(id="rc\.alarm\.%d" % code, priority=priority/.test(AL);
ok("A1 后端规则抓到条数（两种 Rule(...) 写法都覆盖）", rules.length >= 25, `共 ${rules.length} 条`);
ok("A2 报警码派生规则（rc.alarm.<code>）识别到了 —— 它靠数量不定的一族，必须走前缀兜底", codeRules);
// safety=True 的规则（逐条抓 `Rule("id", … safety=True`，允许中间跨行）
const safetyIds = new Set();
for (const m of AL.matchAll(/Rule\(\s*(?:id\s*=\s*)?"([^"]+)"[\s\S]{0,400}?safety=True/g)) {
  safetyIds.add(m[1]);
}
const mustHave = new Set(rules.filter((r) => r.pr === "P_CRITICAL" || r.pr === "P_HIGH").map((r) => r.id));
for (const id of safetyIds) mustHave.add(id);
ok("A3 判据本身可用：safety=True 的规则非空、P_CRITICAL/P_HIGH 非空",
   safetyIds.size >= 8 && mustHave.size >= 12,
   `safety=${safetyIds.size} 必办=${mustHave.size}`);

// ---------------------------------------------------------------- 前端去处表
const tableIds = [...DEEP.matchAll(/^\s{2}"([a-z0-9_.]+)":\s*\{\s*view:/gm)].map((m) => m[1]);
const tableSet = new Set(tableIds);
const prefixes = [...DEEP.matchAll(/\["([a-z_.]+)",\s*\{\s*view:/g)].map((m) => m[1]);

ok("B1 去处表非空（当前 " + tableIds.length + " 条）", tableIds.length >= 20);
const missing = [...mustHave].filter((id) => {
  if (tableSet.has(id)) return false;
  // rc.alarm.<code> 这一族由前缀兜底；其余必须逐条在册
  return !prefixes.some((p) => id.startsWith(p));
});
ok("B2 ★ 要人动手的报警（safety=True 或 CRITICAL/HIGH）**一条都不能没有去处**",
   missing.length === 0, missing.join(", "));
const dead = [...tableSet].filter((id) => !rules.some((r) => r.id === id));
ok("B3 表里不许有后端不存在的规则（死条目 = 点了没反应）", dead.length === 0, dead.join(", "));
const noTarget = [...DEEP.matchAll(/^\s{2}"([a-z0-9_.]+)":\s*\{\s*view:\s*"([a-z]+)",\s*card:\s*"([^"]*)",\s*act:\s*"([^"]*)"/gm)];
ok("B4 每条去处都写清了 view + 要看哪张卡 + 第 3 击点什么（三样都不许空）",
   noTarget.length === tableIds.length
   && noTarget.every((m) => m[2] && m[3] && m[4]));

// ---------------------------------------------------------------- tab 与四层
const tabKeys = [...TABS.matchAll(/key:\s*"([a-z]+)"/g)].map((m) => m[1]);
const badView = [...DEEP.matchAll(/view:\s*"([a-z]+)"/g)].map((m) => m[1])
  .filter((v) => !tabKeys.includes(v));
ok("C1 去处指向的 tab 全部真实存在（tabs.js 是唯一来源，没抄第二份 key 列表）",
   /import \{ TABS \} from "\.\.\/tabs\.js"/.test(DEEP)
   && /TABS\.map\(\(t\) => t\.key\)/.test(DEEP) && badView.length === 0, badView.join(", "));
ok("C2 L1 总览页 = tabs.js 的 HOME_VIEW（不许两份口径）",
   new RegExp(`export const L1_VIEW = "(\\w+)"`).test(DEEP)
   && RegExp.$1 === (TABS.match(/export const HOME_VIEW = "(\w+)"/) || [])[1]);
ok("C3 深度封顶 3 击（jumpFor 为空 / tab 不存在时返回 null，不返回假深度）",
   /return 3;/.test(DEEP) && /if \(!j \|\| !j\.exists\) return null/.test(DEEP));
ok("C4 没有去处一律 null（jumpFor 三条出口：命中 / 前缀 / null），且注释写清「不许硬编去处」",
   /const hit = ALARM_TARGETS\[id\]/.test(DEEP) && /return null;/.test(DEEP)
   && /jumpFor\(\) 返回 null/.test(DEEP));

// ---------------------------------------------------------------- 界面真的接线了
ok("D1 报警台账真的渲染了「去处理」入口（不是只有表）",
   /去处理/.test(EV) && /jumpFor\(/.test(EV));
ok("D2 无去处时**如实说明**（不硬编一个跳过去的按钮）",
   /没有可直接行动的页面|这条没有去处|无直接行动页/.test(EV));
ok("D3 跳转带上上下文（ruleId / seq / zone），存在 robot store 里而不是 URL 参数假装",
   /navContext|setNavContext|navFrom/.test(ROBOT) && /navContext|setNavContext|navFrom/.test(EV));
ok("D4 目标页需要令牌而当前没令牌 → 禁用并说明（不是点了没反应）",
   /viewNeeds\(/.test(EV) && /(disabled|不能点|需要)/.test(EV));
ok("D5 ★ 急停 / 停止这两类刹车**永远可点**（C4 只加入口，没给刹车加门控）",
   !/estop[\s\S]{0,200}(rc\.ready|canControl)/.test(APP));

// ---------------------------------------------------------------- 在链上
ok("E1 本脚本已并入 npm run verify 链（不挂链 = 没钉）",
   /node tools\/verify_nav_depth\.mjs/.test(PKG)
   && existsSync(join(root, "tools", "verify_nav_depth.mjs")));

console.log(`\n  导航深度（C4）守卫：${pass} 过 / ${fail} 败`);
process.exit(fail ? 1 : 0);
