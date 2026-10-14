// =====================================================================
// ★ C3「当前值 + 目标值 + 偏差 + 短趋势」四元组守卫（方案 §C3）
//
// 钉两样东西：
//   ① **纯逻辑**（src/utils/paramTrend.js）：环形缓冲 + 按时间窗取样本 +
//      样本不足不画 + 未知不入环 + 环形角差。
//      这些是"画出来像不像"的根 —— 一旦漂移，界面上就是一条**编出来的趋势线**。
//   ② **接线**（各卡片源码静态断言）：四元组只允许一个实现（ParamQuad.vue），
//      每一处都必须在"读不到"时给「未知」而不是 0，且**带来源标签**
//      （方案原话：不显示无法标注来源的数）。
//
// 对应 §5.5 的两条铁律：
//   铁律③「未知不得塌成布尔/数值」→ ①里的 sample 拒收非有限数 + ②里的「未知」文案；
//   铁律①「水位必须单调/按外部时钟」→ ②里的 `as-of-now`（组件自己调 Date.now()
//   不是响应式依赖，趋势线会停在渲染那一秒）。
//
// 运行： cd frontend && node tools/verify_param_quad.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  sample, series, latest, extremes, sparkPoints, hasTrend, reset, wrapAngleDev,
  TREND_WINDOW_MS, TREND_CAP,
} from "../src/utils/paramTrend.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");
/**
 * 去掉注释只看**代码**。
 * ★ 为什么必须有：本项目大量注释在记录**历史口径**（"原判据 `b.prog_loaded && b.run` 是错的"），
 *   拿全文去做"旧写法不许出现"的反证就会误报 —— 注释里出现 ≠ 代码里在用。
 */
