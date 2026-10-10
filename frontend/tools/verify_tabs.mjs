/**
 * headless 回归：顶部导航的权限门控（src/tabs.js）
 *
 * 背景：用户明确要求「没拿到权限把点位执行和程序执行隐藏掉，拿到权限再显示」，
 *       追加「运维审计也隐藏掉」。这类"显隐"逻辑一旦改错，方向有两种，都很难在
 *       开发机上发现：
 *         - 藏少了 → 现场点进执行页，看到一片灰按钮 / 一串 403，以为系统坏了；
 *         - 藏多了 → 用户明明已经授权，却找不到功能入口。
 *       所以把门控抽成纯函数，在这里把每一种权限组合下的可见集合钉死。
 *
 * 运行：node tools/verify_tabs.mjs     （已挂在 npm run verify 末尾）
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

// ★ Windows 下动态 import 必须走 file:// URL —— 直接给 "D:\...\tabs.js" 会被当成
//   scheme "d:" 而报 ERR_UNSUPPORTED_ESM_URL_SCHEME。
const {
  TABS, HOME_VIEW, tabAllowed, visibleTabs, allowedKeys, safeView, isKnownView,
  navTabs,
} = await import(pathToFileURL(path.join(ROOT, "src", "tabs.js")).href);

let pass = 0, fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; console.log("  PASS  " + msg); }
  else { fail++; console.log("  FAIL  " + msg); }
}
function eq(actual, expected, msg) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  ok(a === b, `${msg}${a === b ? "" : `  (期望 ${b}，实际 ${a})`}`);
}
function section(t) { console.log("\n== " + t + " =="); }

/** 造一个 auth store 快照：controlActive / isAdmin 与真实 getter 的语义保持一致。 */
function auth({ token = false, role = "", expired = false } = {}) {
  const controlActive = !!token && !expired;
  return { token: token ? "t" : "", role, controlActive, isAdmin: controlActive && role === "admin" };
}
const NONE = auth();                              // 无令牌
const OP = auth({ token: true, role: "operator" }); // 操作员
const ADMIN = auth({ token: true, role: "admin" }); // 管理员
const EXPIRED_ADMIN = auth({ token: true, role: "admin", expired: true }); // 管理员令牌已过期

const keys = (a) => allowedKeys(a);

// ---------------------------------------------------------------------
section("A. Tab 定义自洽性");
// ---------------------------------------------------------------------
const allKeys = TABS.map((t) => t.key);
eq(new Set(allKeys).size, allKeys.length, "A1 tab key 无重复");
ok(allKeys.includes(HOME_VIEW), `A2 HOME_VIEW(${HOME_VIEW}) 必须在 TABS 中`);
ok(allKeys[0] === HOME_VIEW, "A3 HOME_VIEW 排在第一位（面板默认落点）");
ok(!TABS.find((t) => t.key === HOME_VIEW).needAuth
   && !TABS.find((t) => t.key === HOME_VIEW).needAdmin, "A4 HOME_VIEW 不需要任何权限（永远可达）");
ok(TABS.every((t) => t.key && t.icon && t.label), "A5 每个 tab 都有 key/icon/label");
// needAdmin 是"更严的一档"：声明了它的条目，操作员必须关闭、管理员必须开放。
// （比检查"有没有同时写 needAuth"更有意义 —— 后者只是写法，这里是真实语义。）
ok(TABS.filter((t) => t.needAdmin).every((t) => tabAllowed(t, OP) === false && tabAllowed(t, ADMIN) === true),
   "A6 每个 needAdmin 条目：操作员关闭 / 管理员开放（admin 档隐含 auth 档）");

