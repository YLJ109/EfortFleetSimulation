/**
 * headless 回归：「关于」页的内容与渲染完整性
 *
 * 为什么值得单写一个：
 *   1) **数据写了但页面没渲染**是本页最容易犯、也最难发现的错 —— 我这次就往
 *      src/guide.js 里加了 CHECKLIST / PROCEDURES / PRECAUTIONS / TROUBLESHOOT
 *      四份实验指导数据，但 AboutView.vue 一开始根本没用它们，
 *      页面上什么都看不到，而所有既有测试都是绿的。所以这里断言"每个导出都被引用"。
 *   2) 模块说明的键必须与 tabs.js 的 TABS **完全一致**：多一个会显示一个点进去不存在的
 *      模块，少一个会让用户以为某个功能没了。
 *   3) 文案里嵌套 ASCII 引号会让浏览器整页白屏 → 交给 tools/check_cjk_bare.mjs，
 *      这里只查结构与一致性。
 *
 * 运行：node tools/verify_about.mjs     （已挂在 npm run verify）
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

// ★ Windows 下动态 import 必须走 file:// URL。
const guide = await import(pathToFileURL(path.join(ROOT, "src", "guide.js")).href);
const {
  QUICKSTART, MODULE_GUIDE, SAFETY_NOTES,
  CHECKLIST, PROCEDURES, PRECAUTIONS, TROUBLESHOOT,
} = guide;
const { TABS } = await import(pathToFileURL(path.join(ROOT, "src", "tabs.js")).href);

const aboutSrc = fs.readFileSync(path.join(ROOT, "src/components/AboutView.vue"), "utf8");
const tplStart = aboutSrc.indexOf("<template>");
const tpl = tplStart >= 0 ? aboutSrc.slice(tplStart) : "";

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

// 图标表（Icon.vue 里没有的 name 会静默渲染空 SVG，不报错）
const iconSrc = fs.readFileSync(path.join(ROOT, "src/components/Icon.vue"), "utf8");
const iconBlock = iconSrc.match(/const ICONS = \{([\s\S]*?)\n\};/);
const ICON_KEYS = new Set(
  [...(iconBlock ? iconBlock[1] : "").matchAll(/^\s{2}([A-Za-z0-9_]+)\s*:/gm)].map((m) => m[1])
);

// ---------------------------------------------------------------------
section("A. 模块说明与真实 Tab 一一对应");
// ---------------------------------------------------------------------
{
  const tabKeys = TABS.map((t) => t.key);
  const modKeys = MODULE_GUIDE.map((m) => m.key);
  eq(new Set(modKeys).size, modKeys.length, "A1 模块说明 key 无重复");
  eq([...modKeys].sort(), [...tabKeys].sort(), "A2 模块说明的 key 集合 == tabs.js 的 TABS 集合");
  eq(modKeys, tabKeys, "A3 顺序也一致（「关于」页按 1~9 编号，顺序错会与顶栏对不上）");
  ok(MODULE_GUIDE.every((m) => m.icon && m.label && m.desc && m.use), "A4 每个模块都有 icon/label/desc/use");
  ok(MODULE_GUIDE.every((m) => Array.isArray(m.tips) && m.tips.length > 0), "A5 每个模块至少有一条 tips");
  for (const m of MODULE_GUIDE) {
    ok(ICON_KEYS.has(m.icon), `A6 模块「${m.label}」的图标 "${m.icon}" 存在于 ICONS`);
    const tab = TABS.find((t) => t.key === m.key);
    ok(!!tab && tab.label === m.label, `A7 模块「${m.key}」的 label 与顶栏一致（${m.label}）`);
  }
}

// ---------------------------------------------------------------------
section("B. 快速上手 / 安全须知");
// ---------------------------------------------------------------------
ok(QUICKSTART.length >= 4, `B1 快速上手至少 4 步（实际 ${QUICKSTART.length}）`);
ok(QUICKSTART.every((s) => s.title && s.desc && s.detail), "B2 每步都有 title/desc/detail");
ok(SAFETY_NOTES.length >= 3, `B3 安全须知至少 3 条（实际 ${SAFETY_NOTES.length}）`);
ok(SAFETY_NOTES.every((n) => ["info", "warn", "danger"].includes(n.level)), "B4 安全须知 level 取值合法");
ok(SAFETY_NOTES.every((n) => n.title && n.text), "B5 安全须知每条都有 title/text");

// ---------------------------------------------------------------------
section("C. 实验前检查清单");
// ---------------------------------------------------------------------
{
  const ids = [];
  CHECKLIST.forEach((g) => g.items.forEach((it) => ids.push(it.id)));
  ok(CHECKLIST.length >= 3, `C1 检查清单至少 3 组（实际 ${CHECKLIST.length}）`);
  eq(new Set(ids).size, ids.length, "C2 检查项 id 全局唯一（否则勾选状态会互相污染）");
  eq(new Set(CHECKLIST.map((g) => g.id)).size, CHECKLIST.length, "C3 分组 id 唯一");
  ok(CHECKLIST.every((g) => g.group && g.icon), "C4 每组都有 group/icon");
  for (const g of CHECKLIST) ok(ICON_KEYS.has(g.icon), `C5 分组「${g.group}」图标 "${g.icon}" 存在`);
  ok(CHECKLIST.every((g) => g.items.length >= 3), "C6 每组至少 3 个检查项");
  ok(ids.length >= 12, `C7 检查项总数不少于 12（实际 ${ids.length}）`);
  ok(CHECKLIST.every((g) => g.items.every((it) => it.label && it.detail)), "C8 每项都有 label/detail");
  ok(CHECKLIST.every((g) => g.items.every((it) => typeof it.critical === "boolean")),
     "C9 critical 是显式布尔值（缺字段会让关键项不计入进度）");
  const crit = ids.filter((id) => CHECKLIST.some((g) => g.items.some((it) => it.id === id && it.critical)));
  ok(crit.length >= 6, `C10 至少 6 个关键项（实际 ${crit.length}）—— 关键项太少就起不到拦截作用`);
  ok(crit.length < ids.length, "C11 不能全部都是关键项（否则等于没有优先级）");
  // 检查项必须与真实设备参数有关，不是"注意安全"这种套话
  const all = JSON.stringify(CHECKLIST);
  ok(all.includes("8 kg"), "C12 检查清单写明了额定负载（8 kg）");
  ok(all.includes("0.72") || all.includes("712"), "C13 检查清单写明了臂展/工作半径");
  ok(all.includes("192.168.1.12"), "C14 检查清单写明了控制器地址");
  ok(all.includes("192.168.1.51"), "C15 检查清单写明了相机地址");
  ok(all.includes("8100") && all.includes("8000"), "C16 检查清单写明了两个端口");
  ok(all.includes("EFORT_REAL_MOTION"), "C17 检查清单写明了真实下发总开关的环境变量名");
}

// ---------------------------------------------------------------------
section("D. 标准操作流程（SOP）");
// ---------------------------------------------------------------------
{
  const ids = PROCEDURES.map((p) => p.id);
  eq(new Set(ids).size, ids.length, "D1 流程 id 唯一");
  ok(PROCEDURES.length >= 5, `D2 至少 5 段流程（实际 ${PROCEDURES.length}）`);
  ok(PROCEDURES.every((p) => p.title && p.why), "D3 每段都有 title/why（why 是这份文档的价值所在）");
  ok(PROCEDURES.every((p) => typeof p.needAuth === "boolean"), "D4 needAuth 是显式布尔值");
  ok(PROCEDURES.every((p) => Array.isArray(p.steps) && p.steps.length >= 2), "D5 每段至少 2 步");
  ok(PROCEDURES.every((p) => p.steps.every((s) => s.text)), "D6 每步都有 text");
  ok(PROCEDURES.every((p) => p.steps.every((s) => s.warn === undefined || typeof s.warn === "boolean")),
     "D7 warn 若有值必须是布尔");
  // ★ 危险步骤必须带解释：只标红不解释，现场会当成"又说一遍安全"，然后忽略它
  const warnNoNote = PROCEDURES.flatMap((p) => p.steps.filter((s) => s.warn && !s.note).map(() => p.id));
  eq(warnNoNote, [], "D8 每个标了 warn 的步骤都必须有 note（说明「做错了会怎样」）");
  ok(PROCEDURES.every((p) => p.why.length >= 15), "D9 why 不是一句话敷衍（至少 15 字）");
  // 必须有急停与收尾这两段 —— 现场最常被跳过、也最不该被跳过
  ok(ids.includes("estop"), "D10 包含「急停与复位」流程");
  ok(ids.includes("bye"), "D11 包含「收尾与关机」流程");
  ok(PROCEDURES.find((p) => p.id === "boot") && PROCEDURES[0].id === "boot", "D12 第一段是开机自检（顺序即现场顺序）");
}

// ---------------------------------------------------------------------
section("E. 注意事项");
// ---------------------------------------------------------------------
{
  ok(PRECAUTIONS.length >= 3, `E1 至少 3 组（实际 ${PRECAUTIONS.length}）`);
  eq(new Set(PRECAUTIONS.map((g) => g.group)).size, PRECAUTIONS.length, "E2 分组名唯一");
  ok(PRECAUTIONS.every((g) => g.group && g.icon), "E3 每组都有 group/icon");
  for (const g of PRECAUTIONS) ok(ICON_KEYS.has(g.icon), `E4 分组「${g.group}」图标 "${g.icon}" 存在`);
  const lv = PRECAUTIONS.flatMap((g) => g.items.map((it) => it.level));
  ok(lv.every((x) => ["danger", "warn", "info"].includes(x)), "E5 level 只取 danger/warn/info（其它值会掉到 info 兜底）");
  ok(PRECAUTIONS.every((g) => g.items.every((it) => it.title && it.text)), "E6 每条都有 title/text");
  ok(PRECAUTIONS.every((g) => g.items.length >= 3), "E7 每组至少 3 条");
  ok(lv.includes("danger"), "E8 存在 danger 级（否则警示色形同虚设）");
  ok(lv.includes("info"), "E9 存在 info 级（精度/习惯类也要有落点）");
}

// ---------------------------------------------------------------------
section("F. 异常处置速查");
// ---------------------------------------------------------------------
{
  ok(TROUBLESHOOT.length >= 6, `F1 至少 6 条（实际 ${TROUBLESHOOT.length}）`);
  ok(TROUBLESHOOT.every((r) => r.symptom && r.cause && r.action), "F2 每行都有 现象/原因/处置");
  const sym = TROUBLESHOOT.map((r) => r.symptom);
  eq(new Set(sym).size, sym.length, "F3 现象不重复（重复会让搜索出现两条一样的）");
  ok(TROUBLESHOOT.every((r) => r.action.length >= 10), "F4 处置写的是动作，不是「请联系厂家」这种废话");
  // 覆盖现场最常见的几类
  const all = JSON.stringify(TROUBLESHOOT);
  ok(all.includes("点动"), "F5 覆盖「点动按了没反应」这一类");
  ok(all.includes("403"), "F6 覆盖权限被拒（403）");
  ok(all.includes("相机"), "F7 覆盖相机连不上");
  ok(all.includes("重启"), "F8 覆盖「改了没生效 / 需重启」");
  ok(all.includes("令牌") || all.includes("权限"), "F9 覆盖令牌到期导致权限页消失");
}

// ---------------------------------------------------------------------
section("H. 渲染完整性：每个数据导出都必须在模版里被用到");
// ---------------------------------------------------------------------
// ★ 这是本文件存在的首要理由。只加数据不改模版 = 页面上什么都看不到，而其它测试全绿。
{
  const scriptPart = tplStart >= 0 ? aboutSrc.slice(0, tplStart) : aboutSrc;

  /**
   * 一个数据导出"被渲染"的两种形态：
   *   ① 模版直接引用它（v-for="x in CHECKLIST" / {{ QUICKSTART.length }}）；
   *   ② 模版引用一个**从它派生**的局部变量（const modules = computed(() => MODULE_GUIDE)）。
   * 形态 ② 必须支持 —— 否则一包装就被误报成"没渲染"，这种假阳性会让人把守卫关掉。
   */
  // 模版里出现过的所有标识符
  const tplIds = new Set([...tpl.matchAll(/[A-Za-z_$][A-Za-z0-9_$]*/g)].map((m) => m[0]));
  // 脚本里的顶层声明：语句切成块，块内找"被声明的名字 + 引用到的名字"
  const stmts = scriptPart
    .split(/\n(?=(?:const|let|var|function|async function)\s)/)
    .filter((s) => /^\s*(?:const|let|var|function|async function)\s/.test(s));
  const derivedFrom = (name) =>
    stmts.some((s) => {
      if (new RegExp(`\\b${name}\\b`).test(s) === false) return false;
      // 该语句里声明的局部变量名，只要有一个出现在模版里，就算"这条链接到了渲染"
      const declared = [...s.matchAll(/(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)/g)].map((m) => m[1]);
      return declared.some((d) => d !== name && tplIds.has(d));
    });

  const rendered = [
    ["QUICKSTART", "快速上手"],
    ["CHECKLIST", "实验前检查清单"],
    ["PROCEDURES", "标准操作流程"],
    ["PRECAUTIONS", "注意事项"],
    ["TROUBLESHOOT", "异常处置速查"],
    ["MODULE_GUIDE", "八个模块"],
    ["SAFETY_NOTES", "安全须知"],
  ];
  for (const [name, label] of rendered) {
    const direct = tpl.includes(name);
    const viaLocal = derivedFrom(name);
    ok(direct || viaLocal,
      `H1 「${label}」的数据 ${name} 被渲染（${direct ? "模版直接引用" : viaLocal ? "经由派生变量" : "既没直接引用也没有派生变量接到模版 —— 加了数据但页面看不到"}）`);
  }

  // v-for 的源变量：必须要么是已知数据（或它的派生变量），要么是外层循环变量
  // （v-for="it in g.items" 里的 g 就是外层的循环变量，不是数据源）。
  const fors = [];
  for (const m of tpl.matchAll(/v-for="([^"]*)"/g)) {
    const expr = m[1];
    // (a, b) in X  |  a in X  → 源在最后
    const mm = /^\s*(?:\(([^)]*)\)|([A-Za-z_$][A-Za-z0-9_$]*))\s+(?:of|in)\s+(.+?)\s*$/.exec(expr);
    if (!mm) continue;
    const vars = (mm[1] || mm[2] || "").split(",").map((s) => s.trim()).filter(Boolean);
    const srcRoot = (mm[3] || "").split(".")[0].trim();
    fors.push({ vars, srcRoot });
  }
  ok(fors.length >= 8, `H2 解析到 ${fors.length} 处 v-for`);
  const allLoopVars = new Set(fors.flatMap((f) => f.vars));
  const localNames = new Set(
    stmts.flatMap((s) => [...s.matchAll(/(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)/g)].map((m) => m[1]))
  );
  const badSrc = [...new Set(
    fors.map((f) => f.srcRoot)
      .filter((s) => !allLoopVars.has(s) && !localNames.has(s) && !Object.keys(guide).includes(s))
  )];
  eq(badSrc, [], "H3 每个 v-for 的源（不是循环变量）都是脚本里真实存在的变量");

  // 反证：故意检查一个不存在的数据名会被判为"没渲染"（证明上面的判据不是恒真）
  ok(!derivedFrom("__NOT_A_REAL_EXPORT__"), "H4 反证：不存在的导出名不会被判为已渲染（判据不是恒真）");
  ok(!tpl.includes("__NOT_A_REAL_EXPORT__"), "H5 反证：模版里确实没有这个假名字");
}

