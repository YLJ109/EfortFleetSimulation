/**
 * headless 回归：机房大屏的作用域（src/utils/fleetScope.js）
 *
 * 为什么值得钉：作用域决定"这一页看哪几台"，而它同时喂给四个地方 ——
 * 卡片列、KPI 统计、3D 相机、控制作用域。判据散在模板里的话，
 * 一定会出现"卡片刻意只显示 6 台、KPI 还按 12 台算"这种自相矛盾的界面，
 * 而那种界面比崩溃更危险：操作员会拿它做判断。
 *
 * 运行：node tools/verify_scope.mjs   （已挂在 npm run verify）
 */
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

const {
  SCOPES, inScope, scopeIds, scopeColumns, scopeRequest, scopeLabel, scopeKeeps,
} = await import(pathToFileURL(path.join(ROOT, "src", "utils", "fleetScope.js")).href);

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

/** 造一张卡片：只需要作用域用得上的四个字段。 */
const card = (zone, group, slot) => ({
  id: `${zone}${group === "lower" ? "" : "u"}${slot}`,
  zone, group, slot,
  order: (zone === "A" ? 0 : 6) + (group === "lower" ? slot : 3 + slot),
});
const CARDS = [
  card("A", "lower", 1), card("A", "lower", 2), card("A", "lower", 3),
  card("A", "upper", 1), card("A", "upper", 2), card("A", "upper", 3),
  card("B", "lower", 1), card("B", "lower", 2), card("B", "lower", 3),
  card("B", "upper", 1), card("B", "upper", 2), card("B", "upper", 3),
];
const ids = (list) => list.map((c) => c.id);

// ---------------------------------------------------------------------
section("A. 作用域清单自洽");
// ---------------------------------------------------------------------
eq(SCOPES.map((s) => s.key), ["all", "upper", "lower", "zoneA", "zoneB"],
   "A1 五个作用域，顺序就是选择器顺序");
ok(SCOPES.every((s) => s.label && s.en && s.hint), "A2 每个作用域都有中文名 / 英文小注 / 悬停提示");
eq(scopeLabel("zoneA"), "A 区", "A3 scopeLabel");
eq(scopeLabel("nonsense"), "全部", "A4 认不出的作用域标签回落「全部」");

// ---------------------------------------------------------------------
section("B. inScope / scopeIds：台数必须正好");
// ---------------------------------------------------------------------
eq(scopeIds(CARDS, "all").length, 12, "B1 全部 = 12 台");
eq(scopeIds(CARDS, "upper").length, 6, "B2 上排 = 6 台（两区各 3）");
eq(scopeIds(CARDS, "lower").length, 6, "B3 下排 = 6 台");
eq(scopeIds(CARDS, "zoneA").length, 6, "B4 A 区 = 6 台");
eq(scopeIds(CARDS, "zoneB").length, 6, "B5 B 区 = 6 台");
eq(scopeIds(CARDS, "hacked"),  ids(CARDS), "B6 手改出来的未知作用域 → 回落全部（不能空屏）");
eq(scopeIds(null, "all"), [], "B7 cards 为 null 不炸");

// ---------------------------------------------------------------------
section("C. 两列覆盖完整：不重不漏");
// ---------------------------------------------------------------------
for (const sc of SCOPES.map((s) => s.key)) {
  const { left, right } = scopeColumns(CARDS, sc);
  const all = [...left, ...right].flatMap((b) => b.cards);
  const want = scopeIds(CARDS, sc);
  eq(ids(all).slice().sort(), want.slice().sort(), `C-${sc} 两列合起来 == 作用域里的台（一台不丢）`);
  eq(new Set(ids(all)).size, all.length, `C-${sc} 没有一台同时出现在两列`);
  ok(left.length > 0 && right.length > 0, `C-${sc} 左右两列都有内容（空列会让大屏看起来缺半边）`);
}

