// =====================================================================
// 井字棋对弈回归（headless）—— 纯逻辑断言 + 源码输入路径扫描
//
//   为什么需要它：井字棋的"拼错一条三连线 / 光标越界 / 结果枚举写反"这类错误
//   编译不报、界面看着也正常，只有真机下起来才发现"明明三连了却不判胜"。
//   所以把**纯逻辑**（games/tictactoe.js）拉到 node 里直接断言，
//   并用源码扫描钉住"键盘 ↑↓←→ + Enter"与"鼠标点格"两条输入路径确实存在。
//
//   ★ 与后端同口径：再从 backend/app/services/tictactoe.py 解析 WIN_LINES 与
//     难度中文名来对拍 —— 前端改了、后端没改，这里立刻红。
//
// 运行： cd frontend && node tools/verify_tictactoe.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  O, X, WIN_LINES, DIFF_CN, newBoard, winner, isDraw, isFull, legal, other,
  cellToSlot, slotToCell, resultOf, moveCursor, diffCn, resultCn, symCn,
} from "../src/games/tictactoe.js";
import { createSortingBlocks } from "../src/three/sortingBlocks.js";
import {
  pressedButtons, edgeButtons, dpadDir, stickDir, actionsOf, subscribeGamepad,
  simulateAction, readPad, readPads, ACTION_CN, BUTTON_CN,
  enableBackendFallback, gamepadSource, rstickDir, RSTICK_ACTION, RSTICK_DEADZONE,
  litButtons,
} from "../src/games/gamepad.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

/** 取 .vue 的 <script> 段；再剥掉整行 // 注释（判"有没有某能力"时不受注释干扰）。 */
function scriptOf(vue) {
  const m = /<script\b[^>]*>([\s\S]*?)<\/script>/i.exec(vue);
  return m ? m[1] : "";
}
const stripComments = (src) => src.split("\n")
  .filter((l) => !l.trim().startsWith("//")).join("\n");

/** 造一个棋盘：给出 {cell: "O"|"X"} 映射。 */
function mk(map) {
  const b = newBoard();
  for (const [i, s] of Object.entries(map)) b[Number(i)] = s;
  return b;
}

console.log("== A. 棋盘 ↔ B 槽位映射（与后端同口径） ==");
{
  ok("WIN_LINES 共 8 条", WIN_LINES.length === 8, "实际 " + WIN_LINES.length);
  ok("cellToSlot(0) = B1", cellToSlot(0) === "B1");
  ok("cellToSlot(8) = B9", cellToSlot(8) === "B9");
  ok("越界 → 空串（不抛）", cellToSlot(-1) === "" && cellToSlot(9) === "");
  ok("slotToCell B1=0 / B9=8", slotToCell("B1") === 0 && slotToCell("B9") === 8);
  ok("大小写/空白不敏感（b5 → 4）", slotToCell(" b5 ") === 4);
  ok("非 B1..B9 → null", slotToCell("A1") === null && slotToCell("B10") === null
    && slotToCell("C") === null && slotToCell("") === null);
  let roundtrip = true;
  for (let i = 0; i < 9; i++) if (slotToCell(cellToSlot(i)) !== i) roundtrip = false;
  ok("cell → slot → cell 往返一致（0..8）", roundtrip);
}

console.log("");
console.log("== B. 胜负判定（横 / 竖 / 斜 全覆盖） ==");
{
  // 8 条三连线逐一构造，符号交替 O/X —— 确保"逐线判胜"，不只看第一条
  let allHit = true;
  WIN_LINES.forEach((line, k) => {
    const sym = k % 2 === 0 ? O : X;
    const b = newBoard();
    line.forEach((i) => { b[i] = sym; });
    const w = winner(b);
    if (!w || w.sym !== sym || JSON.stringify(w.line) !== JSON.stringify(line)) allHit = false;
  });
  ok("8 条三连线逐条判胜正确（O/X 交替）", allHit);

  const row = mk({ 0: X, 1: X, 2: X });
  ok("横三：line = [0,1,2]", JSON.stringify(winner(row).line) === "[0,1,2]");
  const col = mk({ 0: O, 3: O, 6: O });
  ok("竖三：line = [0,3,6]", JSON.stringify(winner(col).line) === "[0,3,6]");
  const dg = mk({ 0: O, 4: O, 8: O });
  ok("斜三：line = [0,4,8]", JSON.stringify(winner(dg).line) === "[0,4,8]");
  const dg2 = mk({ 2: X, 4: X, 6: X });
  ok("斜三：line = [2,4,6]", JSON.stringify(winner(dg2).line) === "[2,4,6]");
  ok("无三连 → null", winner(mk({ 0: O, 1: X, 2: O })) === null);
  ok("反证：空棋盘不判胜", winner(newBoard()) === null);
}

