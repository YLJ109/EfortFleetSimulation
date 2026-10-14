// 回归测试：安全围栏引擎（矩形 / 四点 / 圆形 · 多区域 · 阈值迁移）。
// 运行： cd frontend && node tools/verify_safety.mjs
//
// 只测纯几何与分级算法（three/safety.js 导出的纯函数），不依赖 DOM / WebGL。
import * as THREE from "three";
import { readFileSync } from "node:fs";
import {
  zonePolygon, polygonHalfspaces, clearanceToPolygon, clearanceToCircle,
  basisOf, classify, evaluateZone, classifyGround, STATE_RANK,
} from "../src/three/safety.js";
import {
  freshState, evalHealth, HEALTH_LABEL, FRESH_MAX_MS, FRESH_STOP_MS,
} from "../src/utils/safetyLabels.js";

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log(`  PASS  ${name}${extra ? "  " + extra : ""}`); }
  else { fail++; console.log(`  FAIL  ${name}${extra ? "  " + extra : ""}`); }
}
function near(a, b, eps = 1e-6) { return Math.abs(a - b) <= eps; }

/** 半宽 h 的立方体 AABB（绕原点对称，高 0.8m）。 */
function box(h) {
  return new THREE.Box3(new THREE.Vector3(-h, 0, -h), new THREE.Vector3(h, 0.8, h));
}

const RECT = {
  id: "z1", name: "主工作区", shape: "rect", enabled: true,
  center: { x: 0, z: 0 }, half: { x: 0.82, z: 0.82 }, radius: 0.9,
  corners: [[-0.82, -0.82], [0.82, -0.82], [0.82, 0.82], [-0.82, 0.82]],
  height: 1.2, walls: true,
  thresholds: { basis: "halfwidth", warn: 0.30, danger: 0.10, fixed_mm: 300 },
};

console.log("== 1. 矩形区域：间隙与四档分级 ==");
{
  const c = (h) => clearanceToPolygon(box(h), polygonHalfspaces(zonePolygon(RECT)));
  ok("居中 0.35m 外廓 → 间隙 0.47m", near(c(0.35), 0.47, 1e-9), `实际 ${c(0.35).toFixed(4)}`);
  ok("基准 = 围栏半宽 0.82m", near(basisOf(RECT), 0.82, 1e-9), `实际 ${basisOf(RECT)}`);

  const s1 = evaluateZone(RECT, box(0.35));
  ok("余量 57% → safe", s1.state === "safe", `ratio=${s1.ratio.toFixed(3)}`);

  const s2 = evaluateZone(RECT, box(0.60));   // 间隙 0.22 → ratio 0.268
  ok("余量 26.8% → warn", s2.state === "warn", `ratio=${s2.ratio.toFixed(3)}`);

  const s3 = evaluateZone(RECT, box(0.80));   // 间隙 0.02 → ratio 0.024
  ok("余量 2.4% → danger", s3.state === "danger", `ratio=${s3.ratio.toFixed(3)}`);

  const s4 = evaluateZone(RECT, box(0.90));   // 间隙 -0.08
  ok("越界 → hit", s4.state === "hit", `clearance=${s4.clearance.toFixed(3)}`);

  // 阈值边界两侧各取一点（不取等号，避开浮点误差）
  const justAboveWarn = evaluateZone(RECT, box(0.55));   // 间隙 0.27 → 32.9%
  ok("余量 32.9%（>30%）→ safe", justAboveWarn.state === "safe", `ratio=${justAboveWarn.ratio.toFixed(3)}`);
  const justBelowWarn = evaluateZone(RECT, box(0.60));   // 间隙 0.22 → 26.8%
  ok("余量 26.8%（<30%）→ warn", justBelowWarn.state === "warn", `ratio=${justBelowWarn.ratio.toFixed(3)}`);
  const justAboveDanger = evaluateZone(RECT, box(0.73)); // 间隙 0.09 → 11.0%
  ok("余量 11.0%（>10%）→ warn", justAboveDanger.state === "warn", `ratio=${justAboveDanger.ratio.toFixed(3)}`);
  const justBelowDanger = evaluateZone(RECT, box(0.745));// 间隙 0.075 → 9.1%
  ok("余量 9.1%（<10%）→ danger", justBelowDanger.state === "danger", `ratio=${justBelowDanger.ratio.toFixed(3)}`);
}