// ---------------------------------------------------------------------
section("B. 图标存在性（Icon.vue 里没有的 name 会静默渲染空 SVG）");
// ---------------------------------------------------------------------
const iconSrc = fs.readFileSync(path.join(ROOT, "src/components/Icon.vue"), "utf8");
const iconBlock = iconSrc.match(/const ICONS = \{([\s\S]*?)\n\};/);
ok(!!iconBlock, "B1 解析到 Icon.vue 的 ICONS 表");
if (iconBlock) {
  const iconKeys = new Set(
    [...iconBlock[1].matchAll(/^\s{2}([A-Za-z0-9_]+)\s*:/gm)].map((m) => m[1])
  );
  ok(iconKeys.size > 30, `B2 ICONS 解析出 ${iconKeys.size} 个图标`);
  for (const t of TABS) {
    ok(iconKeys.has(t.icon), `B3 tab "${t.key}" 的图标 "${t.icon}" 存在于 ICONS`);
  }
}

// ---------------------------------------------------------------------
section("C. 无令牌（未授权）：只读页可见，执行类/运维/设置页全部隐藏");
// ---------------------------------------------------------------------
// ★ 「关于」是无令牌也能看的：它只是使用说明、快捷键与系统信息，不含任何下发入口，
//   现场排查时恰恰最需要它（"我该按哪个键、这盏灯什么意思"）。
//   「系统设置」反过来 —— needAuth，授权后才出现。
eq(keys(NONE), ["live", "sim", "about"], "C1 可见 tab = 两个只读页 + 关于（顺序保持一致）");
ok(visibleTabs(NONE).every((t) => !t.needAuth && !t.needAdmin), "C2 无令牌时不含任何受限页");
for (const k of ["point", "program", "sort", "ops", "settings"]) {
  ok(!keys(NONE).includes(k), `C3 "${k}" 对无令牌用户不可见（用户明确要求）`);
}
ok(keys(NONE).includes("about"), "C3b 「关于」对无令牌用户可见（是说明文档，不是操作入口）");
ok(!NONE.isAdmin && !NONE.controlActive, "C4 快照自检：无令牌 controlActive/isAdmin 均为 false");

// ---------------------------------------------------------------------
section("D. 操作员令牌：执行页与系统设置出现，运维审计仍隐藏");
// ---------------------------------------------------------------------
// ★ 2026-10-10 用户口径（顶栏改版）：左 4（真实监控 / 点位执行 / 智能分拣 / AI 执行）
//   + 右 4（模拟仿真 / 运维审计 / 系统设置 / 关于）；「程序执行」**与「点位执行」合并**
//   成右栏 tab，不再占顶栏位置（hidden），但仍是合法页面（allowedKeys 里仍认它）。
//   顺序 = src/tabs.js 的 TABS 顺序（guide.js 的模块清单与之同序，verify_about A3 钉着）。
eq(keys(OP), ["live", "point", "program", "sort", "ai", "sim", "settings", "about"],
   "D1 操作员可见 = 真实监控 + 点位执行(+程序执行) + 智能分拣 + AI 执行 + 模拟仿真 + 系统设置 + 关于");
ok(keys(OP).includes("point"), "D2 拿到控制权限后「点位执行」页出现（不需要刷新）");
ok(keys(OP).includes("program"),
   "D2a 「程序执行」仍是合法页面（与点位执行合并、靠右栏 tab 切换）");
ok(keys(OP).includes("settings"), "D2b 系统设置对操作员可见（可改非管理员项，管理员项由后端逐字段拦）");
ok(keys(OP).includes("sort"), "D2c 智能分拣页对操作员可见（会下发运动，必须持控制令牌）");
ok(keys(OP).includes("ai"),
   "D2d AI 执行页对操作员可见（会下发运动，但 AI 不是权限后门：仍需令牌）");
ok(!keys(OP).includes("ops"), "D3 运维审计对操作员隐藏（需管理员）");
ok(OP.controlActive && !OP.isAdmin, "D4 快照自检：操作员有令牌但不是 admin");

