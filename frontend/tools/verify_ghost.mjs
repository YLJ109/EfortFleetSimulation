// =====================================================================
// 残影（Ghost）回归（headless，真实 THREE，无需 WebGL）
//
// 覆盖后续最容易回归的几点：
//  A. 残影必须是**独立的程序化模型**，尺寸与实体机同量级
//     —— 本项目踩过坑：clone 官方 GLB 单 mesh 会整体放大 1000 倍。
//     这里直接量包围盒，把"1000 倍"钉死成失败。
//  B. 造型：半透明、depthWrite=false（不遮挡实体机）、不可拾取、首帧不可见。
//  C. 运动：首次 setPose 立即落位；之后 setPose 只改目标，由 step() 逐帧收敛。
//
// 运行： node tools/verify_ghost.mjs
// =====================================================================
import * as THREE from "three";
import { createGhost, GHOST_OPACITY, GHOST_ALARM_COLOR,
         GHOST_COLOR, GHOST_COLOR_B } from "../src/three/ghost.js";
import { buildRobot } from "../src/robot/robotModel.js";
import { FALLBACK_DH } from "../src/config.js";
import { EE_META } from "../src/three/endEffector.js";

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const boxOf = (root) => {
  root.updateMatrixWorld(true);
  return new THREE.Box3().setFromObject(root);
};
/** 统计残影里的材质数量（用于"每个材质都满足约束"的断言）。 */
const matsCount = (g) => {
  let n = 0;
  g.root.traverse((o) => { if (o.isMesh) n++; });
  return n;
};
/** 是否所有材质都满足条件（残影的渲染约束是**每个材质都得满足**）。 */
const matAll = (g, fn) => {
  let all = true;
  g.root.traverse((o) => {
    if (!o.isMesh) return;
    (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => {
      if (m && !fn(m)) all = false;
    });
  });
  return all;
};
const sizeOf = (root) => {
  const b = boxOf(root);
  const s = new THREE.Vector3();
  b.getSize(s);
  return s;
};
const maxDiff = (a, b) => Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y), Math.abs(a.z - b.z));

console.log("== 残影 Ghost 回归 ==");

// ---------- A. 独立程序化模型 + 尺寸量级 ----------
const ghost = createGhost(FALLBACK_DH, "floor", 0);
const ref = buildRobot(FALLBACK_DH, "floor");

ok("root 名为 ghost-root（独立节点，不混进实体机）", ghost.root.name === "ghost-root");
ok("mesh 数量 > 0", ghost.meshCount > 0, "meshCount=" + ghost.meshCount);

const gs = sizeOf(ghost.root);
const rs = sizeOf(ref.root);
// 逐轴比值：0.5~2 视为同量级；1000 倍会直接掉出去
const ratios = [gs.x / rs.x, gs.y / rs.y, gs.z / rs.z];
ok("尺寸与实体机同量级（非 1000× 放大）",
  ratios.every((r) => r > 0.5 && r < 2),
  "ghost=" + gs.toArray().map((v) => v.toFixed(3)) + " ref=" + rs.toArray().map((v) => v.toFixed(3)));

// ---------- A2. J6 末端工具必须真的挂在残影上 ----------
// ★ 现场事故：残影自己手写了一份简化版末端工具，写着写着就和官方不一致 →
//   "残影跟随看不到 J6 上的工具"。现在官方与残影**共用 endEffector.buildEndEffector()**，
//   这里把它钉死：节点要在、尺寸要对（不能 1000×）、而且要露在法兰**前方**（不能被埋进腕筒）。
const ee = ghost.root.getObjectByName("end-effector");
ok("残影包含 J6 末端工具（end-effector 节点）", !!ee);
if (ee) {
  const es = sizeOf(ee);
  const emax = Math.max(es.x, es.y, es.z);
  ok("末端工具尺寸合理（0.05~0.8 m，未 1000× 放大）", emax > 0.05 && emax < 0.8,
    "ee=" + es.toArray().map((v) => v.toFixed(3)));
  const flange = ghost.root.getObjectByName("Flange");
  ok("残影存在 J6 法兰节点（工具挂载点）", !!flange);
  if (flange) {
    ghost.root.updateMatrixWorld(true);
    const inv = new THREE.Matrix4().copy(flange.matrixWorld).invert();
    const lb = new THREE.Box3().setFromObject(ee).applyMatrix4(inv);
    ok("末端工具整体在法兰前方（z >= -0.005 m，未被埋进腕部）", lb.min.z >= -0.005,
      "min.z=" + lb.min.z.toFixed(4));
    // ★★ 2026-10-08 现场三改的钉子（用户："J6工具有点太大 / 吸盘要向前一点 /
    //    J6工具没有连接在J6上，还有个什么间隙"）
    ok("★ 工具后缘**贴到法兰面**（min.z ≤ 2mm）—— 修掉「悬在法兰前 16mm 的缝」",
      lb.min.z <= 0.002, "min.z=" + lb.min.z.toFixed(4));
    // ★ 2026-10-09：判据从"钉死具体比例"改成**钉不变量**。
    //   原因：`EE_SCALE` 是**设计参数**，会随现场调整（1.5 → 1.2 是被正常调小的），
    //   钉死数值会让"别人合法调参"变成测试失败（假警报）。
    //   真正该守的不变量只有两条：
    //     · 工具整体尺寸不能大到离谱（不许变回 EE_SCALE=2 那种大工具）；
    //     · 比例本身仍在"设计单位→真实 mm"的合理量级内。
    ok("★ 工具尺寸在上限内（≤0.18m）且缩放比例未反弹回大尺寸",
      emax <= 0.18 && EE_META.scale <= 1.6 && EE_META.scale > 0.5,
      `emax=${emax.toFixed(3)} scale=${EE_META.scale}`);
    ok("★ 吸盘向前移了（cupFwd > 0，不再摆在立板正中）", EE_META.cupFwd > 0);
    // ⚠ 单位：back(设计单位) × scale = **mm**；别拿它跟"米"比（第一版就写错了）
    const backMm = EE_META.back * EE_META.scale;
    ok("★ 后缘压进法兰但不埋进腕部（-5mm ≤ 后缘 < 0mm）",
      backMm < 0 && backMm >= -5, `后缘 = ${backMm} mm`);
  }
}

