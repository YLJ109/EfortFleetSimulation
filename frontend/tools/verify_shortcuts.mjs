/**
 * headless 回归：全局键盘快捷键的解析（src/shortcuts.js）
 *
 * 背景：键盘是"按下去就出事"的东西，而且出事的方式全部**不可逆**：
 *   - 在密码框里按 e，侧栏开合一下（只是烦）；
 *   - 在点位名称里打空格，程序直接跑起来（这就出事了）；
 *   - 想关掉登录框却按了 Esc，机器人被拍停（之后就没人敢用 Esc 急停了）；
 *   - 只想确认一下按了 Enter，正常运行时居然复位了急停。
 * 这些在开发机上人肉点很难穷尽（尤其"焦点在输入框"这一种），
 * 所以判据全部抽进纯函数 resolveKey，在这里把每一种上下文矩阵钉死。
 *
 * 运行：node tools/verify_shortcuts.mjs     （已挂在 npm run verify）
 */
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

// ★ Windows 下动态 import 必须走 file:// URL。
const {
  SHORTCUTS, SHORTCUT_GROUPS, SHORTCUT_BY_ID, TAB_KEY_COUNT,
  isTypingTarget, resolveKey, tabIndexOf, tabKeyFor, shortcutRows,
} = await import(pathToFileURL(path.join(ROOT, "src", "shortcuts.js")).href);

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

/** 造一次按键事件（只带 resolveKey 关心的字段）。 */
function key(k, mods = {}) {
  return {
    key: k,
    ctrlKey: !!mods.ctrl, metaKey: !!mods.meta, altKey: !!mods.alt,
    shiftKey: !!mods.shift, repeat: !!mods.repeat,
  };
}

/** 默认上下文：不在输入框、没有弹层、停在真实监控、没有急停。 */
function ctx(over = {}) {
  return { typing: false, modalOpen: false, view: "live", stopped: false, ...over };
}

// ---------------------------------------------------------------------
section("A. 定义自洽性");
// ---------------------------------------------------------------------
const ids = SHORTCUTS.map((s) => s.id);
eq(new Set(ids).size, ids.length, "A1 动作 id 无重复");
ok(SHORTCUTS.every((s) => s.keys.length > 0), "A2 每个动作至少有一个触发键");
ok(SHORTCUTS.every((s) => s.group && s.desc && s.when), "A3 每个动作都有 group/desc/when（「关于」页直接渲染）");
ok(SHORTCUTS.every((s) => typeof s.needAuth === "boolean" && typeof s.danger === "boolean"),
   "A4 needAuth / danger 都是显式布尔值（缺字段会让界面漏掉徽标）");
eq(SHORTCUT_GROUPS.filter((g) => !shortcutRows().some((r) => r.group === g)), [],
   "A5 每个分组都至少有一个动作（空分组会在「关于」页留下空白块）");
ok(shortcutRows().reduce((n, r) => n + r.items.length, 0) === SHORTCUTS.length,
   "A6 分组后条目总数 == 定义总数（没有动作被漏掉）");
eq(TAB_KEY_COUNT, 8, "A7 数字键位数 = 8（与 tabs.js 的模块数一致）");
// 每个按键只能属于一个动作，否则"按 e 到底干什么"就变成看代码顺序了
{
  const seen = new Map();
  let dup = "";
  for (const s of SHORTCUTS) {
    for (const k of s.keys) {
      if (seen.has(k)) dup = `${k}（${seen.get(k)} 与 ${s.id}）`;
      else seen.set(k, s.id);
    }
  }
  eq(dup, "", "A8 同一个按键没有被两个动作同时占用");
}

// ---------------------------------------------------------------------
section("B. 修饰键：一概不抢（Ctrl+R 刷新、Alt+Tab 切窗口都归浏览器/系统）");
// ---------------------------------------------------------------------
for (const k of ["e", "q", "1", " ", "Escape", "Enter"]) {
  ok(resolveKey(key(k, { ctrl: true }), ctx()) === "", `B/ctrl  ${k} + Ctrl → 不触发`);
  ok(resolveKey(key(k, { meta: true }), ctx()) === "", `B/meta  ${k} + Cmd/Win → 不触发`);
  ok(resolveKey(key(k, { alt: true }), ctx()) === "", `B/alt   ${k} + Alt → 不触发`);
}