// ---------------------------------------------------------------------
section("D′. 顶栏布局（用户 2026-10-10 口径：左 4 / 右 4；点位+程序合并）");
// ---------------------------------------------------------------------
{
  eq(navTabs(ADMIN).map((t) => t.key),
     ["live", "point", "sort", "ai", "sim", "ops", "settings", "about"],
     "D′1 管理员顶栏 = 真实监控/点位执行/智能分拣/AI 执行 + 模拟仿真/运维审计/系统设置/关于");
  ok(!navTabs(ADMIN).some((t) => t.key === "program"), "D′2 「程序执行」不在顶栏（已与点位执行合并）");
  const nav = TABS.filter((t) => !t.hidden);
  eq(nav.filter((t) => t.side !== "right").map((t) => t.key),
     ["live", "point", "sort", "ai"],
     "D′3 标题**左侧** = 真实监控 + 点位执行 + 智能分拣 + AI 执行");
  eq(nav.filter((t) => t.side === "right").map((t) => t.key),
     ["sim", "ops", "settings", "about"],
     "D′4 标题**右侧** = 模拟仿真 + 运维审计 + 系统设置 + 关于");
  ok((TABS.find((t) => t.key === "point").alias || []).includes("program"),
     "D′5 「点位执行」声明 alias=[program]（切到程序执行时顶栏也高亮它）");
  eq(TABS.find((t) => t.key === "sort").label, "智能分拣", "D′6 「分拣」已改名「智能分拣」");
  ok(TABS.some((t) => t.key === "program" && t.hidden === true),
     "D′7 「程序执行」标了 hidden:true（不占顶栏，但仍合法）");
}

// ---------------------------------------------------------------------
section("E. 管理员令牌：全部 tab 可见");
// ---------------------------------------------------------------------
eq(keys(ADMIN), allKeys, "E1 管理员可见集合 = 全部 tab（顺序一致）");
ok(keys(ADMIN).includes("ops"), "E2 运维审计对管理员可见");
eq(visibleTabs(ADMIN).length, TABS.length, "E3 管理员可见数量 = tab 总数");

// ---------------------------------------------------------------------
section("F. 令牌过期：与未授权完全等价（不能留下任何「幽灵入口」）");
// ---------------------------------------------------------------------
eq(keys(EXPIRED_ADMIN), keys(NONE), "F1 过期管理员令牌的可见集合 == 无令牌");
ok(!EXPIRED_ADMIN.isAdmin, "F2 过期后 isAdmin 为 false");
ok(!tabAllowed({ key: "ops", needAdmin: true }, EXPIRED_ADMIN), "F3 过期令牌打不开 needAdmin 页");

// ---------------------------------------------------------------------
section("G. safeView：越权/脏值一律回落，合法值原样放行");
// ---------------------------------------------------------------------
eq(safeView("ops", OP), HOME_VIEW, "G1 操作员请求运维审计 → 回落真实监控");
eq(safeView("point", OP), "point", "G2 操作员请求点位执行 → 原样放行");
eq(safeView("ops", ADMIN), "ops", "G3 管理员请求运维审计 → 原样放行");
eq(safeView("point", NONE), HOME_VIEW, "G4 无令牌请求点位执行 → 回落真实监控");
eq(safeView("nonsense", ADMIN), HOME_VIEW, "G5 未知 key → 回落真实监控");
eq(safeView(undefined, ADMIN), HOME_VIEW, "G6 undefined → 回落真实监控");
eq(safeView(HOME_VIEW, NONE), HOME_VIEW, "G7 HOME_VIEW 永远可达");

// ---------------------------------------------------------------------
section("H. isKnownView：防手改 localStorage 塞垃圾值");
// ---------------------------------------------------------------------
// ★ 2026-10-10 修正：'program'（程序执行页）恢复后重新算合法页面；'exec' 仍为历史废弃值。
ok(isKnownView("live") && isKnownView("ops") && isKnownView("sim") && isKnownView("program"),
   "H1 正常 key 被认作已知页面");
ok(!isKnownView("exec"), "H1b 已废弃的 'exec'（旧 ExecutionView）不再算合法页面");
ok(!isKnownView("") && !isKnownView(null) && !isKnownView("__proto__"), "H3 空值/原型污染键被拒");