// ---------- B. 造型：半透明 / 不写深度 / 不可拾取 / 初始不可见 ----------
let meshes = 0, badMat = 0, badDepth = 0, pickable = 0;
ghost.root.traverse((o) => {
  if (!o.isMesh) return;
  meshes++;
  const mats = Array.isArray(o.material) ? o.material : [o.material];
  for (const m of mats) {
    if (!m.transparent || Math.abs(m.opacity - GHOST_OPACITY) > 1e-6) badMat++;
    if (m.depthWrite !== false) badDepth++;
  }
  // 不可拾取：raycast 必须是空实现
  if (typeof o.raycast === "function" && o.raycast.toString().indexOf("three") !== -1) pickable++;
});
ok("所有材质均为半透明且 opacity = " + GHOST_OPACITY, badMat === 0, "badMat=" + badMat);
ok("所有材质 depthWrite=false（不遮挡实体机）", badDepth === 0, "badDepth=" + badDepth);
ok("残影不可拾取（raycast 已禁用）", pickable === 0, "pickable=" + pickable);
ok("初始不可见（未预演时不显示）", ghost.visible === false);

// ---------- C. 运动：首次落位 + 之后平滑收敛 ----------
const POSE_A = [0, 0, 0, 0, 0, 0];
const POSE_B = [40, 30, -20, 15, 25, 60];

ghost.setPose(POSE_A);
ok("setPose 后残影可见", ghost.visible === true);
const boxA = boxOf(ghost.root);

ghost.setPose(POSE_B);              // 只改目标：应**不**立即跳变
const boxB0 = boxOf(ghost.root);
ok("第二次 setPose 不立即跳变（仍停在旧位姿）", maxDiff(
  boxA.getSize(new THREE.Vector3()), boxB0.getSize(new THREE.Vector3())) < 1e-6);

// 逐步推进 → 应该明显移动
for (let i = 0; i < 4; i++) ghost.step(1 / 60);
const boxMid = boxOf(ghost.root);
ok("step() 推进后确实在移动", maxDiff(
  boxB0.getSize(new THREE.Vector3()), boxMid.getSize(new THREE.Vector3())) > 1e-4);

// 持续推进 → 收敛到目标（与"直接落位到 B"的几何一致）
for (let i = 0; i < 240; i++) ghost.step(1 / 60);
const boxB = boxOf(ghost.root);
const probe = createGhost(FALLBACK_DH, "floor", 0);
probe.setPose(POSE_B);
const boxBRef = boxOf(probe.root);
ok("收敛到目标位姿（与直接落位几何一致，误差 < 1e-3 m）",
  maxDiff(boxB.getSize(new THREE.Vector3()), boxBRef.getSize(new THREE.Vector3())) < 1e-3,
  "diff=" + maxDiff(boxB.getSize(new THREE.Vector3()), boxBRef.getSize(new THREE.Vector3())).toExponential(2));

// 隐藏 / 显示
ghost.setVisible(false);
ok("setVisible(false) 生效", ghost.visible === false);
ghost.setVisible(true);
ok("setVisible(true) 生效", ghost.visible === true);

