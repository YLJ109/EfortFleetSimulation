// =====================================================================
// 「未就绪 → 全部禁止（可录点）」+ 就绪判据修正 回归（headless，源码扫描）—— 2026-10-12
//
// 用户口径：
//   「如果没有就绪，把所有能执行到点、姿势、执行等等所有按钮禁止掉，不能点击的状态」
//   「控制权限的按钮也是！！」「如果没有就绪急停 / 复位急停也用不了」
//   「一键就绪的要求是程序加载、上电等等」「拿不到实时姿态禁止就绪」
//   「没有就绪可以录点，但是不能移动」
//
// 钉死三件事：
//   A. 后端就绪判据**唯一实现** `motion.readiness_reason` / `motion.snapshot_ready`，
//      且**不要求 `prog_loaded`**（现场实测：prog=210 / run=1 / prog_loaded=0 才是正常运行态，
//      "已加载"位 RUN 起来后会落下）；`/api/rc-status` 与采集器都调它（§5.5 铁律④ 同屏同源）。
//   B. 前端所有「会让机器人动」的执行类按钮都绑 `rc.ready`（吸放 8 键 + 急停/复位也在内）。
//   C. 按用户口径**必须保持可用**的例外：录点/删除（只写配置）、停止/暂停（刹车）、
//      方向键选点（纯前端）、回放（只读）。
//
// 运行： node tools/verify_not_ready_gate.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(root, p), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

console.log("== 「未就绪全部禁止 + 就绪判据」回归 ==");

// ---------------- 后端 ----------------
const motionPy = read("../backend/app/services/motion.py");
const robotPy = read("../backend/app/api/robot.py");
const collectorPy = read("../backend/app/services/collector.py");

ok("就绪判据有唯一实现 motion.readiness_reason(snap)",
  /def readiness_reason\(snap\)/.test(motionPy));
ok("就绪判据有布尔包装 motion.snapshot_ready(snap)",
  /def snapshot_ready\(snap\)\s*->\s*bool:/.test(motionPy));
ok("★ 就绪含「常驻服务程序号必须对上」（prog == service_program）",
  /def readiness_reason[\s\S]{0,2200}?prog != svc/.test(motionPy));
ok("★ 就绪含「运行位必须为 1」（run），**不看 prog_loaded**",
  /def readiness_reason[\s\S]{0,2400}?if not bool\(b\.get\("run"\)\)/.test(motionPy));
ok("★ 就绪文案点明「哪一条不满足」而不是笼统写伺服未上电（至少 5 种原因）",
  (motionPy.match(/机器人没有就绪：/g) || []).length >= 5);
