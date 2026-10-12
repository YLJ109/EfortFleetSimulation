// =====================================================================
// 手柄执行页 · 20 点光标导航回归（headless）
//
//   为什么需要它：「按一下往哪走」写在 pad/nav.js 的**显式邻接表**里，
//   拼错一个邻居 / 边界写成"翻墙" / 少写一个点，编译不报、界面也看着正常，
//   只有上手按手柄才发现"光标飞了 / 某个格永远进不去"。这里把它逐条钉死：
//     · 20 个点全覆盖、每个点四向都有定义；
//     · 目标要么是合法点、要么是 null（不动）——**绝不越界**；
//     · 任一点可达任一点（连通性）；
//     · 「正上位」只有 A/B 槽位有（起点 / C 检测没有）。
//
// 运行： cd frontend && node tools/verify_pad_nav.mjs
// =====================================================================
import {
  POINTS, NAV, DIRS, START, CAMERA, ZONE_A, ZONE_B, CURSOR_HOME,
  isSlot, abovePoint, moveCursor, reachable, shortLabel, longLabel,
} from "../src/pad/nav.js";

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}
const eq = (a, b, name) => ok(name, a === b, "期望 " + b + "，实际 " + a);

console.log("== A. 点位清单（20 个，与后端 38 点位口径一致） ==");
{
  eq(POINTS.length, 20, "A1 共 20 个点（A1-9 + B1-9 + C检测 + 起点）");
  const set = new Set(POINTS);
  eq(set.size, 20, "A2 无重复");
  for (let i = 1; i <= 9; i++) {
    ok("A3 含 A" + i + " / B" + i, set.has("A" + i) && set.has("B" + i));
  }
  ok("A4 含 C 检测与起点", set.has(CAMERA) && set.has(START));
  ok("A5 CURSOR_HOME = 起点", CURSOR_HOME === START);
  eq(ZONE_A.flat().length, 9, "A6 A 区 3×3 = 9 格");
  eq(ZONE_B.flat().length, 9, "A7 B 区 3×3 = 9 格");
  ok("A8 渲染用的 ZONE_A/ZONE_B 与 POINTS 同集合",
    [...ZONE_A.flat(), ...ZONE_B.flat()].every((n) => set.has(n)));
}

console.log("");
console.log("== B. 邻接表完整性（每个点四向都有定义，且目标合法） ==");
{
  let missing = "";
  let badTarget = "";
  for (const p of POINTS) {
    const row = NAV[p];
    if (!row) { missing += p + "(整行) "; continue; }
    for (const d of DIRS) {
      if (!(d in row)) { missing += p + "." + d + " "; continue; }
      const t = row[d];
      if (t !== null && !POINTS.includes(t)) badTarget += p + "." + d + "→" + t + " ";
    }
  }
  ok("B1 20 个点的 up/down/left/right 全部有定义", missing === "", missing);
  ok("B2 所有目标要么是合法点、要么是 null（不指向列表外的点）", badTarget === "", badTarget);
  ok("B3 邻接表没有多余条目", Object.keys(NAV).length === 20,
    "实际 " + Object.keys(NAV).length);
}

console.log("");
console.log("== C. 边界不越界（顶不动就是不动，不翻墙） ==");
{
  let bad = "";
  for (const p of POINTS) {
    for (const d of DIRS) {
      if (NAV[p][d] === null && moveCursor(p, d) !== p) bad += p + "." + d + " ";
    }
  }
  ok("C1 指向 null 的方向 → 原地不动（返回自身，不返回 null）", bad === "", bad);
  // 具体边界抽查（最容易被写错的那几个角落）
  eq(moveCursor("A1", "up"), "A1", "C2 A1 ↑ 不动（顶行）");
  eq(moveCursor("A1", "left"), CAMERA, "C3 A1 ← 去 C 检测（A 区在右）");
  eq(moveCursor("A3", "right"), "A3", "C4 A3 → 不动（A 区最右列）");
  eq(moveCursor("A9", "down"), "A9", "C5 A9 ↓ 不动（底行）");
  eq(moveCursor("B1", "left"), "B1", "C6 B1 ← 不动（B 区最左列）");
  eq(moveCursor("B3", "right"), CAMERA, "C7 B3 → 去 C 检测（B 区在左）");
  eq(moveCursor("B9", "down"), "B9", "C8 B9 ↓ 不动（底行）");
  eq(moveCursor(START, "up"), START, "C9 起点 ↑ 不动（最上方）");
  eq(moveCursor(CAMERA, "down"), CAMERA, "C10 C 检测 ↓ 不动");
  eq(moveCursor(CAMERA, "up"), START, "C11 C 检测 ↑ 回起点");
  eq(moveCursor(CAMERA, "left"), "B5", "C12 C 检测 ← 去 B 区中点");
  eq(moveCursor(CAMERA, "right"), "A5", "C13 C 检测 → 去 A 区中点");
  eq(moveCursor(START, "down"), CAMERA, "C14 起点 ↓ 去 C 检测");
  eq(moveCursor("A2", "up"), START, "C15 A2 ↑ 回起点（对称）");
  eq(moveCursor("B2", "up"), START, "C16 B2 ↑ 回起点（对称）");
  eq(moveCursor("不存在", "up"), CURSOR_HOME, "C17 未知点 → 回起点（不静默卡死）");
}

console.log("");
console.log("== D. 连通性（任一点可达任一点） ==");
{
  let bad = "";
  for (const from of POINTS) {
    const r = reachable(from);
    if (r.size !== 20) bad += from + "(" + r.size + ") ";
  }
  ok("D1 从任一点出发都能走到全部 20 个点", bad === "", bad);
  // 反向：邻接是对称的吗？（不要求，但抽查几条常见的来回）
  ok("D2 起点 → C 检测 → 起点 可来回",
    moveCursor(moveCursor(START, "down"), "up") === START);
  ok("D3 A5 → C 检测 → A5 可来回",
    moveCursor(moveCursor("A5", "left"), "right") === "A5");
}

console.log("");
console.log("== E. 槽位 / 正上位（只有 A/B 各 9 格有「+上」） ==");
{
  ok("E1 A1..A9 / B1..B9 都是槽位", POINTS.filter((p) => isSlot(p)).length === 18);
  ok("E2 起点 / C 检测不是槽位", !isSlot(START) && !isSlot(CAMERA));
  eq(abovePoint("A2"), "A2上", "E3 A2 的正上位 = A2上");
  eq(abovePoint("B9"), "B9上", "E4 B9 的正上位 = B9上");
  eq(abovePoint(CAMERA), null, "E5 C 检测没有正上位");
  eq(abovePoint(START), null, "E6 起点没有正上位");
  eq(abovePoint(""), null, "E7 空值 → null（不抛）");
  eq(shortLabel(START), "起点", "E8 起点短标签");
  eq(shortLabel(CAMERA), "C", "E9 C 检测短标签");
  eq(longLabel(CAMERA), CAMERA + "（摄像头）", "E10 C 检测长标签如实标注");
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