console.log("== 2. 偏移中心：远离/接近双向可调 ==");
{
  const off = { ...RECT, center: { x: 0.3, z: 0 } };
  const s = evaluateZone(off, box(0.35));
  // 左边界 -0.52 → 间隙 0.17；右边界 1.12 → 间隙 0.77 → 取小 0.17
  ok("中心右移 → +X 侧余量变小", near(s.clearance, 0.17, 1e-9), `实际 ${s.clearance.toFixed(4)}`);
  const far = { ...RECT, center: { x: 0, z: 0 }, half: { x: 1.2, z: 1.2 } };
  const s2 = evaluateZone(far, box(0.35));
  ok("围栏变大 → 余量变大", s2.clearance > s.clearance, `${s2.clearance.toFixed(3)} > ${s.clearance.toFixed(3)}`);
}

console.log("== 3. 四点区域（旋转 45° 菱形）：法线方向正确 ==");
{
  const QUAD = {
    ...RECT, shape: "quad",
    corners: [[0, 1], [1, 0], [0, -1], [-1, 0]],   // 内切半径 1/√2
  };
  const hs = polygonHalfspaces(zonePolygon(QUAD));
  ok("生成 4 条边", hs.length === 4);
  // box(0.01) 的最近角是 (0.01,0.01)，到边 x+z=1 的距离 = (1-0.02)/√2
  const expect = (1 - 0.02) * Math.SQRT1_2;
  const c0 = clearanceToPolygon(box(0.01), hs);
  ok("中心小盒 → 间隙 = 内切半径 − 角点偏移", near(c0, expect, 1e-6),
    `实际 ${c0.toFixed(6)} 期望 ${expect.toFixed(6)}`);
  ok("基准 = 内切半径", near(basisOf(QUAD), Math.SQRT1_2, 1e-6), `实际 ${basisOf(QUAD).toFixed(6)}`);

  const outside = new THREE.Box3(new THREE.Vector3(0.9, 0, -0.05), new THREE.Vector3(1.1, 0.8, 0.05));
  const cOut = clearanceToPolygon(outside, hs);
  ok("越过 (1,0) 顶点 → 负间隙", cOut < 0, `实际 ${cOut.toFixed(4)}`);
  ok("越界 → hit", evaluateZone(QUAD, outside).state === "hit");

  const inside = new THREE.Box3(new THREE.Vector3(-0.2, 0, -0.2), new THREE.Vector3(0.2, 0.8, 0.2));
  ok("内部小盒 → safe", evaluateZone(QUAD, inside).state === "safe");
}

console.log("== 4. 圆形安全区：精确间隙 ==");
{
  const CIRC = { ...RECT, shape: "circle", radius: 0.9, center: { x: 0, z: 0 } };
  const c = clearanceToCircle(box(0.5), 0, 0, 0.9);
  ok("AABB 半宽 0.5 → 最远角 0.7071，间隙 0.1929",
    near(c, 0.9 - Math.hypot(0.5, 0.5), 1e-9), `实际 ${c.toFixed(6)}`);
  ok("基准 = 半径 0.9", near(basisOf(CIRC), 0.9, 1e-9));
  const s = evaluateZone(CIRC, box(0.5));
  ok("余量 21.4% → warn", s.state === "warn", `ratio=${s.ratio.toFixed(3)}`);
  ok("半径 1.5 → safe", evaluateZone({ ...CIRC, radius: 1.5 }, box(0.5)).state === "safe");
  ok("圆心外移 → 余量减小",
    evaluateZone({ ...CIRC, center: { x: 0.4, z: 0 } }, box(0.5)).clearance < s.clearance);
}

console.log("== 5. 阈值迁移（接近/远离双向） ==");
{
  const b = box(0.60);                       // ratio = 0.2683
  const base = evaluateZone(RECT, b);
  ok("warn=30% danger=10% → warn", base.state === "warn", `ratio=${base.ratio.toFixed(4)}`);

  const loose = evaluateZone({ ...RECT, thresholds: { ...RECT.thresholds, warn: 0.20 } }, b);
  ok("warn 调到 20%（更晚报警） → safe（远离）", loose.state === "safe");

  const tight = evaluateZone({ ...RECT, thresholds: { ...RECT.thresholds, warn: 0.60 } }, b);
  ok("warn 调到 60%（更早报警） → 仍 warn（接近）", tight.state === "warn");

  const dUp = evaluateZone({ ...RECT, thresholds: { ...RECT.thresholds, danger: 0.35 } }, b);
  ok("danger 调到 35% → danger", dUp.state === "danger");

  const fx = evaluateZone({ ...RECT, thresholds: { basis: "fixed", warn: 0.3, danger: 0.1, fixed_mm: 300 } }, b);
  ok("固定基准 300mm → ratio 0.733 → safe", fx.state === "safe", `ratio=${fx.ratio.toFixed(3)}`);

  const fx2 = evaluateZone({ ...RECT, thresholds: { basis: "fixed", warn: 0.9, danger: 0.8, fixed_mm: 300 } }, b);
  ok("固定基准 300mm + danger=80% → danger", fx2.state === "danger", `ratio=${fx2.ratio.toFixed(3)}`);
  const fx3 = evaluateZone({ ...RECT, thresholds: { basis: "fixed", warn: 0.9, danger: 0.5, fixed_mm: 300 } }, b);
  ok("同上 danger=50% → warn", fx3.state === "warn", `ratio=${fx3.ratio.toFixed(3)}`);
}