// ---------------------------------------------------------------------
section("C. 打字优先：焦点在输入控件里时，所有快捷键整体失效");
// ---------------------------------------------------------------------
// ★ 这一节是整套快捷键里最重要的一条：它是"打字打成急停/打成程序运行"的唯一防线。
for (const k of ["e", "q", "1", "2", "8", " ", "Escape", "Enter"]) {
  eq(resolveKey(key(k), ctx({ typing: true })), "", `C1 打字中按 ${JSON.stringify(k)} → 不触发任何动作`);
}
// 即使同时满足其它触发条件（程序页 + 空格 / 急停锁定 + Enter）也必须失效
eq(resolveKey(key(" "), ctx({ typing: true, view: "program" })), "", "C2 打字中 + 程序页 + 空格 → 仍然不触发");
eq(resolveKey(key("Enter"), ctx({ typing: true, stopped: true })), "", "C3 打字中 + 已急停 + Enter → 仍然不触发");
eq(resolveKey(key("Escape"), ctx({ typing: true, modalOpen: true })), "", "C4 打字中 + 弹层 + Esc → 仍然不触发（先点出去）");

// isTypingTarget 的判据本身
ok(isTypingTarget({ tagName: "INPUT" }) === true, "C5 INPUT 判定为打字中");
ok(isTypingTarget({ tagName: "TEXTAREA" }) === true, "C6 TEXTAREA 判定为打字中");
ok(isTypingTarget({ tagName: "SELECT" }) === true, "C7 SELECT 判定为打字中（下拉里按字母是选项跳转）");
ok(isTypingTarget({ tagName: "DIV", isContentEditable: true }) === true, "C8 contenteditable 判定为打字中");
ok(isTypingTarget({ tagName: "DIV", getAttribute: () => "off" }) === true, "C9 data-keys=\"off\" 自报禁用的区域判定为打字中");
ok(isTypingTarget({ tagName: "BUTTON" }) === false, "C10 按钮不算打字中（按 e 仍可开合侧栏）");
ok(isTypingTarget(null) === false && isTypingTarget(undefined) === false, "C11 空元素安全返回 false");
ok(isTypingTarget({ tagName: "input" }) === true, "C12 小写 tagName 也认（大小写不敏感）");

// ---------------------------------------------------------------------
section("D. 数字键 1~8：切模块");
// ---------------------------------------------------------------------
for (const n of [1, 2, 3, 4, 5, 6, 7, 8]) {
  eq(resolveKey(key(String(n)), ctx()), "tab:" + n, `D1 按 ${n} → tab:${n}`);
}
for (const bad of ["0", "9", "10", "12", "3abc", "abc", "", "  "]) {
  eq(resolveKey(key(bad), ctx()), "", `D2 非 1~8 的输入 ${JSON.stringify(bad)} 不当作切模块`);
}
eq(resolveKey(key("1", { repeat: true }), ctx()), "tab:1", "D3 数字键允许长按重复（连续切页是有意为之）");

// ---------------------------------------------------------------------
section("E. E / Q：视图开合");
// ---------------------------------------------------------------------
eq(resolveKey(key("e"), ctx()), "toggle_side", "E1 e → 开合侧栏");
eq(resolveKey(key("E"), ctx()), "toggle_side", "E2 大写 E 同样生效（Shift+e 的情况）");
eq(resolveKey(key("q"), ctx()), "toggle_camera", "E3 q → 开合摄像头画面");
eq(resolveKey(key("Q"), ctx()), "toggle_camera", "E4 大写 Q 同样生效");
eq(resolveKey(key("e", { repeat: true }), ctx()), "", "E5 长按 e 不重复触发（按住会疯狂开合）");
eq(resolveKey(key("q", { repeat: true }), ctx()), "", "E6 长按 q 不重复触发");
// 其它无关字母不该被吞掉
for (const k of ["a", "s", "d", "w", "r", "z", "x"]) {
  eq(resolveKey(key(k), ctx()), "", `E7 无关字母 ${k} 不触发任何动作`);
}

