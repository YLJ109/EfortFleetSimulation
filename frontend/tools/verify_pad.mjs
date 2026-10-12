// =====================================================================
// 手柄执行页回归（headless）—— 源码路径扫描（判据/接线/按键映射）
//
//   为什么需要它：本页把"手柄按键"翻译成真机动作，任何一处**判据写错**都很危险：
//     · 守卫漏了 tab → 同页两个 tab 同时响应，一次按键两个动作；
//     · 忘了"未启动（armed）"闸门 → 误碰手柄就动机器人；
//     · 离开页面不落锁 → 人去看棋了、手柄还带着真机权限；
//     · 气路绕过 exec.vacuum 自己 fetch → 锁存/排队口径分叉。
//   这些编译都不报、界面也看着正常。这里逐条钉死。
//
// 运行： cd frontend && node tools/verify_pad.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

/** 取 .vue 的 <script> 段。 */
function scriptOf(vue) {
  const m = /<script\b[^>]*>([\s\S]*?)<\/script>/i.exec(vue);
  return m ? m[1] : "";
}

const tabs = read("src/tabs.js");
const gp = read("src/games/gamepad.js");
const store = read("src/stores/pad.js");
const view = read("src/components/PadExecView.vue");
const map2d = read("src/pad/PadMap2D.vue");
const nav = read("src/pad/nav.js");
const sortView = read("src/components/SortingView.vue");
const appVue = read("src/App.vue");
const card = read("src/components/TicTacToeCard.vue");
const board = read("src/components/TicTacToeBoard.vue");
const box = read("src/components/GamepadTestCard.vue");