// ---------- D. ★ 下发判据：withTarget 必须量"目标位姿"，且量完不跳变 ----------
// 这是"点位执行按残影目标位姿判定"的地基：manager.evalSafety() 把围栏/地面测量
// 塞进 withTarget 里执行，量到的必须是**终点**（要去的地方），不是"飘到一半"的位置。
// 一旦这里量错，拦截判据就会系统性偏松或偏严，而且画面看不出任何异常。
console.log("\n[D] withTarget 量目标位姿 + 量完还原动画姿态");
{
  const g = createGhost(FALLBACK_DH, "floor", 0);
  g.setPose(POSE_A);                       // 首次落位：当前 = A
  g.setPose(POSE_B);                       // 只改目标：当前仍停在 A，动画未推进
  const boxAnimated = boxOf(g.root);

  const measured = g.withTarget((root) => boxOf(root));
  const refB = createGhost(FALLBACK_DH, "floor", 0);
  refB.setPose(POSE_B);
  const boxB = boxOf(refB.root);

  ok("withTarget 量到的是**目标位姿**(B)，不是动画中的当前位姿(A)",
    maxDiff(measured.getSize(new THREE.Vector3()), boxB.getSize(new THREE.Vector3())) < 1e-6,
    "measured=" + measured.getSize(new THREE.Vector3()).toArray().map((v) => v.toFixed(3)));
  ok("withTarget 量到的**不是**当前动画位姿(A)（否则判据形同虚设）",
    maxDiff(measured.getSize(new THREE.Vector3()), boxAnimated.getSize(new THREE.Vector3())) > 1e-3);

  const boxAfter = boxOf(g.root);
  ok("withTarget 返回后立刻还原动画姿态（视觉零跳变）",
    maxDiff(boxAfter.getSize(new THREE.Vector3()), boxAnimated.getSize(new THREE.Vector3())) < 1e-9,
    "diff=" + maxDiff(boxAfter.getSize(new THREE.Vector3()), boxAnimated.getSize(new THREE.Vector3())).toExponential(2));
  g.dispose();
}

// ---------- E. ★ 报警着色：泛红只改颜色，不许破坏"半透明/不遮挡" ----------
console.log("\n[E] setAlarm 泛红（且不动透明与深度写入）");
{
  const g = createGhost(FALLBACK_DH, "floor", 0);
  g.setPose(POSE_A);
  const matsOf = () => {
    const out = [];
    g.root.traverse((o) => {
      if (!o.isMesh) return;
      (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => out.push(m));
    });
    return out;
  };
  const hexOf = (m) => (m.color ? m.color.getHex() : null);
  const before = matsOf().map(hexOf);

  ok("初始 alarmed = false", g.alarmed === false);
  g.setAlarm(true);
  ok("setAlarm(true) 后 alarmed = true", g.alarmed === true);
  const during = matsOf().map(hexOf);
  ok("泛红：所有材质基色都变成报警色 " + GHOST_ALARM_COLOR.toString(16),
    during.length > 0 && during.every((h) => h === GHOST_ALARM_COLOR),
    "得到 " + [...new Set(during)].map((h) => h && h.toString(16)).join(","));
  ok("泛红后仍全部半透明且 opacity 不变（硬约束）",
    matsOf().every((m) => m.transparent && Math.abs(m.opacity - GHOST_OPACITY) < 1e-6));
  ok("泛红后仍 depthWrite=false（不遮挡实体机）", matsOf().every((m) => m.depthWrite === false));

  g.setAlarm(false);
  ok("setAlarm(false) 还原淡蓝", matsOf().map(hexOf).join(",") === before.join(","));
  ok("还原后 alarmed = false", g.alarmed === false);
  g.setAlarm(false);
  ok("重复 setAlarm(false) 幂等（不产生副作用）", g.alarmed === false);
  g.dispose();
}

// ---------------------------------------------------------------------
console.log("");
console.log("E. 双残影（对比用）：第二个残影必须与第一个同构但异色");
// ---------------------------------------------------------------------
{
  const hexOf = (g) => {
    let h = null;
    g.root.traverse((o) => {
      if (h !== null || !o.isMesh) return;
      const m = Array.isArray(o.material) ? o.material[0] : o.material;
      if (m && m.color) h = m.color.getHex();
    });
    return h;
  };
  const a = createGhost(FALLBACK_DH, "floor", 0);
  const b = createGhost(FALLBACK_DH, "floor", 0, GHOST_COLOR_B);

  const sa = sizeOf(a.root), sb = sizeOf(b.root);
  ok("B 与 A 包围盒同量级（同构，不是被缩放的克隆）",
     Math.abs(sa.x - sb.x) < 1e-6 && Math.abs(sa.y - sb.y) < 1e-6
     && Math.abs(sa.z - sb.z) < 1e-6, JSON.stringify([sa, sb]));

  ok("A 用淡蓝 GHOST_COLOR", hexOf(a) === GHOST_COLOR, hexOf(a));
  ok("B 用淡橙 GHOST_COLOR_B", hexOf(b) === GHOST_COLOR_B, hexOf(b));
  ok("A/B 颜色必须不同（同屏要能一眼分开）", hexOf(a) !== hexOf(b));

  ok("B 也是半透明且不写深度（与 A 同一套渲染约束）",
     matsCount(b) > 0 && matAll(b, (m) => m.transparent && m.depthWrite === false),
     "mats=" + matsCount(b));

  ok("B 默认不可见（由界面按需打开，不自己乱显）", b.visible === false, String(b.visible));

  // ★ 别写 ok(..., true) 这种恒真断言 —— 那等于没测。这里真调一遍并捕获异常。
  let threw = "";
  try { a.dispose(); b.dispose(); } catch (e) { threw = String(e); }
  ok("两个残影都能 dispose（不抛异常）", threw === "", threw);
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