console.log("== 6. 多区域取最差 ==");
{
  const big = { ...RECT, id: "z1", name: "大区", half: { x: 1.2, z: 1.2 } };
  const small = { ...RECT, id: "z2", name: "小区", half: { x: 0.45, z: 0.45 } };
  const r1 = evaluateZone(big, box(0.35));
  const r2 = evaluateZone(small, box(0.35));
  ok("大区余量大 → safe", r1.state === "safe", `ratio=${r1.ratio.toFixed(3)}`);
  ok("小区余量小 → 更严重", r2.state === "warn" || r2.state === "danger",
    `state=${r2.state} ratio=${r2.ratio.toFixed(3)}`);
}

console.log("== 7. 地面碰撞分级（绿/黄/红/红闪，报警垫配色依据） ==");
{
  const g = { warn_mm: 100, danger_mm: 50, hit_mm: 20 };   // 阈值单位 mm，输入单位 m
  ok("离地 0.50m（>接近）→ safe", classifyGround(0.50, g) === "safe", `minY=0.500`);
  ok("离地 0.15m（>接近）→ safe", classifyGround(0.15, g) === "safe", `minY=0.150`);
  ok("离地 0.08m（<接近,>危险）→ warn", classifyGround(0.08, g) === "warn", `minY=0.080`);
  ok("离地 0.06m（<接近,>危险）→ warn", classifyGround(0.06, g) === "warn", `minY=0.060`);
  ok("离地 0.04m（<危险,>碰撞）→ danger", classifyGround(0.04, g) === "danger", `minY=0.040`);
  ok("离地 0.03m（<危险,>碰撞）→ danger", classifyGround(0.03, g) === "danger", `minY=0.030`);
  ok("离地 0.01m（<碰撞）→ hit", classifyGround(0.01, g) === "hit", `minY=0.010`);
  ok("离地 0.00m（触地）→ hit", classifyGround(0.0, g) === "hit");
  ok("离地 -0.05m（穿地）→ hit", classifyGround(-0.05, g) === "hit");
  // 边界取等号（<= 计入更严重档）
  ok("恰等于接近阈值 → warn", classifyGround(0.10, g) === "warn");
  ok("恰等于危险阈值 → danger", classifyGround(0.05, g) === "danger");
  ok("恰等于碰撞阈值 → hit", classifyGround(0.02, g) === "hit");
  ok("恰高于接近阈值 → safe", classifyGround(0.101, g) === "safe");
  // 阈值可调：三级整体收小
  const tight = { warn_mm: 60, danger_mm: 30, hit_mm: 10 };
  ok("阈值收到 60/30/10mm → 0.08m 变 safe", classifyGround(0.08, tight) === "safe");
  ok("同上 0.05m → warn", classifyGround(0.05, tight) === "warn");
  ok("同上 0.02m → danger", classifyGround(0.02, tight) === "danger");
  // 缺省阈值兜底（不传阈值也不报错）
  ok("缺省阈值兜底可用", ["safe", "warn", "danger", "hit"].includes(classifyGround(0.5, {})));
}

console.log("== 8. ★ 下发门控判据（点位/示教/程序按残影目标位姿拦截） ==");
{
  // 全站三处严重度比较（围栏取最差 / 残影泛红 / 执行域拦截）都建立在这张表上。
  // 顺序一旦被改动，"危险"就可能比"接近"还轻 —— 拦截会静默失效，界面却完全正常。
  ok("STATE_RANK 顺序严格递增 safe<warn<danger<hit",
    STATE_RANK.safe < STATE_RANK.warn && STATE_RANK.warn < STATE_RANK.danger
      && STATE_RANK.danger < STATE_RANK.hit,
    JSON.stringify(STATE_RANK));
  ok("STATE_RANK 只含四档、无多余键", Object.keys(STATE_RANK).length === 4);

  // 门控规则（与 stores/exec.js 的 gateAt / ghostUnsafe 同一表达式）：
  //   拦截 ⇔ rank(state) >= rank(danger)
  const blocks = (state) => STATE_RANK[state] >= STATE_RANK.danger;
  ok("safe → 放行", blocks("safe") === false);
  ok("warn（接近）→ 放行（接近不等于撞上，仍允许运动）", blocks("warn") === false);
  ok("danger → 拦截", blocks("danger") === true);
  ok("hit → 拦截", blocks("hit") === true);

  // 反证：判据不是恒真/恒假
  ok("反证：四个状态里既有放行也有拦截",
    ["safe", "warn", "danger", "hit"].map(blocks).includes(true)
      && ["safe", "warn", "danger", "hit"].map(blocks).includes(false));
}