console.log("");
console.log("== C. 平局 / 满格 ==");
{
  const draw = mk({ 0: X, 1: O, 2: X, 3: X, 4: O, 5: O, 6: O, 7: X, 8: X });
  ok("满格", isFull(draw));
  ok("满格且无三连 → 平局", isDraw(draw) && winner(draw) === null);
  ok("空棋盘不是满格、不是平局", !isFull(newBoard()) && !isDraw(newBoard()));
  const nearWin = mk({ 0: O, 1: O, 2: O, 3: X, 4: X });
  ok("有三连 → 不算平局", isDraw(nearWin) === false);
}

console.log("");
console.log("== D. resultOf 枚举（\"\" / user_win / robot_win / draw） ==");
{
  const oWin = mk({ 0: O, 1: O, 2: O });
  ok("O 三连 + 用户执 O → user_win", resultOf(oWin, O).result === "user_win"
    && JSON.stringify(resultOf(oWin, O).winLine) === "[0,1,2]");
  ok("O 三连 + 用户执 X → robot_win", resultOf(oWin, X).result === "robot_win");
  const draw = mk({ 0: X, 1: O, 2: X, 3: X, 4: O, 5: O, 6: O, 7: X, 8: X });
  ok("满格无三连 → draw（winLine 为空）",
    resultOf(draw, O).result === "draw" && resultOf(draw, O).winLine.length === 0);
  ok("进行中 → 空结果", resultOf(mk({ 0: O }), O).result === ""
    && resultOf(mk({ 0: O }), O).winLine.length === 0);
  ok("resultCn 映射齐全", resultCn("user_win") === "你赢了"
    && resultCn("robot_win") === "你输了" && resultCn("draw") === "平局"
    && resultCn("") === "进行中");
}

console.log("");
console.log("== E. 光标边界（↑↓←→ 夹住、不环绕） ==");
{
  ok("第 1 行 ↑ 停住", moveCursor(0, "up") === 0 && moveCursor(2, "up") === 2);
  ok("第 3 行 ↓ 停住", moveCursor(6, "down") === 6 && moveCursor(8, "down") === 8);
  ok("第 1 列 ← 停住", moveCursor(0, "left") === 0 && moveCursor(3, "left") === 3);
  ok("第 3 列 → 停住（不绕到第 1 列）", moveCursor(2, "right") === 2
    && moveCursor(5, "right") === 5 && moveCursor(8, "right") === 8);
  ok("中间：↑↓←→ 各移一格", moveCursor(4, "up") === 1 && moveCursor(4, "down") === 7
    && moveCursor(4, "left") === 3 && moveCursor(4, "right") === 5);
  ok("未知方向 → 原样返回", moveCursor(4, "x") === 4);
  // 反证：任意格 + 任意方向都落在 0..8，且**同行/同列相邻**（没有跳行/跳列）
  let sane = true;
  const dirs = ["up", "down", "left", "right"];
  for (let i = 0; i < 9; i++) {
    for (const d of dirs) {
      const n = moveCursor(i, d);
      if (n < 0 || n > 8) sane = false;
      const dr = Math.abs(Math.floor(n / 3) - Math.floor(i / 3));
      const dc = Math.abs((n % 3) - (i % 3));
      const moveOk = (d === "up" || d === "down") ? (dc === 0 && dr <= 1)
        : (dr === 0 && dc <= 1);
      if (!moveOk) sane = false;
    }
  }
  ok("反证：任意移动都不越界、只在同行或同列相邻", sane);
}