// ---------------------------------------------------------------------
section("F. Esc：弹层优先，其次才是急停");
// ---------------------------------------------------------------------
eq(resolveKey(key("Escape"), ctx()), "estop", "F1 没有弹层时 Esc = 急停");
eq(resolveKey(key("Escape"), ctx({ modalOpen: true })), "close_modal", "F2 有弹层时 Esc 先关弹层");
eq(resolveKey(key("Escape"), ctx({ modalOpen: true, view: "program" })), "close_modal", "F3 弹层优先级不受页面影响");
// ★ 反证：这条一旦退化成"Esc 永远是急停"，用户想关个窗口就会拍停机器人，之后不敢再用 Esc。
ok(resolveKey(key("Escape"), ctx({ modalOpen: true })) !== "estop",
   "F4 反证：弹层开着时 Esc 绝不能是急停");
// 急停不因页面/输入权限而失效 —— 任何页面都能停
for (const v of ["live", "sim", "point", "program", "ops", "settings", "about"]) {
  eq(resolveKey(key("Escape"), ctx({ view: v })), "estop", `F5 ${v} 页按 Esc 都是急停`);
}

// ---------------------------------------------------------------------
section("G. Enter：必须先确认「确实锁着」，正常运行时误按无副作用");
// ---------------------------------------------------------------------
eq(resolveKey(key("Enter"), ctx({ stopped: false })), "", "G1 未处于急停锁定 → Enter 什么都不做");
eq(resolveKey(key("Enter"), ctx({ stopped: true })), "estop_reset", "G2 急停锁定中 → Enter 复位");
eq(resolveKey(key("Enter"), ctx({ modalOpen: true })), "submit_modal", "G3 弹层开着 → Enter 提交表单");
eq(resolveKey(key("Enter"), ctx({ modalOpen: true, stopped: true })), "submit_modal",
   "G4 弹层 + 已急停 → 仍然优先提交表单（避免「填口令时误复位急停」）");

// ---------------------------------------------------------------------
section("H. 空格：只在「程序执行」页接管，别处是翻页");
// ---------------------------------------------------------------------
eq(resolveKey(key(" "), ctx({ view: "program" })), "program_run_or_stop", "H1 程序执行页空格 = 运行/停止");
for (const v of ["live", "sim", "point", "ops", "settings", "about"]) {
  eq(resolveKey(key(" "), ctx({ view: v })), "", `H2 ${v} 页空格不接管（留给浏览器翻页）`);
}
eq(resolveKey(key(" "), ctx({ view: undefined })), "", "H3 页面未知时保守不接管");
eq(resolveKey(key("Spacebar"), ctx({ view: "program" })), "", "H4 只认标准的 \" \"，不认旧别名 Spacebar（避免双重触发）");

// ---------------------------------------------------------------------
section("I. 空/异常输入：一律不动作，且不抛异常");
// ---------------------------------------------------------------------
eq(resolveKey(null, ctx()), "", "I1 null 事件 → 空动作");
eq(resolveKey(undefined, ctx()), "", "I2 undefined 事件 → 空动作");
eq(resolveKey({}, ctx()), "", "I3 没有 key 字段的事件 → 空动作");
eq(resolveKey(key("F5"), ctx()), "", "I4 F5（刷新）不抢");
eq(resolveKey(key("Tab"), ctx()), "", "I5 Tab（焦点移动）不抢");
eq(resolveKey(key("ArrowUp"), ctx()), "", "I6 方向键不抢（3D 视口自己要用）");
ok(resolveKey(key("e"), {}) === "toggle_side", "I7 上下文缺字段时按默认值走（typing 视为 false）");

// ---------------------------------------------------------------------
section("J. tab:N 的解码：越界必须返回空，不能回落到第一个");
// ---------------------------------------------------------------------
eq(tabIndexOf("tab:3"), 3, "J1 tab:3 → 3");
eq(tabIndexOf("tab:0"), 0, "J2 tab:0 → 0（非法）");
eq(tabIndexOf("tab:9"), 0, "J3 tab:9 → 0（超出 1~7）");
eq(tabIndexOf("toggle_side"), 0, "J4 非 tab 动作 → 0");
eq(tabIndexOf(""), 0, "J5 空串 → 0");
eq(tabIndexOf(null), 0, "J6 null → 0");

