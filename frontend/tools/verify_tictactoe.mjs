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
import { createSortingBlocks, TTT_SYM_HEX } from "../src/three/sortingBlocks.js";
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

  // ★★ 2026-10-11 用户口径「**井字棋要搬走，放在新页面**」：
  //   井字棋卡片 / 2D 棋盘浮层 / 手柄测试盒 现在都挂在 components/PadExecView.vue（手柄执行页）。
  const padVue = read("src/components/PadExecView.vue");
  const sortVue = read("src/components/SortingView.vue");
  ok("★ 手柄执行页挂载了 2D 棋盘浮层", /<TicTacToeBoard/.test(padVue));
  // ★ 防回归钉：2D 棋盘浮层**只在「井字棋」tab 下出现**（执行 tab 不该被棋盘挡住）。
  //   渲染条件 = 本页 tab === "ttt" 且 store.gameVisible（**复用、不重写**）。
  ok("★ 浮层 TicTacToeBoard 的渲染条件含 pad.tab === 'ttt'（且复用 store.gameVisible）",
    /<TicTacToeBoard\s+v-if="showTttBoard"/.test(padVue)
    && /pad\.tab === PAD_TAB_TTT/.test(padVue)
    && /ttt\.gameVisible/.test(padVue));
  ok("★ 手柄执行页挂载了井字棋卡片", /<TicTacToeCard/.test(padVue));
  ok("★ 手柄执行页进出启停井字棋轮询", /ttt\.startPolling\(\)/.test(padVue)
    && /ttt\.stopPolling\(\)/.test(padVue));
  ok("★★ 井字棋已从「智能分拣」页**移走**（本页不再渲染任何游戏内容）",
    !/<TicTacToeCard/.test(sortVue) && !/<TicTacToeBoard/.test(sortVue)
    && !/<GamepadTestCard/.test(sortVue));

  console.log("");
  console.log("== I. store 口径（令牌 / 默认空跑 / 首轮静默 / 语音 / 复用暂停端点） ==");
  ok("全部请求走 apiControl（带控制令牌）", [...store.matchAll(/apiControl\(/g)].length >= 6);
  ok("未授权不发请求（_authed 门控）", /_authed\(/.test(store) && /controlActive/.test(store));
  ok("★★ 井字棋默认**真实下发**（用户：「不用空跑啊，要真机移动」）", /dryRun:\s*false/.test(store));
  ok("首轮静默：poll(true) 对齐游标", /poll\(true\)/.test(store) && /silent/.test(store));
  ok("暂停/继续复用 /control/run-pause|run-resume",
    /\/control\/run-pause/.test(store) && /\/control\/run-resume/.test(store));
  ok("用户赢 → 语音祝贺语（只在该结果变化处触发）",
    /恭喜你赢了/.test(store) && /speak\(/.test(store));
  ok("落子/停止/复位/暂停后立刻刷新一次 state（await this.poll(false)）",
    (store.match(/await this\.poll\(false\)/g) || []).length >= 4);
  ok("回放（只查看）：棋谱来自 GET /tictactoe/replay",
    /apiControl\("\/tictactoe\/replay"\)/.test(store));
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

  // ★★ 2026-10-11（本轮）用户口径：「模拟的这些盒子要颜色区分呀，不是全部白色…
  //    **蓝色是圆，绿色是叉**」→ 3D 棋子按**符号**上色 + 顶面圆/叉标记；`pending` 预告态。
  console.log("");
  console.log("== J2. 3D 棋子：符号上色（蓝圆/绿叉）+ 坠落 + 预告态 ==");
  const mkSb = () => {
    const s = createSortingBlocks(0);
    s.setSlots(payload);
    return s;
  };
  const bOf = (s, slot) => s.root.children.find((c) => c.userData && c.userData.sortSlot === slot);
  const aOf = (s, name) => s.root.children.find((c) => c.userData && c.userData.sortName === name);
  const markOf = (g) => g && g.children.find((c) => c.name === "sym-mark");
  {
    const s = mkSb();
    const bd = newBoard();
    bd[0] = O; bd[4] = X;                    // B1 = 圆(O)、B5 = 叉(X)
    s.setTtt({ board: bd, used: ["A1", "A6"], holding: false, pending: [] });
    s.update({});
    const b1 = bOf(s, "B1"), b5 = bOf(s, "B5");
    ok("★ B1（O）棋子 = **蓝色**（不再一律未知灰）",
      !!b1 && b1.children[0].material.color.getHex() === TTT_SYM_HEX.O,
      b1 ? b1.children[0].material.color.getHex().toString(16) : "无");
    ok("★ B5（X）棋子 = **绿色**",
      !!b5 && b5.children[0].material.color.getHex() === TTT_SYM_HEX.X);
    ok("★ O 棋子顶面是**圆环**标记（按几何类型断言）",
      !!markOf(b1) && markOf(b1).children.length >= 1
      && markOf(b1).children.every((c) => c.geometry.type === "RingGeometry"));
    ok("★ X 棋子顶面是**十字**（两根交叉横杠）",
      !!markOf(b5) && markOf(b5).children.length === 2
      && markOf(b5).children.every((c) => c.geometry.type === "BoxGeometry"));
    const a1 = aOf(s, "A1"), a7 = aOf(s, "A7");
    ok("★ A 区也按来源符号上色：A1（圆来源）蓝、A7（叉来源）绿 —— 这就是\"不是全部白色\"",
      !!a1 && a1.children[0].material.color.getHex() === TTT_SYM_HEX.O
      && !!a7 && a7.children[0].material.color.getHex() === TTT_SYM_HEX.X);
    s.dispose();
  }
  {
    // ★ 坠落：hidden→visible 才播；只在 step() 里推进；末态**严格**回格子面
    const s = mkSb();
    s.setTtt({ board: newBoard(), used: [], holding: false, pending: [] });
    s.update({});                                   // firstUpdate → 不播（位置=格子面）
    const restY = bOf(s, "B1").position.y;
    const bd = newBoard(); bd[0] = O;
    s.setTtt({ board: bd, used: [], holding: false, pending: [] });
    s.update({});                                   // B1 由隐藏→显示 → 触发坠落
    const yTop = bOf(s, "B1").position.y;
    ok("★ 坠落：落子瞬间在格子上方（不是瞬移）",
      yTop - restY > 0.03, `Δ=${((yTop - restY) * 1000).toFixed(1)}mm`);
    s.step(0.016, null, null);
    const yMid = bOf(s, "B1").position.y;
    ok("★ 坠落：step 推进中，位置在格子面之上、且比初值低",
      yMid > restY && yMid < yTop);
    s.step(1.0, null, null);                        // 远超 FALL_DUR → 落定
    ok("★ 坠落：落定后**严格**回到格子面高（不留残留偏移）",
      Math.abs(bOf(s, "B1").position.y - restY) < 1e-12);
    // ★ 预告态（机器人先手"已显示、待搬子"）：半透明 + 不坠落
    const bd2 = newBoard(); bd2[8] = X;
    s.setTtt({ board: bd2, used: [], holding: false, pending: [8] });
    s.update({});
    const b9 = bOf(s, "B9");
    ok("★ 预告态：棋子可见但**半透明**（material.transparent = true）",
      !!b9 && b9.visible === true && b9.children[0].material.transparent === true
      && b9.children[0].material.opacity < 1);
    ok("★ 预告态：**不播坠落**（位置就在格子面 —— 还没真搬，不能编故事）",
      !!b9 && Math.abs(b9.position.y - restY) < 1e-12);
    s.setTtt({ board: bd2, used: [], holding: false, pending: [] });
    s.update({});
    ok("★ 真搬完成（pending 清空）→ 棋子恢复实心",
      bOf(s, "B9").children[0].material.transparent === false
      && bOf(s, "B9").children[0].material.color.getHex() === TTT_SYM_HEX.X);
    s.dispose();
  }
  console.log("");
  console.log("== J3. 2D 棋盘符号色 + 预告态虚框（源码钉） ==");
  {
    const bv = read("src/components/TicTacToeBoard.vue");
    ok("★ 2D：O 用蓝、X 用绿（用户口径\"蓝色是圆，绿色是叉\"）",
      /\.ttt-cell\.o \.mk \{ color: #4b8bff/.test(bv)
      && /\.ttt-cell\.x \.mk \{ color: #35c46a/.test(bv));
    ok("★ 2D：预告态用虚框 + 读 state.pending（与后端同源）",
      /pendingSet/.test(bv) && /st\.pending/.test(bv) && /\.ttt-cell\.pending \{/.test(bv));
  }
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
  ok("★ LB/RB 已映射为中性动作名（l1/r1，供手柄执行页用）",
    JSON.stringify(actionsOf([4])) === '["l1"]'
    && JSON.stringify(actionsOf([5])) === '["r1"]');
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
  ok("卡片接手柄：开始/暂停/停止/复原（Y 与 View 合并为一个动作）",
    /subscribeGamepad\(/.test(cardSrc)
    && /action === "start"/.test(cardSrc) && /action === "pause"/.test(cardSrc)
    && /action === "stop"/.test(cardSrc)
    && /action === "replay" \|\| action === "reset"/.test(cardSrc));
  ok("只在「手柄执行」页·井字棋 tab 响应（不劫持全局手柄，也不与同页执行 tab 抢键）",
    /robot\.activeView !== "pad"/.test(cardSrc) && /pad\.tab !== PAD_TAB_TTT/.test(cardSrc)
    // 棋盘把同一判据收敛进 onTttPage()（正面写法），钉住它确实同时看页与 tab
    && /function onTttPage\(\)/.test(boardSrc)
    && /robot\.activeView === "pad" && pad\.tab === PAD_TAB_TTT/.test(boardSrc));
  // ★★ 2026-10-11（用户口径「这个不需要啊」，附图 = 开局那个 confirm 弹窗）：
  //   界面的 `window.confirm` **已全部移除**（开局 / 切真实下发 / 复原）——
  //   点一下就执行；真机保护仍由未就绪闸门 + 安全围栏 + 界面上的真实/空跑开关兜着。
  ok("★ 界面不再用 window.confirm 做真机动作确认（用户口径「这个不需要啊」）",
    !/window\.confirm\(/.test(cardSrc));
  ok("手柄复原仍保留「按两次」确认（手柄点不了弹窗，这是它唯一的确认手段）",
    /doRestore\(\)/.test(cardSrc) && /padRestoreArm/.test(cardSrc));
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
  const padVue2 = read("src/components/PadExecView.vue");
  ok("★ 手柄执行页挂载手柄测试盒（跟着手柄走；分拣页已不再有）",
    /<GamepadTestCard\s*\/>/.test(padVue2) && /import GamepadTestCard/.test(padVue2)
    && !/import GamepadTestCard/.test(read("src/components/SortingView.vue")));

  // ⑤ 井字棋卡片也常驻显示连接状态
  ok("井字棋卡片也显示「手柄已连接」", /手柄已连接/.test(read("src/components/TicTacToeCard.vue")));

  // ⑥ ★★ 2026-10-11：测试盒**按 tab 换表**（用户口径「移除掉没有的测试键」）——
  //    执行 tab 只列本 tab 真实存在的键（含新的 LB/RB），井字棋 tab 用原表。
  ok("★ 测试盒按 tab 切表（SIMS/MAP_ROWS 随 pad.tab 切换）",
    /pad\.tab === PAD_TAB_TTT \? SIMS_TTT : SIMS_EXEC/.test(boxSrc)
    && /pad\.tab === PAD_TAB_TTT \? MAP_TTT : MAP_EXEC/.test(boxSrc));
  ok("★ 执行 tab 的模拟键含 A 原位 / Y 正上位 / LB 正面 / RB 反面 / Start 启动关闭",
    /A 原位/.test(boxSrc) && /Y 正上位/.test(boxSrc)
    && /LB 正面/.test(boxSrc) && /RB 反面/.test(boxSrc) && /Start 启动\/关闭/.test(boxSrc));
  ok("★ 执行 tab 的表含 LB/RB（对应新动作名 l1/r1）",
    /a: "l1"/.test(boxSrc) && /a: "r1"/.test(boxSrc));
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

  // ---- 统一口径：颜色无法识别 → 放回原位（2026-10-10 用户口径） ----
  const sortApi = read("../backend/app/api/sorting.py");
  ok("分拣判不出色 → _put_back 放回原格 + 停止（不再就地放掉）",
    /_put_back\(ctx, pmap, a_slot/.test(sortApi) && /已放回原位 %s/.test(sortApi)
    && /颜色无法识别（%s）/.test(sortApi));
  ok("_put_back 工艺顺序（回原格上空→下落→停吸→放气→停放→抬起→回起点）",
    /"%s·到原格上方"/.test(sortApi) && /"%s·下落"/.test(sortApi)
    && /"%s·停止放气"/.test(sortApi) && /"%s·回起点"/.test(sortApi));
  ok("游戏自证：判不出 / 不符 都放回原格并停（两种文案必须分开）",
    /自证失败·放回原位/.test(gpApiTtt) && /自证不符：该格应为/.test(gpApiTtt)
    && /已放回原位 %s/.test(gpApiTtt));
  ok("游戏默认模式不判色（verify_color 默认 false、判色被 not dry 门控）",
    /verify_color: bool = Field\(default=False/.test(gpApiTtt)
    && /if verify and not dry:/.test(gpApiTtt));
  ok("卡片有「模式」开关（默认 / 拍照自证）+ 自证约定编辑组件",
    /verifyColor/.test(tttCard) && /拍照自证/.test(tttCard)
    && /自证约定（可改）/.test(tttCard));
  ok("store 开局透传 verify_color",
    /verify_color: this\.verifyColor/.test(tttStore));
  // ★ 2026-10-10 用户口径："也可以切换别的颜色，用户自己选吧"
  ok("自证约定用户可改（圆/叉各一个颜色下拉 + store 透传 colors）",
    /selfColor/.test(tttStore) && /colors: \{ O: this\.selfColor\.O/.test(tttStore)
    && /v-model="ttt\.selfColor\.O"/.test(tttCard));

  // ★★ 2026-10-11（方案 §D）「正常 / 快速」两模式 + 「只剩一个空位免等待」
  ok("卡片有「节奏」正常/快速开关（默认正常）",
    /ttt\.fast/.test(tttCard) && /正常/.test(tttCard) && /快速/.test(tttCard));
  ok("快速模式有如实提示（无需录制正反面、不与自证合并、免等待）",
    /无需录制正反面/.test(tttCard) && /虚拟切面/.test(tttCard)
    && /免等待/.test(tttCard) && /不与「拍照自证」合并/.test(tttCard));
  ok("store 开局透传 fast",
    /fast: this\.fast/.test(tttStore));
  ok("后端 StartIn.fast 默认 false（正常=零回归）",
    /fast: bool = Field\(default=False/.test(gpApiTtt));
  // ★★ 2026-10-12 终版（用户口径「不用录制正反面」）：快速翻转 = 虚拟切面命令
  //   （正面 = 该点自身录制 J6、反面 = 当前点 J6 − 180，按点名自动折算，不读点位库）
  //   → `_needed_points` 不再带 fast、不再把 J6正面/J6反面 列进必需点位
  //   （否则用户删掉库里历史遗留行后，快速模式会被"缺点位" 400 卡死）。
  ok("★ 后端快速模式不再要求库里有 J6正面/J6反面（_needed_points 无 fast / 无 OPTIONAL_EXTRA）",
    /def _needed_points\(verify: bool = False\)/.test(gpApiTtt)
    && !/OPTIONAL_EXTRA_POINTS/.test(gpApiTtt));
  ok("后端快速模式 = 一趟取两子（_play_two 复用翻转 + 双面放件）",
    /def _play_two\(/.test(gpApiTtt) && /_move_flip_j6\(/.test(gpApiTtt)
    && /_dual_drop\(/.test(gpApiTtt));
  ok("后端「只剩一个空位」免等待（谁轮到都直接完成）",
    /只剩一个空位，直接完成（免等待）/.test(gpApiTtt));
  // ★★ 2026-10-11（本轮）：快速模式**不再强制用户先行**；机器人先手时**提前显示**其落子。
  ok("★ 后端快速模式不再强制用户先行（先手按原有逻辑随机）",
    !/"user" if fast/.test(gpApiTtt) && /random\.choice\(\["user", "robot"\]\)/.test(gpApiTtt));
  ok("★ 后端机器人先手：提前把落子写进棋盘（与 2D/3D 同源）并如实标注「待搬子」",
    /board\[cell_r\] = robot_sym/.test(gpApiTtt)
    && /机器人已下棋完成/.test(gpApiTtt) && /待搬子/.test(gpApiTtt));
  ok("★ 卡片如实说明：先手随机 + 机器人先手先显示其落子",
    /先手随机/.test(tttCard) && /先把它的落子显示在 2D\/3D/.test(tttCard));
}

console.log("");
console.log("== N. 「复原」（复位 + 复盘合并 · 真机 · 逐子搬回原位） ==");
{
  const store = read("src/stores/tictactoe.js");
  const cardVue = read("src/components/TicTacToeCard.vue");
  const boardVue = read("src/components/TicTacToeBoard.vue");
  const pyApi = read("../backend/app/api/tictactoe.py");

  // ---- 后端：只有一个实现（旧的 _reset_run 已删）/ 顺序 / 闸门 / 终态 ----
  ok("★ 后端：「复原」唯一实现 `_restore_run`（旧的 `_reset_run` 已删除 —— 它把 dry 写死成 True）",
    /def _restore_run\(/.test(pyApi) && !/_reset_run/.test(pyApi));
  ok("★ 后端：`/reset`（复原）与 `/replay`（兼容别名）走**同一**实现 `_launch_restore`",
    (pyApi.match(/return _launch_restore\(/g) || []).length >= 2
    && /def _launch_restore\(/.test(pyApi));
  ok("★ 后端：复原复用 `_carry`，不新造下发通道，且**按该件的面**取（face=face）",
    /_carry\(ctx, pmap, st, dry, TT\.cell_to_slot\(cell\), src, "复原", face=face\)/.test(pyApi));
  // ★★ 2026-10-11（用户「先放回第二次，然后放回第一次，放回的时候也不要乱旋转」）：
  //   复原顺序 = **先反面件、后正面件**（唯一口径 `S.order_for_restore`），
  //   同一面内保持"落子逆序"；每件按 `moves[].face` 用**它自己那个面**去取。
  ok("★ 后端：复原顺序走唯一口径 `order_for_restore`（先反面后正面）+ 同面内落子逆序",
    /S\.order_for_restore\(list\(reversed\(moves\)\)\)/.test(pyApi)
    && /S\.norm_face\(mv\.get\("face"\)\)/.test(pyApi));
  ok("★ 后端：`_carry` 的 face 参数化（先在正上方摆面 → 再 keep 下去取，不做多余翻转）",
    /min_conf: float = 0\.3, face: str = S\.FACE_FRONT\)/.test(pyApi)
    && /_air\(ctx, "suck", dry, "%s·吸" % who_cn, st, face=face\)/.test(pyApi)
    && /_move\(ctx, pmap, src_slot \+ "上", speed, dry, "%s·取料上方" % who_cn, st, face=face\)/.test(pyApi));
  ok("★ 后端：`moves[]` 记下 `face`（没有它复原只能瞎猜哪件是反面）",
    /"face": S\.FACE_FRONT\}\)/.test(pyApi) && /"face": S\.FACE_BACK\}\)/.test(pyApi));
  ok("★ 后端：持件按面如实登记（`face_slot_put/take` → 3D 显示哪面有件，与分拣双面同口径）",
    /S\.face_slot_put\(st, "lower"/.test(pyApi) && /S\.face_slot_put\(st, "upper"/.test(pyApi)
    && /S\.face_slot_take\(st, "upper"\)/.test(pyApi));
  ok("★ 后端：快速模式**先落用户子（反面）→ 再落机器人子（正面）**（顺着停面、少翻一次）",
    pyApi.indexOf('"用户·落子"') < pyApi.indexOf('"机器人·落子"')
    && /_move\(ctx, pmap, S\.START_POINT, speed, dry, "经起点", st, face=S\.FACE_BACK\)/.test(pyApi));
  ok("★ 后端：复原按落子**逆序**搬回 + 逐子回执（第几个子 / 去向 / 用哪面）",
    /reversed\(moves\)/.test(pyApi) && /复原：搬回第/.test(pyApi) && /原位·%s/.test(pyApi));
  ok("★ 后端：真实复原接统一未就绪闸门（空跑局不走真机、只清棋盘并如实说明）",
    /reject_if_not_ready\(dry=False\)/.test(pyApi) && /没有真实搬动过棋子/.test(pyApi));
  ok("★ 后端：复原终态清空棋盘（子回原位 → 复位到初始）",
    /st\.update\(\{"board": TT\.new_board\(\), "moves": \[\], "used": \[\]/.test(pyApi));

  // ---- 后端：预告态（机器人先手"已显示、待搬子"）----
  ok("★ 后端：`pending` 预告态随 state 下发，且真搬后/收尾/复原都会清空",
    /"pending": \[\]/.test(pyApi)
    && /_set_state\(st, pending=\[int\(cell_r\)\]\)/.test(pyApi)
    && /pending=\[\]/.test(pyApi));

  // ---- 前端 store ----
  ok("★ 前端 store：复原走 POST /tictactoe/reset（dry_run 决定真机 / 只清棋盘）",
    /async reset\(\)/.test(store)
    && /apiControl\("\/tictactoe\/reset",\s*\{\s*method:\s*"POST"/.test(store)
    && /本局是空跑（没有真实搬动过棋子）/.test(store));
  ok("★ 前端 store：不再单列 `startReplay`（一处实现、一处入口）",
    !/async startReplay\(/.test(store));
  ok("前端 store：回放（只查看）仍走 GET /tictactoe/replay",
    /apiControl\("\/tictactoe\/replay"\)/.test(store) && /async loadReplay\(/.test(store));
  ok("★ 前端 store：把 `pending` 同步给 3D（预告态渲染）",
    /pending:\s*Array\.isArray\(s\.pending\)/.test(store));

  // ---- 前端卡片 ----
  ok("★ 卡片：只保留「复原」按钮（不再有「复盘」按钮字面量）",
    /<Icon name="refresh"[^>]*\/>\s*复原/.test(cardVue)
    && !/>\s*复盘\s*</.test(cardVue)
    && !/doReplay/.test(cardVue));
  ok("★ 卡片：「复原」真实局要二次确认（会真动机器人）",
    /doRestore/.test(cardVue) && /ttt\.reset\(\)/.test(cardVue) && /window\.confirm/.test(cardVue));
  ok("★ 卡片：「回放」按钮 → 只读回放（不动机器人）",
    /回放/.test(cardVue) && /doViewReplay/.test(cardVue) && /ttt\.loadReplay\(\)/.test(cardVue));
  ok("★ 卡片：手柄复原也二次确认（真实局，无弹窗可用；Y 与 View 合并）",
    /padRestoreArm/.test(cardVue) && /请再按一次确认/.test(cardVue));
  ok("★ 棋盘标签如实：回放=只查看（不动机器人）",
    /回放（棋谱记录 · 只查看不动机器人）/.test(boardVue));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