console.log("");
console.log("== F. 走子合法性 / 对手 / 难度中文名 ==");
{
  const b = newBoard();
  ok("空格可落", legal(b, 0) && legal(b, 8));
  b[4] = O;
  ok("已占格不可落", legal(b, 4) === false);
  ok("越界不可落", legal(b, -1) === false && legal(b, 9) === false);
  ok("other(O)=X / other(X)=O", other(O) === X && other(X) === O);
  ok("难度中文名 easy=入门 / normal=普通 / hard=困难",
    diffCn("easy") === "入门" && diffCn("normal") === "普通" && diffCn("hard") === "困难");
  ok("非法难度回落「普通」", diffCn("") === "普通" && diffCn("XX") === "普通");
  ok("DIFF_CN 三个键齐全", Object.keys(DIFF_CN).length === 3);
  ok("symCn：O=○ / X=✕", symCn(O) === "○" && symCn(X) === "✕");
}

console.log("");
console.log("== G. 与后端对拍（services 纯逻辑 + api 接口；改了前端必须同步后端） ==");
{
  const py = read("../backend/app/services/tictactoe.py");
  const pyApi = read("../backend/app/api/tictactoe.py");
  const block = (py.match(/WIN_LINES[\s\S]*?\n\)/) || [""])[0];
  const pyLines = [...block.matchAll(/\((\d), (\d), (\d)\)/g)].map((m) => [+m[1], +m[2], +m[3]]);
  ok("后端 WIN_LINES 解析到 8 条", pyLines.length === 8, "实际 " + pyLines.length);
  ok("前端 WIN_LINES 与后端逐条一致",
    JSON.stringify(pyLines) === JSON.stringify(WIN_LINES),
    "py=" + JSON.stringify(pyLines));
  ok("后端难度中文名与前端一致",
    py.includes('"easy": "入门"') && py.includes('"normal": "普通"')
    && py.includes('"hard": "困难"'));
  ok("后端先手执圆：先手 = (O, X)，后手 = (X, O)",
    /\(TT\.O, TT\.X\) if first == "user" else \(TT\.X, TT\.O\)/.test(pyApi));
  ok("后端默认空跑（StartIn.dry_run 默认 True）",
    /dry_run:\s*bool\s*=\s*True/.test(pyApi));
}