console.log("== A. 顶栏入口（左侧 / 需授权 / 与 shortcuts 键位数一致） ==");
{
  ok("A1 tabs.js 注册了 pad（手柄执行）",
    /\{ key: "pad"[\s\S]{0,160}label: "手柄执行"/.test(tabs));
  ok("A2 side:left 且 needAuth:true",
    /\{ key: "pad"[^}]*side: "left"/.test(tabs) && /\{ key: "pad"[^}]*needAuth: true/.test(tabs));
  ok("A3 排在 ai 之后（用户：应该是左边吧）",
    tabs.indexOf('key: "ai"') < tabs.indexOf('key: "pad"'));
  ok("A4 App.vue 挂载 PadExecView（v-show activeView==='pad'）",
    /import PadExecView/.test(appVue)
    && /<PadExecView\s+v-show="robot\.activeView === 'pad'"/.test(appVue));
}

console.log("");
console.log("== B. gamepad.js：LB/RB 有了映射（此前完全没接） ==");
{
  ok("B1 BTN.LB(4) → l1、BTN.RB(5) → r1",
    /\[BTN\.LB\]: "l1"/.test(gp) && /\[BTN\.RB\]: "r1"/.test(gp));
  ok("B2 ACTION_CN 补了 l1/r1（测试盒日志用）",
    /l1: "LB"/.test(gp) && /r1: "RB"/.test(gp));
}

console.log("");
console.log("== C. pad store：总闸 + 按键→动作（唯一入口，全部经既有接口） ==");
{
  ok("C1 有「未启动」闸门（_gate 检查 armed）", /_gate\(\)/.test(store) && /this\.armed/.test(store));
  ok("C2 未授权也拒绝（controlActive）", /controlActive/.test(store));
  ok("C3 Start = 启动/关闭 toggle（toggleArmed）", /toggleArmed\(\)/.test(store));
  ok("C4 去点走 POST /sorting/goto（不新造接口）", /apiControl\("\/sorting\/goto"/.test(store));
  ok("C5 去点速度取右上角滑块（exec.speed，全项目唯一速度口径）",
    /speed_pct: useExecStore\(\)\.speed/.test(store));
  ok("C6 气路复用 exec.vacuum（**不自己 fetch /control/vacuum**，免得锁存/排队口径分叉）",
    /\.vacuum\(/.test(store) && !/apiControl\("\/control\/vacuum"/.test(store));
  ok("C7 Y（正上位）非槽位点明确拒绝、不乱指（abovePoint → null 分支）",
    /abovePoint\(this\.cursor\)/.test(store) && /没有正上位/.test(store));
  ok("C8 离开本页 / 切 tab 会落锁（disarm）", /disarm\(/.test(store));
  // 按键 → 动作（A=原位 Y=正上位 X=吸气toggle B=放气toggle LB=J6正面 RB=J6反面 Start=启停）
  ok("C9 A(confirm) → 去原位", /case "confirm": this\.gotoOrig\(\)/.test(store));
  ok("C10 Y(replay) → 去正上位", /case "replay": this\.gotoAbove\(\)/.test(store));
  ok("C11 X(pause) → 吸气⇄停吸 toggle", /case "pause": this\.toggleSuck\(\)/.test(store));
  ok("C12 B(stop) → 放气⇄停放 toggle", /case "stop": this\.toggleBlow\(\)/.test(store));
  ok("C13 LB(l1) → 切 J6 正面（走原地切面点，不是整姿态到点）",
    /case "l1": this\.gotoFaceFront\(\)/.test(store)
    && /FACE_POINT_FRONT = "J6正面"/.test(store));
  ok("C14 RB(r1) → 切 J6 反面",
    /case "r1": this\.gotoFaceBack\(\)/.test(store)
    && /FACE_POINT_BACK = "J6反面"/.test(store));
  // ★ 与后端同口径：点名必须与 OPTIONAL_EXTRA_POINTS 一字不差（前端改了后端没改会立刻红）。
  //   ★★ 2026-10-12 终版：这两点是**虚拟切面命令**（无需示教、不读库），后端已改为
  //   符号常量定义 → 断言同时钉住常量行与中文字面量（防止任何一侧悄悄改名）。
  ok("C14b J6 正/反面点名与后端 OPTIONAL_EXTRA_POINTS 一字不差",
    /OPTIONAL_EXTRA_POINTS[^=]*=\s*\(J6_FACE_FRONT_POINT,\s*J6_FACE_BACK_POINT\)/.test(
      read("../backend/app/services/sorting.py"))
    && /J6_FACE_FRONT_POINT = "J6正面"/.test(read("../backend/app/services/sorting.py"))
    && /J6_FACE_BACK_POINT = "J6反面"/.test(read("../backend/app/services/sorting.py")));
  ok("C15 Start(start) → 启动/关闭", /case "start": this\.toggleArmed\(\)/.test(store));
  ok("C16 方向键 → 移动光标（纯前端、不需 armed）",
    /case "up": case "down": case "left": case "right":\s*\n\s*this\.moveDir\(action\)/.test(store)
    || /this\.moveDir\(action\)/.test(store));
  ok("C17 吸气 toggle 按 exec.vacuumState 判（与既有锁存同源，不另立状态）",
    /vacuumState === "suck"/.test(store) && /vacuumState === "blow"/.test(store));
}

console.log("");
console.log("== D. PadExecView：手柄归属 + 落锁 + 右栏两 tab ==");
{
  const vs = scriptOf(view);
  ok("D1 手柄订阅带守卫（本页 + exec tab，防同页两个 tab 抢键）",
    /robot\.activeView !== "pad"/.test(vs) && /pad\.tab !== PAD_TAB_EXEC/.test(vs));
  ok("D2 离开本页 → 落锁 + 停井字棋轮询",
    /pad\.disarm\(/.test(vs) && /ttt\.stopPolling\(\)/.test(vs));
  ok("D3 切到井字棋 tab → 真机动作落锁",
    /PAD_TAB_TTT\) pad\.disarm\(/.test(vs));
  ok("D4 右栏两个 tab（执行 / 井字棋）", /pad\.setTab\('exec'\)/.test(view)
    && /pad\.setTab\('ttt'\)/.test(view));
  ok("D5 挂载 2D 执行图 / 井字棋棋盘 / 井字棋卡片 / 手柄测试盒",
    /<PadMap2D/.test(view) && /<TicTacToeBoard/.test(view)
    && /<TicTacToeCard/.test(view) && /<GamepadTestCard/.test(view));
  ok("D6 2D 图只在执行 tab 显示 / 棋盘只在井字棋 tab 显示",
    /showMap = computed\(\(\) => pad\.tab === PAD_TAB_EXEC\)/.test(vs)
    && /showTttBoard = computed\(\(\) => pad\.tab === PAD_TAB_TTT && ttt\.gameVisible\)/.test(vs));
}

console.log("");
console.log("== E. 2D 执行图（布局口径：左 B / 中下 C / 上起点 / 右 A） ==");
{
  ok("E1 复用 nav.js 的格子顺序（不自己重排）",
    /from "\.\.\/pad\/nav\.js"/.test(scriptOf(map2d))
    && /ZONE_A/.test(map2d) && /ZONE_B/.test(map2d));
  const tpl = map2d.slice(map2d.indexOf("<template>"));
  ok("E2 起点在上一行、C 检测在中间、B 在左 A 在右（模板渲染顺序）",
    tpl.indexOf("pm-toprow") > -1 && tpl.indexOf("pm-toprow") < tpl.indexOf("pm-mid")
    && tpl.indexOf("ZONE_B") > -1 && tpl.indexOf("ZONE_B") < tpl.indexOf("ZONE_A"));
  ok("E3 点格子 = 只改选中（pick → pad.select，不动机器人）",
    /function pick\(name\) \{ pad\.select\(name\); \}/.test(map2d));
  ok("E4 选中态来自 store（单一判据）", /pad\.cursor === name/.test(map2d));
}

console.log("");
console.log("== F. 井字棋已从「智能分拣」页移走（一处入口） ==");
{
  ok("F1 分拣页不再渲染井字棋卡片/棋盘/手柄测试盒",
    !/<TicTacToeCard/.test(sortView) && !/<TicTacToeBoard/.test(sortView)
    && !/<GamepadTestCard/.test(sortView) && !/import TicTacToe/.test(sortView));
  ok("F2 分拣页 tab 只剩 执行 / 点位",
    /tab === 'run'/.test(sortView) && /tab === 'point'/.test(sortView)
    && !/tab === 'ttt'/.test(sortView));
  ok("F3 井字棋两个组件的手柄守卫都改到 pad 页（不会在分拣页偷响应）",
    /robot\.activeView !== "pad"/.test(card) && /pad\.tab !== PAD_TAB_TTT/.test(card)
    && /robot\.activeView === "pad"/.test(board) && /pad\.tab === PAD_TAB_TTT/.test(board));
}

console.log("");
console.log("== G. 测试盒按 tab 换表（移除本 tab 不存在的测试键） ==");
{
  ok("G1 按 pad.tab 切换模拟键与对照表",
    /pad\.tab === PAD_TAB_TTT \? SIMS_TTT : SIMS_EXEC/.test(box)
    && /pad\.tab === PAD_TAB_TTT \? MAP_TTT : MAP_EXEC/.test(box));
  ok("G2 执行 tab 含 LB/RB 两条（l1/r1）", /a: "l1"/.test(box) && /a: "r1"/.test(box));
  ok("G2b 执行 tab 的模拟键标成 LB 正面 / RB 反面（不写「停吸/停放」）",
    /LB 正面/.test(box) && /RB 反面/.test(box)
    && !/LB 停吸/.test(box) && !/RB 停放/.test(box));
  ok("G3 nav.js 是唯一邻接表来源（含 START/CAMERA 常量）",
    /export const START/.test(nav) && /export const CAMERA/.test(nav) && /export const NAV/.test(nav));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