console.log("== 9. ★ A6 评估健康三态（停摆不许显示\"安全\"） ==");
{
  // 背景：围栏余量原本只在渲染循环里算，渲染循环在页面隐藏/WebGL 上下文丢失时**彻底停帧**
  //   （黑屏第一根因的修法）。停帧 = 停评估 = `last` 冻在上一帧，界面可以继续显示"安全"
  //   而机器人正在真撞台面。这一节钉的是"不知道就说不知道"（§5.5 铁律③）。
  ok("三态词表齐全（never/fresh/stale/stopped，不是布尔）",
    Object.keys(HEALTH_LABEL).length === 4 && HEALTH_LABEL.fresh === "实时"
      && HEALTH_LABEL.stopped === "已停止");
  ok("阈值口径：fresh < 2s ≤ stale < 5s ≤ stopped",
    FRESH_MAX_MS === 2000 && FRESH_STOP_MS === 5000 && FRESH_MAX_MS < FRESH_STOP_MS,
    `${FRESH_MAX_MS}/${FRESH_STOP_MS}ms（后台 timer 被浏览器节流到 ≥1s）`);
  ok("1.5s → fresh", freshState(1500) === "fresh");
  ok("3s → stale（不是 safe 也不是危险）", freshState(3000) === "stale");
  ok("30s → stopped", freshState(30000) === "stopped");
  // ★ 反证①：不许把"没数据"塌成"新鲜"
  ok("null / NaN / 负数 → never（绝不塌成 fresh）",
    freshState(null) === "never" && freshState(NaN) === "never" && freshState(-5) === "never");
  ok("反证：三态判定不是恒真/恒假",
    new Set([0, 3000, 30000].map(freshState)).size === 3);

  const hStopped = evalHealth({ ageMs: 30000, ticks: 42 });
  ok("停摆时 ok=false 且话术写明\"数据不可信\"",
    hStopped.ok === false && hStopped.label === "已停止" && hStopped.notice.includes("不可信"),
    hStopped.notice);
  ok("停摆话术点名\"上一帧的安全不算数\"", hStopped.notice.includes("上一帧"));
  ok("新鲜时不产生任何停摆话术", evalHealth({ ageMs: 300, ticks: 42 }).notice === "");
  ok("title 带上单调评估次数（水位只增不减）", hStopped.title.includes("42"));

  // ★ 反证②：界面确实接了这个判据，而不是又多一套本地猜测
  const srcChip = readFileSync("src/components/SafetyChip.vue", "utf8");
  const srcApp = readFileSync("src/App.vue", "utf8");
  const srcPanel = readFileSync("src/components/SafetyPanel.vue", "utf8");
  const srcStore = readFileSync("src/stores/safety.js", "utf8");
  const srcMgr = readFileSync("src/three/manager.js", "utf8");
  ok("状态片按 health 切灰态（stalled），不再直接吃 last.state",
    srcChip.includes("stalled") && srcChip.includes("health.value.ok"));
  ok("报警条按 health 改口（healthStalled → bannerText）",
    srcApp.includes("healthStalled") && srcApp.includes("health.value.notice"));
  ok("调试面板顶部徽标同源（headState 用 safety.health）",
    srcPanel.includes("headState") && srcPanel.includes("safety.health"));
  ok("三态判定只有**一处**实现（各显示组件都从 safetyLabels 引用）",
    srcChip.includes("safetyLabels.js") && srcApp.includes("safetyLabels.js")
      && !/function freshState/.test(srcApp) && !/function freshState/.test(srcChip));
  ok("store 由外部时钟刷新（tickHealth 读 manager 的 ticks/at，不用\"结论变没变\"推断）",
    srcStore.includes("getSafetyHealth") && srcStore.includes("tickHealth"));
  ok("评估计数单调递增（safetyTicks += 1，没有清零路径）",
    /safetyTicks \+= 1/.test(srcMgr) && !/safetyTicks = 0;/.test(srcMgr.split("function updateSafety")[1] || ""));
  ok("停帧期间保留低频评估（setInterval + 只做 updateSafety 不 render）",
    srcMgr.includes("startIdleSafetyEval") && srcMgr.includes("setInterval")
      && srcApp.includes("startIdleSafetyEval()"));
  ok("低频评估在渲染循环正常时自动让位（不重复量 AABB）",
    /performance\.now\(\) - safetyTickAt < 200/.test(srcMgr));
}

console.log(`\n结果: ${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