const stripComments = (s) => s
  .replace(/<!--[\s\S]*?-->/g, "")
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .split("\n")
  .filter((l) => !/^\s*(\/\/|\*)/.test(l))
  .map((l) => l.replace(/\s+\/\/[^"'`]*$/g, ""))
  .join("\n");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

console.log("");
console.log("== A. 环形缓冲：未知不入环（铁律③）==");
{
  reset();
  ok("非有限数被拒收（NaN）", sample("x", NaN) === false);
  ok("非有限数被拒收（undefined）", sample("x", undefined) === false);
  ok("非有限数被拒收（null —— 不许变成 0）", sample("x", null) === false);
  ok("字符串数字可入环（Number 转换）", sample("x", "12.5") === true);
  ok("空键名拒收", sample("", 1) === false);
  ok("只有那一个合法值入了环（**没有 0 顶替**）",
     series("x").length === 1 && series("x")[0].v === 12.5,
     JSON.stringify(series("x")));
  ok("latest() 无数据返回 null（不是 0、不是 {v:0}）",
     latest("nope") === null);
  ok("latest() 有数据返回最后一个", latest("x").v === 12.5);
  reset();
}

console.log("");
console.log("== B. 按**时间窗**取样本（不按“最近 N 个”）==");
{
  reset();
  const now = 1_000_000;
  sample("s", 10, now - 90_000);      // 90s 前：窗口(60s)外
  sample("s", 20, now - 40_000);
  sample("s", 30, now - 5_000);
  const s = series("s", TREND_WINDOW_MS, now);
  ok("窗口外的老样本被排除（WS 卡住时不画“刚刚还正常”）",
     s.length === 2 && s[0].v === 20 && s[1].v === 30, JSON.stringify(s));
  ok("极值只在窗口内取", extremes("s", TREND_WINDOW_MS, now).max === 30);
  ok("窗口收窄 → 只剩最新一个", series("s", 10_000, now).length === 1);
  ok("样本不足 2 个 → hasTrend 为假", hasTrend("s", 10_000, now) === false);
  ok("样本不足 2 个 → **不画线**（返回 null，不是一条假平线）",
     sparkPoints("s", 60, 16, { windowMs: 10_000, now }) === null);
  reset();
}

console.log("");
console.log("== C. 容量与清理 ==");
{
  reset();
  for (let i = 0; i < TREND_CAP + 50; i++) sample("c", i, 10_000 + i * 100);
  ok("环形上限生效（不会无限涨）", series("c", 10_000_000, 9_999_999).length <= TREND_CAP,
     String(series("c", 10_000_000, 9_999_999).length));
  // 掉出窗口 2 倍的老样本应被清掉（否则内存里全是历史）
  reset();
  sample("o", 1, 0);
  sample("o", 2, TREND_WINDOW_MS * 4);
  const ring = series("o", TREND_WINDOW_MS * 10, TREND_WINDOW_MS * 4);
  ok("远超窗口的老样本被顺带清掉", ring.length === 1, JSON.stringify(ring));
  reset();
  ok("reset() 后键被清空", latest("o") === null && series("o").length === 0);
}

console.log("");
console.log("== D. sparkline 坐标 ==");
{
  reset();
  const W = 60, H = 16;
  sample("p", 0, 1000); sample("p", 100, 2000);
  const pts = sparkPoints("p", W, H, { now: 2000 });
  ok("两点画得出线", typeof pts === "string" && pts.split(" ").length === 2, pts);
  const [a, b] = pts.split(" ").map((xy) => xy.split(",").map(Number));
  ok("首点在左、末点在右（时间轴单调）", b[0] > a[0], pts);
  ok("值大者 y 小（SVG 纵轴向下）", b[1] < a[1], pts);
  // 固定量程：完全平的序列在自身极值定标下不该除零
  reset();
  sample("f", 42, 1000); sample("f", 42, 2000);
  const flat = sparkPoints("f", W, H, { now: 2000 });
  ok("完全平的序列不炸（lo==hi 时给余量）",
     typeof flat === "string" && !/NaN/.test(flat), flat);
  reset();
  sample("d", 50, 1000); sample("d", 50, 2000);
  const dom = sparkPoints("d", W, H, { now: 2000, domain: [0, 100] });
  const dy = Number(dom.split(" ")[0].split(",")[1]);
  ok("固定量程按量程定标（50/0~100 → 画在中间）",
     Math.abs(dy - (1 - 0.5) * (H - 2) - 1) < 0.35, dom);
  reset();
}

console.log("");
console.log("== E. 环形角差（J6 整圈同一姿态）==");
{
  ok("差 1° 显示成 1（不是 359）", wrapAngleDev(-359) === 1, String(wrapAngleDev(-359)));
  ok("差 359° 折算成 −1（取最短）", wrapAngleDev(359) === -1, String(wrapAngleDev(359)));
  ok("0 还是 0", wrapAngleDev(0) === 0);
  ok("180 与 −180 是同一姿态（取 −180）", wrapAngleDev(180) === -180);
  ok("非有限数返回 null（未知不塌成 0）",
     wrapAngleDev(NaN) === null && wrapAngleDev(undefined) === null);
  ok("结果恒在 ±180 内",
     [-720, -540.5, -181, 181, 360, 500].every((d) => {
       const r = wrapAngleDev(d);
       return r !== null && r >= -180 && r <= 180;
     }));
}

// =====================================================================
// 下面是**接线**（静态源码）：四元组只许一个实现，每处都得给来源与「未知」
// =====================================================================
const PQ = stripComments(read("src/components/ParamQuad.vue"));
const app = stripComments(read("src/App.vue"));
const rrc = stripComments(read("src/components/RcReadyCard.vue"));
const sp = stripComments(read("src/components/SafetyPanel.vue"));
const sv = stripComments(read("src/components/SortingView.vue"));
const sf = stripComments(read("src/components/SortFlow.vue"));

console.log("");
console.log("== F. 四元组只有**一个实现**（§5.5 铁律④：同一屏同一结论同源）==");
{
  ok("ParamQuad.vue 存在且**不发请求**（趋势只读前端环形缓冲）",
     PQ.includes("paramTrend.js") && !/\bfetch\(|apiControl|axios/.test(PQ));
  ok("ParamQuad.vue 不写后端（方案“不为趋势新增后端存储”）",
     !/method:\s*"POST"|method:\s*"PUT"/.test(PQ));
  const users = [["RcReadyCard.vue", rrc], ["SafetyPanel.vue", sp], ["SortingView.vue", sv]];
  for (const [f, src] of users) {
    ok(`${f} 复用 ParamQuad（不自己再写一份四元组）`,
       /import ParamQuad from "\.\/ParamQuad\.vue"/.test(src) && /<ParamQuad/.test(src), f);
  }
  ok("★ 无来源标签的值**不许上屏**（方案原话：不显示无法标注来源的数）",
     /props\.curSource \? fmt\(props\.current\) : null/.test(PQ)
     && /props\.tgtSource \? fmt\(props\.target\) : null/.test(PQ));
  ok("★ 未知显示「未知」，**不是 0**（铁律③）",
     /\?\? "未知"/.test(PQ));
  ok("★ 样本不足 2 个不画线，并如实写「暂无趋势」",
     /points/.test(PQ) && /暂无趋势/.test(PQ));
  ok("★ 过期必须显式标记（不靠颜色）", /数据已过期/.test(PQ));
  ok("★ 偏差超容差才转 danger（给了容差才判色）",
     /Number\.isFinite\(props\.warnDev\)/.test(PQ));
}

console.log("");
console.log("== G. 趋势采样挂在 App 的 1 秒时钟上（不许绑渲染循环 / 不许自起定时器）==");
{
  ok("App.vue 引入 sample 并集中采样",
     /from "\.\/utils\/paramTrend\.js"/.test(app) && /function sampleTrends\(\)/.test(app));
  ok("采样由 checkTtl（1s 时钟）调用，不在 loop（渲染循环）里",
     /try \{ sampleTrends\(\); \}/.test(app.split("function loop()")[0]));
  ok("App.vue 没有为趋势自起 setInterval",
     !/setInterval\(\s*\(\)\s*=>\s*sampleTrends/.test(app));
  ok("★ 只在“这一路还新鲜”时入环（冻结的数据不进窗口 = 假平线）",
     /if \(!rc\.stale/.test(app) && /!robot\.telemetryStale/.test(app) && /safe\.health && safe\.health\.ok/.test(app));
}

console.log("");
console.log("== H. 速度四元组（AGENTS.md「设定 ≠ 实际」那条最疼的）==");
{
  ok("40003 实读与滑块设定**同屏**（一个 ParamQuad 里并列）",
     /rc\.snap\.speed_pct/.test(rrc) && /exec\.speed/.test(rrc) && /<ParamQuad/.test(rrc));
  ok("★ 来源如实标注：实读=40003 / 设定=40103 且明写“无回读”（不假装读到设定值）",
     /40003 实读/.test(rrc) && /40103/.test(rrc) && /无回读/.test(rrc));
  ok("偏差容差走唯一出处 SPEED_DEV_TOL（不是各写一个 5）",
     /import \{ SPEED_DEV_TOL \} from "\.\.\/core\/safetyConst\.js"/.test(rrc)
     && /:warn-dev="SPEED_DEV_TOL"/.test(rrc));
  const SC = read("src/core/safetyConst.js");
  ok("SPEED_DEV_TOL 在 core/safetyConst.js 里唯一定义",
     /export const SPEED_DEV_TOL = (\d+);/.test(SC));
  ok("★ 排查口径上屏（210 内 vNNperc + 示教器全局倍率）",
     /v100perc|vNNperc/.test(rrc) && /示教器全局速度倍率|全局速度倍率/.test(rrc));
  ok("趋势按外部时钟取窗（as-of-now）", /:as-of-now="rc\.clk"/.test(rrc));
}

console.log("");
console.log("== I. 就绪四要素：当前 + 期望（红的时候要说清应该是什么）==");
{
  ok("每行带 want（期望值）", /want:/.test(rrc) && /应为 \{\{ row\.want \}\}/.test(rrc));
  ok("★ 期望值只在**不满足**时显示（满足时复述是噪音）",
     /v-if="!row\.ok"[^>]*class="want"/.test(rrc));
  ok("伺服/档位/报警/程序四条各有期望",
     /want: "上电"/.test(rrc) && /want: "AUTO \/ 远程"/.test(rrc)
     && /want: "无/.test(rrc) && /且 RUN=1"/.test(rrc));
  // ★ 判据与后端同源：程序那一行**不再**要求 prog_loaded（正常运行态 RUN 起来后会落下）
  ok("★「程序」行判据 = prog==服务程序 且 run=1（**不再要求 prog_loaded**，与后端 ready 同源）",
     /rc\.snap\.prog === rc\.snap\.service_program && b\.run/.test(rrc)
     && !/b\.prog_loaded && b\.run/.test(rrc));
}

console.log("");
console.log("== J. 围栏净空：当前 + 阈值线 + 30s 趋势 + 结论来源 ==");
{
  ok("净空四元组上屏（当前/告警线/趋势/来源）",
     /<ParamQuad class="pq-clear"/.test(sp) && /trend-key="clearance"/.test(sp));
  ok("★ 评估不新鲜时**不给数**（冻结的上一帧不是实测余量）",
     /if \(!safety\.health \|\| !safety\.health\.ok\) return null;/.test(sp));
  ok("趋势窗 30s（方案给围栏定的口径，不用默认 60s）",
     /CLEARANCE_TREND_MS = 30000/.test(sp) && /:trend-window-ms="CLEARANCE_TREND_MS"/.test(sp));
  ok("★ 来源区分实测/推断（残影预演不得冒充实测）",
     /残影预演（流程推断）/.test(sp) && /实体机实测/.test(sp));
  ok("阈值线取**同一个区**（否则两边不同源）",
     /clearanceZone/.test(sp) && /basisOf\(z\)/.test(sp));
  ok("服务端独立评估另说一句，不与本路混写",
     /服务端另有独立评估/.test(sp));
}

console.log("");
console.log("== K. J6：角度 + 面 + 与目标偏差（环形）==");
{
  ok("J6 四元组上屏", /<ParamQuad class="pq-j6"/.test(sv) && /trend-key="j6"/.test(sv));
  ok("★ 偏差走环形差（wrap）—— 直接相减会把 1° 显示成 359°",
     /\bwrap\b/.test(sv.match(/<ParamQuad class="pq-j6"[\s\S]*?\/>/)[0]));
  ok("当前=WS 遥测实读 · 目标=后端最后下发目标（不是前端猜）",
     /cur-source="WS 遥测实读"/.test(sv) && /tgt-source="后端最后下发目标"/.test(sv));
  ok("★ 读不到实时姿态 → 当前如实 null（未知），不拿 0 顶",
     /if \(!robot\.connected \|\| robot\.telemetryStale\) return null;/.test(sv));
  ok("★ 面只取后端 state.face，**不在前端按角度算面**",
     /faceNow[\s\S]{0,200}st\.value\.face/.test(sv) && !/faceOfJ6|face_of_j6/.test(sv));
  ok("面标注为流程推断（现场无面传感器）", /流程推断·现场无面传感器/.test(sv));
  ok("取不到面 → 「—（未知）」", /—<span class="muted">（未知）/.test(sv));
}

console.log("");
console.log("== L. 分拣步耗时：界面观测，不冒充控制器实测、不给 ETA ==");
{
  ok("步骤条显示观测耗时", /durText\(/.test(sf) && /观测 /.test(sf));
  ok("★ 如实标注“界面观测（按轮询粒度、非控制器实测）”",
     /界面观测/.test(sf) && /非控制器实测/.test(sf));
  ok("★ 不输出剩余时间预估（ETA）",
     !/预计剩余|剩余时间约/.test(sf));
  ok("没观测到 → 空串（不显示 0ms，那会被读成“瞬间完成”）",
     /return "";\s*\n\s*if \(seg\.value\.key === key/.test(sf));
  ok("换一件重新计时（不把上一件算进来）",
     /if \(pc !== pieceSeen\.value\)/.test(sf) && /durs\.value = \{\}/.test(sf));
  ok("未运行不计时（停了就不给“进行中”的数）",
     /if \(!running\.value\) return ""/.test(sf));
  ok("★ 没为耗时新增后端字段（与“不为趋势新增后端存储”同纪律）",
     !/fetch\(|apiControl/.test(sf));
}

console.log("");
console.log("== M. 反证（判据不是恒真的）==");
{
  reset();
  ok("反证：只喂一个样本 → 画不出趋势",
     sample("z", 5, 1000) === true && sparkPoints("z", 60, 16, { now: 1000 }) === null);
  // ★★ 这条是**真踩过的坑**：`Number(null) === 0` —— 一旦用 `Number.isFinite(Number(v))`
  //   判空，"读不到"就会被采成一个真实的 0 样本（趋势线上凭空掉出一个 0%）。
  reset();
  sample("n", null); sample("n", undefined); sample("n", ""); sample("n", false);
  ok("反证：null/undefined/空串/布尔 **不会**被 Number() 折成 0 入环",
     series("n").length === 0, JSON.stringify(series("n")));
  ok("反证：环形角差对 null 给 null（不是 −180）",
     wrapAngleDev(null) === null && wrapAngleDev(undefined) === null);
  const fakeCard = '<span>{{ (clearance || 0).toFixed(0) }} mm</span>';
  ok("反证：`|| 0` 那种“未知塌成 0”的写法会被围栏那一条判据抓到",
     /\|\| 0\)/.test(fakeCard) && !/\|\| 0\)/.test(
       (sp.match(/:current="clearanceMm"/) || [""])[0]));
  ok("反证：没有来源标签的假四元组过不了 F 段的正则",
     !/props\.curSource \? fmt\(props\.current\) : null/.test("<div>{{ v }}</div>"));
  reset();
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