const VISIBLE_NONE = ["live", "sim", "about"];                  // 无权限：只读页 + 关于
const VISIBLE_OP = ["live", "sim", "point", "program", "settings", "about"];  // 操作员
const VISIBLE_ADMIN = ["live", "sim", "point", "program", "ops", "settings", "about"];
eq(tabKeyFor("tab:1", VISIBLE_NONE), "live", "J7 无权限时按 1 → live");
eq(tabKeyFor("tab:3", VISIBLE_NONE), "about", "J8 无权限时按 3 → about（序号按可见集合顺延）");
eq(tabKeyFor("tab:4", VISIBLE_NONE), "", "J9 无权限时按 4 → 空（不能回落到 live）");
eq(tabKeyFor("tab:3", VISIBLE_OP), "point", "J10 操作员按 3 → point");
eq(tabKeyFor("tab:6", VISIBLE_OP), "about", "J11 操作员按 6 → about");
eq(tabKeyFor("tab:5", VISIBLE_ADMIN), "ops", "J12 管理员按 5 → ops（运维审计）");
eq(tabKeyFor("tab:7", VISIBLE_ADMIN), "about", "J12b 管理员按 7 → about（第 7 个模块）");
eq(tabKeyFor("tab:1", []), "", "J13 可见集合为空 → 空");
eq(tabKeyFor("tab:1", null), "", "J14 可见集合为 null → 空");
eq(tabKeyFor("nonsense", VISIBLE_ADMIN), "", "J15 非 tab 动作 → 空");

// 反向钉子：只要序号在范围内，解出的 key 必须真的在可见集合里
{
  let bad = "";
  for (const list of [VISIBLE_NONE, VISIBLE_OP, VISIBLE_ADMIN]) {
    for (let i = 1; i <= 7; i++) {
      const k = tabKeyFor("tab:" + i, list);
      if (k && !list.includes(k)) bad = `tab:${i} → ${k}`;
    }
  }
  eq(bad, "", "J16 解出的 key 一定属于可见集合（不会跳到没权限的页）");
}

// ---------------------------------------------------------------------
section("K. 与 tabs.js 的真实模块对齐");
// ---------------------------------------------------------------------
{
  const { TABS } = await import(pathToFileURL(path.join(ROOT, "src", "tabs.js")).href);
  eq(TAB_KEY_COUNT, TABS.length, `K1 数字键位数(${TAB_KEY_COUNT}) == 真实模块数(${TABS.length})`);
  const list = TABS.map((t) => t.key);
  eq(tabKeyFor("tab:1", list), list[0], "K2 按 1 → 第一个模块");
  eq(tabKeyFor("tab:" + list.length, list), list[list.length - 1], "K3 按最后一个数字 → 最后一个模块");
  ok(list.length >= TAB_KEY_COUNT, "K4 模块数不少于键位数（否则有键位是死的）");
}

// ---------------------------------------------------------------------
section("L. 危险动作的取向：急停不需要确认，复位必须确认状态");
// ---------------------------------------------------------------------
ok(SHORTCUT_BY_ID.estop && SHORTCUT_BY_ID.estop.danger === true, "L1 estop 标记为危险动作（界面警示色）");
ok(SHORTCUT_BY_ID.estop.needAuth === true, "L2 estop 需要授权（无令牌时给明确提示而不是静默失效）");
ok(SHORTCUT_BY_ID.estop_reset.needAuth === true, "L3 estop_reset 需要授权");
ok(SHORTCUT_BY_ID.estop_reset.danger === false, "L4 estop_reset 不是危险动作（复位是恢复）");
eq(SHORTCUT_BY_ID.estop.keys, ["escape"], "L5 急停绑在 Esc 上");
ok(SHORTCUT_BY_ID.estop.when.includes("弹层"), "L6 急停的 when 说明了弹层优先规则（界面说明与实现对得上）");
eq(SHORTCUT_BY_ID.program_run_or_stop.needAuth, true, "L7 空格运行程序需要授权");
ok(!SHORTCUT_BY_ID.toggle_side.needAuth && !SHORTCUT_BY_ID.toggle_camera.needAuth,
   "L8 视图开合不需要授权（这两个动作不碰机器人）");

console.log(`\n结果: ${pass} passed / ${fail} failed  → ${fail ? "FAIL" : "PASS"}`);
process.exit(fail ? 1 : 0);