ok("★ /api/rc-status 的 ready 来自 snapshot_ready（唯一实现）",
  /snapshot_ready\(/.test(robotPy));
ok("★ 采集器（WS 广播）的 ready 也来自 snapshot_ready（同屏同源）",
  /snapshot_ready\(/.test(collectorPy));
ok("★ 反向钉：不再有旧的 `prog_loaded and run` 式 ready 判据",
  !/b\["prog_loaded"\]/.test(robotPy) && !/b\["prog_loaded"\]/.test(collectorPy));

// ---------------- 前端：执行类按钮一律绑 rc.ready ----------------
const execCard = read("src/components/ExecControlCard.vue");
const sortView = read("src/components/SortingView.vue");
const tttCard = read("src/components/TicTacToeCard.vue");
const aiJs = read("src/stores/ai.js");
const padJs = read("src/stores/pad.js");
const rcCard = read("src/components/RcReadyCard.vue");
const pointView = read("src/components/PointExecView.vue");
const padView = read("src/components/PadExecView.vue");

// ① 控制权限卡：吸放两路 + 急停/复位 都走 canAct(= canControl && rc.ready)
ok("① ExecControlCard：canAct = canControl && rc.ready",
  /const canAct = computed\(\(\) => canControl\.value && rc\.ready\)/.test(execCard));
ok("① ExecControlCard：急停/复位也受就绪门控（canEstop = canAct）",
  /const canEstop = canAct;/.test(execCard) && /:disabled="!canEstop/.test(execCard));
ok("① ExecControlCard：吸/放 8 键全部绑 canAct（不再只看 canControl）",
  (execCard.match(/:disabled="!canAct/g) || []).length >= 8
  && !/:disabled="!canControl/.test(execCard));

// ② 分拣页：开始 / 到点 / 切面 门控；存点 / 删除 保持可点（录点允许）
ok("② SortingView：canStart 含 rc.ready",
  /canStart = computed\([\s\S]{0,200}?rc\.ready/.test(sortView));
ok("② SortingView：canGoto = canTeach && rc.ready（到点/切面）",
  /const canGoto = computed\(\(\) => canTeach\.value && rc\.ready\)/.test(sortView));
ok("② SortingView：到点按钮绑 canGoto（不是 canTeach）",
  /title="移到该点位复核" \@click="goto\(sl\.name\)"/.test(sortView)
  && (sortView.match(/:disabled="!isTaught\(sl\.name\) \|\| !canGoto/g) || []).length >= 2);
ok("② SortingView：★ 存点仍只判 canTeach（未就绪也能录点）",
  /把机器人当前位置存为该点位" \@click="teach\(sl\.name\)"/.test(sortView)
  && (sortView.match(/:disabled="!canTeach \|\| pointBusy === sl\.name"/g) || []).length >= 2);

// ③ 井字棋：开始 / 复原 门控；停止 / 暂停继续 不受门控（刹车必须可点）
ok("③ TicTacToeCard：canRun = canCtrl && rc.ready",
  /const canRun = computed\(\(\) => canCtrl\.value && rc\.ready\)/.test(tttCard));
ok("③ TicTacToeCard：开始 / 复原 绑 canRun",
  /:disabled="!canRun \|\| running"/.test(tttCard)
  && /:disabled="!canRun \|\| locked \|\| !hasMoves"/.test(tttCard));
ok("③ TicTacToeCard：★ 停止 / 暂停继续 仍绑 canCtrl（刹车不受就绪门控）",
  (tttCard.match(/:disabled="!canCtrl" \@click="ttt\.(togglePause|stop)"/g) || []).length >= 2);

// ④ AI 执行：canExec 含 令牌 + 就绪
ok("④ ai.canExec 含 auth.controlActive && rc.ready",
  /canExec\(\)[\s\S]{0,400}?a\.controlActive && rc\.ready/.test(aiJs));

// ⑤ 手柄执行：真机动作闸门加第三道 rc.ready
ok("⑤ pad._gate 第三道 = 完整就绪（useRcReadyStore().ready）",
  /_gate\(\)[\s\S]{0,800}?useRcReadyStore\(\)\.ready/.test(padJs));
ok("⑤ 手柄页如实告知未就绪（引用 NOT_READY_HINT）",
  padView.includes("NOT_READY_HINT"));

// ⑥ 一键就绪：非 AUTO 禁点 + 拿不到实时姿态禁点
ok("⑥ RcReadyCard：非 AUTO/远程（!rc.joggable）→ 一键就绪禁用",
  /readyDisabled[\s\S]{0,900}?if \(!rc\.joggable\) return true;/.test(rcCard));
ok("⑥ RcReadyCard：拿不到实时姿态（canControl 含遥测新鲜）→ 一键就绪禁用",
  /readyDisabled[\s\S]{0,900}?if \(!canControl\.value\) return true;/.test(rcCard));
ok("⑥ RcReadyCard：状态行「程序」按 程序号+运行位 判（不再用 prog_loaded）",
  /rc\.snap\.prog === rc\.snap\.service_program && b\.run/.test(rcCard));

// ⑦ 点位执行：录点保持可点、读姿态只需遥测
ok("⑦ PointExecView：★ 存为点位只要求令牌（未就绪也能录点）",
  /:disabled="!auth\.controlActive" \@click="saveTeachAsPoint"/.test(pointView));
ok("⑦ PointExecView：取当前位姿只要求 canControl（只读、不动机器人）",
  /:disabled="!canControl \|\| exec\.teachBusy" \@click="exec\.syncTeachFromRobot\(\)"/.test(pointView));
ok("⑦ PointExecView：执行到位姿仍走 exec.canExec（含就绪门控）",
  /:disabled="!exec\.canExec \|\| exec\.teachBusy"/.test(pointView));

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