// ---------------------------------------------------------------------
section("D. 方向恒定：左 = A 区/上排，右 = B 区/下排，列内编号倒序");
// ---------------------------------------------------------------------
{
  const all = scopeColumns(CARDS, "all");
  eq(all.left.flatMap((b) => b.cards).every((c) => c.zone === "A"), true,
     "D1「全部」时左列只有 A 区");
  eq(all.right.flatMap((b) => b.cards).every((c) => c.zone === "B"), true,
     "D2「全部」时右列只有 B 区");
  // 上排在前的组块（GROUP_ORDER 已改成 upper 先）
  eq(all.left.map((b) => b.group), ["upper", "lower"], "D3 左列里上排组块在上方（与等轴画面对齐）");
  // 夹具里 A 区：下排 order=1~3、上排 order=4~6（"从下面 1 开始数"）
  eq(all.left[0].cards.map((c) => c.order), [6, 5, 4], "D4 列内编号倒序（A6→A4）");
  eq(all.left[1].cards.map((c) => c.order), [3, 2, 1], "D5 下排组块同样倒序（A3→A1）");
  eq(all.right[0].cards.map((c) => c.order), [12, 11, 10], "D4b 右列上排同样倒序（B6→B4）");
}
{
  const a = scopeColumns(CARDS, "zoneA");
  eq(a.left.flatMap((b) => b.cards).every((c) => c.zone === "A" && c.group === "upper"), true,
     "D6「A 区」时左列 = A 区上排（单区页改成按排分左右）");
  eq(a.right.flatMap((b) => b.cards).every((c) => c.zone === "A" && c.group === "lower"), true,
     "D7「A 区」时右列 = A 区下排");
}
{
  const u = scopeColumns(CARDS, "upper");
  eq(u.left.flatMap((b) => b.cards).every((c) => c.zone === "A"), true, "D8「上排」时左列 = A 区那 3 台");
  eq(u.right.flatMap((b) => b.cards).every((c) => c.zone === "B"), true, "D9「上排」时右列 = B 区那 3 台");
}

// ---------------------------------------------------------------------
section("E. 预期外的分区要露出来，不能被静默吞掉");
// ---------------------------------------------------------------------
{
  const weird = [...CARDS, { id: "C1", zone: "C", group: "middle", slot: 1, order: 13 }];
  const { left, right } = scopeColumns(weird, "all");
  const found = [...left, ...right].flatMap((b) => b.cards).map((c) => c.id);
  ok(found.includes("C1"), "E1 改过 fleet.yaml 多出一个 C 区时，它仍然出现在某一列里");
  eq(ids(scopeIds(weird, "all")).length, 13, "E2 作用域统计也把它算进去（KPI 与列不会打架）");
}

// ---------------------------------------------------------------------
section("F. 控制作用域：前端只报意图，ids 由后端算");
// ---------------------------------------------------------------------
eq(scopeRequest("all"), { kind: "all" }, "F1 全部");
eq(scopeRequest("zoneA"), { kind: "zone", zone: "A" }, "F2 A 区");
eq(scopeRequest("zoneB"), { kind: "zone", zone: "B" }, "F3 B 区");
eq(scopeRequest("upper"), { kind: "row", group: "upper" }, "F4 上排");
eq(scopeRequest("lower"), { kind: "row", group: "lower" }, "F5 下排");
// ★ 这条钉的是"前端不许自己拼 ids 发给后端"：名单是异步刷新的，
//   前端缓存的 id 列表可能已经过期，信它就等于把控制权发给一台不存在的机器。
ok(!JSON.stringify(scopeRequest("all")).includes("ids"), "F6 请求体里没有 ids 字段");

// ---------------------------------------------------------------------
section("G. scopeKeeps：换页后选中的台不在这一页了，要取消选中");
// ---------------------------------------------------------------------
{
  const a1 = CARDS.find((c) => c.zone === "A" && c.group === "lower");
  ok(scopeKeeps(a1, "all") === true, "G1「全部」里选着 A 下 1 → 保留");
  ok(scopeKeeps(a1, "upper") === false, "G2 切到「上排」→ 不再保留（看不见就不许还能操作）");
  ok(scopeKeeps(a1, "zoneA") === true, "G3 切到「A 区」→ 保留");
  ok(scopeKeeps(null, "all") === false, "G4 没选中任何东西 → false");
}

console.log(`\n结果: ${pass} passed / ${fail} failed  → ${fail ? "FAIL" : "PASS"}`);
process.exit(fail ? 1 : 0);
