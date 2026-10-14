// =====================================================================
// ★ B6：回归套件自身的耗时预算（**只 WARN，不 FAIL**）
//
// 为什么要它：`npm run verify` 已经涨到 30+ 个脚本，没人知道慢在哪一步。
// 慢到没人愿意跑 = 回归钉形同虚设（和本项目"看不见就等于没有"是同一种病）。
//
// 口径：
//   · 逐个跑 package.json 里 `verify` 链上的 **tools/*.mjs**（跳过 eslint / vite，
//     那些是前置检查，不是回归钉，耗时另说）；
//   · 单脚本 > 20s 或总计 > 180s ⇒ 打印 WARN（**不改变退出码**）；
//   · 但**脚本自己失败 ⇒ 仍然失败退出**（预算是性能问题，正确性是另一回事，
//     绝不能因为"跑得快"就把红钉放绿）。
//
// 运行： cd frontend && npm run verify:time
// =====================================================================
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const PER_SCRIPT_S = 20;
const TOTAL_S = 180;

const chain = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).scripts.verify;
// 只取 `node tools/xxx.mjs` 这些步骤；eslint / check_* 属于前置检查，不在预算口径里
const steps = chain.split("&&").map((s) => s.trim())
  .filter((s) => /^node tools\/[\w.\-]+\.mjs$/.test(s));

if (!steps.length) {
  console.log("没在 package.json 的 verify 链里找到任何 `node tools/*.mjs` 步骤。");
  process.exit(1);
}

const rows = [];
let failed = [];
const t0 = Date.now();
for (const step of steps) {
  const file = step.replace(/^node /, "");
  const s = Date.now();
  const r = spawnSync(process.execPath, [file], { cwd: root, encoding: "utf8" });
  const ms = Date.now() - s;
  const out = (r.stdout || "") + (r.stderr || "");
  const m = out.match(/(\d+)\s*过\s*\/\s*(\d+)\s*败/);
  rows.push({ file, ms, code: r.status, pass: m ? Number(m[1]) : null, fail: m ? Number(m[2]) : null });
  if (r.status !== 0) { failed.push(file); console.log(out.trim().split(String.fromCharCode(10)).slice(-8).join(String.fromCharCode(10))); }
  console.log(`  ${(ms / 1000).toFixed(1)}s  ${file}${m ? `   (${m[1]} 过 / ${m[2]} 败)` : ""}`);
}
const total = (Date.now() - t0) / 1000;

rows.sort((a, b) => b.ms - a.ms);
console.log(String.fromCharCode(10) + "最慢的 8 个：");
for (const r of rows.slice(0, 8)) {
  console.log(`  ${(r.ms / 1000).toFixed(1)}s  ${r.file}${r.code ? "   ← 失败" : ""}`);
}

const slow = rows.filter((r) => r.ms / 1000 > PER_SCRIPT_S);
if (slow.length) {
  console.log(String.fromCharCode(10) + `WARN 单脚本超预算（>${PER_SCRIPT_S}s）：`
    + slow.map((r) => `${r.file} ${(r.ms / 1000).toFixed(1)}s`).join("、"));
  console.log("  → 只是提醒（不改退出码）。真要提速就把它拆成两条或改成静态检查；"
    + "但**别为了快把断言删掉** —— 少一条钉就是少一次现场事故。");
}
if (total > TOTAL_S) {
  console.log(`WARN 总耗时 ${total.toFixed(1)}s 超预算（>${TOTAL_S}s）：整条链该分档了`
    + "（快速档每次提交跑、完整档收尾跑）。");
}
const cases = rows.reduce((n, r) => n + (r.pass || 0), 0);
const badCases = rows.reduce((n, r) => n + (r.fail || 0), 0);
console.log(String.fromCharCode(10) + `合计 ${rows.length} 个脚本 · ${total.toFixed(1)}s · `
  + `断言 ${cases} 过 / ${badCases} 败`);
if (failed.length) {
  console.log("失败脚本：" + failed.join("、"));
  process.exit(1);
}