console.log("");
console.log("== H. 前端输入路径（键盘 ↑↓←→ + Enter / 鼠标点格） ==");
{
  const boardVue = read("src/components/TicTacToeBoard.vue");
  const cardVue = read("src/components/TicTacToeCard.vue");
  const viewVue = read("src/components/SortingView.vue");
  const store = read("src/stores/tictactoe.js");
  const bs = scriptOf(boardVue);
  const bClean = stripComments(bs);

  ok("★ 键盘：处理 ArrowUp/Down/Left/Right",
    /ArrowUp/.test(bs) && /ArrowDown/.test(bs) && /ArrowLeft/.test(bs) && /ArrowRight/.test(bs));
  ok("★ 键盘：Enter 落子", /"Enter"/.test(bs));
  ok("★ 键盘：调用 moveCursor 移动光标", /moveCursor\(/.test(bs));
  ok("★ 键盘：不占用 Space（Space 已归语音）", !/["']Space["']/.test(bClean));
  ok("★ 键盘：焦点在输入框/文本域时不拦截",
    /isTyping/.test(bs) && /INPUT/.test(bs) && /TEXTAREA/.test(bs));
  ok("★ 键盘：仅「轮到你」时生效（canPlay 门控）", /canPlay/.test(bs) && /ttt\.canPlay/.test(bs));
  ok("★ 鼠标：点格落子 @click", /@click="onCell\(/.test(boardVue));
  ok("★ 鼠标：悬停与光标同步 @mouseenter", /@mouseenter="cursor = i"/.test(boardVue));
  ok("★ 落子统一走 store.move（键盘/鼠标同源，不是各写一套）",
    /ttt\.move\(/.test(bs) && /async move\(/.test(store));

  ok("卡片：暂停⇄继续按钮存在且为琥珀色 .pause-btn",
    /ttt\.togglePause/.test(cardVue) && /pause-btn/.test(cardVue));
  ok("卡片：停止（红色）按钮存在", /ttt\.stop/.test(cardVue) && /stop-btn/.test(cardVue));
  ok("卡片：开始按钮存在", /ttt\.start\(/.test(cardVue) && /doStart/.test(cardVue));
  ok("卡片：难度三档单选（入门/普通/困难）",
    /DIFFS/.test(cardVue) && /diffCn/.test(cardVue));
  ok("卡片：真实下发需二次确认（window.confirm）", /window\.confirm/.test(cardVue));
  ok("卡片：鼠标点击可用（按钮 @click）", /@click=/.test(cardVue));

  ok("分拣页挂载了 2D 棋盘浮层", /<TicTacToeBoard/.test(viewVue));
  ok("分拣页挂载了井字棋卡片", /<TicTacToeCard/.test(viewVue));
  ok("分拣页进出启停井字棋轮询", /ttt\.startPolling\(\)/.test(viewVue)
    && /ttt\.stopPolling\(\)/.test(viewVue));

  console.log("");
  console.log("== I. store 口径（令牌 / 默认空跑 / 首轮静默 / 语音 / 复用暂停端点） ==");
  ok("全部请求走 apiControl（带控制令牌）", [...store.matchAll(/apiControl\(/g)].length >= 6);
  ok("未授权不发请求（_authed 门控）", /_authed\(/.test(store) && /controlActive/.test(store));
  ok("默认空跑 dryRun: true", /dryRun:\s*true/.test(store));
  ok("首轮静默：poll(true) 对齐游标", /poll\(true\)/.test(store) && /silent/.test(store));
  ok("暂停/继续复用 /control/run-pause|run-resume",
    /\/control\/run-pause/.test(store) && /\/control\/run-resume/.test(store));
  ok("用户赢 → 语音祝贺语（只在该结果变化处触发）",
    /恭喜你赢了/.test(store) && /speak\(/.test(store));
  ok("落子/停止/复位/暂停后立刻刷新一次 state（await this.poll(false)）",
    (store.match(/await this\.poll\(false\)/g) || []).length >= 4);
  ok("复盘只放 2D：replay 来自 GET /tictactoe/replay",
    /\/tictactoe\/replay/.test(store));
  // ★★ 防回归钉（2026-10-10 现场事故）：apiUrl() 会自己拼 `/api`，
  //   若调用方再写 `apiControl("/api/...")`，真实请求会变成 `/api/api/...` → 全部 404。
  //   这条断言把"路径不带 /api 前缀"钉死。
  ok("apiControl 路径不带 /api 前缀（apiUrl 会拼 /api，避免 /api/api 双前缀）",
    !/apiControl\(\s*["'`]\/api\//.test(store));
}

console.log("");
console.log("== J. 3D 联动（B 区按棋盘显隐 / A 区按已取用隐藏） ==");
{
  const slots = [];
  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 3; c++) {
      const i = r * 3 + c;
      slots.push({ name: "A" + (i + 1), role: "A", pos: [300 + c * 90, 400 - r * 90, 0], taught: true });
      slots.push({ name: "B" + (i + 1), role: "B", pos: [600 + c * 90, 400 - r * 90, 0], taught: true });
    }
  }
  const payload = { slots, plane_z: 0, plane_h_mm: 0, block_mm: [70, 70, 14],
                    camera: { taught: false }, overlaps: [], missing: [],
                    plane_spread: 0, plane_tol: 5 };
  const sb = createSortingBlocks(0);
  sb.setSlots(payload);
  const board = newBoard();
  board[0] = O; board[4] = X;
  sb.setTtt({ board, used: ["A1", "A6"], holding: false });
  sb.update({});                       // 模拟分拣轮询喂一次 state（分拣字段为空）
  const bVis = {};
  sb.root.children.forEach((ch) => {
    if (ch.userData && ch.userData.sortSlot) bVis[ch.userData.sortSlot] = ch.visible;
  });
  ok("board[0] 已落 → B1 显示", bVis.B1 === true, "B1=" + bVis.B1);
  ok("board[4] 已落 → B5 显示", bVis.B5 === true, "B5=" + bVis.B5);
  ok("空格 → B2 不显示", bVis.B2 === false, "B2=" + bVis.B2);
  const aVis = {};
  sb.root.children.forEach((ch) => {
    if (ch.userData && ch.userData.sortName) aVis[ch.userData.sortName] = ch.visible;
  });
  ok("used 里的 A1 → 隐藏", aVis.A1 === false, "A1=" + aVis.A1);
  ok("A6（已取用）隐藏", aVis.A6 === false, "A6=" + aVis.A6);
  ok("A2（未取用）仍在", aVis.A2 === true, "A2=" + aVis.A2);
  sb.setTtt(null);                     // 清除覆盖（无对局）
  sb.update({});
  const b2 = {};
  sb.root.children.forEach((ch) => {
    if (ch.userData && ch.userData.sortSlot) b2[ch.userData.sortSlot] = ch.visible;
  });
  const a2 = {};
  sb.root.children.forEach((ch) => {
    if (ch.userData && ch.userData.sortName) a2[ch.userData.sortName] = ch.visible;
  });
  ok("清除覆盖后 B 区全部隐藏（回到分拣显示）",
    Object.values(b2).every((v) => v === false));
  ok("清除覆盖后 A 区料全部恢复", Object.values(a2).every((v) => v === true));
  sb.dispose();
}

console.log("");
console.log("== K. 手柄（Xbox / 标准映射） ==");
{
  /** 造一个假手柄：指定按下的按钮下标与摇杆轴。 */
  const pad = (idx = [], axes = [0, 0]) => ({
    connected: true, id: "Xbox", mapping: "standard",
    buttons: Array.from({ length: 17 }, (_, i) =>
      ({ pressed: idx.includes(i), value: idx.includes(i) ? 1 : 0 })),
    axes,
  });

  ok("pressedButtons 只报按下的键",
    JSON.stringify(pressedButtons(pad([0, 12]))) === "[0,12]");
  ok("edgeButtons 只报**新**按下（长按不反复触发）",
    JSON.stringify(edgeButtons(new Set([0]), [0, 1])) === "[1]");
  ok("dpadDir：12/13/14/15 → up/down/left/right（其它键不产方向）",
    dpadDir(new Set([12])) === "up" && dpadDir(new Set([13])) === "down"
    && dpadDir(new Set([14])) === "left" && dpadDir(new Set([15])) === "right"
    && dpadDir(new Set([0])) === null);
  ok("stickDir：上 = axes[1] 为负；死区内 → null",
    stickDir(pad([], [0, -0.9])) === "up" && stickDir(pad([], [0, 0.9])) === "down"
    && stickDir(pad([], [-0.9, 0])) === "left" && stickDir(pad([], [0.9, 0])) === "right"
    && stickDir(pad([], [0.2, 0.2])) === null);
  // ★ 右摇杆 = 动作（axes[2]/[3]）；死区更大（0.7）防误触"停止"
  ok("rstickDir 读 axes[2]/[3]（右摇杆），死区 0.7",
    RSTICK_DEADZONE === 0.7
    && rstickDir(pad([], [0, 0, 0, -0.8])) === "up"
    && rstickDir(pad([], [0, 0, 0.8, 0])) === "right"
    && rstickDir(pad([], [0, 0, 0, 0.8])) === "down"
    && rstickDir(pad([], [0, 0, -0.8, 0])) === "left"
    && rstickDir(pad([], [0, 0, 0.6, 0.6])) === null);       // 未推到底 → 不触发
  ok("右摇杆 → 动作：↑下棋 →暂停 ↓停止 ←复盘",
    RSTICK_ACTION.up === "confirm" && RSTICK_ACTION.right === "pause"
    && RSTICK_ACTION.down === "stop" && RSTICK_ACTION.left === "replay");
  ok("右摇杆**边沿**触发（lastRDir：推住不放不会连发）",
    /const rd = rstickDir\(gp\)/.test(read("src/games/gamepad.js"))
    && /rd !== lastRDir/.test(read("src/games/gamepad.js"))
    && /lastRDir = rd/.test(read("src/games/gamepad.js")));
  ok("动作映射：A=落子 Menu=开始 X=暂停 B=停止 Y=复盘 View=复位",
    JSON.stringify(actionsOf([0])) === '["confirm"]'
    && JSON.stringify(actionsOf([9])) === '["start"]'
    && JSON.stringify(actionsOf([2])) === '["pause"]'
    && JSON.stringify(actionsOf([1])) === '["stop"]'
    && JSON.stringify(actionsOf([3])) === '["replay"]'
    && JSON.stringify(actionsOf([8])) === '["reset"]');
  ok("未映射按钮（LB=4）不产生任何动作", actionsOf([4]).length === 0);
  ok("subscribeGamepad 返回可取消函数（node 无 rAF 也不崩）",
    (() => { const un = subscribeGamepad(() => {}); const isFn = typeof un === "function";
             un(); return isFn; })());

  // 源码扫描：两处接线存在，且**分工不重叠**（一次按键不会触发两处）
  const boardSrc = read("src/components/TicTacToeBoard.vue");
  const cardSrc = read("src/components/TicTacToeCard.vue");
  ok("棋盘接手柄：订阅 + 方向移动 + A 落子",
    /subscribeGamepad\(/.test(boardSrc) && /moveCursor\(/.test(boardSrc)
    && /action === "confirm"/.test(boardSrc)
    && !/action === "start"/.test(boardSrc));
  ok("卡片接手柄：开始/暂停/停止/复盘/复位",
    /subscribeGamepad\(/.test(cardSrc)
    && /action === "start"/.test(cardSrc) && /action === "pause"/.test(cardSrc)
    && /action === "stop"/.test(cardSrc) && /action === "replay"/.test(cardSrc)
    && /action === "reset"/.test(cardSrc));
  ok("只在分拣页响应（不劫持全局手柄）",
    /robot\.activeView !== "sort"/.test(boardSrc)
    && /robot\.activeView !== "sort"/.test(cardSrc));
  ok("手柄复位走二次确认（不依赖手柄点不了的 confirm 弹窗）",
    /doReset\(true\)/.test(cardSrc) && /padResetArm/.test(cardSrc));
}

console.log("");
console.log("== L. 手柄测试盒（模拟触发 = 与真手柄同一条广播） ==");
{
  // ① 功能：simulateAction 必须经 emit 广播到订阅者；取消订阅后不再收到
  const got = [];
  const un = subscribeGamepad((a) => got.push(a));
  simulateAction("up");
  simulateAction("confirm");
  un();
  simulateAction("stop");      // 已退订 → 不应再入列
  ok("simulateAction 与真手柄走同一条广播（订阅者收到动作）",
    JSON.stringify(got) === '["up","confirm"]', JSON.stringify(got));
  ok("空/非法动作不广播（simulateAction 只认字符串）",
    (() => { const g2 = []; const u2 = subscribeGamepad((a) => g2.push(a));
             simulateAction(""); simulateAction(null); simulateAction(123); u2();
             return g2.length === 0; })());

  // ② 只读快照：node 无 navigator → 安全返回"未连接"，不抛
  const snap = readPad();
  ok("readPad 无手柄时安全返回未连接（不抛）",
    snap && snap.connected === false && Array.isArray(snap.buttons) && Array.isArray(snap.axes));
  ok("readPad 带 count/ids（诊断\"浏览器看见几个\"）",
    typeof snap.count === "number" && Array.isArray(snap.ids));
  ok("readPads 无手柄时返回空数组", Array.isArray(readPads()) && readPads().length === 0);

  // ②b 检测健壮性：除轮询外还听浏览器连接事件（有些环境事件到才有数据）
  const gpSrc = read("src/games/gamepad.js");
  ok("监听 gamepadconnected / gamepaddisconnected",
    /addEventListener\("gamepadconnected"/.test(gpSrc)
    && /addEventListener\("gamepaddisconnected"/.test(gpSrc));

  // ③ 说明文案齐全（界面直接用）
  ok("ACTION_CN 覆盖 10 个逻辑动作",
    ["up", "down", "left", "right", "confirm", "start", "pause", "stop", "replay", "reset"]
      .every((k) => !!ACTION_CN[k]));
  ok("BUTTON_CN 覆盖标准布局关键键（A/Menu/View/十字键）",
    BUTTON_CN[0] === "A" && BUTTON_CN[9] === "Menu" && BUTTON_CN[8] === "View"
    && BUTTON_CN[12] === "↑" && BUTTON_CN[15] === "→");

  // ④ 源码：测试盒存在、连上显示「手柄已连接」、10 个模拟按钮、已挂进分拣页
  const boxSrc = read("src/components/GamepadTestCard.vue");
  ok("测试盒：有手柄时显示「手柄已连接」（未连接给唤醒提示）",
    /手柄已连接/.test(boxSrc) && /未检测到手柄/.test(boxSrc) && /readPad\(\)/.test(boxSrc));
  ok("测试盒：10 个模拟触发按钮（含 方向/A/Start/X/B/Y/View）",
    /SIMS/.test(boxSrc) && /simulateAction/.test(boxSrc)
    && ["\"up\"", "\"down\"", "\"left\"", "\"right\"", "\"confirm\"", "\"start\"",
        "\"pause\"", "\"stop\"", "\"replay\"", "\"reset\""].every((s) => boxSrc.includes(s)));
  ok("测试盒：记录真实触发的动作（订阅同一条广播）",
    /subscribeGamepad\(onAction\)/.test(boxSrc));
  ok("测试盒：常显\"浏览器可见手柄 N 个\" + 检测不到时的排查清单（Win+R joy.cpl / XInput / Chrome / Steam 独占）",
    /浏览器可见手柄/.test(boxSrc) && /joy\.cpl/.test(boxSrc)
    && /XInput/.test(boxSrc) && /Chrome \/ Edge/.test(boxSrc)
    && /Steam/.test(boxSrc));
  ok("测试盒：有\"重新扫描\"按钮（点了立刻给一次反馈）",
    /rescan/.test(boxSrc) && /重新扫描/.test(boxSrc));
  ok("测试盒：有\"复制诊断\"（把浏览器看到的一键带走，绕开控制台粘贴限制）",
    /copyDiag/.test(boxSrc) && /复制诊断/.test(boxSrc) && /diagText/.test(boxSrc));
  ok("测试盒对照表含右摇杆动作（↑下棋 →暂停 ↓停止 ←复盘）",
    /右摇杆 ↑/.test(boxSrc) && /右摇杆 →/.test(boxSrc)
    && /右摇杆 ↓/.test(boxSrc) && /右摇杆 ←/.test(boxSrc));
  // ★ 高亮反馈（用户口径："我点击哪个点哪个点高亮"）
  ok("测试盒：键帽灯条（按哪个亮哪个）",
    /class="caps"/.test(boxSrc) && /class="cap"/.test(boxSrc) && /litSet\.has\(i\)/.test(boxSrc));
  ok("测试盒：触发的动作 → 对应模拟按钮闪一下",
    /flash === s\.a/.test(boxSrc) && /const flash = ref\(""\)/.test(boxSrc));
  ok("手柄模块：按键余晖（短按也看得见）+ litButtons 导出",
    /litButtons/.test(gpSrc) && /markLit\(cur, now\)/.test(gpSrc) && /LIT_MS/.test(gpSrc));
  ok("litButtons 在 node 返回数组（无手柄 → 空）",
    Array.isArray(litButtons()) && litButtons().length === 0);
  const sortVue2 = read("src/components/SortingView.vue");
  ok("分拣页挂载手柄测试盒", /<GamepadTestCard\s*\/>/.test(sortVue2)
    && /import GamepadTestCard/.test(sortVue2));

  // ⑤ 井字棋卡片也常驻显示连接状态
  ok("井字棋卡片也显示「手柄已连接」", /手柄已连接/.test(read("src/components/TicTacToeCard.vue")));
}

console.log("");
console.log("== M. 后端来源（Windows XInput 直读兜底） ==");
{
  const gpSrc2 = read("src/games/gamepad.js");
  const boxSrc2 = read("src/components/GamepadTestCard.vue");
  ok("gamepad.js 导出 enableBackendFallback / gamepadSource",
    typeof enableBackendFallback === "function" && typeof gamepadSource === "function");
  ok("readPad 带 source 字段（node 无手柄 → none）",
    readPad().source === "none" && gamepadSource() === "none");
  ok("前端有后端回退链路（轮询 + 假手柄 + 浏览器优先 + 共用 processPad）",
    /backendUrl/.test(gpSrc2) && /beTimer/.test(gpSrc2) && /backendPad\(/.test(gpSrc2)
    && /stopBackendPoll\(\)/.test(gpSrc2) && /processPad\(/.test(gpSrc2));
  ok("测试盒注入后端地址（/gamepad/state）并常轮询",
    /enableBackendFallback\(apiUrl\("\/gamepad\/state"\)\)/.test(boxSrc2)
    && /setInterval\(poll, 200\)/.test(boxSrc2));
  ok("测试盒显示「来源」（浏览器 / 后端 XInput 直读）",
    /sourceText/.test(boxSrc2) && /来源/.test(boxSrc2) && /后端（XInput 直读）/.test(boxSrc2));

  // ---- 后端侧 ----
  const gpApi = read("../backend/app/api/gamepad.py");
  const gpSvc = read("../backend/app/services/gamepad.py");
  const gpApiTtt = read("../backend/app/api/tictactoe.py");
  const mainPy = read("../backend/app/main.py");
  ok("后端有 /api/gamepad/state（只读免令牌，走 gamepad.drain）",
    /prefix="\/api\/gamepad"/.test(gpApi) && /gamepad\.drain\(\)/.test(gpApi)
    && !/require_control/.test(gpApi));
  ok("后端 XInput → 标准下标（A=0 / Menu=9 / ↑12 ↓13 ←14 →15）",
    /"A", 0\)/.test(gpSvc) && /"START", 9\)/.test(gpSvc) && /"UP", 12\)/.test(gpSvc)
    && /"DOWN", 13\)/.test(gpSvc) && /"LEFT", 14\)/.test(gpSvc)
    && /"RIGHT", 15\)/.test(gpSvc));
  ok("后端短按不漏（tapped 累计、读一次清一次）",
    /tapped/.test(gpSvc) && /_st\["tapped"\] = set\(\)/.test(gpSvc));
  ok("后端暴露 idle_sec / last_input_at（诊断\"连了却不动\"）",
    /idle_sec/.test(gpSvc) && /last_input_at/.test(gpSvc));
  ok("测试盒：\"连了却不动\"明确警告 + joy.cpl 验证指引",
    /gpt-warn/.test(boxSrc2) && /从未/.test(boxSrc2) && /属性 → 测试/.test(boxSrc2));
  ok("main.py 注册了 gamepad 路由", /include_router\(gamepad\.router\)/.test(mainPy));

  // ---- 速度口径：右上角多少，机器人多少（2026-10-10 用户纠正） ----
  const tttStore = read("src/stores/tictactoe.js");
  const tttCard = read("src/components/TicTacToeCard.vue");
  ok("井字棋开局/复位都把**右上角全局速度**传给后端（speed_pct: exec.speed）",
    (tttStore.match(/speed_pct: useExecStore\(\)\.speed/g) || []).length >= 2);
  ok("井字棋卡片显示本局速度（跟顶栏）",
    /speedPct/.test(tttStore) && /跟顶栏/.test(tttCard));
  ok("后端 /tictactoe/start 收 speed_pct（5~100，与分拣同口径）",
    /speed_pct: int = Field\(default=DEFAULT_SPEED_PCT, ge=5, le=100/.test(gpApiTtt));
  ok("后端 _carry 不再写死速度（取 st[\"speed_pct\"]）",
    /int\(st\.get\("speed_pct"\) or DEFAULT_SPEED_PCT\)/.test(gpApiTtt)
    && !/\bSPEED_PCT, dry,/.test(gpApiTtt));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
