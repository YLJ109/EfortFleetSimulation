// =====================================================================
// Pinia store 用法回归（headless，源码扫描）
//
// 背景（真实事故）：exec.js 把 `jointLockEnabled` / `jointLocked` 放进 **getters**，
//   但别处按函数调用 `this.jointLockEnabled()` → 运行时报
//   "this.jointLockEnabled is not a function"（getter 是"值"，不是函数）。
//   这类错误**编译期不报、界面静态看也正常**，只在运行时炸，最该被钉死。
//
// 本脚本钉三条：
//   A. getter 不得声明参数（Pinia 的 getter 不能接收参数；带参判定必须是 action）。
//   B. 任何 `xxx.<getter>( )` 空括号调用都是误用（getter 按值访问，不加括号）。
//   C. 真机链路卡：只保留一个"一键就绪/取消就绪"按钮，不得再出现
//      「声明 AUTO」「声明远程」两个按钮。
//
// 运行： node tools/verify_stores.mjs
// =====================================================================
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(root, p), "utf8");
const readAbs = (p) => readFileSync(p, "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

/** 递归列出 src 下所有 .js/.vue。 */
function listSrc(dir) {
  const out = [];
  for (const n of readdirSync(dir)) {
    const p = join(dir, n);
    if (statSync(p).isDirectory()) out.push(...listSrc(p));
    else if (/\.(js|vue)$/.test(n)) out.push(p);
  }
  return out;
}

/** 取出 `getters: { ... }` 与 `actions: { ... }` 之间的片段（花括号配平）。 */
function sliceBlock(src, key) {
  const at = src.search(new RegExp("\\b" + key + "\\s*:\\s*\\{"));
  if (at < 0) return "";
  const open = src.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(open + 1, i); }
  }
  return src.slice(open + 1);
}

console.log("== Pinia store 用法回归 ==");

const srcDir = join(root, "src");
const storeFiles = listSrc(join(srcDir, "stores"));
const allFiles = listSrc(srcDir);

// ---- A. getter 不得接收"应用级参数" ----
// ★ 注意：Pinia 的 getter **允许**接收 state 作为首个参数（本仓约定命名为 s / state），
//   这不算误用；真正会炸的是 getter 带一个"普通业务参数"（如 jointLocked(j)）——
//   调用方会写成 this.jointLocked(joint) 或 this.jointLocked()，前者拿不到函数、后者直接抛错。
const valueGetters = [];   // {file, name}
let paramGetters = 0;
const STATE_PARAM = /^(s|state|_s)$/;
for (const f of storeFiles) {
  const src = readAbs(f);
  const g = sliceBlock(src, "getters");
  if (!g) continue;
  // 只认"顶格 4 空格"的声明行（getter 体内的行缩进更深，不能当声明）
  const re = /^ {4}([A-Za-z_$][\w$]*)\s*(?:\(([^)]*)\)|\s*:)/gm;
  let m;
  while ((m = re.exec(g)) !== null) {
    const name = m[1];
    const params = m[2];
    if (params !== undefined && params.trim() !== "" && !STATE_PARAM.test(params.trim())) {
      paramGetters++;
      console.log(`  FAIL  getter 带业务参数: ${relative(root, f)} -> ${name}(${params.trim()})`);
      fail++;
    } else {
      valueGetters.push({ file: f, name });
    }
  }
}
ok("A. getters 不接收业务参数（带参判定必须是 action）", paramGetters === 0);

// ---- B. getter 不得被当函数调用（.name( )）----
const names = [...new Set(valueGetters.map((v) => v.name))];
let misuse = 0;
if (names.length) {
  const re = new RegExp("\\.\\s*(" + names.join("|") + ")\\s*\\(\\s*\\)", "g");
  for (const f of allFiles) {
    const src = readAbs(f);
    let m;
    while ((m = re.exec(src)) !== null) {
      const line = src.slice(0, m.index).split("\n").length;
      console.log(`  FAIL  getter 被当函数调用: ${relative(root, f)}:${line} -> ${m[0]}`);
      misuse++; fail++;
    }
  }
}
ok("B. 无 `xxx.<getter>( )` 空括号误用（getter 按值访问）", misuse === 0);

// ---- C. 真机链路卡按钮精简 ----
const card = read("src/components/RcReadyCard.vue");
ok("C1. 已移除「声明 AUTO」按钮（无 claimMode('AUTO')）", !card.includes("claimMode(\"AUTO\")"));
ok("C2. 已移除「声明远程」按钮（无 claimMode('REMOTE')）", !card.includes("claimMode(\"REMOTE\")"));
ok("C3. 仍保留绿/红就绪切换按钮", card.includes("ready-toggle") && card.includes("一键就绪")
  && card.includes("取消就绪"));

// ---- D. 程序执行：`/run-file` 已**异步化**（B1），结果必须从 run-state 取 ----
//   后端 POST /control/run-file 现在只回 {started, run_id, kind}（不再带逐步结果）；
//   前端若还去读响应体的 steps/count，会整块变成 undefined（"undefined/undefined 步"）。
const execStore = read("src/stores/exec.js");
ok("D1. 收到 started 后改走 _awaitRunResult（不再当响应体是结果）",
  /d\.started/.test(execStore) && /_awaitRunResult/.test(execStore));
ok("D2. 结果从 /control/run-state 的 state.result 取",
  /\/control\/run-state/.test(execStore) && /if \(s\.result\) return s\.result/.test(execStore));
ok("D3. 等结果有超时且如实回报（不假装成功）",
  /等待执行结果超时/.test(execStore));

// ---- E. 双面（8 气路动作）：点位执行页 4 个"反面"按钮 + store 支持 8 动作（2026-10-11）----
//   用户权威位表：下板 1~4 位、反面/上板 5~8 位；两面**各自独立气路**。
//   判据：① store 的 vacuum 放行 8 个动作、分面锁存（vacuumState / vacuumStateBack）；
//         ② 控制权限卡片（PointExecView 与程序执行页共用）确有这 4 个反面按钮、
//            且**走同一套控制接口**（exec.vacuum），不另造接口。
const execCard = read("src/components/ExecControlCard.vue");
const pointView = read("src/components/PointExecView.vue");
ok("E1. store 放行 8 个气路动作（含 …_back）",
  /"suck_back", "release_back", "blow_back", "unblow_back"/.test(execStore));
ok("E2. store 分面锁存阀状态（vacuumStateBack）",
  /vacuumStateBack: "idle"/.test(execStore) && /vacuumStateBack/.test(execStore));
ok("E3. 控制权限卡片有 4 个「反面」按钮（反面吸气/停止吸气/放气/停止放气）",
  /exec\.vacuum\('suck_back'\)/.test(execCard)
  && /exec\.vacuum\('release_back'\)/.test(execCard)
  && /exec\.vacuum\('blow_back'\)/.test(execCard)
  && /exec\.vacuum\('unblow_back'\)/.test(execCard)
  && /反面吸气/.test(execCard) && /反面停止吸气/.test(execCard)
  && /反面放气/.test(execCard) && /反面停止放气/.test(execCard));
ok("E4. 4 个反面按钮走**统一控制接口**（exec.vacuum → /control/vacuum，store 内）",
  /\/control\/vacuum/.test(execStore) && /vacuum\(action\)/.test(execStore)
  && !/suck_back/.test(pointView));   // 页面不另造下发通道
ok("E5. 点位执行页复用该控制权限卡片（同一份按钮实现）",
  /ExecControlCard/.test(pointView));

console.log(`\n== store 回归: ${pass} passed, ${fail} failed ==`);
process.exit(fail ? 1 : 0);
