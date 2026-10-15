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
} = await import(pathToFileURL(path.join(ROOT, "src", "tabs.js")).href);
// 数字键位数在 shortcuts.js 里，两边必须一致（E2 就是钉这条）
const { TAB_KEY_COUNT } = await import(pathToFileURL(path.join(ROOT, "src", "shortcuts.js")).href);

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
eq(keys(NONE), ["fleet"], "C1 无令牌只剩机房大屏（真实监控 / 模拟仿真 / 关于已整体移除）");
ok(visibleTabs(NONE).every((t) => !t.needAuth && !t.needAdmin), "C2 无令牌时不含任何受限页");
for (const k of ["point", "program", "ops", "settings"]) {
  ok(!keys(NONE).includes(k), `C3 "${k}" 对无令牌用户不可见（用户明确要求）`);
}
ok(!keys(NONE).includes("about"), "C3b 「关于」不再是可见 tab");
ok(!TABS.some((t) => ["live", "sim", "about"].includes(t.key)),
   "C3c TABS 里根本没有 live / sim / about（是**移除**而不是藏起来）");
ok(!NONE.isAdmin && !NONE.controlActive, "C4 快照自检：无令牌 controlActive/isAdmin 均为 false");
// 机房大屏只展示 12 台的读数与围栏等级，没有任何下发入口（阶段一只读），
// 所以它必须和「真实监控」一样对未授权用户开放 —— 挂上 needAuth 就等于
// 现场没令牌时看不见机房总览，而这恰恰是最需要被看见的那块屏。
{
  const ft = TABS.find((t) => t.key === "fleet");
  ok(!!ft, "C5 机房大屏 tab 存在");
  ok(!!ft && !ft.needAuth && !ft.needAdmin, "C6 机房大屏不需要令牌（只读页，且是默认页 / 回落页）");
}

// ---------------------------------------------------------------------
section("D. 操作员令牌：可见集合与无令牌一致（执行页已下线成面板，不再是 tab）");
// ---------------------------------------------------------------------
eq(keys(OP), ["fleet"], "D1 操作员可见集合 == 无令牌：单页化后没有「多给一个入口」这回事");
eq(keys(OP), keys(NONE), "D1b 操作员与无令牌的可见集合完全相同");
ok(OP.controlActive && !OP.isAdmin, "D2 快照自检：操作员有令牌但不是 admin");
ok(TABS.every((t) => keys(OP).includes(t.key) || t.needAuth || t.needAdmin),
   "D3 任何被操作员挡掉的 tab 都必须自己声明 needAuth/needAdmin（不能靠名单外硬藏）");

// ---------------------------------------------------------------------
section("E. 管理员令牌：同样只有大屏，但门控机制本身必须还有效");
// ---------------------------------------------------------------------
eq(keys(ADMIN), allKeys, "E1 管理员可见集合 = 全部 tab（顺序一致）");
eq(TABS.length, TAB_KEY_COUNT, "E2 模块数 == 数字键位数（现在就是 1 块大屏）");
// ★ 机制留着才有意义：后面"按台/按区授权"还要靠 needAuth / needAdmin 分级。
ok(tabAllowed({ key: "x", needAuth: true }, ADMIN) === true, "E3 needAuth 对管理员放行");
ok(tabAllowed({ key: "x", needAdmin: true }, ADMIN) === true, "E4 needAdmin 对管理员放行");
ok(tabAllowed({ key: "x", needAdmin: true }, OP) === false, "E5 needAdmin 对操作员仍然关闭");

// ---------------------------------------------------------------------
section("F. 令牌过期：与未授权完全等价（不能留下任何「幽灵入口」）");
// ---------------------------------------------------------------------
eq(keys(EXPIRED_ADMIN), keys(NONE), "F1 过期管理员令牌的可见集合 == 无令牌");
ok(!EXPIRED_ADMIN.isAdmin, "F2 过期后 isAdmin 为 false");
ok(!tabAllowed({ key: "ops", needAdmin: true }, EXPIRED_ADMIN), "F3 过期令牌打不开 needAdmin 页");

// ---------------------------------------------------------------------
section("G. safeView：越权/已下线/脏值一律回落大屏，合法值原样放行");
// ---------------------------------------------------------------------
// ★ 这批断言防的是"localStorage 里还留着旧页面名"：单页化之前用户可能停在
//   point / ops / about 上，刷新后若还认这些值，就会打开一个**没有挂载**的页面
//   —— 白屏，而且看起来像后端坏了。
eq(safeView("point", ADMIN), HOME_VIEW, "G1 已下线的点位执行即使管理员也回落大屏");
eq(safeView("ops", ADMIN), HOME_VIEW, "G2 已下线的运维审计回落大屏");
eq(safeView("live", NONE), HOME_VIEW, "G3 已下线的真实监控回落大屏");
eq(safeView("fleet", NONE), "fleet", "G4 大屏对任何人放行（不要令牌）");
eq(safeView("nonsense", ADMIN), HOME_VIEW, "G5 未知 key → 回落大屏");
eq(safeView(undefined, ADMIN), HOME_VIEW, "G6 undefined → 回落大屏");
eq(safeView(HOME_VIEW, NONE), HOME_VIEW, "G7 HOME_VIEW 永远可达");

// ---------------------------------------------------------------------
section("H. isKnownView：防手改 localStorage 塞垃圾值");
// ---------------------------------------------------------------------
ok(isKnownView("fleet"), "H1 大屏是唯一合法页面");
ok(!isKnownView("live") && !isKnownView("sim") && !isKnownView("about"),
   "H2 已移除的 live / sim / about 不再算合法页面");
ok(!isKnownView("point") && !isKnownView("program") && !isKnownView("ops")
   && !isKnownView("settings"),
   "H2b 执行/审计/设置也不再是页面级入口（组件保留但没挂载）");
ok(!isKnownView("exec"), "H3 已废弃的 'exec'（旧 ExecutionView）不算合法页面");
ok(!isKnownView("") && !isKnownView(null) && !isKnownView("__proto__"), "H4 空值/原型污染键被拒");

// ---------------------------------------------------------------------
section("I. 定向钉子：needAdmin 不能被退化成 needAuth");
// ---------------------------------------------------------------------
// 这是最容易犯的错：把"需要管理员"的东西写成 needAuth:true，操作员就会看到它，
// 点进去改围栏配置 → 一串 403。单页化之后 TABS 里已经没有 needAdmin 条目可参照，
// 所以这两档的差异**用合成 tab** 钉死 —— 机制本身还要留着给"按台/按区授权"用。
ok(tabAllowed({ key: "x" }, NONE) === true, "I1 对照：不带任何 need* 标记的入口永远开放（大屏靠这个）");
ok(!tabAllowed({ key: "x", needAuth: true }, NONE), "I2 对照：needAuth 对无令牌隐藏（区分于 admin 档）");
ok(tabAllowed({ key: "x", needAuth: true }, OP) === true, "I3 对照：needAuth 页对操作员是开放的");
ok(tabAllowed({ key: "x", needAdmin: true }, OP) === false, "I4 反证：needAdmin 页对操作员必须关闭");
ok(tabAllowed({ key: "x", needAdmin: true }, ADMIN) === true, "I5 反证：needAdmin 页对管理员必须开放");
ok(tabAllowed(null, ADMIN) === false && tabAllowed(undefined, ADMIN) === false, "I6 空 tab 安全返回 false");

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
