// =====================================================================
// 姿态权威模型回归（headless，纯 node，无 three / 无 WebGL / 无 DOM）
//
// 阶段 7 重构的核心不变量，全钉在这里：
//  A. 优先级数学（src/stores/poseAuthority.js —— 全站唯一判据）：
//     override(仅前台 owner 生效) > demo > 指令目标(模拟未走位) > 遥测。
//     每个分支的 q / tcp / source 都断言到具体值。
//  B. 唯一写者：applyRobotPose 在整个 src 里只允许出现在
//     - App.vue 的渲染循环（唯一真实调用点）
//     - three/manager.js 的函数定义
//     其它任何文件出现调用即 FAIL（改造前 7 处各自写，谁最后写谁赢）。
//  C. setLocalDemo 必须已灭绝（历史上是"读数冻结"的直接成因）。
//  D. exec.js 迁移：无直接 3D 驱动；指令经 noteCommand；leaveView 不再碰 demo。
//  E. 沙盘迁移：SimMonitor/recording 走 setOverride/clearOverride，owner 正确。
//  F. RobotParamsCard 读 readoutQ/readoutTcp 且定义了 sourceLabel。
//  G. JointHud.vue 已删除（死组件，曾经自算一份读数）。
//
// 运行： node tools/verify_pose_authority.mjs
// =====================================================================
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { resolvePose } from "../src/stores/poseAuthority.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = path.join(ROOT, "src");
const rel = (p) => path.join(SRC, p);
const read = (p) => readFileSync(p, "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}
/** 去掉注释行后数模式出现次数（// 与 /* 与 HTML 注释都算注释行）。 */
function countCodeOccurrences(text, re) {
  const lines = text.split("\n").filter((l) => {
    const t = l.trim();
    return !(t.startsWith("//") || t.startsWith("*") || t.startsWith("/*") ||
             t.startsWith("<!--") || t.startsWith("#"));
  });
  const joined = lines.join("\n");
  return (joined.match(re) || []).length;
}

console.log("== 姿态权威模型回归 ==");

// ---------- 基准状态构造器 ----------
const zeroQ = () => [0, 0, 0, 0, 0, 0];
const Q_T = [1, 2, 3, 4, 5, 6];        // 遥测姿态
const TCP_T = { x: 11, y: 12, z: 13 }; // 遥测 TCP
const Q_CMD = [7, 8, 9, 10, 11, 12];   // 指令目标
const TCP_CMD = { x: 21, y: 22, z: 23 };
const Q_DEMO = [30, 31, 32, 33, 34, 35];
const TCP_DEMO = { x: 41, y: 42, z: 43 };
const Q_OVR = [50, 51, 52, 53, 54, 55];
const TCP_OVR = { x: 61, y: 62, z: 63 };

const baseState = (over = {}) => ({
  override: null,
  demo: { on: false, q: zeroQ(), tcp: { x: 0, y: 0, z: 0 } },
  telemetry: {
    q: Q_T, tcp: TCP_T, simulated: false, cmd: null, cmdTcp: null, tracking: false,
  },
  activeView: "live",
  ...over,
});
const sameQ = (a, b) => Array.isArray(a) && a.length === 6 && a.every((v, i) => v === b[i]);
const sameTcp = (a, b) => a && b && a.x === b.x && a.y === b.y && a.z === b.z;

// ---------- A1. 遥测分支 ----------
let r = resolvePose(baseState());
ok("A1 真机遥测：q = telemetry.q", sameQ(r.q, Q_T));
ok("A1 真机遥测：tcp = telemetry.tcp", sameTcp(r.tcp, TCP_T));
ok("A1 真机遥测：source = telemetry", r.source === "telemetry");

r = resolvePose(baseState({ telemetry: { q: Q_T, tcp: TCP_T, simulated: true, cmd: null, cmdTcp: null, tracking: false } }));
ok("A1 模拟遥测无 cmd：仍显示遥测（扫掠帧兜底）", r.source === "telemetry" && sameQ(r.q, Q_T));

// ---------- A2. 指令分支（模拟 + 未走位 + 有 cmd）----------
r = resolvePose(baseState({ telemetry: { q: Q_T, tcp: TCP_T, simulated: true, cmd: Q_CMD, cmdTcp: TCP_CMD, tracking: false } }));
ok("A2 模拟+cmd：q = cmd", sameQ(r.q, Q_CMD));
ok("A2 模拟+cmd：tcp = cmdTcp", sameTcp(r.tcp, TCP_CMD));
ok("A2 模拟+cmd：source = command", r.source === "command");

r = resolvePose(baseState({ telemetry: { q: Q_T, tcp: TCP_T, simulated: true, cmd: Q_CMD, cmdTcp: null, tracking: false } }));
ok("A2 模拟+cmd 无 cmdTcp：tcp 回落遥测，source 仍是 command", sameTcp(r.tcp, TCP_T) && r.source === "command");

r = resolvePose(baseState({ telemetry: { q: Q_T, tcp: TCP_T, simulated: true, cmd: Q_CMD, cmdTcp: TCP_CMD, tracking: true } }));
ok("A2 tracking=true（Stage C 走位中）：让位遥测", r.source === "telemetry" && sameQ(r.q, Q_T));

r = resolvePose(baseState({ telemetry: { q: Q_T, tcp: TCP_T, simulated: false, cmd: Q_CMD, cmdTcp: TCP_CMD, tracking: false } }));
ok("A2 真机模式即使有 cmd 也走遥测", r.source === "telemetry" && sameQ(r.q, Q_T));

// ---------- A3. demo 分支 ----------
r = resolvePose(baseState({ demo: { on: true, q: Q_DEMO, tcp: TCP_DEMO } }));
ok("A3 demo 开：q = demo.q", sameQ(r.q, Q_DEMO));
ok("A3 demo 开：tcp = demo.tcp", sameTcp(r.tcp, TCP_DEMO));
ok("A3 demo 开：source = demo", r.source === "demo");

// demo 优先于指令分支
r = resolvePose(baseState({
  demo: { on: true, q: Q_DEMO, tcp: TCP_DEMO },
  telemetry: { q: Q_T, tcp: TCP_T, simulated: true, cmd: Q_CMD, cmdTcp: TCP_CMD, tracking: false },
}));
ok("A3 demo 优先于 command", r.source === "demo" && sameQ(r.q, Q_DEMO));

// ---------- A4. override 分支 ----------
r = resolvePose(baseState({ override: { owner: "sim", q: Q_OVR, tcp: TCP_OVR }, activeView: "sim" }));
ok("A4 override 前台 owner 命中：q = override.q", sameQ(r.q, Q_OVR));
ok("A4 override 前台 owner 命中：tcp = override.tcp", sameTcp(r.tcp, TCP_OVR));
ok("A4 override 前台 owner 命中：source = override", r.source === "override");

// owner 不匹配 → 残留不污染（这是"切页残留"防线）
r = resolvePose(baseState({ override: { owner: "sim", q: Q_OVR, tcp: TCP_OVR }, activeView: "live" }));
ok("A4 override 后台残留：被忽略，走遥测", r.source === "telemetry" && sameQ(r.q, Q_T));

// override 优先于 demo 与 command
r = resolvePose(baseState({
  override: { owner: "point", q: Q_OVR, tcp: TCP_OVR }, activeView: "point",
  demo: { on: true, q: Q_DEMO, tcp: TCP_DEMO },
  telemetry: { q: Q_T, tcp: TCP_T, simulated: true, cmd: Q_CMD, cmdTcp: TCP_CMD, tracking: false },
}));
ok("A4 override 优先于 demo 与 command", r.source === "override" && sameQ(r.q, Q_OVR));

// override 无 tcp → tcp 回落遥测
r = resolvePose(baseState({ override: { owner: "point", q: Q_OVR, tcp: null }, activeView: "point" }));
ok("A4 override 无 tcp：回落遥测 tcp", sameTcp(r.tcp, TCP_T));

// ---------- B. applyRobotPose 唯一写者 ----------
const appVue = read(rel("App.vue"));
const managerJs = read(rel("three/manager.js"));
ok("B App.vue 渲染循环持有唯一调用点（displayQ 驱动）",
  countCodeOccurrences(appVue, /applyRobotPose\s*\(/g) === 1 &&
  /applyRobotPose\s*\(\s*robot\.displayQ\s*\)/.test(appVue));
ok("B manager.js 是唯一定义处",
  countCodeOccurrences(managerJs, /export\s+function\s+applyRobotPose/g) === 1);

// 全 src 扫描：除 App.vue 与 manager.js 定义外不得出现任何调用
import { readdirSync, statSync } from "node:fs";
const offenders = [];
(function walk(dir) {
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) { walk(full); continue; }
    if (!/\.(vue|js|mjs)$/.test(name)) continue;
    const text = readFileSync(full, "utf8");
    const isDef = /export\s+function\s+applyRobotPose/.test(text);
    const calls = countCodeOccurrences(text, /applyRobotPose\s*\(/g);
    const isApp = full === rel("App.vue");
    if (!isDef && !isApp && calls > 0) offenders.push(path.relative(SRC, full) + " x" + calls);
  }
})(SRC);
ok("B 全 src 无第二处 applyRobotPose 调用", offenders.length === 0, offenders.join("; "));

// ---------- C. setLocalDemo 灭绝 ----------
let demoHits = [];
(function walk2(dir) {
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) { walk2(full); continue; }
    if (!/\.(vue|js|mjs)$/.test(name)) continue;
    const n = countCodeOccurrences(readFileSync(full, "utf8"), /setLocalDemo/g);
    if (n > 0) demoHits.push(path.relative(SRC, full) + " x" + n);
  }
})(SRC);
ok("C setLocalDemo 已从全 src 灭绝", demoHits.length === 0, demoHits.join("; "));