// ---------------------------------------------------------------------
section("I. 定向钉子：needAdmin 不能被退化成 needAuth");
// ---------------------------------------------------------------------
// 这是最容易犯的错：把 ops 写成 needAuth:true，操作员就会看到运维审计，
// 点进去改围栏配置 → 一串 403。这里直接把两种写法的差异钉死。
const opsTab = TABS.find((t) => t.key === "ops");
ok(!!opsTab, "I1 找到 ops tab");
ok(!!opsTab.needAdmin, "I2 ops 必须声明 needAdmin（而不是只声明 needAuth）");
ok(tabAllowed({ key: "x" }, NONE) === true, "I3 对照：不带任何 need* 标记的页面永远开放（只读页靠这个）");
ok(!tabAllowed({ key: "x", needAuth: true }, NONE), "I4 对照：needAuth 对无令牌隐藏（区分于 admin 档）");
ok(tabAllowed({ key: "x", needAuth: true }, OP) === true, "I5 对照：needAuth 页对操作员是开放的");
ok(tabAllowed({ key: "x", needAdmin: true }, OP) === false, "I6 反证：needAdmin 页对操作员必须关闭");
ok(tabAllowed({ key: "x", needAdmin: true }, ADMIN) === true, "I7 反证：needAdmin 页对管理员必须开放");
ok(tabAllowed(null, ADMIN) === false && tabAllowed(undefined, ADMIN) === false, "I8 空 tab 安全返回 false");

// ---------------------------------------------------------------------
section("J. UI 入口图标可用性（Icon.vue 里没有的 name 会静默渲染空 SVG，不报错）");
// ---------------------------------------------------------------------
{
  const iconKeys = new Set(
    [...(iconBlock ? iconBlock[1] : "").matchAll(/^\s{2}([A-Za-z0-9_]+)\s*:/gm)].map((m) => m[1])
  );
  const srcDir = path.join(ROOT, "src");
  const files = [];
  (function walk(d) {
    for (const f of fs.readdirSync(d, { withFileTypes: true })) {
      const fp = path.join(d, f.name);
      if (f.isDirectory()) walk(fp);
      else if (/\.(vue|js)$/.test(f.name)) files.push(fp);
    }
  })(srcDir);

  const badStatic = new Map();
  const badDynamic = new Map();
  const bump = (map, key, file) => map.set(key, (map.get(key) || []).concat(path.relative(srcDir, file)));

  for (const f of files) {
    const s = fs.readFileSync(f, "utf8");
    // 静态写法：<Icon name="x"
    for (const mm of s.matchAll(/<Icon\b[^>]*?(?<!:)\bname="([A-Za-z0-9_]+)"/g)) {
      if (!iconKeys.has(mm[1])) bump(badStatic, mm[1], f);
    }
    // 动态写法：:name="cond ? 'a' : 'b'" → 只取其中的字符串字面量。
    // ★ 必须排除**比较运算的操作数**：像 `:name="kind === 'todo' ? 'check' : 'file'"`
    //   里的 'todo' 是被比较的值，不是图标名，当成图标会误报"缺图标"，
    //   而误报的下场是有人把这个守卫关掉 —— 那比不写它还糟。
    for (const mm of s.matchAll(/<Icon\b[^>]*?:name="([^"]*)"/g)) {
      const expr = mm[1];
      for (const lit of expr.matchAll(/'([a-z][A-Za-z0-9_]*)'/g)) {
        const before = expr.slice(0, lit.index).replace(/\s+$/, "");
        if (/(?:===|!==|==|!=|<=|>=|<|>)$/.test(before)) continue;   // 是操作数，跳过
        if (!iconKeys.has(lit[1])) bump(badDynamic, lit[1], f);
      }
    }
  }

  ok(files.length > 20, `J1 扫描到 ${files.length} 个源文件`);
  eq([...badStatic.keys()], [], "J2 所有静态 <Icon name=\"...\"> 都有对应图标");
  eq([...badDynamic.keys()], [], "J3 所有动态 :name 分支的字符串字面量都有对应图标（如 volumeOff / chevronLeft）");
}

console.log(`\n结果: ${pass} passed / ${fail} failed  → ${fail ? "FAIL" : "PASS"}`);
process.exit(fail ? 1 : 0);