// ---------------------------------------------------------------------
section("I. 章节导航与锚点一致");
// ---------------------------------------------------------------------
{
  // SECTIONS 里的 id 必须在模版里真的存在对应元素，否则点了没反应
  const sections = [...aboutSrc.matchAll(/\{\s*id:\s*"(ab-sec-[a-z]+)",\s*label:\s*"([^"]+)",\s*icon:\s*"([^"]+)"/g)];
  ok(sections.length >= 8, `I1 解析到 ${sections.length} 个导航项`);
  for (const m of sections) {
    ok(tpl.includes(`id="${m[1]}"`), `I2 导航「${m[2]}」的锚点 ${m[1]} 在模版中存在`);
    ok(ICON_KEYS.has(m[3]), `I3 导航「${m[2]}」的图标 "${m[3]}" 存在`);
  }
  // 反向：模版里的每个 ab-sec-* 锚点都应该能被导航点到
  const anchors = [...new Set([...tpl.matchAll(/id="(ab-sec-[a-z]+)"/g)].map((m) => m[1]))];
  const navIds = new Set(sections.map((m) => m[1]));
  const orphan = anchors.filter((a) => !navIds.has(a));
  eq(orphan, [], "I4 模版里没有「导航点不到」的孤立章节");
  eq(anchors.length, navIds.size, "I5 锚点数量 == 导航项数量（没有重复 id）");
}

// ---------------------------------------------------------------------
section("J. 页面不重复硬编码品牌名（改名字只需改 brand.js / index.html）");
// ---------------------------------------------------------------------
{
  const hits = [...aboutSrc.matchAll(/FIT\s*埃夫特/g)].length;
  // 允许出现在文件头注释里说明"名称也在 index.html"，但模版里必须走 BRAND.name
  ok(tpl.includes("BRAND.name"), "J1 模版里的项目名走 BRAND.name");
  ok(!/FIT\s*埃夫特/.test(tpl), "J2 模版里不硬编码全名（改名会漏改）");
  ok(hits <= 2, `J3 硬编码全名出现次数 ${hits}（只允许出现在注释里）`);
}

console.log(`\n结果: ${pass} passed / ${fail} failed  → ${fail ? "FAIL" : "PASS"}`);
process.exit(fail ? 1 : 0);