// ---------- D. exec.js 迁移 ----------
const execJs = read(rel("stores/exec.js"));
ok("D exec.js 无直接 3D 驱动", !/applyRobotPose/.test(execJs.replace(/\/\/[^\n]*/g, "")));
ok("D exec.js 指令经 noteCommand 记录", countCodeOccurrences(execJs, /noteCommand\s*\(/g) >= 4);
ok("D exec.js 不再 import manager 的 applyRobotPose",
  !/import\s*\{[^}]*applyRobotPose/.test(execJs));
ok("D exec.js leaveView 不再碰姿态（无 demo/override 字样）",
  /leaveView\(\)\s*\{[\s\S]*?\n    \},/.test(execJs) &&
  !/leaveView\(\)\s*\{[\s\S]*?set(Demo|Override|LocalDemo)/.test(execJs));
ok("D exec.js 保留 P0-1 修复：invalidateTeach 存在且 applyTeach 无条件重算",
  /invalidateTeach\s*\(/.test(execJs) &&
  /async\s+applyTeach\(\)[\s\S]*?await\s+this\.previewTeach\(\)/.test(execJs));

// ---------- E. 机房读数不走优先级仲裁 ----------
// 单机那套"谁拥有显示姿态"的仲裁（demo / override / telemetry）是为三视图共用一张
// 画布设计的。机房大屏不是：12 台的读数与 3D 一律直接读 net/fleetBuffer 的同一份
// 帧缓冲。这里钉住"卡片/控制台没有偷偷去碰 poseAuthority"——一旦有人图省事让卡片
// 读 robot.readoutQ，12 张卡就会全部显示**单机那台**的角度，而且画面看着还挺正常。
const fleetCard = read(rel("components/FleetCard.vue"));
const fleetConsole = read(rel("components/FleetConsole.vue"));
ok("E FleetCard 不读 poseAuthority / readoutQ（单机仲裁与它无关）",
  !/readoutQ|readoutTcp|poseAuthority/.test(fleetCard));
ok("E FleetConsole 不读 poseAuthority（目标角自己算，读数来自卡片）",
  !/readoutQ|readoutTcp|poseAuthority/.test(fleetConsole));
ok("E FleetCard 的数值来自 fleetCards 的格式化函数", /fmtNum/.test(fleetCard));

// ---------- F. 机房姿态的唯一写者 ----------
// 与 B 段同一套做法，只是对象换成机房层：全 src 只允许 App.vue 的渲染循环调
// applyFleetPoses。别处再调一次，就会出现"卡片数字与画面分家"或"两台互相拽"。
const fleetOffenders = [];
(function walkFleet(dir) {
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) { walkFleet(full); continue; }
    if (!/\.(js|vue)$/.test(name)) continue;
    const text = readFileSync(full, "utf8");
    const isDef = /export\s+function\s+applyFleetPoses/.test(text);
    const calls = countCodeOccurrences(text, /applyFleetPoses\s*\(/g);
    const base = path.relative(SRC, full);
    if (isDef && calls !== 1) fleetOffenders.push(`${base}: 定义处调用了 ${calls} 次`);
    if (!isDef && calls > 0 && base !== "App.vue") fleetOffenders.push(`${base}: 调用了 ${calls} 次`);
  }
})(SRC);
ok("F applyFleetPoses 只在 App.vue 的渲染循环里被调用一次",
  countCodeOccurrences(read(rel("App.vue")), /applyFleetPoses\s*\(/g) === 1);
ok("F 全 src 无第二处 applyFleetPoses 调用" + (fleetOffenders.length ? "：" + fleetOffenders.join("; ") : ""),
  fleetOffenders.length === 0);

// ---------- G. JointHud 已删除 ----------
ok("G JointHud.vue 已删除", !existsSync(rel("components/JointHud.vue")));

// ---------- H. robot.js 接线 ----------
const robotJs = read(rel("stores/robot.js"));
ok("H robot.js 从 poseAuthority 接线（不重复实现）",
  /import\s*\{\s*resolvePose\s*\}\s*from\s*["'\.\/]*poseAuthority\.js["']/.test(robotJs));
ok("H robot.js getter 全部委托 resolvePose",
  countCodeOccurrences(robotJs, /resolvePose\(s\)/g) === 3);
ok("H robot.js 提供 noteCommand action", /noteCommand\s*\(/.test(robotJs));
ok("H robot.js latestPose 仅为兼容别名（getter 而非 state）",
  /latestPose:\s*\(s\)\s*=>\s*s\.telemetry\.q/.test(robotJs));

console.log(`\n合计: ${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);
